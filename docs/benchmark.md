# Picture quality on open footage

This page shows how close each interpolation engine gets to frames a camera (or a renderer)
really recorded. The footage is openly licensed, and the tool and its settings are in this
repository, so anyone can run the benchmark again and check the numbers.

Speed is not measured here. Picture quality depends on the engine, the model and the
numeric precision, not on how fast the GPU is.

## How it works

1. **Real in-between frames.** Each piece of footage was shot (or rendered) at a high rate:
   50, 60 or 120 frames per second. The benchmark keeps every s-th frame as a *source*
   frame and hides the frames between them. Each engine rebuilds the hidden frames from the
   sources, and the rebuilt frames are compared with the real ones.
2. **The playback path itself.** Every engine runs through the same VapourSynth template
   that ButterEye uses for playback, with the default profile's settings (scene threshold,
   hold fill, still skip). The exact settings of each row are stored in the result file.
   TensorRT rows use the app's own engines (fp16, half-precision input and output), as
   playback does.
3. **Moments.** A moment is a short window of the footage:
   - **2x tier:** five source frames, re-timed to 24 fps and doubled to 48 fps. The two
     middle in-betweens are scored, so each has a real frame on both sides of its pair.
   - **Display tier:** four source frames, re-timed to 24 fps and multiplied by m (4, 5 or 6)
     to 96, 120 or 144 fps. The m-1 in-betweens of the middle pair are scored. The CC BY
     headline has BBB at x6 and the SVT and El Fuente scenes at x5 and x6 (every s-th frame
     of 50 or 59.94 fps footage: steps of 83 to 120 ms); UVG at x4, x5 and x6 has its own
     table.
   - **Cel scenes:** 2D animation has no open footage drawn at a high rate, so six scenes
     are drawn by the benchmark itself (flat fills, ink outlines, a parallax pan, cut-out
     limbs, a bouncing ball with a turning stripe, speed lines, held text, a camera push),
     once on ones and once on twos (each drawing held for two frames). The truth is the
     continuous motion at the in-between time. They are reported apart and never count in
     the headline.
4. **Moment selection.** Every moment was checked at its own stride before it was pinned:
   - the sources must move: the smallest mean luma step between consecutive sources is at
     least 1.0 (8-bit);
   - no cut: the largest source step is at most 4 x the second largest + 2, and no step
     between native frames is more than 4 x its neighbours + 2;
   - no held frames: no native frame equals the one before it.

   A failing start tried half a second, one second and one and a half seconds either side.
   Moments with no passing start were dropped and are listed in `tools/perf/public_set.json`
   (`selection.dropped`); the threshold was not lowered.

   At most two of BBB's 2x-tier moments may start in its end credits (frame 29580 on,
   `caps` in the manifest): scrolling text over a plain background weighed four of 23
   moments before. The two others were moved into the film, to the middle of the gaps
   between moments nearest one and two thirds of it, and then checked by the same rule
   (they settled at frames 10439 and 19035).

### Scores

- **Worst areas** (higher is better, 1.0 is the real frame): the mean structural
  similarity (SSIM) of the worst 0.25 % of 16x16 luma blocks (at least 4 blocks), taken over
  the moment's worst scored frame. Interpolation errors are local: a broken edge or a
  smeared hand can be a small part of the frame, and a whole-frame average hides it.
- **Flicker** (lower is better, 0 is the real frames): how much the error changes from one
  scored in-between to the next in its worst areas, in 8-bit levels. An error that comes and
  goes between frames is seen as shimmer.
- **Squared error relative to mvgpu** (lower is better): the mean squared luma error, as a
  percentage above or below the mvgpu row. Worst areas alone once ranked engines wrongly, so
  squared error is always reported next to them.

Each figure is a mean over a row's moments. The **headline** is the mean of the group
means, so each kind of footage weighs the same however many moments it has; in the
display tier a group with an x5 and an x6 row counts once (the mean of its two rows). The
tiers are never mixed.

### Configurations

| name | what runs |
|---|---|
| `blend` | a weighted mix of the two frames around each in-between, made by the benchmark: the floor |
| `mvtools` | MVTools motion vectors through the template |
| `mvgpu` | ButterEye's own GPU motion-vector engine |
| `rife-trt-4.26` | RIFE v4.26 on TensorRT |
| `rife-trt-4.22-lite` | RIFE v4.22 lite on TensorRT (2x tier) |
| `rife-ncnn-4.26` | RIFE v4.26 on ncnn/Vulkan, the portable row for AMD and Intel GPUs (2x tier) |
| `cascade-trt` | RIFE v4.26 on TensorRT to 48 fps, then mvgpu to the display rate (display tier) |
| `cascade-ncnn` | the same on ncnn/Vulkan (display tier) |
| `profile-2d` | the app's 2D rules, as for a profile set to 2D animation or a video the content probe finds hand-drawn: the first engine of the app's 2D order that runs on the machine (RIFE on TensorRT, RIFE on ncnn, MVTools, mvgpu), held drawings re-timed, no cascade (cel scenes only; the engine it took is in the result file) |

## Results

Row names are footage groups: **cg** is Big Buck Bunny (rendered at 60 fps), **live50** the
SVT scenes (50 fps), **live60** El Fuente (59.94 fps), **live120** the UVG clips (120 fps)
and **2d** the cel scenes. "x5" and "x6" mean the display tier at five and six times the
source rate.

- **Display tier.** The headline has three groups: BBB at x6, SVT and El Fuente at x5 and
  x6. At steps of 83 to 120 ms the water, foam, leaves and crowds of several SVT and El
  Fuente moments change almost at random from one source to the next: there every engine,
  and the blend, scores below 0 in its worst areas, so those rows separate the engines
  less than BBB does. The scores of each moment are in the result file.
- **TensorRT rows** are as playback runs them on this date: fp16, half-precision input
  and output, and *precise warp*: the positions RIFE's last warp reads from are kept in
  full precision (spike M0(x)). Before it, fp16 placed fine detail slightly off position
  (about 0.05 px on a static scene), and `rife-trt-4.26` had more squared error than
  `rife-ncnn-4.26` on most rows. Against `results-2026-11-14b.json`, which ran the same
  moments without it, every TensorRT row lost squared error: `rife-trt-4.26` 13.8 % in the
  2x tier and 4.1 % in the display tier, `rife-trt-4.22-lite` 12.7 %, `cascade-trt` 2.2 %,
  and 24.6 % on the cel scenes. Worst areas rose by 0.004 to 0.007 on the headlines; single
  rows moved by up to 0.02 either way (`rife-trt-4.26` on live60 in the 2x tier lost
  0.017). The rows that use no TensorRT engine gave the same scores, bit for bit.
- **`profile-2d`** took RIFE v4.26 on TensorRT on this machine, with held drawings
  re-timed and no cascade. In the 2x tier the `rife-trt-4.26` row runs with the same
  settings (the default profile re-times held drawings here too), so the two rows are
  equal. They differ on machines where the 2D order picks another engine.
- **Cel scenes.** The ball, cut-out and dash scenes were slowed (their revision is in the
  result file) because no engine scored above 0 on twos: the ball's bounce now follows a
  gentler curve and its stripe turns 9 px a frame at the rim, the cut-out limbs swing at
  half speed, and the dash's streaks, runner and legs move at 32, 28 and about 23 px a
  frame. They are still hard on twos. Their scores are not comparable with earlier files.
- **Credits.** Taking two of the four scrolling-credit moments out of BBB's 2x tier moved
  the cg row's squared error for `rife-trt-4.26` from +26 % to -19 % against mvgpu
  (`results-2026-11-14b.json`, before precise warp): those two moments had a squared
  error of 90 to 108 on TensorRT against 18 to 28 on mvgpu. Squared error is a mean, and
  a few moments can carry it.
- **No half-flow row.** Earlier files listed `rife-trt-4.26-flow0.5` (RIFE's flow at half
  scale), which never ran: its 1920x1080 engine did not build. The build is fixed, but
  playback no longer has a half-flow step (above 1080 lines a smaller picture scores
  better at less GPU time), so the row is gone rather than run.
- **What ran.** The development checkout at the revision below with its uncommitted
  changes (precise warp, the 2D rules). The result file says so (`buttereye_from`) and
  hashes the app's sources (`app_sha256`); both tiers ran the same sources.

<!-- results:start -->
Measured 2026-11-14, 164 moments (69 in the 2x tier, 95 in the display tier); result file `docs/benchmark/results-2026-11-14c.json`.

### 2x tier (24 to 48 fps), openly licensed footage (CC BY)

worst areas / flicker (1.0 / 0 = the real frame)

| | n | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 |
|---|---|---|---|---|---|---|---|
| cg | 23 | -0.233 / 64.22 | 0.199 / 40.88 | 0.222 / 40.41 | 0.316 / 31.56 | 0.334 / 30.78 | 0.318 / 31.50 |
| live50 | 7 | 0.056 / 52.33 | 0.263 / 39.43 | 0.243 / 38.90 | 0.271 / 39.93 | 0.244 / 44.25 | 0.271 / 39.77 |
| live60 | 6 | -0.208 / 59.69 | 0.466 / 23.69 | 0.391 / 24.96 | 0.513 / 20.79 | 0.516 / 21.60 | 0.520 / 20.78 |
| **headline** | | **-0.128 / 58.75** | **0.309 / 34.67** | **0.286 / 34.76** | **0.367 / 30.76** | **0.364 / 32.21** | **0.370 / 30.68** |

squared error relative to mvgpu, % (lower is better)

| | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 |
|---|---|---|---|---|---|---|
| cg | +417.7 | +2.0 | +0.0 | -32.1 | -27.1 | -30.5 |
| live50 | +126.6 | +2.1 | +0.0 | +6.0 | +22.4 | +5.9 |
| live60 | +603.6 | +22.9 | +0.0 | -15.4 | -8.6 | -14.3 |
| **headline** | +251.7 | +5.7 | +0.0 | -3.2 | +9.8 | -2.9 |

### Display tier (24 to 24 x m fps), openly licensed footage (CC BY)

worst areas / flicker (1.0 / 0 = the real frame)

| | n | blend | mvtools | mvgpu | rife-trt-4.26 | cascade-trt | cascade-ncnn |
|---|---|---|---|---|---|---|---|
| cg x6 | 10 | -0.270 / 50.47 | 0.124 / 40.59 | 0.137 / 40.28 | 0.251 / 35.74 | 0.246 / 35.84 | 0.228 / 36.65 |
| live50 x5 | 7 | -0.191 / 45.70 | -0.044 / 43.27 | -0.073 / 44.75 | -0.014 / 54.27 | -0.012 / 44.82 | -0.015 / 44.67 |
| live50 x6 | 7 | -0.263 / 47.21 | -0.259 / 49.46 | -0.145 / 45.85 | -0.071 / 54.10 | -0.080 / 46.66 | -0.072 / 46.03 |
| live60 x5 | 6 | -0.491 / 55.54 | -0.028 / 49.49 | -0.104 / 49.96 | 0.007 / 45.52 | 0.009 / 45.26 | 0.012 / 45.07 |
| live60 x6 | 6 | -0.531 / 55.65 | -0.115 / 56.09 | -0.199 / 54.72 | -0.063 / 54.92 | -0.066 / 54.35 | -0.064 / 54.47 |
| **headline** | | **-0.336 / 50.84** | **-0.033 / 46.58** | **-0.041 / 45.97** | **0.060 / 46.72** | **0.057 / 43.79** | **0.053 / 43.92** |

squared error relative to mvgpu, % (lower is better)

| | blend | mvtools | mvgpu | rife-trt-4.26 | cascade-trt | cascade-ncnn |
|---|---|---|---|---|---|---|
| cg x6 | +518.3 | +5.5 | +0.0 | -9.9 | -20.1 | -17.4 |
| live50 x5 | +58.4 | +5.2 | +0.0 | +6.1 | -1.1 | -1.9 |
| live50 x6 | +54.0 | +54.6 | +0.0 | +6.1 | +0.1 | -0.8 |
| live60 x5 | +184.7 | +6.6 | +0.0 | -14.6 | -13.5 | -13.3 |
| live60 x6 | +193.6 | +4.1 | +0.0 | -15.6 | -14.5 | -14.2 |
| **headline** | +132.2 | +21.5 | +0.0 | -1.7 | -6.1 | -6.4 |

### 2D cel scenes (drawn by the benchmark; reported apart)

worst areas / flicker (1.0 / 0 = the real frame)

| | n | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 | profile-2d |
|---|---|---|---|---|---|---|---|---|
| 2d | 12 | -0.246 / 87.06 | 0.514 / 27.38 | 0.395 / 35.47 | 0.673 / 16.38 | 0.755 / 14.92 | 0.675 / 16.61 | 0.673 / 16.38 |

squared error relative to mvgpu, % (lower is better)

| | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 | profile-2d |
|---|---|---|---|---|---|---|---|
| 2d | +1030.2 | +7.2 | +0.0 | -56.3 | -62.8 | -52.5 | -56.3 |

### Film step (UVG, CC BY-NC 3.0: numbers only, never in the headline)

worst areas / flicker (1.0 / 0 = the real frame)

| | n | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 | cascade-trt | cascade-ncnn |
|---|---|---|---|---|---|---|---|---|---|
| live120 2x, s=4 | 21 | -0.261 / 72.86 | 0.004 / 54.99 | 0.058 / 56.18 | 0.051 / 48.34 | 0.065 / 47.90 | 0.051 / 48.64 | - | - |
| live120 x4 | 21 | -0.249 / 52.30 | 0.005 / 47.68 | 0.037 / 47.24 | 0.066 / 42.69 | - | - | 0.086 / 44.64 | 0.080 / 45.31 |
| live120 x5 | 18 | -0.439 / 61.20 | -0.203 / 57.68 | -0.195 / 58.32 | -0.143 / 54.28 | - | - | -0.100 / 53.82 | -0.100 / 53.77 |
| live120 x6 | 20 | -0.430 / 57.88 | -0.207 / 55.41 | -0.202 / 56.54 | -0.189 / 56.74 | - | - | -0.152 / 54.38 | -0.152 / 54.25 |

squared error relative to mvgpu, % (lower is better)

| | blend | mvtools | mvgpu | rife-trt-4.26 | rife-trt-4.22-lite | rife-ncnn-4.26 | cascade-trt | cascade-ncnn |
|---|---|---|---|---|---|---|---|---|
| live120 2x, s=4 | +193.2 | +0.0 | +0.0 | -26.9 | -27.8 | -26.3 | - | - |
| live120 x4 | +182.8 | -0.8 | +0.0 | -27.8 | - | - | -29.2 | -28.7 |
| live120 x5 | +141.7 | -5.2 | +0.0 | -33.1 | - | - | -31.3 | -31.1 |
| live120 x6 | +117.0 | -4.1 | +0.0 | -26.5 | - | - | -26.5 | -26.6 |

ButterEye 0.0.7 (488a360b49, modified, app sources 83c2997cbf), VapourSynth R79, TensorRT 11.3.0, vs-mlrt v16.3.test1-0-g44033da; NVIDIA GeForce RTX 4090, driver 620.70.06; Fedora Linux 45 (COSMIC).
<!-- results:end -->

## Footage

<!-- footage:start -->
| set | licence | credit | what was changed | images |
|---|---|---|---|---|
| Big Buck Bunny (60 fps 4K Sunflower edition) | [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) | Big Buck Bunny, (c) 2008 Blender Foundation, www.bigbuckbunny.org; 60 fps 4K Sunflower edition (c) 2013 Janus B. Kristensen. Licensed CC BY 3.0. | frames extracted, scaled to half size by a 2x2 box filter (3840x2160 to 1920x1080) | yes |
| SVT Open Content Video Test Suite 2022 - Natural Complexity | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | SVT Open Content Video Test Suite 2022 - Natural Complexity, (c) Sveriges Television AB (SVT). Licensed CC BY 4.0. | frames extracted from the EXR sequence, ACEScc decoded, scaled to half size by a 2x2 box filter in linear light, exposed so the median luma is 0.18 (lowered by one stop at most to keep highlights), tone-mapped with a fixed curve (not SVT's grade), BT.709 encoded, rounded to 8 bits | yes |
| El Fuente (Netflix Open Content): Boat, FoodMarket | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | El Fuente, (c) Netflix, Inc., Netflix Open Content. Licensed CC BY 4.0. | frames extracted, scaled to half size by a 2x2 box filter (4096x2160 to 2048x1080), centre-cropped to 1920x1080, rounded from 10 to 8 bits | numbers only |
| UVG dataset, 120 fps 4K sequences | [CC BY-NC 3.0](https://creativecommons.org/licenses/by-nc/3.0/) | UVG dataset, Ultra Video Group, Tampere University. A. Mercat, M. Viitanen and J. Vanne, "UVG dataset: 50/120fps 4K sequences for video codec analysis and development", Proc. ACM Multimedia Systems Conf., 2020. Licensed CC BY-NC 3.0. | frames extracted, scaled to half size by a 2x2 box filter (3840x2160 to 1920x1080); numbers only, no images | numbers only |
| Cel scenes drawn by the benchmark | [AGPL-3.0-or-later](https://www.gnu.org/licenses/agpl-3.0.html) | Cel scenes drawn by ButterEye's public_bench.py. | none: drawn at 2x2 supersampling with analytic edge coverage, boxed to 1920x1080, rounded to 8 bits | yes |
<!-- footage:end -->

Images (`run --dump`) are made only of BBB, SVT and the cel scenes, each with its credit and
the changes in a caption. The UVG footage is under a non-commercial licence: it is fetched
only with `--accept-nc`, its numbers are shown in their own table, it never counts in the
headline, and no images of it are made. No footage is stored in this repository.

## Run it yourself

```
tools/perf/public_bench.py fetch --accept-nc     # about 7.4 GB; leave out --accept-nc to skip UVG
tools/perf/public_bench.py prepare --accept-nc   # decodes the moments, about 1.7 GB
tools/perf/public_bench.py run --accept-nc --json mine.json \
    --compare docs/benchmark/results-2026-11-14c.json
```

- `run` scores both tiers (about 8 minutes on an RTX 4090) and, with `--compare`, ends
  with what moved against the published file: per table, row and configuration, the
  change in worst areas and in squared error. `compare OLD.json NEW.json` does the same
  for two files.
- Downloads go to `$XDG_CACHE_HOME/buttereye-bench/` (`BUTTEREYE_BENCH_CACHE` moves it).
  Every download and every decoded moment is checked against the sha256 pinned in
  `tools/perf/public_set.json`; a mismatch is an error.
- `run --tier 2x` and `run --tier display` can be run apart and joined with
  `report A.json B.json --json all.json` (`--compare` works there too).
- `BUTTEREYE_BENCH_APP=DIR` measures the `buttereye` package in `DIR` (a frozen copy)
  instead of the checkout's; the result file says so.

**After a change** to an engine (a TensorRT fix, say), one command re-runs the rows it
touches and shows what moved:

```
tools/perf/public_bench.py run --accept-nc \
    --configs rife-trt-4.26,rife-trt-4.22-lite,cascade-trt,profile-2d \
    --compare docs/benchmark/results-2026-11-14c.json
```

TensorRT engines that the change needs are built first (minutes each) unless `--no-build`.
A run of some configurations only is never published; to publish, run everything and add
`--publish`, which writes `docs/benchmark/results-<date>.json` (`<date>b` and so on when
that day already has one) and never overwrites a published file. `report RESULT.json
--docs docs/benchmark.md` writes the tables into this page.
- `run --dump DIR` writes each configuration's worst frame per group with its error map
  and a caption carrying the credit and the changes (images-allowed footage only).

**Requirements**

- A ButterEye install (its VapourSynth plugins; TensorRT set up in the app for the
  TensorRT rows, which exist on NVIDIA GPUs only) and a checkout of this repository for
  `tools/`.
- Python 3 with numpy, and VapourSynth.
- ffmpeg with the `h264` and `hevc` decoders (stock Fedora `ffmpeg-free` may lack them).
- About 7.4 GB of download and about 9 GB of disk for downloads and prepared moments.

## Known limits

- The figures are not comparable with earlier internal tables, which used two sources a
  moment and no outer frames.
- 120 fps is not 144 Hz: no open footage has film's 41.7 ms step and a x6 multiplier
  together. The UVG x5 rows are the only exact film-step case. The display-tier headline
  steps 2 to 3 times film's motion: BBB x6 100 ms, SVT x5 and x6 100 and 120 ms, El
  Fuente x5 and x6 83 and 100 ms.
- Shutter: every fifth frame of a 120 fps capture looks like 24 fps shot with a very short
  shutter (at most 72 degrees), much sharper than film's usual 180 degrees. SVT's 1/100 s
  shutter halved to 25 fps is 90 degrees. Engines behave differently on input without
  motion blur.
- The BBB and UVG truth frames are compressed (H.264 at about 8 Mbit/s, HEVC at about
  49 Mbit/s); boxing to 1080p averages much of that noise away. The calibration against
  the uncompressed UVG source was not done yet.
- SVT describes its scenes as having no camera motion: only the subject moves (water,
  smoke, fire, leaves). Their colours are a fixed mapping of the camera data (exposure,
  tone curve, BT.709), not SVT's grade, and the camera's colour space is used as is, so
  colours look cooler than the scene.
- Water, foam, smoke and crowds at 83 to 120 ms steps barely follow any motion an engine
  can find: in the display tier several SVT and El Fuente moments score below 0 for every
  engine. They are kept (they passed the selection rule, and real video has them), but
  they narrow the gaps between engines in those rows.
- There is no real hand-drawn 2D footage; the cel scenes stand in for it. On twos their
  truth is the continuous motion, which rewards re-timing held drawings.
- One run per tier on one machine. The scores are deterministic for a fixed set and
  software (a second run reproduced every unchanged score exactly), so no spread is
  given; a different GPU or driver was not tried.
- TensorRT fp16 kernels and ncnn Vulkan may give slightly different numbers on other GPUs
  and drivers (the third decimal); this is not measured. The result file records the GPU,
  driver, versions and engine names.
