#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Spike M0(q): RIFE v4.x with its optical flow at a lower resolution (``scale``).

RIFE's IFNet estimates flow in four blocks, each on a picture shrunk by 16, 8, 4
and 2 (``scale_list = [16, 8, 4, 2]`` in Practical-RIFE); ``scale=0.5`` doubles
those factors (32, 16, 8, 4) so the flow costs about a quarter, while warping
and blending stay at full resolution. Practical-RIFE supports it for v4.x;
vs-mlrt only for its older v1 exports below v4.7 ("not supported"). This tool
patches vs-mlrt's v2 export of v4.26 the same way vs-mlrt patches v1:

- ``/blockN/Resize`` (down by 1/s) scales x k, ``/blockN/Resize_1`` (up by s) / k,
- ``/blockN/Mul`` (output flow x s) / k,
- the three Muls that rescale flow for the next block (x 1/s) x k,

refusing any graph whose constants aren't exactly the expected ones.

Usage (dev venv with onnx and onnxruntime-gpu, see ``ort_ceiling.py``):
  rife_scale.py patch SRC.onnx DST.onnx [--scale 0.5]
  rife_scale.py quality --model rife_v4.26.onnx --clip PATH@t1,t2 [--label L] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

#: block down-resize factors and output-flow multipliers at scale 1 (v4.26 v2 export)
BLOCK_SCALES = (16.0, 8.0, 4.0, 2.0)
#: flow handed to blocks 1-3 is divided by their scale
NEXT_FLOW = (0.125, 0.25, 0.5)


def patch(model: Any, k: float) -> Any:
    """``model`` (an onnx ModelProto, modified in place) with flow at ``k`` x the
    resolution. Raises ValueError when the graph isn't the expected IFNet."""
    from onnx.numpy_helper import from_array, to_array

    g = model.graph
    users: dict[str, int] = {}
    for n in g.node:
        for x in n.input:
            users[x] = users.get(x, 0) + 1
    const = {n.output[0]: n for n in g.node if n.op_type == "Constant"}
    produced_by = {o: n for n in g.node for o in n.output}

    def constant_input(node: Any, index: int | None = None) -> Any:
        names = [x for x in node.input if x in const]
        if index is not None:
            names = [node.input[index]] if node.input[index] in const else []
        if len(names) != 1 or users.get(names[0], 0) != 1:
            raise ValueError(f"{node.name}: expected one private constant")
        return const[names[0]]

    def rewrite(cnode: Any, fn: Any) -> None:
        arr = to_array(cnode.attribute[0].t).copy()
        cnode.attribute[0].t.CopyFrom(from_array(fn(arr), cnode.attribute[0].t.name))

    changed = 0
    for i, s in enumerate(BLOCK_SCALES):
        down = next((n for n in g.node if n.name == f"/block{i}/Resize"), None)
        up = next((n for n in g.node if n.name == f"/block{i}/Resize_1"), None)
        mul = next((n for n in g.node if n.name == f"/block{i}/Mul"), None)
        if down is None or up is None or mul is None:
            raise ValueError(f"block {i}: Resize/Resize_1/Mul not found")
        cd, cu, cm = constant_input(down, 2), constant_input(up, 2), constant_input(mul)
        vd, vu, vm = (to_array(c.attribute[0].t) for c in (cd, cu, cm))
        if list(vd) != [1, 1, 1 / s, 1 / s] or list(vu) != [1, 1, s, s] or float(vm) != s:
            raise ValueError(f"block {i}: unexpected constants {vd}, {vu}, {vm}")
        rewrite(cd, lambda a: (a * [1, 1, k, k]).astype(a.dtype))
        rewrite(cu, lambda a: (a / [1, 1, k, k]).astype(a.dtype))
        rewrite(cm, lambda a: (a / k).astype(a.dtype))
        changed += 3
    found = []
    for n in g.node:
        if n.op_type != "Mul" or n.name.startswith("/block"):
            continue
        consts = [x for x in n.input if x in const]
        others = [x for x in n.input if x not in const]
        if len(consts) != 1 or len(others) != 1:
            continue
        v = to_array(const[consts[0]].attribute[0].t)
        if v.size == 1 and float(v) in NEXT_FLOW:
            src = produced_by.get(others[0])
            # the flow: block0's output, or the running sum of block outputs
            if src is not None and (src.name == "/block0/Mul" or src.op_type == "Add"):
                found.append((float(v), n))
    if sorted(v for v, _ in found) != sorted(NEXT_FLOW):
        raise ValueError(f"next-block flow multipliers: found {[v for v, _ in found]}")
    for _, n in found:
        rewrite(constant_input(n), lambda a: (a * k).astype(a.dtype))
        changed += 1
    if changed != 15:
        raise ValueError(f"patched {changed} constants, expected 15")
    return model


def cmd_patch(args: argparse.Namespace) -> int:
    import onnx

    model = patch(onnx.load(str(args.src)), args.scale)
    onnx.checker.check_model(model)
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(model, str(args.dst))
    print(f"wrote {args.dst} (flow at {args.scale} x resolution)")
    return 0


def _resize(np: Any, img: Any, w: int, h: int) -> Any:
    """(3, H, W) float RGB to (3, h, w) with ffmpeg's bicubic (as mpv's scaler)."""
    _, ih, iw = img.shape
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gbrpf32le",
         "-s", f"{iw}x{ih}", "-i", "-", "-vf", f"scale={w}:{h}:flags=bicubic",
         "-f", "rawvideo", "-pix_fmt", "gbrpf32le", "-"],
        input=np.ascontiguousarray(img[[1, 2, 0]]).astype("<f4").tobytes(),
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    out = np.frombuffer(raw, dtype="<f4").reshape(3, h, w)
    return np.clip(out[[2, 0, 1]], 0, 1)


def detail(np: Any, img: Any, ref: Any) -> float:
    """High-frequency energy of ``img`` relative to ``ref`` (luma Laplacian
    variance): 1.0 = as sharp as the real frame, below = softer."""

    def lap_var(x: Any) -> float:
        y = 0.2126 * x[0] + 0.7152 * x[1] + 0.0722 * x[2]
        lap = -4 * y[1:-1, 1:-1] + y[:-2, 1:-1] + y[2:, 1:-1] + y[1:-1, :-2] + y[1:-1, 2:]
        return float(lap.var())

    return round(lap_var(img) / max(lap_var(ref), 1e-12), 3)


def save_crop(np: Any, img: Any, path: Path, box: tuple[int, int, int, int]) -> None:
    x, y, w, h = box
    crop = (np.clip(img[:, y : y + h, x : x + w], 0, 1) * 255 + 0.5).astype(np.uint8)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gbrp", "-s", f"{w}x{h}",
         "-i", "-", str(path)],
        input=np.ascontiguousarray(crop[[1, 2, 0]]).tobytes(), check=True,
    )  # fmt: skip


def cmd_quality(args: argparse.Namespace) -> int:
    import memguard

    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "rifescale"
    )
    memguard.Watchdog(label="rife_scale").start()

    import fp16_quality as fq
    import numpy as np
    import onnx
    import onnxruntime as ort

    if hasattr(ort, "preload_dlls"):
        ort.preload_dlls()
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    cuda = [("CUDAExecutionProvider", {"device_id": 0})]
    full = ort.InferenceSession(str(args.model), sess_options=opts, providers=cuda)
    half_model = patch(onnx.load(str(args.model)), 0.5)
    half = ort.InferenceSession(half_model.SerializeToString(), sess_options=opts, providers=cuda)
    inp = full.get_inputs()[0].name

    def rife(sess: Any, a: Any, c: Any) -> Any:
        h, w = a.shape[-2:]
        x = np.concatenate([a, c, np.full((1, h, w), 0.5, np.float32)])[None]
        xp, _, _ = fq.pad(np, x)
        return np.clip(sess.run(None, {inp: xp})[0][0, :, :h, :w], 0, 1)

    rows: list[dict[str, Any]] = []
    labels = args.label or []
    for i, spec in enumerate(args.clip):
        path, _, times = spec.rpartition("@")
        label = labels[i] if i < len(labels) else f"clip{i + 1}"
        w, h = fq.probe(path)
        for t in times.split(","):
            a, b, c = fq.frames(np, path, float(t), w, h)
            ref = rife(full, a, c)
            out: dict[str, Any] = {"clip": label, "t": float(t), "size": f"{w}x{h}"}
            out["blend"] = fq.psnr(np, (a + c) / 2, b)
            out["full"] = fq.psnr(np, ref, b)
            y = rife(half, a, c)
            out["half"] = fq.psnr(np, y, b)
            out["half_vs_full"] = fq.psnr(np, y, ref)
            out["half_px_over_10pct"] = round(float((np.abs(y - ref).max(0) > 0.1).mean() * 100), 3)
            out["detail_full"] = detail(np, ref, b)
            out["detail_half"] = detail(np, y, b)
            shots = {"real": b, "full": ref, "half": y}
            for name, sh in (("smaller_1080", 1080), ("smaller_720", 720)):
                sw = round(w * sh / h / 2) * 2 if h > w * 9 / 16 else round(sh * 16 / 9 / 2) * 2
                sh2 = round(h * sw / w / 2) * 2
                small = rife(full, _resize(np, a, sw, sh2), _resize(np, c, sw, sh2))
                up = _resize(np, small, w, h)
                out[name] = fq.psnr(np, up, b)
                out["detail_" + name] = detail(np, up, b)
                shots[name] = up
            if args.crops is not None:
                args.crops.mkdir(parents=True, exist_ok=True)
                box = (w // 2 - 320, h // 2 - 180, 640, 360)
                for k, img in shots.items():
                    save_crop(np, img, args.crops / f"{label[:8]}-{t}-{k}.png", box)
            print(json.dumps(out), file=sys.stderr, flush=True)
            rows.append(out)
    cols = (
        "blend", "full", "half", "smaller_1080", "smaller_720", "half_vs_full",
        "detail_full", "detail_half", "detail_smaller_1080", "detail_smaller_720",
    )  # fmt: skip
    print("| clip | t | " + " | ".join(cols) + " | half px > 10 % vs full |")
    print("|---|---|" + "---|" * (len(cols) + 1))
    for r in rows:
        vals = " | ".join(str(r[c]) for c in cols)
        print(f"| {r['clip']} | {r['t']:g} | {vals} | {r['half_px_over_10pct']} |")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("patch")
    p.add_argument("src", type=Path)
    p.add_argument("dst", type=Path)
    p.add_argument("--scale", type=float, default=0.5)
    q = sub.add_parser("quality")
    q.add_argument("--model", type=Path, required=True)
    q.add_argument("--clip", action="append", required=True, help="PATH@t1,t2,...")
    q.add_argument("--label", action="append")
    q.add_argument("--json", type=Path)
    q.add_argument("--crops", type=Path, help="write 640x360 centre crops of each variant here")
    q.add_argument("--mem-max", type=int, default=12288)
    args = ap.parse_args()
    return cmd_patch(args) if args.cmd == "patch" else cmd_quality(args)


if __name__ == "__main__":
    sys.exit(main())
