# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv's scalers and shader upscalers (SCOPE §15.2, spike M0(o)).

mpv's ``gpu-next`` output scales with libplacebo; ffmpeg's ``libplacebo`` filter
runs the same scalers and mpv ``.hook``/``.glsl`` user shaders on the same
Vulkan GPU, so this measures what mpv would show, offline. Same test as M0(n):
a real frame is shrunk 2x with Bicubic (as yuv420p10, BT.709-tagged FFV1, the
way decoded video arrives) and brought back by each method, then scored against
the original (PSNR, SSIM: fidelity). Speed: 1080p -> 2160p per method, reported
as the extra milliseconds per frame over bilinear (upload/download excluded by
the subtraction; mpv itself does neither).

Dev-box tool in the dev venv (``ort_ceiling.py``) under ``tools/memguard.py``.
Test frames and outputs are temporary files under ``--work``; crops for a
visual check go to ``--crops`` (local only). Labels in the output are neutral.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import memguard  # noqa: E402  (tools/memguard.py)

DEV = Path.home() / ".cache/buttereye-dev/p2"
SHADERS = DEV / "shaders"
#: name -> (libplacebo upscaler, shader file or None, licence)
METHODS: dict[str, tuple[str, str | None, str]] = {
    "bilinear": ("bilinear", None, "libplacebo (LGPL-2.1+)"),
    "spline36": ("spline36", None, "libplacebo"),
    "lanczos": ("lanczos", None, "libplacebo"),
    "ewa_lanczos": ("ewa_lanczos", None, "libplacebo"),
    "ewa_lanczossharp": ("ewa_lanczossharp", None, "libplacebo"),
    "ewa_lanczos4sharpest": ("ewa_lanczos4sharpest", None, "libplacebo"),
    "FSRCNNX_x2_8": ("ewa_lanczossharp", "FSRCNNX_x2_8-0-4-1.glsl", "GPL-3.0"),
    "FSRCNNX_x2_16": ("ewa_lanczossharp", "FSRCNNX_x2_16-0-4-1.glsl", "GPL-3.0"),
    "ravu-lite-ar-r4": ("ewa_lanczossharp", "ravu-lite-ar-r4.hook", "LGPL-3.0"),
    "ravu-r3-yuv": ("ewa_lanczossharp", "ravu-r3-yuv.hook", "LGPL-3.0"),
    "ArtCNN_C4F16": ("ewa_lanczossharp", "ArtCNN_C4F16.glsl", "MIT"),
    "Anime4K_CNN_x2_M": ("ewa_lanczossharp", "Anime4K_Upscale_CNN_x2_M.glsl", "MIT"),
}
TAGS = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv"]  # fmt: skip


def placebo(method: str, w: int, h: int, out_fmt: str) -> str:
    up, shader, _ = METHODS[method]
    vf = f"libplacebo=w={w}:h={h}:upscaler={up}:format={out_fmt}"
    if shader:
        vf += f":custom_shader_path={SHADERS / shader}"
    return vf


def run(argv: list[str], timeout: float = 600) -> None:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip() and "dlssnr" not in ln]
        raise RuntimeError(" | ".join(lines[-3:]))


def speed(method: str, frames: int = 240) -> float:
    argv = ["ffmpeg", "-v", "error", "-init_hw_device", "vulkan", "-f", "lavfi",
            "-i", "testsrc2=size=1920x1080:rate=24,format=yuv420p10le,setparams=colorspace=bt709",
            "-frames:v", str(frames), "-vf", placebo(method, 3840, 2160, "yuv420p10le"),
            "-f", "null", "-"]  # fmt: skip
    t0 = time.monotonic()
    run(argv)
    return (time.monotonic() - t0) * 1000 / frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clip", action="append", default=[], help="PATH@t1,t2")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--methods", default=",".join(METHODS))
    ap.add_argument("--work", type=Path, default=DEV / "scratch" / "scalers")
    ap.add_argument("--crops", type=Path)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(8192))
    args = ap.parse_args()

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "scalers"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="scaler_spike").start()

    import fp16_quality as fq
    import numpy as np

    methods = [m for m in args.methods.split(",") if m in METHODS]
    out: dict[str, Any] = {"speed": [], "quality": []}

    # speed: extra ms per frame over bilinear, median of 3
    base: list[float] = []
    for _ in range(3):
        base.append(speed("bilinear"))
    base_ms = sorted(base)[1]
    for m in methods:
        print(f"speed {m} ...", file=sys.stderr, flush=True)
        try:
            ms = sorted(speed(m) for _ in range(3))[1]
            out["speed"].append({"method": m, "ms_per_frame": round(ms, 2),
                                 "extra_ms": round(ms - base_ms, 2)})  # fmt: skip
        except Exception as exc:
            out["speed"].append({"method": m, "error": str(exc)[:300]})

    args.work.mkdir(parents=True, exist_ok=True)
    small, up = args.work / "small.mkv", args.work / "up.raw"
    try:
        for i, spec in enumerate(args.clip):
            path, _, times = spec.rpartition("@")
            label = args.label[i] if i < len(args.label) else f"clip{i + 1}"
            w, h = fq.probe(path)
            for t in times.split(","):
                # the reference goes through the same libplacebo YUV -> RGB path as
                # every upscale (swscale and libplacebo convert differently, ~38 dB
                # apart even unscaled), so only the scaler differs
                full = args.work / "full.mkv"
                run(["ffmpeg", "-v", "error", "-y", "-ss", t, "-i", path, "-frames:v", "1",
                     "-vf", "format=yuv420p10le", *TAGS, "-c:v", "ffv1", str(full)])  # fmt: skip
                run(["ffmpeg", "-v", "error", "-y", "-init_hw_device", "vulkan", "-i", str(full),
                     "-vf", placebo("bilinear", w, h, "rgb48le"), "-frames:v", "1",
                     "-f", "rawvideo", str(up)])  # fmt: skip
                orig = np.fromfile(up, dtype="<u2").reshape(h, w, 3)
                orig = orig.astype(np.float32).transpose(2, 0, 1) / 65535.0
                run(["ffmpeg", "-v", "error", "-y", "-i", str(full),
                     "-vf", "scale=iw/2:ih/2:flags=bicubic,format=yuv420p10le",
                     *TAGS, "-c:v", "ffv1", str(small)])  # fmt: skip
                full.unlink(missing_ok=True)
                outs: dict[str, Any] = {}
                for m in methods:
                    print(f"quality {label} @{t} {m} ...", file=sys.stderr, flush=True)
                    try:
                        run(["ffmpeg", "-v", "error", "-y", "-init_hw_device", "vulkan",
                             "-i", str(small), "-vf", placebo(m, w, h, "rgb48le"),
                             "-frames:v", "1", "-f", "rawvideo", str(up)])  # fmt: skip
                        y = np.fromfile(up, dtype="<u2").reshape(h, w, 3)
                        y = y.astype(np.float32).transpose(2, 0, 1) / 65535.0
                        outs[m] = y
                        luma = (0.2126, 0.7152, 0.0722)
                        y_l = sum(c * y[i] for i, c in enumerate(luma))
                        o_l = sum(c * orig[i] for i, c in enumerate(luma))
                        out["quality"].append({"clip": label, "t": float(t), "method": m,
                                               "psnr": fq.psnr(np, y, orig),
                                               "psnr_luma": fq.psnr(np, y_l, o_l),
                                               "ssim": fq.ssim(y, orig)})  # fmt: skip
                    except Exception as exc:
                        out["quality"].append({"clip": label, "t": float(t), "method": m,
                                               "error": str(exc)[:300]})  # fmt: skip
                if args.crops is not None and outs:
                    from PIL import Image

                    args.crops.mkdir(parents=True, exist_ok=True)
                    cy, cx = h // 2 - 70, w // 2 - 110
                    keys = ["original", *outs]
                    tiles = []
                    for k in keys:
                        x = orig if k == "original" else outs[k]
                        tile = x[:, cy : cy + 140, cx : cx + 220].transpose(1, 2, 0) * 255
                        tiles.append(tile.astype(np.uint8))
                    img = Image.fromarray(np.concatenate(tiles, axis=1))
                    img = img.resize((img.width * 2, img.height * 2), Image.NEAREST)
                    name = f"{label}-t{int(float(t))}"
                    img.save(args.crops / f"{name}.png")
                    (args.crops / f"{name}.txt").write_text(", ".join(keys) + "\n")
    finally:
        small.unlink(missing_ok=True)
        up.unlink(missing_ok=True)

    print("| method | licence | ms/frame 1080p→4K | extra vs bilinear |")
    print("|---|---|---|---|")
    for r in out["speed"]:
        lic = METHODS[r["method"]][2]
        if "error" in r:
            print(f"| {r['method']} | {lic} | error: {r['error']} | |")
        else:
            print(f"| {r['method']} | {lic} | {r['ms_per_frame']} | {r['extra_ms']} |")
    print()
    print("| clip | t | method | PSNR | luma PSNR | SSIM |")
    print("|---|---|---|---|---|---|")
    for q in out["quality"]:
        if "error" in q:
            print(f"| {q['clip']} | {q['t']:g} | {q['method']} | error: {q['error']} | | |")
        else:
            print(f"| {q['clip']} | {q['t']:g} | {q['method']} | {q['psnr']} "
                  f"| {q['psnr_luma']} | {q['ssim']} |")  # fmt: skip
    if args.json:
        args.json.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
