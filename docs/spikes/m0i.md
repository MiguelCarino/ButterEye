<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(i): performance breakdown (performance track P0)

Status: **in progress** (started 2026-10-09; 1080p and 2160p recorded the same day). Scope and decision rule: SCOPE §15.1.
Dev box: Fedora 44, VapourSynth R72, mpv 0.41.0, RTX 4090, Ryzen 9 5900X (12 cores / 24 threads).
Reproducer: `tools/perf/breakdown.py` with `tools/perf/stage.vpy` (generated clips only).

## Question

The earlier numbers say RIFE is not limited by the model: v4.26, v4.22-lite and v4.18 all run at
~69–70 fps in vspipe at 1080p 2× (bench.json, 2026-10-09), and a 4K 23.976 → 60 render ran at
8.25 output fps, about a quarter of the 1080p rate. Which step costs the time: colour conversion,
transfers, the plugin, the mpv filter path or an implicit Vulkan layer?

## How to run

```sh
python3 tools/perf/breakdown.py --mpv --json docs/spikes/m0i-results.json > docs/spikes/m0i-table.md
```

`--quick` runs 1080p only. Each run samples `nvidia-smi dmon -s ut` (SM %, PCIe rx/tx). Stages:
`source`, `convert` (YUV → RGBS → YUV, as template.vpy), `rife` (the live path), `rife-rgb`
(RIFE without per-frame conversion), `mvtools`. Model, `gpu_thread` (1/2/4/8), implicit Vulkan
layers off (`VK_LOADER_LAYERS_DISABLE=~implicit~`) and a 60 fps target are separate rows.

## Results, 1080p (2026-10-09, `--quick --mpv`)

Raw numbers: `docs/spikes/m0i-quick.json`. 23.976 fps source, 2× target, 8 concurrent
requests, 1 GB VapourSynth cache, run under the memory failsafes (`tools/memguard.py`).
"New fps" is interpolated frames per second; at 2× the source frames pass through.

| Host | Stage | Model | gpu_thread | Out fps | New fps | SM % |
|---|---|---|---|---|---|---|
| vspipe | convert (YUV → RGBS → YUV) | — | — | 246 | — | 2 |
| vspipe | rife | v4.26 | 4 | 76.8 | 38.4 | 67 |
| vspipe | rife | v4.22-lite | 4 | 75.6 | 37.8 | 54 |
| vspipe | rife | v4.18 | 4 | 74.8 | 37.4 | 62 |
| vspipe | rife-rgb (no conversion) | v4.26 | 4 | 72.6 | 36.3 | 63 |
| vspipe | rife-rgb | v4.26 | 1 | 29.9 | 14.9 | 32 |
| vspipe | rife-rgb | v4.26 | 2 | 50.5 | 25.2 | 46 |
| vspipe | rife-rgb | v4.26 | 8 | 82.4 | 41.2 | 74 |
| vspipe | rife, implicit Vulkan layers off | v4.26 | 4 | 78.0 | 39.0 | 73 |
| vspipe | mvtools | — | — | 161.6 | — | 0 |
| mpv | convert | — | — | 157.3 | — | 1 |
| mpv | rife | v4.26 | 4 | 71.9 | 36.0 | 56 |
| mpv | rife, layers off | v4.26 | 4 | 65.1 | 32.6 | 52 |
| mpv | mvtools (concurrent-frames 4) | — | — | 77.6 | — | 1 |

Findings at 1080p:

1. **Not the model.** Three models of clearly different cost run within 3 % of each other
   (~37–38 new frames/s), and the GPU is busy only 54–74 % of the time.
2. **Not the colour conversion.** RIFE without any per-frame conversion (`rife-rgb`) is no
   faster than the full path, and conversion alone runs at 246 fps.
3. **Latency-bound, per frame.** Throughput follows the number of frames in flight: 15 → 25 →
   36 → 41 new frames/s at `gpu_thread` 1, 2, 4, 8. Each frame waits on a synchronous
   upload → compute → download inside the plugin; more parallel jobs hide part of it, and
   the GPU never fills. This is the P2C signature (overlap transfers and compute, keep
   frames on the GPU), or a runtime that pipelines (P2A/P2B).
4. **The implicit Vulkan layer is not a factor** (+2 % in vspipe, −9 % in mpv: within run noise).
5. **mpv costs ~7 % for RIFE** (72 vs 77) but **halves MVTools** (78 vs 162). The mpv run used
   `concurrent-frames=4` against vspipe's 8 requests; P1 should have the speed test pick
   MVTools' `concurrent-frames` too.

**Decision (1080p):** GPU busy ~55–74 %, so by the SCOPE §15.1 rule this is overhead-bound:
P2C (plugin pipelining) and P2B (vs-mlrt runtimes) come before P2A. `gpu_thread=8` is a
cheap P1 gain (+13 %), but it must be re-checked against the earlier Xid faults (m0f.md)
before it becomes a default.

Earlier, plugin-free 2160p runs: colour conversion alone reaches only ~51 source frames/s at
4K, so at 4K the conversion does matter, unlike 1080p.

## Results, full run (2026-10-09, `--mpv`)

Raw numbers: `docs/spikes/m0i-results.json`; full table: `docs/spikes/m0i-table.md`. The GPU
showed ~15–22 % "SM busy" even in stages that don't use it (the desktop was running), so SM %
is high by about that much. No Xid faults in `journalctl -k` during the run (including
`gpu_thread=8`).

| Case | Out fps | New fps | Needed for real time | SM % |
|---|---|---|---|---|
| 1080p 2×, RIFE v4.26, vspipe | 71.1 | 35.5 | 48 out | 71 |
| 1080p 2×, RIFE v4.26, mpv | 65.2 | 32.6 | 48 out | 64 |
| 1080p 23.976 → 60, RIFE v4.26, vspipe | 40.4 | 40.4 | 60 out | 80 |
| 1080p 2×, `gpu_thread` 8 (rife-rgb) | 81.9 | 41.0 | — | 86 |
| 2160p 2×, RIFE v4.26 | 14.1 | 7.1 | 48 out | 32 |
| 2160p 2×, RIFE without conversion (rife-rgb) | 14.2 | 7.1 | 48 out | 60 |
| 2160p convert only | 50.4 src | — | — | 16 |
| 2160p 2×, MVTools (CPU) | 37.1 | 18.6 | 48 out | 0 |
| 1080p 2×, MVTools, vspipe / mpv | 149.8 / 73.9 | — | 48 out | — |

What the full run changes:

1. **1080p → 60 is not real time** with RIFE-ncnn v4.26 on the 4090: 40 fps against 60 needed.
   Only 2× (48) fits, with ~35 % headroom in mpv. The 60 fps and display targets must keep
   relying on the speed test (they do, §5.6), and 23.976 → 60 at 1080p is a P2 target, not a
   P1 one.
2. **At 4K the colour conversion is still not the limit.** RIFE with and without conversion
   both give 7.1 new frames/s, a 3.4× shortfall for 2× live. Conversion (~50 source fps)
   becomes the next wall only after RIFE gets ~7× faster.
3. **4K costs 5× 1080p, not 4×** (7.1 vs 35.5 new frames/s), so 4K is worse than the pixel
   count alone explains.
4. **More frames in flight helps only up to a point.** `gpu_thread` 8 gives +15 % (41 new
   frames/s) and the GPU is then ~86 % busy (~65–70 % after the desktop's share). Pipelining
   inside the plugin (P2C) can therefore win roughly 15–30 % at 1080p, not the 3–5× needed
   for 4K.
5. **Model-indifference still holds at `gpu_thread` 4** (v4.26 / v4.22-lite / v4.18: 35.5 /
   37.3 / 33.8). A model sweep at `gpu_thread` 8 is the open check: if lite pulls ahead
   there, the ncnn kernels are the ceiling.
6. **The implicit Vulkan layer doesn't matter** (71.2 vs 71.1; mpv 64.4 vs 65.2).
7. **mpv halves MVTools again** (74 vs 150) while costing RIFE only ~8 %. MVTools at 4K
   (37 out fps) can't do 2× live on this CPU in any case.

**Revised decision (supersedes the 1080p-only one above):** the RIFE-ncnn plugin path is
capped near ~41 new frames/s at 1080p and ~7 at 4K on an RTX 4090. Pipelining (P2C) is
worth doing but can't close a 3–7× gap. The big step has to come from a faster runtime:
**P2A (TensorRT, NVIDIA) and P2B (vs-mlrt's ncnn/ONNX Runtime, cross-vendor), benchmarked
side by side with `scale=0.5` for 4K**, with P2C as the fallback for GPUs neither covers.
P1 still takes: `gpu_thread` 8 (after repeated Xid checks), and the speed test choosing
MVTools' `concurrent-frames` inside mpv.

## Still to run

- [x] 1080p matrix with the plugins.
- [x] 2160p rows and the 60 fps target.
- [ ] Model sweep at `gpu_thread` 8 (are the ncnn kernels the ceiling?).
- [ ] Repeat `gpu_thread=8` runs with Xid checks before making it a default.
- [ ] MVTools in mpv at `concurrent-frames` 8 and 16.
- [ ] P2A/P2B: the same stages through vs-mlrt (TensorRT and ncnn/ORT), RIFE v4.22-lite,
      `scale` 1.0 and 0.5.
- [ ] Optional: a Nsight Systems trace of one `rife` run.
- [ ] Reference numbers from other tools on the same clips (kept outside the repository).
