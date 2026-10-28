# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Render provider: probe, a sequential queue, cancel, stale jobs (SCOPE §7, §7.8;
docs/design/GUI.md §2.5, §12.7).

One job at a time. A job: ffprobe the source → write the live script with a file
source (ffms2, spike M0(e)) → ``vspipe | ffmpeg`` into a hidden video-only MKV next
to the output → ``mkvmerge`` (or ``ffmpeg``) copies every other stream of the
source → rename into place. vspipe and ffmpeg share one process group, recorded in
a manifest under ``jobs_dir`` so a job left by a crashed GUI can be found and
stopped (``stale_jobs``). Cancel and failure delete every partial file, except
the encoded video when only the remux failed: hours of encoding are kept, and
adding the same copy again finishes it without converting again.

The group stops (SIGSTOP) while a video plays in ButterEye or a benchmark runs,
and continues (SIGCONT) after: the render and the player are separate processes
on the same GPU, and lower CPU priority does nothing for the GPU. On macOS the
Mac doesn't idle-sleep while a job runs (``osapi.begin_activity`` and
``caffeinate -i -w`` on vspipe).

HDR (§7.4): HDR10 sources, and HDR10+ / Dolby Vision ones with an HDR10 base
layer (their dynamic metadata dropped, with a warning), become HDR10 copies through
x265 or SVT-AV1, with the mastering display and content light level carried into
the encoder's stream; mkvmerge also writes them into the container, ffmpeg's
remux can't. HLG, Dolby Vision profile 5 and Dolby Vision without an HDR10 base
layer are refused.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buttereye.core import capabilities, filelog, osapi
from buttereye.core import paths as _paths
from buttereye.core.backends import trt
from buttereye.core.capabilities import ProviderRef
from buttereye.core.commands import install_command
from buttereye.core.doctor.checks_pkgs import parse_encoders
from buttereye.core.doctor.proc import run
from buttereye.core.errors import ButterEyeError, ErrorCode, InsufficientSpace, RenderRefused
from buttereye.core.events import JobChanged
from buttereye.core.mpvctl import decide
from buttereye.core.ops import _own_group, terminate_group
from buttereye.core.profiles import luts
from buttereye.core.render import pipeline
from buttereye.core.render import probe as rprobe
from buttereye.core.scriptgen.generator import (
    MVGPU_CONCURRENT_FRAMES_DEFAULT,
    RIFE_CONCURRENT_FRAMES_DEFAULT,
    FilterParams,
    ScriptConstants,
    rife_gpu_thread,
    user_data,
    write_atomic,
    write_script,
)
from buttereye.core.types import (
    BackendId,
    BenchResult,
    Config,
    ContentKind,
    Finding,
    HdrClass,
    JobId,
    Msg,
    OpState,
    Profile,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
    RenderProbe,
    Section,
    Severity,
    StaleJob,
    Target,
    TargetKind,
)

if TYPE_CHECKING:
    from buttereye.core.api import ProviderContext

_log = logging.getLogger(__name__)

STATE_KEY = "render.jobs"
_BENCH_HISTORY = ProviderRef("buttereye.core.bench.runner", "history")
_LIVE_SESSIONS = ProviderRef("buttereye.core.mpvctl.session", "sessions")
MODEL_SUBDIR = "rife-ncnn-models"
USER_MODEL_SUBDIR = "models"
PROBE_TIMEOUT_S = 60.0
REMUX_TIMEOUT_S = 6 * 3600.0
RENICE = 10  # §7.6: renders always run at lower CPU priority than the desktop
#: ffms2 comes with ButterEye on Windows (its installer), with dnf on Fedora and
#: with Homebrew on macOS, where packaging it is phase 4 of the port. The same
#: three answers as ``capabilities._FFMS2_INSTALL``, for the same condition:
#: ``ffms2`` is not a key in the DNF/WINGET/BREW maps, because those three are
#: kept the same shape and Windows installs it rather than naming it.
INSTALL_FFMS2 = (
    "" if osapi.IS_WINDOWS else "brew install ffms2" if osapi.IS_MACOS else "sudo dnf install ffms2"
)
INSTALL_MKVTOOLNIX = install_command("mkvtoolnix")
PROGRESS_INTERVAL_S = 0.5
#: how often a running render checks for playback or a benchmark to stop for
PAUSE_POLL_S = 0.5
#: Homebrew's two prefixes: a Mac app started from Finder has neither on PATH
BREW_BINS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
ACTIVITY_REASON = "ButterEye is saving a smooth copy"


# ---------------------------------------------------------------- state
@dataclass(slots=True)
class _Job:
    id: JobId
    spec: RenderJobSpec
    state: OpState = OpState.QUEUED
    phase: RenderPhase | None = None
    done: int | None = None
    total: int | None = None
    fps: float | None = None
    eta_s: float | None = None
    est_bytes: int | None = None
    error: ButterEyeError | None = None
    task: asyncio.Task[None] | None = None
    #: the encoded video kept after a failed remux, until the job is forgotten
    kept: Path | None = None
    #: stopped (SIGSTOP) while a video plays or a benchmark runs
    paused: bool = False


@dataclass(slots=True)
class _Registry:
    jobs: dict[JobId, _Job] = field(default_factory=dict)  # insertion order = queue order
    worker: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _reg(ctx: ProviderContext) -> _Registry:
    return ctx.state(STATE_KEY, _Registry)


def _snapshot(job: _Job) -> RenderJobState:
    return RenderJobState(
        id=job.id,
        spec=job.spec,
        state=job.state,
        phase=job.phase,
        done_frames=job.done,
        total_frames=job.total,
        fps=job.fps,
        eta_s=job.eta_s,
        est_bytes=job.est_bytes,
        error=job.error,
    )


def _emit(ctx: ProviderContext, job: _Job, *, progress: bool = False) -> None:
    ev = JobChanged(_snapshot(job))
    if progress:
        ctx.emit_coalesced(f"render-{job.id}", ev, min_interval_s=PROGRESS_INTERVAL_S)
    else:
        ctx.emit(ev)


def _title(job: _Job) -> str:
    return job.spec.source.name


# ---------------------------------------------------------------- tools and facts
@dataclass(frozen=True, slots=True)
class _Tools:
    ffprobe: str
    ffmpeg: str
    vspipe: str
    mkvmerge: str | None


def _missing(what: str, command: str | None) -> ButterEyeError:
    """``command`` None: ButterEye's own installer brings it (Windows: Repair)."""
    if command is None:
        return ButterEyeError(
            ErrorCode.PKG_MISSING,
            Msg("{tool} is not installed, so videos can't be converted.", {"tool": what}),
            Msg("Run the ButterEye installer again and choose Repair."),
        )
    return ButterEyeError(
        ErrorCode.PKG_MISSING,
        Msg("{tool} is not installed, so videos can't be converted.", {"tool": what}),
        Msg("Install it, then try again."),
        commands=(command,),
    )


def _tools() -> _Tools:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    vspipe = osapi.vspipe()
    if ffprobe is None or ffmpeg is None:
        raise _missing("ffmpeg", install_command("ffmpeg"))
    if vspipe is None:
        # Windows: vspipe is part of the installer's bundled VapourSynth
        raise _missing("vspipe", None if osapi.IS_WINDOWS else install_command("vapoursynth"))
    return _Tools(ffprobe, ffmpeg, vspipe, _mkvmerge())


def _mkvmerge() -> str | None:
    """The mkvmerge to remux with: the one the app carries (``Resources/bin``,
    or the AppImage's ``bin``), then Homebrew's on a Mac, then PATH."""
    for root in (osapi.MACOS_APP, osapi.LINUX_APP):
        if root is not None:
            own = root / osapi.APP_BIN / "mkvmerge"
            if own.is_file():
                return os.fspath(own)
    if osapi.IS_MACOS:
        for d in BREW_BINS:
            brew = d / "mkvmerge"
            if brew.is_file() and os.access(brew, os.X_OK):
                return os.fspath(brew)
    return shutil.which("mkvmerge")


#: (ffmpeg, its mtime) → the VideoToolbox encoders that encoded a test clip
_VT_WORKING: dict[tuple[str, int], frozenset[str]] = {}


async def _videotoolbox_working(ffmpeg: str, listed: Sequence[str]) -> frozenset[str]:
    """The listed VideoToolbox encoders that really encode here, with the options
    a render passes: ``-encoders`` lists them on every macOS build of ffmpeg,
    with or without the hardware (a VM, an Intel Mac without HEVC). A 6-frame
    test each, about 0.15 s on an M2, once per ffmpeg."""
    wanted = [e for e in listed if e in rprobe.VIDEOTOOLBOX_ENCODERS]
    if not wanted:
        return frozenset()
    try:
        key = (ffmpeg, os.stat(ffmpeg).st_mtime_ns)
    except OSError:
        key = (ffmpeg, 0)
    found = _VT_WORKING.get(key)
    if found is None:
        ok = []
        for enc in wanted:
            res = await run(pipeline.encoder_test_argv(ffmpeg, enc), timeout_s=15.0)
            if res.ok:
                ok.append(enc)
            else:
                _log.info("%s is listed but doesn't encode here: %s", enc,
                          (res.stderr or "").strip()[-300:])  # fmt: skip
        found = _VT_WORKING[key] = frozenset(ok)
    return found


async def _encoders(tools: _Tools, *, hdr10: bool, nvidia: bool) -> tuple[str, ...]:
    """``rprobe.usable_encoders`` from this ffmpeg's list, without VideoToolbox
    encoders that fail their test."""
    enc = await run((tools.ffmpeg, "-hide_banner", "-encoders"), timeout_s=10.0)
    listed = parse_encoders(enc.stdout) if enc.ok else ()
    if osapi.IS_MACOS and not hdr10:
        broken = set(rprobe.VIDEOTOOLBOX_ENCODERS) - await _videotoolbox_working(
            tools.ffmpeg, listed
        )
        listed = tuple(e for e in listed if e not in broken)
    return rprobe.usable_encoders(listed, nvidia=nvidia, hdr10=hdr10, apple=osapi.IS_MACOS)


async def _ffprobe(tools: _Tools, src: Path) -> Mapping[str, Any]:
    res = await run(
        (
            tools.ffprobe, "-v", "error", "-of", "json",
            "-show_streams", "-show_format", "-show_chapters", os.fspath(src),
        ),
        timeout_s=PROBE_TIMEOUT_S,
    )  # fmt: skip
    if not res.ok:
        raise RenderRefused(
            ErrorCode.CODEC_NOT_DECODABLE,
            Msg("ffprobe couldn't read this file."),
            Msg("Check that it is a video file."),
            detail=(res.stderr or "")[-2000:] or None,
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise RenderRefused(
            ErrorCode.CODEC_NOT_DECODABLE, Msg("ffprobe couldn't read this file.")
        ) from exc
    return data if isinstance(data, Mapping) else {}


async def _first_frame(tools: _Tools, src: Path) -> Mapping[str, Any] | None:
    """The first video frame's side data (§7.4: HDR metadata and HDR10+ live in the
    frames); None when ffprobe can't say, which leaves the stream's own data."""
    res = await run(
        (
            tools.ffprobe, "-v", "error", "-of", "json", "-select_streams", "v:0",
            "-show_frames", "-read_intervals", "%+#1",
            "-show_entries", "frame=media_type,side_data_list", os.fspath(src),
        ),
        timeout_s=PROBE_TIMEOUT_S,
    )  # fmt: skip
    if not res.ok:
        return None
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    return rprobe.first_video_frame(data) if isinstance(data, Mapping) else None


async def _source_info(tools: _Tools, src: Path) -> rprobe.SourceInfo | None:
    return rprobe.source_info(await _ffprobe(tools, src), await _first_frame(tools, src))


def _refusal(code: ErrorCode, title: str, cause: str, fix: str, **params: str) -> Finding:
    return Finding(
        id="render.refusal",
        section=Section.RENDER,
        severity=Severity.BLOCKING,
        code=code,
        title=Msg(title, params),
        cause=Msg(cause, params),
        fix=Msg(fix, params),
        commands=(INSTALL_FFMS2,) if code is ErrorCode.FFMS2_MISSING and INSTALL_FFMS2 else (),
    )


def refusal_for(
    info: rprobe.SourceInfo | None, decoders: frozenset[str], ffms2: bool
) -> Finding | None:
    """Why this source can't be converted in this build, or None (§7.1, §7.4, §7.8)."""
    if not ffms2:
        return _refusal(
            ErrorCode.FFMS2_MISSING,
            "Converting needs ffms2",
            "ffms2 reads your video for VapourSynth.",
            "Install it, then try again.",
        )
    if info is None:
        return _refusal(
            ErrorCode.CODEC_NOT_DECODABLE,
            "This file has no video to convert",
            "ffprobe found no video stream in it.",
            "Choose a video file.",
        )
    if decoders and info.codec not in decoders:
        return _refusal(
            ErrorCode.CODEC_NOT_DECODABLE,
            "This computer can't decode {codec} video",
            "The installed FFmpeg has no {codec} decoder, so ffms2 can't read this file.",
            "Install a libavcodec that decodes it, such as RPM Fusion's libavcodec-freeworld.",
            codec=info.codec,
        )
    if info.hdr_class is HdrClass.HLG:
        return _refusal(
            ErrorCode.HDR_CLASS_REFUSED,
            "HLG videos can't be converted yet",
            "This version converts SDR and HDR10 video.",
            "Choose an SDR or HDR10 video.",
        )
    if info.hdr_class is HdrClass.DV and not rprobe.renders_as_hdr10(info):
        if info.hdr.dv_profile == 5:
            return _refusal(
                ErrorCode.HDR_CLASS_REFUSED,
                "Dolby Vision profile 5 videos can't be converted",
                "Profile 5 has no HDR10 picture underneath to convert.",
                "Choose an SDR or HDR10 video.",
            )
        return _refusal(
            ErrorCode.HDR_CLASS_REFUSED,
            "This Dolby Vision video can't be converted",
            "It has no HDR10 picture underneath to convert.",
            "Choose an SDR or HDR10 video.",
        )
    if info.width <= 0 or info.height <= 0 or info.duration_s <= 0:
        return _refusal(
            ErrorCode.CODEC_NOT_DECODABLE,
            "ButterEye couldn't read this video's size or length",
            "ffprobe reported no frame size or duration.",
            "Try remuxing the file, then convert again.",
        )
    return None


def hdr_warnings(info: rprobe.SourceInfo | None, *, mkvmerge: bool = True) -> list[Finding]:
    """What an HDR10 copy keeps and drops (§7.4). ``mkvmerge`` False: ffmpeg
    remuxes, and its Matroska muxer has no option for the mastering display or
    light levels, so they are only in the encoded stream (x265's SEI, SVT-AV1's
    metadata OBUs), which most players read but some go by the container."""
    if info is None or not rprobe.renders_as_hdr10(info):
        return []
    if mkvmerge:
        hdr10 = Finding(
            "render.hdr10", Section.RENDER, Severity.INFO, None,
            Msg("Will be saved as HDR10"),
            Msg("The copy keeps the PQ picture, BT.2020 colour, mastering display and "
                "light levels; x265 or SVT-AV1 encodes it."),
            Msg("Nothing to do."),
        )  # fmt: skip
    else:
        hdr10 = Finding(
            "render.hdr10", Section.RENDER, Severity.INFO, None,
            Msg("Will be saved as HDR10"),
            Msg("The copy keeps the PQ picture and BT.2020 colour; x265 or SVT-AV1 "
                "writes the mastering display and light levels into the video stream, "
                "but without mkvmerge the file itself doesn't list them, which a few "
                "players need."),
            Msg("Install mkvtoolnix to have them in the file too."),
            commands=(INSTALL_MKVTOOLNIX,),
        )  # fmt: skip
    out = [hdr10]
    dropped = {HdrClass.HDR10PLUS: "HDR10+", HdrClass.DV: "Dolby Vision"}.get(info.hdr_class)
    if dropped is not None:
        out.append(
            Finding(
                "render.hdr_dynamic", Section.RENDER, Severity.DEGRADED, None,
                Msg("{kind} metadata will be dropped", {"kind": dropped}),
                Msg("Its scene-by-scene metadata can't follow new in-between frames, so "
                    "the copy is plain HDR10.", {"kind": dropped}),
                Msg("Nothing to do; HDR10 displays show it correctly."),
            )
        )  # fmt: skip
    return out


def _ffms2_lib() -> Path:
    return ScriptConstants().ffms2_lib


def _constants(ctx: ProviderContext, engine: BackendId | None = None) -> ScriptConstants:
    """The script's ``PLUGIN_DIR``: the one directory that really holds ``engine``'s
    plugin (``paths.plugin_dir_for``), as live play and the benchmark resolve it.

    A ``.vpy`` names a single directory, so this cannot be the search path, and on
    a Mac with the tap it must not be ``paths.rpm_plugin_dir`` either: that is
    ButterEye's own directory there, and Homebrew's ``mvtools.dylib`` is in
    VapourSynth's autoload one - measured in docs/macos-release-bar.md, item 6.
    Off macOS the search path is that one directory, so nothing changes there.
    ``engine`` None or TensorRT: no plugin to resolve, the packaged directory.
    """
    name = decide.BACKEND_PLUGIN.get(engine) if engine is not None else None
    return ScriptConstants(_paths.plugin_dir_for(ctx.paths, name), _ffms2_lib())


# ---------------------------------------------------------------- profile, engine, estimates
def _config(ctx: ProviderContext) -> Config | None:
    try:
        return ctx.config()
    except ButterEyeError:
        return None


def _hold_fill(engine: BackendId, profile: Profile | None) -> bool:
    """Whether a copy re-times held drawings (``FilterParams.hold_fill``).

    On unless the profile is set to live action: a copy has no real-time limit,
    so Automatic can afford the extra work, and the per-frame detection never
    fires on live action anyway. TensorRT has no hold fill."""
    if engine is BackendId.RIFE_TRT:
        return False
    return profile is None or profile.content is not ContentKind.LIVE


def _profile(cfg: Config | None, profile_id: str | None) -> Profile | None:
    if cfg is None or not cfg.profiles:
        return None
    by_id = {p.id: p for p in cfg.profiles}
    if profile_id is not None and profile_id in by_id:
        return by_id[profile_id]
    return by_id.get("simple") or cfg.profiles[0]


#: RIFE-ncnn render requests on macOS, where ``gpu_thread`` is 1 (MoltenVK has
#: one queue): 8 would hold about six 4K RGBS frames (100 MB each, out of 16 GB
#: shared with the GPU) that only wait. gpu_thread + 2 would be 3; one more keeps
#: the CPU's RGB conversion of the next frames overlapping. Not measured on the
#: M2 yet (peak memory and fps at 3 / 4 / 8): conservative, not minimal
RIFE_REQUESTS_MACOS = 4


def vspipe_requests(engine: BackendId) -> int | None:
    """vspipe's frame requests in flight for a render. RIFE-ncnn runs only
    ``gpu_thread`` frames at once; vspipe's default (one per CPU thread, 24 on the
    dev box) only holds RGBS frames waiting for the GPU: 4K 24 → 60 used 18.9 GB
    and ran 7.3 fps by default, 14.5 GB and 7.7 fps with 8. At least
    gpu_thread + 2, and ``RIFE_REQUESTS_MACOS`` on a Mac. mvgpu serialises its GPU
    work, so requests beyond its live concurrent-frames only hold frames. MVTools
    (CPU-bound) keeps vspipe's default."""
    if engine is BackendId.MVGPU:
        return MVGPU_CONCURRENT_FRAMES_DEFAULT
    if engine is BackendId.RIFE_NCNN:
        if osapi.IS_MACOS:
            return max(RIFE_REQUESTS_MACOS, rife_gpu_thread() + 2)
        return max(RIFE_CONCURRENT_FRAMES_DEFAULT, rife_gpu_thread() + 2)
    if engine is BackendId.RIFE_TRT:
        return RIFE_CONCURRENT_FRAMES_DEFAULT  # a few frames per CUDA stream
    return None


def offline_target(target: Target, src_fps: Fraction) -> Fraction:
    """§7.8: 2× and a fixed rate as asked; a display target renders as 2×."""
    if target.kind is TargetKind.FPS and target.fps is not None:
        return target.fps
    return src_fps * 2


def _trt(ctx: ProviderContext, profile: Profile | None) -> tuple[trt.TrtInstall, str] | None:
    """The TensorRT install and model for a render, when opted in and set up."""
    cfg = _config(ctx)
    if cfg is None or not cfg.general.trt_experimental:
        return None
    inst = trt.find_install(ctx.paths)
    if inst is None:
        return None
    avail = trt.available_models(inst)
    wanted = profile.model if profile is not None else None
    for name in (wanted, trt.DEFAULT_MODEL, *sorted(avail)):
        if name in avail:
            return inst, name
    return None


def _installed(ctx: ProviderContext, profile: Profile | None = None) -> frozenset[BackendId]:
    p = ctx.paths
    # every plugin directory, not one: on a Mac with the tap the one would be
    # ButterEye's own and MVTools would vanish from the render (paths.plugin_dirs)
    return decide.installed_backends(
        _paths.plugin_dirs(p),
        p.rpm_data_dir / MODEL_SUBDIR,
        (p.data_dir / USER_MODEL_SUBDIR,),
        trt=_trt(ctx, profile) is not None,
    )


def _candidates(
    ctx: ProviderContext, profile: Profile | None
) -> tuple[tuple[BackendId, decide.ModelChoice | None], ...]:
    """The engines this copy may use, best picture first, each with its model:
    the profile's own when it is pinned and installed, else every installed one
    of ``rprobe.OFFLINE_RANKING`` (``ANIMATION_RANKING`` for 2D animation, as
    live playback ranks it: RIFE-ncnn first, no TensorRT, which has no hold
    fill). Which one a copy takes is decided by time (``rprobe.pick_offline``)."""
    backend = profile.backend if profile is not None else "auto"
    animation = profile is not None and profile.content is ContentKind.ANIMATION
    order = rprobe.offline_order(backend, _installed(ctx, profile), animation=animation)
    out = _with_models(ctx, profile, order)
    if not out and backend != "auto":
        # the pinned engine can't run (no RIFE model, TensorRT not set up any
        # more): the usual order, never TensorRT
        others = _installed(ctx, None) - {BackendId.RIFE_TRT}
        out = _with_models(ctx, profile, rprobe.offline_order("auto", others))
    return out


def _with_models(
    ctx: ProviderContext, profile: Profile | None, order: Sequence[BackendId]
) -> tuple[tuple[BackendId, decide.ModelChoice | None], ...]:
    """``order`` with each engine's model; RIFE-ncnn without a model and a
    TensorRT that isn't set up are left out."""
    p = ctx.paths
    model = profile.model if profile is not None else None
    models = (p.rpm_data_dir / MODEL_SUBDIR, (p.data_dir / USER_MODEL_SUBDIR,))
    out: list[tuple[BackendId, decide.ModelChoice | None]] = []
    for b in order:
        if b is BackendId.RIFE_TRT:
            found = _trt(ctx, profile)
            if found is not None:
                out.append((b, decide.ModelChoice(found[1], None, None)))
        elif b is BackendId.RIFE_NCNN:
            mc = decide.find_model(model, *models)
            if mc.path is not None:
                out.append((b, mc))
        else:
            out.append((b, None))
    return tuple(out)


def _engine(
    ctx: ProviderContext, profile: Profile | None
) -> tuple[BackendId | None, decide.ModelChoice | None]:
    """The best-picture candidate: what a copy uses without benchmark results."""
    cands = _candidates(ctx, profile)
    return cands[0] if cands else (None, None)


def _rates(
    results: Sequence[BenchResult],
    cands: Sequence[tuple[BackendId, decide.ModelChoice | None]],
    sizes: Sequence[tuple[int, int]],
) -> dict[BackendId, dict[tuple[int, int], float | None]]:
    """``bench_rate`` for every candidate at every size."""
    return {
        b: {wh: bench_rate(results, b, mc.name if mc is not None else None, wh) for wh in sizes}
        for b, mc in cands
    }


async def _trt_engine(
    ctx: ProviderContext,
    profile: Profile | None,
    model: str,
    size: tuple[int, int],
    script: Path,
) -> dict[str, Any]:
    """Build (or reuse) the TensorRT engine for this size; the script's ``trt`` data."""
    found = _trt(ctx, profile)
    ident = await trt.nvidia_identity(None)
    if found is None or ident is None:
        raise _fail(
            ErrorCode.TRT_UNSUPPORTED,
            Msg("TensorRT isn't set up any more."),
            Msg("Run the System checks, or choose another engine."),
        )
    inst = found[0]
    key = trt.EngineKey(
        ident[0], ident[1], inst.vstrt_version, inst.trt_version, model, size[0], size[1]
    )
    folder = await trt.build_engine(ctx.paths, inst, key, script=script)
    return trt.script_settings(inst, key, folder, streams=trt.streams_for(*size), build=False)


async def _bench_results(ctx: ProviderContext) -> tuple[BenchResult, ...]:
    history = capabilities.resolve(_BENCH_HISTORY)
    if history is None:
        return ()
    try:
        return tuple(await history(ctx))
    except Exception:
        _log.exception("benchmark history failed")
        return ()


def bench_rate(
    results: Sequence[BenchResult],
    backend: BackendId,
    model: str | None,
    size: tuple[int, int],
) -> float | None:
    """In-mpv benchmark fps for ``backend``/``model`` scaled to ``size`` by pixel
    count (closest measured size, then newest), as a 2× output rate
    (``decide.load_rate`` units); None without a clean measurement."""
    from buttereye.core.bench.verdict import measured_in_mpv
    from buttereye.core.mpvctl.session import _measured_size

    px = size[0] * size[1]
    if px <= 0:
        return None
    found: list[tuple[float, float, float]] = []
    for result in results:
        when = result.when.timestamp() if result.when is not None else -math.inf
        for m in result.measurements:
            if m.backend is not backend or (model is not None and m.model != model):
                continue
            if m.gpu_faults or not measured_in_mpv(m) or not m.mpv_fps > 0:
                continue
            w, h = _measured_size(m.label, result.request)
            if w <= 0 or h <= 0:
                continue
            ratio = (w * h) / px
            req = result.request
            fps = decide.as_2x_rate(float(m.mpv_fps), req.source_fps, req.target_fps)
            found.append((round(-abs(math.log(ratio)), 6), when, fps * ratio))
    return max(found)[2] if found else None


def _lut(ctx: ProviderContext, cfg: Config | None) -> tuple[str | None, Path | None]:
    """The colour LUT the settings name (``GeneralSettings.lut``) and its file,
    or (None, None); a name whose file is gone or not safe gives (name, None)."""
    name = cfg.general.lut if cfg is not None else None
    if not name:
        return None, None
    return name, luts.resolve_lut(ctx.paths, name)


def _lut_note(
    ctx: ProviderContext, cfg: Config | None, info: rprobe.SourceInfo | None
) -> Finding | None:
    """What happens to the colour LUT in this copy, or None without one."""
    name, path = _lut(ctx, cfg)
    if name is None or info is None:
        return None
    if rprobe.renders_as_hdr10(info):
        return Finding(
            "render.lut", Section.RENDER, Severity.INFO, None,
            Msg("The colour LUT isn't used for HDR copies"),
            Msg("{name} is made for SDR video; an HDR10 copy keeps its own colour.",
                {"name": name}),
            Msg("Nothing to do."),
        )  # fmt: skip
    if path is None:
        return Finding(
            "render.lut", Section.RENDER, Severity.DEGRADED, None,
            Msg("The colour LUT can't be used"),
            Msg("{name} is missing from ButterEye's LUT folder, so the copy keeps its "
                "own colour.", {"name": name}),
            Msg("Import the LUT again in Settings."),
        )  # fmt: skip
    return Finding(
        "render.lut", Section.RENDER, Severity.INFO, None,
        Msg("The colour LUT is applied"),
        Msg("The copy is graded with {name}, as playback is.", {"name": name}),
        Msg("Nothing to do."),
    )  # fmt: skip


async def _pick(
    ctx: ProviderContext,
    profile: Profile | None,
    info: rprobe.SourceInfo,
    size: tuple[int, int],
    target: Fraction,
    wanted: BackendId | None,
) -> tuple[BackendId | None, decide.ModelChoice | None]:
    """The engine for a copy at ``size``: ``wanted`` (the window's choice) when it
    is one of the candidates, else what ``rprobe.pick_offline`` takes from the
    benchmark results, as the window showed it; the best picture without them."""
    cands = _candidates(ctx, profile)
    if not cands:
        return None, None
    by_engine = dict(cands)
    if wanted is not None and wanted in by_engine:
        return wanted, by_engine[wanted]
    rates = _rates(await _bench_results(ctx), cands, (size,))
    src = (info.width, info.height)
    options = [(b, rprobe.offline_fps(rates[b][size], size, src)) for b, _ in cands]
    chosen = rprobe.pick_offline(options, decide.load_rate(info.fps, target))
    engine = chosen[0] if chosen is not None else cands[0][0]
    return engine, by_engine[engine]


# ---------------------------------------------------------------- provider: probe
async def probe(ctx: ProviderContext, src: Path, *, profile_id: str | None = None) -> RenderProbe:
    tools = _tools()
    src = Path(src)
    info = await _source_info(tools, src)
    dec = await run((tools.ffmpeg, "-hide_banner", "-decoders"), timeout_s=10.0)
    decoders = rprobe.parse_decoders(dec.stdout) if dec.ok else frozenset()
    report = ctx.last_report()
    nvidia = report is None or any(g.vendor == "NVIDIA" for g in report.hardware.gpus)
    hdr10 = info is not None and rprobe.renders_as_hdr10(info)
    encoders = await _encoders(tools, hdr10=hdr10, nvidia=nvidia)
    refusal = refusal_for(info, decoders, _ffms2_lib().exists())
    if refusal is None and hdr10 and not encoders:
        refusal = _refusal(
            ErrorCode.ENCODER_FAILED,
            "HDR10 render needs the x265 or SVT-AV1 encoder",
            "Only these two take HDR10 mastering data; your ffmpeg lists neither.",
            "Install ffmpeg-free (SVT-AV1) or RPM Fusion's ffmpeg (x265).",
        )
    if refusal is None and not encoders:
        refusal = _refusal(
            ErrorCode.ENCODER_FAILED,
            "Your ffmpeg has no encoder ButterEye can use",
            "ffmpeg lists none of HEVC (NVENC or x265), AV1 (NVENC or SVT-AV1) or x264.",
            "Install ffmpeg-free or RPM Fusion's ffmpeg.",
        )
    warnings: list[Finding] = hdr_warnings(info, mkvmerge=tools.mkvmerge is not None)
    cfg = _config(ctx)
    lut_note = _lut_note(ctx, cfg, info)
    if lut_note is not None:
        warnings.append(lut_note)
    if info is not None and info.vfr:
        warnings.append(
            Finding(
                "render.vfr", Section.RENDER, Severity.INFO, None,
                Msg("Will be converted to constant frame rate"),
                Msg("This video has a variable frame rate; the copy plays at {fps} fps.",
                    {"fps": decide.fmt_rate(info.fps)}),
                Msg("Nothing to do."),
            )
        )  # fmt: skip
    if tools.mkvmerge is None:
        warnings.append(
            Finding(
                "render.mkvmerge", Section.RENDER, Severity.INFO, ErrorCode.PKG_MISSING,
                Msg("mkvmerge is not installed"),
                Msg("ffmpeg will copy the other streams instead."),
                Msg("Install mkvtoolnix for the preferred path."),
                commands=(INSTALL_MKVTOOLNIX,),
            )
        )  # fmt: skip
        if info is not None and any(
            st.kind == "subtitle" and st.codec == "mov_text" for st in info.streams
        ):
            warnings.append(
                Finding(
                    "render.mov_text", Section.RENDER, Severity.INFO, None,
                    Msg("MP4 subtitles become SubRip"),
                    Msg("Matroska can't hold MP4 text subtitles, so ffmpeg converts them; "
                        "their text and timing stay, styling such as colour doesn't."),
                    Msg("Nothing to do."),
                )
            )  # fmt: skip
    sizes: tuple[Any, ...] = ()
    if info is not None and info.width > 0 and info.height > 0:
        profile = _profile(cfg, profile_id)
        cands = _candidates(ctx, profile)
        results = await _bench_results(ctx) if cands else ()
        all_sizes = ((info.width, info.height), *decide.smaller_sizes(info.width, info.height))
        target = offline_target(
            profile.target if profile is not None else Target(TargetKind.X2), info.fps
        )
        # every engine at every size, each size with the engine that makes it in
        # time (P1): the window shows engine and time per size, and the job takes
        # the same one (``_pick``)
        sizes = rprobe.plan_sizes(
            info.width, info.height, _rates(results, cands, all_sizes),
            [b for b, _ in cands], decide.load_rate(info.fps, target),
        )  # fmt: skip
    try:
        free = shutil.disk_usage(src.parent).free
    except OSError:
        free = 0
    return RenderProbe(
        source=src,
        hdr_class=info.hdr_class if info is not None else HdrClass.SDR,
        vfr=info.vfr if info is not None else False,
        duration_s=info.duration_s if info is not None else 0.0,
        streams=info.streams if info is not None else (),
        chapters=info.chapters if info is not None else 0,
        encoders=encoders,
        remux_tool="mkvmerge" if tools.mkvmerge else "ffmpeg",
        free_bytes=free,
        warnings=tuple(warnings),
        refusal=refusal,
        width=info.width if info is not None else 0,
        height=info.height if info is not None else 0,
        fps=info.fps if info is not None else None,
        sizes=sizes,
    )


# ---------------------------------------------------------------- provider: queue
def _check_spec(spec: RenderJobSpec) -> None:
    src, out = Path(spec.source), Path(spec.output)
    if not src.is_file():
        raise RenderRefused(
            ErrorCode.CODEC_NOT_DECODABLE,
            Msg("The video {name} doesn't exist any more.", {"name": src.name}),
        )
    if out.suffix.lower() != ".mkv":
        raise RenderRefused(ErrorCode.OUTPUT_EXISTS, Msg("The copy must be saved as an .mkv file."))
    with contextlib.suppress(OSError):
        if out.resolve() == src.resolve():
            raise RenderRefused(
                ErrorCode.OUTPUT_EXISTS,
                Msg("The copy can't replace the video it is made from."),
                Msg("Choose another name."),
            )
    if out.exists() and not spec.overwrite:
        raise RenderRefused(
            ErrorCode.OUTPUT_EXISTS,
            Msg("{name} already exists.", {"name": out.name}),
            Msg("Choose another name, or confirm replacing it."),
        )
    if not out.parent.is_dir():
        raise RenderRefused(
            ErrorCode.OUTPUT_EXISTS,
            Msg("The folder {folder} doesn't exist.", {"folder": os.fspath(out.parent)}),
        )


async def enqueue(ctx: ProviderContext, spec: RenderJobSpec) -> JobId:
    _check_spec(spec)
    reg = _reg(ctx)
    job = _Job(JobId(uuid.uuid4().hex[:12]), spec)
    reg.jobs[job.id] = job
    jlog = filelog.for_item(_log, filelog.job_key(job.id))
    jlog.info("%s: queued a smooth copy → %s", _title(job), os.fspath(spec.output))
    _emit(ctx, job)
    if reg.worker is None or reg.worker.done():
        reg.worker = ctx.spawn(_worker(ctx), name="render-worker")
    return job.id


async def jobs(ctx: ProviderContext) -> tuple[RenderJobState, ...]:
    return tuple(_snapshot(j) for j in _reg(ctx).jobs.values())


async def busy(ctx: ProviderContext) -> bool:
    """A conversion is using the machine right now: running and not stopped for
    playback or a benchmark (a RUNNING job may be either). A benchmark that
    starts stops the render within ``PAUSE_POLL_S``."""
    return any(j.state is OpState.RUNNING and not j.paused for j in _reg(ctx).jobs.values())


def _get(ctx: ProviderContext, job_id: JobId) -> _Job:
    job = _reg(ctx).jobs.get(job_id)
    if job is None:
        raise ButterEyeError(ErrorCode.INTERNAL, Msg("That conversion no longer exists."))
    return job


async def move(ctx: ProviderContext, job_id: JobId, delta: int) -> None:
    reg = _reg(ctx)
    job = _get(ctx, job_id)
    if job.state is not OpState.QUEUED or delta == 0:
        return
    queued = [j for j in reg.jobs.values() if j.state is OpState.QUEUED]
    i = queued.index(job)
    queued.insert(max(0, min(len(queued) - 1, i + delta)), queued.pop(i))
    others = [j for j in reg.jobs.values() if j.state is not OpState.QUEUED]
    reg.jobs = {j.id: j for j in (*others, *queued)}
    for j in queued:
        _emit(ctx, j)


async def cancel(ctx: ProviderContext, job_id: JobId) -> None:
    job = _get(ctx, job_id)
    if job.state is OpState.QUEUED:
        job.state = OpState.CANCELLED
        _emit(ctx, job)
    elif job.state is OpState.RUNNING:
        job.state = OpState.CANCELLING
        _emit(ctx, job)
        if job.task is not None:
            job.task.cancel()


async def forget(ctx: ProviderContext, job_id: JobId) -> None:
    """Remove a finished job from the list; a video it kept after a failed remux
    is deleted with it, unless a queued or running job will finish from it."""
    job = _get(ctx, job_id)
    if job.state in (OpState.SUCCEEDED, OpState.FAILED, OpState.CANCELLED):
        reg = _reg(ctx)
        reg.jobs.pop(job_id, None)
        ctx.forget_coalesced(f"render-{job_id}")
        if job.kept is not None:
            out = Path(job.spec.output)
            reusing = any(
                Path(j.spec.output) == out
                and j.state in (OpState.QUEUED, OpState.RUNNING, OpState.CANCELLING)
                for j in reg.jobs.values()
            )
            if not reusing:
                _remove(job.kept, ctx.paths.jobs_dir / f"render-{job.id}.json")
            job.kept = None


async def shutdown(ctx: ProviderContext, *, cancel: bool, timeout_s: float) -> tuple[JobId, ...]:
    """``close()``: cancel queued and running jobs (partial files are deleted);
    returns the ids of the jobs cancelled."""
    reg = _reg(ctx)
    if not cancel:
        return ()
    tasks = []
    cancelled: list[JobId] = []
    for job in list(reg.jobs.values()):
        if job.state is OpState.QUEUED:
            job.state = OpState.CANCELLED
            cancelled.append(job.id)
            _emit(ctx, job)
        elif job.state in (OpState.RUNNING, OpState.CANCELLING) and job.task is not None:
            job.state = OpState.CANCELLING
            job.task.cancel()
            tasks.append(job.task)
            cancelled.append(job.id)
    if tasks:
        await asyncio.wait(tasks, timeout=timeout_s)
    return tuple(cancelled)


async def _worker(ctx: ProviderContext) -> None:
    reg = _reg(ctx)
    while not ctx.closing:
        job = next((j for j in reg.jobs.values() if j.state is OpState.QUEUED), None)
        if job is None:
            return
        job.state = OpState.RUNNING
        job.task = asyncio.ensure_future(_run(ctx, job))
        try:
            await asyncio.shield(job.task)
        except asyncio.CancelledError:
            if not job.task.done():  # the worker itself was cancelled (core closing)
                job.task.cancel()
                with contextlib.suppress(BaseException):
                    await job.task
                raise
        except Exception:
            _log.exception("render job crashed")


# ---------------------------------------------------------------- one job
@dataclass(slots=True)
class _Manifest:
    path: Path
    data: dict[str, Any]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(self.path, json.dumps(self.data, indent=1))

    def remove(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink()


def _index_cache(ctx: ProviderContext, src: Path) -> Path:
    st = src.stat()
    key = f"{os.fspath(src.resolve())}\0{st.st_size}\0{st.st_mtime_ns}"
    d = ctx.paths.cache_dir / "render" / "index"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha256(key.encode()).hexdigest()[:32] + ".ffindex")


def _fail(
    code: ErrorCode, cause: Msg, fix: Msg | None = None, detail: str | None = None
) -> ButterEyeError:
    return ButterEyeError(code, cause, fix, detail=detail)


def encode_key(
    src: Path,
    spec: RenderJobSpec,
    target: Fraction,
    size: tuple[int, int],
    lut: Path | None,
    profile: Profile | None = None,
) -> str:
    """What an encoded video track depends on, apart from the engine: the source
    file as it is now, the encoder, the rate, the size, the bit depth, the LUT and
    the profile's smoothing (model, scene-cut threshold, content, edge smoothing).
    A kept track (failed remux) is reused only for the same key."""
    st = src.stat()
    lut_id = None
    if lut is not None:
        with contextlib.suppress(OSError):
            lut_id = [os.fspath(lut), lut.stat().st_mtime_ns]
    data = {
        "source": [os.fspath(src.resolve()), st.st_size, st.st_mtime_ns],
        "encoder": spec.encoder,
        "target": str(target),
        "size": list(size),
        "ten_bit": spec.ten_bit,
        "lut": lut_id,
    }
    if profile is not None:  # keys a profile-less copy never had stay out of its key
        data["profile"] = [
            profile.model,
            profile.sc_threshold,
            profile.content.value,
            profile.aa,
        ]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32]


def _find_kept(
    ctx: ProviderContext, out: Path, key: str, engine: BackendId | None
) -> tuple[Path, Path, BackendId | None] | None:
    """A video track kept by an earlier job for this output and key: (its
    manifest, the track, the engine that made it)."""
    video, _part = pipeline.temp_paths(out)
    for mpath, data in _manifests(ctx):
        if not data.get("kept") or data.get("output") != os.fspath(out):
            continue
        if data.get("encode") != key or not video.is_file():
            continue
        try:
            made_by = BackendId(str(data.get("engine")))
        except ValueError:
            made_by = None
        if engine is not None and made_by is not None and made_by is not engine:
            continue
        return mpath, video, made_by
    return None


def _drop_kept_claims(ctx: ProviderContext, out: Path, own_manifest: Path) -> None:
    """This job is about to encode into ``out``'s video track, which other jobs
    may have kept after a failed remux. The path depends only on the output, so
    their claim no longer holds: their manifests go, so a later "same copy" does
    not finish around this job's track, and their rows stop offering to."""
    for mpath, data in _manifests(ctx):
        if mpath != own_manifest and data.get("kept") and data.get("output") == os.fspath(out):
            _remove(mpath)
    video, _part = pipeline.temp_paths(out)
    for other in _reg(ctx).jobs.values():
        if other.kept == video:
            other.kept = None


async def _run(ctx: ProviderContext, job: _Job) -> None:
    spec = job.spec
    jlog = filelog.for_item(_log, filelog.job_key(job.id))
    src, out = Path(spec.source), Path(spec.output)
    video_tmp, part = pipeline.temp_paths(out)
    paths = ctx.paths
    manifest = _Manifest(
        paths.jobs_dir / f"render-{job.id}.json",
        {
            "id": job.id,
            "pgid": None,
            "source": os.fspath(src),
            "output": os.fspath(out),
            "partials": [os.fspath(video_tmp), os.fspath(part)],
            "started": time.time(),
        },
    )
    script = paths.cache_dir / "render" / f"job-{job.id}.vpy"
    log_path = paths.logs_dir / f"render-{job.id}.log"
    t0 = time.monotonic()
    activity: int | None = None
    keep_video = False
    try:
        job.phase = RenderPhase.PROBE
        _emit(ctx, job)
        tools = _tools()
        info = await _source_info(tools, src)
        dec = await run((tools.ffmpeg, "-hide_banner", "-decoders"), timeout_s=10.0)
        refusal = refusal_for(
            info,
            rprobe.parse_decoders(dec.stdout) if dec.ok else frozenset(),
            _ffms2_lib().exists(),
        )
        if refusal is not None or info is None:
            raise RenderRefused(
                refusal.code
                if refusal is not None and refusal.code
                else ErrorCode.CODEC_NOT_DECODABLE,
                refusal.title if refusal is not None else Msg("No video stream."),
                refusal.fix if refusal is not None else None,
                commands=refusal.commands if refusal is not None else (),
            )
        hdr10 = rprobe.renders_as_hdr10(info)
        if hdr10 and spec.encoder not in rprobe.HDR10_ENCODERS:
            raise RenderRefused(
                ErrorCode.ENCODER_FAILED,
                Msg("{encoder} can't make an HDR10 copy.", {"encoder": spec.encoder}),
                Msg("Choose libx265 or libsvtav1."),
            )
        cfg = _config(ctx)
        profile = _profile(cfg, spec.profile_id)
        target = offline_target(
            spec.target or (profile.target if profile is not None else Target(TargetKind.X2)),
            info.fps,
        )
        if target <= info.fps:
            raise RenderRefused(
                ErrorCode.CODEC_NOT_DECODABLE,
                Msg("This video already plays at {fps} fps; choose Double (2×).",
                    {"fps": decide.fmt_rate(info.fps)}),
            )  # fmt: skip
        allowed = {(info.width, info.height), *decide.smaller_sizes(info.width, info.height)}
        size = tuple(spec.size) if spec.size is not None else (info.width, info.height)
        if size not in allowed:
            raise RenderRefused(
                ErrorCode.OUTPUT_EXISTS,
                Msg("A copy can't be made at {w} × {h}.", {"w": size[0], "h": size[1]}),
            )
        w, h = size[0], size[1]
        # the window's engine for this size, else the same choice by time it showed
        wanted: BackendId | None = spec.engine
        engine, mc = await _pick(ctx, profile, info, (w, h), target, wanted)
        if engine is None:
            raise _fail(
                ErrorCode.PKG_MISSING,
                Msg("No interpolation engine is installed."),
                Msg("Install ButterEye's plugins, then try again."),
            )
        lut_name, lut = _lut(ctx, cfg)
        if hdr10:
            lut = None  # a display-referred LUT would misread PQ values
        elif lut_name is not None and lut is None:
            jlog.info("%s: colour LUT %s not found; converting without it", src.name, lut_name)
        ten_bit = spec.ten_bit
        key = encode_key(src, spec, target, (w, h), lut, profile)
        kept = _find_kept(ctx, out, key, wanted)
        est = rprobe.estimate_bytes(
            spec.encoder, (w, h), target, info.duration_s, src.stat().st_size
        )
        if kept is not None:  # the track exists: only the finished file is new
            est = max(0, est - kept[1].stat().st_size)
        job.est_bytes = est
        free = shutil.disk_usage(out.parent).free
        if free < est:
            raise InsufficientSpace(
                ErrorCode.NO_SPACE,
                Msg("There isn't enough free space for this copy "
                    "(about {need} needed, {free} free).",
                    {"need": _gib(est), "free": _gib(free)}),
                Msg("Free some space or save the copy somewhere else."),
                need_bytes=est,
                free_bytes=free,
            )  # fmt: skip
        job.total = pipeline.target_frames(info, target)
        manifest.data["encode"] = key
        manifest.data["engine"] = engine.value
        manifest.save()
        # macOS: no idle sleep and no App Nap until the copy is saved (B12)
        activity = osapi.begin_activity(ACTIVITY_REASON)

        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            if kept is not None:
                _remove(kept[0])  # this job's manifest lists the track now
                for other in _reg(ctx).jobs.values():
                    if other.kept == kept[1]:
                        other.kept = None  # its Remove must not delete the track
                job.done = job.total
                jlog.info(
                    "%s: reusing the video converted before (%s); copying the other "
                    "streams again", src.name, os.fspath(kept[1]),
                )  # fmt: skip
            else:
                _drop_kept_claims(ctx, out, manifest.path)
                # ---- render: vspipe | ffmpeg in one process group
                job.phase = RenderPhase.RENDER
                _emit(ctx, job)
                script.parent.mkdir(parents=True, exist_ok=True)
                write_script(script, _constants(ctx, engine))
                trt_settings: dict[str, Any] | None = None
                if engine is BackendId.RIFE_TRT and mc is not None and mc.name is not None:
                    trt_settings = await _trt_engine(ctx, profile, mc.name, (w, h), script)
                params = FilterParams(
                    gen=1,
                    backend=engine,
                    src_fps=info.fps,
                    target_fps=target,
                    buffered_frames=4,
                    concurrent_frames=8,
                    matrix=rprobe.vs_matrix(info),
                    range=rprobe.vs_range(info),
                    model_path=mc.path if mc is not None else None,
                    gpu_id=(
                        _gpu_index(ctx)
                        if engine in (BackendId.RIFE_NCNN, BackendId.MVGPU)
                        else None
                    ),
                    gpu_thread=rife_gpu_thread() if engine is BackendId.RIFE_NCNN else None,
                    uhd=decide.uhd_mode(w, h),
                    sc_threshold=profile.sc_threshold if profile is not None else 0.12,
                    hold_fill=_hold_fill(engine, profile),
                    aa=profile.aa if profile is not None else False,
                    still_skip=True,
                    title=src.name,
                    size=(w, h) if (w, h) != (info.width, info.height) else None,
                    # offline keeps the finest settings: no real-time limit (§4.4)
                    dither="error_diffusion",
                    mv_pel=2,
                    trt=trt_settings,
                )
                ud: dict[str, Any] = json.loads(user_data(params))
                ud["source"] = {
                    "kind": "file",
                    "path": os.fspath(src),
                    "cache": os.fspath(_index_cache(ctx, src)),
                    "cfr": info.vfr,
                }
                jlog.info(
                    "%s: converting %s → %s fps with %s%s at %s × %s (video %s × %s), "
                    "%s%s%s → %s",
                    src.name, decide.fmt_rate(info.fps), decide.fmt_rate(target),
                    decide.BACKEND_NAMES.get(engine, engine.value),
                    f" {mc.name}" if mc is not None else "", w, h, info.width, info.height,
                    spec.encoder, " 10-bit" if ten_bit else "",
                    f" with LUT {lut.name}" if lut is not None else "", os.fspath(out),
                )  # fmt: skip
                await _encode(
                    ctx, job, tools, script, ud, info, video_tmp, manifest, log, t0,
                    requests=vspipe_requests(engine), ten_bit=ten_bit, lut=lut,
                )  # fmt: skip

            # ---- remux: every other stream of the source
            job.phase = RenderPhase.REMUX
            job.eta_s = None
            _emit(ctx, job)
            # from here on the encoded track is worth keeping if the remux fails
            keep_video = True
            await _remux(tools, video_tmp, src, part, info, log)

        job.phase = RenderPhase.CLEANUP
        _emit(ctx, job)
        if out.exists() and not spec.overwrite:
            raise RenderRefused(
                ErrorCode.OUTPUT_EXISTS,
                Msg("{name} appeared while converting.", {"name": out.name}),
            )
        os.replace(part, out)
        _remove(video_tmp)
        job.state = OpState.SUCCEEDED
        job.phase = None
        job.eta_s = 0.0
        if job.total is not None:
            job.done = job.total
        jlog.info(
            "%s: smooth copy saved in %.0f s → %s", src.name, time.monotonic() - t0, os.fspath(out)
        )
        _emit(ctx, job)
    except asyncio.CancelledError:
        _remove(video_tmp, part)
        job.state = OpState.CANCELLED
        job.phase = None
        job.eta_s = None
        jlog.info("%s: conversion cancelled; partial files deleted", src.name)
        _emit(ctx, job)
        if ctx.closing:
            raise
    except ButterEyeError as exc:
        kept_now = keep_video and exc.code is ErrorCode.REMUX_FAILED and video_tmp.is_file()
        if kept_now:
            # B2: hours of encoding are not thrown away for a remux that failed;
            # the manifest stays (marked kept) so the same copy, added again,
            # finishes from it, and a later start lists it as a partial file
            _remove(part)
            job.kept = video_tmp
            manifest.data["kept"] = True
            manifest.data["pgid"] = None
            with contextlib.suppress(OSError):
                manifest.save()
            exc = _kept_error(exc, video_tmp)
        else:
            _remove(video_tmp, part)
        job.state = OpState.FAILED
        job.error = exc
        job.eta_s = None
        jlog.info("%s: conversion failed (%s): %s", src.name, exc.code.code, exc.cause.key)
        if kept_now:
            jlog.info("%s:   the converted video is kept: %s", src.name, os.fspath(video_tmp))
        if exc.detail:
            for line in exc.detail.splitlines()[-10:]:
                jlog.info("%s:   %s", src.name, line)
        _emit(ctx, job)
    except Exception as exc:
        _remove(video_tmp, part)
        jlog.exception("conversion crashed")
        job.state = OpState.FAILED
        job.error = ButterEyeError(
            ErrorCode.INTERNAL, Msg("Converting failed unexpectedly."), detail=repr(exc)
        )
        _emit(ctx, job)
    finally:
        osapi.end_activity(activity)
        if job.kept is None:
            manifest.remove()
        cfg_now = _config(ctx)
        if cfg_now is not None and not cfg_now.general.keep_logs:  # opted out of logs
            with contextlib.suppress(OSError):
                log_path.unlink()
        with contextlib.suppress(OSError):
            script.unlink()


def _kept_error(exc: ButterEyeError, video: Path) -> ButterEyeError:
    """The remux failure, saying the converted video is kept and how to finish."""
    return ButterEyeError(
        exc.code,
        exc.cause,
        Msg("The converted video is kept. Add the same copy again to finish it without "
            "converting again; installing mkvtoolnix may help."),
        detail=exc.detail,
    )  # fmt: skip


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GB"


def _remove(*paths: Path) -> None:
    for p in paths:
        with contextlib.suppress(OSError):
            p.unlink()


def _gpu_index(ctx: ProviderContext) -> int | None:
    from buttereye.core.bench import runner

    with contextlib.suppress(Exception):
        return runner._gpu(ctx)[1]
    return None


def _signal_group(pgid: int, stop: bool) -> None:
    """SIGSTOP (``stop``) or SIGCONT to a render's group; nothing on Windows."""
    if sys.platform == "win32":
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGSTOP if stop else signal.SIGCONT)


async def _gpu_wanted_elsewhere(ctx: ProviderContext) -> str | None:
    """Why a render should stop for now: ``"playback"`` while a video plays in
    ButterEye, ``"benchmark"`` while one runs (its result would measure the
    render too, and is kept), else None."""
    sessions = capabilities.resolve(_LIVE_SESSIONS)
    if sessions is not None:
        try:
            if await sessions(ctx):
                return "playback"
        except Exception:
            _log.debug("render: live sessions unreadable", exc_info=True)
    from buttereye.core.bench import runner

    with contextlib.suppress(Exception):
        if runner._state(ctx).lock.locked():
            return "benchmark"
    return None


@dataclass(slots=True)
class _Pause:
    """SIGSTOP / SIGCONT of a render's process group (B11); POSIX only."""

    pgid: int
    since: float | None = None  # monotonic time it stopped, None = running
    total_s: float = 0.0  # time spent stopped before ``since``

    def stop(self) -> None:
        if self.since is None:
            _signal_group(self.pgid, True)
            self.since = time.monotonic()

    def resume(self) -> None:
        if self.since is not None:
            _signal_group(self.pgid, False)
            self.total_s += time.monotonic() - self.since
            self.since = None

    def stopped_s(self) -> float:
        now = time.monotonic()
        return self.total_s + (now - self.since if self.since is not None else 0.0)


async def _pause_while_busy(ctx: ProviderContext, job: _Job, pause: _Pause, jlog: Any) -> None:
    """Stop the group while playback or a benchmark runs, continue after."""
    while True:
        why = await _gpu_wanted_elsewhere(ctx)
        if why is not None and pause.since is None:
            pause.stop()
            job.paused = True
            job.eta_s = None
            jlog.info("%s: paused while %s runs", _title(job), why)
            _emit(ctx, job, progress=True)
        elif why is None and pause.since is not None:
            pause.resume()
            job.paused = False
            jlog.info("%s: continuing", _title(job))
        await asyncio.sleep(PAUSE_POLL_S)


#: vspipe's return code when the encoder closed the pipe under it
if sys.platform == "win32":
    _BROKEN_PIPE_RC = -13
else:
    _BROKEN_PIPE_RC = -int(signal.SIGPIPE)


def _failure(
    vs_rc: int, ff_rc: int, vs_first: bool, encoder: str, vs_lines: Sequence[str], ff_err: str
) -> ButterEyeError | None:
    """Which side of ``vspipe | ffmpeg`` failed (B18). An encoder that dies first
    (a full disk, an option this build lacks) takes vspipe down with it (a broken
    pipe), which is the encoder's failure, not the smoothing script's."""
    encoder_error = _fail(
        ErrorCode.ENCODER_FAILED,
        Msg("The {encoder} encoder stopped with an error.", {"encoder": encoder}),
        Msg("Choose another format and try again."),
        detail=pipeline.tail(ff_err.splitlines()),
    )
    if ff_rc != 0 and (vs_rc == 0 or not vs_first or vs_rc == _BROKEN_PIPE_RC):
        return encoder_error
    if vs_rc != 0:
        return _fail(
            ErrorCode.VSPIPE_FRAME_ERROR,
            Msg("The smoothing script failed while converting."),
            Msg("Try another smoothness setting, or check the log."),
            detail=pipeline.tail(vs_lines),
        )
    return None


async def _encode(
    ctx: ProviderContext,
    job: _Job,
    tools: _Tools,
    script: Path,
    ud: Mapping[str, Any],
    info: rprobe.SourceInfo,
    video_tmp: Path,
    manifest: _Manifest,
    log: Any,
    t0: float,
    *,
    requests: int | None = None,
    ten_bit: bool = False,
    lut: Path | None = None,
) -> None:
    rfd, wfd = os.pipe()
    vs_proc: asyncio.subprocess.Process | None = None
    ff_proc: asyncio.subprocess.Process | None = None
    vs_lines: list[str] = []
    awake = None
    pause: _Pause | None = None
    watcher: asyncio.Task[None] | None = None
    jlog = filelog.for_item(_log, filelog.job_key(job.id))
    try:
        # vspipe leads a new process group that ffmpeg joins (a group can only be
        # joined from the same session, so no new session here); cancel and the
        # stale-job check signal the whole group. Windows has no joinable groups:
        # each gets its own, below normal priority from the start, both inside
        # ButterEye's kill-on-close job, and ffmpeg is ended on its own below.
        vs_group: dict[str, Any] = (
            osapi.group_spawn_kwargs(low_priority=True) if osapi.IS_WINDOWS
            else {"process_group": 0}
        )  # fmt: skip
        vs_proc = await asyncio.create_subprocess_exec(
            *pipeline.vspipe_argv(tools.vspipe, script, ud, requests=requests),
            stdin=subprocess.DEVNULL,
            stdout=wfd,
            stderr=subprocess.PIPE,
            **vs_group,
        )
        _own_group(vs_proc.pid)
        ff_group: dict[str, Any] = (
            osapi.group_spawn_kwargs(low_priority=True) if osapi.IS_WINDOWS
            else {"process_group": vs_proc.pid}
        )  # fmt: skip
        ff_proc = await asyncio.create_subprocess_exec(
            *pipeline.encode_argv(
                tools.ffmpeg, job.spec.encoder, info, video_tmp, ten_bit=ten_bit, lut=lut
            ),
            stdin=rfd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **ff_group,
        )
        os.close(rfd)
        os.close(wfd)
        rfd = wfd = -1
        manifest.data["pgid"] = vs_proc.pid
        manifest.save()
        # §7.6: a render is a batch job; playback, the GUI and the desktop come
        # first whether they start before or after it (threads started later
        # inherit). That is CPU time only; the GPU is shared by stopping (below)
        osapi.lower_group_priority(vs_proc.pid, RENICE)
        # macOS: no idle sleep while vspipe runs, even if ButterEye dies (B12)
        awake = osapi.keep_awake(vs_proc.pid)
        if not osapi.IS_WINDOWS:
            pause = _Pause(vs_proc.pid)
            watcher = asyncio.ensure_future(_pause_while_busy(ctx, job, pause, jlog))

        async def read_ffmpeg() -> str:
            assert ff_proc is not None and ff_proc.stderr is not None
            return (await ff_proc.stderr.read()).decode("utf-8", "replace")

        exited: list[str] = []  # which of the two ended first (B18)

        async def wait(name: str, proc: asyncio.subprocess.Process) -> int:
            rc = await proc.wait()
            exited.append(name)
            return rc

        ff_err_task = asyncio.ensure_future(read_ffmpeg())
        vs_wait = asyncio.ensure_future(wait("vspipe", vs_proc))
        ff_wait = asyncio.ensure_future(wait("ffmpeg", ff_proc))
        assert vs_proc.stderr is not None
        buf = ""
        while True:
            chunk = await vs_proc.stderr.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            *done_lines, buf = buf.replace("\r", "\n").split("\n")
            for line in done_lines:
                if line.startswith("Frame:"):
                    continue
                if line.strip():
                    vs_lines.append(line)
                    log.write(line + "\n")
            prog = pipeline.parse_progress("\n".join(done_lines) + "\n" + buf)
            if prog is not None:
                job.done = min(prog.done, prog.total)
                job.total = prog.total
                stopped = pause.stopped_s() if pause is not None else 0.0
                rate = job.done / max(0.001, time.monotonic() - t0 - stopped)
                # vspipe's own fps counts the time it was stopped too
                job.fps = round(rate, 1) if stopped else (prog.fps or round(rate, 1))
                job.eta_s = (prog.total - job.done) / job.fps if job.fps else None
                _emit(ctx, job, progress=True)
        vs_rc = await vs_wait
        ff_rc = await ff_wait
        ff_err = await ff_err_task
        if ff_err.strip():
            log.write(ff_err)
        log.flush()
        err = _failure(vs_rc, ff_rc, exited[:1] == ["vspipe"], job.spec.encoder, vs_lines, ff_err)
        if err is not None:
            raise err
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(BaseException):
                await watcher
        if pause is not None:
            pause.resume()  # a stopped process acts on SIGTERM only once continued
            job.paused = False
        if awake is not None:
            with contextlib.suppress(OSError):
                awake.terminate()
        for fd in (rfd, wfd):
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
        if vs_proc is not None and (
            vs_proc.returncode is None or (ff_proc and ff_proc.returncode is None)
        ):
            await asyncio.shield(terminate_group(vs_proc))
            if ff_proc is not None and ff_proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    ff_proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ff_proc.wait(), 2.0)


async def _remux(
    tools: _Tools, video: Path, src: Path, part: Path, info: rprobe.SourceInfo, log: Any
) -> None:
    if tools.mkvmerge is not None:
        argv = pipeline.mkvmerge_argv(
            tools.mkvmerge, video, src, part, start_s=info.start_s, src=info
        )
    else:
        argv = pipeline.ffmpeg_remux_argv(
            tools.ffmpeg, video, src, part, start_s=info.start_s, src=info
        )
    res = await run(
        pipeline.low_priority(argv, nice=shutil.which("nice"), ionice=shutil.which("ionice"),
                              level=RENICE, taskpolicy=_taskpolicy()),
        timeout_s=REMUX_TIMEOUT_S,
    )  # fmt: skip
    text = (res.stdout or "") + (res.stderr or "")
    if text.strip():
        log.write(text)
    # mkvmerge exits 1 for warnings, 2 for errors
    ok = res.returncode == 0 or (tools.mkvmerge is not None and res.returncode == 1)
    if not ok or not part.exists():
        raise _fail(
            ErrorCode.REMUX_FAILED,
            Msg("Copying the audio and subtitles into the new file failed."),
            Msg("Check the log; installing mkvtoolnix may help."),
            detail=pipeline.tail(text.splitlines()),
        )


def _taskpolicy() -> str | None:
    """macOS's ``taskpolicy`` (always in /usr/sbin there), else None."""
    if not osapi.IS_MACOS:
        return None
    path = Path("/usr/sbin/taskpolicy")
    return os.fspath(path) if path.is_file() else None


# ---------------------------------------------------------------- stale jobs
def _manifests(ctx: ProviderContext) -> list[tuple[Path, Mapping[str, Any]]]:
    out: list[tuple[Path, Mapping[str, Any]]] = []
    d = ctx.paths.jobs_dir
    if not d.is_dir():
        return out
    for p in sorted(d.glob("render-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            out.append((p, data))
    return out


def _group_alive(pgid: int | None) -> bool:
    return osapi.group_alive(pgid)


async def stale_jobs(ctx: ProviderContext) -> tuple[StaleJob, ...]:
    """Jobs whose manifest survives with no job of this core behind it (a crashed GUI)."""
    live = set(_reg(ctx).jobs)
    out = []
    for _p, data in _manifests(ctx):
        jid = JobId(str(data.get("id", "")))
        if jid in live:
            continue
        pgid = data.get("pgid") if isinstance(data.get("pgid"), int) else None
        partials = tuple(Path(x) for x in data.get("partials") or () if Path(x).exists())
        out.append(StaleJob(jid, pgid, _group_alive(pgid), partials))
    return tuple(out)


async def stale_stop(ctx: ProviderContext, job_id: JobId) -> None:
    """SIGTERM the stale job's group (SIGKILL after 3 s), delete its partial files."""
    for p, data in _manifests(ctx):
        if str(data.get("id")) != job_id:
            continue
        pgid = data.get("pgid") if isinstance(data.get("pgid"), int) else None
        if pgid and _group_alive(pgid):
            osapi.signal_group(pgid, kill=False)
            for _ in range(30):
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.1)
            if _group_alive(pgid):
                osapi.signal_group(pgid, kill=True)
        _remove(*(Path(x) for x in data.get("partials") or ()))
        _remove(p)


__all__ = [
    "probe",
    "enqueue",
    "jobs",
    "busy",
    "move",
    "cancel",
    "forget",
    "stale_jobs",
    "stale_stop",
    "shutdown",
    "bench_rate",
    "offline_target",
    "vspipe_requests",
    "refusal_for",
]
