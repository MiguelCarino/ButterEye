# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live health detection (GUI.md R12, §8 risk 2; SCOPE §4.11, F1 Xid line).

``HealthDetector`` is a pure state machine fed with property samples. It answers
two questions for the session: are frames flowing through the filter (so the
filter may be called ACTIVE), and has playback failed in the M0(f) way, where
audio keeps running over a frozen picture (STALLED, which the session answers
with an automatic rollback). It also computes the drop rate over 10 s (DROPPING
above 1 % of output frames, §4.11).

Signals were measured on mpv 0.41.0 with ``--vo=null --ao=null``: a filter that
never returns a frame freezes ``playback-time`` with ``core-idle`` true while not
paused; a filter that is too slow lets ``audio-pts`` run ahead of
``playback-time``. ``estimated-vf-fps`` is not a performance signal (§4.2 step 7).

Thresholds are first M1 values, unvalidated on real content (GUI.md §8 risk 2).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from buttereye.core import capabilities
from buttereye.core.capabilities import ProviderRef
from buttereye.core.errors import ErrorCode
from buttereye.core.mpvctl.ipc import LABEL
from buttereye.core.types import Health, Msg

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Thresholds:
    flow_min_s: float = 0.3  # playback advance that proves frames are flowing
    init_grace_s: float = 12.0  # filter (re)init allowance (RIFE model load, Vulkan setup)
    frozen_s: float = 4.0  # playback frozen while playing, after frames flowed once
    lag_s: float = 2.0  # audio ahead of video by this much ...
    lag_hold_s: float = 2.0  # ... for this long → STALLED
    drop_window_s: float = 10.0
    drop_ratio: float = 0.01  # > 1 % of output frames dropped over the window
    drop_min_span_s: float = 5.0  # need this much history before judging drops


@dataclass(frozen=True, slots=True)
class Sample:
    t: float  # monotonic seconds
    playback_time: float | None
    audio_pts: float | None
    paused: bool
    core_idle: bool
    paused_for_cache: bool
    seeking: bool
    frame_drops: int


@dataclass(frozen=True, slots=True)
class Verdict:
    health: Health
    code: ErrorCode | None
    reason: Msg | None
    evidence: tuple[str, ...]
    flowing: bool
    drop_rate: float | None


def sample_from(props: Mapping[str, Any], t: float) -> Sample:
    def num(key: str) -> float | None:
        v = props.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    drops = props.get("frame-drop-count")
    return Sample(
        t=t,
        playback_time=num("playback-time"),
        audio_pts=num("audio-pts"),
        paused=props.get("pause") is True,
        core_idle=props.get("core-idle") is True,
        paused_for_cache=props.get("paused-for-cache") is True,
        seeking=props.get("seeking") is True,
        frame_drops=drops if isinstance(drops, int) and not isinstance(drops, bool) else 0,
    )


class HealthDetector:
    """Feed ``update()`` every tick; call ``filter_started()`` after each verified
    ``vf add`` and ``filter_stopped()`` after a removal. STALLED latches until the
    next ``filter_started()``; ButterEye never re-enables a filter by itself."""

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.th = thresholds or Thresholds()
        self.active = False
        self.flowing = False
        self._start = 0.0
        self._grace_until = 0.0
        self._last_pt: float | None = None
        self._last_progress = 0.0
        self._advance = 0.0
        self._lag_since: float | None = None
        self._stalled: Verdict | None = None
        self._drops: deque[tuple[float, int]] = deque()
        self._target_fps: float | None = None

    def filter_started(self, t: float, target_fps: float | None) -> None:
        self.active = True
        self.flowing = False
        self._start = t
        self._grace_until = t + self.th.init_grace_s
        self._last_pt = None
        self._last_progress = t
        self._advance = 0.0
        self._lag_since = None
        self._stalled = None
        self._drops.clear()
        self._target_fps = target_fps

    def filter_stopped(self) -> None:
        self.active = False
        self.flowing = False
        self._lag_since = None
        self._drops.clear()

    def note_restart(self, t: float) -> None:
        """A seek or a filter reinit: the script and core are recreated (§4.4)."""
        self._grace_until = max(self._grace_until, t + self.th.init_grace_s)
        self._last_progress = t
        self._last_pt = None
        self._lag_since = None
        self._drops.clear()

    def _ok(self, drop_rate: float | None) -> Verdict:
        return Verdict(Health.OK, None, None, (), self.flowing, drop_rate)

    def update(self, s: Sample) -> Verdict:
        if self._stalled is not None:
            return self._stalled
        if not self.active:
            return self._ok(None)
        th = self.th
        holding = s.paused or s.paused_for_cache or s.seeking
        pt = s.playback_time
        if pt is not None and self._last_pt is not None:
            delta = pt - self._last_pt
            if delta > 1e-3:
                self._last_progress = s.t
                if delta < 5.0:  # ignore jumps (seeks)
                    self._advance += delta
        self._last_pt = pt
        if not self.flowing and self._advance >= th.flow_min_s:
            self.flowing = True
        if holding:
            self._last_progress = s.t
            self._lag_since = None

        # 1. frozen picture while playing (no frame ever, or no frame for frozen_s)
        if not holding:
            settled = self.flowing and s.t >= self._grace_until
            limit = th.frozen_s if settled else th.init_grace_s
            frozen_for = s.t - self._last_progress
            if frozen_for >= limit:
                shown = f"{pt:.2f} s" if pt is not None else "unavailable"
                return self._stall(
                    Msg("The picture stopped while playback was not paused."),
                    (f"playback-time {shown}, unchanged for {frozen_for:.1f} s",),
                )

        # 2. audio running ahead of a stalled picture (the M0(f) failure)
        if not holding and s.audio_pts is not None and pt is not None:
            lag = s.audio_pts - pt
            if lag >= th.lag_s:
                if self._lag_since is None:
                    self._lag_since = s.t
                if s.t - self._lag_since >= th.lag_hold_s:
                    return self._stall(
                        Msg("Video stalled while audio kept playing."),
                        (f"audio-pts {s.audio_pts:.2f} s, playback-time {pt:.2f} s",),
                    )
            else:
                self._lag_since = None

        # 3. drops over the window
        self._drops.append((s.t, s.frame_drops))
        while self._drops and s.t - self._drops[0][0] > th.drop_window_s:
            self._drops.popleft()
        rate = self._drop_rate()
        if rate is not None and rate > th.drop_ratio:
            return Verdict(
                Health.DROPPING,
                None,
                Msg("Dropping frames ({percent}% over 10 s).", {"percent": round(rate * 100, 1)}),
                (),
                self.flowing,
                rate,
            )
        return self._ok(rate)

    def _drop_rate(self) -> float | None:
        if len(self._drops) < 2 or not self._target_fps:
            return None
        (t0, d0), (t1, d1) = self._drops[0], self._drops[-1]
        span = t1 - t0
        if span < self.th.drop_min_span_s:
            return None
        return max(0, d1 - d0) / (self._target_fps * span)

    def _stall(self, reason: Msg, evidence: tuple[str, ...]) -> Verdict:
        self._stalled = Verdict(
            Health.STALLED, ErrorCode.FILTER_STALLED, reason, evidence, self.flowing, None
        )
        return self._stalled


# ---------------------------------------------------------------------------
# mpv log classification
# ---------------------------------------------------------------------------

LogClass = Literal["filter_failed", "device_lost"]

_VS_FAILURES = ("Script evaluation failed", "could not init VS", "Filter error at frame")
DEVICE_LOST_MARKERS = (
    "vkQueueSubmit failed",
    "vkWaitForFences failed",
    "VK_ERROR_DEVICE_LOST",
)


def classify_log(msg: Mapping[str, Any], *, only_our_vapoursynth: bool = True) -> LogClass | None:
    """Classify an mpv ``log-message`` event.

    ``only_our_vapoursynth``: the generic vapoursynth errors count only when
    ``@buttereye`` is the only vapoursynth filter in the chain.
    """
    prefix, level, text = msg.get("prefix"), msg.get("level"), msg.get("text")
    if not isinstance(text, str):
        return None
    if any(m in text for m in DEVICE_LOST_MARKERS):
        return "device_lost"
    if prefix == "vf" and f"Disabling filter {LABEL} because it has failed" in text:
        return "filter_failed"
    if prefix == "vapoursynth" and level in ("fatal", "error") and only_our_vapoursynth:
        if text.startswith(_VS_FAILURES):
            return "filter_failed"
    return None


def device_lost_lines(log_file: Path, *, max_bytes: int = 256 * 1024) -> tuple[str, ...]:
    """Device-lost lines in the tail of an mpv log (ncnn writes them to stderr)."""
    try:
        with log_file.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read()
    except OSError:
        return ()
    lines = data.decode("utf-8", errors="replace").splitlines()
    hits = [ln.strip() for ln in lines if any(m in ln for m in DEVICE_LOST_MARKERS)]
    return tuple(hits[:5])


# ---------------------------------------------------------------------------
# GPU fault (Xid) check through U3's doctor.gpufault.scan_xid (lazy; absent = unknown)
# ---------------------------------------------------------------------------

GPUFAULT_MODULE = "buttereye.core.doctor.gpufault"
JOURNAL_SETTLE_S = 0.3  # journald needs a moment to store the kernel lines of a late fault


@dataclass(frozen=True, slots=True)
class FaultScan:
    """New Xid lines and the timestamps of the newest one (the next scan's cursor)."""

    lines: tuple[str, ...]
    newest_wall: float | None  # seconds since the epoch
    newest_monotonic: float | None  # CLOCK_MONOTONIC seconds, this boot


async def gpu_fault_scan(
    *, since_monotonic: float, since_wall: float | None = None
) -> FaultScan | None:
    """NVIDIA Xid kernel lines logged at or after the cursor (F1, M0(f)).

    ``None`` means unknown: the scanner is not in this build, or the journal is
    unreadable. Never reported as "no faults". Not filtered by pid: the driver
    reports the faulting thread (``name=vo``), and any fault during a RIFE
    session on this user's desktop matters. Callers move the cursor past
    ``newest_*`` so each fault is reported once.
    """
    scan = capabilities.resolve(ProviderRef(GPUFAULT_MODULE, "scan_xid"))
    if scan is None:
        return None
    await asyncio.sleep(JOURNAL_SETTLE_S)
    try:
        result = scan(since_monotonic=since_monotonic, since_wall=since_wall)
        if inspect.isawaitable(result):
            result = await result
    except Exception:
        _log.warning("GPU fault check failed", exc_info=True)
        return None
    if getattr(result, "readable", False) is not True:
        return None
    lines = getattr(result, "lines", None)
    if not isinstance(lines, tuple):
        return None
    newest_wall: float | None = None
    newest_mono: float | None = None
    for ev in getattr(result, "events", ()) or ():
        rt, mono = getattr(ev, "realtime_us", None), getattr(ev, "monotonic_us", None)
        if isinstance(rt, int):
            newest_wall = max(newest_wall or 0.0, rt / 1e6)
        if isinstance(mono, int):
            newest_mono = max(newest_mono or 0.0, mono / 1e6)
    return FaultScan(tuple(str(x) for x in lines), newest_wall, newest_mono)


async def gpu_fault_lines(
    *, since_monotonic: float, since_wall: float | None = None
) -> tuple[str, ...] | None:
    """``gpu_fault_scan`` lines only (``None`` = unknown)."""
    res = await gpu_fault_scan(since_monotonic=since_monotonic, since_wall=since_wall)
    return None if res is None else res.lines


def mark_rife_session(paths: Any, pids: Iterable[int]) -> None:
    """Record that RIFE-ncnn starts in ``pids`` now (``doctor.gpufault`` session
    marker), so the doctor blames only faults from this point on (BE-1030).
    Best effort: an absent scanner module or an unwritable state folder is logged."""
    write = capabilities.resolve(ProviderRef(GPUFAULT_MODULE, "write_session_marker"))
    if write is None:
        return
    try:
        write(paths, pids=tuple(pids))
    except OSError as exc:
        _log.warning("could not record the RIFE session marker: %s", exc)


__all__ = [
    "mark_rife_session",
    "Thresholds",
    "Sample",
    "Verdict",
    "HealthDetector",
    "sample_from",
    "classify_log",
    "device_lost_lines",
    "gpu_fault_lines",
    "gpu_fault_scan",
    "FaultScan",
    "DEVICE_LOST_MARKERS",
    "GPUFAULT_MODULE",
]
