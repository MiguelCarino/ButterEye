# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""On-screen pacing and video delay of driver-trial runs (``nvtrial_run.py --probe DIR
--timing --probe-top DIR``).

Reads two logs of the timing layer:

- ``frames.csv``, the layer below the driver's present layer: every present that
  reaches the driver (mpv's and the driver's own), with its present id (``P`` rows)
  and the time its first pixel left for the screen (``T`` rows, present timing,
  stage first-pixel-out, calibrated against CLOCK_MONOTONIC by the ``C`` rows);
- ``top.csv``, a renamed copy of the layer above the driver's layer: the CPU times
  of mpv's own presents.

Per run, over the steady window only, on the fullscreen swapchain:

- presents per second that reached the driver, and how many were never shown;
- screen intervals between presents with consecutive ids, in refreshes: a gap of
  two refreshes or more is a refresh where the picture should have moved on and
  did not ("held"); the drift of a 143.856 fps stream on a 144 Hz screen holds one
  refresh about every 7 s, and is counted apart;
- the spread of on-screen times against the content's own times (the k-th mpv
  frame at k/rate; the driver's frame half-way between two): the residual of a
  straight-line fit, its standard deviation and 99th percentile;
- the video delay: from mpv's present call to the first pixel of that picture on
  screen. With the driver's frames on, each mpv present is followed by two
  presents below the driver's layer: first the frame made half-way to it, then
  mpv's own picture (checked against grim captures; see m0y). mpv's picture is the
  last present before mpv's next one.

usage: nvtrial_screen.py [--json OUT] RUN [RUN ...]
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from pathlib import Path

import numpy as np

RUNS = Path.home() / ".cache/buttereye-dev/nvtrial/runs"


def _rows(path: Path, tag: str) -> list[list[str]]:
    if not path.exists():
        return []
    return [ln.rstrip("\n").split(",") for ln in path.open() if ln.startswith(tag + ",")]


def analyse(d: Path) -> dict[str, object]:
    facts = json.loads((d / "facts.json").read_text())
    m0 = float(facts["mono_steady"])
    span = float(facts.get("steady_s") or 0)
    p_rows = _rows(d / "frames.csv", "P")
    if not p_rows:
        return {"run": d.name, "error": "no frames.csv"}
    sc = Counter(r[2] for r in p_rows).most_common(1)[0][0]
    p_rows = [r for r in p_rows if r[2] == sc]
    fp_by_id = {int(r[2]): int(r[3]) for r in _rows(d / "frames.csv", "T") if r[1] == sc}
    cal = [r for r in _rows(d / "frames.csv", "C") if r[1] == sc]
    offset = (int(cal[-1][3]) - int(cal[-1][2])) / 1e9 if cal else 0.0
    call = np.array([int(r[1]) for r in p_rows]) / 1e9
    ids = np.array([int(r[5]) for r in p_rows])
    fp = np.array([fp_by_id.get(int(i), -1) for i in ids], dtype=np.float64)
    known = fp >= 0
    shown = fp > 0
    fp = np.where(shown, fp / 1e9 + offset, np.nan)
    win = (call >= m0) & (call <= m0 + span)
    refresh = 1 / 144
    res: dict[str, object] = {
        "run": d.name,
        "arm": d.name.split("_")[1] if d.name.count("_") >= 2 else d.name,
        "steady_s": round(span, 1),
        "presents_per_s": round(int(win.sum()) / span, 3) if span else None,
        "timing_results": int((win & known).sum()),
        "never_shown": int((win & known & ~shown).sum()),
    }
    # screen intervals between consecutive present ids, both shown, in the window
    w = np.flatnonzero(win & shown)
    pairs = [(a, b) for a, b in zip(w, w[1:], strict=False) if ids[b] == ids[a] + 1]
    if len(pairs) < 20:
        return res | {"error": "too few timed presents"}
    gaps = np.array([fp[b] - fp[a] for a, b in pairs])
    slots = np.rint(gaps / refresh).astype(int)
    held = int(np.maximum(slots - 1, 0).sum())
    hist = Counter(slots.tolist())
    minutes = span / 60
    res |= {
        "screen_gap_ms_p1_p50_p99": [
            round(float(np.percentile(gaps, q)) * 1000, 3) for q in (1, 50, 99)
        ],  # fmt: skip
        "slots_hist": {int(k): int(v) for k, v in sorted(hist.items())},
        "held": held,
        "held_per_min": round(held / minutes, 1),
        "same_refresh": int((slots == 0).sum()),
    }
    # mpv's presents above the driver's layer
    top = _rows(d / "top.csv", "P")
    if top:
        tsc = Counter(r[2] for r in top).most_common(1)[0][0]
        tt = np.array([int(r[1]) for r in top if r[2] == tsc]) / 1e9
        twin = tt[(tt >= m0) & (tt <= m0 + span)]
        res["mpv_presents_per_s"] = round(len(twin) / span, 3)
        # the presents below that follow each mpv present, up to its next one
        owner = np.searchsorted(tt, call, side="right") - 1
        groups: dict[int, list[int]] = {}
        for i in np.flatnonzero(win):
            if owner[i] >= 0:
                groups.setdefault(int(owner[i]), []).append(int(i))
        sizes = Counter(len(v) for v in groups.values())
        res["presents_per_mpv_present"] = {int(k): int(v) for k, v in sorted(sizes.items())}
        lat, lat_made = [], []
        content_t, screen_t = [], []
        rate = len(twin) / span if span else 0.0
        for k, members in sorted(groups.items()):
            real = members[-1]
            if shown[real]:
                lat.append(fp[real] - tt[k])
                content_t.append(k / rate)
                screen_t.append(fp[real])
            for m in members[:-1]:
                if shown[m]:
                    lat_made.append(fp[m] - tt[k])
                    content_t.append((k - 0.5) / rate)
                    screen_t.append(fp[m])
        if lat:
            a = np.array(lat) * 1000
            res["delay_ms_p1_p50_p99"] = [round(float(np.percentile(a, q)), 2) for q in (1, 50, 99)]
            res["delay_ms_mean"] = round(float(a.mean()), 2)
        if lat_made:
            a = np.array(lat_made) * 1000
            res["made_delay_ms_p50"] = round(float(np.median(a)), 2)
        if len(content_t) > 20:
            c, s = np.array(content_t), np.array(screen_t)
            o = np.argsort(c)
            c, s = c[o], s[o]
            fit = np.polyfit(c, s, 1)
            r = (s - np.polyval(fit, c)) * 1000
            res["residual_ms_sd"] = round(float(r.std()), 2)
            res["residual_ms_p99_abs"] = round(float(np.percentile(np.abs(r), 99)), 2)
            res["content_rate_on_screen"] = round(1 / fit[0] * rate, 3)  # content s per s
            if (facts.get("deltas") or {}).get("frame-drop-count"):
                res["residual_note"] = "mpv dropped frames: content times are off after the first"
    ipc = d / "ipc.jsonl"
    if ipc.exists():
        samples = [json.loads(x) for x in ipc.read_text().splitlines()]
        av = [x["avsync"] for x in samples if isinstance(x.get("avsync"), (int, float))]
        if av:
            res["mpv_avsync_ms_median"] = round(statistics.median(av) * 1000, 2)
            res["mpv_avsync_ms_range"] = [round(min(av) * 1000, 2), round(max(av) * 1000, 2)]
        res["drops"] = (facts.get("deltas") or {}).get("frame-drop-count")
    # the drift: a 143.856 fps stream (mpv at 71.928 doubled, audio sync) on 144 Hz holds
    # one refresh every 6.9 s; display sync resamples to the screen and has none
    sync = str((facts.get("last") or {}).get("video-sync") or "")
    drift = 0.0 if sync.startswith("display") else round((144 - 143.856) * 60, 1)
    res["drift_held_per_min"] = drift
    res["held_excl_drift_per_min"] = round(max(0.0, held / minutes - drift), 1)
    res["vrr"] = [(facts.get("window") or {}).get("vrr_monitor"),
                  (facts.get("window_end") or {}).get("vrr_monitor")]  # fmt: skip
    res["screen"] = (facts.get("window") or {}).get("monitor")
    res["engine"] = facts.get("final")
    return res


COLS = ("run", "presents_per_s", "mpv_presents_per_s", "never_shown", "held_per_min",
        "held_excl_drift_per_min", "slots_hist", "residual_ms_sd", "residual_ms_p99_abs",
        "delay_ms_p1_p50_p99", "made_delay_ms_p50", "mpv_avsync_ms_median", "drops")  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--json")
    ns = ap.parse_args()
    rows = []
    for r in ns.runs:
        d = Path(r) if Path(r).is_absolute() else RUNS / r
        rows.append(analyse(d))
    print("| " + " | ".join(COLS) + " |")
    print("|" + "---|" * len(COLS))
    for r in rows:
        print("| " + " | ".join("" if r.get(c) is None else str(r.get(c)) for c in COLS) + " |")
    if ns.json:
        Path(ns.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
