<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(m): quick wins inside mpv (performance track P1)

Date: 2026-10-10. Dev box: Fedora 44, Ryzen 9 5900X (12 cores / 24 threads), RTX 4090,
mpv 0.41.0, VapourSynth R72. In mpv with `--untimed --vo=null` on a generated nv12 source,
23.976 fps → 2×, 3 runs per setting (median), Xid faults counted from `journalctl -k`.
Raw numbers: `m0m-results.json`. Reproducer: `tools/perf/breakdown.py` (`Run.concurrent`).

## Questions

M0(i) left two P1 items: (1) mpv seemed to halve MVTools (78 vs 150 fps in vspipe), and
(2) RIFE-ncnn `gpu_thread` 8 was +15 % in vspipe; is it worth making the default?

## Results (output fps)

| Setting | 1080p | 2160p | Xid |
|---|---|---|---|
| MVTools, concurrent-frames 4 (M0(i)'s profiler) | 79.2 | — | 0 |
| MVTools, concurrent-frames 8 (product until now) | 128.3 | 27.9 | 0 |
| MVTools, concurrent-frames 12 | — | 33.5 | 0 |
| **MVTools, concurrent-frames 16** | **148.3** | **35.1** | 0 |
| MVTools, concurrent-frames 24 | 136.8 | 34.0 | 0 |
| RIFE-ncnn gpu_thread 4, concurrent 8 (default) | 70.0 (spread 8.6 %) | 14.4 (spread 20 %) | 0 |
| RIFE-ncnn gpu_thread 8, concurrent 8 | 68.1 (2.5 %) | 15.0 (2.5 %) | 0 |
| RIFE-ncnn gpu_thread 4, concurrent 12 | 69.9 | — | 0 |
| RIFE-ncnn gpu_thread 8, concurrent 12 | 74.2 | — | 0 |

## Findings

1. **mpv does not halve MVTools in the product.** M0(i)'s profiler gave MVTools 4
   concurrent frames; the product used 8 (128 fps, vs 150 in vspipe).
2. **16 concurrent frames is the MVTools sweet spot here:** +16 % at 1080p and +26 % at
   4K over 8, matching vspipe; 24 is slower again. **Applied:** `MVTOOLS_MAX_CONCURRENT`
   8 → 16 (still capped by the CPU count), used by live play and the speed test alike.
3. **`gpu_thread` 8 for RIFE-ncnn gains 0–6 %**, within run-to-run spread, though it is
   steadier. With the earlier fault history of this build (m0f.md) and TensorRT now the
   fast NVIDIA path, the default stays 4. No Xid in these 33 runs.

## Addendum (2026-10-10): the scene-change callback

P1 also listed replacing the per-frame Python scene-change callback
(`template.vpy` `_mark_scene_changes`: `std.PlaneStats` + `std.ModifyFrame`) with a native
filter. Fedora's VapourSynth R72 ships neither `misc.SCDetect` nor akarin, so a native
version would mean a new COPR plugin package. Measured first, through the real template on
the TensorRT path (fp16, half-precision frames, 3 runs each, vspipe `-r 8`):

| Size | Scene marking off | Scene marking on (0.12) | Cost |
|---|---|---|---|
| 1920×1080 → 2× | 201.7 fps | 200.6 fps | 0.5 % |
| 1280×720 → 2× | 485.7 fps | 484.7 fps | 0.2 % |

**Decision:** keep the Python callback; its cost is within run-to-run noise even at
TensorRT speeds, so a new plugin dependency isn't justified. P1 is closed.
