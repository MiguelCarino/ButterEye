# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Accessibility sweep over every registered page and the shell's dialogs
(docs/design/GUI.md §4.0, §6 ``test_a11y_sweep``, F17).

Per page (it iterates ``PAGE_REGISTRY``, so new pages are covered
automatically) and per dialog:

- every widget that takes Tab focus has a non-empty accessible name;
- every editable input has a buddy label (``a11y.labelled``);
- the Tab cycle visits every enabled, visible Tab-focus control once and comes
  back to the start (no trap; editable multi-line text must not eat Tab).
"""

from __future__ import annotations

from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QAccessible
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QSlider,
    QTextEdit,
    QWidget,
)

from buttereye.core.testing import scenarios
from buttereye.gui.pages import PAGE_REGISTRY
from buttereye.gui.quit_dialog import DetachProgress, LiveSessionsDialog

SCENARIOS = ("devbox_now", "all_ready", "live_active")


def accessible_name(w: QWidget) -> str:
    iface = QAccessible.queryAccessibleInterface(w)
    if iface is not None and iface.isValid():
        name = iface.text(QAccessible.Text.Name)
        if name and name.strip():
            return name.strip()
    return w.accessibleName().strip()


def tab_focusable(w: QWidget, root: QWidget) -> bool:
    return bool(
        w.focusPolicy() & Qt.FocusPolicy.TabFocus
        and w.isVisibleTo(root)
        and w.isEnabled()
        and w.isVisible()
        and w.focusProxy() is None
    )


def is_editable_input(w: QWidget) -> bool:
    if isinstance(w, QLineEdit):
        return not w.isReadOnly() and not isinstance(w.parentWidget(), QAbstractSpinBox | QComboBox)
    if isinstance(w, QAbstractSpinBox):
        return not w.isReadOnly()
    if isinstance(w, QComboBox | QSlider):
        return True
    if isinstance(w, QPlainTextEdit | QTextEdit):
        return not w.isReadOnly()
    return False


def has_buddy(w: QWidget, window: QWidget) -> bool:
    for label in window.findChildren(QLabel):
        buddy = label.buddy()
        if buddy is not None and (buddy is w or buddy.isAncestorOf(w)):
            return True
    return False


def check_names_and_buddies(root: QWidget, window: QWidget) -> list[str]:
    problems: list[str] = []
    for w in root.findChildren(QWidget):
        if not tab_focusable(w, root):
            continue
        where = f"{type(w).__name__} {w.objectName()!r} in {type(root).__name__}"
        if not accessible_name(w):
            problems.append(f"no accessible name: {where}")
        if is_editable_input(w) and not has_buddy(w, window):
            problems.append(f"no buddy label: {where}")
        if (
            isinstance(w, QPlainTextEdit | QTextEdit)
            and not w.isReadOnly()
            and not w.tabChangesFocus()
        ):
            problems.append(f"Tab trap (editable text without tabChangesFocus): {where}")
    return problems


def check_tab_cycle(window: QWidget, start: QWidget, qtbot: Any) -> list[str]:
    start.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.wait(10)
    first = QApplication.focusWidget()
    if first is None:
        return [f"{type(window).__name__}: nothing takes focus"]
    visited: list[QWidget] = [first]
    for _ in range(2000):
        window.focusNextPrevChild(True)
        current = QApplication.focusWidget()
        if current is None:
            return [f"{type(window).__name__}: focus lost after {visited[-1]!r}"]
        if current is first:
            break
        if current in visited:
            return [f"{type(window).__name__}: Tab cycle loops without returning to the start"]
        visited.append(current)
    else:
        return [f"{type(window).__name__}: Tab cycle never returns (trap)"]
    eligible = [w for w in window.findChildren(QWidget) if tab_focusable(w, window)]
    missed = [w for w in eligible if w not in visited]
    return [
        f"never reached by Tab: {type(w).__name__} {w.objectName()!r} {accessible_name(w)!r}"
        for w in missed
    ]


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize("spec", PAGE_REGISTRY, ids=lambda s: s.page_id)
def test_page_a11y(make_window: Any, qtbot: Any, scenario: str, spec: Any) -> None:
    win = make_window(scenario)
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    win.go(spec.page_id)
    qtbot.wait(250)  # page refresh replies + a drain
    page = win.page(spec.page_id)
    assert page is not None
    problems = check_names_and_buddies(page, win)
    problems += check_tab_cycle(win, win.sidebar, qtbot)
    assert not problems, "\n".join(problems)


def test_shell_a11y(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    assert win.sidebar.accessibleName()
    assert win.status_label.accessibleName()
    problems = check_names_and_buddies(win.centralWidget(), win)
    assert not problems, "\n".join(problems)


def _dialogs(qtbot: Any) -> list[QDialog]:
    live = scenarios.get("live_active").sessions
    dialogs: list[QDialog] = [LiveSessionsDialog(live, None), DetachProgress(2, None)]
    for d in dialogs:
        qtbot.addWidget(d)
    return dialogs


def test_quit_dialogs_a11y(qtbot: Any) -> None:
    for dlg in _dialogs(qtbot):
        dlg.show()
        qtbot.waitExposed(dlg)
        dlg.activateWindow()
        qtbot.waitUntil(lambda d=dlg: QApplication.activeWindow() is d, timeout=2000)
        problems = check_names_and_buddies(dlg, dlg)
        focusables = [w for w in dlg.findChildren(QWidget) if tab_focusable(w, dlg)]
        if focusables:
            problems += check_tab_cycle(dlg, focusables[0], qtbot)
        assert not problems, "\n".join(problems)
        dlg.hide()
