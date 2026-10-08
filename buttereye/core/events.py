# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Core event stream (docs/design/GUI.md §2.3, FROZEN) and its bus.

Ordering rule: transitions are never dropped. Only *coalesced* events (counter-only
``SessionChanged``) may be dropped when a subscriber falls behind; the subscriber
then receives one ``EventsDropped`` and must resync from snapshots.
``Progress`` is not an ``Event``; it goes to the op's ``ProgressSink``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, cast

from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.types import (
    Capabilities,
    Health,
    InstanceCandidate,
    Msg,
    OpId,
    OpKind,
    OpState,
    RenderJobState,
    SessionId,
    SessionSnapshot,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Progress:
    op_id: OpId
    phase: Msg
    done: int | None
    total: int | None  # total None -> indeterminate
    unit: Literal["frames", "bytes", "steps", "checks"]
    rate: float | None
    eta_s: float | None  # render ETA from the slower of vspipe/ffmpeg (§7.6)
    detail: Msg | None = None


@dataclass(frozen=True, slots=True)
class OpStarted:
    op_id: OpId
    kind: OpKind


@dataclass(frozen=True, slots=True)
class OpFinished:
    op_id: OpId
    state: OpState
    error: ButterEyeError | None


@dataclass(frozen=True, slots=True)
class SessionAdded:
    snapshot: SessionSnapshot


@dataclass(frozen=True, slots=True)
class SessionChanged:
    snapshot: SessionSnapshot  # coalesced <= 4 Hz per session for counter-only changes


@dataclass(frozen=True, slots=True)
class SessionEnded:
    sid: SessionId
    reason: Literal["mpv_exited", "detached", "connection_lost"]


@dataclass(frozen=True, slots=True)
class HealthChanged:
    sid: SessionId
    health: Health
    code: ErrorCode | None
    reason: Msg
    evidence: tuple[str, ...]
    auto_action: Msg | None  # "interpolation turned off"


@dataclass(frozen=True, slots=True)
class JobChanged:
    job: RenderJobState


@dataclass(frozen=True, slots=True)
class ConfigChanged:
    revision: str  # emitted after any save through this core


@dataclass(frozen=True, slots=True)
class CapabilitiesChanged:
    caps: Capabilities


@dataclass(frozen=True, slots=True)
class OrphansFound:
    candidates: tuple[InstanceCandidate, ...]


@dataclass(frozen=True, slots=True)
class Notice:
    message: Msg
    code: ErrorCode | None = None
    sid: SessionId | None = None


@dataclass(frozen=True, slots=True)
class EventsDropped:
    count: int  # subscriber must resync via snapshots


Event = (
    OpStarted
    | OpFinished
    | SessionAdded
    | SessionChanged
    | SessionEnded
    | HealthChanged
    | JobChanged
    | ConfigChanged
    | CapabilitiesChanged
    | OrphansFound
    | Notice
    | EventsDropped
)

EVENT_TYPES: tuple[type, ...] = (
    OpStarted,
    OpFinished,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    HealthChanged,
    JobChanged,
    ConfigChanged,
    CapabilitiesChanged,
    OrphansFound,
    Notice,
    EventsDropped,
)


def coalesce_key(ev: Event) -> str | None:
    """The coalescing key an event belongs to: its session id, or ``None``."""
    if isinstance(ev, (SessionAdded, SessionChanged)):
        return str(ev.snapshot.sid)
    if isinstance(ev, (SessionEnded, HealthChanged)):
        return str(ev.sid)
    if isinstance(ev, Notice) and ev.sid is not None:
        return str(ev.sid)
    return None


# ---------------------------------------------------------------------------
# Bus (implementation detail shared by ButterEye and FakeCore; loop thread only)
# ---------------------------------------------------------------------------

_CLOSED = object()


class _Subscriber:
    __slots__ = ("maxlen", "queue", "wakeup", "dropped", "closed")

    def __init__(self, maxlen: int) -> None:
        self.maxlen = maxlen
        # (event, droppable) pairs; _CLOSED sentinel ends the iterator.
        self.queue: deque[tuple[object, bool]] = deque()
        self.wakeup = asyncio.Event()
        self.dropped = 0
        self.closed = False

    def push(self, ev: Event, droppable: bool) -> None:
        if self.closed:
            return
        if len(self.queue) >= self.maxlen:
            # Make room by dropping the oldest droppable entry; transitions stay.
            for i, (_, can_drop) in enumerate(self.queue):
                if can_drop:
                    del self.queue[i]
                    self.dropped += 1
                    break
            else:
                if droppable:
                    self.dropped += 1
                    self.wakeup.set()
                    return
                # Only transitions queued: grow past maxlen rather than lose one.
        self.queue.append((ev, droppable))
        self.wakeup.set()

    def close(self) -> None:
        self.closed = True
        self.queue.append((_CLOSED, False))
        self.wakeup.set()


class EventBus:
    """Fan-out of core events to subscribers; must be used on the core loop."""

    def __init__(self) -> None:
        self._subs: list[_Subscriber] = []
        self._closed = False
        # coalescing: key -> (latest pending event or None, last delivery time, handle)
        self._pending: dict[str, Event] = {}
        self._last_sent: dict[str, float] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}

    @property
    def closed(self) -> bool:
        return self._closed

    def publish(self, ev: Event, *, key: str | None = None) -> None:
        """Deliver a transition to every subscriber. Never dropped.

        Ordering with coalesced updates: if a coalesced update for the same key
        is still pending (its flush timer has not fired), it is delivered *now*,
        ahead of the transition, instead of later with older state on top of the
        newer transition (§2.3: no stale overwrite). ``key`` defaults to the
        event's session id (``coalesce_key``), the key ``emit_coalesced`` uses.
        """
        if not isinstance(ev, EVENT_TYPES):
            raise TypeError(f"not an Event: {type(ev).__name__}")
        k = key if key is not None else coalesce_key(ev)
        if k is not None and k in self._timers:
            self._flush_now(k)
        for sub in tuple(self._subs):
            sub.push(ev, False)

    def _flush_now(self, key: str) -> None:
        handle = self._timers.pop(key, None)
        if handle is not None:
            handle.cancel()
        pending = self._pending.pop(key, None)
        if pending is not None and not self._closed:
            self._deliver_droppable(key, pending, time.monotonic())

    def publish_coalesced(self, key: str, ev: Event, *, min_interval_s: float = 0.25) -> None:
        """Deliver ``ev`` at most once per ``min_interval_s`` for ``key``; latest wins.

        Used for counter-only ``SessionChanged`` (key = session id). Such events
        are droppable when a subscriber is full.
        """
        if not isinstance(ev, EVENT_TYPES):
            raise TypeError(f"not an Event: {type(ev).__name__}")
        if self._closed:
            return
        now = time.monotonic()
        last = self._last_sent.get(key)
        if key in self._timers:
            self._pending[key] = ev  # a flush is scheduled; replace the payload
            return
        if last is None or now - last >= min_interval_s:
            self._deliver_droppable(key, ev, now)
            return
        self._pending[key] = ev
        loop = asyncio.get_running_loop()
        self._timers[key] = loop.call_later(min_interval_s - (now - last), self._flush, key)

    def _deliver_droppable(self, key: str, ev: Event, now: float) -> None:
        self._last_sent[key] = now
        for sub in tuple(self._subs):
            sub.push(ev, True)

    def _flush(self, key: str) -> None:
        self._timers.pop(key, None)
        ev = self._pending.pop(key, None)
        if ev is not None and not self._closed:
            self._deliver_droppable(key, ev, time.monotonic())

    def forget(self, key: str) -> None:
        """Drop pending coalesced state for ``key`` (e.g. the session ended)."""
        handle = self._timers.pop(key, None)
        if handle is not None:
            handle.cancel()
        self._pending.pop(key, None)
        self._last_sent.pop(key, None)

    def subscribe(self, maxlen: int = 4096) -> AsyncIterator[Event]:
        if maxlen < 1:
            raise ValueError("maxlen must be >= 1")
        sub = _Subscriber(maxlen)
        if self._closed:
            sub.close()
        else:
            self._subs.append(sub)
        return self._iterate(sub)

    async def _iterate(self, sub: _Subscriber) -> AsyncIterator[Event]:
        try:
            while True:
                if sub.dropped:
                    count, sub.dropped = sub.dropped, 0
                    yield EventsDropped(count)
                    continue
                if not sub.queue:
                    sub.wakeup.clear()
                    await sub.wakeup.wait()
                    continue
                item, _ = sub.queue.popleft()
                if item is _CLOSED:
                    return
                yield cast(Event, item)
        finally:
            sub.closed = True
            if sub in self._subs:
                self._subs.remove(sub)

    def close(self) -> None:
        """End every subscription; pending coalesced events are flushed first."""
        if self._closed:
            return
        for key in tuple(self._timers):
            handle = self._timers.pop(key)
            handle.cancel()
            ev = self._pending.pop(key, None)
            if ev is not None:
                self._deliver_droppable(key, ev, time.monotonic())
        self._closed = True
        for sub in tuple(self._subs):
            sub.close()
        self._subs.clear()

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)


__all__ = [
    "Progress",
    "OpStarted",
    "OpFinished",
    "SessionAdded",
    "SessionChanged",
    "SessionEnded",
    "HealthChanged",
    "JobChanged",
    "ConfigChanged",
    "CapabilitiesChanged",
    "OrphansFound",
    "Notice",
    "EventsDropped",
    "Event",
    "EVENT_TYPES",
    "EventBus",
]
