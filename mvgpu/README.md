<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# mvgpu — GPU block motion-vector interpolation for VapourSynth (Vulkan)

ButterEye's own frame-interpolation engine for what RIFE can't do in real time (4K at
60/120 fps). Spike M0(r) prototyped it in CUDA (`docs/spikes/m0r.md`); this is the
cross-vendor Vulkan plugin.

```python
core.std.LoadPlugin("libmvgpu.so")
smooth = core.mvgpu.Interpolate(clip, fps_num=120, fps_den=1[, device=0])
```

- In: constant YUV 4:2:0, 8–16-bit integer, constant frame rate. Out: the same format at
  `fps_num/fps_den`, the same duration.
- Per source pair: the frames are uploaded once and get a luma pyramid; a symmetric block
  search centred on the in-between frame (8×8 blocks, ±8 at 1/8 size, ±1 refinement of the
  block's and its neighbours' doubled vectors at every finer level, 3×3 vector median),
  then a 4×4-block refinement at full size.
- At full size the search works in whole pixels of *motion* (A → C) rather than of the
  half-vector, so 1, 3 or 5 px a frame (slow pans, credit rolls) is matched instead of being
  rounded to an even step; a last pass refines it to ½ px (`subpel.comp`), and the render
  samples Catmull-Rom so in-between frames stay as sharp as the source frames.
- The full-size medians are checked against the picture: the median vector (one of the 9)
  replaces a block's own only where it matches about as well, so the one column of blocks
  that sees a flat object's edge or a thin line keeps its vector instead of being outvoted
  by the flat background around it.
- Per in-between frame: **per-pixel vector selection** — among the 3×3 neighbouring
  blocks' vectors, the one under which the two warped frames agree best (bilinear blending
  of block vectors mixed a moving object's vector with the background's and ghosted both);
  then the render. Where even the best vector leaves the frames disagreeing (occlusion), it
  takes the frame that has the content instead of averaging two different things: A where
  the vectors converge (something is being covered), C where they spread (uncovered), else
  the nearer frame.
- Source positions are the source frames, untouched. A frame with `_SceneChangeNext` set
  (ButterEye's scripts mark them) is repeated instead of interpolated.
- `MVGPU_PROFILE=1` prints the mean CPU/GPU time per upload and per render.
- For comparisons: `MVGPU_SELECT=0` (bilinear block vectors), `MVGPU_FINE=0` (no 4×4
  level), `MVGPU_SUBPEL=0` (no ½ px refinement), `MVGPU_MEDIAN=0` (plain component median at
  full size too), `MVGPU_OCC=lo,hi` (the mismatch ramp, default `0.04,0.12`; `9,10` turns it
  off).

## Build

Needs `vulkan-headers`, `vulkan-loader-devel`, `vapoursynth-devel`, `meson` (1.1 or newer:
the options live in `meson.options`), `gcc-c++` and a GLSL → SPIR-V compiler (`glslang` from
Fedora, or any `glslang`/`glslangValidator`):

```sh
meson setup build [-Dglslang=/path/to/glslang] [-Dplugindir=...]
ninja -C build            # build/libmvgpu.so
```

On arm64 macOS the same two lines build `build/libmvgpu.dylib` natively, with no source
changes and no cross toolchain: `brew install meson ninja pkgconf glslang vulkan-loader
molten-vk vapoursynth` is the whole dependency list (`PATH=/opt/homebrew/bin:$PATH` so meson
finds Homebrew's `pkg-config` and `glslang`). Nothing here compiles a `.metal` file, so a full
Xcode install is not needed — MoltenVK translates the SPIR-V at run time.

The version (`meson.build`, now 0.0.1) is also what the plugin reports to VapourSynth:
0.0.1 as (0, 1), x.y.z as (x, 100·y + z). `plugindir` (default
`lib64/buttereye/vapoursynth`) is where `meson install` puts `libmvgpu.so`.

Errors are clean VapourSynth errors, never a hang: the GPU must allow 512-invocation compute
groups and a storage buffer of 16 bytes a pixel ("GPU limit: …" otherwise); a failed render
gives its readback buffer back, and after a lost device every later frame fails at once.

## Package and use in ButterEye

The COPR package is **`buttereye-vs-mvgpu`** (`packaging/plugins/buttereye-vs-mvgpu.spec`,
SCOPE §9 package 8), built by `packaging/build.sh` from this directory with
`-Dplugindir=%{_lib}/buttereye/vapoursynth`, so it lands in ButterEye's private plugin
directory and is never autoloaded:

```sh
sudo dnf install buttereye-vs-mvgpu
```

`buttereye` recommends it. When `libmvgpu.so` is there, ButterEye uses it as the live engine
`BackendId.MVGPU` ("GPU motion vectors"): Automatic takes it after RIFE where RIFE can't keep
up at full size or can't reach the display rate (SCOPE §5.3, GUI.md §11.9), the speed test
measures it, and the simple window offers "Fastest — GPU (motion vectors)".

## macOS: Vulkan through MoltenVK

macOS has no native Vulkan. MoltenVK implements it on Metal and registers itself as a
**portability driver** (its ICD JSON says `"is_portability_driver": true`), and the Vulkan
loader hides such a driver from an application that has not asked to see it: `vkCreateInstance`
returns `VK_ERROR_INCOMPATIBLE_DRIVER` (-9) and nothing else happens. So `Gpu::init` enables
`VK_KHR_portability_enumeration` and sets `VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR`.

It asks only after the plain call has been refused. `Gpu::init` creates the instance exactly
as it always did — no extensions, flags 0 — and only when that returns
`VK_ERROR_INCOMPATIBLE_DRIVER`, **and** the loader advertises the extension, does it set the
two fields and try once more. So on every machine with a native driver the first call succeeds
and the `VkInstanceCreateInfo` is byte for byte the one Linux and Windows have always passed;
where no driver at all answers, the retry fails the same way and the error text is unchanged
(measured, with a deliberately broken ICD as the only driver).

The trigger is the refusal rather than the platform or the extension's presence, for two
separate reasons. Not `#ifdef __APPLE__`, because being a portability driver is a property of
the ICDs installed on the machine rather than of the operating system — translation layers
exist on Linux and Windows too — and a platform macro would be a lie about what was detected.
Not the extension list on its own, because the loader turns out to advertise
`VK_KHR_portability_enumeration` **unconditionally** (measured on loader 1.4.363: it is listed
with no portability driver installed, and even when the only ICD is a broken one, because the
loader rather than a driver implements it). Asking on that basis would therefore set the flag
on Linux too, where a portability driver installed beside the real one would start appearing in
the device list and shift the indices `device=` selects. The list is still checked before the
retry, because enabling an extension the loader does not advertise fails with
`VK_ERROR_EXTENSION_NOT_PRESENT` — which matters for loaders older than 1.3.212, where the
extension did not exist.

Device choice is unchanged, and on an Apple Silicon Mac it is the second test that fires:
MoltenVK reports the GPU as `INTEGRATED`, never `DISCRETE`, so "the first discrete GPU, else
the first non-CPU device" picks it on the second clause (measured with an instrumented build:
one device, `Apple M2`, `deviceType` 1, chosen by the non-CPU clause — the null guard after the
loop never runs). A discrete card still wins where there is one.

## Measured (Apple M2, MoltenVK 1.4.2, 2026-10-12)

VapourSynth R81 from Homebrew, Apple clang 21, Vulkan loader 1.4.363. `libmvgpu.dylib` is an
arm64 Mach-O bundle that exports `_VapourSynthPluginInit2` and links only
`libvulkan.1.dylib`, `libc++.1.dylib` and `libSystem.B.dylib`.

The correctness check is the Linux one (a textured 96×96 square moving 16 px a frame, 640×368,
24 → 48): the in-between frame is **bit-exact** with the square drawn at the half-way position
— mean absolute error 0.0 and maximum 0.0 over the square's rows, against 8.43 for a blend of
the two source frames and 9.72 against either source frame alone. Source positions come
through untouched and a `_SceneChangeNext` frame is repeated, as on Linux.

| synthetic, `vspipe` to /dev/null | out fps |
|---|---|
| 720p 8-bit, 24 → 120 | 287 |
| 1080p 8-bit, 24 → 60 | 112 |
| 1080p 8-bit, 24 → 120 | 137 |
| 4K 8-bit, 24 → 60 | 30 |
| 4K 8-bit, 24 → 120 | 36 |
| 4K 10-bit, 24 → 120 | 34 |

Warm figures (the first run after a cold GPU is about half as fast). A higher output rate is
faster per frame because one block search is amortised over more in-between frames. The shape
matches the RTX 4090 table below, about 14× slower: at 4K this M2 is far short of live
playback, and 1080p → 120 (137 fps) is the largest live case with headroom.
`MVGPU_PROFILE=1` at 4K: 0.87 ms CPU + 3.35 ms GPU per load, 35.14 ms GPU + 0.75 ms CPU per
render — GPU-bound, not readback-bound.

The limits are not tight: `maxComputeWorkGroupInvocations` 1024 against the 512 the coarse
search needs, `maxStorageBufferRange` 4294967295 against the 16 bytes a pixel the vector field
wants (127 MiB at 4K). All seven shaders translate from SPIR-V to MSL and compile with no
MoltenVK warning: with `MVK_CONFIG_DEBUG=1 MVK_CONFIG_LOG_LEVEL=4` a full render logs 8
SPIR-V → MSL conversions and 10 Metal shader compilations and not one `[mvk-warn]` or
`[mvk-error]` line.

## Measured (RTX 4090, 2026-10-10)

| 4K, vspipe | out fps |
|---|---|
| synthetic 10-bit, 24 → 120 | 253 |
| synthetic 8-bit, 24 → 120 | 502 |
| real VP9 video (decode included), 24 → 60 | 268 |
| real VP9 video (decode included), 24 → 120 | 378 |

Quality (`tools/perf/mvgpu_quality.py`, M0(q) triples): as good as RIFE on pans and anime.
On a fast swinging sleeve, the first version drew a translucent double (ghost); selection,
the occlusion fallback and 4×4 vectors make it one solid shape with slightly broken edges
(PSNR about 0.2 dB lower there: averaging scores better than a sharp shape in a slightly
wrong place, but ghosts are what the eye notices). 4K 10-bit 24 → 120: 370 fps synthetic.

The odd-motion search, ½ px refinement and checked median (2026-10-15) came after these
tables. `MVGPU_PROFILE=1`, 1080p 2x, GPU ms per render before → after: a still frame
0.79 → 0.82, a uniform pan 0.78 → 0.87, two real clips scaled to 1080p 0.84 → 1.07 and
0.83 → 0.99, random noise 0.85 → 1.15. Real content costs about 20–35 % more, not the
10 % a uniform pan shows: neighbouring blocks with different vectors each get their own
search window. At 1080p on this card the CPU is the limit, so the frame rate barely moves;
on smaller GPUs, at 4K or at high multipliers it will.

Test: `tests/integration/test_mvgpu_real.py` (a textured square moving 16 px per frame, the
reported version, clean construction errors), `tests/integration/test_mvgpu_live_real.py`
(live play in mpv and the speed-test candidate) and `tests/test_mvgpu_live.py` (the live
decisions, without a GPU).
