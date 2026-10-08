# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Play page, attach dialog and include-line dialog (docs/design/GUI.md §4.3, §6, §7 U7).

Asserts: every FilterState x BypassReason x Health text; announcements on
transitions only (never counters); the unavailable state shows zero rows;
refused candidates are visible and disabled; the include line is written only
after the confirmation; the page works on a core where LIVE/DISCOVER exist but
ATTACH/ORPHANS do not (the real core today).
"""

from __future__ import annotations

import dataclasses
import itertools
from collections import deque
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from PySide6.QtCore import QSettings, Qt, QUrl
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from buttereye.core.api import (
    BackendId,
    BypassReason,
    CandidateId,
    Counters,
    DetachPolicy,
    ErrorCode,
    Feature,
    FilterState,
    Health,
    HealthChanged,
    Msg,
    Origin,
    Reason,
    SessionEnded,
    SessionId,
)
from buttereye.core.capabilities import unavailable_state
from buttereye.core.testing import scenarios as sc
from buttereye.gui import a11y
from buttereye.gui.dialogs.attach import AttachDialog
from buttereye.gui.dialogs.include_line import IncludeLineDialog
from buttereye.gui.pages import sessions as sp
from buttereye.gui.pages.base import PageAction
from tests.gui.helpers import click_modal, fake_core

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

DESIGN_BYPASS = {
    BypassReason.ALREADY_AT_RATE: "Already at your screen's rate — nothing to do",
    BypassReason.INTERLACED: "Interlaced video — smoothing skipped",
    BypassReason.HDR_SKIP: "HDR video — smoothing skipped (default)",
    BypassReason.UNSUPPORTED_FORMAT: "This video format can't be smoothed",
    BypassReason.NO_VIDEO: "No video to smooth",
    BypassReason.NO_REALTIME: "Too slow for real time at this resolution — smoothing off",
}

GPU = "NVIDIA GeForce RTX 4090"


def _live_without_attach() -> sc.Scenario:
    """What the real core offers today: LIVE + DISCOVER, no ATTACH/ORPHANS."""
    base = sc.get("live_active")
    caps = dict(base.capabilities)
    for f in (Feature.ATTACH, Feature.ORPHANS):
        caps[f] = unavailable_state(f, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)
    return dataclasses.replace(
        base, name="live_without_attach", capabilities=MappingProxyType(caps)
    )


@pytest.fixture
def announcements() -> Iterator[list[tuple[str, bool]]]:
    seen: list[tuple[str, bool]] = []
    remove = a11y.add_announcement_listener(
        lambda _w, text, assertive: seen.append((text, assertive))
    )
    try:
        yield seen
    finally:
        remove()


def open_page(make_window: Any, qtbot: Any, scenario: Any) -> tuple[Any, sp.SessionsPage]:
    win = make_window(scenario)
    win.go("sessions")
    page = win.page("sessions")
    assert isinstance(page, sp.SessionsPage)
    qtbot.waitUntil(lambda: page.panel.state != "loading", timeout=3000)
    return win, page


def in_core(qtbot: Any, bridge: Any, fn: Callable[[Any], None]) -> None:
    """Run ``fn(fake_core)`` on the core loop, then drain events to the GUI."""
    done: list[bool] = []

    async def call(core: Any) -> None:
        fn(core)

    owner = QApplication.instance()
    assert owner is not None
    bridge.call(call, owner=owner, ok=lambda _v: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=3000)
    qtbot.wait(150)


def banner_button(banner: Any, text: str) -> QPushButton:
    for b in banner.buttons:
        if a11y.plain(b.text()) == text:
            return b  # type: ignore[no-any-return]
    raise AssertionError(f"no button {text!r} in {[b.text() for b in banner.buttons]}")


def row_texts(page: sp.SessionsPage) -> dict[str, tuple[str, str, str]]:
    out = {}
    for r in range(page.model.rowCount()):
        cells = [page.model.item(r, c).text() for c in range(4)]
        out[cells[0]] = (cells[1], cells[2], cells[3])
    return out


# ---------------------------------------------------------------------------
# pure text: every FilterState x BypassReason x Health
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", list(BypassReason))
def test_bypass_texts_match_design(reason: BypassReason) -> None:
    assert sp.bypass_text(reason) == DESIGN_BYPASS[reason]
    snap = sc.session(1, filter=FilterState.BYPASSED, bypass=reason)
    assert sp.headline(snap) == ("info", DESIGN_BYPASS[reason])


def test_filter_state_headlines() -> None:
    active = sc.session(1)
    kind, text = sp.headline(active, gpu=GPU)
    assert kind == "ok"
    assert text == f"Smooth: 23.976 → 119.88 fps (5×) · RIFE (Vulkan) v4.26 · {GPU}"
    assert sp.headline(sc.session(1, filter=FilterState.PENDING)) == (
        "busy",
        "Starting… waiting for the video window",
    )
    assert sp.headline(sc.session(1, filter=FilterState.OFF)) == ("off", "Smoothing off")
    rb = sc.session(1, filter=FilterState.ROLLED_BACK, health_code=ErrorCode.VF_ROLLED_BACK)
    kind, text = sp.headline(rb)
    assert kind == "blocking" and text.endswith("(BE-3003)")
    from buttereye.core.api import FilterFailed

    err = FilterFailed(
        ErrorCode.VF_ROLLED_BACK, Msg("mpv rejected it."), Msg("Try another."), rolled_back=True
    )
    assert sp.headline(rb, failure=err) == ("blocking", "mpv rejected it. Try another. (BE-3003)")
    for f in FilterState:
        assert sp.headline(sc.session(1, filter=f))[1]  # every state has a text


def test_mvtools_headline_has_no_gpu() -> None:
    snap = dataclasses.replace(sc.session(1), backend=BackendId.MVTOOLS, model=None)
    assert sp.headline(snap, gpu=GPU)[1] == "Smooth: 23.976 → 119.88 fps (5×) · MVTools (CPU)"


EXPECTED_HEALTH = {
    Health.DROPPING: ("degraded", "Dropping frames (3% over 10 s).", None),
    Health.STALLED: (
        "blocking",
        "Video stalled while audio kept playing; interpolation was turned off.",
        "BE-3004",
    ),
    # no HealthChanged evidence: no Xid number is claimed
    Health.GPU_FAULT: ("blocking", "The GPU reported a fault in RIFE-ncnn.", "BE-1030"),
    Health.DEVICE_LOST: ("blocking", "The GPU stopped responding (Vulkan device lost).", "BE-3006"),
    Health.CONNECTION_LOST: ("blocking", "Lost the connection to mpv.", "BE-3005"),
}


@pytest.mark.parametrize(
    ("filt", "bypass", "health"),
    [
        (f, b, h)
        for f, b, h in itertools.product(FilterState, [None, *BypassReason], Health)
        if (b is None) or f is FilterState.BYPASSED
    ],
)
def test_headline_precedence_every_combination(
    filt: FilterState, bypass: BypassReason | None, health: Health
) -> None:
    snap = sc.session(1, filter=filt, bypass=bypass, health=health, drop_rate=0.03)
    ended = dataclasses.replace(snap, ended=True)
    assert sp.headline(ended, ended_reason="detached") == ("off", "Detached")
    kind, text = sp.headline(snap)
    spec = sp.health_banner_spec(snap)
    if health is Health.OK:
        assert spec is None
        assert text == sp.headline(dataclasses.replace(snap, health=Health.OK))[1]
    else:
        assert (kind, text) == (sp.health_word(health)[0], sp.health_text(health, 0.03))
        assert spec is not None
        exp_kind, exp_title, exp_code = EXPECTED_HEALTH[health]
        assert (spec.kind, spec.title, spec.code) == (exp_kind, exp_title, exp_code)
    assert sp.health_banner_spec(ended) is None


def test_ended_texts() -> None:
    assert sp.ended_text("mpv_exited") == "Ended: mpv exited"
    assert sp.ended_text("detached") == "Detached"
    assert sp.ended_text("connection_lost") == "Connection lost"


def test_formatters() -> None:
    assert sp.fmt_rate(Fraction(24000, 1001)) == "23.976"
    assert sp.fmt_rate(Fraction(120000, 1001)) == "119.88"
    assert sp.fmt_rate(60) == "60"
    assert sp.fmt_multiplier(Fraction(5, 2)) == "2.5×"
    assert sp.model_short("rife-v4.26_ensembleFalse") == "v4.26"
    assert sp.model_short("rife-v4.25-lite_ensembleFalse") == "v4.25 lite"
    assert sp.model_short("custom.onnx") == "custom.onnx"


def test_counter_delta_over_ten_seconds() -> None:
    def c(n: int) -> Counters:
        return Counters(n, 0, n * 2, 0, True)

    hist: deque[tuple[float, Counters]] = deque([(0.0, c(0)), (5.0, c(4)), (11.0, c(10))])
    d = sp.counter_delta(hist, now=15.0)
    assert d is not None and d.frame_drop == 6 and d.vo_delayed == 12  # base = t=5 (>=10 s ago)
    assert sp.counter_delta(deque([(0.0, c(1))]), now=1.0) is None


# ---------------------------------------------------------------------------
# page states
# ---------------------------------------------------------------------------


def test_unavailable_shows_zero_rows(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    before = fake_core(win.bridge).fake_call_count("sessions")
    win.go("sessions")
    page = win.page("sessions")
    assert isinstance(page, sp.SessionsPage)
    qtbot.wait(200)
    assert page.panel.state == "unavailable"
    assert page.model.rowCount() == 0
    head, _body, code = page.panel.unavailable_texts()
    assert head == "Live playback control isn't in this build yet."
    assert code == "BE-9001"
    hint_text = page.panel.unavailable_hint.text()
    assert hint_text is not None and hint_text.startswith("buttereye play")
    assert fake_core(win.bridge).fake_call_count("sessions") == before
    assert win.ctx.current_session_facts() is None
    # shell actions on a gated page are reported as not handled
    assert page.handle_action(PageAction.TOGGLE_INTERPOLATION) is False


def test_empty_state_has_drop_zone_and_hint(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "all_ready")
    assert page.panel.state == "content"
    assert page.view_state() == "empty"
    assert page.drop_zone.isVisibleTo(page)
    assert page.drop_zone.accessibleName() == (
        "Drop a video file here, or press Enter to choose one"
    )
    empty_hint = page.empty_hint.text()
    assert empty_hint is not None and empty_hint.startswith("buttereye play")
    assert page.open_button.isEnabled() and page.attach_button.isEnabled()


def test_live_active_detail_and_facts(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    assert page.view_state() == "sessions"
    assert page.model.rowCount() == 1
    assert page.selected_sid() == SessionId("s1")
    qtbot.waitUntil(lambda: "RTX 4090" in page.headline.text(), timeout=3000)
    assert page.headline.text().startswith("Smooth: 23.976 → 119.88 fps (5×) · RIFE (Vulkan) v4.26")
    assert page.fields["rate"].text() == "23.976 → 119.88 fps (5×)"
    assert page.fields["display"].text() == "120 Hz"
    assert page.fields["gen"].text() == "7"
    assert page.fields["profile"].text() == "Quality"
    assert page.fields["sync"].text() == "Yes"
    assert page.interp.isChecked()
    assert not page.step_button.isVisibleTo(page)
    facts = win.ctx.current_session_facts()
    assert facts is not None and facts.width == 1920
    hint = page.cli_hint()
    assert hint is not None and hint.text() == "buttereye play /videos/film.mkv"


def test_every_state_row_and_headline(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "live_bypassed_each")
    rows = row_texts(page)
    assert rows["sixty.mkv"][1] == "Skipped"
    assert rows["paused.mkv"][1] == "Off"
    assert rows["starting.mkv"][1] == "Starting"
    assert rows["broken.mkv"][1] == "Rolled back"
    assert rows["gone.mkv"][1] == "Ended"
    assert rows["attached.mkv"][0] == "Attached"
    assert rows["dropping.mkv"][2] == "Dropping frames"
    expect = {
        "sixty.mkv": DESIGN_BYPASS[BypassReason.ALREADY_AT_RATE],
        "song.flac": DESIGN_BYPASS[BypassReason.NO_VIDEO],
        "paused.mkv": "Smoothing off",
        "starting.mkv": "Starting… waiting for the video window",
        "gone.mkv": "Ended",
        "dropping.mkv": "Dropping frames (3% over 10 s)",
    }
    for r in range(page.model.rowCount()):
        title = page.model.item(r, 0).text()
        page.tree.setCurrentIndex(page.model.index(r, 0))
        if title in expect:
            assert page.headline.text() == expect[title], title
        if title == "dropping.mkv":
            assert page.step_button.isVisibleTo(page)
            assert page.step_label.text() == "Dropping 3% of frames — step down to Fast?"
            assert page.health_banner is not None
            assert page.health_banner.title() == "Dropping frames (3% over 10 s)."
        if title == "attached.mkv":
            assert page.fields["restore"].text() == "hwdec, interpolation"
        if title == "gone.mkv":
            assert not page.interp.isEnabled()
            assert not page.detach_button.isEnabled()


def test_announces_transitions_not_counters(
    make_window: Any, qtbot: Any, announcements: list[tuple[str, bool]]
) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    qtbot.wait(200)
    announcements.clear()
    snap = sc.session(1)
    busy = dataclasses.replace(snap, counters=Counters(40, 2, 3, 1, True), drop_rate_10s=0.001)
    in_core(qtbot, win.bridge, lambda core: core.fake_set_session(busy))
    assert page.fields["drops"].text().startswith("40")
    assert announcements == []
    off = dataclasses.replace(busy, filter=FilterState.OFF)
    in_core(qtbot, win.bridge, lambda core: core.fake_set_session(off))
    assert ("film.mkv: Smoothing off", False) in announcements
    n = len(announcements)
    in_core(
        qtbot,
        win.bridge,
        lambda core: core.fake_set_session(
            dataclasses.replace(off, counters=Counters(41, 2, 3, 1, True))
        ),
    )
    assert len(announcements) == n


def test_toggle_interpolation_checkbox_space_and_shell(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    core = fake_core(win.bridge)
    page.interp.click()
    qtbot.waitUntil(lambda: core.fake_call_count("set_interpolation") == 1, timeout=3000)
    assert core.fake_calls[-1].args == (SessionId("s1"), False)
    qtbot.waitUntil(lambda: page.headline.text() == "Smoothing off", timeout=3000)
    assert not page.interp.isChecked()
    page.tree.setFocus()
    qtbot.keyClick(page.tree, Qt.Key.Key_Space)
    qtbot.waitUntil(lambda: core.fake_call_count("set_interpolation") == 2, timeout=3000)
    assert core.fake_calls[-1].args == (SessionId("s1"), True)
    qtbot.waitUntil(lambda: page.interp.isChecked(), timeout=3000)
    assert win.page_action("sessions", PageAction.TOGGLE_INTERPOLATION)
    qtbot.waitUntil(lambda: core.fake_call_count("set_interpolation") == 3, timeout=3000)


def test_apply_profile_reports_generation(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    core = fake_core(win.bridge)
    names = [page.profile_combo.itemText(i) for i in range(page.profile_combo.count())]
    assert names[0] == "Automatic (rules)"
    assert {"Quality", "Balanced", "Fast", "CPU (MVTools)"} <= set(names)
    page.profile_combo.setCurrentIndex(page.profile_combo.findData("fast"))
    page.apply_button.click()
    assert page.apply_status.text() == "Applying… (gen 7)"
    qtbot.waitUntil(lambda: page.apply_status.text() == "Applied (gen 8)", timeout=3000)
    call = [c for c in core.fake_calls if c.name == "apply_profile"][-1]
    assert call.args == (SessionId("s1"), "fast")
    hint = page.cli_hint()
    assert hint is not None and hint.text().endswith("--profile fast")


def test_apply_rollback_banner(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "apply_fails")
    page.apply_button.click()
    qtbot.waitUntil(lambda: page.apply_banner is not None, timeout=3000)
    assert page.apply_banner is not None and page.apply_banner.code == "BE-3003"
    assert page.apply_status.text() == "Rolled back"
    qtbot.waitUntil(lambda: page.headline.kind() == "blocking", timeout=3000)
    assert page.headline.text() == (
        "mpv rejected the new filter; rolled back. Try another profile. (BE-3003)"
    )


def test_step_down(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "live_bypassed_each")
    core = fake_core(_win.bridge)
    for r in range(page.model.rowCount()):
        if page.model.item(r, 0).text() == "dropping.mkv":
            page.tree.setCurrentIndex(page.model.index(r, 0))
    page.step_button.click()
    qtbot.waitUntil(lambda: core.fake_call_count("step_down") == 1, timeout=3000)
    qtbot.waitUntil(lambda: not page.step_button.isVisibleTo(page), timeout=3000)
    assert page.fields["profile"].text() == "Fast"


def test_detach_then_ended_row_and_delete(
    make_window: Any, qtbot: Any, announcements: list[tuple[str, bool]]
) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    core = fake_core(win.bridge)
    page.disable_detach_button.click()
    qtbot.waitUntil(lambda: core.fake_call_count("detach") == 1, timeout=3000)
    assert core.fake_calls[-1].args == (SessionId("s1"), DetachPolicy.DISABLE_FILTER)
    qtbot.waitUntil(lambda: row_texts(page)["film.mkv"][1] == "Detached", timeout=3000)
    assert page.headline.text() == "Detached"
    assert ("film.mkv: Detached", False) in announcements
    assert not page.detach_button.isEnabled()
    assert win.ctx.current_session_facts() is None
    page.tree.setFocus()
    qtbot.keyClick(page.tree, Qt.Key.Key_Delete)
    assert page.model.rowCount() == 0
    assert page.view_state() == "empty"


def test_ended_rows_are_removed_after_linger(
    make_window: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sp, "ENDED_LINGER_MS", 50)
    win, page = open_page(make_window, qtbot, "live_active")
    in_core(
        qtbot, win.bridge, lambda core: core.fake_emit(SessionEnded(SessionId("s1"), "mpv_exited"))
    )
    assert page.headline.text() in ("Ended: mpv exited", "")
    qtbot.waitUntil(lambda: page.model.rowCount() == 0, timeout=3000)
    # a late snapshot of the dropped player (refresh, coalesced update) never revives it
    snap = sc.get("live_active").sessions[0]
    assert snap.sid == SessionId("s1")
    page._apply_snapshot(dataclasses.replace(snap, ended=True))
    page._apply_snapshot(snap)
    assert page.model.rowCount() == 0


# ---------------------------------------------------------------------------
# health banners
# ---------------------------------------------------------------------------


def test_stalled_banner_and_mvtools(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "stalled")
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    banner = page.health_banner
    assert banner is not None
    assert banner.title() == "Video stalled while audio kept playing; interpolation was turned off."
    assert banner.code == "BE-3004"
    assert banner.detail_button is not None  # [Details]
    banner_button(banner, "Use MVTools for this file").click()
    core = fake_core(win.bridge)
    qtbot.waitUntil(lambda: core.fake_call_count("apply_profile") == 1, timeout=3000)
    assert core.fake_calls[-1].args == (SessionId("s1"), "cpu")


def test_stalled_keep_off_dismisses(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "stalled")
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    assert page.health_banner is not None
    banner_button(page.health_banner, "Keep off").click()
    assert page.health_banner is None


def test_gpu_fault_retry_needs_confirmation(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "gpu_fault")
    core = fake_core(win.bridge)
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    banner = page.health_banner
    assert banner is not None and banner.code == "BE-1030"
    assert banner.title() == "The GPU reported a fault in RIFE-ncnn (Xid 13)."
    assert banner.detail_view is not None and "Xid 13" in banner.detail_view.toPlainText()
    click_modal(qtbot, "retryRifeConfirmNo")
    banner_button(banner, "Retry RIFE").click()
    qtbot.wait(100)
    assert core.fake_call_count("set_interpolation") == 0
    click_modal(qtbot, "retryRifeConfirmYes")
    banner_button(banner, "Retry RIFE").click()
    qtbot.waitUntil(lambda: core.fake_call_count("set_interpolation") == 1, timeout=3000)
    banner_button_text = [a11y.plain(b.text()) for b in banner.buttons]
    assert "Copy diagnostic command" in banner_button_text


def test_gpu_fault_copy_diagnostic(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "gpu_fault")
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    assert page.health_banner is not None
    banner_button(page.health_banner, "Copy diagnostic command").click()
    clip = QApplication.clipboard()
    assert clip is not None and clip.text() == sp.DIAGNOSTIC_COMMAND


def test_connection_lost_banner(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "connection_lost")
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    banner = page.health_banner
    assert banner is not None and banner.code == "BE-3005"
    assert {a11y.plain(b.text()) for b in banner.buttons} == {"Reconnect", "Dismiss"}
    banner_button(banner, "Dismiss").click()
    assert page.health_banner is None


def test_connection_lost_without_attach_offers_no_reconnect(make_window: Any, qtbot: Any) -> None:
    """A player that stops answering while attaching isn't in this build: no
    [Reconnect] (it would open a gated dialog), and the body says what to do."""
    base = sc.get("connection_lost")
    caps = dict(base.capabilities)
    for f in (Feature.ATTACH, Feature.ORPHANS):
        caps[f] = unavailable_state(f, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)
    scen = dataclasses.replace(base, name="lost_no_attach", capabilities=MappingProxyType(caps))
    _win, page = open_page(make_window, qtbot, scen)
    qtbot.waitUntil(lambda: page.health_banner is not None, timeout=3000)
    banner = page.health_banner
    assert banner is not None
    assert {a11y.plain(b.text()) for b in banner.buttons} == {"Dismiss"}
    spec = sp.health_banner_spec(page._tracks[next(iter(page._tracks))].snap)
    assert spec is not None and "isn't answering" in spec.body
    assert "Reconnect" not in spec.body


def test_health_change_is_announced_assertively(
    make_window: Any, qtbot: Any, announcements: list[tuple[str, bool]]
) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    qtbot.wait(200)
    announcements.clear()
    ev = HealthChanged(
        SessionId("s1"),
        Health.STALLED,
        ErrorCode.FILTER_STALLED,
        Msg("Video stalled while audio kept playing."),
        (),
        Msg("Interpolation was turned off."),
    )
    in_core(qtbot, win.bridge, lambda core: core.fake_emit(ev))
    assert announcements and announcements[-1][1] is True
    assert "Video stalled" in announcements[-1][0]
    assert page.health_banner is not None and page.health_banner.code == "BE-3004"


def test_attached_details_mention_weaker_detection(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "live_active")
    snap = sc.session(
        2,
        title="mine.mkv",
        origin=Origin.ATTACHED,
        filter=FilterState.ROLLED_BACK,
        health=Health.STALLED,
        health_code=ErrorCode.FILTER_STALLED,
    )
    in_core(qtbot, win.bridge, lambda core: core.fake_set_session(snap))
    page.select(SessionId("s2"))
    assert page.health_banner is not None and page.health_banner.detail_view is not None
    assert "Only mpv's log is visible for attached players" in (
        page.health_banner.detail_view.toPlainText()
    )


# ---------------------------------------------------------------------------
# open / play / drop / not ready
# ---------------------------------------------------------------------------


def test_open_and_play(
    make_window: Any, qtbot: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    monkeypatch.setattr(sp.SessionsPage, "chooser", lambda _parent: video)
    win, page = open_page(make_window, qtbot, "all_ready")
    core = fake_core(win.bridge)
    assert win.page_action("sessions", PageAction.OPEN_AND_PLAY, switch=True)
    qtbot.waitUntil(lambda: core.fake_call_count("play") == 1, timeout=3000)
    assert core.fake_calls[-1].args == (video,)
    qtbot.waitUntil(lambda: page.view_state() == "sessions", timeout=3000)
    qtbot.waitUntil(lambda: page.headline.text().startswith("Smooth"), timeout=3000)
    track_title = page.model.item(0, 0).text()
    assert track_title == "clip.mkv"
    assert page.selected_sid() is not None


def test_drop_non_local_is_refused(make_window: Any, qtbot: Any) -> None:
    win, page = open_page(make_window, qtbot, "all_ready")
    page.drop_zone.offer_urls([QUrl("https://example.invalid/video.mkv")])
    assert page.message_banner is not None
    assert "Only local files can be opened" in page.message_banner.title()
    assert page.message_banner.code == "BE-3007"
    assert fake_core(win.bridge).fake_call_count("play") == 0


def test_play_error_banner(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    from buttereye.core.api import ButterEyeError

    scen = dataclasses.replace(
        sc.get("all_ready"),
        errors=MappingProxyType(
            {"play": ButterEyeError(ErrorCode.MPV_NOT_FOUND, Msg("mpv was not found."))}
        ),
    )
    _win, page = open_page(make_window, qtbot, scen)
    page.play_file(tmp_path / "x.mkv")
    qtbot.waitUntil(lambda: page.message_banner is not None, timeout=3000)
    assert page.message_banner is not None and page.message_banner.code == "BE-1004"
    assert page.view_state() == "empty"


def test_not_ready_when_blocked(make_window: Any, qtbot: Any) -> None:
    _win, page = open_page(make_window, qtbot, "blocking_mpv")
    assert page.not_ready_banner is not None
    assert page.not_ready_banner.title() == "Setup isn't finished — 1 problem needs fixing"
    assert not page.open_button.isEnabled()
    assert not page.attach_button.isEnabled()
    assert not page.drop_zone.isEnabled()
    assert page.open_button.accessibleDescription() == page.not_ready_banner.title()
    assert {a11y.plain(b.text()) for b in page.not_ready_banner.buttons} == {"Open Setup"}


def test_not_ready_without_config(make_window: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("all_ready"), config_load=sc.config_load(exists=False))
    _win, page = open_page(make_window, qtbot, scen)
    qtbot.waitUntil(lambda: page.not_ready_banner is not None, timeout=3000)
    assert not page.open_button.isEnabled()


# ---------------------------------------------------------------------------
# real-core-shaped capabilities: LIVE yes, ATTACH/ORPHANS no
# ---------------------------------------------------------------------------


def test_live_without_attach(make_window: Any, qtbot: Any) -> None:
    """ATTACH gated: [Attach…] is disabled with the §5.2 headline as its reason,
    and neither the button path nor File > Attach opens a dialog claiming that
    playing isn't in this build while a player is visibly playing."""
    win, page = open_page(make_window, qtbot, _live_without_attach())
    assert page.view_state() == "sessions"
    why = page.attach_unavailable_reason()
    assert why is not None and why.startswith("Attaching to an mpv you started yourself")
    assert "Playing" not in why
    assert not page.attach_button.isEnabled()
    assert page.attach_button.accessibleDescription() == why
    assert page.attach_button.toolTip() == why
    assert page.open_button.isEnabled() and page.drop_zone.isEnabled()
    before = fake_core(win.bridge).fake_call_count("discover")
    said: list[str] = []
    remove = a11y.add_announcement_listener(lambda _w, text, _a: said.append(text))
    try:
        page.open_attach()
        assert page.handle_action(PageAction.ATTACH)
    finally:
        remove()
    assert page._dialog is None
    assert said.count(why) == 2
    qtbot.wait(100)
    assert fake_core(win.bridge).fake_call_count("discover") == before


def test_gpu_fault_title_names_only_seen_xids() -> None:
    lines = (
        "NVRM: Xid (PCI:0000:01:00): 13, pid=9, name=vo, Graphics Exception",
        "NVRM: Xid 31, pid=149688, name=vspipe, Ch 00000010, MMU Fault",
        "NVRM: Xid 13, pid=9, name=vo",
    )
    assert sp.xid_numbers(lines) == (13, 31)
    assert sp.gpu_fault_title(Health.GPU_FAULT, lines) == (
        "The GPU reported a fault in RIFE-ncnn (Xid 13, 31)."
    )
    assert sp.gpu_fault_title(Health.GPU_FAULT, lines[1:2]) == (
        "The GPU reported a fault in RIFE-ncnn (Xid 31)."
    )
    assert sp.gpu_fault_title(Health.GPU_FAULT) == "The GPU reported a fault in RIFE-ncnn."
    assert sp.gpu_fault_title(Health.DEVICE_LOST, ("vk: device lost",)) == (
        "The GPU stopped responding (Vulkan device lost)."
    )


# ---------------------------------------------------------------------------
# attach dialog
# ---------------------------------------------------------------------------


def test_attach_dialog_lists_refused_disabled(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    dlg = AttachDialog(bridge)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: dlg.panel.state == "content", timeout=3000)
    texts = [dlg.list.item(i).text() for i in range(dlg.list.count())]
    assert texts[0] == "mpv — film.mkv (PID 4211)"
    refused = dlg.list.item(1)
    assert "Refused: socket owned by another user" in refused.text()
    assert not (refused.flags() & Qt.ItemFlag.ItemIsEnabled)
    assert "leftover ButterEye filter" in texts[2]
    assert dlg.list.currentRow() == 0 and dlg.attach_button.isEnabled()
    assert not dlg.remove_button.isVisibleTo(dlg)


def test_attach_dialog_enter_attaches(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    dlg = AttachDialog(bridge)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg.panel.state == "content", timeout=3000)
    with qtbot.waitSignal(dlg.attached, timeout=3000) as blocker:
        dlg.list.setFocus()
        qtbot.keyClick(dlg.list, Qt.Key.Key_Return)
    assert isinstance(blocker.args[0], str)
    core = fake_core(bridge)
    assert core.fake_calls[-1].name == "attach"
    assert core.fake_calls[-1].args == (CandidateId("c1"),)


def test_attach_dialog_orphan_remove_confirmed(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    core = fake_core(bridge)
    dlg = AttachDialog(bridge)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg.panel.state == "content", timeout=3000)
    dlg.list.setCurrentRow(2)
    assert dlg.remove_button.isVisibleTo(dlg) and dlg.remove_button.isEnabled()
    click_modal(qtbot, "orphanConfirmCancel")
    dlg.list.setFocus()
    qtbot.keyClick(dlg.list, Qt.Key.Key_Delete)
    qtbot.wait(100)
    assert core.fake_call_count("remove_orphan_filter") == 0
    click_modal(qtbot, "orphanConfirmRemove")
    dlg.remove_button.click()
    qtbot.waitUntil(lambda: core.fake_call_count("remove_orphan_filter") == 1, timeout=3000)
    qtbot.waitUntil(lambda: core.fake_call_count("discover") == 2, timeout=3000)


def test_attach_dialog_unavailable_on_devbox_now(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("devbox_now")
    dlg = AttachDialog(bridge)
    qtbot.addWidget(dlg)
    assert dlg.panel.state == "unavailable"
    assert dlg.list.count() == 0
    qtbot.wait(100)
    assert fake_core(bridge).fake_call_count("discover") == 0


def test_attach_dialog_empty(make_bridge: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("all_ready"), candidates=())
    bridge = make_bridge(scen)
    dlg = AttachDialog(bridge)
    qtbot.addWidget(dlg)
    qtbot.waitUntil(lambda: dlg.panel.state == "empty", timeout=3000)
    assert "No running mpv players were found" in dlg.panel.empty_label.text()


# ---------------------------------------------------------------------------
# include line
# ---------------------------------------------------------------------------


def test_include_line_text_is_accurate(make_bridge: Any, qtbot: Any) -> None:
    """The include line defines the [buttereye] profile only; until attaching ships it
    must not promise that every mpv the user starts gets smooth motion."""
    from buttereye.gui.dialogs.setup_wizard import IncludeLineFallback

    dlg = IncludeLineDialog(make_bridge("all_ready"))
    qtbot.addWidget(dlg)
    fallback = IncludeLineFallback("include=~/.config/buttereye/mpv/buttereye.conf")
    qtbot.addWidget(fallback)
    texts = [dlg.explain.text()] + [lab.text() for lab in fallback.findChildren(QLabel)]
    for text in texts:
        if "mpv.conf" not in text:
            continue
        assert "every mpv you start" not in text
        assert "--profile=buttereye" in text
        assert "does not smooth" in text and "attaching" in text


def test_include_line_written_only_after_confirm(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    core = fake_core(bridge)
    dlg = IncludeLineDialog(bridge)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitUntil(lambda: dlg.line() is not None, timeout=3000)
    line = dlg.line()
    assert line is not None and line.startswith("include=")
    assert dlg.field.text() == line
    dlg.copy_line()
    clip = QApplication.clipboard()
    assert clip is not None and clip.text() == line
    assert core.fake_call_count("add_include_to_mpv_conf") == 0

    click_modal(qtbot, "includeConfirmCancel")
    dlg.add_button.click()
    qtbot.wait(100)
    assert core.fake_call_count("add_include_to_mpv_conf") == 0

    click_modal(qtbot, "includeConfirmAdd")
    with qtbot.waitSignal(dlg.added, timeout=3000) as blocker:
        dlg.add_button.click()
    assert core.fake_call_count("add_include_to_mpv_conf") == 1
    assert core.fake_calls[-1].kwargs == {"consent": True}
    assert line in blocker.args[0]
    assert dlg.result_label.text().startswith("Added to ")


def test_include_confirm_shows_the_diff(make_bridge: Any, qtbot: Any) -> None:
    from buttereye.gui.dialogs.include_line import IncludeConfirmDialog

    dlg = IncludeConfirmDialog("include=~/.config/buttereye/mpv/buttereye.conf", "~/x/mpv.conf")
    qtbot.addWidget(dlg)
    assert dlg.diff.toPlainText() == "+include=~/.config/buttereye/mpv/buttereye.conf"
    assert dlg.cancel_button.isDefault()


# ---------------------------------------------------------------------------
# real core (no mpv is started): LIVE present, ATTACH unavailable
# ---------------------------------------------------------------------------


def test_real_core_page_and_attach_unavailable(
    xdg_env: Any, qtbot: Any, slow_callbacks: Any, tmp_path: Path
) -> None:
    from buttereye.core.api import Feature as F
    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.main_window import MainWindow

    bridge = CoreBridge(debug=True)
    with qtbot.waitSignal(bridge.ready, timeout=10000):
        bridge.start()
    try:
        caps = bridge.capabilities
        assert caps is not None
        if not caps.ok(F.LIVE):
            pytest.skip("the live provider (U10) is not in this tree")
        settings = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
        win = MainWindow(
            bridge, settings, log_path=tmp_path / "gui.log", open_setup_when_missing=False
        )
        win._test_allow_close = True  # type: ignore[attr-defined]
        win._may_close = True
        qtbot.addWidget(win)
        win.show()
        win.go("sessions")
        page = win.page("sessions")
        assert isinstance(page, sp.SessionsPage)
        qtbot.waitUntil(lambda: page.panel.state != "loading", timeout=10000)
        assert page.panel.state == "content"
        assert page.view_state() == "empty"
        assert page.model.rowCount() == 0
        # A fresh XDG home has no config.toml: setup isn't finished yet.
        qtbot.waitUntil(lambda: page.not_ready_banner is not None, timeout=10000)
        assert not page.open_button.isEnabled()
        attach_state = caps.states[F.ATTACH]
        dlg = AttachDialog(bridge, page)
        if not attach_state.available and attach_state.reason is Reason.NOT_IMPLEMENTED:
            assert dlg.panel.state == "unavailable"
            assert dlg.list.count() == 0
        dlg.reject()
    finally:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)


# ---------------------------------------------------------------------------
# accessibility of the page in live states and of both dialogs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scenario", ["live_bypassed_each", "stalled", "gpu_fault"])
def test_page_a11y_live_states(make_window: Any, qtbot: Any, scenario: str) -> None:
    from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle

    win, page = open_page(make_window, qtbot, scenario)
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    qtbot.wait(150)
    problems = check_names_and_buddies(page, win)
    problems += check_tab_cycle(win, win.sidebar, qtbot)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("which", ["attach", "attach_unavailable", "include"])
def test_dialogs_a11y(make_bridge: Any, qtbot: Any, which: str) -> None:
    from PySide6.QtWidgets import QDialog, QWidget

    from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle

    bridge = make_bridge("devbox_now" if which == "attach_unavailable" else "all_ready")
    dlg: QDialog = IncludeLineDialog(bridge) if which == "include" else AttachDialog(bridge)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.waitExposed(dlg)
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    qtbot.wait(200)
    problems = check_names_and_buddies(dlg, dlg)
    focusables = [
        w
        for w in dlg.findChildren(QWidget)
        if w.focusPolicy() & Qt.FocusPolicy.TabFocus and w.isVisible() and w.isEnabled()
    ]
    if focusables:
        problems += check_tab_cycle(dlg, focusables[0], qtbot)
    assert not problems, "\n".join(problems)


def test_pending_headline_shows_the_start_deadline_notice() -> None:
    """Stuck in PENDING past the core's start deadline: the headline says so
    instead of "Starting…" forever."""
    base = sc.get("live_active").sessions[0]
    pending = dataclasses.replace(base, filter=FilterState.PENDING, notice=None, health=Health.OK)
    assert sp.headline(pending) == ("busy", "Starting… waiting for the video window")
    stuck = dataclasses.replace(pending, notice=Msg("mpv hasn't opened the video yet."))
    assert sp.headline(stuck) == ("degraded", "mpv hasn't opened the video yet.")
