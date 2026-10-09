# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""fp16 quality gate for RIFE on TensorRT (SCOPE §5.2 "Quality risk", §12 M2).

For each clip and time, takes three consecutive frames A, B, C, has RIFE rebuild
B from A and C (timestep 0.5), and compares every runtime variant:

- with the fp32 CUDA result (``ref``): how far each precision drifts;
- with the real middle frame B: absolute quality, next to a plain A/C blend.

Variants: ``cuda32`` (reference), ``trt32``, ``trt16`` (TensorRT builder fp16,
no layer pins), ``conv16-trt`` and ``conv16-cuda`` (the model converted with
``onnxconverter_common.float16``, keep_io_types, Cast nodes left in fp32; how
vsmlrt prepares fp16 for TensorRT 11, which then runs without the fp32 layer pins
it used before, plus the Cast fix that conversion needs to load at all).

Frames are decoded with ffmpeg into memory and never written anywhere. Dev-box
tool in its own venv (see ``ort_ceiling.py``); runs under ``tools/memguard.py``.

Usage::

    VENV=~/.cache/buttereye-dev/p2/venv
    $VENV/bin/python tools/perf/fp16_quality.py --models ~/.cache/buttereye-dev/p2/models/rife_v2 \\
        --clip "/path/film.mkv@600,1800,3600" --clip "/path/4k.webm@30,90"
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
import memguard  # noqa: E402  (tools/memguard.py)

ALIGN = 64  # RIFE v4.26 needs a multiple of 64
VARIANTS = ("cuda32", "trt32", "trt16", "conv16-trt", "conv16-cuda")


def probe(path: str) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True,
    ).stdout.strip().split(",")  # fmt: skip
    return int(out[0]), int(out[1])


def frames(np: Any, path: str, at: float, w: int, h: int) -> Any:
    """Three consecutive frames from ``at`` seconds as float32 RGB (3, 3, h, w) in [0, 1]."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(at), "-i", path, "-frames:v", "3",
         "-vf", "scale=in_color_matrix=bt709:out_range=full", "-f", "rawvideo",
         "-pix_fmt", "rgb48le", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    arr = np.frombuffer(raw, dtype="<u2").reshape(3, h, w, 3).astype(np.float32) / 65535.0
    return arr.transpose(0, 3, 1, 2).copy()


def pad(np: Any, x: Any) -> tuple[Any, int, int]:
    h, w = x.shape[-2:]
    ph, pw = (-h) % ALIGN, (-w) % ALIGN
    return np.pad(x, [(0, 0)] * (x.ndim - 2) + [(0, ph), (0, pw)], mode="edge"), h, w


def providers(name: str, cache: Path) -> list[Any]:
    cuda = ("CUDAExecutionProvider", {"device_id": 0})
    if name.endswith("cuda") or name == "cuda32":
        return [cuda]
    trt = {
        "device_id": 0,
        "trt_fp16_enable": name == "trt16",
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": str(cache),
    }
    return [("TensorrtExecutionProvider", trt), cuda]


def psnr(np: Any, a: Any, b: Any) -> float:
    mse = float(np.mean((a - b) ** 2))
    return 99.0 if mse == 0 else round(10 * np.log10(1.0 / mse), 2)


def ssim(a: Any, b: Any) -> float:
    from skimage.metrics import structural_similarity

    return round(
        float(structural_similarity(a, b, channel_axis=0, data_range=1.0, gaussian_weights=True)),
        5,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", type=Path, required=True)
    ap.add_argument("--names", default="rife_v4.26")
    ap.add_argument("--clip", action="append", required=True, help="PATH@t1,t2,... (seconds)")
    ap.add_argument("--variants", default=",".join(VARIANTS))
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache/buttereye-dev/p2/trt")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(12288))
    args = ap.parse_args()

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "fp16q"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="fp16_quality").start()

    import numpy as np
    import onnx
    import onnxruntime as ort
    from onnxconverter_common import float16

    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    args.cache.mkdir(parents=True, exist_ok=True)
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    variants = args.variants.split(",")

    # all frames first (small: a few triples), so sessions are built once per size
    cases = []
    for spec in args.clip:
        path, _, times = spec.rpartition("@")
        w, h = probe(path)
        for t in times.split(","):
            cases.append({"clip": Path(path).name, "t": float(t), "size": f"{w}x{h}",
                          "abc": frames(np, path, float(t), w, h)})  # fmt: skip

    rows: list[dict[str, Any]] = []
    for name in args.names.split(","):
        model32 = args.models / f"{name}.onnx"
        conv_path = args.cache / f"{name}.fp16-keepio-castfp32.onnx"
        if any(v.startswith("conv16") for v in variants) and not conv_path.exists():
            # Plain conversion (what vsmlrt 3.23.2 does) gives a model that fails to
            # load: the converter retypes the outputs of the graph's Cast nodes but
            # not their "to" attribute. Keeping the Cast nodes out of the conversion
            # fixes it (spike M0(k)).
            m32 = onnx.load(str(model32))
            casts = [n.name for n in m32.graph.node if n.op_type == "Cast"]
            m16 = float16.convert_float_to_float16(m32, keep_io_types=True, node_block_list=casts)
            onnx.save(m16, str(conv_path))
        sessions: dict[str, Any] = {}
        for v in variants:
            model = conv_path if v.startswith("conv16") else model32
            print(f"{name}: session {v} ...", file=sys.stderr, flush=True)
            try:
                sessions[v] = ort.InferenceSession(
                    str(model), sess_options=opts, providers=providers(v, args.cache)
                )
            except Exception as exc:
                rows.append({"model": name, "variant": v, "error": str(exc)[:300]})
        for case in cases:
            a, b, c = case["abc"]
            x = np.concatenate([a, c, np.full((1, *a.shape[1:]), 0.5, np.float32)])[None]
            xp, h, w = pad(np, x)
            outs: dict[str, Any] = {}
            for v, sess in sessions.items():
                inp, out = sess.get_inputs()[0].name, sess.get_outputs()[0].name
                y = sess.run([out], {inp: xp})[0][0, :, :h, :w]
                t0 = time.monotonic()
                for _ in range(5):
                    sess.run([out], {inp: xp})
                ms = (time.monotonic() - t0) / 5 * 1000
                outs[v] = (np.clip(y, 0, 1), ms)
            ref = outs.get("cuda32", (None, 0))[0]
            blend = (a + c) / 2
            base = {"model": name, "clip": case["clip"], "t": case["t"], "size": case["size"]}
            rows.append({**base, "variant": "blend", "vs_truth_psnr": psnr(np, blend, b),
                         "vs_truth_ssim": ssim(blend, b)})  # fmt: skip
            for v, (y, ms) in outs.items():
                row = {**base, "variant": v, "ms": round(ms, 1),
                       "vs_truth_psnr": psnr(np, y, b), "vs_truth_ssim": ssim(y, b)}  # fmt: skip
                if ref is not None and v != "cuda32":
                    row["vs_ref_psnr"] = psnr(np, y, ref)
                    row["vs_ref_ssim"] = ssim(y, ref)
                    row["vs_ref_maxdiff"] = round(float(np.max(np.abs(y - ref))), 4)
                rows.append(row)
            print(f"  {case['clip'][:30]} @{case['t']}: done", file=sys.stderr, flush=True)
        del sessions

    print(
        "| clip | t | variant | ms | vs real PSNR | vs real SSIM "
        "| vs fp32 PSNR | vs fp32 SSIM | max diff |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| | | {r['variant']} | error: {r['error']} | | | | | |")
            continue
        print(
            f"| {r['clip'][:28]} | {r['t']:g} | {r['variant']} | {r.get('ms', '')} "
            f"| {r['vs_truth_psnr']} | {r['vs_truth_ssim']} | {r.get('vs_ref_psnr', '')} "
            f"| {r.get('vs_ref_ssim', '')} | {r.get('vs_ref_maxdiff', '')} |"
        )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
