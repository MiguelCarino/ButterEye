# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Render provider: probe, a sequential queue, cancel, stale jobs (SCOPE §7, §7.8;
docs/design/GUI.md §2.5, §12.7).

One job at a time. A job: ffprobe the source → write the live script with a file
source (ffms2, spike M0(e)) → ``vspipe | ffmpeg`` into a hidden video-only MKV next
to the output → ``mkvmerge`` (or ``ffmpeg``) copies every other stream of the
source → rename into place. vspipe and ffmpeg share one process group, recorded in
a manifest under ``jobs_dir`` so a job left by a crashed GUI can be found and
stopped (``stale_jobs``). Cancel and failure delete every partial file.

SDR first (owner decision 2026-10-08): HDR sources are refused with a plain
explanation until HDR10 render lands (§7.4).
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
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buttereye.core import capabilities
from buttereye.core.capabilities import ProviderRef
from buttereye.core.doctor.checks_pkgs import parse_encoders
from buttereye.core.doctor.proc import run
from buttereye.core.errors import ButterEyeError, ErrorCode, InsufficientSpace, RenderRefused
from buttereye.core.events import JobChanged
from buttereye.core.mpvctl import decide
from buttereye.core.ops import _own_group, terminate_group
from buttereye.core.render import pipeline
from buttereye.core.render import probe as rprobe
from buttereye.core.scriptgen.generator import (
    RIFE_CONCURRENT_FRAMES_DEFAULT,
    RIFE_GPU_THREAD_DEFAULT,
    FilterParams,
    ScriptConstants,
    user_data,
    write_atomic,
    write_script,
)
from buttereye.core.types import (
    BackendId,
    BenchResult,
    Config,
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
INSTALL_FFMS2 = "sudo dnf install ffms2"
INSTALL_MKVTOOLNIX = "sudo dnf install mkvtoolnix"
PROGRESS_INTERVAL_S = 0.5


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


def _missing(what: str, command: str) -> ButterEyeError:
    return ButterEyeError(
        ErrorCode.PKG_MISSING,
        Msg("{tool} is not installed, so videos can't be converted.", {"tool": what}),
        Msg("Install it, then try again."),
        commands=(command,),
    )


def _tools() -> _Tools:
    ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
    vspipe = shutil.which("vspipe")
    if ffprobe is None or ffmpeg is None:
        raise _missing("ffmpeg", "sudo dnf install ffmpeg-free")
    if vspipe is None:
        raise _missing("vspipe", "sudo dnf install vapoursynth-tools")
    return _Tools(ffprobe, ffmpeg, vspipe, shutil.which("mkvmerge"))


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


def _refusal(code: ErrorCode, title: str, cause: str, fix: str, **params: str) -> Finding:
    return Finding(
        id="render.refusal",
        section=Section.RENDER,
        severity=Severity.BLOCKING,
        code=code,
        title=Msg(title, params),
        cause=Msg(cause, params),
        fix=Msg(fix, params),
        commands=(INSTALL_FFMS2,) if code is ErrorCode.FFMS2_MISSING else (),
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
    if info.hdr_class in (HdrClass.HLG, HdrClass.DV):
        return _refusal(
            ErrorCode.HDR_CLASS_REFUSED,
            "HLG and Dolby Vision videos can't be converted",
            "Only SDR video can be converted in this version.",
            "Choose an SDR video.",
        )
    if info.hdr_class is not HdrClass.SDR:
        return _refusal(
            ErrorCode.HDR_CLASS_REFUSED,
            "HDR videos can't be converted yet",
            "This build converts SDR video; HDR10 comes in a later one.",
            "Choose an SDR video.",
        )
    if info.width <= 0 or info.height <= 0 or info.duration_s <= 0:
        return _refusal(
            ErrorCode.CODEC_NOT_DECODABLE,
            "ButterEye couldn't read this video's size or length",
            "ffprobe reported no frame size or duration.",
            "Try remuxing the file, then convert again.",
        )
    return None


def _ffms2_lib() -> Path:
    return ScriptConstants().ffms2_lib


def _constants(ctx: ProviderContext) -> ScriptConstants:
    return ScriptConstants(ctx.paths.rpm_plugin_dir, _ffms2_lib())


# ---------------------------------------------------------------- profile, engine, estimates
def _config(ctx: ProviderContext) -> Config | None:
    try:
        return ctx.config()
    except ButterEyeError:
        return None


def _profile(cfg: Config | None, profile_id: str | None) -> Profile | None:
    if cfg is None or not cfg.profiles:
        return None
    by_id = {p.id: p for p in cfg.profiles}
    if profile_id is not None and profile_id in by_id:
        return by_id[profile_id]
    return by_id.get("simple") or cfg.profiles[0]


def vspipe_requests(engine: BackendId) -> int | None:
    """vspipe's frame requests in flight for a render. RIFE-ncnn runs only
    ``gpu_thread`` frames at once; vspipe's default (one per CPU thread, 24 on the
    dev box) only holds RGBS frames waiting for the GPU: 4K 24 → 60 used 18.9 GB
    and ran 7.3 fps by default, 14.5 GB and 7.7 fps with 8. At least
    gpu_thread + 2. MVTools (CPU-bound) keeps vspipe's default."""
    if engine is BackendId.RIFE_NCNN:
        return max(RIFE_CONCURRENT_FRAMES_DEFAULT, RIFE_GPU_THREAD_DEFAULT + 2)
    return None


def offline_target(target: Target, src_fps: Fraction) -> Fraction:
    """§7.8: 2× and a fixed rate as asked; a display target renders as 2×."""
    if target.kind is TargetKind.FPS and target.fps is not None:
        return target.fps
    return src_fps * 2


def _installed(ctx: ProviderContext) -> frozenset[BackendId]:
    p = ctx.paths
    return decide.installed_backends(
        p.rpm_plugin_dir, p.rpm_data_dir / MODEL_SUBDIR, (p.data_dir / USER_MODEL_SUBDIR,)
    )


def _engine(
    ctx: ProviderContext, profile: Profile | None
) -> tuple[BackendId | None, decide.ModelChoice | None]:
    backend = profile.backend if profile is not None else "auto"
    engine = rprobe.engine_for(backend, _installed(ctx))
    if engine is not BackendId.RIFE_NCNN:
        return engine, None
    p = ctx.paths
    mc = decide.find_model(
        profile.model if profile is not None else None,
        p.rpm_data_dir / MODEL_SUBDIR,
        (p.data_dir / USER_MODEL_SUBDIR,),
    )
    if mc.path is None:
        return (BackendId.MVTOOLS if BackendId.MVTOOLS in _installed(ctx) else None), None
    return engine, mc


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
    from buttereye.core.mpvctl.session import _measured_in_mpv, _measured_size

    px = size[0] * size[1]
    if px <= 0:
        return None
    found: list[tuple[float, float, float]] = []
    for result in results:
        when = result.when.timestamp() if result.when is not None else -math.inf
        for m in result.measurements:
            if m.backend is not backend or (model is not None and m.model != model):
                continue
            if m.gpu_faults or not _measured_in_mpv(m) or not m.mpv_fps > 0:
                continue
            w, h = _measured_size(m.label, result.request)
            if w <= 0 or h <= 0:
                continue
            ratio = (w * h) / px
            req = result.request
            fps = decide.as_2x_rate(float(m.mpv_fps), req.source_fps, req.target_fps)
            found.append((round(-abs(math.log(ratio)), 6), when, fps * ratio))
    return max(found)[2] if found else None


# ---------------------------------------------------------------- provider: probe
async def probe(ctx: ProviderContext, src: Path, *, profile_id: str | None = None) -> RenderProbe:
    tools = _tools()
    src = Path(src)
    data = await _ffprobe(tools, src)
    info = rprobe.source_info(data)
    dec = await run((tools.ffmpeg, "-hide_banner", "-decoders"), timeout_s=10.0)
    enc = await run((tools.ffmpeg, "-hide_banner", "-encoders"), timeout_s=10.0)
    decoders = rprobe.parse_decoders(dec.stdout) if dec.ok else frozenset()
    report = ctx.last_report()
    nvidia = report is None or any(g.vendor == "NVIDIA" for g in report.hardware.gpus)
    encoders = rprobe.usable_encoders(parse_encoders(enc.stdout) if enc.ok else (), nvidia=nvidia)
    refusal = refusal_for(info, decoders, _ffms2_lib().exists())
    if refusal is None and not encoders:
        refusal = _refusal(
            ErrorCode.ENCODER_FAILED,
            "Your ffmpeg has no encoder ButterEye can use",
            "ffmpeg lists none of HEVC (NVENC or x265), AV1 (NVENC or SVT-AV1) or x264.",
            "Install ffmpeg-free or RPM Fusion's ffmpeg.",
        )
    warnings: list[Finding] = []
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
    sizes: tuple[Any, ...] = ()
    if info is not None and info.width > 0 and info.height > 0:
        engine, mc = _engine(ctx, _profile(_config(ctx), profile_id))
        results = await _bench_results(ctx) if engine is not None else ()
        model = mc.name if mc is not None else None
        rate_at = {
            (w, h): bench_rate(results, engine, model, (w, h)) if engine is not None else None
            for w, h in ((info.width, info.height), *decide.smaller_sizes(info.width, info.height))
        }
        sizes = rprobe.render_sizes(info.width, info.height, rate_at)
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
    _log.info("%s: queued a smooth copy → %s", _title(job), os.fspath(spec.output))
    _emit(ctx, job)
    if reg.worker is None or reg.worker.done():
        reg.worker = ctx.spawn(_worker(ctx), name="render-worker")
    return job.id


async def jobs(ctx: ProviderContext) -> tuple[RenderJobState, ...]:
    return tuple(_snapshot(j) for j in _reg(ctx).jobs.values())


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
    job = _get(ctx, job_id)
    if job.state in (OpState.SUCCEEDED, OpState.FAILED, OpState.CANCELLED):
        _reg(ctx).jobs.pop(job_id, None)
        ctx.forget_coalesced(f"render-{job_id}")


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


async def _run(ctx: ProviderContext, job: _Job) -> None:
    spec = job.spec
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
    try:
        job.phase = RenderPhase.PROBE
        _emit(ctx, job)
        tools = _tools()
        info = rprobe.source_info(await _ffprobe(tools, src))
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
        engine, mc = _engine(ctx, profile)
        if engine is None:
            raise _fail(
                ErrorCode.PKG_MISSING,
                Msg("No interpolation engine is installed."),
                Msg("Install ButterEye's plugins, then try again."),
            )
        allowed = {(info.width, info.height), *decide.smaller_sizes(info.width, info.height)}
        size = tuple(spec.size) if spec.size is not None else (info.width, info.height)
        if size not in allowed:
            raise RenderRefused(
                ErrorCode.OUTPUT_EXISTS,
                Msg("A copy can't be made at {w} × {h}.", {"w": size[0], "h": size[1]}),
            )
        w, h = size[0], size[1]
        est = rprobe.estimate_bytes(
            spec.encoder, (w, h), target, info.duration_s, src.stat().st_size
        )
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
        manifest.save()

        # ---- render: vspipe | ffmpeg in one process group
        job.phase = RenderPhase.RENDER
        _emit(ctx, job)
        script.parent.mkdir(parents=True, exist_ok=True)
        write_script(script, _constants(ctx))
        params = FilterParams(
            gen=1,
            backend=engine,
            src_fps=info.fps,
            target_fps=target,
            buffered_frames=4,
            concurrent_frames=8,
            matrix=None,
            range=None,
            model_path=mc.path if mc is not None else None,
            gpu_id=_gpu_index(ctx) if engine is BackendId.RIFE_NCNN else None,
            gpu_thread=RIFE_GPU_THREAD_DEFAULT if engine is BackendId.RIFE_NCNN else None,
            uhd=decide.uhd_mode(w, h),
            sc_threshold=(profile.sc_threshold if profile is not None else 0.12)
            if engine is BackendId.RIFE_NCNN
            else None,
            title=src.name,
            size=(w, h) if (w, h) != (info.width, info.height) else None,
            # offline keeps the finest settings: no real-time limit (§4.4)
            dither="error_diffusion",
            mv_pel=2,
        )
        ud: dict[str, Any] = json.loads(user_data(params))
        ud["source"] = {
            "kind": "file",
            "path": os.fspath(src),
            "cache": os.fspath(_index_cache(ctx, src)),
            "cfr": info.vfr,
        }
        _log.info(
            "%s: converting %s → %s fps with %s%s at %s × %s (video %s × %s), %s → %s",
            src.name, decide.fmt_rate(info.fps), decide.fmt_rate(target),
            decide.BACKEND_NAMES.get(engine, engine.value),
            f" {mc.name}" if mc is not None else "", w, h, info.width, info.height,
            spec.encoder, os.fspath(out),
        )  # fmt: skip
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as log:
            await _encode(
                ctx, job, tools, script, ud, info, video_tmp, manifest, log, t0,
                requests=vspipe_requests(engine),
            )  # fmt: skip

            # ---- remux: every other stream of the source
            job.phase = RenderPhase.REMUX
            job.eta_s = None
            _emit(ctx, job)
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
        _log.info(
            "%s: smooth copy saved in %.0f s → %s", src.name, time.monotonic() - t0, os.fspath(out)
        )
        _emit(ctx, job)
    except asyncio.CancelledError:
        _remove(video_tmp, part)
        job.state = OpState.CANCELLED
        job.phase = None
        job.eta_s = None
        _log.info("%s: conversion cancelled; partial files deleted", src.name)
        _emit(ctx, job)
        if ctx.closing:
            raise
    except ButterEyeError as exc:
        _remove(video_tmp, part)
        job.state = OpState.FAILED
        job.error = exc
        job.eta_s = None
        _log.info("%s: conversion failed (%s): %s", src.name, exc.code.code, exc.cause.key)
        if exc.detail:
            for line in exc.detail.splitlines()[-10:]:
                _log.info("%s:   %s", src.name, line)
        _emit(ctx, job)
    except Exception as exc:
        _remove(video_tmp, part)
        _log.exception("conversion crashed")
        job.state = OpState.FAILED
        job.error = ButterEyeError(
            ErrorCode.INTERNAL, Msg("Converting failed unexpectedly."), detail=repr(exc)
        )
        _emit(ctx, job)
    finally:
        manifest.remove()
        with contextlib.suppress(OSError):
            script.unlink()


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
) -> None:
    rfd, wfd = os.pipe()
    vs_proc: asyncio.subprocess.Process | None = None
    ff_proc: asyncio.subprocess.Process | None = None
    vs_lines: list[str] = []
    try:
        # vspipe leads a new process group that ffmpeg joins (a group can only be
        # joined from the same session, so no new session here); cancel and the
        # stale-job check signal the whole group
        vs_proc = await asyncio.create_subprocess_exec(
            *pipeline.vspipe_argv(tools.vspipe, script, ud, requests=requests),
            stdin=subprocess.DEVNULL,
            stdout=wfd,
            stderr=subprocess.PIPE,
            process_group=0,
        )
        _own_group(vs_proc.pid)
        ff_proc = await asyncio.create_subprocess_exec(
            *pipeline.encode_argv(tools.ffmpeg, job.spec.encoder, info, video_tmp),
            stdin=rfd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            process_group=vs_proc.pid,
        )
        os.close(rfd)
        os.close(wfd)
        rfd = wfd = -1
        manifest.data["pgid"] = vs_proc.pid
        manifest.save()
        # §7.6: a render is a batch job; playback, the GUI and the desktop come
        # first whether they start before or after it (threads started later inherit)
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PGRP, vs_proc.pid, RENICE)

        async def read_ffmpeg() -> str:
            assert ff_proc is not None and ff_proc.stderr is not None
            return (await ff_proc.stderr.read()).decode("utf-8", "replace")

        ff_err_task = asyncio.ensure_future(read_ffmpeg())
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
                rate = job.done / max(0.001, time.monotonic() - t0)
                job.fps = prog.fps or round(rate, 1)
                job.eta_s = (prog.total - job.done) / job.fps if job.fps else None
                _emit(ctx, job, progress=True)
        vs_rc = await vs_proc.wait()
        ff_rc = await ff_proc.wait()
        ff_err = await ff_err_task
        if ff_err.strip():
            log.write(ff_err)
        log.flush()
        if vs_rc != 0:
            raise _fail(
                ErrorCode.VSPIPE_FRAME_ERROR,
                Msg("The smoothing script failed while converting."),
                Msg("Try another smoothness setting, or check the log."),
                detail=pipeline.tail(vs_lines),
            )
        if ff_rc != 0:
            raise _fail(
                ErrorCode.ENCODER_FAILED,
                Msg("The {encoder} encoder stopped with an error.", {"encoder": job.spec.encoder}),
                Msg("Choose another format and try again."),
                detail=pipeline.tail(ff_err.splitlines()),
            )
    finally:
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
        argv = pipeline.mkvmerge_argv(tools.mkvmerge, video, src, part, start_s=info.start_s)
    else:
        argv = pipeline.ffmpeg_remux_argv(tools.ffmpeg, video, src, part, start_s=info.start_s)
    res = await run(
        pipeline.low_priority(argv, nice=shutil.which("nice"), ionice=shutil.which("ionice"),
                              level=RENICE),
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
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGTERM)
            for _ in range(30):
                if not _group_alive(pgid):
                    break
                await asyncio.sleep(0.1)
            if _group_alive(pgid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, signal.SIGKILL)
        _remove(*(Path(x) for x in data.get("partials") or ()))
        _remove(p)


__all__ = [
    "probe",
    "enqueue",
    "jobs",
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
