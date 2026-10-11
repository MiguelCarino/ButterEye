# Platforms: Linux, Windows, macOS

ButterEye 0.0.1 supports **Fedora 44 and Fedora 45 on x86_64** and nothing else
(SCOPE §2, "Non-goals"). Windows and macOS are explicitly out of v1. This page
records, honestly, how far the code is from running on them, so a later decision
starts from facts rather than guesses. Nothing here is a promise or a schedule.

Assessed against the 0.0.1 tree on 2026-10-10. The Windows port's four phases
landed on 2026-10-11 and are described below: there is now a Windows installer,
tested under Wine on the Linux dev box and, in CI, installed and tested on GitHub's
Windows Server 2025 machines (no GPU there). It has **not yet been used on a real
Windows desktop PC with a GPU**, so it is not released. No macOS build has been made.

## Summary

| Piece | Linux (Fedora 44/45) | Windows | macOS |
|---|---|---|---|
| Python core (`buttereye.core`) | supported | platform layer, detection and doctor done (phases 1-2) | needs a smaller platform layer (peer credentials, paths, `/proc`, detection) |
| PySide6 GUI | supported | bundled by the installer; opens under Wine, Qt loads on Windows Server 2025 (CI); the window is untried on a real desktop | Qt itself runs; inherits the core's gaps (`fcntl` is available) |
| mpv control (JSON IPC) | Unix sockets + socketpair | **named pipes**, done; tested under Wine with a real `mpv.exe` | Unix sockets work; `SO_PEERCRED` does not exist |
| mpv `vapoursynth` filter | distro mpv has it | depends on the mpv build | depends on the mpv build |
| VapourSynth + `vspipe` | Fedora packages | bundled by the installer (the official wheel, inside its own Python) | available (e.g. Homebrew); untested here |
| `buttereye-vs-mvtools` | COPR RPM | `mvtools.dll`, cross-built (phase 3) | needs a macOS build |
| `buttereye-vs-rife-ncnn` | COPR RPM (bundled ncnn) | `librife.dll`, cross-built with a Windows path fix (phase 3) | no native Vulkan: only via MoltenVK, unverified |
| `buttereye-vs-mvgpu` (Vulkan compute) | COPR RPM | `libmvgpu.dll`, cross-built (phase 3) | MoltenVK only; compute-shader limits unverified |
| TensorRT (experimental) | user-built `vstrt` | not yet (vs-mlrt is Windows-first upstream, so it is possible) | not possible (no CUDA/TensorRT on macOS) |
| Packaging / installs | RPM + COPR | per-user installer with its own Python, Qt and VapourSynth (phase 4) | an app bundle would be new work |
| `doctor` checks | `rpm -q`, `dnf` lines, `journalctl -k`, sysfs | done: install record, `winget` lines, Vulkan devices (phase 2) | must be rewritten for the platform |

Short version: **the architecture ports, and on Windows the core's plumbing now does too** (phase 1, below). The core is
a GUI-free asyncio library with the OS-specific parts concentrated in a handful
of modules, and the GUI only talks to `buttereye.core.api`. Those modules were
written for Linux on purpose; the Windows branch now lives in `buttereye.core.osapi`,
and there is still no macOS branch.

## What is Linux-specific today

This section describes the 0.0.1 tree. On Windows, phase 1 (below) replaced
everything under "mpv IPC and process control" and "Paths and locks"; the
rest still applies.

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
refuses any other. When it is
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

macOS is closer to Linux for the core and further away for the GPU work.

- **Core.** Unix sockets, `posix_spawn`, process groups and `fcntl` are all
  there. Needed: peer credentials via `getpeereid`/`LOCAL_PEERCRED` instead of
  `SO_PEERCRED`, process inspection without `/proc`, a private runtime directory
  without `$XDG_RUNTIME_DIR`, and macOS paths (`~/Library/...`).
- **GPU.** macOS has **no native Vulkan**. RIFE-ncnn-Vulkan and `mvgpu` could
  only run through **MoltenVK** (Vulkan on Metal), whose compute-shader feature
  and limits coverage would have to be verified for both. A native Metal port of
  `mvgpu` would be a separate engine. **TensorRT is not available on macOS** at
  all. MVTools (CPU) is the only backend with no GPU question.
- **mpv/VapourSynth.** Both are available on macOS (for example through
  Homebrew), but whether a given mpv build includes the `vapoursynth` filter
  must be checked; ButterEye's F1 checks already refuse an mpv without it.
- **Detection and doctor** need macOS sources (`system_profiler`, IOKit) in
  place of sysfs, rpm and the journal.

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
