# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live targets never go above a known display rate (decide.over_display).

Measured on an Apple M2 with its 60 Hz panel (docs/macos-release-bar.md, "Live
smoothing of a 1080p50 AV1 film"): the simple window's default "Double (2x)"
took a 50 fps video to 100 fps, mpv dropped 40 of every 100 frames at the video
output by design (also with ``--vf=fps=100`` and no smoothing at all), the
health detector reported "Dropping frames" although MVTools kept up at 720p with
3.2 cores to spare, and live correction stored those drops as slowness:
``(1 - 0.4) x 0.9 = 0.54``, the factor found on disk for MVTools at 1080p and
at 720p. Pure: runs on every platform.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core.mpvctl import decide
from buttereye.core.mpvctl import livelimits as ll
from buttereye.core.types import BackendId, BypassReason, TargetKind

#: what mpv reported as display-fps for the M2's built-in panel
M2_PANEL = 60.0024
FIFTY = Fraction(50)


def _opt(backend: BackendId, cap: float | None) -> decide.EngineOption:
    return decide.EngineOption(backend, cap)


# ---------------------------------------------------------------- over_display


def test_over_display_is_what_mpv_drops() -> None:
    assert decide.over_display(Fraction(100), M2_PANEL)
    assert not decide.over_display(Fraction(60), M2_PANEL)
    # within display-resample's 1 %: shown frame for frame, not dropped
    assert not decide.over_display(Fraction(60), 59.94)
    assert not decide.over_display(Fraction(120), 119.0)  # 0.8 %
    assert decide.over_display(Fraction(121), 119.0)  # 1.7 %
    # unknown display (None, or mpv's 0 before a window exists): never "over"
    assert not decide.over_display(Fraction(100), None)
    assert not decide.over_display(Fraction(100), 0.0)
    assert not decide.over_display(None, M2_PANEL)


# ---------------------------------------------------------------- 2x


def test_double_of_50_on_a_60_hz_panel_is_60() -> None:
    tc = decide.choose_target(FIFTY, M2_PANEL, TargetKind.X2)
    assert tc == decide.TargetChoice(Fraction(60), None)
    # as many inferred frames as 50 -> 100: the cap is costed the same way
    assert decide.load_rate(FIFTY, Fraction(60)) == decide.load_rate(FIFTY, Fraction(100))
    fits = decide.choose_target(FIFTY, M2_PANEL, TargetKind.X2, sustainable_fps=100.0)
    assert fits.target == 60
    short = decide.choose_target(FIFTY, M2_PANEL, TargetKind.X2, sustainable_fps=99.0)
    assert short.bypass is BypassReason.NO_REALTIME


def test_double_below_the_display_is_unchanged() -> None:
    ntsc = Fraction(24000, 1001)
    assert decide.choose_target(ntsc, 60.0, TargetKind.X2).target == ntsc * 2
    assert decide.choose_target(FIFTY, 144.0, TargetKind.X2).target == 100
    # 30 -> 60 on a 59.94 Hz panel: display-resample shows all of them
    assert decide.choose_target(Fraction(30), 59.94, TargetKind.X2).target == 60
    # no display rate known yet: 2x as before (the session re-decides when it comes)
    assert decide.choose_target(FIFTY, None, TargetKind.X2).target == 100
    assert decide.choose_target(FIFTY, 0.0, TargetKind.X2).target == 100


def test_a_source_the_display_already_shows_is_at_rate() -> None:
    for src, hz in ((Fraction(60), 60.0), (Fraction(60000, 1001), 60.0), (FIFTY, 50.0)):
        tc = decide.choose_target(src, hz, TargetKind.X2)
        assert tc == decide.TargetChoice(None, BypassReason.ALREADY_AT_RATE), (src, hz)
    # a 60 Hz video on a 50 Hz display, too
    assert decide.choose_target(Fraction(60), 50.0, TargetKind.X2).bypass is (
        BypassReason.ALREADY_AT_RATE
    )


def test_a_capped_double_takes_the_highest_refresh_k_that_fits() -> None:
    # 30 fps on a 50 Hz display: 50 (refresh/1); 24 on a 30 Hz one: 30
    assert decide.choose_target(Fraction(30), 50.0, TargetKind.X2).target == 50
    assert decide.choose_target(Fraction(24), 30.0, TargetKind.X2).target == 30


# ---------------------------------------------------------------- fixed rate


def test_a_fixed_rate_above_the_display_is_capped_too() -> None:
    assert decide.choose_target(Fraction(24), 50.0, TargetKind.FPS, Fraction(60)).target == 50
    assert decide.choose_target(Fraction(24), 60.0, TargetKind.FPS, Fraction(120)).target == 60
    # at or within 1 % of the display: as asked
    assert decide.choose_target(Fraction(24), 60.0, TargetKind.FPS, Fraction(60)).target == 60
    assert decide.choose_target(Fraction(24), 59.94, TargetKind.FPS, Fraction(60)).target == 60
    assert decide.choose_target(Fraction(24), None, TargetKind.FPS, Fraction(120)).target == 120
    # at or below the source: nothing to add, as before
    assert decide.choose_target(FIFTY, 60.0, TargetKind.FPS, Fraction(50)).bypass is (
        BypassReason.ALREADY_AT_RATE
    )


def test_display_targets_are_unchanged() -> None:
    assert decide.choose_target(FIFTY, M2_PANEL, TargetKind.DISPLAY).target == 60
    assert decide.choose_target(Fraction(24), 180.0, TargetKind.DISPLAY_MAX).target == 90


# ---------------------------------------------------------------- the owner's file


def test_the_m2_case_pinned_mvtools_smooths_at_720p_and_60() -> None:
    """A 1080p50 video, the CPU profile, the M2's benchmark: MVTools' 1080p cap
    (115.36 in mpv / 1.25 = 92.3) is short of the 100 that 50 -> 60 costs, its
    720p cap (199.21 / 1.25, less the shrink) is not. Measured live: 0 drops in
    15 s windowed and 3 in 25 s full screen at 3.0-3.3 cores, where 50 -> 100 at
    720p 'dropped' 40 frames a second."""
    full = [_opt(BackendId.MVTOOLS, 115.36 / 1.25)]
    p = decide.pick_engine(full, FIFTY, M2_PANEL, TargetKind.X2, auto=False)
    assert p.target.bypass is BypassReason.NO_REALTIME
    cap_720 = decide.shrink_cap(199.21 / 1.25, 1920, 1080)
    sized = [((1280, 720), [_opt(BackendId.MVTOOLS, cap_720)])]
    found = decide.pick_size(sized, FIFTY, M2_PANEL, TargetKind.X2, auto=False)
    assert found is not None
    pick, size = found
    assert (pick.backend, pick.target.target, size) == (BackendId.MVTOOLS, 60, (1280, 720))


def test_automatic_takes_the_gpu_engine_at_a_smaller_size_when_the_display_caps_it() -> None:
    """Automatic's "the GPU engine at a smaller size only if it doubles" rule asked
    a 50 fps video for 99 fps on a 60 Hz panel, which nothing can show: mvgpu
    reaching 60 at 720p lost to the CPU. It now needs only what the display
    allows."""
    cap_mvgpu = decide.shrink_cap(201.27 / 1.25, 1920, 1080)
    cap_mv = decide.shrink_cap(199.21 / 1.25, 1920, 1080)
    sized = [((1280, 720), [_opt(BackendId.MVGPU, cap_mvgpu), _opt(BackendId.MVTOOLS, cap_mv)])]
    found = decide.pick_size(sized, FIFTY, M2_PANEL, TargetKind.X2, auto=True)
    assert found is not None
    assert (found[0].backend, found[0].target.target) == (BackendId.MVGPU, 60)
    # a display that does show 2x: the same caps double, as before
    found = decide.pick_size(sized, FIFTY, 144.0, TargetKind.X2, auto=True)
    assert found is not None
    assert (found[0].backend, found[0].target.target) == (BackendId.MVGPU, 100)


def test_smooth_anyway_never_asks_for_more_than_the_display() -> None:
    p = decide.pick_engine(
        [_opt(BackendId.MVTOOLS, 10.0)], FIFTY, M2_PANEL, TargetKind.X2, auto=False, forced=True
    )
    assert p.backend is BackendId.MVTOOLS and p.target.target == 60


# ---------------------------------------------------------------- live-limits


def test_records_from_before_the_cap_are_not_trusted(tmp_path: Path) -> None:
    """The M2's live-limits.json as 0.0.3 wrote it (factors rounded): learned
    from by-design drops, indistinguishable from a real slowdown, so dropped."""
    path = tmp_path / ll.FILE
    old = {
        "-|mvtools|-|1920x1080|1": {"factor": 0.5398880054634757, "when": 1000.0},
        "-|mvtools|-|1280x720|1": {"factor": 0.5395721083557977, "when": 1000.0},
    }
    path.write_text(json.dumps(old))
    assert ll.load(path, now=1000.0 + 60) == {}
    key = ll.LimitKey(None, "mvtools", None, 1920, 1080)
    limits: dict[str, dict[str, float]] = {}
    ll.record(path, limits, key, 0.7, now=2000.0)
    again = ll.load(path, now=2000.0 + 60)
    assert ll.factor(again, key) == pytest.approx(0.7)
    assert json.loads(path.read_text())[key.text()]["v"] == ll.VERSION


# ---------------------------------------------------------------- display rate


@pytest.mark.parametrize(
    ("hz", "expected"),
    [
        (60.0024, Fraction(60)),  # the M2 panel, as mpv reported it with a filter
        (60.000131, Fraction(60)),  # ... and without one
        (59.94, Fraction(60000, 1001)),  # NTSC stays NTSC
        (119.88, Fraction(120000, 1001)),
        (143.998, Fraction(144)),
        (74.97, Fraction(75)),
        (165.0, Fraction(165)),
        (59.0, Fraction(59)),
        (None, None),
        (0.0, None),
    ],
)
def test_display_rate_snaps_what_display_resample_absorbs(
    hz: float | None, expected: Fraction | None
) -> None:
    assert decide.display_rate(hz) == expected


def test_a_display_target_on_the_m2_panel_shares_frames_with_the_source() -> None:
    """60.0024 as 49982/833 made every output frame an inferred one (no source
    frame on the grid); as 60 one output frame in six is a source frame."""
    tc = decide.choose_target(FIFTY, 60.0024, TargetKind.DISPLAY)
    assert tc.target == 60
    assert decide.interp_rate(FIFTY, Fraction(60)) == 50
