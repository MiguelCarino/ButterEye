<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(y): letting the NVIDIA driver double the frame rate (2026-11-14)

**Question.** Can ButterEye smooth to half the screen's rate and let NVIDIA Smooth Motion
(the driver's Vulkan layer `VK_LAYER_NV_present`) double it, as an option next to "Match
your display"?

**Answer.** It works, but it is not worth shipping now.

- The layer engages under mpv and makes one frame half-way between each pair of frames
  mpv presents (interpolation: the made frame is shown before the later real one). It
  needs `video-sync=audio` with `display-fps-override` at half the refresh. With any
  display-sync mode it breaks: mpv presents hundreds of times a second.
- **Pacing.** Only mvgpu ×3 + the driver (arm E) paces well. On screen (present timing,
  first pixel out) it shows a new picture on every refresh except one every 7 s, the
  drift of 143.856 fps on a 144 Hz screen. RIFE ×3 + the driver (arm B) does not pace:
  about 1 in 8 of the driver's frames goes out late (phase 1, at the present calls).
- **Picture.** E's picture is clearly worse than the shipped cascade (RIFE ×2, mvgpu ×3):
  worst areas −0.066 ± 0.010 on screen over 97 real moments, better than the shipped
  cascade in only 24 of them. It is about level with mvgpu ×6 (+0.013 ± 0.004). B's picture is
  level with the cascade (−0.006 ± 0.009), but B fails pacing and draws more power than
  today's output.
- **A/V, hangs, variants.** E passes. The layer adds 8–11 ms of video delay over the
  same pipeline without it (11–14 ms from mpv's present to first pixel, against 19–20 ms
  for ButterEye's own 144 fps output). Seek, pause, resize, the fullscreen toggle and
  moves to the 60 and 75 Hz screens never hung. The owner's `mpv.conf`, the SDR
  swapchain and a real 23.976 fps film change nothing.
- **Recommendation: do not ship an experimental option.** The one arm that paces (E) gives
  mvgpu-class pictures for less power. ButterEye's own output already beats it on
  picture, and the arm with the good picture (B) does not pace. Revisit if a later driver
  paces the layer's frames behind heavy CUDA/TensorRT work (B), or if a "low power"
  mode is wanted, where E is the candidate (106 W against 182–194 W here).

RTX 4090, driver 620.70.06, mpv 0.41.0 (`gpu-next`, Vulkan, Wayland), Hyprland. The main
screen (DP-1) is 3440 × 1440 at 144 Hz in HDR mode. The other screens are 1920 × 1080
at 60 Hz and 1600 × 900 at 75 Hz. Variable refresh was off on every screen in every run
(read from the compositor at the start and end of each run, never changed). Unless
stated: fullscreen, `--video-unscaled=yes`, `--no-config`, software decoding, and the
synthetic clip at 23.976 fps described below.

## Arms
| arm | engine | sync | driver's frames |
|---|---|---|---|
| A (today) | ButterEye "Match your display": RIFE ×6 → 143.856 on the bar clip | display-resample | off |
| B | RIFE ×3 → 71.928 | audio, override 72 | on |
| C | RIFE ×3 at 720 lines → 71.928 | audio, override 72 | on |
| E | mvgpu ×3 → 71.928 | audio, override 72 | on |
| Eoff | as E | audio, override 72 | off |
| B2, Z | as B; today's settings | display-resample (with/without override) | on |

"Off" is `VK_LOADER_LAYERS_DISABLE=VK_LAYER_NV_present`; "on" is
`NVPRESENT_ENABLE_SMOOTH_MOTION=1` (see "Switching the layer on and off").

## Tools (`tools/perf/`)
- `nvtrial_clip.py`: the test clip. FFV1 with a quiet audio track, so audio sync has a
  real clock. A striped bar moves 16 px per frame, and the frame number sits in a
  12-bit barcode next to a black/white flicker patch.
- `nvtrial_probe.py`: one plain-mpv run with the layer's log, the loader's layer log,
  mpv's counters over IPC, `nvidia-smi` and optional `grim` captures.
- `nvtrial_run.py`: the same through ButterEye's core, in a throw-away profile, with the
  arm's environment and mpv options passed through `LiveOptions`. It adds:
  - `nvidia-smi pmon`, pressure-stall totals and mpv's CPU time;
  - MangoHud (`--mango`);
  - the timing layer (`--probe`). This is the pass-through layer of `mvgpu/layer/`
    (`VK_LAYER_BUTTEREYE_frames`), placed below the driver's layer, so it sees every
    present that reaches the driver, the made ones included.
  - `--timing`: the layer also asks for present timing (`VK_EXT_present_timing`, stage
    first-pixel-out) on that swapchain. It runs as k = 1 with no images added, so every
    present passes through unchanged apart from a present id and the timing query.
  - `--probe-top`: a second copy of the layer, renamed so that it reads its own
    variables, placed above the driver's layer. It logs the times of mpv's own presents.
  - `--owner-config`: mpv reads the user's `mpv.conf` (read only) instead of `--no-config`.
- `nvtrial_screen.py`: on-screen pacing and video delay from those two logs.
- `nvtrial_gate5.py`: the seek, pause, resize and screen-move script (gate 5), with a
  per-step report.
- `nvtrial_grim.py`: reads `grim` captures (bar position, made or decoded).
- `nvtrial_t3.py`: per-run and per-arm summary of mpv's counters, MangoHud, power, GPU
  share and pressure stalls.
- `nvtrial_q2.py`: the picture-quality pass. Steps: offline frames, playback clip,
  playback with screen captures, scoring, pooling.

## Switching the layer on and off
The desktop session sets `VK_INSTANCE_LAYERS=VK_LAYER_NV_dlssnr:VK_LAYER_NV_present`
for every program (an `environment.d` file of another package). So every Vulkan mpv on
this machine, ButterEye's included, already loads the layer and with it `libcuda`. It
stays inert ("Smooth Motion enabled [0]") until `NVPRESENT_ENABLE_SMOOTH_MOTION=1` is
set, which the layer reads itself. Loaded but inert, it cost nothing measurable (60.9 W
against 59.8 W, plain playback).

| environment | layer mapped | Smooth Motion |
|---|---|---|
| session default | yes | 0 (inert) |
| + `NVPRESENT_ENABLE_SMOOTH_MOTION=1` | yes | 1 |
| + enable + `DISABLE_LAYER_NV_PRESENT_1=1` | yes | **1** (the disable variable loses to `VK_INSTANCE_LAYERS`) |
| + enable + `VK_LOADER_LAYERS_DISABLE=VK_LAYER_NV_present` | no | – |
| no `VK_INSTANCE_LAYERS`, enable | yes | 1 |
| no `VK_INSTANCE_LAYERS`, enable + disable variable | no | – |
| no `VK_INSTANCE_LAYERS`, `VK_LOADER_LAYERS_ENABLE=VK_LAYER_NV_present` + disable variable | no | – |
| no `VK_INSTANCE_LAYERS`, `VK_LOADER_LAYERS_ENABLE` only | yes | 0 |

- **Off, reliably:** `VK_LOADER_LAYERS_DISABLE=VK_LAYER_NV_present`.
- **On:** `NVPRESENT_ENABLE_SMOOTH_MOTION=1`, whichever way the loader put the layer in.
- **Order (loader log):** application, MangoHud (when on), `MESA_device_select`,
  `NV_dlssnr`, `NV_present`, the timing layer (when on), driver.
- **Log:** `NVPRESENT_LOG_LEVEL=3` is the lowest level that prints "Smooth Motion
  enabled [1]" and each swapchain the layer creates. Levels 1 and 2 print only "Feature
  Smooth Motion requested"; level 0 prints nothing.

What the layer does under mpv:
- It creates its swapchains as `R10G10B10A2_UNORM` with "HW Flip Metering Supported via
  App queue". It engages with mpv's HDR10 swapchain (`A2B10G10R10_UNORM +
  HDR10_ST2084`, mpv's own choice on this screen) and with an SDR one
  (`--target-colorspace-hint=no`). Neither is passed through.
- Seen from below, it creates the real swapchain in FIFO with `TRANSFER_DST` usage only.
  mpv draws into the layer's own images, and the present chain carries
  `VkSetPresentConfigNV`.
- After each mpv present, two presents follow below it, both after mpv's: first the
  made frame, then mpv's own picture. The order was checked against `grim` captures. With
  the capture instant taken 17 ms after the start of each `grim` call, 388 of 390
  captures agree: a made frame (bar half-way) whenever the latest present on screen was
  the first of its pair, and a real frame whenever it was the second. So it
  interpolates: the frame half-way to N is shown before N.

## Sync modes (plain mpv, no smoothing; phase 1)
| clip | sync | layer | s | dropped | late (`vo-delayed`) | mpv's measured refresh |
|---|---|---|---|---|---|---|
| 23.976 | audio | off | 12 | 0 | – | – |
| 23.976 | audio | on | 20 | 0 | – | – |
| 71.928 | audio, override 72 | off | 20 | 0 | – | – |
| 71.928 | audio, override 72 | on | 20 | 0 | – | – |
| 71.928 | display-resample | off | 20 | 0 | 0 | 144.0 |
| 23.976 | display-resample, override 72 | on | 20 | 0 | 1947 | 1167 Hz; A/V drifted to −6.0 s |
| 23.976 | display-resample | off | 20 | 0 | 14 | 143.1 |
| 23.976 | display-resample | on | 20 | 36 | 1996 | 222 Hz |

- **Display sync does not work with the layer.** Presents through it no longer block
  mpv, so mpv presents as fast as it can (200–430 a second through ButterEye, arms B2 and
  Z) and counts nearly every frame late. This is also what happens if a user turns the
  layer on while ButterEye runs as it does today.
- **The override works in audio sync.** `display-fps` reads 72 with
  `--display-fps-override=72`, follows a change over IPC at runtime, and audio playback
  is unchanged (no speed correction, A/V offset under 0.1 ms).

## Gate 1: two presents per frame, made frames at the mid-point
Phase 1: `grim` captures of the video area. The phase is measured between the frames
mpv presents: 0 for a frame mpv presented, 0.5 for one half-way between two.

| run | captures read | made by the driver | their phase (mean ± sd) |
|---|---|---|---|
| 23.976, audio, layer off | 143 | 0 % | – |
| 23.976, audio, layer on | 194 | 55 % | 0.499 ± 0.002 |
| ButterEye RIFE ×3 → 71.928, layer on | 237 | 46 % | 0.509 ± 0.061 |
| ButterEye mvgpu ×3 → 71.928, layer on | 143 | 50 % | 0.496 ± 0.021 |
| ButterEye RIFE ×3 at 720 lines, layer on | 190 | 61 % | 0.496 ± 0.082 |

Below the driver's layer, 143.85–143.86 presents a second arrive for mpv's 71.93 in every
audio-sync run with the layer, including all phase 2 runs (4322–4327 pairs per minute,
with at most 4 groups of one or three). **Pass** for B, C and E.

## Pacing at the present calls (phase 1)
CPU times of the present calls that reach the driver, 60 s per run, synthetic clip.
"Held": a present arrived a refresh late. "Bunched": a present came less than half a
refresh after the one before.

| arm | runs | presents/s | gap p1 / p50 / p99 (ms) | held per minute | bunched per minute |
|---|---|---|---|---|---|
| A (no layer) | 3 | 143.7 | 2.8–4.0 / 6.94 / 10.0–11.0 | 81 [65–107] | 72 [59–133] |
| B | 3 | 143.86 | 0.17–0.18 / 7.04 / 14.3–14.5 | **987 [984–1036]** | **1119 [1074–1142]** |
| C | 3 | 143.86 | 1.4–2.5 / 7.03 / 10.2–11.3 | 125 [78–127] | 207 [140–216] |
| E | 4 | 143.85 | 4.3–4.7 / 7.02 / 8.6–8.9 | **6 [0–8]** | 25 [10–26] |

- In B, about 1 in 8 of the driver's presents comes 0.2 ms after the one before, then
  a two-refresh gap, in every run, with no drop counted by mpv. My reading (unverified):
  the layer's CUDA work waits behind RIFE's back-to-back TensorRT inferences (about
  17 ms per source frame). With mvgpu (about 1 ms) it does not happen.
- RIFE ×3 in audio sync also had intermittent drop bursts (6–15 s of drops, then a step
  down to ×2) with the layer on and off alike. Their cause is still open; engine
  throughput, outside GPU load and memory pressure were ruled out in phase 1.

## Gate 2 on screen: present timing
**Method.** The timing layer below the driver's layer asks for `VK_EXT_present_timing`
(first-pixel-out; the domain is the monotonic clock, calibrated offset 0) on every
present. A screen interval is the gap between the first pixels of two presents with
consecutive ids, in refreshes. A gap of 2 or more is a refresh where the picture
should have moved on and did not ("held"). In E the drift of a 143.856 fps stream on
144 Hz holds one refresh every 6.9 s (8.6 a minute); it is counted apart. A, E and Eoff
ran round-robin, 60 s of steady play each, under the GPU lock. A fourth A run replaced
A1, which ran while an outside TensorRT build used 84 % of the GPU.

| run | presents/s on screen | never shown | screen intervals (refreshes: count) | held per minute | held per minute, drift excluded | drops (mpv) |
|---|---|---|---|---|---|---|
| A1 (outside GPU load) | 141.27 | 0 | 1: 8552, 2: 2, 3: 3, 18: 1, 35: 1, 103: 1 | 159 | 159 | 133 |
| A2 | 143.98 | 0 | 1: 8650, 2: 2 | 2 | 2 | 0 |
| A3 (CPU stalls up to 40 ms/s) | 135.90 | 42 | 1: 7867, 2: 115, 3: 52, 4: 29, … 14: 1 | 523 | 523 | 484 |
| A4 | 143.99 | 0 | 1: 8653 | 0 | 0 | 0 |
| E1 | 143.84 | 0 | 1: 8635, 2: 9 | 9 | 0.4 | 0 |
| E2 | 143.86 | 15 | 1: 8601, 2: 14 | 14 | 5.4 | 0 |
| E3 | 143.85 | 3 | 1: 8648, 2: 7 | 7 | 0 | 0 |
| Eoff1–3 (72 a second, each shown twice) | 71.93 | 0 | 1: 134–170, 2: 3973–4045, 3: 143–179 | – | – | 0 |

- **E on screen:** a new picture on every refresh except the drift's. Screen intervals
  p1–p99 6.1–7.5 ms. The on-screen times sit within 2 ms (sd) of a straight line
  through the content times, which is the drift's sawtooth. 0–15 presents in a minute
  were never shown.
- **A:** perfect in the clean runs (0–2 held a minute), very sensitive to outside load
  (A1, A3). The median of the four runs is 80 a minute, about phase 1's 81.
- **Eoff** shows that mpv alone at 72 on 144 Hz alternates 1-, 2- and 3-refresh holds
  (about 6 % of frames off the 2-refresh rhythm), which the driver's frames remove.
- **Gate 2 (held refreshes, drift excluded, at or below A's median): pass** for E
  (0–5.4 against 80). Against A's clean runs E is level except for the drift.

## Gate 4: video delay and A/V offset
From mpv's present call (the copy of the layer above the driver's layer) to the first
pixel of that picture (present timing below it). With the layer on, mpv's picture is the
second of the two presents that follow its present call. mpv's own A/V offset (`avsync`)
was 0.0–0.01 ms in every audio-sync run.

| arm | runs | delay p50 (ms), per run | p1–p99 (ms) | made frame p50 (ms) |
|---|---|---|---|---|
| A | 4 | 20.4, 20.1, 19.1, 20.4 | 3.7–20.5 | – |
| E | 3 | 10.9, 11.3, 13.7 | 8.5–18.4 | 4.0, 4.4, 6.8 |
| Eoff | 3 | 3.0, 3.3, 2.1 | 0.8–7.1 | – |

- **The layer adds 8–11 ms** of video delay over the same pipeline without it (E against
  Eoff, medians 11.3 against 3.0 ms). That is about one refresh: the frame half-way to N
  must be shown before N.
- **E is steady:** p1–p99 8.5–18.4 ms, no drift over 60 s, and mpv keeps audio in sync
  with its own clock.
- ButterEye's output today (A) reaches the screen 19–20 ms after mpv's present: FIFO
  queues three images at 144 a second. Whether mpv's display-sync timing already allows
  for that queue was not established. If it does not, E's video is 6–9 ms earlier than
  today's.
- **Gate 4 (a steady offset under 45 ms of video lag): pass.** An `audio-delay` of
  about 8 ms would cancel the layer's share.
- These runs used `--ao=null`. A sound device's latency is compensated by mpv in both
  cases and does not change the difference.

## Gate 5: seek, pause, resize, fullscreen, other screens
`nvtrial_gate5.py` plays the bar clip through ButterEye and runs, over mpv's IPC:
- steady play, then seek +30 s and −20 s;
- pause 3 s, then unpause;
- windowed (the compositor tiles it, so a resize), window scale 0.5, then fullscreen;
- a move to the 60 Hz screen, then to the 75 Hz screen, then back to DP-1.

The moves are mpv's own (`fs-screen-name`, fullscreen off and on); no desktop setting
was touched. With the driver's frames (E), the override is set to half the new
screen's refresh after each move, as a product would. Three E runs and two A runs.

| step (E, 3 runs) | IPC answered | playback moved | below the driver's layer |
|---|---|---|---|
| seek ±, pause, unpause | always | as the wall clock | 143.7–144.0/s; first picture 260–430 ms after a seek (A: 600 ms) |
| windowed, scale 0.5, fullscreen | always | yes | 143.7–144.0/s, 0–1 never shown |
| to 60 Hz, override still 72 | always | yes | 143.8/s on a 60 Hz screen: **about 620 of 935 presents never shown** |
| override 30 on 60 Hz | always | yes | ButterEye retargets to 30 (mvgpu ×1.25); 64/s, single gaps of 5–18 refreshes while the filter rebuilds |
| to 75 Hz, override still 30 | always | yes | 60/s on 75 Hz: 113 two-refresh holds in 6.5 s |
| override 37.5 on 75 Hz | always | yes | retarget to 37.5; 68/s, then even |
| back to DP-1, override still 37.5 | always | yes | 75/s on 144 Hz, mostly two-refresh holds |
| override 72 on DP-1 | always | yes | retarget to 71.928; a 22–52 refresh gap while the filter rebuilds |

- **No hang in any run.** Every IPC call was answered, playback moved by the wall clock
  after every step (0.0 s while paused), and every player closed on its own.
- **The override must follow the screen.** Until it does, the driver's frames are made
  for the wrong rate. On the 60 Hz screen more than half of all presents were discarded.
  When it is set, ButterEye retargets on its own (it reads the override as the display
  rate), and play settles within a few seconds.
- Arm A on the same script: no hang either. It retargeted to 60 and 75 itself, and moving
  back to DP-1 cost 213 drops (RIFE ×6 restarting).

## Variants (one run each, 60 s, present timing)
| run | swapchain | ButterEye's engine | held per minute (drift excluded) | delay p50 | power |
|---|---|---|---|---|---|
| A, owner's `mpv.conf` | HDR10 | RIFE ×6 → 143.856 | 0 | 19.9 ms | 184 W |
| E, owner's `mpv.conf` | HDR10 | mvgpu ×3 → 71.928 | 9 (0.4) | 11.7 ms | 104 W |
| A, SDR (`--target-colorspace-hint=no`) | SRGB_NONLINEAR | RIFE ×6 → 143.856 | 3 | 20.4 ms | 178 W |
| E, SDR | SRGB_NONLINEAR | mvgpu ×3 → 71.928 | 8 (0) | 12.4 ms | 102 W |
| A, a real film¹ | HDR10 | RIFE ×3 → 71.928² | 0 | 20.1 ms | 210 W |
| E, a real film¹ | HDR10 | mvgpu ×3 → 71.928 | 9 (0.4) | 12.9 ms | 110 W |

¹ A 1080p 23.976 fps Blu-ray feature (animated, AV1), from 10 minutes in.
² ButterEye chose RIFE ×3 → 71.928 for this film (each frame shown for two refreshes),
not ×6.

- The owner's `mpv.conf` (`profile=high-quality`, `target-colorspace-hint=yes`,
  `interpolation=yes`, user scripts) was read as mpv normally does. The runner's own
  options come after it, so `--interpolation=no`, `--hwdec=no`, no resume and no
  watch-later still applied. It changed neither the swapchain nor the pacing.
- The SDR swapchain paces and delays like HDR10, and the layer engages on it.
- On the real film E paces like on the bar clip.

## Gate 3: picture quality (Q2)
### Why phase 1's scores did not match m0u
Phase 1 scored only frame 3, the middle of the five frames made between frames 0 and 6.
m0u's table (`q6.py`, a scratch script built on `truth.py`'s metrics) averages all five
positions, 1 to 5. The middle frame is the hardest, so its mean is lower. On the first
10 moments, RIFE ×6 scores by position (worst areas) −0.015, −0.177, −0.249, −0.257 and
−0.086: −0.157 over all five, −0.249 at 3. RIFE ×6's frame 3 and RIFE ×2's middle frame
are bit-identical. With all five positions the method reproduces m0u exactly (file
domain, 99 moments): RIFE ×6 −0.059, the cascade −0.058, mvgpu ×6 −0.139, RIFE ×3 +
mvgpu ×2 −0.052. `truth.py --around` itself scores a different set-up (sources two
apart, from frames 0, 2, 4 and 6), so it cannot reproduce the 6-apart table.

### Method (phase 2)
**Set.** The 99 real moments of seven frames m0u used (native 50/60 fps footage, 1080p).
Frames 0 and 6 go in; 1 to 5 are the truth.

**Frames made offline:**
- RIFE v4.26 (TensorRT) ×6, ×3 and ×2;
- mvgpu ×6 and ×3;
- the cascade (RIFE ×2, mvgpu ×3);
- RIFE ×3, mvgpu ×2.

mvgpu is the build of the current source (`5b1ad2e`); the live runs above used the
installed package. mvgpu passes RIFE's frames on unchanged: RIFE ×3 + mvgpu ×2 at 2 and
4 equals RIFE ×3's frames bit for bit.

**Playback.** One clip per batch of 20 moments, x264 lossless at 72 fps, played in mpv
with the driver's frames on (audio sync, override 72). Each moment has:
- 23 still frames, each shown 0.3 s: the truth and every other engine's frames;
- arm E's cycle 0, 2, 4, 6, 4, 2, … with mvgpu ×3's 2 and 4, shown 32 times. The driver
  makes 1, 3 and 5.
- arm B's cycle with RIFE ×3's 2 and 4, shown 32 times.

**Finding the frames.** A strip above the picture carries the moment number, a frame
code and a bar. Each frame's bar sits at its own step: arm E's cycle at 0–3, arm B's at
6–9, the stills from 12 up. A frame the driver makes between two frames shows the bar
half-way between their steps. At 0.5, 1.5 and 2.5 it is arm E's 1, 3 and 5; at 6.5,
7.5 and 8.5 it is arm B's. Frames made across the joins between stills and cycles land
elsewhere and are skipped.

**Capture and scoring.**
- `grim` captures the screen, one capture every 36–37 ms.
- A still or cycle frame is used only when two captures of it agree: the largest
  difference was 1 level.
- Up to 8 driver frames are kept per moment and position (median 8).
- The luma of each capture is scored against the captured truth with `truth.py`'s
  metrics.

**Swapchains.** Every batch played twice: SDR (`--target-colorspace-hint=no`) and mpv's
default HDR10.
- In SDR the capture is close to the file: luma × 1.13, no clipping.
- In HDR10 the capture clips highlights above about 200 of 235 and stretches the rest by
  about 1.4–1.5, which lowers every score.
- Both are reported; the comparisons inside each are what count.

**Completeness.** 97 (SDR) and 98 (HDR10) of the 99 moments were complete on screen.
In the others, the first still at the start of a batch was missed.

### Results
Worst areas (mean SSIM of the worst 0.25 % of 16 × 16 blocks), mean over moments of the
mean over positions 1–5, and by position.

**File domain** (frames from the file, m0u's method):

| frame from | positions 1–5 | 1 | 2 | 3 | 4 | 5 | whole frame |
|---|---|---|---|---|---|---|---|
| RIFE ×6 | −0.059 | 0.021 | −0.113 | −0.133 | −0.114 | 0.044 | 0.923 |
| RIFE ×2, mvgpu ×3 (shipped) | −0.058 | 0.015 | −0.100 | −0.133 | −0.102 | 0.032 | 0.921 |
| RIFE ×3, mvgpu ×2 | −0.052 | 0.038 | −0.113 | −0.126 | −0.114 | 0.052 | 0.923 |
| mvgpu ×6 | −0.139 | −0.065 | −0.184 | −0.201 | −0.176 | −0.073 | 0.907 |

**Screen, SDR swapchain** (97 moments):

| frame from | positions 1–5 | 1 | 2 | 3 | 4 | 5 | whole frame | PSNR |
|---|---|---|---|---|---|---|---|---|
| RIFE ×6 | −0.096 | −0.018 | −0.148 | −0.166 | −0.150 | 0.002 | 0.909 | 29.24 |
| RIFE ×2, mvgpu ×3 (shipped) | −0.095 | −0.025 | −0.133 | −0.166 | −0.139 | −0.010 | 0.907 | 29.53 |
| RIFE ×3, mvgpu ×2 | −0.090 | −0.003 | −0.148 | −0.159 | −0.150 | 0.010 | 0.910 | 29.48 |
| mvgpu ×6 | −0.174 | −0.104 | −0.215 | −0.229 | −0.207 | −0.113 | 0.891 | 28.76 |
| **E: mvgpu ×3, driver ×2** | **−0.161** | −0.080 | −0.215 | −0.211 | −0.207 | −0.092 | 0.894 | 28.75 |
| **B: RIFE ×3, driver ×2** | **−0.101** | −0.021 | −0.148 | −0.187 | −0.150 | 0.002 | 0.910 | 29.33 |

**Screen, HDR10 swapchain** (98 moments):

| frame from | positions 1–5 | 1 | 2 | 3 | 4 | 5 | whole frame | PSNR |
|---|---|---|---|---|---|---|---|---|
| RIFE ×6 | −0.148 | −0.072 | −0.201 | −0.216 | −0.200 | −0.049 | 0.892 | 27.22 |
| RIFE ×2, mvgpu ×3 (shipped) | −0.144 | −0.076 | −0.186 | −0.216 | −0.183 | −0.056 | 0.890 | 27.48 |
| RIFE ×3, mvgpu ×2 | −0.142 | −0.059 | −0.201 | −0.209 | −0.201 | −0.039 | 0.892 | 27.46 |
| mvgpu ×6 | −0.224 | −0.155 | −0.266 | −0.281 | −0.256 | −0.163 | 0.870 | 26.67 |
| **E: mvgpu ×3, driver ×2** | **−0.212** | −0.133 | −0.266 | −0.266 | −0.256 | −0.141 | 0.874 | 26.68 |
| **B: RIFE ×3, driver ×2** | **−0.151** | −0.070 | −0.201 | −0.238 | −0.201 | −0.046 | 0.893 | 27.33 |

**Paired on screen** (same moments; driver arm minus the other, mean ± standard error;
"better in": moments where the driver arm scores higher):

| arm | against | worst areas, 1–5 | worst areas, 3 only | whole frame, 1–5 | better in (worst, 1–5) |
|---|---|---|---|---|---|
| E | shipped cascade, SDR | **−0.066 ± 0.010** | −0.045 ± 0.013 | −0.013 ± 0.002 | 24 of 97 |
| E | shipped cascade, HDR10 | **−0.069 ± 0.010** | −0.050 ± 0.013 | −0.015 ± 0.003 | 23 of 98 |
| E | RIFE ×6, SDR | −0.065 ± 0.012 | −0.045 ± 0.013 | −0.015 ± 0.003 | 30 of 97 |
| E | RIFE ×6, HDR10 | −0.065 ± 0.011 | −0.050 ± 0.013 | −0.018 ± 0.003 | 29 of 98 |
| E | mvgpu ×6, SDR | +0.013 ± 0.004 | +0.018 ± 0.007 | +0.003 ± 0.001 | 68 of 97 |
| E | mvgpu ×6, HDR10 | +0.012 ± 0.004 | +0.015 ± 0.007 | +0.004 ± 0.001 | 72 of 98 |
| B | shipped cascade, SDR | −0.006 ± 0.009 | −0.021 ± 0.010 | +0.003 ± 0.001 | 53 of 97 |
| B | shipped cascade, HDR10 | −0.007 ± 0.010 | −0.022 ± 0.012 | +0.004 ± 0.001 | 48 of 98 |
| B | RIFE ×6, SDR | −0.005 ± 0.004 | −0.021 ± 0.010 | +0.0008 ± 0.0004 | 57 of 97 |
| B | RIFE ×3, mvgpu ×2, SDR | −0.011 ± 0.005 | −0.028 ± 0.011 | +0.001 ± 0.001 | 31 of 97 |

- **E fails gate 3 clearly.** Its frames 2 and 4 are mvgpu ×3's, which at 24 fps is the
  weak engine (m0u). The driver's frames between them are slightly better than mvgpu's
  own (E against mvgpu ×6: +0.021 ± 0.007 at positions 1, 3 and 5, SDR), but the whole
  stays near mvgpu ×6, 0.066 below the cascade.
- **B's picture is level with the shipped cascade** over all five positions (−0.006 ±
  0.009, better in 53 of 97 moments). At position 3 alone the driver's frame is slightly
  worse (−0.021 ± 0.010), which is phase 1's result: phase 1 was right about frame 3 and
  only its absolute level differed from m0u. At positions 1 and 5 the driver's frame
  (between a real frame and RIFE's) is level with RIFE's own. B would pass gate 3; it
  fails gate 2.
- The two swapchains agree on every comparison.

## What it costs
`nvidia-smi` power draw, mean over 60 s of steady play:

| arm | runs | power |
|---|---|---|
| A, today (RIFE ×6 → 143.856) | 4 + 2 variants | 182–194 W (178–184 W variants) |
| E (mvgpu ×3 + driver) | 3 + 3 variants | 105–108 W (102–110 W) |
| Eoff (mvgpu ×3 alone, 72 a second) | 3 | 69–73 W |
| B (RIFE ×3 + driver, phase 1) | 4 | 221 W [179–227] |
| C (RIFE ×3 at 720 lines + driver, phase 1) | 4 | 143 W |

The driver's frames cost about 35 W at this swapchain size (3440 × 1440), whatever the
engine below them.

## Gate verdicts
| gate | B: RIFE ×3 | C: RIFE ×3, 720 lines | E: mvgpu ×3 |
|---|---|---|---|
| 1. two presents per mpv frame, made frames at 0.5 ± 0.1 | pass | pass | pass |
| 2. held refreshes, drift excluded, ≤ A's median | fail (present calls) | fail, narrowly (present calls) | **pass on screen** (0–5.4 a minute against 80) |
| 3. Q2 worst areas ≥ the shipped cascade | level (−0.006 ± 0.009) | not scored | **fail** (−0.066 ± 0.010) |
| 4. steady A/V offset under 45 ms of video lag | not measured | not measured | pass (+8–11 ms over Eoff) |
| 5. no hangs on seek, pause, resize, fullscreen, screen moves | not tested | not tested | pass (3 runs) |

No arm passes every gate.

## Recommendation
**Do not ship an experimental "let the driver double" option now.**
- E is the only arm that paces, and its picture is mvgpu-class: 0.066 below the shipped
  cascade in the worst areas, level with ButterEye's own mvgpu ×6. What it offers is
  power: about 106 W against 182–194 W.
- B has the right picture, level with the cascade, but the layer's frames go out
  unevenly behind RIFE's TensorRT work. It also draws more power than today's output.

If it is taken up later:
- **For a "low power" mode, E.** Use mvgpu ×3 + driver, with audio sync, the override at
  half the refresh, and the override reset on every screen change.
  `VK_LOADER_LAYERS_DISABLE=VK_LAYER_NV_present` must be set whenever the option is off,
  so display sync never meets the layer.
- **For best picture, B,** but only after the pacing problem is solved. Candidates: a
  newer driver, `NVPRESENT_QUEUE_FAMILY=1`, or RIFE's work split so the layer's work is
  not queued behind it.

## Problems and limits
- **Shared machine.**
  - Runs waited up to 10 minutes for the GPU lock.
  - Two A runs were disturbed: an outside TensorRT build at 84 % of the GPU, and CPU
    stalls. They are kept in the table and a fourth A run was added.
  - The Q2 captures and the gate runs are timing-insensitive to that, except where noted.
- **Run counts are small:** 3–4 runs per arm for pacing and delay, one per variant.
- **The video delay is from mpv's present call to first pixel.** Audio latency and
  mpv's own allowance for the display queue are not part of it. No physical (camera or
  photodiode) measurement was made.
- **Present timing was read below the driver's layer only.** That is the real
  swapchain; the layer's own swapchain above it has no timing.
- **Q2 limits.**
  - The driver's frames were scored in a back-and-forth cycle (0, 2, 4, 6, 4, 2). In
    real play the driver sees only forward motion.
  - Scene cuts, OSD and subtitles under the driver's frames were not checked.
- **Not tested:** 4K sources, windowed play over long periods, VRR, other GPUs and
  drivers, and `NVPRESENT_QUEUE_FAMILY=1`.
