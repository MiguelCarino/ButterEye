# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The colour depth choice of a smooth copy (SCOPE §7.5): "Same as source
(N-bit)", 8-bit, 10-bit and, when anything here can write it, 12-bit.

Choices the chosen encoder can't write are disabled with a tooltip that says
why; ``note()`` is the sentence to show when the copy keeps fewer bits than the
source. What each choice gives comes from the core (``api.copy_bits``), the
same rule the job follows.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QStandardItemModel
from PySide6.QtWidgets import QComboBox, QWidget

from buttereye.core.api import (
    COLOUR_DEPTHS,
    ColourDepth,
    HdrClass,
    RenderProbe,
    copy_bits,
)

CTX = "DepthCombo"

_CHROMA = {"420": "4:2:0", "422": "4:2:2", "444": "4:4:4"}
_HDR10 = (HdrClass.HDR10, HdrClass.HDR10PLUS, HdrClass.DV)


def _t(text: str) -> str:
    return QCoreApplication.translate(CTX, text)


def depth_text(bits: int, chroma: str = "") -> str:
    """ "10-bit 4:2:0", or "10-bit" when the layout isn't YUV or not known."""
    layout = _CHROMA.get(chroma)
    if layout is None:
        return _t("{n}-bit").format(n=bits)
    return _t("{n}-bit {chroma}").format(n=bits, chroma=layout)


def hdr_text(hdr: HdrClass) -> str:
    names = {
        HdrClass.SDR: _t("SDR"),
        HdrClass.HDR10: _t("HDR10"),
        HdrClass.HDR10PLUS: _t("HDR10+"),
        HdrClass.HLG: _t("HLG"),
        HdrClass.DV: _t("Dolby Vision"),
    }
    return names.get(hdr, hdr.value.upper())


class DepthCombo(QComboBox):
    """``set_probe(probe, default)`` fills it, ``set_encoder(encoder)`` enables
    what that encoder writes; ``choice()`` is the choice for the job spec."""

    def __init__(
        self, names: Callable[[str], str] | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._names = names or (lambda e: e)
        self.probe: RenderProbe | None = None
        self.encoder = ""
        self.addItem(_t("Same as source"), "source")

    def set_probe(self, probe: RenderProbe, default: ColourDepth = "source") -> None:
        self.probe = probe
        self.blockSignals(True)
        self.clear()
        self.addItem(_t("Same as source ({n}-bit)").format(n=probe.bits), "source")
        twelve = probe.bits >= 12 or any(12 in d for d in probe.encoder_depths.values())
        for d in COLOUR_DEPTHS[1:]:
            if d != "12" or twelve:
                self.addItem(_t("{n}-bit").format(n=d), d)
        idx = self.findData(default)
        self.setCurrentIndex(idx if idx >= 0 else 0)
        self.blockSignals(False)

    def _at(self, index: int) -> ColourDepth:
        data = self.itemData(index)
        return next((d for d in COLOUR_DEPTHS if d == data), "source")

    def choice(self) -> ColourDepth:
        return self._at(self.currentIndex())

    def bits(self) -> int | None:
        """The depth the copy will be written with."""
        if self.probe is None or not self.encoder:
            return None
        return copy_bits(self.probe, self.encoder, self.choice())

    def set_encoder(self, encoder: str) -> None:
        self.encoder = encoder
        probe = self.probe
        model = cast(QStandardItemModel, self.model())
        if probe is None:
            return
        name = self._names(encoder)
        hdr10 = probe.hdr_class in _HDR10
        for i in range(self.count()):
            d = self._at(i)
            item = model.item(i)
            if d == "source" or item is None:
                continue
            got, n = copy_bits(probe, encoder, d), int(d)
            item.setEnabled(got == n)
            tip = ""
            if got != n:
                if hdr10 and n < 10:
                    tip = _t("HDR10 copies keep at least 10 bits.")
                elif got < n:
                    tip = _t("{encoder} writes at most {n} bits.").format(encoder=name, n=got)
                else:
                    tip = _t("{encoder} always writes {n} bits.").format(encoder=name, n=got)
            self.setItemData(i, tip or None, Qt.ItemDataRole.ToolTipRole)
        current = model.item(self.currentIndex())
        if current is not None and not current.isEnabled():
            self.setCurrentIndex(0)

    def note(self) -> str:
        """When the copy keeps fewer bits than the source: says so, and which
        other encoder keeps them. "" otherwise."""
        probe, encoder = self.probe, self.encoder
        if probe is None or not encoder or self.choice() != "source":
            return ""
        got = copy_bits(probe, encoder, "source")
        want = min(probe.bits, 12)
        if got >= want:
            return ""
        if encoder == "libx264" and copy_bits(probe, encoder, "10") >= want:
            return _t(
                "This copy will be {n}-bit; the source is {m}-bit. H.264 at {m} bits "
                "plays on fewer devices; choose {m}-bit to keep it anyway."
            ).format(n=got, m=want)
        text = _t("This copy will be {n}-bit; the source is {m}-bit.").format(n=got, m=want)
        other = next((e for e in probe.encoders if copy_bits(probe, e, "source") >= want), None)
        if other is not None:
            text += " " + _t("{encoder} keeps {m} bits.").format(encoder=self._names(other), m=want)
        return text


__all__ = ["DepthCombo", "depth_text", "hdr_text"]
