<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(x): RIFE finds the motion on a smaller picture (2026-11-14)

**Question.** RIFE at the output size is too slow above 1080p, and M0(v) showed that
long GPU bursts cost late refreshes on a 144 Hz screen. Can the RIFE network run on a
smaller copy of the two frames (k = 2/3, 1/2 or 1/3 of the size) while the final warp
and blend still run at full size ("net scale")? Does that shorten the burst enough to
matter, and above 1080p is it better than what the app does today when RIFE at the
output size cannot keep up: run RIFE on a smaller picture and scale the result up?

**Answer.** No; nothing of net scale is integrated.

- **1080p:** k < 1 loses quality broadly (k = 2/3: −0.025 worst areas on 164 moments),
  and 1080p stays at k = 1.
- **1440p and 4K output** (57 moments of open 4K footage, below): at the same network
  size net scale ties today's smaller picture on worst areas (−0.004 and −0.003) and
  is sharper over the whole frame (+0.9 and +0.6 dB mean luma PSNR, +0.1 dB median).
  But it costs about 20 % more GPU time, and because the frames are still moved and
  converted at the output size, the app path makes about a third of the new frames per
  second (74 against 233 at 1440p, 37 against 104 at 4K). Where it is cheaper than the
  next smaller picture up, it is worse. It never wins on worst areas at equal or lower
  cost.
- The late-frame budget in the plan (k = 1/2 at a 1440p frame within 4.1 ms) was missed
  at first (4.37 ms); with the precise-warp conversion it is now 4.05 ms at 2560×1440,
  but that no longer decides anything.

Two side results are kept:

- **Precise warp** (below, integrated): today's TensorRT RIFE drew its final warp from
  an fp16 grid; fixing the fp16 conversion gives +1.19 dB mean PSNR at 1080p for about
  0.1 ms.
- **Half flow** (the app's step before a smaller picture at 2560×1440 and up) is worse
  than the smaller picture at about twice its GPU time, at both 1440p and 4K output.
  The step was removed from live play on 2026-11-14 (Follow-ups).

## How it works

RIFE v4.26's output is exactly `m·warp(img0, f0) + (1−m)·warp(img1, f1)`; there is no
refinement step after the last block. The change is made to the ONNX graph before the
engine is built ("route G"), so the engine still takes and returns RGB pictures and
the rest of the pipeline (hold fill, cuts, the cascade) is unchanged:

- **Head.** Reflect-pad the input to a multiple of q, then average-pool by q (k = 1/2:
  q = 2; k = 1/3: q = 3). k = 2/3 first enlarges ×2 (linear), then pools by 3.
- **Network.** Unchanged, on the small picture (it still pads to a multiple of 64).
- **Tail.** The last block's flow and mask logit are enlarged to full size (linear),
  the flow multiplied by 1/k, then a new full-size GridSample (bilinear, border) warps
  both frames, the sigmoid is taken after enlarging, and the two are blended.
- **Precision.** The path from the flow to the final GridSample is listed by name and
  kept in fp32 when the model is converted to fp16. Without that list the flow and the
  sampling positions drop to fp16.
- The change finds the nodes by structure (it also works on v4.22 lite) and refuses
  any graph that does not match.

CPU checks with onnxruntime fp32 (2 models, 1080p and odd sizes such as 1918×1078):

| check | result | limit |
|---|---|---|
| k = 1 (new tail only) against the original graph, away from the edges | max 1.9e-4, mean ~1e-6 | 1e-4 |
| k = 2/3, 1/2, 1/3 against a numpy reference of the same maths, 36 runs | max 1.4e-4 | 1e-3 |
| changed graphs refused | 12 of 12 | |

The k = 1 excess is fp32 rounding of the sampling positions: against a float64 render
of the same flow, the original graph is 1.2–1.5e-4 off and the new tail 1.2–1.4e-4.

## Speed

RTX 4090, TensorRT 11.3, fp16 engines with half-float I/O, optimisation level 3, built
the way the app builds them. Single-stream GPU time is trtexec's median GPU compute time,
3 runs of 6 s each (mean of medians, range in brackets). New frames/s are from vspipe,
2 runs each of 200 to 600 frames, while other CPU work ran on the machine: read them as
rough figures. "Bare" feeds RIFE ready RGB frames; "app" includes the YUV↔RGB conversions.

| network input → output | GPU median, ms | p99, ms | new/s bare, 1 / 2 streams | new/s app, 1 / 2 streams |
|---|---|---|---|---|
| 1920×1080, k = 1 (today) | 8.05 (7.97–8.22) | 11.5 | 62 / 86 | 54 / 92 |
| 1920×1080, k = 1, new tail | 8.68 (8.53–8.96) | 12.6 | 66 / 99 | 62 / 96 |
| 1920×1080, k = 2/3 | 5.22 (5.19–5.25) | 7.9 | 70 / 97 | 61 / 97 |
| 1920×1080, k = 1/2 | 2.08 (2.08–2.08) | 3.4 | 113 / 183 | 97 / 130 |
| 2560×1440, k = 1/2 | **4.37 (4.36–4.38)** | 7.2 | 62 / 101 | 53 / 73 |
| 2560×1440, k = 1/3 | 2.23 (2.23–2.23) | 3.5 | 81 / 107 | 57 / 63 |
| 3440×1440, k = 1 (today) | 21.56 (20.90–21.96) | 25.7 | 27 / 41 | 26 / 38 |
| 3440×1440, k = 1/2 | **6.29 (6.23–6.40)** | 10.1 | 45 / 70 | 40 / 57 |
| 3440×1440, k = 1/3 | 3.05 (3.05–3.06) | 5.6 | 54 / 75 | 43 / 53 |
| 3840×2160, k = 1/2 | 10.21 (10.15–10.32) | 14.1 | 27 / 41 | 20 / 24 |
| 3840×2160, k = 1/3 | 5.26 (5.25–5.27) | 8.4 | 32 / 43 | 23 / 26 |

- **GPU time follows the network's padded size**, about 4 ms per megapixel, so the
  64-padding (720 → 768 lines) is paid too.
- **The added head and tail are small**: about 0.6 ms of 5.3 at 1080p k = 2/3 (mostly
  the ×2 enlarge), about 1.1 ms at 3440×1440 and 1.5 ms at 4K. The full-size GridSample
  alone is 0.06 ms at 1080p and 0.67 ms at 4K (one per-layer profile each, approximate).
- **New frames/s do not follow GPU time** once k < 1. At 4K, going from 1/2 to 1/3
  halves GPU time but adds only 2–3 new frames/s; the bare path rises 41 → 43. The limit
  is moving the frames between host and GPU and converting colour on the CPU, at the
  output size, which a smaller network does not change.
- With 4 streams the app path was slower than with 2 at both 1440p widths (62 against
  73, 48 against 57). Three of four 4K runs at 4 streams gave no result.

## Quality

`truth.py` on 164 real 1080p moments (the truth set without its five moments of other
widths), scored against the current TensorRT RIFE engine. Worst areas: higher is better.

| engine | worst areas | Δ mean | Δ median | moments losing > 0.05 | moments winning > 0.02 | luma PSNR | PSNR Δ median |
|---|---|---|---|---|---|---|---|
| today | 0.307 | 0 | 0 | 0 | 0 | 36.41 | 0 |
| today, final warp positions in fp32 | 0.307 | −0.000 | −0.000 | 1 | 5 | 36.51 | +0.06 |
| k = 1, new tail | 0.315 | +0.008 | +0.002 | 3 | 31 | 37.52 | +0.61 |
| k = 2/3 | 0.283 | −0.025 | −0.011 | 43 | 36 | 36.89 | +0.22 |
| k = 1/2 | 0.219 | −0.089 | −0.061 | 85 | 26 | 35.48 | −0.80 |

TensorRT fp16 against onnxruntime fp32 on 33 of those moments (the plan's gate: within
0.01 worst areas):

| model | fp32 | fp16 | Δ | PSNR fp32 → fp16 |
|---|---|---|---|---|
| today | 0.288 | 0.283 | −0.004 | 37.07 → 35.51 |
| k = 1, new tail | 0.288 | 0.291 | +0.004 | 37.08 → 36.93 |
| k = 2/3 | 0.236 | 0.240 | +0.004 | 36.30 → 36.14 |
| k = 1/2 | 0.135 | 0.144 | +0.009 | 34.99 → 34.31 |

- **Today's fp16 engine loses about 1.5 dB mean PSNR against fp32.** Keeping only the
  final warp positions in fp32 recovers 0.1 dB; the new tail recovers almost all of it.
  The cause is found and fixed in "Precise warp" below.
- k = 2/3 loses broadly, not on one outlier: 43 of 164 moments lose more than 0.05, and
  the per-clip means range from −0.003 to −0.051.
- The 1080p truth set says nothing about 1440p and 4K, where the network would run at
  720 lines or more; that is measured next.

## Above 1080p: net scale against the smaller picture

When RIFE at the output size cannot keep up, the app smooths a smaller picture
(`SMOOTH_HEIGHTS` 1440 / 1080 / 720, and the cascade's sizes down to 1080 lines) and mpv
scales the result up to the window. A 4K film on a 3440×1440 screen is smoothed at
2560×1440 (the screen's size) or, when that does not keep up, at 1920×1080. Net scale
would instead keep the output size and shrink only the network's input. The question is
whether it is better at equal or lower GPU cost.

**Footage.** The 2x-tier moments of the public benchmark (`public_bench.py`), taken at
full size instead of halved: Big Buck Bunny (60 fps, 23 moments), El Fuente Boat and
FoodMarket (60 fps, centre-cropped to 3840×2160, rounded to 8 bits, 6), the six SVT 2022
scenes (50 fps, exposed and tone-mapped as the benchmark does, at full size, 7), and the
seven UVG 120 fps scenes (21, sources 4 frames apart). 57 moments. Each has five native
frames: the engine gets frames 1, 3 and 5 (30 fps sources; 25 for SVT) and rebuilds 2
and 4, which are the truth. The 2560×1440 set is the same frames reduced by an exact 3:2
area average.

**Arms**, all fp16 TensorRT with half-float I/O and the precise-warp conversion, one
stream, the way the app builds its engines:

- **full size**: RIFE at the output size (the reference; it builds at 3840×2160 too).
- **smaller picture** at 1080 or 720 lines (1440 or 1080 for 4K): Spline36 down as
  `_shrink` does, RIFE at that size, then Lanczos 3 back up to the output size. mpv's
  own upscaler (here `ewa_lanczossharp`) is not reproduced exactly; Lanczos 3 stands in.
- **net scale** k = 1/2 and 1/3 at the output size (the graph change above).
- **half flow** (`flow_scale` 0.5) at the output size, today's step before a smaller
  picture at 2560×1440 and up.

Scores are `truth.py`'s: per moment the lower worst areas of the two in-between frames,
their mean luma PSNR, and squared error summed over all moments. One run per arm (the
engines are deterministic). "Floor" is the truth frame itself sent down and up through
the smaller picture's resampling: what a perfect engine at that size would score.

**Cost.** GPU time is trtexec's single-stream median GPU compute time, 6 runs of 6 s in
two rounds (median of medians, range). New frames/s are vspipe through the app's path
from a 4K source (Spline36 to the smoothing size where it differs, YUV → RGB, RIFE ×2,
RGB → YUV), with the app's stream count (4 at 2560×1440 and up, 2 below), 240 output
frames, 2 runs; they are short runs on a shared machine, so read them as rough figures.

### 2560×1440 output

| arm | network size | GPU ms | new frames/s | worst areas | Δ | luma PSNR | Δ | squared error |
|---|---|---|---|---|---|---|---|---|
| full size | 2560×1440 | 15.59 (15.56–16.01) | 56 (54–58) | 0.225 | 0 | 37.29 | 0 | 0 |
| half flow | 2560×1440 | 14.97 (14.95–15.70) | 62 (61–62) | 0.146 | −0.080 | 36.35 | −0.94 | +17.9 % |
| smaller picture, 1080 | 1920×1080 | 8.08 (8.07–8.09) | 104 (101–107) | 0.217 | −0.008 | 36.63 | −0.66 | +0.7 % |
| smaller picture, 720 | 1280×720 | 3.39 (3.38–3.40) | 233 (232–233) | 0.179 | −0.046 | 35.43 | −1.86 | +19.1 % |
| **net scale 1/2** | 1280×720 | 4.05 (4.05–4.26) | 74 (72–76) | 0.175 | −0.051 | 36.32 | −0.97 | +10.8 % |
| net scale 1/3 | 853×480 | 2.16 (2.15–2.28) | 74 (72–76) | 0.122 | −0.104 | 35.24 | −2.05 | +31.8 % |
| floor, 1080 | | | | 0.973 | | 50.32 | | |
| floor, 720 | | | | 0.910 | | 45.69 | | |

### 3840×2160 output

| arm | network size | GPU ms | new frames/s | worst areas | Δ | luma PSNR | Δ | squared error |
|---|---|---|---|---|---|---|---|---|
| full size | 3840×2160 | 35.17 (35.00–35.34) | 25 (24–26) | 0.192 | 0 | 37.04 | 0 | 0 |
| half flow | 3840×2160 | 32.91 (32.11–34.18) | 28 (28–29) | 0.131 | −0.061 | 36.29 | −0.75 | +9.9 % |
| smaller picture, 1440 | 2560×1440 | 15.59 | 56 | 0.187 | −0.005 | 36.50 | −0.54 | −1.0 % |
| smaller picture, 1080 | 1920×1080 | 8.08 | 104 | 0.176 | −0.017 | 35.95 | −1.09 | +1.8 % |
| **net scale 1/2** | 1920×1080 | 9.84 (9.69–10.03) | 37 (37–37) | 0.173 | −0.020 | 36.51 | −0.53 | −0.7 % |
| net scale 1/3 | 1280×720 | 5.07 (4.96–5.18) | 37 (36–38) | 0.126 | −0.066 | 35.82 | −1.22 | +10.7 % |
| floor, 1440 | | | | 0.970 | | 50.74 | | |
| floor, 1080 | | | | 0.931 | | 47.83 | | |

(The smaller pictures' GPU time and new frames/s are those of the 1440p table: the same
engines, fed from the same 4K source.)

**Head to head**, net scale against the smaller picture with the same network size
(first against second; "moments" counts those losing / winning more than 0.02 worst
areas, and those with better PSNR):

| comparison | worst areas Δ mean / median | moments losing / winning | PSNR Δ mean / median | moments with better PSNR | squared error | GPU time |
|---|---|---|---|---|---|---|
| 1440p: net 1/2 vs 720 lines | −0.004 / −0.004 | 11 / 5 | +0.89 / +0.10 | 37 of 57 | −7.0 % | +0.66 ms (+19 %) |
| 4K: net 1/2 vs 1080 lines | −0.003 / −0.002 | 4 / 1 | +0.56 / +0.06 | 32 of 57 | −2.5 % | +1.76 ms (+22 %) |

and against the next smaller picture up, which costs more:

| comparison | worst areas Δ mean / median | moments losing / winning | PSNR Δ mean / median | squared error | GPU time |
|---|---|---|---|---|---|
| 1440p: net 1/2 vs 1080 lines | −0.042 / −0.025 | 33 / 4 | −0.31 / −0.15 | +9.9 % | −4.03 ms (−50 %) |
| 4K: net 1/2 vs 1440 lines | −0.015 / −0.008 | 20 / 10 | +0.01 / −0.09 | +0.2 % | −5.75 ms (−37 %) |

By footage, net 1/2 against the smaller picture of the same network size: the PSNR gain
is in the CG film (+1.58 dB at 1440p, +1.08 at 4K) and the live 60/50 fps scenes (+1.15,
+0.68); on the UVG scenes it is nil (−0.02, −0.09). Worst areas are within −0.001 to
−0.008 in every group. (UVG's worst areas sit near zero for every engine: its worst
blocks are fine grain and water, which no engine rebuilds.)

What the tables show:

- **At the same network size the two tie on worst areas**, and net scale is sharper over
  the whole frame, mostly where the picture has fine detail at rest. This is the CPU
  probe's result (worst areas a draw, PSNR better), at about half the PSNR gain.
- **Net scale is not cheaper for it.** It takes 0.7 ms (1440p) and 1.8 ms (4K) more GPU
  time than RIFE on the same network size, for its full-size head and tail, and the
  frames still travel to the GPU and through the RGB conversion at the output size. On the app path that is the limit: 74 against 233 new
  frames/s at 1440p, 37 against 104 at 4K. A 24 fps film needs 24 new frames/s for 2×,
  so 4K net scale leaves little room for a cascade or for anything else on the CPU.
- **Cheaper than the next size up, it is worse.** At 1440p, net 1/2 costs half of RIFE at
  1080 lines and loses 0.042 worst areas; at 4K, 37 % less than 1440 lines and 0.015.
- **k = 1/3 loses at every comparison** (−0.05 to −0.06 worst areas against the smaller
  picture of similar cost).
- **The smaller picture's own resampling costs little** on these moments: its floor is
  0.93–0.97 worst areas and 46–51 dB, far above any engine. What it loses is the network
  working on fewer pixels, which net scale loses the same way. (M0(q) reported 7–12 dB
  lost on slow, sharp frames; over this set the smaller picture at 1080 lines is 0.66 dB
  under full size at 1440p and 1.09 dB at 4K.)
- **Half flow loses to the smaller picture at about twice its GPU time**: at 1440p 0.146
  worst areas at 15.0 ms against 0.217 at 8.1 ms (1080 lines); at 4K 0.131 at 32.9 ms
  against 0.187 at 15.6 ms (1440 lines). Its PSNR loss against full size (−0.75 dB at 4K)
  is close to M0(q)'s −0.3 to −0.6 dB; worst areas, which M0(q) did not score, show the
  difference.

## Verdict

| gate | result |
|---|---|
| k = 1/2 at a 1440p frame ≤ about 4.1 ms single-stream | first build 4.37 ms (2560 wide), 6.29 ms (3440 wide); with the precise-warp conversion 4.05 ms at 2560×1440 (6 runs) |
| fp16 within 0.01 of the fp32 scores | met: +0.004, +0.004, +0.009 for k = 1, 2/3, 1/2 |
| better than the smaller picture at equal or lower GPU cost, 1440p and 4K | **not met**: a tie on worst areas at the same network size for about 20 % more GPU time and a third or less of the new frames/s; worse where cheaper |
| real-window late frames | not run: nothing to integrate |

- Net scale is not integrated: no setting, engine key or decision rule in the app uses it.
- 1080p stays at k = 1.
- At 4K the limit of the app path is moving and converting frames at the output size,
  not the network. A smaller network alone does not change that; keeping the frames on
  the GPU (a Vulkan warp next to mvgpu, "route W") would be the way, and these numbers do
  not make the case for it: the smaller picture already gets within 0.005–0.017 worst
  areas of full size.

## Follow-ups

- **Half flow**: the app tried it before a smaller picture at 2560×1440 and up
  (`HALF_FLOW_MIN_PIXELS`). On this set it is dominated by the smaller picture in both
  quality and GPU time. **Removed 2026-11-14**: live play now goes from full size
  straight to the smaller picture (`decide.py`, `session.py`; remembered live limits
  lost their flow dimension). `EngineKey.flow_scale` and `_scale_flow` stay because the
  public benchmark still runs a half-flow engine.
- The new tail at k = 1 as a quality fix: superseded by precise warp (below), which gets
  the same gain without changing the graph.
- Not measured: 3440×1440 output (the 2560×1440 numbers are the screen-fit size of a 4K
  film on a 3440×1440 screen), the cascade's motion-vector stage at 1440 lines against
  1080 lines, and the private 1080p set at 1440p (there is no higher-resolution source
  for it).

## Precise warp (2026-11-14)

**Question.** Why does TensorRT RIFE (fp16) put fine detail 0.05–0.13 px off position,
when ncnn RIFE with the same model does not, and what is the smallest change that
removes it?

**Answer.** The final warp's sampling grid passed through fp16. The fp16 conversion
keeps the nodes it is told to keep in fp32, but where a kept node's output has more than
one user it casts that output to fp16 once and back to fp32 for each kept user. In
RIFE's export the base grid (`linspace(−1, 1)` over the padded frame) and the flow
divisor 2/(W−1) are read by all five warps, so even with every position node kept (the
`grid` setting DRBA uses) the final warp read a grid rounded to fp16. Near ±1 a half
float is 1/2048 apart: up to 0.23 px at 1920 wide (mean 0.08 px) and 0.47 px at 3840.
Without the keep list (RIFE until now) the grid sum itself was fp16 as well.

**Result.** On 164 real 1080p moments: +1.19 dB mean luma PSNR (+0.64 median, better on
150), 5.9 % less squared error, +0.005 worst areas; about 0.1 ms more GPU time per
in-between frame at 1080p, and no change in dropped or late frames in a real window.

The fix is in the fp16 conversion only (`_fp16_model` in template.vpy, "precise warp"):

- Keep in fp32 the final warp (the GridSample the output is read from, found from the
  graph output through the blend) and every node that works out where it reads. The
  walk follows what does not depend on the picture (base grid, divisor) and stops at a
  network tensor that other nodes read too: the flow sum that the earlier warps and the
  last block read stays fp16, exactly as the network computed it.
- After the conversion, a kept node's fp32 output goes straight to its kept users (the
  fp32 → fp16 → fp32 hop is removed); fp16 users keep their cast.
- The graph is unchanged: no new nodes, same output for every pixel at fp32. All RIFE
  TensorRT paths share the engine (plain, cascade, hold fill). DRBA is unchanged.

Five engines were measured, at 1920×1080 with the same settings as above:

| engine | what stays fp32 |
|---|---|
| today | Cast nodes only |
| grid | every warp's position nodes (DRBA's setting), hops left in |
| new tail (k = 1) | the patched graph from above, its keep list, `grid` |
| warp, all | final warp positions back to the network, flow sums included, no hops |
| **precise warp** | final warp positions, flow sums the network shares left fp16, no hops |

**Quality.** `truth.py` on the 164 1080p moments (one run each; the engines are
deterministic: a second run of today's engine gave identical scores). Squared error is
the sum over all moments, computed from luma PSNR.

| engine | worst areas | Δ mean | Δ median | moments losing / winning > 0.02 | luma PSNR | PSNR Δ mean / median | moments with better PSNR | squared error |
|---|---|---|---|---|---|---|---|---|
| today | 0.3071 | 0 | 0 | – | 36.41 | 0 | – | 0 |
| grid | 0.3071 | −0.0001 | 0.0000 | 4 / 5 | 36.51 | +0.10 / +0.06 | 131 | −0.6 % |
| new tail (k = 1) | 0.3155 | +0.0083 | +0.0019 | 10 / 33 | 37.52 | +1.11 / +0.61 | 150 | −5.7 % |
| warp, all | 0.3125 | +0.0053 | +0.0011 | 8 / 25 | 37.60 | +1.19 / +0.64 | 149 | −5.9 % |
| **precise warp** | 0.3126 | +0.0054 | +0.0011 | 9 / 25 | 37.60 | +1.19 / +0.64 | 150 | −5.9 % |

- Precise warp and "warp, all" score the same: per moment the worst areas differ by at
  most 0.006 and PSNR by at most 0.02 dB (equal on 124 of 164). The shared flow sum in
  fp16 costs nothing measurable.
- The new tail scores +0.003 worst areas better and 0.08 dB PSNR worse than precise
  warp. That difference is not precision: at fp32 the two graphs differ only where a
  sample falls in the export's padding below the frame (8 lines at 1080; the new tail
  clamps at the frame's edge, the export reads the reflection), see the CPU checks above.

**Speed.** trtexec single-stream GPU compute time, interleaved runs of 6 s, with other
work on the machine (the medians wander by up to 0.7 ms between rounds; the minimum is
steady to 0.05 ms, so both are given).

| engine | runs | min, ms | median of medians, ms (range) | p99 (median), ms |
|---|---|---|---|---|
| today | 15 | 7.95 | 8.34 (7.97–8.68) | 9.15 |
| grid | 3 | 8.51 | 8.55 (8.54–9.01) | 9.52 |
| new tail (k = 1) | 15 | 8.50 | 8.54 (8.52–9.28) | 9.75 |
| warp, all | 9 | 8.68 | 9.19 (8.71–9.55) | 10.24 |
| **precise warp** | 9 | 8.06 | 8.10 (8.08–8.82) | 9.13 |

- Precise warp costs about 0.1 ms: in the 6 runs where it alternated directly with
  today's engine the medians were 8.08–8.52 ms against 7.97–8.49 ms. The new tail costs
  about 0.55 ms and "warp, all" 0.7 ms.
- Keeping the shared flow sums in fp32 ("warp, all") cost 0.6 ms more than precise warp
  for no gain: the earlier warps and the last block then read them through casts.
- A final GridSample that reads the picture in fp16 with an fp32 grid (allowed by ONNX)
  would save the fp32 copy of both frames, but TensorRT 11.3 refuses it ("`input` and
  `grid` must be of same type").

**Real window.** A 1080p 23.976 fps live-action film (1920×1080), fullscreen on the
144 Hz screen, the app's own choice: RIFE TensorRT to 48 fps, the motion-vector cascade
to 144. Engines in a temporary profile; the "today" arm used the key from before precise
warp (its folder name matched the one the app had built for that size before). Six
runs of 60 s of steady play, alternating, after one warm-up run per arm; one run each.

| run | engine | dropped | delayed | whole GPU, mean / peak |
|---|---|---|---|---|
| 1 | today | 0 | 0 | 49 / 68 % |
| 1 | precise warp | 0 | 1 | 48 / 65 % |
| 2 | today | 0 | 0 | 49 / 59 % |
| 2 | precise warp | 0 | 2 | 50 / 64 % |
| 3 | today | 0 | 1 | 47 / 56 % |
| 3 | precise warp | 0 | 1 | 48 / 62 % |

No frame was dropped in either arm. Delayed frames were 1 of 25 920 refreshes for today's
engine and 4 for precise warp (3 × 60 s × 144 Hz each); at these counts that is within run-to-run noise, and the
GPU share is the same (mean 48 % against 49 %). The health check reported "ok" in every
half-second of every run.

**Engines.** `EngineKey.precise_warp` (default on) puts precise-warp engines in folders
of their own; a key with `precise_warp=False` names exactly the folder of an engine built
before (DRBA's folders do not change). Every RIFE TensorRT engine already in a user's
cache is therefore built again once, at its first use (24–52 s per size on an RTX 4090;
the app plays with another engine meanwhile, as for any missing engine). The old
folders stay until the cache is cleared from the storage page.

### Half flow at 1080 lines (fixed)

`EngineKey(flow_scale=0.5)` failed to build at 1920×1080: "IShuffleLayer /Reshape_13:
reshape changes volume [1,4,1152,1920] → [2,2,1088,1920]". The export pads the input to
a multiple of 64 so that the first block's picture (1/16 of the frame, halved twice
inside the block) comes out whole. At flow scale k that picture is k/16 of the frame, so
the pad must be 64/k: 1080 lines pad to 1088 = 8.5 × 128, the first block rounded up and
returned 1152 lines. `_scale_flow` now rewrites the export's six padding constants to
64/k (and refuses a k for which that is not whole). The same failure hit every height
whose 64-padding is an odd multiple of 64 (1080, 1440, 800 lines); 720 and 2160 lines
built before by chance. With the fix, engines build at 1920×1080, 1280×720, 2560×1440,
3840×2160 and 1920×800 (one build each, 25–37 s).

On the 164 1080p moments the fixed half-flow engine (final warp in fp32) scores 0.228
worst areas against 0.307 for today's full-flow engine (−0.080 mean, −0.031 median, 90
moments lose more than 0.02) and +0.35 dB PSNR: the pictures are sound, and the app
kept half flow to 2560×1440 and above (`HALF_FLOW_MIN_PIXELS`, removed 2026-11-14),
which could not build at 1440 lines before (1440 pads to 1472, an odd multiple of 64; derived from the
cause, not rebuilt with the old code).
