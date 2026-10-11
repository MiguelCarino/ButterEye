# Platforms: Linux, Windows, macOS

ButterEye 0.0.1 supports **Fedora 44 and Fedora 45 on x86_64** and nothing else
(SCOPE §2, "Non-goals"). Windows and macOS are explicitly out of v1. This page
records, honestly, how far the code is from running on them, so a later decision
starts from facts rather than guesses. Nothing here is a promise or a schedule.

Assessed against the 0.0.1 tree on 2026-10-10; Windows phase 1 (the core
platform layer) landed on 2026-10-11 and is described below. No Windows or
macOS build has been made yet.

## Summary

| Piece | Linux (Fedora 44/45) | Windows | macOS |
|---|---|---|---|
| Python core (`buttereye.core`) | supported | platform layer, detection and doctor done (phases 1-2) | needs a smaller platform layer (peer credentials, paths, `/proc`, detection) |
| PySide6 GUI | supported | Qt itself runs; its lock goes through the platform layer; untried on Windows | Qt itself runs; inherits the core's gaps (`fcntl` is available) |
| mpv control (JSON IPC) | Unix sockets + socketpair | **named pipes**, done; tested under Wine with a real `mpv.exe` | Unix sockets work; `SO_PEERCRED` does not exist |
| mpv `vapoursynth` filter | distro mpv has it | depends on the mpv build | depends on the mpv build |
| VapourSynth + `vspipe` | Fedora packages | official installers exist | available (e.g. Homebrew); untested here |
| `buttereye-vs-mvtools` | COPR RPM | upstream code is portable C/C++; needs a Windows build | needs a macOS build |
| `buttereye-vs-rife-ncnn` | COPR RPM (bundled ncnn) | upstream plugin targets Windows too; needs our patches re-applied and verified | no native Vulkan: only via MoltenVK, unverified |
| `buttereye-vs-mvgpu` (Vulkan compute) | COPR RPM | plain C++17 + Vulkan + VapourSynth API4: should port with modest work | MoltenVK only; compute-shader limits unverified |
| TensorRT (experimental) | user-built `vstrt` | not yet (vs-mlrt is Windows-first upstream, so it is possible) | not possible (no CUDA/TensorRT on macOS) |
| Packaging / installs | RPM + COPR | an installer or zip would be new work | an app bundle would be new work |
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
| 3 | Windows builds of the four VapourSynth plugins | to do |
| 4 | Installer (and the GitHub Actions build that makes it) | to do |

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
in the script's header). Wine is a stand-in, not proof: the installer phase is
tested on real Windows before any release.

### Remaining phases

1. **Plugins (phase 3).** MVTools and RIFE-ncnn-Vulkan are portable upstream
   projects; our packaging patches would need re-applying and the M0(f)
   GPU-fault spike re-running on Windows drivers. `mvgpu` is plain C++17 against
   Vulkan and the VapourSynth API4 headers with shaders compiled by glslang at
   build time, so a meson build for Windows should be modest work. TensorRT via
   vs-mlrt is better supported upstream on Windows than on Linux.
2. **Distribution (phase 4).** An installer built on GitHub's Windows runners,
   with the AGPL Corresponding Source offer and the plugin licence files carried
   over (SCOPE §8). It writes `buttereye-install.json` (the format is in
   `checks_pkgs.manifest_db`) and ships `vulkaninfo.exe` and `ffms2.dll` next to
   the plugins.
3. **Later, not scheduled:** the TensorRT path on Windows (vs-mlrt's Windows
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
