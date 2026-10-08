# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The simple window and the Details dialog (docs/design/GUI.md §12).

Over FakeCore: the three choices become one ``simple`` profile with a single
catch-all rule; live sessions get ``apply_profile``; the switch drives
``set_interpolation``; rows follow sessions and events in plain words (no
error codes); GPU choices hide when RIFE can't run; a first run sets up and
starts the speed test silently; Details has four tabs; accessibility and the
dark palette.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from buttereye.core.api import (
    BackendId,
    BypassReason,
    ErrorCode,
    FilterState,
    Health,
    HealthChanged,
    Msg,
    Notice,
    Rule,
    RuleMatch,
    SessionEnded,
    SessionId,
    Target,
    TargetKind,
)
from buttereye.core.testing import scenarios as sc
from buttereye.gui import app as gui_app
from buttereye.gui import simple_window as sw
from buttereye.gui import theme
from buttereye.gui.details_dialog import TABS, DetailsDialog
from tests.gui.helpers import fake_core
from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle
from tests.gui.test_sessions_page import in_core

CODE = re.compile(r"BE-\d+")

Make = Callable[..., Any]


@pytest.fixture
def make_simple(qtbot: Any, make_bridge: Make, tmp_path: Path) -> Make:
    def factory(scenario: Any = "all_ready", *, ready: bool = True, **kw: Any) -> Any:
        bridge = make_bridge(scenario)
        settings = QSettings(str(tmp_path / f"gui-{id(bridge)}.ini"), QSettings.Format.IniFormat)
        kw.setdefault("auto_bench", False)
        win = sw.SimpleWindow(bridge, settings, log_path=tmp_path / "gui.log", **kw)
        qtbot.addWidget(win, before_close_func=_force_close)
        win.show()
        qtbot.waitExposed(win)
        if ready:
            qtbot.waitUntil(lambda: win.status_text() == "Ready.", timeout=5000)
        return win

    return factory


def _force_close(win: Any) -> None:
    win._may_close = True
    if win.details is not None:
        win.details.close()


def calls(win: Any, name: str) -> list[Any]:
    return [c for c in fake_core(win.bridge).fake_calls if c.name == name]


def saved(win: Any) -> Any:
    return calls(win, "save_config")[-1].args[0]


def pick(combo: Any, key: str) -> None:
    i = combo.findData(key)
    assert i >= 0, key
    combo.setCurrentIndex(i)


def texts(root: QWidget) -> list[str]:
    out = [w.text() for w in root.findChildren(QLabel) if w.isVisibleTo(root)]
    out += [w.accessibleName() for w in root.findChildren(QWidget) if w.isVisibleTo(root)]
    return out


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_profile_mapping() -> None:
    p = sw.simple_profile("best", "x2")
    assert (p.id, p.name, p.builtin) == ("simple", "ButterEye", False)
    assert (p.backend, p.model) == (BackendId.RIFE_NCNN, "rife-v4.26_ensembleFalse")
    assert (p.sc_threshold, p.buffered_frames, p.concurrent_frames, p.hdr) == (
        0.12,
        None,
        None,
        "skip",
    )
    assert sw.simple_profile("light", "x2").model == "rife-v4.22_lite_ensembleFalse"
    assert sw.simple_profile("cpu", "x2").backend is BackendId.MVTOOLS
    assert sw.simple_profile("auto", "x2").backend == "auto"
    assert sw.target_for("x2") == Target(TargetKind.X2)
    assert sw.target_for("fps60") == Target(TargetKind.FPS, Fraction(60))
    assert sw.target_for("display") == Target(TargetKind.DISPLAY)
    for key in ("x2", "fps60", "display"):
        assert sw.target_key(sw.target_for(key)) == key
    for key in ("auto", "best", "light", "cpu"):
        assert sw.smoothness_key(sw.simple_profile(key, "x2")) == key


def test_text_helpers() -> None:
    assert sw.short_rate(Fraction(24000, 1001)) == "24"
    assert sw.short_rate(Fraction(120000, 1001)) == "120"
    assert sw.short_rate(59.94) == "60"
    assert sw.short_rate(143.856) == "144"
    assert sw.short_rate(25) == "25"
    assert sw.short_rate(12.5) == "12.5"
    assert sw.without_code("Only local files can be opened. (BE-3007)") == (
        "Only local files can be opened."
    )


def test_row_view_plain_words() -> None:
    active = sc.session(1)
    v = sw.row_view(active)
    assert (v.kind, v.word, v.rates, v.engine) == ("ok", "Smooth", "24 → 120 fps", "GPU smoothing")
    cpu = dataclasses.replace(active, backend=BackendId.MVTOOLS, model=None)
    assert sw.row_view(cpu).engine == "CPU smoothing"
    off = sc.session(2, filter=FilterState.OFF)
    v = sw.row_view(off)
    assert (v.kind, v.word, v.paused, v.rates) == ("off", "Paused", True, "24 fps")
    for snap in sc.get("live_bypassed_each").sessions:
        v = sw.row_view(snap)
        assert v.word and v.kind
        assert not CODE.search(" ".join((v.word, v.rates, v.engine, v.notice)))
    stalled = sc.session(3, filter=FilterState.ROLLED_BACK, health=Health.STALLED)
    v = sw.row_view(stalled)
    assert v.word == "Playing normally" and v.notice and v.kind == "degraded"


# ---------------------------------------------------------------------------
# settings -> config
# ---------------------------------------------------------------------------


def test_startup_adopts_the_simple_profile(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    qtbot.waitUntil(lambda: bool(calls(win, "save_config")))
    cfg = saved(win)
    assert cfg.rules == (Rule(RuleMatch(), "simple"),)
    simple = [p for p in cfg.profiles if p.id == "simple"]
    assert simple == [sw.simple_profile("auto", "x2")]
    builtins = {p.id for p in cfg.profiles if p.builtin}
    assert builtins == {p.id for p in sc.builtin_profiles()}
    assert calls(win, "save_config")[-1].kwargs["expected_revision"] == "rev-1"
    assert win.smooth_combo.currentData() == "auto"
    assert win.target_combo.currentData() == "x2"


def test_choices_save_one_profile(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    qtbot.waitUntil(lambda: len(calls(win, "save_config")) == 1)
    pick(win.smooth_combo, "best")
    qtbot.waitUntil(lambda: len(calls(win, "save_config")) == 2)
    pick(win.target_combo, "fps60")
    qtbot.waitUntil(lambda: len(calls(win, "save_config")) == 3)
    cfg = saved(win)
    simple = [p for p in cfg.profiles if p.id == "simple"]
    assert simple == [sw.simple_profile("best", "fps60")]
    assert simple[0].target == Target(TargetKind.FPS, Fraction(60))
    assert cfg.rules == (Rule(RuleMatch(), "simple"),)
    # each save names the revision the previous one produced
    revs = [c.kwargs["expected_revision"] for c in calls(win, "save_config")]
    assert revs[0] == "rev-1" and len(set(revs)) == 3
    pick(win.target_combo, "display")
    qtbot.waitUntil(lambda: len(calls(win, "save_config")) == 4)
    assert sw.find_simple(saved(win)).target == Target(TargetKind.DISPLAY)  # type: ignore[union-attr]


def test_saved_config_shows_its_choices(make_simple: Make, qtbot: Any) -> None:
    base = sc.get("all_ready")
    cfg = sw.simple_config(base.config_load.config, sw.simple_profile("cpu", "fps60"))
    scenario = dataclasses.replace(base, config_load=sc.config_load(cfg))
    win = make_simple(scenario)
    assert win.smooth_combo.currentData() == "cpu"
    assert win.target_combo.currentData() == "fps60"
    assert calls(win, "save_config") == []  # already simple: nothing to write


def test_live_sessions_get_the_profile(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    qtbot.waitUntil(lambda: bool(calls(win, "apply_profile")))  # startup adoption
    before = len(calls(win, "apply_profile"))
    pick(win.smooth_combo, "light")
    qtbot.waitUntil(lambda: len(calls(win, "apply_profile")) > before)
    last = calls(win, "apply_profile")[-1]
    assert last.args == (SessionId("s1"), "simple")


def test_config_conflict_reloads_and_retries(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("config_conflict")
    qtbot.waitUntil(lambda: len(calls(win, "save_config")) == 2, timeout=5000)
    first, second = calls(win, "save_config")
    assert first.kwargs["expected_revision"] == "rev-1"
    assert second.kwargs["expected_revision"] == "rev-disk"
    assert sw.is_simple(second.args[0])
    assert calls(win, "load_config")  # reloaded after the conflict
    qtbot.waitUntil(lambda: win.config_load.revision not in ("rev-1", "rev-disk"))
    assert dict(second.args[0].unknown) == dict(
        sc.get("config_conflict").config_load.config.unknown
    )


# ---------------------------------------------------------------------------
# smooth switch, playing
# ---------------------------------------------------------------------------


def test_switch_drives_set_interpolation(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    assert win.smooth_switch.isChecked()
    win.smooth_switch.setChecked(False)
    qtbot.waitUntil(lambda: bool(calls(win, "set_interpolation")))
    assert calls(win, "set_interpolation")[-1].args == (SessionId("s1"), False)
    assert win.settings.value(sw.SMOOTH_KEY) in (False, "false")
    qtbot.waitUntil(lambda: win.rows[SessionId("s1")].view.paused)
    row = win.rows[SessionId("s1")]
    assert row.pause_button.text() == "Resume smoothing"
    win.smooth_switch.setChecked(True)
    qtbot.waitUntil(lambda: calls(win, "set_interpolation")[-1].args == (SessionId("s1"), True))
    assert win.smooth_switch.accessibleName() == "Smooth motion"


def test_switch_off_new_videos_play_normally(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    win.smooth_switch.setChecked(False)
    win.drop.fileChosen.emit(Path("/videos/movie.mkv"))
    qtbot.waitUntil(lambda: bool(calls(win, "play")))
    assert calls(win, "play")[0].kwargs["profile_id"] == "simple"
    qtbot.waitUntil(lambda: bool(calls(win, "set_interpolation")), timeout=3000)
    sid = calls(win, "set_interpolation")[0].args[0]
    assert calls(win, "set_interpolation")[0].args == (sid, False)
    qtbot.waitUntil(lambda: sid in win.rows)
    assert win.rows[sid].title.text() == "movie.mkv"


def test_open_button_and_ctrl_o_use_the_chooser(make_simple: Make, qtbot: Any) -> None:
    chosen: list[int] = []

    def chooser(_parent: QWidget) -> Path | None:
        chosen.append(1)
        return Path("/videos/clip.mkv")

    win = make_simple("all_ready", chooser=chooser)
    win.open_button.click()
    qtbot.waitUntil(lambda: bool(calls(win, "play")))
    win.act_open.trigger()
    qtbot.waitUntil(lambda: len(calls(win, "play")) == 2)
    assert len(chosen) == 2
    assert win.act_open.shortcut().toString() == "Ctrl+O"


def test_row_pause_and_let_go(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows)
    row = win.rows[sid]
    assert row.pause_button.accessibleName() == "Pause smoothing for film.mkv"
    row.pause_button.click()
    qtbot.waitUntil(lambda: calls(win, "set_interpolation")[-1:] != [])
    assert calls(win, "set_interpolation")[-1].args == (sid, False)
    row.let_go_button.click()
    qtbot.waitUntil(lambda: bool(calls(win, "detach")))
    assert calls(win, "detach")[0].args[0] == sid
    qtbot.waitUntil(lambda: sid not in win.rows)
    assert not win.playing.isVisible()


def test_too_demanding_row_offers_smooth_anyway(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    core = fake_core(win.bridge)
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows)
    row = win.rows[sid]
    assert not row.force_button.isVisibleTo(row)
    snap = dataclasses.replace(
        core._sessions[sid], filter=FilterState.BYPASSED, bypass=BypassReason.NO_REALTIME
    )
    in_core(qtbot, win.bridge, lambda c: c.fake_set_session(snap))
    qtbot.waitUntil(lambda: row.force_button.isVisibleTo(row))
    assert row.force_button.accessibleName() == "Smooth film.mkv anyway"
    row.force_button.click()
    qtbot.waitUntil(lambda: calls(win, "set_interpolation")[-1:] != [])
    last = calls(win, "set_interpolation")[-1]
    assert last.args == (sid, True) and last.kwargs == {"force": True}
    qtbot.waitUntil(lambda: not row.force_button.isVisibleTo(row))


def test_rows_follow_events_with_plain_notices(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    core = fake_core(win.bridge)
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows)
    notice = Msg("The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing.")
    snap = dataclasses.replace(
        core._sessions[sid], backend=BackendId.MVTOOLS, model=None, notice=notice
    )
    in_core(qtbot, win.bridge, lambda c: c.fake_set_session(snap))
    row = win.rows[sid]
    qtbot.waitUntil(lambda: row.notice.isVisible() and "CPU smoothing" in row.detail.text())
    assert row.notice.text() == notice.key
    note = Notice(Msg("Something went sideways (BE-4001)"), ErrorCode.INTERNAL, sid=sid)
    in_core(qtbot, win.bridge, lambda c: c.fake_emit(note))
    in_core(qtbot, win.bridge, lambda c: c.fake_set_session(dataclasses.replace(snap, notice=None)))
    qtbot.waitUntil(lambda: row.notice.text() == "Something went sideways")
    in_core(qtbot, win.bridge, lambda c: c.fake_emit(SessionEnded(sid, "mpv_exited")))
    qtbot.waitUntil(lambda: sid not in win.rows)


def test_gpu_fault_row_has_no_codes(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("gpu_fault")
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows and win.rows[sid].notice.isVisible())
    row = win.rows[sid]
    assert row.badge.text() == "Playing normally"
    assert row.badge.kind() == "degraded"
    for text in texts(win):
        assert not CODE.search(text), text
    ev = HealthChanged(
        sid, Health.GPU_FAULT, ErrorCode.RIFE_GPU_FAULT, Msg("GPU trouble (BE-5003)"), (), None
    )
    in_core(qtbot, win.bridge, lambda c: c.fake_emit(ev))
    qtbot.wait(150)
    for text in texts(win):
        assert not CODE.search(text), text


def test_display_rate_names_the_target(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    i = win.target_combo.findData("display")
    qtbot.waitUntil(lambda: win.target_combo.itemText(i) == "Your display (120 Hz)")
    assert win.settings.value(sw.DISPLAY_HZ_KEY) is not None


# ---------------------------------------------------------------------------
# GPU options, problems, first run
# ---------------------------------------------------------------------------


def test_gpu_choices_hidden_without_rife(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("cpu_only")
    qtbot.waitUntil(lambda: win.gpu_available is False)
    keys = [win.smooth_combo.itemData(i) for i in range(win.smooth_combo.count())]
    assert keys == ["auto", "cpu"]
    ready = make_simple("all_ready")
    qtbot.waitUntil(lambda: ready.gpu_available is True)
    keys = [ready.smooth_combo.itemData(i) for i in range(ready.smooth_combo.count())]
    assert keys == ["auto", "best", "light", "cpu"]


def test_blocking_problem_is_one_plain_line(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("blocking_mpv")
    qtbot.waitUntil(lambda: "mpv" in win.problem_text())
    qtbot.wait(300)  # later CapabilitiesChanged events keep the doctor's sentence
    assert win.problem_text() == (
        "ButterEye can't smooth video yet: Your mpv was built without VapourSynth."
    )
    assert not CODE.search(win.problem_text())
    assert win.problem_details.isVisible()
    assert calls(win, "bench") == []
    win.problem_details.click()
    qtbot.waitUntil(lambda: win.details is not None and win.details.isVisible())
    assert win.details.current_page_id() == "system"


def _first_run() -> Any:
    return dataclasses.replace(
        sc.get("all_ready"),
        name="first_run",
        config_load=sc.config_load(exists=False),
        report=None,
        bench_history=(),
    )


def test_first_run_sets_up_silently_and_measures(make_simple: Make, qtbot: Any) -> None:
    win = make_simple(_first_run(), ready=False, auto_bench=True)
    qtbot.waitUntil(lambda: bool(calls(win, "bench")), timeout=5000)
    names = [c.name for c in fake_core(win.bridge).fake_calls]
    order = [n for n in names if n in ("doctor", "setup_plan", "setup_apply", "save_config")]
    assert order[:4] == ["doctor", "setup_plan", "setup_apply", "save_config"]
    plan, choices = calls(win, "setup_apply")[0].args
    assert choices.backend is BackendId.RIFE_NCNN
    assert choices.trt_experimental is False and choices.run_smoke_test is True
    assert sw.is_simple(saved(win))
    assert calls(win, "bench")[0].args[0] == sw.FIRST_BENCH
    assert "Measuring your GPU" in win.status_text()
    qtbot.waitUntil(lambda: win.status_text() == "Ready.", timeout=10000)
    assert "apply_bench" not in names
    assert "add_include_to_mpv_conf" not in [c.name for c in fake_core(win.bridge).fake_calls]
    assert not win.problem.isVisible()


def test_no_bench_when_history_exists(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready", auto_bench=True)
    qtbot.waitUntil(lambda: bool(calls(win, "bench_history")))
    qtbot.wait(100)
    assert calls(win, "bench") == []


def test_live_unavailable_disables_playing(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("devbox_now")
    assert not win.open_button.isEnabled()
    assert win.problem.isVisible() and not CODE.search(win.problem_text())


# ---------------------------------------------------------------------------
# Details, app wiring, accessibility, theme
# ---------------------------------------------------------------------------


def test_details_dialog_has_four_tabs(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    win.details_button.click()
    dlg = win.details
    assert isinstance(dlg, DetailsDialog)
    qtbot.waitUntil(dlg.isVisible)
    assert dlg.tabs.count() == 4
    assert [dlg.tabs.tabText(i) for i in range(4)] == ["System", "Speed test", "Storage", "About"]
    assert dlg.page_ids() == tuple(pid for pid, _ in TABS)
    bench = dlg.pages["bench"]
    assert not bench.apply_button.isVisibleTo(dlg)  # type: ignore[attr-defined]
    dlg.go("about")
    assert dlg.current_page_id() == "about"
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    qtbot.keyClick(dlg, Qt.Key.Key_Escape)
    qtbot.waitUntil(lambda: not dlg.isVisible())


def test_app_opens_the_simple_window_and_gates_classic() -> None:
    with pytest.raises(SystemExit) as exc:
        gui_app.parse_args(["--classic"], {})
    assert exc.value.code == 2
    ns, _ = gui_app.parse_args(["--classic"], {"BUTTEREYE_DEV": "1"})
    assert ns.classic is True
    ns, _ = gui_app.parse_args([], {})
    assert ns.classic is False
    source = Path(gui_app.__file__).read_text(encoding="utf-8")
    assert "SimpleWindow(bridge, settings" in source


def test_window_icon_and_title(make_simple: Make) -> None:
    win = make_simple("all_ready")
    assert win.windowTitle() == "ButterEye"
    assert not win.windowIcon().isNull()
    assert sw.APP_ICON.read_text(encoding="utf-8").startswith(
        "<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->"
    )


@pytest.mark.parametrize("scenario", ["all_ready", "live_active", "blocking_mpv"])
def test_a11y_names_buddies_and_tab_cycle(make_simple: Make, qtbot: Any, scenario: str) -> None:
    win = make_simple(scenario)
    if scenario == "live_active":
        qtbot.waitUntil(lambda: bool(win.rows))
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    problems = check_names_and_buddies(win, win)
    start = win.open_button if win.open_button.isEnabled() else win.smooth_switch
    problems += check_tab_cycle(win, start, qtbot)
    assert not problems, "\n".join(problems)
    for w in (win.open_button, win.smooth_switch, win.target_combo, win.smooth_combo):
        assert w.focusPolicy() & Qt.FocusPolicy.TabFocus


def test_details_dialog_a11y(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    win.open_details()
    dlg = win.details
    qtbot.waitUntil(dlg.isVisible)
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    for pid, _ in TABS:
        dlg.go(pid)
        qtbot.wait(100)
        problems = check_names_and_buddies(dlg.tabs.currentWidget(), dlg)
        assert not problems, "\n".join(problems)


def test_switch_shows_state_in_words(qtbot: Any) -> None:
    from buttereye.gui.widgets.switch import Switch

    s = Switch("Smooth motion")
    qtbot.addWidget(s)
    assert s.state_text() == "Off" and s.accessibleDescription() == "Off"
    s.setChecked(True)
    assert s.state_text() == "On" and s.accessibleDescription() == "On"
    s.click()
    assert not s.isChecked()


def test_dark_palette_render(make_simple: Make, qtbot: Any, qapp: Any) -> None:
    controller = theme.install(qapp)
    win = make_simple("live_active")
    qtbot.waitUntil(lambda: bool(win.rows))
    try:
        controller.apply_scheme(Qt.ColorScheme.Dark)
        qtbot.wait(50)
        pal = QApplication.palette()
        assert theme.is_dark(pal)
        R = QPalette.ColorRole  # noqa: N806
        assert theme.contrast_ratio(pal.color(R.HighlightedText), pal.color(R.Highlight)) >= 4.5
        assert theme.contrast_ratio(pal.color(R.Highlight), pal.color(R.Window)) >= 3.0
        btn = win.open_button.palette()
        assert btn.color(R.Button) == pal.color(R.Highlight)
        assert theme.contrast_ratio(btn.color(R.ButtonText), btn.color(R.Button)) >= 4.5
        img = win.grab().toImage()
        assert not img.isNull() and img.width() > 0
        hue = pal.color(R.Highlight).hslHue()
        assert 30 <= hue <= 60  # butter, not the platform blue
    finally:
        controller.apply_scheme(Qt.ColorScheme.Light)
    pal = QApplication.palette()
    assert theme.contrast_ratio(pal.color(R.Highlight), pal.color(R.Window)) >= 3.0
