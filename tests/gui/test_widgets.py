# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U5 widgets and a11y helpers (docs/design/GUI.md §4.0, §6, §7 U5).

Run with QT_QPA_PLATFORM=offscreen QT_ACCESSIBILITY=1.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_ACCESSIBILITY", "1")

import dataclasses  # noqa: E402
import itertools  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from fractions import Fraction  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QDropEvent,
    QGuiApplication,
    QImage,
    QPainter,
    QPalette,
)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)
from pytestqt.qtbot import QtBot  # noqa: E402

from buttereye.core.api import (  # noqa: E402
    BackendId,
    BenchMeasurement,
    ButterEyeError,
    CapState,
    ErrorCode,
    Feature,
    Msg,
    NotAvailable,
    OperationCancelled,
    OpId,
    Progress,
    Reason,
    command_hint,
)
from buttereye.core.capabilities import unavailable_state  # noqa: E402
from buttereye.gui import a11y  # noqa: E402
from buttereye.gui.widgets import (  # noqa: E402
    BADGE_KINDS,
    BadgeKind,
    Banner,
    BenchPlot,
    CliHint,
    CopyField,
    DropZone,
    FractionEdit,
    OpRow,
    StatePanel,
    StatusBadge,
    human_bytes,
)
from buttereye.gui.widgets.bench_plot import contrast_ratio  # noqa: E402
from buttereye.gui.widgets.fraction_edit import FractionValidator, parse_rate  # noqa: E402
from buttereye.gui.widgets.status_badge import badge_pixmap  # noqa: E402

# ----------------------------------------------------------------- helpers


def dark_palette() -> QPalette:
    """A typical dark scheme (colour literals are fine in tests only)."""
    pal = QPalette()
    window, text = QColor(32, 33, 36), QColor(232, 234, 237)
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.Window, window)
        pal.setColor(group, QPalette.ColorRole.WindowText, text)
        pal.setColor(group, QPalette.ColorRole.Base, QColor(24, 25, 27))
        pal.setColor(group, QPalette.ColorRole.Text, text)
        pal.setColor(group, QPalette.ColorRole.Highlight, QColor(53, 132, 228))
        pal.setColor(group, QPalette.ColorRole.Mid, QColor(80, 80, 80))
    pal.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(120, 120, 120))
    return pal


def focusable_children(w: QWidget) -> Iterator[QWidget]:
    for child in itertools.chain([w], w.findChildren(QWidget)):
        if child.focusPolicy() & Qt.FocusPolicy.TabFocus:
            yield child


def assert_named(w: QWidget) -> None:
    for child in focusable_children(w):
        assert child.accessibleName().strip(), f"{type(child).__name__} has no accessible name"


def grey_bytes(img: QImage, background: QColor | None = None) -> bytes:
    """Flatten onto ``background`` (alpha would otherwise vanish) and drop colour."""
    flat = QImage(img.size(), QImage.Format.Format_ARGB32)
    flat.fill(background or QApplication.palette().color(QPalette.ColorRole.Window))
    p = QPainter(flat)
    p.drawImage(0, 0, img)
    p.end()
    g = flat.convertToFormat(QImage.Format.Format_Grayscale8)
    return bytes(g.constBits())[: g.sizeInBytes()]


def measurement(label: str, mpv: float, vs: float, *, faults: int | None = 0) -> BenchMeasurement:
    return BenchMeasurement(
        label=label,
        backend=BackendId.RIFE_NCNN,
        model=label,
        vspipe_fps=vs,
        mpv_fps=mpv,
        cov=0.01,
        repeatable=True,
        startup_s=1.0,
        reload_s=0.5,
        vram_bytes=None,
        realtime=mpv >= 119.88,
        gpu_faults=faults,
    )


# ------------------------------------------------------------------- a11y


def test_labelled_sets_buddy_and_name(qtbot: QtBot) -> None:
    edit = QLineEdit()
    qtbot.addWidget(edit)
    label, w = a11y.labelled("&Width:", edit, description="Pixels")
    qtbot.addWidget(label)
    assert w is edit
    assert label.buddy() is edit
    assert edit.accessibleName() == "Width"
    assert edit.accessibleDescription() == "Pixels"
    assert a11y.plain("Save && &Quit") == "Save & Quit"


def test_require_name(qtbot: QtBot) -> None:
    w = QPushButton()
    qtbot.addWidget(w)
    with pytest.raises(ValueError):
        a11y.require_name(w)
    w.setAccessibleName("Go")
    assert a11y.require_name(w) is w


def test_announce_reaches_listeners(qtbot: QtBot) -> None:
    w = QLabel("x")
    qtbot.addWidget(w)
    seen: list[tuple[str, bool]] = []
    remove = a11y.add_announcement_listener(lambda _w, t, a: seen.append((t, a)))
    try:
        a11y.announce(w, "Applied (gen 8)")
        a11y.announce(w, "Interpolation turned off", assertive=True)
        a11y.announce(w, "")  # ignored
    finally:
        remove()
    a11y.announce(w, "after removal")
    assert seen == [("Applied (gen 8)", False), ("Interpolation turned off", True)]


# ------------------------------------------------------------- StatusBadge


@pytest.mark.parametrize("kind", BADGE_KINDS)
def test_badge_text_and_icon_per_state(qtbot: QtBot, kind: BadgeKind) -> None:
    b = StatusBadge()
    qtbot.addWidget(b)
    b.set_state(kind, "Vulkan loader")
    assert b.kind() == kind
    assert b.text_label.text() == "Vulkan loader"
    assert b.accessibleName() == "Vulkan loader"
    assert b.accessibleDescription() == b.kind_word(kind)
    pm = b.icon_label.pixmap()
    assert not pm.isNull()
    b.set_state(kind, "")
    assert b.text_label.text() == b.kind_word(kind)


def test_badge_rejects_unknown_kind(qtbot: QtBot) -> None:
    b = StatusBadge()
    qtbot.addWidget(b)
    with pytest.raises(ValueError):
        b.set_state("red", "x")  # type: ignore[arg-type]


def test_badge_glyphs_distinct_in_greyscale(qtbot: QtBot) -> None:
    pal = QApplication.palette()
    images = {
        k: grey_bytes(badge_pixmap(k, pal, 32, use_theme=False).toImage()) for k in BADGE_KINDS
    }
    for a, b in itertools.combinations(BADGE_KINDS, 2):
        assert images[a] != images[b], f"{a} and {b} look the same in greyscale"
    # the rendered widgets too (icon label grab)
    grabs = {}
    for k in BADGE_KINDS:
        badge = StatusBadge(k, "same text")
        qtbot.addWidget(badge)
        badge.show()
        grabs[k] = grey_bytes(badge.icon_label.grab().toImage())
    for a, b in itertools.combinations(BADGE_KINDS, 2):
        assert grabs[a] != grabs[b]


def test_badge_follows_palette(qtbot: QtBot) -> None:
    b = StatusBadge("blocking", "Problem")
    qtbot.addWidget(b)
    pal = dark_palette()
    b.setPalette(pal)
    img = b.icon_label.pixmap().toImage()
    want = pal.color(QPalette.ColorRole.WindowText)
    opaque = [
        img.pixelColor(x, y)
        for x in range(img.width())
        for y in range(img.height())
        if img.pixelColor(x, y).alpha() == 255
    ]
    assert opaque, "glyph has no solid pixels"
    assert all(c.rgb() == want.rgb() for c in opaque)


# ---------------------------------------------------------------- Banner


def test_banner_from_error(qtbot: QtBot) -> None:
    err = ButterEyeError(
        ErrorCode.FFMS2_MISSING,
        Msg("Render needs {what}.", {"what": "ffms2"}),
        Msg("Install it, then press F5."),
        detail="traceback line",
        commands=("sudo dnf install ffms2",),
    )
    clicked: list[int] = []
    b = Banner.from_error(err, actions=[("&Retry", lambda: clicked.append(1))])
    qtbot.addWidget(b)
    assert b.kind == "blocking"
    assert b.title() == "Render needs ffms2."
    assert b.body() == "Install it, then press F5."
    assert b.code_label.text() == "BE-4001"
    assert b.code_label.textInteractionFlags() & Qt.TextInteractionFlag.TextSelectableByKeyboard
    assert [f.text() for f in b.command_fields] == ["sudo dnf install ffms2"]
    assert b.buttons[0].text() == "&Retry"
    b.buttons[0].click()
    assert clicked == [1]
    assert b.detail_view is not None and b.detail_button is not None
    assert b.detail_view.isHidden()
    b.detail_button.click()
    assert not b.detail_view.isHidden()
    assert "BE-4001" in b.accessibleDescription()
    assert_named(b)


def test_banner_cancelled_is_neutral(qtbot: QtBot) -> None:
    b = Banner.from_error(OperationCancelled(ErrorCode.INTERNAL, Msg("cancelled")))
    qtbot.addWidget(b)
    assert b.kind == "off"
    assert b.title() == "Cancelled"
    assert b.code_label.isHidden()


def test_banner_not_available_is_info(qtbot: QtBot) -> None:
    state = unavailable_state(Feature.BENCH, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)
    b = Banner.from_error(NotAvailable.for_state(Feature.BENCH, state))
    qtbot.addWidget(b)
    assert b.kind == "info"
    assert b.code_label.text() == "BE-9001"


# -------------------------------------------------------- CopyField/CliHint


def test_copy_field_copies(qtbot: QtBot) -> None:
    f = CopyField("sudo dnf install ffms2", accessible_name="dnf command")
    qtbot.addWidget(f)
    assert f.line.isReadOnly()
    assert f.line.accessibleName() == "dnf command"
    assert "dnf command" in f.button.accessibleName()
    QGuiApplication.clipboard().setText("")
    with qtbot.waitSignal(f.copied):
        f.button.click()
    assert QGuiApplication.clipboard().text() == "sudo dnf install ffms2"
    QGuiApplication.clipboard().setText("")
    f.show()
    f.line.setFocus()
    QTest.keyClick(f.line, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert QGuiApplication.clipboard().text() == "sudo dnf install ffms2"
    with pytest.raises(ValueError):
        CopyField("x", accessible_name=" ")


@pytest.fixture
def cli_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from buttereye.gui.widgets import cli_hint

    monkeypatch.setattr(cli_hint, "cli_available", lambda: True)


def test_cli_hint(qtbot: QtBot, cli_present: None) -> None:
    h = CliHint()
    qtbot.addWidget(h)
    assert h.field.isHidden() and h.text() is None and not h.copy()
    h.set_hint(command_hint("doctor"))
    assert h.field.text() == "buttereye doctor"
    assert not h.field.isHidden()
    assert h.note.isHidden()
    # no command is proposed since SCOPE §4.1 adopted them; the note path still works
    h.set_hint(dataclasses.replace(command_hint("bench.apply", label="v4.26"), status="proposed"))
    assert h.hint() is not None and h.hint().status == "proposed"  # type: ignore[union-attr]
    assert not h.note.isHidden()
    QGuiApplication.clipboard().setText("")
    assert h.copy()
    assert QGuiApplication.clipboard().text() == h.text()
    assert_named(h)


def test_cli_hint_says_when_no_cli_is_installed(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No buttereye CLI in this build: every hint, in_scope or proposed, says so
    (visibly and in the accessible description) and can still be copied."""
    from buttereye.core.api import CLI_MISSING_NOTE, render
    from buttereye.gui.widgets import cli_hint

    monkeypatch.setattr(cli_hint, "cli_available", lambda: False)
    want = render(CLI_MISSING_NOTE)
    h = CliHint()
    qtbot.addWidget(h)
    for hint in (command_hint("doctor"), command_hint("bench.apply", label="v4.26")):
        h.set_hint(hint)
        assert not h.note.isHidden() and h.note.text() == want
        assert want in h.field.line.accessibleDescription()
        QGuiApplication.clipboard().setText("")
        assert h.copy() and QGuiApplication.clipboard().text() == hint.text()


def test_cli_hint_note_disappears_once_the_cli_ships(
    qtbot: QtBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    from buttereye.gui.widgets import cli_hint

    monkeypatch.setattr(cli_hint, "cli_available", lambda: True)
    h = CliHint(command_hint("doctor"))
    qtbot.addWidget(h)
    assert h.note.isHidden()
    assert h.field.line.accessibleDescription() == "buttereye doctor"


def test_real_build_marks_every_page_hint(make_window: Any) -> None:
    """In this build (no console script) no page shows a hint as a working command."""
    from buttereye.core.api import CLI_MISSING_NOTE, cli_available, render

    if cli_available():
        pytest.skip("a buttereye CLI is installed here")
    win = make_window("all_ready")
    hints = [h for p in win.pages for h in p.findChildren(CliHint) if h.hint() is not None]
    assert hints
    for h in hints:
        assert h.note.text() == render(CLI_MISSING_NOTE), h.text()


# ------------------------------------------------------------ FractionEdit


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("24000/1001", Fraction(24000, 1001)),
        ("60", Fraction(60)),
        (" 30000 / 1001 ", Fraction(30000, 1001)),
        ("23.976", Fraction("23.976")),
        ("0", None),
        ("1/0", None),
        ("abc", None),
        ("1.5/2", None),
        ("", None),
        ("24000/", None),
        ("99999", None),
    ],
)
def test_parse_rate(text: str, want: Fraction | None) -> None:
    assert parse_rate(text) == want


def test_fraction_edit(qtbot: QtBot) -> None:
    e = FractionEdit()
    qtbot.addWidget(e)
    assert e.value() is None and e.accessibleName()
    with qtbot.waitSignal(e.valueChanged) as sig:
        e.setValue(Fraction(24000, 1001))
    assert sig.args == [Fraction(24000, 1001)]
    assert e.text() == "24000/1001"
    e.setValue(Fraction(120))
    assert e.text() == "120"
    e.clear()
    assert e.value() is None
    e.show()
    e.setFocus()
    QTest.keyClicks(e, "x24a000/1001")  # invalid characters refused by the validator
    assert e.text() == "24000/1001"
    assert e.value() == Fraction(24000, 1001)
    with pytest.raises(ValueError):
        e.setValue(Fraction(0))
    v = FractionValidator()
    assert v.validate("24000/", 6)[0] is FractionValidator.State.Intermediate
    assert v.validate("24/1001", 7)[0] is FractionValidator.State.Acceptable
    assert v.validate("-5", 2)[0] is FractionValidator.State.Invalid


# --------------------------------------------------------------- DropZone


def test_drop_zone_rejects_non_local(qtbot: QtBot, tmp_path: Path) -> None:
    chosen: list[Path] = []
    z = DropZone(chooser=lambda _p: None)
    qtbot.addWidget(z)
    z.fileChosen.connect(chosen.append)
    assert z.accessibleName() == "Drop a video file here, or press Enter to choose one"
    assert z.focusPolicy() & Qt.FocusPolicy.TabFocus
    with qtbot.waitSignal(z.rejected) as sig:
        assert not z.offer_urls([QUrl("https://example.invalid/film.mkv")])
    assert "BE-3007" in sig.args[0]
    assert "Only local files" in sig.args[0]
    with qtbot.waitSignal(z.rejected):
        assert not z.offer_urls([QUrl.fromLocalFile(str(tmp_path))])
    film = tmp_path / "film.mkv"
    film.write_bytes(b"")
    assert z.offer_urls([QUrl.fromLocalFile(str(film))])
    assert chosen == [film]


def test_drop_zone_drop_event(qtbot: QtBot, tmp_path: Path) -> None:
    z = DropZone(chooser=lambda _p: None)
    qtbot.addWidget(z)
    z.show()
    rejected: list[str] = []
    chosen: list[Path] = []
    z.rejected.connect(rejected.append)
    z.fileChosen.connect(chosen.append)
    mime = QMimeData()
    mime.setUrls([QUrl("smb://server/share/film.mkv")])
    ev = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    # Qt delivers Drop only inside a real drag, so call the handler directly.
    z.dropEvent(ev)
    assert len(rejected) == 1 and not chosen
    film = tmp_path / "a b.mkv"
    film.write_bytes(b"")
    mime2 = QMimeData()
    mime2.setUrls([QUrl.fromLocalFile(str(film))])
    ev2 = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime2,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    z.dropEvent(ev2)
    assert chosen == [film]


def test_drop_zone_keyboard_opens_chooser(qtbot: QtBot, tmp_path: Path) -> None:
    film = tmp_path / "film.mkv"
    film.write_bytes(b"")
    calls: list[QWidget] = []

    def chooser(parent: QWidget) -> Path | None:
        calls.append(parent)
        return film

    z = DropZone(chooser=chooser)
    qtbot.addWidget(z)
    z.show()
    z.setFocus()
    with qtbot.waitSignal(z.fileChosen) as sig:
        QTest.keyClick(z, Qt.Key.Key_Return)
    assert sig.args == [film]
    QTest.keyClick(z, Qt.Key.Key_Space)
    assert calls == [z, z]
    z.setEnabled(False)
    z.choose()
    assert len(calls) == 2
    QTest.mouseClick(z, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(2, 2))
    assert len(calls) == 2


# --------------------------------------------------------------- BenchPlot


def test_bench_plot_contrast_dark(qtbot: QtBot) -> None:
    plot = BenchPlot()
    qtbot.addWidget(plot)
    plot.setPalette(dark_palette())
    plot.set_data(
        [measurement("v4.26", 74.0, 81.0), measurement("v4.22-lite", 130.0, 150.0)], 119.88
    )
    fg, bg = plot.label_colors()
    assert contrast_ratio(fg, bg) >= 4.5
    plot.resize(plot.sizeHint())
    img = plot.grab().toImage()
    assert not img.isNull()
    # the background really is the palette's Window colour (labels sit on it)
    assert img.pixelColor(1, 1).rgb() == bg.rgb()
    # text pixels in the label colour are present
    colours = {
        img.pixelColor(x, y).rgb() for x in range(0, img.width(), 2) for y in range(img.height())
    }
    assert fg.rgb() in colours


def test_bench_plot_description_and_focus(qtbot: QtBot) -> None:
    plot = BenchPlot()
    qtbot.addWidget(plot)
    assert plot.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert plot.accessibleName()
    assert plot.accessibleDescription() == "No measurements"
    rows = [
        measurement("v4.26", 74.0, 81.0),
        measurement("v4.25-lite", 140.0, 150.0, faults=1),  # fastest but faulting
        measurement("v4.22-lite", 120.0, 130.0),
    ]
    plot.set_data(rows, 119.88)
    assert plot.best() is rows[2]
    assert plot.accessibleDescription() == "Best: v4.22-lite, 120 fps in mpv, real time"
    assert "GPU fault" in plot.row_label(rows[1])
    plot.set_data([rows[1]], None)
    assert plot.best() is None
    assert "none is recommended" in plot.accessibleDescription()
    assert plot.target_fps() is None


# -------------------------------------------------------------- StatePanel


def state_of(panel: StatePanel) -> str:
    return panel.state


def test_state_panel_unavailable_shows_code_and_hint(qtbot: QtBot) -> None:
    content = QWidget()
    rows_label = QLabel("data row", content)
    panel = StatePanel(content=content)
    qtbot.addWidget(panel)
    state = unavailable_state(Feature.LIVE, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)
    panel.show_unavailable(Feature.LIVE, state, command_hint("play", file="film.mkv"))
    panel.show()
    assert state_of(panel) == "unavailable"
    head, body, code = panel.unavailable_texts()
    assert head == "Live playback control isn't in this build yet."
    assert body.startswith("This build can check your system")
    assert code == "BE-9001"
    assert panel.unavailable_code.textInteractionFlags() & (
        Qt.TextInteractionFlag.TextSelectableByMouse
    )
    assert panel.unavailable_hint.field.text() == "buttereye play film.mkv"
    assert not rows_label.isVisible()  # zero data rows
    assert_named(panel)


def test_state_panel_unavailable_commands(qtbot: QtBot) -> None:
    panel = StatePanel()
    qtbot.addWidget(panel)
    state = unavailable_state(
        Feature.RENDER,
        Reason.MISSING_DEPENDENCY,
        ErrorCode.FFMS2_MISSING,
        commands=("sudo dnf install ffms2",),
    )
    panel.show_unavailable(Feature.RENDER, state, None)
    head, _body, code = panel.unavailable_texts()
    assert head == "Render needs ffms2."
    assert code == "BE-4001"
    assert [f.text() for f in panel.unavailable_commands] == ["sudo dnf install ffms2"]
    assert panel.unavailable_badge.kind() == "degraded"
    assert panel.unavailable_hint.field.isHidden()
    # code comes from the reason when the state carries none
    bare = CapState(False, Reason.NOT_IMPLEMENTED, None, Msg("Not here.\nLater."))
    panel.show_unavailable(Feature.REPORT, bare, None)
    assert panel.unavailable_texts() == ("Not here.", "Later.", "BE-9001")
    assert panel.unavailable_commands == []


def test_state_panel_states(qtbot: QtBot) -> None:
    panel = StatePanel()
    qtbot.addWidget(panel)
    panel.show()
    assert state_of(panel) == "content" and panel.currentWidget() is panel.content
    hits: list[str] = []
    panel.show_empty("No measurements yet.", actions=[("&Run", lambda: hits.append("run"))])
    assert state_of(panel) == "empty" and panel.empty_label.text() == "No measurements yet."
    current = panel.currentWidget()
    assert current is not None
    buttons = current.findChildren(QPushButton)
    assert [b.text() for b in buttons] == ["&Run"]
    buttons[0].click()
    assert hits == ["run"]
    panel.show_loading("Checking your system", cancel=lambda: hits.append("cancel"))
    assert state_of(panel) == "loading" and panel.loading_bar.maximum() == 0
    panel.loading_cancel.click()
    assert hits[-1] == "cancel" and not panel.loading_cancel.isEnabled()
    panel.show_loading("Loading")
    assert panel.loading_cancel.isHidden()
    err = ButterEyeError(ErrorCode.CONFIG_INVALID, Msg("Bad TOML."))
    panel.show_error(err)
    assert state_of(panel) == "error"
    assert panel.error_banner is not None and panel.error_banner.code_label.text() == "BE-2001"
    panel.show_error(ButterEyeError(ErrorCode.INTERNAL, Msg("Boom.")))
    assert panel.error_banner.code_label.text() == "BE-9999"
    panel.show_content()
    assert state_of(panel) == "content"
    assert_named(panel)


# ------------------------------------------------------------------ OpRow


def prog(done: int | None, total: int | None, **kw: object) -> Progress:
    return Progress(
        op_id=OpId("op"),
        phase=Msg(str(kw.pop("phase", "Checking mpv"))),
        done=done,
        total=total,
        unit=kw.pop("unit", "frames"),  # type: ignore[arg-type]
        rate=kw.pop("rate", None),  # type: ignore[arg-type]
        eta_s=kw.pop("eta_s", None),  # type: ignore[arg-type]
    )


def test_op_row_progress(qtbot: QtBot) -> None:
    cancelled: list[int] = []
    row = OpRow("Rendering film.mkv", lambda: cancelled.append(1))
    qtbot.addWidget(row)
    assert row.bar.accessibleName() == "Rendering film.mkv"
    row.update(prog(None, None))
    assert row.bar.maximum() == 0  # indeterminate
    row.update(prog(50, 100, rate=61.3, eta_s=125))
    assert row.value_permille() == 500 and row.bar.value() == 500
    assert row.rate_label.text() == "61.3 fps"
    assert "2:05" in row.eta_label.text()
    row.update(prog(40, 100))  # never backwards
    assert row.value_permille() == 500
    row.update(prog(None, None))  # unknown again: stays determinate at the last value
    assert row.bar.maximum() == 1000 and row.bar.value() == 500
    row.update()  # plain QWidget.update still works
    row.cancel_button.click()
    assert cancelled == [1] and not row.cancel_button.isEnabled()
    row.finish(False, "Cancelled — partial files removed.", cancelled=True)
    assert row.result_badge.kind() == "off"
    assert row.cancel_button.isHidden()
    row.update(prog(100, 100))  # ignored after finish
    assert row.value_permille() == 500


def test_op_row_large_bytes_and_success(qtbot: QtBot) -> None:
    row = OpRow("Downloading model", None)
    qtbot.addWidget(row)
    assert row.cancel_button.isHidden()
    total = 6 * 2**31
    row.update(prog(total // 4, total, unit="bytes", rate=12_500_000.0))
    assert row.value_permille() == 250
    assert row.rate_label.text() == "12.5 MB/s"
    row.finish(True, "Installed.")
    assert row.bar.value() == row.bar.maximum() == 1000
    assert row.result_badge.kind() == "ok"
    assert human_bytes(0) == "0 bytes"
    assert human_bytes(1_200_000_000) == "1.2 GB"


# ------------------------------------------------------- names by construction


def test_every_widget_named_by_construction(qtbot: QtBot) -> None:
    widgets: list[QWidget] = [
        StatusBadge(),
        Banner("degraded", "Title", "Body", "BE-1022", ("cmd one", "cmd two"), [("&Fix", print)]),
        StatePanel(),
        OpRow("Checking", lambda: None),
        CopyField("x", accessible_name="Path"),
        CliHint(command_hint("setup")),
        FractionEdit(),
        BenchPlot(),
        DropZone(chooser=lambda _p: None),
    ]
    for w in widgets:
        qtbot.addWidget(w)
        assert w.accessibleName(), type(w).__name__
        assert_named(w)


def test_no_colour_literals_in_widgets() -> None:
    import re

    root = Path(__file__).resolve().parents[2] / "buttereye" / "gui"
    files = [root / "a11y.py", *sorted((root / "widgets").glob("*.py"))]
    bad = re.compile(
        r"QColor\(|setStyleSheet|setPixelSize|#[0-9a-fA-F]{6}\b|Qt\.GlobalColor\.(?!transparent)"
    )
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert text.startswith("# SPDX-License-Identifier: AGPL-3.0-or-later"), f
        for ln in text.splitlines():
            if ln.lstrip().startswith(("#", "from ", "import ")):
                continue
            assert not bad.search(ln), f"{f.name}: {ln.strip()}"


def test_icons_licensed_and_present() -> None:
    root = Path(__file__).resolve().parents[2] / "buttereye" / "gui" / "icons"
    for kind in BADGE_KINDS:
        svg = root / f"{kind}.svg"
        assert svg.read_text(encoding="utf-8").startswith(
            "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->"
        )
