# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Is this video hand-drawn 2D animation? (``ContentKind.AUTO``, docs/spikes/m0w.md)

Hand-drawn animation holds each drawing for two or three frames ("on twos", "on
threes"); camera footage and 3D rendering change on every frame. That is the one
sign that told them apart on real files: the share of frame pairs that repeat a
drawing inside a moving passage was 16 to 65 % on 2D titles and 0 to 7 % on live
action and 3D, while how many colours a frame uses did not separate them at all.

The probe reads a few short runs of frames spread over the file with ffmpeg, at
a small size, and lets ffmpeg measure how much each frame differs from the one
before. No picture reaches Python and nothing is written. It answers for the
whole file, once: a 2D profile changes which engines are used, which cannot
follow the picture from scene to scene. The answer is kept (``Cache``) under a
key that holds no file name.

A 2D title that moves on every frame (full animation, long pans) is not found,
and neither is a file ffmpeg cannot read here: both play as before, and the
user's own choice (2D or 3D in the window) always wins over the probe.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from buttereye.core import osapi
from buttereye.core.doctor import proc
from buttereye.core.types import ContentKind

_log = logging.getLogger(__name__)

#: where in the file the runs start (shares of its length): past the opening
#: titles, before the end credits
SPOTS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)
#: frames read per run (one less pair), and the size they are measured at
FRAMES = 49
SIZE = (320, 180)
#: a held drawing, as the script finds it (template.vpy DUP_*), on ffmpeg's
#: 0..255 scale: nearly no change, or (noisy sources) little change and under a
#: quarter of the change around it
DUP_ABS, DUP_NOISY, DUP_RATIO = 0.0015 * 255, 0.012 * 255, 4.0
#: a pair is judged only where something moves within two frames of it: a still
#: shot repeats frames in any kind of video
MOVING = 0.004 * 255
#: 2D animation from this share of held pairs up, once this many pairs were
#: judged. On 19 library videos with this probe: eight 2D titles 16-65 %, seven
#: live-action and three 3D ones 0-7 %; a ninth 2D title, a feature film
#: animated on almost every frame, had 10 % and is not found
HELD_SHARE = 0.12
MIN_JUDGED = 40
#: repeats at a fixed step of at least this many pairs, at least this many times
#: in one run, are a frame-rate conversion (``converted``)
CONVERTED_STEP = 4
CONVERTED_MIN = 4
#: film and television rates only: drawings on twos mean nothing in a 60 fps file
MAX_FPS = 31.0
#: shorter than this there is no room for the runs
MIN_DURATION_S = 20.0
RUN_TIMEOUT_S = 8.0
CACHE_FILE = "content.json"
CACHE_MAX = 500

_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


@dataclass(frozen=True, slots=True)
class Found:
    """What the probe says. ``kind`` None: too little moved in the runs to tell."""

    kind: ContentKind | None
    share: float  # held pairs among the judged ones (0 when none)
    judged: int


def converted(at: Sequence[int]) -> bool:
    """Held pairs at ``at`` (indices in one run) that a frame-rate conversion made,
    not a drawing: each one alone, at a fixed step of four or more pairs (24 → 30
    fps repeats every fifth frame, 25 → 30 every sixth). Drawings held on twos or
    threes come in steps of two or three, and animation never keeps one step
    that long."""
    if len(at) < CONVERTED_MIN:
        return False
    gaps = [b - a for a, b in zip(at, at[1:], strict=False)]
    return min(gaps) >= CONVERTED_STEP and max(gaps) - min(gaps) <= 1


def judge(runs: Sequence[Sequence[float]]) -> Found:
    """``runs``: for each run, how much every frame differs from the one before
    (mean absolute difference, 0..255)."""
    held = judged = 0
    for d in runs:
        at: list[int] = []
        for i, x in enumerate(d):
            around = [d[j] for j in (i - 2, i - 1, i + 1, i + 2) if 0 <= j < len(d)]
            most = max(around, default=0.0)
            if max(most, x) < MOVING:
                continue  # a still shot: nothing moves here or within two frames
            judged += 1
            if x < DUP_ABS or (x < DUP_NOISY and x * DUP_RATIO < most):
                at.append(i)
        if not converted(at):
            held += len(at)
    share = held / judged if judged else 0.0
    if judged < MIN_JUDGED:
        return Found(None, share, judged)
    return Found(ContentKind.ANIMATION if share >= HELD_SHARE else ContentKind.LIVE, share, judged)


def parse(text: str) -> list[float]:
    """The differences ffmpeg printed for one run."""
    return [float(m) for m in _YAVG.findall(text)]


def command(ffmpeg: str, path: str, at_s: float) -> list[str]:
    """One run: ``FRAMES`` frames from the keyframe before ``at_s``, small and
    grey, each turned into its difference from the one before, whose mean is
    printed. Nothing is encoded or written."""
    chain = (
        f"scale={SIZE[0]}:{SIZE[1]}:flags=bilinear,format=gray,"
        "tblend=all_mode=difference,signalstats,"
        "metadata=mode=print:key=lavfi.signalstats.YAVG:file=-"
    )
    return [
        ffmpeg, "-v", "error", "-nostdin", "-noaccurate_seek", "-ss", f"{at_s:.2f}",
        "-i", path, "-map", "0:v:0", "-an", "-sn", "-dn", "-frames:v", str(FRAMES),
        "-vf", chain, "-f", "null", "-",
    ]  # fmt: skip


def local_film(path: str, fps: float) -> bool:
    """A local file at a film or television rate: the only kind worth a look."""
    if not path or "://" in path or fps <= 0 or fps > MAX_FPS:
        return False
    try:
        return Path(path).is_file()
    except OSError:
        return False


def worth_probing(path: str, fps: float, duration_s: float | None) -> bool:
    """``local_film``, and long enough for the runs."""
    return local_film(path, fps) and duration_s is not None and duration_s >= MIN_DURATION_S


async def probe(path: str, duration_s: float, *, ffmpeg: str | None = None) -> Found | None:
    """Look at ``path``. None when ffmpeg is missing or could not read it."""
    tool = ffmpeg or osapi.executable("ffmpeg")
    if tool is None:
        return None
    results = await asyncio.gather(
        *(
            proc.run(command(tool, path, duration_s * share), timeout_s=RUN_TIMEOUT_S)
            for share in SPOTS
        )
    )
    runs = [parse(r.stdout) for r in results if r.ok]
    if not runs:
        return None
    return judge(runs)


# ---------------------------------------------------------------- remembered answers
def file_key(path: str) -> str | None:
    """A key for this exact file that holds no name: the path, its size and its
    modification time, hashed."""
    try:
        st = os.stat(path)
        real = os.path.realpath(path)
    except OSError:
        return None
    text = f"{real}\0{st.st_size}\0{st.st_mtime_ns}"
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:32]


class Cache:
    """Answers already found, in ``<state>/content.json`` (at most ``CACHE_MAX``,
    oldest dropped). A file that cannot be read or written only costs the probe
    again."""

    def __init__(self, file: Path) -> None:
        self.file = file
        self.entries: dict[str, dict[str, object]] = {}
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                self.entries = {
                    k: v for k, v in entries.items() if isinstance(k, str) and isinstance(v, dict)
                }
        except OSError, ValueError:
            pass

    def get(self, key: str | None) -> Found | None:
        e = self.entries.get(key or "")
        if e is None:
            return None
        kind, share, judged = e.get("kind"), e.get("share"), e.get("judged")
        if kind not in (None, ContentKind.ANIMATION.value, ContentKind.LIVE.value):
            return None
        if not isinstance(share, (int, float)) or not isinstance(judged, int):
            return None
        return Found(ContentKind(kind) if isinstance(kind, str) else None, float(share), judged)

    def put(self, key: str | None, found: Found) -> None:
        if key is None:
            return
        self.entries[key] = {
            "kind": found.kind.value if found.kind is not None else None,
            "share": round(found.share, 4),
            "judged": found.judged,
            "at": int(time.time()),
        }
        # another core (the GUI and a `buttereye play`) may have saved answers
        # since this one loaded: keep theirs, ours win for the same file
        self.entries = {**Cache(self.file).entries, **self.entries}
        if len(self.entries) > CACHE_MAX:
            oldest = sorted(self.entries, key=lambda k: _at(self.entries[k]))
            for k in oldest[: len(self.entries) - CACHE_MAX]:
                del self.entries[k]
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_name(f"{self.file.name}.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps({"schema": 1, "entries": self.entries}, indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.file)
        except OSError as exc:
            _log.info("content answers not saved: %s", exc)


def _at(entry: dict[str, object]) -> int:
    at = entry.get("at")
    return at if isinstance(at, int) else 0


__all__ = [
    "CACHE_FILE",
    "CONVERTED_MIN",
    "CONVERTED_STEP",
    "HELD_SHARE",
    "MAX_FPS",
    "MIN_JUDGED",
    "Cache",
    "Found",
    "command",
    "converted",
    "file_key",
    "judge",
    "local_film",
    "parse",
    "probe",
    "worth_probing",
]
