# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The one primary action of a window, in the accent colour (GUI.md §12).

Palette only: Button takes the palette's Highlight and ButtonText its
HighlightedText (held to >= 4.5:1 by ``theme.accessible_palette``). The roles
are re-derived whenever the application palette changes, so the button follows
light and dark schemes.
"""

from __future__ import annotations

import shiboken6
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

_GROUPS = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)


def primary_palette(base: QPalette) -> QPalette:
    out = QPalette(base)
    R = QPalette.ColorRole  # noqa: N806 - enum alias
    for g in _GROUPS:
        out.setColor(g, R.Button, base.color(g, R.Highlight))
        out.setColor(g, R.ButtonText, base.color(g, R.HighlightedText))
    return out


class PrimaryButton(QPushButton):
    """A widget with its own palette gets no palette events when the
    application palette changes, so this follows ``paletteChanged``."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._applying = False
        self._apply()
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.paletteChanged.connect(self._on_app_palette)

    def _on_app_palette(self, _palette: QPalette) -> None:
        if not self._applying and shiboken6.isValid(self):
            self._apply()

    def _apply(self) -> None:
        self._applying = True
        try:
            self.setPalette(primary_palette(QApplication.palette()))
        finally:
            self._applying = False


__all__ = ["PrimaryButton", "primary_palette"]
