# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark provider (docs/design/GUI.md §11.5; SCOPE F14, §5.3, §5.6, §4.6).

For every installed candidate (each packaged RIFE-ncnn model at ``gpu_thread=4``,
SCOPE §4.4 / M0(f), mvgpu and MVTools) at the requested size:

1. vspipe on a generated moving pattern: 1 warm-up + 3 timed runs (F14 a);
2. mpv on ``av://lavfi:testsrc2`` (nv12) through the same script and the same
   pattern, 3 timed runs (F14 b); a failed pass is tried once more;
3. new NVIDIA Xid lines in the kernel log during the candidate (``faults``).

A candidate whose warm-up is under half the real-time bar gets one short pass
of each instead (P4). The live engines (mvgpu, MVTools) are measured first,
while the machine is cool, and also at the bigger ``FULL_SIZES``; the best
configuration live playback would use is then held for ``SUSTAINED_S`` and keeps
the lower of its burst and sustained rates (B7, ``plan``, ``_sustain``).

The bars are ``verdict``'s. ``realtime`` = in-mpv fps >= 1.15 x target (F2
headroom); ``repeatable`` = every in-mpv run gave the same verdict (the spread,
``cov``, is shown only); the recommendation needs the live bar (``verdict.live_ok``,
1.25 x target), the one playback divides by. When mpv cannot be measured the
vspipe value is shown as ``mpv_fps`` with a ``Notice``, and ``realtime`` stays
False because nothing was verified inside mpv.
Each stored result records what measured it and under what (``store.Meta``):
``history`` leaves out results from other software and runs that shared the
machine with playback or a conversion (B8, B9).
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
import hashlib
import json
import logging
import math
import os
import re
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buttereye import __version__ as _APP_VERSION
from buttereye.core import capabilities, osapi
from buttereye.core import paths as _paths
from buttereye.core.backends import trt
from buttereye.core.bench import faults, measure, store, verdict
from buttereye.core.capabilities import ProviderRef
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.events import Notice
from buttereye.core.i18n import render as render_msg
from buttereye.core.ops import OpContext
from buttereye.core.scriptgen.generator import (
    MVGPU_CONCURRENT_FRAMES_DEFAULT,
    MVTOOLS_MAX_CONCURRENT,
    RIFE_CONCURRENT_FRAMES_DEFAULT,
    RIFE_GPU_THREAD_DEFAULT,
    FilterParams,
    ScriptConstants,
    rife_gpu_thread,
    write_script,
)
from buttereye.core.scriptgen.generator import user_data as generator_user_data
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    Config,
    ContentKind,
    Msg,
    OpState,
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
RIFE_PLUGIN = osapi.plugin_file("librife.so")
MVTOOLS_PLUGIN = osapi.plugin_file("mvtools.so")
MVGPU_PLUGIN = osapi.plugin_file("libmvgpu.so")

# §4.4 / M0(f), RTX 4090, 1080p 2x: plugin gpu_thread 2 gives ~52 fps and 4 gives ~72
# fps in vspipe. Inside mpv, concurrent-frames 4 reaches ~46 fps and 8 ~56-58 fps
# (owner decision 2026-10-07: 8). The two are set independently; see scriptgen.
# (1 on macOS, read per run: scriptgen.rife_gpu_thread)
RIFE_GPU_THREAD = RIFE_GPU_THREAD_DEFAULT
RIFE_CONCURRENT_FRAMES = RIFE_CONCURRENT_FRAMES_DEFAULT
BUFFERED_FRAMES = 4  # §4.4
WARMUP_RUNS = 1
TIMED_RUNS = 3
#: the bars live in ``verdict`` (one definition for bench, selection and playback)
REALTIME_HEADROOM = verdict.REALTIME_HEADROOM
LIVE_HEADROOM = verdict.LIVE_HEADROOM
#: P4: frames of the single vspipe and mpv pass a hopeless candidate gets (its
#: warm-up under half the real-time bar)
HOPELESS_FRAMES = 96
#: B10: a failed in-mpv pass is tried once more before the candidate counts as
#: not measured in mpv
MPV_RETRIES = 1
#: B7: the sustained phase for the best live-ok configuration at the requested
#: size: back-to-back in-mpv passes for this long (0 = off), at most this many,
#: shown as this many progress steps; if it falls below the live bar, the next
#: one is held too, up to ``SUSTAINED_TRIES`` in all
SUSTAINED_S = 150.0
SUSTAINED_MAX_PASSES = 120
SUSTAINED_STEPS = 6
SUSTAINED_TRIES = 2
#: measure the live engines at the bigger ``FULL_SIZES`` too (``plan``)
AUTO_LIVE_SIZES = True
#: how long ``history`` trusts the versions it read (stat calls, cheap but not free)
VERSIONS_TTL_S = 30.0
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
#: Windows: the installer brings the plugins and VapourSynth (vspipe) itself
REPAIR = Msg("Run the ButterEye installer again and choose Repair.")


#: ButterEye.app: the plugins and VapourSynth came inside the app
APP_REPAIR = Msg("Download ButterEye again and replace ButterEye.app with the new copy.")
APPIMAGE_REPAIR = Msg("Download the ButterEye AppImage again and replace the old file with it.")


def _install_fix(fix: Msg, command: str) -> tuple[Msg, tuple[str, ...]]:
    """(fix, commands): the dnf line on Linux; on Windows the installer's Repair;
    in ButterEye.app a fresh copy of the app, and in the AppImage a fresh copy
    of that."""
    if osapi.IS_WINDOWS:
        return REPAIR, ()
    if osapi.MACOS_APP is not None:
        return APP_REPAIR, ()
    if osapi.LINUX_APP is not None:
        return APPIMAGE_REPAIR, ()
    return fix, (command,)


# ---------------------------------------------------------------- data
@dataclass(frozen=True, slots=True)
class Candidate:
    label: str
    backend: BackendId
    model: str | None  # RIFE model directory name, as in Profile.model
    model_path: Path | None
    concurrent_frames: int
    trt_install: trt.TrtInstall | None = None  # RIFE_TRT only


@dataclass(frozen=True, slots=True)
class Tools:
    vspipe: str
    mpv: str | None
    vpy: Path
    #: one plugin directory, or the search path (``paths.plugin_dirs``): macOS is
    #: the platform with two at once, and ``candidate_plugin_dir`` resolves them
    #: back to the single directory bench.vpy can name
    plugin_dirs: Path | tuple[Path, ...]
    model_dir: Path
    env: Mapping[str, str] | None = None
    vram: bool = True


@dataclass(slots=True)
class _State:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: (monotonic time read, ``current_versions``)
    versions: tuple[float, dict[str, str | None]] | None = None


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
    """Sort key, best first: RIFE, then mvgpu, then MVTools; full models before
    lite; newer first."""
    if c.backend is BackendId.MVGPU:
        return (1, 0, 0, 0)
    if c.backend is BackendId.MVTOOLS or c.model is None:
        return (2, 0, 0, 0)
    lite = 1 if "lite" in c.model.lower() else 0
    v = _VERSION.search(c.model)
    major, minor = (int(v.group(1)), int(v.group(2))) if v else (0, 0)
    return (-1 if c.backend is BackendId.RIFE_TRT else 0, lite, -major, -minor)


def discover_candidates(
    plugin_dirs: Path | Sequence[Path],
    model_dir: Path,
    *,
    rife: bool = True,
    concurrency: int | None = None,
) -> tuple[Candidate, ...]:
    """Installed configurations, best quality first. RIFE models are the packaged
    model directories (each holds ``*.param``/``*.bin``); mvgpu needs a GPU like
    RIFE (``rife`` False: no usable GPU).

    ``plugin_dirs`` is one directory or the whole search path, as
    ``decide.installed_backends`` takes it: the benchmark must offer exactly the
    engines live play would choose from, and on a Mac with the tap installed
    MVTools is in a different directory from mvgpu and RIFE."""
    out: list[Candidate] = []
    if rife and _paths.plugin_in(plugin_dirs, RIFE_PLUGIN) is not None and model_dir.is_dir():
        for d in sorted(model_dir.iterdir()):
            if d.is_dir() and any(d.glob("*.param")):
                out.append(
                    Candidate(
                        model_label(d.name), BackendId.RIFE_NCNN, d.name, d, RIFE_CONCURRENT_FRAMES
                    )
                )
    if rife and _paths.plugin_in(plugin_dirs, MVGPU_PLUGIN) is not None:
        out.append(Candidate("mvgpu", BackendId.MVGPU, None, None, MVGPU_CONCURRENT_FRAMES_DEFAULT))
    if _paths.plugin_in(plugin_dirs, MVTOOLS_PLUGIN) is not None:
        cpus = concurrency if concurrency is not None else (os.cpu_count() or 1)
        conc = max(1, min(cpus, MVTOOLS_MAX_CONCURRENT))  # as live play uses it
        out.append(Candidate("mvtools", BackendId.MVTOOLS, None, None, conc))
    out.sort(key=_quality_key)
    return tuple(out)


def trt_candidates(ctx: ProviderContext) -> tuple[Candidate, ...]:
    """TensorRT configurations, unless turned off, when vstrt is set up (§5.2)."""
    try:
        if ctx.config().general.trt_experimental is False:  # unset = on when set up
            return ()
    except ButterEyeError:
        return ()
    inst = trt.find_install(ctx.paths)
    if inst is None:
        return ()
    out = [
        Candidate(
            f"{model_label(name)} · TensorRT",
            BackendId.RIFE_TRT,
            name,
            None,
            RIFE_CONCURRENT_FRAMES,
            inst,
        )
        for name in trt.available_models(inst)
    ]
    return tuple(sorted(out, key=_quality_key))


async def _trt_prepare(
    ctx: ProviderContext,
    c: Candidate,
    gpu_uuid: str | None,
    *,
    width: int,
    height: int,
    source: Fraction,
    target: Fraction,
    src_frames: int,
) -> tuple[dict[str, Any], Path]:
    """Build (or reuse) the engine for this size; the template's user_data and path.

    TensorRT is timed through the playback template itself, so the speed test
    measures exactly what plays (a blank clip in vspipe, mpv's frames in mpv)."""
    inst = c.trt_install
    if inst is None or c.model is None:
        raise measure.MeasureFailed("TensorRT isn't set up")
    ident = await trt.nvidia_identity(gpu_uuid)
    if ident is None:
        raise measure.MeasureFailed("nvidia-smi didn't report the GPU")
    key = trt.EngineKey(
        ident[0], ident[1], inst.vstrt_version, inst.trt_version, c.model, width, height
    )
    script = ctx.paths.cache_dir / "bench-template.vpy"
    script.parent.mkdir(parents=True, exist_ok=True)
    # no plugin name to resolve: the TensorRT branch of the template loads vstrt
    # from the install's own directory and never reads PLUGIN_DIR
    write_script(script, ScriptConstants(_paths.plugin_dir_for(ctx.paths)))
    try:
        folder = await trt.build_engine(ctx.paths, inst, key, script=script)
    except ButterEyeError as exc:
        raise measure.MeasureFailed(render_msg(exc.cause), str(exc)) from None
    params = FilterParams(
        gen=0,
        backend=BackendId.RIFE_TRT,
        src_fps=source,
        target_fps=target,
        buffered_frames=BUFFERED_FRAMES,
        concurrent_frames=c.concurrent_frames,
        matrix="709" if height >= 720 else "170m",
        range="limited",
        # the blank clip is nothing but identical frames: either would turn the
        # timed run into copies and measure nothing
        hold_fill=False,
        still_skip=False,
        trt=trt.script_settings(
            inst, key, folder, streams=trt.streams_for(width, height), build=False
        ),
    )
    data = json.loads(generator_user_data(params))
    data["source"] = {
        "kind": "blank",
        "width": width,
        "height": height,
        "fps": [source.numerator, source.denominator],
        "length": src_frames,
    }
    return data, script


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


#: bench.vpy's name for each engine it measures (TensorRT runs the playback template)
_SCRIPT_BACKEND = {
    BackendId.RIFE_NCNN: "rife",
    BackendId.MVTOOLS: "mvtools",
    BackendId.MVGPU: "mvgpu",
}
#: the plugin file bench.vpy loads for each of those, as decide.BACKEND_PLUGIN
#: does for the playback template
_SCRIPT_PLUGIN = {
    BackendId.RIFE_NCNN: RIFE_PLUGIN,
    BackendId.MVTOOLS: MVTOOLS_PLUGIN,
    BackendId.MVGPU: MVGPU_PLUGIN,
}


def candidate_plugin_dir(tools: Tools, backend: BackendId) -> Path:
    """The one directory bench.vpy may name for ``backend``'s plugin.

    ``bench.vpy``'s ``load`` joins one ``plugin_dir`` and a file name, exactly as
    the playback template does, so the search path has to be resolved to the
    directory that really holds this engine's plugin - otherwise the benchmark
    offers a candidate it then cannot load, which on a Mac with the tap installed
    is MVTools (Homebrew's autoload directory) measured against mvgpu and RIFE
    (ButterEye's own).
    """
    where = tools.plugin_dirs
    dirs = (where,) if isinstance(where, Path) else tuple(where)
    found = _paths.plugin_in(dirs, _SCRIPT_PLUGIN[backend])
    # nothing found: the first directory, so the script names the place the
    # plugin belongs and the load fails there (``paths.plugin_dir_for`` does the
    # same for the playback template). A candidate exists only because its file
    # was found, so this is reached only when the plugin went away since.
    return found.parent if found is not None else dirs[0]


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
        "backend": _SCRIPT_BACKEND[c.backend],
        "plugin_dir": os.fspath(candidate_plugin_dir(tools, c.backend)),
        "model_path": os.fspath(c.model_path) if c.model_path is not None else None,
        "gpu_thread": rife_gpu_thread(),
        "gpu_id": gpu_id,
        # §5.3: RIFE-ncnn's uhd flag at 4K; only v1-v3 models read it (v4.x ignore it)
        "uhd": width > 2560 or height > 1440,
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
    target: Fraction | None = None,
) -> str | None:
    """Best configuration that live playback would keep up with, never one with
    GPU faults.

    ``measurements`` are in quality order and the first one that qualifies wins:
    ``repeatable`` is shown, not used to reorder (a better engine that missed a
    spread test lost to a worse one that passed it). With ``target`` the bar is
    the live one (:func:`verdict.live_ok`, what playback divides by), so the
    speed test never recommends an engine playback then calls too slow; without
    it, the benchmark's own ``realtime``. ``requested`` limits the choice to
    labels at the requested size; labels in ``avoid`` (models that keep
    faulting, :func:`repeat_faulters`) are skipped.
    """
    for m in measurements:
        if requested is not None and m.label not in requested:
            continue
        if m.label in avoid or not m.realtime or m.vspipe_fps <= 0 or _faulted(m):
            continue
        if target is None or verdict.live_ok(m, target):
            return m.label
    return None


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
    vspipe = osapi.vspipe()
    if vspipe is None:
        fix, commands = _install_fix(
            Msg("Install VapourSynth's tools, then run the benchmark again."), INSTALL_VSPIPE
        )
        raise _fail(
            Msg("vspipe is not installed, so the benchmark cannot run."), fix, commands=commands
        )
    return Tools(
        vspipe=vspipe,
        mpv=osapi.executable("mpv"),
        vpy=VPY,
        plugin_dirs=_paths.plugin_dirs(paths),
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


# ---------------------------------------------------------------- versions (B9)
def _bundle_root() -> str | None:
    """The real path of ButterEye.app's ``Contents`` or the AppImage's root, when
    this runs from one; None elsewhere."""
    if osapi.MACOS_APP is not None:
        return os.path.realpath(osapi.MACOS_APP.parent)
    if osapi.LINUX_APP is not None:
        return os.path.realpath(osapi.LINUX_APP)
    return None


def _shown(real: str) -> str:
    """``real``, or ``<bundle>/…`` for a file inside the running app or AppImage.
    Both move on every launch: a translocated ButterEye.app runs from a new
    ``AppTranslocation/<uuid>`` folder, an AppImage mounts at a new
    ``/tmp/.mount_XXXXXX``. With the path in the key, every launch would find no
    current result and run the whole speed test again. The files' contents
    travel with the bundle, so they still tell one build from another."""
    root = _bundle_root()
    if root is not None and (real == root or real.startswith(root + os.sep)):
        return "<bundle>" + real[len(root) :]
    return real


#: (real path, size, mtime ns) -> SHA-256 prefix: each file is read once a process
_DIGESTS: dict[tuple[str, int, int], str] = {}
_DIGEST_CHUNK = 1 << 20


def fingerprint(path: str | os.PathLike[str] | None) -> str | None:
    """``<real path>:<size>:<SHA-256 prefix>`` of an executable or library, the path
    relative to the bundle for one inside it (``_shown``); None when absent. An
    upgrade, a reinstall of other bytes or a Homebrew version directory changes it.

    The contents, not the mtime: the same build copied again (a rebuild, a fresh
    drag from the disk image, a restore from backup) gets new mtimes and the same
    bytes, and an mtime key threw away a 20-minute-old benchmark - Automatic then
    fell back to unmeasured MVTools and dropped frames (measured, M2 Air). The
    files hashed are mpv, vspipe, MoltenVK and the plugins: about 10 MB, ~15 ms."""
    if path is None:
        return None
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
        key = (real, st.st_size, st.st_mtime_ns)
        digest = _DIGESTS.get(key)
        if digest is None:
            h = hashlib.sha256()
            with open(real, "rb") as f:
                while chunk := f.read(_DIGEST_CHUNK):
                    h.update(chunk)
            digest = _DIGESTS[key] = h.hexdigest()[:16]
    except OSError:
        return None
    return f"{_shown(real)}:{st.st_size}:{digest}"


def _vulkan_fingerprint(env: Mapping[str, str]) -> str | None:
    """The Vulkan driver the benchmark's processes load, when it is named: the
    MoltenVK manifest ``osapi.point_at_app`` sets in ButterEye.app (and a
    ``VK_ICD_FILENAMES`` override anywhere), with the library it points to."""
    raw = env.get("VK_DRIVER_FILES") or env.get("VK_ICD_FILENAMES")
    if not raw:
        return None
    parts: list[str] = []
    for item in raw.split(os.pathsep):
        if not item:
            continue
        manifest = Path(item)
        parts.append(fingerprint(manifest) or f"{_shown(os.path.realpath(manifest))}:missing")
        try:
            lib = json.loads(manifest.read_text(encoding="utf-8"))["ICD"]["library_path"]
        except OSError, ValueError, KeyError, TypeError:
            continue
        if isinstance(lib, str):
            lib_path = Path(lib) if os.path.isabs(lib) else manifest.parent / lib
            parts.append(fingerprint(lib_path) or f"{_shown(os.path.realpath(lib_path))}:missing")
    return ";".join(parts) or None


#: the components a whole result depends on; ``plugin.<backend>`` keys are
#: per measurement (a new RIFE plugin says nothing about MVTools)
RESULT_KEYS = ("app", "bench", "mpv", "vspipe", "vulkan")


def plugin_key(backend: BackendId) -> str:
    return f"plugin.{backend.value}"


def current_versions(paths: Paths, env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    """What a benchmark run now would be measuring (stat calls only, no process)."""
    dirs = _paths.plugin_dirs(paths)
    out: dict[str, str | None] = {
        "app": _APP_VERSION,
        "bench": str(store.BENCH),
        "mpv": fingerprint(osapi.executable("mpv")),
        "vspipe": fingerprint(osapi.vspipe()),
        "vulkan": _vulkan_fingerprint(env if env is not None else os.environ),
    }
    for backend, name in _SCRIPT_PLUGIN.items():
        out[plugin_key(backend)] = fingerprint(_paths.plugin_in(dirs, name))
    return out


def current_result(entry: store.Entry, versions: Mapping[str, str | None]) -> BenchResult | None:
    """``entry``'s result as far as it still describes this software, or None. Pure.

    Not used at all: a contended run (something else played or converted
    meanwhile, B8) and a result that recorded no versions (schema 1, before
    0.0.6) or other ones for app, script, mpv, vspipe or the Vulkan driver.
    Measurements of an engine whose plugin changed are dropped; the rest stays.
    Age is not judged here: an old result is still the best number there is
    until the background re-run (``verdict.stale``) replaces it.
    """
    meta = entry.meta
    if meta.contended or meta.versions is None:
        return None
    if any(meta.versions.get(k) != versions.get(k) for k in RESULT_KEYS):
        return None
    r = entry.result
    keep = tuple(
        m
        for m in r.measurements
        if m.backend not in _SCRIPT_PLUGIN
        or meta.versions.get(plugin_key(m.backend)) == versions.get(plugin_key(m.backend))
    )
    if not keep:
        return None
    return r if keep == r.measurements else dataclasses.replace(r, measurements=keep)


def _versions_now(ctx: ProviderContext) -> dict[str, str | None]:
    st = _state(ctx)
    now = time.monotonic()
    if st.versions is None or now - st.versions[0] > VERSIONS_TTL_S:
        st.versions = (now, current_versions(ctx.paths))
    return st.versions[1]


# ---------------------------------------------------------------- what else runs (B8)
_LIVE_SESSIONS = ProviderRef("buttereye.core.mpvctl.session", "sessions")
_RECENT_SESSIONS = ProviderRef("buttereye.core.mpvctl.session", "recent_sessions")
_RENDER_JOBS = ProviderRef("buttereye.core.render.jobs", "jobs")
#: True only for a conversion that runs and is not stopped: a render stops its
#: own process group within ``jobs.PAUSE_POLL_S`` (0.5 s) of the benchmark lock
#: being taken (B11), and a stopped one uses neither the GPU nor the CPU
_RENDER_BUSY = ProviderRef("buttereye.core.render.jobs", "busy")
#: how long a benchmark waits, after taking its lock, for a running conversion
#: to stop itself before the first look (twice the render's poll interval)
RENDER_SETTLE_S = 1.0
_SETTLE_POLL_S = 0.1


def _playing(snapshot: Any) -> bool:
    return not getattr(snapshot, "ended", False)


def _converting(job: Any) -> bool:
    return getattr(job, "state", None) in (OpState.RUNNING, OpState.CANCELLING)


async def _ask(ctx: ProviderContext, ref: ProviderRef) -> Any:
    """``ref``'s answer, or None when its module is not loaded or it failed.
    Only modules already loaded are asked: none loaded, none running."""
    if ref.module not in sys.modules:
        return None
    fn = capabilities.resolve(ref)
    if fn is None:
        return None
    try:
        return await fn(ctx)
    except Exception:
        _log.exception("%s.%s failed", ref.module, ref.attr)
        return None


async def _rendering(ctx: ProviderContext) -> bool:
    busy = await _ask(ctx, _RENDER_BUSY)
    if isinstance(busy, bool):
        return busy
    # a jobs module without ``busy``: every running conversion counts
    return any(_converting(j) for j in await _ask(ctx, _RENDER_JOBS) or ())


async def others_running(ctx: ProviderContext) -> bool:
    """A video plays or a conversion runs in this process: either shares the GPU
    and the CPU with the benchmark, so its numbers would be too low (and stored
    for good). A conversion stopped for the benchmark does not count."""
    if any(_playing(x) for x in await _ask(ctx, _LIVE_SESSIONS) or ()):
        return True
    return await _rendering(ctx)


async def sessions_seen(ctx: ProviderContext) -> frozenset[object]:
    """The ids of every player this process knows of, live or recently ended.
    ``others_running`` only looks at moments; one player that opens and closes
    between two looks (inside the 150 s sustained phase, say) shows up here as
    an id that was not there when the benchmark began."""
    snaps = (*(await _ask(ctx, _LIVE_SESSIONS) or ()), *(await _ask(ctx, _RECENT_SESSIONS) or ()))
    return frozenset(sid for sid in (getattr(x, "sid", None) for x in snaps) if sid is not None)


async def settle_renders(ctx: ProviderContext, timeout_s: float = RENDER_SETTLE_S) -> None:
    """Give a running conversion time to stop itself (it polls the lock), so
    the benchmark's first look does not count it as contention."""
    deadline = time.monotonic() + timeout_s
    while await _rendering(ctx) and time.monotonic() < deadline:
        await asyncio.sleep(_SETTLE_POLL_S)


# ---------------------------------------------------------------- measuring one candidate
_STEPS_PER_CANDIDATE = WARMUP_RUNS + 2 * TIMED_RUNS


@dataclass(slots=True)
class _Progress:
    op: OpContext
    total: int
    done: int = 0

    def step(self, phase: Msg) -> None:
        self.op.report(phase, done=self.done, total=self.total, unit="steps")
        self.done = min(self.done + 1, self.total)  # never past the end, never back


@dataclass(slots=True)
class _Thermal:
    """``osapi.thermal_state`` at the start and the highest seen (None off a Mac)."""

    start: int | None = None
    peak: int | None = None

    @classmethod
    def begin(cls) -> _Thermal:
        t = cls()
        t.sample()
        t.start = t.peak
        return t

    def sample(self) -> None:
        now = osapi.thermal_state()
        if now is not None and (self.peak is None or now > self.peak):
            self.peak = now


@dataclass(slots=True)
class _Outcome:
    m: BenchMeasurement
    failure: measure.MeasureFailed | None
    #: the in-mpv pass, for the sustained phase (None: mpv was not measured)
    mpv_argv: list[str] | None = None
    frames: int = 0
    prefilled: int = 0


async def _mpv_pass(
    op: OpContext,
    argv: Sequence[str],
    *,
    frames: int,
    prefilled: int,
    timeout_s: float,
    tools: Tools,
    pids: set[int],
) -> measure.MpvRun:
    return await measure.run_mpv(
        op, argv, frames=frames, prefilled=prefilled, timeout_s=timeout_s, env=tools.env, pids=pids
    )


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
    gpu_uuid: str | None = None,
) -> _Outcome:
    width, height = size
    source, target = req.source_fps, target_fps(req)
    out_frames = output_frames(height)
    src_frames = math.ceil(out_frames * source / target) + 2
    failure: measure.MeasureFailed | None = None
    vpy = tools.vpy
    data: dict[str, Any] = {}
    if c.backend is BackendId.RIFE_TRT:
        prog.step(Msg("{config} — preparing the TensorRT engine", {"config": label}))
        try:
            data, vpy = await _trt_prepare(
                ctx, c, gpu_uuid, width=width, height=height, source=source,
                target=target, src_frames=src_frames,
            )  # fmt: skip
        except measure.MeasureFailed as exc:
            failure = exc
    else:
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
    vs_argv = measure.vspipe_argv(tools.vspipe, vpy, data, out_frames)

    if c.backend in (BackendId.RIFE_NCNN, BackendId.RIFE_TRT):
        faults.mark_rife_session(ctx.paths)  # doctor's BE-1030 window starts here
    before = faults.snapshot()
    pids: set[int] = set()
    vram = measure.VramSampler.detect() if tools.vram else None
    vs_runs: list[measure.VspipeRun] = []
    mpv_runs: list[measure.MpvRun] = []
    mpv_failure: measure.MeasureFailed | None = None
    timed, frames = TIMED_RUNS, out_frames
    try:
        if failure is not None:
            raise failure
        warm: measure.VspipeRun | None = None
        for _ in range(WARMUP_RUNS):
            prog.step(Msg("{config} — warm-up — vspipe", {"config": label}))
            warm = await measure.run_vspipe(
                op, vs_argv, timeout_s=timeout, env=tools.env, pids=pids
            )
        if warm is not None and warm.fps < REALTIME_HEADROOM * float(target) / 2:
            # P4: under half the real-time bar before mpv's own overhead, nothing a
            # longer series finds changes the verdict. RIFE on an M2 took ~23 s a
            # pass, ~3 min a model, and heat-soaked whatever was measured after it.
            # One short pass of each still gives the offline render its mpv rate.
            _log.info(
                "%s: %.1f fps against a %.1f fps bar, measured once",
                label, warm.fps, REALTIME_HEADROOM * float(target),
            )  # fmt: skip
            timed, frames = 1, min(out_frames, HOPELESS_FRAMES)
            vs_argv = measure.vspipe_argv(tools.vspipe, vpy, data, frames)
            timeout = 30.0 + frames / 2.0
        for n in range(1, timed + 1):
            prog.step(
                Msg(
                    "{config} — run {n} of {total} — vspipe",
                    {"config": label, "n": n, "total": timed},
                )
            )
            vs_runs.append(
                await measure.run_vspipe(
                    op, vs_argv, timeout_s=timeout, env=tools.env, vram=vram, pids=pids
                )
            )
    except measure.MeasureFailed as exc:
        failure = exc
    # the pipeline fill: frames in flight when the first one is shown were
    # computed before the clock started (B26)
    prefilled = c.concurrent_frames
    mpv_argv: list[str] | None = None
    if failure is None and tools.mpv is not None:
        mpv_argv = measure.mpv_argv(
            tools.mpv,
            vpy,
            data,
            width=width,
            height=height,
            source_fps=source,
            frames=frames,
            concurrent_frames=c.concurrent_frames,
            buffered_frames=BUFFERED_FRAMES,
        )
        retries = MPV_RETRIES
        n = 1
        retrying = False
        while n <= timed:
            if not retrying:
                prog.step(
                    Msg(
                        "{config} — run {n} of {total} — in mpv",
                        {"config": label, "n": n, "total": timed},
                    )
                )
            try:
                mpv_runs.append(
                    await _mpv_pass(
                        op,
                        mpv_argv,
                        frames=frames,
                        prefilled=prefilled,
                        timeout_s=timeout,
                        tools=tools,
                        pids=pids,
                    )  # fmt: skip
                )
            except measure.MeasureFailed as exc:
                if retries > 0:
                    # B10: one failed pass (a Vulkan device lost to the
                    # compositor, a slow start) once kept mvgpu out of Automatic
                    # for good; one more try separates that from a broken setup
                    retries -= 1
                    retrying = True
                    _log.info("in-mpv pass %d of %s failed (%s); once more", n, label, exc.reason)
                    continue
                mpv_failure = exc
                mpv_runs.clear()
                mpv_argv = None
                break
            retrying = False
            n += 1
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
        return _Outcome(
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
    if mpv_runs:
        mp_fps = [r.fps for r in mpv_runs]
        mpv_fps = statistics.median(mp_fps)
        # B6: the spread is shown; whether the verdict repeats is judged on the
        # in-mpv series against the bar, which is what selection acts on
        spread = measure.cov(mp_fps)
        repeatable = verdict.holds_the_bar(mp_fps, target)
        startup_s = statistics.median(r.startup_s for r in mpv_runs)
        realtime = verdict.realtime(mpv_fps, target)
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
        spread = measure.cov(vs_fps)
        repeatable = False
        startup_s = reload_s
        realtime = False
    return _Outcome(
        BenchMeasurement(
            label=label,
            backend=c.backend,
            model=c.model,
            vspipe_fps=round(vspipe_fps, 2),
            mpv_fps=round(mpv_fps, 2),
            cov=round(spread, 4) if math.isfinite(spread) else spread,
            repeatable=repeatable,
            startup_s=round(startup_s, 3),
            reload_s=round(reload_s, 3),
            vram_bytes=vram.peak if vram else None,
            realtime=realtime,
            gpu_faults=gpu_faults,
        ),
        None,
        mpv_argv,
        frames,
        prefilled,
    )


async def _sustain(
    op: OpContext,
    out: _Outcome,
    tools: Tools,
    prog: _Progress,
    thermal: _Thermal,
) -> tuple[float | None, int | None]:
    """B7: back-to-back in-mpv passes for ``SUSTAINED_S`` -> (the median rate of
    the heat-soaked second half, new GPU faults). A fanless Air holds a burst
    for well under a minute; mvgpu was measured in ~19 s, which says nothing
    about minute three of a film. None: fewer than two passes completed."""
    m = out.m
    assert out.mpv_argv is not None
    pass_s = out.frames / max(m.mpv_fps, 1e-3) + max(m.startup_s, 0.0)
    passes = max(2, min(SUSTAINED_MAX_PASSES, math.ceil(SUSTAINED_S / max(pass_s, 1e-3))))
    every = max(1, math.ceil(passes / SUSTAINED_STEPS))
    timeout = 30.0 + out.frames / 2.0
    before = faults.snapshot()
    pids: set[int] = set()
    runs: list[float] = []
    for i in range(passes):
        if i % every == 0:
            prog.step(
                Msg(
                    "{config} — keeping it up for {s} s — in mpv",
                    {"config": m.label, "s": round(SUSTAINED_S)},
                )
            )
        try:
            run = await _mpv_pass(
                op, out.mpv_argv, frames=out.frames, prefilled=out.prefilled, timeout_s=timeout,
                tools=tools, pids=pids,
            )  # fmt: skip
        except measure.MeasureFailed as exc:
            _log.warning("sustained pass %d of %s failed: %s", i + 1, m.label, exc.reason)
            break
        runs.append(run.fps)
        thermal.sample()
    new_faults = await faults.count_new(before, pids)
    if len(runs) < 2:
        return None, new_faults
    return statistics.median(runs[len(runs) // 2 :]), new_faults


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


def plan(
    req: BenchRequest, cands: Sequence[Candidate]
) -> list[tuple[tuple[int, int], tuple[int, int], Candidate]]:
    """What to measure, in measuring order: ``(order key, size, candidate)``. Pure.

    The order key ``(size index, quality index)`` is the order results are
    reported in. Measuring order differs (B7): the live engines (mvgpu, MVTools)
    first, while the machine is cool, then RIFE and TensorRT, whose minutes of
    GPU load would otherwise heat-soak them. Without ``req.full`` the live
    engines are also measured at the bigger ``FULL_SIZES`` (1440p and 2160p from
    1080p): 4K decisions otherwise come from a 1080p number scaled by an
    exponent measured on an RTX 4090.
    """
    sizes = list(sizes_for(req))
    if not req.full and AUTO_LIVE_SIZES:
        px = req.width * req.height
        sizes += [s for s in FULL_SIZES if s[0] * s[1] > px]
    tasks = [
        ((si, ci), size, c)
        for si, size in enumerate(sizes)
        for ci, c in enumerate(cands)
        if si < len(sizes_for(req)) or c.backend in verdict.LIVE_BACKENDS
    ]
    return sorted(tasks, key=lambda t: (t[2].backend not in verdict.LIVE_BACKENDS, t[0]))


def _steps(c: Candidate) -> int:
    return _STEPS_PER_CANDIDATE + (1 if c.backend is BackendId.RIFE_TRT else 0)


async def _bench_locked(ctx: ProviderContext, req: BenchRequest, op: OpContext) -> BenchResult:
    tools = find_tools(ctx.paths)
    rife_ok, gpu_id, gpu_uuid = _gpu(ctx)
    cands = discover_candidates(tools.plugin_dirs, tools.model_dir, rife=rife_ok)
    if rife_ok:
        cands = trt_candidates(ctx) + cands
    if not cands:
        fix, commands = _install_fix(
            Msg("Install ButterEye's plugin packages, then run the benchmark again."),
            INSTALL_PLUGINS,
        )
        raise _fail(
            Msg("No interpolation plugins are installed, so there is nothing to measure."),
            fix,
            commands=commands,
        )
    try:
        past = store.read(ctx.paths.bench_file)
    except (store.HistoryCorrupt, OSError) as exc:
        _log.warning("benchmark history %s unreadable: %s", ctx.paths.bench_file, exc)
        past = ()
    faulters = repeat_faulters(past, gpu_uuid)
    tasks = plan(req, cands)
    sustain = SUSTAINED_S > 0 and tools.mpv is not None
    prog = _Progress(
        op, total=sum(_steps(c) for _, _, c in tasks) + (SUSTAINED_STEPS if sustain else 0)
    )
    power = osapi.power_snapshot()
    thermal = _Thermal.begin()
    await settle_renders(ctx)
    contended = await others_running(ctx)
    seen = await sessions_seen(ctx)
    outcomes: dict[tuple[int, int], _Outcome] = {}
    failures: list[str] = []
    index = 0
    for key, size, c in tasks:
        prog.done = index  # skipped passes still count
        index += _steps(c)
        label = size_label(c.label, size, req)
        out = await _measure(ctx, op, c, label, tools, req, size, gpu_id, prog, gpu_uuid)
        outcomes[key] = out
        thermal.sample()
        contended = contended or await others_running(ctx)
        if out.failure is not None:
            failures.append(f"{label}: {out.failure.reason}\n{out.failure.detail}")
    if len(failures) == len(outcomes):
        raise _fail(
            Msg("No configuration could be measured."),
            Msg("Open System and press F5 to check the plugins and the GPU."),
            detail="\n\n".join(failures),
        )
    requested = frozenset(c.label for c in cands)
    target = target_fps(req)
    avoid: set[str] = set()
    for out in outcomes.values():
        m = out.m
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
    sustained: dict[str, float] = {}
    if sustain:
        prog.done = index
        tried = 0
        for key in sorted(k for k in outcomes if k[0] == 0):  # the requested size, best first
            out = outcomes[key]
            m = out.m
            if tried >= SUSTAINED_TRIES:
                break
            if out.mpv_argv is None or m.label in avoid or not verdict.live_ok(m, target):
                continue
            tried += 1
            fps, new_faults = await _sustain(op, out, tools, prog, thermal)
            if new_faults:
                out.m = m = dataclasses.replace(m, gpu_faults=(m.gpu_faults or 0) + new_faults)
            if fps is None:
                break  # it held as far as it could be measured: keep the burst
            sustained[m.label] = round(fps, 2)
            held = min(m.mpv_fps, fps)
            rt = verdict.realtime(held, target)
            out.m = dataclasses.replace(
                m, mpv_fps=round(held, 2), realtime=rt, repeatable=m.repeatable and rt == m.realtime
            )
            if verdict.live_ok(out.m, target):
                break
            _log.info("%s fell to %.1f fps in mpv when kept up; trying the next", m.label, fps)
    # a player that came and went between two looks still shared the machine
    contended = contended or await others_running(ctx) or bool(await sessions_seen(ctx) - seen)
    op.report(Msg("Benchmark finished"), done=prog.total, total=prog.total, unit="steps")
    measurements = tuple(outcomes[k].m for k in sorted(outcomes))
    result = BenchResult(
        request=req,
        measurements=measurements,
        recommended=recommend(measurements, requested, frozenset(avoid), target),
        when=datetime.now(UTC),
        gpu_uuid=gpu_uuid,
    )
    meta = store.Meta(
        versions=current_versions(ctx.paths),
        on_battery=power.on_battery,
        low_power=power.low_power,
        thermal_start=thermal.start,
        thermal_max=thermal.peak,
        contended=contended,
        sustained=sustained,
    )
    _state(ctx).versions = None  # the next history() reads them afresh
    if contended:
        ctx.emit(
            Notice(
                Msg(
                    "Something played or converted while the speed test ran, so playback "
                    "won't use these numbers. Run it again while nothing else plays."
                ),
                ErrorCode.BENCH_FAILED,
            )
        )
    try:
        store.append(ctx.paths.bench_file, result, meta=meta)
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
    """Stored results that still describe this software, oldest first (missing or
    unreadable file -> ``()``): see :func:`current_result`. The file keeps the
    rest (the GPU-fault lookback still counts them)."""
    try:
        entries = store.read_entries(ctx.paths.bench_file)
    except (store.HistoryCorrupt, OSError) as exc:
        _log.warning("benchmark history %s unreadable: %s", ctx.paths.bench_file, exc)
        return ()
    versions = _versions_now(ctx)
    return tuple(r for r in (current_result(e, versions) for e in entries) if r is not None)


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
        concurrent_frames=(
            RIFE_CONCURRENT_FRAMES
            if m.backend in (BackendId.RIFE_NCNN, BackendId.RIFE_TRT)
            else MVGPU_CONCURRENT_FRAMES_DEFAULT
            if m.backend is BackendId.MVGPU
            else None
        ),
        hdr=old.hdr if old is not None else "skip",
        builtin=False,
        content=old.content if old is not None else ContentKind.AUTO,
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
    "plan",
    "current_versions",
    "current_result",
    "fingerprint",
    "others_running",
    "sessions_seen",
    "settle_renders",
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
