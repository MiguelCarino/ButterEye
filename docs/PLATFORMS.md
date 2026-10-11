# Platforms: Linux, Windows, macOS

ButterEye 0.0.1 supports **Fedora 44 and Fedora 45 on x86_64** and nothing else
(SCOPE §2, "Non-goals"). Windows and macOS are explicitly out of v1. This page
records, honestly, how far the code is from running on them, so a later decision
starts from facts rather than guesses. Nothing here is a promise or a schedule.

Assessed against the 0.0.1 tree on 2026-10-10. No Windows or macOS build was
attempted; the findings come from reading the code and the upstream projects'
documentation.

## Summary

| Piece | Linux (Fedora 44/45) | Windows | macOS |
|---|---|---|---|
| Python core (`buttereye.core`) | supported | needs a platform layer (IPC, process control, paths, locks, detection) | needs a smaller platform layer (peer credentials, paths, `/proc`, detection) |
| PySide6 GUI | supported | Qt itself runs; inherits the core's gaps plus one `fcntl` lock | Qt itself runs; inherits the core's gaps (`fcntl` is available) |
| mpv control (JSON IPC) | Unix sockets + socketpair | mpv uses **named pipes**; new transport needed | Unix sockets work; `SO_PEERCRED` does not exist |
| mpv `vapoursynth` filter | distro mpv has it | depends on the mpv build | depends on the mpv build |
| VapourSynth + `vspipe` | Fedora packages | official installers exist | available (e.g. Homebrew); untested here |
| `buttereye-vs-mvtools` | COPR RPM | upstream code is portable C/C++; needs a Windows build | needs a macOS build |
| `buttereye-vs-rife-ncnn` | COPR RPM (bundled ncnn) | upstream plugin targets Windows too; needs our patches re-applied and verified | no native Vulkan: only via MoltenVK, unverified |
| `buttereye-vs-mvgpu` (Vulkan compute) | COPR RPM | plain C++17 + Vulkan + VapourSynth API4: should port with modest work | MoltenVK only; compute-shader limits unverified |
| TensorRT (experimental) | user-built `vstrt` | vs-mlrt is Windows-first upstream | not possible (no CUDA/TensorRT on macOS) |
| Packaging / installs | RPM + COPR | an installer or zip would be new work | an app bundle would be new work |
| `doctor` checks | `rpm -q`, `dnf` lines, `journalctl -k`, sysfs | must be rewritten for the platform | must be rewritten for the platform |

Short version: **the architecture ports, the plumbing does not yet.** The core is
a GUI-free asyncio library with the OS-specific parts concentrated in a handful
of modules, and the GUI only talks to `buttereye.core.api`. But those modules
were written for Linux on purpose and none has a Windows or macOS branch today.

## What is Linux-specific today

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

## Windows: the realistic path

The pieces ButterEye drives all exist on Windows: mpv (with VapourSynth support
in builds that enable it), VapourSynth with `vspipe`, ffmpeg and mkvmerge. The
Python core and the Qt GUI could run there with:

1. **An IPC transport for named pipes.** mpv's `--input-ipc-server` on Windows
   creates a named pipe (`\\.\pipe\<name>`) instead of a Unix socket. The JSON
   protocol is the same, so `MpvIpc` would need a second transport, not a second
   client. The security checks would change from file ownership and
   `SO_PEERCRED` to pipe ACLs and the client process's owner.
2. **A process layer.** Replace `setsid`/`killpg`/`/proc` with
   `CREATE_NEW_PROCESS_GROUP` (or a Job Object, which also kills children
   reliably) and Win32 process queries.
3. **Paths and locks.** Map config/state/cache to `%APPDATA%`/`%LOCALAPPDATA%`,
   use a per-user private directory in place of `$XDG_RUNTIME_DIR`, and swap
   `fcntl.flock` for `msvcrt.locking` or a named mutex.
4. **Detection and doctor.** Use DXGI/`vulkaninfo`/`nvidia-smi` for GPUs and
   replace the rpm/dnf/journal checks with Windows equivalents (or drop the Xid
   scan, which has no direct analogue).
5. **Plugins.** MVTools and RIFE-ncnn-Vulkan are portable upstream projects;
   our packaging patches would need re-applying and the M0(f) GPU-fault spike
   re-running on Windows drivers. `mvgpu` is plain C++17 against Vulkan and the
   VapourSynth API4 headers with shaders compiled by glslang at build time, so a
   meson build for Windows should be modest work. TensorRT via vs-mlrt is in
   fact better supported upstream on Windows than on Linux.
6. **Distribution.** An installer or portable zip, with the AGPL Corresponding
   Source offer and the plugin licence files carried over (SCOPE §8).

Each step is a contained module change because the GUI is already isolated
behind `buttereye.core.api`. The cost is in testing on real Windows hardware,
which the project does not have today.

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
