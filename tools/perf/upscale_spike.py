# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""ML upscaling spike (SCOPE §15.2, spike M0(n)): speed and quality of vs-mlrt's
Real-ESRGAN-family models on the TensorRT path.

Speed: each model on a blank clip (fp16 engine, half-precision frames) at the
input sizes a live chain would see, and the chain itself (RIFE at the source
size, then the upscaler on every output frame). Quality: real frames shrunk 2x
(or 4x) with Bicubic and brought back by each model and by Lanczos / Spline36,
scored against the original (PSNR, SSIM). Optional local crops for a visual
check (``--crops DIR``; never written into the repository).

Dev-box tool, run in the dev venv (``ort_ceiling.py``) under
``tools/memguard.py``; frames are decoded into memory and the .npy hand-over
files are deleted afterwards. Clip labels in the output are neutral.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import memguard  # noqa: E402  (tools/memguard.py)

DEV = Path.home() / ".cache/buttereye-dev/p2"
VSTRT_DIR = Path.home() / ".local/share/buttereye/plugins/vstrt/v16.3.test1"
STAGE = HERE / "upscale_stage.vpy"
DONE = re.compile(r"Output (\d+) frames in ([\d.]+) seconds \(([\d.]+) fps\)")

#: (vsmlrt.RealESRGANModel name, scale, licence, shippable in the COPR)
MODELS = {
    "animevideo_xsx2": (2, "BSD-3-Clause (Real-ESRGAN)", True),
    "animevideo_xsx4": (4, "BSD-3-Clause (Real-ESRGAN)", True),
    "animevideov3": (4, "BSD-3-Clause (Real-ESRGAN)", True),
    "animejanaiV3_HD_L1": (2, "CC BY-NC-SA 4.0 (AnimeJaNai)", False),
    "animejanaiV3_HD_L2": (2, "CC BY-NC-SA 4.0 (AnimeJaNai)", False),
    "animejanaiV3_HD_L3": (2, "CC BY-NC-SA 4.0 (AnimeJaNai)", False),
    "Ani4Kv2_G6i2_UltraCompact": (2, "CC BY-NC 4.0 (Ani4K)", False),
    "Ani4Kv2_G6i2_Compact": (2, "CC BY-NC 4.0 (Ani4K)", False),
}


def base_cfg() -> dict[str, Any]:
    return {
        "perf_dir": str(HERE),
        "vstrt_dir": str(VSTRT_DIR),
        "vspy_dir": str(Path.home() / ".local/share/buttereye/python"),
        "onnx_models": str(DEV / "models"),
        "engine_dir": str(DEV / "engines"),
        "fp16": True,
        "half_io": True,
        "streams": 2,
        "trt_model": "v4_26",
    }


def vspipe(cfg: dict[str, Any], out: str = "--", extra: tuple[str, ...] = ()) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if "/p2/venv/" not in p)
    env.pop("LD_LIBRARY_PATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = ["vspipe", "-a", f"user_data={json.dumps(cfg)}", "-r", "8", *extra, str(STAGE), out]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env=env)
    text = proc.stderr + proc.stdout
    if proc.returncode != 0:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        raise RuntimeError(" | ".join(lines[-3:]))
    return text


def speed(model: str, w: int, h: int, *, chain: bool, frames: int) -> dict[str, Any]:
    cfg = base_cfg() | {"mode": "chain" if chain else "speed", "sr_model": model,
                        "width": w, "height": h, "frames": frames}  # fmt: skip
    scale = MODELS[model][0]
    row: dict[str, Any] = {"model": model, "input": f"{w}x{h}",
                           "output": f"{w * scale}x{h * scale}", "chain": chain}  # fmt: skip
    try:
        text = vspipe(cfg)
        m = DONE.search(text)
        row["fps"] = float(m.group(3)) if m else None
    except Exception as exc:  # report and go on
        row["error"] = str(exc)[:300]
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clip", action="append", default=[], help="PATH@t1,t2 (quality)")
    ap.add_argument("--label", action="append", default=[], help="neutral label per --clip")
    ap.add_argument("--factor", action="append", default=[], help="2 or 4 per --clip")
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--no-speed", action="store_true")
    ap.add_argument("--crops", type=Path, help="write visual crops here (local only)")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(12288))
    args = ap.parse_args()

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "upscale"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="upscale_spike").start()

    models = [m for m in args.models.split(",") if m in MODELS]
    out: dict[str, Any] = {"speed": [], "quality": []}

    if not args.no_speed:
        for model in models:
            scale = MODELS[model][0]
            sizes = [(1280, 720), (1920, 1080)] if scale == 2 else [(960, 540)]
            for w, h in sizes:
                for chain in (False, True):
                    print(f"speed {model} {w}x{h} chain={chain} ...", file=sys.stderr, flush=True)
                    row = speed(model, w, h, chain=chain, frames=96)
                    print(f"    -> {row}", file=sys.stderr, flush=True)
                    out["speed"].append(row)

    if args.clip:
        import fp16_quality as fq
        import numpy as np

        npy = DEV / "scratch" / "orig.npy"
        raw = DEV / "scratch" / "up.rgbs"
        npy.parent.mkdir(parents=True, exist_ok=True)
        try:
            for i, spec in enumerate(args.clip):
                path, _, times = spec.rpartition("@")
                label = args.label[i] if i < len(args.label) else f"clip{i + 1}"
                factor = int(args.factor[i]) if i < len(args.factor) else 2
                w, h = fq.probe(path)
                for t in times.split(","):
                    orig = fq.frames(np, path, float(t), w, h)[0]
                    np.save(npy, orig)
                    outs: dict[str, Any] = {}
                    methods = ["lanczos", "spline36"] + [
                        m for m in models if factor in (MODELS[m][0], 2)
                    ]
                    for method in methods:
                        if method in MODELS and MODELS[method][0] != factor:
                            continue  # a 2x model can't do a 4x test and vice versa
                        cfg = base_cfg() | {"mode": "quality", "orig_npy": str(npy),
                                            "factor": factor, "method": method,
                                            "sr_model": method}  # fmt: skip
                        print(f"quality {label} @{t} x{factor} {method} ...", file=sys.stderr,
                              flush=True)  # fmt: skip
                        try:
                            vspipe(cfg, str(raw))
                            y = np.fromfile(raw, dtype=np.float32).reshape(3, h, w)[[2, 0, 1]]
                            y = np.clip(y, 0, 1)
                            outs[method] = y
                            out["quality"].append({
                                "clip": label, "t": float(t), "factor": factor,
                                "method": method, "psnr": fq.psnr(np, y, orig),
                                "ssim": fq.ssim(y, orig),
                            })  # fmt: skip
                        except Exception as exc:
                            out["quality"].append({"clip": label, "t": float(t),
                                                   "factor": factor, "method": method,
                                                   "error": str(exc)[:300]})  # fmt: skip
                    if args.crops is not None and outs:
                        from PIL import Image

                        args.crops.mkdir(parents=True, exist_ok=True)
                        cy, cx, ch, cw = h // 2 - 135, w // 2 - 240, 270, 480
                        tiles = [orig] + [outs[k] for k in outs]
                        row_img = np.concatenate(
                            [(x[:, cy : cy + ch, cx : cx + cw].transpose(1, 2, 0) * 255)
                             .astype(np.uint8) for x in tiles], axis=1,
                        )  # fmt: skip
                        name = f"{label}-t{int(float(t))}-x{factor}.png"
                        Image.fromarray(row_img).save(args.crops / name)
                        (args.crops / f"{name}.txt").write_text(
                            "original, " + ", ".join(outs) + "\n"
                        )
        finally:
            npy.unlink(missing_ok=True)
            raw.unlink(missing_ok=True)

    print("| model | licence | input → output | upscale only fps | RIFE 2x + upscale fps |")
    print("|---|---|---|---|---|")
    by = {(r["model"], r["input"], r["chain"]): r for r in out["speed"]}
    for (model, inp, chain), r in by.items():
        if chain:
            continue
        c = by.get((model, inp, True), {})
        f1 = r.get("fps", r.get("error", "—"))
        f2 = c.get("fps", c.get("error", "—"))
        print(f"| {model} | {MODELS[model][1]} | {inp} → {r['output']} | {f1} | {f2} |")
    print()
    print("| clip | t | factor | method | PSNR | SSIM |")
    print("|---|---|---|---|---|---|")
    for q in out["quality"]:
        if "error" in q:
            print(f"| {q['clip']} | {q['t']:g} | {q['factor']}x | {q['method']} | error: "
                  f"{q['error']} | |")  # fmt: skip
        else:
            print(f"| {q['clip']} | {q['t']:g} | {q['factor']}x | {q['method']} "
                  f"| {q['psnr']} | {q['ssim']} |")  # fmt: skip
    if args.json:
        args.json.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
