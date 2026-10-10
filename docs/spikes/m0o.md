<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(o): mpv's scalers and shader upscalers (SCOPE §15.2)

Date: 2026-10-10. Dev box: RTX 4090, ffmpeg 8.1.3 `libplacebo` filter on Vulkan, which is
the renderer mpv 0.41's `gpu-next` uses (libplacebo 7.360). Reproducer:
`tools/perf/scaler_spike.py`. Raw numbers: `m0o-results.json`; table: `m0o-table.md`.
Same frames as M0(n) (three and two frames of two anime films, two of a 4K live-action
video), shrunk 2× with Bicubic as BT.709-tagged yuv420p10 and brought back by each method.

Harness notes: ffmpeg's swscale and libplacebo convert YUV → RGB differently (about 38 dB
apart even unscaled), so the reference frame goes through the same libplacebo path as
every upscale; libplacebo's Lanczos then scores 45.84 dB on the frame where zimg's scored
45.87 in M0(n). Every libplacebo output is capped at one frame (it can emit two from one
input when timestamps look like a rate change).

## Results

PSNR against the original (fidelity), averaged per group, as the difference to Lanczos;
time is the extra GPU time per frame at 1080p → 2160p over bilinear (ffmpeg's upload and
download are in both and cancel out; mpv does neither).

| Method | Licence | Anime (5 frames) | Live action (2) | Luma, live | +ms / 4K frame |
|---|---|---|---|---|---|
| bilinear | libplacebo LGPL-2.1+ | −2.61 dB | −1.58 | −1.59 | 0 |
| spline36 | libplacebo | −0.13 | −0.12 | −0.12 | 0.3 |
| lanczos | libplacebo | 0 (44.10 dB) | 0 (45.15 dB) | 0 | 0.4 |
| ewa_lanczos | libplacebo | −0.29 | −0.22 | −0.23 | 0.6 |
| ewa_lanczossharp | libplacebo | −0.19 | −0.15 | −0.16 | 0.7 |
| ewa_lanczos4sharpest | libplacebo | −0.11 | −0.27 | −0.28 | 1.0 |
| **FSRCNNX x2 8-0-4-1** | GPL-3.0 | **+0.51** | **+1.25** | +1.27 | **1.7** |
| FSRCNNX x2 16-0-4-1 | GPL-3.0 | +0.70 | +1.07 | +1.09 | 3.4 |
| **ArtCNN C4F16** | MIT | +0.41 | **+1.75** | +1.78 | 2.9 |
| RAVU-lite ar r4 | LGPL-3.0 | −0.05 | −0.16 | −0.17 | 1.2 |
| Anime4K Upscale CNN x2 M | MIT | −0.78 | +0.83 | +0.84 | 0.8 |
| RAVU r3 yuv | LGPL-3.0 | (−5.62) | (−2.75) | | 1.1 |

RAVU r3's YUV variant is excluded: a 5.6 dB loss, worse than bilinear-level results,
points to this harness applying it wrongly rather than to the shader.

Crops (kept locally; frames of commercial films): FSRCNNX and ArtCNN draw slightly crisper
edges than Lanczos without repainting anything; the difference is visible at close range,
not at a glance.

## Findings

1. **Shader upscalers beat every ML model of M0(n)** on fidelity (+0.4 to +1.75 dB over
   Lanczos, where the best ML models were at best equal) at a fraction of the GPU time:
   FSRCNNX 8 costs ~1.7 ms per 4K frame, about 10 % of a 4090 at 60 fps output, against
   16–30 ms per frame for the ML models.
2. **They are all shippable:** FSRCNNX is GPL-3.0 (combinable with AGPL-3.0), RAVU
   LGPL-3.0, ArtCNN and Anime4K MIT.
3. **FSRCNNX x2 8-0-4-1 is the best all-round choice** (anime and live action, cheapest of
   the gainers); ArtCNN C4F16 is best on live action but nearly twice the cost; FSRCNNX 16
   adds little over 8 for twice the time.
4. **mpv's "high-quality" EWA scalers are slightly less faithful than plain Lanczos** (they
   trade fidelity for a sharper look); the difference is small either way.
5. These only matter when the video is smaller than the window (1080p on a 1440p/4K
   display); they run once per displayed frame, after interpolation.

## Decision (proposed; owner to confirm)

- The filter chain's upscaling stage (SCOPE §15.2, slot 2) uses **mpv shaders, not ML**.
- Ship **FSRCNNX x2 8-0-4-1** (GPL-3.0, with its licence text in the COPR) as the "sharper
  upscaling" option, off by default; keep mpv's own scaler as the default.
- ArtCNN C4F16 (MIT) as a second option once the chain exists; ML upscaling stays
  "bring your own model" (M0(n)).
