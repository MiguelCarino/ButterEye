# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What a benchmark number means: the two headroom bars and the verdicts on them. Pure.

One definition for the benchmark (``runner``), backend selection
(``backends.select``), live playback (``mpvctl.session``) and the GUI (through
``api``), so the speed test cannot call an engine "real time" by one bar while
playback holds it to another. Imports nothing but the types, so live playback can
use it without loading the benchmark itself (an optional provider).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any

from buttereye.core.types import BackendId, BenchMeasurement, BenchRequest, BenchResult

#: The benchmark's own verdict (F2): in-mpv fps >= this x the target is "real
#: time" in the results table. The bench runs mpv untimed with ``--vo=null``.
REALTIME_HEADROOM = 1.15
#: Live playback cap = benchmark ``mpv_fps`` / this (§11.9). Real playback also
#: renders every output frame to the display and shares the GPU with the desktop
#: compositor, so it sustains less: 1080p RIFE-ncnn at 61 fps in the bench is ~49
#: live, enough for 24 -> 48 but not for 30 -> 60. The 1.15 / 1.25 split is
#: deliberate; :func:`live_ok` is the verdict on this bar. Both are values to tune
#: per platform on the device.
LIVE_HEADROOM = 1.25
#: A result older than this is re-measured in the background (it is still used
#: until the new one is in): drivers, thermal paste and background load drift.
STALE_AFTER = timedelta(days=60)
#: The engines live playback chooses between per file (mvgpu, MVTools). The
#: benchmark measures these first, while the machine is cool, measures them at
#: the bigger sizes too, and holds the best of them for a sustained phase.
LIVE_BACKENDS = frozenset({BackendId.MVGPU, BackendId.MVTOOLS})


def target_of(req: BenchRequest) -> Fraction:
    """The rate a request measured at (None = twice the source rate)."""
    return req.target_fps if req.target_fps is not None else req.source_fps * 2


def measured_in_mpv(m: BenchMeasurement | Any) -> bool:
    """False for an entry whose in-mpv pass failed: the runner then stores the
    vspipe speed as ``mpv_fps`` (and the vspipe reload time as ``startup_s``)
    with ``realtime`` False."""
    if getattr(m, "realtime", False):
        return True
    return not (m.mpv_fps == m.vspipe_fps and m.startup_s == m.reload_s)


def real_measurement(m: BenchMeasurement) -> bool:
    """A number taken inside mpv, without GPU faults: what live caps may use."""
    return measured_in_mpv(m) and m.mpv_fps > 0 and not m.gpu_faults


def realtime(mpv_fps: float, target: Fraction) -> bool:
    """The benchmark's verdict (``REALTIME_HEADROOM``)."""
    return mpv_fps >= REALTIME_HEADROOM * float(target)


def live_ok(m: BenchMeasurement, target: Fraction) -> bool:
    """Live playback would keep up at ``target`` with this measurement: the same
    bar ``Session.sustainable_fps`` divides by. A failed in-mpv pass or a GPU
    fault is never live-ok."""
    return real_measurement(m) and m.mpv_fps / LIVE_HEADROOM >= float(target)


def holds_the_bar(runs: list[float] | tuple[float, ...], target: Fraction) -> bool:
    """Repeatable (B6): every in-mpv run came out on the same side of the
    real-time bar as their median. The verdict is what has to repeat, not the
    number: MVTools 72 % above the bar with a 5.2 % spread is as repeatable as a
    verdict gets, and two 3-run series with 4 % jitter failed a 5 % spread test
    about 38 % of the time. No runs: not repeatable."""
    if not runs:
        return False
    s = sorted(runs)
    n = len(s)
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    verdict = realtime(median, target)
    return all(realtime(f, target) == verdict for f in s)


def stale(result: BenchResult, now: datetime) -> bool:
    """Older than ``STALE_AFTER`` (an undated or naive timestamp counts as UTC)."""
    when = result.when
    if when.tzinfo is None and now.tzinfo is not None:
        when = when.replace(tzinfo=now.tzinfo)
    elif when.tzinfo is not None and now.tzinfo is None:
        now = now.replace(tzinfo=when.tzinfo)
    return now - when > STALE_AFTER


__all__ = [
    "REALTIME_HEADROOM",
    "LIVE_HEADROOM",
    "STALE_AFTER",
    "LIVE_BACKENDS",
    "target_of",
    "measured_in_mpv",
    "real_measurement",
    "realtime",
    "live_ok",
    "holds_the_bar",
    "stale",
]
