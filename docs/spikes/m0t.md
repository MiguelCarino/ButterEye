<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(t): a ground-truth test for in-between frames, and what it found in mvgpu

Date: 2026-11-12. Dev box: RTX 4090, Fedora 45, VapourSynth R79. Tool:
`tools/perf/truth.py`. Raw numbers of the synthetic run: `m0t-results.json`.

## Question

Which artifacts does each engine still draw, and where? PSNR on a few real triples
(M0(q), M0(r)) could not say: a torn edge or a vanished line is a small area that is very
wrong, and the mean barely moves for it.

## The test

Eleven scenes are drawn at twice the source rate from band-limited layers that are moved
by a Fourier shift, so every in-between frame is exact, half pixels included. Each scene
isolates one cause. The engine gets every second frame and rebuilds the others. The score
is **worst areas**: the mean SSIM of the worst 0.25 % of the 16×16 luma blocks (1.0 = the
real frame), beside luma PSNR. Real footage is scored the same way by its own middle
frames (`--clip PATH@t1,t2`), which is only valid for footage that moves on every frame: a
24 fps film inside a 60 fps recording, or drawings held on twos, have no middle frame to
rebuild (`--min-motion` skips those).

`--reference FILE.py` scores any other engine through a three-line adapter kept outside
the repository.

## Results (1280×720, worst areas / luma PSNR)

| scene | blend | mvgpu before | **mvgpu now** | MVTools | RIFE v4.26 | RIFE v4.22-lite |
|---|---|---|---|---|---|---|
| pan | 0.841 / 39.8 | 0.976 / 65.3 | 0.976 / 65.4 | 0.981 / 64.3 | 0.997 / 57.6 | 0.997 / 56.3 |
| pan, half-pixel | 0.767 / 38.4 | 0.972 / 55.5 | 0.969 / 55.5 | 0.976 / 52.7 | 0.988 / 50.8 | 0.989 / 51.1 |
| object over a still background | −0.125 / 37.3 | 0.779 / 54.5 | 0.781 / 54.4 | 0.658 / 51.2 | 0.951 / 57.5 | 0.811 / 54.4 |
| object and background both moving | −0.092 / 35.3 | 0.748 / 52.7 | 0.748 / 53.5 | 0.647 / 51.8 | 0.946 / 53.7 | 0.936 / 50.9 |
| thin bars | −0.276 / 28.8 | 0.241 / 42.9 | **0.939 / 59.9** | 0.935 / 53.9 | 0.965 / 53.0 | 0.980 / 58.2 |
| fence over a moving background | 0.352 / 28.4 | −0.791 / 23.1 | −0.785 / 24.0 | −0.797 / 32.8 | −0.789 / 24.1 | −0.777 / 36.8 |
| 96 px a frame | 0.319 / 37.4 | 0.382 / 45.3 | 0.330 / 45.3 | 0.100 / 37.0 | 0.045 / 43.4 | 0.415 / 47.2 |
| small fast object | −0.093 / 40.1 | −0.093 / 40.1 | −0.093 / 40.1 | 0.168 / 41.1 | 0.865 / 53.9 | 0.546 / 45.1 |
| pan under a fade | 0.843 / 40.7 | 0.895 / 51.6 | 0.909 / 51.2 | 0.659 / 40.9 | 0.981 / 46.9 | 0.986 / 51.0 |
| pan under grain | 0.556 / 32.4 | 0.687 / 33.9 | 0.679 / 33.9 | 0.673 / 33.6 | 0.634 / 33.0 | 0.658 / 33.5 |
| rolling credits | 0.289 / 22.2 | −0.298 / 21.8 | **0.998 / 57.0** | 0.962 / 51.7 | 0.986 / 45.1 | 0.991 / 45.7 |
| **mean of worst** | 0.307 | 0.409 | **0.586** | 0.542 | 0.688 | 0.685 |

Real footage, 26 triples of a 3440×1440 60 fps screen recording (mean of worst areas):
blend 0.009, mvgpu before 0.139, **mvgpu now 0.165**, MVTools 0.132, RIFE v4.26 0.167.

## What was wrong in mvgpu, and the two changes

1. **Rolling credits were erased in patches (−0.30, worse than a blend).** At 1/8 size
   the rows of text are gone or aliased, the coarsest level settles on some vector, and the
   ±1 steps of the three finer levels can bring a vector back by about 8 px at most. Every
   finer level now also refines around *no motion*, as a sixth candidate beside the parent
   block's vector and its four neighbours'.
2. **Thin bars vanished (0.24).** The search is symmetric around the in-between frame, and
   for a block the bar passes through, "nothing here, plain background in both frames"
   matches exactly as well as "the bar, in both frames". Short vectors won that tie. A
   match is now credited with the detail in what matched (the smaller of the two patches'
   standard deviations, half of it per pixel): two patches with detail that agree are
   evidence, two flat ones are not. `MVGPU_ACT` overrides the share (0 = off); 0.25, 0.5
   and 1.0 gave 0.90, 0.99 and 0.98 on the bars before change 1, and nothing else moved.

Cost on the GPU alone, 24 → 48 of generated noise: 1080p 1128 → 995 fps, 4K 292 → 254.

Tried and not kept: fading to the plain blend where the frames disagree but the vectors
neither converge nor spread. It changed no score on either set.

## Third change: vectors searched from the source frames (2026-11-12, later)

A small object over a detailed background was not drawn at all: at its in-between
position the background matches itself, so the symmetric search never proposes the
object's vector. `uni.comp` asks from the other side. A block of a source frame that the
in-between vectors do not explain (the frames differ there by more than 0.04 a pixel
under them) is searched for in the other frame, ±64 px, every second position and then
±1. `proj.comp` carries what was found to where the content is at the in-between time
(a block of A half-way at +m/2, a block of C at −m/2), and marks the place it left, or
has not reached, so the render takes the frame that shows the background there instead
of averaging a ghost.

A find counts only when the other frame says the same (the block of A found in C at +m
lies where a block of C was found in A at −m). Without that, pieces of letters and edges
were "found" in many places: credits 1.00 → 0.04, thin bars 0.94 → 0.06. With it:

| scene | before | now |
|---|---|---|
| small fast object | −0.093 / 40.1 | 0.245 / 44.0 |
| object over a still background | 0.781 / 54.4 | 0.81 |
| thin bars | 0.939 / 59.9 | 0.97 |
| all scenes, mean of worst | 0.586 | 0.62 |
| real 60 fps footage (26 triples) | 0.165 | 0.168 |

GPU time per in-between frame, 24 → 48: generated noise 1080p 1.19 → 1.57 ms and 4K
4.75 → 6.60 ms (252 → 204 fps); a real 1080p clip 1.47 → 2.2 ms. `MVGPU_UNI=0` turns it
off; `MVGPU_UNI_T=flag,abs,rel` and `MVGPU_UNI_LAND` are its thresholds.

The small object is drawn but not whole (RIFE: 0.87): blocks on its edge are part
object, part background, and are not found. Matching only the pixels the in-between
vector leaves unexplained found them (0.43) but left a one-pixel stray line beside the
thin bars (0.97 → 0.07) whose cause was not found; not kept. A wider landing (6 or 8 px
instead of 4) made the object worse (0.06, 0.03).

## A flicker score, and vectors remembered from the pair before (tried, off)

`truth.py` now also prints **flicker**: how much the error changes from one in-between
frame to the next, in its worst areas (8-bit levels, the truth's own change taken out).
An error that stays is one thing; one that comes and goes is shimmer. A scene of soft,
low-contrast shapes under light grain (`soft`) was added for it. `--pairs 4`:

| | blend | mvgpu | MVTools | RIFE v4.26 |
|---|---|---|---|---|
| flicker, mean of 12 scenes | 30.81 | 16.25 | 21.13 | 16.57 |
| flicker, real 60 fps footage | – | 46.08 | 47.82 | 46.41 |

The search can now try each block's vector of the pair before (a seventh candidate at
every level but the coarsest) and charge for leaving it. Measured (`MVGPU_TEMPORAL`):

| | worst areas | flicker | real worst | real flicker |
|---|---|---|---|---|
| off | 0.625 | 16.25 | 0.168 | 46.08 |
| candidate only (0) | 0.626 | 15.13 | 0.168 | 46.13 |
| cost 0.002 | 0.620 | 15.94 | 0.157 | 46.61 |
| cost 0.005 | 0.592 | 14.33 | – | – |

The whole difference in the synthetic mean is the fence scene, which fails either way
(105 → 92 → 72); every other scene moves by a few hundredths, and the real footage not at
all as a candidate and for the worse with a cost. So it is **off as shipped**: the flicker
these scenes show comes from occlusions and from areas no vector explains, not from
vectors that wander where there is little to match. Open: sequences longer than four
pairs, where a remembered vector could carry a fast motion the coarsest level loses.

## Occlusion bands: two things tried, neither kept

`truth.py --dump` now also writes each engine's error map (the difference from the real
frame, 8×). On the two occlusion scenes mvgpu's whole error is two strips beside the
object, as wide as the two motions differ: the strip the object uncovers and the one it
covers. Their content is in one frame only, no vector makes the frames agree, and each
pixel is left with whatever matched by chance, so the background there comes out mottled.

1. **The background's vector for the whole strip.** Near two motions, take the one nearer
   to how the picture as a whole moves as the layer behind, give the strip that vector,
   and show the frame in which the other sample is the thing in front (found in the other
   frame where the other motion puts it). Four versions: by pixel behind a mismatch
   threshold (object over still background 0.81 → 0.40: wrong vectors in the strip often
   match well enough to pass), by block on an 8×8 window (0.06, then 0.66 once blocks
   either motion explains were left alone: the window is wider than most of the strip),
   and by block on its own 4×4 pixels (0.78; both moving 0.75 → 0.68 with the background
   motion given by hand). None reached what the divergence rule already does.
2. **A median of the two warped samples and the plain blend** where the frames disagree:
   no score moved, on the scenes or on the real footage, with or without the divergence
   rule deciding first.

The limit is the two frames. What a strip shows can be decided only with a third: content
uncovered now is still there in the next frame, and its motion is known from that pair.

## A proper set of real footage, and what it corrected (2026-11-12, last)

Everything above that says "real footage" is one desktop recording. With a library of
native 50 and 60 fps sources to hand (two filmed concerts, game footage, trailers, a
screen recording; nothing held, nothing on twos), `truth.py` gained `--clip PATH@auto40`
(moments spread over a file), `--height`, a check that drops moments across a cut, and
`--save-set` / `--set`, which keep the chosen moments decoded so a run needs no decoding
(169 moments at 1080p, 2.5 GB, under a minute a run). Worst areas / flicker:

| | blend | mvgpu as above | **mvgpu now** | MVTools | RIFE v4.26 | RIFE v4.22-lite |
|---|---|---|---|---|---|---|
| 169 real moments | −0.238 / 69.8 | 0.150 / 48.4 | **0.179 / 47.3** | 0.194 / 44.7 | 0.312 / 40.2 | 0.314 / 40.6 |
| 12 drawn scenes | 0.353 / 30.8 | 0.643 / 14.7 | **0.663 / 13.3** | 0.551 / 21.1 | 0.699 / 16.6 | – |

**Corrected: mvgpu is not level with RIFE on real footage.** RIFE is about twice as good
as either block engine there, and MVTools is ahead of mvgpu on filmed pictures (the two
concerts: 0.11 and 0.28 against 0.04 and 0.20). The desktop recording flattered a block
search: flat shapes, straight motion.

Each part of mvgpu, turned off, on the real set (worst areas; 0.150 with all on):

| off | score | so the part |
|---|---|---|
| credit for detail | 0.139 | helps |
| vectors from the source frames | 0.134 | helps |
| 4×4 refinement | 0.134 | helps |
| checked median | 0.143 | helps |
| candidate from the pair before, *on* | 0.153 | helps a little |
| the fade to a plain blend where vectors fail over an area | **0.168** | **hurts** |
| that, and the one-frame choice at mismatches | **0.180** | **hurts** |

The last two are what an error map of a filmed frame shows: small, hard, fully wrong
patches at fingers and strings, where MVTools and RIFE are softly wrong. Both were tuned
on single clips by eye and by PSNR (0.0.7). Their thresholds are now 2.5 and 3 times
higher (`MVGPU_FAIL` 0.05, 0.15; `MVGPU_OCC` 0.12, 0.36), so they act only where the
frames disagree badly, and the candidate from the pair before is on. On the drawn scenes
the small fast object went 0.24 → 0.46 with it, and nothing fell.

Tried on this set and not kept: a cost for leaving the coarser level's vector (0.001 to
0.012: 0.174 down to 0.133, and the drawn scenes with it); a wider margin for the
per-pixel choice (0.146, 0.136); a stronger or weaker credit for detail (1.0: drawn
scenes 0.566; 0.25: 0.144).

A reference engine's own motion-vector mode scored 0.045 / 60.8 on the real set through
the adapter, with settings chosen here and not tuned: not a verdict on it.

What this says about the product: where RIFE keeps up it should be what plays, and it is
what Automatic picks. mvgpu is for what RIFE cannot do in real time (4K at 60 and 120),
and there it is now close to MVTools at many times the speed, not a replacement for RIFE.

## What the test says is still wrong

- **A small fast object** (20 px, 40 px a frame) is drawn in part only (0.24; RIFE 0.87):
  its edge blocks are not found (above).
- **A fence over a moving background** fails in every engine tested: two motions
  interleaved below the block size.
- **Occlusion edges**: 0.75–0.78 against RIFE's 0.95.

## Order of work from here

1. The edge blocks of a found object (masked matching, with the stray line explained).
2. Occlusion strips from three frames: the vectors of the pair before and the pair after.
3. Fewer in-between frames when the vectors of a whole frame are poor.

(1 and 2: see "Three frames, and the edge of a found object" below.)

## Three frames, and the edge of a found object (2026-11-12)

Items 2 and 1 of the list above.

**Three frames.** The engine now holds four source frames (the pair, the frame before
and the frame after) and the final vectors of three pairs. `select.comp` uses them in
two ways:

- *Strips.* Where the vectors of the pair before and of the pair after differ at a
  place, two motions meet there. Over a patch of 25 pixels, when neither motion explains
  A and C: content that C and the frame after agree on (under that pair's vector) was
  just uncovered and is taken from C; content that A and the frame before agree on is
  about to be covered and is taken from A. The object's soft edge beside the strip is
  left as it was.
- *A better vector.* Where A and C disagree under the best vector, a vector of a
  neighbouring pair that halves the error is taken.

What it took to get there, since each step looked right and scored worse:

1. Asking only "do C and the frame after agree" picks the right side wherever it is
   sure (2.4 levels of error against 57 for the wrong frame), but a partial weight with
   the changed vector blended two different things. All or nothing.
2. Five pixels are too few on a smooth texture: among nine candidate vectors one
   matches by chance, in either pair. Hence the wide patch, and only the vector of the
   block itself.
3. Comparing two small errors with each other decides at random. Hence the test that
   neither motion explains A and C, which is what a strip is.
4. The last loss was a column one or two pixels wide: an anti-aliased edge is part
   object, part background, and neither frame alone has it. Pixels that the other
   motion explains, or that lie within 3 px of one along the motion, are left alone.

| | off | strips | better vector | both |
|---|---|---|---|---|
| 12 drawn scenes, mean of worst | 0.641 | 0.649 | 0.649 | **0.657** |
| object over a still background | 0.808 / 55.5 | 0.825 / 56.0 | | 0.825 / 56.0 |
| both moving | 0.753 / 54.4 | 0.753 / 54.9 | | 0.753 / 54.9 |
| 96 px a frame | 0.259 | 0.351 | | 0.351 |
| small fast object | 0.457 | | 0.544 | 0.544 |
| 99 real moments, seven frames | 0.253 | 0.259 | 0.256 | **0.261** |

No scene fell. On the occlusion scene the squared error of every in-between frame
fell by 10 to 25 %; the strips are still not clean (RIFE: 0.95), because where the
texture is smooth the patch test does not fire and the pixel keeps its chance match.
Thresholds (`MVGPU_TRI_T`) moved the real set by 0.002 at most in either direction.
GPU time per in-between frame on the real moments, 24 → 48: 1080p 3.69 → 4.15 ms, 4K
14.5 → 16.7 ms. `MVGPU_TRI=0` turns it off.

The real set for this is new: `truth.py --around` keeps seven frames a moment and
scores the middle in-between frame, whose pair has a real frame on both sides, as
nearly every pair has in playback (99 moments; the five-frame set gives every
in-between frame one side only, and the strips need both).

**The edge of a found object: built, off.** `select.comp` can offer the vector of a
block `proj.comp` carried to the pixels up to two blocks around it, taken where it makes
the frames agree and brings detail. The small fast object goes 0.544 → 0.675 and two of
three frames of the thin bars improve (squared error 48215 → 14477), but in the third a
bar's end had been matched to the neighbouring bar (identical bars, 40 px apart, moving
16), and what was a 3-pixel fragment becomes a whole phantom bar: thin bars 0.79 → 0.55,
the real set 0.261 → 0.253. Two frames cannot tell that match from the right one, and
nor can three (the bars repeat in every frame). `MVGPU_EDGE=1` turns it on. What it
needs first is a source-frame search that refuses a match when another place matches as
well.

`MVGPU_SHOW=1|2|3` draws what was decided instead of the picture (the side taken, the
mismatch, the vector's length); it is how each of the four steps above was found.

## RIFE models compared through TensorRT

The same two sets, each model run through TensorRT (fp16) as playback runs it
(`truth.py --engines trt-4.26,...`). Worst areas / flicker:

| Model | Real footage (169 moments) | Synthetic scenes (12) |
|---|---|---|
| v4.26 (the default) | 0.309 / 40.7 | 0.696 / 16.8 |
| v4.26 heavy | 0.312 / 40.1 | 0.697 / 11.9 |
| v4.25 | 0.309 / 40.9 | 0.709 / 10.0 |
| v4.25 heavy | 0.281 / 41.4 | 0.667 / 15.2 |
| v4.22 | 0.309 / 41.1 | 0.688 / 12.3 |
| v4.22 lite | 0.308 / 41.1 | 0.693 / 12.5 |

- On real footage five of the six are the same (0.308-0.312, a spread smaller
  than the difference between two clips); v4.25 heavy is worse. No model
  change improves the picture, and the default stays.
- TensorRT at fp16 gives the picture RIFE-ncnn gives (v4.26: 0.309 against
  0.312).
- The synthetic differences are single scenes: the large flat object (0.05 to
  0.42) and the small fast one (0.44 to 0.93) move the mean; every model fails
  the fence.

## Hold fill on TensorRT

A second instance of the same engine fills drawings held two or three frames
(`_rife_trt` in `template.vpy`), as RIFE-ncnn does. On a 1080p 24 fps
animated film, RTX 4090, 600 source frames through `vspipe` with the scoring
above running on the same GPU: TensorRT 179 fps plain, 170 fps with hold fill,
about 0.9 GB more video memory; RIFE-ncnn 67 and 66 fps. Hold fill changed 75
and 143 of 480 output frames in two sections of the film. The held-drawing
tests (`tests/integration/test_template_hold_fill_real.py`) pass on TensorRT.

## DRBA (DistilDRBA) through TensorRT

DistilDRBA is one network: three frames and a time in, one frame out
(`contrib/drba_export.py` exports it to ONNX; `_drba` in `template.vpy` runs it).

- **The export is exact.** TensorRT at fp32 against PyTorch on a 480x270 test
  picture: worst pixel 0.16 of 255. At fp16 with only the Cast nodes kept:
  worst 26, mean 0.70; with the arithmetic of the sampling positions kept in
  fp32 as well (`_grid_nodes`): worst 12.7, mean 0.29.
- **Speed.** 1080p 24 fps animated film, RTX 4090, `vspipe`, 2x: v1 196 fps
  (3.0 GB), v2 lite 204 fps (1.5 GB), RIFE v4.26 213 fps (2.1 GB).
- **On the drawn scenes** (generic motion, not what it is for): v1 0.500 / 19.1,
  v2 lite 0.543 / 20.8, RIFE v4.26 0.696 / 16.8. It reads the frame after the
  pair, and a repeated frame there counts as a held drawing: scored with the
  last frame repeated, v1 fell to 0.232. The test now draws one frame more.
- **What it does with animation** (320x192 clip, disc drawn on threes moving
  12 px a drawing, output steps of the disc in px, six output frames):

  | | backdrop still | backdrop panning 4 px a frame |
  |---|---|---|
  | RIFE v4.26 | 0 0 0 0 6 6 | 0 0 0 0 6 6, backdrop even (sd 0.26) |
  | RIFE v4.26 + hold fill | 2 2 2 2 2 2 | as above: no frame repeats, nothing to fill |
  | DRBA v1 | 0 0 0 0 12 0 | 0 0 0 0 10 2, backdrop sd 0.65 |

  DRBA keeps the drawing where it was drawn and smooths the camera. On motion
  drawn on every frame it is less even than RIFE (disc steps sd 0.46 against
  0.06). It is a look some prefer for animation, not more smoothness, so no
  rule picks it: a profile names the model.
