# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Theme hooks (docs/design/GUI.md §4.0 "Theme").

Palette only: no colour literals, no style sheets with colours, no pixel font
sizes. Fusion is used (Hyprland has no Qt platform theme). The palette follows
the platform/portal colour scheme through ``QStyleHints.colorSchemeChanged``;
when the platform gives no dark palette for a dark scheme, one is derived from
Fusion's standard palette by inverting lightness (computed, not hard-coded).

``FocusFrameStyle`` draws a 2 px ``palette.highlight`` focus frame: it replaces
the style's thin focus rectangle, and a ``QFocusFrame`` follows the focus onto
widgets for which Fusion draws no focus indicator at all (labels, views,
custom widgets). It also outlines line edits, check boxes and radio buttons in
``Mid``: Fusion's own outline (``Window.darker()``) is about 1.1:1 against a dark
window and under 2:1 in light, below WCAG 1.4.11's 3:1.

``accessible_palette()`` runs on every palette the controller applies and
moves (lightness only, hue kept) the roles that carry contrast duties:

- ``Mid`` (control outlines, drop-zone border) to >= 3:1 against Window and Base;
- ``Highlight`` and ``Accent`` until ``HighlightedText`` on them is >= 4.5:1,
  keeping >= 3:1 against Window (the focus frame is drawn in Highlight);
- ``Link``/``LinkVisited`` to >= 4.5:1 against Base.

Accent (2026-10-07 redesign, GUI.md §12): ButterEye's one brand colour is a
warm butter gold, defined once below as an HSL triple (``BUTTER_HSL``) and
applied by ``butter_palette()`` to Highlight and Accent, with dark text
(HighlightedText) on it. It then goes through ``accessible_palette()`` like
every other role, so in a light scheme it settles on a deeper amber that keeps
>= 3:1 against the window and >= 4.5:1 under the dark text; in a dark scheme it
stays bright gold. No other colour is hard-coded.

Carino Systems branding (2026-10-10, owner decision): ButterEye wears the Carino
visual language (branding.carino.systems, ``carino-branding.css``) though it is
not a Carino product: near-black surfaces, the one Sharp Gold accent, hairline
cards, IBM Plex Sans text, IBM Plex Mono uppercase buttons and a Red Hat Display
wordmark. The brand is dark only, so the palette no longer follows the desktop's
light/dark scheme; ``CARINO`` holds the tokens (the only colour literals). The
brand palette still goes through ``accessible_palette()``: control outlines and
drop-zone borders are lifted from the #262626 hairline to >= 3:1, and focus stays a
2 px gold frame. Cards (``CARD_PROPERTY``) keep the decorative hairline. Buttons,
combo boxes and cards are painted by ``FocusFrameStyle`` (no style sheets), so the
focus frame keeps working. Fonts fall back to the system's when the Fedora font
packages (ibm-plex-sans-fonts, ibm-plex-mono-fonts, redhat-display-fonts) are
missing.

Font scaling: ``apply_fixed_font()`` gives monospace widgets the system fixed
family at a size that follows the application font, and ``a11y.heading()``
labels remember their factor; both are recomputed on
``QEvent.ApplicationFontChange`` (one application-level filter).
"""

from __future__ import annotations

import logging
from typing import Any, cast

import shiboken6
from PySide6.QtCore import QEvent, QObject, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSlider,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFocusFrame,
    QLabel,
    QLineEdit,
    QProxyStyle,
    QPushButton,
    QStyle,
    QStyleFactory,
    QStyleOption,
    QTabBar,
    QWidget,
)

from buttereye.gui.a11y import HEADING_PROPERTY

_log = logging.getLogger(__name__)

FOCUS_WIDTH = 2
HEADING_FACTOR = 1.25
#: Dynamic properties ``rescale_fonts`` looks for (set by ``apply_fixed_font``
#: and ``a11y.heading``).
FIXED_FONT_PROPERTY = "buttereyeFixedFont"
HEADING_FACTOR_PROPERTY = HEADING_PROPERTY

#: Widgets for which Fusion already draws a focus indication (border highlight or
#: a focus rectangle, which this style thickens). Everything else gets the frame.
_STYLE_DRAWS_FOCUS: tuple[type[QWidget], ...] = (
    QAbstractButton,
    QLineEdit,
    QAbstractSpinBox,
    QComboBox,
    QAbstractSlider,
    QTabBar,
)

_GROUPS = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled)
#: Groups held to the contrast minimums (disabled controls are exempt, WCAG 1.4.3/1.4.11).
_ENABLED_GROUPS = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)

TEXT_CONTRAST = 4.5
NON_TEXT_CONTRAST = 3.0
#: Margin over the minimums so rounding/antialiasing never dips below them.
_CONTRAST_MARGIN = 0.15


def relative_luminance(c: QColor) -> float:
    """WCAG 2.x relative luminance of ``c``."""

    def channel(v: float) -> float:
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(c.redF()) + 0.7152 * channel(c.greenF()) + 0.0722 * channel(c.blueF())


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG 2.x contrast ratio (1..21)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def with_contrast(color: QColor, against: tuple[QColor, ...], ratio: float) -> QColor:
    """``color`` with its HSL lightness moved away from ``against`` until it
    reaches ``ratio`` against every one of them (or lightness runs out).
    Hue, saturation and alpha are kept."""
    out = QColor(color)
    if not against or min(contrast_ratio(out, a) for a in against) >= ratio:
        return out
    mean_lum = sum(relative_luminance(a) for a in against) / len(against)
    step = 1 if mean_lum < 0.18 else -1  # lighten on dark backgrounds, else darken
    hue = max(out.hslHue(), 0)
    sat, light, alpha = out.hslSaturation(), out.lightness(), out.alpha()
    while 0 <= light + step <= 255:
        light += step
        out = QColor.fromHsl(hue, sat, light, alpha)
        if min(contrast_ratio(out, a) for a in against) >= ratio:
            break
    return out


def heading_font(base: QFont, factor: float = HEADING_FACTOR) -> QFont:
    """``base`` scaled to ``factor`` × its point size, bold (§4.0 headings)."""
    font = QFont(base)
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(size * factor)
    font.setBold(True)
    return font


def fixed_font(reference: QFont | None = None) -> QFont:
    """The system monospace family at a size that follows the application font.

    ``QFontDatabase.systemFont(FixedFont)`` has a fixed platform size (9 pt here)
    that ignores the application font. Scale it by the application font's size
    relative to the system's general font, never below the system monospace
    size (a monospace size the user picked is kept).
    """
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    ref = reference if reference is not None else QApplication.font()
    fixed_pt = font.pointSizeF()
    general_pt = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont).pointSizeF()
    ref_pt = ref.pointSizeF()
    if fixed_pt > 0 and general_pt > 0 and ref_pt > 0:
        font.setPointSizeF(max(fixed_pt, fixed_pt * ref_pt / general_pt))
    return font


def apply_fixed_font(w: QWidget) -> QWidget:
    """Give ``w`` ``fixed_font()``, kept in step with application font changes."""
    w.setProperty(FIXED_FONT_PROPERTY, True)
    w.setFont(fixed_font())
    return w


def rescale_fonts() -> None:
    """Re-derive the explicit fonts (monospace widgets, ``a11y.heading`` labels)
    from the current application font. Connected to ``fontChanged``."""
    app_font = QApplication.font()
    for w in QApplication.allWidgets():
        if not shiboken6.isValid(w):
            continue
        if w.property(FIXED_FONT_PROPERTY):
            w.setFont(fixed_font(app_font))
        if w.property(BUTTON_FONT_PROPERTY):
            w.setFont(button_font(app_font))
        factor = w.property(HEADING_FACTOR_PROPERTY)
        if isinstance(factor, int | float) and factor > 0:
            font = QFont(w.font())
            size = app_font.pointSizeF()
            if size > 0:
                font.setPointSizeF(size * float(factor))
            font.setBold(True)
            w.setFont(font)


def _draw_focus(painter: QPainter, rect: QRect, palette: QPalette) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    pen = QPen(palette.color(QPalette.ColorRole.Highlight))
    pen.setWidth(FOCUS_WIDTH)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    half = FOCUS_WIDTH // 2
    painter.drawRect(rect.adjusted(half, half, -half, -half))
    painter.restore()


_OUTLINED = frozenset(
    {
        QStyle.PrimitiveElement.PE_FrameLineEdit,
        QStyle.PrimitiveElement.PE_IndicatorCheckBox,
        QStyle.PrimitiveElement.PE_IndicatorRadioButton,
    }
)


def _draw_outline(
    element: QStyle.PrimitiveElement, option: QStyleOption, painter: QPainter
) -> None:
    """A 1 px ``Mid`` edge (>= 3:1 against Window/Base) over Fusion's faint one.

    A focused line edit keeps Fusion's highlight outline (its focus indicator).
    """
    state = option.state
    if element == QStyle.PrimitiveElement.PE_FrameLineEdit and (
        state & QStyle.StateFlag.State_HasFocus
    ):
        return
    painter.save()
    pen = QPen(option.palette.color(QPalette.ColorRole.Mid))
    pen.setWidth(1)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    r = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
    if element == QStyle.PrimitiveElement.PE_IndicatorRadioButton:
        # An antialiased 1 px circle never reaches full coverage: use 1.5 px.
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.drawEllipse(r.adjusted(0.5, 0.5, -0.5, -0.5))
    else:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.drawRect(r)
    painter.restore()


class FocusFrameStyle(QProxyStyle):
    """Fusion with a 2 px highlight-coloured focus frame; with ``brand`` set, Carino
    buttons, combo boxes and cards."""

    brand = False

    def drawPrimitive(
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            _draw_focus(painter, option.rect, option.palette)
            return
        if element == QStyle.PrimitiveElement.PE_PanelButtonCommand and self.brand:
            _draw_button(option, painter, widget)
            return
        super().drawPrimitive(element, option, painter, widget)
        if element in _OUTLINED:
            _draw_outline(element, option, painter)

    def drawControl(
        self,
        element: QStyle.ControlElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if element == QStyle.ControlElement.CE_FocusFrame:
            _draw_focus(painter, option.rect, option.palette)
            return
        if (
            element == QStyle.ControlElement.CE_ShapedFrame
            and self.brand
            and widget is not None
            and widget.property(CARD_PROPERTY)
        ):
            _draw_card(option, painter)
            return
        super().drawControl(element, option, painter, widget)

    def polish(self, arg: Any) -> Any:  # widget, palette or application overloads
        out = super().polish(arg)
        if self.brand and isinstance(arg, QPushButton):
            apply_button_font(arg)
        return out

    def sizeFromContents(  # type: ignore[override]  # PySide stubs disagree
        self,
        ct: QStyle.ContentsType,
        opt: QStyleOption,
        size: Any,
        widget: QWidget,
    ) -> Any:
        out = super().sizeFromContents(ct, opt, size, widget)
        if self.brand and ct == QStyle.ContentsType.CT_PushButton and opt is not None:
            line = opt.fontMetrics.height()
            out.setHeight(max(out.height(), round(line * BUTTON_MIN_LINES)))
            out.setWidth(out.width() + round(line * BUTTON_PAD_LINES))
        return out

    def pixelMetric(
        self,
        metric: QStyle.PixelMetric,
        option: QStyleOption | None = None,
        widget: QWidget | None = None,
    ) -> int:
        if metric in (
            QStyle.PixelMetric.PM_FocusFrameHMargin,
            QStyle.PixelMetric.PM_FocusFrameVMargin,
        ):
            return FOCUS_WIDTH
        return super().pixelMetric(metric, option, widget)


class _FocusFollower(QObject):
    """Moves one ``QFocusFrame`` onto the focused widget when the style draws none."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self._frame: QFocusFrame | None = None
        app.focusChanged.connect(self._on_focus)

    def needs_frame(self, w: QWidget | None) -> bool:
        if w is None or w.focusPolicy() == Qt.FocusPolicy.NoFocus:
            return False
        if isinstance(w, QFocusFrame) or w.isWindow():
            return False
        return not isinstance(w, _STYLE_DRAWS_FOCUS)

    def _on_focus(self, _old: QWidget | None, new: QWidget | None) -> None:
        frame = self._frame
        if frame is not None and not shiboken6.isValid(frame):
            frame = self._frame = None
        if not self.needs_frame(new):
            if frame is not None:
                frame.setWidget(cast(QWidget, None))  # nullptr: stop tracking
                frame.hide()
            return
        assert new is not None
        if frame is None:
            frame = self._frame = QFocusFrame(new)
            frame.setObjectName("buttereye-focus-frame")
        frame.setWidget(new)
        frame.show()

    @property
    def frame(self) -> QFocusFrame | None:
        if self._frame is not None and not shiboken6.isValid(self._frame):
            self._frame = None
        return self._frame


def _invert(color: QColor) -> QColor:
    hue = max(color.hslHue(), 0)  # -1 for achromatic colours
    return QColor.fromHsl(hue, color.hslSaturation(), 255 - color.lightness(), color.alpha())


def derived_dark_palette(light: QPalette) -> QPalette:
    """A dark palette computed from ``light`` (lightness inverted per role).

    Highlight, HighlightedText and Accent are kept as they are; Link and
    LinkVisited are inverted like the rest. ``accessible_palette()`` (applied by
    the controller afterwards) brings all of them, and Mid, to their contrast
    minimums.
    """
    dark = QPalette(light)
    keep = {
        QPalette.ColorRole.Highlight,
        QPalette.ColorRole.HighlightedText,
        QPalette.ColorRole.Accent,
    }
    for group in _GROUPS:
        for role_value in range(QPalette.ColorRole.NColorRoles.value):
            role = QPalette.ColorRole(role_value)
            if role in keep or role == QPalette.ColorRole.NoRole:
                continue
            dark.setColor(group, role, _invert(light.color(group, role)))
    return dark


def accessible_palette(palette: QPalette) -> QPalette:
    """``palette`` with Mid, Highlight/Accent and Link roles moved to their
    contrast minimums (see the module docstring). Pure: returns a copy."""
    out = QPalette(palette)
    R = QPalette.ColorRole  # noqa: N806 - enum alias
    text_min = TEXT_CONTRAST + _CONTRAST_MARGIN
    ui_min = NON_TEXT_CONTRAST + _CONTRAST_MARGIN
    for group in _ENABLED_GROUPS:
        window, base = out.color(group, R.Window), out.color(group, R.Base)
        out.setColor(group, R.Mid, with_contrast(out.color(group, R.Mid), (window, base), ui_min))
        on_hl = out.color(group, R.HighlightedText)
        for role in (R.Highlight, R.Accent):
            tuned = with_contrast(out.color(group, role), (on_hl,), text_min)
            if contrast_ratio(tuned, window) < NON_TEXT_CONTRAST:
                # Darkening towards white text lost the window edge: settle for
                # the lightness that keeps both (dark schemes only, in practice).
                tuned = _best_between(out.color(group, role), on_hl, window)
            out.setColor(group, role, tuned)
        for role in (R.Link, R.LinkVisited):
            out.setColor(group, role, with_contrast(out.color(group, role), (base,), text_min))
    return out


def _best_between(color: QColor, text: QColor, window: QColor) -> QColor:
    """The lightness of ``color`` maximising min(contrast to text / 4.5, to window / 3)."""
    hue = max(color.hslHue(), 0)
    best, score = QColor(color), -1.0
    for light in range(256):
        c = QColor.fromHsl(hue, color.hslSaturation(), light, color.alpha())
        sc = min(
            contrast_ratio(c, text) / TEXT_CONTRAST, contrast_ratio(c, window) / NON_TEXT_CONTRAST
        )
        if sc > score:
            best, score = c, sc
    return best


#: Butter gold (about #E8B931) as (hue, saturation, lightness), Qt's 0-359/0-255
#: scales. The single brand colour (see the module docstring).
BUTTER_HSL = (45, 204, 140)


def butter() -> QColor:
    """The untuned butter accent colour."""
    h, s, lum = BUTTER_HSL
    return QColor.fromHsl(h, s, lum)


def butter_palette(palette: QPalette) -> QPalette:
    """``palette`` with the butter accent on Highlight/Accent and dark text on it.

    The dark text is the darkest of the palette's own text/background colours,
    so it follows the scheme. Run ``accessible_palette()`` afterwards.
    """
    out = QPalette(palette)
    R = QPalette.ColorRole  # noqa: N806 - enum alias
    for group in _ENABLED_GROUPS:
        candidates = [out.color(group, r) for r in (R.WindowText, R.Text, R.Window, R.Base)]
        dark = min(candidates, key=relative_luminance)
        out.setColor(group, R.Highlight, butter())
        out.setColor(group, R.Accent, butter())
        out.setColor(group, R.HighlightedText, dark)
    return out


#: Carino Systems design tokens (carino-branding.css :root), the brand's colours.
CARINO: dict[str, str] = {
    "bg": "#050505",
    "bg_elev": "#0d0d0d",
    "bg_card": "#0b0b0b",
    "accent": "#eab308",
    "accent_hover": "#fbbf24",
    "border": "#262626",
    "text": "#ffffff",
    "text_sec": "#a3a3a3",
    "text_muted": "#666666",
    "ok": "#22c55e",
    "warn": "#eab308",
    "err": "#ef4444",
    "info": "#38bdf8",
    # --gold-text gradient stops (the wordmark)
    "gold_light": "#fef08a",
    "gold_dark": "#b45309",
}
SANS = "IBM Plex Sans"
MONO = "IBM Plex Mono"
DISPLAY = "Red Hat Display"
#: QFrames with this dynamic property set are drawn as Carino cards.
CARD_PROPERTY = "carinoCard"
BUTTON_RADIUS = 6
CARD_RADIUS = 10


def token(name: str) -> QColor:
    return QColor(CARINO[name])


def carino_palette(base: QPalette) -> QPalette:
    """The Carino palette (dark only) on top of ``base`` (Fusion's, for the
    shading roles). Run ``accessible_palette()`` afterwards."""
    out = QPalette(base)
    R = QPalette.ColorRole  # noqa: N806 - enum alias
    roles = {
        R.Window: "bg",
        R.WindowText: "text",
        R.Base: "bg_elev",
        R.AlternateBase: "bg_card",
        R.Text: "text",
        R.Button: "bg_elev",
        R.ButtonText: "text",
        R.BrightText: "text",
        R.Highlight: "accent",
        R.HighlightedText: "bg",
        R.Accent: "accent",
        R.Mid: "border",
        R.Link: "accent",
        R.LinkVisited: "accent_hover",
        R.ToolTipBase: "bg_elev",
        R.ToolTipText: "text",
        R.PlaceholderText: "text_sec",
    }
    for group in _GROUPS:
        for role, name in roles.items():
            out.setColor(group, role, token(name))
        out.setColor(group, R.Dark, token("bg"))
        out.setColor(group, R.Shadow, token("bg"))
        out.setColor(group, R.Light, token("border").lighter(150))
        out.setColor(group, R.Midlight, token("border"))
    dis = QPalette.ColorGroup.Disabled
    for role in (R.WindowText, R.Text, R.ButtonText):
        out.setColor(dis, role, token("text_muted"))
    return out


def secondary(w: QWidget) -> QWidget:
    """Carino secondary text (#a3a3a3, about 8:1 on the background)."""
    pal = w.palette()
    for group in _GROUPS:
        pal.setColor(group, QPalette.ColorRole.WindowText, token("text_sec"))
    w.setPalette(pal)
    return w


def _has_family(family: str) -> bool:
    return family in QFontDatabase.families()


def brand_font(family: str, reference: QFont | None = None) -> QFont:
    """``family`` at the reference (application) size, or the reference font
    when the family isn't installed."""
    ref = QFont(reference) if reference is not None else QApplication.font()
    if not _has_family(family):
        return ref
    f = QFont(ref)
    f.setFamilies([family])
    return f


def button_font(reference: QFont | None = None) -> QFont:
    """Buttons: IBM Plex Sans semibold in sentence case. (The brand's web buttons
    are small uppercase mono; in the app readability wins, GUI.md §12.3.)"""
    f = brand_font(SANS, reference)
    f.setWeight(QFont.Weight.DemiBold)
    return f


#: buttons are at least this many text lines tall (~32 px at a 15 px font, over
#: WCAG 2.5.8's 24 px target) with this many lines of side padding
BUTTON_MIN_LINES = 2.1
BUTTON_PAD_LINES = 0.9


def wordmark_font(reference: QFont, factor: float) -> QFont:
    """Red Hat Display Black, uppercase, ``factor`` × the reference size."""
    f = brand_font(DISPLAY, reference)
    f.setWeight(QFont.Weight.Black)
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.0)
    if reference.pointSizeF() > 0:
        f.setPointSizeF(reference.pointSizeF() * factor)
    return f


def kicker(label: QLabel) -> QLabel:
    """A Carino kicker: a section label in IBM Plex Mono, uppercase, letter-spaced,
    in gold (about 10:1 on the background). Same size as body text, bold."""
    f = brand_font(MONO, label.font())
    f.setWeight(QFont.Weight.Bold)
    f.setCapitalization(QFont.Capitalization.AllUppercase)
    f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
    label.setFont(f)
    label.setTextFormat(Qt.TextFormat.PlainText)
    pal = label.palette()
    for group in _GROUPS:
        pal.setColor(group, QPalette.ColorRole.WindowText, token("accent"))
    label.setPalette(pal)
    label.setAccessibleDescription("heading")
    return label


def install_brand_fonts(app: QApplication) -> None:
    """IBM Plex Sans for text and Plex Mono uppercase for buttons, at the size the
    desktop chose (no fixed pixel sizes)."""
    base = app.font()
    if _has_family(SANS):
        app.setFont(brand_font(SANS, base))
    # buttons get their face per widget (FocusFrameStyle.polish, rescale_fonts):
    # a class font set here is cleared by any later application-wide setFont


BUTTON_FONT_PROPERTY = "carinoButtonFont"


def apply_button_font(w: QWidget) -> None:
    w.setProperty(BUTTON_FONT_PROPERTY, True)
    w.setFont(button_font(QApplication.font()))


def _draw_button(option: QStyleOption, painter: QPainter, widget: QWidget | None) -> None:
    """A Carino button: rounded, filled with the palette's Button (gold for the
    primary button), a >= 3:1 Mid edge, gold edge and faint gold on hover."""
    pal = option.palette
    state = option.state
    enabled = bool(state & QStyle.StateFlag.State_Enabled)
    hover = enabled and bool(state & QStyle.StateFlag.State_MouseOver)
    pressed = bool(state & (QStyle.StateFlag.State_Sunken | QStyle.StateFlag.State_On))
    fill = pal.color(QPalette.ColorRole.Button)
    accent = pal.color(QPalette.ColorRole.Highlight)
    primary = fill == accent
    if primary:
        if hover:
            fill = token("accent_hover")
        if pressed:
            fill = fill.darker(112)
        edge = fill
    else:
        if hover:
            faint = QColor(accent)
            faint.setAlphaF(0.08)
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            r0 = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
            painter.drawRoundedRect(r0, BUTTON_RADIUS, BUTTON_RADIUS)
            painter.restore()
            fill = faint
        if pressed:
            fill = pal.color(QPalette.ColorRole.Button).lighter(140)
        edge = accent if hover else pal.color(QPalette.ColorRole.Mid)
    if not enabled:
        fill.setAlphaF(fill.alphaF() * 0.5)
        edge.setAlphaF(edge.alphaF() * 0.6)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(edge)
    pen.setWidthF(1.0)
    painter.setPen(pen)
    painter.setBrush(fill)
    r = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
    painter.drawRoundedRect(r, BUTTON_RADIUS, BUTTON_RADIUS)
    painter.restore()


def _draw_card(option: QStyleOption, painter: QPainter) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(QPen(token("border"), 1.0))
    painter.setBrush(token("bg_card"))
    r = QRectF(option.rect).adjusted(0.5, 0.5, -0.5, -0.5)
    painter.drawRoundedRect(r, CARD_RADIUS, CARD_RADIUS)
    painter.restore()


def is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


class ThemeController(QObject):
    """Owns the style and keeps the palette in step with the colour scheme."""

    def __init__(self, app: QApplication, *, accent: bool = True, brand: bool = True) -> None:
        super().__init__(app)
        self._app = app
        self._accent = accent
        self._brand = brand
        base = QStyleFactory.create("Fusion")
        self.style = FocusFrameStyle(base)
        self.style.brand = brand
        if brand:
            install_brand_fonts(app)
        app.setStyle(self.style)
        self._follower = _FocusFollower(app)
        app.styleHints().colorSchemeChanged.connect(self.apply_scheme)
        app.fontChanged.connect(lambda _font: rescale_fonts())
        self.apply_scheme(app.styleHints().colorScheme())

    @property
    def brand(self) -> bool:
        return self._brand

    def set_brand(self, on: bool) -> None:
        """Carino branding on (the default) or off (the palette then follows the
        desktop's light/dark scheme, with the butter accent)."""
        self._brand = on
        self.style.brand = on
        self.apply_scheme(self._app.styleHints().colorScheme())

    @property
    def focus_follower(self) -> _FocusFollower:
        return self._follower

    def apply_scheme(self, scheme: Qt.ColorScheme) -> None:
        """Set the palette for ``scheme`` (connected to ``colorSchemeChanged``)."""
        palette = self.style.standardPalette()
        if self._brand:
            # the Carino brand is dark only: the desktop's scheme doesn't change it
            self._app.setPalette(accessible_palette(carino_palette(palette)))
            for w in self._app.topLevelWidgets():
                w.update()
            return
        if scheme == Qt.ColorScheme.Dark and not is_dark(palette):
            palette = derived_dark_palette(palette)
        elif scheme == Qt.ColorScheme.Light and is_dark(palette):
            palette = derived_dark_palette(palette)
        if self._accent:
            palette = butter_palette(palette)
        self._app.setPalette(accessible_palette(palette))
        # Widgets get QEvent.PaletteChange from setPalette; custom-painted ones
        # repaint on it. Force an update for top-levels so nothing stays stale.
        for w in self._app.topLevelWidgets():
            w.update()
        _log.debug("palette for colour scheme %s applied", scheme)


def install(app: QApplication) -> ThemeController:
    """Install the ButterEye theme hooks once per application."""
    existing = app.findChild(ThemeController)
    if existing is not None:
        return existing
    return ThemeController(app)


def palette_changed(event: QEvent) -> bool:
    """True for the events after which custom painting must re-read ``palette()``."""
    return event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange)


__all__ = [
    "CARD_PROPERTY",
    "CARINO",
    "DISPLAY",
    "MONO",
    "SANS",
    "brand_font",
    "BUTTON_MIN_LINES",
    "button_font",
    "carino_palette",
    "install_brand_fonts",
    "kicker",
    "secondary",
    "token",
    "wordmark_font",
    "FIXED_FONT_PROPERTY",
    "FOCUS_WIDTH",
    "FocusFrameStyle",
    "HEADING_FACTOR",
    "HEADING_FACTOR_PROPERTY",
    "NON_TEXT_CONTRAST",
    "TEXT_CONTRAST",
    "ThemeController",
    "BUTTER_HSL",
    "accessible_palette",
    "butter",
    "butter_palette",
    "apply_fixed_font",
    "contrast_ratio",
    "derived_dark_palette",
    "fixed_font",
    "heading_font",
    "install",
    "is_dark",
    "palette_changed",
    "relative_luminance",
    "rescale_fonts",
    "with_contrast",
]
