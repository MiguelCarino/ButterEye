<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (C) 2026 The ButterEye contributors -->
# Spike: buttereye-vs-rife-ncnn on arm64 macOS (phase 3)

Date: 2026-10-12. Machine: macOS 27.0.1 (Darwin 27.0.0) on an Apple M2 (arm64), Homebrew prefix
`/opt/homebrew`, Apple clang 21.0.0 against SDK 27.0 (Command Line Tools only, no
Xcode and no `metal` compiler), meson 1.12.1, ninja 1.13.2, cmake 4.4.4,
pkgconf 3.0.8, VapourSynth R81 (API 4.1) under Homebrew's python3.14,
MoltenVK 1.4.2, vulkan-loader 1.4.363.0, ncnn 20260526_6, glslang 16.6.0,
libomp 23.1.3. Plugin source: `styler00dollar/VapourSynth-RIFE-ncnn-Vulkan` tag
`r9_mod_v33`, commit `c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f` — the commit
`packaging/plugins/repack-rife-ncnn.sh` pins.

## The question

Can `buttereye-vs-rife-ncnn` be built for arm64 macOS and render a **correct**
frame through MoltenVK? Phase 3's precedent is the Windows cross-build; the
worry is that macOS has no native Vulkan, and that RIFE's own model loader has
already produced silently black frames once on a new platform.

## Verdict

**Works.** Both variants of the package build natively on arm64 with the patches
ButterEye already carries, and both render a correct in-between frame through
MoltenVK on the first try. Nothing about MoltenVK blocks RIFE: unlike `mvgpu`,
the ncnn the plugin uses already enables `VK_KHR_portability_enumeration`, so the
Vulkan loader does not hide the driver.

| Question | Answer |
|---|---|
| Builds on arm64 against Homebrew's ncnn 20260526 | **yes**, with the two patches the Fedora `--with system_ncnn` path already applies |
| Builds on arm64 with the bundled pinned ncnn 305837fd + glslang a9ac7d5f, static | **yes**, 466 ninja targets, no source change beyond the host-pool patch |
| Loads into Homebrew's VapourSynth R81 | **yes**, namespace `rife`, `com.holywu.rife` |
| Finds the GPU through MoltenVK | **yes**, `[0 Apple M2]` |
| Renders a correct in-between frame (not black) | **yes**, mean absolute error 0.0045 against the truth where a blend scores 0.0331 |
| Fast enough for live mpv playback at 1080p | **no.** 7–10 fps against the ≈48 fps 2× 24p needs |
| Fast enough for offline render | yes — slow but correct: 96 output frames at 1080p in 10–13 s, and the render estimates already scale from a measured benchmark |
| Needs new ButterEye patches | **one**, and it is a hard crash: see "The model-path abort" |
| Needs new build flags | **two**, the OpenMP link flags: see "The OpenMP time bomb" |

Three findings are product decisions rather than build problems: `gpu_thread`
must be 1 on Apple Silicon, live playback is out of reach at 1080p, and
Homebrew's ncnn is **not** the same product as the pinned one (21.3 % of output
samples differ).

## How to repeat it

Everything happens outside the repository.

```sh
work=$(mktemp -d)                   # e.g. $TMPDIR/rife-spike
repo=$HOME/Github/ButterEye         # your checkout
export PATH=/opt/homebrew/bin:$PATH

# 1. the plugin source, at the commit repack-rife-ncnn.sh pins, without
#    models/ and without the subprojects/ncnn submodule
git clone --quiet --filter=blob:none --no-checkout --depth 1 \
    --branch r9_mod_v33 \
    https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan.git "$work/src"
git -C "$work/src" rev-parse HEAD   # must be c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f
git -C "$work/src" sparse-checkout set --no-cone '/*' '!/models/' '!/subprojects/' \
    '/models/rife-v4.26_ensembleFalse/' '/models/rife-v4.22_lite_ensembleFalse/'
git -C "$work/src" checkout --quiet

# 2. the two seds the spec's %prep already does, and the two patches
cp -a "$work/src" "$work/build-tree" && rm -rf "$work/build-tree/.git"
cd "$work/build-tree"
sed -i '' "s|vapoursynth_dep.get_variable(pkgconfig: 'libdir') / 'vapoursynth'|get_option('libdir') / 'buttereye' / 'vapoursynth'|" meson.build
sed -i '' "/^install_subdir('models',/,/^)/d" meson.build
patch -p1 < "$repo/packaging/plugins/buttereye-vs-rife-ncnn-no-pack8.patch"
patch -p1 < "$repo/packaging/plugins/buttereye-vs-rife-ncnn-host-pool.patch"

# 3. build against Homebrew's ncnn
meson setup "$work/build-sys" -Duse_system_ncnn=true
ninja -C "$work/build-sys"
```

The bundled variant replaces step 3 with the pinned sources, fetched and
checksum-verified the way `packaging/windows/pins.sh` does it (macOS has
`shasum -a 256`, not `sha256sum`), and does **not** apply the no-pack8 patch —
the pinned ncnn still has pack8:

```sh
fetch() { local f=${1##*/}; [[ -f $f ]] || curl -fsSL --retry 3 -o "$f" "$1"; echo "$2  $f" | shasum -a 256 -c; }
cd "$work/dl"
fetch https://github.com/Tencent/ncnn/archive/305837fd4a722ebc47c5d72e72d8ec9ae970e932/ncnn-305837fd4a722ebc47c5d72e72d8ec9ae970e932.tar.gz \
      d0dd1cbea993381fe40657b379835a2c3bedccbed8db6c79ffb8d222b25102db
fetch https://github.com/nihui/glslang/archive/a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc/glslang-a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc.tar.gz \
      0aa8276b27998fbf22f924279d72b5abd48c5802c234b57d5316567b547aa55f
mkdir -p "$work/build-tree-b/subprojects/ncnn/glslang"
tar -xzf ncnn-305837fd*.tar.gz    -C "$work/build-tree-b/subprojects/ncnn" --strip-components=1
tar -xzf glslang-a9ac7d5f*.tar.gz -C "$work/build-tree-b/subprojects/ncnn/glslang" --strip-components=1
meson setup "$work/build-bundled" -Duse_system_ncnn=false \
    -Dcpp_args="-I/opt/homebrew/opt/libomp/include" \
    -Dcpp_link_args="-L/opt/homebrew/opt/libomp/lib -lomp"
ninja -C "$work/build-bundled"
```

Both checksums verified:

```
ncnn-305837fd4a722ebc47c5d72e72d8ec9ae970e932.tar.gz: OK
glslang-a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc.tar.gz: OK
```

## What happened, in order

### 1. meson refuses to configure: Homebrew's `vapoursynth.pc` has no `libdir`

```
Run-time dependency vapoursynth found: YES 81
meson.build:34:32: ERROR: Could not get pkg-config variable and no default provided for <PkgConfigDependency vapoursynth: True ['>=55']>
```

Homebrew's file is three lines of `prefix`, `includedir` and `Cflags` — no
`libdir` and no `Libs`, because VapourSynth lives in a `libexec` virtualenv. **Not
a new problem:** `buttereye-vs-rife-ncnn.spec` already rewrites exactly that line
in `%prep`, to install into ButterEye's private plugin directory. The macOS build
needs the same sed, for a second reason.

### 2. `use_shader_pack8` is gone from Homebrew's ncnn too

```
../build-tree/RIFE/warp.cpp:62:13: error: no member named 'use_shader_pack8' in 'ncnn::Option'
   62 |     if (opt.use_shader_pack8)
      |         ~~~ ^
```

The same failure Fedora's ncnn 20250916 produced in M0(f), on ncnn 20260526.
`buttereye-vs-rife-ncnn-no-pack8.patch` applies cleanly and fixes it, and the
reasoning behind it still holds: Homebrew's `libncnn` has no pack8 Vulkan path, so
`elempack == 8` cannot reach the custom warp layer.

### 3. With those, it builds clean

`buttereye-vs-rife-ncnn-no-pack8.patch` and
`buttereye-vs-rife-ncnn-host-pool.patch` both apply with no fuzz; the
Windows-paths patch is `#if _WIN32` only and is not wanted here.

```
[1/4] Compiling C++ object librife.dylib.p/RIFE_warp.cpp.o
[2/4] Compiling C++ object librife.dylib.p/RIFE_plugin.cpp.o
[3/4] Compiling C++ object librife.dylib.p/RIFE_rife.cpp.o
[4/4] Linking target librife.dylib
```

Three warnings, none macOS-specific: an unused `opt` parameter that the no-pack8
patch leaves behind, an unused `queueCount` upstream already has, and two
`sprintf is deprecated` notices — which turn out to matter (below).

| Variant | Size | `file` | Links |
|---|---|---|---|
| Homebrew ncnn | 167 536 B | Mach-O 64-bit bundle arm64 | `libncnn.1.dylib`, `libc++.1.dylib`, `libSystem.B.dylib` |
| bundled pinned ncnn, static | 5 864 160 B | Mach-O 64-bit bundle arm64 | `libvulkan.1.dylib`, `libc++.1.dylib`, `libSystem.B.dylib` (+ `libomp.dylib` with the OpenMP flags) |

Both export the one symbol that matters:

```
$ nm -gU "$work/build-sys/librife.dylib" | grep -i vapoursynth
0000000000001368 T _VapourSynthPluginInit2
```

The two variants reach Vulkan by **different routes**, which is worth knowing
before `check-dylibs.sh` writes an allow-list:

- against Homebrew's ncnn, `libncnn` weak-links `libMoltenVK.dylib` directly and
  the Vulkan loader is not involved at all. It still tries a bare-name
  `dlopen("libvulkan.1.dylib")` first and fails noisily on stderr — the same
  bare-name class of bug as the phase-4 `libvapoursynth-script.dylib` one, here
  harmless because MoltenVK is already linked:
  ```
  dlopen failed dlopen(libvulkan.1.dylib, 0x0006): tried: 'libvulkan.1.dylib' (no such file), … '/usr/lib/libvulkan.1.dylib' (no such file, not in dyld cache), 'libvulkan.1.dylib' (no such file)
  ```
- the bundled variant links `libvulkan.1.dylib` from `vulkan-loader` (RIFE's own
  `dependency('vulkan')` supplies the symbols ncnn's simplevk wants) and goes
  loader → MoltenVK.

### 4. It finds the GPU through MoltenVK, and the portability dance is already there

```
$ python3.14 -c "import vapoursynth as vs; vs.core.std.LoadPlugin('$work/build-sys/librife.dylib'); \
    vs.core.rife.RIFE(vs.core.std.BlankClip(format=vs.RGBS,width=64,height=64,length=4), list_gpu=1).get_frame(0)"
[0 Apple M2]  queueC=0[1]  queueT=0[1]  rebar=1  r-score=10
[0 Apple M2]  fp16-p/s/u/a=1/1/1/1  int8-p/s/u/a=1/1/1/1  bf16-p/s=1/0
[0 Apple M2]  subgroup=32(4~32)  ops=1/1/1/1/1/1/1/1/1/1
[0 Apple M2]  fp16-cm=0  int8-cm=0  bf16-cm=0  fp8-cm=0
```

The three lines `mvgpu` is missing are in ncnn already. In the pinned
ncnn 305837fd, `src/gpu.cpp`:

```
2064:        else if (strcmp(exp.extensionName, "VK_KHR_portability_enumeration") == 0)
2065:            support_VK_KHR_portability_enumeration = exp.specVersion;
2092:    if (support_VK_KHR_portability_enumeration)
2093:        enabledExtensions.push_back("VK_KHR_portability_enumeration");
2172:    if (support_VK_KHR_portability_enumeration)                 # the CreateInfo flag
2744:    if (info.support_VK_KHR_portability_subset())               # and the device extension
```

which is why the bundled variant sees the driver the loader would otherwise hide
(`/opt/homebrew/etc/vulkan/icd.d/MoltenVK_icd.json` declares
`"is_portability_driver" : true`, and `/opt/homebrew/etc/vulkan/icd.d` is on the
loader's search path). No environment variable is needed: `VK_ICD_FILENAMES` and
`VK_DRIVER_FILES` were both unset for every run on this page.

**`queueC=0[1]`: one compute queue.** `gpu_thread` above 1 is refused with a
warning and buys nothing (see "Throughput"). This is a product decision for
ButterEye, whose default is `gpu_thread=4`.

### 5. Correctness: a textured square, where it really is

The Linux check is `tests/integration/test_mvgpu_real.py`: a 96 px textured
square moving 16 px per frame must come out shifted 8 px in the in-between
frame, and the error against that truth must be under 0.15 × the error a plain
blend of the two neighbours would score. This spike reproduces it for RIFE in
RGBS (the format the real script feeds it), reading pixels back through the frame
memoryviews — **there is no numpy on this machine** and this spike installed
nothing.

```
$ python3.14 "$work/rife_check.py" "$work/build-sys/librife.dylib" \
      "$work/src/models/rife-v4.26_ensembleFalse"
```

| Build / model | frames | fps | `is_black` | source frame kept | mid error | blend error | ratio |
|---|---|---|---|---|---|---|---|
| Homebrew ncnn / v4.26 | 12 | 48/1 | false | exact (err 0.0) | 0.004542 | 0.033087 | 0.137 |
| bundled ncnn / v4.26 | 12 | 48/1 | false | exact (err 0.0) | 0.004559 | 0.033087 | 0.138 |
| Homebrew ncnn / v4.22-lite | 12 | 48/1 | false | exact (err 0.0) | 0.004200 | 0.033087 | 0.127 |
| bundled ncnn / v4.22-lite | 12 | 48/1 | false | exact (err 0.0) | 0.004213 | 0.033087 | 0.127 |

All four pass the Linux threshold (ratio < 0.15). The in-between frame is a real
interpolation, not a blend and not a copy: its error against the shifted truth is
7× smaller than a blend's, and its distance *from* the blend (0.0336) is as large
as the blend's own error. The pixels are alive — `mid_min` 0.1270, `mid_max`
0.8154, `mid_mean` 0.1421 — so the Windows black-frame failure mode does not
occur here. Source positions pass through bit-exactly.

### 6. Throughput: offline yes, live no

`vspipe -p` on a synthetic 24 fps clip with real texture, 2× to 48 fps, 96 output
frames, `gpu_thread=1`, nothing else running:

| Resolution | Homebrew ncnn | bundled pinned ncnn |
|---|---|---|
| 1920×1080 | 8.5 fps | 9.8 fps |
| 1280×720 | 14.7 fps | 19.0 fps |
| 854×480 | 21.8 fps | 35.0 fps |
| 640×360 | 33.7 fps | 55.9 fps |

Real time for 2× 24p needs ≥ 48 fps, and SCOPE F2 asks for 1.15× headroom
(≥ 55). Only 640×360 on the bundled build reaches that, and only just. At 1080p
an M2 through MoltenVK is about **five times too slow for live playback** — for
comparison, M0(f) measured 56–58 fps on an RTX 4090. Offline render is
unaffected: it is the same engine, and the render estimates already scale from a
measured benchmark.

`gpu_thread` above 1 is pointless, as the single compute queue predicts
(1280×720, bundled build):

```
gt=1  Output 96 frames in 5.10 seconds (18.81 fps)
gt=2  Warning: gpu_thread is recommended to be between 1 and 1 (inclusive)  Output 96 frames in 4.88 seconds (19.66 fps)
gt=4  Warning: gpu_thread is recommended to be between 1 and 1 (inclusive)  Output 96 frames in 5.45 seconds (17.62 fps)
```

### 7. Stability: 8 of 8 clean

Eight 1080p runs back to back on the bundled build, no failures, no hangs, no
Metal fault, nothing lost:

```
run 1: Output 96 frames in 22.48 seconds (4.27 fps)
run 2: Output 96 frames in 15.85 seconds (6.06 fps)
run 3: Output 96 frames in 13.80 seconds (6.96 fps)
run 4: Output 96 frames in 12.97 seconds (7.40 fps)
run 5: Output 96 frames in 12.74 seconds (7.54 fps)
run 6: Output 96 frames in 12.66 seconds (7.58 fps)
run 7: Output 96 frames in 12.33 seconds (7.78 fps)
run 8: Output 96 frames in 12.42 seconds (7.73 fps)
clean=8 failed=0
```

The spread is machine load and thermals (builds were finishing during run 1), not
RIFE. Eight runs is a sanity check, not M0(f)'s 42-run soak; the GPU-fault
question that drove the bundled ncnn decision on NVIDIA has no measured answer
here, and macOS gives the doctor no fault history to read in any case (phase 2
already says so).

## The two things that need changing

### The OpenMP time bomb

`warp.cpp:110` has one `#pragma omp parallel for`, for the CPU warp path. The
plugin's own meson.build handles clang like this:

```meson
if meson.get_compiler('cpp').get_id() == 'clang'
  omp_dep = declare_dependency(
  compile_args : ['-Xpreprocessor', '-fopenmp']
  )
```

Compile args and **no link args**, and Apple clang does emit OpenMP calls from
that. The result links anyway, because meson's `shared_module` gets
`-undefined dynamic_lookup`, and then loads anyway, because the CPU warp path is
never taken while Vulkan works — so the symbols are never looked up. Force the
binding and the bundled build falls over:

```
$ python3.14 -c "import ctypes; ctypes.CDLL('$work/build-bundled/librife.dylib', mode=ctypes.RTLD_LOCAL|0x2)"
OSError: dlopen(…/build-bundled/librife.dylib, 0x0006): symbol not found in flat namespace '___kmpc_for_static_fini'

$ python3.14 -c "import ctypes; ctypes.CDLL('$work/build-sys/librife.dylib', mode=ctypes.RTLD_LOCAL|0x2)"
bound OK
```

The Homebrew-ncnn build only survives by luck: `libncnn.1.dylib` links
`libomp.dylib`, so the symbols are in the process by accident. The bundled ncnn
is built with `Could NOT find OpenMP` (cmake says so in the configure log) and
brings nothing. A build that ever reaches the CPU warp — a GPU that fails to
create a pipeline, a future upstream change — would abort instead of falling
back.

The fix is two flags, no patch, measured working:

```sh
meson setup … -Dcpp_args="-I/opt/homebrew/opt/libomp/include" \
              -Dcpp_link_args="-L/opt/homebrew/opt/libomp/lib -lomp"
```

```
$ otool -L "$work/build-bundled-omp/librife.dylib"
	/opt/homebrew/opt/libomp/lib/libomp.dylib (compatibility version 5.0.0, current version 5.0.0)
	/opt/homebrew/opt/vulkan-loader/lib/libvulkan.1.dylib (compatibility version 1.0.0, current version 1.4.0)
	/usr/lib/libc++.1.dylib (compatibility version 1.0.0, current version 2200.27.0)
	/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1359.0.0)
$ python3.14 -c "import ctypes; ctypes.CDLL('…/build-bundled-omp/librife.dylib', mode=ctypes.RTLD_LOCAL|0x2)"
RTLD_NOW bound OK
```

`libomp` is keg-only, so the flags are needed even though Homebrew installed it
as an ncnn dependency. `-Xpreprocessor -fopenmp` without them is the
`/opt/homebrew`-era advice for a *program*; it was never right for a bundle.

The `-DVulkan_LIBRARY` the task expected to be needed is **not** needed. ncnn's
`src/CMakeLists.txt:275` does warn —

```
CMake Warning at src/CMakeLists.txt:275 (message):
Vulkan_LIBRARY shall be defined for simplevk static linkage on APPLE
```

— and then `target_link_libraries(ncnn PUBLIC ${Vulkan_LIBRARY})` adds nothing,
but the static `libncnn.a` does not have to resolve anything on its own: RIFE's
own `dependency('vulkan')` puts `-lvulkan` on the final link, which is what
simplevk's symbols bind to. Passing `-DVulkan_LIBRARY` is still worth doing in a
real build script to silence the warning and pin the choice explicitly.

### The model-path abort

This is the macOS member of the family the Windows `swprintf("%s")` bug belongs
to, and it is worse than a black frame. `RIFE/rife.cpp`, the `#else` branch of
`load_param_model`:

```c++
static void load_param_model(ncnn::Net& net, const std::string& modeldir, const char* name)
{
    char parampath[256];
    char modelpath[256];
    sprintf(parampath, "%s/%s.param", modeldir.c_str(), name);
    sprintf(modelpath, "%s/%s.bin", modeldir.c_str(), name);
```

Two fixed 256-byte stack buffers and `sprintf`. `model_path` is a user-supplied
string that ButterEye passes straight through. Measured, with a real model
directory moved to progressively longer paths:

```
len=200 exit=0 -> ok min/max=0.5020/0.5020
len=235 exit=0 -> ok min/max=0.5020/0.5020
len=240 exit=0 -> ok min/max=0.5020/0.5020
len=241 exit=0 -> ok min/max=0.5020/0.5020
len=242 exit=134 -> <no output>
len=245 exit=134 -> <no output>
len=260 exit=134 -> <no output>
```

The threshold is exactly 242 characters, which is the first length at which
`<modeldir>/flownet.param` plus its terminator no longer fits (241 + 14 + 1 =
256), and exit 134 is **SIGABRT**: the whole `vspipe` (or mpv) process dies. On
the way out, `parampath` has already run over into `modelpath`, which you can
read off the stderr — the printed path has lost its first characters and its
tail:

```
fopen dddddddddddddddddd/dddddddddddddddddddd/…/rife-v4.26_ensembleFalse/flownet.bin failed
network graph not ready
```

242 characters is not far-fetched: a Homebrew tap under a non-default prefix, a
user-chosen model folder, or a path under `~/Library/Application Support/…` all
get there, and the sandbox-style long temporary directories macOS hands out are
longer than that on their own. The fix is the same shape as the Windows one and
should be a new patch, `buttereye-vs-rife-ncnn-posix-paths.patch`: build the two
paths as `std::string`, drop the 256-character limit, and keep the
`#if _WIN32`/`#else` split. Nothing in it is macOS-specific — the same overflow
exists in the Fedora and the generic POSIX build, where nobody has hit it yet,
so the patch belongs in the spec unconditionally rather than behind a platform
guard.

## Is Homebrew's ncnn an acceptable substitute for the pinned one?

Both build. Both render correctly. They are still **different products**, and the
decision has to be made rather than assumed.

The pinned ncnn exists for two reasons (SCOPE §8.2, M0(f)): reproducibility — the
SRPM carries the exact bytes and the build never re-downloads — and the GPU
faults Fedora's newer ncnn produced on NVIDIA at `gpu_thread ≥ 2`. The second
reason evaporates on an M2: there is one compute queue, `gpu_thread` is pinned at
1 anyway, and 8 of 8 runs were clean on both. The first does not evaporate at
all, and a third reason appears: the two do not agree about pixels.

Same source clip, same model, four interpolated 640×368 RGBS frames, dumped and
compared sample by sample:

```
floats: 2826240 differing: 602388 (21.314%) max abs diff: 0.52832 mean abs diff: 0.000467487
samples with |diff| > 0.0039: 51480 (1.8215%)
samples with |diff| > 0.0100: 22272 (0.7880%)
samples with |diff| > 0.0500:  3012 (0.1066%)
samples with |diff| > 0.1000:   984 (0.0348%)
samples with |diff| > 0.2500:   420 (0.0149%)
p99.9 = 0.05151  p99.99 = 0.33301
```

21.3 % of samples differ at all; 1.8 % differ by more than one 8-bit step; a
handful differ by half the range. Visually that is a thin halo on moving edges,
not a wrong frame — but it is not the Linux output either.

**The cause is the ncnn version, not ButterEye's patch.** The obvious suspect was
the no-pack8 patch, which the Fedora spec applies only to the system-ncnn build:
if the pinned ncnn took the pack8 warp pipeline and Homebrew's took pack4, that
alone would explain the gap. It does not. Rebuilding the bundled variant *with*
the no-pack8 patch (the pinned ncnn still has `use_shader_pack8`, so it applies)
gives output that is **bit-identical** to the bundled variant without it:

```
bundled pack8 vs bundled no-pack8        differing       0 ( 0.000%)  >1/255      0 (0.0000%)  max 0
bundled no-pack8 vs Homebrew ncnn        differing  602388 (21.314%)  >1/255  51480 (1.8215%)  max 0.52832
bundled pack8    vs Homebrew ncnn        differing  602388 (21.314%)  >1/255  51480 (1.8215%)  max 0.52832
```

`elempack == 8` never reaches the custom warp layer through MoltenVK, so the
patch is a no-op on this platform — which also means it is safe to apply to
either variant, and the macOS build script need not care which ncnn it is
building against. The whole difference is ncnn 20260526 against 305837fd.

The honest reading:

- **For a Homebrew tap (phase 4), Homebrew's ncnn is the right dependency.** A
  tap that statically bundles a 2025 ncnn inside a 5.9 MB bundle, while Homebrew
  ships `ncnn` as a maintained formula that every other consumer links, is
  fighting the platform. The binary is 35× smaller, the build is 466 → 4 targets,
  the licence story collapses from "bundled ncnn + glslang, five licences, two
  extra pristine tarballs" to "a formula dependency", and `brew` does the
  security updates. The two patches needed are both patches ButterEye already
  has.
- **It is a different product, and the docs must say so.** It must not be
  described as "the same build as Linux". `components.json`/the install record
  should name the ncnn it was built against, and any pixel-level comparison
  against a Linux render should expect ~1.8 % of samples to differ by a step.
- **Reproducibility is weaker and that is a real cost.** The tap pins a formula
  version, not bytes; a Homebrew ncnn bump changes the output without any change
  on ButterEye's side. If that is unacceptable, the bundled variant is proven to
  build here and should be kept as the fallback — it is also 15–65 % faster,
  which is not nothing when the engine is five times too slow.
- Keep `use_system_ncnn` as the switch it already is, exactly as the spec keeps
  `--with system_ncnn`. The macOS build script should default to the Homebrew
  ncnn and be able to produce the bundled one.

## The models

RIFE needs them; nothing is embedded in the plugin. `model_path` points at one
model directory, and the directory **name** is load-bearing — the plugin reads
the padding (64 for v4.25/v4.26, 128 for v4.25-lite) and the v2/v4 architecture
out of the name, so it may not be renamed. With no `model_path`, the plugin
defaults to `dirname(<plugin file>)/models`, which is POSIX-clean on macOS.

Fetched from the same tag as the plugin, by the same sparse checkout, and all
four files match `packaging/plugins/PROVENANCE-rife-ncnn-models.md` exactly:

```
$ shasum -a 256 rife-v4.26_ensembleFalse/* rife-v4.22_lite_ensembleFalse/*
94d58e30b75d7c7609cfa6f3bdad524deddd14f5f75e85c36d2f827ef5c64731  rife-v4.26_ensembleFalse/flownet.bin
79f16c28903f93f8308f0c4c947f8c7e0c17d99a57b85473e5f298dd578d137b  rife-v4.26_ensembleFalse/flownet.param
792da5d62937199886bf8bb781ab8e0eddf663854d57e4a43cd7b32e4e81b29b  rife-v4.22_lite_ensembleFalse/flownet.bin
091119e19a5891a5477de8f90b3c9e979baece5ee57c27d4e4138a6cc2552cc3  rife-v4.22_lite_ensembleFalse/flownet.param
```

Licence: **MIT** throughout, and the chain is the one `PROVENANCE-rife-ncnn-models.md`
records — the ncnn conversions from `styler00dollar/VapourSynth-RIFE-ncnn-Vulkan`
(MIT, © HolyWu and contributors), of the RIFE v4.x weights from hzwer's
Practical-RIFE (MIT, © hzwer), continuing ECCV2022-RIFE (MIT, © Megvii Inc.),
following nihui's rife-ncnn-vulkan (MIT, © 2020 nihui). The four texts are
already in `packaging/plugins/licenses/`: `LICENSE.Practical-RIFE`,
`LICENSE.ECCV2022-RIFE`, `LICENSE.rife-ncnn-vulkan`, and the tag's own `LICENSE`.
Nothing new is needed for macOS, and the models are architecture-independent, so
a macOS build ships the identical files the COPR and the Windows installer do.

For the bundled variant, the two extra licence texts the spec names are present
in the pinned tarballs and must travel with it:
`subprojects/ncnn/LICENSE.txt` ("Tencent is pleased to support the open source
community by making ncnn available.") and
`subprojects/ncnn/glslang/LICENSE.txt`.

## What a real macOS build needs, concretely

1. The two `%prep` seds (the `vapoursynth.pc` `libdir` rewrite and dropping
   `install_subdir('models')`) — the spec's, unchanged.
2. `buttereye-vs-rife-ncnn-no-pack8.patch` and
   `buttereye-vs-rife-ncnn-host-pool.patch`. Not the Windows-paths one. Unlike
   the Fedora spec, the no-pack8 patch can be applied to **both** variants here:
   it is required against Homebrew's ncnn and bit-identical against the pinned
   one.
3. **A new patch** for the 256-byte `sprintf` model paths (above).
4. `-Dcpp_args=-I/opt/homebrew/opt/libomp/include` and
   `-Dcpp_link_args="-L/opt/homebrew/opt/libomp/lib -lomp"`, on both variants.
5. `-Duse_system_ncnn=true` by default, with the bundled path kept and
   `-DVulkan_LIBRARY` set when it is used.
6. Installed Homebrew formulae: `vapoursynth`, `ncnn`, `libomp`, `pkgconf`,
   `meson`, `ninja` (+ `cmake` and `vulkan-loader` for the bundled variant).
   Everything was already installed on this machine; the spike installed nothing.
7. A ButterEye-side decision: `gpu_thread` must be 1 on Apple Silicon, and the
   live-play path should not offer RIFE at 1080p on an M2.

## Not measured

- Whether a macOS RIFE frame matches a **Linux** RIFE frame. Both macOS variants
  were compared against each other and against the geometric truth; neither was
  compared against a Linux render, and there is no Linux machine here.
- GPU-fault behaviour under a real soak. Eight 1080p runs were clean; M0(f) needed
  20–42 runs to see the NVIDIA faults, and macOS exposes no Xid-equivalent log
  for the doctor to read.
- RIFE inside **mpv** on macOS. Nothing was played; the throughput numbers say it
  would not keep up at 1080p, and the mpv VSScript bare-name `dlopen` problem is
  phase 4's.
- `tta`, `uhd`, `sc` and `skip` (which wants the VMAF plugin), and the other 70
  model directories.
- Intel Macs. arm64 only, as everywhere else on `docs/PLATFORMS.md`.
- Apple's own Metal path. The `metal` compiler is absent (Command Line Tools
  only), so nothing here may depend on compiling a `.metal` file, and nothing
  does.
