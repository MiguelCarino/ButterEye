# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A render job's process handling without vspipe or a GPU: stopping for
playback (B11), staying awake (B12), which side failed (B18), the encoded video
kept after a failed remux (B2), the VideoToolbox test encode (P2). ``sh`` stands
in for vspipe and ffmpeg."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import subprocess
import sys
from collections.abc import Callable, Coroutine
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.doctor.proc import CmdResult
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.profiles import defaults
from buttereye.core.render import jobs, pipeline
from buttereye.core.render import probe as rp
from buttereye.core.types import (
    BackendId,
    JobId,
    Msg,
    OpState,
    Paths,
    RenderJobSpec,
    RenderPhase,
)

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")


class Ctx:
    """The parts of ``ProviderContext`` a render uses."""

    def __init__(self, root: Path) -> None:
        self.paths = Paths(*(root / n for n in (
            "config.toml", "inc.conf", "mpv.conf", "data", "cache", "state", "logs", "jobs",
            "bench.json", "run",
        )))  # fmt: skip
        self.closing = False
        self.events: list[Any] = []
        self._state: dict[str, Any] = {}
        self.cfg = defaults.default_config()

    def config(self) -> Any:
        return self.cfg

    def last_report(self) -> None:
        return None

    def emit(self, ev: Any) -> None:
        self.events.append(ev)

    def emit_coalesced(self, key: str, ev: Any, *, min_interval_s: float = 0.25) -> None:
        self.events.append(ev)

    def forget_coalesced(self, key: str) -> None:
        pass

    def state(self, key: str, factory: Callable[[], Any]) -> Any:
        return self._state.setdefault(key, factory())

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        return asyncio.ensure_future(coro)


def _info() -> rp.SourceInfo:
    info = rp.source_info({
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
                     "height": 1080, "pix_fmt": "yuv420p", "r_frame_rate": "24/1",
                     "avg_frame_rate": "24/1"}],
        "format": {"duration": "10"},
    })  # fmt: skip
    assert info is not None
    return info


def _state(pid: int) -> str:
    return subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()


# ---------------------------------------------------------------- B11: stop for playback
@posix
async def test_pause_stops_and_continues_the_whole_group() -> None:
    proc = await asyncio.create_subprocess_exec("sleep", "30", process_group=0)
    try:
        pause = jobs._Pause(proc.pid)
        pause.stop()
        await asyncio.sleep(0.1)
        assert _state(proc.pid).startswith("T")  # stopped
        assert pause.stopped_s() > 0
        pause.resume()
        await asyncio.sleep(0.1)
        assert not _state(proc.pid).startswith("T")
        pause.resume()  # twice is harmless
        assert pause.since is None and pause.total_s > 0
    finally:
        proc.kill()
        await proc.wait()


async def test_the_render_waits_for_playback_and_benchmarks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    why: list[str | None] = ["playback"]

    async def wanted(_ctx: Any) -> str | None:
        return why[0]

    signals: list[str] = []

    class FakePause(jobs._Pause):
        def stop(self) -> None:
            signals.append("stop")
            super().stop()

        def resume(self) -> None:
            signals.append("cont")
            super().resume()

    monkeypatch.setattr(jobs, "_gpu_wanted_elsewhere", wanted)
    monkeypatch.setattr(jobs, "_signal_group", lambda pgid, sig: None)
    monkeypatch.setattr(jobs, "PAUSE_POLL_S", 0.01)
    spec = RenderJobSpec(tmp_path / "a.mkv", tmp_path / "a.smooth.mkv", "simple", "libx265")
    job = jobs._Job(JobId("j"), spec, state=OpState.RUNNING, eta_s=60.0)
    jobs._reg(ctx).jobs[job.id] = job
    pause = FakePause(4242)
    task = asyncio.ensure_future(jobs._pause_while_busy(ctx, job, pause, jobs._log))
    await asyncio.sleep(0.05)
    assert signals == ["stop"] and job.paused and job.eta_s is None
    assert not await jobs.busy(ctx)  # a stopped render doesn't count against a benchmark
    why[0] = "benchmark"
    await asyncio.sleep(0.05)
    assert signals == ["stop"]  # still stopped, not stopped twice
    why[0] = None
    await asyncio.sleep(0.05)
    assert signals == ["stop", "cont"] and not job.paused
    assert await jobs.busy(ctx)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_what_a_render_stops_for(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from buttereye.core import capabilities
    from buttereye.core.bench import runner

    ctx: Any = Ctx(tmp_path)
    live: list[object] = []

    async def sessions(_ctx: Any) -> tuple[object, ...]:
        return tuple(live)

    monkeypatch.setattr(capabilities, "resolve", lambda ref: sessions)
    assert await jobs._gpu_wanted_elsewhere(ctx) is None
    live.append(object())
    assert await jobs._gpu_wanted_elsewhere(ctx) == "playback"
    live.clear()
    async with runner._state(ctx).lock:
        assert await jobs._gpu_wanted_elsewhere(ctx) == "benchmark"


# ---------------------------------------------------------------- _encode with sh
def _tools() -> Any:
    return jobs._Tools("sh", "sh", "sh", None)


async def _encode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vspipe: str, ffmpeg: str
) -> tuple[jobs._Job, list[int]]:
    ctx: Any = Ctx(tmp_path)
    monkeypatch.setattr(pipeline, "vspipe_argv", lambda *a, **k: ["sh", "-c", vspipe])
    monkeypatch.setattr(pipeline, "encode_argv", lambda *a, **k: ["sh", "-c", ffmpeg])
    awake: list[int] = []

    def keep_awake(pid: int) -> None:
        awake.append(pid)

    async def idle(_ctx: Any) -> None:
        return None

    monkeypatch.setattr(osapi, "keep_awake", keep_awake)
    monkeypatch.setattr(jobs, "_gpu_wanted_elsewhere", idle)
    spec = RenderJobSpec(tmp_path / "a.mkv", tmp_path / "a.smooth.mkv", "simple", "libx265")
    job = jobs._Job(JobId("j"), spec, state=OpState.RUNNING)
    manifest = jobs._Manifest(tmp_path / "jobs" / "m.json", {})
    await jobs._encode(
        ctx, job, _tools(), tmp_path / "s.vpy", {}, _info(), tmp_path / "v.mkv",
        manifest, io.StringIO(), 0.0,
    )  # fmt: skip
    return job, awake


@posix
async def test_encode_reports_progress_and_keeps_the_mac_awake(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vspipe = r"printf 'Frame: 120/240 (40.0 fps)\r' >&2; printf 'Frame: 240/240\r' >&2; echo y4m"
    job, awake = await _encode(monkeypatch, tmp_path, vspipe, "cat > /dev/null")
    assert (job.done, job.total) == (240, 240)
    assert len(awake) == 1 and awake[0] > 0  # caffeinate -w <vspipe's pid>


@posix
async def test_an_encoder_dying_first_is_not_blamed_on_the_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # B18: a full disk. ffmpeg stops; vspipe then fails writing to the pipe
    vspipe = "sleep 0.3; while :; do echo frame || exit 1; done"
    ffmpeg = "echo 'No space left on device' >&2; exit 1"
    with pytest.raises(ButterEyeError) as err:
        await _encode(monkeypatch, tmp_path, vspipe, ffmpeg)
    assert err.value.code is ErrorCode.ENCODER_FAILED
    assert err.value.detail == "No space left on device"


@posix
async def test_a_failing_script_is_the_scripts_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vspipe = "echo 'Script evaluation failed: no ffms2' >&2; exit 1"
    ffmpeg = "cat > /dev/null; sleep 0.2; echo 'Invalid data found' >&2; exit 1"
    with pytest.raises(ButterEyeError) as err:
        await _encode(monkeypatch, tmp_path, vspipe, ffmpeg)
    assert err.value.code is ErrorCode.VSPIPE_FRAME_ERROR
    assert "no ffms2" in (err.value.detail or "")


# ---------------------------------------------------------------- B2: keep the encode
@posix
async def test_a_failed_remux_keeps_the_encode_and_the_same_copy_finishes_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    src = tmp_path / "film.mp4"
    src.write_bytes(b"source")
    out = tmp_path / "film.smooth.mkv"
    video, part = pipeline.temp_paths(out)
    encodes: list[int] = []
    remux_ok = [False]

    async def source_info(_tools: Any, _src: Path) -> rp.SourceInfo:
        return _info()

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        return CmdResult(tuple(argv), 0, "", "")

    async def pick(*_a: Any) -> tuple[BackendId, None]:
        return BackendId.MVGPU, None

    async def encode(*a: Any, **_k: Any) -> None:
        encodes.append(1)
        video.write_bytes(b"hours of encoding")

    async def remux(_tools: Any, v: Path, _s: Path, p: Path, *_a: Any) -> None:
        if not remux_ok[0]:
            raise ButterEyeError(ErrorCode.REMUX_FAILED, Msg("Copying failed."))
        p.write_bytes(v.read_bytes() + b"+streams")

    monkeypatch.setattr(jobs, "_tools", _tools)
    monkeypatch.setattr(jobs, "_source_info", source_info)
    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_ffms2_lib", lambda: src)  # exists
    monkeypatch.setattr(jobs, "_pick", pick)
    monkeypatch.setattr(jobs, "_encode", encode)
    monkeypatch.setattr(jobs, "_remux", remux)
    monkeypatch.setattr(jobs, "write_script", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_constants", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_gpu_index", lambda ctx: None)
    activity: list[object] = []

    def begin(reason: str) -> int:
        activity.append(reason)
        return 7

    monkeypatch.setattr(osapi, "begin_activity", begin)
    monkeypatch.setattr(osapi, "end_activity", activity.append)
    spec = RenderJobSpec(src, out, "simple", "libx265")

    first = jobs._Job(JobId("first"), spec, state=OpState.RUNNING)
    jobs._reg(ctx).jobs[first.id] = first
    await jobs._run(ctx, first)
    assert first.state is OpState.FAILED and first.error is not None
    assert first.error.code is ErrorCode.REMUX_FAILED
    assert "kept" in (first.error.fix.key if first.error.fix else "")
    assert video.read_bytes() == b"hours of encoding" and not part.exists()
    assert first.kept == video
    manifest = json.loads((ctx.paths.jobs_dir / "render-first.json").read_text())
    assert manifest["kept"] is True and manifest["engine"] == "mvgpu"
    assert activity == [jobs.ACTIVITY_REASON, 7]  # B12: held for the job, then let go
    # after a restart it is listed as a partial file of an old job
    restarted: Any = Ctx(tmp_path)
    stale = await jobs.stale_jobs(restarted)
    assert [s.id for s in stale] == ["first"] and video in stale[0].partial_files

    # the same copy again: no second encode, the remux runs from the kept track
    remux_ok[0] = True
    second = jobs._Job(JobId("second"), spec, state=OpState.RUNNING)
    jobs._reg(ctx).jobs[second.id] = second
    phases: list[RenderPhase | None] = []
    ctx.emit = lambda ev: phases.append(ev.job.phase)
    await jobs._run(ctx, second)
    assert second.state is OpState.SUCCEEDED and encodes == [1]
    assert RenderPhase.RENDER not in phases and RenderPhase.REMUX in phases
    assert out.read_bytes() == b"hours of encoding+streams" and not video.exists()
    assert first.kept is None and not list(ctx.paths.jobs_dir.glob("*.json"))


@posix
async def test_another_encode_into_the_track_drops_the_kept_claim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    src = tmp_path / "film.mp4"
    src.write_bytes(b"source")
    out = tmp_path / "film.smooth.mkv"
    video, _part = pipeline.temp_paths(out)
    encodes: list[str] = []

    async def source_info(_tools: Any, _src: Path) -> rp.SourceInfo:
        return _info()

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        return CmdResult(tuple(argv), 0, "", "")

    async def pick(*_a: Any) -> tuple[BackendId, None]:
        return BackendId.MVGPU, None

    async def encode(_ctx: Any, job: Any, *a: Any, **_k: Any) -> None:
        encodes.append(job.spec.encoder)
        video.write_bytes(job.spec.encoder.encode())

    async def remux(*_a: Any) -> None:
        raise ButterEyeError(ErrorCode.REMUX_FAILED, Msg("Copying failed."))

    monkeypatch.setattr(jobs, "_tools", _tools)
    monkeypatch.setattr(jobs, "_source_info", source_info)
    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_ffms2_lib", lambda: src)
    monkeypatch.setattr(jobs, "_pick", pick)
    monkeypatch.setattr(jobs, "_encode", encode)
    monkeypatch.setattr(jobs, "_remux", remux)
    monkeypatch.setattr(jobs, "write_script", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_constants", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_gpu_index", lambda ctx: None)
    monkeypatch.setattr(osapi, "begin_activity", lambda reason: None)
    monkeypatch.setattr(osapi, "end_activity", lambda token: None)
    x265 = RenderJobSpec(src, out, "simple", "libx265")
    a = jobs._Job(JobId("a"), x265, state=OpState.RUNNING)
    jobs._reg(ctx).jobs[a.id] = a
    await jobs._run(ctx, a)
    assert a.kept == video
    # another encoder for the same output: a new track over the kept one
    b = jobs._Job(JobId("b"), RenderJobSpec(src, out, "simple", "libsvtav1"))
    b.state = OpState.RUNNING
    jobs._reg(ctx).jobs[b.id] = b
    await jobs._run(ctx, b)
    assert encodes == ["libx265", "libsvtav1"] and b.kept == video
    assert a.kept is None  # its row no longer offers to finish from b's track
    left = [p.name for p in ctx.paths.jobs_dir.glob("*.json")]
    assert left == ["render-b.json"]
    # a's copy again: encoded anew, never remuxed around the AV1 track
    again = jobs._Job(JobId("again"), x265, state=OpState.RUNNING)
    jobs._reg(ctx).jobs[again.id] = again
    await jobs._run(ctx, again)
    assert encodes == ["libx265", "libsvtav1", "libx265"]


async def test_forget_deletes_a_kept_encode_nobody_will_use(tmp_path: Path) -> None:
    ctx: Any = Ctx(tmp_path)
    out = tmp_path / "a.smooth.mkv"
    video, _part = pipeline.temp_paths(out)
    video.write_bytes(b"v")
    ctx.paths.jobs_dir.mkdir()
    manifest = ctx.paths.jobs_dir / "render-old.json"
    manifest.write_text("{}")
    spec = RenderJobSpec(tmp_path / "a.mkv", out, "simple", "libx265")
    old = jobs._Job(JobId("old"), spec, state=OpState.FAILED, kept=video)
    queued = jobs._Job(JobId("new"), spec)
    reg = jobs._reg(ctx)
    reg.jobs.update({old.id: old, queued.id: queued})
    await jobs.forget(ctx, old.id)
    assert video.exists()  # the queued copy finishes from it
    other = jobs._Job(JobId("other"), spec, state=OpState.FAILED, kept=video)
    reg.jobs = {other.id: other}
    (ctx.paths.jobs_dir / "render-other.json").write_text("{}")
    await jobs.forget(ctx, other.id)
    assert not video.exists() and not (ctx.paths.jobs_dir / "render-other.json").exists()


def test_a_kept_track_is_found_only_for_its_own_copy(tmp_path: Path) -> None:
    ctx: Any = Ctx(tmp_path)
    out = tmp_path / "a.smooth.mkv"
    video, _ = pipeline.temp_paths(out)
    video.write_bytes(b"v")
    ctx.paths.jobs_dir.mkdir()
    data = {"id": "x", "kept": True, "output": os.fspath(out), "encode": "k", "engine": "mvgpu"}
    (ctx.paths.jobs_dir / "render-x.json").write_text(json.dumps(data))
    found = jobs._find_kept(ctx, out, "k", None)
    assert found is not None and found[1] == video and found[2] is BackendId.MVGPU
    assert jobs._find_kept(ctx, out, "other", None) is None
    # the window asked for another engine: convert again
    assert jobs._find_kept(ctx, out, "k", BackendId.RIFE_NCNN) is None
    assert jobs._find_kept(ctx, tmp_path / "b.mkv", "k", None) is None


# ---------------------------------------------------------------- B1: the job's engine
async def test_the_job_takes_the_engine_the_window_showed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    cands = ((BackendId.RIFE_NCNN, None), (BackendId.MVGPU, None), (BackendId.MVTOOLS, None))
    rates = {BackendId.RIFE_NCNN: 8.37, BackendId.MVGPU: 71.8, BackendId.MVTOOLS: 76.0}
    monkeypatch.setattr(jobs, "_candidates", lambda ctx, profile: cands)

    async def results(_ctx: Any) -> tuple[()]:
        return ()

    monkeypatch.setattr(jobs, "_bench_results", results)
    monkeypatch.setattr(jobs, "bench_rate", lambda res, b, model, size: rates[b])
    info, target = _info(), Fraction(48)
    got = await jobs._pick(ctx, None, info, (1920, 1080), target, None)
    assert got[0] is BackendId.MVGPU  # RIFE would take 7× the video's length
    pinned = await jobs._pick(ctx, None, info, (1920, 1080), target, BackendId.RIFE_NCNN)
    assert pinned[0] is BackendId.RIFE_NCNN
    # a choice that isn't a candidate any more is chosen again
    gone = await jobs._pick(ctx, None, info, (1920, 1080), target, BackendId.RIFE_TRT)
    assert gone[0] is BackendId.MVGPU


# ---------------------------------------------------------------- P2: test encode
async def test_videotoolbox_encoders_must_encode_to_be_offered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    calls: list[str] = []

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        enc = argv[argv.index("-c:v") + 1]
        calls.append(enc)
        rc = 0 if enc == "hevc_videotoolbox" else 1
        return CmdResult(tuple(argv), rc, "", "no hardware")

    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_VT_WORKING", {})
    listed = ("libx265", "hevc_videotoolbox", "prores_videotoolbox")
    got = await jobs._videotoolbox_working(os.fspath(ffmpeg), listed)
    assert got == frozenset({"hevc_videotoolbox"})
    assert calls == ["hevc_videotoolbox", "prores_videotoolbox"]
    await jobs._videotoolbox_working(os.fspath(ffmpeg), listed)
    assert len(calls) == 2  # once per ffmpeg
    assert await jobs._videotoolbox_working(os.fspath(ffmpeg), ("libx265",)) == frozenset()


def test_engine_size_is_a_render_size() -> None:
    from buttereye.core.types import RenderSize

    s = rp.EngineSize(1920, 1080, 57.4, BackendId.MVGPU, ((BackendId.MVGPU, 57.4),))
    assert isinstance(s, RenderSize) and dataclasses.replace(s, est_fps=1.0).engine is (
        BackendId.MVGPU
    )
