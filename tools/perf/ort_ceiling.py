# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""P2 inference ceiling (SCOPE §15.1, spike M0(j)): RIFE ONNX on faster runtimes.

Times vs-mlrt's RIFE ONNX models (the ``rife_v2`` representation: img0, img1 and
a timestep plane in, one RGB frame out) under ONNX Runtime with the CUDA and
TensorRT execution providers, fp32 and fp16, at 1080p and 2160p. Two numbers per
case:

- ``gpu``: inputs and output stay on the GPU (IO binding); the pure inference
  ceiling a well-pipelined plugin could approach.
- ``host``: fp32 frames are copied from and back to system memory every call,
  as a plugin that does not pipeline would.

This is a dev-box measurement tool, not part of ButterEye. It runs in its own
virtual environment (NVIDIA's CUDA/cuDNN/TensorRT wheels, user-installed) and
never inside ButterEye's process (SCOPE §8.1). It runs under the memory
failsafes of ``tools/memguard.py``.

Usage::

    VENV=~/.cache/buttereye-dev/p2/venv
    $VENV/bin/python tools/perf/ort_ceiling.py --models ~/.cache/buttereye-dev/p2/models/rife_v2
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import memguard  # noqa: E402  (tools/memguard.py)

SIZES = {"1080": (1920, 1080), "2160": (3840, 2160)}
ALIGN = 32  # vs-mlrt pads RIFE input to a multiple of 32 at scale 1.0


def padded(n: int) -> int:
    return (n + ALIGN - 1) // ALIGN * ALIGN


def providers(name: str, cache: Path) -> list[Any]:
    if name == "cuda":
        return [("CUDAExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
    fp16 = name == "trt-fp16"
    trt = {
        "device_id": 0,
        "trt_fp16_enable": fp16,
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": str(cache),
        "trt_timing_cache_enable": True,
        "trt_timing_cache_path": str(cache),
    }
    return [("TensorrtExecutionProvider", trt), ("CUDAExecutionProvider", {"device_id": 0})]


def bench(
    ort: Any, np: Any, model: Path, ep: str, size: str, iters: int, cache: Path
) -> dict[str, Any]:
    w, h = SIZES[size]
    pw, ph = padded(w), padded(h)
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    t0 = time.monotonic()
    sess = ort.InferenceSession(str(model), sess_options=opts, providers=providers(ep, cache))
    used = sess.get_providers()[0]
    inp = sess.get_inputs()[0].name
    out = sess.get_outputs()[0].name
    x = np.random.rand(1, 7, ph, pw).astype(np.float32)
    x[:, 6] = 0.5  # timestep
    sess.run([out], {inp: x})  # engine build / first run
    setup_s = time.monotonic() - t0

    # host: copies in and out every call
    for _ in range(3):
        sess.run([out], {inp: x})
    t = time.monotonic()
    for _ in range(iters):
        sess.run([out], {inp: x})
    host_fps = iters / (time.monotonic() - t)

    # gpu: IO binding, data stays on the device
    xd = ort.OrtValue.ortvalue_from_numpy(x, "cuda", 0)
    binding = sess.io_binding()
    binding.bind_ortvalue_input(inp, xd)
    binding.bind_output(out, "cuda", 0)
    for _ in range(3):
        sess.run_with_iobinding(binding)
    t = time.monotonic()
    for _ in range(iters):
        sess.run_with_iobinding(binding)
    binding.synchronize_outputs()
    gpu_fps = iters / (time.monotonic() - t)
    del sess
    return {
        "model": model.stem,
        "ep": ep,
        "provider": used,
        "size": size,
        "padded": f"{pw}x{ph}",
        "setup_s": round(setup_s, 1),
        "gpu_fps": round(gpu_fps, 1),
        "host_fps": round(host_fps, 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--models", type=Path, required=True, help="directory of rife_v2 *.onnx")
    ap.add_argument("--names", default="rife_v4.26,rife_v4.22_lite")
    ap.add_argument("--eps", default="cuda,trt-fp32,trt-fp16")
    ap.add_argument("--sizes", default="1080,2160")
    ap.add_argument("--iters", type=int, default=60)
    ap.add_argument("--cache", type=Path, default=Path.home() / ".cache/buttereye-dev/p2/trt")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(12288))
    args = ap.parse_args()

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "ort"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="ort_ceiling").start()

    import numpy as np
    import onnxruntime as ort

    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()  # CUDA/cuDNN from the nvidia-* wheels
    args.cache.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in args.names.split(","):
        model = args.models / f"{name}.onnx"
        for size in args.sizes.split(","):
            for ep in args.eps.split(","):
                print(f"{name} {size}p {ep} ...", file=sys.stderr, flush=True)
                try:
                    row = bench(ort, np, model, ep, size, args.iters, args.cache)
                except Exception as exc:  # report and go on with the next case
                    row = {"model": name, "ep": ep, "size": size, "error": str(exc)[:300]}
                print(f"    -> {row}", file=sys.stderr, flush=True)
                rows.append(row)

    print("| model | size | runtime | provider | gpu fps | host fps | setup s |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| {r['model']} | {r['size']}p | {r['ep']} | error: {r['error']} | | | |")
        else:
            print(
                f"| {r['model']} | {r['size']}p | {r['ep']} | {r['provider']} "
                f"| {r['gpu_fps']} | {r['host_fps']} | {r['setup_s']} |"
            )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
