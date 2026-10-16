# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What a profile's content (``Profile.content``) changes in live playback: hold
fill (held drawings re-timed) on 2D animation and Automatic only when the speed
test leaves room for it, never on live action or TensorRT, and never taught to
the engine's live limits when it drops frames; identical frames
copied (still skip) on every live run; the engine ranking for 2D animation; and
the chroma location and scene-cut threshold handed to the script.

Same fakes and dev box numbers as tests/test_live_decisions.py.
"""

from __future__ import annotations

import dataclasses
import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import decide
from buttereye.core.mpvctl import session as live
from buttereye.core.types import (
    BackendId,
    BenchResult,
    ContentKind,
    Profile,
    Target,
    TargetKind,
)
from tests.test_live_decisions import (
    MV_CAP,
    NTSC,
    RIFE_CAP,
    FakeCtx,
    _config,
    _devbox,
    _engine,
    _facts,
    _load,
    _m,
    _paths,
    _result,
    _session,
    _simple,
)
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture
from tests.test_live_trt import TrtFake, _ncnn_m, _opted, _ready, _trt_m
from tests.test_live_trt import fake_trt as _fake_trt_fixture

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture
fake_trt = _fake_trt_fixture


def _profile(content: ContentKind, backend: BackendId | str = "auto") -> Profile:
    return dataclasses.replace(_simple(backend), content=content)


def _filters(s: Any) -> list[tuple[dict[str, Any], int]]:
    """(user-data, buffered-frames) of every ``vf add``."""
    out = []
    for c in s.ipc.commands:
        if c[:2] == ("vf", "add"):
            arg = str(c[2])
            m = re.search(r":buffered-frames=(\d+)", arg)
            raw = arg[arg.index(":user-data=") + len(":user-data=") :]
            raw = raw[raw.index("%", 1) + 1 :] if raw.startswith("%") else raw
            out.append((json.loads(raw), int(m.group(1)) if m else -1))
    return out


# ---------------------------------------------------------------------------
# pure: the load hold fill adds, chroma location names
# ---------------------------------------------------------------------------


def test_hold_fill_factor_is_the_worst_case_on_threes() -> None:
    # the longest hold re-timed (HOLD_MAX) is the worst case: (1 - 1/6) / (1 - 1/2)
    assert decide.HOLD_FILL_GROUP == live.HOLD_MAX
    assert decide.hold_fill_factor(Fraction(24), Fraction(48)) == pytest.approx(5 / 3)
    assert decide.hold_fill_factor(Fraction(24), Fraction(48), group=2) == pytest.approx(1.5)
    # (1 - 1/7.5) / (1 - 1/2.5) at 24 → 60
    assert decide.hold_fill_factor(Fraction(24), Fraction(60)) == pytest.approx((1 - 1 / 7.5) / 0.6)
    assert decide.hold_fill_factor(Fraction(60), Fraction(60)) == 1.0
    # 1080p 2x: RIFE's 48.96 can't absorb 1.67 × 48; MVTools' 81.85 can
    assert decide.hold_fill_fits(NTSC, NTSC * 2, 80.0)
    assert not decide.hold_fill_fits(NTSC, NTSC * 2, 75.0)  # enough for twos, not threes
    assert not decide.hold_fill_fits(NTSC, NTSC * 2, RIFE_CAP)
    assert decide.hold_fill_fits(NTSC, NTSC * 2, MV_CAP)
    assert not decide.hold_fill_fits(NTSC, NTSC * 2, None)  # no benchmark: no promise


@pytest.mark.parametrize(
    ("mpv", "zimg"),
    [
        ("mpeg2/4/h264", "left"),
        ("mpeg1/jpeg", "center"),
        ("uhd", "top_left"),
        ("top", "top"),
        ("bottom-left", "bottom_left"),
        ("bottom", "bottom"),
        ("unknown", None),
        (None, None),
    ],
)
def test_chroma_location_names(mpv: str | None, zimg: str | None) -> None:
    assert decide.chroma_loc_name(mpv) == zimg


# ---------------------------------------------------------------------------
# pure: the engine ranking for 2D animation
# ---------------------------------------------------------------------------


def test_animation_keeps_rife_where_mvgpu_would_reach_the_display() -> None:
    opts = [
        decide.EngineOption(BackendId.RIFE_NCNN, RIFE_CAP),
        decide.EngineOption(BackendId.MVGPU, 1000.0),
    ]
    live_pick = decide.pick_engine(opts, NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert live_pick.backend is BackendId.MVGPU  # reaches 180, RIFE only 2x
    anime = decide.pick_engine(opts, NTSC, 180.0, TargetKind.DISPLAY, auto=True, animation=True)
    assert anime.backend is BackendId.RIFE_NCNN and anime.target.target == NTSC * 2


def test_animation_smaller_size_asks_rife_only_first() -> None:
    small = [
        decide.EngineOption(BackendId.RIFE_NCNN, None),
        decide.EngineOption(BackendId.MVTOOLS, MV_CAP),
        decide.EngineOption(BackendId.MVGPU, 1000.0),
    ]
    sized = [((1920, 1080), small)]
    found = decide.pick_size(sized, NTSC, 60.0, TargetKind.X2, auto=True)
    assert found is not None and found[0].backend is BackendId.MVGPU
    found = decide.pick_size(sized, NTSC, 60.0, TargetKind.X2, auto=True, animation=True)
    assert found is not None and found[0].backend is BackendId.MVTOOLS


# ---------------------------------------------------------------------------
# the session
# ---------------------------------------------------------------------------


async def test_animation_fills_holds_with_a_bigger_buffer(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.ANIMATION))), tmp_path)
    _load(s, fps=24.0031, w=1280, h=720)  # RIFE's 110 at 720p covers 1.67 × 48
    s.props["video-params"]["chroma-location"] = "mpeg2/4/h264"
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    ((data, buffered),) = _filters(s)
    assert data["hold_fill"] is True and data["hold_max"] == live.HOLD_MAX
    assert data["still_skip"] is True
    assert data["chromaloc"] == "left"
    # the script reads frames further back than mpv's default buffer of 4
    assert buffered >= 2 * live.HOLD_MAX + 4 == live.hold_fill_buffer(live.HOLD_MAX)


async def test_animation_without_room_keeps_the_engine_and_skips_fill(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    # 1080p: RIFE keeps up with 2x (48.96 ≥ 48) but not with hold fill on top:
    # RIFE stays, unfilled, rather than dropping a third of the frames
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.ANIMATION))), tmp_path)
    _load(s, fps=24.0031)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    ((data, buffered),) = _filters(s)
    assert data["hold_fill"] is False and buffered == 4


async def test_drops_with_fill_turn_it_off_and_teach_nothing(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    from buttereye.core.mpvctl import livelimits

    bench_history.append(_devbox())
    paths = _paths(tmp_path)
    s = _session(FakeCtx(paths, _config(_profile(ContentKind.ANIMATION))), tmp_path)
    _load(s, fps=24.0031, w=1280, h=720)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN and s.run_fill and s.auto_chosen
    await s.live_fallback(0.3)
    # same engine, no hold fill, nothing recorded against the engine
    assert _engine(s) is BackendId.RIFE_NCNN
    assert _filters(s)[-1][0]["hold_fill"] is False and not s.run_fill
    assert not (paths.state_dir / livelimits.FILE).exists()
    # a live-action video next still gets RIFE at 1080p
    s2 = _session(FakeCtx(paths, _config(_profile(ContentKind.LIVE))), tmp_path)
    _load(s2, fps=24.0031)
    await s2.apply(raise_on_fail=False)
    assert _engine(s2) is BackendId.RIFE_NCNN


async def test_animation_puts_rife_ncnn_before_tensorrt_when_it_has_room(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    anime = _profile(ContentKind.ANIMATION)
    # 1080p: RIFE-ncnn at 120 covers 1.67 × 48 with hold fill; TensorRT has none
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(120.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(anime))
    s = _session(ctx, tmp_path)
    _ready(ctx, s)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    assert _filters(s)[-1][0]["hold_fill"] is True
    # live action keeps TensorRT first
    ctx2 = FakeCtx(_paths(tmp_path), _opted(_profile(ContentKind.LIVE)))
    s2 = _session(ctx2, tmp_path)
    _load(s2, fps=23.976)
    await s2.apply(raise_on_fail=False)
    assert _engine(s2) is BackendId.RIFE_TRT


async def test_animation_keeps_tensorrt_when_rife_ncnn_has_no_room(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))  # 65 < 1.67 × 48
    ctx = FakeCtx(_paths(tmp_path), _opted(_profile(ContentKind.ANIMATION)))
    s = _session(ctx, tmp_path)
    _ready(ctx, s)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_TRT
    assert _filters(s)[-1][0]["hold_fill"] is False


async def test_live_action_never_fills(tmp_path: Path, bench_history: list[BenchResult]) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.LIVE))), tmp_path)
    _load(s, fps=24.0031, w=1280, h=720)  # plenty of room: still off
    await s.apply(raise_on_fail=False)
    ((data, buffered),) = _filters(s)
    assert data["hold_fill"] is False and data["still_skip"] is True
    assert buffered == 4
    assert "chromaloc" not in data  # mpv did not say: zimg's default


async def test_automatic_fills_only_with_room_to_spare(
    tmp_path: Path, bench_history: list[BenchResult], caplog: pytest.LogCaptureFixture
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.AUTO))), tmp_path)
    # 1080p: RIFE keeps up with 2x (48.96 ≥ 48) but not with 1.67 × that
    _load(s, fps=24.0031)
    with caplog.at_level("INFO", logger="buttereye.core.mpvctl.session"):
        await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    assert _filters(s)[-1][0]["hold_fill"] is False
    assert "re-timing held drawings off" in caplog.text
    # 720p: 2.25 × the 1080p rate covers it
    s2 = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.AUTO))), tmp_path)
    _load(s2, fps=24.0031, w=1280, h=720)
    await s2.apply(raise_on_fail=False)
    ((data, buffered),) = _filters(s2)
    assert data["hold_fill"] is True and buffered >= live.hold_fill_buffer(live.HOLD_MAX)


async def test_automatic_without_a_benchmark_does_not_fill(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.AUTO))), tmp_path)
    _load(s, fps=23.976, w=1280, h=720)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVTOOLS
    ((data, _),) = _filters(s)
    assert data["hold_fill"] is False
    # MVTools gets the scene-cut threshold too now (cut handling in the script)
    assert data["sc_threshold"] == pytest.approx(0.12)


async def test_tensorrt_never_fills(tmp_path: Path, bench_history: list[BenchResult]) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.ANIMATION))), tmp_path)
    profile = _profile(ContentKind.ANIMATION, BackendId.RIFE_TRT)
    on, why = await s.hold_fill_choice(
        profile, BackendId.RIFE_TRT, _facts(), None, NTSC * 2, s.ctx.config(),
        model=None, forced=False, block=False,
    )  # fmt: skip
    assert on is False and "TensorRT" in why
    on, _ = await s.hold_fill_choice(
        profile, BackendId.MVTOOLS, _facts(), None, NTSC * 2, s.ctx.config(),
        model=None, forced=False, block=False,
    )  # fmt: skip
    assert on is True  # no speed test: 2D animation asks for it
    for forced, block in ((True, False), (False, True)):
        on, _ = await s.hold_fill_choice(
            profile, BackendId.MVTOOLS, _facts(), None, NTSC * 2, s.ctx.config(),
            model=None, forced=forced, block=block,
        )  # fmt: skip
        assert on is False  # the engine already can't keep up


async def test_automatic_forced_or_block_mode_does_not_fill(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_profile(ContentKind.AUTO))), tmp_path)
    profile = _profile(ContentKind.AUTO)
    small = _facts(1280, 720)
    for forced, block in ((True, False), (False, True)):
        on, _ = await s.hold_fill_choice(
            profile, BackendId.MVTOOLS, small, None, NTSC * 2, s.ctx.config(),
            model=None, forced=forced, block=block,
        )  # fmt: skip
        assert on is False


async def test_animation_ranks_mvtools_before_mvgpu(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    paths = _paths(tmp_path)
    (paths.rpm_plugin_dir / decide.MVGPU_PLUGIN).write_bytes(b"")
    # mvgpu measured and fast, RIFE never measured
    bench_history.append(_result(_m("mvgpu", 400.0, backend=BackendId.MVGPU, model=None)))
    s = _session(FakeCtx(paths, _config(_simple())), tmp_path)
    _load(s, fps=24.0031)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVGPU
    s2 = _session(FakeCtx(paths, _config(_profile(ContentKind.ANIMATION))), tmp_path)
    _load(s2, fps=24.0031)
    await s2.apply(raise_on_fail=False)
    assert _engine(s2) is BackendId.MVTOOLS


async def test_pinned_engine_is_kept_for_animation(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    paths = _paths(tmp_path)
    (paths.rpm_plugin_dir / decide.MVGPU_PLUGIN).write_bytes(b"")
    bench_history.append(_result(_m("mvgpu", 400.0, backend=BackendId.MVGPU, model=None)))
    profile = dataclasses.replace(
        _profile(ContentKind.ANIMATION, BackendId.MVGPU), target=Target(TargetKind.X2)
    )
    s = _session(FakeCtx(paths, _config(profile)), tmp_path)
    _load(s, fps=24.0031)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVGPU
    ((data, _),) = _filters(s)
    assert data["hold_fill"] is True
