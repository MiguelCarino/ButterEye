<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Changelog

All notable changes to ButterEye are listed here, newest first. Versions follow
[semantic versioning](https://semver.org); while the version starts with `0.`, any
release may change behaviour or settings.

## Unreleased

### Fixed

- **4K films no longer get an engine that can't keep up.** ButterEye estimated an
  engine's speed on a bigger picture from a 1080p speed test by pixel count alone;
  real cost grows faster, so a 3840×1616 HDR10 film got RIFE · TensorRT and dropped
  most of its frames. The estimate now accounts for that.
- **ButterEye corrects itself while you watch.** If an engine Automatic chose keeps
  dropping many frames, ButterEye switches to one that keeps up and remembers it for
  that size, so reopening the film doesn't repeat the problem. An engine you chose
  yourself is left alone, as before.

### Windows port, phase 4 of 4

- **ButterEye has a Windows installer.** One `setup.exe` installs everything ButterEye
  needs except mpv: its own Python, Qt and VapourSynth, the smoothing plugins, the
  RIFE models, the GPU list tool and the video reader for smooth copies. It installs
  for your account only, without administrator rights, adds "ButterEye" to the Start
  menu (and the desktop if you want), a `buttereye.cmd` for the command line, and an
  uninstaller in Settings ▸ Apps. Running it again repairs an installation. The one
  thing it may ask Windows' permission for is Microsoft's Visual C++ runtime, which
  VapourSynth needs; the system checks tell you when it is missing.
- It has only been tested under Wine so far, so it is not released yet; real-Windows
  testing comes first (`docs/PLATFORMS.md`). It needs 64-bit Windows 10 version 1903
  or newer. Every licence text is installed with it, and the source code of
  everything built for it is published next to it.
- **Fixed for Windows:** the first `buttereye play` on a new Windows account failed
  because a parent folder didn't exist yet; the command line crashed on "→" when its
  output went to a file or another program; the GPU list never arrived (vulkaninfo
  waits for its console forever, so ButterEye now has it write to a file); and the
  checks and speed test suggested Linux or `winget` fixes for pieces the installer
  brings.

### Windows port, phase 1 of 4

- **The core now has a Windows half.** Everything that differs between Linux and
  Windows goes through one module, `buttereye.core.osapi`: mpv is controlled over a
  named pipe (checked to belong to your own account), helper programs end with
  ButterEye while mpv keeps playing, settings live in `%APPDATA%\ButterEye`, and
  file locks and folder checks use the Windows equivalents. Linux behaves exactly as
  before. There is no Windows build yet: system checks, plugin builds and an
  installer are the next three phases (`docs/PLATFORMS.md`).
- `tests/windows/` tests the Windows code against the real Windows API, including a
  real `mpv.exe`; `tools/wine-tests.sh` runs them under Wine on Linux.

### Windows port, phase 3 of 4

- **The video plugins build for Windows.** `packaging/windows/build-plugins.sh`
  cross-compiles MVTools, RIFE and mvgpu on Fedora into self-contained DLLs, with every
  licence text and the source code for the GPL parts. Under Wine they load into
  VapourSynth for Windows and interpolate correctly, on the GPU too.
- **Fixed for Windows:** RIFE built with MinGW cut its model folder to one letter and
  produced black in-between frames without an error
  (`buttereye-vs-rife-ncnn-windows-paths.patch`).

### Windows port, phase 2 of 4

- **The system checks speak Windows.** On Windows the doctor reads ButterEye's install
  record instead of rpm, finds GPUs through Vulkan, and every fix is a Windows fix:
  run the installer again for ButterEye's own pieces, or the `winget` line for mpv,
  VapourSynth, ffmpeg, mkvtoolnix and 7-Zip. Setup lists those `winget` lines the way
  it lists `dnf` lines on Fedora. Two things aren't on Windows yet, and the checks say
  so: the GPU-fault history and the experimental TensorRT path.

## 0.0.1 — 2026-10-10

The first release. ButterEye is in **alpha**: it works on the machines it was tested
on, but expect rough edges. It runs on **Fedora 44 and 45 only** (Linux, x86_64).

### What ButterEye does

- **Smoother playback in mpv.** Drop a video on the window (or run `buttereye play
  FILE`) and it plays in your distribution's own mpv with new in-between frames, so
  motion looks smoother. ButterEye never edits your `mpv.conf`; it starts mpv with its
  own settings and adds a VapourSynth filter while the video plays.
- **Four smoothing engines, picked for your computer:**
  - **RIFE** (a neural network) on the GPU through **Vulkan**, on AMD, Intel or NVIDIA
    cards.
  - **RIFE through NVIDIA TensorRT** (experimental): about four times faster than the
    Vulkan path on the cards it was measured on. You build the TensorRT plugin yourself
    (`contrib/build-vstrt.sh`); once it is set up, ButterEye uses it by default.
    ButterEye never ships or downloads NVIDIA software.
  - **mvgpu**, ButterEye's own GPU motion-vector engine (Vulkan compute): very fast,
    even at 4K and high refresh rates, for videos RIFE can't keep up with live.
  - **MVTools** on the CPU, for computers without a usable GPU.
- **A speed test** measures each engine inside mpv once per GPU, and **Automatic**
  chooses the best engine that keeps up. If a video is too demanding, ButterEye smooths
  it at a smaller size or switches engine instead of stuttering, and it notices a stalled
  or crashed filter and recovers.
- **Frame-rate choices:** double (2×), 60 fps, or match your display's refresh rate.
- **HDR10.** Smooth copies of HDR10 videos (also HDR10+ and Dolby Vision with an HDR10
  base layer) are saved as HDR10 with x265 or SVT-AV1, keeping the mastering data.
  Live smoothing of HDR10 is available as an **experimental** option on the video's row.
- **Smooth copies.** The second drop zone ("Save a smooth copy") renders a smoothed file
  in the background, with progress, time left, cancel, and an optional smaller output
  size. Copies are saved as MKV and keep the audio, subtitles and chapters.
- **Picture options:** Standard, Sharper (the bundled FSRCNNX upscaling shader), Less
  banding (mpv's own debanding), or both. Your own mpv shaders are kept.
- **On-screen info in mpv:** a short line with the frame rates, whether the CPU or the
  GPU smooths, and the engine in use (choose how long it stays and what it shows in Details ▸ Settings).
- **Per-file logs:** every playing video and every smooth copy has **Copy log**, which
  copies what ButterEye and mpv or the encoder logged about that file. Closed or crashed
  players keep their row for a while, with the reason.
- **System checks** (Details ▸ System): mpv, VapourSynth, the plugins, the GPU,
  recent GPU faults and conflicting `mpv.conf` settings, each with a plain explanation
  and the command that fixes it.
- **A `buttereye` command** for everything the window does (`doctor`, `setup`, `play`,
  `render`, `bench`, `profiles`, `licence` and more), with `--json` output.
- **The Carino look:** a dark interface in the Carino Systems visual language (gold
  accents, IBM Plex and Red Hat Display type), keyboard and screen-reader friendly.
- **Fedora 44 and 45 RPMs** for the application and its plugins: `buttereye`,
  `buttereye-vs-rife-ncnn`, `buttereye-rife-ncnn-models`, `buttereye-vs-mvtools` and
  `buttereye-vs-mvgpu`, built from source with networking off.
- **About ▸ Releases and updates** opens this project's releases page; ButterEye itself
  never goes online to check for updates.

### Known limits

- **Linux only, Fedora 44 and 45 only.** macOS and Windows are not supported (see the
  README). Other Linux distributions may work from source but are untested.
- **4K live smoothing** with RIFE needs a very fast GPU (RTX 4080/4090 class with
  TensorRT); on other GPUs ButterEye smooths 4K at a smaller size or uses mvgpu.
- **TensorRT is experimental** and needs NVIDIA's own packages, which NVIDIA does not
  publish for Fedora. The first play at a new size builds an engine (about 30 s at
  1080p); the video plays with the Vulkan path meanwhile.
- **Live HDR** covers HDR10 (PQ) only and is experimental. HLG videos play without
  smoothing; HDR10+ and Dolby Vision dynamic metadata is not carried live. Smooth
  copies refuse HLG, Dolby Vision profile 5 and Dolby Vision without an HDR10 base.
- **Not in this release:** attaching to an mpv you started yourself, acting on
  leftover filters, writing the include line into your `mpv.conf` for you, bug-report
  bundles, model downloads and installing a model from a file, and denoising.
- **No COPR repository yet.** Install the RPMs from the GitHub release (see the
  README); a `dnf copr enable` repository is planned.
- The interface is in English only.
