# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark history: ``Paths.bench_file`` (``$XDG_STATE_HOME/buttereye/bench.json``, §4.6).

The file is JSON ``{"schema": 1, "results": [...]}``, oldest first, at most
``MAX_RESULTS`` entries. Writes are atomic (0600 temp file in the same directory,
fsync, rename). A file that cannot be parsed is moved aside to
``bench.json.bad-<timestamp>`` before a new result is written, so nothing is
silently overwritten. Results stay local (SCOPE §2 non-goals: no telemetry).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.types import BackendId, BenchMeasurement, BenchRequest, BenchResult

SCHEMA = 1
MAX_RESULTS = 50

_log = logging.getLogger(__name__)


class HistoryCorrupt(ValueError):
    """``bench.json`` exists but is not a ButterEye benchmark history."""


# ---------------------------------------------------------------- encoding
def _frac(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


def _unfrac(s: object) -> Fraction:
    if not isinstance(s, str | int):
        raise HistoryCorrupt(f"bad fraction {s!r}")
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError) as exc:
        raise HistoryCorrupt(f"bad fraction {s!r}") from exc


def encode_result(r: BenchResult) -> dict[str, Any]:
    req = r.request
    return {
        "request": {
            "width": req.width,
            "height": req.height,
            "source_fps": _frac(req.source_fps),
            "target_fps": _frac(req.target_fps) if req.target_fps is not None else None,
            "full": req.full,
            "user_file": os.fspath(req.user_file) if req.user_file is not None else None,
        },
        "measurements": [
            {
                "label": m.label,
                "backend": m.backend.value,
                "model": m.model,
                "vspipe_fps": m.vspipe_fps,
                "mpv_fps": m.mpv_fps,
                "cov": m.cov,
                "repeatable": m.repeatable,
                "startup_s": m.startup_s,
                "reload_s": m.reload_s,
                "vram_bytes": m.vram_bytes,
                "realtime": m.realtime,
                "gpu_faults": m.gpu_faults,
            }
            for m in r.measurements
        ],
        "recommended": r.recommended,
        "when": r.when.isoformat(),
        "gpu_uuid": r.gpu_uuid,
    }


def _get(d: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in d:
        raise HistoryCorrupt(f"missing {key!r}")
    v = d[key]
    if not isinstance(v, kind) or (isinstance(v, bool) and bool not in _as_tuple(kind)):
        raise HistoryCorrupt(f"{key!r} has the wrong type")
    return v


def _as_tuple(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


_NUM = (int, float)
_OPT_STR = (str, type(None))
_OPT_INT = (int, type(None))


def decode_result(d: object) -> BenchResult:
    if not isinstance(d, dict):
        raise HistoryCorrupt("result is not an object")
    rq = _get(d, "request", dict)
    target = _get(rq, "target_fps", _OPT_STR + (int,))
    user_file = _get(rq, "user_file", _OPT_STR)
    request = BenchRequest(
        width=_get(rq, "width", int),
        height=_get(rq, "height", int),
        source_fps=_unfrac(_get(rq, "source_fps", (str, int))),
        target_fps=_unfrac(target) if target is not None else None,
        full=_get(rq, "full", bool),
        user_file=Path(user_file) if user_file is not None else None,
    )
    ms: list[BenchMeasurement] = []
    for m in _get(d, "measurements", list):
        if not isinstance(m, dict):
            raise HistoryCorrupt("measurement is not an object")
        try:
            backend = BackendId(_get(m, "backend", str))
        except ValueError as exc:
            raise HistoryCorrupt("unknown backend") from exc
        ms.append(
            BenchMeasurement(
                label=_get(m, "label", str),
                backend=backend,
                model=_get(m, "model", _OPT_STR),
                vspipe_fps=float(_get(m, "vspipe_fps", _NUM)),
                mpv_fps=float(_get(m, "mpv_fps", _NUM)),
                cov=float(_get(m, "cov", _NUM)),
                repeatable=_get(m, "repeatable", bool),
                startup_s=float(_get(m, "startup_s", _NUM)),
                reload_s=float(_get(m, "reload_s", _NUM)),
                vram_bytes=_get(m, "vram_bytes", _OPT_INT),
                realtime=_get(m, "realtime", bool),
                gpu_faults=_get(m, "gpu_faults", _OPT_INT),
            )
        )
    try:
        when = datetime.fromisoformat(_get(d, "when", str))
    except ValueError as exc:
        raise HistoryCorrupt("bad timestamp") from exc
    return BenchResult(
        request=request,
        measurements=tuple(ms),
        recommended=_get(d, "recommended", _OPT_STR),
        when=when,
        gpu_uuid=_get(d, "gpu_uuid", _OPT_STR),
    )


# ---------------------------------------------------------------- file I/O
def read(path: Path) -> tuple[BenchResult, ...]:
    """All stored results, oldest first. Missing file -> ``()``.

    Raises ``HistoryCorrupt`` for a file that is not valid history and
    ``OSError`` when it cannot be read.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ()
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCorrupt(f"not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise HistoryCorrupt("unknown schema")
    results = doc.get("results")
    if not isinstance(results, list):
        raise HistoryCorrupt("missing results")
    return tuple(decode_result(r) for r in results)


def write_atomic(path: Path, results: Sequence[BenchResult]) -> None:
    """Replace ``path`` atomically with ``results`` (0600, parent dir created 0700)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    data = json.dumps(
        {"schema": SCHEMA, "results": [encode_result(r) for r in results]},
        indent=1,
        ensure_ascii=False,
    ).encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:  # mkstemp creates the file 0600
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def append(path: Path, result: BenchResult, *, now: datetime | None = None) -> None:
    """Append ``result`` (keeping the newest ``MAX_RESULTS``)."""
    try:
        old = list(read(path))
    except HistoryCorrupt as exc:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        aside = path.with_name(f"{path.name}.bad-{stamp}")
        _log.warning("benchmark history %s is unreadable (%s); moved to %s", path, exc, aside)
        os.replace(path, aside)
        old = []
    old.append(result)
    write_atomic(path, old[-MAX_RESULTS:])


__all__ = [
    "SCHEMA",
    "MAX_RESULTS",
    "HistoryCorrupt",
    "encode_result",
    "decode_result",
    "read",
    "write_atomic",
    "append",
]
