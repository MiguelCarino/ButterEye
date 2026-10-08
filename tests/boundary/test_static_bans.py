# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""AST bans (docs/design/GUI.md §6 ``test_static_bans``, R10, R13, F17, F20, §4.0).

- ``core/`` never imports Qt;
- ``gui/`` imports from the core only ``buttereye.core.api``;
- no ``subprocess``/``socket``/``asyncio.create_subprocess*`` in ``gui/``;
- no ``preexec_fn`` anywhere in ``buttereye/``;
- network modules only in ``core/plugins/download.py`` (F20);
- no ``QSystemTrayIcon``, no colour literals (``setStyleSheet`` colours,
  ``QColor`` literals, ``Qt.GlobalColor``), no ``setPixelSize``, no constant
  ``setFixed*`` in ``gui/``;
- ``buttereye.core.testing`` (FakeCore) is never imported by shipped code;
- every source file carries the SPDX header.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PKG = REPO / "buttereye"
CORE = PKG / "core"
GUI = PKG / "gui"
DOWNLOAD = CORE / "plugins" / "download.py"
TESTING = CORE / "testing"

NETWORK_MODULES = frozenset(
    {
        "urllib",
        "http",
        "ssl",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "xmlrpc",
        "requests",
        "httpx",
        "aiohttp",
        "urllib3",
        "websockets",
        "socketserver",
    }
)
NETWORK_CALLS = frozenset({"open_connection", "create_connection", "start_server"})
NETWORK_ATTRS = frozenset({"AF_INET", "AF_INET6"})
QT_ROOTS = ("PySide6", "shiboken6", "PyQt5", "PyQt6", "PySide2")
COLOUR_CSS = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|hsla?\(|\bcolor\s*:|background", re.I)
#: The one dynamic import of the test doubles: the BUTTEREYE_DEV=1 ``--fake`` flag.
DEV_FAKE_IMPORT = (GUI / "app.py", "buttereye.core.testing")


def py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            for a in node.names:
                yield node.lineno, f"{node.module}.{a.name}"


def dynamic_imports(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
        if name in ("import_module", "__import__"):
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                yield node.lineno, arg.value


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def test_core_never_imports_qt() -> None:
    bad = [
        f"{_rel(p)}:{line} {mod}"
        for p in py_files(CORE)
        for line, mod in imports(parse(p))
        if mod.split(".")[0] in QT_ROOTS
    ]
    bad += [
        f"{_rel(p)}:{line} {mod} (dynamic)"
        for p in py_files(CORE)
        for line, mod in dynamic_imports(parse(p))
        if mod.split(".")[0] in QT_ROOTS
    ]
    assert not bad, bad


def test_gui_imports_only_the_core_api() -> None:
    bad: list[str] = []
    for p in py_files(GUI):
        tree = parse(p)
        for line, mod in imports(tree):
            if mod.startswith("buttereye.core") and not (
                mod == "buttereye.core" or mod.startswith("buttereye.core.api")
            ):
                bad.append(f"{_rel(p)}:{line} {mod}")
            if mod in ("buttereye", "buttereye.__version__"):
                bad.append(f"{_rel(p)}:{line} {mod} (use legal_notices().version)")
        for line, mod in dynamic_imports(tree):
            if (p, mod) == DEV_FAKE_IMPORT:
                continue
            if mod.startswith("buttereye.core") and not mod.startswith("buttereye.core.api"):
                bad.append(f"{_rel(p)}:{line} {mod} (dynamic)")
    assert not bad, bad


def test_gui_from_core_imports_name_the_api_module() -> None:
    """``from buttereye.core import X`` is allowed only for X == api."""
    bad: list[str] = []
    for p in py_files(GUI):
        for node in ast.walk(parse(p)):
            if isinstance(node, ast.ImportFrom) and node.module == "buttereye.core":
                bad += [f"{_rel(p)}:{node.lineno} {a.name}" for a in node.names if a.name != "api"]
    assert not bad, bad


def test_gui_spawns_nothing_and_opens_no_sockets() -> None:
    bad: list[str] = []
    for p in py_files(GUI):
        tree = parse(p)
        for line, mod in imports(tree):
            if mod.split(".")[0] in ("subprocess", "socket", "multiprocessing", "pty"):
                bad.append(f"{_rel(p)}:{line} import {mod}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and (
                node.attr.startswith("create_subprocess")
                or node.attr in ("open_unix_connection", "open_connection", "system", "popen")
                and isinstance(node.value, ast.Name)
                and node.value.id in ("asyncio", "os")
            ):
                bad.append(f"{_rel(p)}:{node.lineno} {node.attr}")
            if isinstance(node, ast.Attribute) and node.attr in ("startDetached", "QProcess"):
                bad.append(f"{_rel(p)}:{node.lineno} {node.attr}")
            if isinstance(node, ast.Name) and node.id == "QProcess":
                bad.append(f"{_rel(p)}:{node.lineno} QProcess")
    assert not bad, bad


def test_no_preexec_fn_anywhere() -> None:
    bad = [
        f"{_rel(p)}:{node.lineno}"
        for p in py_files(PKG)
        for node in ast.walk(parse(p))
        if isinstance(node, ast.keyword) and node.arg == "preexec_fn"
    ]
    assert not bad, bad


def test_network_code_only_in_download_module() -> None:
    bad: list[str] = []
    for p in py_files(PKG):
        if p == DOWNLOAD:
            continue
        tree = parse(p)
        for line, mod in imports(tree):
            if mod.split(".")[0] in NETWORK_MODULES:
                bad.append(f"{_rel(p)}:{line} import {mod}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in NETWORK_CALLS | NETWORK_ATTRS:
                bad.append(f"{_rel(p)}:{node.lineno} {node.attr}")
    assert not bad, bad


def test_gui_has_no_tray_and_no_colour_or_pixel_literals() -> None:
    bad: list[str] = []
    for p in py_files(GUI):
        for node in ast.walk(parse(p)):
            where = f"{_rel(p)}:{getattr(node, 'lineno', '?')}"
            if isinstance(node, ast.Name | ast.Attribute):
                name = node.id if isinstance(node, ast.Name) else node.attr
                if name == "QSystemTrayIcon":
                    bad.append(f"{where} QSystemTrayIcon")
            if isinstance(node, ast.Attribute) and _global_colour(node):
                bad.append(f"{where} Qt colour literal {node.attr}")
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            fname = (
                f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else ""
            )
            consts = [a for a in node.args if isinstance(a, ast.Constant)]
            if fname == "setStyleSheet":
                for a in ast.walk(node):
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        if COLOUR_CSS.search(a.value):
                            bad.append(f"{where} setStyleSheet colour {a.value!r}")
            elif fname == "setPixelSize":
                bad.append(f"{where} setPixelSize")
            elif fname in ("setFixedWidth", "setFixedHeight", "setFixedSize") and (
                consts or any(_constant_size(a) for a in node.args)
            ):
                bad.append(f"{where} {fname} with a constant")
            elif fname in ("QColor", "fromRgb", "fromRgbF", "fromHsv", "fromString") and consts:
                bad.append(f"{where} {fname} colour literal")
    assert not bad, bad


#: Qt.GlobalColor values that are not colours (transparent fills, bitmap bits).
NOT_COLOURS = frozenset({"transparent", "color0", "color1"})
QT_COLOUR_NAMES = frozenset(
    {
        "black",
        "white",
        "red",
        "green",
        "blue",
        "cyan",
        "magenta",
        "yellow",
        "gray",
        "darkGray",
        "lightGray",
        "darkRed",
        "darkGreen",
        "darkBlue",
        "darkCyan",
        "darkMagenta",
        "darkYellow",
    }
)


def _global_colour(node: ast.Attribute) -> bool:
    owner = node.value
    if isinstance(owner, ast.Attribute) and owner.attr == "GlobalColor":
        return node.attr not in NOT_COLOURS
    return isinstance(owner, ast.Name) and owner.id == "Qt" and node.attr in QT_COLOUR_NAMES


def _constant_size(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "QSize"
        and all(isinstance(a, ast.Constant) for a in node.args)
    )


def test_test_doubles_never_imported_by_shipped_code() -> None:
    bad: list[str] = []
    for p in py_files(PKG):
        if TESTING in p.parents:
            continue
        tree = parse(p)
        for line, mod in imports(tree):
            if mod.startswith("buttereye.core.testing"):
                bad.append(f"{_rel(p)}:{line} {mod}")
        for line, mod in dynamic_imports(tree):
            if mod.startswith("buttereye.core.testing") and (p, mod) != DEV_FAKE_IMPORT:
                bad.append(f"{_rel(p)}:{line} {mod} (dynamic)")
    assert not bad, bad


def test_dev_fake_import_is_guarded_by_the_dev_flag() -> None:
    source = (GUI / "app.py").read_text(encoding="utf-8")
    assert "BUTTEREYE_DEV" in source
    assert 'importlib.import_module("buttereye.core.testing")' in source


@pytest.mark.parametrize("path", py_files(GUI) + [REPO / "tools" / "extract_ts.py"], ids=_rel)
def test_spdx_header(path: Path) -> None:
    head = path.read_text(encoding="utf-8").splitlines()[:2]
    assert any(line.startswith("# SPDX-License-Identifier: AGPL-3.0-or-later") for line in head)
