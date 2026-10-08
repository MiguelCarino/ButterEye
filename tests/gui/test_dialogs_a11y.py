# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Every dialog of gui/dialogs and gui/pages: Tab reaches every enabled button
(F17: the accept action must be reachable without the Alt mnemonic) and wrapped
text is never clipped at 1.5x / 2x font (§4.0 "font scale scrolls, never clips").
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QDialog,
    QLabel,
    QPushButton,
    QWidget,
)

from buttereye.core.api import (
    CleanTarget,
    DownloadItem,
    Msg,
    StorageEntry,
    StorageKind,
)
from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle, tab_focusable

DialogFactory = Callable[[Any], QDialog]


def _trt(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.trt_optin import TrtOptInDialog

    return TrtOptInDialog()


def _consent(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.consent import ConsentDialog

    item = DownloadItem("rife-v4.26", "https://example.org/r.7z", 1000, "MIT", "a" * 64, True)
    dlg = ConsentDialog([item])
    dlg.table.set_checked("rife-v4.26", True)  # the accept button must be enabled
    return dlg


def _clean(_win: Any) -> QDialog:
    from buttereye.gui.pages.storage import CleanConfirmDialog

    entry = StorageEntry(
        StorageKind.ENGINES,
        Path("/tmp/be-engines"),
        2048,
        True,
        False,
        Msg("Engines"),
        CleanTarget.ENGINES,
    )
    return CleanConfirmDialog([entry])


def _sha(_win: Any) -> QDialog:
    from buttereye.gui.pages.storage import Sha256Dialog

    dlg = Sha256Dialog(Path("/tmp/model.7z"))
    return dlg


def _report(_win: Any) -> QDialog:
    from buttereye.gui.pages.system import ReportDialog

    return ReportDialog()


def _include_confirm(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.include_line import IncludeConfirmDialog

    return IncludeConfirmDialog("include=~/.config/buttereye/mpv/buttereye.conf", "~/mpv.conf")


def _include(win: Any) -> QDialog:
    from buttereye.gui.dialogs.include_line import IncludeLineDialog

    return IncludeLineDialog(win.bridge)


def _include_fallback(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.setup_wizard import IncludeLineFallback

    return IncludeLineFallback("include=~/.config/buttereye/mpv/buttereye.conf")


def _rule(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.rule_edit import RuleEditDialog

    return RuleEditDialog(None, ())


def _viewer(_win: Any) -> QDialog:
    from buttereye.gui.dialogs.text_viewer import TextViewer

    return TextViewer("Licence", [Path("/nonexistent/COPYING")])


DIALOGS: dict[str, DialogFactory] = {
    "trt_optin": _trt,
    "consent": _consent,
    "clean_confirm": _clean,
    "sha256": _sha,
    "report": _report,
    "include_confirm": _include_confirm,
    "include_line": _include,
    "include_fallback": _include_fallback,
    "rule_edit": _rule,
    "text_viewer": _viewer,
}


def _show(dlg: QDialog, qtbot: Any) -> None:
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    qtbot.wait(150)  # core replies (include line) + a drain


@pytest.mark.parametrize("name", sorted(DIALOGS))
def test_dialog_tab_reaches_every_button(make_window: Any, qtbot: Any, name: str) -> None:
    win = make_window("all_ready")
    dlg = DIALOGS[name](win)
    _show(dlg, qtbot)
    focusables = [w for w in dlg.findChildren(QWidget) if tab_focusable(w, dlg)]
    assert focusables, name
    problems = check_names_and_buddies(dlg, dlg)
    problems += check_tab_cycle(dlg, focusables[0], qtbot)
    buttons = [b for b in dlg.findChildren(QPushButton) if tab_focusable(b, dlg)]
    assert len(buttons) >= 1
    assert not problems, "\n".join(problems)
    dlg.close()


@pytest.mark.parametrize("name", ["trt_optin", "consent", "clean_confirm"])
def test_consent_dialogs_cancel_is_default_and_action_reachable(
    make_window: Any, qtbot: Any, name: str
) -> None:
    win = make_window("all_ready")
    dlg = DIALOGS[name](win)
    _show(dlg, qtbot)
    buttons = {b.accessibleName(): b for b in dlg.findChildren(QPushButton)}
    assert buttons["Cancel"].isDefault()
    accept = next(b for n, b in buttons.items() if n != "Cancel")
    assert not accept.isDefault()
    seen: set[QPushButton] = set()
    dlg.findChildren(QPushButton)[0].setFocus(Qt.FocusReason.TabFocusReason)
    for _ in range(40):
        dlg.focusNextPrevChild(True)
        w = QApplication.focusWidget()
        if isinstance(w, QPushButton):
            seen.add(w)
    assert accept in seen and buttons["Cancel"] in seen
    dlg.close()


def _clipped(dlg: QDialog) -> list[str]:
    out: list[str] = []
    for label in dlg.findChildren(QLabel):
        if not label.isVisibleTo(dlg) or not label.wordWrap() or not label.text():
            continue
        if any(isinstance(p, QAbstractScrollArea) for p in _ancestors(label, dlg)):
            continue
        need = label.heightForWidth(label.width())
        if need > label.height() + 1:
            out.append(f"{label.text()[:40]!r}: {label.height()} px < {need} px")
    return out


def _ancestors(w: Any, stop: Any) -> Iterator[Any]:
    p = w.parentWidget()
    while p is not None and p is not stop:
        yield p
        p = p.parentWidget()


@pytest.mark.parametrize("scale", [1.5, 2.0])
@pytest.mark.parametrize("name", sorted(DIALOGS))
def test_dialog_text_not_clipped_at_large_font(
    qapp: QApplication, make_window: Any, qtbot: Any, name: str, scale: float
) -> None:
    original = QFont(qapp.font())
    big = QFont(original)
    big.setPointSizeF((original.pointSizeF() if original.pointSizeF() > 0 else 10.0) * scale)
    qapp.setFont(big)
    try:
        win = make_window("all_ready", show=False)
        dlg = DIALOGS[name](win)
        _show(dlg, qtbot)
        problems = _clipped(dlg)
        assert not problems, "\n".join(problems)
        dlg.close()
    finally:
        qapp.setFont(original)
