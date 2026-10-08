# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Status badge: a distinct glyph per state plus a word, never colour alone (§4.0).

Glyphs: ok = check, degraded = triangle, blocking = octagon, info = info circle,
busy = hourglass, off = pause. The icon theme is used when it provides the icon
(Hyprland/offscreen usually has none); otherwise the bundled monochrome SVG in
``buttereye/gui/icons/`` is recoloured with the palette's text colour, so the
glyph follows light and dark schemes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

BadgeKind = Literal["ok", "degraded", "blocking", "info", "busy", "off"]
BADGE_KINDS: tuple[BadgeKind, ...] = get_args(BadgeKind)

ICON_DIR = Path(__file__).resolve().parent.parent / "icons"

# Freedesktop icon names; None = always the bundled glyph (themes animate
# "process-working" as a sprite sheet, which reads badly as a static badge).
_THEME_NAMES: dict[BadgeKind, str | None] = {
    "ok": "emblem-default",
    "degraded": "dialog-warning",
    "blocking": "dialog-error",
    "info": "dialog-information",
    "busy": None,
    "off": "media-playback-pause",
}


def icon_path(kind: BadgeKind) -> Path:
    """Bundled SVG for ``kind``."""
    return ICON_DIR / f"{kind}.svg"


def tinted_pixmap(svg: Path, size: int, color: QColor, dpr: float = 1.0) -> QPixmap:
    """Render the monochrome ``svg`` at ``size`` logical px, recoloured to ``color``."""
    px_size = max(1, round(size * dpr))
    src = QIcon(str(svg)).pixmap(QSize(px_size, px_size))
    out = QPixmap(src.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, src)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    p.fillRect(out.rect(), color)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def badge_pixmap(
    kind: BadgeKind,
    palette: QPalette,
    size: int,
    dpr: float = 1.0,
    *,
    use_theme: bool = True,
    enabled: bool = True,
) -> QPixmap:
    """The glyph for ``kind``: theme icon when available, else the tinted bundled SVG."""
    theme_name = _THEME_NAMES[kind]
    mode = QIcon.Mode.Normal if enabled else QIcon.Mode.Disabled
    if use_theme and theme_name and QIcon.themeName() and QIcon.hasThemeIcon(theme_name):
        icon = QIcon.fromTheme(theme_name)
        pm = icon.pixmap(QSize(size, size), dpr, mode)
        if not pm.isNull():
            return pm
    group = QPalette.ColorGroup.Active if enabled else QPalette.ColorGroup.Disabled
    return tinted_pixmap(
        icon_path(kind), size, palette.color(group, QPalette.ColorRole.WindowText), dpr
    )


class StatusBadge(QWidget):
    """Glyph + word. ``set_state(kind, text)``; the word is ``text`` (or the kind's word)."""

    def __init__(
        self, kind: BadgeKind = "info", text: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._kind: BadgeKind = kind
        self._text = ""
        self.icon_label = QLabel(self)
        self.icon_label.setAccessibleName("")
        self.icon_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.text_label = QLabel(self)
        self.text_label.setTextFormat(Qt.TextFormat.PlainText)
        self.text_label.setWordWrap(True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.text_label, 1)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.set_state(kind, text)

    # ------------------------------------------------------------------ API
    def set_state(self, kind: BadgeKind, text: str) -> None:
        if kind not in BADGE_KINDS:
            raise ValueError(f"unknown badge kind {kind!r}")
        self._kind = kind
        self._text = text or self.kind_word(kind)
        self.text_label.setText(self._text)
        word = self.kind_word(kind)
        self.setAccessibleName(self._text)
        self.setAccessibleDescription(word if word != self._text else "")
        self.setToolTip(word if word != self._text else "")
        self._refresh_icon()

    def kind(self) -> BadgeKind:
        return self._kind

    def text(self) -> str:
        return self._text

    def kind_word(self, kind: BadgeKind) -> str:
        """The plain word for ``kind`` (what the glyph means)."""
        words: dict[BadgeKind, str] = {
            "ok": self.tr("OK"),
            "degraded": self.tr("Warning"),
            "blocking": self.tr("Problem"),
            "info": self.tr("Info"),
            "busy": self.tr("Working"),
            "off": self.tr("Off"),
        }
        return words[kind]

    def icon_size(self) -> int:
        return max(8, self.fontMetrics().height())

    # ------------------------------------------------------------- internals
    def _refresh_icon(self) -> None:
        size = self.icon_size()
        pm = badge_pixmap(
            self._kind, self.palette(), size, self.devicePixelRatioF(), enabled=self.isEnabled()
        )
        self.icon_label.setPixmap(pm)
        self.icon_label.setFixedSize(QSize(size, size))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.FontChange,
            QEvent.Type.StyleChange,
            QEvent.Type.EnabledChange,
            QEvent.Type.ApplicationPaletteChange,
        ):
            self._refresh_icon()
        super().changeEvent(event)


__all__ = [
    "BADGE_KINDS",
    "BadgeKind",
    "ICON_DIR",
    "StatusBadge",
    "badge_pixmap",
    "icon_path",
    "tinted_pixmap",
]
