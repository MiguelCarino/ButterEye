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
- Per in-between frame: **per-pixel vector selection** — among the 3×3 neighbouring
  blocks' vectors, the one under which the two warped frames agree best (bilinear blending
  of block vectors mixed a moving object's vector with the background's and ghosted both);
  then the render. Where even the best vector leaves the frames disagreeing (occlusion), it
  takes the frame nearer in time instead of averaging two different things.
- Source positions are the source frames, untouched. A frame with `_SceneChangeNext` set
  (ButterEye's scripts mark them) is repeated instead of interpolated.
- `MVGPU_PROFILE=1` prints the mean CPU/GPU time per upload and per render.
- For comparisons: `MVGPU_SELECT=0` (bilinear block vectors), `MVGPU_FINE=0` (no 4×4
  level), `MVGPU_OCC=lo,hi` (the mismatch ramp, default `0.04,0.12`; `9,10` turns it off).

## Build

Needs `vulkan-headers`, `vulkan-loader-devel`, `vapoursynth-devel`, `meson`, `gcc-c++` and a
GLSL → SPIR-V compiler (`glslang` from Fedora, or any `glslang`/`glslangValidator`):

```sh
meson setup build [-Dglslang=/path/to/glslang] [-Dplugindir=...]
ninja -C build            # build/libmvgpu.so
```

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

Test: `tests/integration/test_mvgpu_real.py` (a textured square moving 16 px per frame, the
reported version, clean construction errors), `tests/integration/test_mvgpu_live_real.py`
(live play in mpv and the speed-test candidate) and `tests/test_mvgpu_live.py` (the live
decisions, without a GPU).
