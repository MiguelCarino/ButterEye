#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Picture-quality benchmark on openly licensed footage.

The in-between frames an engine makes are compared with frames the camera (or
the renderer) really recorded: footage shot at a high rate is thinned to a lower
one, each engine rebuilds the frames that were taken out, and the rebuilt frames
are scored against the real ones with the metrics of ``truth.py``.

The footage is listed in ``public_set.json`` next to this file: where to get
it, its licence and credit line, the frames each moment uses and the sha256 of
every download and of every decoded moment. Nothing from the footage is kept in
the repository.

Steps::

    public_bench.py fetch   [--tier core|calib] [--accept-nc] [--only ID,...] [--pin]
    public_bench.py prepare [--accept-nc] [--only ID,...] [--limit N] [--pin]
    public_bench.py run     [--tier 2x|display|all] [--configs A,B] [--accept-nc]
                            [--only ID,...] [--limit N] [--json F] [--md F] [--dump DIR]
                            [--no-build] [--publish] [--compare OLD.json]
    public_bench.py check   # the manifest is well formed; what is in the cache
    public_bench.py report  RESULT.json [RESULT2.json] [--md F] [--json F] [--publish]
                            [--allow-skipped A,B] [--docs docs/benchmark.md]
                            [--compare OLD.json]
    public_bench.py compare OLD.json NEW.json [--fail-over D]
    public_bench.py select  --pin [--accept-nc] [--only ID,...]   # maintainer, once

``fetch`` downloads into ``$XDG_CACHE_HOME/buttereye-bench/downloads/<id>/``
(``BUTTEREYE_BENCH_CACHE`` moves the whole cache). Downloads resume from their
``.part`` file, try each mirror in turn and are checked against the pinned
sha256 before they are kept; a mismatch is an error. Footage under a
non-commercial licence is fetched only with ``--accept-nc``. Per-frame sources
(an EXR sequence over FTP, frames sliced out of a large y4m by HTTP Range) fetch
only the frames the moments use.

``prepare`` decodes each moment's frames, scales them to 1920x1080 with a 2x2
box (in linear light for the EXR footage), and writes one compressed ``.npz``
per moment start under ``moments/v1/``: ``y``, ``u``, ``v`` (8-bit 4:2:0
stacks) and ``index`` (the native frame numbers). The decoded frames are hashed
per moment; for H.264, HEVC and y4m a mismatch with the pinned hash is an
error (those decoders are bit-exact), for EXR only a warning (float maths may
differ by one level between numpy builds).

``--pin`` (maintainer) writes every hash that is still empty in the manifest,
and the per-scene exposure of the EXR footage, back into ``public_set.json``.

``run`` scores every named configuration (``CONFIGS``) on every prepared
moment through the playback template itself: the app's generator writes the
template and its ``user_data`` (the default profile's scene threshold, hold
fill and still skip), the moment's source frames go in as ``video_in`` at 24 fps,
and the output frames at the in-between times are compared with the real ones.
TensorRT rows use the app's own engines (fp16, half I/O), built first when
missing unless ``--no-build``. ``blend`` (a mix of the two frames around each
in-between, made here) is the floor. The 2x tier re-times every moment to
24 -> 48 fps and scores the in-betweens of its two middle pairs; the display
tier to 24 -> 24 x m and scores the m-1 in-betweens of its middle pair. Cel
scenes (2D, drawn here) are scored and reported apart. The JSON keeps every
score, the exact ``user_data`` of each configuration and what ran (machine,
versions, plugins, engines, model hashes); the home folder is written as ``~``.
``profile-2d`` is the app's 2D rules: the first engine of the session's 2D
order that runs here, held drawings re-timed, no cascade (cel scenes only).
``BUTTEREYE_BENCH_APP`` names a folder whose ``buttereye`` package is measured
instead of the checkout's (a frozen copy); a hash of the app's sources is
recorded either way.
``--dump`` writes each configuration's worst frame per group, with its error
map and a caption, for footage whose licence allows images. ``--publish``
copies the JSON to ``docs/benchmark/results-<date>.json`` (``<date>b`` and so
on when the day has one; nothing is overwritten) and refuses a partial run, a
skipped configuration or one outside ``CONFIGS``. ``--compare OLD`` ends with
what moved against an earlier result. ``run`` holds the GPU: one at a time.

``report`` prints the tables of a result file; given runs of different tiers
(the 2x tier in one GPU hold, the display tier in another) it merges them into
one result, refusing runs of another manifest, machine, software or settings,
and ``--publish`` copies the merged result as ``run --publish`` does.
``compare`` prints what moved between two result files, per table, row and
configuration (worst areas and squared error).

``select --pin`` (maintainer) checks every moment at its own stride before its
start is pinned: the sources must move (smallest step between consecutive
sources at least 1.0 in 8-bit luma), no step may be a cut (the largest at most
4 x the second largest + 2) and no native frame may repeat the one before it.
A native step more than 4 x its neighbours + 2 is also a cut.
A failing start tries half a second, a second and a second and a half either
side; a moment with no passing start is dropped and listed in the manifest's
``selection`` block. Then ``fetch --pin`` and ``prepare --pin`` pin the new
hashes.

Needs Python 3, numpy and an ffmpeg with the h264 and hevc decoders; ``run``
also needs VapourSynth and an installed ButterEye (its plugins and, for the
TensorRT rows, its TensorRT set-up). CPU work runs at nice 19 with at most
``--threads`` decoder threads (6). ``run`` re-executes itself under
``tools/memguard.py`` (a capped scope and a watchdog).
"""

from __future__ import annotations

import argparse
import copy
import ftplib
import gc
import hashlib
import io
import json
import math
import os
import platform
import shutil
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
MANIFEST = HERE / "public_set.json"
SCHEMA = 1
MOMENTS_DIR = "moments/v1"
OUT_W, OUT_H = 1920, 1080
CHUNK = 1 << 20
USER_AGENT = "buttereye-public-bench/1"
FETCH_KINDS = ("zip-member", "whole", "y4m-range", "ftp-frame")
DECODERS = {"mp4": "h264", "hevc": "hevc"}
GROUPS = ("cg", "live50", "live60", "live120", "2d")
TIERS = ("2x", "display")


class BenchError(Exception):
    """A step that cannot go on: the message says what to do."""


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- cache


def cache_root() -> Path:
    env = os.environ.get("BUTTEREYE_BENCH_CACHE")
    if env:
        return Path(env)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "buttereye-bench"


def download_dir(root: Path, footage_id: str) -> Path:
    return root / "downloads" / footage_id


def moment_path(root: Path, footage_id: str, first: int) -> Path:
    return root / MOMENTS_DIR / f"{footage_id}@{first}.npz"


# ---------------------------------------------------------------- manifest


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _line(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def dump_manifest(m: dict[str, Any]) -> str:
    """The manifest as text that diffs well: one footage field, frame or moment
    a line, keys in the order they were written."""
    out = ["{"]
    keys = list(m)
    for ki, key in enumerate(keys):
        comma = "," if ki < len(keys) - 1 else ""
        val = m[key]
        if key == "sets":
            out.append('  "sets": {')
            names = list(val)
            for si, name in enumerate(names):
                out.append(f"    {_line(name)}: {{")
                fields = list(val[name])
                for fi, f in enumerate(fields):
                    c = "," if fi < len(fields) - 1 else ""
                    out.append(f"      {_line(f)}: {_line(val[name][f])}{c}")
                out.append("    }" + ("," if si < len(names) - 1 else ""))
            out.append("  }" + comma)
        elif key in ("footage", "calib"):
            out.append(f"  {_line(key)}: [")
            for ei, entry in enumerate(val):
                out.append("    {")
                fields = list(entry)
                for fi, f in enumerate(fields):
                    c = "," if fi < len(fields) - 1 else ""
                    if f == "frames" and isinstance(entry[f], dict) and entry[f]:
                        out.append('      "frames": {')
                        frames = list(entry[f])
                        for ni, n in enumerate(frames):
                            cc = "," if ni < len(frames) - 1 else ""
                            out.append(f"        {_line(n)}: {_line(entry[f][n])}{cc}")
                        out.append("      }" + c)
                    else:
                        out.append(f"      {_line(f)}: {_line(entry[f])}{c}")
                out.append("    }" + ("," if ei < len(val) - 1 else ""))
            out.append("  ]" + comma)
        elif key == "moments":
            out.append('  "moments": [')
            for ei, entry in enumerate(val):
                out.append("    " + _line(entry) + ("," if ei < len(val) - 1 else ""))
            out.append("  ]" + comma)
        else:
            out.append(f"  {_line(key)}: {_line(val)}{comma}")
    out.append("}")
    return "\n".join(out) + "\n"


def save_manifest(m: dict[str, Any], path: Path = MANIFEST) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_manifest(m), encoding="utf-8")
    os.replace(tmp, path)


def window(moment: dict[str, Any]) -> list[int]:
    """The native frames a moment uses: 2x tier, five sources ``s`` apart and
    everything between them (4s+1 frames); display tier, four sources (3s+1)."""
    s = int(moment["s"])
    span = 4 * s if moment["tier"] == "2x" else 3 * s
    f = int(moment["f"])
    return list(range(f, f + span + 1))


def slot_positions(n_frames: int, count: int, span: int) -> list[int]:
    """Where ``count`` moments of ``span + 1`` frames sit in ``n_frames``: spread
    evenly over the middle 90 %, as truth.py's ``@autoN``."""
    last = n_frames - 1 - span
    return [
        min(last, math.floor(n_frames * (0.05 + 0.9 * (k + 0.5) / count))) for k in range(count)
    ]


def footage_by_id(m: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {f["id"]: f for f in m["footage"]}


def moments_of(m: dict[str, Any], footage_id: str) -> list[dict[str, Any]]:
    return [x for x in m["moments"] if x["footage"] == footage_id]


def needed_frames(m: dict[str, Any], footage_id: str) -> list[int]:
    frames: set[int] = set()
    for moment in moments_of(m, footage_id):
        frames.update(window(moment))
    return sorted(frames)


def slots(m: dict[str, Any], footage_id: str) -> dict[int, list[int]]:
    """Moment start -> the native frames stored for it (the union of the
    windows of every moment that starts there)."""
    out: dict[int, set[int]] = {}
    for moment in moments_of(m, footage_id):
        out.setdefault(int(moment["f"]), set()).update(window(moment))
    return {f: sorted(v) for f, v in sorted(out.items())}


def validate(m: dict[str, Any]) -> list[str]:
    """Everything wrong with the manifest, as messages (empty when fine)."""
    errs: list[str] = []
    if m.get("schema") != SCHEMA:
        errs.append(f"schema is {m.get('schema')!r}, this tool reads {SCHEMA}")
    sets = m.get("sets", {})
    need = (
        "title",
        "licence",
        "licence_url",
        "attribution",
        "changes",
        "images_ok",
        "nc",
        "group",
        "headline",
        "decoded_hash",
    )
    for name, st in sets.items():
        for k in need:
            if k not in st:
                errs.append(f"set {name}: no {k!r}")
        if st.get("group") not in GROUPS:
            errs.append(f"set {name}: group {st.get('group')!r} not in {GROUPS}")
        if st.get("nc") and (st.get("headline") or st.get("images_ok")):
            errs.append(f"set {name}: non-commercial footage is numbers only, never headline")
        if "NC" in str(st.get("licence", "")) and not st.get("nc"):
            errs.append(f"set {name}: licence {st['licence']} but nc is false")
        if st.get("decoded_hash") not in ("hard", "soft"):
            errs.append(f"set {name}: decoded_hash must be hard or soft")
    ids: set[str] = set()
    for f in [*m.get("footage", []), *m.get("calib", [])]:
        fid = f.get("id", "?")
        if fid in ids:
            errs.append(f"footage {fid}: id repeated")
        ids.add(fid)
        if f.get("set") not in sets:
            errs.append(f"footage {fid}: set {f.get('set')!r} unknown")
        if f.get("fetch") not in FETCH_KINDS:
            errs.append(f"footage {fid}: fetch {f.get('fetch')!r} not in {FETCH_KINDS}")
        if not f.get("urls"):
            errs.append(f"footage {fid}: no urls")
        if f.get("fetch") in ("y4m-range", "ftp-frame"):
            if not all("{n}" in u for u in f["urls"]) and f["fetch"] == "ftp-frame":
                errs.append(f"footage {fid}: ftp-frame urls need a {{n}} placeholder")
            if not isinstance(f.get("frames"), dict):
                errs.append(f"footage {fid}: per-frame source needs a frames map")
        for u in f.get("urls", []):
            if urllib.parse.urlsplit(u).scheme not in ("https", "http", "ftp"):
                errs.append(f"footage {fid}: url scheme of {u!r}")
    by_id = footage_by_id(m) if "footage" in m else {}
    seen: set[tuple[str, int, int, int, str]] = set()
    for i, mo in enumerate(m.get("moments", [])):
        where = f"moment {i} ({mo.get('footage')}@{mo.get('f')})"
        fp = by_id.get(mo.get("footage", ""))
        if fp is None:
            errs.append(f"{where}: unknown footage")
            continue
        if mo.get("tier") not in TIERS:
            errs.append(f"{where}: tier {mo.get('tier')!r}")
            continue
        s, mm = int(mo.get("s", 0)), int(mo.get("m", 0))
        if s < 1 or mm < 2 or s % mm:
            errs.append(f"{where}: multiplier m={mm} must divide stride s={s}")
        if mo["tier"] == "2x" and mm != 2:
            errs.append(f"{where}: the 2x tier has m=2")
        if mo["tier"] == "display" and mm != s:
            errs.append(f"{where}: the display tier has m=s")
        key = (mo["footage"], int(mo["f"]), s, mm, mo["tier"])
        if key in seen:
            errs.append(f"{where}: repeated")
        seen.add(key)
        win = window(mo)
        if win[0] < 0 or win[-1] >= int(fp.get("frames_total", 0)):
            errs.append(f"{where}: window {win[0]}..{win[-1]} outside the footage")
    for fid, fp in by_id.items():
        if fp.get("fetch") in ("y4m-range", "ftp-frame") and isinstance(fp.get("frames"), dict):
            extra = set(map(int, fp["frames"])) - set(needed_frames(m, fid))
            if extra:
                errs.append(f"footage {fid}: frames pinned that no moment uses: {sorted(extra)}")
    for cap in m.get("caps", []):
        # at most ``max`` moments of a footage and tier may start inside a span
        # (the end credits of a film, say): one kind of picture must not dominate
        lo, hi = int(cap["from"]), int(cap.get("to", 1 << 62))
        inside = [
            int(mo["f"])
            for mo in m.get("moments", [])
            if mo.get("footage") == cap["footage"]
            and mo.get("tier") == cap["tier"]
            and lo <= int(mo["f"]) < hi
        ]
        if len(inside) > int(cap["max"]):
            errs.append(
                f"footage {cap['footage']} {cap['tier']}: {len(inside)} moments start in "
                f"{cap.get('what', 'the capped span')} ({sorted(inside)}), at most {cap['max']}"
            )
    return errs


# ---------------------------------------------------------------- hashing


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while b := fh.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def planes_sha256(np: Any, y: Any, u: Any, v: Any) -> str:
    h = hashlib.sha256()
    for p in (y, u, v):
        h.update(np.ascontiguousarray(p, dtype=np.uint8).tobytes())
    return h.hexdigest()


# ---------------------------------------------------------------- fetch


class Mismatch(BenchError):
    pass


def _check_done(part: Path, dest: Path, size: int | None, sha: str | None) -> str:
    """Check a finished ``.part``; rename it to ``dest`` and return its sha256."""
    got_size = part.stat().st_size
    if size is not None and got_size != size:
        if got_size > size:
            part.unlink()  # cannot be resumed
        raise BenchError(f"{dest.name}: {got_size} bytes, expected {size}")
    digest = sha256_file(part)
    if sha is not None and digest != sha:
        part.unlink()
        raise Mismatch(f"{dest.name}: sha256 {digest} does not match the pinned {sha}")
    os.replace(part, dest)
    return digest


def http_get(
    url: str,
    part: Path,
    *,
    offset: int = 0,
    length: int | None = None,
    timeout: float = 60.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    """Append to ``part`` the bytes of ``url`` from ``offset`` (+ what ``part``
    already holds) to ``offset + length`` (or the end). Starts over when the
    server ignores the Range."""
    have = part.stat().st_size if part.exists() else 0
    if length is not None and have >= length:
        return
    start = offset + have
    end = "" if length is None else str(offset + length - 1)
    headers = {"User-Agent": USER_AGENT}
    if start > 0 or length is not None:
        headers["Range"] = f"bytes={start}-{end}"
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = opener(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and length is None and have > 0:
            return  # already complete
        raise
    with resp:
        status = getattr(resp, "status", 200)
        mode = "ab"
        if "Range" in headers and status != 206:
            if offset or length is not None:
                raise BenchError(f"{url}: the server does not serve byte ranges")
            mode = "wb"  # whole file again
        elif status == 206:
            cr = resp.headers.get("Content-Range", "")
            if not cr.startswith(f"bytes {start}-"):
                raise BenchError(f"{url}: asked for bytes {start}-, got {cr!r}")
        with part.open(mode) as fh:
            while b := resp.read(CHUNK):
                fh.write(b)


def ftp_get(url: str, part: Path, conn: dict[str, Any], timeout: float = 60.0) -> int:
    """Append to ``part`` the rest of an FTP file (REST resumes); returns the
    server's size. ``conn`` keeps one connection per host between calls."""
    sp = urllib.parse.urlsplit(url)
    host = sp.hostname or ""
    ftp = conn.get(host)
    if ftp is None:
        ftp = ftplib.FTP(host, timeout=timeout)
        ftp.login()
        ftp.voidcmd("TYPE I")
        conn[host] = ftp
    path = urllib.parse.unquote(sp.path)
    size = ftp.size(path)
    if size is None:
        raise BenchError(f"{url}: the server gave no size")
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have < size:
        with part.open("ab") as fh:
            ftp.retrbinary(f"RETR {path}", fh.write, blocksize=CHUNK, rest=have or None)
    return int(size)


def _close_ftp(conn: dict[str, Any]) -> None:
    for ftp in conn.values():
        try:
            ftp.quit()
        except OSError, EOFError, ftplib.Error:
            ftp.close()
    conn.clear()


def fetch_file(
    urls: Sequence[str],
    dest: Path,
    *,
    size: int | None,
    sha: str | None,
    offset: int = 0,
    length: int | None = None,
    ftp_conn: dict[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
    retries: int = 3,
) -> str:
    """Fetch one file (or one byte range of ``urls``) into ``dest``, trying each
    mirror in turn; returns its sha256. A kept ``dest`` is re-hashed, not
    downloaded again."""
    if dest.exists():
        digest = sha256_file(dest)
        if sha is None or digest == sha:
            return digest
        log(f"  {dest.name}: kept copy does not match the pinned hash; fetching again")
        dest.unlink()
    part = dest.with_name(dest.name + ".part")
    errors: list[str] = []
    for url in urls:
        for attempt in range(retries):
            try:
                if url.startswith("ftp://"):
                    got = ftp_get(url, part, ftp_conn if ftp_conn is not None else {})
                    size = size if size is not None else got
                    if got != size:
                        raise BenchError(f"{url}: server size {got}, expected {size}")
                else:
                    http_get(url, part, offset=offset, length=length, opener=opener)
                return _check_done(part, dest, size if length is None else length, sha)
            except Mismatch as exc:
                errors.append(f"{url}: {exc}")
                break  # this mirror serves other bytes; try the next one
            except (OSError, EOFError, ftplib.Error, urllib.error.URLError, BenchError) as exc:
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
                if ftp_conn is not None:
                    _close_ftp(ftp_conn)
                if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 404, 410):
                    break
                time.sleep(min(30, 2**attempt))
    raise BenchError(f"{dest.name}: every source failed:\n  " + "\n  ".join(errors[-6:]))


def _licence_notice(name: str, st: dict[str, Any]) -> str:
    return (
        f"{name}: {st['title']}\n  licence: {st['licence']} ({st['licence_url']})\n"
        f"  credit: {st['attribution']}"
    )


def fetch_footage(
    m: dict[str, Any],
    fp: dict[str, Any],
    root: Path,
    *,
    pin: bool,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, Any]:
    """Fetch one footage entry; returns a summary. Fills empty hashes in ``fp``
    (and so in ``m``) when ``pin`` is set."""
    folder = download_dir(root, fp["id"])
    folder.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    kind = fp["fetch"]
    summary: dict[str, Any] = {"id": fp["id"], "bytes": 0, "files": 0, "unpinned": 0}
    if kind in ("zip-member", "whole"):
        name = Path(urllib.parse.urlsplit(fp["urls"][0]).path).name
        dest = folder / name
        digest = fetch_file(
            fp["urls"], dest, size=fp.get("size"), sha=fp.get("sha256"), opener=opener
        )
        if fp.get("sha256") is None:
            summary["unpinned"] += 1
            if pin:
                fp["sha256"] = digest
        if fp.get("size") is None and pin:
            fp["size"] = dest.stat().st_size
        summary["bytes"] += dest.stat().st_size
        summary["files"] += 1
    elif kind == "y4m-range":
        hdr, fb = int(fp["header_bytes"]), int(fp["frame_bytes"])
        frames: dict[str, Any] = fp.setdefault("frames", {})
        for n in needed_frames(m, fp["id"]):
            dest = folder / f"frame_{n:06d}.y4mframe"
            entry = frames.get(str(n)) or {}
            digest = fetch_file(
                fp["urls"],
                dest,
                size=fb,
                sha=entry.get("sha256"),
                offset=hdr + n * fb,
                length=fb,
                opener=opener,
            )
            if entry.get("sha256") is None:
                summary["unpinned"] += 1
                if pin:
                    frames[str(n)] = {"sha256": digest}
            summary["bytes"] += fb
            summary["files"] += 1
        fp["frames"] = {k: frames[k] for k in sorted(frames, key=int)}
    elif kind == "ftp-frame":
        frames = fp.setdefault("frames", {})
        base = int(fp.get("frame_base", 0))
        conn: dict[str, Any] = {}
        publisher = _publisher_hashes(fp, folder, conn)
        try:
            for n in needed_frames(m, fp["id"]):
                urls = [u.format(n=base + n) for u in fp["urls"]]
                dest = folder / Path(urllib.parse.urlsplit(urls[0]).path).name
                entry = frames.get(str(n)) or {}
                digest = fetch_file(
                    urls, dest, size=entry.get("size"), sha=entry.get("sha256"), ftp_conn=conn
                )
                want = publisher.get(dest.name)
                if want is not None and want != digest:
                    dest.unlink()
                    raise BenchError(f"{dest.name}: sha256 differs from the publisher's list")
                if entry.get("sha256") is None:
                    summary["unpinned"] += 1
                    if pin:
                        frames[str(n)] = {"size": dest.stat().st_size, "sha256": digest}
                summary["bytes"] += dest.stat().st_size
                summary["files"] += 1
        finally:
            _close_ftp(conn)
        fp["frames"] = {k: frames[k] for k in sorted(frames, key=int)}
    summary["seconds"] = round(time.monotonic() - t0, 1)
    return summary


def _publisher_hashes(fp: dict[str, Any], folder: Path, conn: dict[str, Any]) -> dict[str, str]:
    """The publisher's own sha256 list for a per-frame source, when it has one
    (checked against every frame; the pinned hashes stay what is enforced)."""
    url = fp.get("publisher_sha256")
    if not url:
        return {}
    dest = folder / "publisher.sha256"
    if not dest.exists():
        part = dest.with_name(dest.name + ".part")
        if part.exists():
            part.unlink()
        try:
            if url.startswith("ftp://"):
                ftp_get(url, part, conn)
            else:
                http_get(url, part)
            os.replace(part, dest)
        except (OSError, EOFError, ftplib.Error, urllib.error.URLError, BenchError) as exc:
            log(f"  {fp['id']}: publisher hash list not fetched ({exc}); pinned hashes only")
            _close_ftp(conn)
            return {}
    out: dict[str, str] = {}
    for line in dest.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            out[Path(parts[1]).name] = parts[0].lower()
    return out


def _select(m: dict[str, Any], only: str | None, accept_nc: bool, tier: str) -> list[Any]:
    sets = m["sets"]
    pool = m["calib"] if tier == "calib" else m["footage"]
    wanted = set(only.split(",")) if only else None
    out = []
    for fp in pool:
        if wanted is not None and fp["id"] not in wanted and fp["set"] not in wanted:
            continue
        st = sets[fp["set"]]
        if st["nc"] and not accept_nc:
            continue
        out.append(fp)
    return out


def _nc_skipped(m: dict[str, Any], only: str | None, accept_nc: bool) -> list[str]:
    if accept_nc:
        return []
    wanted = set(only.split(",")) if only else None
    names = []
    for name, st in m["sets"].items():
        if st["nc"] and (
            wanted is None
            or name in wanted
            or any(f["set"] == name and f["id"] in wanted for f in m["footage"])
        ):
            names.append(name)
    return names


def cmd_fetch(args: argparse.Namespace) -> int:
    m = load_manifest(args.manifest)
    errs = validate(m)
    if errs:
        raise BenchError("manifest: " + "; ".join(errs))
    root = cache_root()
    for name in _nc_skipped(m, args.only, args.accept_nc):
        log(_licence_notice(name, m["sets"][name]))
        log("  skipped: non-commercial licence; pass --accept-nc to fetch it for your own use")
    for name in sorted({fp["set"] for fp in _select(m, args.only, args.accept_nc, args.tier)}):
        st = m["sets"][name]
        if st["nc"]:
            log(_licence_notice(name, st))
            log("  fetched under --accept-nc: numbers only, never redistributed")
    failed: list[str] = []
    total = 0
    t0 = time.monotonic()
    for fp in _select(m, args.only, args.accept_nc, args.tier):
        log(f"{fp['id']}: fetching ({fp['fetch']})")
        try:
            s = fetch_footage(m, fp, root, pin=args.pin)
        except BenchError as exc:
            log(f"{fp['id']}: FAILED: {exc}")
            failed.append(fp["id"])
            continue
        finally:
            if args.pin:
                save_manifest(m, args.manifest)
        total += s["bytes"]
        note = f", {s['unpinned']} unpinned" if s["unpinned"] and not args.pin else ""
        pinned = f", {s['unpinned']} hashes pinned" if s["unpinned"] and args.pin else ""
        log(
            f"{fp['id']}: {s['files']} files, {s['bytes'] / 1e6:.1f} MB, "
            f"{s['seconds']} s{note}{pinned}"
        )
    log(f"{total / 1e9:.2f} GB in {root / 'downloads'}, {time.monotonic() - t0:.0f} s")
    if failed:
        log("failed: " + ", ".join(failed))
        return 1
    return 0


# ---------------------------------------------------------------- pixels


def box8(np: Any, a: Any) -> Any:
    """2x2 box of an 8-bit plane, rounded half up (deterministic)."""
    h, w = a.shape[0] // 2, a.shape[1] // 2
    s = a[: 2 * h, : 2 * w].astype(np.uint16).reshape(h, 2, w, 2).sum(axis=(1, 3))
    return ((s + 2) >> 2).astype(np.uint8)


def box10to8(np: Any, a: Any) -> Any:
    """2x2 box of a 10-bit plane straight to 8 bits: one rounding, (sum+8)>>4."""
    h, w = a.shape[0] // 2, a.shape[1] // 2
    s = a[: 2 * h, : 2 * w].astype(np.uint32).reshape(h, 2, w, 2).sum(axis=(1, 3))
    return np.minimum((s + 8) >> 4, 255).astype(np.uint8)


def crop_centre(a: Any, w: int, h: int) -> Any:
    y0, x0 = (a.shape[0] - h) // 2, (a.shape[1] - w) // 2
    if y0 < 0 or x0 < 0:
        raise BenchError(f"frame {a.shape[1]}x{a.shape[0]} is smaller than {w}x{h}")
    return a[y0 : y0 + h, x0 : x0 + w]


def to_1080_8bit(np: Any, y: Any, u: Any, v: Any, ten_bit: bool) -> tuple[Any, Any, Any]:
    box = box10to8 if ten_bit else box8
    return (
        crop_centre(box(np, y), OUT_W, OUT_H),
        crop_centre(box(np, u), OUT_W // 2, OUT_H // 2),
        crop_centre(box(np, v), OUT_W // 2, OUT_H // 2),
    )


def acescc_decode(np: Any, v: Any) -> Any:
    """ACEScc code values to linear (ACES S-2014-003), in float32."""
    v = np.asarray(v, dtype=np.float32)
    lin = np.exp2(v * np.float32(17.52) - np.float32(9.72))
    low = v < np.float32((9.72 - 15.0) / 17.52)
    lin = np.where(low, (lin - np.float32(2.0**-16)) * np.float32(2.0), lin)
    return np.minimum(lin, np.float32(65504.0))


def tone(np: Any, x: Any) -> Any:
    """The fixed tone curve: extended Reinhard with white at 4 (0 -> 0, 4 -> 1)."""
    x = np.maximum(x, 0)
    return np.minimum(x * (1 + x / 16) / (1 + x), 1)


def oetf709(np: Any, x: Any) -> Any:
    x = np.clip(x, 0, 1)
    return np.where(x < 0.018, x * 4.5, np.power(x, 0.45) * 1.099 - 0.099).astype(x.dtype)


def rgb_to_ycbcr709(np: Any, r: Any, g: Any, b: Any) -> tuple[Any, Any, Any]:
    """Non-linear R'G'B' in 0..1 to limited-range 8-bit BT.709 Y'CbCr 4:2:0."""
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    cb = (b - y) / 1.8556
    cr = (r - y) / 1.5748

    def sub(c: Any) -> Any:
        h, w = c.shape[0] // 2, c.shape[1] // 2
        return c[: 2 * h, : 2 * w].reshape(h, 2, w, 2).mean(axis=(1, 3))

    def q(x: Any, scale: float, off: float) -> Any:
        return np.clip(np.rint(off + scale * x), 0, 255).astype(np.uint8)

    return q(y, 219.0, 16.0), q(sub(cb), 224.0, 128.0), q(sub(cr), 224.0, 128.0)


def luma_lin(r: Any, g: Any, b: Any) -> Any:
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def box_float(np: Any, a: Any) -> Any:
    h, w = a.shape[0] // 2, a.shape[1] // 2
    return a[: 2 * h, : 2 * w].reshape(h, 2, w, 2).mean(axis=(1, 3))


def exr_to_yuv(np: Any, rgb_cc: dict[str, Any], exposure: float) -> tuple[Any, Any, Any]:
    """An SVT EXR (ACEScc-encoded, camera primaries) to 1080p 8-bit Y'CbCr:
    ACEScc decode, 2x2 box in linear light, exposure, tone curve, BT.709 OETF."""
    out = []
    for c in ("R", "G", "B"):
        lin = box_float(np, acescc_decode(np, rgb_cc[c]))
        out.append(oetf709(np, tone(np, lin * np.float32(exposure))))
    r, g, b = (crop_centre(c, OUT_W, OUT_H) for c in out)
    return rgb_to_ycbcr709(np, r, g, b)


TONE_WHITE = 4.0  # linear light the tone curve maps to full white
CLIP_SHARE = 0.02  # the share of the picture allowed to reach white
MAX_PULL = 0.5  # highlight protection lowers the median-rule gain by one stop at most


def exr_exposure(np: Any, rgb_cc: dict[str, Any]) -> float:
    """The scene's gain, to 4 digits: the one that puts the frame's median
    linear luma at 0.18, lowered (by one stop at most) until no more than 2 %
    of the picture's luma reaches the tone curve's white."""
    r, g, b = (box_float(np, acescc_decode(np, rgb_cc[c])) for c in ("R", "G", "B"))
    lum = luma_lin(r, g, b)
    med = float(np.median(lum))
    if med <= 0:
        raise BenchError("EXR frame has no light to expose on")
    gain = 0.18 / med
    high = float(np.percentile(lum, 100.0 * (1.0 - CLIP_SHARE)))
    if high * gain > TONE_WHITE:
        gain = max(gain * MAX_PULL, TONE_WHITE / high)
    return float(f"{gain:.4g}")


# ---------------------------------------------------------------- EXR


def _exr_attrs(buf: bytes) -> tuple[dict[str, tuple[str, bytes]], int]:
    magic, version = struct.unpack_from("<ii", buf, 0)
    if magic != 20000630:
        raise BenchError("not an OpenEXR file")
    if version & 0x200 or version & 0x800 or version & 0x1000:
        raise BenchError("tiled, deep or multi-part EXR is not supported")
    pos = 8
    attrs: dict[str, tuple[str, bytes]] = {}
    while True:
        end = buf.index(b"\0", pos)
        name = buf[pos:end].decode()
        pos = end + 1
        if not name:
            return attrs, pos
        end = buf.index(b"\0", pos)
        typ = buf[pos:end].decode()
        pos = end + 1
        (size,) = struct.unpack_from("<i", buf, pos)
        pos += 4
        attrs[name] = (typ, buf[pos : pos + size])
        pos += size


def _exr_channels(raw: bytes) -> list[tuple[str, int]]:
    out, pos = [], 0
    while raw[pos] != 0:
        end = raw.index(b"\0", pos)
        name = raw[pos:end].decode()
        pixel_type, _plin, xs, ys = struct.unpack_from("<iB3xii", raw, end + 1)
        if xs != 1 or ys != 1:
            raise BenchError("subsampled EXR channels are not supported")
        out.append((name, pixel_type))
        pos = end + 1 + 16
    return out


def _unpredict(np: Any, raw: bytes) -> bytes:
    """Undo OpenEXR's ZIP predictor and byte interleaving."""
    d = np.frombuffer(raw, dtype=np.uint8).copy()
    d[1:] -= np.uint8(128)
    t = np.cumsum(d, dtype=np.uint8)
    half = (t.size + 1) // 2
    out = np.empty_like(t)
    out[0::2] = t[:half]
    out[1::2] = t[half:]
    return bytes(out.tobytes())


def read_exr(np: Any, path: Path) -> dict[str, Any]:
    """The channels of a scanline OpenEXR (no, ZIPS or ZIP compression) as
    float32 arrays."""
    buf = path.read_bytes()
    attrs, pos = _exr_attrs(buf)
    chans = _exr_channels(attrs["channels"][1])
    (comp,) = struct.unpack("<B", attrs["compression"][1][:1])
    if comp not in (0, 2, 3):
        raise BenchError(f"EXR compression {comp} is not supported")
    x0, y0, x1, y1 = struct.unpack("<iiii", attrs["dataWindow"][1])
    w, h = x1 - x0 + 1, y1 - y0 + 1
    lines = {0: 1, 2: 1, 3: 16}[comp]
    nchunks = (h + lines - 1) // lines
    offsets = struct.unpack_from(f"<{nchunks}Q", buf, pos)
    sizes = [{1: 2, 0: 4, 2: 4}[pt] for _, pt in chans]
    dtypes = [{1: "<f2", 0: "<u4", 2: "<f4"}[pt] for _, pt in chans]
    line_bytes = w * sum(sizes)
    out = {name: np.empty((h, w), dtype=np.float32) for name, _ in chans}
    for off in offsets:
        cy, n = struct.unpack_from("<ii", buf, off)
        data = buf[off + 8 : off + 8 + n]
        rows = min(lines, y1 + 1 - cy)
        expect = rows * line_bytes
        if comp != 0 and n < expect:
            data = _unpredict(np, zlib.decompress(data))
        if len(data) != expect:
            raise BenchError(f"{path.name}: chunk at line {cy} has {len(data)} bytes")
        p = 0
        for r in range(rows):
            for (name, _), sz, dt in zip(chans, sizes, dtypes, strict=True):
                row = np.frombuffer(data, dtype=dt, count=w, offset=p)
                out[name][cy - y0 + r] = row.astype(np.float32)
                p += w * sz
    return out


# ---------------------------------------------------------------- decode


def check_decoders(needed: set[str], ffmpeg: str = "ffmpeg") -> None:
    if not needed:
        return
    if shutil.which(ffmpeg) is None:
        raise BenchError("ffmpeg is not installed; prepare needs it for " + ", ".join(needed))
    text = subprocess.run(
        [ffmpeg, "-hide_banner", "-decoders"], capture_output=True, text=True, check=False
    ).stdout
    have = {ln.split()[1] for ln in text.splitlines() if len(ln.split()) > 1 and ln[:1] == " "}
    missing = sorted(needed - have)
    if missing:
        raise BenchError(
            f"this ffmpeg has no {', '.join(missing)} decoder; install a build that has it "
            "(stock distribution builds sometimes leave H.264 and HEVC out)"
        )


def select_filter(frames: Sequence[int]) -> str:
    """An ffmpeg ``select`` keeping exactly ``frames`` (ascending), as ranges."""
    ranges: list[list[int]] = []
    for n in frames:
        if ranges and n == ranges[-1][1] + 1:
            ranges[-1][1] = n
        else:
            ranges.append([n, n])
    return "select='" + "+".join(f"between(n,{a},{b})" for a, b in ranges) + "'"


def ffmpeg_frames(
    np: Any,
    path: Path,
    frames: Sequence[int],
    width: int,
    height: int,
    *,
    input_args: Sequence[str] = (),
    threads: int = 6,
    ffmpeg: str = "ffmpeg",
) -> Iterator[tuple[int, Any, Any, Any]]:
    """Decode ``path`` once, in order, keeping only ``frames``: no seeking, so no
    off-by-one around B-frames. Yields (frame, y, u, v) at full size, 8-bit 4:2:0."""
    frames = sorted(frames)
    cmd = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-threads",
        str(threads),
        *input_args,
        "-i",
        os.fspath(path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-filter_threads",
        "1",
        "-vf",
        select_filter(frames),
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(frames)),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "yuv420p",
        "-",
    ]
    ysz, csz = width * height, (width // 2) * (height // 2)
    fsz = ysz + 2 * csz
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stream = cast(io.BufferedReader, proc.stdout)
    got = 0
    try:
        for n in frames:
            buf = bytearray(fsz)
            view = memoryview(buf)
            p = 0
            while p < fsz:
                k = stream.readinto(view[p:])
                if not k:
                    break
                p += k
            if p < fsz:
                break
            a = np.frombuffer(buf, dtype=np.uint8)
            y = a[:ysz].reshape(height, width)
            u = a[ysz : ysz + csz].reshape(height // 2, width // 2)
            v = a[ysz + csz :].reshape(height // 2, width // 2)
            got += 1
            yield n, y, u, v
    finally:
        stream.close()
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        rc = proc.wait()
    if got < len(frames):
        raise BenchError(
            f"{path.name}: ffmpeg gave {got} of {len(frames)} frames (exit {rc}): {err[-400:]}"
        )


def y4m_frame(np: Any, path: Path, width: int, height: int) -> tuple[Any, Any, Any]:
    """One frame cut from a 10-bit 4:2:0 y4m (``FRAME\\n`` + planes)."""
    buf = path.read_bytes()
    if buf[:6] != b"FRAME\n":
        raise BenchError(f"{path.name}: does not start with FRAME (wrong offset?)")
    a = np.frombuffer(buf, dtype="<u2", offset=6)
    ysz, csz = width * height, (width // 2) * (height // 2)
    if a.size != ysz + 2 * csz:
        raise BenchError(f"{path.name}: {a.size} samples, expected {ysz + 2 * csz}")
    if int(a.max()) > 1023:
        raise BenchError(f"{path.name}: samples above 10 bits")
    return (
        a[:ysz].reshape(height, width),
        a[ysz : ysz + csz].reshape(height // 2, width // 2),
        a[ysz + csz :].reshape(height // 2, width // 2),
    )


def _zip_member(fp: dict[str, Any], folder: Path) -> Path:
    """The video inside a zip download, streamed out once (CRC checked)."""
    zpath = folder / Path(urllib.parse.urlsplit(fp["urls"][0]).path).name
    if not zpath.exists():
        raise BenchError(f"{fp['id']}: {zpath.name} is not downloaded; run fetch first")
    out = folder / Path(fp["member"]).name
    with zipfile.ZipFile(zpath) as zf:
        info = zf.getinfo(fp["member"])
        if out.exists() and out.stat().st_size == info.file_size:
            return out
        log(f"{fp['id']}: unpacking {info.filename} ({info.file_size / 1e6:.0f} MB)")
        tmp = out.with_name(out.name + ".part")
        with zf.open(info) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        os.replace(tmp, out)
    return out


def decode_footage(
    np: Any,
    fp: dict[str, Any],
    frames: Sequence[int],
    root: Path,
    *,
    threads: int,
    exposure: float | None,
) -> Iterator[tuple[int, Any, Any, Any]]:
    """Yield (frame, y, u, v) at 1920x1080 8-bit 4:2:0 for ``frames`` of ``fp``."""
    folder = download_dir(root, fp["id"])
    prep = fp["prepare"]
    w, h = int(fp["width"]), int(fp["height"])
    kind = prep["decode"]
    if kind in ("mp4", "hevc"):
        path = (
            _zip_member(fp, folder)
            if fp["fetch"] == "zip-member"
            else folder / Path(urllib.parse.urlsplit(fp["urls"][0]).path).name
        )
        if not path.exists():
            raise BenchError(f"{fp['id']}: {path.name} is not downloaded; run fetch first")
        args = list(prep.get("input_args", []))
        for n, y, u, v in ffmpeg_frames(np, path, frames, w, h, input_args=args, threads=threads):
            yield (n, *to_1080_8bit(np, y, u, v, ten_bit=False))
    elif kind == "y4m":
        for n in frames:
            path = folder / f"frame_{n:06d}.y4mframe"
            if not path.exists():
                raise BenchError(f"{fp['id']}: frame {n} is not downloaded; run fetch first")
            y, u, v = y4m_frame(np, path, w, h)
            yield (n, *to_1080_8bit(np, y, u, v, ten_bit=True))
    elif kind == "exr":
        if exposure is None:
            raise BenchError(f"{fp['id']}: no exposure")
        base = int(fp.get("frame_base", 0))
        for n in frames:
            name = Path(urllib.parse.urlsplit(fp["urls"][0].format(n=base + n)).path).name
            path = folder / name
            if not path.exists():
                raise BenchError(f"{fp['id']}: frame {n} is not downloaded; run fetch first")
            ch = read_exr(np, path)
            if ch["R"].shape != (h, w):
                raise BenchError(f"{path.name}: {ch['R'].shape[::-1]}, expected {w}x{h}")
            yield (n, *exr_to_yuv(np, ch, exposure))
    else:
        raise BenchError(f"{fp['id']}: unknown decode {kind!r}")


def _first_exr(np: Any, fp: dict[str, Any], root: Path, first: int) -> dict[str, Any]:
    base = int(fp.get("frame_base", 0))
    name = Path(urllib.parse.urlsplit(fp["urls"][0].format(n=base + first)).path).name
    path = download_dir(root, fp["id"]) / name
    if not path.exists():
        raise BenchError(f"{fp['id']}: frame {first} is not downloaded; run fetch first")
    return read_exr(np, path)


def prepare_footage(
    np: Any,
    m: dict[str, Any],
    fp: dict[str, Any],
    root: Path,
    *,
    limit: int | None,
    threads: int,
    pin: bool,
) -> dict[str, Any]:
    st = m["sets"][fp["set"]]
    want = slots(m, fp["id"])
    if limit is not None:
        want = dict(list(want.items())[:limit])
    summary: dict[str, Any] = {
        "id": fp["id"],
        "moments": 0,
        "written": 0,
        "frames": 0,
        "warnings": [],
        "pinned": 0,
    }
    if not want:
        return summary
    t0 = time.monotonic()
    exposure = None
    if fp["prepare"]["decode"] == "exr":
        exposure = fp["prepare"].get("exposure")
        if exposure is None:
            exposure = exr_exposure(np, _first_exr(np, fp, root, next(iter(want))))
            state = "pinned" if pin else "not pinned"
            log(f"{fp['id']}: exposure {exposure} (median rule, highlights kept), {state}")
            if pin:
                fp["prepare"]["exposure"] = exposure
                summary["pinned"] += 1
    out_dir = root / MOMENTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    recipe = dict(fp["prepare"], exposure=exposure) if exposure is not None else fp["prepare"]
    todo: dict[int, list[int]] = {}
    for first, frames in want.items():
        path = moment_path(root, fp["id"], first)
        if path.exists():
            with np.load(path) as d:
                same = json.loads(str(d["meta"])).get("prepare") == recipe
                if same and [int(x) for x in d["index"]] == frames:
                    continue
        todo[first] = frames
    stacks: dict[int, dict[int, tuple[Any, Any, Any]]] = {f: {} for f in todo}
    owner: dict[int, list[int]] = {}
    for first, frames in todo.items():
        for n in frames:
            owner.setdefault(n, []).append(first)
    if todo:
        for n, y, u, v in decode_footage(
            np, fp, sorted(owner), root, threads=threads, exposure=exposure
        ):
            for first in owner[n]:
                stacks[first][n] = (y.copy(), u.copy(), v.copy())
            for first in owner[n]:
                if len(stacks[first]) == len(todo[first]):
                    _write_slot(np, root, fp, first, todo[first], stacks.pop(first), recipe)
                    summary["written"] += 1
                    summary["frames"] += len(todo[first])
    # hash every moment from what is on disk
    for mo in moments_of(m, fp["id"]):
        if int(mo["f"]) not in want:
            continue
        with np.load(moment_path(root, fp["id"], int(mo["f"]))) as d:
            index = [int(x) for x in d["index"]]
            sel = [index.index(n) for n in window(mo)]
            digest = planes_sha256(np, d["y"][sel], d["u"][sel], d["v"][sel])
        summary["moments"] += 1
        pinned = mo.get("decoded_sha256")
        if pinned is None:
            if pin:
                mo["decoded_sha256"] = digest
                summary["pinned"] += 1
        elif pinned != digest:
            msg = (
                f"{fp['id']}@{mo['f']} s{mo['s']} {mo['tier']}: decoded frames differ "
                f"from the pinned hash"
            )
            if st["decoded_hash"] == "hard":
                moment_path(root, fp["id"], int(mo["f"])).unlink()
                raise BenchError(msg + " (wrong frame index or a broken decoder)")
            summary["warnings"].append(msg)
    summary["seconds"] = round(time.monotonic() - t0, 1)
    return summary


def _write_slot(
    np: Any,
    root: Path,
    fp: dict[str, Any],
    first: int,
    frames: list[int],
    got: dict[int, tuple[Any, Any, Any]],
    recipe: dict[str, Any],
) -> None:
    path = moment_path(root, fp["id"], first)
    tmp = path.with_name(path.stem + ".tmp.npz")
    y = np.stack([got[n][0] for n in frames])
    u = np.stack([got[n][1] for n in frames])
    v = np.stack([got[n][2] for n in frames])
    meta = json.dumps({"footage": fp["id"], "set": fp["set"], "first": first, "prepare": recipe})
    np.savez_compressed(
        tmp, y=y, u=u, v=v, index=np.array(frames, dtype=np.int64), meta=np.array(meta)
    )
    os.replace(tmp, path)


def cmd_prepare(args: argparse.Namespace) -> int:
    import numpy as np

    m = load_manifest(args.manifest)
    errs = validate(m)
    if errs:
        raise BenchError("manifest: " + "; ".join(errs))
    if args.nice:
        os.nice(args.nice)
    root = cache_root()
    chosen = _select(m, args.only, args.accept_nc, "core")
    for name in _nc_skipped(m, args.only, args.accept_nc):
        log(f"{name}: skipped (non-commercial licence; --accept-nc)")
    check_decoders(
        {DECODERS[fp["prepare"]["decode"]] for fp in chosen if fp["prepare"]["decode"] in DECODERS}
    )
    failed: list[str] = []
    t0 = time.monotonic()
    for fp in chosen:
        try:
            s = prepare_footage(
                np, m, fp, root, limit=args.limit, threads=args.threads, pin=args.pin
            )
        except BenchError as exc:
            log(f"{fp['id']}: FAILED: {exc}")
            failed.append(fp["id"])
            continue
        finally:
            if args.pin:
                save_manifest(m, args.manifest)
        for w in s["warnings"]:
            log(f"  warning: {w}")
        log(
            f"{fp['id']}: {s['moments']} moments checked, {s['written']} files written "
            f"({s['frames']} frames), {s['pinned']} pinned, {s.get('seconds', 0)} s"
        )
    log(f"prepare took {time.monotonic() - t0:.0f} s; moments in {root / MOMENTS_DIR}")
    if failed:
        log("failed: " + ", ".join(failed))
        return 1
    return 0


# ---------------------------------------------------------------- selection (maintainer)
# ``select`` checks every moment at its own stride before it is pinned: the
# sources must move (the smallest step between consecutive sources at least
# MIN_MOTION in 8-bit luma), no step may be a cut (the largest at most four
# times the second largest plus 2, and no step between native frames more than
# four times its neighbours plus 2) and no native frame in the window may repeat
# the one before it (a held frame has nothing to rebuild). A moment that fails
# tries starts half a second, a second and a second and a half either side; a
# moment with no passing start is dropped and listed, the threshold is never
# lowered.

MIN_MOTION = 1.0
SELECT_TRIES = 6


def candidate_starts(
    f0: int, fps: float, span: int, total: int, tries: int = SELECT_TRIES
) -> list[int]:
    """``f0``, then ``f0`` +- half a second, +- a second, ... (``tries`` more),
    each kept only when its window of ``span + 1`` frames fits in the footage."""
    half = max(1, round(fps / 2))
    out = [f0]
    for k in range(1, tries // 2 + 1):
        out += [f0 + k * half, f0 - k * half]
    return [f for f in out if 0 <= f and f + span < total]


def judge(
    steps: Sequence[float],
    holds: Sequence[int],
    min_motion: float = MIN_MOTION,
    natives: Sequence[float] = (),
) -> str | None:
    """Why a window is rejected (None when it passes). ``steps``: the mean
    absolute luma step between consecutive sources; ``holds``: native frames
    that repeat the frame before them; ``natives``: the steps between
    consecutive native frames (a cut is one far larger than its neighbours,
    which a source step can miss when the motion after the cut is fast)."""
    srt = sorted(steps)
    if srt[0] < min_motion:
        return f"too little motion (smallest source step {srt[0]:.2f} < {min_motion})"
    if len(srt) > 1 and srt[-1] > 4.0 * srt[-2] + 2.0:
        return f"a cut (largest step {srt[-1]:.1f}, next {srt[-2]:.1f})"
    for i, d in enumerate(natives):
        near = max([*natives[max(0, i - 1) : i], *natives[i + 1 : i + 2]], default=d)
        if d > 4.0 * near + 2.0:
            return f"a cut (native step {i + 1} is {d:.1f}, its neighbours at most {near:.1f})"
    if holds:
        return f"held frame at {holds[0]}"
    return None


class PairStats:
    """Mean absolute luma differences between frames ``k`` apart (k = 1 and the
    strides), fed one frame at a time in ascending order within a run."""

    def __init__(self, np: Any, strides: set[int]) -> None:
        self.np = np
        self.ks = sorted({1, *strides})
        self.recent: dict[int, Any] = {}
        self.d: dict[tuple[int, int], float] = {}
        self.same: dict[int, bool] = {}  # n -> frame n equals frame n - 1

    def feed(self, n: int, y: Any) -> None:
        np = self.np
        y16 = y.astype(np.int16)
        for k in self.ks:
            prev = self.recent.get(n - k)
            if prev is None or (n - k, n) in self.d:
                continue
            diff = np.abs(y16 - prev)
            self.d[(n - k, n)] = float(diff.mean())
            if k == 1:
                self.same[n] = not bool(diff.any())
        self.recent[n] = y16
        far = self.ks[-1]
        for old in [x for x in self.recent if abs(x - n) > far]:
            del self.recent[old]

    def window(
        self, f: int, s: int, tier: str
    ) -> tuple[list[float], list[int], list[float]] | None:
        """(source steps, held frames, native steps) of a window, or None when
        frames are missing."""
        n_src = 5 if tier == "2x" else 4
        span = (n_src - 1) * s
        steps = [self.d.get((f + k * s, f + (k + 1) * s)) for k in range(n_src - 1)]
        natives = [self.d.get((n - 1, n)) for n in range(f + 1, f + span + 1)]
        same = [self.same.get(n) for n in range(f + 1, f + span + 1)]
        if None in steps or None in natives or None in same:
            return None
        return (
            [float(x) for x in steps if x is not None],
            [f + 1 + i for i, x in enumerate(same) if x],
            [float(x) for x in natives if x is not None],
        )


def _feed_prepared(
    np: Any, stats: PairStats, m: dict[str, Any], fp: dict[str, Any], root: Path
) -> None:
    """Feed the frames already prepared for ``fp`` (the current moment starts)."""
    for first in slots(m, fp["id"]):
        path = moment_path(root, fp["id"], first)
        if not path.exists():
            continue
        with np.load(path) as d:
            recipe = json.loads(str(d["meta"])).get("prepare", {})
            if recipe.get("exposure", fp["prepare"].get("exposure")) != fp["prepare"].get(
                "exposure"
            ):
                continue
            ys, index = d["y"], [int(x) for x in d["index"]]
        stats.recent.clear()
        for i, n in enumerate(index):
            stats.feed(n, ys[i])


def select_footage(
    np: Any,
    m: dict[str, Any],
    fp: dict[str, Any],
    root: Path,
    *,
    threads: int,
    min_motion: float = MIN_MOTION,
) -> list[dict[str, Any]]:
    """Pick a start for every moment of ``fp``; returns one record per moment
    ({moment, f, reason, tried, steps, holds}); ``f`` is None when none passed.
    Frames the current starts need are read from the prepared files; other
    candidates are decoded in one pass (video) or fetched one window at a time
    (per-frame sources)."""
    mos = moments_of(m, fp["id"])
    fps = float(Fraction(*fp["native_fps"]))
    total = int(fp["frames_total"])
    stats = PairStats(np, {int(mo["s"]) for mo in mos})
    _feed_prepared(np, stats, m, fp, root)
    exposure = fp["prepare"].get("exposure")
    taken: set[tuple[int, int, str]] = set()
    recs: list[dict[str, Any]] = []
    for mo in mos:
        s, tier = int(mo["s"]), mo["tier"]
        span = (4 if tier == "2x" else 3) * s
        recs.append(
            {
                "moment": mo,
                "cands": candidate_starts(int(mo["f"]), fps, span, total),
                "f": None,
                "reason": None,
                "tried": [],
                "s": s,
                "tier": tier,
                "span": span,
            }
        )

    def settle(rec: dict[str, Any], fetch: Callable[[list[int]], None] | None) -> bool:
        """Walk ``rec``'s candidates in order; False when frames are missing and
        ``fetch`` is None (the caller decodes them and asks again)."""
        for f in rec["cands"]:
            if any(t[0] == f for t in rec["tried"]):
                continue
            if (f, rec["s"], rec["tier"]) in taken:
                rec["tried"].append((f, "start already used by another moment"))
                continue
            got = stats.window(f, rec["s"], rec["tier"])
            if got is None:
                if fetch is None:
                    return False
                frames = list(range(f, f + rec["span"] + 1))
                fetch(frames)
                got = stats.window(f, rec["s"], rec["tier"])
                if got is None:
                    raise BenchError(f"{fp['id']}@{f}: frames still missing after decoding")
            steps, holds, natives = got
            why = judge(steps, holds, min_motion, natives)
            rec["tried"].append((f, why))
            if why is None:
                rec["f"], rec["steps"], rec["holds"] = f, [round(x, 2) for x in steps], holds
                taken.add((f, rec["s"], rec["tier"]))
                return True
        rec["reason"] = "; ".join(f"@{f}: {why}" for f, why in rec["tried"])
        return True

    def decode_into(frames: list[int]) -> None:
        stats.recent.clear()
        for n, y, _u, _v in decode_footage(
            np, fp, sorted(set(frames)), root, threads=threads, exposure=exposure
        ):
            stats.feed(n, y)

    if fp["prepare"]["decode"] in ("mp4", "hevc"):
        open_recs = [r for r in recs if not settle(r, None)]
        if open_recs:
            need: set[int] = set()
            for r in open_recs:
                for f in r["cands"]:
                    need.update(range(f, f + r["span"] + 1))
            log(f"{fp['id']}: decoding {len(need)} frames for {len(open_recs)} moments")
            decode_into(sorted(need))
            for r in open_recs:
                settle(r, None)
                if r["f"] is None and r["reason"] is None:
                    raise BenchError(f"{fp['id']}: frames missing after the decode pass")
    else:
        kept = set(needed_frames(m, fp["id"]))

        def fetch(frames: list[int]) -> None:
            fetch_frames(fp, root, frames)
            decode_into(frames)

        for r in recs:
            settle(r, fetch)
        used = {
            n for r in recs if r["f"] is not None for n in range(r["f"], r["f"] + r["span"] + 1)
        }
        for n in sorted(_downloaded_frames(fp, root) - used - kept):
            _frame_path(fp, root, n).unlink()
    out = []
    for r in recs:
        out.append({k: r.get(k) for k in ("moment", "f", "reason", "tried", "steps", "holds")})
    return out


def _frame_path(fp: dict[str, Any], root: Path, n: int) -> Path:
    folder = download_dir(root, fp["id"])
    if fp["fetch"] == "y4m-range":
        return folder / f"frame_{n:06d}.y4mframe"
    base = int(fp.get("frame_base", 0))
    return folder / Path(urllib.parse.urlsplit(fp["urls"][0].format(n=base + n)).path).name


def _downloaded_frames(fp: dict[str, Any], root: Path) -> set[int]:
    folder = download_dir(root, fp["id"])
    out: set[int] = set()
    if fp["fetch"] == "y4m-range":
        for p in folder.glob("frame_*.y4mframe"):
            out.add(int(p.stem.split("_")[1]))
    else:
        base = int(fp.get("frame_base", 0))
        for p in folder.glob("*.exr"):
            digits = p.stem.rsplit("_", 1)[-1]
            if digits.isdigit():
                out.add(int(digits) - base)
    return out


def fetch_frames(fp: dict[str, Any], root: Path, frames: list[int]) -> None:
    """Fetch single frames of a per-frame source (selection reads; nothing pinned)."""
    folder = download_dir(root, fp["id"])
    folder.mkdir(parents=True, exist_ok=True)
    pinned = fp.get("frames", {})
    conn: dict[str, Any] = {}
    try:
        for n in frames:
            entry = pinned.get(str(n)) or {}
            dest = _frame_path(fp, root, n)
            if fp["fetch"] == "y4m-range":
                hdr, fb = int(fp["header_bytes"]), int(fp["frame_bytes"])
                fetch_file(
                    fp["urls"],
                    dest,
                    size=fb,
                    sha=entry.get("sha256"),
                    offset=hdr + n * fb,
                    length=fb,
                )
            else:
                base = int(fp.get("frame_base", 0))
                urls = [u.format(n=base + n) for u in fp["urls"]]
                fetch_file(
                    urls, dest, size=entry.get("size"), sha=entry.get("sha256"), ftp_conn=conn
                )
    finally:
        _close_ftp(conn)


def apply_selection(m: dict[str, Any], picks: list[dict[str, Any]], min_motion: float) -> list[str]:
    """Write the picked starts into ``m``: a moved moment loses its decoded hash,
    a moment with no passing start is dropped (and listed), per-frame sources
    keep only the frames the moments use. Returns the dropped moments."""
    dropped: list[str] = []
    keep_ids = set()
    for p in picks:
        mo = p["moment"]
        if p["f"] is None:
            dropped.append(
                f"{mo['footage']} {mo['tier']} s={mo['s']} near {mo['f']}: {p['reason']}"
            )
            continue
        if int(mo["f"]) != p["f"]:
            mo["f"] = p["f"]
            mo["decoded_sha256"] = None
        keep_ids.add(id(mo))
    m["moments"] = [
        mo
        for mo in m["moments"]
        if id(mo) in keep_ids or mo["footage"] not in {p["moment"]["footage"] for p in picks}
    ]
    for fp in m["footage"]:
        if isinstance(fp.get("frames"), dict):
            need = set(needed_frames(m, fp["id"]))
            fp["frames"] = {str(n): fp["frames"].get(str(n)) for n in sorted(need)}
            fp["frames"] = {k: v for k, v in fp["frames"].items() if v is not None}
    m["moments_selected"] = True
    # a dropped moment leaves the manifest, so no later run can try it again:
    # what earlier runs dropped stays listed
    earlier = dict(m.get("selection", {}))  # other keys (notes) are kept as they are
    m["selection"] = {
        **earlier,
        "min_motion": min_motion,
        "cut": "largest source step at most 4 x the second largest + 2",
        "holds": "no native frame equal to the one before it",
        "tries": SELECT_TRIES,
        "dropped": sorted({*earlier.get("dropped", []), *dropped}),
    }
    return dropped


def cmd_select(args: argparse.Namespace) -> int:
    import numpy as np

    if not args.pin:
        raise BenchError("select rewrites the manifest's moments; pass --pin to do it")
    m = load_manifest(args.manifest)
    errs = validate(m)
    if errs:
        raise BenchError("manifest: " + "; ".join(errs))
    if args.nice:
        os.nice(args.nice)
    root = cache_root()
    chosen = _select(m, args.only, args.accept_nc, "core")
    picks: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    for fp in chosen:
        t0 = time.monotonic()
        got = select_footage(np, m, fp, root, threads=args.threads, min_motion=args.min_motion)
        picks += got
        moved = sum(1 for p in got if p["f"] is not None and p["f"] != int(p["moment"]["f"]))
        lost = sum(1 for p in got if p["f"] is None)
        log(
            f"{fp['id']}: {len(got)} moments, {moved} moved, {lost} dropped "
            f"({time.monotonic() - t0:.0f} s)"
        )
        for p in got:
            mo = p["moment"]
            report.append(
                {
                    "footage": mo["footage"],
                    "tier": mo["tier"],
                    "s": mo["s"],
                    "was": int(mo["f"]),
                    "f": p["f"],
                    "steps": p.get("steps"),
                    "tried": p["tried"],
                }
            )
    dropped = apply_selection(m, picks, args.min_motion)
    for d in dropped:
        log(f"dropped: {d}")
    errs = validate(m)
    if errs:
        raise BenchError("selection left the manifest invalid: " + "; ".join(errs))
    save_manifest(m, args.manifest)
    if args.report:
        args.report.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    log("moments pinned; now run 'fetch --pin' and 'prepare --pin' to pin the new hashes")
    return 0


# ---------------------------------------------------------------- cel scenes
# 2D animation has no open footage drawn at a high rate, so these scenes are
# drawn here: flat fills, ink outlines, jointed cut-out parts, a three-layer
# parallax pan, speed lines, held text and a camera push. Each scene is a
# function of continuous time t (in 24 fps source frames), so the truth at any
# in-between time is simply the drawing at that time. "ones" draws a new picture
# for every source frame; "twos" holds each drawing for two source frames, as
# hand-drawn animation does, and the truth stays the continuous motion: the
# smooth result an engine that re-times held drawings is meant to reach. The
# "title" scene starts with a still hold longer than three frames, which hold
# fill must leave alone.

CEL_SCENES = ("parallax", "cutout", "ball", "dash", "title", "zoom")
CEL_VARIANTS = ("ones", "twos")
CEL_SOURCES = 9
CEL_SS = 2
CEL_VERSION = 1
#: a scene redrawn differently gets a new revision here (only it is drawn again).
#: Revision 2 of ball and cutout and 3 of dash slowed scenes that no engine could
#: follow on twos (every row at or below 0): the ball's bounce is lower and
#: slower (a curve too tight for any two-frame engine) and its stripe spins less,
#: the cut-out limbs swing at half speed, and the dash's streaks, runner and legs
#: move slower. They are still hard on twos, not impossible.
CEL_REVISIONS = {"ball": 2, "cutout": 2, "dash": 3}
INK = (0.08, 0.07, 0.10)

#: a shape: signed distance in scene pixels (negative inside), bounding box
#: (x0, y0, x1, y1) in scene pixels, fill colour (R'G'B' 0..1), outline width
Sdf = Callable[[Any, Any], Any]
Shape = tuple[Sdf, tuple[float, float, float, float], tuple[float, float, float], float]


def circle(np: Any, cx: float, cy: float, r: float) -> tuple[Sdf, tuple[float, ...]]:
    return (lambda x, y: np.hypot(x - cx, y - cy) - r), (cx - r, cy - r, cx + r, cy + r)


def rbox(
    np: Any, cx: float, cy: float, hw: float, hh: float, angle: float = 0.0, r: float = 0.0
) -> tuple[Sdf, tuple[float, ...]]:
    """A rectangle (half sizes hw, hh) turned by ``angle`` radians, corners rounded by r."""
    c, s = math.cos(angle), math.sin(angle)

    def sdf(x: Any, y: Any) -> Any:
        dx, dy = x - cx, y - cy
        qx = np.abs(c * dx + s * dy) - hw + r
        qy = np.abs(-s * dx + c * dy) - hh + r
        outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
        return outside + np.minimum(np.maximum(qx, qy), 0.0) - r

    ext = math.hypot(hw, hh)
    return sdf, (cx - ext, cy - ext, cx + ext, cy + ext)


def capsule(
    np: Any, ax: float, ay: float, bx: float, by: float, r: float
) -> tuple[Sdf, tuple[float, ...]]:
    """A segment from a to b, r thick on each side (limbs, speed lines)."""
    vx, vy = bx - ax, by - ay
    ll = vx * vx + vy * vy or 1e-9

    def sdf(x: Any, y: Any) -> Any:
        px, py = x - ax, y - ay
        h = np.clip((px * vx + py * vy) / ll, 0.0, 1.0)
        return np.hypot(px - h * vx, py - h * vy) - r

    box = (min(ax, bx) - r, min(ay, by) - r, max(ax, bx) + r, max(ay, by) + r)
    return sdf, box


def below(
    np: Any, ridge: Callable[[Any], Any], slope: Callable[[Any], Any], top: float, bottom: float
) -> tuple[Sdf, tuple[float, ...]]:
    """Everything under the curve y = ridge(x) (hills)."""

    def sdf(x: Any, y: Any) -> Any:
        return (ridge(x) - y) / np.sqrt(1.0 + slope(x) ** 2)

    return sdf, (-1e6, top, 1e6, bottom)


def both(a: tuple[Sdf, tuple[float, ...]], b: tuple[Sdf, tuple[float, ...]], np: Any) -> Any:
    """The intersection of two shapes (a stripe across a ball)."""
    return (lambda x, y: np.maximum(a[0](x, y), b[0](x, y))), a[1]


def wrap(x: float, period: float, lo: float) -> float:
    return lo + (x - lo) % period


def _shape(geom: Any, colour: tuple[float, float, float], outline: float = 0.0) -> Shape:
    return (geom[0], tuple(geom[1]), colour, outline)


class Canvas:
    """An R'G'B' picture drawn ``ss`` times larger than its output size."""

    def __init__(self, np: Any, w: int, h: int, ss: int) -> None:
        self.np = np
        self.k = (w / OUT_W) * ss  # scene pixels (1920x1080) to canvas pixels
        self.w, self.h = w * ss, h * ss
        self.ss = ss
        self.rgb = np.zeros((self.h, self.w, 3), dtype=np.float32)

    def gradient(self, top: tuple[float, ...], bottom: tuple[float, ...]) -> None:
        np = self.np
        t = ((np.arange(self.h, dtype=np.float64) + 0.5) / self.h)[:, None]
        for i in range(3):
            self.rgb[:, :, i] = (top[i] * (1.0 - t) + bottom[i] * t).astype(np.float32)

    def paint(self, shape: Shape) -> None:
        """Paint a shape over what is there; an outline straddles its edge in ink.
        Edge coverage is the distance to the edge in canvas pixels, clipped."""
        np = self.np
        sdf, box, colour, outline = shape
        k, pad = self.k, outline + 2.0 / self.k
        x0 = max(0, int(math.floor((box[0] - pad) * k)))
        y0 = max(0, int(math.floor((box[1] - pad) * k)))
        x1 = min(self.w, int(math.ceil((box[2] + pad) * k)))
        y1 = min(self.h, int(math.ceil((box[3] + pad) * k)))
        if x0 >= x1 or y0 >= y1:
            return
        xs = ((np.arange(x0, x1, dtype=np.float64) + 0.5) / k)[None, :]
        ys = ((np.arange(y0, y1, dtype=np.float64) + 0.5) / k)[:, None]
        d = np.broadcast_to(sdf(xs, ys), (y1 - y0, x1 - x0)) * k
        sub = self.rgb[y0:y1, x0:x1]
        if outline > 0:
            half = outline * k / 2.0
            self._over(sub, np.clip(0.5 - (d - half), 0.0, 1.0), INK)
            self._over(sub, np.clip(0.5 - (d + half), 0.0, 1.0), colour)
        else:
            self._over(sub, np.clip(0.5 - d, 0.0, 1.0), colour)

    def _over(self, sub: Any, a: Any, colour: tuple[float, ...]) -> None:
        np = self.np
        c = np.asarray(colour, dtype=np.float32)
        sub += (c - sub) * a[:, :, None].astype(np.float32)

    def planes(self) -> tuple[Any, Any, Any]:
        """The picture boxed to its output size, as 8-bit BT.709 Y'CbCr 4:2:0."""
        np, ss = self.np, self.ss
        h, w = self.h // ss, self.w // ss
        rgb = self.rgb.reshape(h, ss, w, ss, 3).mean(axis=(1, 3), dtype=np.float64)
        return rgb_to_ycbcr709(np, rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2])


def _scene_parallax(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.55, 0.74, 0.95), (0.86, 0.92, 0.98))
    out = [_shape(circle(np, 1600, 170, 70), (1.0, 0.88, 0.45), 2.5)]

    def ridge(x: Any) -> Any:  # far hills, 3 px a frame
        u = x + 3.0 * t
        return 560 + 50 * np.sin(u / 150.0) + 20 * np.sin(u / 57.0 + 1.0)

    def slope(x: Any) -> Any:
        u = x + 3.0 * t
        return 50 / 150.0 * np.cos(u / 150.0) + 20 / 57.0 * np.cos(u / 57.0 + 1.0)

    out.append(_shape(below(np, ridge, slope, 470, 1080), (0.47, 0.6, 0.55), 2.0))
    out.append(_shape(rbox(np, 960, 960, 1100, 170), (0.42, 0.63, 0.3), 2.5))
    for i in range(8):  # trees, 11 px a frame
        x = wrap(i * 300 + 80 - 11.0 * t, 2400, -240)
        out.append(_shape(rbox(np, x, 770, 14, 75, r=4), (0.48, 0.32, 0.2), 2.5))
        out.append(_shape(circle(np, x, 650, 72), (0.24, 0.5, 0.3), 2.5))
    for i in range(16):  # grass tufts on the ground, 20 px a frame
        x = wrap(i * 150 + 40 - 20.0 * t, 2400, -240)
        out.append(_shape(rbox(np, x, 860 + 23 * (i % 3), 18, 6, 0.3, 3), (0.33, 0.52, 0.22)))
    out.append(_shape(rbox(np, 960, 905, 1100, 6), (0.9, 0.84, 0.72), 2.0))  # fence rail
    for i in range(14):  # fence posts, 28 px a frame
        x = wrap(i * 160 - 28.0 * t, 2240, -160)
        out.append(_shape(rbox(np, x, 960, 15, 110, r=3), (0.92, 0.86, 0.74), 3.0))
    return out


def _scene_cutout(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.94, 0.89, 0.79), (0.9, 0.84, 0.72))
    out = [
        _shape(rbox(np, 960, 960, 1100, 140), (0.7, 0.55, 0.4), 2.5),
        _shape(rbox(np, 1500, 360, 150, 110, r=6), (0.72, 0.85, 0.95), 3.0),
        _shape(rbox(np, 1500, 360, 3, 110), INK),
    ]
    x = 520 + 9.0 * t
    lean = 0.06 * math.sin(0.2 * t)
    hip = (x, 690)
    for phase, shade in ((math.pi, 0.75), (0.0, 1.0)):  # back leg, then front
        a = 0.38 * math.sin(0.225 * t + phase)  # a leg swings about 13 px a frame
        foot = (hip[0] + 150 * math.sin(a), hip[1] + 150 * math.cos(a))
        col = (0.2 * shade, 0.26 * shade, 0.52 * shade)
        out.append(_shape(capsule(np, *hip, *foot, 22), col, 2.5))
    shoulder = (x + 30 * math.sin(lean), 480)

    def arm(phase: float, shade: float) -> list[Shape]:
        a1 = 0.9 + 0.6 * math.sin(0.225 * t + phase)
        elbow = (shoulder[0] + 95 * math.sin(a1), shoulder[1] + 95 * math.cos(a1))
        a2 = a1 + 0.8 + 0.5 * math.sin(0.3 * t + 1.0 + phase)
        hand = (elbow[0] + 85 * math.sin(a2), elbow[1] + 85 * math.cos(a2))
        col = (0.85 * shade, 0.3 * shade, 0.25 * shade)
        return [
            _shape(capsule(np, *shoulder, *elbow, 18), col, 2.5),
            _shape(capsule(np, *elbow, *hand, 16), col, 2.5),
            _shape(circle(np, *hand, 17), (0.96 * shade, 0.8 * shade, 0.66 * shade), 2.5),
        ]

    out += arm(math.pi, 0.78)
    out.append(_shape(rbox(np, x, 580, 58, 112, lean, 22), (0.85, 0.3, 0.25), 3.0))
    head = (x + 110 * math.sin(lean), 410)
    out.append(_shape(circle(np, *head, 56), (0.96, 0.8, 0.66), 3.0))
    out.append(_shape(circle(np, head[0] + 20, head[1] - 8, 6), INK))
    out.append(_shape(circle(np, head[0] - 14, head[1] - 8, 6), INK))
    out += arm(0.0, 1.0)
    return out


def _scene_ball(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.96, 0.95, 0.9), (0.93, 0.92, 0.86))
    out = [_shape(rbox(np, 960, 1000, 1100, 130), (0.62, 0.66, 0.72), 2.5)]
    x = 250 + 24.0 * t
    # a hop every 24 frames, at the top at t = 2: the curve bends the path by about
    # 0.4 px at most between two frames, 1.7 px across a held pair (a two-frame engine
    # moves in straight lines)
    hgt = 200 * abs(math.sin(math.pi * (t + 10.0) / 24.0))
    y = 870 - 90 - hgt
    width = 95 * (1.0 - hgt / 700.0)
    out.append(_shape(rbox(np, x, 880, width, 14, r=14), (0.5, 0.53, 0.58)))
    ball = circle(np, x, y, 90)
    out.append(_shape(ball, (0.96, 0.76, 0.12), 3.0))
    a = 0.10 * t  # the stripe turns 9 px a frame at the rim
    sn, cs = math.sin(a), math.cos(a)
    band = (lambda px, py: np.abs(-sn * (px - x) + cs * (py - y)) - 22.0), ball[1]
    out.append(_shape(both(ball, band, np), (0.85, 0.2, 0.2)))
    out.append(_shape(circle(np, x - 35, y - 35, 14), (1.0, 0.97, 0.85)))
    return out


def _scene_dash(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.9, 0.93, 0.98), (0.84, 0.88, 0.95))
    out: list[Shape] = []
    rng = [((k * 7919) % 1000) / 1000.0 for k in range(24)]
    for k in range(24):  # streaks of the background, 32 px a frame
        y = 70 + 42 * k
        length = 160 + 260 * rng[k]
        # off the left edge whole (up to 420 px long) before it comes back on the right
        x = wrap(rng[(k + 5) % 24] * 2440 - 32.0 * t, 2440, -480)
        out.append(_shape(capsule(np, x, y, x + length, y, 2.5), (0.7, 0.76, 0.88)))
    x = 260 + 28.0 * t
    for k in range(5):  # speed lines behind the runner
        y = 520 + 45 * k
        out.append(_shape(capsule(np, x - 110 - 60 * (k % 3) - 160, y, x - 110, y, 2.5), INK))
    stride = math.sin(0.3 * t)
    for phase in (0.0, math.pi):  # legs: the feet move up to about 23 px a frame
        a = 0.55 * math.sin(0.3 * t + phase)
        out.append(
            _shape(
                capsule(np, x, 720, x + 140 * math.sin(a), 720 + 140 * math.cos(a), 20),
                (0.2, 0.25, 0.45),
                2.5,
            )
        )
    out.append(_shape(rbox(np, x + 10, 630, 58, 95, -0.28, 26), (0.3, 0.6, 0.85), 3.0))
    out.append(_shape(circle(np, x + 48, 500 + 4 * stride, 50), (0.96, 0.8, 0.66), 3.0))
    out.append(_shape(circle(np, x + 70, 492 + 4 * stride, 6), INK))
    return out


_GLYPHS: list[list[tuple[int, int]]] = []


def _glyphs() -> list[list[tuple[int, int]]]:
    """Fourteen blocky 3x5 glyphs, the same every run."""
    if not _GLYPHS:
        state = 12345
        for _ in range(14):
            cells = []
            for r in range(5):
                for c in range(3):
                    state = (state * 1103515245 + 12345) % (1 << 31)
                    if state >> 16 & 1 or (r in (0, 4) and c == 1):
                        cells.append((r, c))
            _GLYPHS.append(cells)
    return _GLYPHS


def _scene_title(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.1, 0.13, 0.28), (0.16, 0.2, 0.38))
    # the shape behind the title waits for three frames, then sweeps right
    x = 380 + 16.0 * max(0.0, t - 3.0)
    out = [
        _shape(rbox(np, x, 560, 150, 150, 0.785 + 0.004 * (x - 380), 18), (0.95, 0.6, 0.2), 3.0),
        _shape(circle(np, 1500, 300, 60), (0.98, 0.95, 0.8), 2.5),
        _shape(rbox(np, 960, 540, 540, 92, r=14), (0.98, 0.97, 0.92), 3.0),
    ]
    for i, cells in enumerate(_glyphs()):  # the held text
        gx = 960 - 7 * 68 + 34 + 68 * i
        for r, c in cells:
            out.append(_shape(rbox(np, gx - 16 + 16 * c, 508 + 16 * r, 8, 8), INK))
    return out


def _scene_zoom(np: Any, t: float, cv: Canvas) -> list[Shape]:
    cv.gradient((0.6, 0.78, 0.96), (0.8, 0.88, 0.97))
    z = 1.035**t  # a slow camera push towards the door
    cx, cy = 900.0, 620.0

    def at(geom: Any) -> Any:  # the drawing as the camera sees it
        sdf, box = geom

        def moved(x: Any, y: Any) -> Any:
            return z * sdf(cx + (x - cx) / z, cy + (y - cy) / z)

        return moved, (
            cx + (box[0] - cx) * z,
            cy + (box[1] - cy) * z,
            cx + (box[2] - cx) * z,
            cy + (box[3] - cy) * z,
        )

    w = 2.5 * z
    return [
        _shape(at(rbox(np, 960, 900, 1400, 240)), (0.45, 0.66, 0.32), w),
        _shape(at(circle(np, 300, 250, 80)), (1.0, 0.9, 0.5), w),
        _shape(at(rbox(np, 900, 380, 190, 190, 0.785, 10)), (0.7, 0.25, 0.2), w),
        _shape(at(rbox(np, 900, 560, 250, 170, r=4)), (0.95, 0.88, 0.7), w),
        _shape(at(rbox(np, 900, 640, 50, 90, r=6)), (0.45, 0.3, 0.22), w),
        _shape(at(circle(np, 930, 640, 7)), (0.95, 0.8, 0.3)),
        _shape(at(rbox(np, 760, 540, 55, 45, r=3)), (0.75, 0.88, 0.97), w),
        _shape(at(rbox(np, 1040, 540, 55, 45, r=3)), (0.75, 0.88, 0.97), w),
        _shape(at(rbox(np, 760, 540, 2, 45)), INK),
        _shape(at(rbox(np, 1040, 540, 2, 45)), INK),
        _shape(at(rbox(np, 1450, 690, 14, 90, r=4)), (0.48, 0.32, 0.2), w),
        _shape(at(circle(np, 1450, 560, 95)), (0.25, 0.5, 0.3), w),
    ]


CEL_DRAW: dict[str, Callable[[Any, float, Canvas], list[Shape]]] = {
    "parallax": _scene_parallax,
    "cutout": _scene_cutout,
    "ball": _scene_ball,
    "dash": _scene_dash,
    "title": _scene_title,
    "zoom": _scene_zoom,
}


def cel_frame(
    np: Any, scene: str, t: float, w: int = OUT_W, h: int = OUT_H, ss: int = CEL_SS
) -> tuple[Any, Any, Any]:
    """The scene at time t (source frames), as 8-bit Y'CbCr 4:2:0 at w x h."""
    cv = Canvas(np, w, h, ss)
    for shape in CEL_DRAW[scene](np, float(t), cv):
        cv.paint(shape)
    return cv.planes()


def drawing_time(j: int, variant: str) -> float:
    """The time drawn on source frame j: every frame, or every other one held."""
    return float(j if variant == "ones" else 2 * (j // 2))


def scored_outputs(tier: str, n_sources: int, m: int) -> list[int]:
    """Output frames scored (at m x the source rate, frame m*j is source j):
    2x tier, the in-betweens of the two middle pairs, each with real frames on
    both sides as in playback; display tier, the m-1 in-betweens of the middle
    pair."""
    if tier == "2x":
        c = n_sources // 2
        return [m * (c - 1) + m // 2, m * c + m // 2]
    c = (n_sources - 1) // 2
    return [m * c + k for k in range(1, m)]


# ---------------------------------------------------------------- moments to score


@dataclass
class Moment:
    """One moment as an engine sees it: the source frames (24 fps) and, for
    each scored output frame, the real frame it should match."""

    id: str
    set: str
    group: str
    table: str
    tier: str
    s: int
    m: int
    footage: str
    sources: list[tuple[Any, Any, Any]]
    truths: list[tuple[int, tuple[Any, Any, Any]]]
    images_ok: bool
    chroma_loc: str
    warnings: list[str] = field(default_factory=list)

    def meta(self) -> dict[str, Any]:
        return {
            "set": self.set,
            "group": self.group,
            "table": self.table,
            "tier": self.tier,
            "s": self.s,
            "m": self.m,
            "footage": self.footage,
            "sources": len(self.sources),
            "scored": [o for o, _ in self.truths],
        }


def moment_id(mo: dict[str, Any]) -> str:
    return f"{mo['footage']}@{mo['f']}-s{mo['s']}-{mo['tier']}"


def chroma_loc_of(fp: dict[str, Any]) -> str:
    """Where the footage's chroma sits: MPEG's left for H.264/HEVC, centred for
    what prepare subsampled itself (EXR) and for the El Fuente y4m (C420)."""
    return "left" if fp["prepare"]["decode"] in DECODERS else "center"


def load_moment(np: Any, m: dict[str, Any], mo: dict[str, Any], root: Path) -> Moment:
    """A prepared moment, checked against its pinned decoded hash."""
    fp = footage_by_id(m)[mo["footage"]]
    st = m["sets"][fp["set"]]
    first = int(mo["f"])
    path = moment_path(root, fp["id"], first)
    if not path.exists():
        raise BenchError(f"{fp['id']}@{first}: not prepared (run 'prepare' first)")
    win = window(mo)
    with np.load(path) as d:
        index = [int(x) for x in d["index"]]
        missing = sorted(set(win) - set(index))
        if missing:
            raise BenchError(f"{fp['id']}@{first}: frames {missing[:4]} missing; prepare again")
        sel = [index.index(n) for n in win]
        y, u, v = d["y"][sel], d["u"][sel], d["v"][sel]
    warnings = []
    pinned = mo.get("decoded_sha256")
    if pinned and planes_sha256(np, y, u, v) != pinned:
        msg = f"{moment_id(mo)}: decoded frames differ from the pinned hash"
        if st["decoded_hash"] == "hard":
            raise BenchError(msg + " (prepare again)")
        warnings.append(msg)
    s, mm, tier = int(mo["s"]), int(mo["m"]), mo["tier"]
    pos = {n: i for i, n in enumerate(win)}

    def frame(n: int) -> tuple[Any, Any, Any]:
        i = pos[n]
        return y[i], u[i], v[i]

    n_src = 5 if tier == "2x" else 4
    return Moment(
        id=moment_id(mo),
        set=fp["set"],
        group=st["group"],
        table=st.get("table", "headline" if st["headline"] else "other"),
        tier=tier,
        s=s,
        m=mm,
        footage=fp["id"],
        sources=[frame(first + j * s) for j in range(n_src)],
        truths=[(o, frame(first + o * s // mm)) for o in scored_outputs(tier, n_src, mm)],
        images_ok=bool(st["images_ok"]) and not st["nc"],
        chroma_loc=chroma_loc_of(fp),
        warnings=warnings,
    )


def cel_moment(
    np: Any, root: Path | None, scene: str, variant: str, w: int = OUT_W, h: int = OUT_H
) -> Moment:
    """A cel scene as a moment: nine sources, so hold fill sees whole holds on
    both sides of the scored pairs. Kept under moments/ once drawn."""
    st = load_manifest()["sets"]["cel"]
    recipe = {
        "version": CEL_VERSION,
        "revision": CEL_REVISIONS.get(scene, 1),
        "ss": CEL_SS,
        "size": [w, h],
    }
    path = root / MOMENTS_DIR / f"cel-{scene}-{variant}.npz" if root is not None else None
    outs = scored_outputs("2x", CEL_SOURCES, 2)
    srcs: list[tuple[Any, Any, Any]] | None = None
    truths: list[tuple[Any, Any, Any]] | None = None
    if path is not None and path.exists():
        with np.load(path) as d:
            if json.loads(str(d["meta"])).get("prepare") == recipe:
                srcs = [(d["y"][k], d["u"][k], d["v"][k]) for k in range(CEL_SOURCES)]
                truths = [(d["ty"][k], d["tu"][k], d["tv"][k]) for k in range(len(outs))]
    if srcs is None or truths is None:
        drawn: dict[float, tuple[Any, Any, Any]] = {}
        for t in [drawing_time(j, variant) for j in range(CEL_SOURCES)] + [o / 2 for o in outs]:
            if t not in drawn:
                drawn[t] = cel_frame(np, scene, t, w, h)
        srcs = [drawn[drawing_time(j, variant)] for j in range(CEL_SOURCES)]
        truths = [drawn[o / 2] for o in outs]
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.stem + ".tmp.npz")
            meta = json.dumps({"footage": f"cel-{scene}", "variant": variant, "prepare": recipe})
            np.savez_compressed(
                tmp,
                y=np.stack([f[0] for f in srcs]),
                u=np.stack([f[1] for f in srcs]),
                v=np.stack([f[2] for f in srcs]),
                ty=np.stack([f[0] for f in truths]),
                tu=np.stack([f[1] for f in truths]),
                tv=np.stack([f[2] for f in truths]),
                meta=np.array(meta),
            )
            os.replace(tmp, path)
    return Moment(
        id=f"cel-{scene}-{variant}",
        set="cel",
        group=st["group"],
        table=st["table"],
        tier="2x",
        s=2,
        m=2,
        footage=f"cel-{scene}",
        sources=srcs,
        truths=list(zip(outs, truths, strict=True)),
        images_ok=True,
        chroma_loc="center",
    )


# ---------------------------------------------------------------- configurations

#: The named configurations (design 2.5). ``backend`` None is the bench's own
#: blend, the floor every engine must clear; the others run through the
#: playback template exactly as mpv does.
CONFIGS: dict[str, dict[str, Any]] = {
    "blend": {"backend": None, "tiers": ("2x", "display")},
    "mvtools": {"backend": "mvtools", "tiers": ("2x", "display")},
    "mvgpu": {"backend": "mvgpu", "tiers": ("2x", "display")},
    "rife-trt-4.26": {
        "backend": "rife-trt",
        "model": "rife-v4.26_ensembleFalse",
        "tiers": ("2x", "display"),
    },
    "rife-trt-4.22-lite": {
        "backend": "rife-trt",
        "model": "rife-v4.22_lite_ensembleFalse",
        "tiers": ("2x",),
    },
    # no half-flow row: playback no longer computes RIFE's flow at half scale
    # (spike M0(x): a smaller picture beats it at every size it was used at).
    # A configuration with "flow_scale" still runs (see Engines._trt)
    "rife-ncnn-4.26": {
        "backend": "rife-ncnn",
        "model": "rife-v4.26_ensembleFalse",
        "tiers": ("2x",),
    },
    "cascade-trt": {
        "backend": "rife-trt",
        "model": "rife-v4.26_ensembleFalse",
        "cascade": True,
        "tiers": ("display",),
    },
    "cascade-ncnn": {
        "backend": "rife-ncnn",
        "model": "rife-v4.26_ensembleFalse",
        "cascade": True,
        "tiers": ("display",),
    },
    # the app's 2D rules (a profile set to 2D animation, or an Auto profile whose
    # content probe found hand-drawn animation): the first engine of the session's
    # ANIMATION_RANKING that runs here, held drawings re-timed, no cascade
    "profile-2d": {
        "backend": "auto",
        "content": "animation",
        "hold_fill": True,
        "tiers": ("2x",),
        "groups": ("2d",),
    },
}
#: the model an engine chosen by the app's own order runs (``trt.DEFAULT_MODEL``)
AUTO_MODEL = "rife-v4.26_ensembleFalse"
SRC_FPS = 24
NCNN_MODELS = "rife-ncnn-models"


class Unavailable(BenchError):
    """A configuration that cannot run on this machine (says why)."""


def blend_frames(np: Any, mo: Moment, planes: int = 1) -> list[tuple[Any, ...]]:
    """The floor: each in-between is the two source frames around it mixed by
    its position between them."""
    out = []
    for o, _ in mo.truths:
        a, k = divmod(o, mo.m)
        w = k / mo.m
        frame = []
        for p in range(planes):
            x = mo.sources[a][p].astype(np.float64)
            y = mo.sources[min(a + 1, len(mo.sources) - 1)][p].astype(np.float64)
            frame.append(np.clip(np.floor((1 - w) * x + w * y + 0.5), 0, 255).astype(np.uint8))
        out.append(tuple(frame))
    return out


#: a folder holding a ``buttereye`` package to measure instead of the checkout's
#: (a frozen copy, while the checkout is being edited); recorded as "snapshot"
APP_ENV = "BUTTEREYE_BENCH_APP"


def app_root() -> Path:
    env = os.environ.get(APP_ENV)
    return Path(env).resolve() if env else REPO


def _import_app() -> None:
    """The checkout's ``buttereye`` package first (or ``$BUTTEREYE_BENCH_APP``'s):
    the template measured is the one in this tree (its git state and a hash of
    its sources are recorded)."""
    root = app_root()
    if (root / "buttereye").is_dir() and os.fspath(root) not in sys.path:
        sys.path.insert(0, os.fspath(root))


def app_sha256(package: Path) -> str:
    """One hash over the app's Python sources and the playback template: two
    runs with the same hash ran the same code."""
    h = hashlib.sha256()
    for f in sorted(package.rglob("*")):
        if f.suffix in (".py", ".vpy") and "__pycache__" not in f.parts:
            h.update(f.relative_to(package).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()


def _truth() -> Any:
    """``truth.py``'s metrics (block SSIM, scores, flicker, clips)."""
    if os.fspath(HERE) not in sys.path:
        sys.path.insert(0, os.fspath(HERE))
    import truth  # type: ignore[import-not-found]

    return truth


def hold_max_default() -> int:
    try:
        from buttereye.core.mpvctl.session import HOLD_MAX
    except Exception:  # noqa: BLE001 - the session module may not import on its own
        return 3
    return int(HOLD_MAX)


def _build_log_tail(paths: Any, folder: str) -> str:
    """The first TensorRT error line of an engine build log, if any."""
    log_path = Path(paths.logs_dir) / f"engine-{folder}.log"
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        if "] [E] Error" in line:
            return line.split("] [E] ", 1)[1][:300]
    return ""


class Engines:
    """The playback template, run once per moment and configuration in this
    process: ``video_in`` is the moment's clip and ``user_data`` comes from the
    app's own generator, as mpv's VapourSynth filter would hand it over."""

    def __init__(
        self,
        np: Any,
        *,
        build: bool,
        hold_fill: bool,
        size: tuple[int, int] = (OUT_W, OUT_H),
        cache_mb: int = 1024,
        threads: int | None = None,
    ) -> None:
        _import_app()
        import vapoursynth as vs  # type: ignore[import-untyped]

        from buttereye.core import paths as _paths
        from buttereye.core.profiles.defaults import DEFAULT_SC_THRESHOLD
        from buttereye.core.scriptgen import generator

        self.np, self.vs, self.core = np, vs, vs.core
        self.core.max_cache_size = cache_mb
        if threads:
            self.core.num_threads = threads
        self.gen = generator
        self.paths = _paths.resolve()
        self.consts = generator.ScriptConstants(_paths.plugin_dir_for(self.paths))
        self.code = compile(generator.render_script(self.consts), "template.vpy", "exec")
        self.build, self.hold_fill, self.size = build, hold_fill, size
        self.sc_threshold = DEFAULT_SC_THRESHOLD
        self.hold_max = hold_max_default()
        self.trt_cache: dict[tuple[str, float], dict[str, Any]] = {}
        self.resolved: dict[str, dict[str, Any]] = {}
        self.engines_used: dict[str, str] = {}
        self.models_used: dict[str, str] = {}
        self.truth = _truth()

    # ---- settings
    def _trt(self, spec: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        from buttereye.core.backends import trt

        key_id = (spec["model"], float(spec.get("flow_scale", 1.0)))
        if key_id in self.trt_cache:
            return self.trt_cache[key_id]
        inst = trt.find_install(self.paths)
        if inst is None:
            raise Unavailable("TensorRT: " + "; ".join(trt.problems(self.paths)))
        if spec["model"] not in trt.available_models(inst):
            raise Unavailable(f"TensorRT: no ONNX file for {spec['model']}")
        ident = asyncio.run(trt.nvidia_identity(None))
        if ident is None:
            raise Unavailable("TensorRT: nvidia-smi did not report a GPU")
        w, h = self.size
        key = trt.EngineKey(
            ident[0], ident[1], inst.vstrt_version, inst.trt_version, spec["model"], w, h,
            flow_scale=key_id[1],
        )  # fmt: skip
        folder = trt.engine_dir(self.paths, key)
        if not trt.is_ready(folder):
            if not self.build:
                raise Unavailable(f"TensorRT engine {folder.name} is not built (--no-build)")
            # as the app's speed test: the template in build mode, then vsmlrt's trtexec
            script = cache_root() / "template.vpy"
            script.parent.mkdir(parents=True, exist_ok=True)
            self.gen.write_script(script, self.consts)
            log(f"building TensorRT engine {folder.name} (minutes)")
            try:
                folder = asyncio.run(trt.build_engine(self.paths, inst, key, script=script))
            except Exception as exc:  # noqa: BLE001 - a failed build is a row not run
                tail = _build_log_tail(self.paths, folder.name)
                shutil.rmtree(folder, ignore_errors=True)  # no half-built engine left behind
                raise Unavailable(
                    f"TensorRT engine {folder.name} could not be built ({type(exc).__name__}"
                    f"{': ' + tail if tail else ''})"
                ) from exc
        settings = trt.script_settings(
            inst, key, folder, streams=trt.streams_for(w, h), build=False
        )
        onnx = trt.onnx_file(inst, spec["model"])
        if onnx is not None and onnx.is_file():
            self.models_used[onnx.name] = sha256_file(onnx)
        self.trt_cache[key_id] = settings
        return settings

    def _ncnn(self, spec: dict[str, Any]) -> Path:
        folder: Path = Path(self.paths.rpm_data_dir) / NCNN_MODELS / spec["model"]
        if not folder.is_dir():
            raise Unavailable(f"RIFE-ncnn model {spec['model']} is not installed")
        for f in sorted(folder.iterdir()):
            if f.is_file():
                self.models_used[f"{folder.name}/{f.name}"] = sha256_file(f)
        return folder

    def resolve(self, name: str) -> dict[str, Any]:
        """The configuration as it runs here. An ``auto`` one takes the first
        engine of the app's own order that can run on this machine (for the 2D
        rules ``session.ANIMATION_RANKING``: RIFE on TensorRT, RIFE-ncnn,
        MVTools, mvgpu); the choice is kept in the result file."""
        spec = CONFIGS[name]
        if spec["backend"] != "auto":
            return spec
        if name in self.resolved:
            return self.resolved[name]
        from buttereye.core.mpvctl.session import ANIMATION_RANKING, LIVE_RANKING
        from buttereye.core.types import BackendId

        ranking = ANIMATION_RANKING if spec.get("content") == "animation" else LIVE_RANKING
        why: list[str] = []
        for b in ranking:
            cand = {k: v for k, v in spec.items() if k != "backend"} | {"backend": b.value}
            if b in (BackendId.RIFE_TRT, BackendId.RIFE_NCNN):
                cand["model"] = AUTO_MODEL
            try:
                if b is BackendId.RIFE_TRT:
                    self._trt(cand)
                elif b is BackendId.RIFE_NCNN:
                    self._ncnn(cand)
                elif not (self.consts.plugin_dir / f"{b.value}.so").is_file():
                    raise Unavailable(f"{b.value}: plugin not installed")
            except Unavailable as exc:
                why.append(str(exc))
                continue
            self.resolved[name] = cand
            return cand
        raise Unavailable("no engine of the app's order runs here: " + "; ".join(why))

    def user_data(self, name: str, m: int, chroma_loc: str) -> str:
        """The exact user_data the template gets for this configuration."""
        from buttereye.core.types import BackendId

        spec = self.resolve(name)
        backend = BackendId(spec["backend"])
        buffered, concurrent = self.gen.default_frames(backend)
        src = Fraction(SRC_FPS)
        trt_settings = self._trt(spec) if backend is BackendId.RIFE_TRT else None
        model_path = self._ncnn(spec) if backend is BackendId.RIFE_NCNN else None
        if trt_settings is not None:
            self.engines_used[name] = Path(trt_settings["engine_dir"]).name
        params = self.gen.FilterParams(
            gen=0,
            backend=backend,
            src_fps=src,
            target_fps=src * m,
            buffered_frames=buffered,
            concurrent_frames=concurrent,
            matrix="709",
            range="limited",
            chroma_loc=chroma_loc,
            model_path=model_path,
            gpu_thread=self.gen.rife_gpu_thread() if backend is BackendId.RIFE_NCNN else None,
            uhd=False,
            sc_threshold=self.sc_threshold,
            hold_fill=bool(spec.get("hold_fill", self.hold_fill)),
            hold_max=self.hold_max,
            still_skip=True,
            cascade_fps=src * 2 if spec.get("cascade") and m > 2 else None,
            trt=trt_settings,
        )
        text: str = self.gen.user_data(params)
        return text

    def check(self, name: str) -> None:
        """Raises Unavailable when the configuration cannot run here."""
        if CONFIGS[name]["backend"] is not None:
            self.user_data(name, 2, "left")

    # ---- running
    def run(self, name: str, mo: Moment, planes: int = 1) -> list[tuple[Any, ...]]:
        """The scored output frames of one configuration on one moment."""
        np, vs = self.np, self.vs
        if CONFIGS[name]["backend"] is None:
            return blend_frames(np, mo, planes)
        ud = self.user_data(name, mo.m, mo.chroma_loc)
        g: dict[str, Any] = {
            "__name__": "__vapoursynth__",
            "video_in": self.truth.as_clip(self.core, vs, np, mo.sources),
            "user_data": ud,
        }
        vs.clear_outputs()
        node = None
        try:
            exec(self.code, g)  # noqa: S102 - the app's own generated template
            out = vs.get_outputs()[0]
            node = out.clip if hasattr(out, "clip") else out
            need = max(o for o, _ in mo.truths)
            if node.num_frames <= need:
                raise BenchError(f"{node.num_frames} output frames, frame {need} needed")
            if node.format.id != vs.YUV420P8:
                raise BenchError(f"output format {node.format.name}, not YUV420P8")
            got = []
            for o, _ in mo.truths:
                f = node.get_frame(o)
                got.append(tuple(np.asarray(f[p]).copy() for p in range(planes)))
            return got
        finally:
            node = None
            g.clear()
            vs.clear_outputs()
            gc.collect()

    def plugins(self) -> list[dict[str, Any]]:
        out = []
        for p in self.core.plugins():
            path = getattr(p, "plugin_path", None)
            if not path or "vapoursynth/libvapoursynth" in str(path):
                continue
            out.append(
                {
                    "namespace": p.namespace,
                    "identifier": p.identifier,
                    "path": str(path),
                    "version": ".".join(map(str, tuple(getattr(p, "version", ()) or ()))),
                }
            )
        return sorted(out, key=lambda x: x["namespace"])


# ---------------------------------------------------------------- scoring


def score_moment(truth: Any, np: Any, outs: list[tuple[Any, ...]], mo: Moment) -> dict[str, Any]:
    """Worst areas: the lowest over the moment's truth frames; mean SSIM, PSNR
    and squared error: their mean; flicker across consecutive in-betweens."""
    lumas = [o[0] for o in outs]
    reals = [t[1][0] for t in mo.truths]
    rows = [truth.score(np, a, b) for a, b in zip(lumas, reals, strict=True)]
    worst_i = min(range(len(rows)), key=lambda i: rows[i]["worst"])
    return {
        "worst": min(r["worst"] for r in rows),
        "mean": round(sum(r["mean"] for r in rows) / len(rows), 4),
        "psnr": round(sum(r["psnr"] for r in rows) / len(rows), 2),
        "mse": round(sum(r["mse"] for r in rows) / len(rows), 4),
        "flicker": truth.flicker(np, lumas, reals),
        "worst_frame": mo.truths[worst_i][0],
    }


METRICS = ("worst", "flicker", "mean", "psnr", "mse")


def _row_key(meta: dict[str, Any]) -> tuple[str, str]:
    """(table, row) a moment counts in. Tiers are never mixed in one figure."""
    tier, group = meta["tier"], meta["group"]
    if meta["table"] == "film-step":
        what = f"2x, s={meta['s']}" if tier == "2x" else f"x{meta['m']}"
        return "film-step", f"{group} {what}"
    if meta["table"] == "2d":
        return "2d", group
    label = group if tier == "2x" else f"{group} x{meta['m']}"
    return f"{tier}-{meta['table']}", label


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def summarize(
    results: dict[str, dict[str, Any]], moments: dict[str, dict[str, Any]], configs: list[str]
) -> dict[str, Any]:
    """Per table, the mean of each row's moments per configuration; in headline
    tables the headline is the mean of the group means (each real-footage group
    weighs the same). Squared error is also given relative to mvgpu, in %.

    Every configuration in a row is averaged over the same moments: the ones
    each configuration that scored in the row finished (``n`` of ``moments``).
    A moment one engine failed on is left out for all of them, so no figure
    compares means over different moments (the failed moments are often the
    hard ones). A configuration that failed every moment of a row has no cell
    there, and so no headline."""
    rows: dict[str, dict[str, list[str]]] = {}
    for mid, meta in moments.items():
        table, row = _row_key(meta)
        rows.setdefault(table, {}).setdefault(row, []).append(mid)
    out: dict[str, Any] = {}
    for table, by_row in rows.items():
        t: dict[str, Any] = {"rows": {}, "headline": None}
        for row, mids in by_row.items():

            def ok(x: str, c: str) -> bool:
                g = results.get(x, {}).get(c)
                return g is not None and "error" not in g

            scored = [c for c in configs if any(ok(x, c) for x in mids)]
            common = [x for x in mids if all(ok(x, c) for c in scored)]
            cells: dict[str, Any] = {}
            for c in scored if common else []:
                got = [results[x][c] for x in common]
                cells[c] = {k: _mean([g[k] for g in got]) for k in METRICS}
                cells[c]["n"] = len(got)
            rel = _relative(cells)
            t["rows"][row] = {"moments": len(mids), "cells": cells, "mse_vs_mvgpu": rel}
        if table.endswith("-headline"):
            # each group weighs the same, however many rows (multipliers) it has
            groups: dict[str, list[str]] = {}
            for row in t["rows"]:
                groups.setdefault(row.split(" ", 1)[0], []).append(row)
            head: dict[str, Any] = {}
            for c in configs:
                if not all(c in r["cells"] for r in t["rows"].values()):
                    continue
                per = [
                    {k: _mean([t["rows"][r]["cells"][c][k] for r in rs]) for k in METRICS}
                    for rs in groups.values()
                ]
                head[c] = {k: _mean([p[k] for p in per]) for k in METRICS}
            t["headline"] = {"cells": head, "mse_vs_mvgpu": _relative(head)}
        out[table] = t
    return out


def _relative(cells: dict[str, Any]) -> dict[str, float]:
    base = cells.get("mvgpu", {}).get("mse")
    if not base:
        return {}
    return {c: round(100.0 * (v["mse"] / base - 1.0), 1) for c, v in cells.items()}


TABLE_TITLES = {
    "2x-headline": "2x tier (24 to 48 fps), openly licensed footage (CC BY)",
    "display-headline": "Display tier (24 to 24 x m fps), openly licensed footage (CC BY)",
    "2d": "2D cel scenes (drawn by the benchmark; reported apart)",
    "film-step": "Film step (UVG, CC BY-NC 3.0: numbers only, never in the headline)",
}
TABLE_ORDER = ("2x-headline", "display-headline", "2d", "film-step")


SHORT_NOTE = (
    "n/m: an engine failed on some of the row's m moments; every figure in that row, "
    "and the headline, covers only the n moments all of them finished."
)


def _scored(r: dict[str, Any]) -> int:
    """How many of a summary row's moments its figures cover (see ``summarize``)."""
    return max((v["n"] for v in r["cells"].values()), default=0)


def _count(r: dict[str, Any]) -> str:
    n = _scored(r)
    return str(r["moments"]) if n == r["moments"] else f"{n}/{r['moments']}"


def markdown(doc: dict[str, Any]) -> str:
    """The tables: rows are groups, columns configurations, cells
    ``worst / flicker``; then squared error relative to mvgpu."""
    lines: list[str] = []
    summary = doc["summary"]
    for table in sorted(summary, key=lambda t: (TABLE_ORDER + (t,)).index(t)):
        t = summary[table]
        # the configurations that ran on this table's moments, in CONFIGS order
        configs = [c for c in doc["configs"] if any(c in r["cells"] for r in t["rows"].values())]
        lines += [f"### {TABLE_TITLES.get(table, table)}", ""]
        lines.append("worst areas / flicker (1.0 / 0 = the real frame)")
        lines.append("")
        lines.append("| | n | " + " | ".join(configs) + " |")
        lines.append("|---|---|" + "---|" * len(configs))
        rows = list(t["rows"].items())
        short = False
        for row, r in rows:
            cells = []
            for c in configs:
                v = r["cells"].get(c)
                if v is None:
                    cells.append("-")
                    continue
                cells.append(f"{v['worst']:.3f} / {v['flicker']:.2f}")
            short = short or _scored(r) != r["moments"]
            lines.append(f"| {row} | {_count(r)} | " + " | ".join(cells) + " |")
        if t["headline"] is not None:
            h = t["headline"]["cells"]
            cells = [f"**{h[c]['worst']:.3f} / {h[c]['flicker']:.2f}**" if c in h else "-"
                     for c in configs]  # fmt: skip
            lines.append("| **headline** | | " + " | ".join(cells) + " |")
        if short:
            lines += ["", SHORT_NOTE]
        if not any(r["mse_vs_mvgpu"] for _, r in rows):
            lines.append("")
            continue
        lines += ["", "squared error relative to mvgpu, % (lower is better)", ""]
        lines.append("| | " + " | ".join(configs) + " |")
        lines.append("|---|" + "---|" * len(configs))
        for row, r in rows:
            rel = r["mse_vs_mvgpu"]
            cells = [f"{rel[c]:+.1f}" if c in rel else "-" for c in configs]
            lines.append(f"| {row} | " + " | ".join(cells) + " |")
        if t["headline"] is not None:
            rel = t["headline"]["mse_vs_mvgpu"]
            cells = [f"{rel[c]:+.1f}" if c in rel else "-" for c in configs]
            lines.append("| **headline** | " + " | ".join(cells) + " |")
        lines.append("")
    sw, hw = doc["software"], doc["machine"]
    lines.append(
        f"ButterEye {sw.get('buttereye')} ({sw.get('git_rev', '?')[:10]}"
        f"{', modified' if sw.get('git_dirty') else ''}"
        f"{', a frozen copy of the app' if sw.get('buttereye_from') == 'snapshot' else ''}"
        f"{', app sources ' + sw['app_sha256'][:10] if sw.get('app_sha256') else ''}), "
        f"VapourSynth {sw.get('vapoursynth')}, "
        f"TensorRT {sw.get('tensorrt') or '-'}, vs-mlrt {sw.get('vsmlrt') or '-'}; "
        f"{hw.get('gpu') or '?'}, driver {hw.get('driver') or '?'}; {hw.get('os') or '?'}."
    )
    if doc.get("skipped"):
        lines.append("")
        for c, why in doc["skipped"].items():
            short = why if len(why) <= 240 else why[:239] + "..."
            lines.append(f"Not run: {c}: {short}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- provenance


def _cmd(argv: list[str], timeout: float = 20.0) -> str:
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError, subprocess.TimeoutExpired:
        return ""
    return res.stdout if res.returncode == 0 else ""


def machine_info() -> dict[str, Any]:
    info: dict[str, Any] = {"kernel": platform.release(), "python": platform.python_version()}
    smi = _cmd(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"])
    if smi.strip():
        name, _, driver = smi.strip().splitlines()[0].partition(",")
        info["gpu"], info["driver"] = name.strip(), driver.strip()
    vk = _cmd(["vulkaninfo", "--summary"])
    names = [ln.split("=", 1)[1].strip() for ln in vk.splitlines() if "deviceName" in ln]
    if names:
        info["vulkan_devices"] = names
        info.setdefault("gpu", names[0])
    try:
        cpu = Path("/proc/cpuinfo").read_text(encoding="utf-8")
        info["cpu"] = next(
            ln.split(":", 1)[1].strip() for ln in cpu.splitlines() if ln.startswith("model name")
        )
    except OSError, StopIteration:
        info["cpu"] = platform.processor()
    try:
        rel = Path("/etc/os-release").read_text(encoding="utf-8")
        info["os"] = next(
            ln.split("=", 1)[1].strip().strip('"')
            for ln in rel.splitlines()
            if ln.startswith("PRETTY_NAME=")
        )
    except OSError, StopIteration:
        info["os"] = platform.platform()
    return info


def software_info(np: Any, engines: Engines | None, mvgpu_env: dict[str, str]) -> dict[str, Any]:
    info: dict[str, Any] = {"numpy": np.__version__, "mvgpu_env_cleared": mvgpu_env}
    rev = _cmd(["git", "-C", os.fspath(REPO), "rev-parse", "HEAD"]).strip()
    if rev:
        info["git_rev"] = rev
        info["git_dirty"] = bool(_cmd(["git", "-C", os.fspath(REPO), "status", "--porcelain"]))
    ff = _cmd(["ffmpeg", "-hide_banner", "-version"]).splitlines()
    info["ffmpeg"] = ff[0] if ff else ""
    if engines is not None:
        import buttereye

        info["buttereye"] = getattr(buttereye, "__version__", "?")
        where = Path(buttereye.__file__).resolve().parent
        if where == REPO / "buttereye":
            info["buttereye_from"] = "checkout"
        elif where == app_root() / "buttereye":
            info["buttereye_from"] = "snapshot"  # a frozen copy (BUTTEREYE_BENCH_APP)
        else:
            info["buttereye_from"] = os.fspath(where)
        info["app_sha256"] = app_sha256(where)
        info["vapoursynth"] = str(engines.core.core_version)
        info["plugins"] = engines.plugins()
        info["plugin_dir"] = os.fspath(engines.consts.plugin_dir)
        info["engines"] = dict(engines.engines_used)
        info["models"] = dict(engines.models_used)
        if engines.trt_cache:
            from buttereye.core.backends import trt

            inst = trt.find_install(engines.paths)
            if inst is not None:
                info["tensorrt"], info["vsmlrt"] = inst.trt_version, inst.vstrt_version
        info["defaults"] = {
            "sc_threshold": engines.sc_threshold,
            "hold_fill": engines.hold_fill,
            "hold_max": engines.hold_max,
            "still_skip": True,
        }
    return info


def redact(value: Any, home: str | None = None) -> Any:
    """The JSON with the home folder written as ``~`` (nothing personal in it)."""
    home = home if home is not None else os.fspath(Path.home())
    if isinstance(value, str):
        return value.replace(home, "~") if home and home != "/" else value
    if isinstance(value, list):
        return [redact(v, home) for v in value]
    if isinstance(value, dict):
        return {redact(k, home): redact(v, home) for k, v in value.items()}
    return value


def publish_problems(doc: dict[str, Any]) -> list[str]:
    """Why a result file may not be published (empty when it may)."""
    errs: list[str] = []
    for c in doc.get("configs", {}):
        if c not in CONFIGS:
            errs.append(f"configuration {c!r} is not one of the named ones")
    for tier in doc.get("results", {}).values():
        for row in tier.values():
            if "reference" in row:
                errs.append("a third-party reference row is in the results")
                break
    if doc.get("partial"):
        errs.append("the run covered part of the set (--only, --limit or --configs)")
    allowed = set(doc.get("skipped_allowed", []))
    left = [c for c in doc.get("skipped", {}) if c not in allowed]
    if left:
        errs.append("configurations were skipped: " + ", ".join(left))
    failed: dict[str, int] = {}
    for tier in doc.get("results", {}).values():
        for row in tier.values():
            for c, cell in row.items():
                if isinstance(cell, dict) and "error" in cell:
                    failed[c] = failed.get(c, 0) + 1
    if failed:
        errs.append(
            "engines failed on some moments (the tables leave those moments out): "
            + ", ".join(f"{c} on {n}" for c, n in sorted(failed.items()))
        )
    text = json.dumps(doc)
    home = os.fspath(Path.home())
    if home != "/" and home in text:
        errs.append("the file names the home folder")
    return errs


# ---------------------------------------------------------------- images (--dump)


def png_bytes(img: Any) -> bytes:
    """An 8-bit grey (h, w) or RGB (h, w, 3) array as PNG, standard library only."""
    import numpy as np

    h, w = img.shape[:2]
    rows = img.reshape(h, -1)
    raw = np.concatenate([np.zeros((h, 1), dtype=np.uint8), rows], axis=1).tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    colour = 2 if img.ndim == 3 else 0
    head = struct.pack(">IIBBBBB", w, h, 8, colour, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", head)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def yuv_to_rgb(np: Any, y: Any, u: Any, v: Any) -> Any:
    """Limited-range BT.709 4:2:0 to 8-bit RGB (chroma repeated, for viewing)."""
    yf = (y.astype(np.float64) - 16.0) / 219.0
    cb = (np.repeat(np.repeat(u, 2, 0), 2, 1).astype(np.float64) - 128.0) / 224.0
    cr = (np.repeat(np.repeat(v, 2, 0), 2, 1).astype(np.float64) - 128.0) / 224.0
    cb, cr = cb[: y.shape[0], : y.shape[1]], cr[: y.shape[0], : y.shape[1]]
    r = yf + 1.5748 * cr
    g = yf - 0.1873 * cb - 0.4681 * cr
    b = yf + 1.8556 * cb
    return np.clip(np.rint(np.dstack([r, g, b]) * 255.0), 0, 255).astype(np.uint8)


def write_dump(
    np: Any, folder: Path, key: str, out: tuple[Any, ...], real: tuple[Any, ...], st: dict[str, Any]
) -> None:
    """An engine's worst frame, its error (8x the difference from the real
    frame) and a caption with the footage's credit and what was changed."""
    if not st.get("images_ok") or st.get("nc"):
        raise BenchError(f"{key}: this footage's licence allows numbers only")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{key}.png").write_bytes(png_bytes(yuv_to_rgb(np, *out)))
    err = np.clip(np.abs(out[0].astype(np.int16) - real[0].astype(np.int16)) * 8, 0, 255)
    (folder / f"{key}-error.png").write_bytes(png_bytes(err.astype(np.uint8)))
    (folder / f"{key}.txt").write_text(
        f"{st['attribution']}\nLicence: {st['licence']} ({st['licence_url']})\n"
        f"Changes: {st['changes']}; frame made by the engine named in the file name.\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- run


def plan_moments(
    m: dict[str, Any], args: argparse.Namespace
) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[str]]:
    """The manifest moments and cel scenes this run scores, and what was left out."""
    tiers = TIERS if args.tier == "all" else (args.tier,)
    only = {x for x in (args.only or "").split(",") if x}
    by_id = footage_by_id(m)
    notes: list[str] = []
    picked: list[dict[str, Any]] = []
    count: dict[tuple[str, str], int] = {}
    nc_left: set[str] = set()
    for mo in m["moments"]:
        fp = by_id[mo["footage"]]
        if mo["tier"] not in tiers:
            continue
        if only and not ({fp["id"], fp["set"]} & only):
            continue
        if m["sets"][fp["set"]]["nc"] and not args.accept_nc:
            nc_left.add(fp["set"])
            continue
        seen = (fp["id"], mo["tier"])
        count[seen] = count.get(seen, 0) + 1
        if args.limit is not None and count[seen] > args.limit:
            continue
        picked.append(mo)
    for name in sorted(nc_left):
        notes.append(f"{name}: left out (non-commercial licence; --accept-nc)")
    cel: list[tuple[str, str]] = []
    if "2x" in tiers and (not only or "cel" in only or {f"cel-{s}" for s in CEL_SCENES} & only):
        scenes = [s for s in CEL_SCENES if not only or "cel" in only or f"cel-{s}" in only]
        for s in scenes:
            for k, v in enumerate(CEL_VARIANTS):
                if args.limit is None or k < args.limit:
                    cel.append((s, v))
    return picked, cel, notes


def cmd_run(args: argparse.Namespace) -> int:
    import numpy as np

    if not args.no_memcap:
        short = memguard().preflight()
        if short is not None:
            raise BenchError(f"not enough memory to start: {short}")
        argv = [sys.executable, os.fspath(Path(__file__).resolve()), *args.argv]
        why = memguard().reexec_capped(argv, args.mem_max, "pubbench")
        if not memguard().capped():
            log(f"memory failsafe: no hard cap ({why}); watchdog only")
        memguard().Watchdog(label="public_bench").start()
    if args.nice:
        os.nice(args.nice)
    m = load_manifest(args.manifest)
    errs = validate(m)
    if errs:
        raise BenchError("manifest: " + "; ".join(errs))
    names = [c for c in (args.configs or ",".join(CONFIGS)).split(",") if c]
    unknown = [c for c in names if c not in CONFIGS]
    if unknown:
        raise BenchError(f"unknown configuration {', '.join(unknown)} (have {', '.join(CONFIGS)})")
    mvgpu_env = {k: os.environ.pop(k) for k in sorted(os.environ) if k.startswith("MVGPU_")}
    if mvgpu_env:
        log("cleared for the run: " + ", ".join(f"{k}={v}" for k, v in mvgpu_env.items()))
    root = cache_root()
    picked, cel, notes = plan_moments(m, args)
    for n in notes:
        log(n)
    engines = Engines(np, build=not args.no_build, hold_fill=args.hold_fill == "on",
                      cache_mb=args.cache_mb, threads=args.vs_threads)  # fmt: skip
    skipped: dict[str, str] = {}
    for c in names:
        try:
            engines.check(c)
        except BenchError as exc:
            skipped[c] = str(exc)
            log(f"{c}: not run: {exc}")
    configs = [c for c in names if c not in skipped]
    ud: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, dict[str, Any]]] = {t: {} for t in TIERS}
    moments: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    dumps: dict[tuple[str, str], tuple[float, str, tuple[Any, ...], tuple[Any, ...], str]] = {}
    planes = 3 if args.dump else 1
    t0 = time.monotonic()
    todo: list[dict[str, Any] | tuple[str, str]] = [*picked, *cel]
    for i, item in enumerate(todo):
        tm = time.monotonic()
        mo = (
            load_moment(np, m, item, root)
            if isinstance(item, dict)
            else cel_moment(np, root, item[0], item[1])
        )
        warnings += mo.warnings
        moments[mo.id] = mo.meta()
        row: dict[str, dict[str, Any]] = {}
        for c in configs:
            if mo.tier not in CONFIGS[c]["tiers"]:
                continue
            if mo.group not in CONFIGS[c].get("groups", GROUPS):
                continue
            try:
                if CONFIGS[c]["backend"] is not None:
                    ud.setdefault(c, {})[f"x{mo.m}/{mo.chroma_loc}"] = json.loads(
                        engines.user_data(c, mo.m, mo.chroma_loc)
                    )
                outs = engines.run(c, mo, planes)
                sc = score_moment(engines.truth, np, outs, mo)
            except Exception as exc:  # noqa: BLE001 - one engine failing is a result
                row[c] = {"error": f"{type(exc).__name__}: {exc}"}
                log(f"  {mo.id} / {c}: {row[c]['error']}")
                continue
            k = sc.pop("worst_frame")
            row[c] = sc
            if args.dump and mo.images_ok:
                slot = (mo.group, c)
                if slot not in dumps or sc["worst"] < dumps[slot][0]:
                    j = [o for o, _ in mo.truths].index(k)
                    dumps[slot] = (sc["worst"], f"{mo.id}-o{k}", outs[j], mo.truths[j][1], mo.set)
        results[mo.tier][mo.id] = row
        cells = " ".join(
            f"{c} {row[c]['worst']:.3f}" if "error" not in row[c] else f"{c} ERR" for c in row
        )
        log(f"[{i + 1}/{len(todo)}] {mo.id}: {cells} ({time.monotonic() - tm:.1f} s)")
        del mo
    flat = {mid: r for tier in results.values() for mid, r in tier.items()}
    doc: dict[str, Any] = {
        "schema": 1,
        "set_version": m.get("set_version"),
        "manifest_sha256": sha256_file(args.manifest),
        "moments_selected": m.get("moments_selected", False),
        "date": time.strftime("%Y-%m-%d"),
        "partial": bool(args.only or args.limit is not None or args.configs),
        "machine": machine_info(),
        "software": software_info(np, engines, mvgpu_env),
        "configs": {c: _config_entry(c, engines, ud) for c in configs},
        "skipped": skipped,
        "skipped_allowed": _allowed(args, skipped),
        "moments": moments,
        "results": {t: r for t, r in results.items() if r},
        "summary": summarize(flat, moments, configs),
        "warnings": warnings,
        "seconds": round(time.monotonic() - t0, 1),
    }
    if args.dump:
        for (group, c), (_, key, out, real, set_name) in sorted(dumps.items()):
            write_dump(np, args.dump, f"{group}-{c}-{key}", out, real, m["sets"][set_name])
    doc = redact(doc)
    text = markdown(doc)
    print(text)
    json_path = args.json or root / "results" / f"run-{time.strftime('%Y%m%d-%H%M%S')}.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    log(f"results: {json_path}")
    if args.md is not None:
        args.md.write_text(text, encoding="utf-8")
    if args.publish:
        publish(doc)
    return show_compare(args, doc)


def _allowed(args: argparse.Namespace, skipped: dict[str, str]) -> list[str]:
    """The skipped configurations the maintainer let a publication go without
    (``--allow-skipped``); they stay listed as not run, with the reason."""
    names = {c for c in (getattr(args, "allow_skipped", None) or "").split(",") if c}
    return sorted(names & set(skipped))


def publish_name(folder: Path, date: str) -> str:
    """``results-<date>.json``, or with b, c, ... after the date when that day
    already has a published result (published files are never overwritten)."""
    for suffix in ["", *"bcdefghijklmnopqrstuvwxyz"]:
        name = f"results-{date}{suffix}.json"
        if not (folder / name).exists():
            return name
    raise BenchError(f"not published: too many results dated {date}")


def published_as(doc: dict[str, Any], folder: Path | None = None) -> str | None:
    """The name of the published file that holds exactly ``doc``, if any."""
    folder = folder or REPO / "docs" / "benchmark"
    for p in sorted(folder.glob(f"results-{doc['date']}*.json")):
        try:
            if json.loads(p.read_text(encoding="utf-8")) == doc:
                return p.name
        except OSError, ValueError:
            continue
    return None


def publish(doc: dict[str, Any], folder: Path | None = None) -> Path:
    """Copy a result to ``docs/benchmark/results-<date>.json`` (``<date>b`` and
    so on when that name is taken); refuses what ``publish_problems`` names."""
    problems = publish_problems(doc)
    if problems:
        raise BenchError("not published: " + "; ".join(problems))
    folder = folder or REPO / "docs" / "benchmark"
    dest = folder / publish_name(folder, doc["date"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    log(f"published: {dest}")
    return dest


MERGE_SAME = ("schema", "set_version", "manifest_sha256", "moments_selected", "partial")


def merge_results(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """One result from runs of different tiers (each GPU hold stays short).
    The runs must be of the same manifest, machine and software, run the same
    configurations with the same ``user_data`` and cover different tiers."""
    if len(docs) == 1:
        # the summary is made anew, so a result written by an older summarize
        # gets the same tables as a fresh one
        out = copy.deepcopy(docs[0])
        flat = {mid: r for tier in out.get("results", {}).values() for mid, r in tier.items()}
        out["summary"] = summarize(flat, out["moments"], list(out["configs"]))
        return out
    first = docs[0]
    out = copy.deepcopy(first)
    for d in docs[1:]:
        for k in MERGE_SAME:
            if d.get(k) != first.get(k):
                raise BenchError(f"cannot merge: {k} differs between the runs")
        for k in ("machine", "software"):
            if d.get(k) != first.get(k):
                diff = sorted(
                    x for x in set(d[k]) | set(first[k]) if d[k].get(x) != first[k].get(x)
                )
                raise BenchError(f"cannot merge: {k} differs ({', '.join(diff)})")
        overlap = set(d.get("results", {})) & set(out.get("results", {}))
        if overlap:
            raise BenchError(f"cannot merge: both runs scored tier {', '.join(sorted(overlap))}")
        for c, spec in d.get("configs", {}).items():
            have = out["configs"].setdefault(c, copy.deepcopy(spec))
            for key, ud in spec.get("user_data", {}).items():
                if have["user_data"].setdefault(key, ud) != ud:
                    raise BenchError(f"cannot merge: {c} ran with other settings ({key})")
        for c, why in d.get("skipped", {}).items():
            out["skipped"].setdefault(c, why)
        out["skipped_allowed"] = sorted(
            set(out.get("skipped_allowed", [])) | set(d.get("skipped_allowed", []))
        )
        for c in list(out["skipped"]):
            if c in out["configs"]:
                del out["skipped"][c]  # skipped in one tier only because it has no rows there
        out["moments"].update(d.get("moments", {}))
        out["results"].update(copy.deepcopy(d.get("results", {})))
        out["warnings"] = [*out.get("warnings", []), *d.get("warnings", [])]
        out["seconds"] = round(out.get("seconds", 0) + d.get("seconds", 0), 1)
        out["date"] = max(out["date"], d["date"])
    order = [c for c in CONFIGS if c in out["configs"]] + sorted(set(out["configs"]) - set(CONFIGS))
    out["configs"] = {c: out["configs"][c] for c in order}
    out["results"] = {t: out["results"][t] for t in TIERS if t in out["results"]}
    flat = {mid: r for tier in out["results"].values() for mid, r in tier.items()}
    out["summary"] = summarize(flat, out["moments"], list(out["configs"]))
    return out


def _spec(name: str) -> dict[str, Any]:
    return {k: (list(v) if isinstance(v, tuple) else v) for k, v in CONFIGS[name].items()}


def _config_entry(name: str, engines: Engines, ud: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entry: dict[str, Any] = {"spec": _spec(name)}
    if name in engines.resolved:
        got = engines.resolved[name]
        entry["resolved"] = {k: got[k] for k in ("backend", "model") if got.get(k)}
    entry["user_data"] = ud.get(name, {})
    return entry


_MEMGUARD: list[Any] = []


def memguard() -> Any:
    """``tools/memguard.py``: refuse when memory is short, a capped scope, a watchdog."""
    if not _MEMGUARD:
        if os.fspath(HERE.parent) not in sys.path:
            sys.path.insert(0, os.fspath(HERE.parent))
        import memguard as mg  # type: ignore[import-not-found]

        _MEMGUARD.append(mg)
    return _MEMGUARD[0]


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What moved between two result files: per table, row and configuration
    the change in worst areas, flicker and squared error (in %), and the moments
    or configurations only one of them has."""
    out: dict[str, Any] = {"tables": {}, "only_old": [], "only_new": []}
    so, sn = old.get("summary", {}), new.get("summary", {})
    for table in [t for t in TABLE_ORDER if t in so or t in sn] + sorted(
        (set(so) | set(sn)) - set(TABLE_ORDER)
    ):
        rows: dict[str, Any] = {}
        ro = so.get(table, {}).get("rows", {})
        rn = sn.get(table, {}).get("rows", {})
        pairs = [(r, ro.get(r, {}).get("cells", {}), rn.get(r, {}).get("cells", {})) for r in rn]
        ho, hn = (so.get(table) or {}).get("headline"), (sn.get(table) or {}).get("headline")
        if ho and hn:
            pairs.append(("headline", ho["cells"], hn["cells"]))
        for row, co, cn in pairs:
            cells: dict[str, Any] = {}
            for c in cn:
                if c not in co:
                    continue
                a, b = co[c], cn[c]
                cells[c] = {
                    "worst": round(b["worst"] - a["worst"], 4),
                    "flicker": round(b["flicker"] - a["flicker"], 3),
                    "mse_pct": round(100.0 * (b["mse"] / a["mse"] - 1.0), 1) if a["mse"] else None,
                }
            if cells:
                rows[row] = cells
        out["tables"][table] = rows
    mo, mn = set(old.get("moments", {})), set(new.get("moments", {}))
    out["only_old"] = sorted(mo - mn)
    out["only_new"] = sorted(mn - mo)
    co_, cn_ = set(old.get("configs", {})), set(new.get("configs", {}))
    out["configs_only_old"] = sorted(co_ - cn_)
    out["configs_only_new"] = sorted(cn_ - co_)
    return out


def compare_text(diff: dict[str, Any]) -> str:
    lines: list[str] = []
    for table, rows in diff["tables"].items():
        if not rows:
            continue
        configs = sorted({c for cells in rows.values() for c in cells}, key=_config_order)
        lines += [f"### {TABLE_TITLES.get(table, table)}", ""]
        lines.append("change in worst areas / squared error (%)")
        lines.append("")
        lines.append("| | " + " | ".join(configs) + " |")
        lines.append("|---|" + "---|" * len(configs))
        for row, cells in rows.items():
            out = []
            for c in configs:
                v = cells.get(c)
                if v is None:
                    out.append("-")
                    continue
                pct = "-" if v["mse_pct"] is None else f"{v['mse_pct']:+.1f}"
                out.append(f"{v['worst']:+.4f} / {pct}")
            lines.append(f"| {row} | " + " | ".join(out) + " |")
        lines.append("")
    for key, what in (
        ("only_old", "moments only in the old file"),
        ("only_new", "moments only in the new file"),
        ("configs_only_old", "configurations only in the old file"),
        ("configs_only_new", "configurations only in the new file"),
    ):
        if diff.get(key):
            lines.append(f"{what}: " + ", ".join(diff[key]))
    return "\n".join(lines) + "\n"


def _config_order(name: str) -> tuple[int, str]:
    names = list(CONFIGS)
    return (names.index(name) if name in names else len(names), name)


def cmd_compare(args: argparse.Namespace) -> int:
    old = json.loads(args.old.read_text(encoding="utf-8"))
    new = json.loads(args.new.read_text(encoding="utf-8"))
    diff = compare(old, new)
    print(compare_text(diff), end="")
    if args.fail_over is not None:
        worst = max(
            (
                abs(v["worst"])
                for rows in diff["tables"].values()
                for cells in rows.values()
                for v in cells.values()
            ),
            default=0.0,
        )
        if worst > args.fail_over:
            log(f"largest change in worst areas {worst:.4f} > {args.fail_over}")
            return 1
    return 0


def footage_rows(m: dict[str, Any]) -> str:
    """The footage table of the docs: one row per set, with its licence, the
    exact credit line, what was changed and whether images may be shown."""
    rows = ["| set | licence | credit | what was changed | images |", "|---|---|---|---|---|"]
    for st in m["sets"].values():
        lic = f"[{st['licence']}]({st['licence_url']})"
        images = "yes" if st["images_ok"] else "numbers only"
        rows.append(f"| {st['title']} | {lic} | {st['attribution']} | {st['changes']} | {images} |")
    return "\n".join(rows) + "\n"


def fill_docs(
    text: str, doc: dict[str, Any], m: dict[str, Any], result_name: str | None = None
) -> str:
    """``text`` with the parts between the ``<!-- results:start -->`` /
    ``<!-- footage:start -->`` markers (and their ``end``) written anew;
    ``result_name`` is the published file the tables come from."""

    def put(text: str, name: str, body: str) -> str:
        a, b = f"<!-- {name}:start -->\n", f"<!-- {name}:end -->"
        if a not in text or b not in text:
            raise BenchError(f"the docs have no {name} markers")
        head, rest = text.split(a, 1)
        return head + a + body + b + rest.split(b, 1)[1]

    counts = {t: len(r) for t, r in doc.get("results", {}).items()}
    result_name = result_name or f"results-{doc['date']}.json"
    lead = (
        f"Measured {doc['date']}, {sum(counts.values())} moments "
        f"({', '.join(f'{n} in the {t} tier' for t, n in counts.items())}); "
        f"result file `docs/benchmark/{result_name}`.\n\n"
    )
    return put(put(text, "results", lead + markdown(doc)), "footage", footage_rows(m))


def cmd_report(args: argparse.Namespace) -> int:
    docs = [json.loads(p.read_text(encoding="utf-8")) for p in args.results]
    doc = merge_results(docs)
    doc["skipped_allowed"] = sorted(
        set(doc.get("skipped_allowed", [])) | set(_allowed(args, doc.get("skipped", {})))
    )
    text = markdown(doc)
    if args.md is not None:
        args.md.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    if args.json is not None:
        args.json.write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    name = publish(doc).name if args.publish else published_as(doc)
    if args.docs is not None:
        m = load_manifest(args.manifest)
        text = fill_docs(args.docs.read_text(encoding="utf-8"), doc, m, name)
        args.docs.write_text(text, "utf-8")
        log(f"tables written into {args.docs}")
    return show_compare(args, doc)


def show_compare(args: argparse.Namespace, doc: dict[str, Any]) -> int:
    """``--compare OLD``: what moved against an earlier result, after the tables."""
    if getattr(args, "compare", None) is None:
        return 0
    old = json.loads(args.compare.read_text(encoding="utf-8"))
    print(f"\n## Against {args.compare.name}\n")
    print(compare_text(compare(old, doc)), end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    m = load_manifest(args.manifest)
    errs = validate(m)
    for e in errs:
        print("error:", e)
    root = cache_root()
    for fp in m["footage"]:
        mos = moments_of(m, fp["id"])
        have = sum(moment_path(root, fp["id"], f).exists() for f in slots(m, fp["id"]))
        unpinned = sum(mo.get("decoded_sha256") is None for mo in mos)
        print(
            f"{fp['id']:24} {fp['set']:9} {len(mos):3} moments, {have:3} prepared, "
            f"{unpinned:3} decoded hashes unpinned"
        )
    return 1 if errs else 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument("--manifest", type=Path, default=MANIFEST)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="download the footage")
    f.add_argument("--tier", choices=("core", "calib"), default="core")
    f.add_argument(
        "--accept-nc", action="store_true", help="also fetch footage under a non-commercial licence"
    )
    f.add_argument("--only", help="comma-separated footage or set ids")
    f.add_argument("--pin", action="store_true", help="write empty hashes into the manifest")
    p = sub.add_parser("prepare", help="decode the moments into .npz files")
    p.add_argument("--accept-nc", action="store_true")
    p.add_argument("--only")
    p.add_argument("--limit", type=int, help="only the first N moment starts per footage")
    p.add_argument("--threads", type=int, default=6)
    p.add_argument("--nice", type=int, default=19)
    p.add_argument("--pin", action="store_true")
    r = sub.add_parser("run", help="score the configurations on the prepared moments")
    r.add_argument("--tier", choices=("2x", "display", "all"), default="all")
    r.add_argument("--configs", help="comma-separated names (default: all of CONFIGS)")
    r.add_argument("--accept-nc", action="store_true", help="also score non-commercial footage")
    r.add_argument("--only", help="comma-separated footage or set ids ('cel' for the 2D scenes)")
    r.add_argument("--limit", type=int, help="only the first N moments per footage and tier")
    r.add_argument("--json", type=Path, help="where the results go (default: the cache)")
    r.add_argument("--md", type=Path, help="also write the tables here")
    r.add_argument("--dump", type=Path, help="worst frames as PNG (images-allowed footage only)")
    r.add_argument("--no-build", action="store_true", help="skip TensorRT engines not built")
    r.add_argument("--hold-fill", choices=("on", "off"), default="on")
    r.add_argument("--publish", action="store_true", help="copy the JSON to docs/benchmark/")
    r.add_argument("--allow-skipped", help="publish although these configurations did not run")
    r.add_argument("--compare", type=Path, help="then show what moved against this result")
    r.add_argument("--mem-max", type=int, default=8192, help="memory cap in MiB (memguard)")
    r.add_argument("--no-memcap", action="store_true", help=argparse.SUPPRESS)
    r.add_argument("--cache-mb", type=int, default=1024, help="VapourSynth frame cache")
    r.add_argument("--vs-threads", type=int, default=8, help="VapourSynth threads")
    r.add_argument("--nice", type=int, default=10)
    s = sub.add_parser("select", help="maintainer: check and pin every moment's start")
    s.add_argument("--pin", action="store_true", help="required: rewrites the manifest")
    s.add_argument("--accept-nc", action="store_true")
    s.add_argument("--only")
    s.add_argument("--min-motion", type=float, default=MIN_MOTION)
    s.add_argument("--threads", type=int, default=6)
    s.add_argument("--nice", type=int, default=19)
    s.add_argument("--report", type=Path, help="write what was tried per moment (JSON)")
    c = sub.add_parser("compare", help="what changed between two result files")
    c.add_argument("old", type=Path)
    c.add_argument("new", type=Path)
    c.add_argument("--fail-over", type=float, help="exit 1 when worst areas move more than this")
    w = sub.add_parser("report", help="the tables of a result file, as markdown")
    w.add_argument("results", type=Path, nargs="+", help="one result, or runs of other tiers")
    w.add_argument("--md", type=Path, help="write here instead of printing")
    w.add_argument("--json", type=Path, help="write the (merged) result here")
    w.add_argument("--docs", type=Path, help="write the tables into this page's markers")
    w.add_argument("--publish", action="store_true", help="copy it to docs/benchmark/")
    w.add_argument("--allow-skipped", help="publish although these configurations did not run")
    w.add_argument("--compare", type=Path, help="then show what moved against this result")
    sub.add_parser("check", help="validate the manifest and list the cache")
    args = ap.parse_args(argv)
    args.argv = list(argv) if argv is not None else sys.argv[1:]
    try:
        if args.cmd == "fetch":
            return cmd_fetch(args)
        if args.cmd == "prepare":
            return cmd_prepare(args)
        if args.cmd == "run":
            return cmd_run(args)
        if args.cmd == "select":
            return cmd_select(args)
        if args.cmd == "compare":
            return cmd_compare(args)
        if args.cmd == "report":
            return cmd_report(args)
        return cmd_check(args)
    except BenchError as exc:
        log(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
