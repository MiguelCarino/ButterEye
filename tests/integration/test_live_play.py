# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live play against the real mpv of the dev box (U10, GUI.md §11.4).

mpv runs with ``--vo=null --ao=null --display-fps-override=60`` on a generated
640x360 24 fps clip with a sine audio track. Every mpv started here is killed in
the fixture teardown. HOME and every XDG dir point at temporary directories.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import types as pytypes
from collections.abc import AsyncIterator, Callable, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.doctor import gpufault
from buttereye.core.errors import ButterEyeError, ErrorCode, FilterFailed
from buttereye.core.events import (
    Event,
    HealthChanged,
    Notice,
    OrphansFound,
    SessionAdded,
    SessionEnded,
)
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import Thresholds, gpu_fault_lines
from buttereye.core.mpvctl.ipc import MpvIpc
from buttereye.core.scriptgen import generator
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchResult,
    BypassReason,
    Config,
    ConfigLoad,
    DetachPolicy,
    FilterState,
    Health,
    Paths,
    Profile,
    SessionId,
    SessionSnapshot,
    Target,
    TargetKind,
)

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")


def _profile(pid: str, backend: BackendId, kind: TargetKind, model: str | None = None) -> Profile:
    return Profile(
        id=pid, name=pid, backend=backend, model=model, scale=None, target=Target(kind),
        sc_threshold=0.12, buffered_frames=None, concurrent_frames=None,
    )  # fmt: skip


def _config() -> Config:
    base = sc.default_config()
    extra = (
        _profile("mv", BackendId.MVTOOLS, TargetKind.DISPLAY),
        _profile("mv2x", BackendId.MVTOOLS, TargetKind.X2),
        _profile("rife", BackendId.RIFE_NCNN, TargetKind.DISPLAY, "rife-v4.22_lite_ensembleFalse"),
    )
    return dataclasses.replace(base, profiles=base.profiles + extra)


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to generate the test clip")
    out = tmp_path_factory.mktemp("clip") / "test clip, 'q'.mkv"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", os.fspath(out),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    return out


def _make_clip(out: Path, *, rate: int, seconds: int = 20, vf: str | None = None) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to generate the test clip")
    filters = ["-vf", vf] if vf else []
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={rate}",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", str(seconds), *filters, "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.fspath(out),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    return out


@pytest.fixture(scope="module")
def clip30(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_clip(tmp_path_factory.mktemp("clip30") / "b30.mkv", rate=30)


@pytest.fixture(scope="module")
def clip_hdr30(tmp_path_factory: pytest.TempPathFactory) -> Path:
    pq = "setparams=color_trc=smpte2084:colorspace=bt2020nc:color_primaries=bt2020"
    return _make_clip(tmp_path_factory.mktemp("hdr30") / "hdr30.mkv", rate=30, vf=pq)


def _raw(sock: Path, *cmd: object) -> Any:
    """A raw test client (not ButterEye's allowlisted IPC): what the user or a
    playlist does to mpv behind ButterEye's back."""
    import socket as socket_mod

    with socket_mod.socket(socket_mod.AF_UNIX) as c:
        c.settimeout(5)
        c.connect(os.fspath(sock))
        c.sendall((json.dumps({"command": list(cmd), "request_id": 99}) + "\n").encode())
        buf = b""
        while True:
            buf += c.recv(65536)
            for line in buf.split(b"\n"):
                if line.strip():
                    msg = json.loads(line)
                    if msg.get("request_id") == 99:
                        return msg
            if not buf:
                return None


@dataclasses.dataclass
class Live:
    core: ButterEye
    paths: Paths
    events: list[Event]
    pids: list[int]
    held: dict[SessionId, Any] = dataclasses.field(default_factory=dict)

    @property
    def reg(self) -> live.Registry:
        return live.registry(self.core._ctx)

    async def play(self, file: Path, profile: str | None) -> SessionId:
        sid = await self.core.play(file, profile_id=profile).result()
        self.held[sid] = self.reg.sessions[sid]  # ended sessions leave the registry
        pid = self.held[sid].pid
        assert pid is not None
        self.pids.append(pid)
        return sid

    def s(self, sid: SessionId) -> Any:
        return self.held[sid]

    async def snap(self, sid: SessionId) -> SessionSnapshot:
        live_ = [x for x in await self.core.sessions() if x.sid == sid]
        return live_[0] if live_ else self.held[sid].snapshot()

    async def wait(
        self, sid: SessionId, pred: Callable[[SessionSnapshot], bool], timeout: float = 15.0
    ) -> SessionSnapshot:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        snap = None
        while loop.time() < end:
            snap = await self.snap(sid)
            if pred(snap):
                return snap
            await asyncio.sleep(0.1)
        raise AssertionError(f"timed out; last snapshot: {snap}")

    async def vf(self, sid: SessionId) -> Any:
        return await self.held[sid].ipc.get("vf")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    with contextlib.suppress(OSError, ValueError):
        # a zombie of ours counts as gone
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        return state != "Z"
    return True


@pytest.fixture
def config_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cfg = _config()

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-test")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("live play must not save the config")

    name = "buttereye.core.profiles.config"
    mod = pytypes.ModuleType(name)
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, mod)
    yield


@pytest.fixture
async def live_env(xdg_env: Any, config_provider: None) -> AsyncIterator[Live]:
    paths = sc.fake_paths(env=os.environ)
    assert os.fspath(paths.runtime_dir).startswith(os.fspath(xdg_env.runtime))
    core = await ButterEye.open(paths)
    reg = live.registry(core._ctx)
    reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")
    reg.options.thresholds = Thresholds(init_grace_s=8.0, frozen_s=2.0, lag_s=1.0, lag_hold_s=1.0)
    events: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            events.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    env = Live(core, paths, events, [])
    try:
        yield env
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)
        task.cancel()
        for pid in env.pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        for pid in env.pids:
            for _ in range(100):
                if not _alive(pid):
                    break
                await asyncio.sleep(0.02)
            assert not _alive(pid), f"mpv {pid} survived the test"
        user_mpv = xdg_env.home / ".config" / "mpv"
        assert not user_mpv.exists(), "ButterEye must never write ~/.config/mpv"


def _ours(vf: Any) -> dict[str, Any] | None:
    entry = generator.filter_entry(vf)
    return dict(entry) if entry is not None else None


async def test_play_mvtools_toggle_profile_detach(live_env: Live, clip: Path) -> None:
    if not (PLUGIN_DIR / "mvtools.so").is_file():
        pytest.skip("buttereye-vs-mvtools not installed")
    env = live_env
    sid = await env.play(clip, "mv")
    added = [e for e in env.events if isinstance(e, SessionAdded)]
    assert added and added[0].snapshot.filter is FilterState.PENDING
    s = env.s(sid)
    # launched per §4.3: own profile applied via --include, socket in the 0700 dir
    assert s.socket.parent == env.paths.runtime_dir
    assert (env.paths.runtime_dir.stat().st_mode & 0o777) == 0o700
    assert await s.ipc.get("interpolation") is False
    assert await s.ipc.get("hr-seek-framedrop") is False
    assert env.paths.mpv_include.is_file()
    assert os.getpgid(s.pid or 0) == s.pid  # own session / process group

    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    assert snap.backend is BackendId.MVTOOLS and snap.gen == 1
    assert snap.target_fps == Fraction(60) and snap.multiplier == Fraction(5, 2)
    assert snap.source is not None and (snap.source.width, snap.source.height) == (640, 360)
    entry = _ours(await env.vf(sid))
    assert entry is not None and entry["enabled"] is True
    ud = json.loads(entry["params"]["user-data"])
    assert ud["gen"] == 1 and ud["matrix"] == "170m" and ud["title"] == clip.name
    assert (s.script_path.stat().st_mode & 0o777) == 0o600

    off = await env.core.set_interpolation(sid, False)
    assert off.filter is FilterState.OFF and _ours(await env.vf(sid)) is None
    on = await env.core.set_interpolation(sid, True)
    assert on.gen == 2 and on.filter in (FilterState.ACTIVE, FilterState.PENDING)
    twice = await env.core.apply_profile(sid, "mv2x")
    assert twice.gen == 3 and twice.target_fps == Fraction(48) and twice.profile_id == "mv2x"
    entry = _ours(await env.vf(sid))
    assert entry is not None and json.loads(entry["params"]["user-data"])["gen"] == 3

    # counters observed, advancing playback
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)

    pid = s.pid or 0
    await env.core.detach(sid, DetachPolicy.KEEP_FILTER)
    await asyncio.sleep(0.05)
    assert any(isinstance(e, SessionEnded) and e.reason == "detached" for e in env.events)
    assert _alive(pid), "detach must leave mpv running"
    assert env.s(sid).snapshot().ended
    assert all(x.sid != sid for x in await env.core.sessions())  # ended: out of the registry

    # the instance is rediscovered with its filter still in place
    cands = await env.core.discover()
    mine = [c for c in cands if c.pid == pid]
    assert mine and mine[0].orphan_filter and mine[0].managed_by_us and mine[0].refused is None
    await asyncio.sleep(0.05)  # let the event pump deliver
    assert any(isinstance(e, OrphansFound) for e in env.events)
    ipc = await MpvIpc.connect(mine[0].socket)
    ipc.start()
    try:
        assert _ours(await ipc.get("vf")) is not None
    finally:
        await ipc.close()


async def test_close_keeps_mpv_playing(live_env: Live, clip: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    pid = env.s(sid).pid or 0
    report = await env.core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)
    assert report.detached == (sid,) and report.failed == ()
    await asyncio.sleep(0.5)
    assert _alive(pid)


async def test_disable_and_detach_removes_filter(live_env: Live, clip: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    sock = env.s(sid).socket
    await env.core.detach(sid, DetachPolicy.DISABLE_FILTER)
    ipc = await MpvIpc.connect(sock)
    ipc.start()
    try:
        assert _ours(await ipc.get("vf")) is None
    finally:
        await ipc.close()


def _template_with(body: str) -> str:
    head = (
        "import vapoursynth as vs\n"
        f"{generator.BLOCK_BEGIN}\nPLUGIN_DIR = ''\n{generator.BLOCK_END}\n"
        "core = vs.core\n"
    )
    return head + body


async def test_broken_filter_rolls_back(
    live_env: Live, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = live_env
    monkeypatch.setattr(
        generator, "template_text", lambda: _template_with("raise vs.Error('broken on purpose')\n")
    )
    sid = await env.play(clip, "mv")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ROLLED_BACK)
    assert snap.health_code is ErrorCode.VF_ROLLED_BACK
    await asyncio.sleep(0.05)
    assert _ours(await env.vf(sid)) is None
    assert any(
        isinstance(e, Notice) and e.code is ErrorCode.VF_ROLLED_BACK and e.sid == sid
        for e in env.events
    )
    # never re-enabled automatically; playback continues without the filter
    await asyncio.sleep(1.0)
    t0 = await env.s(sid).ipc.get("playback-time")
    await asyncio.sleep(1.5)
    t1 = await env.s(sid).ipc.get("playback-time")
    assert t1 > t0
    assert (await env.snap(sid)).filter is FilterState.ROLLED_BACK
    # an explicit change that fails raises FilterFailed after rollback (F4)
    with pytest.raises(FilterFailed) as info:
        await env.core.apply_profile(sid, "mv")
    assert info.value.rolled_back and info.value.code is ErrorCode.VF_ROLLED_BACK
    assert _ours(await env.vf(sid)) is None


SLOW = """
import time
clip = core.std.AssumeFPS(video_in, fpsnum=24, fpsden=1)
def slow(n, f):
    if n > 12:
        time.sleep(0.3)
    return f
core.std.ModifyFrame(clip, clip, slow).set_output()
"""

FROZEN = """
import time
clip = core.std.AssumeFPS(video_in, fpsnum=24, fpsden=1)
def stuck(n, f):
    if n > 12:
        time.sleep(4)
    return f
core.std.ModifyFrame(clip, clip, stuck).set_output()
"""


@pytest.mark.parametrize("body", [SLOW, FROZEN], ids=["audio-over-slow-video", "frozen"])
async def test_stalled_filter_rolls_back(
    live_env: Live, clip: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    env = live_env
    monkeypatch.setattr(generator, "template_text", lambda: _template_with(body))
    sid = await env.play(clip, "mv")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ROLLED_BACK, timeout=25)
    assert snap.health is Health.STALLED and snap.health_code is ErrorCode.FILTER_STALLED
    await asyncio.sleep(0.05)
    changed = [e for e in env.events if isinstance(e, HealthChanged) and e.sid == sid]
    assert changed and changed[-1].health is Health.STALLED and changed[-1].auto_action is not None
    assert changed[-1].evidence
    # a stuck filter can block mpv; the removal goes through once mpv answers
    vf = None
    for _ in range(30):
        with contextlib.suppress(TimeoutError):
            vf = await env.vf(sid)
            if _ours(vf) is None:
                break
        await asyncio.sleep(0.5)
    assert _ours(vf) is None


RIFE_ONLY_FROZEN = """
import json, time
clip = core.std.AssumeFPS(video_in, fpsnum=24, fpsden=1)
if json.loads(user_data)["backend"] == "rife-ncnn":
    def stuck(n, f):
        if n > 12:
            time.sleep(4)
        return f
    clip = core.std.ModifyFrame(clip, clip, stuck)
clip.set_output()
"""


async def test_rife_stall_switches_to_cpu(
    live_env: Live, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11.9: a stalled RIFE-ncnn filter is replaced by MVTools once, in place."""
    if not (PLUGIN_DIR / "mvtools.so").is_file() or not (PLUGIN_DIR / "librife.so").is_file():
        pytest.skip("needs buttereye-vs-mvtools and buttereye-vs-rife-ncnn")
    env = live_env
    monkeypatch.setattr(generator, "template_text", lambda: _template_with(RIFE_ONLY_FROZEN))
    sid = await env.play(clip, "rife")
    await env.wait(sid, lambda x: x.backend is BackendId.RIFE_NCNN, timeout=20)
    snap = await env.wait(
        sid,
        lambda x: x.backend is BackendId.MVTOOLS and x.filter is FilterState.ACTIVE,
        timeout=30,
    )
    assert snap.health is Health.OK and snap.notice is not None
    assert "switched this video to CPU smoothing" in snap.notice.key
    entry = _ours(await env.vf(sid))
    assert entry is not None and json.loads(entry["params"]["user-data"])["backend"] == "mvtools"
    changed = [e for e in env.events if isinstance(e, HealthChanged) and e.sid == sid]
    assert changed[0].health in (Health.STALLED, Health.DEVICE_LOST)
    assert changed[0].auto_action is not None and "CPU" in changed[0].auto_action.key
    await asyncio.sleep(2.0)  # MVTools keeps running
    snap = await env.snap(sid)
    assert snap.backend is BackendId.MVTOOLS and snap.filter is FilterState.ACTIVE, snap


async def test_helper_toggle_key(live_env: Live, clip: Path) -> None:
    """Alt+b in mpv → Lua helper → script-message → ButterEye turns the filter off."""
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    sock = env.s(sid).socket
    # a raw test client presses the helper's binding (not ButterEye's allowlisted IPC)
    reader, writer = await asyncio.open_unix_connection(os.fspath(sock))
    try:
        cmd = {"command": ["script-binding", "buttereye/toggle"], "request_id": 1}
        writer.write((json.dumps(cmd) + "\n").encode())
        await writer.drain()
        snap = await env.wait(sid, lambda x: x.filter is FilterState.OFF, timeout=5)
        assert snap.gen == 1
        await asyncio.sleep(1.0)  # the helper got the ack: no local toggle on top
        assert _ours(await env.vf(sid)) is None
    finally:
        writer.close()


async def test_mpv_quit_ends_session(live_env: Live, clip: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    pid = env.s(sid).pid or 0
    os.killpg(pid, signal.SIGTERM)
    await env.wait(sid, lambda x: x.ended, timeout=10)
    await asyncio.sleep(0.05)
    assert any(isinstance(e, SessionEnded) and e.reason == "mpv_exited" for e in env.events)


@pytest.mark.gpu
async def test_rife_live_short(live_env: Live, clip: Path) -> None:
    """RIFE-ncnn in mpv (bundled ncnn build), short: active, frames flow, no stall."""
    if not (PLUGIN_DIR / "librife.so").is_file():
        pytest.skip("buttereye-vs-rife-ncnn not installed")
    env = live_env
    sid = await env.play(clip, "rife")
    snap = await env.wait(sid, lambda x: x.filter is not FilterState.PENDING, timeout=30)
    assert snap.filter is FilterState.ACTIVE, snap
    assert snap.backend is BackendId.RIFE_NCNN and snap.model == "rife-v4.22_lite_ensembleFalse"
    entry = _ours(await env.vf(sid))
    assert entry is not None
    assert entry["params"]["concurrent-frames"] == "8"  # §4.4: mpv concurrent-frames 8
    ud = json.loads(entry["params"]["user-data"])
    assert ud["gpu_thread"] == 4 and ud["backend"] == "rife-ncnn"  # plugin gpu_thread stays 4
    # the doctor's BE-1030 window starts at this RIFE session, with mpv's pid
    marker = gpufault.read_session_marker(env.paths)
    assert marker is not None and env.s(sid).pid in marker.pids
    await asyncio.sleep(3.0)
    snap = await env.snap(sid)
    assert snap.filter is FilterState.ACTIVE and snap.health is not Health.STALLED, snap
    pid = env.s(sid).pid or 0
    os.killpg(pid, signal.SIGTERM)
    await env.wait(sid, lambda x: x.ended, timeout=15)
    # Xid check after the session (F1): a fault is reported as GPU_FAULT; an
    # unavailable or unreadable check is reported as unknown, never as OK.
    lines = await gpu_fault_lines(since_monotonic=env.s(sid).started_mono)
    await asyncio.sleep(0.5)
    xid = [e for e in env.events if isinstance(e, HealthChanged) and e.health is Health.GPU_FAULT]
    unknown = [e for e in env.events if isinstance(e, Notice) and e.sid == sid and e.code is None
               and "GPU faults" in e.message.key]  # fmt: skip
    if lines is None:
        assert unknown
    elif lines:
        assert xid and xid[-1].code is ErrorCode.RIFE_GPU_FAULT
    else:
        assert not xid and not unknown


# ---------------------------------------------------------------------------
# Review fixes (another file, display change, Setup engine, lifetime, hangs)
# ---------------------------------------------------------------------------


async def test_next_file_redecides_hdr(live_env: Live, clip: Path, clip_hdr30: Path) -> None:
    """loadfile replace in the same mpv: SDR 24 fps → HDR10 30 fps must bypass (F7),
    never keep the old filter with src_fps 24 and matrix 170m."""
    env = live_env
    sid = await env.play(clip, "mv")
    first = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    assert first.source is not None and first.source.fps == 24
    _raw(env.s(sid).socket, "loadfile", os.fspath(clip_hdr30), "replace")
    snap = await env.wait(
        sid, lambda x: x.filter is FilterState.BYPASSED and x.source is not None, timeout=15
    )
    assert snap.bypass is BypassReason.HDR_SKIP, snap
    assert snap.source is not None and snap.source.fps == 30
    assert snap.source.path == os.fspath(clip_hdr30) and snap.title == clip_hdr30.name
    assert snap.target_fps is None and snap.multiplier is None
    assert _ours(await env.vf(sid)) is None


async def test_next_file_new_rate(live_env: Live, clip: Path, clip30: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    _raw(env.s(sid).socket, "loadfile", os.fspath(clip30), "replace")
    snap = await env.wait(
        sid,
        lambda x: x.filter is FilterState.ACTIVE and x.source is not None and x.source.fps == 30,
        timeout=20,
    )
    assert snap.target_fps == Fraction(60) and snap.multiplier == Fraction(2), snap
    entry = _ours(await env.vf(sid))
    assert entry is not None
    ud = json.loads(entry["params"]["user-data"])
    assert ud["src_fps"] == [30, 1] and ud["title"] == clip30.name and ud["gen"] == snap.gen


async def test_display_change_redecides_bypass(live_env: Live, clip: Path) -> None:
    """ALREADY_AT_RATE at 24 Hz; the window moves to a 60 Hz screen → smoothing starts."""
    env = live_env
    env.reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=24")
    env.reg.options.display_debounce_s = 0.5
    sid = await env.play(clip, "mv")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.BYPASSED)
    assert snap.bypass is BypassReason.ALREADY_AT_RATE
    _raw(env.s(sid).socket, "set_property", "display-fps-override", 60)
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE, timeout=15)
    assert snap.target_fps == Fraction(60) and snap.bypass is None


async def test_setup_engine_choice_applies_to_builtin_rules(
    live_env: Live, clip: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setup chose MVTools: the default rule sends this clip to Quality (shipped
    with RIFE), which must follow the Setup choice."""
    if not (PLUGIN_DIR / "mvtools.so").is_file():
        pytest.skip("buttereye-vs-mvtools not installed")
    base = _config()
    cfg = dataclasses.replace(
        base, general=dataclasses.replace(base.general, backend_override=BackendId.MVTOOLS)
    )
    mod = sys.modules["buttereye.core.profiles.config"]
    monkeypatch.setattr(mod, "load", lambda paths: sc.config_load(cfg, revision="rev-mv"))
    env = live_env
    assert (await env.core.load_config()).config.general.backend_override is BackendId.MVTOOLS
    sid = await env.play(clip, None)
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    assert snap.profile_id == "quality" and snap.backend is BackendId.MVTOOLS, snap
    assert snap.model is None


async def test_detached_mpv_survives_core_gc(live_env: Live, clip: Path) -> None:
    """mpv's lifetime is decided at spawn (§4.10): dropping every ButterEye object
    that referred to it must not kill it."""
    import gc

    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    pid = env.s(sid).pid or 0
    report = await env.core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)
    assert report.detached == (sid,)
    env.held.clear()
    live.registry(env.core._ctx).sessions.clear()
    gc.collect()
    await asyncio.sleep(0.5)
    gc.collect()
    assert _alive(pid), "mpv was killed when ButterEye dropped its objects"


CLI_SCRIPT = """
import asyncio, os, sys
from pathlib import Path
from buttereye.core.api import ButterEye
from buttereye.core.mpvctl import session as live
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import DetachPolicy

async def main() -> None:
    core = await ButterEye.open(sc.fake_paths(env=os.environ))
    live.registry(core._ctx).options.extra_args = ("--vo=null", "--ao=null")
    sid = await core.play(Path(sys.argv[1]), profile_id=None).result()
    print(live.registry(core._ctx).sessions[sid].pid, flush=True)
    await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)

asyncio.run(main())
"""


async def test_mpv_survives_asyncio_run_exit(live_env: Live, clip: Path) -> None:
    """A CLI (asyncio.run, interpreter exit) must leave the player running."""
    env = live_env
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", CLI_SCRIPT, os.fspath(clip),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=dict(os.environ),
    )  # fmt: skip
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()
    pid = int(out.decode().split()[0])
    env.pids.append(pid)
    await asyncio.sleep(1.0)
    assert _alive(pid), "mpv died with the ButterEye process"


async def test_hung_mpv_is_reported_and_detach_is_quick(live_env: Live, clip: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    await env.core.set_interpolation(sid, False)
    pid = env.s(sid).pid or 0
    os.kill(pid, signal.SIGSTOP)
    try:
        snap = await env.wait(sid, lambda x: x.health is Health.CONNECTION_LOST, timeout=10)
        assert not snap.ended
        changed = [e for e in env.events if isinstance(e, HealthChanged) and e.sid == sid]
        assert changed and changed[-1].health is Health.CONNECTION_LOST
        assert "not responding" in changed[-1].reason.key
        with pytest.raises(ButterEyeError) as info:  # a change would only time out
            await env.core.set_interpolation(sid, True)
        assert info.value.code is ErrorCode.IPC_LOST
    finally:
        os.kill(pid, signal.SIGCONT)
    snap = await env.wait(sid, lambda x: x.health is Health.OK, timeout=10)
    assert not snap.ended
    # hung again with the filter on: detach stays well inside close()'s 5 s budget
    await env.core.set_interpolation(sid, True)
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    os.kill(pid, signal.SIGSTOP)
    try:
        await asyncio.sleep(5.0)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await env.core.detach(sid, DetachPolicy.DISABLE_FILTER)
        assert loop.time() - t0 < 4.0
        assert env.s(sid).snapshot().ended
    finally:
        os.kill(pid, signal.SIGCONT)
    assert _alive(pid)


async def test_mpv_exit_removes_runtime_files_and_log_is_quiet(live_env: Live, clip: Path) -> None:
    env = live_env
    sid = await env.play(clip, "mv")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE)
    s = env.s(sid)
    await asyncio.sleep(2.0)
    assert s.socket.exists() and s.script_path.exists()
    log = env.paths.logs_dir / f"mpv-{sid}.log"
    assert "AV:" not in log.read_text(errors="replace")  # no terminal status line
    os.killpg(s.pid or 0, signal.SIGTERM)
    await env.wait(sid, lambda x: x.ended, timeout=10)
    assert not s.socket.exists() and not s.script_path.exists()


async def test_socket_moved_by_mpvsockets_is_adopted(tmp_path: Path) -> None:
    """The mpvSockets user script replaces ``input-ipc-server`` as it loads, so the
    socket ButterEye asked for refuses connections. ``connect`` adopts
    ``<dir>/<pid>`` instead, and only for the mpv it started (SCOPE §4.3)."""
    from buttereye.core.mpvctl import launcher

    moved = tmp_path / "mpvSockets"
    moved.mkdir(mode=0o700)
    script = tmp_path / "move.lua"
    script.write_text(
        "local utils = require 'mp.utils'\n"
        f"mp.set_property('options/input-ipc-server', '{moved}/' .. utils.getpid())\n"
    )
    runtime = tmp_path / "rt"
    runtime.mkdir(mode=0o700)
    ours = runtime / "mpv-test.sock"
    argv = [
        "mpv", "--no-config", "--idle=yes", "--vo=null", "--ao=null",
        f"--input-ipc-server={ours}", f"--script={script}",
    ]  # fmt: skip
    proc = await launcher.spawn_mpv(argv, tmp_path / "mpv.log")
    try:
        target = moved / str(proc.pid)
        for _ in range(200):  # the script has run: our own socket is dead now
            if target.exists():
                break
            await asyncio.sleep(0.02)
        assert target.exists()
        with pytest.raises(ConnectionRefusedError):
            (await MpvIpc.connect(ours, timeout_s=0.5))  # noqa: B018 - must refuse
        ipc = await launcher.connect(
            ours, proc, tmp_path / "mpv.log", timeout_s=3.0, mpvsockets_dir=moved
        )
        try:
            assert ipc.path == target and ipc.peer_pid == proc.pid
            ipc.start()
            assert await ipc.command("get_property", "pid") == proc.pid
        finally:
            await ipc.close()
        # turned off: our dead socket is all there is, so connecting times out
        with pytest.raises(ButterEyeError) as info:
            await launcher.connect(
                ours, proc, tmp_path / "mpv.log", timeout_s=0.5, mpvsockets_dir=None
            )
        assert info.value.code is ErrorCode.IPC_LOST
    finally:
        await launcher.terminate(proc, grace_s=2.0)


async def test_too_demanding_video_smooths_at_a_smaller_size(
    live_env: Live, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 4K video MVTools can't double live is smoothed at 1080p (§11.9): the
    filter gets ``size`` and frames keep flowing."""
    if not (PLUGIN_DIR / "mvtools.so").is_file():
        pytest.skip("buttereye-vs-mvtools not installed")
    from tests.test_live_decisions import _m, _result

    # MVTools 80 fps at 1080p: ~16 live at 4K, ~31 at 1440p, ~51 at 1080p (≥ 48)
    bench = (_result(_m("mvtools", 80.0, backend=BackendId.MVTOOLS), gpu=None),)

    async def history(ctx: Any) -> tuple[BenchResult, ...]:
        return bench

    mod = pytypes.ModuleType("buttereye.core.bench.runner")
    mod.history = history  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.bench.runner", mod)
    big = _make_clip(tmp_path / "big.mkv", rate=24, seconds=12, vf="scale=3840:2160")
    env = live_env
    sid = await env.play(big, "mv2x")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE, timeout=30.0)
    assert snap.backend is BackendId.MVTOOLS and snap.target_fps == 48
    assert snap.notice is not None and "1080p" in snap.notice.key.format(**snap.notice.params)
    entry = _ours(await env.vf(sid))
    assert entry is not None
    assert json.loads(entry["params"]["user-data"])["size"] == [1920, 1080]
    # frames keep flowing at the smaller size for a few seconds
    await asyncio.sleep(3.0)
    assert env.s(sid).snapshot().filter is FilterState.ACTIVE
    assert env.s(sid).snapshot().health is Health.OK
