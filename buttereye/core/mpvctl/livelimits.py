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
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILE = "live-limits.json"
MAX_AGE_S = 30 * 24 * 3600.0
#: what is kept of the observed rate: a little under it, so the next choice fits
MARGIN = 0.9
#: a factor never goes below this (an engine is not ruled out by one bad scene)
FLOOR = 0.2


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


def load(path: Path, *, now: float | None = None) -> dict[str, dict[str, float]]:
    """The unexpired records; an unreadable file reads as none."""
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
        if isinstance(f, (int, float)) and isinstance(when, (int, float)):
            if 0 < f <= 1 and now - when <= MAX_AGE_S:
                out[k] = {"factor": float(f), "when": float(when)}
    return out


def factor(limits: dict[str, dict[str, float]], key: LimitKey) -> float:
    """Multiplier for the estimate of ``key`` (1.0: nothing learned)."""
    rec = limits.get(key.text())
    return rec["factor"] if rec is not None else 1.0


def lowered(current: float, drop_rate: float) -> float:
    """The new factor after dropping ``drop_rate`` of the frames: what got through,
    a margin below it, never under ``FLOOR``."""
    kept = max(0.0, 1.0 - drop_rate)
    return max(FLOOR, min(current, current * kept * MARGIN))


def record(
    path: Path,
    limits: dict[str, dict[str, float]],
    key: LimitKey,
    new_factor: float,
    *,
    now: float | None = None,
) -> None:
    """Store ``new_factor`` for ``key`` (in ``limits`` and on disk). A failed write
    keeps it for this run only."""
    limits[key.text()] = {"factor": new_factor, "when": time.time() if now is None else now}
    from buttereye.core.scriptgen.generator import write_atomic

    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(path, json.dumps(limits, indent=1, sort_keys=True) + "\n")


__all__ = [
    "FILE",
    "FLOOR",
    "MARGIN",
    "MAX_AGE_S",
    "LimitKey",
    "factor",
    "load",
    "lowered",
    "record",
]
