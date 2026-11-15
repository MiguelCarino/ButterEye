# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Read the captures of ``nvtrial_probe.py --grim``: which frames were on screen.

Each capture of the test clip (``nvtrial_clip.py``) is classified from three marks:

- the barcode: a block is crisp black or white on a decoded frame; a grey block
  means the picture was made between two frames whose bits differ there;
- the flicker patch: black or white on decoded frames;
- the bar: its centre gives the position in time, in source frames. The fraction
  ``phase`` is 0 for a decoded frame and about 0.5 for one made half-way.

Prints the counts and the phase histogram, and writes ``grim.json`` next to the npz.

With ``--sub=N`` (N mpv frames per source frame, 3 for ButterEye at x3) the phase
is taken between mpv's frames: 0 for a frame mpv presented, 0.5 for one the driver
made between two of them.

usage: nvtrial_grim.py [--sub=N] RUN_DIR [RUN_DIR ...]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nvtrial_clip import BAR_W, BITS, BLOCK, FLICKER_X, STEP  # noqa: E402

LO, HI = 40.0, 215.0  # full-range luma: black ~0, white ~255 after mpv's expansion


def classify(top: np.ndarray, barrow: np.ndarray, width: int) -> dict[str, object]:
    m = BLOCK // 4
    bits = [float(top[m:-m, b * BLOCK + m : (b + 1) * BLOCK - m].mean()) for b in range(BITS)]
    grey_bits = sum(1 for v in bits if LO < v < HI)
    k = 0
    for v in bits:
        k = (k << 1) | (1 if v >= 128 else 0)
    flick = float(top[m:-m, FLICKER_X[0] + m : FLICKER_X[1] - m].mean())
    bg = float(np.median(barrow))
    w = np.clip(barrow - bg - 20.0, 0, None)
    total = float(w.sum())
    if total <= 0:
        return {"k": k, "grey_bits": grey_bits, "flicker": flick, "pos": None}
    cx = float((w * np.arange(width)).sum() / total)
    above = int((barrow > bg + 0.5 * (barrow.max() - bg)).sum())
    pos = (cx - (BAR_W - 1) / 2) / STEP
    return {"k": k, "grey_bits": grey_bits, "flicker": flick, "pos": pos,
            "phase": pos - np.floor(pos), "bar_cols": above,
            "peak": float(barrow.max() - bg)}  # fmt: skip


def analyse(run: Path, sub: int = 1) -> dict[str, object]:
    d = np.load(run / "grim.npz")
    top, rows, stamps = d["top"], d["barrow"], d["stamps"]
    width = rows.shape[1]
    recs = [classify(top[i].astype(np.float32), rows[i], width) for i in range(len(rows))]
    period = width // STEP
    real = gen = odd = 0
    phases = []
    for r in recs:
        if r["pos"] is None:
            odd += 1
            continue
        ph = float(r["pos"]) * sub  # type: ignore[arg-type]
        ph -= np.floor(ph)  # phase between mpv's frames (sub per source frame)
        # the bar near the wrap is split across the edge: skip it
        if r["pos"] < 0.5 or r["pos"] > period - 3.5:  # type: ignore[operator]
            odd += 1
            continue
        phases.append(ph)
        crisp = sub > 1 or r["grey_bits"] == 0 and (r["flicker"] < LO or r["flicker"] > HI)  # type: ignore[operator]
        near_int = min(ph, 1 - ph) < 0.15
        if crisp and near_int:
            real += 1
        elif not near_int:
            gen += 1
        else:
            odd += 1
    hist, edges = np.histogram(phases, bins=10, range=(0, 1))
    dt = np.diff(stamps[:, 0]) if len(stamps) > 1 else np.array([0.0])
    gen_ph = [p for p in phases if min(p, 1 - p) >= 0.15]
    res = {
        "run": run.name, "sub": sub, "captures": len(recs), "decoded_like": real,
        "generated_like": gen,
        "other": odd, "generated_share": round(gen / max(1, real + gen), 3),
        "phase_hist_0.1": hist.tolist(),
        "generated_phase_mean": round(float(np.mean(gen_ph)), 3) if gen_ph else None,
        "generated_phase_sd": round(float(np.std(gen_ph)), 3) if gen_ph else None,
        "grey_bits_on_generated": int(sum(1 for r, p in zip(recs, phases, strict=False)
                                          if r.get("grey_bits"))),
        "capture_interval_ms": round(float(np.median(dt)) * 1000, 1),
        "bar_cols_median": float(np.median([r["bar_cols"] for r in recs if r["pos"] is not None]
                                           or [0])),
    }  # fmt: skip
    (run / "grim.json").write_text(
        json.dumps({"summary": res, "records": recs}, indent=1, default=float)
    )
    return res


def main() -> int:
    args = sys.argv[1:]
    sub = 1
    if args and args[0].startswith("--sub="):
        sub = int(args.pop(0).split("=", 1)[1])
    for a in args:
        print(json.dumps(analyse(Path(a), sub)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
