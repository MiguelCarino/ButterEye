# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A cascade for "Match your display": TensorRT RIFE to twice the source rate,
mvgpu from there to the display rate (docs/spikes/m0u.md). No mpv, no GPU: the
decisions and the filter the session adds are real, the engines are not run.

Also here: the decision waits until mpv's window is on a screen, because the
display rate mpv reports before that is not the window's.

Caps are the dev box's (RTX 4090, in mpv, 2x): TensorRT RIFE v4.26 206 fps at
1080p; mvgpu 527 at 1080p, 268 at 1440p, 114 at 2160p."""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.bench.verdict import LIVE_HEADROOM
from buttereye.core.mpvctl import decide
from buttereye.core.scriptgen import generator as g
from buttereye.core.types import (
    BackendId,
    BenchResult,
    ContentKind,
    FilterState,
    Profile,
    Target,
    TargetKind,
)
from tests.test_live_decisions import (
    NTSC,
    FakeCtx,
    _load,
    _result,
    _session,
    _simple,
)
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture
from tests.test_live_trt import TrtFake, _filters, _opted, _ready, _trt_m
from tests.test_live_trt import fake_trt as _fake_trt_fixture
from tests.test_mvgpu_live import _mvgpu_m, _mvgpu_paths

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture
fake_trt = _fake_trt_fixture

TRT_1080, MV_1080, MV_1440, MV_2160 = 206.0, 527.0, 268.0, 114.0
F24 = Fraction(24)


def _devbox() -> BenchResult:
    return _result(
        _trt_m(TRT_1080),
        _mvgpu_m(MV_1080),
        _mvgpu_m(MV_1440, "mvgpu @ 1440p"),
        _mvgpu_m(MV_2160, "mvgpu @ 2160p"),
    )


# ---------------------------------------------------------------- the rates
@pytest.mark.parametrize(
    ("src", "hz", "expected"),
    [
        (F24, 144.0, (Fraction(48), Fraction(144))),
        (NTSC, 144.0, (NTSC * 2, NTSC * 6)),  # 143.856: mpv resamples the 0.1 %
        (Fraction(30), 120.0, (Fraction(60), Fraction(120))),
        (Fraction(30), 180.0, (Fraction(60), Fraction(180))),
        (F24, 96.0, (Fraction(48), Fraction(96))),
        (F24, 60.0, None),  # 2.5x: RIFE alone, as before
        (F24, 120.0, None),  # 2.5 steps of 48
        (F24, 165.0, None),
        (Fraction(30), 144.0, None),
        (F24, 240.0, None),  # 10x is over the display target's 8x
        (F24, 48.0, None),  # 2x: nothing for mvgpu to add
        (F24, None, None),
    ],
)
def test_cascade_target(
    src: Fraction, hz: float | None, expected: tuple[Fraction, Fraction] | None
) -> None:
    assert decide.cascade_target(src, hz) == expected


def test_cascade_load_on_the_dev_box() -> None:
    """4K 24 fps on 144 Hz: fits at 1080p, not at 1440p (measured in mpv: 1.30x
    real time at 1080p, 0.90x at 1440p)."""
    rife, mv = TRT_1080 / LIVE_HEADROOM, MV_1080 / LIVE_HEADROOM
    at_1080 = decide.cascade_load(F24, Fraction(48), Fraction(144), rife, mv,
                                  shrunk_from=(3840, 2160))  # fmt: skip
    assert at_1080 is not None and 0.7 < at_1080 <= 1.0
    ratio = (1920 * 1080) / (2560 * 1440)
    at_1440 = decide.cascade_load(F24, Fraction(48), Fraction(144), rife * ratio,
                                  MV_1440 / LIVE_HEADROOM, shrunk_from=(3840, 2160))  # fmt: skip
    assert at_1440 is not None and at_1440 > 1.0
    # the video's own size costs no shrinking; hold fill's worst case costs RIFE more
    own = decide.cascade_load(F24, Fraction(48), Fraction(144), rife, mv)
    assert own is not None and own < at_1080
    held = decide.cascade_load(F24, Fraction(48), Fraction(144), rife, mv, rife_factor=1.5)
    assert held is not None and held > own
    assert decide.cascade_load(F24, Fraction(48), Fraction(144), None, mv) is None
    assert decide.cascade_load(F24, Fraction(48), Fraction(144), rife, 0.0) is None


# ---------------------------------------------------------------- the session
def _display(backend: BackendId | str = "auto") -> Profile:
    """The shipped profile: "Match your display"."""
    return _simple(backend, Target(TargetKind.DISPLAY))


def _ctx(tmp_path: Path, profile: Profile | None = None) -> FakeCtx:
    return FakeCtx(_mvgpu_paths(tmp_path), _opted(profile or _display()))


async def test_4k_on_144hz_runs_the_cascade_at_1080p(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["size"] == [1920, 1080]
    assert f["target_fps"] == [144, 1] and f["cascade_fps"] == [48, 1]
    assert "cascade_gpu_id" in f and f["trt"]["build"] is False
    assert s.backend is BackendId.RIFE_TRT and s.target == 144 and s.cascade == 48
    assert s.notice == decide.cascade_notice(1080)
    assert fake_trt.builds == []


async def test_1080p_on_144hz_runs_the_cascade_at_its_own_size(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """RIFE alone can't make 144 at 1080p (206 fps measured, 300 needed), and
    mvgpu alone would: the cascade has RIFE's picture, so it takes over."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and "size" not in f
    assert f["target_fps"] == [144, 1] and f["cascade_fps"] == [48, 1]
    assert s.notice == decide.cascade_notice(None)


async def test_ntsc_film_takes_whole_steps(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=23.976, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["cascade_fps"] == [48000, 1001] and f["target_fps"] == [144000, 1001]


async def test_rife_that_makes_the_display_rate_alone_is_kept(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(400.0), _mvgpu_m(MV_1080)))
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and "cascade_fps" not in f and s.cascade is None


@pytest.mark.parametrize("case", ["60hz", "pinned-mvgpu", "animation", "2x", "no-mvgpu"])
async def test_no_cascade(
    case: str, tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    profile = _display(BackendId.MVGPU) if case == "pinned-mvgpu" else _display()
    if case == "animation":
        profile = dataclasses.replace(profile, content=ContentKind.ANIMATION)
    if case == "2x":
        profile = dataclasses.replace(profile, target=Target(TargetKind.X2))
    paths = _mvgpu_paths(tmp_path, mvgpu=case != "no-mvgpu")
    ctx = FakeCtx(paths, _opted(profile))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=60.0 if case == "60hz" else 144.0)
    await s.apply(raise_on_fail=False)
    assert s.cascade is None
    assert all("cascade_fps" not in f for f in _filters(s))


async def test_engine_for_the_cascade_size_is_built_first(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """No 1080p engine yet: another engine plays while it is built, without a cascade."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _load(s, fps=24.0, w=3840, h=2160, hz=144.0)
    await s.apply(raise_on_fail=False)
    first = _filters(s)[0]
    assert first["backend"] != "rife-trt" and "cascade_fps" not in first
    assert s.cascade is None or s.backend is not BackendId.RIFE_TRT
    assert s.trt_waiting is not None
    assert (s.trt_waiting.width, s.trt_waiting.height) == (1920, 1080)


# ---------------------------------------------------------------- the display rate
async def test_decision_waits_for_the_window_to_be_on_a_screen(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """mpv said 75 Hz (another screen's rate) until its window was on one, then
    144: nothing is decided on the first answer."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=75.0)
    s.props["display-names"] = []
    await s.apply(raise_on_fail=False)
    assert _filters(s) == [] and s.filter is FilterState.PENDING
    s.props.update({"display-names": ["DP-1"], "display-fps": 144.0})
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["target_fps"] == [144, 1]


async def test_a_window_that_never_reaches_a_screen_does_not_wait_forever(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=144.0)
    s.props["display-names"] = []
    s.opts.display_settle_s = 0.0
    await s.apply(raise_on_fail=False)
    assert len(_filters(s)) == 1


def test_no_display_names_means_nothing_to_wait_for(tmp_path: Path) -> None:
    s = _session(_ctx(tmp_path), tmp_path)
    assert not s.display_unsettled({"display-fps": 60.0})  # --vo=null: no such property
    assert not s.display_unsettled({"display-names": ["DP-1"]})
    assert s.display_unsettled({"display-names": []})


# ---------------------------------------------------------------- the script
def test_user_data_carries_the_cascade_only_for_rife() -> None:
    base: dict[str, Any] = dict(
        gen=1, src_fps=F24, target_fps=Fraction(144), buffered_frames=4, concurrent_frames=8,
        cascade_fps=Fraction(48), cascade_gpu_id=0,
    )  # fmt: skip
    import json

    mv = json.loads(g.user_data(g.FilterParams(backend=BackendId.MVGPU, **base)))
    assert "cascade_fps" not in mv and "cascade_gpu_id" not in mv
    ncnn = json.loads(g.user_data(g.FilterParams(backend=BackendId.RIFE_NCNN, **base)))
    assert ncnn["cascade_fps"] == [48, 1] and ncnn["cascade_gpu_id"] == 0


def test_template_runs_mvgpu_after_the_rife_engine() -> None:
    text = g.template_text()
    assert "def _cascade(clip, cfg, mid, target):" in text
    build = text[text.index("def build():") :]
    assert build.index("_rife_trt(clip, cfg, props, target)") < build.index("_cascade(out, cfg")
    assert "cfg.get('cascade_fps') and backend in ('rife-ncnn', 'rife-trt')" in build
