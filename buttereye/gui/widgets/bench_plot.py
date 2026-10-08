# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark bars drawn with QPainter (no QtCharts, F17; §4.6).

Two horizontal bars per measurement: in-mpv fps solid, vspipe fps hatched, each
with its number written beside the bar. An optional dashed target line is
labelled. All colours come from ``palette()``; the two series also differ by
fill pattern, so the plot reads in greyscale. Text is drawn in WindowText on
the Window background (never on a bar), which keeps label contrast at the
palette's text contrast. The plot takes no focus: the results table next to it
is the accessible source of truth; the plot's accessible description sums it up.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPaintEvent, QPalette, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from buttereye.core.api import BenchMeasurement
from buttereye.gui.theme import contrast_ratio, relative_luminance


def _fps(v: float) -> str:
    return f"{v:.1f}"


def _rate(v: float) -> str:
    """Target rates keep up to 3 decimals: 119.88, 23.976, 60."""
    return f"{v:.3f}".rstrip("0").rstrip(".")


class BenchPlot(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[BenchMeasurement, ...] = ()
        self._target: float | None = None
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.Window)
        self.setAccessibleName(self.tr("Benchmark chart"))
        self._update_description()

    # ------------------------------------------------------------------ API
    def set_data(self, rows: Sequence[BenchMeasurement], target_fps: float | None) -> None:
        self._rows = tuple(rows)
        self._target = target_fps if target_fps and target_fps > 0 else None
        self._update_description()
        self.updateGeometry()
        self.update()

    def rows(self) -> tuple[BenchMeasurement, ...]:
        return self._rows

    def target_fps(self) -> float | None:
        return self._target

    def best(self) -> BenchMeasurement | None:
        """Highest in-mpv fps among configurations without GPU faults (§4.6)."""
        clean = [r for r in self._rows if not r.gpu_faults]
        return max(clean, key=lambda r: r.mpv_fps, default=None)

    def label_colors(self) -> tuple[QColor, QColor]:
        """(text, background) used for every label and number."""
        pal = self.palette()
        return pal.color(QPalette.ColorRole.WindowText), pal.color(QPalette.ColorRole.Window)

    def row_label(self, r: BenchMeasurement) -> str:
        if r.gpu_faults:
            return self.tr("{label} — GPU fault").format(label=r.label)
        return r.label

    # ---------------------------------------------------------------- sizing
    def _line(self) -> int:
        return self.fontMetrics().height()

    def sizeHint(self) -> QSize:
        line = self._line()
        n = max(1, len(self._rows))
        # header (target label) + rows (2 bars + gap) + legend
        height = line * 2 + n * (line * 2 + line // 2 + line // 2) + line * 2
        return QSize(line * 30, height)

    def minimumSizeHint(self) -> QSize:
        hint = self.sizeHint()
        return QSize(self._line() * 16, hint.height())

    def hasHeightForWidth(self) -> bool:
        return False

    # -------------------------------------------------------------- painting
    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.FontChange):
            self.updateGeometry()
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        fg, _bg = self.label_colors()
        bar_color = pal.color(QPalette.ColorRole.Highlight)
        fm = self.fontMetrics()
        line = self._line()
        pad = line // 2
        rect = QRectF(self.rect()).adjusted(pad, pad, -pad, -pad)
        p.setPen(fg)

        if not self._rows:
            p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), self.tr("No measurements"))
            p.end()
            return

        labels = [self.row_label(r) for r in self._rows]
        label_w = min(rect.width() * 0.4, max(fm.horizontalAdvance(t) for t in labels) + pad)
        top = rect.top() + line * 1.5  # room for the target label
        legend_h = line * 1.5
        plot_left = rect.left() + label_w + pad
        values = [v for r in self._rows for v in (r.mpv_fps, r.vspipe_fps)]
        vmax = max([*values, self._target or 0.0, 1.0])
        num_w = fm.horizontalAdvance(_fps(vmax) + " 0") + pad
        plot_w = max(1.0, rect.right() - plot_left - num_w)

        def x_of(v: float) -> float:
            return plot_left + plot_w * max(0.0, v) / vmax

        bar_h = float(line)
        y = top
        for r, text in zip(self._rows, labels, strict=True):
            block = QRectF(rect.left(), y, label_w, bar_h * 2)
            p.setPen(fg)
            p.drawText(
                block,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                fm.elidedText(text, Qt.TextElideMode.ElideRight, int(label_w)),
            )
            for i, (v, solid) in enumerate(((r.mpv_fps, True), (r.vspipe_fps, False))):
                by = y + i * bar_h
                bar = QRectF(plot_left, by + 1, x_of(v) - plot_left, bar_h - 2)
                if solid:
                    p.fillRect(bar, bar_color)
                else:
                    p.fillRect(bar, QBrush(bar_color, Qt.BrushStyle.BDiagPattern))
                    p.setPen(QPen(bar_color, 1))
                    p.drawRect(bar)
                p.setPen(fg)
                p.drawText(
                    QRectF(bar.right() + pad / 2, by, num_w, bar_h),
                    int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                    _fps(v),
                )
            y += bar_h * 2 + line

        if self._target is not None:
            tx = x_of(self._target)
            pen = QPen(fg, max(1, line // 10))
            pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.drawLine(QPointF(tx, rect.top() + line), QPointF(tx, y - line / 2))
            p.setPen(fg)
            tlabel = self.tr("Target {fps} fps").format(fps=_rate(self._target))
            tw = fm.horizontalAdvance(tlabel)
            lx = min(max(rect.left(), tx - tw / 2), rect.right() - tw)
            p.drawText(
                QRectF(lx, rect.top(), tw + 1, line),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                tlabel,
            )

        # legend: solid swatch "in mpv", hatched swatch "vspipe"
        ly = min(y, rect.bottom() - legend_h)
        sw = float(line)
        x = rect.left()
        for solid, name in ((True, self.tr("in mpv")), (False, self.tr("vspipe only"))):
            swatch = QRectF(x, ly + 2, sw, sw - 4)
            if solid:
                p.fillRect(swatch, bar_color)
            else:
                p.fillRect(swatch, QBrush(bar_color, Qt.BrushStyle.BDiagPattern))
                p.setPen(QPen(bar_color, 1))
                p.drawRect(swatch)
            p.setPen(fg)
            x += sw + pad / 2
            w = fm.horizontalAdvance(name)
            p.drawText(
                QRectF(x, ly, w + 1, sw),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                name,
            )
            x += w + line
        p.end()

    # ---------------------------------------------------------- accessibility
    def _update_description(self) -> None:
        if not self._rows:
            self.setAccessibleDescription(self.tr("No measurements"))
            return
        best = self.best()
        if best is None:
            self.setAccessibleDescription(
                self.tr("Every measured configuration had a GPU fault; none is recommended.")
            )
            return
        rt = self.tr("real time") if best.realtime else self.tr("not real time")
        self.setAccessibleDescription(
            self.tr("Best: {label}, {fps} fps in mpv, {rt}").format(
                label=best.label, fps=f"{best.mpv_fps:.0f}", rt=rt
            )
        )


__all__ = ["BenchPlot", "contrast_ratio", "relative_luminance"]
