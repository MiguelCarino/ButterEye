# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""On/off switch (docs/design/GUI.md §12).

A ``QCheckBox`` underneath, so assistive technology sees a check box with a
checked state and Space toggles it. It paints a rounded track with a knob and
the word "On" or "Off" next to it: the state is told by position and by word,
never by colour alone. Colours come from the palette (Highlight when on, Mid
outline when off); the focus ring is drawn in Highlight, 2 px.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QCheckBox, QWidget

from buttereye.gui.theme import FOCUS_WIDTH


class Switch(QCheckBox):
    """Checkable switch. The accessible name is the setting ("Smooth motion")."""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAccessibleName(name)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggled.connect(self._on_toggled)
        self._on_toggled(self.isChecked())

    # ------------------------------------------------------------------ text
    def on_text(self) -> str:
        return self.tr("On")

    def off_text(self) -> str:
        return self.tr("Off")

    def state_text(self) -> str:
        return self.on_text() if self.isChecked() else self.off_text()

    def _on_toggled(self, on: bool) -> None:
        self.setAccessibleDescription(self.state_text())
        self.update()

    # ------------------------------------------------------------------ geometry
    def _track_size(self) -> QSize:
        h = self.fontMetrics().height()
        return QSize(round(h * 2.4), round(h * 1.3))

    def _margin(self) -> int:
        return FOCUS_WIDTH + 2

    def sizeHint(self) -> QSize:
        track = self._track_size()
        fm = self.fontMetrics()
        words = max(fm.horizontalAdvance(self.on_text()), fm.horizontalAdvance(self.off_text()))
        gap = fm.height() // 2
        m = self._margin()
        return QSize(m + track.width() + gap + words + m, max(track.height(), fm.height()) + 2 * m)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos: QPoint) -> bool:
        return self.rect().contains(pos)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.FontChange,
            QEvent.Type.EnabledChange,
        ):
            self.updateGeometry()
            self.update()
        super().changeEvent(event)

    # ------------------------------------------------------------------ paint
    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        group = QPalette.ColorGroup.Active if self.isEnabled() else QPalette.ColorGroup.Disabled
        R = QPalette.ColorRole  # noqa: N806 - enum alias
        track = self._track_size()
        m = self._margin()
        y = (self.height() - track.height()) / 2
        rect = QRectF(m, y, track.width(), track.height())
        radius = rect.height() / 2
        on = self.isChecked()

        if self.hasFocus():
            ring = QPen(pal.color(group, R.Highlight))
            ring.setWidthF(FOCUS_WIDTH)
            p.setPen(ring)
            p.setBrush(Qt.BrushStyle.NoBrush)
            off = FOCUS_WIDTH + 0.5
            outer = rect.adjusted(-off, -off, off, off)
            p.drawRoundedRect(outer, outer.height() / 2, outer.height() / 2)

        edge = QPen(pal.color(group, R.Highlight if on else R.Mid))
        edge.setWidthF(1.5)
        p.setPen(edge)
        p.setBrush(pal.color(group, R.Highlight) if on else pal.color(group, R.Base))
        p.drawRoundedRect(rect.adjusted(0.75, 0.75, -0.75, -0.75), radius, radius)

        inset = rect.height() * 0.16
        d = rect.height() - 2 * inset
        kx = rect.right() - inset - d if on else rect.left() + inset
        knob = QRectF(kx, rect.top() + inset, d, d)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pal.color(group, R.Base if on else R.Mid))
        p.drawEllipse(knob)

        p.setPen(pal.color(group, R.WindowText))
        gap = self.fontMetrics().height() // 2
        text_rect = QRectF(rect.right() + gap, 0, self.width() - rect.right() - gap, self.height())
        p.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.state_text(),
        )
        p.end()


__all__ = ["Switch"]
