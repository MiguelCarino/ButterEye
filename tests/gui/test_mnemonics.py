# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Alt mnemonics (docs/design/GUI.md §4.0, §8 keyboard).

On every page, each visible mnemonic letter is used once in the window, and
the menu-bar letters (F, P, T, G, H) are never taken by a page widget: Alt+T on
Bench must open the Tools menu, not focus "Target rate". Where two widgets
share a letter, Alt only cycles focus between them and activates neither.

Shared-widget internals are left out here and reported for the widget owner:
``CopyField``'s "&Copy" button (one per command field, so it repeats on most
pages) and ``Banner``'s "Show &details" toggle.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import pytest
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QGroupBox,
    QLabel,
    QTabBar,
    QTabWidget,
    QWidget,
)

from buttereye.gui.pages import PAGE_REGISTRY
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.copy_field import CopyField

_MNEMONIC = re.compile(r"(?<!&)&([^&\s])")
MENU_LETTERS = frozenset("fptgh")
SCENARIOS = ("all_ready", "live_active")


def _letter(text: str) -> str | None:
    m = _MNEMONIC.search(text.replace("&&", ""))
    return m.group(1).lower() if m else None


def _shared_internal(w: QWidget) -> bool:
    p: QWidget | None = w.parentWidget()
    while p is not None:
        if isinstance(p, CopyField):
            return True
        if isinstance(p, Banner) and w is getattr(p, "detail_button", None):
            return True
        p = p.parentWidget()
    return False


def page_mnemonics(page: QWidget, window: QWidget) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = defaultdict(list)
    for w in page.findChildren(QWidget):
        if not w.isVisibleTo(window) or _shared_internal(w):
            continue
        text: str | None = None
        if isinstance(w, QAbstractButton):
            text = w.text()
            if not w.isEnabled():
                text = text  # a disabled button still owns its letter
        elif isinstance(w, QLabel) and w.buddy() is not None:
            text = w.text()
        elif isinstance(w, QGroupBox) and w.isCheckable():
            text = w.title()
        elif isinstance(w, QTabBar):
            for i in range(w.count()):
                letter = _letter(w.tabText(i))
                if letter:
                    seen[letter].append(f"tab {w.tabText(i)!r}")
            continue
        letter = _letter(text) if text else None
        if letter:
            seen[letter].append(f"{type(w).__name__} {text!r}")
    return seen


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("spec", PAGE_REGISTRY, ids=lambda s: s.page_id)
def test_page_mnemonics_unique_and_clear_of_menus(
    make_window: Any, qtbot: Any, scenario: str, spec: Any
) -> None:
    win = make_window(scenario)
    win.go(spec.page_id)
    qtbot.wait(250)
    page = win.page(spec.page_id)
    assert page is not None
    problems: list[str] = []
    tab_widgets = [t for t in page.findChildren(QTabWidget) if t.isVisibleTo(win)]
    states = [(t, i) for t in tab_widgets for i in range(t.count())] or [(None, 0)]
    for tabs, index in states:  # every tab of a tabbed page is its own visible set
        if tabs is not None:
            tabs.setCurrentIndex(index)
            qtbot.wait(30)
        where = f"tab {index}: " if tabs is not None else ""
        seen = page_mnemonics(page, win)
        problems += [f"{where}Alt+{k.upper()}: {v}" for k, v in seen.items() if len(v) > 1]
        problems += [
            f"{where}Alt+{k.upper()} is a menu-bar letter: {v}"
            for k, v in seen.items()
            if k in MENU_LETTERS
        ]
    assert not problems, "\n".join(problems)


def test_menu_letters_are_the_reserved_ones(make_window: Any) -> None:
    win = make_window("all_ready")
    letters = {_letter(a.text()) for a in win.menuBar().actions()}
    assert letters == set(MENU_LETTERS)


@pytest.mark.parametrize("spec", PAGE_REGISTRY, ids=lambda s: s.page_id)
def test_alt_menu_letters_open_the_menu(make_window: Any, qtbot: Any, spec: Any) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    win = make_window("all_ready")
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    win.go(spec.page_id)
    qtbot.wait(150)
    page = win.page(spec.page_id)
    inside = [
        w
        for w in page.findChildren(QWidget)
        if w.isVisibleTo(win)
        and w.isEnabled()
        and w.focusPolicy() & Qt.FocusPolicy.TabFocus
        and w.focusProxy() is None
    ]
    start = inside[0] if inside else win.sidebar
    for key in (Qt.Key.Key_F, Qt.Key.Key_P, Qt.Key.Key_T, Qt.Key.Key_G, Qt.Key.Key_H):
        start.setFocus(Qt.FocusReason.OtherFocusReason)
        qtbot.waitUntil(lambda: QApplication.focusWidget() is start, timeout=1000)
        QTest.keyClick(start, key, Qt.KeyboardModifier.AltModifier)
        qtbot.waitUntil(lambda: QApplication.activePopupWidget() is not None, timeout=1000)
        popup = QApplication.activePopupWidget()
        assert popup is not None
        QTest.keyClick(popup, Qt.Key.Key_Escape)
        QTest.keyClick(win.menuBar(), Qt.Key.Key_Escape)
        qtbot.waitUntil(lambda: QApplication.activePopupWidget() is None, timeout=1000)
