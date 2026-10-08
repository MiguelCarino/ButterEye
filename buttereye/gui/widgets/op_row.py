# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""One running operation: label, progress bar, phase, rate, ETA, [Cancel] (§4.0, §4.2).

The bar is indeterminate while ``Progress.total`` is unknown and never moves
backwards. Values are mapped onto a fixed 0..1000 scale so byte counts above
2**31 work with ``QProgressBar``'s int range.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QWidget,
)

from buttereye.core.api import Progress, render
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge

_SCALE = 1000
_SI = ("bytes", "kB", "MB", "GB", "TB", "PB")


def human_bytes(n: int | float) -> str:
    """SI size: ``0 bytes``, ``512 bytes``, ``1.2 GB``."""
    if n < 1000:
        return f"{int(n)} bytes"
    value = float(n)
    unit = 0
    while value >= 1000 and unit < len(_SI) - 1:
        value /= 1000
        unit += 1
    return f"{value:.1f} {_SI[unit]}"


def format_duration(seconds: float) -> str:
    """``0:42``, ``12:05``, ``1:02:03``."""
    s = max(0, round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class OpRow(QWidget):
    """``OpRow(label, cancel)``; feed ``update(progress)``; end with ``finish(ok, text)``."""

    def __init__(
        self, label: str, cancel: Callable[[], None] | None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._label = label
        self._cancel_cb = cancel
        self._scaled = 0
        self._finished = False
        self.last_progress: Progress | None = None

        self.label = QLabel(label, self)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.label.setWordWrap(True)
        self.bar = QProgressBar(self)
        self.bar.setAccessibleName(label)
        self.bar.setRange(0, 0)  # indeterminate until a total arrives
        self.label.setBuddy(self.bar)
        self.phase_label = QLabel(self)
        self.phase_label.setTextFormat(Qt.TextFormat.PlainText)
        self.phase_label.setWordWrap(True)
        self.rate_label = QLabel(self)
        self.eta_label = QLabel(self)
        self.result_badge = StatusBadge("busy", "", self)
        self.result_badge.setVisible(False)
        self.cancel_button = QPushButton(self.tr("Cancel"), self)
        self.cancel_button.setAccessibleName(self.tr("Cancel: {label}").format(label=label))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setVisible(cancel is not None)
        self.cancel_button.clicked.connect(self._on_cancel)

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.label, 0, 0, 1, 3)
        grid.addWidget(self.bar, 1, 0, 1, 2)
        grid.addWidget(self.cancel_button, 1, 2)
        grid.addWidget(self.phase_label, 2, 0)
        grid.addWidget(self.rate_label, 2, 1)
        grid.addWidget(self.eta_label, 2, 2)
        grid.addWidget(self.result_badge, 3, 0, 1, 3)
        grid.setColumnStretch(0, 1)
        self.setAccessibleName(label)

    # ------------------------------------------------------------------ API
    def update(self, *args: Any) -> None:  # noqa: D401 - frozen API name (§7 U5)
        """``update(p: Progress)`` applies progress; any other call is ``QWidget.update``."""
        if len(args) == 1 and isinstance(args[0], Progress):
            self.set_progress(args[0])
            return
        super().update(*args)

    def set_progress(self, p: Progress) -> None:
        if self._finished:
            return
        self.last_progress = p
        phase = render(p.phase)
        if p.detail is not None:
            phase = f"{phase} — {render(p.detail)}"
        self.phase_label.setText(phase)
        if p.total is None or p.total <= 0 or p.done is None:
            if self._scaled == 0:
                self.bar.setRange(0, 0)
        else:
            scaled = min(_SCALE, max(0, p.done * _SCALE // p.total))
            self._scaled = max(self._scaled, scaled)
            self.bar.setRange(0, _SCALE)
            self.bar.setValue(self._scaled)
        self.rate_label.setText(self._rate_text(p))
        self.eta_label.setText(
            self.tr("About {t} left").format(t=format_duration(p.eta_s))
            if p.eta_s is not None
            else ""
        )
        self.bar.setAccessibleDescription(
            " · ".join(t for t in (phase, self.rate_label.text(), self.eta_label.text()) if t)
        )

    def finish(self, ok: bool, text: str, *, cancelled: bool = False) -> None:
        """End the row: full bar on success, result badge with ``text``, Cancel hidden."""
        self._finished = True
        if ok:
            self._scaled = _SCALE
            self.bar.setRange(0, _SCALE)
            self.bar.setValue(_SCALE)
        elif self.bar.maximum() == 0:
            self.bar.setRange(0, _SCALE)
            self.bar.setValue(self._scaled)
        self.cancel_button.setVisible(False)
        self.eta_label.setText("")
        kind: BadgeKind = "off" if cancelled else ("ok" if ok else "blocking")
        self.result_badge.set_state(kind, text)
        self.result_badge.setVisible(True)
        self.bar.setAccessibleDescription(text)

    def is_finished(self) -> bool:
        return self._finished

    def value_permille(self) -> int:
        """Current progress on the 0..1000 scale (0 while indeterminate)."""
        return self._scaled

    # ------------------------------------------------------------- internals
    def _rate_text(self, p: Progress) -> str:
        if p.rate is None:
            return ""
        if p.unit == "frames":
            return self.tr("{rate} fps").format(rate=f"{p.rate:.1f}")
        if p.unit == "bytes":
            return self.tr("{rate}/s").format(rate=human_bytes(p.rate))
        return ""

    def _on_cancel(self) -> None:
        if self._cancel_cb is None or self._finished:
            return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(self.tr("Cancelling…"))
        self.cancel_button.setAccessibleName(
            self.tr("Cancelling: {label}").format(label=self._label)
        )
        self._cancel_cb()


__all__ = ["OpRow", "format_duration", "human_bytes"]
