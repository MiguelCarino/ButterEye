<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(r): a GPU block motion-vector engine (4K at 60/120 fps)

Date: 2026-10-10. Dev box: RTX 4090. Prototype: `tools/perf/gpu_mv_spike.py` (CUDA
kernels through CuPy 14.2 in the dev venv). Raw results: `m0r-speed.json`,
`m0r-quality.json`; RIFE and smaller-size numbers for the same frames: `m0q-quality.json`.

## Question

RIFE can't reach 4K at 60 fps or more on any engine here (M0(j), M0(l), M0(q): 38 ms per
4K inference on TensorRT). Real-time 4K 120 fps elsewhere comes from block motion vectors
on the GPU. How fast is that class of engine on this GPU, including the copies mpv's
VapourSynth filter path adds, and how does it look against RIFE?

## The prototype (about 200 lines of CUDA)

1. Luma pyramid: full, 1/2, 1/4, 1/8 (2x box).
2. Symmetric block search centred on the in-between frame (8x8 blocks, SAD): block p
   matches A(p - u) with C(p + u), so the vector field has no holes. ±8 exhaustive at 1/8
   (±128 px of motion at full size), then ±1 around the doubled vectors of the block and
   its 4 neighbours at each finer level.
3. 3x3 component median of the vectors after every level.
4. Render per pixel: vectors interpolated between block centres, then
   (1-t)·A(x - 2t·u) + t·C(x + 2(1-t)·u), bilinear. One search per source pair serves every
   in-between frame.

No occlusion handling, no sub-pixel search, no overlapped blocks, no scene detection.

## Speed (ms on the GPU; copies: one 10-bit 4:2:0 frame up per source frame, one down per
output frame, pinned memory)

| Size | Search | Render (4:2:0) | Up | Down | 24 → 48 | 24 → 60 | 24 → 120 |
|---|---|---|---|---|---|---|---|
| 1920×1080 | 0.23 | 0.02 | 0.31 | 0.25 | 0.8 ms / source frame | 1.2 | 1.6 |
| 3840×2160 | 0.99 | 0.16 | 0.99 | 0.96 | 3.1 | 4.8 | **6.5** |

At 4K, 24 → 120 needs 24 source frames a second; this does about **155**: 6x headroom,
with the copies included. For comparison one RIFE inference at 4K is 38 ms on TensorRT.

## Quality (PSNR of the in-between frame against the real one; detail 1.0 = as sharp)

| Clip | t | blend | RIFE v4.26 (M0(q)) | **GPU vectors** | detail |
|---|---|---|---|---|---|
| 4K live action | 30 | 23.02 | 27.41 | 25.85 | 0.64 |
| 4K live action | 90 | 22.49 | 27.82 | 25.94 | 0.63 |
| 4K live action | 150 | 20.45 | 25.20 | 23.03 | 0.70 |
| 4K live action | 200 | 20.26 | 20.99 | 20.45 | 0.68 |
| 3840×1616 anime | 600 | 42.37 | 51.15 | **51.32** | 0.87 |
| 3840×1616 anime | 1800 | 38.54 | 34.88 | **35.28** | 0.90 |
| 3840×1616 anime | 3000 | 37.45 | 31.66 | **32.38** | 0.31 |
| 3840×1616 anime | 4500 | 18.28 | 17.83 | 17.42 | 0.70 |

- **Pans and moderate motion: as good as RIFE**, and as sharp (crops of the anime pan are
  indistinguishable from the real frame and RIFE).
- **Fast-moving objects: ~2 dB below RIFE**, still ~3 dB above blending. The crop shows the
  known failure of plain block vectors: a ghosted, doubled outline at the edge of a fast
  swinging sleeve (occlusion), where RIFE gives a coherent, slightly blurred shape.
- Detail is kept (the frames are warped at full size, never shrunk).

## Verdict: go

Speed is not the problem for this class of engine: 4K 120 fps fits with room for better
quality. The work is quality at object edges, which is well-known engineering, not research.

## Plan for a ButterEye engine ("GPU motion", working name `vs-mvgpu`)

1. **Plugin**: a VapourSynth plugin in C++ with Vulkan compute (GLSL → SPIR-V), so AMD,
   Intel and NVIDIA all work (CUDA was only for this spike). YUV 4:2:0 in and out, 8/10-bit;
   luma for the search, all planes rendered. Packaged in the COPR like the other plugins.
   Licence to decide (owner): it loads into mpv's process like RIFE-ncnn.
2. **Quality**, measured on the M0(q) triples plus crops, target: within 1 dB of RIFE on
   fast live action, no visible ghosting:
   - forward and backward vectors with a consistency check; occluded pixels take the side
     that sees them (the standard occlusion fix for the sleeve case);
   - overlapped block rendering (smooth vector field at block edges);
   - 4x4 blocks at the finest level and half-pixel refinement;
   - per-block match cost to fall back to blending where vectors are unreliable, and scene
     change detection (repeat instead of interpolate).
3. **Integration**: a third live engine in `decide` (its speed from the speed test, like the
   others), offered as a Smoothness choice ("Fastest, any resolution" or similar), and as
   the automatic choice where RIFE can't keep up instead of a smaller picture or MVTools.
   Renders keep RIFE.
