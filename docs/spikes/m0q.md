<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(q): RIFE with half-resolution optical flow on TensorRT (4K)

Date: 2026-10-10. Dev box: RTX 4090, TensorRT 11.3, vs-mlrt vstrt v16.3.test1, RIFE v4.26
(vs-mlrt's v2 ONNX export), fp16 engines with half-precision frames. Tools:
`tools/perf/rife_scale.py` (patch + quality, ONNX Runtime CUDA fp32, which matched TensorRT
in M0(k)) and `tools/perf/breakdown.py --trt --onnx-models … --engine-dir …` (speed).
Raw results: `m0q-quality.json`, `m0q-detail.json`, `m0q-speed.json`.

## Question

At 4K, ButterEye smooths a smaller picture when RIFE can't keep up (720p for a 3840×1616
film on Vulkan). Practical-RIFE's `scale=0.5` estimates motion at half resolution and warps
and blends at full resolution; it is recommended for 4K. vs-mlrt supports it only for its
v1 exports below v4.7. Can v4.26 run that way, how much faster is it, and how does it look
against full flow and against today's smaller picture?

## The patch

The v2 export of v4.26 has the IFNet structure: four flow blocks, each shrinking its input
by 16, 8, 4, 2 (`/blockN/Resize`), growing the flow back (`/blockN/Resize_1`) and scaling
it (`/blockN/Mul`), with three Muls rescaling the flow for the next block (0.125, 0.25,
0.5). `scale=k` multiplies the down factors by k, divides the up factors and flow
multipliers by k, and multiplies the next-block rescales by k: 15 constants. The patch
refuses any graph whose constants differ. The engine built from ButterEye's generated script
(the same patch embedded in `template.vpy`) has the same content hash as the tool's.

## Quality (PSNR of the rebuilt middle frame against the real one; detail = high-frequency
energy relative to the real frame, 1.0 = as sharp)

| Clip | t | blend | full flow | **half flow** | smaller 1080p | smaller 720p |
|---|---|---|---|---|---|---|
| 4K live action (SDR) | 30 | 23.02 | 27.41 | 26.89 | 27.34 | 27.18 |
| 4K live action (SDR) | 90 | 22.49 | 27.82 | 27.47 | 27.89 | 27.82 |
| 4K live action (SDR) | 150 | 20.45 | 25.20 | 24.88 | 25.38 | 25.01 |
| 4K live action (SDR) | 200 | 20.26 | 20.99 | 21.10 | 21.03 | 20.99 |
| 3840×1616 anime (HDR10) | 600 | 42.37 | 51.15 | 50.51 | 43.71 | 39.03 |
| 3840×1616 anime (HDR10) | 1800 | 38.54 | 34.88 | 34.54 | 34.78 | 34.54 |
| 3840×1616 anime (HDR10) | 3000 | 37.45 | 31.66 | 31.70 | 31.65 | 31.61 |
| 3840×1616 anime (HDR10) | 4500 | 18.28 | 17.83 | 17.70 | 17.60 | 17.57 |

| Detail | full flow | half flow | smaller 1080p | smaller 720p |
|---|---|---|---|---|
| live action t=90 | 0.67 | 0.66 | 0.23 | 0.10 |
| anime t=600 | 0.99 | 0.91 | 0.34 | 0.13 |

(The HDR film is decoded with a BT.709 matrix here; identical for every variant.)

- **Half flow keeps the picture**: within 0.3–0.6 dB of full flow, nearly the same detail,
  0–3 % of pixels differ by more than 10 % (fast motion only). Crops: indistinguishable.
- **A smaller picture loses detail**, not motion accuracy: in fast motion every variant
  scores alike (motion errors dominate), but the in-between frames keep a third (1080p) to
  a tenth (720p) of the detail, and on slow, sharp scenes PSNR drops 7–12 dB. Live, those
  soft frames alternate with the sharp source frames.

## Speed (2160p, out fps; 2× needs 48)

| | full flow | half flow |
|---|---|---|
| vspipe 2×, 2 streams | 38.1 | 42.2 |
| vspipe 2×, 4 streams | 46.5 | 41.6 |
| **mpv 2×** | **44.5** | **49.4** |
| vspipe 24 → 60, 4 streams | 23.2 | 25.4 |
| mpv 24 → 60 | 20.9 | 22.1 |
| trtexec, GPU compute per inference | 38.3 ms | 35.3 ms |

- **Only 8–11 % faster.** v4.26 spends most of its 4K time in full-resolution layers (the
  feature encoder, warping and the final blend), which `scale` doesn't change; the flow
  blocks are about 8 % of the compute. The pipeline is already near that compute limit.
- That is enough at 4K 2× in mpv: 44.5 (stutters) → 49.4 (keeps up). 4K at 60 fps or more
  stays out of reach for RIFE on any engine here.

## Applied

- Live play with TensorRT: when full flow can't keep up at the video's own size and the frame
  is at least 2560×1440, half flow at full size (cap × 1.08, `decide.HALF_FLOW_GAIN`) comes
  before a smaller picture. Never when full flow keeps up. The row says "Estimating motion
  at half resolution so TensorRT can keep up at full size."
- Engines carry `flow_scale` (`EngineKey`; folder `…-flow0.5-…`; full-flow folders are
  unchanged); the generated script writes the patched model into the engine folder when it
  builds it.
- Faster than this at 4K needs a different kind of engine (GPU block motion vectors), not a
  cheaper RIFE.
