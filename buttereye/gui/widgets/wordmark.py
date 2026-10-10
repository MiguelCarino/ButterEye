# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The app name as a Carino wordmark: Red Hat Display Black, uppercase, filled
with the brand's gold gradient (carino-branding.css ``--gold-text``).

It stays a ``QLabel`` whose text is the plain name, so screen readers, the
accessibility tree and tests read "ButterEye"; only the painting differs. The
gradient's darkest stop keeps >= 3:1 against the near-black window (large text,
WCAG 1.4.3).
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QLinearGradient, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from buttereye.gui import a11y, theme

#: --gold-text: light gold → Sharp Gold → amber (theme.CARINO tokens)
GOLD_STOPS: tuple[tuple[float, str], ...] = (
    (0.0, "gold_light"),
    (0.5, "accent"),
    (1.0, "gold_dark"),
)


class Wordmark(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None, *, factor: float = 1.6) -> None:
        super().__init__(text, parent)
        self._factor = factor
        self.setTextFormat(Qt.TextFormat.PlainText)
        a11y.heading(self, factor=factor)  # heading semantics and font-scale tracking
        self._apply_font()

    def _apply_font(self) -> None:
        self._updating = True
        try:
            self.setFont(theme.wordmark_font(QApplication.font(), self._factor))
        finally:
            self._updating = False

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange and not getattr(self, "_updating", False):
            # rescale_fonts() re-derives headings as bold; keep the wordmark's face
            f = self.font()
            if f.weight() != theme.wordmark_font(f, 1.0).weight() or not f.capitalization():
                self._apply_font()

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        fm = self.fontMetrics()
        text = self.text()
        path = QPainterPath()
        y = (self.height() + fm.ascent() - fm.descent()) / 2
        path.addText(QPointF(0, y), self.font(), text)
        box = path.boundingRect()
        grad = QLinearGradient(box.topLeft(), box.bottomRight())
        for at, name in GOLD_STOPS:
            grad.setColorAt(at, theme.token(name))
        p.fillPath(path, grad)
        p.end()


__all__ = ["GOLD_STOPS", "Wordmark"]
