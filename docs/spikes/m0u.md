<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(u): a cascade for high multipliers (2026-11-13)

**Question.** A 24 fps video on a 144 Hz screen needs five new frames for every
source frame. RIFE makes them well but not fast enough; the GPU motion-vector engine
(mvgpu) is fast enough but tears fast motion apart, because at 24 fps the motion
between two frames is large. Can RIFE make some of the frames and mvgpu the rest?

**Answer.** Yes. RIFE to twice the source rate, then mvgpu from there to the target,
scores like RIFE making every frame, at a fraction of the cost. Adopted for "Match
your display" (SCOPE §5.6).

## Picture

99 real moments of seven consecutive frames from native 50/60 fps footage at 1080p
(`truth.py --around`). The source frames are taken 4 or 6 apart and the frames between
are the truth, so the motion is that of 15 and 10 fps footage: harder than film.
RIFE v4.26 (TensorRT), mvgpu as shipped.

| 6 apart (five frames to make) | worst areas | whole frame | squared error |
|---|---|---|---|
| mvgpu ×6 | −0.139 | 0.907 | (reference) |
| RIFE ×6 | −0.059 | 0.923 | −19 % |
| RIFE ×2, mvgpu ×3 | −0.058 | 0.921 | −16 % |
| RIFE ×3, mvgpu ×2 | −0.052 | 0.923 | −19 % |

| 4 apart (three frames to make) | worst areas | whole frame | squared error |
|---|---|---|---|
| mvgpu ×4 | 0.002 | 0.938 | (reference) |
| RIFE ×4 | 0.090 | 0.949 | −21 % |
| RIFE ×2, mvgpu ×2 | 0.085 | 0.949 | −19 % |

Between two frames RIFE has already brought close together, the block search has
half the motion or less to find, which is where it works. Lower failure thresholds
for the mvgpu stage (`MVGPU_FAIL`) did not help (worst areas −0.058 → −0.064 at
`0.02,0.06`), so it runs as shipped.

What is left: where two source frames do not match at all (a whip between two poses,
a flash), RIFE draws a soft smear and mvgpu, between a sharp frame and the smear,
still draws some hard-edged flecks. Far fewer than mvgpu alone, which tears the whole
figure apart there.

Scene cuts: the RIFE stage repeats the frame before a cut, and the copy carries
`_SceneChangeNext`, so mvgpu repeats it too (five identical frames, then the cut;
checked on a music video with a cut every few seconds).

## Speed

A 4K 24 fps AV1 music video to 144 fps, inside mpv, untimed, RTX 4090 (a browser was
using 20 to 30 % of the GPU during these runs):

| | × real time |
|---|---|
| mvgpu at 4K → 48 (what Automatic chose before) | 2.33 |
| RIFE at 1080p → 144 | 0.73–0.92 |
| RIFE at 1080p → 72, mvgpu → 144 | 0.99 |
| **RIFE at 1080p → 48, mvgpu → 144** | **1.30** |
| RIFE at 1600×900 → 48, mvgpu → 144 | 1.62 |
| RIFE at 1440p → 48, mvgpu → 144 | 0.90 |
| mvgpu at 1440p → 144 | 1.98 |

The speed test's 2× numbers, added up for the two stages plus decoding and shrinking
the 4K frame, say 1.13× for the 1080p cascade; mpv runs 1.30×. The two stages share
the GPU for less than they cost apart (`decide.CASCADE_GAIN` 0.87).

In a real window on a 144 Hz screen (gpu-next, `profile=high-quality`, 2265 × 1274):
audio and video stay within 0.02 s, a seek recovers at once, and 0.2 to 1 % of the
frames are dropped at the output, which mpv attributes to the display side (the
scaler shares the GPU).

## The frame queue: measured, left as it is

mpv asks VapourSynth for up to `concurrent-frames` frames at once (8 for RIFE). Untimed,
a deeper queue is faster: the 1080p cascade runs 1.25–1.33× real time at 8, 1.54× at 16,
1.71× at 24 and 1.63–1.95× at 32 (the 1440p one 0.79× at 8 and 1.03–1.06× at 24 to 32,
still short of real time with headroom). Timed, in a real window, it is not better:

| queue | dropped in 45 s of steady play, each run | a seek plays again after | player memory |
|---|---|---|---|
| 8 | 78, 53, 24, 27 | 0.7 s forward, 1.3 s back | 2.9 GB |
| 24 | 36, 35, 60 | 1.1–1.6 s forward, 3.6–3.8 s back | 3.8 GB |

45 s are 6480 frames; the 4K video above on a 3440 × 1440, 144 Hz screen. The spread
from run to run is larger than the difference between the two, a seek takes longer and
the player holds 0.9 GB more, so the queue stays at 8. What is dropped is late at the
output, not in the filter: the whole GPU is at 50 to 55 % and the CPU at 14 %, and the
count follows the run, not the queue and not the place in the video. (The seek and
memory figures are from runs with mpv's own frame blending forced on by the test
harness, which ButterEye's profile turns off; the dropped-frame counts are from runs
with ButterEye's own settings.)

## What ButterEye does with it

- "Match your display", where the display rate is a whole multiple (2 or more) of
  twice the source rate: 24 → 48 → 144, 23.976 → 47.952 → 143.856, 30 → 60 → 120.
- At the size smoothing starts from (the video's own, or its screen's when the video
  is larger: SCOPE §4.11) when the two stages fit there, else at 1440 or 1080 lines,
  never lower (`CASCADE_MIN_HEIGHT`).
- TensorRT RIFE only, with mvgpu installed. Not for a 2D animation profile, a DRBA
  model, "full size", or another engine the user pinned.
- A RIFE that makes the display rate alone at the video's own size is kept.

## Also found

mpv reports a display rate before its window is on a screen. On a desktop with 144,
144 and 75 Hz screens it said 75 Hz for the first second (`display-names` empty), then
144. ButterEye decided on the first answer and again two seconds later. It now waits
for `display-names` (at most 3 s).
