# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""GPU faults during a benchmark candidate: new NVIDIA Xid faults in the kernel log.

Uses the doctor's scanner ``buttereye.core.doctor.gpufault.scan_xid`` (imported
lazily). ``None`` means "unknown": the scanner is not in this build, or the
journal could not be read. It is never reported as zero (M0(f): a run can finish
with the right frame rate and still have faulted the GPU).

Every Xid number counts (13 from the Fedora-ncnn build, 109 CTX SWITCH TIMEOUT seen
once on the bundled build, ...); only lines of the candidate's own processes logged
after its :func:`snapshot` are counted, so faults from earlier runs or other
programs are never blamed on it.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any, cast

GPUFAULT_MODULE = "buttereye.core.doctor.gpufault"
JOURNAL_SETTLE_S = 0.3  # let journald store the kernel lines of a fault at the very end

_log = logging.getLogger(__name__)


def _scanner() -> Callable[..., Awaitable[Any]] | None:
    try:
        mod = importlib.import_module(GPUFAULT_MODULE)
    except ImportError:
        return None
    except Exception:  # a broken module is treated as absent
        _log.exception("could not import %s", GPUFAULT_MODULE)
        return None
    fn = getattr(mod, "scan_xid", None)
    return cast(Callable[..., Awaitable[Any]], fn) if callable(fn) else None


def snapshot() -> float:
    """Start mark for :func:`count_new` (CLOCK_MONOTONIC, the journal's clock)."""
    return time.monotonic()


async def count_new(since_monotonic: float, pids: Collection[int]) -> int | None:
    """Distinct Xid faults logged since ``since_monotonic`` by ``pids``; None = unknown."""
    scan = _scanner()
    if scan is None:
        return None
    await asyncio.sleep(JOURNAL_SETTLE_S)
    try:
        result = await scan(since_monotonic=since_monotonic, pids=frozenset(pids))
    except Exception:
        _log.exception("GPU fault scan failed")
        return None
    if not getattr(result, "readable", False):
        _log.info("kernel log unreadable: %s", getattr(result, "error", None))
        return None
    count = getattr(result, "count", None)
    return count if isinstance(count, int) and not isinstance(count, bool) else None


def mark_rife_session(paths: Any, pids: Collection[int] = ()) -> None:
    """Record a RIFE-ncnn run start for the doctor's GPU-fault window (BE-1030).

    Best effort: an absent scanner module or an unwritable state folder only
    means the doctor falls back to the RIFE package's install time."""
    try:
        mod = importlib.import_module(GPUFAULT_MODULE)
    except Exception:
        return
    write = getattr(mod, "write_session_marker", None)
    if not callable(write):
        return
    try:
        write(paths, pids=tuple(pids))
    except OSError as exc:
        _log.warning("could not record the RIFE session marker: %s", exc)


__all__ = ["GPUFAULT_MODULE", "snapshot", "count_new", "mark_rife_session"]
