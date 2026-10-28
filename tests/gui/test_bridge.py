# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""CoreBridge rules 1-8 (docs/design/GUI.md §3, §6 ``test_bridge``)."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import shiboken6
from PySide6.QtCore import QObject

from buttereye.core.api import (
    ButterEye,
    ButterEyeError,
    DetachPolicy,
    ErrorCode,
    Msg,
    Notice,
    OperationCancelled,
    OpFinished,
    OpKind,
    OpState,
    Progress,
    ShutdownReport,
)
from buttereye.core.api import (
    Operation as CoreOperation,
)
from buttereye.core.testing import FakeCore
from buttereye.gui.bridge import CLOSE_MARGIN_S, CoreBridge, close_wait_s
from tests.gui.helpers import fake_core


class Owner(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.errors: list[ButterEyeError] = []

    def show_error(self, err: ButterEyeError) -> None:
        self.errors.append(err)


def test_ready_carries_capabilities(make_bridge: Any) -> None:
    bridge = make_bridge("devbox_now")
    caps = bridge.capabilities
    assert caps is not None
    from buttereye.core.api import Feature

    assert not caps.ok(Feature.LIVE)  # devbox_now: live is not in this build


def test_replies_run_on_gui_thread(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    threads: list[threading.Thread] = []
    values: list[Any] = []

    def ok(v: Any) -> None:
        threads.append(threading.current_thread())
        values.append(v)

    bridge.call(lambda core: core.hardware(), owner=owner, ok=ok)
    qtbot.waitUntil(lambda: bool(values), timeout=3000)
    assert threads == [threading.main_thread()]
    assert values[0].gpus  # HardwareInfo from the scenario


def test_errors_run_on_gui_thread_and_default_to_show_error(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("devbox_now")
    owner = Owner()
    bridge.call(lambda core: core.sessions(), owner=owner, ok=lambda v: None)
    qtbot.waitUntil(lambda: bool(owner.errors), timeout=3000)
    assert owner.errors[0].code is ErrorCode.NOT_IMPLEMENTED


def test_owner_destroyed_drops_reply(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    called: list[Any] = []

    async def slow(core: ButterEye) -> int:
        await asyncio.sleep(0.2)
        return 1

    bridge.call(slow, owner=owner, ok=called.append, err=called.append)
    shiboken6.delete(owner)
    qtbot.wait(500)
    assert called == []


def test_non_butter_eye_exception_becomes_internal(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    errors: list[ButterEyeError] = []

    async def boom(core: ButterEye) -> None:
        raise ValueError("kaput")

    bridge.call(boom, owner=owner, ok=lambda v: None, err=errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=3000)
    assert errors[0].code is ErrorCode.INTERNAL
    assert errors[0].detail is not None and "ValueError" in errors[0].detail


def test_cancel_reaches_operation_and_drops_reply(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    replies: list[Any] = []
    finished: list[OpFinished] = []
    bridge.event.connect(lambda ev: finished.append(ev) if isinstance(ev, OpFinished) else None)
    progress: list[Progress] = []
    ticket = bridge.run_op(
        lambda core: core.doctor(),
        owner=owner,
        ok=replies.append,
        err=replies.append,
        progress=progress.append,
    )
    qtbot.waitUntil(lambda: bool(progress), timeout=3000)
    ticket.cancel()
    assert ticket.cancelled
    qtbot.waitUntil(lambda: any(f.state is OpState.CANCELLED for f in finished), timeout=3000)
    qtbot.wait(150)
    assert replies == []
    assert owner.errors == []


def test_operation_cancelled_goes_to_err(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    errors: list[ButterEyeError] = []

    def start_and_cancel(core: ButterEye) -> CoreOperation[Any]:
        op = core.doctor()
        op.cancel()
        return op

    bridge.run_op(start_and_cancel, owner=owner, ok=lambda v: None, err=errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=3000)
    assert isinstance(errors[0], OperationCancelled)


def test_progress_flood_is_coalesced(make_bridge: Any, qtbot: Any) -> None:
    """10 000 progress updates in about 1 s -> at most 12 GUI updates, last = final."""
    bridge = make_bridge("all_ready")
    owner = Owner()
    seen: list[Progress] = []
    done: list[Any] = []
    total = 10_000

    def flood(core: ButterEye) -> CoreOperation[int]:
        async def body(op: Any) -> int:
            started = time.monotonic()
            for i in range(1, total + 1):
                op.report(Msg("flood"), done=i, total=total, unit="frames")
                if i % 10 == 0:
                    # pace to ~10 000/s
                    target = started + i / total
                    await asyncio.sleep(max(0.0, target - time.monotonic()))
            return total

        return CoreOperation(OpKind.BENCH, body)

    bridge.run_op(flood, owner=owner, ok=done.append, progress=seen.append)
    qtbot.waitUntil(lambda: bool(done), timeout=10_000)
    assert done == [total]
    assert 1 <= len(seen) <= 12, len(seen)
    assert seen[-1].done == total
    dones = [p.done or 0 for p in seen]
    assert dones == sorted(dones)


def test_events_arrive_in_order(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    got: list[str] = []
    bridge.event.connect(lambda ev: got.append(ev.message.key) if isinstance(ev, Notice) else None)

    async def emit_many(core: ButterEye) -> None:
        for i in range(50):
            core.fake_emit(Notice(Msg(f"n{i}")))  # type: ignore[attr-defined]

    bridge.call(emit_many, owner=owner, ok=lambda v: None)
    qtbot.waitUntil(lambda: len(got) == 50, timeout=3000)
    assert got == [f"n{i}" for i in range(50)]


def test_an_idle_bridge_runs_no_timer(make_bridge: Any, qtbot: Any) -> None:
    """P10: nothing in flight and nothing published -> no drain tick and no
    watchdog tick (before, 10 + 2 wakeups a second while the window sat idle)."""
    bridge = make_bridge("all_ready")
    qtbot.waitUntil(lambda: not bridge.watchdog_active, timeout=3000)
    assert not bridge._drain_timer.isActive()
    drains: list[float] = []
    real = bridge._drain
    bridge._drain = lambda: (drains.append(time.monotonic()), real())[1]  # type: ignore[method-assign]
    qtbot.wait(400)
    assert drains == []
    assert not bridge.watchdog_active


def test_events_wake_the_gui_and_drains_are_rate_limited(make_bridge: Any, qtbot: Any) -> None:
    """One wake per burst; bursts closer than DRAIN_INTERVAL_MS share a drain."""
    from buttereye.gui import bridge as bridge_mod

    bridge = make_bridge("all_ready")
    owner = Owner()
    got: list[str] = []
    bridge.event.connect(lambda ev: got.append(ev.message.key) if isinstance(ev, Notice) else None)
    drains: list[float] = []
    real = bridge._drain

    def counting() -> None:
        drains.append(time.monotonic())
        real()

    bridge._drain = counting  # type: ignore[method-assign]
    bridge._drain_timer.timeout.disconnect()
    bridge._drain_timer.timeout.connect(counting)

    async def emit_slowly(core: ButterEye) -> None:
        for i in range(20):
            core.fake_emit(Notice(Msg(f"n{i}")))  # type: ignore[attr-defined]
            await asyncio.sleep(0.01)

    bridge.call(emit_slowly, owner=owner, ok=lambda v: None)
    qtbot.waitUntil(lambda: len(got) == 20, timeout=3000)
    assert got == [f"n{i}" for i in range(20)]
    # 20 events over ~0.2 s: a handful of drains, never one per event
    assert 1 <= len(drains) <= 6, drains
    gaps = [b - a for a, b in zip(drains, drains[1:], strict=False)]
    assert all(g >= bridge_mod.DRAIN_INTERVAL_MS / 1000 * 0.9 for g in gaps), gaps


def test_the_watchdog_runs_while_a_request_is_in_flight(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    qtbot.waitUntil(lambda: not bridge.watchdog_active, timeout=3000)
    owner = Owner()
    release = threading.Event()
    done: list[bool] = []

    async def slow(_core: ButterEye) -> bool:
        await asyncio.to_thread(release.wait, 5)
        return True

    bridge.call(slow, owner=owner, ok=done.append)
    assert bridge.watchdog_active
    qtbot.wait(700)
    assert bridge.watchdog_active  # still waiting on the core
    release.set()
    qtbot.waitUntil(lambda: done == [True], timeout=3000)
    qtbot.waitUntil(lambda: not bridge.watchdog_active, timeout=3000)


def test_calls_before_start_are_queued(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready", start=False)
    owner = Owner()
    values: list[float] = []
    bridge.call(lambda core: core.ping(), owner=owner, ok=values.append)
    with qtbot.waitSignal(bridge.ready, timeout=5000):
        bridge.start()
    qtbot.waitUntil(lambda: bool(values), timeout=3000)


def test_capabilities_cache_follows_events(make_bridge: Any, qtbot: Any) -> None:
    from buttereye.core.api import CapState, Feature

    bridge = make_bridge("devbox_now")
    owner = Owner()
    assert not bridge.capabilities.ok(Feature.BENCH)

    async def enable(core: ButterEye) -> None:
        core.fake_set_capability(Feature.BENCH, CapState(True))  # type: ignore[attr-defined]

    bridge.call(enable, owner=owner, ok=lambda v: None)
    qtbot.waitUntil(lambda: bridge.capabilities.ok(Feature.BENCH), timeout=3000)


def test_fatal_on_open_failure(qtbot: Any) -> None:
    async def failing(paths: Any) -> ButterEye:
        raise ButterEyeError(ErrorCode.RUNTIME_DIR_UNSAFE, Msg("runtime dir unsafe"))

    bridge = CoreBridge(opener=failing, debug=True)
    owner = Owner()
    errors: list[ButterEyeError] = []
    try:
        with qtbot.waitSignal(bridge.fatal, timeout=3000) as blocker:
            bridge.start()
        assert blocker.args[0].code is ErrorCode.RUNTIME_DIR_UNSAFE
        bridge.call(lambda core: core.ping(), owner=owner, ok=lambda v: None, err=errors.append)
        qtbot.waitUntil(lambda: bool(errors), timeout=3000)
        assert errors[0].code is ErrorCode.RUNTIME_DIR_UNSAFE
        assert not bridge.is_ready
    finally:
        assert bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=False) is None


def test_shutdown_returns_report_and_passes_policy(make_bridge: Any) -> None:
    bridge = make_bridge("live_active")
    core = fake_core(bridge)
    report = bridge.shutdown(DetachPolicy.DISABLE_FILTER, cancel_jobs=True, timeout_s=3.0)
    assert isinstance(report, ShutdownReport)
    assert report.detached  # live_active has a session
    close_calls = [c for c in core.fake_calls if c.name == "close"]
    assert close_calls[0].args == (DetachPolicy.DISABLE_FILTER,)
    assert close_calls[0].kwargs["cancel_jobs"] is True
    assert bridge.is_closed
    # idempotent, and late calls are dropped quietly
    assert bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=False) is report
    owner = Owner()
    ticket = bridge.call(lambda c: c.ping(), owner=owner, ok=lambda v: None)
    assert ticket.cancelled


class _HangingClose(FakeCore):
    async def close(
        self, policy: Any = DetachPolicy.KEEP_FILTER, *, cancel_jobs: bool, timeout_s: float = 5.0
    ) -> ShutdownReport:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


def test_shutdown_timeout_returns_none(qtbot: Any) -> None:
    async def opener(paths: Any) -> ButterEye:
        return await _HangingClose.for_scenario("all_ready").open(paths)

    bridge = CoreBridge(opener=opener, debug=True)
    with qtbot.waitSignal(bridge.ready, timeout=5000):
        bridge.start()
    started = time.monotonic()
    assert bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=False, timeout_s=0.3) is None
    assert bridge.shutdown_timed_out
    assert time.monotonic() - started < close_wait_s(0.3) + CLOSE_MARGIN_S + 1.5


class _DetachThenSlowOps(FakeCore):
    """Detaches every session at once, then spends ``slow_s`` on op cleanup
    (a child that takes seconds to exit on SIGTERM; finding: close budget)."""

    slow_s = 4.0

    async def close(
        self, policy: Any = DetachPolicy.KEEP_FILTER, *, cancel_jobs: bool, timeout_s: float = 5.0
    ) -> ShutdownReport:
        report = await super().close(policy, cancel_jobs=cancel_jobs, timeout_s=timeout_s)
        await asyncio.sleep(self.slow_s)
        return report


def _slow_close_bridge(qtbot: Any, slow_s: float) -> CoreBridge:
    base = _DetachThenSlowOps.for_scenario("live_active")
    cls: type[FakeCore] = type("_Slow", (base,), {"slow_s": slow_s})

    async def opener(paths: Any) -> ButterEye:
        return await cls.open(paths)

    bridge = CoreBridge(opener=opener, debug=True)
    with qtbot.waitSignal(bridge.ready, timeout=5000):
        bridge.start()
    return bridge


def _live(bridge: CoreBridge, qtbot: Any) -> tuple[Any, ...]:
    out: list[Any] = []
    owner = Owner()
    bridge.call(lambda c: c.sessions(), owner=owner, ok=lambda snaps: out.extend(snaps))
    qtbot.waitUntil(lambda: bool(out), timeout=3000)
    return tuple(s for s in out if not s.ended)


def test_slow_op_cleanup_does_not_turn_detached_into_failed(qtbot: Any) -> None:
    """One live session + ops that take 4 s to exit: the GUI waits for the whole
    close (the old 1x-timeout wait gave up first and listed every player)."""
    from buttereye.gui.quit_dialog import QuitNeeds, failed_titles

    bridge = _slow_close_bridge(qtbot, 4.0)
    snaps = _live(bridge, qtbot)
    live = tuple(s.sid for s in snaps)
    assert live
    report = bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=1.0, live=live)
    assert report is not None and not bridge.shutdown_timed_out
    assert set(report.detached) == set(live) and not report.failed
    needs = QuitNeeds(live=snaps, running_jobs=(), queued_jobs=())
    assert failed_titles(needs, report) == ()  # no "Could not detach from: ..."


def test_timeout_after_detach_reports_partial_result(qtbot: Any) -> None:
    """Even when the GUI wait runs out, players seen detaching are not 'failed'."""
    bridge = _slow_close_bridge(qtbot, 60.0)
    live = tuple(s.sid for s in _live(bridge, qtbot))
    started = time.monotonic()
    report = bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=0.1, live=live)
    assert time.monotonic() - started < close_wait_s(0.1) + CLOSE_MARGIN_S + 1.5
    assert bridge.shutdown_timed_out
    assert report is not None
    assert set(report.detached) == set(live) and not report.failed


def test_close_wait_covers_the_core_stages() -> None:
    # live (t+1) + render (t+1) + ops (t) + tasks (1): core/api.py ButterEye.close
    assert close_wait_s(5.0) == pytest.approx(18.0)
    assert close_wait_s(0.0) == pytest.approx(3.0)


@pytest.mark.allow_slow_callbacks
def test_watchdog_flags_blocked_loop_then_clears(make_window: Any, qtbot: Any) -> None:
    """slow_core: the first ping blocks the loop for 6 s -> banner, then it clears."""
    win = make_window("slow_core")
    bridge = win.bridge
    qtbot.waitUntil(lambda: bridge.is_unresponsive, timeout=8000)
    banner = win.banner("unresponsive")
    assert banner is not None
    assert "not responding" in banner.title()
    assert str(win.log_path) in banner.title()
    qtbot.waitUntil(lambda: not bridge.is_unresponsive, timeout=5000)
    qtbot.waitUntil(lambda: win.banner("unresponsive") is None, timeout=1000)


def test_debug_mode_refuses_qobject_payload(make_bridge: Any, qtbot: Any) -> None:
    bridge = make_bridge("all_ready")
    owner = Owner()
    errors: list[ButterEyeError] = []
    leaked = QObject()

    async def leak(core: ButterEye) -> QObject:
        return leaked

    bridge.call(leak, owner=owner, ok=lambda v: None, err=errors.append)
    qtbot.waitUntil(lambda: bool(errors), timeout=3000)
    assert errors[0].code is ErrorCode.INTERNAL


@pytest.mark.parametrize(
    "first", ["buttereye.gui.bridge", "shiboken6"], ids=["gui-first", "shiboken-first"]
)
def test_the_core_loop_runs_threads_with_pyside_imported(first: str) -> None:
    """``asyncio.to_thread`` on the GUI's core loop, in a fresh interpreter that has
    imported shiboken6 as ``buttereye-gui`` always has, in either order. On Python
    3.15 the default executor's class is a lazy binding shiboken6 stops resolving,
    so the Storage page failed with BE-9999 on Fedora 45 until
    ``buttereye.gui._resolve_lazy_executors``. A subprocess, because
    tests/conftest.py repairs the binding for the suite's own loops and would hide
    a regression here."""
    code = (
        f"import asyncio, importlib; importlib.import_module({first!r})\n"
        "import shiboken6\n"
        "from buttereye.gui.bridge import CoreThread\n"
        "t = CoreThread()\n"
        "t.start()\n"
        "try:\n"
        "    print(t.submit(asyncio.to_thread(sum, [1, 2])).result(30))\n"
        "finally:\n"
        "    t.stop()\n"
        "    t.join(30)\n"
    )
    root = Path(__file__).resolve().parents[2]
    res = subprocess.run(
        [sys.executable, "-c", code], cwd=root, capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "3"
