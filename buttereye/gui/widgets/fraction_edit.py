# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Exact frame-rate entry: ``24000/1001``, ``60`` or ``23.976`` (§4.4, rule tester).

Rates are ``Fraction`` end to end (§0 graft). A decimal is taken literally
(``23.976`` = 2997/125); type ``24000/1001`` for NTSC film. ``value()`` is
``None`` while the text is empty or incomplete.
"""

from __future__ import annotations

import re
from fractions import Fraction

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QValidator
from PySide6.QtWidgets import QLineEdit, QWidget

_FULL = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:/\s*(\d+))?\s*$")
_PARTIAL = re.compile(r"^\s*(\d+(?:\.\d*)?)?\s*(?:/\s*\d*)?\s*$")

MAX_RATE = Fraction(10_000)
"""Upper bound: no display or source runs above this."""


def parse_rate(text: str) -> Fraction | None:
    """Parse a positive rate ``n``, ``n.m`` or ``n/d``; ``None`` if not a valid rate."""
    m = _FULL.match(text)
    if m is None:
        return None
    num = Fraction(m.group(1))
    if m.group(2) is not None:
        if "." in m.group(1):
            return None
        den = int(m.group(2))
        if den == 0:
            return None
        num = num / den
    if num <= 0 or num > MAX_RATE:
        return None
    return num


def format_rate(value: Fraction) -> str:
    """``60`` for whole rates, else ``n/d``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


class FractionValidator(QValidator):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def validate(self, text: str, pos: int) -> tuple[QValidator.State, str, int]:
        if parse_rate(text) is not None:
            return QValidator.State.Acceptable, text, pos
        if _PARTIAL.match(text):
            return QValidator.State.Intermediate, text, pos
        return QValidator.State.Invalid, text, pos


class FractionEdit(QLineEdit):
    """Line edit for a positive ``Fraction``; ``valueChanged(Fraction | None)``."""

    valueChanged = Signal(object)

    def __init__(self, value: Fraction | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._value: Fraction | None = None
        self.setValidator(FractionValidator(self))
        self.setPlaceholderText("24000/1001")
        # Default name; labelled() overrides it with the visible label.
        self.setAccessibleName(self.tr("Frame rate"))
        self.setAccessibleDescription(
            self.tr("A whole number, a decimal, or a fraction such as 24000/1001")
        )
        self.textChanged.connect(self._on_text)
        if value is not None:
            self.setValue(value)

    def value(self) -> Fraction | None:
        return self._value

    def setValue(self, value: Fraction | None) -> None:  # noqa: N802 - Qt naming (frozen API)
        if value is not None and (value <= 0 or value > MAX_RATE):
            raise ValueError(f"rate out of range: {value}")
        self.setText("" if value is None else format_rate(value))
        self._on_text(self.text())

    def is_valid(self) -> bool:
        return self._value is not None

    def _on_text(self, text: str) -> None:
        new = parse_rate(text)
        if new != self._value:
            self._value = new
            self.valueChanged.emit(new)


__all__ = ["FractionEdit", "FractionValidator", "MAX_RATE", "format_rate", "parse_rate"]
