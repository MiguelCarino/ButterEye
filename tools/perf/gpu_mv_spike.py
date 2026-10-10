#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Spike M0(r): a GPU block motion-vector interpolator, the class of engine that
reaches 4K at 60/120 fps elsewhere. Prototype in CUDA (CuPy); a ButterEye engine
would be Vulkan compute for every vendor, so this answers feasibility: the GPU
time per frame at 1080p and 4K, the copies mpv's filter path adds, and the
picture against RIFE on the M0(q) frame triples.

Algorithm (one motion search per source pair, one render per in-between frame):
1. Luma pyramid (box 2x): full, 1/2, 1/4, 1/8.
2. Symmetric block search centred on the in-between frame: block b at p matches
   A(p - u) with C(p + u) (SAD), u = half the A→C motion. The coarsest level
   searches ±R exhaustively; each finer level refines the doubled vectors of the
   block and its four neighbours by ±1.
3. 3x3 component median of the vector field after every level.
4. Render: per pixel, the vectors interpolated bilinearly at x; output(t) =
   (1-t)·A(x - 2t·u) + t·C(x + 2(1-t)·u), bilinear sampling, per plane.

Usage (dev venv with cupy-cuda13x, onnxruntime-gpu not needed):
  gpu_mv_spike.py speed [--sizes 1920x1080,3840x2160] [--renders 4]
  gpu_mv_spike.py quality --clip PATH@t1,t2 [--label L] [--crops DIR] [--json OUT]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("CUDA_PATH", "/usr/local/cuda")

BLOCK = 8  # block size at every pyramid level (8 px = 64 px at the 1/8 level)
COARSE_RADIUS = 8  # ±8 at 1/8 = ±64 px of half-motion (±128 px A→C) at full size
LEVELS = 4

_SRC = r"""
extern "C" {

__global__ void luma(const float* rgb, float* y, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) y[i] = 0.2126f * rgb[i] + 0.7152f * rgb[n + i] + 0.0722f * rgb[2 * n + i];
}

__global__ void down2(const float* src, float* dst, int sw, int sh, int dw, int dh) {
    int x = blockIdx.x * blockDim.x + threadIdx.x, y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= dw || y >= dh) return;
    int x0 = min(2 * x, sw - 1), x1 = min(2 * x + 1, sw - 1);
    int y0 = min(2 * y, sh - 1), y1 = min(2 * y + 1, sh - 1);
    dst[y * dw + x] = 0.25f * (src[y0 * sw + x0] + src[y0 * sw + x1]
                               + src[y1 * sw + x0] + src[y1 * sw + x1]);
}

__device__ __forceinline__ float px(const float* p, int w, int h, int x, int y) {
    x = min(max(x, 0), w - 1); y = min(max(y, 0), h - 1);
    return p[y * w + x];
}

// one CUDA block per image block; one thread per candidate vector
// cand: predictors (from the parent level, already scaled) x refinement window
__global__ void search(const float* a, const float* c, int w, int h, int bw, int bh,
                       const int* parent, int pw, int ph, int radius, int npred,
                       int* out, float* cost) {
    int bx = blockIdx.x, by = blockIdx.y;
    if (bx >= bw || by >= bh) return;
    int win = 2 * radius + 1;
    int per = win * win;
    int k = threadIdx.x;
    extern __shared__ float sh[];
    float* best = sh;                 // blockDim.x
    int* bestv = (int*)(sh + blockDim.x);
    float s = 3.4e38f; int ux = 0, uy = 0;
    if (k < npred * per) {
        int pi = k / per, r = k % per;
        int px0 = 0, py0 = 0;
        if (radius == 1) {
            // finer levels: the doubled vectors of the parent block and its 4 neighbours
            int dx[5] = {0, -1, 1, 0, 0}, dy[5] = {0, 0, 0, -1, 1};
            int qx = min(max(bx / 2 + dx[pi], 0), pw - 1);
            int qy = min(max(by / 2 + dy[pi], 0), ph - 1);
            px0 = 2 * parent[2 * (qy * pw + qx)];
            py0 = 2 * parent[2 * (qy * pw + qx) + 1];
        }
        ux = px0 + r % win - radius; uy = py0 + r / win - radius;
        s = 0.f;
        int x0 = bx * 8, y0 = by * 8;
        for (int j = 0; j < 8; ++j)
            for (int i = 0; i < 8; ++i) {
                int x = x0 + i, y = y0 + j;
                s += fabsf(px(a, w, h, x - ux, y - uy) - px(c, w, h, x + ux, y + uy));
            }
        // a little bias towards short vectors (less noise on flat areas)
        s += 0.002f * (abs(ux) + abs(uy));
    }
    best[k] = s; bestv[2 * k] = ux; bestv[2 * k + 1] = uy;
    __syncthreads();
    for (int stride = blockDim.x / 2; stride > 0; stride >>= 1) {
        if (k < stride && best[k + stride] < best[k]) {
            best[k] = best[k + stride];
            bestv[2 * k] = bestv[2 * (k + stride)]; bestv[2 * k + 1] = bestv[2 * (k + stride) + 1];
        }
        __syncthreads();
    }
    if (k == 0) {
        out[2 * (by * bw + bx)] = bestv[0]; out[2 * (by * bw + bx) + 1] = bestv[1];
        cost[by * bw + bx] = best[0];
    }
}

__device__ int med9(int* v) {
    for (int i = 0; i < 5; ++i)
        for (int j = i + 1; j < 9; ++j)
            if (v[j] < v[i]) { int t = v[i]; v[i] = v[j]; v[j] = t; }
    return v[4];
}

__global__ void median(const int* src, int* dst, int bw, int bh) {
    int x = blockIdx.x * blockDim.x + threadIdx.x, y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= bw || y >= bh) return;
    for (int c = 0; c < 2; ++c) {
        int v[9], n = 0;
        for (int j = -1; j <= 1; ++j)
            for (int i = -1; i <= 1; ++i) {
                int xx = min(max(x + i, 0), bw - 1), yy = min(max(y + j, 0), bh - 1);
                v[n++] = src[2 * (yy * bw + xx) + c];
            }
        dst[2 * (y * bw + x) + c] = med9(v);
    }
}

__device__ float bil(const float* p, int w, int h, float x, float y) {
    x = fminf(fmaxf(x, 0.f), w - 1.001f); y = fminf(fmaxf(y, 0.f), h - 1.001f);
    int x0 = (int)x, y0 = (int)y; float fx = x - x0, fy = y - y0;
    const float* r0 = p + y0 * w; const float* r1 = r0 + w;
    return (r0[x0] * (1 - fx) + r0[x0 + 1] * fx) * (1 - fy)
         + (r1[x0] * (1 - fx) + r1[x0 + 1] * fx) * fy;
}

// render one in-between frame at time t from per-block half-vectors u (full size)
__global__ void render(const float* a, const float* c, float* out, int w, int h, int planes,
                       const int* u, int bw, int bh, float t) {
    int x = blockIdx.x * blockDim.x + threadIdx.x, y = blockIdx.y * blockDim.y + threadIdx.y;
    if (x >= w || y >= h) return;
    // bilinear vector interpolation between block centres
    float gx = (x + 0.5f) / 8.f - 0.5f, gy = (y + 0.5f) / 8.f - 0.5f;
    int x0 = (int)floorf(gx), y0 = (int)floorf(gy); float fx = gx - x0, fy = gy - y0;
    float ux = 0.f, uy = 0.f;
    for (int j = 0; j < 2; ++j)
        for (int i = 0; i < 2; ++i) {
            int bx = min(max(x0 + i, 0), bw - 1), by = min(max(y0 + j, 0), bh - 1);
            float wgt = (i ? fx : 1 - fx) * (j ? fy : 1 - fy);
            ux += wgt * u[2 * (by * bw + bx)]; uy += wgt * u[2 * (by * bw + bx) + 1];
        }
    // u is half the A→C motion on the middle grid: A at x - 2t·u, C at x + 2(1-t)·u
    float ax = x - 2.f * t * ux, ay = y - 2.f * t * uy;
    float cx = x + 2.f * (1 - t) * ux, cy = y + 2.f * (1 - t) * uy;
    int n = w * h;
    for (int p = 0; p < planes; ++p)
        out[p * n + y * w + x] = (1 - t) * bil(a + p * n, w, h, ax, ay)
                               + t * bil(c + p * n, w, h, cx, cy);
}
}
"""


class Engine:
    def __init__(self, cp: Any, w: int, h: int) -> None:
        self.cp, self.w, self.h = cp, w, h
        mod = cp.RawModule(code=_SRC, options=("--use_fast_math",))
        self.k = {n: mod.get_function(n) for n in ("luma", "down2", "search", "median", "render")}
        self.sizes = [(w, h)]
        for _ in range(LEVELS - 1):
            pw, ph = self.sizes[-1]
            self.sizes.append(((pw + 1) // 2, (ph + 1) // 2))

    def pyramid(self, rgb: Any) -> list[Any]:
        cp, w, h = self.cp, self.w, self.h
        n = w * h
        y = cp.empty(n, cp.float32)
        self.k["luma"](((n + 255) // 256,), (256,), (rgb, y, cp.int32(n)))
        levels = [y]
        for (sw, sh), (dw, dh) in zip(self.sizes, self.sizes[1:], strict=False):
            d = cp.empty(dw * dh, cp.float32)
            grid = ((dw + 15) // 16, (dh + 15) // 16)
            dims = (cp.int32(sw), cp.int32(sh), cp.int32(dw), cp.int32(dh))
            self.k["down2"](grid, (16, 16), (levels[-1], d, *dims))
            levels.append(d)
        return levels

    def vectors(self, pa: list[Any], pc: list[Any]) -> tuple[Any, int, int]:
        """Half-motion vectors per 8x8 block at full size (A at p-u, C at p+u)."""
        cp = self.cp
        parent, pw, ph = None, 0, 0
        for lvl in range(LEVELS - 1, -1, -1):
            w, h = self.sizes[lvl]
            bw, bh = (w + BLOCK - 1) // BLOCK, (h + BLOCK - 1) // BLOCK
            radius, npred = (COARSE_RADIUS, 1) if parent is None else (1, 5)
            ncand = npred * (2 * radius + 1) ** 2
            threads = 1 << (ncand - 1).bit_length()
            out = cp.empty(2 * bw * bh, cp.int32)
            cost = cp.empty(bw * bh, cp.float32)
            self.k["search"](
                (bw, bh), (threads,),
                (pa[lvl], pc[lvl], cp.int32(w), cp.int32(h), cp.int32(bw), cp.int32(bh),
                 # the coarsest level searches around (0, 0): a dummy, never read
                 parent if parent is not None else cp.zeros(2, cp.int32),
                 cp.int32(pw), cp.int32(ph), cp.int32(radius),
                 cp.int32(npred if parent is not None else 1), out, cost),
                shared_mem=threads * 12,
            )  # fmt: skip
            med = cp.empty_like(out)
            self.k["median"](((bw + 15) // 16, (bh + 15) // 16), (16, 16),
                             (out, med, cp.int32(bw), cp.int32(bh)))  # fmt: skip
            parent, pw, ph = med, bw, bh
        assert parent is not None
        return parent, pw, ph

    def render(self, a: Any, c: Any, u: Any, bw: int, bh: int, t: float, planes: int = 3) -> Any:
        cp, w, h = self.cp, self.w, self.h
        out = cp.empty(planes * w * h, cp.float32)
        self.k["render"](((w + 15) // 16, (h + 15) // 16), (16, 16),
                         (a, c, out, cp.int32(w), cp.int32(h), cp.int32(planes), u,
                          cp.int32(bw), cp.int32(bh), cp.float32(t)))  # fmt: skip
        return out


def _timed(cp: Any, fn: Any, n: int = 20) -> float:
    """Mean GPU milliseconds of ``fn`` over ``n`` runs (after one warm-up)."""
    start, stop = cp.cuda.Event(), cp.cuda.Event()
    fn()
    cp.cuda.Device().synchronize()
    start.record()
    for _ in range(n):
        fn()
    stop.record()
    stop.synchronize()
    return float(cp.cuda.get_elapsed_time(start, stop)) / n


def _measure(cp: Any, w: int, h: int) -> dict[str, float]:
    import numpy as np

    eng = Engine(cp, w, h)
    a = cp.random.default_rng(1).random(3 * w * h, dtype=cp.float32)
    c = cp.roll(a, 7)  # content doesn't change the cost
    state: dict[str, Any] = {}

    def me() -> None:
        state["u"] = eng.vectors(eng.pyramid(a), eng.pyramid(c))

    t_me = _timed(cp, me)
    u, bw, bh = state["u"]
    t_rgb = _timed(cp, lambda: eng.render(a, c, u, bw, bh, 0.5, planes=3))
    # mpv's filter path: one 10-bit 4:2:0 source frame up, every output frame down
    pinned = cp.cuda.alloc_pinned_memory(w * h * 3)  # 1.5 planes x 2 bytes
    host = np.frombuffer(pinned, dtype=np.uint8, count=w * h * 3)
    dev = cp.empty(w * h * 3, cp.uint8)
    return {
        "me": t_me,
        "render_yuv": t_rgb * 1.5 / 3,  # 4:2:0 = 1.5 planes' worth of pixels
        "up": _timed(cp, lambda: dev.set(host)),
        "down": _timed(cp, lambda: dev.get(out=host)),
    }


def cmd_speed(args: argparse.Namespace) -> int:
    import cupy as cp

    rows = []
    for spec in args.sizes.split(","):
        w, h = (int(x) for x in spec.split("x"))
        m = _measure(cp, w, h)
        for target, renders in (("2x", 1.0), ("24→60", 2.5), ("24→120", 4.0)):
            per_src = m["me"] + renders * (m["render_yuv"] + m["down"]) + m["up"]
            rows.append({
                "size": spec, "target": target, "me_ms": round(m["me"], 2),
                "render_yuv_ms": round(m["render_yuv"], 2), "up_ms": round(m["up"], 2),
                "down_ms": round(m["down"], 2), "ms_per_source_frame": round(per_src, 2),
                "max_source_fps": round(1000 / per_src, 1),
            })  # fmt: skip
    print("| size | target | ME ms | render ms (YUV) | up ms | down ms "
          "| ms per source frame | max source fps (needs 24) |")  # fmt: skip
    print("|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['size']} | {r['target']} | {r['me_ms']} | {r['render_yuv_ms']} "
            f"| {r['up_ms']} | {r['down_ms']} | {r['ms_per_source_frame']} "
            f"| {r['max_source_fps']} |"
        )
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    import cupy as cp
    import fp16_quality as fq
    import numpy as np
    import rife_scale as rs

    rows = []
    labels = args.label or []
    for i, spec in enumerate(args.clip):
        path, _, times = spec.rpartition("@")
        label = labels[i] if i < len(labels) else f"clip{i + 1}"
        w, h = fq.probe(path)
        eng = Engine(cp, w, h)
        for t in times.split(","):
            a, b, c = fq.frames(np, path, float(t), w, h)
            da, dc = cp.asarray(a.reshape(-1)), cp.asarray(c.reshape(-1))
            u, bw, bh = eng.vectors(eng.pyramid(da), eng.pyramid(dc))
            y = cp.asnumpy(eng.render(da, dc, u, bw, bh, 0.5)).reshape(3, h, w)
            y = np.clip(y, 0, 1)
            row = {"clip": label, "t": float(t), "size": f"{w}x{h}",
                   "blend": fq.psnr(np, (a + c) / 2, b), "gpu_mv": fq.psnr(np, y, b),
                   "detail_gpu_mv": rs.detail(np, y, b)}  # fmt: skip
            rows.append(row)
            print(json.dumps(row), file=sys.stderr, flush=True)
            if args.crops is not None:
                args.crops.mkdir(parents=True, exist_ok=True)
                rs.save_crop(np, y, args.crops / f"{label[:8]}-{t}-gpu_mv.png",
                             (w // 2 - 320, h // 2 - 180, 640, 360))  # fmt: skip
    print("| clip | t | blend | GPU motion vectors | detail |")
    print("|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['clip']} | {r['t']:g} | {r['blend']} | {r['gpu_mv']} | {r['detail_gpu_mv']} |")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("speed")
    s.add_argument("--sizes", default="1920x1080,3840x2160")
    s.add_argument("--json", type=Path)
    q = sub.add_parser("quality")
    q.add_argument("--clip", action="append", required=True, help="PATH@t1,t2,...")
    q.add_argument("--label", action="append")
    q.add_argument("--crops", type=Path)
    q.add_argument("--json", type=Path)
    args = ap.parse_args()
    return cmd_speed(args) if args.cmd == "speed" else cmd_quality(args)


if __name__ == "__main__":
    sys.exit(main())
