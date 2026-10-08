# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Read-only monospace line + [Copy] (commands, paths, include lines; §4.0).

Ctrl+C on the focused field copies the whole line when nothing is selected.
The text is never executed by ButterEye.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtGui import QGuiApplication, QKeyEvent, QKeySequence
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QPushButton, QWidget

from buttereye.gui import theme

_FEEDBACK_MS = 1500


class _CopyLine(QLineEdit):
    copy_all = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Copy) and not self.hasSelectedText():
            self.copy_all.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class CopyField(QWidget):
    """``CopyField(text, accessible_name=...)``: the field is named ``accessible_name``,
    the button "Copy: <accessible_name>"."""

    copied = Signal(str)

    def __init__(self, text: str, *, accessible_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if not accessible_name.strip():
            raise ValueError("CopyField needs an accessible name")
        self._name = accessible_name
        self.line = _CopyLine(self)
        self.line.setReadOnly(True)
        theme.apply_fixed_font(self.line)
        self.line.setAccessibleName(accessible_name)
        self.line.copy_all.connect(self.copy)
        self.button = QPushButton(self.tr("&Copy"), self)
        self.button.setAccessibleName(self.tr("Copy: {name}").format(name=accessible_name))
        self.button.setAutoDefault(False)
        self.button.clicked.connect(self.copy)
        self._reset = QTimer(self)
        self._reset.setSingleShot(True)
        self._reset.setInterval(_FEEDBACK_MS)
        self._reset.timeout.connect(self._restore_button)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.line, 1)
        lay.addWidget(self.button)
        self.setAccessibleName(accessible_name)
        self.setFocusProxy(self.line)
        self.set_text(text)

    def text(self) -> str:
        return self.line.text()

    def set_text(self, text: str) -> None:
        self.line.setText(text)
        self.line.setCursorPosition(0)
        self.line.setToolTip(text)
        self.line.setAccessibleDescription(text)

    def copy(self) -> None:
        """Put the whole line on the clipboard."""
        QGuiApplication.clipboard().setText(self.text())
        self.button.setText(self.tr("Copied"))
        self._reset.start()
        self.copied.emit(self.text())

    def _restore_button(self) -> None:
        self.button.setText(self.tr("&Copy"))


__all__ = ["CopyField"]
