# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""More logs (GUI.md §12.1.1): every log ButterEye still has — videos playing now,
players that closed or crashed recently, and copies — each with Copy log."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.gui import theme


@dataclass(frozen=True, slots=True)
class LogEntry:
    title: str
    state: str  # "Playing", "Closed", "mpv crashed", "Copy saved", ...
    copy: Callable[[], None]


class LogsDialog(QDialog):
    def __init__(self, entries: Sequence[LogEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("logsDialog")
        self.setWindowTitle(self.tr("Recent logs"))
        lay = QVBoxLayout(self)
        intro = QLabel(
            self.tr("Copy a log to paste it in a report. ButterEye keeps the most recent ones."),
            self,
        )
        intro.setWordWrap(True)
        theme.secondary(intro)
        lay.addWidget(intro)
        self.buttons: list[QPushButton] = []
        if not entries:
            empty = QLabel(self.tr("No logs yet."), self)
            lay.addWidget(empty)
        for e in entries:
            row = QHBoxLayout()
            name = QLabel(e.title, self)
            name.setTextFormat(Qt.TextFormat.PlainText)
            name.setToolTip(e.title)
            state = QLabel(e.state, self)
            state.setTextFormat(Qt.TextFormat.PlainText)
            theme.secondary(state)
            copy = QPushButton(self.tr("Copy log"), self)
            copy.setAutoDefault(False)
            copy.setAccessibleName(self.tr("Copy the log for {title}").format(title=e.title))
            copy.clicked.connect(e.copy)
            self.buttons.append(copy)
            row.addWidget(name, 1)
            row.addWidget(state, 0)
            row.addWidget(copy, 0)
            lay.addLayout(row)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.resize(560, self.sizeHint().height())


__all__ = ["LogEntry", "LogsDialog"]
