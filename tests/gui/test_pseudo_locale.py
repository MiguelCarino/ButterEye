# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Pseudo-locale sweep (docs/design/GUI.md §6 ``test_pseudo_locale``).

A translator that wraps every GUI string as ``[!!...!!]`` is installed before
the window is built. Then no visible label, button, header, tab, menu entry or
sidebar item may show a catalogued GUI source string untranslated (a missing
``tr()``), the shell's own strings must all be pseudo-translated, and the
longer texts must still fit: pages scroll instead of clipping.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QTranslator
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QGroupBox,
    QHeaderView,
    QLabel,
    QListWidget,
    QTabBar,
    QWidget,
)

from buttereye.gui.a11y import plain

REPO = Path(__file__).resolve().parents[2]
PREFIX = "[!!"


class PseudoTranslator(QTranslator):
    def translate(
        self, context: str, source: str, disambiguation: str | None = None, n: int = -1
    ) -> str:
        if not source:
            return source
        return f"{PREFIX}{source}!!]"

    def isEmpty(self) -> bool:  # noqa: N802 - Qt API
        return False


def _catalogue_sources() -> set[str]:
    spec = importlib.util.spec_from_file_location("extract_ts", REPO / "tools" / "extract_ts.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["extract_ts"] = mod
    spec.loader.exec_module(mod)
    cat = mod.extract()
    return {plain(src) for (_ctx, src, _c) in cat.entries if "{" not in src and len(src) > 1}


def _visible_texts(root: QWidget) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root):
            continue
        if isinstance(w, QLabel) and w.text():
            out.append((f"QLabel {w.objectName()}", w.text()))
        elif isinstance(w, QAbstractButton) and w.text():
            out.append((f"{type(w).__name__} {w.objectName()}", w.text()))
        elif isinstance(w, QGroupBox) and w.title():
            out.append(("QGroupBox", w.title()))
        elif isinstance(w, QTabBar):
            out += [("tab", w.tabText(i)) for i in range(w.count())]
        elif isinstance(w, QListWidget):
            out += [("item", w.item(i).text()) for i in range(w.count())]
        elif isinstance(w, QHeaderView) and isinstance(w.model(), object) and w.model():
            m = w.model()
            orient = w.orientation()
            out += [
                ("header", str(m.headerData(i, orient)))
                for i in range(m.columnCount() if orient.name == "Horizontal" else m.rowCount())
                if m.headerData(i, orient)
            ]
    return out


@pytest.fixture
def pseudo(qapp: QApplication) -> Any:
    tr = PseudoTranslator()
    qapp.installTranslator(tr)
    try:
        yield tr
    finally:
        qapp.removeTranslator(tr)


@pytest.mark.parametrize("scenario", ["devbox_now", "all_ready"])
def test_no_untranslated_gui_strings(
    pseudo: Any, make_window: Any, qtbot: Any, scenario: str
) -> None:
    assert QCoreApplication.translate("Any", "probe").startswith(PREFIX)
    sources = _catalogue_sources()
    win = make_window(scenario)
    problems: list[str] = []
    for spec_index in range(len(win.pages)):
        win.sidebar.setCurrentRow(spec_index)
        qtbot.wait(150)
        for where, text in _visible_texts(win.centralWidget()):
            if text.startswith(PREFIX):
                continue
            if plain(text) in sources:
                problems.append(f"page {spec_index}: {where}: {text!r}")
    assert not problems, "\n".join(problems)


def test_shell_strings_are_pseudo_translated(pseudo: Any, make_window: Any) -> None:
    win = make_window("devbox_now")
    for i in range(win.sidebar.count()):
        assert win.sidebar.item(i).text().startswith(PREFIX)
    for menu in win.menus:
        assert menu.title().startswith(PREFIX), menu.title()
        for act in menu.actions():
            if not act.isSeparator():
                assert act.text().startswith(PREFIX), act.text()
    assert win.sidebar.accessibleName().startswith(PREFIX)
    assert win.status_label.accessibleName().startswith(PREFIX)


def test_long_texts_scroll_instead_of_clipping(pseudo: Any, make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    win.resize(640, 480)
    for i, page in enumerate(win.pages):
        win.sidebar.setCurrentRow(i)
        qtbot.wait(100)
        scroll = page.parentWidget().parentWidget()
        assert scroll.widgetResizable()
        hint = page.minimumSizeHint()
        if hint.isValid():
            assert page.width() >= hint.width() - 1, type(page).__name__
            assert page.height() >= hint.height() - 1, type(page).__name__
        for view in page.findChildren(QAbstractItemView):
            # A deliberately hidden view (e.g. a table's row-number header) has no
            # geometry to clip; only views the user can actually see must have width.
            if not view.isVisibleTo(page):
                continue
            assert view.width() > 0, f"{type(page).__name__}: {view.objectName()}"
