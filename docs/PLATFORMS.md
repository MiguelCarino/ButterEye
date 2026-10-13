# Platforms: Linux, Windows, macOS

ButterEye 0.0.1 supports **Fedora 44 and Fedora 45 on x86_64** and nothing else
(SCOPE §2, "Non-goals"). Windows and macOS are explicitly out of v1. This page
records, honestly, how far the code is from running on them, so a later decision
starts from facts rather than guesses. Nothing here is a promise or a schedule.

Assessed against the 0.0.1 tree on 2026-10-10. The Windows port's four phases
landed on 2026-10-11 and are described below: there is now a Windows installer,
tested under Wine on the Linux dev box and, in CI, installed and tested on GitHub's
Windows Server 2025 machines (no GPU there). It has **not yet been used on a real
Windows desktop PC with a GPU**, so it is not released. macOS is not released
either, and there will be no macOS application bundle at all — the distribution
written in phase 4 is a Homebrew tap, for the reason under "Why a tap and not an
`.app`" below. macOS phases 0 and 1 (both
2026-10-12) make the three-platform type check
and ButterEye's own macOS tests run and pass on an Apple Silicon Mac, and give the
platform layer a third value — the core's hot paths (process liveness, the private
runtime folder, mpv's control channel, locks, plugin file names, durable renames)
all ask `buttereye.core.osapi` for the platform's answer instead of calling Linux
directly. They do **not** empty the core of Linux-only calls: the doctor and
detection modules still read `/proc` and `/usr/lib64` themselves, and "What phase
1 does not do" below names each one and the phase that owns it. So this is a
platform layer and a gate against later breakage, not support, and a dozen
`tests/core` tests had to be taught that there are three platforms before the gate
was green (phase 1, below). Phase 2 (also 2026-10-12) stopped the doctor answering
a Mac with Fedora advice and gave it macOS sources. Phase 3 (same day) built the
first macOS plugin: `libmvgpu.dylib`, native arm64, which VapourSynth loads and
which now reaches MoltenVK — while MVTools and ffms2 turn out to need no ButterEye
build at all on a Mac, because Homebrew already ships them. RIFE-ncnn was
measured in the same phase, in a spike (`packaging/macos/RIFE-SPIKE.md`): it
builds natively on arm64 and interpolates correctly through MoltenVK, and
nothing in the build path produces it, so a Mac still has no RIFE engine.
Phase 4 (same day) is the phase that makes a Mac smooth video: mpv could not
load VapourSynth's VSScript at all, `osapi.use_bundled_runtime` now hands it
the file, and `tests/integration/test_hdr_live_real.py` — red in every earlier
phase — went green, with the full doctor going from one blocking finding to
none. Phase 4 also builds RIFE at last (`packaging/macos/build-rife.sh`) and
writes the distribution: a Homebrew tap, not an `.app` bundle, for a reason
about money rather than engineering (below). Phase 5 (2026-10-13) is the
real-hardware gate, and it is the one phase that builds nothing: it measures,
and then writes the result down with the machine attached. Its deliverables are
the table "Every macOS number in this repository, and the machine it came from"
below, next to the list of what this machine **cannot** measure and what each of
those would take, and `docs/macos-release-bar.md`, which puts the same evidence
as a twenty-item checklist with a verdict on each. None of that makes macOS supported: nothing is published,
nothing is signed, no formula has ever been installed, and whether any of it
ships is the owner's decision rather than a phase's (SCOPE §2, §13).

## Summary

| Piece | Linux (Fedora 44/45) | Windows | macOS |
|---|---|---|---|
| Python core (`buttereye.core`) | supported | platform layer, detection and doctor done (phases 1-2) | the platform layer is done and three-valued (phase 1): peer check, process state, process exit watch, folders, locks, durable saves, plugin names. The doctor, hardware detection and setup now give macOS answers too (phase 2): no finding carries a `dnf`, `rpm` or `journalctl` line. One plugin is now built for macOS and two are taken from Homebrew (phase 3), RIFE joined them in phase 4, and phase 4 is also where mpv can finally load VapourSynth's VSScript, so live smoothing works (`osapi.use_bundled_runtime`, `osapi.vsscript_library`). The distribution is written and unpublished: a Homebrew tap |
| PySide6 GUI | supported | bundled by the installer; opens under Wine, Qt loads on Windows Server 2025 (CI); the window is untried on a real desktop | Qt itself runs; inherits the core's gaps (`fcntl` is available) |
| mpv control (JSON IPC) | Unix sockets + socketpair | **named pipes**, done; tested under Wine with a real `mpv.exe` | Unix sockets work; `SO_PEERCRED` does not exist, so the uid comes from `LOCAL_PEERCRED` and the pid from `LOCAL_PEERPID` (phase 0, measured against a Homebrew `mpv`) |
| mpv `vapoursynth` filter | distro mpv has it | depends on the mpv build | **measured on the binary, not inferred from the formula**: Homebrew's `mpv` is installed here as 0.41.0 (exactly SCOPE's minimum) and `mpv --vf=help` lists `vapoursynth`. The mpv.io / stolendata app bundle, which a Mac user is likelier to have already, is 0.40.0 **without** the filter (0 hits in `strings`; `--vf=help` lists only `format`, `lavfi`, `lavfi-bridge`, `sub`), and that cask has been disabled in Homebrew since 2026-09-01 for failing Gatekeeper. Having the filter compiled in was not enough on its own: mpv `dlopen`s VSScript by bare leaf name, and Homebrew keeps the only copy inside the `vapoursynth` formula's libexec virtualenv where dyld never looks, so out of the box no ButterEye filter could be created inside mpv at all. **Phase 4 fixes that** (below): `osapi.use_bundled_runtime` hands each child mpv the file and its directory, and live smoothing works — `tests/integration/test_hdr_live_real.py`, red in every earlier phase, passes all three |
| VapourSynth + `vspipe` | Fedora packages | bundled by the installer (the official wheel, inside its own Python) | installed and run here: Homebrew's `vspipe` reports **VapourSynth Core R81**, and autoloads from `/opt/homebrew/lib/python3.14/site-packages/vapoursynth/plugins/`, which holds `mvtools.dylib` and `libffms2.dylib` |
| `buttereye-vs-mvtools` | COPR RPM | `mvtools.dll`, cross-built (phase 3) | **it really interpolates on arm64**: a 24-frame clip through `mv.Super`/`mv.Analyse`/`mv.FlowFPS` came out of `vspipe` as 48 frames at 3484 fps, with `mvtools.dylib` autoloaded. Homebrew ships `vapoursynth-mvtools` 29 as an arm64 bottle. Upstream's `meson.build` already takes the aarch64 branch and bundles `sse2neon.h`, and on an M2 clang -O3 auto-vectorises the plain-C SAD to NEON at 227.6 M SAD/s for 16×16, 1.3× faster than the same SSE2 kernel through sse2neon. **Phase 3 therefore builds nothing here**: the bottle is the answer, and the Windows build's reasons for compiling its own do not carry over |
| `buttereye-vs-rife-ncnn` | COPR RPM (bundled ncnn) | `librife.dll`, cross-built with a Windows path fix (phase 3) | **measured in a spike in phase 3, built by phase 4** (`packaging/macos/build-rife.sh`, the step `build-plugins.sh` had only reserved; the spike is `packaging/macos/RIFE-SPIKE.md`): from the commit `packaging/plugins/repack-rife-ncnn.sh` pins, with ButterEye's two existing patches (`buttereye-vs-rife-ncnn-no-pack8.patch` and `buttereye-vs-rife-ncnn-host-pool.patch`, both applying with no fuzz), it builds natively on arm64 — against Homebrew's `ncnn` 20260526 with `-Duse_system_ncnn=true`, and with the bundled pinned ncnn — loads into VapourSynth R81, finds the GPU through MoltenVK (`[0 Apple M2]`) and renders a real interpolation: mean absolute error 0.0045 against the geometric truth, where a blend of the two neighbours scores 0.0331. Offline only, though, and the docs may not say otherwise: 8.5–9.8 fps at 1080p against the ≈48 fps a live 2× 24p needs, and `gpu_thread` has to be 1 because MoltenVK exposes one compute queue. It wants OpenMP link arguments the plugin's `meson.build` omits, and one new ButterEye patch for a crash that is not macOS-specific (the model-path overflow under "Plugins" above, which phase 4 wrote); `-DVulkan_LIBRARY` turns out **not** to be needed, and `build-rife.sh` deliberately does not pass it. Phase 4's build takes Homebrew's `ncnn` by default (`BUTTEREYE_RIFE_NCNN=bundled` builds the pinned static one instead), which is **not** the Linux build's pixels: 21.3 % of output samples differ |
| `buttereye-vs-mvgpu` (Vulkan compute) | COPR RPM | `libmvgpu.dll`, cross-built (phase 3) | **built, loadable and dispatching on arm64** (phase 3): compiling it needed no source change at all — Apple clang 21, meson, ninja and Homebrew's `glslang` turn all seven shaders into SPIR-V and link `libmvgpu.dylib` against nothing but `libvulkan.1.dylib`, `libc++` and `libSystem`; it exports `_VapourSynthPluginInit2` and VapourSynth loads it. Reaching the GPU took a guarded retry in `Gpu::init` (`mvgpu/src/mvgpu.cpp`): the plain `vkCreateInstance` — API 1.1, no instance extensions, `flags` 0 — came back `VK_ERROR_INCOMPATIBLE_DRIVER` (-9), because MoltenVK's ICD says `"is_portability_driver": true` and the loader hides such a driver until the application enables `VK_KHR_portability_enumeration` and sets `VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR`; that refusal, and only that refusal, is what makes it ask for the two and call again, so Linux and Windows are still handed the struct they always were (phase 3, below). With the retry in, on MoltenVK 1.4.2 (M2): all eight pipelines are created and the in-between frame is **bit-exact** against the square drawn at the half-way position (mean and maximum absolute error 0.0, against 8.43 for a blend of the two sources), and a full render under `MVK_CONFIG_DEBUG=1 MVK_CONFIG_LOG_LEVEL=4` logs 8 SPIR-V → MSL conversions and 10 Metal shader compilations and not one `[mvk-warn]` or `[mvk-error]` line. The limits are far from tight: `maxComputeWorkGroupInvocations` 1024 against the 512 needed, `maxStorageBufferRange` 4294967295 against the 127 MiB the vector field wants at 4K. `vulkaninfo` agrees the device is real: `deviceName` "Apple M2", `driverName` MoltenVK (`driverID` `DRIVER_ID_MOLTENVK`, `driverInfo` 1.4.2) at `apiVersion` 1.4.357 — not the mock ICD. Its `deviceType` is `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, which is a phase-2 problem on its own (below) |
| ffms2 (render's source filter) | Fedora package | `ffms2.dll`, built from source in the same chroot (phase 4) | a Homebrew bottle (`ffms2` 5.0), so the Windows source-offer problem does not arise; installed here as `libffms2.dylib` in VapourSynth's autoload directory, and it has now decoded: a ButterEye-generated `.vpy` read a real `.mkv` through it and MVTools turned 72 frames at 24 fps into 144 at 48, through the real `vspipe`, at 1432 fps (phase 3). Nothing is built here, so phase 3's build script does not produce it |
| TensorRT (experimental) | user-built `vstrt` | not yet (vs-mlrt is Windows-first upstream, so it is possible) | **permanently** not possible: there is no CUDA or TensorRT for macOS, and no Apple Silicon Mac has an NVIDIA GPU. The seam says so as of phase 1 (`osapi.HAS_VSTRT` is False), and the generated script refuses the TensorRT branch before it looks for an engine |
| Packaging / installs | RPM + COPR | per-user installer with its own Python, Qt and VapourSynth (phase 4) | a **Homebrew tap**, written in phase 4 and not published: `packaging/macos/homebrew/` holds four formulae (`buttereye.rb` and one per engine component), and once there is a release to point them at, `brew install <owner>/buttereye/buttereye` is meant to be the whole install. No `.app` bundle, ever — that is a decision about money, not an unwritten feature (below). Nothing is signed or notarised and nothing needs to be, because `brew` fetches over curl and marks nothing with `com.apple.quarantine` (measured). No formula has been installed: each one's `url` names a `v0.0.3` release that does not exist (the newest tag here is `v0.0.2`, which predates the whole macOS port) and each `sha256` is 64 zeros, a placeholder that stops a download rather than passing one. Tagging a release, filling those in and installing the result is still to be done: phase 5 deliberately did **not** do it, because an install writes into the Homebrew prefix and that is the owner's machine to change. The exact commands it would take are under phase 5 below, and `docs/macos-release-bar.md` item 6 is the same question as a verdict |
| `doctor` checks | `rpm -q`, `dnf` lines, `journalctl -k`, sysfs | done: install record, `winget` lines, Vulkan devices (phase 2) | done (phase 2): Homebrew's Cellar read as files, `brew` lines, IOKit GPUs, the loader and MoltenVK under the Homebrew prefix, `kern.bootsessionuuid` for the boot id — and the things a Mac genuinely cannot answer say so rather than naming a Fedora package (GPU fault history is not read yet, TensorRT is permanent). Its one blocking finding, the in-mpv probe (BE-1005), stopped blocking in phase 4, with nothing exported by hand: measured here, a full run goes from `11 ok, 4 info, 1 degraded, 1 blocking` to `13 ok, 5 info, 1 degraded, 0 blocking` and exit code 0, the one degraded row being RIFE not installed. Before it, a full run answered with `sudo dnf copr enable`, `libvulkan.so.1` and `usermod -aG systemd-journal`, and reported ffms2 and the Vulkan loader missing while both were installed |

Short version: **the architecture ports, and on Windows the core's plumbing now does too** (phase 1, below). The core is
a GUI-free asyncio library with the OS-specific parts concentrated in a handful
of modules, and the GUI only talks to `buttereye.core.api`. Those modules were
written for Linux on purpose; the Windows branch now lives in `buttereye.core.osapi`,
and the macOS branch there is one module and a seam (`IS_MACOS`, `_darwin.py`) so
far: phase 0, below.

## What is Linux-specific today

This section describes the 0.0.1 tree. On Windows, phase 1 (below) replaced
everything under "mpv IPC and process control" and "Paths and locks"; on macOS,
phase 1 replaced the parts of those two that are not plain POSIX — the peer
check, the `/proc` read, the pidfd and the runtime directory — and left the
socket, the process group and `flock` as they were. The rest still applies to
both.

These are the places a port would have to touch. File paths are relative to the
repository root.

### mpv IPC and process control

- `buttereye/core/mpvctl/ipc.py` connects to mpv's JSON IPC over a **Unix
  socket** and checks the peer with **`SO_PEERCRED`** (Linux only). The socket
  safety checks (`lstat`, `S_ISSOCK`, owner uid, private parent directory) assume
  POSIX ownership semantics (SCOPE §4.3 "IPC security").
- `buttereye play` uses `--input-ipc-client=fd://N` with a **socketpair**
  (SCOPE §4.3). mpv documents this mechanism for Unix.
- `buttereye/core/mpvctl/launcher.py` starts mpv with `os.posix_spawnp(...,
  setsid=True)`, tracks it through **`/proc/<pid>/stat`**, and stops it with
  `os.killpg` + `SIGTERM`/`SIGKILL`. `buttereye/core/ops.py` and
  `buttereye/core/render/jobs.py` use the same process-group model for
  `vspipe | ffmpeg` pipelines.
- `buttereye/cli/__init__.py` installs asyncio signal handlers
  (`loop.add_signal_handler`), which Windows event loops do not support.

### Paths and locks

- `buttereye/core/paths.py` follows the **XDG** base-directory spec and requires
  `$XDG_RUNTIME_DIR` (a private 0700 directory) for sockets and generated `.vpy`
  files. Neither Windows nor macOS sets it.
- `buttereye/core/profiles/config.py` and `buttereye/gui/app.py` take
  **`fcntl.flock`** locks. `fcntl` does not exist on Windows (macOS has it).

### Detection and diagnostics

- `buttereye/core/hw/detect.py` reads GPUs from **`/sys/class/drm`** plus hwdata
  `pci.ids`; `vulkaninfo` and `nvidia-smi` parts are portable in principle.
- `buttereye/core/doctor/gpufault.py` counts NVIDIA Xid faults in
  **`journalctl -k`** and reads `/proc/sys/kernel/random/boot_id`.
- `buttereye/core/doctor/checks_pkgs.py` and `buttereye/core/setup.py` check
  packages with **`rpm -q`** and tell the user the exact **`dnf`** lines to run;
  `buttereye/core/licences.py` points at `dnf download --source`.

### Development tooling

- `tools/memguard.py` (used by `tests/conftest.py`) runs the test suite and the
  dev tools inside a memory-capped **`systemd-run --user` scope** and reads
  `/proc/self/cgroup`. It already skips itself where no capped scope can be
  made, so it is not a blocker for the application, only for parity of the
  developer workflow.
- `packaging/build.sh` builds Fedora RPMs in **mock**.

### Plugins

- The four VapourSynth plugins ship as Fedora RPMs built in mock. RIFE-ncnn is
  built with a pinned, statically linked ncnn and three local patches (SCOPE §9,
  `packaging/plugins/`; the third is phase 4's, below). None of that output is
  usable off Linux.
- **One defect found off Linux is live on Linux**, and it belongs here rather
  than in the macOS section below. Upstream RIFE-ncnn-Vulkan's
  `load_param_model` (`RIFE/rife.cpp`) builds its two model paths with `sprintf`
  into two fixed `char[256]` **stack** buffers, out of the `modeldir` string
  ButterEye hands it from the user's `model_path`. The threshold is exactly 242
  characters, measured, and past it the plugin does not return an error — the
  process **aborts** (exit 134, `SIGABRT`), taking the whole `vspipe` or mpv
  with it, after `parampath` has already run over into `modelpath`. The branch
  that does this is the `#else` of upstream's `#if _WIN32` split, which is the
  branch Fedora builds today, so this is reachable on Fedora now; 242 characters
  is not far-fetched under a deep user-chosen model folder, a non-default
  package prefix, or `~/Library/Application Support/...` on a Mac. The
  measurement is in `packaging/macos/RIFE-SPIKE.md`, section "The model-path
  abort". **Phase 4 fixed it**, because phase 4 owns packaging:
  `packaging/plugins/buttereye-vs-rife-ncnn-model-path.patch` builds both paths
  as `std::string`, keeps upstream's `#if _WIN32`/`#else` split and touches only
  the `#else` half (the Windows half is
  `buttereye-vs-rife-ncnn-windows-paths.patch`, and this patch leaves it alone).
  It is applied **unconditionally rather than behind a platform guard**, and in
  both ncnn variants of the spec — `%autopatch -p1` covers it with
  `--with system_ncnn`, and the bundled branch, which is the one Fedora builds,
  applies it by number (`Patch2`, `%patch -P2 -p1`). So this is the one thing
  phase 4 changes about the Linux build, deliberately: it turns a crash into a
  path that works. `packaging/macos/build-rife.sh` applies the same three
  patches in the same order.

## Windows: the port, phase by phase

The pieces ButterEye drives all exist on Windows: mpv (with VapourSynth support
in builds that enable it), VapourSynth with `vspipe`, ffmpeg and mkvmerge. The
port is done in four phases; each leaves Linux exactly as it was.

| Phase | What | State |
|---|---|---|
| 1 | Core platform layer: processes, mpv's control channel, folders, locks | **done** (`buttereye/core/osapi/`) |
| 2 | Doctor, hardware detection and setup checks for Windows | **done** |
| 3 | Windows builds of the four VapourSynth plugins | **done** (`packaging/windows/`) |
| 4 | Installer | **done** (`packaging/windows/build-installer.sh`); real-Windows testing and a CI build to do |

### Phase 1: the core platform layer (done)

Every call that differs between the two systems now goes through
`buttereye.core.osapi`; nothing else in the core touches POSIX directly.
`mypy --platform win32 buttereye` is clean, which is the check that nothing was
missed.

| | Linux | Windows |
|---|---|---|
| mpv control | Unix socket, `SO_PEERCRED` | named pipe `\\.\pipe\buttereye-<hash>`; the pipe's server process must run as this user (its token SID). A marker file in the runtime folder stands for the pipe, so discovery and cleanup work unchanged |
| mpv lifetime | `posix_spawnp(setsid)`, pidfd | `Popen` that breaks away from ButterEye's job; polled |
| process groups | new session, SIGTERM then SIGKILL | the leader's process tree. ButterEye runs in a kill-on-close job object, so a crashed window never leaves a render or a probe running. The polite stop closes the tree's windows (mpv quits normally); a tree without windows is terminated at once |
| folders | XDG | settings in `%APPDATA%\ButterEye`, the rest in `%LOCALAPPDATA%\ButterEye` (`data`, `cache`, `state`, `run`) |
| private folder check | 0700, own uid, not a symlink | owned by this user's SID, not a reparse point (junction or link) |
| locks | `flock` | `msvcrt.locking` on a lock file |
| storage cleaning | directory file descriptors with `O_NOFOLLOW` | `lstat` walk; links and junctions are removed as links, never followed |
| render priority | renice the group | below-normal priority class at spawn |

The plugin and model folders on Windows sit next to the installation
(`BUTTEREYE_HOME`, else the folder above the package): phase 4 fixes that layout.

### Phase 2: checks and setup (done)

The doctor, hardware detection and setup now give Windows answers; no Windows
finding carries a `dnf`, `sudo` or `rpm` line (a test checks that).

| Check | Linux | Windows |
|---|---|---|
| Installed plugins and licences | `rpm -q` | the install record `buttereye-install.json` that the installer writes next to ButterEye (`checks_pkgs.manifest_db`); a missing piece says to run the installer again (Repair) |
| Plugin files | `librife.so`, `mvtools.so`, `libmvgpu.so` | the same names with `.dll` (`osapi.plugin_file`); the generated scripts map the name themselves |
| Tools ButterEye doesn't ship | `sudo dnf install …` | `winget install --exact --id …`: mpv `shinchiro.mpv`, VapourSynth `VapourSynth.VapourSynth`, ffmpeg `Gyan.FFmpeg`, mkvtoolnix `MoritzBunkus.MKVToolNix`, 7-Zip `7zip.7zip` (`commands.install_command`) |
| GPUs | sysfs + `pci.ids`, then Vulkan and `nvidia-smi` | the physical Vulkan devices, then `nvidia-smi`; `vulkaninfo.exe` from the install folder first |
| Vulkan loader and drivers | `libvulkan.so.1`, ICD folders | `System32\vulkan-1.dll`; drivers are found by the device list (no ICD folders to read); fixes say to update the graphics driver |
| GPU fault history | NVIDIA Xid lines in `journalctl -k` | not checked (an INFO row says so): Windows logs GPU faults in its event log, which ButterEye doesn't read yet |
| TensorRT (experimental) | vs-mlrt's vstrt built by `contrib/build-vstrt.sh` | not available on Windows yet (an INFO row; never selected) |
| mpvSockets conflict | checked | not applicable (the script needs Unix sockets) |
| Setup's command list | the `dnf` lines | the `winget` lines |

`tests/windows` now also runs the whole doctor against a fake install under
Wine, with a real `mpv.exe`.

### Testing the Windows code

`tests/windows/` exercises the Windows half against the real Windows API: job
objects, process trees, named pipes with the peer check, locks, junction-safe
cleaning, settings saved over an existing file, and a real `mpv.exe` driven
over its pipe. On Windows it runs with plain `pytest`; on the Linux dev box,
`tools/wine-tests.sh` runs it under Wine with a Windows Python (one-time setup
in the script's header). Wine is a stand-in, not proof; GitHub Actions now runs
the same tests on real Windows (below, "On real Windows: GitHub Actions"), but
without a GPU, so a desktop PC test is still due before any release.

### Phase 3: the plugins (done)

`packaging/windows/build-plugins.sh` cross-compiles MVTools, RIFE-ncnn-Vulkan and
mvgpu on the Fedora dev box (a mock chroot with Fedora's MinGW-w64 toolchain) from
the same sources and patches as the COPR packages. Each is one self-contained DLL
that imports only Windows itself (and `vulkan-1.dll`, from the GPU driver); the
plugins use only VapourSynth's C API, so they load into the official Windows
VapourSynth. Details in `packaging/README.md`.

Under Wine, with VapourSynth R81 for Windows and the host GPU passed through:

- all three load, and each moves a textured square to where it really is in the
  in-between frame (the same check as the Linux mvgpu test); the Windows RIFE build
  matches the Linux one frame for frame;
- ButterEye's own speed-test script runs each engine through `vspipe.exe`.

Found and fixed on the way: RIFE's Windows model loader built its paths with
`swprintf("%s")`, which under MinGW reads a narrow string, so the model folder was
cut to its first letter and every in-between frame came out black, with no error.
`buttereye-vs-rife-ncnn-windows-paths.patch` builds the paths as strings instead.

Phase 4 added `ffms2.dll` and `vulkaninfo.exe` to the same build (below).

### Phase 4: the installer (done; Wine and CI-tested)

`packaging/windows/build-installer.sh` makes `ButterEye-<version>-setup.exe` on the
Fedora dev box. The installer needs nothing on the PC first: it brings its own
Python, Qt and VapourSynth, so a user installs ButterEye and mpv
(`winget install shinchiro.mpv`) and nothing else. Microsoft's Visual C++ runtime is
the one exception (below).

**What it installs**, per user and without administrator rights, into
`%LOCALAPPDATA%\Programs\ButterEye` (or a folder you choose):

| Folder | What | Size |
|---|---|---|
| `python\` | Python 3.14.8 (the official embeddable build) with PySide6-Essentials 6.11.2 and VapourSynth R81 from their official Windows wheels | 84 MiB |
| `buttereye\` | ButterEye itself | 3 MiB |
| `vapoursynth\` | the three plugins (phase 3) and `ffms2.dll` | 39 MiB |
| `share\rife-ncnn-models\` | the RIFE models | 31 MiB |
| `vulkaninfo.exe` | the GPU list | 2 MiB |
| `licenses\` | every licence text, `SOURCES.txt` (the source offer) | 1 MiB |
| `buttereye-install.json` | the install record the doctor reads (phase 2) | |
| `buttereye.cmd` | the command line: `buttereye.cmd doctor`, `buttereye.cmd play VIDEO` | |

159 MiB on disk (the prototype was 370 MiB: Qt is trimmed to the modules ButterEye
uses, Core, Gui, Widgets, Svg and SvgWidgets with the Windows platform, style and SVG
plugins only, without the wheels' copies of Microsoft's C++ runtime, and VapourSynth's
debug symbols are left out); the installer is 63 MiB. A Start-menu entry "ButterEye"
starts the window without a console (`pythonw -m buttereye.gui`); a desktop shortcut
is an option. The
uninstaller is registered for this user (Settings ▸ Apps) and removes only what the
installer put there; settings and history in `%APPDATA%` and `%LOCALAPPDATA%` stay.
Running the installer again over an installation repairs it (no file of the earlier
version survives). It refuses a folder that already holds other files.

One installation per user: the uninstall entry and the shortcuts belong to the folder
the entry names. The installer refuses a second copy in another folder while the
first is installed (uninstall it, or choose its folder to repair it), and an
uninstaller run for another folder leaves the entry and the shortcuts alone. Repair,
upgrade and uninstall first make sure nothing runs from the folder: a running
ButterEye holds a named mutex for its install folder (`osapi.install_mutex_name`,
`Local\ButterEye-` and the folder's long path in lower case, `/` for `\`; the NSIS
script builds the same name), and a player it started (mpv) still has VapourSynth's
DLLs loaded, which the installer notices because they can't be opened for writing
and deleting. It asks to close them and retry; silent (`/S`), it stops with exit code
4 and changes nothing. Exit codes: 0 done, 2 not here (Windows too old, a folder with
other files, ButterEye installed in another folder), 4 in use.

How the pieces find each other:

- **VapourSynth.** The VapourSynth package sits inside the bundled Python, where its
  "implicit config" finds that Python by itself: no registry entries, no
  `vapoursynth config`. Windows mpv builds load VapourSynth at run time from
  `VSSCRIPT_PATH` (else the registry, else the DLL search), so on Windows
  `ButterEye.open()` sets `VSSCRIPT_PATH` to the bundled `vsscript.dll` and puts the
  Python folder and the VapourSynth folder first on `PATH`
  (`osapi.use_bundled_runtime`). Every child then uses the bundle: mpv's filter, the
  doctor's probe, setup's smoke test, the speed test, and `vspipe` for offline render.
  `vspipe` is started by the full path of the bundled `vspipe.exe`, never looked up
  by name. Windows looks for a bare program name in the current folder before `PATH`
  (a `vspipe.exe` or `mpv.exe` in Downloads would win), so ButterEye sets
  `NoDefaultCurrentDirectoryInExePath=1` for itself and its children, and
  `osapi.executable` only returns absolute paths found in absolute `PATH` folders.
  Linux is untouched.
- **ButterEye's folder.** Python's `._pth` file lists the install folder, so the
  `buttereye` package is importable and `paths.install_dir()` (the folder above the
  package) is the install folder; `BUTTEREYE_HOME` is not needed.
- **vulkaninfo.** Started without a console, as ButterEye starts its helpers, plain
  `vulkaninfo` ends by waiting forever for its console to close (upstream behaviour
  of every build, the one a GPU driver installs too), so the GPU list always timed
  out. On Windows ButterEye now runs `vulkaninfo --text -o <file>` and reads the
  file.

**Built here, from source.** `build-plugins.sh` now also makes, in the same MinGW
chroot and with the same "imports only Windows" check:

- `ffms2.dll`: FFMS2 5.0 with FFmpeg 8.1.3 (decoders, demuxers and parsers only: no
  encoders, filters, network or GPL parts, so it stays LGPL-2.1-or-later), dav1d 1.5.4
  for AV1 and zlib, all linked in. Under Wine it reads H.264, 10-bit HEVC and AV1.
  The official FFMS2 Windows build was not used: it is GPLv3 (FFmpeg with OpenSSL)
  and its exact source can't be reconstructed, so its source offer couldn't be met.
- `vulkaninfo.exe`: Vulkan-Tools 1.4.328.1 with volk, linked statically. Upstream sets
  up its user32 functions only for MSVC builds, so every MinGW build (Fedora's
  `mingw64-vulkan-tools` too) crashes on Windows;
  `packaging/windows/vulkaninfo-mingw-user32.patch` fixes that.

Every download is pinned by SHA-256 in `packaging/windows/pins.sh`, and a mismatch
stops the build.

**Microsoft's Visual C++ runtime.** VapourSynth needs it (14.50.35719 or newer) and
it only installs machine-wide, which takes administrator rights once. It can't be
put next to ButterEye instead: mpv loads VapourSynth into its own process. Qt uses
the same system runtime: the PySide6 and Shiboken6 wheels each carry a copy
(`msvcp140*.dll`, `concrt140.dll`, `vcomp140.dll`, ...), and the installer leaves both
out, so without the runtime the window doesn't open either: it shows a message box
naming the missing runtime, its download link and the `winget` line instead (the
command line and its doctor still run). The only Microsoft DLLs shipped are `python\vcruntime140.dll` and
`vcruntime140_1.dll`, part of Python's official embeddable build, redistributed under
the conditions in its `LICENSE.txt` (`licenses\python\MICROSOFT-VCRUNTIME.txt`; the
install record lists them as `microsoft-vcruntime`); `winstage.py check-imports`
refuses any other. Those conditions require end users to agree to terms protecting
the files at least as much as Microsoft does: the installer's second licence page
shows `packaging/windows/MICROSOFT-TERMS.txt` with an "I accept" box (owner decision,
2026-10-11; a silent install accepts them, as the text says), and the text is
installed as `licenses\python\MICROSOFT-TERMS.txt`. When it is
missing or too old, the installer offers to download it from Microsoft and Windows
asks for permission; a silent install (`/S`) leaves it alone. The file goes to the
installer's own temporary folder and is held open (readable, not writable or
deletable) from its check until it has run. The check, by Windows PowerShell started
by its full path, accepts only a valid Authenticode signature by Microsoft Corporation
chained to Microsoft's root, on a file whose version information says it is
Microsoft's x64 Visual C++ redistributable (`VC_redist.x64.exe`); an embedded copy
(`VCREDIST=`) is checked the same way. Either way the doctor checks it
(`packages.vcruntime`, with the `winget` line). The redistributable is not bundled:
Microsoft allows that only to licensed Visual Studio users (`VCREDIST=` in
`build-installer.sh` embeds it if the owner decides to).

**Licences and source.** `licenses\<component>\` holds the texts of every bundled
part: ButterEye (`COPYING`, `LICENSES\`; the FSRCNNX shader's LGPL-3.0 is in
`buttereye\data\shaders\`), Python, PySide6/Shiboken6/Qt (Qt's LGPL-3.0 and the
GPL-3.0 it builds on, and in `licenses\pyside6\third-party\` the notices of the
third-party code inside the shipped Qt binaries, FreeType, HarfBuzz, PCRE2, zlib,
libpng, md4c, double-conversion and the rest, taken from the pinned qtbase and qtsvg
sources' `qt_attribution.json`), VapourSynth, the plugins, ffms2 with FFmpeg, dav1d
and zlib, Vulkan-Tools with volk, and the MinGW-w64 runtime. The wheels' own licence
file is Qt's commercial licence, which doesn't apply to this copy: the trim replaces
it with a note saying which licence does. `licenses\SOURCES.txt` says where each
part's source is: `ButterEye-<version>-windows-sources.tar`, made next to the
installer, holds the complete source of everything built for it, with the build
scripts (`packaging/`, `mvgpu/`), fftw's Fedora source package and the models
archive; Python, Qt/PySide6 (Core, Gui, Widgets, Svg, SvgWidgets and the
`qwindows`, `qoffscreen`, `qminimal`, `qmodernwindowsstyle`, `qsvgicon` and `qsvg`
plugins, all from qtbase and qtsvg) and VapourSynth are unmodified official builds,
listed with their source download pages and a written offer. In the app, the
Licences tab reads the install record: on Windows FFmpeg is the LGPL build inside
`ffms2.dll`, with rows of their own for dav1d, zlib, fftw and volk, and the About
tab's source line is `licenses\SOURCES.txt`.

**Minimum Windows:** 64-bit Windows 10 version 1903 or newer (Qt 6.11 uses the ICU
that ships with Windows from 1903); the installer refuses older versions.

**Verified under Wine 11** (dev prefix with the host's RTX 4090 passed through):

- `setup.exe /S /D=<folder>` installs; `Uninstall.exe /S` removes the folder, the
  Start-menu entry and the registry entry, and leaves a file of the user's alone;
  installing over an installation removes a stale file; a folder with other files
  is refused (exit code 2). The test installs into a folder with a space and a
  non-ASCII letter (`Butter Eye ñ`, `/D=` last and unquoted), where RIFE loads its
  model too; it skips when this user already has a ButterEye registered elsewhere.
- One installation per user: a second copy in another folder is refused (exit code
  2) and an uninstaller run for another folder leaves the entry and the shortcut.
  Repair and uninstall stop with exit code 4 and change nothing while the installed
  ButterEye's core runs, while a process holds its mutex name (the name the NSIS
  script builds is the one `osapi.install_mutex_name` gives), and while a plain
  process has the bundled Python's DLLs loaded; once it has ended, the repair works.
- The installed runtime alone (this test's own Python taken off `PATH`, a fresh
  profile): `buttereye.cmd doctor --json` with stdout a pipe reports the plugins,
  models, licence files, ffms2, the Visual C++ runtime and the RTX 4090 OK, and with
  mpv on `PATH`, the in-mpv probe passes (exit code 0). The bundled `vspipe.exe` runs
  ButterEye's speed-test script with MVTools, mvgpu and RIFE; the bundled ffms2
  reads a video. With the runtime's registry key removed, a silent install still
  succeeds and the doctor blocks with the `winget` line.
- `buttereye.cmd play` from a fresh profile, mpv on `PATH`: "23.976 → 47.952 fps with
  mvtools (active, ok)", and RIFE active with the default profile.
- The window, from the installed Python with Qt trimmed, opens and draws (in a Wine
  prefix with a stand-in `icuuc.dll`, which Wine lacks; in `tests/windows` that test
  skips under Wine).

`tests/windows/test_installer_windows.py` does all of the above except the play and
GUI-window runs; `tools/wine-tests.sh` runs it when the installer exists.

**Not verified under Wine** (the next section says what GitHub's Windows machines
have covered since: Qt with Windows' own `icuuc.dll`, among others):

- anything on real Windows: SmartScreen (the installer isn't code-signed), Defender,
  the Visual C++ download and its permission prompt (Wine has neither PowerShell nor
  UAC), the installer's pages, shortcuts and Settings ▸ Apps entry as a user sees them;
- on-screen playback and its timing (Wine was only
  run with `vo=null`); fonts (the interface's typefaces are Linux packages; Windows
  falls back to its own);
- whether `winget install shinchiro.mpv` puts `mpv.exe` on `PATH` as ButterEye expects;
- offline render end to end (ffms2 reads video, but ffmpeg and mkvmerge were not
  installed in the test prefix).

### On real Windows: GitHub Actions

`.github/workflows/windows.yml` (since 2026-10-11) builds the installer in a
`fedora:44` container with the same scripts as the dev box (`--no-mock`), then on
`windows-latest` installs, tests and uninstalls it. First green run:
<https://github.com/MiguelCarino/ButterEye/actions/runs/38159088098>, on
**Windows Server 2025 Datacenter 10.0.26100** (Hyper-V VM, AMD EPYC, 4 threads, no
GPU; Visual C++ runtime 14.51 already present), with shinchiro's mpv 0.41.0 build
(20261011, pinned by SHA-256).

**Verified there:**

- `setup.exe /S` and `/S /D=<folder>`: the files, the install record with every
  component's licence files, the uninstall entry and the Start-menu shortcut;
  the default folder is `%LOCALAPPDATA%\Programs\ButterEye` (535 files, 167 MiB once
  the installer has compiled the `.pyc` files). `Uninstall.exe /S` removes the
  folder, the entry and the shortcut.
- The installed `buttereye.cmd doctor --json`, with stdout a pipe and no other
  Python on `PATH`: plugins, models, licence files, Visual C++ runtime and ffms2
  OK; mpv's VapourSynth filter OK; the in-mpv probe runs the bundled VapourSynth
  R81 with Python 3.14.8 and loads RIFE-ncnn-Vulkan and MVTools; exit code 0.
  Plain `doctor --verbose` through a pipe prints UTF-8.
- The bundled `vspipe.exe` runs ButterEye's speed-test script with MVTools; MVTools
  interpolates the moving-square test correctly; all three plugin DLLs load into
  VapourSynth; the bundled ffms2 reads an H.264 video (made by mpv on the runner).
- Qt from the installed Python (`buttereye.gui --version`) with Windows' own
  `icuuc.dll`.
- All of `tests/windows/test_osapi_windows.py` run by the **installed** ButterEye's
  own Python: job objects, process trees, named pipes with the peer check, locks,
  junction-safe cleaning, atomic saves, the whole doctor, and a real `mpv.exe`
  driven over its pipe.

**Verified there since** (run
<https://github.com/MiguelCarino/ButterEye/actions/runs/38165626433>, same runner
image, after the licence and installer-safety fixes; the installer test 13 passed and
2 skipped, the installed Python's `tests/windows` 24 passed and 5 skipped):

- The install folder without the wheels' copies of Microsoft's C++ runtime: the only
  Microsoft DLLs in it are `python\vcruntime140.dll` and `vcruntime140_1.dll`, and Qt
  loads with the system's runtime (14.51): `buttereye.gui --version` (its `Source:`
  line is `licenses\SOURCES.txt`) and an SVG icon rendered offscreen through the
  shipped `qsvg`/`qsvgicon` plugins. The install record lists `python3-shiboken6`,
  `microsoft-vcruntime` and Qt's third-party notices, every licence file present.
- Installing into a folder with a space and a non-ASCII letter (`Butter Eye ñ`, `/D=`
  last and unquoted): every test above ran from there.
- One installation per user: a second copy in another folder is refused (exit code
  2) and an uninstaller run for another folder leaves the entry and the shortcut.
- In use: repair and uninstall stop with exit code 4 and change nothing while a
  process holds the folder's mutex name, while the installed core runs, and while a
  plain process has the bundled Python's DLLs loaded; once it has ended, the repair
  works.
- `osapi.executable` ignores a program planted in the current folder.

**Found and fixed by it:** on Windows Server (and in any elevated process) new
folders are owned by the Administrators group, not the user, so ButterEye refused
its own fresh runtime folder (BE-2010) and nothing ran; `osapi.owned_by_me` now
accepts the token's default owner too.

**Skipped there, and why:** the runner has `vulkan-1.dll` but no Vulkan driver
(`vkCreateInstance` returns -9), so RIFE and mvgpu are only loaded, never run (the
interpolation and speed-test tests skip with that reason; the doctor reports
"Vulkan did not start"); the windowed mpv test (no desktop session to look at);
offline render (no ffmpeg or mkvmerge on the runner, and the doctor says so).

**Still not verified, needs a real Windows desktop PC with a GPU:** RIFE and mvgpu on
a real GPU under Windows (only under Wine so far), on-screen playback and its timing,
the GUI window as a user sees it, the installer's pages, SmartScreen and Defender on an
unsigned installer, the Visual C++ download and its permission prompt (the runner
already had the runtime, so the PowerShell check of the download's Microsoft
signature and version information has never run, nor has the same check of an
embedded `VCREDIST=` copy), RIFE loading its model from a non-ASCII folder (only
under Wine; the runner can't start Vulkan), the window without the Visual C++
runtime (it shouldn't open; the command line should still run), fonts, `winget install shinchiro.mpv` and `PATH`, and a
non-administrator account (the runner's user is an administrator).

### Remaining work

1. **A real Windows desktop PC with a GPU** before any release (the list just
   above), then code signing.
2. **Later, not scheduled:** the TensorRT path on Windows (vs-mlrt's Windows
   releases), and reading GPU faults from the Windows event log.

## macOS: the realistic path

macOS is closer to Linux for the core and further away for the GPU work. It is
**not a supported platform**, and phases 0 to 5 do not make it one: what they
make is a gate, a platform layer, a doctor that tells the truth, two built
plugins, live smoothing that works on the machine it was measured on, a tap
nobody has installed, and — phase 5 — a written record of every number with the
machine it came from and of everything that still has no number.
`mypy --platform darwin buttereye` is
clean, as `linux` and `win32` already were, which is what stops a later phase
quietly breaking either supported platform. `tests/macos` and `tests/boundary`
pass. `tests/core` did not when phase 1's platform layer first landed: twelve of
its tests assert the Linux folder layout, the `XDG_RUNTIME_DIR` refusal or Linux
plugin file names, and met the macOS answers instead. Phase 1, below, lists them
and what each one needed. `tools/macos-tests.sh` is what says whether the gate is
green; this page does not.

`.github/workflows/macos.yml` runs the type check and the test suite on
GitHub's Apple Silicon runners, and installs nothing from Homebrew: no mpv, no
VapourSynth, no Vulkan loader, and it builds no plugin and has never installed
the tap, so the tests that need any of those skip there rather than fail. Every
measurement on this page comes from a developer's Mac that does have them, and
none of it was ever proved in CI — the job's own header now says so, item by
item (phase 5, below).

The measurements below come from one machine: macOS 27.0.1 on an Apple M2
(arm64), Homebrew's python3.14. **arm64 is the only target measured**, and Intel
Macs are out of the first cut: Homebrew's prefix there is `/usr/local`, which
also sits inside the protected `/usr` root `buttereye/core/storage.py` refuses
to clean, where `/opt/homebrew` does not.

| Phase | What | State |
|---|---|---|
| 0 | The `IS_MACOS` seam, the three live defects, `tests/macos` and CI | **done** (`buttereye/core/osapi/_darwin.py`) |
| 1 | The platform layer becomes three-valued: libproc in place of `/proc`, the per-user temporary directory in place of `$XDG_RUNTIME_DIR`, `~/Library` folders, `.dylib` plugin names, a kqueue in place of a pidfd | **done** (`buttereye/core/osapi/_darwin.py`, `paths._resolve_macos`) |
| 2 | Doctor, detection and setup speak macOS: Homebrew's Cellar in place of `rpm -q`, `brew` lines in place of `dnf` ones, `system_profiler`/`ioreg` in place of sysfs, the Vulkan loader and MoltenVK under the Homebrew prefix, and the real reason the in-mpv probe fails | **done** (`buttereye/core/doctor/brew.py`, `checks_pkgs.brew_db`, `detect.read_iokit_gpus`) |
| 3 | The plugins: `libmvgpu.dylib` built natively for arm64 and reaching MoltenVK, MVTools and ffms2 taken from Homebrew rather than built, RIFE measured in a spike | **partly done**: mvgpu is built and its frame is bit-exact, MVTools and ffms2 need no ButterEye build at all, and RIFE builds and interpolates correctly in a spike (`packaging/macos/RIFE-SPIKE.md`) that phase 3's build path did not run, so after phase 3 a Mac had no RIFE engine — phase 4's `build-rife.sh` is what builds it (`packaging/macos/build-plugins.sh`, `packaging/macos/check-dylibs.sh`) |
| 4 | Live smoothing — mpv is handed the VSScript file it cannot find for itself — RIFE built at last, a Homebrew tap, and the cross-platform model-path crash patched | **the code is done and measured here; the tap is written and unverified**: `tests/integration/test_hdr_live_real.py` is 3 passed with nothing in the environment and the doctor's in-mpv probe passes with no blocking row (`osapi.use_bundled_runtime`, `osapi.vsscript_library`, `osapi.point_at_vsscript`, `tests/macos/test_runtime_macos.py`); `packaging/macos/build-rife.sh` builds RIFE and `check-dylibs.sh` passes it; `packaging/macos/homebrew/` holds the tap, whose `url`s name a release that does not exist and whose `sha256`s are placeholders, and **no formula has been installed, bottled or audited anywhere**, which is why every claim about what an install does is marked unverified below |
| 5 | The real-hardware gate: measure what no test suite can see — the engine ButterEye picks on an Apple GPU, the GUI as a user meets it, the tap, and benchmark numbers from this machine rather than the dev box's — then write down what is still unmeasured | **the measuring is done; macOS is still not supported**, which is the only thing "done" can mean for a phase that builds nothing. The GUI has now been seen on a Mac (`tools/screenshots.py` is three-platform and `docs/design/screenshots/macos/` holds the 18 views), every measured number is collected with its machine in one table below, and what this machine cannot measure — a cross-UID socket refusal, `dirhelper`'s three-day sweep, `flock` on a network home, a sandboxed temporary directory, any Intel Mac, a Linux frame beside a macOS one, and a real `brew install` — is written down with what each would take. Whether any of it is released is the owner's decision (SCOPE §2, §13), not this phase's |

- **Core.** Unix sockets, `posix_spawn`, process groups and `fcntl` are all
  there. The two things that were missing — a private runtime directory without
  `$XDG_RUNTIME_DIR`, and macOS paths (`~/Library/...`) — are phase 1, below.
- **Peer credentials** take three constants Python does not export. The uid
  comes from `LOCAL_PEERCRED` (level 0, an `xucred` of 76 bytes read as
  `=IIh`, whose `cr_version` must be 0 before `cr_uid` is worth trusting) and
  the pid from `LOCAL_PEERPID` (level 0, option `0x002`). `os.getpeereid` does
  not exist in CPython on macOS, and neither call yields a pid on its own, so
  `getpeereid`/`LOCAL_PEERCRED` was never the whole answer. One macOS-only
  failure mode: once the peer has gone, `LOCAL_PEERPID` fails with `ENOTCONN`,
  where Linux's `SO_PEERCRED` keeps answering from its cache. That is the third
  phase-0 defect: the connect retry in `buttereye/core/mpvctl/launcher.py` now
  waits out that reset as it already waited out a socket nobody serves yet.
- **Process inspection without `/proc`** is one call site, `osapi.pid_alive`,
  whose `OSError` fallback silently reported every zombie as alive: a
  reaped-but-unwaited mpv answers `os.kill(pid, 0)` while `sysctl` says
  `p_stat` is `SZOMB`. The answer is libproc's `proc_pidinfo` (1.04 µs a call,
  against 2.25 ms for `ps`); phase 1 did that.
- **pidfd** was never a blocker: `os.pidfd_open` does not exist on macOS, the
  `not hasattr` arm already returned `None`, and the Windows-style poll fallback
  already worked. Phase 1 took the exact equivalent anyway — a kqueue holding
  one `EVFILT_PROC`/`NOTE_EXIT` filter — because it works for a pid ButterEye is
  not the parent of, which is the shape of an mpv it attached to.
- **The plugin extension is `.dylib`**, from VapourSynth R81's own
  `VSCore::libraryExtension` (`src/core/vscore.cpp`), which governs both a
  manifest load and the extension filter of the autoload directory. Only the
  extension changes: Homebrew's files are `mvtools.dylib` and `libffms2.dylib`,
  the same stems Linux has. ButterEye keeps **four** copies of that rule, and
  phase 1 gave all four the third value: `osapi.plugin_file`, and the plugin
  loader of each generated script — `_load` in
  `buttereye/core/scriptgen/template.vpy`, `load` in
  `buttereye/core/bench/bench.vpy` and `_load` in
  `buttereye/core/doctor/smoke.vpy`. The three copies exist because those
  scripts run inside VapourSynth's Python, not ButterEye's, where `os.name` is
  `posix` and would have asked for the Linux name.
- **GPU.** macOS has **no native Vulkan**. RIFE-ncnn-Vulkan and `mvgpu` can
  only run through **MoltenVK** (Vulkan on Metal). For `mvgpu` that path is
  measured and, as of phase 3, taken: `Gpu::init` makes the plain instance call
  first and asks for the portability extension and its flag only when the loader
  refuses that call, and the plugin builds, loads and renders a bit-exact frame.
  For RIFE the path is measured too, in a spike
  (`packaging/macos/RIFE-SPIKE.md`): it builds and interpolates correctly
  through MoltenVK, but nothing in the build path produces it, so a Mac has no
  RIFE engine.
  A native Metal port of `mvgpu` would be a separate engine, and this box could
  not build one anyway: there is no `metal` compiler without the full Xcode
  (phase 3, below). **TensorRT is not available on
  macOS** and permanently cannot be: there is no CUDA or TensorRT for macOS, and
  no Apple Silicon Mac has an NVIDIA GPU. MVTools (CPU) is the only backend with
  no GPU question, and Homebrew already ships it.
- **mpv/VapourSynth.** Both are Homebrew bottles, and the formula's `mpv`
  carries the `vapoursynth` filter while the app bundle from mpv.io does not
  (the summary table has the detail). ButterEye's F1 checks already refuse an
  mpv without it.
- **Detection and doctor** need macOS sources (`system_profiler`, IOKit) in
  place of sysfs, rpm and the journal. Phase 2, below, did that: Homebrew's
  Cellar is the install database, `SPDisplaysDataType` and `AGXAccelerator`
  are the GPU list, and the two things a Mac cannot answer — the GPU fault
  history and TensorRT — say which of the two they are.

Two numbers nothing else in the repository writes down:

- **An `AF_UNIX` path is at most 103 bytes.** macOS's `sun_path` is 104 bytes
  (`sun_path[104]` in the SDK header; bisecting real binds agrees), against
  Linux's 108. `mpv-<12 hex>.sock` is 21 bytes, which leaves 82 for the runtime
  directory. Past that the failure surfaces as an `OSError` in mpv's own log,
  not in ButterEye.
- **The per-user temporary directory is garbage-collected on age**, not
  "cleared at logout": `dirhelper`
  (`/System/Library/LaunchDaemons/com.apple.bsd.dirhelper.plist`) runs daily at
  03:35 and deletes what is older than three days
  (`CLEAN_FILES_OLDER_THAN_DAYS=3`). Phase 1 puts ButterEye's runtime directory
  there, so this is now a cost it carries, written up under phase 1 below.
  `storage.py` used to tell every user "cleared at logout"; it now says "cleared
  once three days old" on macOS and "nothing clears these for you" on Windows,
  where the folder is part of the user's profile and the old wording was simply
  wrong.

### Phase 1: the platform layer becomes three-valued (done)

The seam is three-valued, and the core's hot paths all go through it: process
liveness, the private runtime folder, mpv's control channel, locks, plugin file
names and durable renames ask `buttereye.core.osapi` rather than POSIX. Linux-only
calls do remain in the core, every one of them in the doctor and detection modules
a later phase owns; "What phase 1 does not do" below names each file and its phase.
`mypy --platform darwin buttereye` is clean, as `linux` and `win32` are, and
`tests/macos` covers the four new calls and the folder layout on the machine
they are written for.

**The platform layer first landed with twelve `tests/core` failures on a Mac**,
all of one kind: a test in a file phase 1 did not own asserts a Linux answer that
is now a macOS answer.

- five in `tests/core/test_paths.py`, which check the XDG table and the
  `XDG_RUNTIME_DIR` refusal against `resolve` — and `resolve` takes the
  `_resolve_macos` branch here, where there is nothing to refuse;
- four in `tests/core/test_setup_cancel.py`: three through an `only_logs`
  helper that expects `state/buttereye/logs` and finds `Library/Logs/ButterEye`
  (the log went where it should; the helper looks in one place), and one
  because `mpv_include` is now inside `$HOME`, so the `include=` line comes
  back abbreviated as `~/Library/...` where the test's own comment reads "XDG
  config is outside HOME";
- three in `tests/core/test_doctor_fixtures.py`, whose fixture install plants
  `librife.so` and `mvtools.so` while the doctor now asks `osapi.plugin_file`
  and looks for `.dylib`, so it reports them missing and blocks.

None of the twelve is a defect in the platform layer, and none may be made to
pass by weakening an assertion. The plugin-name ones are fixed by asking the seam:
a fixture that wants a plugin's file name calls `osapi.plugin_file` for it, which
on Linux is the identity function and hands back exactly the `.so` the fixture
hardcoded before, so Linux behaviour does not move and the test keeps testing what
it tested. The folder-layout ones are genuinely about the Linux XDG table and the
`XDG_RUNTIME_DIR` refusal, so they need a platform-aware expectation rather than a
seam call. A red gate is why phase 1 did not land the first time, and
`tools/macos-tests.sh` — not this page — is what says it is green.

`_darwin.py` holds four functions in 222 lines, against `_win32.py`'s 394, and
that is the shape of the problem rather than less care: macOS is POSIX. The
process group already does what a Windows job object is for, signals reach mpv
so there is no `WM_CLOSE` problem, a uid comparison replaces a token SID, and
`flock` replaces a named mutex. Most rows below are the Linux code, unchanged.

| | Linux | macOS |
|---|---|---|
| mpv control | Unix socket, `SO_PEERCRED` | the same Unix socket. The peer's uid comes from `LOCAL_PEERCRED` (an `xucred`, trusted only when `cr_version` is 0) and its pid from `LOCAL_PEERPID`; Python exports `socket.LOCAL_PEERCRED` but neither `SOL_LOCAL` nor `LOCAL_PEERPID`, so both are spelled out in `_darwin.peer_cred`. It fails closed — a short answer, an unexpected version, or any errno but `ENOTCONN` refuses the peer rather than raising. `ENOTCONN` means there is no connected peer (it has gone, or there never was one) and becomes a `ConnectionResetError` the launcher's connect retry waits out. Phase 0 |
| process liveness | `os.kill(pid, 0)`, then state `Z`/`X` in `/proc/<pid>/stat` | the same `os.kill` first — it is the only thing that answers for another user's process, where macOS gives `EPERM` and world-readable `/proc` gives a state — then libproc's `proc_pidinfo` with `PROC_PIDTBSDINFO` (`_darwin.pid_alive`), 1.04 µs a call. It fails with `ESRCH` for a zombie, which is exactly what the `/proc` read computes. `ps` was rejected on more than its 2.25 ms: `ps -o comm=` prints whatever `argv[0]` says (`exec -a averylongname /bin/sleep` reports the fake name, while `proc_pidpath` still reports `/bin/sleep`), and SCOPE §4.3's threat is a same-uid process pretending to be mpv |
| mpv lifetime | `posix_spawnp(setsid)`, a pidfd the loop watches | the same spawn; `os.pidfd_open` does not exist, so `osapi.DetachedChild.open_pidfd` returns a kqueue carrying one `EVFILT_PROC`/`NOTE_EXIT` filter (`_darwin.exit_watch`). It needs no parent relationship — measured against an orphan reparented to `launchd`, which is the shape of an mpv ButterEye attached to rather than started — and it is level-triggered, so the descriptor stays readable without the event being dequeued. macOS therefore does **not** fall back to the 10 Hz poll; the poll is still what answers when the filter cannot be registered |
| process groups | new session, `SIGTERM` then `SIGKILL` | the same, unchanged. One trap, left alone deliberately: `create_subprocess_exec(process_group=<leader>)` fails with `PermissionError` on macOS when the leader was spawned with `start_new_session=True`, so the `vspipe`/`ffmpeg` pipeline in `render/jobs.py` keeps its explicit `process_group=0` and `process_group=<vspipe pid>` on the POSIX side instead of taking `osapi.group_spawn_kwargs` |
| folders | XDG | the folders a Mac application uses: settings, data and state under `~/Library/Application Support/ButterEye`, caches under `~/Library/Caches/ButterEye`, and logs under `~/Library/Logs/ButterEye` rather than inside the state folder as on Linux — `~/Library/Logs` is the one place Console.app reads, and a log is the thing a user is asked to hand over. `paths._resolve_macos` does not read the XDG variables for ButterEye's own folders: a Mac user who exports them for other tools did not ask ButterEye to move |
| the user's mpv config | `~/.config/mpv/mpv.conf` | `~/.config/mpv/mpv.conf`, the one folder that stays XDG. `mpv(1)` for the 0.41.0 build installed here documents `~/.config/mpv` as the standard configuration directory with no macOS exception, overridable by `$XDG_CONFIG_HOME` and `$MPV_HOME`; the only macOS-specific config path it names is `~~osxbundle/`, the read-only resource path inside an app bundle. So ButterEye reads and offers its `include=` line in the same place on both |
| runtime folder | `$XDG_RUNTIME_DIR/buttereye` | `confstr(_CS_DARWIN_USER_TEMP_DIR)/buttereye` (`_darwin.user_temp_dir`), with `XDG_RUNTIME_DIR` honoured first so a test can point the sockets somewhere. Not `$TMPDIR`: `os.confstr` exposes no Darwin names, and `env -i` loses the variable where the call always answers. Measured here, the directory is 49 bytes long, which is the whole reason for the choice — the session socket under it is 80 bytes against the 103-byte bind limit, where a `$TMPDIR`-rooted pytest `tmp_path` under the same root measures 119 bytes before a socket name is added, which is why `tests/conftest.py` puts the test runtime directory under `/tmp` |
| private folder check | 0700, own uid, not a symlink | the same function, unchanged: the kernel's directory measures 0700, owner uid 501, not a symlink, on local APFS |
| locks | `flock` | `flock`, identically, including the part that matters — a holder that is killed releases it. The GUI's single-instance lock is still not taken on macOS, as it is not on Windows: `gui/app.lock_path` resolves `XDG_RUNTIME_DIR` itself and returns `None` without it, and teaching it the two other runtime folders is one decision in two files |
| plugin files | `mvtools.so` | `mvtools.dylib`: the extension changes and the stem never does (`osapi.plugin_file`, and the three `.vpy` copies of the rule) |
| durable saves | `fsync` on the directory holding the rename | the same, then `fcntl(F_FULLFSYNC)` on the same descriptor. On macOS `fsync` only hands the data to the drive, which may still hold it in a volatile cache; without the second call a profile saved seconds before a power cut could come back as the old one — never as a damaged one, since the rename stays atomic |
| storage cleaning | directory file descriptors with `O_NOFOLLOW`; a symlink answers `ELOOP` | the same walk; a symlink answers `ENOTDIR` instead, because `O_DIRECTORY` is consulted first there even for a link that does point at a directory (measured). Narrowing that check to `ELOOP` would have turned a swapped-in symlink from "removed as a link" into an unhandled `OSError` |
| render priority | renice the group, then `ionice -c2 -n7` | renice works. `ionice` does not exist on macOS, and `render/pipeline.low_priority` builds its prefix from `shutil.which`, so the disk-priority throttle silently drops out — not an error, just absent. `/usr/sbin/taskpolicy -b` and libc's `setiopolicy_np` are the macOS answers; **neither is done**, and this is the one row phase 1 leaves worse than Linux |

**One known cost, not a solved problem.** The runtime folder now lives in the
per-user temporary directory, and `dirhelper` sweeps that daily at 03:35 for
anything older than three days (the bullet above). A session left attached to
one mpv for longer than three days could therefore have its socket unlinked
underneath it. Nothing in phase 1 prevents that: it is written down here and in
`paths._resolve_macos` so that whoever meets it recognises it, and the storage
overview now tells the user "cleared once three days old" instead of Linux's
"cleared at logout".

**One drift from the plan, worth knowing.** TensorRT's absence is a seam flag,
`osapi.HAS_VSTRT`, and not `VSTRT_LIB = None`: every caller joins `VSTRT_LIB`
onto a path, so making it optional would have moved a type error into
`backends/trt.py` and `doctor/probe.py`. On macOS `VSTRT_LIB` therefore still
carries the Linux name and nothing is ever found under it; a caller has to ask
`HAS_VSTRT` before it looks, and phase 2 is where those two files start doing so.
The generated script refuses the TensorRT branch outright (`_rife_trt` in
`template.vpy`), ahead of the engine check that would otherwise blame a missing
engine for something no build can produce.

**Running the gate on a Mac.** `tools/macos-tests.sh` runs the three-platform
type check and then the test suite with the exclusion list
`.github/workflows/macos.yml` explains case by case — the same list, kept in
step by hand, because neither file can call the other. Phase 1 took one
exclusion off it: `tests/integration/test_live_trt_real.py` used to resolve the
XDG paths while being imported and so raised BE-2010 during collection, before
any marker could skip it; with a macOS runtime directory it collects and then
skips on its `gpu` marker like any other machine without a GPU. What is left is
seven Alt+letter mnemonic cases that belong to the GUI phase, three tests whose
*own* helpers read `/proc` for a child's state (not `osapi`'s — two re-implement
the read inline and the third walks `/proc` for a process tree, in files phase 1
did not own), and one `tests/gui` race that fails about one run in ten on Linux
too. Unlike `tools/wine-tests.sh` this script carries no emulation, no second
Python and no drive-letter translation: the machine is the target.

Phase 2 gave it one environment variable. `tests/macos/test_doctor_macos.py` runs
the whole doctor against the machine's own Homebrew, `vulkaninfo` and mpv, so it
is opt-in behind `BUTTEREYE_TEST_BREW` — the shape `tests/windows` already uses
for `BUTTEREYE_TEST_PLUGINS` — and `tools/macos-tests.sh` sets it, because the
Mac you run it on is the machine those tests are about. `BUTTEREYE_TEST_BREW=`
(empty) opts out again. `.github/workflows/macos.yml` installs no Homebrew
package, so it does not set it and that file skips on the runner by design; the
fixture half of the same ground, `tests/core/test_macos_doctor_pure.py`, needs no
Homebrew and runs everywhere.

**What phase 1 does not do.** It does not make macOS supported, and it does not
make the doctor speak macOS. At the end of phase 1 a full `doctor` run on this Mac
answered with `sudo dnf copr enable`, `libvulkan.so.1` and `usermod -aG
systemd-journal`, and reported ffms2 and the Vulkan loader missing while both were
installed — that was phase 2's job, and phase 2 below did it. Plugin builds are
phase 3, distribution phase 4, and the real-hardware gate phase 5.

It also does not empty the core of Linux-only calls, and an earlier draft of this
page claimed it did. These are the ones phase 1 left, each with the phase that
owns it; the Owner column says what has since happened to it, which for all seven
of the phase-2 rows is that phase 2 gave the call a macOS answer. They are all in
detection and diagnostics, which is the one part of the core that asks the
operating system questions rather than asking it to do things, and so the one part
the seam was never going to cover. The two rows still open are in `tests/` and
`tools/`, not in the application.

| Linux-only call left in the core | Where | Owner |
|---|---|---|
| `/proc/sys/kernel/random/boot_id`, to tell this boot's Xid faults from an older boot's | `gpufault.current_boot_id` | **done** (phase 2): macOS reads `kern.bootsessionuuid` through libc instead (`gpufault._sysctl_string`), which is new at every boot. `kern.uuid` is the hardware id and is deliberately not read |
| `journalctl -k`, the kernel log the Xid scan reads | `gpufault.scan_xid` | **done** (phase 2): the scan is "not readable" on macOS with a reason of its own (`gpufault.MACOS_NOT_READ`), and the INFO row says the history exists in the graphics device's IOReport channels and that ButterEye does not read them yet — not that macOS has none |
| `/usr/lib64/libvulkan.so.1`, `/lib64/libvulkan.so.1` and the Debian multiarch path, plus the ICD folders under `/usr/share` and `/etc` | `checks_vulkan.LOADER_PATHS`, `checks_vulkan.ICD_DIRS` | **done** (phase 2): `libvulkan.1.dylib` and `etc/vulkan/icd.d` under the derived Homebrew prefix (`types.homebrew_prefix`) |
| `/usr/lib64/libffms2.so*`, `/usr/lib/libffms2.so*`, `/usr/lib64/vapoursynth/*ffms2*` | `checks_pkgs.FFMS2_GLOBS` | **done** (phase 2): `checks_pkgs.macos_ffms2_globs` is the macOS answer, built per call because the prefix is derived |
| `/usr/lib64/vapoursynth`, `/usr/lib/vapoursynth` | `probe.AUTOLOAD_DIRS` | **done** (phase 2): on macOS that tuple is empty and `probe.AUTOLOAD_GLOBS` carries Homebrew's directory, from the same pattern the generated scripts use (`types._BREW_AUTOLOAD_GLOB`) |
| `/usr/lib64/libnvinfer.so.11` | `trt.LIBNVINFER` | **done** (phase 2) by not reaching it: `checks_trt.trt_findings` returns one macOS row before anything looks (`checks_trt.trt_macos_finding`), and `trt._vstrt_dirs` already asked `osapi.HAS_VSTRT` in phase 1. The constant still holds the Linux path, which nothing on a Mac now reads — the only other user, `trt.problems`, is reached through `trt.find_install`, which `_vstrt_dirs` has already made empty |
| `rpm -q` and the `dnf` lines | `checks_pkgs`, `setup`, `licences` | **done** (phase 2): Homebrew's Cellar read as files (`doctor/brew.py`, `checks_pkgs.brew_db`), `brew` lines from one table (`commands.BREW`), and the repository as the source offer where there is no SRPM (`licences.MACOS_SOURCE`) |
| `/proc/<pid>/status` and `/proc/<pid>/stat`, each re-read inline instead of through `osapi.pid_alive` | `_alive` in `tests/test_bench_runner.py`, `_state` in `tests/test_live_review_fixes.py` | whoever owns `tests/`. Two of the three `/proc`-reading tests the gate's exclusion list names, above. A helper that duplicates the read instead of calling the seam has to be taught each platform on its own, which is the cost of not asking |
| a `/proc` walk for a process tree | `descendants` in `tools/memguard.py` | whoever owns `tools/`. It already skips itself where no capped scope can be made, so it costs the developer workflow, not the application |

**A phase-2 problem phase 1 has already uncovered.** `vulkaninfo` reports the M2
as `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, which is the truth — the GPU shares
system memory with the CPU — but `backends/select.py` gates its high-quality
RIFE model on `dev.device_type == "discrete"` (with a heap threshold). Every
Apple GPU answers the same way, so an M1 Air and an M3 Ultra would both be
classified as low-end and always get the lite model. The rule is a Linux
PCIe-era proxy for "has its own fast memory", and unified memory does not fit
it; phase 2 had to pick a different test for Apple Silicon rather than widen
"discrete". What phase 2 decided, and why widening it would have been wrong on
three platforms at once, is under "The model gate and unified memory" below.

**A phase-4 blocker phase 1 has already uncovered: out of the box, Homebrew's mpv
cannot load a ButterEye filter at all.** mpv's `vapoursynth` filter loads VSScript
at run time by `dlopen` of a bare leaf name — `libvsscript.dylib`, then
`libvapoursynth-script.dylib`; both strings are in `/opt/homebrew/bin/mpv`, next to
"Failed to load VapourSynth VSScript library", and `otool -L` shows mpv links
neither. Homebrew puts neither name anywhere the dynamic loader looks. VSScript is
installed as
`/opt/homebrew/Cellar/vapoursynth/81/libexec/lib/python3.14/site-packages/vapoursynth/libvsscript.dylib`,
inside the formula's own libexec virtualenv, and `/opt/homebrew/lib` holds no
`libvapoursynth*` and no `libvsscript*` at all. Measured on this Mac, with a 1 s
320x240 clip and a one-line `.vpy` that passes `video_in` straight through:

```
$ mpv --no-config --vo=null --ao=null --frames=1 --vf=vapoursynth=s.vpy clip.mp4
[vapoursynth] Failed to load VapourSynth VSScript library: dlopen(
  libvapoursynth-script.dylib, 0x0006): tried: 'libvapoursynth-script.dylib'
  (no such file), ... '/usr/lib/libvapoursynth-script.dylib' (no such file, not
  in dyld cache), 'libvapoursynth-script.dylib' (no such file)
Exiting... (Errors when loading file)
```

dyld's own "tried:" list there is the Swift toolchain directories and `/usr/lib`
and nothing else: a symlink dropped in `/opt/homebrew/lib` would not be found
either. Two things do work, both measured on the same clip and script, which is
why this is distribution work and not a wall — each one reaches `VO: [null]
320x240 yuv444p` with the filter in the chain:

- `VSSCRIPT_PATH` pointed at that `libvsscript.dylib`;
- `DYLD_LIBRARY_PATH` pointed at the directory holding it, **with no symlink** —
  which is also the proof that `libvsscript.dylib` is genuinely in mpv's candidate
  list and not only in its strings, since nothing of the other name exists there.

So **mpv honours `VSSCRIPT_PATH` on every platform, not only Windows**, which is
where this page describes it above ("Phase 4: the installer", `osapi.use_bundled_runtime`,
which returns immediately when `not IS_WINDOWS`). macOS is the half-mirror of that
story: a Homebrew tap can set the variable, or place a file of an expected name
somewhere dyld searches, and phase 4 is where that decision belongs. **Phase 4
decided: ButterEye sets the variable itself for every child it starts, and the
tap places no file** — measured there, dyld does not search `<prefix>/lib` for a
bare leaf name at all, so a symlink dropped in it would answer nothing. See
"Phase 4: live smoothing works, and a Homebrew tap", below. Note what is
*not* broken — `vspipe` and the plugins never go through VSScript's bare-name
`dlopen`, which is why a real ButterEye `.vpy` already renders here (R81, real
`libffms2.dylib` and `mvtools.dylib`, 72 frames at 24 fps to 144 at 48 fps, 1432
fps). Only mpv's in-process filter is affected, so phase 1's live path is the part
that needs the tap.

**A finding about ButterEye's own code, for its owner to judge: a doctor gate that
does not gate.** `buttereye play` runs on a machine whose doctor report blocks,
although `Feature.LIVE` is in `capabilities.DOCTOR_GATED`. Measured during
phase-1 verification: with a report carrying two blocking findings, `play` returned
ok with a session id and started a real mpv that played the clip on this hardware
("Using hardware decoding (videotoolbox-copy)", "VO: [gpu-next] 320x240 nv24").
`cmd_play` does consult capabilities — `await need(core, Feature.LIVE)` is its
first line — and the gate still passes, because the `BLOCKED_BY_DOCTOR` branch of
`capabilities.compute` runs only `if report is not None`, and after its `None`
initialiser `ButterEye._report` is assigned in exactly one place: the body of
`ButterEye.doctor()`. A one-shot `buttereye play` process never calls that, so it
resolves capabilities with no report and nothing in `DOCTOR_GATED` is gated.
**This is not a macOS finding** — the same is true on Linux and Windows, where it
has simply never been pointed at. The port only made it visible, by producing a
machine that blocks for real while the pieces `play` needs happen to work.
Whether a one-shot command should run the
doctor first, or whether the gate is meant to be a GUI-session rule only, is the
owner's call and not this port's to make.

### Phase 2: the doctor speaks macOS (done)

The doctor, hardware detection and setup now give macOS answers. The starting
point is worth writing down, because it was not a crash: after phase 1 a full
`doctor` run on this Mac **ran**, in 0.2 s of its 10 s budget, and produced 19
findings of which 2 were blocking. What was wrong was the advice — every line of
it was Fedora's — and, in four rows, the verdict with it:

- `sudo dnf install mesa-vulkan-drivers`, to a Mac where MoltenVK was installed
  and working, under "No GPU Vulkan driver is installed";
- "The Vulkan loader is not installed (BE-1010). RIFE needs `libvulkan.so.1`",
  with `libvulkan.1.dylib` sitting in the Homebrew prefix;
- "ffms2 is not installed (BE-4001)" with `sudo dnf install ffms2`, while
  `libffms2.dylib` was installed and decoding;
- "No interpolation plugin is installed (BE-1020) … neither `librife.so` nor
  `mvtools.so` was found in `/usr/lib64/buttereye/vapoursynth`", on a machine
  whose `mvtools.dylib` really interpolates — 72 frames at 24 fps to 144 at 48
  through the real `vspipe`;
- and, for the kernel log it could not read, `sudo usermod -aG systemd-journal`.

Everything in the table below is the replacement for one of those, or for a
Fedora line behind a verdict that was already right.

**What the same machine answers now** (`doctor --json` on this Mac, after phase
2): 17 findings — 11 ok, 4 info, 1 degraded, 1 blocking — and not one `dnf`,
`sudo`, `rpm`, `copr` or `journalctl` word anywhere in them. Every row above has
moved:

- `vulkan.loader` is **ok**, "Vulkan: Apple M2", "RIFE will run on Apple M2
  (Vulkan device 0)" — found through MoltenVK's manifest under the Homebrew
  prefix, and not the mock ICD;
- `render.ffms2` is **ok**, version 5.0, read out of the Cellar;
- `packages.mvtools` is **ok**, "`vapoursynth-mvtools` 29", so BE-1020 is no
  longer claimed against a machine that interpolates; what is left of it is
  `packages.rife` at degraded, "RIFE has no macOS package yet, so only MVTools
  (CPU) smoothing works here", with no command, because there is none;
- `packages.mvgpu` is info, "ButterEye doesn't build mvgpu for macOS yet";
- `gpu_fault.xid` is info, "GPU faults aren't checked on macOS yet", and says
  where the history is and what reading it would take;
- `trt.off` is info and says *can't*, not *isn't set up yet*;
- `packages.rpm` is gone: the Cellar answered, so there is no "can't be checked"
  row at all, and `packages.licence_files` is ok against the keg's own texts;
- the one **blocking** finding is the honest one, `probe.run` (BE-1005), "mpv
  can't find VapourSynth's VSScript library", naming the exact
  `libvsscript.dylib` in the formula's libexec virtualenv and offering the
  `export VSSCRIPT_PATH=…` line. That is a true blocker, and phase 4 removed it:
  the same machine now reports 0 blocking, because `ButterEye.open()` sets the
  variable for the children it starts (phase 4, below).

Checked on that output: no serial number, no platform UUID, no `kern.uuid` and no
`SPHardwareDataType` anywhere in it. The two UUIDs it does carry are Vulkan
device UUIDs, which SCOPE §2 names as allowed and local, and the one `$HOME` path
is the mpv config file that `doctor --report` already redacts.

No macOS finding now carries a `dnf`, `rpm`, `sudo`, `copr` or `journalctl` word.
A test says so, as one does on Windows:
`tests/core/test_macos_doctor_pure.py` has its own `LINUX_WORDS` tuple and runs
the findings of a bare Mac and of an installed Mac past it, with the same run on
Fedora asserted to still speak Fedora. It is the peer of
`test_windows_doctor_pure.py` and pure in the same sense — no Homebrew, no Mac,
no subprocess: `osapi.IS_MACOS` and `commands.sys.platform` are set for the call,
`HOMEBREW_PREFIX` points at a planted Cellar, and the two checks that freeze
their Linux answer in a module constant (`checks_vulkan.LOADER_PATHS`,
`checks_vulkan.ICD_DIRS`) take their directories as arguments.
`tests/core/test_macos_detect_pure.py` covers the two IOKit parsers, the vendor
table and the mock-ICD exclusion the same way. Both run on every platform, so a
Linux CI machine keeps the macOS rules honest. The real-Mac half, which
`test_macos_doctor_pure.py`'s own docstring points at, is
`tests/macos/test_doctor_macos.py`: the same doctor run against this machine's
Homebrew, `vulkaninfo` and mpv instead of against a planted prefix. It needs the
bottles, so it is opt-in behind `BUTTEREYE_TEST_BREW` ("Running the gate on a
Mac" above).

| Check | Linux | macOS |
|---|---|---|
| Installed packages | `rpm -q` | **Homebrew's Cellar, read as files** (`doctor/brew.py`; `checks_pkgs.brew_db` turns it into the same `RpmDb` the rest of the checks already take). `brew` is never run — see "Why the Cellar and not `brew`" below. `BrewKeg` carries the version, the licence expression, the licence texts and `installed_at`, which is the `%{INSTALLTIME}` the Xid cutoff uses. A name Homebrew has no formula for is simply not installed, which is the answer rpm gives too; no Cellar at all is reported as "Homebrew isn't installed", not as nine missing packages (`checks_pkgs._unreadable_db_finding`) |
| Plugin files | `librife.so`, `mvtools.so`, `libmvgpu.so` | the same stems with `.dylib` (`osapi.plugin_file`, phase 1; the generated scripts map the name themselves). One macOS-only wrinkle: ButterEye's plugin directory can *be* Homebrew's autoload directory, so a plugin found there is reported as the system's copy and not as ButterEye's (`probe.plugin_statuses`) |
| Licence files | rpm's `%license` entries | the `LICENSE`/`COPYING`/`NOTICE` texts in the keg's root (`brew.BrewKeg.licence_files`), and the formula's SPDX expression from the keg's `sbom.spdx.json` (`BrewKeg.licence`). The word `%license` is dropped from the text, because Homebrew has no such tag |
| Tools ButterEye doesn't ship | `sudo dnf install …` | `brew install …`: mpv `mpv`, VapourSynth `vapoursynth` (the formula that ships `vspipe`), ffmpeg `ffmpeg`, mkvtoolnix `mkvtoolnix`, 7-Zip `sevenzip` (`commands.BREW`, `commands.install_command`). Every name was read off a real Cellar, none guessed |
| ButterEye's own plugins | the COPR lines | **no install line, because none can work**: Homebrew has no formula for RIFE-ncnn-Vulkan and none for mvgpu (`brew search` finds neither), and ButterEye has no macOS package of its own yet. Those findings say so (`checks_pkgs.NO_MACOS_PACKAGE`) instead of offering a command that would fail. MVTools is the exception and the one real line: `brew install vapoursynth-mvtools` (`checks_pkgs.FORMULA_MVTOOLS`), which is why "no interpolation plugin is installed" now names the plugin a Mac user can actually have (`checks_pkgs.MACOS_MVTOOLS_FIX`) |
| ffms2 | `/usr/lib64/libffms2.so*` and the distro VapourSynth directory | both plugin directories (`types.macos_plugin_dirs`, autoload first — phase 4 made that one list) and then `<prefix>/lib/libffms2*.dylib`, which is where the bottle also puts the library render links against (`checks_pkgs.macos_ffms2_globs`). Built per call, not frozen at import, because the prefix is derived |
| The in-mpv probe (BE-1005) | `sudo dnf install python3-vapoursynth vapoursynth-tools` | the real reason, named: mpv can't load VSScript at all (`probe._vsscript_finding`, below). The autoload directories are Homebrew's, globbed from the prefix and from the same pattern the generated scripts use (`probe.AUTOLOAD_GLOBS`, `types._BREW_AUTOLOAD_GLOB`) rather than a second guess at it |
| Setup's smoke test | `sudo dnf install vapoursynth-tools` | `brew install vapoursynth`, which is the only formula that ships `vspipe` (`smoke.INSTALL_VSPIPE_MACOS`). The smoke test itself needed nothing: its directories come from `Paths`, which phase 1 already taught macOS, and it ran 24 frames in and 48 out at 898 fps through the real `vspipe` |
| GPUs | sysfs + `pci.ids`, then Vulkan and `nvidia-smi` | `system_profiler -json SPDisplaysDataType`, with `ioreg -rd1 -c AGXAccelerator` as the fallback (`detect.read_iokit_gpus`, `detect.parse_system_profiler_gpus`, `detect.parse_ioreg_gpus`); both named by absolute path, because an app launched from Finder inherits a minimal `PATH`. `nvidia-smi` is never run: no Apple Silicon Mac has an NVIDIA GPU. An Apple GPU has no PCI address, no device id, no driver version and no VRAM of its own, and each of those is left an explicit unknown rather than filled from Vulkan's device-local heap, which on MoltenVK is the whole machine's RAM and would read as a 16 GB graphics card |
| Vulkan loader | `/usr/lib64/libvulkan.so.1`, `/lib64/…`, the Debian multiarch path | `<prefix>/lib/libvulkan.1.dylib` and `libvulkan.dylib`, plus `/usr/local/lib` where LunarG's macOS SDK installs one (`checks_vulkan.LOADER_PATHS`). The prefix is derived, never hardcoded: `checks_vulkan` asks the same `types.homebrew_prefix` the generated scripts and `Paths` already agree with |
| Vulkan drivers (ICDs) | `/usr/share/vulkan/icd.d`, `/etc/vulkan/icd.d`, `/usr/local/share/vulkan/icd.d` | `<prefix>/etc/vulkan/icd.d` — which is where Homebrew puts `MoltenVK_icd.json`, the one real driver manifest on a Mac — plus `<prefix>/share/vulkan/icd.d` and `/usr/local/share/vulkan/icd.d` (`checks_vulkan.ICD_DIRS`). The fixes say `brew install vulkan-loader molten-vk`, and that **nothing else is to be updated**: the Metal driver is part of macOS, so the Linux and Windows advice "install or update your graphics driver" has no macOS equivalent |
| GPU fault history | NVIDIA Xid lines in `journalctl -k` | **not read yet, and said so** (`gpufault.MACOS_NOT_READ`, an INFO row from `gpufault.xid_finding`). This is deliberately not "macOS has no GPU fault history": the graphics device publishes IOReport channels named "GPU Restart Count" and "Last GPU Restart", so the history exists — reading it needs the IOReport API or `powermetrics` as root, and ButterEye does neither. The row says that, and says the Linux check counts NVIDIA Xid errors, which a Mac cannot have. The boot id that tells this boot's faults from an older boot's is `sysctl kern.bootsessionuuid`, read through libc rather than the `sysctl` program (`gpufault.current_boot_id`, `gpufault._sysctl_string`) |
| TensorRT (experimental) | vs-mlrt's vstrt built by `contrib/build-vstrt.sh` | **permanently impossible, not pending** (`checks_trt.trt_macos_finding`, one INFO row in place of the seven-step Linux chain): no CUDA for macOS and no NVIDIA GPU to put it on. The capability text says the same rather than naming two missing pieces as a to-do list (`capabilities._TRT_IMPOSSIBLE`), and `osapi.HAS_VSTRT` (phase 1) already stops anything globbing for an engine that cannot exist. Windows' row says "yet"; this one does not |
| mpvSockets conflict | checked | checked, identically. macOS has Unix sockets, the script's folder is `~/.config/mpv/scripts` there too (measured: Homebrew's mpv resolves `~~home/` to `~/.config/mpv`, not to anything under `~/Library`) and `/tmp` is the same writable place (`checks_mpv.mpvsockets_finding`) |
| mpv shipped as an app bundle | not a shape that exists | **named, not only refused** (`checks_mpv.app_bundle`, `checks_mpv._bundle_fix`). The mpv a Mac user most likely already has is the mpv.io / stolendata bundle, 0.40.0 with no VapourSynth filter, so it fails both the version check and the filter check — and "your mpv was built without VapourSynth" tells that user nothing they can act on. Both checks now end in one answer, so they cannot say two different things to the same user: this is an application bundle, Homebrew does not update it, `brew install mpv` |
| Setup's command list | the `dnf` lines | the `brew` lines (`setup.dnf_lines`, whose field name stays `SetupPlan.dnf_lines`; `setup._BREW_INSTALL` and `setup._INSTALL_PREFIXES` are the macOS arm) |
| The source offer | `dnf download --source buttereye` | the repository (`licences.MACOS_SOURCE`): there is no source RPM and no installer source offer to point at. A missing licence text asks for a `brew reinstall` of the formula that provided the component, without a line to paste, because `Component.package` is an rpm name and nothing maps it to a formula (`licences._reinstall`) |

#### Why the Cellar and not `brew`

`brew` is never run. Three measured reasons, all on an arm64 Mac on 2026-10-12:

- `brew info --json=v2 --installed` would answer the whole question in one call,
  but it took **1.09 s warm** and it can go to the network to refresh its API
  cache. SCOPE F1 gives the whole doctor 10 s "with no network access", and §2
  says ButterEye makes "no network connections except downloads the user starts".
- `brew --prefix <formula>` looks like the question "is this installed" and is
  not: for a formula that is **not** installed it prints an error on stderr,
  answers `<prefix>/opt/<formula>` anyway, and **exits 0**.
- `<prefix>/Cellar/<formula>/<version>/` exists only for an installed formula,
  and `INSTALL_RECEIPT.json` in it carries `source.versions.stable`, `time` (the
  epoch install time, rpm's `%{INSTALLTIME}`) and `runtime_dependencies`.
  Globbing every receipt on this Mac measured 7.56 ms; `brew.py` records 12 ms
  for the directory scan and 49 ms to parse all 172 of them. No subprocess, no
  network, and a rounding error inside the budget.

**The prefix moves** — `/opt/homebrew` on arm64, `/usr/local` on Intel — so it is
derived in every place that needs it and hardcoded in none. `HOMEBREW_PREFIX` is
only the first guess: it is set in a brew shell and **not** in an application
launched from Finder (measured: `launchctl getenv PATH` answers nothing), so the
prefix that has a `Cellar` decides, and the architecture decides when neither
does. There is one such helper, `types.homebrew_prefix`: `brew.prefix` returns
it, and `checks_vulkan` and `checks_pkgs.macos_ffms2_globs` call it rather than
keeping a second copy of the rule. Intel Macs are still out of the first cut.

#### Two things that are now diagnosed rather than mysterious

**Why the in-mpv probe fails (BE-1005).** Phase 1 measured the cause and phase 2
is where the doctor says it. mpv's `vapoursynth` filter does not link
VapourSynth: it `dlopen`s VSScript at run time **by bare leaf name**,
`libvapoursynth-script.dylib`, and Homebrew ships no file of that name anywhere
the dynamic loader looks — VSScript is `libvsscript.dylib` inside the formula's
own libexec virtualenv (the phase-1 write-up above has mpv's own error text and
dyld's candidate list). So the old advice was wrong twice over: the package it
named exists only on Fedora, and VapourSynth *is* installed. The doctor now
recognises mpv's failure line (`probe.vsscript_load_failed`), finds the real
library (`probe.vsscript_library`, `probe.BREW_VSSCRIPT_GLOBS`) and says which
file it is and that nothing is wrong with the installation. Because mpv honours
`VSSCRIPT_PATH` on **every** platform and not only Windows — which phase 1
measured by making the filter load that way — the finding offers exactly that
`export VSSCRIPT_PATH=<that file>` line, and says the probe passes with it set.
Doing it for the user was phase 4's packaging work, not a check's, and phase 4
did it (below), so on a machine like this one the finding no longer fires. It is
still the right check to keep: it is what a user sees when VapourSynth is not
installed at all, or when the mpv they pointed ButterEye at cannot be reached
this way.

**The mock ICD hazard.** The `vulkan-tools` formula — which is the formula the
doctor asks a Mac user for, so this is a hazard ButterEye creates for itself —
installs a mock driver at `<prefix>/opt/vulkan-tools/lib/mock_icd/VkICD_mock_icd.json`,
and its own brew caveat invites the user to point `VK_ICD_FILENAMES` at it.
Measured through `vulkaninfo` with the variable set, it answers as "Vulkan Mock
Device", `VIRTUAL_GPU`, an 8 GB heap, PCI `0000:00:00.0`. A ButterEye that
believed it would report a software rasteriser as the GPU and send RIFE there. It
is excluded twice: by manifest name in `checks_vulkan.CPU_ICDS`, so it never
counts as a GPU driver, and by its vendor id in `detect.MOCK_VENDOR_ID`
(`detect.drop_mock_devices`), so it never enters the device list. The exclusion
is by vendor id rather than by device type, because a virtio GPU on Linux
answers `VIRTUAL_GPU` too and is a real device.

**MoltenVK is not a CPU ICD, and that took deciding.** `CPU_ICDS` exists to drop
"manifests that only provide CPU or translation devices", and MoltenVK *is* a
translation driver — Vulkan over Metal, exactly what `dzn` is on Windows. But on
macOS it is the **only** route to the real GPU, so it has to count as a real
driver there, and it is deliberately not in `CPU_ICDS`. `vulkaninfo` here reports
`deviceName` "Apple M2", `driverName` MoltenVK, `driverID` `DRIVER_ID_MOLTENVK`,
`driverInfo` 1.4.2 at `apiVersion` 1.4.357 — a real device, not the mock. When a
Mac answers with a software device only, that means MoltenVK is absent, so the
"llvmpipe only" wording is replaced there: a Mac has no llvmpipe.

#### The model gate and unified memory

Phase 1 uncovered this and it is a real problem, not a cosmetic one:
`backends/select.py` gated the high-quality RIFE model on `dev.device_type ==
"discrete"` with a 16 GiB heap threshold, and every Apple GPU from an M1 to an M3
Ultra reports `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, so every Mac would always
get the lite model.

**What was not done, on purpose: `device_type` was not widened.** MoltenVK
reports a device-local heap close to `hw.memsize`, so a 16 GB MacBook Air whose
GPU shares that RAM with everything else would pass a 16 GiB threshold — and so
would a desktop with an Intel UHD on Linux. `device_type` is not the loose half
of that test; it is what makes `heap_bytes` mean anything, because only a
discrete card's device-local heap is memory of its own. Relaxing it would have
been wrong on three platforms at once.

**Phase 5 measured how close "close to `hw.memsize`" is, and it is closer than
this paragraph assumed.** On this M2 `heap_bytes` is 17179869184 — byte-for-byte
`sysctl hw.memsize`, the whole machine's RAM, and **exactly** equal to
`select.HIGH_END_HEAP` (verified 2026-10-13 against `vulkaninfo` through
`detect.parse_vulkaninfo`). So the heap half of `_high_end_gpu` passes on this
machine and only the `discrete` half refuses it: a heap threshold on its own
would have handed a 16 GB Mac `rife-v4.26_ensembleFalse`, which phase 5's
benchmark measures at **8.33 fps in mpv at 1080p**
(`docs/spikes/m0s-results.json`), five to six times short of live. The gate holds
here by an exact coincidence rather than by a margin, which is the strongest
argument there is for leaving `device_type` in it.

**What was done:** the rule is now one documented function,
`select._high_end_gpu`, which says that in its docstring, and a machine with
unified memory keeps the conservative default **until it has been measured**.
That is not a dead end, because ButterEye already measures: the in-mpv benchmark
walks the candidates best quality first (`bench.runner.recommend`,
`select._from_bench`), and a benchmark recommendation beats the default, so the
full model is reached on a Mac by a measurement on that Mac rather than by a
guess from a device name. That is what SCOPE asks for: its fifth product
principle is "Backend selection is hardware-aware and backed by a built-in
benchmark that measures inside mpv. We do not quote published fps figures", and
F14 is that benchmark.

#### A privacy rule phase 2 had to obey

Phase 2 is the first phase that asks macOS questions about the machine, and the
obvious programs for it leak machine identity. `system_profiler
SPHardwareDataType` carries the serial number and the platform UUID, and `sysctl
kern.uuid` is the same hazard: a hardware id. None of that may reach a
`Finding`'s evidence, a log or `doctor --report`, which is the bundle users
attach to bug reports and which SCOPE §2 already requires to be redacted.

So detection asks `SPDisplaysDataType`, never `SPHardwareDataType`, and reads
five named fields out of it — `sppci_device_type`, `sppci_model` or `_name`,
`spdisplays_vendor`, and inside `spdisplays_ndrvs` only `spdisplays_online` —
rather than dumping the report; the raw text stays inside
`detect.parse_system_profiler_gpus` and never reaches a `SysGpu`, because the
display report *does* carry an attached monitor's serial number.
`tests/core/test_macos_detect_pure.py` keeps a serial number in its fixture on
purpose, so that a parser which started passing one through would fail a test.
The boot id is `kern.bootsessionuuid`, which is new at every boot and identifies
nothing; `gpufault.current_boot_id` says in its own docstring that `kern.uuid`
must never be read there.

The same rule governs this page: no machine-identifying value is written down in
it, and the Homebrew paths in it are the prefix, which is the same on every
arm64 Mac.

#### Phase 2 first landed with a red gate too, for the same reason as phase 1

Phase 1's lesson is that a check which changes what it looks for, or what a
finding says, breaks the fixtures that assert the old string — and that those
fixtures live in files the phase does not own. Phase 2 repeated it, in three
shapes, all in `tests/` and none a defect in the macOS answers:

- **`tests/core/test_doctor_fixtures.py`** (nineteen tests) drives the whole
  doctor through a fake `run` with an allowlist of expected programs, and raises
  `AssertionError(f"unexpected command {a}")` for anything else. Detection now
  runs `/usr/sbin/system_profiler` on a Mac, which that allowlist has never heard
  of, so every one of them fails inside `probe_hardware`. An arm for
  `system_profiler` and `ioreg` is not the whole fix: these tests assert Fedora
  wording (the COPR lines among it), and on a Mac `checks_pkgs.rpm_query` takes
  the Homebrew branch and never calls `rpm` at all, so they need the explicit
  platform that `tests/core/test_paths.py` was given in phase 1.
- **`tests/core/test_xid_parser.py`** (three tests) constructs an unreadable
  `XidScan` with a reason of its own ("insufficient permissions") and expects
  BE-1031, and hands `scan_xid` an explicit `journalctl=` stub. Both macOS arms
  are keyed on the platform rather than on the scan's own reason, so on a Mac the
  first gets the "not read yet" row with no code and the second never reads the
  stub. The Windows arms have exactly the same shape and were never caught,
  because the suite only ever ran them on Linux.
- **`tests/core/test_windows_doctor_pure.py::test_install_commands_per_platform`**
  asserts `commands.install_command("mkvtoolnix") == "sudo dnf install mkvtoolnix"`
  with no platform forced, and gets `brew install mkvtoolnix` on a Mac. It is a
  test about the per-platform table; it has to say which platform it means.

None of the three may be made to pass by weakening an assertion: the Fedora
strings in them are the Fedora packaging contract. `tools/macos-tests.sh` — not
this page — is what says whether the gate is green.

#### What phase 2 does not do

**macOS is still not a supported platform.** After phase 2 the doctor tells the
truth on a Mac — the verdicts were already right, and now the fixes are too — and
that is all it does. At the end of phase 2 no plugin had been built for macOS
and mvgpu could not create a Vulkan instance there at all; phase 3, below, did
both. What phase 2 leaves to later phases either way: there is no
distribution (phase 4), live playback still does not work out of the box because
mpv cannot find VSScript under the name it looks for until something sets
`VSSCRIPT_PATH` for it (phase 4), and RIFE on macOS had at that point neither
been built nor rendered a frame — phase 3's spike changed the second half of
that and not the first. Phase 5, the real-hardware gate, is what would decide
any of this.

### Phase 3: the plugins (one built, two taken from Homebrew, RIFE spiked but not built)

Windows phase 3 cross-compiled four plugins in a mock chroot with Fedora's
MinGW-w64. macOS needs both less and more than that, and the two directions are
the whole of this phase.

**Less: MVTools and ffms2 are not built at all.** Homebrew ships
`vapoursynth-mvtools` 29 and `ffms2` 5.0 as arm64 bottles, and installs both into
VapourSynth's own autoload directory as `mvtools.dylib` and `libffms2.dylib`.
They work, measured end to end: a ButterEye-generated `.vpy` took a real `.mkv`
from 72 frames at 24 fps to 144 at 48 through the real `vspipe`, the real
`libffms2.dylib` and the real `mvtools.dylib`, at 1432 fps. MVTools on arm64 is
not the performance hole it looks like either — upstream's `meson.build` already
takes the aarch64 branch, `sse2neon.h` is bundled, and clang `-O3`
auto-vectorises the plain-C SAD into NEON at 227.6 M SAD/s for 16×16, 1.3×
*faster* than the same SSE2 kernel through sse2neon. And the reason the Windows
build compiles its own ffms2 — the official Windows binary is GPLv3 through its
FFmpeg, with source nobody can reconstruct, so ButterEye could not meet the
source offer for a binary it conveyed — evaporates here, because ButterEye
conveys nothing: the user installs the bottle and Homebrew carries the source
duty.

**More: there is no native Vulkan, and the loader hides the driver that stands in
for it.** `mvgpu` and RIFE-ncnn are Vulkan compute, and the only Vulkan a Mac
has is MoltenVK, a portability driver on top of Metal. The Vulkan loader
does not show a portability driver to an application that has not asked for one,
which is why `mvgpu`'s first dispatch failed; the fix is below. Only `mvgpu` had
to be taught: ncnn asks already — its `src/gpu.cpp` enables
`VK_KHR_portability_enumeration` and sets the matching instance flag whenever
the loader offers them — which is why the RIFE spike saw the GPU on its first
try.

`packaging/macos/build-plugins.sh` therefore builds **one** plugin, natively —
Apple clang out of this repository, no chroot, no cross-compiler, nothing
downloaded — and says out loud which plugins it did not build and why, because a
silent omission is how a packager ends up shipping three plugins and believing it
shipped four. Output goes to `$XDG_CACHE_HOME/buttereye/macos` (never the
repository): `vapoursynth/libmvgpu.dylib`, `licenses/<component>/`,
`components.json`, `sources/` and `SHA256SUMS`. Nothing here goes through
`packaging/build.sh`, which wants `mock`, `rpmbuild`, `spectool` and GNU
`stat -c`; a Mac has none of those, so the macOS build is a new script in a new
directory, exactly as the Windows one was. Details in `packaging/README.md`.

`packaging/macos/check-dylibs.sh` is the macOS peer of the Windows import check
(`check_dll` in `packaging/windows/build-in-chroot.sh`, which refuses any
Microsoft DLL but `msvcrt` and `KERNEL32`). The question both ask is whether the
file would still work on a machine that is not the build machine. For each
plugin it checks arm64 Mach-O, the exported `_VapourSynthPluginInit2`, and that
nothing is linked a user would not have: macOS itself and a short list of
Homebrew formulae a macOS build may link, each through the versionless
`opt/<formula>` symlink that survives a `brew upgrade`. That list is **not**
ButterEye's declared dependencies, and the script's own comment says so: of its
six formulae only `vulkan-loader` is declared and checked today
(`checks_pkgs.BREW_FORMULAE`, read off the Cellar by `brew.py`; nothing in
`buttereye/` asks about `ncnn`, `libomp`, `glslang`, `spirv-tools` or `fftw`).
So the RIFE build phase 4 is expected to make would link `opt/ncnn` and
`opt/libomp` and pass this gate while nothing told the user to install either.
Declaring them, and teaching the doctor to check them, is phase 4's debt — and
phase 4 paid half of it: `buttereye-vs-rife-ncnn.rb` declares `ncnn` and
`libomp`, so a tap install gets them, while `checks_pkgs.BREW_FORMULAE` still
does not list either, so the doctor still cannot explain a hand-built
`librife.dylib` that will not load.

A baked-in Cellar path,
an `@rpath`/`@loader_path` reference, any `LC_RPATH`, and anything else absolute
(`/usr/local` included — that is an Intel prefix or a stray local build) are
refused with the reason named.

#### mvgpu builds on arm64 with no source changes

Not one line of `mvgpu/src/` had to move to *compile* it. `meson setup` finds
Apple clang 21.0.0, `cpu_family` `aarch64`, pkgconf 3.0.8, `vapoursynth` 81,
`vulkan` 1.4.363 and Homebrew's `glslang`; `ninja` compiles all seven shaders —
`unpack`, `down2`, `search`, `median`, `select`, `render`, `pack`, the list in
`mvgpu/meson.build` — to SPIR-V and links `libmvgpu.dylib` (127 616 bytes in the
build tree, 124 416 as `meson install` leaves it) with no warning worth the
name. `otool -L` lists three dependencies and no more:
`libvulkan.1.dylib` through Homebrew's `opt/vulkan-loader` symlink,
`/usr/lib/libc++.1.dylib` and `/usr/lib/libSystem.B.dylib`. `nm -gU` shows
`_VapourSynthPluginInit2`. VapourSynth loads it: after
`core.std.LoadPlugin(<path>)`, `core.plugins()` lists the `mvgpu` namespace
beside `ffms2`, `mv`, `resize`, `std` and `text`, with the signature
`Interpolate(clip:vnode; fps_num:int; fps_den:int:opt; device:int:opt)`.

Phase 3 also gave `mvgpu/meson.build` a `meson_version: '>=1.1'`, which it was
missing while using a feature (`meson.options`) that arrived in 1.1. It cannot
shut out a build host that worked before: an older meson would have ignored
`meson.options` and failed for want of `-Dplugindir` anyway.

#### The one blocker: the loader hid MoltenVK

Dispatching, rather than loading, is where it stopped. `core.mvgpu.Interpolate`
through `vspipe` failed with

```
vapoursynth.Error: mvgpu.Interpolate: vkCreateInstance failed (-9)
```

and −9 is `VK_ERROR_INCOMPATIBLE_DRIVER`. `Gpu::init` in `mvgpu/src/mvgpu.cpp`
built its instance with `apiVersion` `VK_API_VERSION_1_1` and no instance
extensions at all. MoltenVK's ICD declares `"is_portability_driver": true`, and
the Vulkan loader hides such a driver until the application enables the
`VK_KHR_portability_enumeration` instance extension **and** sets
`VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR` in
`VkInstanceCreateInfo::flags`. On a Mac that driver is the only one there is, so
hiding it leaves `vkCreateInstance` with nothing to create an instance on.

**The fix is a guarded retry, and the guard is the loader's refusal.**
`Gpu::init` makes the first `vkCreateInstance` call exactly as every build
always has — no instance extensions, `flags` 0 — and only when that call comes
back `VK_ERROR_INCOMPATIBLE_DRIVER`, **and** `portability_offered()` says the
loader advertises the extension, does it add `VK_KHR_portability_enumeration`
and `VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR` and try once more. So
wherever a native driver answers — every stock Linux and Windows install — the
first call succeeds, nothing after it runs, and the `VkInstanceCreateInfo` is
byte for byte the one those platforms have always passed. Where no driver
answers at all, the retry fails the same way and the error text is unchanged:
measured on a machine whose only driver was a deliberately broken ICD,
`vkCreateInstance failed (-9)` either way.

The obvious version — ask the loader whether it offers the extension, enable it
if so — was **rejected, because that test does not test anything**. Measured
with a small C program against the real Vulkan loader (1.4.363), with MoltenVK's
ICD replaced by a single deliberately broken non-portability ICD pointing at
`/nonexistent.dylib`: `VK_KHR_portability_enumeration` was still listed among
the instance extensions, 4 of them with that broken ICD as the only driver and
19 with MoltenVK present, the portability one in both lists. The **loader**
implements that extension, not a driver, so it advertises it unconditionally and
its presence is evidence of nothing. Keying the guard on it would therefore have
set the flag on Linux and Windows as well, which is not a no-op there: a
portability driver installed beside a native one would start appearing in the
device list and shift the indices `device=` selects. Anyone tempted to simplify
this into an unconditional enable should rerun that broken-ICD test first.

`portability_offered()` is still there and still calls
`vkEnumerateInstanceExtensionProperties`, but it answers only the secondary
question, "may I even ask for this": enabling an extension the loader does not
advertise fails with `VK_ERROR_EXTENSION_NOT_PRESENT`, which is what a loader
older than 1.3.212 — from before the extension existed — would do. The trigger
is deliberately **not** `#ifdef __APPLE__` either: being a portability driver is
a property of the ICDs on the machine, not of the operating system — translation
layers exist on Linux and Windows too — and a platform macro would claim
something that was never detected. The extension's name and bit are spelled out
under `#ifdef VK_KHR_portability_enumeration`, with the header's own constants
preferred where they exist, so a build host with `vulkan-headers` older than
1.3.212 still compiles.

One more thing fell out of the same block. `Gpu::init`'s automatic device choice
looks for a discrete GPU first, and on Apple Silicon there is none: MoltenVK
reports the M-series GPU as `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, so it is the
second test, "the first non-CPU device", that picks it. An instrumented build
confirmed it: one device, `Apple M2`, `deviceType` 1, chosen by the non-CPU
clause, and the null guard after the loop never runs. The code says so now, next
to the line that keeps a CPU-only install (lavapipe, SwiftShader) working
instead of crashing on that null. A discrete card still wins where there is one.

**What it measures with the fix in.** `meson setup` and `ninja` are clean and
the meson warning is gone; `check-dylibs.sh` passes the built
`libmvgpu.dylib`; `core.mvgpu.Interpolate(clip, fps_num=48, fps_den=1)` no
longer fails but renders; and `tests/macos/test_plugins_macos.py` passes all
seven of its checks on this M2, the interpolation one included — a textured
square moving 16 px a frame lands 8 px on in the in-between frame, graded
against nine candidate positions, so a plugin that duplicated a source frame or
blended its neighbours would fail it. `mvgpu/README.md` carries the whole
measurement under "macOS: Vulkan through MoltenVK" and "Measured (Apple M2,
MoltenVK 1.4.2, 2026-10-12)"; the parts worth repeating here:

- the picture is not merely plausible, it is **bit-exact**. On the Linux
  correctness clip (a textured 96×96 square moving 16 px a frame, 640×368,
  24 → 48) the in-between frame matches the square drawn at the half-way
  position with mean absolute error 0.0 and maximum 0.0, against 8.43 for a
  blend of the two source frames and 9.72 for either source frame alone. Source
  frames come through untouched and a scene change is repeated, as on Linux.
- it is **about 14× slower than the Linux dev box's RTX 4090** (whose numbers are
  that machine's, not a Mac's) and the shape of the curve is the
  same: 720p 24 → 120 at 287 fps, 1080p 24 → 120 at 137 fps, 4K 24 → 120 at
  36 fps. So 1080p → 120 is the largest live case with headroom and 4K is far
  short of live playback on an M2. `MVGPU_PROFILE=1` at 4K says GPU-bound rather
  than readback-bound: 35.14 ms GPU against 0.75 ms CPU per render.
- the Metal translation is quiet. With `MVK_CONFIG_DEBUG=1
  MVK_CONFIG_LOG_LEVEL=4` a full render logs 8 SPIR-V → MSL conversions and 10
  Metal shader compilations and not one `[mvk-warn]` or `[mvk-error]` line. The
  limits are far from tight: `maxComputeWorkGroupInvocations` 1024 against the
  512 the coarse search needs, `maxStorageBufferRange` 4294967295 against the
  127 MiB the vector field wants at 4K.

What is still unmeasured is real footage inside mpv rather than synthetic clips
through `vspipe`, and phase 5 did not close it either: mvgpu's dylib is not in a
directory ButterEye searches, so the in-mpv benchmark has never seen it
(`docs/macos-release-bar.md`, item 4).

#### There is no Metal shader compiler here

This Mac has Apple's Command Line Tools and not the full Xcode, so
`xcrun -sdk macosx metal --version` fails with `unable to find utility "metal"`
and `xcodebuild` does not exist. Nothing phase 3 writes may depend on compiling
a `.metal` file, and nothing does: MoltenVK is a prebuilt bottle that translates
SPIR-V to MSL itself at run time. It is also the reason a native-Metal `mvgpu`
is not proposed as part of the port. It would need a compiler no ButterEye build
host is guaranteed to have, and it would be a second engine to maintain rather
than this one ported.

#### Testing what was built

`tests/macos/test_plugins_macos.py` is the gate, and it opts in the way phase 2's
`tests/macos/test_doctor_macos.py` opts in: `BUTTEREYE_TEST_PLUGINS` names the
directory `build-plugins.sh` wrote, which is the variable `tests/windows` already
uses for the same job. Without it the file skips, because GitHub's
`macos-latest` runner builds no plugin and a test that failed there would fail
the gate for a machine that was never asked to have one.

It reads the Mach-O header and symbol table itself rather than parsing `otool`
and `nm`, so it and `check-dylibs.sh` are two independent readings of the same
file; it loads the plugin by absolute path and asserts through
`Plugin.plugin_path` that the plugin it got is the one in the directory, because
Homebrew's autoload directory supplies `mv` and `ffms2` to the same core and a
namespace alone would not say which file answered; and it grades the in-between
frame. One rule in it is worth repeating, because it is the phase-3 regression:
a Vulkan failure is a skip only when the loader or MoltenVK is missing. With
both installed, a plugin that cannot create an instance — `vkCreateInstance`
failed (-9) — is a **failure**, not a skip, so the portability work cannot
quietly come undone.

#### RIFE-ncnn: it builds and renders in a spike, and the build path still skips it

Both halves are true and neither replaces the other. `packaging/macos/RIFE-SPIKE.md`
is a measured spike on this M2: `buttereye-vs-rife-ncnn` builds natively on
arm64 and renders a correct in-between frame through MoltenVK. And nothing in
the shipped build path builds it, so a Mac has no RIFE engine today.

What the spike measured, from the commit `packaging/plugins/repack-rife-ncnn.sh`
pins (`c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f`, tag `r9_mod_v33`):

- **it builds, with the patches ButterEye already carries.**
  `buttereye-vs-rife-ncnn-no-pack8.patch` and
  `buttereye-vs-rife-ncnn-host-pool.patch` both apply with no fuzz; the
  Windows-paths patch is `#if _WIN32` and is not wanted here. Both variants
  build: against Homebrew's `ncnn` 20260526 with `-Duse_system_ncnn=true` (4
  ninja targets, a 167 536-byte bundle) and with the bundled pinned ncnn (466
  targets, 5 864 160 bytes, static). The two `%prep` seds the Fedora spec
  already does are needed here too — one of them because Homebrew's
  `vapoursynth.pc` carries no `libdir` at all.
- **it renders.** `[0 Apple M2]` through MoltenVK, with `VK_ICD_FILENAMES` and
  `VK_DRIVER_FILES` unset for every run. The in-between frame scores 0.0045 mean
  absolute error against the geometric truth where a blend of the two neighbours
  scores 0.0331 — the check `tests/integration/test_mvgpu_real.py` applies on
  Linux, reproduced for RIFE in RGBS, and all four build/model combinations pass
  its ratio threshold. Not black, not a blend, not a copy, and source frames
  pass through bit-exactly.
- **it is offline-only.** 8.5 fps at 1080p against Homebrew's ncnn and 9.8 with
  the bundled one, against the ≈48 fps a live 2× 24p needs and SCOPE F2's 1.15×
  headroom on top of that — about five times too slow for live playback at
  1080p, where only 640×360 on the bundled build clears the headroom. Offline
  render is unaffected: same engine, and the estimates already scale from a
  measured benchmark. `gpu_thread` has to be 1 on Apple Silicon: MoltenVK
  reports one compute queue, and the plugin warns and gains nothing above 1,
  where ButterEye's default is 4.
- **`-DVulkan_LIBRARY` is not needed**, against what this page said before the
  spike measured it. ncnn's `src/CMakeLists.txt` does warn about it for
  `simplevk` static linkage on Apple, but RIFE's own `dependency('vulkan')` puts
  `-lvulkan` on the final link, and that is what simplevk's symbols bind to.
  Passing it is still worth doing in a real build script, to silence the warning
  and pin the choice explicitly.
- **the OpenMP defect is real, and it is two flags rather than a patch.** The
  plugin's `meson.build` gives clang `-Xpreprocessor -fopenmp` and no link
  arguments; the bundle links anyway because `shared_module` gets
  `-undefined dynamic_lookup`, and loads anyway because the CPU warp path is
  never taken while Vulkan works. Forcing the binding shows the hole (`symbol
  not found in flat namespace '___kmpc_for_static_fini'`), and a build that ever
  did reach the CPU warp would abort instead of falling back.
  `-Dcpp_args=-I<libomp prefix>/include` and
  `-Dcpp_link_args="-L<libomp prefix>/lib -lomp"` fix it, measured.
- **Homebrew's ncnn is not the same product as the pinned one.** 21.3 % of
  output samples differ, 1.8 % of them by more than one 8-bit step. The cause is
  the ncnn version and not ButterEye's patch: the bundled build is bit-identical
  with and without the no-pack8 patch, because `elempack == 8` never reaches the
  custom warp layer through MoltenVK. A macOS build on the bottle must therefore
  not be described as "the same build as Linux".
- **one new ButterEye patch is needed**, and it is a crash rather than a
  cosmetic fix: the model-path stack overflow recorded under "Plugins" near the
  top of this page. It is a cross-platform defect, not a macOS one, and it has
  no owner.

What is missing is the build step itself. `build-plugins.sh` reserves it — if a
`packaging/macos/build-rife.sh` appears beside it, it runs it, merges its licence
directory and its `components.json` entries, and reports what happened — and
until then every run prints that RIFE is not built and that macOS therefore has
no RIFE engine. Writing that script was not in phase 3's scope; the spike's own
"What a real macOS build needs, concretely" is the list it has to satisfy, and
the ncnn choice it leaves open (the bottle, or the pinned bundle for
reproducibility) is a decision for phase 4's tap rather than a measurement.
Phase 4 wrote that script and chose the bottle, keeping the bundled build behind
`BUTTEREYE_RIFE_NCNN=bundled`; the trade it accepted is under "RIFE-ncnn: built
at last, and offline only", below.

Still unmeasured: whether a macOS RIFE frame matches a **Linux** RIFE frame
(both macOS variants were compared against each other and against the geometric
truth, and there is no Linux machine here), GPU-fault behaviour under a real
soak (8 of 8 1080p runs were clean, where M0(f) needed 20–42 runs to see the
NVIDIA faults, and macOS gives the doctor no fault history to read anyway), and
RIFE inside mpv. Phase 4 removed the VSScript blocker that one waited on, so it
is now measurable rather than impossible — but it is also not the point, because
8.5–9.8 fps at 1080p is five times too slow for live playback, so RIFE on a Mac
stays an offline-render engine.

#### What phase 3 does not do

**macOS is still not a supported platform.** What phase 3 produced is one
`.dylib` in a cache directory, a script that builds it, a check that refuses a
build a user's Mac could not load, and a test that grades the frame. It did not
produce the four things below; **phase 4 produced the first three of them**, and
the bullets are kept as the record of what it inherited:

- **a distribution.** Nothing is signed or notarised, and nothing is published.
  Phase 4 is a Homebrew tap, chosen because `brew` does not mark what it fetches
  with `com.apple.quarantine` while an unsigned `.app` from a browser download
  does carry it and Gatekeeper refuses it outright.
- **live playback.** mpv cannot load VSScript by itself: it `dlopen`s
  `libvsscript.dylib` and then `libvapoursynth-script.dylib` by bare leaf name,
  and Homebrew's only copy is inside a `libexec` virtualenv, which is on no dyld
  search path. Until something sets `VSSCRIPT_PATH` or a `DYLD_*` library path,
  the in-mpv probe fails and so does the live filter. Phase 4 sets both.
- **RIFE in the build path.** The spike above proves the plugin builds and
  interpolates correctly on this Mac; `build-plugins.sh` does not build it, so at
  the end of phase 3 the engine list on a Mac was MVTools and `mvgpu` and nothing
  else — and TensorRT never, which phase 1's seam already says.
  `packaging/macos/build-rife.sh`, phase 4, is the step that fills the hook.
- **a verdict on real footage.** The numbers above are synthetic clips through
  `vspipe` on one M2. Phase 5, the real-hardware gate, is what would decide
  whether any of this is worth releasing.

### Phase 4: live smoothing works, and a Homebrew tap

Windows phase 4 is a per-user installer that brings its own Python, Qt and
VapourSynth. macOS phase 4 is two much smaller things, and why it is smaller is
worth writing down: Homebrew's `python@3.14` already **is** the interpreter
ButterEye runs under, and the `vapoursynth` formula drops a
`homebrew-vapoursynth.pth` into that Python's `site-packages` pointing at its own
`libexec` virtualenv, so `import vapoursynth` answers R81 (API R4.3) in
`<prefix>/bin/python3.14` with nothing bundled and nothing staged. There is no
macOS counterpart to the installer's `python\` folder or to
`osapi.point_at_runtime`, and none is wanted. What a Mac needed instead was one
file handed to mpv, and a way to install ButterEye.

#### The headline: a Mac smooths video live, in mpv

`tests/integration/test_hdr_live_real.py` was red in every earlier phase of this
port. Measured on this M2 before phase 4 landed, two of its three tests fail, and
the session snapshot is the diagnosis — `FilterState.ROLLED_BACK`, `BE-3003`,
"The smoothing filter failed and was removed; playback continues" — after
ButterEye had correctly decided "24 → 48 fps with MVTools (CPU) at 640 × 360" and
"HDR10 smoothed with the experimental HDR pass-through". After phase 4, with
**no** environment variable set by hand, because ButterEye sets it itself:
**3 passed**, in about three seconds (3.14 s when the fix landed, 3.03 s
re-measured afterwards with `VSSCRIPT_PATH`, `DYLD_LIBRARY_PATH` and
`DYLD_FALLBACK_LIBRARY_PATH` removed from the environment first — the seconds
move from run to run and only the three passes are the result). Real HDR10 live
smoothing, through the real Homebrew mpv, on real Apple hardware, with the colour
parameters mpv hands its video output unchanged by the filter — which is the gate
that test exists for. Exporting `DYLD_FALLBACK_LIBRARY_PATH` by hand instead
gives the same three passes, which is how the fix was found before it was
written.

Plain `buttereye play` says it in one line, measured on this Mac with nothing
exported: **"24 → 60.002 fps with mvtools (active, ok)"** — `active`, not rolled
back, with the target picked from the 60 Hz display. Reproduced word for word on
a second run with `VSSCRIPT_PATH`, `DYLD_LIBRARY_PATH` and
`DYLD_FALLBACK_LIBRARY_PATH` removed from the environment, on a generated 24 fps
clip.

The whole doctor moves the same way: `11 ok, 4 info, 1 degraded, 1 blocking`
before, the blocker being BE-1005, against `13 ok, 5 info, 1 degraded, 0
blocking` and exit code 0 now, again with nothing exported. The one degraded row
left is RIFE not being installed — which this phase also makes installable.

The blocking row was the in-mpv probe, which had never passed on a Mac in any
earlier phase, and it is worth reading what it says rather than that it is green:
`probe.vs_version` is ok with "mpv runs VapourSynth R81 with Python 3.14.8" and
`VapourSynth R81 (API 4.3)` in its evidence, and `probe.plugins` is ok with
"MVTools load inside mpv" (`doctor --json`, nothing exported). That is
VapourSynth and MVTools running inside mpv's own process, which is the thing the
bare-name `dlopen` had made impossible. A passing probe emits no `probe.run` row
at all — that row exists to say why the probe could not run — which is what a
phase-2 test that waited for this had to be taught (`tests/macos/test_doctor_macos.py`).

Offline render was never blocked by this, and now has an end-to-end measurement
it did not have before: on this Mac `buttereye render --target 2x` turned a
48-frame 24 fps H.264 MKV into a 96-frame 48 fps HEVC MKV through the real
`vspipe`, `ffmpeg` (libx265) and `mkvmerge` v102.0, with MVTools as the engine
and with **no** VapourSynth variable set at all — re-measured the same way
afterwards, `ffprobe -count_frames` on the output reporting `codec_name=hevc`,
`r_frame_rate=48/1` and 96 frames — `vspipe` loads VapourSynth as a
Python module and never goes near VSScript's bare-name `dlopen`. No test covers
that: all four of `tests/integration/test_render.py`'s tests are `devbox`-marked
and skip on a Mac for want of `rpm` and the `buttereye-*` RPMs.

#### The diagnosis, exactly, because it was mis-stated more than once

Four facts, each measured here against Homebrew's mpv 0.41.0:

1. **mpv has the filter.** `mpv --vf=help` prints
   `vapoursynth      VapourSynth bridge`. (`mpv --version` prints no feature
   list on this build; that list is in a build log, not in the binary.)
2. **mpv does not link VSScript; it `dlopen`s it by bare leaf name.**
   `otool -L <prefix>/bin/mpv` lists 61 libraries and not one is VapourSynth,
   while `strings` finds `getVSScriptAPI` next to **two** leaf names,
   `libvsscript.dylib` and `libvapoursynth-script.dylib`. A bare leaf name sends
   dyld to a fixed list: `DYLD_LIBRARY_PATH`, then the calling image's own
   `LC_RPATH` entries, then `/usr/lib` and the dyld cache. mpv's only two
   `LC_RPATH`s are Swift toolchain paths
   (`…/XcodeDefault.xctoolchain/usr/lib/swift/macosx` and `/usr/lib/swift`), so
   Homebrew's prefix is never looked at — which is why a symlink in
   `<prefix>/lib` would not help, and why the tap ships none
   (`packaging/macos/SOURCES.md` records that as a packaging decision that is
   really a measurement).
3. **Both names are tried, `libvsscript.dylib` first**, and this is the part the
   record had wrong. mpv's error text names only `libvapoursynth-script.dylib`,
   which reads as though the other name were never reached.
   `DYLD_PRINT_SEARCHING=1` says otherwise:

   ```
   dyld[…]: find path "libvsscript.dylib"
   dyld[…]: find path "libvapoursynth-script.dylib"
   ```

   mpv reports only the last failure. So **nothing has to be renamed or
   symlinked**: a directory holding Homebrew's own `libvsscript.dylib`, under its
   own name, is enough. Measured on the same 320×240 clip and a one-line
   pass-through `.vpy`, four routes all reach `VO: [null] 320x240 yuv444p` with
   the filter in the chain — `VSSCRIPT_PATH` at the file, `DYLD_LIBRARY_PATH` at
   the real directory, `DYLD_FALLBACK_LIBRARY_PATH` at the real directory, and
   either variable at a directory of symlinks under either name.
4. **The library exists, under the other name, somewhere dyld will not look.**
   The only copy here is
   `<prefix>/Cellar/vapoursynth/81/libexec/lib/python3.14/site-packages/vapoursynth/libvsscript.dylib`,
   inside the formula's own virtualenv, and `<prefix>/lib` holds no
   `libvapoursynth*` and no `libvsscript*` at all. Both halves of that matter: it
   is not that Homebrew ships no such file, it is that the file it ships is
   called something else and sits where dyld never searches. An earlier reading
   of this page took the first half for the whole.

Two more measurements, because together they decide how the fix has to behave.

**`VSSCRIPT_PATH` wins and does not fall back.** Pointed at a file that does not
exist, it makes mpv fail with that path in its error and no filter created — even
with `DYLD_FALLBACK_LIBRARY_PATH` pointed at the real directory in the same run:
`Failed to load VapourSynth VSScript library: dlopen(/nonexistent/libvsscript.dylib,
0x0006): tried: … (no such file)`, then `Creating filter 'vapoursynth' failed`.
Pointed at some other real dylib it gets one step further and no further:
`Failed to load VapourSynth VSScript library: dlsym(0x…,
getVSScriptAPILastError): symbol not found`. A wrong value is therefore worse
than none, and whatever sets the variable must set a path it has just derived
from the VapourSynth that is installed now.

**`DYLD_LIBRARY_PATH` rewrites even an absolute `dlopen`, and that is why the fix
does not use it.** The same dangling absolute `VSSCRIPT_PATH`, with
`DYLD_LIBRARY_PATH` at the keg directory instead of the fallback variable,
**plays** — `VO: [null] 640x360 yuv420p`, the filter in the chain — because dyld
matched the leaf name of a path that does not exist against the directory it was
handed and loaded the keg's library in its place. `DYLD_LIBRARY_PATH` is consulted
before a library's own path is honoured, for every library in every child
process; `DYLD_FALLBACK_LIBRARY_PATH` is consulted only after the normal search
has failed, so it cannot shadow a library a child asked for by full path. Both
load the filter when the path is right, which is the whole reason the blunt one is
not worth its blast radius.

#### What ButterEye does about it

`osapi.use_bundled_runtime`, which `ButterEye.open()` calls and which until now
returned immediately on anything but Windows, has a macOS branch. It bundles
nothing: it hands mpv the file.

- **`osapi.vsscript_library() -> Path | None`** finds VSScript without writing
  down a version, so the runtime, the doctor and the tap ask the same question:
  `probe.vsscript_library` now asks the seam first and keeps its own
  `BREW_VSSCRIPT_GLOBS` only as the fallback a planted-prefix test needs. Three
  directories in order (`_darwin.vsscript_library`, `_darwin._vsscript_dirs`,
  `_darwin.VSSCRIPT_NAMES`), each read as files and **never imported** — SCOPE
  §8.1 keeps VapourSynth out of ButterEye's own process, and
  `tests/boundary/import_guard.py` refuses the name outright for the GUI, which is
  why even `importlib.util.find_spec` is not used here where
  `osapi.runtime_dirs` uses it on Windows: a `vapoursynth` package directory on
  `sys.path` (a Homebrew `python@3.14`, whose `.pth` puts the keg's
  `site-packages` there, or a `pip install vapoursynth`), then every
  `vapoursynth` keg's `libexec` virtualenv under Homebrew's prefix, newest
  version first (`_darwin._KEG_GLOB`, `_darwin._keg_key`), then `<prefix>/lib`.
  The second route is what answers inside a `python -m venv` made without
  `--system-site-packages`, which is the shape of this repository's own test
  environment; the third is where a from-source `make install` leaves the file.
  Both names mpv asks for are accepted: `libvsscript.dylib` is what
  VapourSynth's meson build installs beside its Python module, and
  `libvapoursynth-script.dylib` is the name its older autotools build leaves in
  `<prefix>/lib`. Nothing is cached and no file is created, so there is no stale
  answer to validate — a `brew upgrade vapoursynth` moves the keg, because the
  path carries both the formula version and the Python minor version, and the
  next `ButterEye.open()` derives the new one.
- **That `sys.path` route was a way to run code inside mpv, and phase 5 found it
  open.** What `vsscript_library()` returns is handed to every child mpv as
  `VSSCRIPT_PATH` and `dlopen`-ed in the player's own process, so it is not an
  informational path: it is a library ButterEye asks another program to load.
  Under `python -m buttereye.cli` — which is how ButterEye runs from a checkout,
  and there is no installed console script on this machine — `sys.path[0]` **is
  the working directory**, so running ButterEye from a directory holding
  `./vapoursynth/libvsscript.dylib` handed that file to mpv. Measured before the
  fix: `vsscript_library()` answered with a planted copy under `/private/tmp`.
  Two checks close it, both in `_darwin` and both named rather than inlined:
  `_vsscript_dirs` never asks the working directory, however it reaches
  `sys.path` (the empty entry, the path itself, or anything that resolves to it),
  and `_trustworthy_dir` refuses any candidate directory not owned by this uid or
  writable by group or other. Both halves are needed — the owner test alone
  accepts a group-writable directory of our own, and the mode test alone accepts
  a tight one belonging to somebody else, who can loosen it whenever they like.
  Re-measured 2026-10-13 with both attacks re-run: the planted directory is no
  longer among the directories `_vsscript_dirs` yields, a 0777 `vapoursynth`
  directory reached through `PYTHONPATH` is refused by `_trustworthy_dir`, and
  both runs answer with Homebrew's real
  `<prefix>/Cellar/vapoursynth/81/libexec/…/libvsscript.dylib` instead. Nothing
  legitimate is lost, because an installed VapourSynth is never in the working
  directory. Tested at both levels, each test leading with the control that the
  same plant *is* returned before the attack is arranged, so a search that
  stopped reading `sys.path` would fail rather than pass: four cases in
  `tests/core/test_windows_runtime_pure.py`
  (`test_macos_vsscript_is_never_taken_from_the_directory_it_was_started_in`,
  `…_skips_the_working_directory_under_either_spelling`,
  `…_is_never_taken_from_a_directory_others_can_write`,
  `…_is_never_taken_from_a_directory_another_user_owns`) which need no Homebrew
  and so run in the macOS CI job, and the same two attacks against the real
  library in `tests/macos/test_runtime_macos.py` behind `BUTTEREYE_TEST_BREW`.

  **The lesson belongs to the phase-4 design and not only to this function.**
  `point_at_vsscript` below exports `DYLD_FALLBACK_LIBRARY_PATH` into *every*
  child ButterEye spawns, so any directory that reaches this answer becomes a
  directory those children load libraries from. Every candidate therefore has to
  be one only this user controls, and a later simplification of this search —
  "just take `sys.path`", "just use `find_spec`" — reintroduces the hole. That is
  why the skip and the ownership test are two separate functions carrying their
  measurement in their docstrings rather than two lines folded into the loop.
- **`osapi.point_at_vsscript`** puts two variables in the environment the
  children inherit. `VSSCRIPT_PATH`, the exact file, because mpv honours it on
  every platform and not only on Windows (phase 1 measured that) and because it
  is not a `DYLD_*` variable, so dyld never strips it. And
  `DYLD_FALLBACK_LIBRARY_PATH`, the file's directory, which answers the bare-name
  `dlopen` for an mpv that ignores `VSSCRIPT_PATH`. **The fallback variable
  rather than `DYLD_LIBRARY_PATH`, deliberately**, for the reason measured above:
  `DYLD_LIBRARY_PATH` is consulted before a library's own path is honoured and
  was seen to substitute its own leaf-name match for an absolute `dlopen`, in
  every child and for every library, where the fallback list is consulted only
  after the normal search has failed. Both load the filter, so the blunter one
  buys nothing. `VSSCRIPT_PATH` is always ButterEye's own derived answer and
  never an inherited one: mpv does not look past a `VSSCRIPT_PATH` it cannot
  load, the Cellar path carries VapourSynth's version, so a value exported from a
  shell before a `brew upgrade` is a single stale string that kills live
  smoothing on a machine where everything else is in order
  (`test_a_stale_vsscript_path_breaks_mpv_and_is_replaced_rather_than_trusted`).
- **`DYLD_*` is stripped for a protected child, and that is in a comment rather
  than assumed.** `<prefix>/bin/mpv` is user-installed and ad-hoc signed without
  the hardened runtime, which is why the fallback variable reaches it at all.
  Point ButterEye at an mpv inside a signed app bundle or under `/usr/bin` and
  the `DYLD_*` entry is stripped; only `VSSCRIPT_PATH` survives, which is enough
  for an mpv that reads it and otherwise fails exactly as it did before, with
  mpv's own "Failed to load VapourSynth VSScript library" in the session log.
- **It never raises and never stops ButterEye opening.** The branch is wrapped in
  `contextlib.suppress(OSError)` and returns without touching the environment
  when `vsscript_library()` is None, because the caller is `ButterEye.open()` and
  a Mac with no VapourSynth has to open anyway — the doctor is what tells the
  user.

No symlink is created anywhere, by ButterEye or by the tap: the alternative was
measured away rather than skipped, by facts 2 and 3 above.
`tests/macos/test_runtime_macos.py` covers both halves, including a real mpv run
with the fix and without it; it needs Homebrew's VapourSynth and is behind
`BUTTEREYE_TEST_BREW`, so the parts that can run without a bottle — the
`VSSCRIPT_PATH`/`DYLD_FALLBACK_LIBRARY_PATH` contract and the four
trusted-directory cases of the bullet above — live in
`tests/core/test_windows_runtime_pure.py` and run in CI.

#### Why a tap and not an `.app`

It is a decision about money, not an engineering gap, and this page should not
imply the bundle is merely unwritten. Notarising an application needs a paid
Apple Developer ID (99 USD a year). Without one, an unsigned or ad-hoc-signed
bundle downloaded from a GitHub release arrives carrying `com.apple.quarantine`
and current macOS refuses it outright — the Control-click ▸ Open bypass is gone
from the UI. Measured here on macOS 27.0.1 (build 26A434): a freshly compiled
binary reports `flags=0x20002(adhoc,linker-signed)`, `Signature=adhoc`,
`TeamIdentifier=not set`, and with `com.apple.quarantine` set on it
`spctl -a -t exec` answers `rejected` (exit 3).

A tap sidesteps that rather than working around it: `brew` fetches over curl and
marks nothing it downloads. Measured — `xattr -l` prints nothing at all for a
bottle tarball in `~/Library/Caches/Homebrew/downloads/` and nothing for an
installed keg's dylib — so Gatekeeper is never asked and nothing in the tap needs
to be signed or notarised. If the owner ever buys a Developer ID an `.app`
becomes possible; until then it is not a thing waiting to be written, it is a
thing that would not run.

#### The tap

`packaging/macos/homebrew/` holds four formulae. ButterEye's own code and the
engines are separate formulae because Homebrew has no peer of rpm's
`Recommends:` — every `depends_on` is mandatory — and ButterEye runs with the
MVTools path alone, so making a GPU engine compulsory would force a long source
build on a user who did not want one.

| Formula | What it installs, and where | Licence |
|---|---|---|
| `buttereye.rb` | ButterEye itself in a virtualenv, with the `buttereye` and `buttereye-gui` commands; `COPYING` and `LICENSES/` into `doc` and symlinked into `site-packages`, which is where `licences.py` looks for the texts (**unverified**: no formula has been installed, and phase 4 did change `licences.py` for a Mac — the conveyance table and the source offer, below); `build-plugins.sh` and `check-dylibs.sh` into `pkgshare` | AGPL-3.0-or-later AND LGPL-3.0-or-later (the bundled FSRCNNX shader), the same expression as `packaging/buttereye.spec` |
| `buttereye-vs-mvgpu.rb` | `libmvgpu.dylib`, built from `mvgpu/` with meson and Homebrew's `glslang`, into `<prefix>/lib/buttereye/vapoursynth` — private and **not** autoloaded, the analogue of Fedora's `%{_libdir}/buttereye/vapoursynth` | AGPL-3.0-or-later |
| `buttereye-vs-rife-ncnn.rb` | `librife.dylib`, built against Homebrew's `ncnn` with ButterEye's three patches and the two `libomp` flags, into the same private directory | MIT AND LGPL-2.1-or-later AND WTFPL (the plugin, the bundled `VapourSynth4.h` and `VSHelper4.h`) — "MIT" alone would be wrong |
| `buttereye-rife-ncnn-models.rb` | the three curated RIFE models into `<prefix>/share/buttereye/rife-ncnn-models`, which is where `types._packaged_data_dir` looks | MIT |

What a user would type, once phase 5 has tagged a release and filled the
checksums in. **Nobody has run these lines**, here or anywhere:

```sh
brew tap <owner>/buttereye
brew install buttereye                                           # the application
brew install buttereye-vs-mvgpu                                  # optional: GPU motion vectors
brew install buttereye-vs-rife-ncnn buttereye-rife-ncnn-models   # optional: RIFE, offline render only
buttereye doctor
```

`buttereye.rb` depends on `ffmpeg`, `ffms2`, `molten-vk`, `mpv`, `pyside`,
`python@3.14`, `vapoursynth`, `vapoursynth-mvtools` and `vulkan-loader`, and on
`:macos` with `arch: :arm64` — stated rather than discovered, because every
macOS measurement behind phases 0 to 4 was made on Apple Silicon, `arch` alone
would be satisfied by a Linux aarch64 machine where none of this works, and an
Intel Mac's prefix is `/usr/local`, which nothing here has tried.

`mkvtoolnix` and `vulkan-tools` are deliberately **not** dependencies. Each is
wanted for one thing — keeping a source's subtitles, chapters and extra audio
through a render; `vulkaninfo` for the GPU list — ButterEye degrades without
either and already prints the `brew install` line at the moment it wants one
(`commands.install_command`), and `mkvtoolnix` alone pulls 11 direct and 42
recursive dependencies (measured with `brew deps`), including a Qt its own GUI
needs and ButterEye never starts. `pyside` *is* a dependency, and its cost is
stated rather than hidden: 29 direct dependencies and 69 recursive, among them
`qtwebengine` and `llvm`, where ButterEye's GUI uses Qt Essentials only
(SCOPE F17, enforced by a test) — so what the Addons buy is disk. It is still
the right choice, because `pyproject.toml` declares no Qt dependency on purpose
and neither the formula nor the RPM should have `pip` fetch a Qt of its own.

Three things the formulae get right that are easy to get wrong, each with the
measurement in a comment beside it:

- **`system_site_packages` must stay Homebrew's default of true.** VapourSynth's
  Python module is not a wheel a formula could install; it is that `.pth` in
  `python@3.14`'s `site-packages`. Measured on this Mac: a venv made with
  `--system-site-packages` imports `vapoursynth` R81 from the Cellar and the same
  venv without the flag answers `ModuleNotFoundError` — which is exactly why
  `osapi.vsscript_library` needs its Cellar route as well as its `sys.path` one.
  Passing `system_site_packages: false` would also hide Homebrew's PySide6 and
  the window would stop opening.
- **A Mac has two plugin directories, and the doctor had to be taught both.**
  Homebrew VapourSynth's autoload directory holds the `vapoursynth-mvtools` and
  `ffms2` bottles, because a formula cannot install into another formula's keg;
  `<prefix>/lib/buttereye/vapoursynth` holds what the tap and
  `packaging/macos/build-plugins.sh` build. A Mac with the tap has plugins in
  both at once, so this is not a fallback chain, and asking only the first
  answer was measured to ship a **false BLOCKING row**: installing a tap plugin
  formula created ButterEye's own directory, `types.macos_plugin_dir()` stopped
  falling back to the autoload one, and `packages.mvtools` went degraded with
  `packages.plugins` blocking and exit 3 on a Mac whose autoloaded MVTools was
  interpolating at that moment. **Fixed in this phase, in `buttereye/core`**:
  `types.macos_plugin_dirs()` answers the search path — the autoload directory
  first, because a plugin there is in the core before a generated script runs and
  is the copy that wins — and the doctor asks it through
  `checks_pkgs.plugin_dirs`, `checks_pkgs.installed_plugin` and
  `checks_pkgs.plugin_dir_text`, so the RIFE, MVTools, mvgpu and ffms2 rows all
  look in both. `types.macos_plugin_dir()` stays one directory for the one
  question that has room for one answer, a generated script's `PLUGIN_DIR`.
  Checked here against a planted prefix: with ButterEye's own directory present
  and holding `libmvgpu.dylib`, `installed_plugin` still finds `mvtools.dylib` in
  the autoload directory. The formulae create the private directory with a plugin
  in it, which is now a packaging detail rather than the thing holding the
  blocking row off.

  **Read "the doctor had to be taught both" literally: in phase 4 only the doctor
  was.** The search path lived in `checks_pkgs`, so the engine chooser, the
  benchmark and `session.can_switch_to_cpu` went on asking
  `paths.rpm_plugin_dir` alone — which on a Mac with the tap is ButterEye's own
  directory and not the one Homebrew's MVTools is in. Phase 5 measured the
  consequence (MVTools absent from `installed_backends` on a tap-shaped prefix)
  and finished the fix by moving the list to `paths.plugin_dirs`; the phase-5
  section below has the before-and-after. Nothing in this paragraph was wrong,
  but it was read as more than it said, which is why the limit is now stated here
  too.
- **No Cellar path may be baked into a plugin**, or a `brew upgrade` of `ncnn` or
  `libomp` breaks it; the RIFE formula's own `test do` block refuses a
  `/Cellar/` in `otool -L`, which is the one rule of
  `packaging/macos/check-dylibs.sh` a formula can check by itself.

**Licences and the source offer.** A tap conveys source, not binaries: `brew`
downloads the upstream archive and builds it on the user's machine, so the
Corresponding Source is what it fetched, and each formula carries its own licence
expression and installs its texts. `packaging/macos/SOURCES.md` is where the tap
states that — and phase 4 also had to make ButterEye's **own** notice say the
same thing, because it did not. The Licences pane and `buttereye licence` were
showing the Fedora conveyance table on a Mac, which declares ncnn and glslang
"shipped, statically linked into the RIFE plugin" and MVTools "shipped", where
the tap links Homebrew's `ncnn` and `libomp` dynamically and conveys no MVTools
at all. `licences.components(macos=True)` (`licences._macos_rows`) is the macOS
table, with `SOURCES.md` as its authority: what the tap's own formulae build is
conveyed, every bottle is the user's install under Homebrew's formula and its own
licence, and the three Fedora rows with no macOS peer (glslang, `vsmlrt.py`,
`onnxconverter-common`) are left out rather than re-related. The source offer
moved with it (`licences._source_link`, `licences.MACOS_SOURCE`): the repository,
because a Mac has no source RPM to download, and the tap's `SOURCES.md` in
ButterEye's own keg in preference to it — a path no formula writes yet
(`buttereye-vs-mvgpu.rb` installs `SOURCES.md` into its own `doc`, not
ButterEye's), so until one does, a Mac answers with the repository. Two debts are open and named rather than papered over: the RIFE
formula still has to add `packaging/plugins/licenses/`' pinned texts as
`resource`s, because the licences SCOPE §9 asks for are not all inside the
upstream tarball; and the repacked RIFE tarball the formula's `url` names does
not exist yet — it could not even be produced on this Mac, because
`repack-rife-ncnn.sh` needs GNU `tar --sort` and this machine has only
`bsdtar 3.5.3` and no `gtar` (measured).

**What is not verified, and it is a lot.** No formula has been installed,
bottled, `brew audit`ed or `brew style`d. `ruby -c` passes on all four
(`Syntax OK`); `brew style` was not run because rubocop is not vendored in this
Homebrew and running it would install gems into the user's installation, which no
phase of this port does. **Nothing the tap names can be fetched**: every formula's
`url` points into a `v0.0.3` release, the newest tag in this repository is
`v0.0.2` (`git tag`), and every `sha256` is 64 zeros — a placeholder that fails a
download rather than installing something unverified. `v0.0.2` predates the whole
macOS port anyway — phases 0 and 1 land after it and 2 to 4 are not committed —
so a formula built from that tag would install a ButterEye with no macOS platform
layer, and in `mvgpu`'s case a `libmvgpu.dylib` that cannot create a Vulkan
instance on any Mac at all (the portability retry is not in that tag). `--HEAD`
is the only path with anything behind it, and only for two of the four:
`buttereye.rb` and `buttereye-vs-mvgpu.rb` carry a `head` line, while the RIFE
plugin and the models formulae have none, so RIFE could not be installed from
this tap today by any route. Tagging a release, filling those in, and installing
the result on a clean machine is still the first unblocking step, and phase 5
deliberately left it to the owner: `brew install` writes into the Homebrew prefix.
Phase 5 writes down the commands instead.

#### RIFE-ncnn: built at last, and offline only

`packaging/macos/build-rife.sh` is the step `build-plugins.sh` had only reserved,
and it satisfies that hook's contract rather than just producing a dylib: the
plugin, the three models, `licenses/buttereye-vs-rife-ncnn/`,
`licenses/buttereye-rife-ncnn-models/`, its paragraph of the written offer in
`licenses/SOURCES.rife.txt` (concatenated onto `SOURCES.txt`, never overwriting
it), its Corresponding Source under `sources/`, and `components-rife.json`.
`build-plugins.sh` refuses to finish a build that has a `librife.dylib` without
those, which is the whole point of the contract: `librife.dylib` is not
ButterEye's own code.

Run here, it produces a 162 528-byte `librife.dylib` next to `libmvgpu.dylib`,
and `check-dylibs.sh` passes it: arm64 Mach-O bundle, `_VapourSynthPluginInit2`
exported, linking `opt/libomp`, `opt/ncnn`, `libc++` and `libSystem` and nothing
else — no Cellar path, no `@rpath`, no `LC_RPATH`.
`tests/macos/test_plugins_macos.py` passes 9 of 9 against the output directory,
and the plugin really runs: loaded into Homebrew's VapourSynth R81 it reports
`[0 Apple M2]` through MoltenVK and turns 8 frames into 16.

It is the one step that downloads — `build-plugins.sh` does not, and its header
says so — because neither the plugin source nor the ~20 MB of model weights is in
this repository. Everything fetched is pinned: a blobless sparse clone of the
commit `repack-rife-ncnn.sh` pins, with the commit hash verified before anything
is built (the sparse checkout is what keeps upstream's ~1.4 GB of models out: the
plain tarball of that commit is 1.37 GB), the model files checked against the
SHA-256 block in `packaging/plugins/PROVENANCE-rife-ncnn-models.md`, and the
bundled variant's two tarballs against the sums in the spec's `Source2`/`Source3`.

**Which ncnn, and the trade.** Default and tap: Homebrew's `ncnn`
(`-Duse_system_ncnn=true`), with `BUTTEREYE_RIFE_NCNN=bundled` kept working for
the pinned static one. The choice is not a detail — the phase-3 spike measured
that the two are not the same product, 21.3 % of output samples differing and
1.8 % by more than one 8-bit step — so what each costs is written down:

- the bottle gives a 167 KB bundle against 5.9 MB, 4 ninja targets against 466,
  and Homebrew doing the security updates on a library every other consumer
  links;
- it costs **reproducibility**, which is the real price: a tap pins a formula
  version, not bytes, so a `brew` bump of `ncnn` moves ButterEye's pixels with no
  change on ButterEye's side. Nothing here may call this build "the same as
  Linux", and neither the run nor `SOURCES.rife.txt` does;
- the other reason Fedora pins does **not** carry over. Those were NVIDIA GPU
  faults with ncnn 20250916 at `gpu_thread ≥ 2` (`docs/spikes/m0f.md`), and an M2
  exposes one compute queue, so `gpu_thread` is 1 whichever ncnn is used.

**Offline only, and no user-facing string may say otherwise.** 8.5–9.8 fps at
1080p on an M2 against the ≈48 fps a live 2× 24p needs, before SCOPE F2's 1.15×
headroom: about five times too slow. `buttereye render` uses it; live playback
should not. `gpu_thread` must be 1, where ButterEye's default is 4. The RIFE
formula's `caveats` says both of those and the ncnn difference, in those words.

`-DVulkan_LIBRARY` is deliberately **not** passed, against what this page said
before the spike measured it: RIFE's own `dependency('vulkan')` puts `-lvulkan`
on the final link, the define only reaches an ncnn cmake subproject the default
build does not have, and passing it would mean a third edit into upstream source
that Fedora does not make — not worth it to silence a cosmetic warning.

#### What phase 4 does not do

**macOS is still not a supported platform**, and phase 4 does not make it one.

- **Nothing is published or installed.** The tap is four files in this
  repository; no formula has been installed, bottled or audited, their `url`s
  name a `v0.0.3` release that does not exist and their `sha256`s are 64 zeros,
  so there is no route by which any of it installs today. Nothing is signed or
  notarised and nothing in a tap needs to be.
- **The declared-dependency debt is only half paid.** Phase 3 left it:
  `check-dylibs.sh` allows a plugin to link `ncnn` and `libomp` while nothing in
  `buttereye/` declares either. `buttereye-vs-rife-ncnn.rb` now declares both, so
  a tap install gets them — but `checks_pkgs.BREW_FORMULAE` still does not list
  them, so the doctor cannot explain a hand-built `librife.dylib` that will not
  load on a Mac without those two formulae. **Still open after phase 5**:
  `checks_pkgs.BREW_FORMULAE`'s ten formulae are `ffmpeg`, `ffms2`, `mkvtoolnix`,
  `molten-vk`, `mpv`, `sevenzip`, `vapoursynth`, `vapoursynth-mvtools`,
  `vulkan-loader` and `vulkan-tools` — neither `ncnn` nor `libomp` among them.
- **Two doctor strings have drifted behind the build.** The "mvgpu is not
  installed" finding still says ButterEye "doesn't build mvgpu for macOS yet"
  (`checks_pkgs.py`), which phase 3 made untrue and this tap makes installable.
  And BE-1005's cause names only `libvapoursynth-script.dylib` as the bare name
  mpv looks for (`probe.MPV_VSSCRIPT_NAME`), where the measurement above is that
  mpv asks for `libvsscript.dylib` first and reports only the second failure —
  accurate about the name in mpv's error text, incomplete about what mpv does.
  Both are strings in `buttereye/core/doctor/`, not in this page.
- **No measurement of a Linux frame against a macOS frame.** RIFE on Homebrew's
  ncnn is known to differ from the pinned build by 21.3 % of samples; what has
  never been compared is a macOS render against a real Linux render, because
  there is no Linux machine here.
- **No real-hardware gate.** Everything above is one M2 running macOS 27.0.1.
  Live RIFE in mpv is not tried (it is too slow to be the point), on-screen
  playback and its timing are unmeasured, the GUI window on a Mac was unmeasured
  when phase 4 ended, and a clean machine has never installed any of this. That
  is phase 5, below, which measured the GUI and left the rest named rather than
  guessed; the release bar it produced is `docs/macos-release-bar.md`.

### Phase 5: the real-hardware gate (the measuring is done; the decision is not)

Phase 5 builds nothing. Its output is numbers and the honest list of what has no
number yet, and it exists because of Windows: four Windows phases are complete
and Windows is still unreleased for exactly one reason — nobody has run it on a
real Windows PC with a GPU. macOS has no equivalent excuse, because the machine
this port was written on **is** the target. So phase 5 holds macOS to the same
bar Windows is held to, and that bar is not "the tests pass": it is a frame
really interpolated on an Apple GPU, the application as a user meets it, and
benchmark numbers measured on this hardware instead of quoted from the Linux dev
box.

"Done" for this phase therefore means **the measuring is done**. It does not mean
macOS is supported, and phase 5 is not allowed to make it so: see "The support
decision is not phase 5's to make", last in this section.

#### What landed

Several streams of measurement ran at once, and they are reported here as they
landed rather than as they were planned. This is what each left in the tree, and
what it proved or failed to.

- **The release bar** — `docs/macos-release-bar.md`, the checklist form of this
  evidence: the twenty questions Windows is asked, each with a verdict. Four are
  verified (live playback, offline render, the doctor, and code signing in the
  only form a tap has), two are half verified (the GUI, and the benchmark now
  that `docs/spikes/m0s.md` has measured every engine), seven are open, five
  cannot be answered on this machine at all, one is a **measured negative** that
  the documentation has to respect (live RIFE on an M2), and one is impossible
  forever (TensorRT). Read it for the per-item evidence; this section does not
  repeat it. Its recommendation is the owner's to accept or refuse, and the
  sentence in it worth repeating here is why macOS fails the bar differently from
  Windows: what macOS lacks is not evidence that the code works, it is evidence
  that anybody other than its author can get it.
- **The GUI, seen at last.** `tools/screenshots.py` is now three-platform and
  `docs/design/screenshots/macos/` holds **18 views** — the simple window empty,
  on its silent first run, with a live MVTools session and in dark, all four tabs
  of the Details dialog, the setup wizard, and all seven pages of the classic
  window with two of them (Sessions and System) also in dark. The question the release
  bar left to this page is which Qt platform the run actually took, and the
  answer is the real one: `screenshots._qt_platform` asks a **child** process to
  start `QApplication` under `QT_QPA_PLATFORM=cocoa` (a platform plugin that
  fails to start calls `qFatal`, which no `except` in this process could catch)
  and falls back to `offscreen` only if that child dies; run here it answers
  **`cocoa`**. So these are pictures of Qt's `macos` style on a logged-in
  session, not of the offscreen renderer — which is the whole reason they are
  worth having.
- **A blind spot those screenshots exposed, measured for this page.** Both the
  gate and CI run the GUI tests under `QT_QPA_PLATFORM=offscreen`, and the real
  `cocoa` platform does not agree with them. Measured here on the same tree, same
  command, only the platform changed: `tests/gui/test_theme_scale.py` is
  **16 passed** under `offscreen` and **4 failed, 12 passed** under `cocoa` —
  `test_monospace_and_headings_follow_font_scaling` (the monospace face comes
  back at 15.0 pt where the scaled heading is 19.5, because the `macos` style
  hands out `.AppleSystemUIFont` at its own size),
  `test_applied_palettes_meet_contrast_minimums` (4.29 against the 4.5 the test
  demands, in the light scheme's active group),
  `test_dark_palette_keeps_text_contrast`, and
  `test_focus_frame_follows_widgets_without_focus_drawing`, which times out
  waiting for a focus frame the Mac style draws itself. None of that is a defect
  in the port's platform layer and none of it is visible to CI, because a
  headless runner has no window server to give `cocoa` — so this is a real
  finding with no gate behind it, and it wants the GUI phase's owner rather than
  a deselect.
- **Every engine is now measured, and the engine choice is still the weak part.**
  `docs/spikes/m0s.md` (landed after the two paragraphs above were written, and
  after `docs/macos-release-bar.md` item 4) is the first benchmark on an Apple GPU
  with all five configurations present: three RIFE models, mvgpu and MVTools, at
  720p, 1080p, 1440p and 4K. The headline is that **on an Apple machine the GPU is
  the slow part and the CPU is the fast part** — at 1080p in mpv, MVTools 91.07
  against mvgpu 89.21, where the dev box's 4090 runs mvgpu at roughly seven times
  MVTools. RIFE-ncnn is offline-only at every size, by a multiple of the bar
  rather than a few per cent, and nothing reaches real time at 4K. Every figure
  on this page comes from a row of `docs/spikes/m0s-results.json`, which carries
  nineteen runs: two at 720p, three at 1080p, two at 4K and twelve at 1440p, one
  of them discarded as overloaded.

  **1440p, SCOPE §1's v1 real-time target, does not have a settled verdict, and
  an earlier version of this bullet said it did.** It read "at 1440p MVTools is
  the only engine that keeps up", on two runs (mvgpu 52.92 and 53.39 in mpv,
  MVTools 57.16 and 58.84, against the 55.14 the 1.15× headroom demands). A
  reviewer re-ran the spike's own command on a quieter machine and measured mvgpu
  at 51.99 through `vspipe` and **56.92 in mpv — real time**, with `bench`
  recommending mvgpu rather than mvtools; the spike then ran it seven times more
  with the load average recorded beside each. Over **eleven valid runs** mvgpu
  measured 52.77–58.31 in mpv — 96 % to 106 % of the bar, real time in three of
  them — and `bench` recommended mvtools in eight and mvgpu in three. MVTools
  measured 56.33–59.69, real time in all ten runs that reported it but never by
  more than 8 %, and 32.39 in the one overloaded run. The honest statement is
  that at 1440p both motion-vector engines sit *on* the bar and which one clears
  it is not reproducible, machine load being the largest single influence but not
  the whole of it — the two quietest runs, at mean load 3.42 and 3.19, gave 58.31
  and 54.25. So this page states no 1440p winner; `bench`'s real-time verdict is
  a threshold rather than a score, which is why a loaded machine changes the
  answer instead of shading it. `docs/spikes/m0s.md` and `m0s-table.md` own the
  per-run rows and carry the same correction; `docs/macos-release-bar.md` item 4
  carries the consequence for the chooser.

  Those runs needed `HOMEBREW_PREFIX` pointed at a shadow prefix of symlinks,
  because `types.macos_plugin_dirs` returns
  `<prefix>/lib/python3.14/site-packages/vapoursynth/plugins` and
  `<prefix>/lib/buttereye/vapoursynth`, the first holds only Homebrew's
  `mvtools.dylib` and `libffms2.dylib` and the second does not exist, while the
  `libmvgpu.dylib` and `librife.dylib` phases 3 and 4 built sit in a build
  directory under `~/.cache` that ButterEye never searches. So **a plain Homebrew
  install still measures one engine**, and the fix for that is a release to point
  the tap at, not more code: item 1 of the release bar's shortest honest path.
  What the measurement does not settle is the chooser. `select._from_bench` reads
  only `res.recommended`, and mvgpu is deliberately absent from `ranked`, so a
  result that recommends mvgpu — the normal case at 720p and 1080p on a Mac, and
  at 1440p too on a quiet machine — is discarded whole and the user is told no
  measurement exists (`from_benchmark` False, "MVTools on the CPU: RIFE can't run
  here", reproduced here). On this Mac the pick is still `mvtools`, the right
  engine for a wrong reason, only because RIFE is not installed and so not in
  `ranked`. Fed the same 720p and 1080p rows with RIFE available, `select.select`
  returns **live `rife-ncnn`** — measured 2026-10-13 against
  `tests/core/test_macos_doctor_pure._ready_mac` — in a run whose own RIFE rows
  were 20.07 and 8.33 fps with `realtime` False. That is why
  `docs/macos-release-bar.md` moves this fix ahead of installing the tap: the
  install is what makes the wrong reason into a wrong engine.
- **A plugin-search fix that had landed in the doctor only, and now has not.**
  Phase 4 made the plugin search a path because asking one directory shipped a
  false BLOCKING doctor row, and this page said so under "The tap" above — but
  the search path lived in `checks_pkgs`, where only the doctor could reach it.
  Measured in phase 5 against a scratch prefix shaped like a tap install
  (`libmvgpu.dylib`, `librife.dylib` and a model directory in
  `<prefix>/lib/buttereye/vapoursynth`, `mvtools.dylib` where Homebrew autoloads
  it): the doctor found MVTools in the autoload directory while
  `decide.installed_backends(paths.rpm_plugin_dir, …)` answered
  `[mvgpu, rife-ncnn]` — **MVTools missing from the engine chooser** on a Mac
  where it was installed and interpolating, and MVTools is the engine the chooser
  picks here. `session.can_switch_to_cpu` and the benchmark's candidate discovery
  asked the same single directory. The fix moved the list to where every caller
  can reach it: `paths.plugin_dirs` is the load-ordered search path (it lives in
  `paths` and not the doctor because the doctor imports `paths`, so the other
  direction would invert the layering) and is `(paths.rpm_plugin_dir,)` off
  macOS, so Linux and Windows resolve exactly the directory they resolved before;
  `paths.plugin_in` takes one directory **or** a list, which is what lets callers
  and tests written against the single-directory signature keep passing a bare
  `Path`; and `paths.installed_plugin`, `paths.plugin_dir_for` and
  `checks_pkgs.plugin_dirs` are all built on it. `decide.installed_backends`,
  both `session.py` call sites and `bench.runner.Tools` now carry the search
  path. Re-measured on the same scratch prefix: `[mvgpu, mvtools, rife-ncnn]`,
  and `plugin_dir_for` resolves `mvtools.dylib` to the autoload directory and
  `libmvgpu.dylib` to ButterEye's own, so the doctor and the generated `.vpy` can
  no longer name different copies of the same plugin. Held by
  `tests/core/test_macos_doctor_pure.py::test_the_engine_chooser_asks_every_plugin_directory_a_mac_has`,
  which asserts the pre-fix answer before the post-fix one, so a revert fails it.
- **A code-execution hole in phase 4's VSScript search, found and closed.**
  `vsscript_library()` read `sys.path`, whose first entry under `python -m` is the
  current directory, and what it returns is handed to mpv as `VSSCRIPT_PATH` and
  `dlopen`-ed there — so running ButterEye from a directory holding
  `./vapoursynth/libvsscript.dylib` loaded that file into the player. Measured
  before the fix. "What ButterEye does about it" under phase 4 carries the detail,
  the two checks that close it and the general rule they come from, because
  ButterEye exports `DYLD_FALLBACK_LIBRARY_PATH` to every child it spawns and a
  later simplification of that search would reopen this.

#### Every macOS number in this repository, and the machine it came from

One machine, throughout: macOS 27.0.1 on an Apple M2 (arm64, 8 threads), with
Homebrew's mpv 0.41.0, VapourSynth R81 and `vspipe`, MoltenVK 1.4.2, the Vulkan
loader and tools, `vapoursynth-mvtools` 29, `ffms2` 5.0, ffmpeg, mkvtoolnix and
`ncnn`. Every row below was measured first-hand on it; the phase named in the
third column owns the measurement and has the exact command.

| What | Measured on this M2 | Phase |
|---|---|---|
| `doctor`, a full run | `13 ok, 5 info, 1 degraded, 0 blocking` in well under a second (0.7 s and 0.6 s on two runs), exit code 0, with no macOS finding carrying a `dnf`, `rpm`, `sudo`, `copr` or `journalctl` line; the one degraded row is RIFE not being installed | 2, 4 |
| the in-mpv probe (BE-1005) | passes: VapourSynth R81 (API 4.3) with Python 3.14.8 inside mpv's own process, and MVTools loaded there | 4 |
| live smoothing, HDR10, in mpv | `tests/integration/test_hdr_live_real.py` **3 passed** with `VSSCRIPT_PATH`, `DYLD_LIBRARY_PATH` and `DYLD_FALLBACK_LIBRARY_PATH` removed from the environment — the file was red in phases 0 to 3 | 4 |
| `buttereye play`, one line | "24 → 60.002 fps with mvtools (active, ok)", the target taken from the 60 Hz display | 4 |
| offline render, end to end | a 640×360 clip from 72 frames at 24 fps to 144 at 48 fps with `libx265`, through the real `vspipe`, `ffmpeg` and `mkvmerge` | 3, 4 |
| MVTools through `vspipe` | 1432 fps on that 640×360 2× render; clang's auto-vectorised NEON SAD reaches 227.6 M SAD/s at 16×16, 1.3× the same SSE2 kernel through `sse2neon` | 3 |
| mvgpu, correctness | **bit-exact**: mean and maximum absolute error 0.0 against the square drawn half-way, against 8.43 for a blend of the two source frames | 3 |
| mvgpu, speed | 720p 24 → 120 at 287 fps, 1080p at 137 fps, 4K at 36 fps through `vspipe` — about **14× slower than the Linux dev box's RTX 4090**, with the same curve shape | 3 |
| RIFE-ncnn | correct through MoltenVK and **offline only**: 8.5–9.8 fps at 1080p against the ≈48 fps a live 2× 24p needs, with `gpu_thread` forced to 1 by MoltenVK's single compute queue | 3, 4 |
| TensorRT | permanently unavailable rather than merely absent: no CUDA or TensorRT for macOS and no NVIDIA GPU in any Apple Silicon Mac, so `osapi.HAS_VSTRT` is False | 1 |
| the GUI | 18 views drawn through Qt's **real `cocoa` platform** and `macos` style, not the offscreen renderer (`docs/design/screenshots/macos/`); under that same platform `tests/gui/test_theme_scale.py` is 4 failed, 12 passed where `offscreen` is 16 passed | 5 |
| the in-mpv benchmark | **all five configurations**, at 720p/1080p/1440p/4K, with the built plugins reachable via a shadow `HOMEBREW_PREFIX` (`docs/spikes/m0s.md`): 720p mvgpu 201.27 / MVTools 199.21, 1080p 89.21 / 91.07, 4K 32.20 / 28.58, all against a 55.14 fps bar; RIFE offline-only at every size. **1440p is unsettled** — over eleven runs mvgpu measured 52.77–58.31 in mpv (real time in three) and MVTools 56.33–59.69 (real time in all ten runs that reported it, by 2–8 %), with `bench` recommending mvtools in eight runs and mvgpu in three; no 1440p ordering is stated anywhere. A plain Homebrew install still measures one engine (`docs/macos-release-bar.md`, item 4) | 5 |
| the type check and the gates | `mypy --platform` for `linux`, `win32` and `darwin` all clean on `buttereye`; both gate shapes `tools/macos-tests.sh` runs green | 0–4 |

**Every other performance figure in this repository is the Linux dev box's**, and
is labelled as such where it appears: Fedora 44, a Ryzen 9 5900X (12 cores / 24
threads) and an **RTX 4090**, which is the machine `docs/spikes/` and README's
Status section were measured on. Some of those can never get a macOS counterpart
— every TensorRT number (`docs/spikes/m0j.md`, `m0l.md`, `m0q.md`) belongs to a
path that cannot exist on a Mac — and the rest are a different class of hardware,
so none of them may be read as a macOS expectation. Where this page compares the
two it names both machines, as the mvgpu row above does.

#### What phase 5 could not measure on this machine, and what each would take

An honest "not measured" is a deliverable here, not a gap to be papered over.
None of the following has ever been observed:

- **A cross-UID socket refusal**, which is SCOPE §4.3's actual threat model.
  Every peer test in this port had both ends at the same uid, so what has been
  exercised is the mechanism — `_darwin.peer_cred` reading an `xucred` and
  trusting `cr_uid` only when `cr_version` is 0 — and never the refusal itself.
  It needs a **second user account** on the machine and a connection made across
  it.
- **The three-day sweep of the per-user temporary directory.** `dirhelper`
  (`/System/Library/LaunchDaemons/com.apple.bsd.dirhelper.plist`) runs daily at
  03:35 and deletes what is older than three days
  (`CLEAN_FILES_OLDER_THAN_DAYS=3`), and phase 1 put ButterEye's runtime
  directory there (`_darwin.user_temp_dir`), so a session attached for longer
  than that could lose its socket. It would take a session left attached for over
  three days, or a forced `dirhelper` run against an aged directory.
- **`flock` on a network home** (SMB or AFP), where `fcntl.flock` can refuse
  outright instead of blocking. Both places ButterEye takes a lock already
  anticipate this, in different ways, and **neither guess has been tested**:
  `gui/app.InstanceLock`'s own docstring says such a mount can refuse with an
  `OSError` rather than a `BlockingIOError`, and `main` therefore treats a plain
  `OSError` as "run unlocked"; the profile writer's `osapi.locked_dir` sits
  inside a `try` that turns any `OSError` into "config.toml could not be written
  (…); nothing was changed". So what is unmeasured is not whether the code
  crashes — it is whether the refusal really arrives as an `OSError`, whether
  `flock` on a *directory* descriptor behaves at all over SMB, and what the user
  is actually shown. It needs a Mac logged in to a network home directory; this
  one is on local APFS, which is also where `InstanceLock` argues its own lock
  will always live.
- **What the per-user temporary directory becomes inside a sandboxed app
  bundle.** This is the tightest budget in the port — the session socket measures
  80 bytes of the 103 an `AF_UNIX` path may have, and a sandbox container path is
  longer, so the headroom is bytes rather than tens of bytes. No number is
  written here because a calculation is not a measurement: reading it would take
  a signed, sandboxed bundle built for no other purpose, and ButterEye ships no
  bundle and never will ("Why a tap and not an `.app`" above). The risk it
  carries is bounded: past the limit the failure surfaces as an `OSError` in
  mpv's own log.
- **Intel Macs: nothing at all.** arm64 is the only target measured. One code
  question is already known and written into `storage.py` beside `rpm_roots`:
  Homebrew's prefix on an Intel Mac is `/usr/local`, which sits inside the `/usr`
  root the cleaner refuses, where `/opt/homebrew` does not — and that comment's
  own conclusion is that the protection an Intel Mac appears to have is an
  accident of the prefix and does not carry over. Nothing else about an Intel Mac
  has been looked at. It needs an Intel Mac.
- **A macOS frame against a Linux frame.** RIFE built on Homebrew's `ncnn` is
  known to differ from the pinned static build by 21.3 % of output samples (phase
  3); what has never been compared is a macOS render against a real Linux render,
  because there is no Linux machine here. It needs the dev box and one shared
  clip.
- **A real tap install.** No `brew install` was run in this phase: an install
  writes into `/opt/homebrew`, which is the owner's machine to change and not
  this port's. So all four formulae parse (`ruby -c`), every formula name they
  depend on exists, and **nothing beyond that is verified** — not dependency
  resolution, not the build, not `brew audit`, not the `caveats` text a user
  would actually read. What it would take is the owner's own hands: tag a
  release, replace the four `url`s (they name a `v0.0.3` that does not exist) and
  the four `sha256`s (64 zeros today), then on a machine that has never had
  ButterEye run `brew tap <owner>/buttereye` and
  `brew install --verbose <owner>/buttereye/buttereye`, with
  `brew audit --strict --online` on each of the four formulae.

#### CI is blind to all of it, and the job now says so

`.github/workflows/macos.yml` installs no Homebrew package, so GitHub's Apple
Silicon runner has no mpv, no `vspipe`, no VapourSynth, no plugins, no Vulkan
loader, no MoltenVK and no `vulkaninfo`, and it has never installed the tap. The
runner's Metal GPU is real — the one way these runners beat the GPU-less Windows
ones — but macOS has no system Vulkan, so with no bottles installed there is no
Vulkan instance to create and neither mvgpu nor RIFE can run a frame; on macOS
the `gpu` gate asks `vulkaninfo --summary` for a non-CPU device
(`tests/conftest.py`, `_have_gpu_darwin`), so without that bottle every `gpu`
test skips for want of it. What a green tick there means is the three-platform
type check and the pure-Python and Qt layers, and nothing else.

The job's header now says that, and says plainly that **everything the macOS port
proves was measured on a developer's Mac rather than in CI**. No `brew install`
step was added to it: that would make it a different and much slower job, and
paying for it is the owner's call, not this file's. The shape that would fit, as
a proposal and not a decision, is a **second, opt-in workflow**
(`workflow_dispatch` and perhaps a schedule, never on every push) that installs
`mpv`, `vapoursynth`, `vapoursynth-mvtools`, `ffms2`, `ffmpeg`, `mkvtoolnix`,
`molten-vk`, `vulkan-loader` and `vulkan-tools`, then runs `tests/integration`
and `tests/macos` with `BUTTEREYE_TEST_BREW=1`. That would cover live smoothing
in mpv, offline render and the doctor against a real Homebrew, on hardware nobody
has to own. It would still not cover the tap, which wants a published release
first.

#### The support decision is not phase 5's to make

macOS is a **v1 non-goal** in SCOPE §2 (its "Non-goals" list reads "Windows and
macOS"), SCOPE §13 puts no macOS question to the owner at all, and the one entry
in SCOPE's decision log that touches this page is Appendix B's "Platform
assessment (2026-10-10)", whose own words are "Assessment only: both stay
non-goals, no dates". Nothing in phase 5 edits any of that. What phase 5 produces
instead is the evidence, so that a decision — to release something for macOS, to
keep it as a measured port in the tree, or to stop — starts from numbers with
machines attached rather than from a mood.

## What would not change

- The `.vpy` script contract, the profile and rules model, the command
  allowlist, the Lua OSD helper and the offline `vspipe -> ffmpeg -> mkvmerge`
  pipeline are platform-neutral.
- The GUI never imports a subsystem module, so a port lives under
  `buttereye/core/` and the plugins, not in the GUI.

## Linux distributions other than Fedora

Not supported in v1 (SCOPE §2). The core would mostly work on any systemd-based
distribution with mpv ≥ 0.41 and VapourSynth ≥ R72; what differs is the package
checks (`rpm`/`dnf`) and the absence of built plugins. That is packaging work,
not porting work.
