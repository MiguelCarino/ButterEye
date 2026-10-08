# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Qt <-> core bridge (docs/design/GUI.md §3).

M0(h) is no-go on PySide6 6.11.2 (QtAsyncio lacks subprocesses and Unix
sockets), so a dedicated non-daemon thread runs a stock asyncio loop and the
facade is opened inside it. This module is the only place that knows the loop
lives in a thread; a future ``QtAsyncioBridge`` would replace it with the same
public surface.

Rules implemented here (numbers from §3):

1. ``call``/``run_op`` submit with ``asyncio.run_coroutine_threadsafe``; replies
   come back through ``_Inbox.reply`` (``Signal(object, object)``) on a QObject
   that lives in the GUI thread, so ``ok``/``err`` always run on the GUI thread.
2. Each request is bound to its ``owner``; a reply for a deleted owner is dropped.
3. ``err`` defaults to ``owner.show_error``; ``OperationCancelled`` goes there too.
4. Non-``ButterEyeError`` exceptions become ``ButterEyeError(INTERNAL)`` with the
   traceback in ``detail``; the loop's exception handler logs.
5. One pump coroutine copies the event stream into a locked deque; progress
   sinks write the latest ``Progress`` per request; a 100 ms GUI timer drains.
6. A watchdog pings the loop every 2 s; no reply within 5 s emits
   ``unresponsive(True)``, the next reply ``unresponsive(False)``.
7. Only §2 values cross; in debug mode a QObject in a reply is refused.
8. ``shutdown`` posts ``close()``, waits in a local ``QEventLoop`` bounded by a
   timer, then stops the loop and joins the thread (1 s). Never ``.result()``.
   The timer covers the core's whole close budget (``close_wait_s``) plus a
   margin, so a close that is merely slow (ops whose children take seconds to
   exit) still reports. If the timer does win, the detach outcome is rebuilt
   from the ``SessionEnded`` events the core published while closing, so
   players that did detach are not reported as failed.
"""

from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import itertools
import logging
import threading
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar, cast

import shiboken6
from PySide6.QtCore import QEventLoop, QObject, QTimer, Signal, Slot

from buttereye.core.api import (
    ButterEye,
    ButterEyeError,
    Capabilities,
    CapabilitiesChanged,
    DetachPolicy,
    ErrorCode,
    Event,
    Operation,
    Paths,
    Progress,
    SessionEnded,
    SessionId,
    ShutdownReport,
    internal_error,
)

T = TypeVar("T")

_log = logging.getLogger(__name__)

Opener = Callable[[Paths | None], Awaitable[ButterEye]]

DRAIN_INTERVAL_MS = 100
PING_INTERVAL_S = 2.0
PING_TIMEOUT_S = 5.0
JOIN_TIMEOUT_S = 1.0
#: GUI wait beyond the core's own close budget (reply hop, event pump, slack).
CLOSE_MARGIN_S = 2.0


def close_wait_s(timeout_s: float) -> float:
    """Upper bound on ``ButterEye.close(timeout_s=...)``'s run time.

    ``close`` currently runs its stages one after another, each with its own
    budget: live detach ``timeout_s + 1``, render shutdown ``timeout_s + 1``,
    cancelled operations ``timeout_s``, background tasks 1 s
    (``core/api.py`` ``ButterEye.close``). If the core moves to one overall
    deadline this bound stays safe (merely generous); it never cuts a close
    short that the core would still finish.
    """
    t = max(0.0, timeout_s)
    return (t + 1.0) + (t + 1.0) + t + 1.0


# ---------------------------------------------------------------------------
# Requests and tickets
# ---------------------------------------------------------------------------


class _Request:
    __slots__ = ("rid", "owner", "ok", "err", "progress", "is_op", "cancelled", "op")

    def __init__(
        self,
        rid: int,
        owner: QObject,
        ok: Callable[[Any], None],
        err: Callable[[ButterEyeError], None] | None,
        progress: Callable[[Progress], None] | None,
        is_op: bool,
    ) -> None:
        self.rid = rid
        self.owner = owner
        self.ok = ok
        self.err = err
        self.progress = progress
        self.is_op = is_op
        self.cancelled = False
        self.op: Operation[Any] | None = None  # set on the loop thread


class _Ok:
    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


class _Err:
    __slots__ = ("error",)

    def __init__(self, error: ButterEyeError) -> None:
        self.error = error


class Ticket:
    """Handle for one request; GUI thread only."""

    __slots__ = ("_bridge", "_req")

    def __init__(self, bridge: CoreBridge, req: _Request) -> None:
        self._bridge = bridge
        self._req = req

    def cancel(self) -> None:
        """Drop the reply; for operations also cancel the ``Operation``. Idempotent."""
        self._bridge._cancel(self._req)

    @property
    def cancelled(self) -> bool:
        return self._req.cancelled

    @property
    def pending(self) -> bool:
        return self._bridge._is_pending(self._req)


# ---------------------------------------------------------------------------
# Core thread
# ---------------------------------------------------------------------------


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    exc = context.get("exception")
    _log.error("core loop: %s", context.get("message", "unhandled error"), exc_info=exc or None)


class CoreThread(threading.Thread):
    """Non-daemon thread running a stock asyncio loop (§3 Decision)."""

    def __init__(self, *, debug: bool = False) -> None:
        super().__init__(name="buttereye-core", daemon=False)
        self.loop = asyncio.new_event_loop()
        self.loop.set_exception_handler(_loop_exception_handler)
        if debug:
            self.loop.set_debug(True)
            self.loop.slow_callback_duration = 0.1

    def run(self) -> None:
        loop = self.loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # pragma: no cover - best effort teardown
                _log.exception("core loop teardown failed")
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def submit(self, coro: Awaitable[Any]) -> concurrent.futures.Future[Any]:
        return asyncio.run_coroutine_threadsafe(cast(Any, coro), self.loop)

    def call_soon(self, fn: Callable[[], None]) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn)

    def stop(self) -> None:
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.loop.stop)


# ---------------------------------------------------------------------------
# GUI-thread receiver
# ---------------------------------------------------------------------------


class _Inbox(QObject):
    """Lives in the GUI thread; cross-thread emits are queued to it (rule 1)."""

    reply = Signal(object, object)  # (request id, _Ok | _Err)
    opened = Signal(object)  # Capabilities
    failed = Signal(object)  # ButterEyeError

    def __init__(self, bridge: CoreBridge) -> None:
        super().__init__()
        self._bridge = bridge
        self.reply.connect(self._on_reply)
        self.opened.connect(self._on_opened)
        self.failed.connect(self._on_failed)

    @Slot(object, object)
    def _on_reply(self, rid: object, outcome: object) -> None:
        self._bridge._deliver(cast(int, rid), cast("_Ok | _Err", outcome))

    @Slot(object)
    def _on_opened(self, caps: object) -> None:
        self._bridge._on_opened(cast(Capabilities, caps))

    @Slot(object)
    def _on_failed(self, err: object) -> None:
        self._bridge._on_failed(cast(ButterEyeError, err))


def _alive(obj: QObject) -> bool:
    try:
        return bool(shiboken6.isValid(obj))
    except Exception:  # pragma: no cover - defensive
        return False


def _has_qobject(value: object, depth: int = 0) -> bool:
    if isinstance(value, QObject):
        return True
    if depth < 2 and isinstance(value, tuple | list | frozenset | set):
        return any(_has_qobject(v, depth + 1) for v in value)
    return False


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class CoreBridge(QObject):
    """The GUI's only way into the core (§3)."""

    event = Signal(object)  # type: ignore[assignment]  # Event, in order, GUI thread
    ready = Signal(object)  # Capabilities after open()
    fatal = Signal(object)  # ButterEyeError at startup (e.g. BE-2010)
    unresponsive = Signal(bool)  # watchdog

    def __init__(
        self,
        paths: Paths | None = None,
        parent: QObject | None = None,
        *,
        opener: Opener | None = None,
        debug: bool = False,
        ping_interval_s: float = PING_INTERVAL_S,
        ping_timeout_s: float = PING_TIMEOUT_S,
    ) -> None:
        super().__init__(parent)
        self._paths = paths
        self._opener: Opener = opener if opener is not None else ButterEye.open
        self._debug = debug
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s

        self._inbox = _Inbox(self)
        self._thread: CoreThread | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, _Request] = {}
        self._backlog: list[Callable[[], None]] = []

        self._lock = threading.Lock()
        self._events: collections.deque[Event] = collections.deque()
        self._progress: dict[int, Progress] = {}

        # loop-thread state
        self._core: ButterEye | None = None
        self._open_error: ButterEyeError | None = None
        self._core_ready = asyncio.Event()  # binds to the core loop on first use

        # GUI-thread state
        self._caps: Capabilities | None = None
        self._opened = False
        self._failed: ButterEyeError | None = None
        self._closed = False
        self._shutdown_report: ShutdownReport | None = None
        self._timed_out = False
        self._unresponsive = False
        self._ping_sent: float | None = None
        self._last_ping = 0.0

        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(DRAIN_INTERVAL_MS)
        self._drain_timer.timeout.connect(self._drain)
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(
            max(10, int(min(self._ping_interval_s, self._ping_timeout_s) * 250))
        )
        self._watch_timer.timeout.connect(self._watch)

    # ------------------------------------------------------------------ public
    @property
    def capabilities(self) -> Capabilities | None:
        """Latest cached capabilities (``None`` before ``ready``)."""
        return self._caps

    @property
    def is_ready(self) -> bool:
        return self._opened

    @property
    def is_unresponsive(self) -> bool:
        return self._unresponsive

    @property
    def startup_error(self) -> ButterEyeError | None:
        return self._failed

    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        if self._thread is not None or self._closed:
            return
        self._thread = CoreThread(debug=self._debug)
        self._thread.start()
        self._thread.submit(self._open_core())
        backlog, self._backlog = self._backlog, []
        for submit in backlog:
            submit()
        self._drain_timer.start()

    def call(
        self,
        fn: Callable[[ButterEye], Awaitable[T]],
        *,
        owner: QObject,
        ok: Callable[[T], None],
        err: Callable[[ButterEyeError], None] | None = None,
    ) -> Ticket:
        req = self._new_request(owner, ok, err, None, is_op=False)
        self._submit(lambda: self._run_call(req, fn))
        return Ticket(self, req)

    def run_op(
        self,
        fn: Callable[[ButterEye], Operation[T]],
        *,
        owner: QObject,
        ok: Callable[[T], None],
        err: Callable[[ButterEyeError], None] | None = None,
        progress: Callable[[Progress], None] | None = None,
    ) -> Ticket:
        req = self._new_request(owner, ok, err, progress, is_op=True)
        self._submit(lambda: self._run_op(req, fn))
        return Ticket(self, req)

    def shutdown(
        self,
        policy: DetachPolicy,
        *,
        cancel_jobs: bool,
        timeout_s: float = 5.0,
        live: Iterable[SessionId] = (),
    ) -> ShutdownReport | None:
        """Close the core (§3 rule 8).

        ``timeout_s`` is the core's per-stage budget; the GUI waits up to
        ``close_wait_s(timeout_s) + CLOSE_MARGIN_S``. ``None`` if the core never
        opened, or on timeout when ``live`` is empty. On timeout with ``live``
        (the sessions that were live when the user chose to quit), a partial
        report is returned: sessions the core reported as detached while
        closing are ``detached``, the rest ``failed`` (``shutdown_timed_out``
        is then True).
        """
        if self._closed:
            return self._shutdown_report
        self._closed = True
        live_ids = tuple(live)
        self._drain_timer.stop()
        self._watch_timer.stop()
        thread = self._thread
        report: ShutdownReport | None = None
        if thread is not None and self._opened:
            box: list[ShutdownReport] = []
            local = QEventLoop()

            def on_closed(rep: ShutdownReport) -> None:
                box.append(rep)
                local.quit()

            def on_close_error(e: ButterEyeError) -> None:
                _log.warning("core close failed: %s", e)
                local.quit()

            # Bypasses _new_request: new requests are refused once closing.
            req = _Request(next(self._ids), self._inbox, on_closed, on_close_error, None, False)
            self._pending[req.rid] = req
            thread.submit(
                self._run_call(
                    req,
                    lambda core: core.close(policy, cancel_jobs=cancel_jobs, timeout_s=timeout_s),
                )
            )
            timer = QTimer()
            timer.setSingleShot(True)
            timer.timeout.connect(local.quit)
            wait_s = close_wait_s(timeout_s) + CLOSE_MARGIN_S
            timer.start(max(0, int(wait_s * 1000)))
            if not box:
                local.exec()
            timer.stop()
            report = box[0] if box else None
            if report is None:
                self._timed_out = True
                _log.warning("core did not close within %.1f s", wait_s)
                if live_ids:
                    report = self._partial_report(live_ids)
        self._pending.clear()
        with self._lock:
            self._events.clear()
            self._progress.clear()
        if thread is not None:
            thread.stop()
            thread.join(JOIN_TIMEOUT_S)
            if thread.is_alive():
                _log.warning("core thread did not stop within %.1f s", JOIN_TIMEOUT_S)
        self._shutdown_report = report
        return report

    @property
    def shutdown_timed_out(self) -> bool:
        """True when ``shutdown`` gave up waiting for the core's report."""
        return self._timed_out

    def _partial_report(self, live: tuple[SessionId, ...]) -> ShutdownReport:
        """Detach outcome from the events published while the core was closing."""
        with self._lock:
            ended = {ev.sid: ev.reason for ev in self._events if isinstance(ev, SessionEnded)}
        detached = tuple(sid for sid in live if ended.get(sid) == "detached")
        failed = tuple((sid, ErrorCode.IPC_LOST) for sid in live if sid not in detached)
        return ShutdownReport(detached, failed, ())

    # ------------------------------------------------------------------ requests
    def _new_request(
        self,
        owner: QObject,
        ok: Callable[[Any], None],
        err: Callable[[ButterEyeError], None] | None,
        progress: Callable[[Progress], None] | None,
        *,
        is_op: bool,
    ) -> _Request:
        req = _Request(next(self._ids), owner, ok, err, progress, is_op)
        if self._closed:
            req.cancelled = True
        else:
            self._pending[req.rid] = req
        return req

    def _submit(self, make: Callable[[], Awaitable[None]]) -> None:
        if self._closed:
            return
        if self._thread is None:
            self._backlog.append(lambda: self._submit(make))
            return
        self._thread.submit(make())

    def _is_pending(self, req: _Request) -> bool:
        return req.rid in self._pending and not req.cancelled

    def _cancel(self, req: _Request) -> None:
        if req.cancelled:
            return
        req.cancelled = True
        self._pending.pop(req.rid, None)
        with self._lock:
            self._progress.pop(req.rid, None)
        if req.is_op and self._thread is not None:
            self._thread.call_soon(lambda: self._cancel_in_loop(req))

    # ---- loop thread ----
    def _cancel_in_loop(self, req: _Request) -> None:
        if req.op is not None:
            req.op.cancel()

    async def _open_core(self) -> None:
        try:
            core = await self._opener(self._paths)
            self._core = core
            self._core_ready.set()
            stream = core.subscribe()
            asyncio.get_running_loop().create_task(self._pump(stream), name="buttereye-gui-pump")
            caps = await core.capabilities()
        except ButterEyeError as exc:
            self._fail_open(exc)
            return
        except Exception as exc:
            _log.exception("core failed to open")
            self._fail_open(internal_error(exc))
            return
        self._inbox.opened.emit(caps)

    def _fail_open(self, exc: ButterEyeError) -> None:
        self._open_error = exc
        self._core_ready.set()
        self._inbox.failed.emit(exc)

    async def _pump(self, stream: Any) -> None:
        try:
            async for ev in stream:
                with self._lock:
                    self._events.append(ev)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("event pump failed")

    async def _wait_core(self) -> ButterEye:
        await self._core_ready.wait()
        if self._core is None:
            if self._open_error is not None:
                raise self._open_error
            raise internal_error(RuntimeError("the core is not open"))
        return self._core

    def _post(self, req: _Request, outcome: _Ok | _Err) -> None:
        if req.cancelled:
            return
        if self._debug and isinstance(outcome, _Ok) and _has_qobject(outcome.value):
            outcome = _Err(internal_error(TypeError("a QObject crossed the bridge (§3 rule 7)")))
        self._inbox.reply.emit(req.rid, outcome)

    async def _run_call(self, req: _Request, fn: Callable[[ButterEye], Awaitable[Any]]) -> None:
        try:
            core = await self._wait_core()
            if req.cancelled:
                return
            value = await fn(core)
        except asyncio.CancelledError:
            raise
        except ButterEyeError as exc:
            self._post(req, _Err(exc))
            return
        except Exception as exc:
            _log.exception("core call failed")
            self._post(req, _Err(internal_error(exc)))
            return
        self._post(req, _Ok(value))

    async def _run_op(self, req: _Request, fn: Callable[[ButterEye], Operation[Any]]) -> None:
        try:
            core = await self._wait_core()
            if req.cancelled:
                return
            op = fn(core)
            req.op = op
            if req.cancelled:
                op.cancel()
            if req.progress is not None:
                rid = req.rid

                def sink(p: Progress) -> None:
                    with self._lock:
                        self._progress[rid] = p

                op.add_progress_sink(sink)
            value = await op.result()
        except asyncio.CancelledError:
            raise
        except ButterEyeError as exc:
            self._post(req, _Err(exc))
            return
        except Exception as exc:
            _log.exception("core operation failed")
            self._post(req, _Err(internal_error(exc)))
            return
        self._post(req, _Ok(value))

    # ---- GUI thread ----
    def _deliver(self, rid: int, outcome: _Ok | _Err) -> None:
        req = self._pending.pop(rid, None)
        if req is None or req.cancelled:
            return
        with self._lock:
            last = self._progress.pop(rid, None)
        if not _alive(req.owner):
            return
        try:
            if last is not None and req.progress is not None:
                req.progress(last)  # the final progress arrives before ok/err
            if isinstance(outcome, _Ok):
                req.ok(outcome.value)
                return
            handler = req.err
            if handler is None:
                show = getattr(req.owner, "show_error", None)
                if callable(show):
                    handler = cast(Callable[[ButterEyeError], None], show)
            if handler is None:
                _log.warning(
                    "unhandled core error for %s: %s", type(req.owner).__name__, outcome.error
                )
                return
            handler(outcome.error)
        except Exception:
            _log.exception("reply handler failed")

    def _drain(self) -> None:
        with self._lock:
            events = list(self._events)
            self._events.clear()
            progress = self._progress
            self._progress = {}
        for rid, p in progress.items():
            req = self._pending.get(rid)
            if req is None or req.cancelled or req.progress is None or not _alive(req.owner):
                continue
            try:
                req.progress(p)
            except Exception:
                _log.exception("progress handler failed")
        for ev in events:
            if isinstance(ev, CapabilitiesChanged):
                self._caps = ev.caps
            self.event.emit(ev)

    def _on_opened(self, caps: Capabilities) -> None:
        if self._closed:
            return
        self._opened = True
        self._caps = caps
        self._last_ping = 0.0
        self._watch_timer.start()
        self._watch()  # first liveness check right away
        self.ready.emit(caps)

    def _on_failed(self, err: ButterEyeError) -> None:
        self._failed = err
        if not self._closed:
            self.fatal.emit(err)

    def drain_now(self) -> None:
        """Deliver queued events and progress immediately (tests, shutdown)."""
        self._drain()

    # ---- watchdog (rule 6) ----
    def _watch(self) -> None:
        if self._closed or not self._opened:
            return
        now = time.monotonic()
        if self._ping_sent is None:
            if now - self._last_ping >= self._ping_interval_s:
                self._send_ping(now)
        elif now - self._ping_sent > self._ping_timeout_s and not self._unresponsive:
            self._unresponsive = True
            _log.warning("core loop unresponsive for %.1f s", now - self._ping_sent)
            self.unresponsive.emit(True)

    def _send_ping(self, now: float) -> None:
        self._ping_sent = now
        self.call(
            lambda core: core.ping(), owner=self._inbox, ok=self._on_pong, err=self._on_ping_error
        )

    def _on_pong(self, _value: float) -> None:
        self._ping_sent = None
        self._last_ping = time.monotonic()
        if self._unresponsive:
            self._unresponsive = False
            self.unresponsive.emit(False)

    def _on_ping_error(self, err: ButterEyeError) -> None:
        _log.warning("ping failed: %s", err)
        self._on_pong(0.0)


__all__ = ["CoreBridge", "CoreThread", "Ticket", "Opener"]
