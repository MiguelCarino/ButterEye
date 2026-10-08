# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shared dialog plumbing (docs/design/GUI.md §4.0 "font scale scrolls, never clips").

- ``add_buttons()`` adds an accept and a cancel button to a ``QDialogButtonBox``
  and only then makes the cancel button the default. Under Qt 6.11 calling
  ``setDefault(True)`` *before* ``QDialogButtonBox.addButton()`` drops the button
  added earlier from the Tab chain, so keyboard users never reach the action.
- ``fit_to_text()`` sizes a dialog from its layout's height-for-width. A
  top-level window ignores ``heightForWidth``, so a word-wrapped ``QLabel`` gets
  its one-line height and is clipped at a large font; this grows the dialog to
  what the wrapped text needs, capped to the screen.
- ``FitDialog`` is a ``QDialog`` that calls ``fit_to_text()`` when it is shown,
  so text set after construction (e.g. a reply from the core) is measured too.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QPushButton, QWidget

#: Width a wrapped dialog aims for, in multiples of the font's "M" advance.
DEFAULT_EM = 40


def add_buttons(
    box: QDialogButtonBox,
    accept: QPushButton,
    cancel: QPushButton,
    *,
    accept_role: QDialogButtonBox.ButtonRole = QDialogButtonBox.ButtonRole.AcceptRole,
    default: QPushButton | None = None,
) -> None:
    """Add ``accept`` and ``cancel`` to ``box``, then set the default button.

    ``default`` is ``cancel`` unless given (consent is never the default).
    """
    accept.setAutoDefault(False)
    cancel.setAutoDefault(False)
    box.addButton(accept, accept_role)
    box.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
    (default or cancel).setDefault(True)


def fit_to_text(dialog: QWidget, *, em: int = DEFAULT_EM) -> None:
    """Resize ``dialog`` so every wrapped label gets the height it needs."""
    lay = dialog.layout()
    if lay is None:
        return
    dialog.ensurePolished()
    lay.activate()
    em_px = max(1, dialog.fontMetrics().horizontalAdvance("M"))
    width = max(dialog.sizeHint().width(), em * em_px)
    if dialog.testAttribute(Qt.WidgetAttribute.WA_Resized):
        width = max(width, dialog.width())
    screen = dialog.screen()
    avail = screen.availableGeometry() if screen is not None else None
    if avail is not None:
        width = min(width, avail.width())
    width = max(width, dialog.minimumSizeHint().width())
    height = dialog.sizeHint().height()
    if lay.hasHeightForWidth():
        height = max(height, lay.totalHeightForWidth(width))
    if dialog.testAttribute(Qt.WidgetAttribute.WA_Resized):
        height = max(height, dialog.height())
    if avail is not None:
        height = min(height, avail.height())
    dialog.resize(width, height)


class FitDialog(QDialog):
    """A ``QDialog`` sized from its wrapped text each time it is shown."""

    fit_em: int = DEFAULT_EM

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        fit_to_text(self, em=self.fit_em)
        super().showEvent(event)


__all__ = ["DEFAULT_EM", "FitDialog", "add_buttons", "fit_to_text"]
