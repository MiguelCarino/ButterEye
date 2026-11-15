# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Summarise driver frame-doubling trial runs (``nvtrial_run.py`` output) by arm.

Per run, over the steady window only:

- what ButterEye decided (engine, multiplier, target), the rates it showed in turn
  and how often it rebuilt its filter (a step-down);
- mpv's counters: dropped (late) frames, delayed refreshes (display sync only),
  mistimed frames; drop bursts (seconds with 5 or more drops) and the longest one;
  the largest audio/video offset; how fast playback ran against the wall clock;
- MangoHud's log of mpv's own presents (above the driver's layer): presents per
  second, interval median and 99th percentile, share of intervals over 1.5x and
  2.5x the median;
- the GPU: mean power, mpv's SM share and the largest SM share of any other process
  (``nvidia-smi pmon``);
- the machine: pressure-stall time per second (CPU, memory, IO; worst second and
  mean), mpv's CPU use, and whether a drop burst coincided with stalls;
- the screen's adaptive-sync state at the start and the end of the run;
- with the timing layer below the driver's present layer (``--probe``): presents
  per second that reached the driver, the spread of their spacing, refreshes held
  where the picture should have moved on, and pairs of presents in one refresh.

Run names are ``<prefix>_<ARM>_<n>``. Prints a markdown table per run and per arm
(median and range) and writes ``t3.json`` next to the runs when asked.

usage: nvtrial_t3.py [--json OUT] [--prefix t3] [RUN ...]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import sys
from pathlib import Path

import numpy as np

RUNS = Path.home() / ".cache/buttereye-dev/nvtrial/runs"
BURST = 5  # drops in one second that count as a burst second


def _samples(d: Path) -> list[dict]:
    p = d / "ipc.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def _per_second(samples: list[dict], key: str) -> list[float]:
    vals = [(s["t"], s.get(key)) for s in samples if isinstance(s.get(key), (int, float))]
    return [b[1] - a[1] for a, b in zip(vals, vals[1:], strict=False)]


def _smi(d: Path, w0: float, w1: float) -> dict[str, float | None]:
    p = d / "smi.csv"
    pw = []
    for ln in p.read_text().splitlines() if p.exists() else []:
        f = [x.strip() for x in ln.split(",")]
        if len(f) < 4:
            continue
        try:
            t = dt.datetime.strptime(f[0], "%Y/%m/%d %H:%M:%S.%f").timestamp()
            if w0 <= t <= w1:
                pw.append(float(f[2]))
        except ValueError:
            continue
    return {"power_w": round(statistics.fmean(pw), 1) if pw else None}


def _pmon(d: Path, pid: int | None, w0: float, w1: float) -> dict[str, object]:
    p = d / "pmon.txt"
    day = dt.date.fromtimestamp(w0)
    mine: list[float] = []
    other: dict[str, list[float]] = {}
    for ln in p.read_text().splitlines() if p.exists() else []:
        if ln.startswith("#"):
            continue
        f = ln.split()
        if len(f) < 6:
            continue
        try:
            t = dt.datetime.combine(day, dt.time.fromisoformat(f[0])).timestamp()
        except ValueError:
            continue
        if not w0 <= t <= w1 or f[2] == "-":
            continue
        sm = float(f[4]) if f[4].replace(".", "").isdigit() else 0.0
        if pid is not None and int(f[2]) == pid:
            mine.append(sm)
        else:
            other.setdefault(f"{f[-1]}:{f[2]}", []).append(sm)
    worst = max(((k, max(v)) for k, v in other.items()), key=lambda x: x[1], default=None)
    return {
        "mpv_sm": round(statistics.fmean(mine), 1) if mine else None,
        "other_sm_max": worst[1] if worst else 0.0,
        "other_sm_who": worst[0] if worst and worst[1] > 0 else None,
    }


def _mango(d: Path, w0: float, w1: float) -> dict[str, object]:
    logs = [p for p in d.glob("mpv_*.csv") if not p.name.endswith("_summary.csv")]
    if not logs:
        return {}
    # one log per swapchain; the video's is the long one
    p = max(logs, key=lambda x: x.stat().st_size)
    stamp = p.stem.split("_", 1)[1]
    start = dt.datetime.strptime(stamp, "%Y-%m-%d_%H-%M-%S").timestamp()
    rows = p.read_text().splitlines()[3:]
    ft, el = [], []
    for ln in rows:
        f = ln.split(",")
        try:
            ft.append(float(f[1]))
            el.append(float(f[-1]) / 1e9 + start)
        except ValueError, IndexError:
            continue
    keep = (np.array(el) >= w0) & (np.array(el) <= w1)
    a, t = np.array(ft)[keep], np.array(el)[keep]
    if len(a) < 10:
        return {"mango_n": int(len(a))}
    med = float(np.median(a))
    return {
        "mango_n": int(len(a)),
        "presents_per_s": round((len(a) - 1) / float(t[-1] - t[0]), 2),
        "ft_med_ms": round(med, 2),
        "ft_p99_ms": round(float(np.percentile(a, 99)), 2),
        "ft_max_ms": round(float(a.max()), 1),
        "late_1_5x": round(float((a > 1.5 * med).mean()), 4),
        "late_2_5x": round(float((a > 2.5 * med).mean()), 4),
    }


def _frames(d: Path, m0: float | None, span: float, sync: str, layer_on: bool) -> dict:
    """The timing layer's log (``frames.csv``): presents that reached the driver on
    the fullscreen swapchain over the steady window, and their spacing against the
    refresh period they should keep (one refresh with the driver's frames or display
    sync, two for mpv alone at 72 on 144). These are CPU times of the present calls,
    not scan-out."""
    p = d / "frames.csv"
    if not p.exists() or m0 is None:
        return {}
    text = p.read_text()
    if "\nP," in text or text.startswith("P,"):
        # the later layer builds tag their rows: P,t_ns,swapchain,...
        pr = [
            ln.split(",") for ln in text.splitlines() if ln.startswith("P,") and ln.count(",") >= 10
        ]
        rows = np.array([[int(f[1]), int(f[2])] for f in pr], dtype=np.int64).reshape(-1, 2)
    else:
        rows = np.loadtxt(p, delimiter=",", comments="#", dtype=np.int64, ndmin=2)
    if rows.size == 0:
        return {}
    sc = np.bincount(rows[:, 1]).argmax()
    r = rows[rows[:, 1] == sc]
    t = r[:, 0] / 1e9
    t = t[(t >= m0) & (t <= m0 + span)]
    if len(t) < 20:
        return {"probe_n": int(len(t))}
    gap = np.diff(t) * 1000
    refresh = 1000 / 144
    want = refresh if (layer_on or sync.startswith("display")) else 2 * refresh
    slots = np.maximum(np.rint(gap / refresh), 1)
    held = np.maximum(np.rint(gap / want) - 1, 0)
    return {
        "probe_n": int(len(t)),
        "probe_per_s": round((len(t) - 1) / float(t[-1] - t[0]), 2),
        "probe_gap_p1": round(float(np.percentile(gap, 1)), 2),
        "probe_gap_p50": round(float(np.median(gap)), 2),
        "probe_gap_p99": round(float(np.percentile(gap, 99)), 2),
        "probe_gap_sd": round(float(gap.std()), 2),
        "probe_held": int(held.sum()),  # refreshes the content should have moved on
        "probe_short": int((gap < 0.5 * want).sum()),  # two presents in one slot
        "probe_slots_hist": {
            int(k): int(v) for k, v in zip(*np.unique(slots, return_counts=True), strict=False)
        },
    }


def analyse(d: Path) -> dict[str, object]:
    f = json.loads((d / "facts.json").read_text())
    s = _samples(d)
    steady = [x for x in s if "wall" in x] or s
    w0 = steady[0].get("wall") or f.get("wall_steady") or 0.0
    w1 = steady[-1].get("wall") or w0 + float(f.get("steady_s") or 0)
    drops = _per_second(steady, "frame-drop-count")
    burst = [x >= BURST for x in drops]
    longest = cur = 0
    for b in burst:
        cur = cur + 1 if b else 0
        longest = max(longest, cur)
    # what ButterEye showed: one status line per engine it ran ("23.976 → 71.928 fps (3×) · …")
    log = (d / "mpv.log").read_text(errors="replace") if (d / "mpv.log").exists() else ""
    shown = re.findall(r'args="ButterEye: [0-9.]+ → ([0-9.]+) fps \((\d+)×\)', log)
    adds = len(re.findall(r'Run command: vf, flags=\d+, args=\[operation="add"', log))
    fin = f.get("final") or {}
    pt = [x.get("playback-time") for x in steady if isinstance(x.get("playback-time"), float)]
    span = (steady[-1]["t"] - steady[0]["t"]) if len(steady) > 1 else 0.0
    av = [abs(x["avsync"]) for x in steady if isinstance(x.get("avsync"), (int, float))]
    psi = {}
    for k in ("psi_cpu_some", "psi_memory_some", "psi_memory_full", "psi_io_some"):
        per = [v / 1000 for v in _per_second(steady, k)]  # ms stalled per second
        if per:
            psi[k + "_max_ms"] = round(max(per), 1)
            psi[k + "_mean_ms"] = round(statistics.fmean(per), 1)
            if any(burst) and not all(burst):
                inb = [v for v, b in zip(per, burst, strict=False) if b]
                outb = [v for v, b in zip(per, burst, strict=False) if not b]
                psi[k + "_burst_vs_calm_ms"] = [
                    round(statistics.fmean(inb), 1), round(statistics.fmean(outb), 1)]  # fmt: skip
    cpu = _per_second(steady, "mpv_cpu_s")
    win, wend = f.get("window") or {}, f.get("window_end") or {}
    last = f.get("last") or {}
    res: dict[str, object] = {
        "run": d.name,
        "arm": d.name.split("_")[1] if d.name.count("_") >= 2 else d.name,
        "engine": f"{fin.get('backend')} x{fin.get('multiplier')} -> {fin.get('target')}",
        "health_end": fin.get("health"),
        "step_downs": max(0, adds - 1),
        "rates_shown": "/".join(f"{r}({m}x)" for r, m in shown),
        "steady_s": round(span, 1),
        "drops": (f.get("deltas") or {}).get("frame-drop-count"),
        "delayed": (f.get("deltas") or {}).get("vo-delayed-frame-count"),
        "mistimed": (f.get("deltas") or {}).get("mistimed-frame-count"),
        "burst_s": int(sum(burst)),
        "longest_burst_s": longest,
        "avsync_max_ms": round(max(av) * 1000, 1) if av else None,
        "play_rate": round((pt[-1] - pt[0]) / span, 4) if len(pt) > 1 and span else None,
        "display_fps": last.get("display-fps"),
        "est_display_fps": last.get("estimated-display-fps"),
        "vsync_jitter": last.get("vsync-jitter"),
        "mpv_cpu_pct": round(100 * statistics.fmean(cpu), 0) if cpu else None,
        "vrr": [win.get("vrr_monitor"), wend.get("vrr_monitor")],
        "screen": win.get("monitor"),
        "layer_on": any(
            "Smooth Motion enabled" in x and "[1]" in x for x in f.get("nvpresent_lines") or []
        ),  # fmt: skip
    }
    res |= _smi(d, w0, w1) | _pmon(d, f.get("pid"), w0, w1) | psi | _mango(d, w0, w1)
    res |= _frames(d, f.get("mono_steady"), span, str(last.get("video-sync") or ""),
                   bool(res["layer_on"]))  # fmt: skip
    g = d / "grim.json"
    if g.exists():
        gs = json.loads(g.read_text())["summary"]
        keys = ("captures", "generated_share", "generated_phase_mean", "generated_phase_sd")
        res["grim"] = {k: gs.get(k) for k in keys}
    return res


COLS = ("run", "rates_shown", "step_downs", "drops", "delayed", "burst_s", "longest_burst_s",
        "avsync_max_ms", "play_rate", "presents_per_s", "ft_p99_ms", "late_1_5x",
        "power_w", "mpv_sm", "other_sm_max", "other_sm_who", "psi_cpu_some_max_ms",
        "psi_memory_some_max_ms", "probe_per_s", "probe_gap_p1", "probe_gap_p99",
        "probe_held", "probe_short", "vrr")  # fmt: skip
ARM_COLS = ("drops", "delayed", "burst_s", "avsync_max_ms", "play_rate", "presents_per_s",
            "ft_p99_ms", "late_1_5x", "power_w", "mpv_sm", "probe_per_s", "probe_held",
            "probe_short", "probe_gap_sd")  # fmt: skip


def _cell(v: object) -> str:
    return "" if v is None else str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="*")
    ap.add_argument("--prefix", default="t3")
    ap.add_argument("--json")
    ns = ap.parse_args()
    dirs = [RUNS / r if not Path(r).is_absolute() else Path(r) for r in ns.runs] or sorted(
        p for p in RUNS.glob(f"{ns.prefix}_*") if (p / "facts.json").exists()
    )
    rows = []
    for d in dirs:
        try:
            rows.append(analyse(d))
        except (OSError, ValueError, KeyError) as e:
            print(f"{d.name}: {e}", file=sys.stderr)
    print("| " + " | ".join(COLS) + " |")
    print("|" + "---|" * len(COLS))
    for r in rows:
        print("| " + " | ".join(_cell(r.get(c)) for c in COLS) + " |")
    print()
    arms: dict[str, list[dict]] = {}
    for r in rows:
        arms.setdefault(str(r["arm"]), []).append(r)
    print("| arm | runs | " + " | ".join(ARM_COLS) + " |")
    print("|" + "---|" * (len(ARM_COLS) + 2))
    for arm, rs in arms.items():
        cells = []
        for c in ARM_COLS:
            v = [float(r[c]) for r in rs if isinstance(r.get(c), (int, float))]
            if not v:
                cells.append("")
            elif len(v) == 1:
                cells.append(f"{v[0]:g}")
            else:
                cells.append(f"{statistics.median(v):g} [{min(v):g}-{max(v):g}]")
        print(f"| {arm} | {len(rs)} | " + " | ".join(cells) + " |")
    if ns.json:
        Path(ns.json).write_text(json.dumps(rows, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
