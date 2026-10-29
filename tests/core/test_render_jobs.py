# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A render job's process handling without vspipe or a GPU: stopping for
playback (B11), staying awake (B12), which side failed (B18), the encoded video
kept after a failed remux (B2), the hardware test encode (P2), the install line
when ffmpeg has no usable encoder. ``sh`` stands in for vspipe and ffmpeg."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import shutil
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


def test_pause_on_windows_suspends_the_tree_and_ffmpeg_and_resumes_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows has no SIGSTOP: vspipe's tree and ffmpeg (its sibling, not its
    # child) are suspended, and exactly those resumed. The real calls run under
    # Wine (tests/windows/test_suspend_windows.py)
    suspended: list[tuple[int, ...]] = []
    resumed: list[list[tuple[int, int]]] = []

    def suspend(pids: Any) -> list[tuple[int, int]]:
        suspended.append(tuple(pids))
        return [(p, 1000 + p) for p in pids]

    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(jobs, "_suspend", suspend)
    monkeypatch.setattr(jobs, "_resume", lambda held: resumed.append(list(held)))
    pause = jobs._Pause(100, (200,))
    pause.stop()
    pause.stop()  # already stopped: not suspended twice (suspensions count)
    assert suspended == [(100, 200)] and pause.held == [(100, 1100), (200, 1200)]
    pause.resume()
    pause.resume()
    assert resumed == [[(100, 1100), (200, 1200)]] and pause.held == []
    assert pause.since is None


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
    monkeypatch.setattr(jobs, "_signal_group", lambda *a, **k: [])
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


# ---------------------------------------------------------------- non-ASCII names
@posix
@pytest.mark.skipif(
    not (shutil.which("mkvmerge") and shutil.which("ffmpeg")), reason="no mkvmerge or ffmpeg"
)
async def test_mkvmerge_remuxes_a_file_name_that_is_not_ascii(tmp_path: Path) -> None:
    """A YouTube title ("K⧸DA - POP⧸STARS ｜ …") reached mkvmerge as "K" under
    run()'s LC_ALL=C and the remux refused to overwrite its own source."""
    mkvmerge, ffmpeg = shutil.which("mkvmerge"), shutil.which("ffmpeg")
    assert mkvmerge and ffmpeg
    src = tmp_path / "K⧸DA - POP⧸STARS ｜ Music Video.webm"
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64",
         "-f", "lavfi", "-i", "sine=d=1", "-c:v", "libvpx-vp9", "-c:a", "libopus", str(src)],
        check=True,
    )  # fmt: skip
    out = tmp_path / "K⧸DA - POP⧸STARS ｜ Music Video.smooth.mkv"
    video, part = pipeline.temp_paths(out)
    subprocess.run(
        [ffmpeg, "-loglevel", "error", "-i", str(src), "-an", "-c", "copy", str(video)], check=True
    )
    tools = jobs._Tools("ffprobe", ffmpeg, "vspipe", mkvmerge)
    await jobs._remux(tools, video, src, part, _info(), io.StringIO())
    assert part.stat().st_size > 0


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
async def test_hardware_encoders_must_encode_to_be_offered(
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
    monkeypatch.setattr(jobs, "_HW_WORKING", {})
    listed = ("libx265", "hevc_videotoolbox", "prores_videotoolbox")
    got = await jobs._hw_working(os.fspath(ffmpeg), listed)
    assert got == frozenset({"hevc_videotoolbox"})
    assert calls == ["hevc_videotoolbox", "prores_videotoolbox"]
    await jobs._hw_working(os.fspath(ffmpeg), listed)
    assert len(calls) == 2  # once per ffmpeg
    assert await jobs._hw_working(os.fspath(ffmpeg), ("libx265",)) == frozenset()


async def test_a_timeout_is_temporary_for_nvenc_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # a hung VideoToolbox test is not offered and not tested again, as before
    # NVENC was tested; a hung NVENC one is offered and tested again next time
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    calls: list[str] = []

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        calls.append(argv[argv.index("-c:v") + 1])
        return CmdResult(tuple(argv), None, "", "", timed_out=True)

    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_HW_WORKING", {})
    vt = ("hevc_videotoolbox",)
    assert await jobs._hw_working(os.fspath(ffmpeg), vt) == frozenset()
    assert await jobs._hw_working(os.fspath(ffmpeg), vt) == frozenset()
    assert calls == ["hevc_videotoolbox"]
    calls.clear()
    nv = ("hevc_nvenc",)
    assert await jobs._hw_working(os.fspath(ffmpeg), nv) == frozenset(nv)
    assert await jobs._hw_working(os.fspath(ffmpeg), nv) == frozenset(nv)
    assert calls == ["hevc_nvenc", "hevc_nvenc"]


async def test_nvenc_is_test_encoded_and_a_busy_gpu_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    calls: list[str] = []
    answers = {
        "hevc_nvenc": (0, ""),
        # an RTX 30: no AV1 encoder
        "av1_nvenc": (1, "[av1_nvenc] OpenEncodeSessionEx failed: unsupported device (2)"),
    }

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        enc = argv[argv.index("-c:v") + 1]
        calls.append(enc)
        rc, err = answers[enc]
        return CmdResult(tuple(argv), rc, "", err)

    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_HW_WORKING", {})
    listed = ("hevc_nvenc", "av1_nvenc", "libx265")
    assert await jobs._hw_working(os.fspath(ffmpeg), listed) == frozenset({"hevc_nvenc"})
    assert await jobs._hw_working(os.fspath(ffmpeg), listed) == frozenset({"hevc_nvenc"})
    assert calls == ["hevc_nvenc", "av1_nvenc"]  # a lasting failure is kept

    # every encode session taken (a game, another render): offered as before the
    # test, and tested again next time
    jobs._HW_WORKING.clear()
    calls.clear()
    answers["av1_nvenc"] = (1, "OpenEncodeSessionEx failed: out of memory (10): (no details)")
    assert await jobs._hw_working(os.fspath(ffmpeg), listed) == frozenset(
        {"hevc_nvenc", "av1_nvenc"}
    )
    answers["av1_nvenc"] = (0, "")
    assert await jobs._hw_working(os.fspath(ffmpeg), listed) == frozenset(
        {"hevc_nvenc", "av1_nvenc"}
    )
    assert calls == ["hevc_nvenc", "av1_nvenc"] * 2
    await jobs._hw_working(os.fspath(ffmpeg), listed)
    assert len(calls) == 4  # now kept


async def test_only_the_hardware_this_machine_is_offered_is_tested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    listed = "\n".join(
        f" V....D {e:<20} x" for e in ("hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1")
    )
    tested: list[tuple[str, ...]] = []

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        return CmdResult(tuple(argv), 0, " ------\n" + listed + "\n", "")

    async def hw_working(ffmpeg: str, candidates: Any) -> frozenset[str]:
        tested.append(tuple(candidates))
        return frozenset(c for c in candidates if c != "av1_nvenc")

    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_hw_working", hw_working)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    tools = jobs._Tools("ffprobe", os.fspath(tmp_path / "ffmpeg"), "vspipe", None)
    got = await jobs._encoders(tools, hdr10=False, nvidia=True)
    assert got[0] == "hevc_nvenc" and "av1_nvenc" not in got and "libx265" in got
    assert tested == [("hevc_nvenc", "av1_nvenc")]
    tested.clear()
    await jobs._encoders(tools, hdr10=False, nvidia=False)  # no NVIDIA GPU: no test
    await jobs._encoders(tools, hdr10=True, nvidia=True)  # HDR10 uses neither
    assert tested == [()]


# ---------------------------------------------------------------- A3: install line
@pytest.mark.parametrize("hdr10", [False, True])
def test_no_usable_encoder_names_this_platforms_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, hdr10: bool
) -> None:
    asked: list[str] = []

    def install(tool: str) -> str:
        asked.append(tool)
        return f"winget install --exact --id Gyan.FFmpeg ({tool})"

    monkeypatch.setattr(jobs, "install_command", install)
    r = jobs.encoder_refusal((), hdr10=hdr10)
    assert r is not None and r.code is ErrorCode.ENCODER_FAILED
    assert r.commands == ("winget install --exact --id Gyan.FFmpeg (ffmpeg)",)
    assert asked == ["ffmpeg"]
    assert "RPM Fusion" not in r.fix.key and "ffmpeg-free" not in r.fix.key
    assert jobs.encoder_refusal(("libx265",), hdr10=hdr10) is None


def test_engine_size_is_a_render_size() -> None:
    from buttereye.core.types import RenderSize

    s = rp.EngineSize(1920, 1080, 57.4, BackendId.MVGPU, ((BackendId.MVGPU, 57.4),))
    assert isinstance(s, RenderSize) and dataclasses.replace(s, est_fps=1.0).engine is (
        BackendId.MVGPU
    )


# ---------------------------------------------------------------- colour depth
X265_HELP = (
    "Encoder libx265 [libx265 H.265 / HEVC]:\n"
    "    Supported pixel formats: yuv420p yuv420p10le yuv420p12le yuv444p12le\n"
)


async def test_encoder_depths_are_asked_once_and_tested_on_the_gpu(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ffmpeg = tmp_path / "ffmpeg"
    ffmpeg.write_text("")
    calls: list[tuple[str, ...]] = []

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        calls.append(tuple(argv))
        if "-h" in argv:
            enc = argv[-1].removeprefix("encoder=")
            text = X265_HELP if enc == "libx265" else "Supported pixel formats: yuv420p p010le\n"
            return CmdResult(tuple(argv), 0, text, "")
        # the 10-bit test encode: this GPU has no 10-bit AV1
        enc = argv[argv.index("-c:v") + 1]
        ok = enc != "av1_nvenc"
        return CmdResult(tuple(argv), 0 if ok else 1, "", "" if ok else "10 bit unsupported")

    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_DEPTHS", {})
    got = await jobs._encoder_depths(os.fspath(ffmpeg), ("libx265", "hevc_nvenc", "av1_nvenc"))
    assert got == {"libx265": (8, 10, 12), "hevc_nvenc": (8, 10), "av1_nvenc": (8,)}
    tests = [c for c in calls if "-c:v" in c]
    assert [c[c.index("-c:v") + 1] for c in tests] == ["hevc_nvenc", "av1_nvenc"]
    assert all(c[c.index("-pix_fmt") + 1] == "p010le" for c in tests)
    n = len(calls)
    await jobs._encoder_depths(os.fspath(ffmpeg), ("libx265", "hevc_nvenc", "av1_nvenc"))
    assert len(calls) == n  # once per ffmpeg


async def test_a_job_writes_the_sources_depth_and_tells_the_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    src = tmp_path / "film.mkv"
    src.write_bytes(b"source")
    out = tmp_path / "film.smooth.mkv"
    got: dict[str, Any] = {}

    def info12() -> rp.SourceInfo:
        return dataclasses.replace(_info(), bits=12, pix_fmt="yuv420p12le")

    async def source_info(_tools: Any, _src: Path) -> rp.SourceInfo:
        return info12()

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        return CmdResult(tuple(argv), 0, X265_HELP if "-h" in argv else "", "")

    async def pick(*_a: Any) -> tuple[BackendId, None]:
        return BackendId.MVTOOLS, None

    async def encode(_ctx: Any, job: Any, _tools: Any, _script: Any, ud: Any, *a: Any,
                     **k: Any) -> None:  # fmt: skip
        got["ud"], got["bits"] = ud, k.get("bits")
        pipeline.temp_paths(out)[0].write_bytes(b"video")

    async def remux(_tools: Any, v: Path, _s: Path, p: Path, *_a: Any) -> None:
        p.write_bytes(v.read_bytes())

    monkeypatch.setattr(jobs, "_tools", _tools)
    monkeypatch.setattr(jobs, "_source_info", source_info)
    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_DEPTHS", {})
    monkeypatch.setattr(jobs, "_ffms2_lib", lambda: src)
    monkeypatch.setattr(jobs, "_pick", pick)
    monkeypatch.setattr(jobs, "_encode", encode)
    monkeypatch.setattr(jobs, "_remux", remux)
    monkeypatch.setattr(jobs, "write_script", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_constants", lambda *a, **k: None)
    monkeypatch.setattr(jobs, "_gpu_index", lambda ctx: None)
    monkeypatch.setattr(osapi, "begin_activity", lambda reason: None)
    monkeypatch.setattr(osapi, "end_activity", lambda token: None)
    for spec, bits in (
        (RenderJobSpec(src, out, "simple", "libx265"), 12),  # same as source
        (RenderJobSpec(src, out, "simple", "libx265", depth="10"), 10),
        (RenderJobSpec(src, out, "simple", "libx265", overwrite=True, ten_bit=True), 10),
    ):
        out.unlink(missing_ok=True)
        job = jobs._Job(JobId(f"j{bits}{spec.depth}"), spec, state=OpState.RUNNING)
        jobs._reg(ctx).jobs[job.id] = job
        await jobs._run(ctx, job)
        assert job.state is OpState.SUCCEEDED, job.error
        assert got["bits"] == bits and got["ud"]["out_bits"] == bits
        assert got["ud"]["dither"] == "error_diffusion"  # the reduction, every engine
        assert jobs._snapshot(job).bits == bits
    # the settings' default applies to a spec without a choice
    ctx.cfg = dataclasses.replace(ctx.cfg, render=dataclasses.replace(ctx.cfg.render, depth="8"))
    out.unlink(missing_ok=True)
    job = jobs._Job(JobId("dflt"), RenderJobSpec(src, out, "simple", "libx265"),
                    state=OpState.RUNNING)  # fmt: skip
    jobs._reg(ctx).jobs[job.id] = job
    await jobs._run(ctx, job)
    assert got["bits"] == 8


async def test_the_probe_carries_the_source_depth_and_the_encoders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ctx: Any = Ctx(tmp_path)
    src = tmp_path / "film.mkv"
    src.write_bytes(b"source")
    listed = "".join(f" V....D {e:<20} x\n" for e in ("libx264", "libx265", "libsvtav1"))

    async def source_info(_tools: Any, _src: Path) -> rp.SourceInfo:
        return dataclasses.replace(_info(), bits=10, pix_fmt="yuv444p10le", chroma="444")

    async def run(argv: Any, **_kw: Any) -> CmdResult:
        if "-h" in argv:
            enc = argv[-1].removeprefix("encoder=")
            text = {"libx265": X265_HELP, "libx264": "Supported pixel formats: yuv420p"}.get(
                enc, "Supported pixel formats: yuv420p yuv420p10le"
            )
            return CmdResult(tuple(argv), 0, text, "")
        return CmdResult(tuple(argv), 0, " ------\n" + listed, "")

    monkeypatch.setattr(jobs, "_tools", _tools)
    monkeypatch.setattr(jobs, "_source_info", source_info)
    monkeypatch.setattr(jobs, "run", run)
    monkeypatch.setattr(jobs, "_DEPTHS", {})
    monkeypatch.setattr(jobs, "_ffms2_lib", lambda: src)
    monkeypatch.setattr(jobs, "_candidates", lambda ctx, profile: ())
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    probe = await jobs.probe(ctx, src)
    assert (probe.bits, probe.chroma, probe.pix_fmt) == (10, "444", "yuv444p10le")
    assert probe.encoders == ("libx265", "libsvtav1", "libx264")  # x264 would lower it
    assert dict(probe.encoder_depths) == {
        "libsvtav1": (8, 10), "libx265": (8, 10, 12), "libx264": (8,),
    }  # fmt: skip
