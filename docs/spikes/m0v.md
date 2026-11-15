<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(v): frames shown late on a fast screen (2026-11-14)

**Question.** At 144 fps, 0.3 to 1.4 % of the frames are dropped at the output although
the engine is fast enough (the 1080p cascade of M0(u) runs 1.3× real time untimed, with
the GPU at about 50 % and the CPU at 14 %). Why, and what can ButterEye do about it?

**Answer.** The screen misses refreshes while the GPU is busy, and with display sync mpv
then drops the next frame. It is not the engine falling behind, it does not depend on
what the video shows, and no player setting tried here absorbs it. ButterEye now tells
the two apart instead of blaming smoothing. Removing the late frames needs shorter
bursts of GPU work or a different way of putting frames on the screen.

RTX 4090, Fedora 45, a 3440 × 1440 screen at 144 Hz among four screens of 60 to 144 Hz,
a compositing Wayland desktop without variable refresh and without direct scanout.
A 4K 24 fps video, mpv with `gpu-next`, Vulkan and `video-sync=display-resample`.

## Any GPU work does it, and longer bursts do it more

The plain player (no smoothing) in a window, while another program runs a RIFE engine
in bursts with pauses between, so the load is about the same and only the length of a
burst changes. Late refreshes are mpv's own count (`vo-delayed-frame-count`), 30 s each:

| other program | late refreshes | of its bursts |
|---|---|---|
| none | 6 | – |
| 3.6 ms bursts, 6 ms pauses | 9 | 0.1 % |
| 9.1 ms bursts, 12 ms pauses | 20 | 1 % |
| 17.9 ms bursts, 24 ms pauses | 46 | 6 % |
| 41 ms bursts, 54 ms pauses | 31 | 8 % |

A refresh lasts 6.9 ms here. A burst shorter than that almost never costs one; a longer
one does more often the longer it is. The plain player loses nothing visible (each film
frame is up for six refreshes); at 144 fps every late refresh costs a frame.

## Nothing on the player's side absorbs it

The plain player under the 18 ms bursts (46 late refreshes with its own settings):

| player setting | late refreshes in 30 s |
|---|---|
| `swapchain-depth=8` | 67 |
| `swapchain-depth=1` | 150 |
| fullscreen | 52 |
| `profile=fast` (cheap scaling) | 81 |
| `gpu-api=opengl` | 267 |
| `vulkan-swap-mode=mailbox` | thousands (the counter stops meaning anything) |

## In the pipeline

Frames dropped in 60 s of steady play at 144 fps (8640 frames), ButterEye's own player
settings, each run:

| what runs | dropped | late refreshes |
|---|---|---|
| cascade, RIFE at 1080 lines (8 to 9 ms a frame) | 36, 18, 90 | 47, 31, 103 |
| cascade, RIFE at 900 lines (about 6 ms) | 28 | 40 |
| cascade, RIFE at 720 lines (3.9 ms) | 25, 10 | 35, 20 |
| cascade, v4.22 lite at 1080 lines (about 7 ms) | 70 | 85 |

Film doubled with RIFE at 1440 lines (16 ms a frame, 48 fps, each frame up for three
refreshes): 111 to 152 late refreshes and 30 to 43 dropped frames in 60 s, the same with
one TensorRT stream as with four. Motion vectors alone at 4K (about 1 ms a frame): 14
late refreshes and 2 dropped frames in 45 s.

So the neural engine's bursts add to a floor the desktop has by itself. A smaller
inference shortens them, but as the pipeline is that means a softer picture, and the
floor stays. The spread from run to run is large: the numbers above carry a trend, not
a second decimal. A deeper frame queue does not help either (M0(u)).

## What ButterEye does with it

- **It says what it is.** mpv counts the refreshes its output missed. A dropped frame
  that such a refresh explains is reported as shown late by the screen ("Smooth, some
  frames late"), with no step down offered: a lighter engine does not make the screen
  keep its refreshes. Before, the row said smoothing could not quite keep up.
- **The engine is judged by the rest.** Only dropped frames that no missed refresh
  explains count against the engine, for the step down, for replacing it and for what
  is remembered about its speed (`health.Verdict.engine_rate`, SCOPE §4.11).
- **It does not depend on the content.** The bursts are the same for any picture. What
  the script already skips shortens them a little: still pairs are copied, and a cut
  repeats a frame.

## What would remove them

- **Shorter bursts.** Motion estimated by the network on a small picture, with the
  full-size frame built by mvgpu's renderer: a 720-line inference is 3.9 ms, under one
  refresh. This is the next engine step for speed as well.
- **The display side.** Variable refresh would show a late frame a few milliseconds
  late instead of a whole refresh late, and direct scanout would take the compositor's
  own drawing out of the path. Both are desktop settings, off on this machine. Variable
  refresh is measured below; direct scanout is not tried.

## Variable refresh, measured (2026-11-14)

The same 4K 24 fps video as above, smoothed to 144 fps by the cascade at 1080 lines,
fullscreen on the second screen (2560 × 1080 at 144 Hz, variable range 50–144 Hz). Each
round is 60 s of steady play with the screen's variable refresh off, then 60 s with it
on for fullscreen windows (Hyprland 0.57, `vrr = 2` on that screen only).

| round | refresh | dropped | late (mpv) | app's health, share of half-seconds "ok" |
|---|---|---|---|---|
| 1 | fixed | 88 | 116 | 58 % |
| 1 | variable | 65 | 0 | 71 % |
| 2 | fixed | 40 | 53 | 92 % |
| 2 | variable | 0 | 23 | 100 % |
| 3 | fixed | 18 | 29 | 98 % |
| 3 | variable | 4 | 43 | 100 % |

- **It helps.** Two of the three runs with variable refresh dropped nothing or almost
  nothing, and the app's health stayed "ok" the whole minute; every fixed-refresh run
  dropped 18–88 frames. The first variable run is the exception: the GPU peaked at 98 %
  in it (54–62 % in every other run), so something else ran on the GPU then, and mpv
  counted 928 mistimed frames there against 0–88 elsewhere. Three rounds is a small sample.
- **mpv still counts late frames with it** (23 and 43): a frame that misses its refresh
  is now shown a little late instead of a whole refresh late, and is no longer dropped.
- **The monitor's menu decides whether the driver offers it.** The main screen
  (3440 × 1440, its EDID advertises 48–180 Hz) was reported as not capable with
  "Adaptive-Sync" on in its menu, with motion-blur reduction off, in SDR and after a
  factory reset. It became capable only with "FreeSync Premium" on in its menu. The
  driver reads this when the screen connects, so each menu change needs a power cycle.

### The main screen (3440 × 1440, HDR)

The same video and cascade, fullscreen on the main screen at 144 Hz, with the desktop in
HDR (PQ output), RIFE on TensorRT at 144 fps. Same rounds as above.

| round | refresh | dropped | late (mpv) | app's health, share of half-seconds "ok" |
|---|---|---|---|---|
| 1 | fixed | 30 | 45 | 100 % |
| 1 | variable | 2 | 34 | 100 % |
| 2 | fixed | 38 | 54 | 100 % |
| 2 | variable | 3 | 28 | 100 % |
| 3 | fixed | 18 | 34 | 100 % |
| 3 | variable | 4 | 52 | 100 % |

- **It helps here too, and steadily.** Every variable run dropped 2–4 frames a minute
  against 18–38 with fixed refresh; the GPU was at 51–53 % mean (64–66 % peak) in every
  run, so nothing else interfered this time.
- **Always-on variable refresh flickers on this panel's desktop**; fullscreen only
  (`vrr = 2`) does not, and is what the screen is set to.
- **A Hyprland detail:** a screen rule that differs from the active one only in `vrr` is
  skipped as already applied. It takes effect with a mode change (another refresh rate,
  then back) or at the next start of the session.
