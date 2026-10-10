#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Quality of the mvgpu plugin on real frame triples (A, B, C consecutive):
the plugin gets A and C as a 2-frame clip and its in-between frame is compared
with the real B (luma PSNR in the video's own YUV), next to plain blending.
Runs in the system Python (VapourSynth); optional PNG crops of the results.

Usage: mvgpu_quality.py --plugin libmvgpu.so --clip PATH@t1,t2 [--label L] [--crops DIR]
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path


def psnr(a, b, peak: float) -> float:  # type: ignore[no-untyped-def]
    import numpy as np

    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return round(10 * math.log10(peak * peak / mse), 2) if mse > 0 else 99.0


def main() -> int:
    import numpy as np
    import vapoursynth as vs

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plugin", required=True)
    ap.add_argument("--clip", action="append", required=True, help="PATH@t1,t2,...")
    ap.add_argument("--label", action="append")
    ap.add_argument("--crops", type=Path)
    args = ap.parse_args()
    core = vs.core
    core.std.LoadPlugin(args.plugin)
    if not hasattr(core, "ffms2"):
        core.std.LoadPlugin("/usr/lib64/libffms2.so.5")
    print("| clip | t | blend (Y PSNR) | mvgpu (Y PSNR) |")
    print("|---|---|---|---|")
    for i, spec in enumerate(args.clip):
        path, _, times = spec.rpartition("@")
        label = (args.label or [])[i] if i < len(args.label or []) else f"clip{i + 1}"
        src = core.ffms2.Source(path, cachefile=f"/tmp/mvgpu-q-{i}.ffindex")
        fps = src.fps_num / src.fps_den
        for t in times.split(","):
            n = round(float(t) * fps)
            pair = core.std.AssumeFPS(src[n] + src[n + 2], fpsnum=24, fpsden=1)
            mid = core.mvgpu.Interpolate(pair, fps_num=48, fps_den=1)[1]
            peak = float((1 << src.format.bits_per_sample) - 1)
            real = np.asarray(src.get_frame(n + 1)[0])
            out = np.asarray(mid.get_frame(0)[0])
            a = np.asarray(src.get_frame(n)[0]).astype(np.float64)
            c = np.asarray(src.get_frame(n + 2)[0]).astype(np.float64)
            print(f"| {label} | {t} | {psnr((a + c) / 2, real, peak)} | {psnr(out, real, peak)} |")
            if args.crops is not None:
                args.crops.mkdir(parents=True, exist_ok=True)
                rgb = core.resize.Bicubic(mid, format=vs.RGB24, matrix_in_s="709")
                h, w = rgb.height, rgb.width
                f = rgb.get_frame(0)
                img = np.dstack([np.asarray(f[p]) for p in range(3)])
                crop = img[h // 2 - 180 : h // 2 + 180, w // 2 - 320 : w // 2 + 320]
                ppm = args.crops / f"{label[:8]}-{t}-mvgpu.ppm"
                with open(ppm, "wb") as fh:
                    fh.write(f"P6 {crop.shape[1]} {crop.shape[0]} 255\n".encode())
                    fh.write(np.ascontiguousarray(crop).tobytes())
    return 0


if __name__ == "__main__":
    sys.exit(main())
