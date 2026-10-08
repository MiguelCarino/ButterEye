# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Focusable file drop target (§4.0 Files, §4.3 Play empty state).

Accepts local files only (``QUrl.isLocalFile()``); anything else emits
``rejected`` with the BE-3007 text. Enter, Space or a click opens the file
dialog (portal-aware ``QFileDialog``). Portal document paths under
``/run/user/*/doc/`` are local files and are accepted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QFocusEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QVBoxLayout, QWidget

from buttereye.core.api import ErrorCode

FileChooser = Callable[[QWidget], Path | None]


def default_chooser(title: str, name_filter: str) -> FileChooser:
    def choose(parent: QWidget) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(parent, title, "", name_filter)
        return Path(path) if path else None

    return choose


class DropZone(QFrame):
    fileChosen = Signal(Path)
    rejected = Signal(str)

    def __init__(
        self,
        text: str | None = None,
        parent: QWidget | None = None,
        *,
        dialog_title: str | None = None,
        name_filter: str | None = None,
        chooser: FileChooser | None = None,
    ) -> None:
        super().__init__(parent)
        shown = (
            text
            if text is not None
            else self.tr("Drop a video file here, or press Enter to choose one")
        )
        self.label = QLabel(shown, self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay = QVBoxLayout(self)
        m = self.fontMetrics().height() * 2
        lay.setContentsMargins(m, m, m, m)
        lay.addWidget(self.label)
        self.setAcceptDrops(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAccessibleName(shown)
        self._chooser = chooser or default_chooser(
            dialog_title or self.tr("Choose a video file"),
            name_filter
            or self.tr("Video files (*.mkv *.mp4 *.webm *.mov *.avi *.m2ts *.ts);;All files (*)"),
        )
        self._drag_active = False
        self._filled = False

    def set_filled(self, filled: bool) -> None:
        """Paint the inside as a soft card (Base, or a lighter Window in dark schemes)."""
        self._filled = filled
        self.update()

    # ---------------------------------------------------------------- choose
    def choose(self) -> None:
        """Open the file dialog (Enter/Space/click)."""
        if not self.isEnabled():
            return
        path = self._chooser(self)
        if path is not None:
            self._accept_path(path)

    def offer_urls(self, urls: list[QUrl]) -> bool:
        """Handle dropped URLs; True if a file was chosen. Exposed for tests/pages."""
        if not urls:
            return False
        url = urls[0]
        if not url.isLocalFile():
            self.rejected.emit(self.local_only_text())
            return False
        return self._accept_path(Path(url.toLocalFile()))

    def local_only_text(self) -> str:
        msg = self.tr("Only local files can be opened. Streaming isn't supported.")
        return f"{msg} ({ErrorCode.FILE_NOT_LOCAL.code})"

    def _accept_path(self, path: Path) -> bool:
        if path.is_dir():
            self.rejected.emit(self.tr("That is a folder. Choose a video file."))
            return False
        self.fileChosen.emit(path)
        return True

    # ---------------------------------------------------------------- events
    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            event.accept()
            self.choose()
            return
        super().keyPressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            event.accept()
            self.choose()
            return
        super().mouseReleaseEvent(event)

    @staticmethod
    def _has_urls(mime: QMimeData | None) -> bool:
        return mime is not None and mime.hasUrls()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self.isEnabled() and self._has_urls(event.mimeData()):
            self._drag_active = True
            event.acceptProposedAction()
            self.update()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._drag_active = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        self._drag_active = False
        self.update()
        mime = event.mimeData()
        if not self._has_urls(mime):
            event.ignore()
            return
        event.acceptProposedAction()
        self.offer_urls(list(mime.urls()))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.EnabledChange):
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        width = max(1, self.fontMetrics().height() // 8)
        highlighted = self.hasFocus() or self._drag_active
        role = QPalette.ColorRole.Highlight if highlighted else QPalette.ColorRole.Mid
        pen = QPen(pal.color(group, role))
        pen.setWidth(width * (2 if highlighted else 1))
        pen.setStyle(Qt.PenStyle.SolidLine if highlighted else Qt.PenStyle.DashLine)
        p.setPen(pen)
        if self._filled:
            window = pal.color(group, QPalette.ColorRole.Window)
            # Light schemes: the Base card. Dark ones: Base is often pure black,
            # so a slightly lighter window reads as a soft card instead.
            dark = window.lightness() < 128
            p.setBrush(window.lighter(125) if dark else pal.color(group, QPalette.ColorRole.Base))
        # Mid is held to >= 3:1 against Window (theme.accessible_palette). Put the
        # pen centre on half pixels so a 1 px edge is one solid pixel, not two
        # half-covered ones at ~half the contrast.
        half = pen.widthF() / 2
        inset = half + 1.0
        r = QRectF(self.rect()).adjusted(inset, inset, -inset, -inset)
        radius = self.fontMetrics().height() * (0.9 if self._filled else 0.5)
        p.drawRoundedRect(r, radius, radius)
        p.end()

    def focusInEvent(self, event: QFocusEvent) -> None:
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.update()
        super().focusOutEvent(event)


__all__ = ["DropZone", "FileChooser", "default_chooser"]
