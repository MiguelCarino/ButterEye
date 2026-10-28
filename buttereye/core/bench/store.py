# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark history: ``Paths.bench_file`` (``$XDG_STATE_HOME/buttereye/bench.json``, §4.6).

The file is JSON ``{"schema": 2, "results": [...]}``, oldest first, at most
``MAX_RESULTS`` entries. Writes are atomic (0600 temp file in the same directory,
fsync, rename). A file that cannot be parsed is moved aside to
``bench.json.bad-<timestamp>`` before a new result is written, so nothing is
silently overwritten. Results stay local (SCOPE §2 non-goals: no telemetry).

Each result carries ``"bench": BENCH``, the version of ``bench.vpy`` that measured
it; measurements an older script took differently from playback are dropped on
read, and so from the file at the next write (once).

Schema 2 adds ``"meta"`` to each result (:class:`Meta`): what measured it (app,
script, mpv, vspipe, Vulkan driver and each engine's plugin, as fingerprints),
the power source and Low Power Mode, the thermal state, whether anything else
played or converted meanwhile (``contended``), and the sustained in-mpv rates.
A schema 1 file still reads; its results have ``Meta()`` (nothing recorded),
which ``runner.history`` treats as measured by other software. It is rewritten
as schema 2, results and all, at the next write.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.types import BackendId, BenchMeasurement, BenchRequest, BenchResult

SCHEMA = 2
#: schemas ``read`` understands (1: before ``meta``)
READABLE = frozenset({1, 2})
MAX_RESULTS = 50
#: ``bench.vpy``'s version, stored with each result. Version 2: before it the
#: script timed MVTools at pel 1 from 720p up while playback (template.vpy) runs
#: pel 2 up to 1080p, ~1.35x the CPU, so those speeds were too high to cap live
#: playback with. A result without it loses its MVTools measurements (4K ones were
#: right, but cannot be told apart without the label; a new run is cheap).
#: Version 3: the mpv pass times the same pattern as vspipe (it was ``testsrc2``),
#: with faster pans and fine detail, and leaves the pipeline fill out of its rate.
BENCH = 3
#: the first script version that timed MVTools as playback does
MVTOOLS_PEL_FIXED = 2

_log = logging.getLogger(__name__)


class HistoryCorrupt(ValueError):
    """``bench.json`` exists but is not a ButterEye benchmark history."""


@dataclass(frozen=True, slots=True)
class Meta:
    """What a result was measured with and under (schema 2)."""

    #: fingerprint per component (``runner.current_versions``); None = not
    #: recorded (a schema 1 result)
    versions: Mapping[str, str | None] | None = None
    on_battery: bool | None = None
    low_power: bool | None = None
    #: ``NSProcessInfo.thermalState`` (0 nominal .. 3 critical) when the run
    #: started, and the highest seen during it; None = not readable here
    thermal_start: int | None = None
    thermal_max: int | None = None
    #: a video played or a conversion ran during the benchmark: not used
    contended: bool = False
    #: label -> median in-mpv fps of the heat-soaked half of the sustained phase
    sustained: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Entry:
    result: BenchResult
    meta: Meta = Meta()


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


def encode_meta(meta: Meta) -> dict[str, Any]:
    return {
        "versions": dict(meta.versions) if meta.versions is not None else None,
        "on_battery": meta.on_battery,
        "low_power": meta.low_power,
        "thermal_start": meta.thermal_start,
        "thermal_max": meta.thermal_max,
        "contended": meta.contended,
        "sustained": dict(meta.sustained),
    }


def _opt(d: Mapping[str, Any], key: str, kind: type) -> Any:
    """A lenient optional field: the wrong type reads as None (meta is advisory)."""
    v = d.get(key)
    if v is None or isinstance(v, bool) is not (kind is bool) or not isinstance(v, kind):
        return None
    return v


def decode_meta(d: object) -> Meta:
    if not isinstance(d, dict):
        return Meta()
    raw = d.get("versions")
    versions: dict[str, str | None] | None = None
    if isinstance(raw, dict):
        versions = {str(k): v if isinstance(v, str) else None for k, v in raw.items()}
    sus = d.get("sustained")
    sustained = (
        {
            str(k): float(v)
            for k, v in sus.items()
            if isinstance(v, int | float) and not isinstance(v, bool)
        }
        if isinstance(sus, dict)
        else {}
    )
    return Meta(
        versions=versions,
        on_battery=_opt(d, "on_battery", bool),
        low_power=_opt(d, "low_power", bool),
        thermal_start=_opt(d, "thermal_start", int),
        thermal_max=_opt(d, "thermal_max", int),
        contended=d.get("contended") is True,
        sustained=sustained,
    )


def encode_result(r: BenchResult, meta: Meta | None = None) -> dict[str, Any]:
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
        "bench": BENCH,
        "meta": encode_meta(meta if meta is not None else Meta()),
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
    script = d.get("bench")
    current = (
        isinstance(script, int) and not isinstance(script, bool) and script >= MVTOOLS_PEL_FIXED
    )
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
        if not current and backend is BackendId.MVTOOLS:
            ms.pop()  # validated, then dropped: an old script timed it at pel 1
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


def decode_entry(d: object, schema: int = SCHEMA) -> Entry:
    result = decode_result(d)
    if schema < 2 or not isinstance(d, dict):
        return Entry(result)
    return Entry(result, decode_meta(d.get("meta")))


# ---------------------------------------------------------------- file I/O
def read(path: Path) -> tuple[BenchResult, ...]:
    """All stored results, oldest first. Missing file -> ``()``.

    Raises ``HistoryCorrupt`` for a file that is not valid history and
    ``OSError`` when it cannot be read.
    """
    return tuple(e.result for e in read_entries(path))


def read_entries(path: Path) -> tuple[Entry, ...]:
    """:func:`read` with each result's :class:`Meta`."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ()
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCorrupt(f"not JSON: {exc}") from exc
    schema = doc.get("schema") if isinstance(doc, dict) else None
    if not isinstance(schema, int) or isinstance(schema, bool) or schema not in READABLE:
        raise HistoryCorrupt("unknown schema")
    results = doc.get("results")
    if not isinstance(results, list):
        raise HistoryCorrupt("missing results")
    return tuple(decode_entry(r, schema) for r in results)


def write_atomic(path: Path, results: Sequence[BenchResult | Entry]) -> None:
    """Replace ``path`` atomically with ``results`` (0600, parent dir created 0700).
    A bare result is written with ``Meta()`` (nothing recorded)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries = [e if isinstance(e, Entry) else Entry(e) for e in results]
    data = json.dumps(
        {"schema": SCHEMA, "results": [encode_result(e.result, e.meta) for e in entries]},
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


def append(
    path: Path, result: BenchResult, *, meta: Meta | None = None, now: datetime | None = None
) -> None:
    """Append ``result`` (keeping the newest ``MAX_RESULTS``)."""
    old: list[Entry]
    try:
        old = list(read_entries(path))
    except HistoryCorrupt as exc:
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        aside = path.with_name(f"{path.name}.bad-{stamp}")
        _log.warning("benchmark history %s is unreadable (%s); moved to %s", path, exc, aside)
        os.replace(path, aside)
        old = []
    old.append(Entry(result, meta if meta is not None else Meta()))
    write_atomic(path, old[-MAX_RESULTS:])


__all__ = [
    "SCHEMA",
    "READABLE",
    "MAX_RESULTS",
    "BENCH",
    "HistoryCorrupt",
    "Meta",
    "Entry",
    "encode_meta",
    "decode_meta",
    "encode_result",
    "decode_result",
    "decode_entry",
    "read",
    "read_entries",
    "write_atomic",
    "append",
]
