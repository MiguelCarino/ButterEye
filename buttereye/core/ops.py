# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Long-running operations (docs/design/GUI.md §2.4, FROZEN).

Cancellation contract: ``cancel()`` -> ``CANCELLING`` -> SIGTERM to each child
**process group** (``start_new_session=True``) -> SIGKILL after the grace period
(5 s) -> delete partial files -> ``CANCELLED``. R72 vspipe ignores SIGINT, so
SIGTERM always. ``preexec_fn`` is banned (the process is multithreaded).

All methods run on the core loop thread; the GUI bridge marshals calls.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import signal
import subprocess
import threading
import traceback
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import IO, Any, Generic, Literal, TypeVar

from buttereye.core.errors import ButterEyeError, ErrorCode, OperationCancelled
from buttereye.core.events import Event, OpFinished, OpStarted, Progress
from buttereye.core.types import Msg, OpId, OpKind, OpState

T = TypeVar("T")

ProgressSink = Callable[[Progress], None]

KILL_GRACE_S = 5.0

_log = logging.getLogger(__name__)

_StdSpec = int | IO[Any] | None


def new_op_id() -> OpId:
    return OpId(uuid.uuid4().hex[:12])


# ---------------------------------------------------------------------------
# Process-group helpers (usable by any provider)
# ---------------------------------------------------------------------------


# Process groups that ButterEye owns and must not outlive the core: every group
# started by ``OpContext.spawn`` or ``process_group``. ``kill_owned_groups`` is the
# synchronous last resort for loop teardown (a loop that stops before a cleanup
# task finished must not leak a group). Groups started with bare ``spawn_group``
# (e.g. mpv players that may outlive the GUI) are not registered.
_OWNED_GROUPS: dict[int, asyncio.AbstractEventLoop] = {}
_OWNED_LOCK = threading.Lock()


def _own_group(pgid: int) -> None:
    loop = asyncio.get_running_loop()
    with _OWNED_LOCK:
        _OWNED_GROUPS[pgid] = loop


def _disown_group(pgid: int) -> None:
    with _OWNED_LOCK:
        _OWNED_GROUPS.pop(pgid, None)


def owned_groups(loop: asyncio.AbstractEventLoop | None = None) -> frozenset[int]:
    """Process-group ids of op / ``process_group`` children not yet reaped
    (only those started on ``loop`` when given)."""
    with _OWNED_LOCK:
        return frozenset(g for g, lp in _OWNED_GROUPS.items() if loop is None or lp is loop)


def kill_owned_groups(loop: asyncio.AbstractEventLoop | None = None) -> tuple[int, ...]:
    """SIGKILL every registered op / ``process_group`` process group that is still
    alive (only those started on ``loop`` when given). Synchronous and safe from
    any thread; returns the pgids signalled.

    The last-resort hook for core shutdown: the loop owner calls it after
    cancelling and gathering the loop's tasks and before ``loop.close()``, so a
    loop that stops before a cleanup task finished cannot leak a group.
    """
    pgids = owned_groups(loop)
    killed: list[int] = []
    for pgid in pgids:
        # SIGKILL is final, so the entry goes either way (an unreaped zombie
        # leader would otherwise keep it "alive" for ever).
        _disown_group(pgid)
        if not _group_alive(pgid):
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
    return tuple(killed)


def _sigkill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL)


async def run_to_completion(aw: Coroutine[Any, Any, T]) -> T:
    """Await ``aw`` in its own task and keep waiting for it even if the caller is
    cancelled (any number of times); re-raise ``CancelledError`` once it is done.

    Unlike ``asyncio.shield`` alone, a repeated cancel cannot abandon the inner
    work half-way. The inner task itself can still be cancelled directly (e.g. by
    a loop teardown that cancels every task); cleanup coroutines used here must
    therefore make cancellation safe on their own (``terminate_group`` escalates
    to SIGKILL when cancelled).
    """
    task = asyncio.ensure_future(aw)
    cancelled = False
    while True:
        try:
            value = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.done():
                if task.cancelled():
                    raise
                value = task.result()
                break
            cancelled = True
    if cancelled:
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError
    return value


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def terminate_group(
    proc: asyncio.subprocess.Process, *, grace_s: float = KILL_GRACE_S
) -> None:
    """SIGTERM the process group led by ``proc``, SIGKILL after ``grace_s``.

    ``proc`` must have been started with ``start_new_session=True`` so that its
    pid is the group id. Waits for ``proc`` itself to be reaped. Never raises for
    an already-gone group.
    """
    pgid = proc.pid
    try:
        await _terminate_group(proc, pgid, grace_s)
    except BaseException:
        # Cancelled (or failed) before the group was confirmed gone: there is no
        # time left for the grace period, so kill it now rather than leak it.
        _sigkill_group(pgid)
        raise
    finally:
        if proc.returncode is not None and not _group_alive(pgid):
            _disown_group(pgid)


async def _terminate_group(proc: asyncio.subprocess.Process, pgid: int, grace_s: float) -> None:
    if proc.returncode is None or _group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + grace_s
    while True:
        if proc.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=0.05)
        if proc.returncode is not None and not _group_alive(pgid):
            return
        if loop.time() >= deadline:
            break
        if proc.returncode is not None:
            await asyncio.sleep(0.05)
    _sigkill_group(pgid)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
    # Grandchildren killed by SIGKILL are reaped by init; give them a moment.
    for _ in range(40):
        if not _group_alive(pgid):
            return
        await asyncio.sleep(0.025)


async def spawn_group(
    argv: Sequence[str],
    *,
    stdin: _StdSpec = subprocess.DEVNULL,
    stdout: _StdSpec = subprocess.DEVNULL,
    stderr: _StdSpec = subprocess.DEVNULL,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> asyncio.subprocess.Process:
    """Start ``argv`` as the leader of a new session/process group (§2.4)."""
    if not argv:
        raise ValueError("empty argv")
    return await asyncio.create_subprocess_exec(
        *argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        env=dict(env) if env is not None else None,
        cwd=cwd,
        start_new_session=True,
    )


@contextlib.asynccontextmanager
async def process_group(
    argv: Sequence[str],
    *,
    stdin: _StdSpec = subprocess.DEVNULL,
    stdout: _StdSpec = subprocess.DEVNULL,
    stderr: _StdSpec = subprocess.DEVNULL,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    grace_s: float = KILL_GRACE_S,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """``async with process_group([...]) as proc:`` - the group is terminated on
    exit (normal, error or cancellation) if anything in it is still alive."""
    proc = await spawn_group(argv, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd)
    _own_group(proc.pid)
    try:
        yield proc
    finally:
        if proc.returncode is None or _group_alive(proc.pid):
            await run_to_completion(terminate_group(proc, grace_s=grace_s))
        else:
            _disown_group(proc.pid)


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------


class OpContext:
    """Handed to an operation's body: progress, partial files, child processes."""

    __slots__ = ("_op", "_partials", "_procs")

    def __init__(self, op: Operation[Any]) -> None:
        self._op = op
        self._partials: list[Path] = []
        self._procs: list[asyncio.subprocess.Process] = []

    @property
    def op_id(self) -> OpId:
        return self._op.id

    @property
    def kind(self) -> OpKind:
        return self._op.kind

    def progress(self, p: Progress) -> None:
        """A ``ProgressSink``. A foreign or empty ``op_id`` is replaced by this op's id."""
        self._op._publish_progress(p)

    def report(
        self,
        phase: Msg,
        *,
        done: int | None = None,
        total: int | None = None,
        unit: Literal["frames", "bytes", "steps", "checks"] = "steps",
        rate: float | None = None,
        eta_s: float | None = None,
        detail: Msg | None = None,
    ) -> None:
        self.progress(Progress(self.op_id, phase, done, total, unit, rate, eta_s, detail))

    def add_partial(self, path: Path) -> None:
        """Register a file that is deleted if the op fails or is cancelled."""
        self._partials.append(path)

    def keep(self, path: Path) -> None:
        """Unregister a partial file (it is complete and must survive)."""
        with contextlib.suppress(ValueError):
            self._partials.remove(path)

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        stdin: _StdSpec = subprocess.DEVNULL,
        stdout: _StdSpec = subprocess.DEVNULL,
        stderr: _StdSpec = subprocess.DEVNULL,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> asyncio.subprocess.Process:
        """Spawn a process group owned by this op; it is terminated when the op
        ends in any way (do not use for processes that must outlive the op)."""
        proc = await spawn_group(argv, stdin=stdin, stdout=stdout, stderr=stderr, env=env, cwd=cwd)
        _own_group(proc.pid)
        self._procs.append(proc)
        return proc

    async def _cleanup(self, *, delete_partials: bool, grace_s: float) -> None:
        # Procs stay listed (and registered in _OWNED_GROUPS) until terminate_group
        # has finished with them, so a teardown that cuts this short still finds them.
        procs = tuple(self._procs)
        if procs:
            await asyncio.gather(
                *(terminate_group(p, grace_s=grace_s) for p in procs), return_exceptions=True
            )
        self._procs = [p for p in self._procs if p not in procs]
        if delete_partials:
            partials, self._partials = self._partials, []
            for path in partials:
                try:
                    if path.is_symlink() or path.is_file():
                        path.unlink()
                except OSError as exc:  # pragma: no cover - best effort
                    _log.warning("could not delete partial file %s: %s", path, exc)


def internal_error(exc: BaseException) -> ButterEyeError:
    """Wrap a non-ButterEye exception as ``ButterEyeError(INTERNAL)`` with the traceback."""
    return ButterEyeError(
        ErrorCode.INTERNAL,
        Msg("Internal error: {error}", {"error": type(exc).__name__}),
        Msg("This is a bug in ButterEye. Please report it with the log."),
        detail="".join(traceback.format_exception(exc)),
    )


class Operation(Generic[T]):
    """A running core job. Create it on the core loop; it starts immediately."""

    id: OpId
    kind: OpKind

    def __init__(
        self,
        kind: OpKind,
        body: Callable[[OpContext], Awaitable[T]],
        *,
        emit: Callable[[Event], None] | None = None,
        op_id: OpId | None = None,
        grace_s: float = KILL_GRACE_S,
        on_success: Callable[[T], None] | None = None,
    ) -> None:
        self.id = op_id or new_op_id()
        self.kind = kind
        self._state = OpState.QUEUED
        self._emit = emit
        self._grace_s = grace_s
        self._on_success = on_success
        self._sinks: list[ProgressSink] = []
        self._last_progress: Progress | None = None
        self._ctx = OpContext(self)
        self._body = body
        self._error: ButterEyeError | None = None
        self._started = False
        self._task: asyncio.Task[T] = asyncio.get_running_loop().create_task(
            self._run(), name=f"buttereye-op-{kind.value}-{self.id}"
        )
        self._task.add_done_callback(self._on_done)

    # -- public surface (§2.4) --
    @property
    def state(self) -> OpState:
        return self._state

    def add_progress_sink(self, sink: ProgressSink) -> None:
        """Loop thread only. A late sink immediately receives the latest progress."""
        self._sinks.append(sink)
        if self._last_progress is not None:
            self._call_sink(sink, self._last_progress)

    async def result(self) -> T:
        """Raises ``ButterEyeError`` / ``OperationCancelled``. Awaiting is shielded:
        cancelling the awaiter does not cancel the operation."""
        try:
            return await asyncio.shield(self._task)
        except asyncio.CancelledError:
            if self._task.cancelled():
                raise self._cancelled_error() from None
            raise

    def cancel(self) -> None:
        """Idempotent; loop thread only."""
        if self._task.done() or self._state is OpState.CANCELLING:
            return
        self._state = OpState.CANCELLING
        self._task.cancel()

    # -- extras --
    @property
    def done(self) -> bool:
        return self._task.done()

    @property
    def error(self) -> ButterEyeError | None:
        return self._error

    @property
    def last_progress(self) -> Progress | None:
        return self._last_progress

    async def wait(self) -> OpState:
        """Wait for the op to finish without raising; returns the final state."""
        with contextlib.suppress(BaseException):
            await asyncio.shield(self._task)
        return self._state

    def kill_children(self) -> tuple[int, ...]:
        """SIGKILL this op's child process groups now, skipping the grace period
        (shutdown budget exhausted). Synchronous; returns the pgids signalled.
        The op's own cleanup still reaps them and finishes normally."""
        killed: list[int] = []
        for proc in tuple(self._ctx._procs):
            if proc.returncode is None or _group_alive(proc.pid):
                _sigkill_group(proc.pid)
                killed.append(proc.pid)
        return tuple(killed)

    # -- internals --
    def _cancelled_error(self) -> OperationCancelled:
        return OperationCancelled(ErrorCode.INTERNAL, Msg("Cancelled."))

    def _publish_progress(self, p: Progress) -> None:
        if p.op_id != self.id:
            p = dataclasses.replace(p, op_id=self.id)
        self._last_progress = p
        for sink in tuple(self._sinks):
            self._call_sink(sink, p)

    @staticmethod
    def _call_sink(sink: ProgressSink, p: Progress) -> None:
        try:
            sink(p)
        except Exception:  # a broken sink must not kill the op
            _log.exception("progress sink failed")

    def _send(self, ev: Event) -> None:
        if self._emit is not None:
            try:
                self._emit(ev)
            except Exception:
                _log.exception("event emit failed")

    async def _cleanup(self, *, delete_partials: bool) -> None:
        """Run the context cleanup to completion even if this task is cancelled
        again meanwhile (close(), then the loop teardown); see ``run_to_completion``."""
        await run_to_completion(
            self._ctx._cleanup(delete_partials=delete_partials, grace_s=self._grace_s)
        )

    async def _run(self) -> T:
        if self._state is OpState.QUEUED:
            self._state = OpState.RUNNING
        self._started = True
        self._send(OpStarted(self.id, self.kind))
        try:
            value = await self._body(self._ctx)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            self._state = OpState.CANCELLING
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup(delete_partials=True)
            raise self._cancelled_error() from None
        except OperationCancelled:
            self._state = OpState.CANCELLING
            await self._cleanup_then_cancelled(delete_partials=True)
            raise
        except ButterEyeError:
            await self._cleanup_then_cancelled(delete_partials=True)
            raise
        except Exception as exc:
            _log.exception("operation %s (%s) failed", self.id, self.kind.value)
            await self._cleanup_then_cancelled(delete_partials=True)
            raise internal_error(exc) from exc
        try:
            await self._cleanup(delete_partials=False)
        except asyncio.CancelledError:
            # cancel() landed while the success-path cleanup ran: the cleanup
            # still finished (children gone); the op ends CANCELLED.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            self._state = OpState.CANCELLING
            await self._cleanup_then_cancelled(delete_partials=True)
            raise self._cancelled_error() from None
        return value

    async def _cleanup_then_cancelled(self, *, delete_partials: bool) -> None:
        """Cleanup on an error path; a cancel during it turns the result into
        ``OperationCancelled`` only after the cleanup has finished."""
        try:
            await self._cleanup(delete_partials=delete_partials)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            self._state = OpState.CANCELLING
            raise self._cancelled_error() from None

    def _on_done(self, task: asyncio.Task[T]) -> None:
        error: ButterEyeError | None
        if task.cancelled():
            # cancelled before the body started: nothing to clean up
            state, error = OpState.CANCELLED, self._cancelled_error()
            if not self._started:
                self._started = True
                self._send(OpStarted(self.id, self.kind))
        else:
            exc = task.exception()
            if exc is None:
                state, error = OpState.SUCCEEDED, None
                if self._on_success is not None:
                    try:
                        self._on_success(task.result())
                    except Exception:
                        _log.exception("on_success hook failed")
            elif isinstance(exc, OperationCancelled):
                state, error = OpState.CANCELLED, exc
            elif isinstance(exc, ButterEyeError):
                state, error = OpState.FAILED, exc
            else:  # pragma: no cover - _run wraps everything
                state, error = OpState.FAILED, internal_error(exc)
        self._state = state
        self._error = error
        self._send(OpFinished(self.id, state, error))


__all__ = [
    "ProgressSink",
    "Operation",
    "OpContext",
    "KILL_GRACE_S",
    "new_op_id",
    "internal_error",
    "spawn_group",
    "terminate_group",
    "process_group",
    "run_to_completion",
    "owned_groups",
    "kill_owned_groups",
]
