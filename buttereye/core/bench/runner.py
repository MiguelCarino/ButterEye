# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark provider (docs/design/GUI.md §11.5; SCOPE F14, §5.3, §5.6, §4.6).

For every installed candidate (each packaged RIFE-ncnn model at ``gpu_thread=4``,
SCOPE §4.4 / M0(f), and MVTools) at the requested size:

1. vspipe on a generated moving pattern: 1 warm-up + 3 timed runs (F14 a);
2. mpv on ``av://lavfi:testsrc2`` (nv12) through the same script, 3 timed runs (F14 b);
3. new NVIDIA Xid lines in the kernel log during the candidate (``faults``).

``repeatable`` = coefficient of variation < 0.05 over the 3 runs (the worse of the
vspipe and mpv series). ``realtime`` = in-mpv fps >= 1.15 x target (F2 headroom);
when mpv cannot be measured the vspipe value is shown as ``mpv_fps`` with a
``Notice``, and ``realtime`` stays False because nothing was verified inside mpv.
A configuration with GPU faults is never recommended. A model that faulted the GPU
in ``REPEAT_FAULT_RUNS`` or more of the last ``FAULT_LOOKBACK`` stored benchmarks on
the same GPU is not recommended either, even when the current run was clean (one
isolated fault does not drop a model; see M0(f): rife-v4.25-lite was dropped after two Xid 109).
Every Xid is counted whatever its number (13, 109, ...), only for the processes of
the candidate and only after its start mark. Only generated clips are used;
nothing is downloaded and no media file is read.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import re
import shutil
import statistics
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buttereye.core import capabilities
from buttereye.core.bench import faults, measure, store
from buttereye.core.capabilities import ProviderRef
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.events import Notice
from buttereye.core.ops import OpContext
from buttereye.core.scriptgen.generator import (
    RIFE_CONCURRENT_FRAMES_DEFAULT,
    RIFE_GPU_THREAD_DEFAULT,
)
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    Config,
    Msg,
    Paths,
    Profile,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)

if TYPE_CHECKING:
    from buttereye.core.api import ProviderContext

_log = logging.getLogger(__name__)
_CHOOSE_DEVICE = ProviderRef("buttereye.core.hw.detect", "choose_device")

VPY = Path(__file__).with_name("bench.vpy")
MODEL_SUBDIR = "rife-ncnn-models"
RIFE_PLUGIN = "librife.so"
MVTOOLS_PLUGIN = "mvtools.so"

# §4.4 / M0(f), RTX 4090, 1080p 2x: plugin gpu_thread 2 gives ~52 fps and 4 gives ~72
# fps in vspipe. Inside mpv, concurrent-frames 4 reaches ~46 fps and 8 ~56-58 fps
# (owner decision 2026-10-07: 8). The two are set independently; see scriptgen.
RIFE_GPU_THREAD = RIFE_GPU_THREAD_DEFAULT
RIFE_CONCURRENT_FRAMES = RIFE_CONCURRENT_FRAMES_DEFAULT
BUFFERED_FRAMES = 4  # §4.4
WARMUP_RUNS = 1
TIMED_RUNS = 3
REPEATABLE_COV = 0.05
REALTIME_HEADROOM = 1.15
REPEAT_FAULT_RUNS = 2  # earlier faulting benchmarks (same GPU) that keep a model out
FAULT_LOOKBACK = 10  # how many of the newest stored results are looked at
FULL_SIZES: tuple[tuple[int, int], ...] = ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))

BENCH_PROFILE_ID = "benchmark"
#: The benchmark rule's ``fps_max`` is the measured rate + 1 % (exact Fraction), so
#: files tagged 24.0031 or 23.98 still match a 24000/1001 benchmark (GUI.md §11.9);
#: the live cap, not this bound, decides whether a faster file can be smoothed.
RULE_FPS_TOLERANCE = Fraction(101, 100)
STATE_KEY = "bench.runner"

INSTALL_PLUGINS = (
    "sudo dnf install buttereye-vs-rife-ncnn buttereye-rife-ncnn-models buttereye-vs-mvtools"
)
INSTALL_VSPIPE = "sudo dnf install vapoursynth-tools"


# ---------------------------------------------------------------- data
@dataclass(frozen=True, slots=True)
class Candidate:
    label: str
    backend: BackendId
    model: str | None  # RIFE model directory name, as in Profile.model
    model_path: Path | None
    concurrent_frames: int


@dataclass(frozen=True, slots=True)
class Tools:
    vspipe: str
    mpv: str | None
    vpy: Path
    plugin_dir: Path
    model_dir: Path
    env: Mapping[str, str] | None = None
    vram: bool = True


@dataclass(slots=True)
class _State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _state(ctx: ProviderContext) -> _State:
    return ctx.state(STATE_KEY, _State)


def _fail(
    cause: Msg, fix: Msg | None = None, *, detail: str | None = None, commands: tuple[str, ...] = ()
) -> ButterEyeError:
    return ButterEyeError(ErrorCode.BENCH_FAILED, cause, fix, detail=detail, commands=commands)


# ---------------------------------------------------------------- candidates (pure)
_ENSEMBLE = re.compile(r"_ensemble(True|False)$")
_VERSION = re.compile(r"v(\d+)\.(\d+)")


def model_label(dirname: str) -> str:
    """``rife-v4.22_lite_ensembleFalse`` -> ``rife-v4.22-lite``."""
    return _ENSEMBLE.sub("", dirname).replace("_", "-")


def _quality_key(c: Candidate) -> tuple[int, int, int, int]:
    """Sort key, best first: RIFE before MVTools; full models before lite; newer first."""
    if c.backend is not BackendId.RIFE_NCNN or c.model is None:
        return (1, 0, 0, 0)
    lite = 1 if "lite" in c.model.lower() else 0
    v = _VERSION.search(c.model)
    major, minor = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    return (0, lite, -major, -minor)


def discover_candidates(
    plugin_dir: Path, model_dir: Path, *, rife: bool = True, concurrency: int | None = None
) -> tuple[Candidate, ...]:
    """Installed configurations, best quality first. RIFE models are the packaged
    model directories (each holds ``*.param``/``*.bin``)."""
    out: list[Candidate] = []
    if rife and (plugin_dir / RIFE_PLUGIN).is_file() and model_dir.is_dir():
        for d in sorted(model_dir.iterdir()):
            if d.is_dir() and any(d.glob("*.param")):
                out.append(
                    Candidate(
                        model_label(d.name), BackendId.RIFE_NCNN, d.name, d, RIFE_CONCURRENT_FRAMES
                    )
                )
    if (plugin_dir / MVTOOLS_PLUGIN).is_file():
        cpus = concurrency if concurrency is not None else (os.cpu_count() or 1)
        out.append(Candidate("mvtools", BackendId.MVTOOLS, None, None, max(1, min(cpus, 8))))
    out.sort(key=_quality_key)
    return tuple(out)


def target_fps(req: BenchRequest) -> Fraction:
    return req.target_fps if req.target_fps is not None else req.source_fps * 2


def output_frames(height: int) -> int:
    """Frames timed per pass (a few seconds each at real-time speed)."""
    if height <= 1080:
        return 192
    if height <= 1440:
        return 144
    return 96


def sizes_for(req: BenchRequest) -> tuple[tuple[int, int], ...]:
    first = (req.width, req.height)
    if not req.full:
        return (first,)
    return (first,) + tuple(s for s in FULL_SIZES if s != first)


def size_label(label: str, size: tuple[int, int], req: BenchRequest) -> str:
    return label if size == (req.width, req.height) else f"{label} @ {size[1]}p"


def user_data(
    c: Candidate,
    tools: Tools,
    *,
    width: int,
    height: int,
    source: Fraction,
    target: Fraction,
    src_frames: int,
    gpu_id: int | None,
) -> dict[str, Any]:
    return {
        "backend": "rife" if c.backend is BackendId.RIFE_NCNN else "mvtools",
        "plugin_dir": os.fspath(tools.plugin_dir),
        "model_path": os.fspath(c.model_path) if c.model_path is not None else None,
        "gpu_thread": RIFE_GPU_THREAD,
        "gpu_id": gpu_id,
        "uhd": width > 2560 or height > 1440,  # §5.3: RIFE-ncnn at 4K uses uhd
        "matrix": "709" if height >= 720 else "170m",
        "src_num": source.numerator,
        "src_den": source.denominator,
        "out_num": target.numerator,
        "out_den": target.denominator,
        "width": width,
        "height": height,
        "frames": src_frames,
    }


def recommend(
    measurements: tuple[BenchMeasurement, ...],
    requested: frozenset[str] | None = None,
    avoid: frozenset[str] = frozenset(),
) -> str | None:
    """Best configuration that holds real time in mpv, never one with GPU faults.

    ``measurements`` are in quality order; repeatable ones are preferred.
    ``requested`` limits the choice to labels at the requested size; labels in
    ``avoid`` (models that keep faulting, :func:`repeat_faulters`) are skipped.
    """
    ok = [
        m
        for m in measurements
        if (requested is None or m.label in requested)
        and m.label not in avoid
        and m.realtime
        and m.vspipe_fps > 0
        and not (m.gpu_faults is not None and m.gpu_faults > 0)
    ]
    for m in ok:
        if m.repeatable:
            return m.label
    return ok[0].label if ok else None


def _faulted(m: BenchMeasurement) -> bool:
    return m.gpu_faults is not None and m.gpu_faults > 0


def repeat_faulters(
    past: tuple[BenchResult, ...], gpu_uuid: str | None
) -> dict[tuple[BackendId, str | None], int]:
    """Models that faulted the GPU in >= ``REPEAT_FAULT_RUNS`` of the newest
    ``FAULT_LOOKBACK`` stored results on ``gpu_uuid`` -> how many. Pure.

    A result counts once per model, whatever its sizes or Xid count. Results from
    another GPU are ignored (a different card says nothing about this one).
    """
    same = [r for r in past if r.gpu_uuid == gpu_uuid][-FAULT_LOOKBACK:]
    counts: dict[tuple[BackendId, str | None], int] = {}
    for r in same:
        for key in {(m.backend, m.model) for m in r.measurements if _faulted(m)}:
            counts[key] = counts.get(key, 0) + 1
    return {k: n for k, n in counts.items() if n >= REPEAT_FAULT_RUNS}


# ---------------------------------------------------------------- environment
def find_tools(paths: Paths) -> Tools:
    vspipe = shutil.which("vspipe")
    if vspipe is None:
        raise _fail(
            Msg("vspipe is not installed, so the benchmark cannot run."),
            Msg("Install VapourSynth's tools, then run the benchmark again."),
            commands=(INSTALL_VSPIPE,),
        )
    return Tools(
        vspipe=vspipe,
        mpv=shutil.which("mpv"),
        vpy=VPY,
        plugin_dir=paths.rpm_plugin_dir,
        model_dir=paths.rpm_data_dir / MODEL_SUBDIR,
    )


def _gpu(ctx: ProviderContext) -> tuple[bool, int | None, str | None]:
    """(RIFE possible, Vulkan index for gpu_id, GPU UUID) from the last doctor report."""
    try:
        wanted = ctx.config().general.gpu
    except ButterEyeError:
        wanted = None
    report = ctx.last_report()
    if report is None:
        return True, None, wanted
    hw = report.hardware
    gpus = [d for d in hw.vulkan if d.device_type != "cpu"]
    # the device doctor and live play pick: an index or a UUID in any case
    choose = capabilities.resolve(_CHOOSE_DEVICE)
    uuid: str | None = None
    if choose is not None and gpus:
        try:
            chosen = choose(hw.vulkan, wanted)
        except Exception:
            _log.exception("choose_device failed")
        else:
            uuid = chosen if isinstance(chosen, str) else None
    if uuid is None:
        uuid = (wanted.strip().lower() if wanted else None) or hw.interpolation_device
    index = next((d.index for d in gpus if d.uuid == uuid), None)
    return bool(gpus) or not hw.vulkan, index, uuid


# ---------------------------------------------------------------- measuring one candidate
_STEPS_PER_CANDIDATE = WARMUP_RUNS + 2 * TIMED_RUNS


@dataclass(slots=True)
class _Progress:
    op: OpContext
    total: int
    done: int = 0

    def step(self, phase: Msg) -> None:
        self.op.report(phase, done=self.done, total=self.total, unit="steps")
        self.done += 1


async def _measure(
    ctx: ProviderContext,
    op: OpContext,
    c: Candidate,
    label: str,
    tools: Tools,
    req: BenchRequest,
    size: tuple[int, int],
    gpu_id: int | None,
    prog: _Progress,
) -> tuple[BenchMeasurement, measure.MeasureFailed | None]:
    width, height = size
    source, target = req.source_fps, target_fps(req)
    out_frames = output_frames(height)
    src_frames = math.ceil(out_frames * source / target) + 2
    data = user_data(
        c,
        tools,
        width=width,
        height=height,
        source=source,
        target=target,
        src_frames=src_frames,
        gpu_id=gpu_id,
    )
    timeout = 30.0 + out_frames / 2.0  # >= 2 fps or it is not worth waiting for
    vs_argv = measure.vspipe_argv(tools.vspipe, tools.vpy, data, out_frames)

    if c.backend is BackendId.RIFE_NCNN:
        faults.mark_rife_session(ctx.paths)  # doctor's BE-1030 window starts here
    before = faults.snapshot()
    pids: set[int] = set()
    vram = measure.VramSampler.detect() if tools.vram else None
    vs_runs: list[measure.VspipeRun] = []
    mpv_runs: list[measure.MpvRun] = []
    failure: measure.MeasureFailed | None = None
    mpv_failure: measure.MeasureFailed | None = None
    try:
        for _ in range(WARMUP_RUNS):
            prog.step(Msg("{config} — warm-up — vspipe", {"config": label}))
            await measure.run_vspipe(op, vs_argv, timeout_s=timeout, env=tools.env, pids=pids)
        for n in range(1, TIMED_RUNS + 1):
            prog.step(
                Msg(
                    "{config} — run {n} of {total} — vspipe",
                    {"config": label, "n": n, "total": TIMED_RUNS},
                )
            )
            vs_runs.append(
                await measure.run_vspipe(
                    op, vs_argv, timeout_s=timeout, env=tools.env, vram=vram, pids=pids
                )
            )
    except measure.MeasureFailed as exc:
        failure = exc
    if failure is None and tools.mpv is not None:
        mpv_argv = measure.mpv_argv(
            tools.mpv,
            tools.vpy,
            data,
            width=width,
            height=height,
            source_fps=source,
            frames=out_frames,
            concurrent_frames=c.concurrent_frames,
            buffered_frames=BUFFERED_FRAMES,
        )
        try:
            for n in range(1, TIMED_RUNS + 1):
                prog.step(
                    Msg(
                        "{config} — run {n} of {total} — in mpv",
                        {"config": label, "n": n, "total": TIMED_RUNS},
                    )
                )
                mpv_runs.append(
                    await measure.run_mpv(
                        op, mpv_argv, frames=out_frames, timeout_s=timeout, env=tools.env, pids=pids
                    )
                )
        except measure.MeasureFailed as exc:
            mpv_failure = exc
            mpv_runs.clear()
    elif failure is None:
        mpv_failure = measure.MeasureFailed("mpv is not installed")
    gpu_faults = await faults.count_new(before, pids)

    if failure is not None:
        _log.warning("benchmark %s failed: %s\n%s", label, failure.reason, failure.detail)
        ctx.emit(
            Notice(
                Msg(
                    "{config} could not be measured: {reason}",
                    {"config": label, "reason": failure.reason},
                ),
                ErrorCode.BENCH_FAILED,
            )
        )
        return (
            BenchMeasurement(
                label,
                c.backend,
                c.model,
                0.0,
                0.0,
                math.inf,
                False,
                0.0,
                0.0,
                vram.peak if vram else None,
                False,
                gpu_faults,
            ),
            failure,
        )

    vs_fps = [r.fps for r in vs_runs]
    vspipe_fps = statistics.median(vs_fps)
    evals = [r.eval_s if r.eval_s is not None else max(0.0, r.wall_s - r.output_s) for r in vs_runs]
    reload_s = statistics.median(evals)
    spread = measure.cov(vs_fps)
    if mpv_runs:
        mp_fps = [r.fps for r in mpv_runs]
        mpv_fps = statistics.median(mp_fps)
        spread = max(spread, measure.cov(mp_fps))
        startup_s = statistics.median(r.startup_s for r in mpv_runs)
        realtime = mpv_fps >= REALTIME_HEADROOM * float(target)
    else:
        assert mpv_failure is not None
        _log.warning(
            "in-mpv benchmark of %s failed: %s\n%s", label, mpv_failure.reason, mpv_failure.detail
        )
        ctx.emit(
            Notice(
                Msg(
                    "In-mpv speed of {config} could not be measured ({reason}); the vspipe "
                    "speed is shown instead and real time is not confirmed.",
                    {"config": label, "reason": mpv_failure.reason},
                ),
                ErrorCode.BENCH_FAILED,
            )
        )
        mpv_fps = vspipe_fps
        startup_s = reload_s
        realtime = False
    return (
        BenchMeasurement(
            label=label,
            backend=c.backend,
            model=c.model,
            vspipe_fps=round(vspipe_fps, 2),
            mpv_fps=round(mpv_fps, 2),
            cov=round(spread, 4) if math.isfinite(spread) else spread,
            repeatable=spread < REPEATABLE_COV,
            startup_s=round(startup_s, 3),
            reload_s=round(reload_s, 3),
            vram_bytes=vram.peak if vram else None,
            realtime=realtime,
            gpu_faults=gpu_faults,
        ),
        None,
    )


# ---------------------------------------------------------------- provider API (§11.5)
async def bench(ctx: ProviderContext, req: BenchRequest, *, op: OpContext) -> BenchResult:
    if req.width <= 0 or req.height <= 0 or req.width % 2 or req.height % 2:
        raise _fail(
            Msg(
                "The benchmark size must be positive and even (got {w}×{h}).",
                {"w": req.width, "h": req.height},
            )
        )
    if req.source_fps <= 0 or (req.target_fps is not None and req.target_fps <= req.source_fps):
        raise _fail(Msg("The target frame rate must be higher than the source frame rate."))
    if req.user_file is not None:
        raise _fail(
            Msg("Benchmarking your own file isn't in this build yet."),
            Msg("Run the benchmark without a file; it uses generated test clips."),
        )
    state = _state(ctx)
    if state.lock.locked():
        raise _fail(Msg("A benchmark is already running."))
    async with state.lock:
        return await _bench_locked(ctx, req, op)


async def _bench_locked(ctx: ProviderContext, req: BenchRequest, op: OpContext) -> BenchResult:
    tools = find_tools(ctx.paths)
    rife_ok, gpu_id, gpu_uuid = _gpu(ctx)
    cands = discover_candidates(tools.plugin_dir, tools.model_dir, rife=rife_ok)
    if not cands:
        raise _fail(
            Msg("No interpolation plugins are installed, so there is nothing to measure."),
            Msg("Install ButterEye's plugin packages, then run the benchmark again."),
            commands=(INSTALL_PLUGINS,),
        )
    try:
        past = store.read(ctx.paths.bench_file)
    except (store.HistoryCorrupt, OSError) as exc:
        _log.warning("benchmark history %s unreadable: %s", ctx.paths.bench_file, exc)
        past = ()
    faulters = repeat_faulters(past, gpu_uuid)
    sizes = sizes_for(req)
    prog = _Progress(op, total=len(sizes) * len(cands) * _STEPS_PER_CANDIDATE)
    results: list[BenchMeasurement] = []
    failures: list[str] = []
    index = 0
    for size in sizes:
        for c in cands:
            prog.done = index * _STEPS_PER_CANDIDATE  # skipped passes still count
            index += 1
            label = size_label(c.label, size, req)
            m, failed = await _measure(ctx, op, c, label, tools, req, size, gpu_id, prog)
            results.append(m)
            if failed is not None:
                failures.append(f"{label}: {failed.reason}\n{failed.detail}")
    if len(failures) == len(results):
        raise _fail(
            Msg("No configuration could be measured."),
            Msg("Open System and press F5 to check the plugins and the GPU."),
            detail="\n\n".join(failures),
        )
    op.report(Msg("Benchmark finished"), done=prog.total, total=prog.total, unit="steps")
    requested = frozenset(c.label for c in cands)
    measurements = tuple(results)
    avoid: set[str] = set()
    for m in measurements:
        n = faulters.get((m.backend, m.model))
        if n is None or m.label not in requested:
            continue
        avoid.add(m.label)
        if not _faulted(m):
            ctx.emit(
                Notice(
                    Msg(
                        "{config} faulted the GPU in {n} earlier benchmarks, so it isn't "
                        "recommended even though this run was clean.",
                        {"config": m.label, "n": n},
                    ),
                    ErrorCode.RIFE_GPU_FAULT,
                )
            )
    result = BenchResult(
        request=req,
        measurements=measurements,
        recommended=recommend(measurements, requested, frozenset(avoid)),
        when=datetime.now(UTC),
        gpu_uuid=gpu_uuid,
    )
    try:
        store.append(ctx.paths.bench_file, result)
    except OSError as exc:
        _log.warning("could not save benchmark history to %s: %s", ctx.paths.bench_file, exc)
        ctx.emit(
            Notice(
                Msg("The benchmark result could not be saved: {error}", {"error": str(exc)}),
                ErrorCode.BENCH_FAILED,
            )
        )
    return result


async def history(ctx: ProviderContext) -> tuple[BenchResult, ...]:
    """Stored results, oldest first (missing or unreadable file -> ``()``)."""
    try:
        return store.read(ctx.paths.bench_file)
    except (store.HistoryCorrupt, OSError) as exc:
        _log.warning("benchmark history %s unreadable: %s", ctx.paths.bench_file, exc)
        return ()


def bench_config(cfg: Config, result: BenchResult, label: str) -> Config:
    """``cfg`` with a user profile ``benchmark`` for ``label`` and a first rule sending
    sources no larger and no faster than the measured one to it. Pure."""
    m = next((x for x in result.measurements if x.label == label), None)
    if m is None:
        raise _fail(Msg("There is no measured configuration called {label}.", {"label": label}))
    if m.vspipe_fps <= 0:
        raise _fail(Msg("{label} could not be measured, so it can't be used.", {"label": label}))
    if m.gpu_faults is not None and m.gpu_faults > 0:
        raise _fail(
            Msg(
                "{label} caused GPU faults during the benchmark, so it won't be used.",
                {"label": label},
            ),
            Msg("Choose another configuration, or MVTools."),
        )
    if " @ " in label:
        raise _fail(
            Msg(
                "{label} was measured at another size; use a configuration measured at {w}×{h}.",
                {"label": label, "w": result.request.width, "h": result.request.height},
            )
        )
    req = result.request
    old = next((p for p in cfg.profiles if p.id == BENCH_PROFILE_ID), None)
    target = (
        Target(TargetKind.FPS, req.target_fps)
        if req.target_fps is not None
        else Target(TargetKind.X2)
    )
    profile = Profile(
        id=BENCH_PROFILE_ID,
        name=f"Benchmark: {label} ({req.width}×{req.height})",
        backend=m.backend,
        model=m.model,
        scale=None,
        target=target,
        sc_threshold=old.sc_threshold if old is not None else _sc_threshold(cfg),
        buffered_frames=BUFFERED_FRAMES,
        concurrent_frames=RIFE_CONCURRENT_FRAMES if m.backend is BackendId.RIFE_NCNN else None,
        hdr=old.hdr if old is not None else "skip",
        builtin=False,
    )
    profiles = tuple(p for p in cfg.profiles if p.id != BENCH_PROFILE_ID) + (profile,)
    rule = Rule(
        RuleMatch(
            fps_max=req.source_fps * RULE_FPS_TOLERANCE,
            width_max=req.width,
            height_max=req.height,
        ),
        BENCH_PROFILE_ID,
    )
    rules = (rule,) + tuple(r for r in cfg.rules if r.profile != BENCH_PROFILE_ID)
    return dataclasses.replace(cfg, profiles=profiles, rules=rules)


def _sc_threshold(cfg: Config) -> float:
    for p in cfg.profiles:
        if p.builtin:
            return p.sc_threshold
    return 0.12


async def apply(
    ctx: ProviderContext, result: BenchResult, label: str, *, expected_revision: str
) -> str:
    """Save ``label`` as the ``benchmark`` profile (+ first rule) via the facade."""
    new = bench_config(ctx.config(), result, label)
    return await ctx.save_config(new, expected_revision=expected_revision)


__all__ = [
    "bench",
    "history",
    "apply",
    "bench_config",
    "recommend",
    "repeat_faulters",
    "discover_candidates",
    "model_label",
    "Candidate",
    "Tools",
    "find_tools",
    "target_fps",
    "output_frames",
    "sizes_for",
    "BENCH_PROFILE_ID",
    "RULE_FPS_TOLERANCE",
]
