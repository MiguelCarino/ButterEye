# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Theme and font scale (docs/design/GUI.md §4.0 "Theme", §6 ``test_theme_scale``)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import QFont, QImage, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.gui import theme


class Probe(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.palette_changes = 0
        self.paints = 0

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        if theme.palette_changed(event):
            self.palette_changes += 1
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.paints += 1
        p = QPainter(self)
        p.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Window))
        p.end()


@pytest.fixture
def controller(qapp: QApplication) -> Iterator[theme.ThemeController]:
    ctl = theme.install(qapp)
    try:
        yield ctl
    finally:
        ctl.apply_scheme(Qt.ColorScheme.Light)


def test_install_is_idempotent_and_uses_fusion(qapp: QApplication) -> None:
    ctl = theme.install(qapp)
    assert theme.install(qapp) is ctl
    assert isinstance(qapp.style(), theme.FocusFrameStyle)
    style = qapp.style()
    assert isinstance(style, theme.FocusFrameStyle)
    base = style.baseStyle()
    assert base is not None and base.name().lower() == "fusion"


def test_scheme_switch_repaints(controller: theme.ThemeController, qtbot: Any) -> None:
    probe = Probe()
    qtbot.addWidget(probe)
    probe.show()
    qtbot.waitExposed(probe)
    controller.apply_scheme(Qt.ColorScheme.Light)
    qtbot.wait(20)
    light = QApplication.palette()
    assert not theme.is_dark(light)
    before_changes, before_paints = probe.palette_changes, probe.paints

    # the real signal path: QStyleHints.colorSchemeChanged -> apply_scheme
    QApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    qtbot.waitUntil(lambda: probe.paints > before_paints, timeout=2000)
    dark = QApplication.palette()
    assert theme.is_dark(dark)
    assert probe.palette_changes > before_changes
    assert probe.palette().color(QPalette.ColorRole.Window) == dark.color(QPalette.ColorRole.Window)

    QApplication.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
    qtbot.waitUntil(lambda: not theme.is_dark(QApplication.palette()), timeout=2000)


def test_dark_palette_keeps_text_contrast() -> None:
    light = theme.FocusFrameStyle().standardPalette()
    dark = theme.derived_dark_palette(light)
    for bg, fg in (
        (QPalette.ColorRole.Window, QPalette.ColorRole.WindowText),
        (QPalette.ColorRole.Base, QPalette.ColorRole.Text),
        (QPalette.ColorRole.Button, QPalette.ColorRole.ButtonText),
    ):
        diff = abs(dark.color(bg).lightness() - dark.color(fg).lightness())
        assert diff > 120, (bg, fg, diff)
    assert theme.is_dark(dark)


def test_focus_frame_follows_widgets_without_focus_drawing(
    controller: theme.ThemeController, qtbot: Any
) -> None:
    host = QWidget()
    qtbot.addWidget(host)
    lay = QVBoxLayout(host)
    label = QLabel("selectable")
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    button = QPushButton("&Button")
    lay.addWidget(label)
    lay.addWidget(button)
    host.show()
    qtbot.waitExposed(host)
    host.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is host, timeout=2000)
    label.setFocus()
    qtbot.waitUntil(lambda: controller.focus_follower.frame is not None, timeout=1000)
    frame = controller.focus_follower.frame
    assert frame is not None and frame.widget() is label and frame.isVisible()
    button.setFocus()
    qtbot.wait(20)
    assert not frame.isVisible()  # Fusion draws the button's focus itself


def test_focus_rect_is_two_pixels_of_highlight(controller: theme.ThemeController) -> None:
    from PySide6.QtWidgets import QStyle, QStyleOptionFocusRect

    img = QImage(40, 20, QImage.Format.Format_ARGB32)
    img.fill(0)
    opt = QStyleOptionFocusRect()
    opt.rect = img.rect()
    opt.palette = QApplication.palette()
    p = QPainter(img)
    controller.style.drawPrimitive(QStyle.PrimitiveElement.PE_FrameFocusRect, opt, p, None)
    p.end()
    hl = QApplication.palette().color(QPalette.ColorRole.Highlight).rgb()
    assert img.pixel(QPoint(0, 10)) == hl
    assert img.pixel(QPoint(1, 10)) == hl
    assert img.pixel(QPoint(2, 10)) != hl


def test_heading_font_scales_point_size() -> None:
    base = QFont()
    base.setPointSizeF(10.0)
    h = theme.heading_font(base)
    assert h.pointSizeF() == pytest.approx(12.5)
    assert h.bold()


def _too_small(root: QWidget) -> list[str]:
    out: list[str] = []
    for w in root.findChildren(QWidget):
        if not w.isVisibleTo(root) or w.isWindow():
            continue
        parent = w.parentWidget()
        lay = parent.layout() if parent is not None else None
        if parent is None or lay is None or lay.indexOf(w) < 0:
            continue  # only layout-managed widgets have a size we control
        if isinstance(parent, QAbstractScrollArea) or isinstance(w, QAbstractScrollArea):
            continue  # scroll areas scroll instead (§4.0)
        hint = w.minimumSizeHint()
        if not hint.isValid():
            continue
        if w.width() + 1 < hint.width() or w.height() + 1 < hint.height():
            out.append(
                f"{type(w).__name__} {w.objectName()!r}: {w.width()}x{w.height()} < "
                f"{hint.width()}x{hint.height()}"
            )
    return out


def test_double_font_nothing_below_minimum_size(
    qapp: QApplication, make_window: Any, qtbot: Any
) -> None:
    original = QFont(qapp.font())
    big = QFont(original)
    big.setPointSizeF((original.pointSizeF() if original.pointSizeF() > 0 else 10.0) * 2)
    qapp.setFont(big)
    try:
        win = make_window("all_ready")
        win.resize(800, 600)
        problems: list[str] = []
        for i in range(len(win.pages)):
            win.sidebar.setCurrentRow(i)
            qtbot.wait(120)
            problems += [f"page {i}: {p}" for p in _too_small(win.page_stack.currentWidget())]
        problems += _too_small(win.centralWidget())
        assert not problems, "\n".join(problems)
        label = win.status_label
        assert label.font().pointSizeF() == pytest.approx(big.pointSizeF())
    finally:
        qapp.setFont(original)


# ---------------------------------------------------------------- review fixes

_R = QPalette.ColorRole
_ENABLED = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive)


def _schemes(controller: theme.ThemeController) -> Iterator[Qt.ColorScheme]:
    for scheme in (Qt.ColorScheme.Light, Qt.ColorScheme.Dark):
        controller.apply_scheme(scheme)
        assert theme.is_dark(QApplication.palette()) is (scheme == Qt.ColorScheme.Dark)
        yield scheme


def test_applied_palettes_meet_contrast_minimums(controller: theme.ThemeController) -> None:
    for scheme in _schemes(controller):
        pal = QApplication.palette()
        for g in _ENABLED:

            def c(
                a: QPalette.ColorRole, b: QPalette.ColorRole, g: Any = g, pal: Any = pal
            ) -> float:
                return theme.contrast_ratio(pal.color(g, a), pal.color(g, b))

            assert c(_R.HighlightedText, _R.Highlight) >= 4.5, (scheme, g)
            assert c(_R.Highlight, _R.Window) >= 3.0, (scheme, g)  # 2 px focus frame
            assert c(_R.HighlightedText, _R.Accent) >= 4.5, (scheme, g)
            assert c(_R.Mid, _R.Window) >= 3.0 and c(_R.Mid, _R.Base) >= 3.0, (scheme, g)
            assert c(_R.Link, _R.Base) >= 4.5 and c(_R.LinkVisited, _R.Base) >= 4.5, (scheme, g)
            for bg, fg in ((_R.Window, _R.WindowText), (_R.Base, _R.Text)):
                assert c(fg, bg) >= 4.5, (scheme, g, bg, fg)


def test_with_contrast_keeps_hue() -> None:
    from PySide6.QtGui import QColor

    blue, white = QColor(48, 140, 198), QColor(255, 255, 255)
    out = theme.with_contrast(blue, (white,), 4.5)
    assert theme.contrast_ratio(out, white) >= 4.5
    assert abs(out.hslHue() - blue.hslHue()) <= 2


def _edge_contrast(w: QWidget, rect: Any, window: Any) -> float:
    from PySide6.QtGui import QColor

    img = w.grab().toImage()
    return max(
        theme.contrast_ratio(QColor(img.pixel(x, y)), window)
        for x in range(max(rect.left(), 0), min(rect.right() + 1, img.width()))
        for y in range(max(rect.top(), 0), min(rect.bottom() + 1, img.height()))
    )


def test_unchecked_controls_have_a_3_to_1_boundary(
    controller: theme.ThemeController, qtbot: Any
) -> None:
    """WCAG 1.4.11: an unchecked box/radio and an empty field must be visible."""
    from PySide6.QtWidgets import (
        QCheckBox,
        QLineEdit,
        QRadioButton,
        QStyle,
        QStyleOptionButton,
    )

    for scheme in _schemes(controller):
        host = QWidget()
        qtbot.addWidget(host)
        lay = QVBoxLayout(host)
        check, radio, edit = (
            QCheckBox("Show experimental options"),
            QRadioButton("MVTools"),
            QLineEdit(),
        )
        for w in (check, radio, edit):
            lay.addWidget(w)
        host.show()
        qtbot.waitExposed(host)
        window = QApplication.palette().color(_R.Window)
        for w, sub in (
            (check, QStyle.SubElement.SE_CheckBoxIndicator),
            (radio, QStyle.SubElement.SE_RadioButtonIndicator),
        ):
            opt = QStyleOptionButton()
            w.initStyleOption(opt)
            rect = w.style().subElementRect(sub, opt, w)
            assert _edge_contrast(w, rect, window) >= 3.0, (scheme, type(w).__name__)
        assert _edge_contrast(edit, edit.rect(), window) >= 3.0, (scheme, "QLineEdit")
        host.close()


def test_drop_zone_border_meets_3_to_1(controller: theme.ThemeController, qtbot: Any) -> None:
    from buttereye.gui.widgets.drop_zone import DropZone

    for scheme in _schemes(controller):
        zone = DropZone()
        qtbot.addWidget(zone)
        zone.resize(240, 120)
        zone.show()
        qtbot.waitExposed(zone)
        window = zone.palette().color(_R.Window)
        assert _edge_contrast(zone, zone.rect(), window) >= 3.0, scheme
        zone.close()


def _fixed_and_heading(qapp: QApplication) -> tuple[Any, QLabel]:
    from buttereye.gui import a11y
    from buttereye.gui.widgets.copy_field import CopyField

    field = CopyField("buttereye doctor", accessible_name="Command")
    head = a11y.heading(QLabel("ButterEye 0.1.0"))
    return field, head


def test_monospace_and_headings_follow_font_scaling(
    qapp: QApplication, controller: theme.ThemeController
) -> None:
    original = QFont(qapp.font())
    base = original.pointSizeF() if original.pointSizeF() > 0 else 10.0
    try:
        # built at the larger size
        big = QFont(original)
        big.setPointSizeF(base * 1.5)
        qapp.setFont(big)
        field, head = _fixed_and_heading(qapp)
        assert field.line.font().pointSizeF() >= big.pointSizeF() - 0.01
        assert head.font().pointSizeF() == pytest.approx(big.pointSizeF() * theme.HEADING_FACTOR)
        # ... and after a runtime change
        bigger = QFont(original)
        bigger.setPointSizeF(base * 2)
        qapp.setFont(bigger)
        assert field.line.font().pointSizeF() >= bigger.pointSizeF() - 0.01
        assert head.font().pointSizeF() == pytest.approx(bigger.pointSizeF() * theme.HEADING_FACTOR)
        assert head.font().bold()
        assert (
            field.line.font().fixedPitch()
            or field.line.font().family() == theme.fixed_font().family()
        )
        field.deleteLater()
        head.deleteLater()
    finally:
        qapp.setFont(original)


def test_fixed_font_never_below_system_monospace(qapp: QApplication) -> None:
    from PySide6.QtGui import QFontDatabase

    system = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).pointSizeF()
    tiny = QFont(qapp.font())
    tiny.setPointSizeF(4.0)
    assert theme.fixed_font(tiny).pointSizeF() >= system


def test_form_labels_are_not_clipped_at_one_and_a_half(
    qapp: QApplication, make_window: Any, qtbot: Any
) -> None:
    """Wrapped grid labels lost lines ("Target rate (fps)", "Display refresh at
    least"): a QGridLayout row takes its height from the field, so a short form
    label must not break onto two lines at all, and nothing may be cut."""
    original = QFont(qapp.font())
    big = QFont(original)
    big.setPointSizeF((original.pointSizeF() if original.pointSizeF() > 0 else 10.0) * 1.5)
    qapp.setFont(big)
    try:
        win = make_window("all_ready")
        win.resize(800, 600)
        problems: list[str] = []
        for i in range(len(win.pages)):
            win.sidebar.setCurrentRow(i)
            qtbot.wait(120)
            page = win.page_stack.currentWidget()
            for label in page.findChildren(QLabel):
                if not label.isVisibleTo(page) or label.buddy() is None or not label.text():
                    continue
                broken = label.width() + 1 < label.sizeHint().width()
                short = len(label.text()) <= 40
                if label.wordWrap() and broken and short:
                    problems.append(f"page {i}: short label {label.text()!r} wraps")
                elif label.wordWrap() and label.height() + 1 < label.heightForWidth(label.width()):
                    problems.append(f"page {i}: {label.text()!r} clipped (height)")
                elif not label.wordWrap() and broken:
                    problems.append(f"page {i}: {label.text()!r} clipped (width)")
        assert not problems, "\n".join(problems)
    finally:
        qapp.setFont(original)


def test_labelled_does_not_wrap_by_default() -> None:
    from PySide6.QtWidgets import QLineEdit

    from buttereye.gui import a11y

    label, _ = a11y.labelled("&Target rate (fps)", QLineEdit())
    assert not label.wordWrap()
    long_label, _ = a11y.labelled("A long explanation", QLineEdit(), wrap=True)
    assert long_label.wordWrap()
