<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(n): ML video upscaling on the TensorRT path (SCOPE §15.2)

Date: 2026-10-10. Dev box: RTX 4090, TensorRT 11.3 through vs-mlrt's `vstrt`
(v16.3.test1), fp16 engines with half-precision frames, 2 streams.
Reproducer: `tools/perf/upscale_spike.py` + `upscale_stage.vpy` (shared setup:
`trt_common.py`). Raw numbers: `m0n-results.json`; table: `m0n-table.md`.

## Models and licences

vs-mlrt's `RealESRGAN()` wrapper, models from its v16.3.test1 `models` and
`contrib-models` archives (kept outside the repository).

| Model | Scale | Licence | Shippable in the COPR |
|---|---|---|---|
| Real-ESRGAN animevideo xsx2 / xsx4, animevideov3 | 2× / 4× / 4× | BSD-3-Clause | yes |
| AnimeJaNai V3 HD L1 / L2 / L3 | 2× | CC BY-NC-SA 4.0 | **no** (non-commercial) |
| Ani4K v2 UltraCompact / Compact | 2× | CC BY-NC 4.0 | **no** (non-commercial) |

Non-commercial models may still be used by a user who installs them for personal use;
ButterEye would only load them, never convey them (§8.1).

## Speed (output fps, vspipe, blank clip)

Live 2× needs 48 output fps. "Chain" = RIFE v4.26 (TensorRT) at the source size, then the
upscaler on every output frame, the order a live chain would use.

| Model | 720p → 1440p | + RIFE 2× | 1080p → 2160p | + RIFE 2× |
|---|---|---|---|---|
| Real-ESRGAN xsx2 (BSD) | 80.0 | 75.7 | 33.3 | 33.1 |
| AnimeJaNai V3 HD L1 | 135.7 | 172.3 | 57.3 | 60.1 |
| AnimeJaNai V3 HD L2 | 103.8 | 114.8 | 43.4 | 46.6 |
| AnimeJaNai V3 HD L3 | 84.1 | 76.3 | 31.1 | 32.9 |
| Ani4K v2 UltraCompact | 106.5 | 117.1 | 46.1 | 47.7 |
| Ani4K v2 Compact | 80.9 | 76.8 | 33.9 | 32.6 |

4× models, 540p → 2160p: Real-ESRGAN xsx4 61.1 (chain 71.5), animevideov3 58.0 (68.6).
(The chain is sometimes faster than the upscaler alone: its first frame of each pair is a
source frame and the pipeline overlaps better; both are within run-to-run spread.)
No Xid faults during the run.

## Quality

Real frames shrunk 2× (4× for the 4× models) with Bicubic and brought back, scored against
the original. PSNR/SSIM measure **fidelity**; these models repaint rather than restore, so a
crisper-looking result can score lower. Crops were checked by eye (kept locally, not in
the repository: they are frames of commercial films).

| Clip (frames) | Lanczos | Real-ESRGAN xsx2 | JaNai L1 | JaNai L2 | JaNai L3 | Ani4K UC | Ani4K C |
|---|---|---|---|---|---|---|---|
| anime film A, 2× (3) | 46.3 dB | 32.0 | 43.3 | 45.0 | 45.7 | 43.8 | 44.0 |
| anime film B, 2× (2) | 41.6 | 30.1 | 39.3 | 40.5 | 41.4 | 39.3 | 39.1 |
| live action 4K, 2× (2) | 45.1 | 34.1 | 43.9 | 45.2 | 45.4 | 45.2 | 45.4 |

4× (anime film A): Lanczos 37.6 dB, animevideov3 35.0, xsx4 30.0. Spline36 tracks Lanczos
within 0.25 dB everywhere.

What the crops show: **Real-ESRGAN's anime-video models repaint the image** (posterised
shading, oversaturated highlights, lost background softness): they don't look like the film
any more. **AnimeJaNai L2/L3 and Ani4K stay faithful** with slightly crisper line art than
Lanczos; on live action, no model shows a visible gain (they are trained on anime).

## Findings

1. **The shippable (BSD) models are not usable for ButterEye:** Real-ESRGAN's anime-video
   models change the picture's look and lose 9–15 dB, and the 2× one manages only 33 fps at
   1080p → 4K.
2. **The good models are non-commercial.** AnimeJaNai V3 HD and Ani4K v2 are faithful,
   slightly sharper on anime, and fast: every one is real time at 720p → 1440p with RIFE;
   at 1080p → 4K only JaNai L1 (60 fps with RIFE, exactly the 1.25× live headroom) and
   Ani4K UltraCompact / JaNai L2 (46–48, at or below 48) come near, before mpv's own cost.
3. **The gain over a good classic scaler is small:** fidelity is at best equal to Lanczos;
   the visible benefit is crisper anime line art. Live action gains nothing from these
   models.
4. On a 4090, live ML upscaling is practical **up to 1440p output**; 4K output is offline
   (renders) or marginal with the lightest non-commercial model.

## Decision (proposed; owner to confirm)

- **No ML upscaler ships with ButterEye** (none is both free to ship and good).
- Post-v1, optional **"bring your own model"**: a user-installed vs-mlrt
  Real-ESRGAN-family model (e.g. AnimeJaNai) offered for anime, live up to 1440p output and
  in renders at any size, behind the TensorRT opt-in; ButterEye never downloads or ships it.
- The default upscaling stays **mpv's own scaler** (free, no GPU budget taken from RIFE).
  Next: compare mpv's built-in high-quality scalers (and, licences permitting, shader
  upscalers such as FSRCNNX/RAVU) with these numbers before building the filter chain.
