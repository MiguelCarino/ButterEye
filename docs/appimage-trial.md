# The AppImage trial

A feasibility trial of ButterEye as an x86_64 Linux AppImage: built and tested in
CI only, on the `appimage-trial` branch, **not a release and not a supported way
to install ButterEye**. Fedora's RPMs remain the Linux packaging; nothing here
changes them (the seam is inert unless ButterEye's own Python runs from inside an
AppImage, and that is tested). Everything below that claims something working
names the CI run it was measured in.

- Build: `packaging/appimage/build-appimage.sh` (with `pins.sh`, `stage.py`,
  `AppRun`, `host-libs.txt`)
- Tests: `packaging/appimage/test-appimage.sh` and `appimage_tests.py`
- CI: `.github/workflows/appimage.yml` (`workflow_dispatch`, and pushes to
  `appimage-trial` only - never main, where a `packaging/**` push already costs
  the two-hour Windows build)
- Runtime seam: `osapi.LINUX_APP` and its uses (below), pure tests in
  `tests/core/test_appimage_pure.py`

## Result in one paragraph

It works, in the narrow sense this trial could measure. One 75 MiB file, built
in 8.5 minutes on a GitHub runner, started in clean containers of Ubuntu 22.04
and 24.04, Debian 12 and 13, Fedora 44 and Arch Linux - with nothing ButterEye
needs installed, only a desktop's base libraries - and on the runner itself
through a real FUSE mount, and on every one of them: the doctor found mpv,
VapourSynth, MVTools and FFMS2 inside the AppImage with nothing blocking; an
offline render doubled a clip's frame rate exactly; mpv's VapourSynth filter ran
MVTools inside the AppImage's mpv; ButterEye's own live path played a clip at
twice its frame rate in real time; the GUI came up on Qt's offscreen platform; and
no library from outside the AppImage was loaded except those on an explicit
allow-list (or loaded in turn by one of them). What it could not measure is
everything a desktop adds: a GPU, a real X11 or Wayland session, sound, VA-API,
and FUSE on a distribution's own setup.

## What was built, and how

| Part | Version | Where it comes from |
|---|---|---|
| Python | 3.14.8 | python-build-standalone 20261009, `x86_64-unknown-linux-gnu-install_only_stripped`, SHA-256 pinned |
| PySide6-Essentials, Shiboken6 | 6.11.2 | PyPI `manylinux_2_34_x86_64` wheels, trimmed to Core/Gui/Widgets/Svg/SvgWidgets/DBus and the xcb, Wayland, offscreen and minimal platforms |
| VapourSynth | R81 | the official `manylinux_2_28_x86_64` wheel (it exists, so VapourSynth is not built) |
| mpv | 0.41.0 | built: X11 + EGL, VA-API (copy-back), ALSA, PulseAudio, Lua, **vapoursynth**; no Wayland, no Vulkan |
| libplacebo | 7.351.0 | built, static into mpv, OpenGL only |
| FFmpeg | 8.1.3 | built, shared, `--enable-gpl` (x264), dav1d, zlib, iconv, VA-API via libdrm; nothing auto-detected |
| dav1d | 1.5.4 | built, static into libavcodec |
| FFMS2 | 5.0 | built (compiled the way the Windows build does) |
| MVTools | v29_2 | built from the RPM spec's tag and patch |
| libass, FriBidi, Lua 5.2, x264, FFTW, libXss/Xpresent/Xrandr/Xrender, xcb-util × 6 | Ubuntu 22.04's | copied from the build container's packages, each one's exact Debian version recorded |
| AppImage runtime | type2-runtime 20251108 | static (no libfuse2 on the host), SHA-256 pinned |

The engine is MVTools only: no RIFE, no mvgpu, no models.

**Base image: `ubuntu:22.04`, not manylinux_2_28.** The decision was measured
rather than assumed: the PySide6 6.11.2 wheel is `manylinux_2_34`, so the AppImage
cannot start on a glibc older than 2.34 whatever it is built on, and
manylinux_2_28's older glibc would buy no reach at all. Ubuntu 22.04 (glibc 2.35)
is the oldest mainstream base at that floor and has every `-dev` package mpv
needs, where AlmaLinux 8 would have meant building the X11 stack as well. The
build's own gate reports the floor it actually produced: newest symbol versions
**GLIBC_2.35** (libavcodec), **GLIBCXX_3.4.29** (mpv, through libplacebo's C++)
and CXXABI_1.3.13 (QtCore) - so glibc 2.35 and a GCC 11 libstdc++ or newer. That
is Ubuntu 22.04, Debian 12, Fedora 36, RHEL 10 and later; Debian 11 (2.31) and
RHEL 9 (2.34) are out.

**mpv: built, not reused.** No portable mpv build with the vapoursynth filter
was found whose source and build could be reproduced and offered, and the filter
in mpv 0.41 links VSScript at build time, so it has to be built against the
VapourSynth it will run with anyway. The build writes a `vapoursynth-script.pc`
pointing at the wheel's headers and `libvsscript.so`, links mpv against it, and
gives mpv a RUNPATH to the wheel's folder. Feature set: the minimum that is a
usable player on X11 (and on Wayland through XWayland). Wayland itself is off
because mpv 0.41 wants libwayland-client 1.21 and Ubuntu 22.04 has 1.20 - and
libwayland must be the host's, so a newer bundled copy is not an option.

**Layout.** The AppDir mirrors the macOS app's `Contents/Resources`:
`AppRun`, `python/`, `bin/` (mpv, ffmpeg, ffprobe), `lib/`, `vapoursynth/`
(mvtools.so, libffms2.so), `share/`, `licenses/`, `buttereye-install.json`.
`AppRun` is a shell script: no args → `python -I -B -m buttereye.gui`, any args →
`python -I -B -m buttereye.cli ARGS`. Every ELF file finds its libraries through
RUNPATH (`$ORIGIN`-relative, set with patchelf); nothing sets
`LD_LIBRARY_PATH`.

**Closure and the allow-list.** `stage.py closure` walks every ELF file: a
needed library is either on `packaging/appimage/host-libs.txt` (left to the
host) or copied in from the build container next to the libraries the file
already finds, and the copy's Debian package and version are recorded. `stage.py
check` then refuses the build if any dependency resolves neither way or any
RUNPATH points outside the AppDir. The same allow-list is what the tests check
against on every distribution.

### The host allow-list

Everything else ButterEye's AppImage runs is inside it. These are the host's
(`host-libs.txt` is the authority; matched as sonames):

- glibc: `ld-linux-x86-64.so.2`, `libc`, `libm`, `libmvec`, `libdl`, `libpthread`,
  `librt`, `libresolv`, `libutil`, `libanl`; and `libstdc++.so.6`, `libgcc_s.so.1`
  (the host's must be the newer one: Mesa's drivers load into the same process)
- GPU: `libGL`, `libGLX`, `libEGL`, `libOpenGL`, `libGLdispatch`, `libvulkan.so.1`,
  `libdrm`, `libgbm`, `libva`, `libva-drm`, `libva-x11`, `libva-wayland`
- display: `libX11`, `libX11-xcb`, `libXext`, `libXfixes`, `libXau`, `libXdmcp`,
  `libxcb` and the extension libraries built from libxcb's own source (render,
  shape, shm, sync, xfixes, randr, xkb, xinput, glx, present, dri2/3, …),
  `libwayland-client/cursor/server/egl`, `libxkbcommon`, `libxkbcommon-x11`
- desktop: `libfontconfig`, `libfreetype`, `libharfbuzz`, `libglib-2.0`,
  `libgthread-2.0`, `libgobject-2.0`, `libgio-2.0`, `libdbus-1`, `libasound`,
  `libpulse`, `libz`, `libzstd`

and whatever those load in turn (Mesa's, PulseAudio's `libpulsecommon`,
systemd's, libpng/brotli under FreeType, and so on), which is the host's
business. Measured on the runner (Ubuntu 24.04), the AppImage's ELF files name
47 host libraries directly, all on the list, and its three traced processes (the
live mpv, the GUI's Python, the extracted-tree harness) had 62 host files mapped,
all on the list or loaded by one that is.

## The runtime seam

Modelled on the macOS app's `MACOS_APP`, with the minimum added:

- `osapi.LINUX_APP` - the AppImage's root, or None. Decided once, at import,
  by `osapi.appimage_root(sys.executable, osapi.__file__)`: the interpreter must
  be `<root>/python/bin/python3.<minor>`, this very module must be inside
  `<root>/python`, `<root>` must hold the install record and `AppRun`, and
  `<root>` and `<root>/python` must belong to this user or root and be writable
  by nobody else.
  **`$APPDIR` is never read.** The runtime exports it, but so can anyone who
  starts a process; a variable alone must never make ButterEye hand its children
  a plugin folder, a VSScript library or a `PATH`. The answer comes from where
  the code that is already running lives, so it is trusted exactly as far as
  that code is. The ownership test is for `--appimage-extract-and-run`, which
  extracts to `$TMPDIR/appimage_extracted_<md5>` and *reuses* a folder of that
  name if it exists: a tree another local user planted in a shared `/tmp` is
  refused by ButterEye - but the runtime would already have executed that tree's
  `AppRun`, which is the runtime's problem and cannot be fixed from inside.
  `tests/core/test_appimage_pure.py` holds all of this, including "APPDIR
  pointing at a perfect planted tree changes nothing".
- `osapi.executable("mpv"|"ffmpeg"|"ffprobe")` and `bundled_vspipe()` - the
  AppImage's, by full path, never from `PATH`.
- `osapi.use_bundled_runtime()` → `point_at_appimage`: `<root>/bin` first on
  `PATH` (render's `shutil.which` callers), `VSSCRIPT_PATH` to the bundled
  `libvsscript.so` (mpv 0.41 links it through RUNPATH and does not read the
  variable; set anyway, as on the other platforms), `PYTHONNOUSERSITE=1`, and
  `PYTHONHOME`, `PYTHONPATH`, `VAPOURSYNTH_EXTRA_PLUGIN_PATH`, `LD_LIBRARY_PATH`,
  `LD_PRELOAD` removed from the children's environment. Then
  `register_vsscript` writes VSScript's configuration line for the bundled
  Python (`~/.config/vapoursynth/vapoursynth.toml`), as the macOS app does: R81's
  VSScript has no other way to find Python. The FUSE mount point is new on every
  start, so every start writes a line and drops lines of mounts that have gone
  (`_APPIMAGE_VSSCRIPT_KEY`, narrow on purpose: only `.mount_*` and
  `appimage_extracted_*` paths).
- plugin, FFMS2 and data folders (`types`, `scriptgen.generator`,
  `paths.install_dir`) → `<root>/vapoursynth`, `<root>/vapoursynth/libffms2.so`,
  `<root>/share`.
- the doctor: the install record is the package database (`manifest_db`, as in
  the macOS app and on Windows), and no finding offers a dnf, COPR or rpm line:
  what the AppImage carries is "download the AppImage again", anything else
  keeps its advice without the Fedora command. The licences pane and the
  benchmark's install advice follow.
- `osapi.spawn_detached` - see "Problems found", the one change that is not
  AppImage-specific in shape.

## Tests, and what passed where

Each distribution's container gets a desktop's base libraries (per-distribution
lists in `test-appimage.sh`: GL/EGL/Vulkan loader, libdrm/gbm, libva, X11/xcb,
Wayland client, xkbcommon, fontconfig/FreeType/HarfBuzz, GLib, D-Bus, ALSA,
PulseAudio client, zlib/zstd, a font) and nothing else - no mpv, VapourSynth,
FFmpeg or Python. In containers the AppImage runs with
`APPIMAGE_EXTRACT_AND_RUN=1` (no FUSE); on the runner it runs as a user would
run it, through the static runtime's FUSE mount (`/tmp/.mount_Butter*`,
confirmed in the traced library paths; the runner has `fusermount3`).

| Test | What it checks |
|---|---|
| version | `ButterEye.AppImage --version` |
| doctor | `doctor --json`: `mpv.version`, `mpv.vf_vapoursynth`, `packages.mvtools`, `render.ffms2` OK; nothing blocking outside GPU rows; mpv's evidence is the AppImage's; no dnf/COPR/rpm advice |
| render | `render --profile cpu --target 2x` of a generated 6 s 24 fps clip: 48/1 fps, 288 frames |
| mpv-filter | the AppImage's mpv, environment set up by the AppImage's own ButterEye, `--vf=vapoursynth` with an MVTools FlowFPS script, in mpv's encode mode: the script ran inside mpv, output 48/1 fps |
| live | `play --profile cpu` with `vo=null`/`ao=null`: mvtools in mpv's filter chain, `estimated-vf-fps` 48 for a 24 fps source, 8.0 s played in 8.0 s wall (mpv IPC) |
| gui | no arguments, `QT_QPA_PLATFORM=offscreen`: up with QtWidgets and the offscreen plugin mapped, no traceback, ended by SIGTERM |
| libs | `ldd` of all 82 ELF files and `/proc/<pid>/maps` of the live mpv and the GUI process: everything inside, allow-listed, or loaded by an allow-listed host library |

Results, run [38540028085](https://github.com/MiguelCarino/ButterEye/actions/runs/38540028085)
(commit 242e2b1; every row also passed in the run before it, 38534976126,
which had only the repository-suite step still red):

| Distribution (glibc) | version | doctor | render | mpv-filter | live | gui | libs |
|---|---|---|---|---|---|---|---|
| Ubuntu 22.04 (2.35), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| Ubuntu 24.04 (2.39), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| Debian 12 (2.36), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| Debian 13 (2.41), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| Fedora 44 (2.43), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| Arch Linux (2.44), extract-and-run | pass | pass | pass | pass | pass | pass | pass |
| GitHub runner, Ubuntu 24.04 (2.39), **FUSE mount** | pass | pass | pass | pass | pass | pass | pass |

The doctor's report was the same 19 rows everywhere: OK for mpv, the
VapourSynth filter, `probe.vs_version` ("mpv runs VapourSynth R81 with Python
3.14.8"), `probe.plugins` ("MVTools load inside mpv"), MVTools, licence files,
FFMS2; degraded for RIFE (not carried), Vulkan ICD and devices (no GPU, no
vulkaninfo), mkvmerge, the HDR10 encoder (no x265/SVT-AV1 in this FFmpeg); info
rows for mvgpu, GPU faults, 7z, TensorRT. Exit code 0 everywhere. The mpv-filter
encode produces 285 frames for 6 s rather than 288 (the last FlowFPS frames at
end of stream); the test asks for ≥ 280 and the exact count is not claimed.

The runner job also runs the repository's own test suite with the AppImage's
Python against the checkout (`osapi.LINUX_APP` None there): **1,231 passed,
172 skipped, 2 deselected** in 32 s, with `tests/gui` left out. The two
deselected: `tests/boundary/test_import_guard.py::test_allowed_modules_pass`
imports `PySide6.QtTest`, which the trimmed Qt does not carry (nor pytest-qt's
needs, hence no `tests/gui`); and
`tests/test_bench_runner.py::test_count_new_counts_xid_109_through_the_real_scanner`
counted 1 planted Xid fault where it expected 2 - it runs a stub `journalctl`
and touches nothing of the AppImage, the cause was not looked into, and since no
other workflow runs this suite on Linux there is nothing to compare it with. It
is a lead for whoever adds Linux CI, not an AppImage result.

## Sizes

- AppImage: **75 MiB** (78,490,104 bytes); the AppDir is 243 MiB in 1,652 files
  (zstd level 19 SquashFS, 1 MiB blocks)
- Corresponding Source tar: **107 MiB** (FFmpeg, dav1d, mpv, libplacebo with
  submodules, FFMS2, MVTools, VapourSynth, qtbase, qtsvg, pyside-setup, ButterEye
  itself, and the Ubuntu source package - `.dsc`, orig and Debian tarballs - of
  each of the 15 Ubuntu packages the AppImage copies from)
- build: about 8.5 minutes on `ubuntu-latest` (FFmpeg ~4 of them); each
  distribution's tests about one minute (the live test alone is 18 s by design)

## Licences

`licenses/<part>/` in the AppImage carries every part's texts (ButterEye's,
Python's and everything statically linked into it, Qt's third-party notices from
qtbase/qtsvg, VapourSynth, mpv, FFmpeg with its configure line, dav1d,
libplacebo, FFMS2, MVTools, the runtime, and each Ubuntu package's `copyright`),
`licenses/SOURCES.txt` is the written offer, and `buttereye-install.json` lists
28 components with versions and SPDX expressions - which the doctor's licence
row reads ("Licence files present" everywhere). The Ubuntu packages' licences
are taken from Fedora's spec for the same library and checked into
`stage.py`; a package the table does not know stops the build. The whole is GPL
(mpv with `-Dgpl=true`, FFmpeg with x264, MVTools), which ButterEye's AGPL
accommodates.

## Problems found (and what was done)

1. **python-build-standalone's Linux CPython cannot `posix_spawn` with `setsid`.**
   It is built against glibc 2.17 headers, where `POSIX_SPAWN_SETSID` does not
   exist, so `os.posix_spawnp(..., setsid=True)` raises `NotImplementedError`
   before starting anything - and that is how `osapi.spawn_detached` starts mpv
   for live playback. The first test run had render passing and every live
   session dying with BE-9999. Fixed in `spawn_detached`: on that exception only,
   the same child is started with `Popen(start_new_session=True)` (own session,
   stdin `/dev/null`, output to the log, nothing else inherited) and reaped
   through its `Popen`, the Windows shape. Fedora's Python never takes the
   branch. This would bite any future Linux bundle built on
   python-build-standalone, not only an AppImage.
2. **The doctor's advice is too blunt inside the AppImage.** "Download the
   AppImage again" now replaces every Fedora command - correct for what the
   AppImage carries, wrong for `packages.rife`, `packages.mvgpu` (a fresh copy
   still has neither) and `vulkan.icd` (the GPU driver is the host's). Left as
   is for the trial; it wants per-row AppImage wording.
3. **Build-environment fixes, none of them source patches:** libplacebo's
   stub of the Vulkan API includes Vulkan headers even with Vulkan off (its
   `3rdparty/Vulkan-Headers` submodule is fetched); static libplacebo is C++
   underneath and its `.pc` does not say so (`-lstdc++` appended); MVTools v29_2
   calls `isfinite` unqualified, which GCC 11 rejects (`-include math.h`).
4. **The AppImage's lifetime vs. a detached mpv - not tested, and likely a real
   problem.** The runtime unmounts the image (FUSE) or deletes the extraction
   folder (extract-and-run) when `AppRun`'s process exits. ButterEye starts mpv
   to outlive it (detach, `buttereye play` returning, the GUI closing with a
   session open); an mpv still running from a vanished mount would fail on its
   next lazy load (a plugin, a Lua script, a decoder library). In the tests the
   CLI stays up until mpv quits, so this never happened. It needs either mpv to
   hold the runtime's keep-alive descriptor, or ButterEye inside an AppImage to
   refuse to leave a session behind.
5. **No Wayland in mpv** (libwayland 1.20 on the base), so Wayland desktops run
   it through XWayland. Qt's Wayland plugin is carried and resolves, but was not
   exercised.
6. **Loose pins for the Ubuntu-archive part.** The archive moves with security
   updates; the build records and offers the exact versions it copied, but two
   builds a month apart can differ. A `snapshot.ubuntu.com` pin would close it.
7. **Qt third-party notices** use the macOS list of Qt's bundled code; the
   Linux wheel's xcb and Wayland plugins (qtwayland is not pinned at all) were
   not re-derived.

## Not tested, at all

- a real GPU: OpenGL/EGL output, VA-API decode through the host's driver,
  Vulkan, NVIDIA - the runners have none, and every GPU row degrades as expected
- a real X11 or Wayland session: mpv drew to `--vo=null`, the GUI ran on
  `offscreen`; no window was ever shown, no input, no HiDPI, no theme
- sound: mpv ran with `ao=null`
- FUSE on a desktop distribution (only on GitHub's Ubuntu 24.04 runner image)
  and the libfuse-less runtime's behaviour on a system with no `fusermount3`
  at all (it prints an error and suggests `--appimage-extract-and-run`)
- desktop integration (`.desktop`, icon, AppImageLauncher/Gear Lever), updates,
  signing (the AppImage is unsigned)
- long sessions, seeking, subtitles, real-world files beyond a generated H.264
  clip, 4K, HDR
- the detach/lifetime case above, and two AppImage instances at once

## Recommendation (a recommendation, not a decision)

**Go one step further with the AppImage, as an explicitly experimental
download - and do not start a Flatpak yet.** The trial's hard questions came out
well: the whole stack relocates, VapourSynth's official wheel and
python-build-standalone hold up, building mpv and FFmpeg on an old base is an
eight-minute CI job, the allow-list holds on six distributions spanning glibc
2.35 to 2.44, and the seam is small and leaves Fedora untouched. A
Flatpak would answer the GPU question better (the GL/VA-API/NVIDIA extensions
are maintained by Flathub) but costs a runtime, a manifest that rebuilds
everything again under flatpak-builder, sandbox holes for mpv's IPC socket and
for the user's own mpv config, and Flathub's review - a much larger step whose
main advantage lands exactly where this trial is blind, so it is better decided
after a GPU test than before.

Before the AppImage could ship as an experimental download:

1. a run on real hardware - at least one Intel/AMD (Mesa, VA-API) and one NVIDIA
   desktop, X11 and Wayland - with mpv's real video output, sound and the GUI
2. the detach/lifetime problem (4) settled
3. the doctor's AppImage wording per row (2)
4. Wayland in mpv (a newer base for mpv alone is not possible without raising
   the glibc floor; more likely: build libwayland-client against which mpv
   compiles but take the host's at run time, or accept XWayland and say so)
5. RIFE and mvgpu, if the AppImage is to be more than MVTools (both need the
   host's Vulkan, which the allow-list already allows)
6. a pinned Ubuntu snapshot, re-derived Qt notices, a signature, and update
   information (zsync) if updates are wanted
7. a decision on the advertised floor: glibc 2.35 / Ubuntu 22.04 era
