# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.4: Operation lifecycle and the cancellation contract (process groups)."""

from __future__ import annotations

import ast
import asyncio
import os
import time
from pathlib import Path

import pytest

from buttereye.core.errors import ButterEyeError, ErrorCode, OperationCancelled
from buttereye.core.events import Event, OpFinished, OpStarted, Progress
from buttereye.core.ops import OpContext, Operation, process_group
from buttereye.core.types import Msg, OpId, OpKind, OpState


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_dead(pgid: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        await asyncio.sleep(0.02)
    return not _group_alive(pgid)


async def test_cancel_kills_process_group_and_deletes_partials(tmp_path: Path) -> None:
    events: list[Event] = []
    partial = tmp_path / "out.mkv.part"
    started = asyncio.Event()
    pgids: list[int] = []

    async def body(op: OpContext) -> None:
        partial.write_bytes(b"partial")
        op.add_partial(partial)
        proc = await op.spawn(["sh", "-c", "sleep 100 | sleep 100"])
        pgids.append(proc.pid)
        started.set()
        await proc.wait()

    op: Operation[None] = Operation(OpKind.RENDER, body, emit=events.append)
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.1)  # let the pipeline start both sleeps
    assert _group_alive(pgids[0])
    t0 = time.monotonic()
    op.cancel()
    op.cancel()  # idempotent
    assert op.state is OpState.CANCELLING
    with pytest.raises(OperationCancelled) as exc:
        await op.result()
    assert exc.value.exit_code == 130
    assert time.monotonic() - t0 <= 5.0
    assert op.state is OpState.CANCELLED  # type: ignore[comparison-overlap]
    assert await _wait_dead(pgids[0])
    assert not partial.exists()
    assert events[0] == OpStarted(op.id, OpKind.RENDER)
    assert isinstance(events[-1], OpFinished) and events[-1].state is OpState.CANCELLED


async def test_sigkill_after_grace_when_sigterm_ignored() -> None:
    started = asyncio.Event()
    pgids: list[int] = []

    async def body(op: OpContext) -> None:
        # The ignored disposition is inherited by the child sleep, so the whole
        # group survives SIGTERM and must be SIGKILLed.
        proc = await op.spawn(["sh", "-c", "trap '' TERM; sleep 100 & wait"])
        pgids.append(proc.pid)
        started.set()
        await proc.wait()

    op: Operation[None] = Operation(OpKind.BENCH, body, grace_s=0.5)
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.sleep(0.1)
    t0 = time.monotonic()
    op.cancel()
    assert await op.wait() is OpState.CANCELLED
    elapsed = time.monotonic() - t0
    assert 0.4 <= elapsed <= 5.0
    assert await _wait_dead(pgids[0])


async def test_success_keeps_partials_and_reports_progress(tmp_path: Path) -> None:
    out = tmp_path / "done.txt"
    seen: list[Progress] = []

    async def body(op: OpContext) -> str:
        out.write_text("x")
        op.add_partial(out)
        op.progress(Progress(OpId(""), Msg("Working"), 1, 2, "steps", None, None))
        op.report(Msg("Working"), done=2, total=2)
        op.keep(out)
        return "ok"

    op: Operation[str] = Operation(OpKind.CLEAN, body)
    op.add_progress_sink(seen.append)

    def broken(p: Progress) -> None:
        raise RuntimeError("a broken sink never kills the op")

    op.add_progress_sink(broken)
    assert await op.result() == "ok"
    assert op.state is OpState.SUCCEEDED
    assert out.exists()
    assert [p.done for p in seen] == [1, 2]
    assert all(p.op_id == op.id for p in seen)
    late: list[Progress] = []
    op.add_progress_sink(late.append)
    assert late == [seen[-1]]


async def test_failure_states_and_internal_wrapping(tmp_path: Path) -> None:
    partial = tmp_path / "p"

    async def be(op: OpContext) -> None:
        partial.write_text("x")
        op.add_partial(partial)
        raise ButterEyeError(ErrorCode.ENCODER_FAILED, Msg("boom"))

    op1: Operation[None] = Operation(OpKind.RENDER, be)
    with pytest.raises(ButterEyeError) as e1:
        await op1.result()
    assert e1.value.code is ErrorCode.ENCODER_FAILED
    assert op1.state is OpState.FAILED and not partial.exists()

    async def bug(op: OpContext) -> None:
        raise ValueError("bug")

    events: list[Event] = []
    op2: Operation[None] = Operation(OpKind.DOCTOR, bug, emit=events.append)
    with pytest.raises(ButterEyeError) as e2:
        await op2.result()
    assert e2.value.code is ErrorCode.INTERNAL
    assert e2.value.detail and "ValueError" in e2.value.detail
    fin = events[-1]
    assert isinstance(fin, OpFinished) and fin.error is not None
    assert fin.error.code is ErrorCode.INTERNAL


async def test_cancel_before_start() -> None:
    ran = False

    async def body(op: OpContext) -> None:
        nonlocal ran
        ran = True

    events: list[Event] = []
    op: Operation[None] = Operation(OpKind.SETUP, body, emit=events.append)
    op.cancel()
    with pytest.raises(OperationCancelled):
        await op.result()
    assert op.state is OpState.CANCELLED and not ran
    assert [type(e) for e in events] == [OpStarted, OpFinished]


async def test_awaiter_cancellation_does_not_cancel_op() -> None:
    gate = asyncio.Event()

    async def body(op: OpContext) -> int:
        await gate.wait()
        return 7

    op: Operation[int] = Operation(OpKind.BENCH, body)
    waiter = asyncio.ensure_future(op.result())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate.set()
    assert await op.result() == 7


async def test_process_group_context_terminates_on_error() -> None:
    pgid = 0
    with pytest.raises(RuntimeError):
        async with process_group(["sh", "-c", "sleep 100 | sleep 100"]) as proc:
            pgid = proc.pid
            await asyncio.sleep(0.05)
            raise RuntimeError("stop")
    assert await _wait_dead(pgid)


def test_no_preexec_fn_in_core(repo_root: Path) -> None:
    for path in (repo_root / "buttereye" / "core").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword):
                assert node.arg != "preexec_fn", path
