# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.4 cancellation contract under shutdown: an op child that ignores SIGTERM
(an mpv / vspipe wedged in a GPU filter) must not outlive the core, even when the
cleanup is cancelled a second time (close() budget, then the loop teardown)."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from buttereye.core import ops
from buttereye.core.api import ButterEye
from buttereye.core.ops import OpContext, Operation, process_group
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import OpKind, OpState

TERM_IGNORING = ["sh", "-c", "trap '' TERM; sleep 120"]


def _alive(pgid: int) -> bool:
    """A live (non-zombie) process is in group ``pgid``. Zombies are ignored: a
    leader killed during loop teardown may stay unreaped until the process exits,
    which leaks nothing."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rindex(")") + 2 :].split()
        if int(fields[2]) == pgid and fields[0] != "Z":
            return True
    return False


def _wait_dead_sync(pgid: int, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pgid):
            return True
        time.sleep(0.02)
    return not _alive(pgid)


def _reap(pgid: int) -> None:
    if _alive(pgid):  # don't leak a sleeper if the assertion fails
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, 9)


async def _start_op(grace_s: float = ops.KILL_GRACE_S) -> tuple[Operation[None], int]:
    started = asyncio.Event()
    pgids: list[int] = []

    async def body(op: OpContext) -> None:
        proc = await op.spawn(TERM_IGNORING)
        pgids.append(proc.pid)
        started.set()
        await asyncio.sleep(3600)

    op: Operation[None] = Operation(OpKind.BENCH, body, grace_s=grace_s)
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.1)
    return op, pgids[0]


async def test_second_cancel_during_cleanup_still_kills_group() -> None:
    op, pgid = await _start_op(grace_s=1.0)
    try:
        op.cancel()
        await asyncio.sleep(0.2)  # cleanup is inside the SIGTERM grace now
        op._task.cancel()  # a second cancel of the op task (close(), then teardown)
        # The cleanup is not abandoned: it sits out the grace, then SIGKILLs.
        assert await op.wait() is OpState.CANCELLED
        assert _wait_dead_sync(pgid)
    finally:
        _reap(pgid)


def test_loop_teardown_during_cleanup_kills_group() -> None:
    """CoreThread.run's teardown: stop, cancel every task, gather, close."""
    loop = asyncio.new_event_loop()
    holder: list[tuple[Operation[None], int]] = []
    try:

        async def start() -> None:
            op, pgid = await _start_op()
            holder.append((op, pgid))
            op.cancel()
            await asyncio.sleep(0.2)

        loop.run_until_complete(start())
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        ops.kill_owned_groups(loop)
    finally:
        loop.close()
    op, pgid = holder[0]
    try:
        assert op.state is OpState.CANCELLED
        assert _wait_dead_sync(pgid)
        assert pgid not in ops.owned_groups()
    finally:
        _reap(pgid)


async def test_cancel_during_success_cleanup_finishes_cleanup() -> None:
    pgids: list[int] = []

    async def body(op: OpContext) -> None:
        proc = await op.spawn(TERM_IGNORING)
        pgids.append(proc.pid)
        await asyncio.sleep(0.1)  # body returns with the child still running

    op: Operation[None] = Operation(OpKind.BENCH, body, grace_s=0.5)
    try:
        await asyncio.sleep(0.3)  # success-path cleanup is in its grace
        assert not op.done
        op.cancel()
        assert await op.wait() is OpState.CANCELLED
        assert _wait_dead_sync(pgids[0])
    finally:
        _reap(pgids[0])


async def test_process_group_cancelled_twice_kills_group() -> None:
    pgids: list[int] = []
    entered = asyncio.Event()

    async def user() -> None:
        async with process_group(TERM_IGNORING) as proc:
            pgids.append(proc.pid)
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.ensure_future(user())
    await asyncio.wait_for(entered.wait(), 5)
    assert pgids[0] in ops.owned_groups(asyncio.get_running_loop())
    try:
        task.cancel()
        await asyncio.sleep(0.2)
        for t in asyncio.all_tasks():  # teardown cancels the shielded inner task too
            if t is not asyncio.current_task():
                t.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert _wait_dead_sync(pgids[0])
    finally:
        _reap(pgids[0])


async def test_close_kills_term_ignoring_op_child_within_budget(tmp_path: Path) -> None:
    core = await ButterEye.open(sc.fake_paths(tmp_path))
    started = asyncio.Event()
    pgids: list[int] = []

    async def body(op: OpContext) -> None:
        proc = await op.spawn(TERM_IGNORING)
        pgids.append(proc.pid)
        started.set()
        await asyncio.sleep(3600)

    op = core._op(OpKind.BENCH, body)
    await asyncio.wait_for(started.wait(), 5)
    try:
        t0 = time.monotonic()
        await core.close(cancel_jobs=True, timeout_s=0.5)
        assert time.monotonic() - t0 < 3.0
        assert op.state is OpState.CANCELLED
        assert _wait_dead_sync(pgids[0], timeout=1.0)
    finally:
        _reap(pgids[0])


def test_kill_owned_groups_is_scoped_to_loop() -> None:
    a, b = asyncio.new_event_loop(), asyncio.new_event_loop()
    pgids: dict[str, int] = {}
    try:

        async def spawn(name: str) -> None:
            proc = await ops.spawn_group(TERM_IGNORING)
            ops._own_group(proc.pid)
            pgids[name] = proc.pid

        a.run_until_complete(spawn("a"))
        b.run_until_complete(spawn("b"))
        assert ops.kill_owned_groups(a) == (pgids["a"],)
        assert _wait_dead_sync(pgids["a"])
        assert _alive(pgids["b"])
        assert ops.kill_owned_groups(b) == (pgids["b"],)
        assert _wait_dead_sync(pgids["b"])
        ops.kill_owned_groups(a)
        ops.kill_owned_groups(b)
        assert not ({pgids["a"], pgids["b"]} & ops.owned_groups())
    finally:
        for pgid in pgids.values():
            _reap(pgid)
        a.close()
        b.close()
