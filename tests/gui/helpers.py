# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Helpers for GUI tests (import as ``tests.gui.helpers``)."""

from __future__ import annotations

from typing import Any


def fake_core(bridge: Any) -> Any:
    """The FakeCore behind a ``make_bridge`` bridge."""
    return bridge.fake["core"]


def click_modal(qtbot: Any, object_name: str, *, check: str | None = None) -> list[str]:
    """Schedule a click on ``object_name`` in the next modal dialog; returns a log."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QAbstractButton, QApplication, QCheckBox

    seen: list[str] = []

    def attempt(tries: int = 0) -> None:
        modal = QApplication.activeModalWidget()
        if modal is None:
            if tries < 100:
                QTimer.singleShot(20, lambda: attempt(tries + 1))
            return
        seen.append(modal.objectName())
        if check is not None:
            box = modal.findChild(QCheckBox, check)
            if box is not None:
                box.setChecked(True)
        btn = modal.findChild(QAbstractButton, object_name)
        if btn is None:
            seen.append(f"no button {object_name}")
            modal.close()
            return
        btn.click()

    QTimer.singleShot(0, attempt)
    return seen
