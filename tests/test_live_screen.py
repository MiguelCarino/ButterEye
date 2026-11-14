# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A video larger than the screen it is shown on is smoothed at the screen's size
(``decide.screen_fit``): mpv would scale it down to that anyway, so nothing that can
be seen is lost and every engine has less to do. No mpv, no GPU: the decisions and
the filter the session adds are real, the engines are not run.

Caps are the dev box's, as in tests/test_live_cascade.py."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import decide, ipc
from buttereye.core.mpvctl import session as live
from buttereye.core.types import BackendId, BenchResult, Target, TargetKind
from tests.test_live_cascade import MV_2160, _ctx, _devbox, _display
from tests.test_live_decisions import _load, _result, _session, _simple
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture
from tests.test_live_trt import TrtFake, _filters, _ready, _trt_m
from tests.test_live_trt import fake_trt as _fake_trt_fixture
from tests.test_mvgpu_live import _mvgpu_m

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture
fake_trt = _fake_trt_fixture

ULTRAWIDE = (3440, 1440)


# ---------------------------------------------------------------- the size
@pytest.mark.parametrize(
    ("video", "screen", "expected"),
    [
        ((3840, 2160), ULTRAWIDE, (2560, 1440)),  # 16:9 film: the screen's height
        ((3840, 1608), ULTRAWIDE, (3438, 1440)),  # scope film: both sides at once
        ((3840, 1600), ULTRAWIDE, (3436, 1432)),  # the screen's width decides
        ((1920, 1080), (1600, 900), (1600, 900)),
        ((2560, 1440), (2560, 1080), (1920, 1080)),
        ((1920, 1080), ULTRAWIDE, None),  # fits: its own size
        ((3840, 2160), (3840, 2160), None),
        ((3456, 1440), ULTRAWIDE, None),  # 1 % fewer pixels: not worth an engine
        ((3840, 2160), None, None),  # mpv does not say
        ((3840, 2160), (0, 0), None),
        ((0, 0), ULTRAWIDE, None),
    ],
)
def test_screen_fit(
    video: tuple[int, int], screen: tuple[int, int] | None, expected: tuple[int, int] | None
) -> None:
    got = decide.screen_fit(*video, screen)
    assert got == expected
    if got is not None and screen is not None:
        assert got[0] <= screen[0] and got[1] <= screen[1]
        assert got[0] % 2 == 0 and got[1] % 2 == 0
        assert got[0] * got[1] <= decide.SCREEN_FIT_MAX_SHARE * video[0] * video[1]


def test_screen_fit_of_a_picture_mpv_turns() -> None:
    """A phone video coded 1920×1080 and shown upright: the screen's sides swap."""
    assert decide.screen_fit(1920, 1080, (1600, 900), rotated=True) == (900, 506)
    assert decide.screen_fit(1920, 1080, (1080, 1920), rotated=True) is None


def test_screen_size_is_what_mpv_reports() -> None:
    assert decide.screen_size({"display-width": 3440, "display-height": 1440}) == ULTRAWIDE
    assert decide.screen_size({"display-width": 3440.0, "display-height": 1440.0}) == ULTRAWIDE
    assert decide.screen_size({"display-width": 3440}) is None
    assert decide.screen_size({"display-width": 0, "display-height": 0}) is None
    assert decide.screen_size({"display-width": True, "display-height": True}) is None
    assert decide.screen_size({"display-width": "3440", "display-height": "1440"}) is None
    assert decide.screen_size({}) is None


def test_the_screens_size_is_watched() -> None:
    for prop in ("display-width", "display-height"):
        assert prop in ipc.OBSERVABLE and prop in live.WAKE_PROPS


# ---------------------------------------------------------------- the session
def _on(s: live._Session, screen: tuple[int, int]) -> None:
    s.props.update({"display-width": screen[0], "display-height": screen[1]})


async def test_4k_doubled_on_a_1440_line_screen_runs_rife_at_the_screens_size(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """RIFE cannot double a 4K film at 4K here (206 fps at 1080p is 41 at 4K with
    headroom), and it does not have to: the screen shows 1440 lines."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _simple("auto", Target(TargetKind.X2)))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 2560, 1440)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["size"] == [2560, 1440]
    assert f["target_fps"] == [48, 1] and "cascade_fps" not in f
    assert s.backend is BackendId.RIFE_TRT and s.run_size == (2560, 1440) and s.run_fit
    snap = s.snapshot()
    assert snap.smooth_size == (2560, 1440) and snap.smooth_at_screen
    assert snap.notice is None  # nothing was given up, so nothing to say


async def test_without_a_screen_size_the_same_video_gets_motion_vectors_at_4k(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """Where mpv does not report the screen, the old path: RIFE cannot double 4K at
    4K, and motion vectors at the video's own size come before a smaller picture.
    So the rule above is also what gives this video RIFE's picture."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _simple("auto", Target(TargetKind.X2)))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 2560, 1440)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "mvgpu" and "size" not in f
    assert s.run_size is None and not s.run_fit and not s.snapshot().smooth_at_screen


async def test_the_cascade_still_steps_down_from_the_screens_size(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """4K on a 3440×1440, 144 Hz screen: the cascade does not fit at the screen's
    1440 lines, so it runs at 1080 as before, and says so."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["size"] == [1920, 1080]
    assert f["target_fps"] == [144, 1] and f["cascade_fps"] == [48, 1]
    assert not s.run_fit and s.notice == decide.cascade_notice(1080)


async def test_a_video_that_fits_its_screen_keeps_its_own_size(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _simple("auto", Target(TargetKind.X2)))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and "size" not in f
    assert s.run_size is None and not s.run_fit


async def test_full_size_keeps_the_videos_own_size_on_a_smaller_screen(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """[general] full_size: the user asked for the video's own size, so the
    screen's is not taken either."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _simple("auto", Target(TargetKind.X2)))
    ctx.cfg = dataclasses.replace(
        ctx.cfg, general=dataclasses.replace(ctx.cfg.general, full_size=True)
    )
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 3840, 2160)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    assert s.run_size is None and not s.run_fit
    assert all("size" not in f for f in _filters(s))


async def test_moving_to_a_smaller_screen_takes_that_screens_size(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _display("mvgpu"))
    s = _session(ctx, tmp_path)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    assert s.backend is BackendId.MVGPU and s.run_size == (2560, 1440) and s.run_fit
    s.props.update({"display-fps": 75.0})
    _on(s, (1600, 900))
    await s.apply(raise_on_fail=False)
    assert s.run_size == (1600, 900) and s.run_fit
    assert _filters(s)[-1]["size"] == [1600, 900]


async def test_the_steps_below_the_screens_size_keep_their_old_widths(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """A scope film (3840 × 1608) on the ultrawide: the cascade steps down from the
    screen's 3438 × 1440 to the same 2580 × 1080 it used before the screen was
    asked, so the engine built for that size is still the one used. (A machine about
    twice the dev box: there the cascade does not fit a scope film at 1080 lines.)"""
    assert decide.smaller_sizes(3840, 1608)[1] == (2580, 1080)
    bench_history.append(
        _result(
            _trt_m(380.0),
            _mvgpu_m(1200.0),
            _mvgpu_m(300.0, "mvgpu @ 1440p"),
            _mvgpu_m(MV_2160, "mvgpu @ 2160p"),
        )
    )
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 2580, 1080)
    _load(s, fps=24.0, w=3840, h=1608, hz=144.0)
    _on(s, ULTRAWIDE)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["size"] == [2580, 1080]
    assert f["cascade_fps"] == [48, 1] and not s.run_fit
    assert fake_trt.builds == []
