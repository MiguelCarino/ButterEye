# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Read-only viewer for licence and notice files (docs/design/GUI.md §4.9, F18).

The text is shown from the shipped file, never only as a URL. It is selectable
and keyboard-scrollable; Tab leaves the text (no focus trap). A missing file is
reported in the viewer with BE-1021, never hidden.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import ErrorCode
from buttereye.gui import theme
from buttereye.gui.a11y import labelled, selectable
from buttereye.gui.dialogs.fit import FitDialog

#: Licence texts are small; anything bigger is cut (and says so).
MAX_BYTES = 4 * 1024 * 1024


def _t(text: str) -> str:
    return QCoreApplication.translate("TextViewer", text)


def read_text(path: Path) -> tuple[str, bool]:
    """(text, found). Undecodable bytes are replaced; huge files are truncated."""
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError as exc:
        return (
            _t("Licence file not found at {path} ({code}). This is a packaging bug.").format(
                path=path, code=ErrorCode.LICENCE_FILE_MISSING.code
            )
            + f"\n\n{exc.strerror or exc}",
            False,
        )
    text = data[:MAX_BYTES].decode("utf-8", errors="replace")
    if len(data) > MAX_BYTES:
        text += "\n\n" + _t("[The file is longer; only the first 4 MB are shown.]")
    return text, True


class TextViewer(FitDialog):
    """``TextViewer(title, files, parent)``: one or several files, chosen with a combo."""

    def __init__(self, title: str, files: Sequence[Path], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("text_viewer")
        self.setWindowTitle(title)
        self.setSizeGripEnabled(True)
        self._files = tuple(files)
        self.found: dict[Path, bool] = {}

        self.path_label = QLabel(self)
        self.path_label.setTextFormat(Qt.TextFormat.PlainText)
        self.path_label.setWordWrap(True)
        selectable(self.path_label, name=_t("File path"))

        self.chooser = QComboBox(self)
        for p in self._files:
            self.chooser.addItem(p.name, str(p))
        self.chooser_label, _ = labelled(_t("&File"), self.chooser)
        self.chooser_label.setParent(self)
        row = QHBoxLayout()
        row.addWidget(self.chooser_label)
        row.addWidget(self.chooser, 1)

        self.text = QTextBrowser(self)
        self.text.setOpenLinks(False)
        self.text.setTabChangesFocus(True)
        self.text.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        theme.apply_fixed_font(self.text)
        self.text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self.text.setAccessibleName(title)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setObjectName("viewer.close")
        close.setAccessibleName(_t("Close"))
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addLayout(row)
        lay.addWidget(self.path_label)
        lay.addWidget(self.text, 1)
        lay.addWidget(buttons)
        many = len(self._files) > 1
        self.chooser.setVisible(many)
        self.chooser_label.setVisible(many)
        self.chooser.currentIndexChanged.connect(self._show_index)
        if self._files:
            self._show_index(0)
        else:
            self.text.setPlainText(_t("No licence text files were found for this component."))
            self.path_label.setVisible(False)
        metrics = self.fontMetrics()
        self.resize(metrics.horizontalAdvance("M") * 84, metrics.height() * 32)

    def files(self) -> tuple[Path, ...]:
        return self._files

    def current_text(self) -> str:
        return self.text.toPlainText()

    def _show_index(self, i: int) -> None:
        if not 0 <= i < len(self._files):
            return
        path = self._files[i]
        body, found = read_text(path)
        self.found[path] = found
        self.path_label.setText(str(path))
        self.path_label.setAccessibleName(_t("File path {path}").format(path=path))
        self.text.setPlainText(body)
        self.text.setAccessibleDescription(str(path))


__all__ = ["MAX_BYTES", "TextViewer", "read_text"]
