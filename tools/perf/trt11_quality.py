# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""fp16 quality gate on the real TensorRT 11 path (spike M0(l), SCOPE §12 M2).

Same frame triples and metrics as ``fp16_quality.py`` (A, B, C consecutive;
RIFE rebuilds B from A and C), but the frames go through vspipe and vs-mlrt's
vstrt (``trt_pair.vpy``): TensorRT 11 fp16 with half-precision frames, TensorRT
11 fp32, and the RIFE-ncnn plugin ButterEye plays with today. The reference is
ONNX Runtime CUDA fp32 in this process (equal to TensorRT fp32, M0(k)).

Frames are decoded into memory; the two input frames of each triple are written
as .npy files to a scratch directory under ``--work`` and removed afterwards.
Dev-box tool, run in the dev venv (see ``ort_ceiling.py``) under
``tools/memguard.py``.
"""

from __future__ import annotations

import argparse
import json
import os
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
NCNN_PLUGIN_DIR = "/usr/lib64/buttereye/vapoursynth"
NCNN_MODEL = "/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse"
VARIANTS = {
    "trt11-fp16-h": {"engine": "trt", "fp16": True, "half_io": True},
    "trt11-fp32": {"engine": "trt", "fp16": False, "half_io": False},
    "ncnn": {"engine": "ncnn", "fp16": False, "half_io": False},
}


def run_pair(a_npy: Path, c_npy: Path, w: int, h: int, variant: str, out: Path) -> Any:
    import numpy as np

    cfg = {
        "perf_dir": str(HERE),
        "a_npy": str(a_npy),
        "c_npy": str(c_npy),
        "vstrt_dir": str(VSTRT_DIR),
        "vspy_dir": str(DEV / "vspy"),
        "onnx_models": str(DEV / "models"),
        "engine_dir": str(DEV / "engines"),
        "trt_model": "v4_26",
        "streams": 1,
        "plugin_dir": NCNN_PLUGIN_DIR,
        "model_path": NCNN_MODEL,
        **VARIANTS[variant],
    }
    argv = ["vspipe", "-a", f"user_data={json.dumps(cfg)}", "-s", "1", "-e", "1",
            str(HERE / "trt_pair.vpy"), str(out)]  # fmt: skip
    # vspipe runs on the system Python: keep this venv out of its environment
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if "/p2/venv/" not in p)
    env.pop("LD_LIBRARY_PATH", None)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=1800, env=env)
    if proc.returncode != 0:
        lines = [ln for ln in (proc.stderr + proc.stdout).splitlines() if ln.strip()]
        raise RuntimeError(" | ".join(lines[-4:]))
    data = np.fromfile(out, dtype=np.float32).reshape(3, h, w)
    # vspipe writes planar RGB as G, B, R (FFmpeg's gbrp order)
    return np.clip(data[[2, 0, 1]], 0, 1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", type=Path, default=DEV / "models/rife_v2")
    ap.add_argument("--clip", action="append", required=True, help="PATH@t1,t2,...")
    ap.add_argument("--label", action="append", help="neutral label per --clip, for the output")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--work", type=Path, default=DEV / "scratch")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(12288))
    args = ap.parse_args()

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "trt11q"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="trt11_quality").start()

    import fp16_quality as fq
    import numpy as np
    import onnxruntime as ort

    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    ref_sess = ort.InferenceSession(
        str(args.models / "rife_v4.26.onnx"), sess_options=opts,
        providers=[("CUDAExecutionProvider", {"device_id": 0})],
    )  # fmt: skip
    args.work.mkdir(parents=True, exist_ok=True)
    a_npy, c_npy, out_raw = (args.work / n for n in ("a.npy", "c.npy", "mid.rgbs"))
    labels = args.label or []
    rows: list[dict[str, Any]] = []
    try:
        for i, spec in enumerate(args.clip):
            path, _, times = spec.rpartition("@")
            label = labels[i] if i < len(labels) else f"clip{i + 1}"
            w, h = fq.probe(path)
            for t in times.split(","):
                a, b, c = fq.frames(np, path, float(t), w, h)
                np.save(a_npy, a)
                np.save(c_npy, c)
                x = np.concatenate([a, c, np.full((1, h, w), 0.5, np.float32)])[None]
                xp, _, _ = fq.pad(np, x)
                inp = ref_sess.get_inputs()[0].name
                ref = np.clip(ref_sess.run(None, {inp: xp})[0][0, :, :h, :w], 0, 1)
                base = {"clip": label, "t": float(t), "size": f"{w}x{h}"}
                rows.append({**base, "variant": "ort-cuda-fp32 (ref)",
                             "vs_truth_psnr": fq.psnr(np, ref, b)})  # fmt: skip
                for v in args.variants.split(","):
                    print(f"{label} @{t} {v} ...", file=sys.stderr, flush=True)
                    try:
                        y = run_pair(a_npy, c_npy, w, h, v, out_raw)
                    except Exception as exc:
                        rows.append({**base, "variant": v, "error": str(exc)[:300]})
                        continue
                    d = np.abs(y - ref).max(axis=0)
                    rows.append({
                        **base, "variant": v,
                        "vs_truth_psnr": fq.psnr(np, y, b),
                        "vs_ref_psnr": fq.psnr(np, y, ref),
                        "vs_ref_ssim": fq.ssim(y, ref),
                        "vs_ref_maxdiff": round(float(d.max()), 4),
                        "vs_ref_px_over_10pct": round(float((d > 0.1).mean() * 100), 4),
                    })  # fmt: skip
    finally:
        for f in (a_npy, c_npy, out_raw):
            f.unlink(missing_ok=True)

    print(
        "| clip | t | variant | vs real PSNR | vs fp32 PSNR | vs fp32 SSIM | max diff | px > 10 % |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| {r['clip']} | {r['t']:g} | {r['variant']} | error: {r['error']} | | | | |")
            continue
        print(
            f"| {r['clip']} | {r['t']:g} | {r['variant']} | {r['vs_truth_psnr']} "
            f"| {r.get('vs_ref_psnr', '')} | {r.get('vs_ref_ssim', '')} "
            f"| {r.get('vs_ref_maxdiff', '')} | {r.get('vs_ref_px_over_10pct', '')} |"
        )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
