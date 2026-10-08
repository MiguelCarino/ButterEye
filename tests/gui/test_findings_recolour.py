# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Integration regression: finding glyphs follow a live light -> dark switch.

Found while screenshotting the real GUI: rows built under the light palette kept
black glyphs after the scheme turned dark, so Degraded/OK/Info icons vanished on
the System page (only the selected row stayed visible).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QColor, QImage, QPalette

from buttereye.core.api import ErrorCode, Finding, Msg, Section, Severity
from buttereye.gui.widgets.findings_view import FindingsView


def _finding(fid: str, sev: Severity) -> Finding:
    return Finding(
        id=fid,
        section=Section.RENDER,
        severity=sev,
        code=ErrorCode.FFMS2_MISSING if sev is Severity.DEGRADED else None,
        title=Msg(fid),
        cause=Msg("cause"),
        fix=Msg("fix"),
    )


def _mean_luma(view: FindingsView, row: int, child: int | None) -> float:
    item = view.model.item(row, 0)
    assert item is not None
    if child is not None:
        item = item.child(child, 0)
        assert item is not None
    size = view.fontMetrics().height()
    img: QImage = item.icon().pixmap(size, size).toImage()
    total = n = 0.0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.alpha() > 128:
                total += c.lightness()
                n += 1
    assert n > 0
    return total / n


def test_icons_are_recoloured_when_the_palette_turns_dark(qtbot: Any) -> None:
    view = FindingsView()
    qtbot.addWidget(view)
    light = QPalette(view.palette())
    light.setColor(QPalette.ColorRole.WindowText, QColor("black"))
    light.setColor(QPalette.ColorRole.Text, QColor("black"))
    view.setPalette(light)
    view.set_findings([_finding("a", Severity.DEGRADED), _finding("b", Severity.INFO)])
    assert _mean_luma(view, 0, None) < 100
    assert _mean_luma(view, 0, 0) < 100

    dark = QPalette(light)
    dark.setColor(QPalette.ColorRole.WindowText, QColor("white"))
    dark.setColor(QPalette.ColorRole.Text, QColor("white"))
    dark.setColor(QPalette.ColorRole.Window, QColor("black"))
    dark.setColor(QPalette.ColorRole.Base, QColor("black"))
    view.setPalette(dark)
    assert _mean_luma(view, 0, None) > 150  # group glyph
    assert _mean_luma(view, 0, 0) > 150  # row glyph
    assert _mean_luma(view, 1, 0) > 150
