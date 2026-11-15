<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(w): telling hand-drawn 2D animation from other video (2026-11-14)

**Question.** A profile set to 2D animation is smoothed with its own rules: RIFE
first and motion vectors last, no cascade, held drawings re-timed (motion vectors
lose large flat shapes and have to find twice the step on drawings held for two
frames). The shipped profile leaves the kind of video to ButterEye, which re-timed
held drawings frame by frame but never chose the engines by it. Can it tell a 2D
title from camera footage and 3D by itself?

**Answer.** Yes, by how often a drawing is held, not by its colours. A probe of
about a second reads four short runs of frames and counts the frame pairs that
repeat a drawing inside a moving passage. An Auto profile now takes the 2D rules
when that share is 12 % or more, and the window has a Video type choice (Auto,
2D animation, 3D or live action) that always wins over the probe.

## Two candidate signs

Twelve videos from one library, three runs of 49 frames each at 320 × 180:

| kind | held pairs, among those where something moves | colours covering 90 % of a frame |
|---|---|---|
| hand-drawn 2D (5) | 25–62 %, one title 2 % | 22–81 |
| live action (4) | 1–6 % | 24–81 |
| 3D (3) | 1–22 % (the 22 % on 18 pairs) | 5–194 |

The colour count (4 bits a channel) does not separate anything: dark or heavily
graded live action needs as few colours as cel shading. Held drawings do, and they
are also the thing the 2D rules exist for.

## The probe as built

`mpvctl/content.py`: four runs at 20, 40, 60 and 80 % of the file, 49 frames each
from the keyframe before, scaled to 320 × 180 and made grey by ffmpeg, which also
turns each frame into its difference from the one before and prints the mean
(`tblend`, `signalstats`). No picture reaches Python and nothing is written. A pair
is held by the script's own rule for a held drawing (nearly no change, or little
change and under a quarter of the change around it), and is judged only where
something moves at it or within two frames of it: a still shot repeats frames in
any kind of video. Under 40 judged pairs there is no answer.

Nineteen videos with this probe, the held share of each:

| kind | held share | answer |
|---|---|---|
| hand-drawn 2D, eight titles | 16, 18, 20, 25, 41, 52, 53, 65 % | 2D animation |
| hand-drawn 2D, a feature film animated on almost every frame | 10 % | not found |
| live action, seven titles | 0, 0, 1, 2, 4, 6, 7 % | not 2D |
| 3D, three titles | 3, 4, 6 % | not 2D |

Eighteen of nineteen are treated as intended, and no live-action or 3D title is
taken for 2D. The limit of 12 % sits between the two groups (7 and 16 %). The probe
took 0.2 to 1.2 s for 1080p files and 1.6 to 3.0 s for 4K ones (software decoding,
RTX 4090 box, the four runs at once).

## What ButterEye does with it

- **Auto profiles only**, for local files at film and television rates (31 fps and
  under), 20 s or longer. The first decision for a file waits up to 2 s for the
  answer; a later answer decides again. The answer is remembered under a key that
  holds no file name (the path, size and date, hashed), so the same file is not read
  twice.
- **2D found:** the profile's content counts as 2D animation for that video. The row
  says "2D animation, detected".
- **Anything else** (not 2D, cannot tell, ffmpeg missing, a file it cannot read):
  the video plays as before.
- **The user's choice wins.** Video type set to 2D animation or to 3D or live action
  is taken as it is and nothing is probed.

## Limits

- Animation drawn on every frame, and long pans, hold nothing: such a title is not
  found (one of nine here). Its held drawings, where it has them, are still re-timed
  frame by frame as before.
- It answers once for a whole file. A film that mixes kinds is judged by its four
  runs.
- Nineteen videos is a small sample from one library.
