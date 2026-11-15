# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Test clip for the driver frame-doubling trial: a moving bar and a frame barcode.

Every source frame k shows:

- a 12-bit binary barcode of k in the top-left corner (blocks of ``BLOCK`` pixels,
  white for 1, black for 0), so a capture says which frame it came from; a frame the
  driver made between k and k+1 carries grey or mixed blocks where the bits differ;
- a flicker patch right of the barcode, black on even frames and white on odd ones,
  so any frame that is neither black nor white there was not decoded from the file;
- a striped vertical bar that moves ``STEP`` pixels right per frame and wraps every
  ``PERIOD`` frames; its centre gives the frame's position in time to a fraction
  of a frame (0.5 for a frame made half-way between two source frames).

The clip is lossless (FFV1, yuv420p, BT.709 limited range, 16 slices) at 24000/1001 fps
(or ``--fps``) with a
quiet sine track, so mpv's audio sync has a real clock. ``layout()`` gives the
positions the analysis needs.

usage: nvtrial_clip.py OUT.mkv [--seconds 60] [--width 1920] [--height 1080] [--fps 24000/1001]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

BLOCK = 60
BITS = 12
STEP = 16
BAR_W = 48
BG = 64
BAR_HI = 235
BAR_LO = 180
STRIPE = 8
BAR_TOP = 300
FLICKER_X = (BITS * BLOCK + 60, BITS * BLOCK + 60 + 2 * BLOCK)


def layout(width: int, height: int) -> dict[str, object]:
    period = width // STEP
    return {
        "width": width, "height": height, "block": BLOCK, "bits": BITS, "step": STEP,
        "bar_w": BAR_W, "period": period, "bar_top": BAR_TOP, "bar_bottom": height - 60,
        "flicker_x": list(FLICKER_X), "bg": BG, "bar_hi": BAR_HI, "bar_lo": BAR_LO,
        "fps": [24000, 1001],
    }  # fmt: skip


def frame(k: int, width: int, height: int) -> np.ndarray:
    """Grey (luma) picture of source frame k, uint8, studio-swing values."""
    img = np.full((height, width), BG, np.uint8)
    for b in range(BITS):
        bit = (k >> (BITS - 1 - b)) & 1
        img[0:BLOCK, b * BLOCK : (b + 1) * BLOCK] = 235 if bit else 16
    img[0:BLOCK, FLICKER_X[0] : FLICKER_X[1]] = 235 if k & 1 else 16
    x0 = (k % (width // STEP)) * STEP
    rows = np.arange(BAR_TOP, height - 60)
    stripe = np.where((rows // STRIPE) % 2 == 0, BAR_HI, BAR_LO).astype(np.uint8)
    x1 = min(x0 + BAR_W, width)
    img[BAR_TOP : height - 60, x0:x1] = stripe[:, None]
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", default="24000/1001", help="frame rate as N/D")
    ns = ap.parse_args()
    out = Path(ns.out)
    if out.exists():
        print(f"{out} exists; not overwriting", file=sys.stderr)
        return 2
    w, h = ns.width, ns.height
    num, den = (int(v) for v in ns.fps.split("/"))
    n = round(ns.seconds * num / den)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "4",
        "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}", "-framerate", ns.fps,
        "-i", "-",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={n * den / num:.3f}",
        "-filter:a", "volume=0.05",
        "-map", "0:v", "-map", "1:a",
        # the picture is already studio swing: pass Y through, neutral chroma
        "-vf", "format=yuv420p", "-color_range", "tv", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709",
        "-c:v", "ffv1", "-level", "3", "-slices", "16", "-g", "1", "-threads", "4", "-c:a", "flac",
        str(out),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for k in range(n):
        proc.stdin.write(frame(k, w, h).tobytes())
    proc.stdin.close()
    rc = proc.wait()
    meta = layout(w, h) | {"frames": n, "fps": [num, den]}
    out.with_suffix(".json").write_text(json.dumps(meta, indent=1))
    print(f"{out}: {n} frames, rc={rc}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
