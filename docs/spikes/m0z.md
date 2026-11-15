<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# M0(z): a present layer for in-between frames, stages S0 to S4e and the S4 follow-ups (draft, 2026-11-14)

**Question.** Can ButterEye put an explicit Vulkan layer between mpv and the screen,
find mpv's swapchain in it without disturbing the other Vulkan users in mpv's process
(mvgpu, RIFE-ncnn), and see every present with its timing? This is the first stage of a
layer that will later add the in-between frames on the GPU at present time. Stage S2
then asks: can the layer turn each of mpv's presents into k presents, evenly paced on the
screen, without upsetting mpv's timing or A/V sync? Stage S3 asks whether the layer can
make the added frames itself on the GPU, from mpv's last two images, cheaply and with the
colours right, so that stage S4 can put motion-compensated frames in the same place.

**Answer so far.** Yes. The layer (`VK_LAYER_BUTTEREYE_frames`, `mvgpu/layer/`) loads
only when asked for, finds mpv's instance by its Wayland surface extension, leaves the
mvgpu and RIFE-ncnn instances alone, sees one present per mpv frame, and costs well under
a microsecond per present at the median. With it in the chain, mpv's own counters stay
within the spread they have without it. Placed below NVIDIA's present layer, it also sees
the presents that the driver's frame generation adds.

S2: yes. At k = 3 (mpv at 48 fps on the 144 Hz screen, the layer showing each image three
times), the screen got a new present on 99.99 % of its refreshes, mpv dropped nothing,
and A/V stayed where it is without the layer. The cost is delay: the app's image reaches
the screen k − 1 refreshes later than without the layer (+13.2 ms at 144 Hz), as planned,
but only because the layer holds mpv back after its presents. Without that hold, the
deeper swapchain fills up and the delay is +27 to +35 ms.

S3: yes. The layer keeps mpv's last two images, mixes them at 1/3 and 2/3 in one compute
dispatch and shows the mixes as the two added frames. At 3440 × 1440 that costs 31–32 µs (median of three 30 s runs) of
GPU time per in-between frame (the dispatch) and 0.124 ms (99th percentile 0.131 ms) per mpv frame for all of the layer's
GPU work, with pacing and mpv's counters as with plain copies. The mixes are bit-exact
against a reference on real content and on every swapchain format mpv can be made to
pick here, and the colours on screen are right in each of them.

S4: yes. The layer now makes motion-compensated in-between frames with mvgpu's own engine,
inside mpv's present, from mpv's last two images at display size. On truth.py's real
moments its worst areas equal the plugin's in VapourSynth (169 moments: +0.0001 ± 0.0008;
99 seven-frame moments: +0.007 ± 0.004), with PSNR 0.17–0.18 dB lower. With RIFE 24 → 48 in
VapourSynth in front of it, three 60 s runs on the 144 Hz screen had no dropped or late
frame and no missed refresh, as today's cascade had none dropped and 0–1 late. Each mpv
frame costs 2.1–2.2 ms of GPU time at the median (one submit, over the 2 ms the design
aimed for), and up to 7 ms at the 99th percentile when RIFE runs at the same time.

S4 follow-ups: the work is now split into a submit for the pair (1.4 ms at the median) and
one per in-between frame (0.37 ms each), so the largest submit is 1.4 ms at the median
instead of 2.1–2.2 ms, and its 99th percentile under RIFE's load falls from 7.2–7.3 ms to
4.5–5.0 ms; three 60 s runs with RIFE had no drop, late frame or missed refresh. A higher
queue priority is not permitted for an ordinary user on this driver, and an async-compute
queue made the GPU times worse, so neither is on. The engine, the blend's images and the
layer's fences are now made off the present path, and a counter in the control block
counts scene cuts.

RTX 4090, Fedora 45, NVIDIA driver 620.70.06, a 3440 × 1440 screen at 144 Hz, Wayland
compositor without variable refresh. mpv 0.41 with `gpu-next`, Vulkan, `waylandvk`,
ButterEye's player profile (`video-sync=display-resample`), fullscreen. A public test
clip: 76 s of Big Buck Bunny (CC BY 3.0, Blender Foundation), 3840 × 2160, re-timed to
24 fps. For S2, 80 s of the same film at 1920 × 1080, 48 fps (each frame shown twice)
with a test tone, and a generated 48 fps clip of a white bar moving 24 pixels a frame.

## S0: tools

The Khronos validation layer (1.4.350, the Fedora package) was downloaded with
`dnf download` and unpacked into a scratch folder with `rpm2cpio | cpio`; its manifest
there points at the unpacked library by absolute path, and `VK_ADD_LAYER_PATH` finds it.
Nothing was installed. The package signature checked out. On lavapipe (Mesa's CPU Vulkan
driver) the same layer runs the tests without a GPU.

## What S1 is

A pass-through layer. It changes no image, no swapchain and no present. It:

- negotiates loader interface 2 and keeps its own dispatch tables per instance and per
  device; it never links the Vulkan loader;
- marks an instance that enabled `VK_KHR_surface` and `VK_KHR_wayland_surface`
  (mpv's). Every other instance and its devices pass straight through. mpv's instance
  reports no application or engine name, so the name cannot be used to find it;
- with `BUTTEREYE_FRAMES_KMAX` of 2 or more, enables on mpv's instance and device the
  extensions later stages need when they are offered and missing:
  `VK_KHR_get_surface_capabilities2`, `VK_KHR_surface_maintenance1`,
  `VK_KHR_swapchain_maintenance1`, `VK_KHR_present_id2`, `VK_KHR_present_wait2`,
  `VK_KHR_calibrated_timestamps` and `VK_EXT_present_timing`, with their feature bits.
  A feature struct the app already chained is changed for the create call and put back
  afterwards. If the create fails, the layer tries again with the app's own create info
  and leaves that device alone;
- logs what mpv asks for (format, colour space, image count, present mode, usage,
  pNext types) and times every present on mpv's device: when it arrived, how long the
  next layer's `vkQueuePresentKHR` took and how long the layer itself took. A summary
  per swapchain goes to stderr when it is destroyed, and `BUTTEREYE_FRAMES_LOG` writes
  one CSV row per present.

It is built only with `meson setup -Dlayer=enabled`; the option is off by default, and the
plugin built without it is byte for byte the same as before.

## What mpv asks for

On this machine, every run:

- mpv's instance already enables `VK_KHR_get_surface_capabilities2` and a surface
  maintenance extension; the layer added none at instance level;
- mpv's device already enables `VK_KHR_swapchain_maintenance1`; the layer added
  `VK_KHR_present_id2`, `VK_KHR_present_wait2`, `VK_KHR_calibrated_timestamps` and
  `VK_EXT_present_timing` without a retry;
- three swapchains per start: one at the window size, then two at the screen size
  as the window goes fullscreen. The last one, fullscreen on this screen, is
  `A2B10G10R10_UNORM` in the **HDR10 (ST 2084)** colour space, not sRGB. Usage is
  transfer-dst, storage and colour attachment; FIFO; 3 images;
- each present carries `VkSwapchainPresentFenceInfoKHR`, one wait semaphore and one
  swapchain.

## The chain

`VK_LOADER_DEBUG=layer`, instance and device chains:

| environment | chain (app → driver) |
|---|---|
| the session's own (every ButterEye mpv today) | device_select, NV_dlssnr, **NV_present** |
| launcher style without the layer | device_select, NV_dlssnr |
| launcher style with the layer | device_select, NV_dlssnr, **BUTTEREYE_frames** |

Launcher style is `VK_INSTANCE_LAYERS` without NVIDIA's present layer,
`DISABLE_LAYER_NV_PRESENT_1=1`, and the layer turned on with `VK_ADD_LAYER_PATH` and
`VK_LOADER_LAYERS_ENABLE`. NVIDIA's present layer is absent from the chain then.

## Other Vulkan users in mpv's process

Through the layer, with `BUTTEREYE_FRAMES_KMAX=3`:

- **mvgpu** (through ButterEye's core, 1080p source to 144 fps): two instances named
  `mvgpu`, API 1.1, no extensions; both and their devices passed through. 12 s played,
  0 frames dropped, 144 fps out of the filter.
- **RIFE-ncnn** (ButterEye's template, 1080p, 24 → 48 fps, model v4.26): one instance
  named `ncnn` whose device does enable `VK_KHR_swapchain` (31 extensions) but no
  Wayland surface; passed through. 12 s played, 0 dropped, 48 fps out of the filter.
  The core itself picked mvgpu when asked for RIFE-ncnn, so this run went through the
  template directly.

## Timing: with and without the layer

60 s of steady play after 8 s of warm-up, plain player (no smoothing), mpv presents at the
screen's rate here (no `display-fps-override`), so the layer should see 144 presents a
second. Runs were interleaved: rounds 1 to 3 in the order below, rounds 4 and 5 reversed.

| setting | late refreshes in 60 s, each run | median | presents/s seen by the layer | layer's own time per present, µs (median; 99th percentile) |
|---|---|---|---|---|
| the session's own environment, no layer | 230, 21, 26, 168, 68 | 68 | – | – |
| launcher style, no layer | 109, 19, 55, 97, 47 | 55 | – | – |
| launcher style, layer passing through | 60, 19, 151, 125, 200 | 125 | 143.9, 144.0, 142.4, 142.7, 141.3 | 0.34–0.97; 2.7–5.8 |
| launcher style, layer adding its extensions | 24, 23, 117, 178, 103 | 103 | 144.0, 144.0, 142.9, 141.0, 142.9 | 0.31–1.12; 2.8–6.2 |

Frame drops were 0 in every run. The late refreshes (`vo-delayed-frame-count`) swing from
about 20 to over 200 between runs of the same setting, with the other programs on the
shared machine. Every setting's runs fall inside the range the others cover, but the two
layer settings have the higher medians (125 and 103 against 68 and 55), so five runs on a
busy machine do not rule out a small cost. The layer's own work per present is a lookup
and a few clock reads, under a microsecond at the median; the time inside the next
layer's present was 41–70 µs at the median. Presents seen by the layer fall below 144 a
second by about as much as mpv reports late refreshes.

With `--display-fps-override=48` (one 15 s run, no frames added by the layer), mpv still
presented 144 times a second and played the video three times too fast (45 s of video in
15 s): the override only works once the layer holds each of mpv's presents for k
refreshes, which is what stage S2 adds.

The validation layer (`VK_LAYER_KHRONOS_validation` 1.4.350, errors, warnings and
performance) placed below the layer reported nothing in 15 s of play with the layer
passing through, with the layer adding its extensions, and without the layer. On
lavapipe, the headless test program through the layer is also clean, including
synchronisation checks.

## Below NVIDIA's present layer

With the layer listed after `VK_LAYER_NV_present` in `VK_INSTANCE_LAYERS` it sits
between NVIDIA's present layer and the driver, and sees what that layer passes down.
1080p 24 fps, `video-sync=audio` (mpv presents once per video frame), 15 s, 2 runs each:

| NVIDIA smooth motion | presents/s seen below it | interval between presents |
|---|---|---|
| off | 24.0, 24.0 | 41.7 ms (median) |
| on (`NVPRESENT_ENABLE_SMOOTH_MOTION=1`) | 48.0, 48.0 | 20.9 ms (median), 97 % within 19.5–22.5 ms |

With smooth motion on, NVIDIA's layer:

- passes its generated frames down as ordinary `vkQueuePresentKHR` calls, evenly spaced
  three refreshes apart, every other one from swapchain image 0;
- enables more extensions on the way down (instance 8 → 15, device 28 → 47);
- creates the real swapchain with transfer-dst usage only (mpv asked for storage and
  colour attachment too), so mpv draws into images of NVIDIA's layer and the driver
  copies;
- replaces mpv's present-fence struct with `VkSetPresentConfigNV`
  (`VK_NV_present_metering`).

So the probe can measure the driver's frame generation from below: present count and
spacing per second.

## S2: k presents per mpv present

With `BUTTEREYE_FRAMES_KMAX` of 2 or more and k of 2 or more, on a FIFO swapchain of mpv's:

- the swapchain is created with kmax more images (3 + 3 here; the limit is 8), transfer
  usage added, and, where the surface offers them, present ids, present timing (first
  pixel out) and present wait. The present-timing queue holds 64 results;
- each of mpv's presents of image i becomes: acquire k − 1 more images with the layer's
  own semaphores; one submit on mpv's present queue that waits on mpv's semaphores,
  copies i into each of them (`vkCmdCopyImage`, same format, so bit for bit) and signals
  one semaphore per image; then k − 1 presents of the copies and the present of i with
  mpv's own pNext (its present fence). Under FIFO each present holds the screen for one
  refresh, so no timer is needed;
- mpv's present goes through unchanged on the first present, at k = 1, when the gap since
  mpv's last present is over 1.5 × k refreshes (a drop, repeat or redraw), when the event
  sequence in the control block changed (pause, seek), when no image could be acquired
  in time, or when mpv sends its own present ids;
- **the hold**: after the k presents, the layer returns to mpv only once at most k − 1 of
  them are still waiting for the screen (`vkWaitForPresent2KHR`). Without it, the
  added images let mpv draw up to five presents ahead (table below);
- the control block (`$XDG_RUNTIME_DIR/buttereye/frames-<pid>` or `BUTTEREYE_FRAMES_SHM`,
  256 bytes, two seqlocks; layout in `mvgpu/layer/shm.cpp`) carries k and the event
  sequence from ButterEye, and the layer's counters back: presents, presents added,
  passed through, missed refreshes, refresh duration, last delay;
- the per-present log has one row per present (kind: copy, app image after its copies,
  app image passed through, with the reason), one row per present-timing result, the
  clock calibration and the waits.

`--display-fps-override=48` was set at start; mpv's own rate and estimate then read 48.

### What the driver reports

On this driver and compositor, present timing offers the stages "queue operations end"
and "first pixel out", in a present-stage-local time domain. Calibrated against
CLOCK_MONOTONIC once a second, the offset is 0.0 ms (spread under 1 µs): the domain is the
monotonic clock at microsecond resolution. The refresh duration reported is 6.9444 ms.
`vkWaitForPresent2KHR` returns 0.6–0.7 ms after the first pixel out of the id it waits on
(10th to 90th percentile).

FIFO does not block in `vkQueuePresentKHR`: the next layer's present takes 24–29 µs at
the median and under 0.8 ms at most. Without the hold, mpv's back-pressure lands on the
layer's acquire (13.5 ms median); with it, on the hold (20.1 ms median, one mpv frame), and
the acquire takes about 20 µs (median).

### The delay, and the hold

From mpv's acquire of an image to the first pixel of that image, 12 s each, one run
(mpv draws and presents within 0.4 ms of its acquire, so from its present is nearly the
same):

| setting | swapchain images | delay, median |
|---|---|---|
| k = 1, no images added | 3 | 20.7 ms |
| k = 1, 2 added | 5 | 34.6 ms |
| k = 1, 3 added | 6 | 41.5 ms |
| k = 3, no hold | 6 | 55.5 ms |
| k = 3, no hold, 2 added | 5 | 48.5 ms |
| k = 3, wait before the presents until 4 are left | 6 | 47.7 ms |
| k = 3, hold 1 | 6 | 27.1 ms (6 missed refreshes in 12 s) |
| k = 3, hold 1, 2 added | 5 | 27.1 ms |
| **k = 3, hold 2 (the default, k − 1)** | 6 | **34.0 ms** |

Under FIFO every image there is ends up queued, one refresh each. The hold brings the delay
to the planned k − 1 refreshes (+13.3 ms). Waiting before the presents instead does not
help: mpv has then already drawn its next frame. A hold of 1 is a refresh shorter, with
less margin; it needs more runs before it can be the default.

### Pacing, mpv's counters and A/V: `--wayland-present` × k

60 s after 6 s of warm-up, 3 runs each, the second round in reverse order. "k = 1" is
the layer with no images added and present timing on, so the same measurement as k = 3.
Screen intervals come from first-pixel-out times of consecutive presents, in refreshes.

| setting | presents/s | screen intervals of 1 refresh | missed refreshes | mpv drops | mpv late | delay (median; 99th percentile) | A/V (mpv `avsync`) at the end, ms |
|---|---|---|---|---|---|---|---|
| k = 1, feedback on | 142.8, 143.9, 144.0 | 99.2 %, 99.97 %, 99.97 % | 0.83 %, 0.05 %, 0.04 % | 21, 0, 0 | 67, 3, 3 | 20.8; 20.8–27.5 ms | 13.3, −0.4, −0.4 |
| k = 3, feedback on | 143.9, 144.0, 144.0 | 99.98 %, 100 %, 100 % | 0.02 %, 0, 0 | 0, 0, 0 | 0, 1, 1 | 34.0; 34.2 ms | −11.9, 1.8, 4.8 |
| k = 1, `--wayland-present=no` | 143.9, 143.9, 144.0 | 99.96 %, 99.96 %, 99.99 % | 0.04 %, 0.04 %, 0.02 % | 0, 0, 0 | 3, 3, 1 | 20.8; 20.8 ms | 1.0, −1.9, −0.8 |
| k = 3, `--wayland-present=no` | 144.0, 143.9, 143.8 | 100 %, 99.95 %, 99.83 % | 0, 0.05 %, 0.17 % | 0, 0, 3 | 0, 2, 7 | 34.0; 34.2 ms | −0.2, −6.9, 1.4 |

At k = 3 mpv presented 47.9–48.0 times a second and the layer added two copies to each
(2877–2881 of mpv's presents took the k path, 0 or 1 passed through on a cadence break).
mpv's `estimated-display-fps` read 47.95–48.02, playback speed against the wall clock
0.9992–1.0003, and `avsync` stayed within ±21 ms in every run with no trend. Feedback on or
off made no clear difference here, so `--wayland-present` can stay at mpv's default.

### Under GPU bursts

The same, with another program on the GPU running compute bursts of 18 ms (median; 90th
percentile 20.8 ms) with 24 ms pauses, like M0(v)'s heaviest RIFE case. 60 s, 3 runs each:

| setting | missed refreshes | mpv drops | mpv late | delay (median; 99th percentile) | `avsync` range, ms | speed |
|---|---|---|---|---|---|---|
| k = 1 | 2.5 %, 2.6 %, 2.3 % | 55, 63, 55 | 204, 220, 192 | 20.8; 27.7 ms | within ±27 | 0.999 |
| k = 3 | 2.3 %, 2.0 %, 2.4 % | 70, 55, 77 | 90, 85, 91 | 34.0; 41.0 ms | within ±31 | 0.999 |
| k = 3, `--wayland-present=no` | 1.7 %, 2.2 %, 2.2 % | 46, 59, 61 | 70, 91, 89 | 34.0; 41.1 ms | within ±29 | 0.999 |

The screen misses about as many refreshes with the layer as without, and A/V does not
drift: mpv corrects it as it does without the layer. But at k = 3 a missed refresh
costs one of mpv's frames more often (drops were 36–38 % of the missed refreshes,
against 26–28 % at k = 1), and one of mpv's frames there is three refreshes. A missed refresh shifts the
whole FIFO queue by one refresh, and mpv then catches up by dropping. A later stage could
let the layer absorb a miss instead, by sending one copy fewer.

### The moving bar

The bar clip (48 fps, 24 pixels a frame) at k = 3, 25 s, 11 screenshots of the screen
taken during play: each shows one sharp bar (22 pixels wide at this size, the background
unchanged around it) at a whole source position, 32 screen pixels apart. The screenshots
sample the screen at 11 moments only; they show no mixed or torn frame, not that every
refresh was right. Taking them cost refreshes (12 missed, 4 drops in that run).

### Validation

The Khronos validation layer below this one, on the GPU, 20 s at k = 3 with and without
feedback: nothing reported. On lavapipe, the test program at k = 3 with synchronisation
checks, a pause and an event, with the hold on and off: nothing reported.

## S3: blended frames

### What it is

With `BUTTEREYE_FRAMES_MODE=blend` (or mode 2 in the control block; 1 means copies, 0
keeps the environment's choice), the k − 1 added presents show mixes of mpv's previous and
current images instead of copies:

- per swapchain, made on the first present that asks for blends and freed with the
  swapchain: two history images, one output image with one layer per in-between frame
  (kmax − 1), a compute pipeline (`mvgpu/layer/blend.comp`, built into the layer as
  SPIR-V) and a timestamp query pool. 80.6 MB of images at 3440 × 1440 with kmax = 3.
  Every call goes through the layer's own device table, never through the loader;
- in the same submit as S2's copies, on mpv's present queue: mpv's image i is copied into
  a history image. Once the history also holds mpv's previous image, one dispatch makes
  all k − 1 mixes, and the output layers are copied into the k − 1 acquired images. The
  screen then shows: mpv's previous image, the mix at 1/3, the mix at 2/3, image i. The
  delay is the same as in S2;
- each channel is mixed as integers: (a·(k − m) + c·m + k/2) / k for in-between frame m,
  alpha included. A pixel that is the same in both images comes out with the same bits;
- after any pass-through (first present, cadence break, event, failed acquire) the
  history starts again, so the next k-path present sends copies and the one after it
  blends;
- `BUTTEREYE_FRAMES_SHOW=1` draws a 96-pixel marker square near the top left of every
  frame the layer makes: red for m = 1, blue for m = 2, green for m = 3, at half level;
- four timestamps per mpv present (start, after the copy in, after the dispatch, end) go
  to the per-present log as G rows and into the swapchain summary;
- for checks only: `BUTTEREYE_FRAMES_DUMP` (with `BUTTEREYE_FRAMES_DUMP_AT=n`) writes the
  history and output images of the n-th blended present to a file, and
  `BUTTEREYE_FRAMES_TEST_FORMATS` hides every other surface format from mpv's instance, so
  that mpv picks one of the listed formats.

**Formats.** The plan had an output image per swapchain format and an R/B swizzle for
`A2R10G10B10`, which cannot be a storage image on this driver. S3 does it differently:
every swapchain format handled here packs a pixel into 32 bits, so the history and output
images are `R32_UINT`. The copies between them and the swapchain images are bit for bit
(`vkCmdCopyImage` between formats of the same size), and the shader unpacks the channels
itself: three of 8 bits and 8 of alpha, or three of 10 bits and 2 of alpha. No colour is
converted on the way in or out, so `_SRGB` and HDR10 images are mixed on the encoded
values they hold. For mixing, which of the outer channels is red does not matter, so the
swizzle disappears. It matters only for the marker, and later for luma in S4, and there it
is one flag per format. `R16G16B16A16_SFLOAT` (64 bits a pixel) is not handled: the added
frames stay copies, with a log line.

**HDR10.** S1 found the fullscreen swapchain on this screen to be `A2B10G10R10` in the HDR10
(ST 2084) colour space, even for SDR video. The layer handles it like any other 32-bit
format and mixes the PQ-encoded values. For a mix this is fine; PQ is closer to even
perceptual steps than a gamma curve is. SDR video in that swapchain uses only the lower part
of the range: in a real-content dump the brightest pixel sits at 0.58 of full scale (SDR
white mapped to about 203 cd/m²). That matters for S4 (below).

### GPU time

3440 × 1440, `A2B10G10R10`, k = 3, 1080p 48 fps clip, fullscreen, from the layer's
timestamps over every blended present of a 30 s window (about 1440 each):

| run | dispatch per in-between frame, µs (median; 99th pct; max) | all of the layer's GPU work per mpv frame, µs (median; 99th pct; max) | copy in | copies out (2) |
|---|---|---|---|---|
| 1 | 32.3; 34.8; 49.7 | 124.1; 131.1; 160.9 | 20.5 | 39.9 |
| 2 | 32.3; 34.3; 35.8 | 124.3; 130.8; 132.9 | 21.1 | 39.9 |
| 3 | 31.2; 34.3; 35.3 | 123.7; 131.1; 135.3 | 21.0 | 39.9 |

The plan expected under 0.3 ms per in-between frame; the mix takes about 0.03 ms. Most of
the layer's 0.12 ms per mpv frame is copying whole images: about 20 µs per 3440 × 1440 image
(one in, two out). With the other formats (8-bit ones too) the dispatch took 27–32 µs at
the median.

### Pacing: copies against blend

Same clip, 30 s after 6 s of warm-up, 3 runs each, interleaved (the second round in reverse
order):

| setting | presents/s | screen intervals of 1 refresh | missed refreshes | mpv drops | mpv late | delay, mpv acquire to first pixel (median) | largest \|`avsync`\|, ms | speed |
|---|---|---|---|---|---|---|---|---|
| k = 3, copies | 143.97, 144.07, 143.95 | 99.95 %, 100 %, 100 % | 2, 0, 0 | 1, 0, 0 | 1, 1, 0 | 34.02 ms (all three) | 14.5, 15.9, 11.1 | 1.0002, 1.0002, 0.9996 |
| k = 3, blend | 143.95, 143.94, 144.04 | 100 %, 99.98 %, 100 % | 0, 1, 0 | 0, 0, 0 | 0, 0, 0 | 34.00–34.01 ms | 11.7, 13.3, 13.1 | 0.9996, 0.9996, 1.0003 |

With the blend, mpv presented 47.98–48.01 times a second, 96 blended frames a second were
added, every mpv present took the k path, and `estimated-display-fps` read 47.98–48.00.
Pacing, delay and A/V are as with copies. Not measured with the blend: GPU bursts (S2's
burst runs used copies), other screens, long windowed play.

### The moving bar

The bar clip (48 fps, 24 pixels a frame) with blends and the marker, 30 s, 26 screenshots of
the screen taken during play. Each screenshot was classified by its marker and by the bar
profile along one row:

| marker | screenshots | what the row shows |
|---|---|---|
| none (mpv's own image) | 7 | one sharp bar, 23 pixels at full level |
| red (m = 1) | 6 | two bars 32 pixels apart: the earlier position at level 190, the later at 98 |
| blue (m = 2) | 13 | two bars 32 pixels apart: the earlier position at 98, the later at 190 |

All 26 agree with the rule: a double exposure only on marked frames, with the weights in
the right order (2/3 of the previous image at m = 1, 1/3 at m = 2), on a grey of 32. The
screenshots sample 26 moments only. Taking them cost refreshes in that run (0.67 % missed,
18 mpv drops), as in S2.

### Colours and formats

Each format was forced with the test knob, except the default. mpv chose its colour
space itself. The dump check compares every pixel of both in-between frames, outside the
marker (4,944,384 pixels each), with the integer mix of the two history images. The marker
check reads the marker's colour in screenshots taken during play.

| swapchain format | how | colour space mpv chose | pixels off in the dump (m = 1, m = 2) | marker on screen |
|---|---|---|---|---|
| `A2B10G10R10_UNORM` | mpv's own choice | HDR10 (ST 2084) | 0, 0 | red and blue (bar run) |
| `A2R10G10B10_UNORM` | test knob | HDR10 (ST 2084) | 0, 0 | red (2 of 5 screenshots) |
| `B8G8R8A8_UNORM` | test knob | Display P3 | 0, 0 | red and blue |
| `R8G8B8A8_UNORM` | test knob | Display P3 | 0, 0 | red and blue |
| `B8G8R8A8_SRGB`, `R8G8B8A8_SRGB` | test knob | – | – | – |

mpv cannot use a `_SRGB` swapchain here: with only those formats offered, its renderer
reports that it found no usable surface format and mpv falls back to another video output.
The `_SRGB` formats are covered on lavapipe only. There the tests check all four 8-bit
formats the headless surface offers (both `_SRGB` ones included): the mixes are bit-exact,
and the channel layout decodes the test program's clear colours, with red and blue set
apart.

On a still picture (colour bars), a dump showed mpv's two images identical and both mixes
equal to them bit for bit. Screenshots from separate runs cannot show this: two runs of
mpv with the same settings and no blend already differ from each other on about a million
pixels, by a few levels, so the cross-run comparison of copies against blend (about the
same difference) says nothing about the layer.

### Validation

The Khronos validation layer below this one, on the GPU, with blends: 10 s with the marker
and a dump, and 2 × 15 s with the final build (feedback on and off): nothing reported. On
lavapipe, with synchronisation checks, two formats, the marker, a dump, a pause and an
event: nothing reported.

### Tests

`tests/integration/test_frames_layer_probe.py` builds the layer and drives it on lavapipe
(CPU only): 21 tests for S1 to S3. The S3 ones check bit-exact mixes and the channel
layout for all four 8-bit formats, the restart of the history after a pass-through, the
mode in the control block, the format knob, and validation of the blend.

## S4a and S4b: the engine split out of the plugin

**What changed.** mvgpu's GPU work moved from the plugin into an engine
(`mvgpu/src/engine/`) that both the plugin and the layer build:

- **A function table.** The engine calls Vulkan only through a table of 27 device-level
  entry points (`vkfns.h`, an X-macro). The plugin fills it from the loader; the layer
  fills it from the next layer's `vkGetDeviceProcAddr`. `engine.cpp` is compiled with
  `VK_NO_PROTOTYPES`, so a call that bypasses the table does not compile. The layer still
  links no loader and exports one symbol.
- **Record, do not submit.** `Gpu::submit` used to record, submit and wait in one go. The
  engine now only records into a command buffer it is handed: `record_load` (unpack and
  pyramid), `record_vectors` (one pair), `record_select` (the per-pixel field at t) and
  `record_render` (the planes at t, packed). The plugin keeps its own instance and device,
  the uploads, one command buffer that it submits and waits for, and the readback ring, in
  the same order as before.
- **The brightness difference from a buffer.** The plugin measures each frame's mean luma
  on the CPU and passes the difference (gdc) as a push constant. The layer has no CPU copy
  of a frame. So `mean.comp` reduces the mean on the GPU (every 4th pixel of every 4th row,
  like the CPU mean: one group per sampled row, then one group over the rows), and a third
  mode writes gdc for the pair (A − C, A − before, C − after). The six shaders that use gdc
  (`search`, `median`, `subpel`, `uni`, `proj`, `select`) read it from a buffer when the
  specialization constant `GDC_FROM_BUFFER` is set. The plugin never sets it, so it
  specializes nothing it did not before.
- **Two fixes that validation found.** These do not change the plugin's output.
  - The slot and pyramid buffers are copied with `vkCmdCopyBuffer`, but they were made
    without transfer usage. The validation layer reports this for the plugin at HEAD:
    8 errors on the brightness test clip on lavapipe. They now have that usage.
  - The engine's barrier also covers a transfer write after a transfer write. That matters
    once several loads share one command buffer, as in the layer.

**Parity with the shipped plugin.** All four gates passed. The reference was the installed
0.0.3 package, which gives the same bytes as a build of HEAD.

| gate | result |
|---|---|
| Every output frame hashed: 3 drawn clips (8-bit, 8-bit with a flash every other frame, 10-bit at an odd size) at 2× and 3×, plus 6 real 60 fps moments at 2×. 214 frames in each of 10 engine settings (default, three-frame, edge, both with `SHOW=2`, `SHOW=1`, `SHOW=3`, every refinement off, temporal penalty on, temporal off, three-frame without the neighbour vector) | 0 of 2140 frames differ. The installed plugin run twice and the HEAD build also differ in 0 of 2140. |
| `tests/integration/test_mvgpu_real.py` against the new build | 3 passed. The numbers its three scripts print are identical to the installed plugin's. |
| `truth.py --mvgpu`, 12 drawn scenes | Identical in every field (worst, mean, PSNR, flicker). Mean of worst 0.663 for both. |
| `truth.py --mvgpu`, the 169 real moments (run in 4 chunks for memory) | Identical in every field for all 169. Mean of worst 0.174 for both. |
| `MVGPU_PROFILE` | see below |
| Windows and macOS builds | not built here; see below |

**Speed.** `MVGPU_PROFILE` was run on 160 real 1080p frames (40 moments from the
seven-frame set in a row), 24 → 72 fps. Each output frame was requested through
VapourSynth's threads, with the cumulative mean taken after 300 renders. Three rounds
alternate the two plugins, and round 2 swaps the order.

| size | GPU ms per render, installed | GPU ms per render, split engine |
|---|---|---|
| 1920 × 1080 | 2.23, 2.18, 2.19 | 2.23, 2.20, 2.12 |
| 3840 × 2160 (scaled up) | 8.79, 8.89, 8.99 | 9.54, 8.59, 8.92 |

The two plugins are the same within the spread.
- One run of the split engine, 9.54 ms at 2160 lines in round 1, is the outlier. In the
  same run its CPU copy times were 2 to 3 times the usual (load 6.46 ms against about
  2.2 ms), so the machine was busy then.
- The other two 2160-line runs of the split engine are 8.59 and 8.92 ms.
- Per load: about 0.3 ms GPU at 1080p and 1.0 ms at 2160p for both.

**Windows and macOS.** Not built here: the machine has no mingw cross toolchain (the
Windows build runs `meson` with the default options in a Fedora 44 mock chroot or
container, `packaging/windows/build-in-chroot.sh`, which needs `mingw64-*` packages that are
not installed). The macOS job does not build the plugins; `packaging/macos/build-plugins.sh`
runs the same default meson build. Both use the default options, so neither builds the layer
or `engine-check`. What did change for them:
- two more source files;
- one more shader (`mean.comp`), compiled by the same glslang command as the others;
- one more loader function taken by the plugin (`vkCmdUpdateBuffer`, Vulkan 1.0 core,
  exported by every loader).

The engine includes only the Vulkan headers and the C++ standard library. It has no
platform calls, no `__attribute__` and no POSIX headers. The plugin's sources build with
no warnings at `warning_level=2` under GCC. With `-Wpedantic -Wshadow` the only
remaining warning is a shadowed `n` in `getFrame`, which was already there.

The engine's GPU path was checked with `engine-check` (`mvgpu/tools/`, built with
`-Dlayer=enabled`). It runs four drawn frames, every other one brighter, three ways: with
push constants from the CPU means, with gdc read from the buffer after the CPU means are
copied in, and with gdc from the GPU-reduced means. Each way runs with the default rules
and with the three-frame and edge rules on.

- On lavapipe and on the RTX 4090, push constants and the buffer give the same bytes.
- The GPU means are within 9e-8 of the CPU's (on the GPU at 320 × 184, 1920 × 1080 and 3440 × 1440).
- With the GPU means, the output is the same at 320 × 184. At 96 × 64, a few pixels move
  by one level.
- A negative control (zeros copied in as the means) changes 136,248 of 176,640 bytes, so
  the check does see gdc.

**The engine inside the layer.** `BUTTEREYE_FRAMES_ENGINE_TEST=1` makes the layer run the
engine once per device, on the first k-path present, on the app's device and queue. It
uses the table filled from the next layer and the GPU means. It loads four frames, searches
three pairs, then selects and renders one in a single submit. On lavapipe under the
validation layer with synchronisation checks, nothing is reported, and the logged GPU means
equal the CPU's. The same run in mpv on the RTX 4090 (6 s, k = 3 blend, DP-1 fullscreen,
the validation layer below ours) behaved the same way:
- the engine ran on mpv's device and its means equal the CPU's;
- 268 of 273 of mpv's presents then took the k path;
- the validation log stayed empty.

The test runs synchronously inside one present, about 34 ms the first time, which is
fine for a test knob only.

**Not done in S4a/S4b.** No cut statistic on the GPU yet (the design's "mean + cut
reduction"; the plugin takes cuts from the script). The engine still works on YUV planes:
the RGB ingest and render are S4c.

## S4c to S4e: motion-compensated frames in the layer

**What it is.** `BUTTEREYE_FRAMES_MODE=mc` (or mode 3 in the control block) makes the k − 1
added presents motion-compensated in-between frames at t = m/k, made by mvgpu's engine on
mpv's own images (`mvgpu/layer/mc.h`, `mc.cpp`). The work goes where the blend's dispatch
was, in the same submit on mpv's queue, and uses the blend's images (the two `R32_UINT`
history images and the output array):

1. **Copy in.** mpv's image is copied into the history, as for the blend.
2. **Ingest** (`ingest.comp`). The packed pixels are unpacked with the format's bit width and
   red position, and luma is formed from the encoded R′G′B′: BT.709 weights on an SDR
   swapchain; on the HDR10 (PQ) swapchain mpv picks on this screen, BT.2020 weights after
   each channel is brought back to an SDR scale (PQ to cd/m², over the 203 cd/m² of SDR
   white, then a 2.4 gamma). The result is put on the video range (16–235) the plugin's
   thresholds were tuned on, and area-averaged down to source-equivalent scale over the
   video rectangle: the video's height when the control block gives it, never more than
   1080 lines. A 16:9 1080p picture on this 3440 × 1440 screen is 2560 × 1440 and is searched
   at 1920 × 1080, as the plugin would search the source.
3. **Pyramid and mean.** `record_load` is split in two so the layer can call its second half,
   `record_pyramid` (pyramid and GPU mean); the plugin still calls `record_load`, which
   records the same commands as before.
4. **Vectors, cut, select.** With the previous image ingested too: `record_vectors` for the
   pair (with the pair before as the temporal candidate when there is one), the scene-cut
   test (`cut.comp`), then per in-between frame `record_select` at t = m/k.
5. **Render** (`render_rgb.comp`). `render.comp`'s decisions on three colour channels at
   once, at display size: each display pixel takes the field of the source-scale pixel it
   falls in, the vector scaled by display/source, Catmull-Rom taps from the two history
   images, the occlusion side, and the fade to the plain blend where the vectors fail over
   an area. The result is packed back into the format's bits.
6. **Copies out** into the added swapchain images, as for the blend.

Three rules give exact output, as the blend's integer mix (a pixel that is the same in both
images keeps its bits):
- outside the video rectangle;
- **the static-pixel rule**: where the two images agree within 0.004 of full scale at a pixel
  and its four neighbours, except where the vector's two samples agree with each other and
  not with the pixel (something passes over it between the two images: a thin line, a small
  fast object), which the rule would erase. Without that exception the rule cost the drawn
  thin-line scene most of its score (worst areas 0.046 against 0.969);
- **after a cut**, where the frame is the image before it, as the plugin repeats the frame
  before a cut. The test runs on the GPU: d = mean |A − C| over the quarter-size luma, a cut
  when d > 0.30, or when d > 0.02 and d > 5 × the largest d of the last four pairs (at least
  0.003). That is the script's rule without its look-ahead and its MVTools check, which the
  layer does not have. The first pair of a run (start, pause, seek) has no pairs before it,
  so only the 0.30 limit applies there; with the relative rule on it, the first pair of
  every truth.py moment was taken for a cut.

The video rectangle and the source height come from the control block (new fields at
offsets 28–44, see `shm.cpp`) or, for tests, `BUTTEREYE_FRAMES_RECT` and
`BUTTEREYE_FRAMES_SRC_H`. A change of either rebuilds the engine. TRI mode is not available
(it needs the frame after the pair).

**Memory and set-up.** At 3440 × 1440 with the engine at 1920 × 1080: 149.5 MB of engine
buffers on top of the blend's 80.6 MB of images. Making the engine takes 195–201 ms (two
runs), inside the present that first asks for it, so the first k-path present after a new
swapchain is that much late.

**frames-test** (`mvgpu/tools/frames_test.cpp`, built with the layer) runs the same blend
and mc code on its own device, without a window: PPM frames in, each packed into an image
of the swapchain format and passed through in order as consecutive presents, the
in-between frames read back out. `mvgpu/tools/frames_truth.py` plugs it into truth.py as
`--reference`, converting each scene's YUV to the RGB a 10-bit swapchain holds and back.

### Picture: the RGB path against mvgpu in VapourSynth

truth.py, the same moments and the same pair spacing (t = 1/2). "Layer" is frames-test
with the A2B10G10R10 format; "mvgpu" is the installed plugin in VapourSynth. One run each
(the sets were scored in four chunks for memory). The paired differences are layer minus
mvgpu over the same moments, mean ± standard error.

| set | mvgpu, worst / mean / PSNR | layer, worst / mean / PSNR | worst areas, paired | moments better / worse |
|---|---|---|---|---|
| 12 drawn scenes | 0.663 / 0.959 / 49.4 | 0.663 / 0.959 / 48.6 | −0.0005 ± 0.0007 | 6 / 5 |
| 169 real moments | 0.1742 / 0.9545 / 36.79 | 0.1743 / 0.9521 / 36.61 | +0.0001 ± 0.0008 | 78 / 84 |
| 99 seven-frame moments (`--around`) | 0.2459 / 0.9698 / 37.15 | 0.2528 / 0.9669 / 36.98 | +0.0070 ± 0.0036 | 54 / 44 |

- **Worst areas are the same as the plugin's.** Mean SSIM is lower by 0.002–0.003 and PSNR by
  0.17–0.18 dB on both real sets.
- **The RGB round trip alone** (the layer's integer blend through frames-test against
  truth.py's own blend in YUV, 99 set) costs 0.06 dB of PSNR and nothing in worst areas
  (−0.1564 against −0.1571), so most of the PSNR difference is the path itself (RGB luma
  instead of the source's Y, one field for all channels), not the conversion.
- **Drawn scenes:** within 0.004 of the plugin on 11 of 12 (6 the same to the third
  decimal). The thin line scores 0.962 against 0.969 (PSNR 55.7 against 65.0), the static
  rule's remaining cost: with the rule off it scores 0.969 / 67.2.
- **The other paths**, paired against the layer's own SDR run on the same moments (99 set unless said):

  | variant | worst areas | change against the base |
  |---|---|---|
  | HDR10 (PQ): frames in PQ as mpv's swapchain holds SDR video, luma brought back to SDR scale | 0.2499 | −0.0029 ± 0.0024 |
  | the same PQ path on the 169 moments (against the SDR path there) | 0.1777 | +0.0033 ± 0.0018 |
  | display at 1440 lines: frames scaled up, engine at 1080, rendered at 1440, scored scaled back down | 0.2520 | −0.0008 ± 0.0036 |
  | static-pixel rule off | 0.2528 | +0.0000 (6 moments change) |

### Thresholds (S4d)

Because ingest puts luma on the plugin's scale (video range, SDR-like on PQ), the plugin's
thresholds keep their meaning, and the retune on the 99 set found nothing worth a change:

| setting | worst areas | change against the defaults |
|---|---|---|
| defaults (occlusion 0.12–0.36, failure 0.05–0.15, still 0.004) | 0.2528 | – |
| occlusion 0.08–0.24 | 0.2512 | −0.0016 ± 0.0003 |
| occlusion 0.18–0.54 | 0.2535 | +0.0007 ± 0.0002 |
| failure 0.035–0.105 | 0.2491 | −0.0037 ± 0.0017 |
| failure 0.075–0.225 | 0.2531 | +0.0003 ± 0.0013 |
| still 0.002 / 0.008 | 0.2528 / 0.2528 | −0.0000 / −0.0000 |

The layer keeps the plugin's values. Wider occlusion limits gain 0.0007, which is below
anything visible and would part the layer from the plugin. The cut limits were not swept:
the truth sets contain no cuts (truth.py drops moments across one). The PQ path was scored
with the same defaults.

### GPU time at 3440 × 1440

From the layer's timestamps around each stage, in mpv on DP-1 (A2B10G10R10, HDR10), k = 3,
the video rectangle 2560 × 1440, the engine at 1920 × 1080. Each mpv frame is one submit
holding all of it, so the total is also the largest single submit.

| load | runs | per mpv frame p50 / p99 / max (ms) | vectors + cut p50 | two in-between frames p50 | ingest | copies in + out |
|---|---|---|---|---|---|---|
| 48 fps clip, no other GPU work | 3 (12–30 s) | 2.10–2.11 / 2.47–2.58 / 2.59–2.88 | 1.26 | 0.68 | 0.10 | 0.06 |
| RIFE 24 → 48 in VapourSynth at the same time | 3 × 60 s | 2.19–2.20 / 7.17–7.29 / 9.4–14.0 | 1.32 | 0.71 | 0.10 | 0.07 |

- **Over the 2 ms per submit the design aimed for**, at the median already (99.7–100 % of
  submits in every run). The submit was not split.
- **Under RIFE's load** the same work takes about 3 times as long at the 99th percentile:
  our dispatches wait behind RIFE's TensorRT work on the same GPU. In these runs that cost no
  refresh (below), because the hold keeps two presents queued ahead of the screen.
- **Against today's cascade:** the layer spends about 2.2 ms × 48 = 105 ms of GPU time a
  second. Not measured like for like: the plugin in S4b's `MVGPU_PROFILE` took about 2.2 ms
  per render at 1080p (wall time of one in-between frame, its uploads and readback
  included), and the cascade makes 96 of them a second at 48 → 144; mpv also draws 144
  frames a second there against 48 with the layer.

### Real-window runs: today's cascade against RIFE + the layer

A 1080p 24 fps film (40 s of the public clip at 1920 × 1080, played twice in one 80 s file with a test tone), DP-1 fullscreen at 144 Hz,
ButterEye's profile, 60 s windows after 9 s of warm-up, 3 runs each, alternating, round 2
reversed. Both arms run RIFE v4.26 at 1080p through TensorRT, 24 → 48, from ButterEye's own
script generator:

- **cascade:** then mvgpu 48 → 144 in VapourSynth, mpv at 144 fps, no layer;
- **layer:** mpv at 48 fps (`display-fps-override=48`), the layer at k = 3 in mc mode,
  `audio-delay=0.0132` for the layer's +13.2 ms (S2).

| arm | dropped | late (`vo-delayed`) | mistimed | layer: missed refreshes | layer: app presents on the k path | A/V (`avsync`) range, ms |
|---|---|---|---|---|---|---|
| cascade | 0, 0, 0 | 1, 0, 1 | 0, 0, 0 | – | – | −1.0..12.6, −0.5..5.0, −2.0..6.4 |
| layer, mc | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 (every one of 8642–8643 screen intervals was one refresh) | 2881 of 2881 each | −1.8..−0.5, −1.0..8.3, −1.1..9.4 |

Both arms are clean in these runs; the machine was quiet enough that neither shows a
difference. The layer's image reaches the screen 33.8 ms after mpv's present (p50, all
three runs), as in S2 and S3. `avsync` is mpv's own figure with the audio delay applied; the
offset at the screen and the speakers was not measured.

### The moving bar

The 48 fps bar clip (24 px a frame), mc at k = 3 with the marker, 30 s, 25 grim screenshots
of DP-1: **25 of 25 show one sharp bar** (22–23 px wide, peak 255) at the right place: on
unmarked shots (mpv's image) at whole frame positions, on red ones (m = 1) a third of a
frame on, on blue ones (m = 2) two thirds on, each within 0.001 of a frame (8 unmarked,
9 red, 8 blue). Taking the screenshots cost that run 25 missed refreshes and 13 drops; a
15 s run without screenshots missed 2 refreshes and dropped nothing.

### Validation

- **lavapipe** (`tests/integration/test_frames_layer_mc.py`), with the validation layer and
  synchronisation checks: the layer in mc mode (40 presents, a pause, the dump) and
  frames-test both report nothing. This found that the output array was viewed as a plain
  2D image when k = 2 leaves it one layer; it is now always an array view (the blend at
  `BUTTEREYE_FRAMES_KMAX=2` had the same fault).
- **RTX 4090:** 15 s of RIFE + the layer in mc mode with the validation layer below ours:
  the validation log was created and is empty.

### Tests

`tests/integration/test_frames_layer_mc.py`, 6 tests on lavapipe (no GPU): a textured square
moving over a textured background lands half-way (error under a third of the blend's,
overall and on the square's rows), unchanged pixels keep their bits, a cut gives the frame
before it (natural and forced), the video rectangle with a letterbox and the scale-down to
source size, the PQ path, the layer sending mc frames with the control block's rectangle,
and validation. With the S1–S4b tests: 32 passed.

The plugin's parity gates after the `record_pyramid` split: the frame hashes of S4b (10
engine settings × 214 frames) give 0 of 2140 different from the installed plugin;
`test_mvgpu_real.py` 3 passed; truth.py `--mvgpu` on the drawn scenes is identical in every
field. The 169-moment set was not rerun for the plugin.

## S4 follow-ups: submit size, queues, set-up time and a cut counter

### What changed

- **Split submits** (the default; `BUTTEREYE_FRAMES_SPLIT=0` gives the S4c single submit).
  One `vkQueueSubmit` call with one batch per part, on the app's present queue:
  - the pair's batch: the copy into H, ingest, pyramid, vectors and the cut test. It waits on
    mpv's semaphores and signals the semaphore that mpv's own present waits on;
  - one batch per in-between frame m: select, render into Out's layer m − 1, and the copy
    into the added swapchain image. It waits only on that image's acquire semaphore and
    signals that image's present semaphore. So the first added present no longer waits for
    the second frame's work.
  - Submission order and a barrier at the start of each frame's batch order the work. The
    first image of a run (no pair yet) stays one batch with the plain copies.
  - `mc_record_pair` and `mc_record_frame` record the parts; `mc_record` still records all of it
    into one command buffer for `frames-test`, which gives the same bytes either way.
- **Queue global priority** (`BUTTEREYE_FRAMES_PRIORITY=low|medium|high|realtime`, off by
  default). The layer adds `VK_KHR_global_priority` (or the EXT one) and a
  `VkDeviceQueueGlobalPriorityCreateInfoKHR` on the queue the work runs on. On
  `VK_ERROR_NOT_PERMITTED` it makes the device again without it.
- **A queue of its own** (`BUTTEREYE_FRAMES_QUEUE=compute|graphics`, off by default):
  - the compute work (pair and frames) goes to that queue, and the copies in and out stay
    on the app's queue;
  - semaphores order the three steps: app queue (copy in) → own queue (pair, frames) → app
    queue (copies out);
  - the blend's images are made `CONCURRENT` between the two families, so no ownership
    transfers are needed;
  - mpv makes every queue of every family, with its default `--vulkan-queue-count=1` and
    submits only to the first, so the layer takes the family's last queue (index 7 of the
    compute family, 15 of the graphics family). That rests on mpv's default, which is
    one more reason it stays off.
- **Set-up off the present path:**
  - The engine is made on a worker thread when a present first asks for mc, and again
    when the rectangle or the source height changes. The added presents stay blends (or
    the old engine's frames) until it is ready; `BUTTEREYE_FRAMES_MC_THREAD=0` makes it
    inside the present as before.
  - The k path's fences and semaphores are made on a thread started at swapchain
    creation. On this driver each fence took about a millisecond, and the 24 of them were
    most of the remaining set-up time.
  - The blend's images and pipeline are made on that thread too, when the swapchain starts
    in blend or mc mode and there is no own queue.
- **Cut counter.** After the cut test, its d and flag are copied to a host-visible buffer per
  slot and read once the slot's fence is done. Control block offset 152 (u64) counts the
  pairs the test called a cut. The summary line gives cuts and tested pairs, and the M rows
  gain the per-submit times, the largest submit, the cut flag and d.

### GPU time and pacing with RIFE 24 → 48 in front

The S4c set-up: 1080p 24 fps film (80 s file), RIFE v4.26 TensorRT 24 → 48 in VapourSynth,
the layer at k = 3 in mc mode, DP-1 at 144 Hz (HDR10 swapchain, rectangle 2560 × 1440, engine
1920 × 1080). There were 3 runs of 60 s per arm, alternating, with round 2 reversed. "S4c" is the frozen S4c–e
layer build. GPU times are per mpv frame, in ms.

| arm | dropped | late | mistimed | missed refreshes | GPU total p50 / p99 / max | largest submit p50 / p99 / max | submits over 2 ms |
|---|---|---|---|---|---|---|---|
| S4c (one submit) | 0, 0, 0 | 0, 0, 0 | 0, 0, 2 | 0, 0, 1 | 2.19–2.21 / 7.15–7.28 / 9.9–12.2 | the same | 99.6–99.8 % |
| split | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 | 2.20–2.21 / 7.22–7.32 / 9.5–13.7 | 1.44–1.45 / 4.48–5.02 / 8.9–10.8 | 9.8–10.1 % |
| split, own async-compute queue | 0, 0, 0 | 0, 0, 0 | 1, 0, 0 | 1, 0, 0 | 2.30–2.32 / 9.31–9.32 / 12.4–13.5 | 1.55–1.56 / 6.56–6.73 / 9.2–10.6 | 14.2–15.1 % |

- **Split:** the largest submit is the pair's: 1.44 ms at the median, about 2.0 ms at the
  90th percentile. Each frame's submit is 0.37 ms at the median and 0.53–0.55 ms at the
  99th percentile. The total GPU time per mpv frame does not change.
- **Async compute:**
  - Every time is larger, at the median and in the tail (the cross-queue semaphores add
    gaps, and the compute queue's work is no faster).
  - The timestamps of this arm come from two queues; the driver uses one timer for both.
  - **Not kept.**
- **Global priority:** `high` and `realtime` both return `VK_ERROR_NOT_PERMITTED` for mpv
  run by an ordinary user (with the app's queue and with the compute queue). The device
  is made again without it. There was nothing to measure. **Not kept on**; the knob stays,
  off.
- One more 60 s run with the final build (the set-up changes below): 0 dropped, late,
  mistimed or missed; largest submit 1.43 / 4.45 / 8.79 ms.
- The cut counter read 24 cuts in each 60 s window on this film (the same 24 in all six
  runs of the new build).

### Under the S2 burst load

S2's synthetic load: 18 ms compute bursts (p50 18.0, p90 19.0–19.5 ms), 24 ms pauses, in another
process. The clip is the 1080p 48 fps film (no RIFE), mc at k = 3, with 3 runs of 60 s per arm in the same
alternating order.

| arm | dropped | late | mistimed | missed refreshes | GPU total p99 / max | largest submit p50 / p99 | submits over 2 ms |
|---|---|---|---|---|---|---|---|
| S4c (one submit) | 2, 7, 5 | 3, 9, 9 | 2, 10, 5 | 10, 22, 22 | 9.0–9.6 / 25.8–27.5 | 2.14–2.17 / 9.0–9.6 | 94.5–96.0 % |
| split | 2, 4, 2 | 6, 12, 7 | 3, 4, 2 | 16, 27, 13 | 9.4–9.5 / 19.2–26.2 | 1.40–1.41 / 7.9–8.6 | 28.9–30.2 % |
| own async-compute queue | 4, 2, 0 | 9, 5, 1 | 4, 3, 0 | 19, 15, 4 | 11.2–13.3 / 25.1–28.5 | 1.57–1.60 / 7.6–9.0 | 38.7–43.2 % |

- Under bursts every arm misses 0.1–0.3 % of refreshes, and the run-to-run spread is as large
  as any difference between arms. The split does not change pacing here, neither better nor
  worse.
- S2's copies-only k = 3 under the same load missed 2.0–2.4 %. Those runs were on another day
  with another machine load, so this is not a like-for-like comparison.

### Without other GPU work

20 s runs on the 48 fps clip, 2 per arm:

| arm | GPU total p50 / p99 / max | largest submit p50 / p90 / p99 | submits over 2 ms |
|---|---|---|---|
| S4c (one submit) | 2.13–2.14 / 3.99 / 8.9–9.0 | the same | 100 % |
| split | 2.13–2.14 / 3.88–3.89 / 8.8–9.2 | 1.39–1.40 / 2.02–2.04 / 3.07 | 10.8–11.2 % |
| own async-compute queue | 2.23 / 4.10 / 8.8–9.1 | 1.49 / 2.13–2.15 / 3.18–3.29 | 13.2–13.4 % |

The pair's submit is still over 2 ms in about one present in ten. Most of it is the engine's
vectors (1.25 ms at the median), which are recorded by one engine call. Splitting that call
needs a change in the engine, shared with the plugin, and was not done.

### Set-up time

S4c made the fences, the blend and the engine inside the first k path present of each
swapchain. Its own time (the layer's time in that present, waits left out), from the batch
above:

| build | first k path present, fullscreen swapchain (ms) | first k path present, the first (windowed) swapchain (ms) |
|---|---|---|
| S4c | 21.5, 23.0, 23.8, 24.1, 31.2, 54.2, 62.4, 81.7 (8 runs) | 37.7–44.9 (8 runs) |
| now | 0.1, 5.8, 9.4 (3 runs) | 6.1, 6.6, 26.0 (3 runs) |

- In these runs the engine took 8–9 ms to make inside the present, against S4c's 98–195 ms
  then. The driver's shader cache was warm, so the ~0.2 s did not show today.
- On the worker thread it takes 10–50 ms; mc starts that much later, and presents go on
  meanwhile.
- What is left in the first present is its first record and submit: 0–9 ms, once 26 ms.

### Validation

- **lavapipe**, with synchronisation checks: the layer in mc mode with split submits and
  with one submit both report nothing (40 presents, a pause, the dump).
- **RTX 4090**, mpv on DP-1 with the validation layer below ours and synchronisation checks
  on, 10 s each: split submits, the own compute queue and the own graphics queue (all with
  an earlier build), then split and the compute queue again with the final build. Every
  validation log is empty.

### Tests

`tests/integration/test_frames_layer_mc.py` now has 11 tests, all on lavapipe:

- validation with split submits and with one submit;
- split submits give the same bytes as one submit (the dump of one present);
- the engine made on the worker thread: blends while it is made, then in-between frames, and
  no present waits for it;
- the cut counter, with forced cuts and with the drawn ramp's single wrap. It checks the
  summary, the M rows and control block offsets 144 and 152.

The layer's earlier tests set `BUTTEREYE_FRAMES_MC_THREAD=0`, so short runs have frames from
their second app present on. Probe, engine and mc tests together: 37 passed.

The engine (`src/engine`) and the plugin's sources are unchanged in this stage, so the
plugin's parity gates were not rerun.

## What comes next

- **The pair's submit** is 1.4 ms at the median but over 2 ms in about one present in ten.
  Most of it is the engine's vectors in one call. Splitting by pyramid level needs an engine
  change (and the plugin's parity gates).
- **Bursts in other processes** still cost 0.1–0.3 % of refreshes at k = 3, whatever the
  layer's submit size. Higher queue priority would need a right ordinary users lack here.
  Sending one frame fewer after a missed refresh (S2's open point) is the next thing to try.
- **The first submit** of a swapchain still takes 0–9 ms (once 26 ms) in the driver.
- **ButterEye reading the cut counter** (offset 152), for S5.
- **Writing straight into the swapchain images** (saves the 0.05 ms of copies out), as S3
  noted; small next to the engine's 2 ms.
- **S5:** the control block writer (k, mode, the video rectangle from `osd-dimensions`, the
  source height), the runtime `display-fps-override`, and `audio-delay` from the measured
  delay.

## Not done yet

The ButterEye integration (S5) and packaging. Not measured: other screens and rates, VRR, windowed play over a long time,
OUT_OF_DATE during play, sources other than 1080p (720p and 4K on 1440 lines), the offset of
picture and sound at the screen, `R16G16B16A16_SFLOAT` swapchains (they keep copies).
