# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Opt-in to the experimental NVIDIA TensorRT path (SCOPE §5.2, GUI.md §4.2 page 3).

States what "experimental" means before anything is turned on. The default
button is [Cancel]; nothing is downloaded from NVIDIA and nothing is installed.
"""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import command_hint
from buttereye.gui.a11y import heading
from buttereye.gui.dialogs.fit import FitDialog, add_buttons
from buttereye.gui.widgets.cli_hint import CliHint


def _t(text: str) -> str:
    return QCoreApplication.translate("TrtOptInDialog", text)


def optin_text() -> str:
    return _t(
        "The TensorRT path depends on pre-release software: vs-mlrt v16.x and NVIDIA "
        "TensorRT 11 RPMs that you install yourself. It may break on Fedora, TensorRT or "
        "VapourSynth updates, and it is not part of ButterEye's release testing.\n\n"
        "ButterEye never downloads anything from NVIDIA. RIFE (Vulkan) and MVTools keep "
        "working whatever happens to this option."
    )


class TrtOptInDialog(FitDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("trt_optin")
        self.setWindowTitle(_t("Experimental NVIDIA TensorRT"))
        self.headline = heading(QLabel(_t("Turn on experimental NVIDIA TensorRT?"), self))
        self.headline.setTextFormat(Qt.TextFormat.PlainText)
        self.headline.setWordWrap(True)
        self.body = QLabel(optin_text(), self)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.hint = CliHint(command_hint("trt.optin"), self)

        self.buttons = QDialogButtonBox(self)
        self.accept_button = QPushButton(_t("&I understand, turn it on"), self)
        self.accept_button.setObjectName("trt.accept")
        self.accept_button.setAccessibleName(_t("I understand, turn it on"))
        self.cancel_button = QPushButton(_t("Cancel"), self)
        self.cancel_button.setObjectName("trt.cancel")
        self.cancel_button.setAccessibleName(_t("Cancel"))
        add_buttons(self.buttons, self.accept_button, self.cancel_button)  # Cancel is default
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(self.headline)
        lay.addWidget(self.body)
        lay.addWidget(self.hint)
        lay.addWidget(self.buttons)
        self.setAccessibleDescription(optin_text())


__all__ = ["TrtOptInDialog", "optin_text"]
