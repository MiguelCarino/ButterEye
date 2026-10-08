# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live-play review fixes without a real mpv: benchmark cap, GPU override,
Xid cursor, start deadline, Setup engine choice, user models, mpv lifetime and
runtime-file hygiene (the end-to-end runs are in tests/integration/test_live_play.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import os
import signal
import socket
import sys
import time
import types as pytypes
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.events import Event, HealthChanged, Notice
from buttereye.core.mpvctl import decide, health, launcher
from buttereye.core.mpvctl import session as live
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
    SessionId,
    SourceFacts,
    TargetKind,
    VulkanDevice,
)

NTSC = Fraction(24000, 1001)
UUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UUID_B = "bbbbbbbb-0000-0000-0000-000000000002"
DEVICES = (
    VulkanDevice(0, UUID_A, "iGPU", "intel", "integrated", 1 << 30),
    VulkanDevice(1, UUID_B, "RTX", "nvidia", "discrete", 24 << 30),
)


class FakeCtx:
    def __init__(
        self, paths: Any, cfg: Config | None = None, devices: tuple[VulkanDevice, ...] = ()
    ) -> None:
        self.paths = paths
        self.cfg = cfg
        self.report = (
            pytypes.SimpleNamespace(hardware=pytypes.SimpleNamespace(vulkan=devices))
            if devices
            else None
        )
        self.events: list[Event] = []
        self.history: tuple[BenchResult, ...] = ()
        self._state: dict[str, Any] = {}

    def config(self) -> Config:
        assert self.cfg is not None
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
        return asyncio.get_running_loop().create_task(coro, name=name)

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


def _session(tmp_path: Path, ctx: FakeCtx, **opts: Any) -> live._Session:
    options = dataclasses.replace(live.LiveOptions(), **opts)
    proc = pytypes.SimpleNamespace(pid=os.getpid(), returncode=None)
    return live._Session(
        ctx,  # type: ignore[arg-type]
        options,
        sid=SessionId("s1"),
        file=tmp_path / "film.mkv",
        proc=proc,  # type: ignore[arg-type]
        ipc=FakeIpc(),  # type: ignore[arg-type]
        socket=tmp_path / "mpv-s1.sock",
        log_path=tmp_path / "mpv-s1.log",
        script_path=tmp_path / "mpv-s1.vpy",
        profile_request=None,
    )


def _facts(height: int = 1080, fps: Fraction = NTSC) -> SourceFacts:
    return SourceFacts(fps, height * 16 // 9, height, HdrClass.SDR, 60.0, False, "/f.mkv", False)


M26 = "rife-v4.26_ensembleFalse"


def _m(mpv: float, vspipe: float, *, realtime: bool, failed_mpv: bool = False,
       faults: int | None = 0, model: str = M26) -> BenchMeasurement:  # fmt: skip
    return BenchMeasurement(
        label=model, backend=BackendId.RIFE_NCNN, model=model, vspipe_fps=vspipe,
        mpv_fps=vspipe if failed_mpv else mpv, cov=0.01, repeatable=True,
        startup_s=0.9 if failed_mpv else 2.5, reload_s=0.9, vram_bytes=None,
        realtime=realtime, gpu_faults=faults,
    )  # fmt: skip


def _result(when: datetime, *ms: BenchMeasurement, gpu: str | None = UUID_B) -> BenchResult:
    return BenchResult(BenchRequest(1920, 1080, NTSC), ms, None, when, gpu)


@pytest.fixture
def bench_history(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[BenchResult]]:
    results: list[BenchResult] = []

    async def history(ctx: Any) -> tuple[BenchResult, ...]:
        return tuple(results)

    mod = pytypes.ModuleType("buttereye.core.bench.runner")
    mod.history = history  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.bench.runner", mod)
    yield results


# ---------------------------------------------------------------------------
# Finding 3: benchmark cap
# ---------------------------------------------------------------------------


async def test_cap_ignores_failed_mpv_pass_and_keeps_headroom(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config(), DEVICES)
    s = _session(tmp_path, ctx)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    bench_history += [
        _result(now, _m(0, 72.0, realtime=False, failed_mpv=True)),  # vspipe speed only
        _result(now - timedelta(days=1), _m(46.0, 72.0, realtime=False)),  # measured in mpv
    ]
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, "rife-v4.26_ensembleFalse", _facts(),
                                  sc.default_config())  # fmt: skip
    assert cap == pytest.approx(46.0 / live.LIVE_HEADROOM)
    tc = decide.choose_target(NTSC, 60.0, TargetKind.DISPLAY, sustainable_fps=cap)
    # 60 and 30 both infer almost every frame of 23.976 (≥ 60 at 2x): too much
    assert tc.target is None and tc.bypass is BypassReason.NO_REALTIME
    # the newest confirmed result wins over a faster older one
    bench_history.append(_result(now - timedelta(days=5), _m(80.0, 90.0, realtime=True)))
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, "rife-v4.26_ensembleFalse", _facts(),
                                  sc.default_config())  # fmt: skip
    assert cap == pytest.approx(46.0 / live.LIVE_HEADROOM)


async def test_cap_skips_other_gpu_and_faults(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config(), DEVICES)
    s = _session(tmp_path, ctx)
    now = datetime(2026, 10, 1, tzinfo=UTC)
    bench_history += [
        _result(now, _m(200.0, 220.0, realtime=True), gpu=UUID_A),  # the other GPU
        _result(now, _m(150.0, 220.0, realtime=True, faults=2)),  # faulted
    ]
    model = "rife-v4.26_ensembleFalse"
    assert await s.sustainable_fps(BackendId.RIFE_NCNN, model, _facts(), ctx.cfg) is None
    bench_history.append(_result(now - timedelta(hours=1), _m(69.0, 80.0, realtime=True),
                                 gpu=UUID_B.upper()))  # fmt: skip
    cap = await s.sustainable_fps(BackendId.RIFE_NCNN, model, _facts(), ctx.cfg)
    assert cap == pytest.approx(69.0 / live.LIVE_HEADROOM)


# ---------------------------------------------------------------------------
# Finding 4: GPU override as index / any-case UUID
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("override", ["1", UUID_B.upper(), f"  {UUID_B}  ", None])
def test_gpu_override_forms(tmp_path: Path, override: str | None) -> None:
    cfg = sc.default_config(gpu=override)
    s = _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), cfg, DEVICES))
    assert s.gpu_index(cfg) == 1  # the discrete RTX; with no override, doctor's choice
    assert s.gpu_device(cfg) == DEVICES[1]


def test_gpu_override_index_zero_and_no_report(tmp_path: Path) -> None:
    cfg = sc.default_config(gpu="0")
    assert _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), cfg, DEVICES)).gpu_index(cfg) == 0
    assert _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), cfg)).gpu_index(cfg) is None


# ---------------------------------------------------------------------------
# Finding 5: each Xid reported once
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Ev:
    message: str
    realtime_us: int
    monotonic_us: int


class _Scan:
    def __init__(self, events: tuple[_Ev, ...]) -> None:
        self.readable = True
        self.events = events

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(e.message for e in self.events)


async def test_xid_reported_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    journal: list[_Ev] = []
    calls: list[dict[str, Any]] = []

    async def scan_xid(*, since_monotonic: float, since_wall: float | None) -> _Scan:
        calls.append({"mono": since_monotonic, "wall": since_wall})
        return _Scan(tuple(e for e in journal if e.realtime_us >= (since_wall or 0) * 1e6
                           and e.monotonic_us >= since_monotonic * 1e6))  # fmt: skip

    mod = pytypes.ModuleType(health.GPUFAULT_MODULE)
    mod.scan_xid = scan_xid  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, health.GPUFAULT_MODULE, mod)
    monkeypatch.setattr(health, "JOURNAL_SETTLE_S", 0.0)
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config(), DEVICES)
    s = _session(tmp_path, ctx)
    t_wall, t_mono = s.started_wall, s.started_mono
    journal.append(_Ev("NVRM: Xid (PCI:0000:01:00): 13, pid=9, name=vo",
                       int((t_wall + 1) * 1e6), int((t_mono + 1) * 1e6)))  # fmt: skip

    def faults() -> list[HealthChanged]:
        return [e for e in ctx.events if isinstance(e, HealthChanged)
                and e.health is Health.GPU_FAULT]  # fmt: skip

    await s.check_gpu_faults()
    assert len(faults()) == 1 and s.health is Health.GPU_FAULT
    await s.check_gpu_faults()  # a later failure, or the session end
    await s.check_gpu_faults()
    assert len(faults()) == 1
    # the cursor moved past the reported event (its journal time, in whole µs)
    assert calls[-1]["wall"] > journal[0].realtime_us / 1e6
    journal.append(_Ev("NVRM: Xid (PCI:0000:01:00): 109, pid=9, name=vo",
                       int((t_wall + 5) * 1e6), int((t_mono + 5) * 1e6)))  # fmt: skip
    await s.check_gpu_faults()
    got = faults()
    assert len(got) == 2 and got[-1].evidence == (
        "NVRM: Xid (PCI:0000:01:00): 109, pid=9, name=vo",
    )


# ---------------------------------------------------------------------------
# Finding 12: start deadline
# ---------------------------------------------------------------------------


def test_start_deadline_notice(tmp_path: Path) -> None:
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config())
    s = _session(tmp_path, ctx, start_deadline_s=15.0)
    t0 = s.file_started_mono
    s.check_start_deadline(t0 + 10.0)
    assert s.notice is None and not ctx.events
    s.check_start_deadline(t0 + 15.5)
    assert s.notice is not None and "hasn't opened the video" in s.notice.key
    notices = [e for e in ctx.events if isinstance(e, Notice)]
    assert len(notices) == 1 and notices[0].sid == "s1"
    s.check_start_deadline(t0 + 20.0)
    assert len([e for e in ctx.events if isinstance(e, Notice)]) == 1  # once
    # the file loads and plays: the notice goes away
    s.file_loaded_at = t0 + 21.0
    s.props["playback-time"] = 0.5
    s.check_start_deadline(t0 + 21.5)
    assert s.notice is None and s.start_notice is None


def test_start_deadline_cleared_when_filter_leaves_pending(tmp_path: Path) -> None:
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config())
    s = _session(tmp_path, ctx, start_deadline_s=1.0)
    s.check_start_deadline(s.file_started_mono + 2.0)
    assert s.start_notice is not None
    s.filter = FilterState.OFF
    s.check_start_deadline(s.file_started_mono + 3.0)
    assert s.notice is None


# ---------------------------------------------------------------------------
# Finding 1: another file in the same mpv
# ---------------------------------------------------------------------------


async def test_start_file_resets_decisions(tmp_path: Path) -> None:
    ctx = FakeCtx(sc.fake_paths(tmp_path), sc.default_config())
    s = _session(tmp_path, ctx)
    entry = {"label": "buttereye", "name": "vapoursynth", "enabled": True}
    s.props.update(
        {"path": "/a24.mkv", "container-fps": 24.0, "video-params": {"w": 2}, "vf": [entry]}
    )
    s.source, s.active_gen, s.filter = _facts(fps=Fraction(24)), 3, FilterState.ACTIVE
    s.target, s.multiplier, s.applied_display = Fraction(60), Fraction(5, 2), 60.0
    s.on_event({"event": "start-file", "playlist_entry_id": 2})
    assert "path" not in s.props and "video-params" not in s.props and "vf" in s.props
    assert s.new_file and s.file_loaded_at is None
    await s.begin_new_file()
    assert ("vf", "remove", "@buttereye") in s.ipc.commands  # type: ignore[attr-defined]
    assert s.active_gen is None and s.source is None and s.target is None
    assert s.filter is FilterState.PENDING and s.applied_display is None


def test_file_changed_safety_net(tmp_path: Path) -> None:
    s = _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), sc.default_config()))
    s.source = dataclasses.replace(_facts(360, Fraction(24)), width=640, path="/a.mkv")
    s.props.update({"path": "/a.mkv", "container-fps": 24.0,
                    "video-params": {"w": 640, "h": 360, "gamma": "bt.1886"}})  # fmt: skip
    assert not s.file_changed()
    s.props["video-params"] = {"w": 640, "h": 360, "gamma": "pq"}
    assert s.file_changed()
    s.props["video-params"] = {"w": 640, "h": 360}
    s.props["container-fps"] = 30.0
    assert s.file_changed()


# ---------------------------------------------------------------------------
# Finding 11: Setup's engine choice
# ---------------------------------------------------------------------------


def test_setup_choice_applies_to_shipped_profiles(tmp_path: Path) -> None:
    cfg = sc.default_config(backend_override=BackendId.MVTOOLS)
    s = _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), cfg))
    by_id = {p.id: p for p in cfg.profiles}
    for pid in ("quality", "balanced", "fast"):
        assert s.wanted_backend(by_id[pid], cfg) == (BackendId.MVTOOLS, None)
    # a profile the user pinned keeps its engine, and says why
    pinned = dataclasses.replace(by_id["quality"], id="mine", name="Mine", builtin=False)
    wanted, note = s.wanted_backend(pinned, cfg)
    assert wanted is BackendId.RIFE_NCNN and note is not None and "Automatic" in note.key
    # CPU stays MVTools; automatic follows Setup; no override → the profile's engine
    rife_cfg = sc.default_config(backend_override=BackendId.RIFE_NCNN)
    assert s.wanted_backend(by_id["cpu"], rife_cfg)[0] is BackendId.MVTOOLS
    auto = dataclasses.replace(pinned, backend="auto")
    assert s.wanted_backend(auto, cfg) == (BackendId.MVTOOLS, None)
    assert s.wanted_backend(by_id["quality"], sc.default_config()) == (BackendId.RIFE_NCNN, None)
    # a shipped profile the user switched to another engine counts as pinned
    moved = dataclasses.replace(by_id["fast"], backend=BackendId.RIFE_TRT)
    assert s.wanted_backend(moved, cfg)[0] is BackendId.RIFE_TRT


# ---------------------------------------------------------------------------
# Finding 6: user-dir models
# ---------------------------------------------------------------------------


def test_user_dir_models(tmp_path: Path) -> None:
    plugins, packaged, user = tmp_path / "plugins", tmp_path / "pkg", tmp_path / "user"
    plugins.mkdir()
    (plugins / "librife.so").write_bytes(b"")
    user.mkdir()
    assert decide.installed_backends(plugins, packaged, (user,)) == frozenset()
    mine = user / "rife-v4.6-custom"
    mine.mkdir()
    (mine / "flownet.param").write_text("x")
    (mine / "flownet.bin").write_bytes(b"x")
    assert BackendId.RIFE_NCNN in decide.installed_backends(plugins, packaged, (user,))
    choice = decide.find_model("rife-v4.6-custom", packaged, (user,))
    assert choice.name == "rife-v4.6-custom" and choice.path == mine and choice.notice is None
    # an ONNX (TensorRT) folder or a symlink is not an ncnn model
    onnx = user / "rife-trt"
    onnx.mkdir()
    (onnx / "flownet.param").write_text("x")
    (onnx / "rife.onnx").write_bytes(b"x")
    (user / "link").symlink_to(mine)
    packaged.mkdir()
    (packaged / "rife-v4.26_ensembleFalse").mkdir()
    for name in ("rife-trt", "link", "../user/rife-v4.6-custom"):
        got = decide.find_model(name, packaged, (user,))
        assert got.name == "rife-v4.26_ensembleFalse" and got.notice is not None
    # packaged wins over a user folder of the same name
    (user / "rife-v4.26_ensembleFalse").mkdir()
    (user / "rife-v4.26_ensembleFalse" / "flownet.param").write_text("x")
    got = decide.find_model("rife-v4.26_ensembleFalse", packaged, (user,))
    assert got.path == packaged / "rife-v4.26_ensembleFalse"


# ---------------------------------------------------------------------------
# Findings 7 and 9: mpv lifetime, logs and runtime files
# ---------------------------------------------------------------------------


def _state(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        return None


async def test_spawned_process_survives_gc_and_is_reaped(tmp_path: Path) -> None:
    proc = await launcher.spawn_mpv(["sleep", "30"], tmp_path / "logs" / "x.log")
    pid = proc.pid
    try:
        assert os.getpgid(pid) == pid  # own session / process group
        del proc
        gc.collect()
        await asyncio.sleep(0.2)
        assert _state(pid) not in (None, "Z", "X"), "dropping the handle killed the child"
    finally:
        os.killpg(pid, signal.SIGKILL)
    for _ in range(100):  # the pidfd watcher reaps it: no zombie left behind
        if _state(pid) is None:
            break
        await asyncio.sleep(0.02)
    assert _state(pid) is None


async def test_mpv_process_wait_and_terminate(tmp_path: Path) -> None:
    log = tmp_path / "x.log"
    proc = await launcher.spawn_mpv(["sh", "-c", "echo hello; exit 3"], log)
    assert await asyncio.wait_for(proc.wait(), 5) == 3 and proc.returncode == 3
    assert log.read_text() == "hello\n"
    proc = await launcher.spawn_mpv(["sleep", "30"], log)
    await launcher.terminate(proc, grace_s=2.0)
    assert proc.returncode == -signal.SIGTERM
    with pytest.raises(Exception) as info:
        await launcher.spawn_mpv([os.fspath(tmp_path / "no-mpv")], log)
    assert "mpv was not found" in str(getattr(info.value, "cause", info.value))


def test_argv_has_no_status_line(tmp_path: Path) -> None:
    argv = launcher.build_argv("mpv", include=tmp_path / "i.conf", socket=tmp_path / "s",
                               file=Path("f.mkv"), extra=("--vo=null",))  # fmt: skip
    assert "--term-status-msg=" in argv
    assert argv.index("--term-status-msg=") < argv.index("--vo=null") < argv.index("--")


def test_prune_logs(tmp_path: Path) -> None:
    logs, run = tmp_path / "logs", tmp_path / "run"
    logs.mkdir()
    run.mkdir()
    now = time.time()
    for i in range(8):
        p = logs / f"mpv-s{i}.log"
        p.write_text("x")
        os.utime(p, (now - i * 60, now - i * 60))
    old = logs / "mpv-old.log"
    old.write_text("x")
    os.utime(old, (now - 30 * 86400, now - 30 * 86400))
    (run / "mpv-s7.sock").write_text("")  # its player may still run
    (logs / "setup-1.log").write_text("x")
    removed = {p.name for p in launcher.prune_logs(logs, run, keep=5, now=now)}
    assert removed == {"mpv-s5.log", "mpv-s6.log", "mpv-old.log"}
    assert (logs / "mpv-s7.log").exists() and (logs / "setup-1.log").exists()


def test_cap_log(tmp_path: Path) -> None:
    log = tmp_path / "mpv-x.log"
    log.write_bytes(b"a" * 100)
    assert not launcher.cap_log(log, max_bytes=200)
    log.write_bytes(b"a" * 300)
    assert launcher.cap_log(log, max_bytes=200)
    assert log.stat().st_size < 200 and b"truncated" in log.read_bytes()


async def test_discover_removes_dead_sockets(tmp_path: Path, xdg_env: Any) -> None:
    paths = sc.fake_paths(env=os.environ)
    run = launcher.ensure_private_dir(paths.runtime_dir)
    dead = run / "mpv-dead.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(os.fspath(dead))
    srv.close()  # the file stays, nobody listens: a crashed mpv
    (run / "mpv-dead.vpy").write_text("x")
    (run / "mpv-other.vpy").write_text("x")
    ctx = FakeCtx(paths, sc.default_config())
    assert await live.discover(ctx) == ()  # type: ignore[arg-type]
    assert not dead.exists() and not (run / "mpv-dead.vpy").exists()
    assert (run / "mpv-other.vpy").exists()


def test_partial_video_params_are_pending_not_unsupported() -> None:
    assert not decide.params_ready({"w": 640, "h": 360})
    assert decide.params_ready({"w": 640, "h": 360, "pixelformat": "yuv420p"})


async def test_apply_waits_for_full_video_params(tmp_path: Path) -> None:
    s = _session(tmp_path, FakeCtx(sc.fake_paths(tmp_path), sc.default_config()))
    s.props.update(
        {"container-fps": 24.0, "display-fps": 60.0, "video-params": {"w": 640, "h": 360}}
    )
    s.file_loaded_at = time.monotonic()
    await s.apply(raise_on_fail=False)
    assert s.filter is FilterState.PENDING and s.bypass is None


def test_realtime_headroom_matches_the_bench() -> None:
    """The live cap divides by the same headroom the bench uses to call a
    configuration real time (F2); the two copies must not drift apart."""
    from buttereye.core.bench import runner

    assert live.REALTIME_HEADROOM == runner.REALTIME_HEADROOM
