# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What live playback learned about an engine's speed (§11.9, live correction).

The speed test runs a generated 8-bit clip; a real film can cost far more (4K
HDR10 decode, 10-bit conversion and display share the GPU with smoothing). When
an engine that Automatic chose keeps dropping frames heavily, the session records
a factor below 1 for that engine at that size on that GPU and decides again; the
estimate (``Session.sustainable_fps``) is multiplied by it from then on, so the
next time the same file opens it is not chosen again.

Records live in ``<state>/live-limits.json`` and expire after ``MAX_AGE_S``
(drivers and ButterEye change; a new chance is cheap). Pure apart from ``load``
and ``record``, which read and write that file.

A factor is not a ratchet: a sustained clean run of the engine it slowed down
raises it again (``raised``, from the session), and a record that is not
confirmed recovers with age (``RECOVER_AGE_S``), because a factor learned once
rules the engine out of that size, so it might never run there again to prove
otherwise. On a fanless Mac heat is the usual cause of a slow scene: a record is
not made at all while macOS reports a serious thermal state or Low Power Mode
(the session replaces the engine for that video only), and one made at "fair"
expires after ``FAIR_MAX_AGE_S``. Records carry the power state they were made in.
"""

from __future__ import annotations

import contextlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from buttereye.core.types import PowerSnapshot

FILE = "live-limits.json"
MAX_AGE_S = 30 * 24 * 3600.0
#: a record made at thermal state "fair" (1): warm, not throttled by macOS's own
#: account, but already slower than the cold speed test; kept only this long
FAIR_MAX_AGE_S = 6 * 3600.0
#: a record not confirmed for this long gives back one MARGIN step (0.585 ->
#: 0.65 -> 0.72 -> 0.8 -> 0.89 -> 0.99 -> gone after 12 days)
RECOVER_AGE_S = 2 * 24 * 3600.0
#: what is kept of the observed rate: a little under it, so the next choice fits
MARGIN = 0.9
#: a factor never goes below this (an engine is not ruled out by one bad scene)
FLOOR = 0.2
#: Records carry this; older ones are ignored. Version 2 (after 0.0.3):
#: before it a 2x target could run above the display rate, where mpv drops the
#: surplus frames by design (``decide.over_display``), and those drops were
#: learned as slowness - on an Apple M2 at 60 Hz, 50 -> 100 fps drops 40 % with
#: any engine, and the store held exactly ``(1 - 0.4) x MARGIN`` = 0.54 for
#: MVTools at 1080p and at 720p, which ruled out sizes that measured real time.
#: Such a record can't be told from a real one, and a new chance is cheap.
#: Version 3 (after 0.0.5): drops while ButterEye built a TensorRT engine on the
#: same GPU, and near-total drops (a hidden window, another program on the GPU),
#: were learned as slowness - the dev box held the floor (0.2) for mvgpu at
#: 1080p, 1440p and 4K and for TensorRT at 1080p, which left a 4K film on CPU
#: MVTools. Neither is learned now (``session.IMPLAUSIBLE_DROP``).
#: Version 4 (after 0.0.5): heat and Low Power Mode were learned as slowness on
#: the fanless Air - two hot ten-second drop episodes took mvgpu's 1080p cap
#: from 71.8 to 42, under the 47.95 a 2x of 23.976 needs, for 30 days, and the
#: factor could only go down. A version 3 record can't say whether it was made
#: hot; version 4 records say (``thermal``, ``battery``, ``low_power``).
VERSION = 4


@dataclass(frozen=True, slots=True)
class LimitKey:
    gpu: str | None  # Vulkan device UUID (None: unknown)
    backend: str
    model: str | None
    width: int  # the size smoothing runs at
    height: int
    flow: float = 1.0  # TensorRT RIFE flow scale

    def text(self) -> str:
        return (
            f"{self.gpu or '-'}|{self.backend}|{self.model or '-'}|"
            f"{self.width}x{self.height}|{self.flow:g}"
        )


def recovered(factor: float, age_s: float) -> float:
    """``factor`` after ``age_s`` without a new record: one ``MARGIN`` step back
    per ``RECOVER_AGE_S``, never above 1."""
    steps = math.floor(max(0.0, age_s) / RECOVER_AGE_S)
    return min(1.0, factor / MARGIN**steps) if steps else factor


def _max_age(rec: dict[str, float]) -> float:
    return FAIR_MAX_AGE_S if rec.get("thermal") == 1 else MAX_AGE_S


def load(path: Path, *, now: float | None = None) -> dict[str, dict[str, float]]:
    """The records still in force, as stored: the factor learned and when. Age
    is applied where a factor is read (``factor``), never to what is stored:
    ``record`` writes the whole dict back, and a recovered factor written next
    to its old ``when`` would recover again at every load. An unreadable file
    reads as none."""
    now = time.time() if now is None else now
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    out: dict[str, dict[str, float]] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        if not (isinstance(k, str) and isinstance(v, dict)):
            continue
        f, when = v.get("factor"), v.get("when")
        if v.get("v") != VERSION:
            continue
        if not (isinstance(f, (int, float)) and isinstance(when, (int, float))):
            continue
        if not 0 < f <= 1:
            continue
        rec: dict[str, float] = {"factor": float(f), "when": float(when), "v": VERSION}
        for extra in ("thermal", "battery", "low_power"):
            if isinstance(v.get(extra), (bool, int)):
                rec[extra] = v[extra]
        if _aged(rec, now) >= 1.0:
            continue  # expired, or nothing left of it
        out[k] = rec
    return out


def _aged(rec: dict[str, float], now: float) -> float:
    age = now - rec["when"]
    if age > _max_age(rec):
        return 1.0
    return recovered(rec["factor"], age)


def factor(
    limits: dict[str, dict[str, float]], key: LimitKey, *, now: float | None = None
) -> float:
    """Multiplier for the estimate of ``key`` (1.0: nothing learned): the stored
    factor ``recovered`` for its age, 1.0 once expired. Judged at each read, so a
    core that runs for days still lets a "fair" record go after
    ``FAIR_MAX_AGE_S`` and an old one recover step by step."""
    rec = limits.get(key.text())
    if rec is None:
        return 1.0
    return _aged(rec, time.time() if now is None else now)


def lowered(current: float, drop_rate: float) -> float:
    """The new factor after dropping ``drop_rate`` of the frames: what got through,
    a margin below it, never under ``FLOOR``."""
    kept = max(0.0, 1.0 - drop_rate)
    return max(FLOOR, min(current, current * kept * MARGIN))


def raised(current: float) -> float:
    """The factor after a sustained clean run at it: one ``MARGIN`` step back up
    (the margin ``lowered`` took off first), never above 1."""
    return min(1.0, current / MARGIN)


def record(
    path: Path,
    limits: dict[str, dict[str, float]],
    key: LimitKey,
    new_factor: float,
    *,
    now: float | None = None,
    power: PowerSnapshot | None = None,
) -> None:
    """Store ``new_factor`` for ``key`` (in ``limits`` and on disk), with the
    ``power`` state it was learned in; a factor of 1 removes the record. A failed
    write keeps it for this run only."""
    if new_factor >= 1.0:
        limits.pop(key.text(), None)
    else:
        rec: dict[str, float] = {
            "factor": new_factor,
            "when": time.time() if now is None else now,
            "v": VERSION,
        }
        if power is not None:
            if power.thermal is not None:
                rec["thermal"] = power.thermal
            if power.on_battery is not None:
                rec["battery"] = power.on_battery
            if power.low_power is not None:
                rec["low_power"] = power.low_power
        limits[key.text()] = rec
    from buttereye.core.scriptgen.generator import write_atomic

    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(path, json.dumps(limits, indent=1, sort_keys=True) + "\n")


__all__ = [
    "FAIR_MAX_AGE_S",
    "FILE",
    "FLOOR",
    "MARGIN",
    "MAX_AGE_S",
    "RECOVER_AGE_S",
    "VERSION",
    "LimitKey",
    "factor",
    "load",
    "lowered",
    "raised",
    "record",
    "recovered",
]
