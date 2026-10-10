<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(p): MVTools at 4K (performance track P3)

Date: 2026-10-10. Dev box: Ryzen 9 5900X (24 threads), VapourSynth R72, MVTools v24
(ButterEye COPR build), mpv 0.41.0. Reproducer: `tools/perf/mvtools_spike.py` +
`mv_stage.vpy`; mpv rows with `tools/perf/breakdown.py` (`Run.mv_mode`, concurrent-frames
16). Raw numbers: `m0p-results.json`; table: `m0p-table.md`.

## Question

The CPU path reaches ~35 fps at 2160p 2× (M0(m)); real time needs 48. Which MVTools
settings close the gap, and at what cost in picture quality? This MVTools has no
`ScaleVect`, so motion search on a downscaled copy (rendering at full size) isn't available.

## Speed (vspipe, `-r 16`, panning texture, median of 3) and fidelity

Fidelity: eight real frame triples (four from a 1920×804 film, two from an anime film,
two from a 4K live-action video); each variant rebuilds the middle frame from its
neighbours, scored against the real one (PSNR); a plain blend scores 33.21 dB.

| Variant | 1080p fps | 2160p fps | PSNR vs real | vs default |
|---|---|---|---|---|
| default: blksize 16, overlap 4, FlowFPS | 136.7 | 35.3 | 33.62 | — |
| blksize 16, overlap 0 | 148.5 | 36.5 | 33.63 | +0.02 |
| blksize 16, overlap 4, luma-only search | 146.3 | 35.8 | 33.63 | +0.02 |
| blksize 32, overlap 8 | 149.8 | 36.8 | 33.61 | −0.01 |
| blksize 32, overlap 8, luma-only | 144.0 | 37.3 | 33.61 | −0.01 |
| blksize 32, overlap 0 | 152.9 | 37.5 | 33.64 | +0.02 |
| **blksize 16, overlap 4, BlockFPS** | **392.8** | **90.4** | 33.69 | +0.07 |
| blksize 32, overlap 8, BlockFPS | 427.1 | 96.3 | 33.68 | +0.06 |

Inside mpv (concurrent-frames 16, testsrc2): FlowFPS 140.7 / 31.8 fps, **BlockFPS
306.8 / 72.1 fps** at 1080p / 2160p (2.2× / 2.3×).

## Findings

1. **The motion search is not the bottleneck.** Bigger blocks, no overlap and luma-only
   search gain at most 6 % at 4K; FlowFPS's per-pixel warping is where the time goes.
2. **BlockFPS is 2.2–2.9× faster** with the same fidelity (+0.07 dB). At 4K it reaches
   72 fps in mpv, real time for 2× with headroom.
3. **Its weakness is visible only in very fast motion:** on the fastest frame both modes get
   the motion wrong (FlowFPS smears into blobs, BlockFPS into rectangles; blocks are
   more noticeable); on ordinary motion the two look the same (crops checked locally).

## Applied

- Live play: when MVTools is the engine and FlowFPS can't keep up at the video's own size,
  MVTools switches to **BlockFPS at full size** (cap = FlowFPS cap × 2.0, a little under
  the measured 2.2–2.3×) instead of smoothing at a smaller size, with the notice "Using
  MVTools' faster block mode to keep up; very fast motion may look blocky". A GPU engine
  at a smaller size still wins (RIFE at 1440p over MVTools blocks at 4K).
- FlowFPS stays the default everywhere it keeps up, and for renders (time is cheap
  offline).
- The analysis settings are unchanged (no measurable gain).
