<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(i): performance breakdown (performance track P0)

Status: **in progress** (started 2026-10-09; 1080p recorded the same day). Scope and decision rule: SCOPE §15.1.
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

## Still to run

- [x] 1080p matrix with the plugins (above).
- [ ] 2160p rows and the 60 fps target (full run without `--quick`, ~10–15 min).
- [ ] Re-check `gpu_thread=8` for Xid faults over several runs (`journalctl -k`).
- [ ] Optional: a Nsight Systems trace of one `rife` run; the standalone `rife-ncnn-vulkan`
      image-pair rate as the inference ceiling.
- [ ] Reference numbers from other tools on the same clips (kept outside the repository).
