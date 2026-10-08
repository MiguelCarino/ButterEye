# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Reliable live decisions (GUI.md §11.9) without a real mpv: live headroom,
size-scaled benchmark caps, engine chosen after the target, display-target caps,
and the one-time switch from RIFE-ncnn to MVTools after a stall or GPU fault.

The benchmark numbers mirror the dev box (RTX 4090, 3440x1440 @ 180 Hz):
1080p RIFE-ncnn v4.26 61.2 fps and MVTools 102.31 fps in mpv (untimed, --vo=null).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import re
import sys
import time
import types as pytypes
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.errors import ErrorCode
from buttereye.core.events import Event, HealthChanged, Notice
from buttereye.core.mpvctl import decide, health
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import Verdict
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    BypassReason,
    Config,
    FilterState,
    HdrClass,
    Health,
    Msg,
    Profile,
    SessionId,
    SourceFacts,
    Target,
    TargetKind,
    VulkanDevice,
)

NTSC = Fraction(24000, 1001)
# the user's file: misses an exact 24000/1001 bound
ODD: Fraction = decide.fps_fraction(24.0031) or Fraction(0)
assert ODD > 24
GPU = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_GPU = "aaaaaaaa-0000-0000-0000-000000000001"
DEVICES = (
    VulkanDevice(0, OTHER_GPU, "iGPU", "intel", "integrated", 1 << 30),
    VulkanDevice(1, GPU, "RTX 4090", "nvidia", "discrete", 24 << 30),
)
M26 = "rife-v4.26_ensembleFalse"
RIFE_1080 = 61.2
MV_1080 = 102.31
RIFE_CAP = RIFE_1080 / live.LIVE_HEADROOM  # 48.96
MV_CAP = MV_1080 / live.LIVE_HEADROOM  # 81.85
NOW = datetime(2026, 10, 7, 23, 17, tzinfo=UTC)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class FakeCtx:
    def __init__(self, paths: Any, cfg: Config) -> None:
        self.paths = paths
        self.cfg = cfg
        self.report = pytypes.SimpleNamespace(hardware=pytypes.SimpleNamespace(vulkan=DEVICES))
        self.events: list[Event] = []
        self.tasks: list[asyncio.Task[Any]] = []
        self._state: dict[str, Any] = {}

    def config(self) -> Config:
        return self.cfg

    def last_report(self) -> Any:
        return self.report

    def emit(self, ev: Event) -> None:
        self.events.append(ev)

    def emit_coalesced(self, key: object, ev: Event, *, min_interval_s: float) -> None:
        self.events.append(ev)

    def forget_coalesced(self, key: object) -> None:
        pass

    def spawn(self, coro: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self.tasks.append(task)
        return task

    def state(self, key: str, factory: Any) -> Any:
        return self._state.setdefault(key, factory())


class FakeIpc:
    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.last_rx_mono = time.monotonic()
        self.commands: list[tuple[object, ...]] = []

    async def command(self, *args: object, timeout_s: float = 5.0) -> Any:
        self.commands.append(args)
        return None

    async def get(self, prop: str, *, timeout_s: float = 5.0) -> Any:
        return None

    async def close(self) -> None:
        self.closed.set()

    def added(self) -> list[str]:
        """The engine of every ``vf add`` (from the filter's user-data)."""
        out = []
        for c in self.commands:
            if c[:2] == ("vf", "add"):
                arg = str(c[2])
                out.append("rife-ncnn" if '"rife-ncnn"' in arg else "mvtools")
        return out

    def sizes(self) -> list[tuple[int, int] | None]:
        """The ``size`` of every ``vf add`` (None = the video's own size)."""
        out: list[tuple[int, int] | None] = []
        for c in self.commands:
            if c[:2] == ("vf", "add"):
                arg = str(c[2])
                m = re.search(r'"size":\[(\d+),(\d+)\]', arg)
                out.append((int(m.group(1)), int(m.group(2))) if m else None)
        return out


def _paths(tmp_path: Path, *, rife: bool = True, mvtools: bool = True) -> Any:
    plugins, share = tmp_path / "plugins", tmp_path / "share"
    plugins.mkdir(exist_ok=True)
    if rife:
        (plugins / "librife.so").write_bytes(b"")
        for name in decide.DEFAULT_MODELS:
            (share / live.MODEL_SUBDIR / name).mkdir(parents=True, exist_ok=True)
    if mvtools:
        (plugins / "mvtools.so").write_bytes(b"")
    return dataclasses.replace(sc.fake_paths(tmp_path), rpm_plugin_dir=plugins, rpm_data_dir=share)


def _simple(backend: BackendId | str = "auto", target: Target | None = None) -> Profile:
    """The one profile the simple window stores (id "simple")."""
    return Profile(
        id="simple", name="Simple", backend=backend, model=None, scale=None,  # type: ignore[arg-type]
        target=target or Target(TargetKind.X2), sc_threshold=0.12, buffered_frames=None,
        concurrent_frames=None,
    )  # fmt: skip


def _config(profile: Profile) -> Config:
    base = sc.default_config()
    return dataclasses.replace(base, profiles=base.profiles + (profile,))


def _session(ctx: FakeCtx, tmp_path: Path) -> live._Session:
    proc = pytypes.SimpleNamespace(pid=os.getpid(), returncode=None)
    s = live._Session(
        ctx,  # type: ignore[arg-type]
        live.LiveOptions(),
        sid=SessionId("s1"),
        file=tmp_path / "film.mkv",
        proc=proc,  # type: ignore[arg-type]
        ipc=FakeIpc(),  # type: ignore[arg-type]
        socket=tmp_path / "mpv-s1.sock",
        log_path=tmp_path / "mpv-s1.log",
        script_path=tmp_path / "mpv-s1.vpy",
        profile_request="simple",
    )

    async def verify(gen: int, target_fps: float) -> bool:
        return not s.failure

    s.verify = verify  # type: ignore[method-assign]
    return s


def _load(s: live._Session, *, fps: float, w: int = 1920, h: int = 1080, hz: float = 180.0) -> None:
    s.props.update({
        "path": "/videos/film.mkv",
        "container-fps": fps,
        "display-fps": hz,
        "video-params": {"w": w, "h": h, "pixelformat": "yuv420p", "colormatrix": "bt.709"},
    })  # fmt: skip
    s.file_loaded_at = time.monotonic()


def _engine(s: live._Session) -> BackendId | None:
    """The engine in use (read through a call so mypy does not narrow it)."""
    return s.backend


def _facts(w: int = 1920, h: int = 1080, fps: Fraction = NTSC) -> SourceFacts:
    return SourceFacts(fps, w, h, HdrClass.SDR, 180.0, False, "/f.mkv", False)


def _m(label: str, mpv: float, *, backend: BackendId = BackendId.RIFE_NCNN,
       model: str | None = M26, faults: int | None = 0,
       failed_mpv: bool = False) -> BenchMeasurement:  # fmt: skip
    return BenchMeasurement(
        label=label, backend=backend, model=model if backend is BackendId.RIFE_NCNN else None,
        vspipe_fps=mpv + 8, mpv_fps=mpv + 8 if failed_mpv else mpv, cov=0.01, repeatable=True,
        startup_s=0.9 if failed_mpv else 1.1, reload_s=0.9, vram_bytes=None,
        realtime=not failed_mpv, gpu_faults=faults,
    )  # fmt: skip


def _result(*ms: BenchMeasurement, size: tuple[int, int] = (1920, 1080),
            when: datetime = NOW, gpu: str | None = GPU) -> BenchResult:  # fmt: skip
    return BenchResult(BenchRequest(size[0], size[1], NTSC), ms, None, when, gpu)


def _devbox() -> BenchResult:
    """The dev box's real 1080p benchmark (bench.json shape)."""
    return _result(
        _m("rife-v4.26", RIFE_1080),
        _m("rife-v4.18", 59.42, model="rife-v4.18_ensembleFalse", faults=1),
        _m("rife-v4.22-lite", 62.61, model="rife-v4.22_lite_ensembleFalse"),
        _m("mvtools", MV_1080, backend=BackendId.MVTOOLS),
    )


@pytest.fixture
def bench_history(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BenchResult]]:
    results: list[BenchResult] = []

    async def history(ctx: Any) -> tuple[BenchResult, ...]:
        return tuple(results)

    mod = pytypes.ModuleType("buttereye.core.bench.runner")
    mod.history = history  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.bench.runner", mod)
    yield results


@pytest.fixture
def no_xid(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A readable, empty kernel journal (lines can be added per test)."""
    state: dict[str, Any] = {"lines": ()}

    async def scan_xid(**kw: Any) -> Any:
        return pytypes.SimpleNamespace(readable=True, lines=state["lines"])

    mod = pytypes.ModuleType(health.GPUFAULT_MODULE)
    mod.scan_xid = scan_xid  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, health.GPUFAULT_MODULE, mod)
    monkeypatch.setattr(health, "JOURNAL_SETTLE_S", 0.0)
    yield state


def _stall() -> Verdict:
    return Verdict(
        Health.STALLED,
        ErrorCode.FILTER_STALLED,
        Msg("Video stalled while audio kept playing."),
        ("playback-time frozen for 2.0 s",),
        False,
        None,
    )


async def _settle(ctx: FakeCtx) -> None:
    for t in list(ctx.tasks):
        await asyncio.wait_for(t, 5)


# ---------------------------------------------------------------------------
# 2. live headroom and size-scaled caps
# ---------------------------------------------------------------------------


def test_live_headroom_is_stricter_than_the_bench_verdict() -> None:
    assert live.LIVE_HEADROOM == 1.25 and live.REALTIME_HEADROOM == 1.15
    assert RIFE_CAP == pytest.approx(48.96)
    # 1080p RIFE: 24 → 48 fits, 30 → 60 does not
    assert decide.choose_target(NTSC, None, TargetKind.X2, sustainable_fps=RIFE_CAP).target == (
        NTSC * 2
    )
    assert (
        decide.choose_target(ODD, None, TargetKind.X2, sustainable_fps=RIFE_CAP).target == ODD * 2
    )
    thirty = decide.choose_target(Fraction(30), None, TargetKind.X2, sustainable_fps=RIFE_CAP)
    assert thirty.bypass is BypassReason.NO_REALTIME


async def test_cap_from_the_devbox_benchmark(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    cfg = s.ctx.config()
    assert await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(), cfg) == pytest.approx(
        RIFE_CAP
    )
    assert await s.sustainable_fps(BackendId.MVTOOLS, None, _facts(), cfg) == pytest.approx(MV_CAP)
    # the faulted v4.18 is never a cap
    assert await s.sustainable_fps(BackendId.RIFE_NCNN, "rife-v4.18_ensembleFalse", _facts(),
                                   cfg) is None  # fmt: skip


@pytest.mark.parametrize(
    ("video", "factor"),
    [((1280, 720), 2.25), ((3840, 2160), 0.25), ((2560, 1440), 0.5625), ((1920, 800), 1.35)],
)
async def test_cap_scales_by_pixel_count(
    tmp_path: Path, bench_history: list[BenchResult], video: tuple[int, int], factor: float
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(*video), s.ctx.config())
    assert cap == pytest.approx(RIFE_1080 * factor / live.LIVE_HEADROOM)


async def test_cap_prefers_closest_size_then_newest(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    cfg = s.ctx.config()
    bench_history += [
        _result(_m("rife-v4.26", 61.2)),  # 1080p, newest
        _result(_m("rife-v4.26", 130.0), size=(1280, 720), when=NOW - timedelta(days=3)),
    ]
    # a 720p video uses the 720p measurement, though older
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(1280, 720), cfg)
    assert cap == pytest.approx(130.0 / live.LIVE_HEADROOM)
    # equally close (same size): the newest wins over a faster older one
    bench_history.append(_result(_m("rife-v4.26", 90.0), when=NOW - timedelta(days=1)))
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(), cfg)
    assert cap == pytest.approx(61.2 / live.LIVE_HEADROOM)


async def test_cap_reads_full_benchmark_sizes(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    """A full benchmark stores other sizes as "<label> @ <h>p" in one result."""
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    bench_history.append(_result(_m("rife-v4.26", 61.2), _m("rife-v4.26 @ 2160p", 16.0)))
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(3840, 2160), s.ctx.config())
    assert cap == pytest.approx(16.0 / live.LIVE_HEADROOM)


async def test_cap_skips_faults_failed_mpv_pass_and_other_gpu(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    cfg = s.ctx.config()
    bench_history += [
        _result(_m("rife-v4.26", 200.0, faults=1)),
        _result(_m("rife-v4.26", 200.0, failed_mpv=True)),
        _result(_m("rife-v4.26", 200.0), gpu=OTHER_GPU),
    ]
    assert await s.sustainable_fps(BackendId.RIFE_NCNN, M26, _facts(), cfg) is None
    # MVTools runs on the CPU: the GPU it was measured with does not matter
    bench_history.append(_result(_m("mvtools", 100.0, backend=BackendId.MVTOOLS), gpu=OTHER_GPU))
    assert await s.sustainable_fps(BackendId.MVTOOLS, None, _facts(), cfg) == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# 4. display target respects the cap (180 Hz)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("src", [Fraction(24), NTSC, ODD])
def test_display_180hz_respects_the_cap(src: Fraction) -> None:
    # caps are 2x rates; 60 from ~24 infers 4 frames in 5 or more (≥ 96 at 2x)
    for kind in (TargetKind.DISPLAY, TargetKind.DISPLAY_MAX):
        mv = decide.choose_target(src, 180.0, kind, sustainable_fps=MV_CAP)
        assert mv.target == src * 2 and mv.bypass is None
        rife = decide.choose_target(src, 180.0, kind, sustainable_fps=RIFE_CAP)
        assert rife.target == src * 2 and rife.bypass is None
        # 24 → 30/36/45 costs as many inferences as 2x: none of them fits 30
        slow = decide.choose_target(src, 180.0, kind, sustainable_fps=30.0)
        assert slow.target is None and slow.bypass is BypassReason.NO_REALTIME
    uncapped = decide.choose_target(src, 180.0, TargetKind.DISPLAY)
    assert uncapped.target == 60  # the lowest refresh/k that doubles (k=3)
    smoothest = decide.choose_target(src, 180.0, TargetKind.DISPLAY_MAX)
    assert smoothest.target == 90  # k=2: 7.5x at k=1 is over the 5x limit
    fast = decide.choose_target(src, 180.0, TargetKind.DISPLAY_MAX, sustainable_fps=130.0)
    assert fast.target == 60  # 90 needs ≥ 168 at 2x


# ---------------------------------------------------------------------------
# 1. engine chosen after the target (pure)
# ---------------------------------------------------------------------------


def _opts(rife: float | None, mv: float | None = MV_CAP) -> list[decide.EngineOption]:
    return [
        decide.EngineOption(BackendId.RIFE_NCNN, rife),
        decide.EngineOption(BackendId.MVTOOLS, mv),
    ]


def test_auto_uses_rife_when_it_can_double() -> None:
    pick = decide.pick_engine(_opts(RIFE_CAP), ODD, 180.0, TargetKind.X2, auto=True)
    assert pick.backend is BackendId.RIFE_NCNN and pick.target.target == ODD * 2
    assert pick.notice is None
    fixed = decide.pick_engine(_opts(RIFE_CAP), NTSC, 180.0, TargetKind.FPS, Fraction(60),
                               auto=True)  # fmt: skip
    # 23.976 → 60 infers almost every frame (~120 at 2x): neither engine keeps up
    assert fixed.backend is None and fixed.target.bypass is BypassReason.NO_REALTIME


def test_auto_30fps_goes_to_mvtools() -> None:
    pick = decide.pick_engine(_opts(RIFE_CAP), Fraction(30), 180.0, TargetKind.X2, auto=True)
    assert pick.backend is BackendId.MVTOOLS and pick.target.target == 60 and pick.notice is None


def test_auto_without_rife_benchmark_uses_mvtools() -> None:
    pick = decide.pick_engine(_opts(None, None), NTSC, 60.0, TargetKind.X2, auto=True)
    assert pick.backend is BackendId.MVTOOLS and pick.notice is None
    # an unmeasured RIFE is still used when it is the only engine installed
    only = decide.pick_engine([decide.EngineOption(BackendId.RIFE_NCNN, None)], NTSC, 60.0,
                              TargetKind.X2, auto=True)  # fmt: skip
    assert only.backend is BackendId.RIFE_NCNN


def test_auto_display_prefers_the_engine_that_really_smooths() -> None:
    # 180 Hz: RIFE doubles (plain 2x); 60 would need ~120 at 2x → RIFE at 2x
    pick = decide.pick_engine(_opts(RIFE_CAP), NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert pick.backend is BackendId.RIFE_NCNN and pick.target.target == NTSC * 2
    # 144 Hz: 24 → 48 (k=3) is an exact doubling → RIFE
    pick = decide.pick_engine(_opts(RIFE_CAP), Fraction(24), 144.0, TargetKind.DISPLAY,
                              auto=True)  # fmt: skip
    assert pick.backend is BackendId.RIFE_NCNN and pick.target.target == 48
    # a GPU fast enough for 60 → RIFE at 60 (quality first)
    pick = decide.pick_engine(_opts(125.0), NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert pick.backend is BackendId.RIFE_NCNN and pick.target.target == 60


def test_auto_nothing_keeps_up_is_no_realtime_with_notice() -> None:
    pick = decide.pick_engine(_opts(20.0, 25.0), Fraction(30), 60.0, TargetKind.X2, auto=True)
    assert pick.backend is None and pick.target.bypass is BypassReason.NO_REALTIME
    assert pick.notice == decide.no_realtime_notice()
    assert pick.notice.key == (
        "This video is too demanding to smooth in real time on this computer, "
        "so it plays without smoothing."
    )


def test_auto_already_at_rate_is_not_an_engine_question() -> None:
    pick = decide.pick_engine(_opts(RIFE_CAP), Fraction(60), 60.0, TargetKind.DISPLAY, auto=True)
    assert pick.backend is None and pick.target.bypass is BypassReason.ALREADY_AT_RATE
    assert pick.notice is None


def test_pinned_rife_falls_back_to_cpu_with_notice() -> None:
    pick = decide.pick_engine(_opts(RIFE_CAP), Fraction(30), 180.0, TargetKind.X2, auto=False)
    assert pick.backend is BackendId.MVTOOLS and pick.target.target == 60
    assert pick.notice is not None and pick.notice.params == {"target": "60"}
    assert pick.notice.key == (
        "The GPU engine can't keep up with {target} fps at this size, so ButterEye is "
        "using CPU smoothing for this video."
    )
    # pinned RIFE that can keep up stays RIFE; with no benchmark it is trusted
    assert decide.pick_engine(_opts(RIFE_CAP), NTSC, 180.0, TargetKind.X2,
                              auto=False).backend is BackendId.RIFE_NCNN  # fmt: skip
    assert decide.pick_engine(_opts(None), Fraction(30), 180.0, TargetKind.X2,
                              auto=False).backend is BackendId.RIFE_NCNN  # fmt: skip
    # neither keeps up → bypass
    none = decide.pick_engine(_opts(20.0, 25.0), Fraction(30), 180.0, TargetKind.X2, auto=False)
    assert none.backend is None and none.target.bypass is BypassReason.NO_REALTIME
    assert none.notice == decide.no_realtime_notice()


def test_forced_runs_the_first_engine_uncapped() -> None:
    slow = _opts(20.0, 25.0)
    pick = decide.pick_engine(slow, Fraction(30), 60.0, TargetKind.X2, auto=True, forced=True)
    assert pick.backend is BackendId.RIFE_NCNN and pick.target.target == 60
    assert pick.notice == decide.forced_notice()
    pinned = decide.pick_engine(slow, Fraction(30), 60.0, TargetKind.X2, auto=False, forced=True)
    assert pinned.backend is BackendId.RIFE_NCNN and pinned.notice == decide.forced_notice()
    cpu = [decide.EngineOption(BackendId.MVTOOLS, 25.0)]
    pick = decide.pick_engine(cpu, Fraction(30), 60.0, TargetKind.X2, auto=False, forced=True)
    assert pick.backend is BackendId.MVTOOLS and pick.target.target == 60


def test_forced_changes_nothing_that_already_works() -> None:
    for src, hz, kind in ((ODD, 180.0, TargetKind.X2), (Fraction(60), 60.0, TargetKind.DISPLAY)):
        plain = decide.pick_engine(_opts(RIFE_CAP), src, hz, kind, auto=True)
        assert decide.pick_engine(_opts(RIFE_CAP), src, hz, kind, auto=True, forced=True) == plain
    # forcing never smooths past the multiplier cap or a video already at the display rate
    pick = decide.pick_engine(_opts(20.0, 25.0), Fraction(60), 60.0, TargetKind.DISPLAY,
                              auto=True, forced=True)  # fmt: skip
    assert pick.backend is None and pick.target.bypass is BypassReason.ALREADY_AT_RATE


def test_smaller_sizes_keep_the_shape() -> None:
    assert decide.smaller_sizes(3840, 2160) == ((2560, 1440), (1920, 1080), (1280, 720))
    assert decide.smaller_sizes(3840, 1600) == ((3456, 1440), (2592, 1080), (1728, 720))
    assert decide.smaller_sizes(1920, 1080) == ((1280, 720),)
    assert decide.smaller_sizes(1280, 720) == ()
    assert decide.smaller_sizes(0, 0) == ()


def test_shrink_cap_matches_the_devbox_measurements() -> None:
    # measured in mpv, 4K 10-bit HEVC 24 → 48 with RIFE v4.26: 47.5 fps at 1080p,
    # 86 fps at 720p; the model predicts from the 61.2 fps 1080p benchmark
    at_1080 = decide.shrink_cap(RIFE_1080, 3840, 2160)
    at_720 = decide.shrink_cap(RIFE_1080 * 2.25, 3840, 2160)
    assert at_1080 is not None and at_720 is not None
    assert at_1080 == pytest.approx(49.2, abs=0.5) and at_720 == pytest.approx(88.8, abs=1)
    assert decide.shrink_cap(None, 3840, 2160) is None
    assert decide.shrink_cap(RIFE_1080, 1920, 1080) == pytest.approx(57.6, abs=0.5)


def _sized(*caps: tuple[float | None, float | None]) -> list[Any]:
    sizes = ((2560, 1440), (1920, 1080), (1280, 720))
    return [(size, _opts(r, m)) for size, (r, m) in zip(sizes, caps, strict=False)]


def test_pick_size_prefers_the_gpu_at_a_smaller_size() -> None:
    sized = _sized((25.0, 39.0), (41.0, 61.0), (72.0, 120.0))
    found = decide.pick_size(sized, Fraction(24), 180.0, TargetKind.X2, auto=True)
    assert found is not None
    pick, size = found
    assert pick.backend is BackendId.RIFE_NCNN and size == (1280, 720)
    # no measured GPU engine: the largest size any engine keeps up at
    found = decide.pick_size(_sized((None, 39.0), (None, 61.0)), Fraction(24), 180.0,
                             TargetKind.X2, auto=True)  # fmt: skip
    assert found is not None and found[0].backend is BackendId.MVTOOLS
    assert found[1] == (1920, 1080)


def test_pick_size_display_target_needs_a_doubling_for_the_gpu() -> None:
    # 180 Hz: RIFE never reaches 48 at 2x (no doubling at any size); MVTools
    # doubles (plain 2x) already at 1440p, the largest size
    sized = _sized((30.0, 50.0), (37.0, 61.0), (40.0, 90.0))
    found = decide.pick_size(sized, Fraction(24), 180.0, TargetKind.DISPLAY, auto=True)
    assert found is not None and found[0].backend is BackendId.MVTOOLS
    assert found[0].target.target == 48 and found[1] == (2560, 1440)


def test_pick_size_pinned_rife_tries_smaller_before_cpu() -> None:
    sized = _sized((25.0, 39.0), (41.0, 61.0), (72.0, 120.0))
    found = decide.pick_size(sized, Fraction(24), 180.0, TargetKind.X2, auto=False)
    assert found is not None and found[0].backend is BackendId.RIFE_NCNN
    assert found[1] == (1280, 720) and found[0].notice is None
    slow = _sized((20.0, 39.0), (30.0, 61.0), (40.0, 120.0))
    found = decide.pick_size(slow, Fraction(24), 180.0, TargetKind.X2, auto=False)
    assert found is not None and found[0].backend is BackendId.MVTOOLS
    assert found[1] == (1920, 1080) and found[0].notice is not None  # CPU fallback notice


def test_pick_size_nothing_then_forced() -> None:
    sized = _sized((10.0, 10.0), (15.0, 15.0), (20.0, 20.0))
    args = (Fraction(24), 180.0, TargetKind.X2)
    assert decide.pick_size(sized, *args, auto=True) is None
    assert decide.pick_size([], *args, auto=True, forced=True) is None
    found = decide.pick_size(sized, *args, auto=True, forced=True)
    assert found is not None and found[1] == (1280, 720)
    assert found[0].backend is BackendId.RIFE_NCNN
    assert found[0].notice == decide.forced_smaller_notice(720)


def test_pinned_mvtools_never_moves_to_rife() -> None:
    opts = [decide.EngineOption(BackendId.MVTOOLS, 25.0)]
    pick = decide.pick_engine(opts, Fraction(30), 180.0, TargetKind.X2, auto=False)
    assert pick.backend is None and pick.target.bypass is BypassReason.NO_REALTIME


# ---------------------------------------------------------------------------
# 1. the session's decision (apply)
# ---------------------------------------------------------------------------


async def test_session_auto_24fps_uses_rife(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0031)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN and snap.model == M26
    assert snap.target_fps == ODD * 2 and snap.notice is None
    assert s.ipc.added() == ["rife-ncnn"]  # type: ignore[attr-defined]


async def test_session_auto_30fps_uses_mvtools(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=30.0)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.MVTOOLS and snap.model is None
    assert snap.target_fps == 60 and snap.notice is None


async def test_session_auto_without_benchmark_uses_mvtools(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVTOOLS and s.notice is None


async def test_session_display_target_180hz(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    profile = _simple(target=Target(TargetKind.DISPLAY))
    s = _session(FakeCtx(_paths(tmp_path), _config(profile)), tmp_path)
    _load(s, fps=24.0031, hz=180.0)
    await s.apply(raise_on_fail=False)
    # the GPU doubles (2x) instead of MVTools at 60: never 4-5x of 180 Hz
    assert _engine(s) is BackendId.RIFE_NCNN and s.multiplier == 2


async def test_session_pinned_rife_falls_back_for_this_file(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple(BackendId.RIFE_NCNN))), tmp_path)
    _load(s, fps=30.0)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVTOOLS and s.target == 60
    assert s.notice is not None and s.notice.params == {"target": "60"}
    assert "CPU smoothing for this video" in s.notice.key


async def test_session_nothing_keeps_up_bypasses_with_notice(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    # 60 → 120 at 720p: RIFE ~110, MVTools ~184 live; 120 → 240 nothing; 720p has
    # no smaller size to try
    _load(s, fps=120.0, w=1280, h=720)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.filter is FilterState.BYPASSED and snap.bypass is BypassReason.NO_REALTIME
    assert snap.backend is None and snap.target_fps is None
    assert snap.notice == decide.no_realtime_notice()
    assert s.ipc.added() == []  # type: ignore[attr-defined]


async def test_session_4k_smooths_at_a_smaller_size(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    # 24 → 48 at 4K, live caps after shrinking 4K: RIFE ~25 at 1440p, ~41 at
    # 1080p, ~72 at 720p (MVTools would reach 48 at 1080p, but GPU comes first)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.filter is not FilterState.BYPASSED
    assert snap.backend is BackendId.RIFE_NCNN and snap.target_fps == 48
    assert snap.notice == decide.smaller_size_notice(720)
    assert s.ipc.sizes() == [(1280, 720)]  # type: ignore[attr-defined]
    # uhd is decided at the size RIFE really runs at
    assert '"uhd":false' in str(s.ipc.commands[-1][2])  # type: ignore[attr-defined]


async def test_session_own_size_when_it_keeps_up(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0)
    await s.apply(raise_on_fail=False)
    assert s.ipc.sizes() == [None]  # type: ignore[attr-defined]
    assert '"size"' not in str(s.ipc.commands[-1][2])  # type: ignore[attr-defined]


async def test_session_forced_smooths_anyway_at_the_smallest_size(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    # slow box: even 720p can't double 24 fps (RIFE 20 → 36, MVTools 15 → 27 live)
    bench_history.append(
        _result(_m("rife-v4.26", 20.0), _m("mvtools", 15.0, backend=BackendId.MVTOOLS))
    )
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    assert s.snapshot().bypass is BypassReason.NO_REALTIME
    s.forced = True
    s.filter, s.bypass = FilterState.PENDING, None
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN and snap.target_fps == 48
    assert snap.notice == decide.forced_smaller_notice(720)
    assert s.ipc.sizes() == [(1280, 720)]  # type: ignore[attr-defined]


async def test_session_forced_without_smaller_size_runs_at_its_own(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    s.forced = True
    _load(s, fps=120.0, w=1280, h=720)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN and snap.notice == decide.forced_notice()
    assert s.ipc.sizes() == [None]  # type: ignore[attr-defined]


async def test_decisions_and_health_go_to_the_log(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bench_history.append(_devbox())
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    with caplog.at_level("INFO", logger="buttereye.core.mpvctl.session"):
        await s.apply(raise_on_fail=False)
        await s.stalled(_stall())
        await _settle(ctx)
    text = caplog.text
    assert "smoothing 24 → 48 fps with RIFE (Vulkan) rife-v4.26" in text
    assert "at 1280 × 720 (video 3840 × 2160)" in text
    assert "stalled (BE-" in text and "switched this video to CPU smoothing" in text
    assert "playing. → ButterEye switched" in text
    assert "with MVTools (CPU) at 1920 × 1080" in text  # largest size MVTools keeps up


# ---------------------------------------------------------------------------
# 3. stall / GPU fault → MVTools once
# ---------------------------------------------------------------------------


def _changes(ctx: FakeCtx) -> list[HealthChanged]:
    return [e for e in ctx.events if isinstance(e, HealthChanged)]


async def test_rife_stall_switches_to_cpu_once(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.append(_devbox())
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    await s.stalled(_stall())
    await _settle(ctx)
    snap = s.snapshot()
    assert snap.backend is BackendId.MVTOOLS and snap.model is None
    assert snap.filter is not FilterState.ROLLED_BACK and s.active_gen is not None
    assert snap.health is Health.OK and snap.health_code is None
    assert snap.notice == decide.switched_to_cpu_notice()
    assert snap.notice.key == (
        "The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing."
    )
    assert s.ipc.added() == ["rife-ncnn", "mvtools"]  # type: ignore[attr-defined]
    changes = _changes(ctx)
    assert [c.health for c in changes] == [Health.STALLED, Health.OK]
    assert changes[0].auto_action is not None and "CPU smoothing" in changes[0].auto_action.key

    # turning smoothing on again or another profile apply never goes back to RIFE
    await s.set_off(FilterState.OFF)
    s.reset_health()
    s.want, s.filter = True, FilterState.PENDING
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVTOOLS
    # another file in the same player keeps CPU smoothing too
    await s.begin_new_file()
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVTOOLS and s.notice == decide.switched_to_cpu_notice()

    # MVTools stalls as well → off, never retried
    await s.stalled(_stall())
    await _settle(ctx)
    snap = s.snapshot()
    assert snap.filter is FilterState.ROLLED_BACK and not s.want
    assert snap.health is Health.STALLED and snap.health_code is ErrorCode.FILTER_STALLED
    assert snap.notice is not None and "interpolation was turned off" in snap.notice.key
    assert s.ipc.added() == ["rife-ncnn", "mvtools", "mvtools", "mvtools"]  # type: ignore[attr-defined]


async def test_mvtools_stall_turns_off(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    ctx = FakeCtx(_paths(tmp_path), _config(_simple(BackendId.MVTOOLS)))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    await s.stalled(_stall())
    assert s.filter is FilterState.ROLLED_BACK and s.health is Health.STALLED
    assert s.ipc.added() == ["mvtools"]  # type: ignore[attr-defined]


async def test_rife_stall_without_mvtools_turns_off(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    ctx = FakeCtx(_paths(tmp_path, mvtools=False), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN  # the only engine: used unmeasured
    await s.stalled(_stall())
    await _settle(ctx)
    assert s.filter is FilterState.ROLLED_BACK and s.health is Health.STALLED


async def test_rife_switch_when_mvtools_keeps_up_only_smaller(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.append(
        _result(_m("rife-v4.26", 61.2), _m("mvtools", 40.0, backend=BackendId.MVTOOLS))
    )
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    await s.stalled(_stall())
    await _settle(ctx)
    snap = s.snapshot()
    # MVTools can't double 24 fps at 1080p (32 live) but can at 720p (72)
    assert snap.backend is BackendId.MVTOOLS and snap.target_fps == Fraction(48000, 1001)
    assert s.ipc.sizes()[-1] == (1280, 720)  # type: ignore[attr-defined]
    assert snap.notice == decide.switched_to_cpu_notice()


async def test_rife_switch_when_nothing_keeps_up_bypasses(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.append(
        _result(_m("rife-v4.26", 61.2), _m("mvtools", 10.0, backend=BackendId.MVTOOLS))
    )
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    await s.stalled(_stall())
    await _settle(ctx)
    snap = s.snapshot()
    assert snap.filter is FilterState.BYPASSED and snap.bypass is BypassReason.NO_REALTIME
    assert snap.backend is None and snap.notice == decide.no_realtime_notice()


async def test_gpu_fault_switches_rife_to_cpu(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.append(_devbox())
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.RIFE_NCNN
    xid = "NVRM: Xid (PCI:0000:01:00): 109, pid=9, name=vo, CTX SWITCH TIMEOUT"
    no_xid["lines"] = (xid,)
    await s.check_gpu_faults()
    snap = s.snapshot()
    assert snap.backend is BackendId.MVTOOLS and s.active_gen is not None
    assert snap.health is Health.OK and snap.notice == decide.switched_to_cpu_notice()
    changes = _changes(ctx)
    assert changes[0].health is Health.GPU_FAULT and changes[0].evidence == (xid,)
    assert changes[-1].health is Health.OK
    # the same fault found again later (e.g. at the session end) leaves MVTools alone
    no_xid["lines"] = (xid, xid)
    await s.check_gpu_faults()
    assert _engine(s) is BackendId.MVTOOLS and s.active_gen is not None
    notices = [
        e for e in ctx.events if isinstance(e, Notice) and e.code is ErrorCode.RIFE_GPU_FAULT
    ]
    assert notices and "earlier" in notices[-1].message.key


async def test_device_lost_during_rife_switches_to_cpu(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.append(_devbox())
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    entry = {"label": "buttereye", "name": "vapoursynth", "enabled": True}
    s.props["vf"] = [entry]
    s.device_lost = True
    s.failure.append("vulkan: VK_ERROR_DEVICE_LOST")
    await s.step()
    await _settle(ctx)
    assert _engine(s) is BackendId.MVTOOLS and s.active_gen is not None
    assert _changes(ctx)[0].health is Health.DEVICE_LOST


# ---------------------------------------------------------------------------
# per-frame property changes do not wake the controller
# ---------------------------------------------------------------------------


def test_per_frame_properties_wait_for_the_tick(tmp_path: Path) -> None:
    s = _session(FakeCtx(_paths(tmp_path), _config(_simple())), tmp_path)
    s.wake.clear()
    for name, value in (("playback-time", 1.5), ("frame-drop-count", 3), ("audio-pts", 1.4)):
        s.on_event({"event": "property-change", "name": name, "data": value})
    assert not s.wake.is_set() and s.props["frame-drop-count"] == 3
    s.on_event({"event": "property-change", "name": "vf", "data": []})
    assert s.wake.is_set()
    s.wake.clear()
    s.on_event({"event": "client-message", "args": ["buttereye-request", "toggle"]})
    assert s.wake.is_set()


async def test_step_down_skips_lite_only_when_the_bench_says_so(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    cfg = sc.default_config()
    s = _session(FakeCtx(_paths(tmp_path), cfg), tmp_path)
    assert await s.lite_no_faster(cfg.profiles) is False  # no bench: full chain
    bench_history.append(_devbox())  # v4.26 61.2, lite 62.61 (+2 %)
    assert await s.lite_no_faster(cfg.profiles) is True
    bench_history.clear()
    bench_history.append(_result(
        _m("rife-v4.26", 40.0),
        _m("rife-v4.22-lite", 60.0, model="rife-v4.22_lite_ensembleFalse"),
    ))  # fmt: skip
    assert await s.lite_no_faster(cfg.profiles) is False  # lite clearly faster: keep it
