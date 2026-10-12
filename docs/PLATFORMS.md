# Platforms: Linux, Windows, macOS

ButterEye 0.0.1 supports **Fedora 44 and Fedora 45 on x86_64** and nothing else
(SCOPE §2, "Non-goals"). Windows and macOS are explicitly out of v1. This page
records, honestly, how far the code is from running on them, so a later decision
starts from facts rather than guesses. Nothing here is a promise or a schedule.

Assessed against the 0.0.1 tree on 2026-10-10. The Windows port's four phases
landed on 2026-10-11 and are described below: there is now a Windows installer,
tested under Wine on the Linux dev box and, in CI, installed and tested on GitHub's
Windows Server 2025 machines (no GPU there). It has **not yet been used on a real
Windows desktop PC with a GPU**, so it is not released. No macOS build has been
made: macOS phases 0 and 1 (both 2026-10-12) make the three-platform type check
and ButterEye's own macOS tests run and pass on an Apple Silicon Mac, and give the
platform layer a third value — the core's hot paths (process liveness, the private
runtime folder, mpv's control channel, locks, plugin file names, durable renames)
all ask `buttereye.core.osapi` for the platform's answer instead of calling Linux
directly. They do **not** empty the core of Linux-only calls: the doctor and
detection modules still read `/proc` and `/usr/lib64` themselves, and "What phase
1 does not do" below names each one and the phase that owns it. So this is a
platform layer and a gate against later breakage, not support: the doctor still
answers a Mac with Fedora advice (phase 2, below), and a dozen `tests/core` tests
had to be taught that there are three platforms before the gate was green (phase
1, below).

## Summary

| Piece | Linux (Fedora 44/45) | Windows | macOS |
|---|---|---|---|
| Python core (`buttereye.core`) | supported | platform layer, detection and doctor done (phases 1-2) | the platform layer is done and three-valued (phase 1): peer check, process state, process exit watch, folders, locks, durable saves, plugin names. Detection, the doctor and setup are not (phase 2) |
| PySide6 GUI | supported | bundled by the installer; opens under Wine, Qt loads on Windows Server 2025 (CI); the window is untried on a real desktop | Qt itself runs; inherits the core's gaps (`fcntl` is available) |
| mpv control (JSON IPC) | Unix sockets + socketpair | **named pipes**, done; tested under Wine with a real `mpv.exe` | Unix sockets work; `SO_PEERCRED` does not exist, so the uid comes from `LOCAL_PEERCRED` and the pid from `LOCAL_PEERPID` (phase 0, measured against a Homebrew `mpv`) |
| mpv `vapoursynth` filter | distro mpv has it | depends on the mpv build | **measured on the binary, not inferred from the formula**: Homebrew's `mpv` is installed here as 0.41.0 (exactly SCOPE's minimum) and `mpv --vf=help` lists `vapoursynth`. The mpv.io / stolendata app bundle, which a Mac user is likelier to have already, is 0.40.0 **without** the filter (0 hits in `strings`; `--vf=help` lists only `format`, `lavfi`, `lavfi-bridge`, `sub`), and that cask has been disabled in Homebrew since 2026-09-01 for failing Gatekeeper. Having the filter is not enough: Homebrew's mpv cannot find VSScript at all without `VSSCRIPT_PATH` or `DYLD_LIBRARY_PATH` (measured; phase 1's phase-4 blocker, below) |
| VapourSynth + `vspipe` | Fedora packages | bundled by the installer (the official wheel, inside its own Python) | installed and run here: Homebrew's `vspipe` reports **VapourSynth Core R81**, and autoloads from `/opt/homebrew/lib/python3.14/site-packages/vapoursynth/plugins/`, which holds `mvtools.dylib` and `libffms2.dylib` |
| `buttereye-vs-mvtools` | COPR RPM | `mvtools.dll`, cross-built (phase 3) | **it really interpolates on arm64**: a 24-frame clip through `mv.Super`/`mv.Analyse`/`mv.FlowFPS` came out of `vspipe` as 48 frames at 3484 fps, with `mvtools.dylib` autoloaded. Homebrew ships `vapoursynth-mvtools` 29 as an arm64 bottle. Upstream's `meson.build` already takes the aarch64 branch and bundles `sse2neon.h`, and on an M2 clang -O3 auto-vectorises the plain-C SAD to NEON at 227.6 M SAD/s for 16×16, 1.3× faster than the same SSE2 kernel through sse2neon |
| `buttereye-vs-rife-ncnn` | COPR RPM (bundled ncnn) | `librife.dll`, cross-built with a Windows path fix (phase 3) | plausible, **unmeasured**: ncnn upstream already does the portability dance mvgpu lacks and has a full Apple path, and brew's `ncnn` 20260526 is built against `molten-vk`, `glslang` and `spirv-tools` — but nothing has rendered a frame |
| `buttereye-vs-mvgpu` (Vulkan compute) | COPR RPM | `libmvgpu.dll`, cross-built (phase 3) | works on MoltenVK 1.4.2 (M2) bar three lines: all seven compute shaders compile to SPIR-V, translate to MSL without a warning, create all eight pipelines and dispatch correctly (`search.comp` found the true half-vector in 16/16 blocks at workgroup 512, `unpack.comp` 1024/1024 bit-correct samples), and the limits are not tight (`maxComputeWorkGroupInvocations` 1024 against the 512 needed). The blocker: `vkCreateInstance` (`mvgpu/src/mvgpu.cpp:329-331`, API 1.1, no extensions) returns `VK_ERROR_INCOMPATIBLE_DRIVER`, because MoltenVK's ICD says `"is_portability_driver": true` and the loader hides it until the app enables `VK_KHR_portability_enumeration` and sets `VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR`. A tool that does enable it sees a real device: `vulkaninfo` here reports `deviceName` "Apple M2", `driverName` MoltenVK (`driverID` `DRIVER_ID_MOLTENVK`, `driverInfo` 1.4.2) at `apiVersion` 1.4.357 — not the mock ICD. Its `deviceType` is `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, which is a phase-2 problem on its own (below) |
| ffms2 (render's source filter) | Fedora package | `ffms2.dll`, built from source in the same chroot (phase 4) | a Homebrew bottle (`ffms2` 5.0), so the Windows source-offer problem does not arise; installed here as `libffms2.dylib` in VapourSynth's autoload directory, though nothing has decoded with it yet |
| TensorRT (experimental) | user-built `vstrt` | not yet (vs-mlrt is Windows-first upstream, so it is possible) | **permanently** not possible: there is no CUDA or TensorRT for macOS, and no Apple Silicon Mac has an NVIDIA GPU. The seam says so as of phase 1 (`osapi.HAS_VSTRT` is False), and the generated script refuses the TensorRT branch before it looks for an engine |
| Packaging / installs | RPM + COPR | per-user installer with its own Python, Qt and VapourSynth (phase 4) | an app bundle would be new work |
| `doctor` checks | `rpm -q`, `dnf` lines, `journalctl -k`, sysfs | done: install record, `winget` lines, Vulkan devices (phase 2) | must be rewritten for the platform, and phase 1 does not start that: a full `doctor` run on this Mac today answers with `sudo dnf copr enable`, `libvulkan.so.1` and `usermod -aG systemd-journal`, and reports ffms2 and the Vulkan loader missing while both are installed |

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
  built with a pinned, statically linked ncnn and two local patches (SCOPE §9,
  `packaging/plugins/`). None of that output is usable off Linux.

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
**not a supported platform**, and phases 0 and 1 do not make it one: what they
make is a gate and a platform layer. `mypy --platform darwin buttereye` is
clean, as `linux` and `win32` already were, which is what stops a later phase
quietly breaking either supported platform. `tests/macos` and `tests/boundary`
pass. `tests/core` did not when phase 1's platform layer first landed: twelve of
its tests assert the Linux folder layout, the `XDG_RUNTIME_DIR` refusal or Linux
plugin file names, and met the macOS answers instead. Phase 1, below, lists them
and what each one needed. `tools/macos-tests.sh` is what says whether the gate is
green; this page does not.

`.github/workflows/macos.yml` runs the type check and the test suite on
GitHub's Apple Silicon runners, and installs nothing from Homebrew: no mpv, no
VapourSynth, no plugins, because none of that is ported yet. The measurements
on this page come from a Mac that does have them.

The measurements below come from one machine: macOS 27.0.1 on an Apple M2
(arm64), Homebrew's python3.14. **arm64 is the only target measured**, and Intel
Macs are out of the first cut: Homebrew's prefix there is `/usr/local`, which
also sits inside the protected `/usr` root `buttereye/core/storage.py` refuses
to clean, where `/opt/homebrew` does not.

| Phase | What | State |
|---|---|---|
| 0 | The `IS_MACOS` seam, the three live defects, `tests/macos` and CI | **done** (`buttereye/core/osapi/_darwin.py`) |
| 1 | The platform layer becomes three-valued: libproc in place of `/proc`, the per-user temporary directory in place of `$XDG_RUNTIME_DIR`, `~/Library` folders, `.dylib` plugin names, a kqueue in place of a pidfd | **done** (`buttereye/core/osapi/_darwin.py`, `paths._resolve_macos`) |
| 2 | Doctor, detection and setup speak macOS | not started |
| 3 | macOS builds of the plugins | not started |
| 4 | Distribution through a Homebrew tap | not started |
| 5 | The real-hardware gate | not started |

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
  only run through **MoltenVK** (Vulkan on Metal). For `mvgpu` that path is now
  measured and the one blocker is the three lines of instance creation named in
  the summary table; for RIFE it is plausible and unmeasured. A native Metal
  port of `mvgpu` would be a separate engine. **TensorRT is not available on
  macOS** and permanently cannot be: there is no CUDA or TensorRT for macOS, and
  no Apple Silicon Mac has an NVIDIA GPU. MVTools (CPU) is the only backend with
  no GPU question, and Homebrew already ships it.
- **mpv/VapourSynth.** Both are Homebrew bottles, and the formula's `mpv`
  carries the `vapoursynth` filter while the app bundle from mpv.io does not
  (the summary table has the detail). ButterEye's F1 checks already refuse an
  mpv without it.
- **Detection and doctor** need macOS sources (`system_profiler`, IOKit) in
  place of sysfs, rpm and the journal.

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

**What phase 1 does not do.** It does not make macOS supported, and it does not
make the doctor speak macOS. A full `doctor` run on this Mac today still answers
with `sudo dnf copr enable`, `libvulkan.so.1` and `usermod -aG systemd-journal`,
and still reports ffms2 and the Vulkan loader missing while both are installed —
that is phase 2. Plugin builds are phase 3, distribution phase 4, and the
real-hardware gate phase 5.

It also does not empty the core of Linux-only calls, and an earlier draft of this
page claimed it did. These are the ones that are left, each with the phase that
owns it. They are all in detection and diagnostics, which is the one part of the
core that asks the operating system questions rather than asking it to do things,
and so the one part the seam was never going to cover.

| Linux-only call left in the core | Where | Owner |
|---|---|---|
| `/proc/sys/kernel/random/boot_id`, to tell this boot's Xid faults from an older boot's | `gpufault.current_boot_id` | phase 2. It returns `None` on `OSError`, so on a Mac the correlation degrades silently rather than failing — the scan still runs, it just cannot discard stale events |
| `journalctl -k`, the kernel log the Xid scan reads | `gpufault.scan_xid` | phase 2 (macOS has no equivalent to read; the check already reports itself unreadable) |
| `/usr/lib64/libvulkan.so.1`, `/lib64/libvulkan.so.1` and the Debian multiarch path, plus the ICD folders under `/usr/share` and `/etc` | `checks_vulkan.LOADER_PATHS`, `checks_vulkan.ICD_DIRS` | phase 2 |
| `/usr/lib64/libffms2.so*`, `/usr/lib/libffms2.so*`, `/usr/lib64/vapoursynth/*ffms2*` | `checks_pkgs.FFMS2_GLOBS` | phase 2 |
| `/usr/lib64/vapoursynth`, `/usr/lib/vapoursynth` | `probe.AUTOLOAD_DIRS` | phase 2 |
| `/usr/lib64/libnvinfer.so.11` | `trt.LIBNVINFER` | phase 2. Nothing can ever resolve under it on macOS and `osapi.HAS_VSTRT` already says so; `backends/trt.py` and `doctor/probe.py` are the two files that have to start asking (the drift note above) |
| `rpm -q` and the `dnf` lines | `checks_pkgs`, `setup`, `licences` | phase 2 |
| `/proc/<pid>/status` and `/proc/<pid>/stat`, each re-read inline instead of through `osapi.pid_alive` | `_alive` in `tests/test_bench_runner.py`, `_state` in `tests/test_live_review_fixes.py` | whoever owns `tests/`. Two of the three `/proc`-reading tests the gate's exclusion list names, above. A helper that duplicates the read instead of calling the seam has to be taught each platform on its own, which is the cost of not asking |
| a `/proc` walk for a process tree | `descendants` in `tools/memguard.py` | whoever owns `tools/`. It already skips itself where no capped scope can be made, so it costs the developer workflow, not the application |

**A phase-2 problem phase 1 has already uncovered.** `vulkaninfo` reports the M2
as `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU`, which is the truth — the GPU shares
system memory with the CPU — but `backends/select.py` gates its high-quality
RIFE model on `dev.device_type == "discrete"` (with a heap threshold). Every
Apple GPU answers the same way, so an M1 Air and an M3 Ultra would both be
classified as low-end and always get the lite model. The rule is a Linux
PCIe-era proxy for "has its own fast memory", and unified memory does not fit
it; phase 2 has to pick a different test for Apple Silicon rather than widen
"discrete".

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
somewhere dyld searches, and phase 4 is where that decision belongs. Note what is
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
