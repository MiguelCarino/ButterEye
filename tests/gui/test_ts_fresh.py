# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The committed Qt TS catalogue matches ``tools/extract_ts.py`` (R3, §6)."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
TS = REPO / "buttereye" / "data" / "i18n" / "buttereye_en.ts"


def _tool() -> Any:
    spec = importlib.util.spec_from_file_location("extract_ts", REPO / "tools" / "extract_ts.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["extract_ts"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_committed_catalogue_is_fresh() -> None:
    tool = _tool()
    expected = tool.to_ts(tool.extract())
    assert TS.exists(), "run: python tools/extract_ts.py"
    assert TS.read_text(encoding="utf-8") == expected, "stale catalogue: run tools/extract_ts.py"


def test_check_mode_cli() -> None:
    res = subprocess.run(
        [sys.executable, "-I", str(REPO / "tools" / "extract_ts.py"), "--check"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stderr


def test_every_present_page_has_a_context() -> None:
    tool = _tool()
    contexts = set(tool.contexts_in_ts(TS.read_text(encoding="utf-8")))
    assert {"PageRegistry", "MainWindow", "QuitDialog", "Page"} <= contexts
    from buttereye.gui.pages import PAGE_REGISTRY, module_present

    for spec in PAGE_REGISTRY:
        if not module_present(spec):
            continue
        origin = importlib.util.find_spec(spec.module)
        assert origin is not None and origin.origin is not None
        tree = ast.parse(Path(origin.origin).read_text(encoding="utf-8"))
        classes = {n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
        assert classes & contexts, f"{spec.module}: no translatable strings in the catalogue"


def test_page_titles_are_catalogued() -> None:
    tool = _tool()
    cat = tool.extract()
    from buttereye.gui.pages import PAGE_REGISTRY, TITLE_CONTEXT

    for spec in PAGE_REGISTRY:
        assert (TITLE_CONTEXT, spec.title, "") in cat.entries


@pytest.mark.parametrize(
    ("source", "context", "text"),
    [
        ("class A:\n    def f(self):\n        return self.tr('Hello')\n", "A", "Hello"),
        ("x = QCoreApplication.translate('Ctx', 'Hi')\n", "Ctx", "Hi"),
        ("x = QT_TRANSLATE_NOOP('Reg', 'Play')\n", "Reg", "Play"),
        (
            "def _t(text):\n    return QCoreApplication.translate('H', text)\n"
            "x = _t('Via helper')\n",
            "H",
            "Via helper",
        ),
        (
            "CTX = 'Const'\ndef _t(text):\n    return QCoreApplication.translate(CTX, text)\n"
            "x = _t('Via constant context')\n",
            "Const",
            "Via constant context",
        ),
        (
            "CTX = 'Cols'\nCOLUMNS = ('Source', 'Output')\n"
            "def _t(text):\n    return QCoreApplication.translate(CTX, text)\n"
            "def f():\n    for i, h in enumerate(COLUMNS):\n        _t(h)\n",
            "Cols",
            "Output",
        ),
        (
            "class B:\n    @staticmethod\n    def ts(text):\n"
            "        return QCoreApplication.translate('B', text)\n"
            "    def g(self):\n        return B.ts('Static')\n",
            "B",
            "Static",
        ),
    ],
)
def test_extractor_patterns(tmp_path: Path, source: str, context: str, text: str) -> None:
    tool = _tool()
    pkg = REPO / "buttereye" / "gui"
    # the extractor resolves module names relative to the repo; use a temp copy root
    root = tmp_path / "gui"
    root.mkdir()
    (root / "m.py").write_text(source, encoding="utf-8")
    tool_repo = tool.REPO
    try:
        tool.REPO = tmp_path
        cat = tool.extract(root)
    finally:
        tool.REPO = tool_repo
    assert (context, text, "") in cat.entries
    assert pkg.exists()


def test_xml_is_well_formed_and_escaped() -> None:
    tool = _tool()
    cat = tool.Catalogue()
    cat.add(tool.Message("Ctx", 'a < b & "c"'), "f.py")
    text = tool.to_ts(cat)
    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    assert root.find("context/message/source").text == 'a < b & "c"'  # type: ignore[union-attr]


def _extract_source(tmp_path: Path, source: str) -> tuple[Any, list[str]]:
    tool = _tool()
    root = tmp_path / "gui"
    root.mkdir(exist_ok=True)
    (root / "m.py").write_text(source, encoding="utf-8")
    tool_repo = tool.REPO
    try:
        tool.REPO = tmp_path
        return tool.extract(root), tool.unresolved(root)
    finally:
        tool.REPO = tool_repo


def test_numerus_calls_are_marked_plural(tmp_path: Path) -> None:
    cat, problems = _extract_source(
        tmp_path,
        "x = QCoreApplication.translate('C', '%n file(s)', None, n)\n"
        "class A:\n    def f(self, n):\n        return self.tr('%n row(s)', None, n)\n",
    )
    assert not problems
    assert cat.entries[("C", "%n file(s)", "")][0] is True
    assert cat.entries[("A", "%n row(s)", "")][0] is True


def test_unresolvable_calls_are_reported(tmp_path: Path) -> None:
    _cat, problems = _extract_source(
        tmp_path,
        "def _t(text):\n    return QCoreApplication.translate('H', text)\n"
        "def f(name):\n    return _t(name)\n"
        "def g(ctx):\n    return QCoreApplication.translate(ctx, 'x')\n"
        "def h(t):\n    return _t(t)  # i18n: dynamic\n",
    )
    assert len(problems) == 2, problems
    assert any("source text" in p for p in problems)
    assert any("context" in p for p in problems)


#: Files of other areas with calls the extractor can't resolve yet (reported):
#: render.py's _button(name) passes accessible names through _t(name).
_UNRESOLVED_PENDING: tuple[str, ...] = ()


def test_every_gui_translate_call_is_resolvable() -> None:
    """A translate()/helper call whose context or text isn't a literal never
    reaches the catalogue (bench/render headers, job states, verdicts did not)."""
    tool = _tool()
    problems = [p for p in tool.unresolved() if not p.startswith(_UNRESOLVED_PENDING)]
    assert not problems, "\n".join(problems)


def test_catalogue_holds_module_constant_contexts_and_columns() -> None:
    tool = _tool()
    cat = tool.extract()
    for key in (
        ("BenchPage", "Not measured", ""),
        ("RenderPage", "Queued", ""),
        ("RenderPage", "Encoder", ""),  # COLUMNS header
        ("RenderJobDialog", "&Replace", ""),
    ):
        assert key in cat.entries, key


def test_plural_messages_are_numerus_entries() -> None:
    text = TS.read_text(encoding="utf-8")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    numerus = {
        (c.findtext("name"), m.findtext("source"))
        for c in root.iter("context")
        for m in c.iter("message")
        if m.get("numerus") == "yes"
    }
    for key in (
        ("FindingsView", "%n problem(s)"),
        ("FindingsView", "%n check(s) passed"),
        ("QuitDialog", "Detaching from %n player(s)…"),
        ("MainWindow", "ButterEye found %n player(s) it started earlier."),
    ):
        assert key in numerus, key


def test_source_plurals_give_english_forms(qapp: Any) -> None:
    from buttereye.gui.a11y import install_source_plurals
    from buttereye.gui.widgets.findings_view import summary_text

    install_source_plurals(qapp)
    assert summary_text(()) == "0 problems, 0 warnings, 0 checks passed"
    from PySide6.QtCore import QCoreApplication

    assert QCoreApplication.translate("X", "%n player(s)", None, 1) == "1 player"
    assert QCoreApplication.translate("X", "%n player(s)", None, 2) == "2 players"
    assert QCoreApplication.translate("X", "No plural here") == "No plural here"
