<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Changelog

All notable changes to ButterEye are listed here, newest first. Versions follow
[semantic versioning](https://semver.org); while the version starts with `0.`, any
release may change behaviour or settings.

## 0.0.2 — 2026-10-11

ButterEye now runs on **Windows** as well as Fedora 44 and 45, and live playback no
longer stutters on 4K films whose engine was chosen too optimistically.

### Fixed

- **4K films no longer get an engine that can't keep up.** ButterEye estimated an
  engine's speed on a bigger picture from a 1080p speed test by pixel count alone;
  real cost grows faster, so a 3840×1616 HDR10 film got RIFE · TensorRT and dropped
  most of its frames. The estimate now accounts for that.
- **ButterEye corrects itself while you watch.** If an engine Automatic chose keeps
  dropping many frames, ButterEye switches to one that keeps up and remembers it for
  that size, so reopening the film doesn't repeat the problem. An engine you chose
  yourself is left alone, as before.

### New: ButterEye for Windows (experimental)

- **One installer.** `ButterEye-0.0.2-setup.exe` installs everything ButterEye needs
  except mpv (`winget install shinchiro.mpv`): its own Python, Qt and VapourSynth, the
  smoothing plugins (RIFE, GPU motion vectors, MVTools), the RIFE models, the GPU list
  tool and the video reader for smooth copies. It installs for your account only,
  without administrator rights, adds "ButterEye" to the Start menu (and the desktop if
  you want), `buttereye.cmd` for the command line and an uninstaller in Settings ▸ Apps.
  Running it again repairs an installation. It needs 64-bit Windows 10 version 1903 or
  newer and Microsoft's Visual C++ runtime, which it offers to download from Microsoft;
  ButterEye's system checks say when it is missing, and the window says so too.
- **Microsoft's terms.** Python's Windows build includes two files of Microsoft's
  runtime; the installer asks you to accept Microsoft's terms for them (everything
  else is free software, with every licence text installed alongside).
- **What is tested.** The installer installs, runs its system checks and smooths
  with MVTools on GitHub's Windows machines, and runs every engine on a real GPU under
  Wine. RIFE and GPU motion vectors on a real Windows PC with a graphics card have not
  been tried yet, the installer is not code-signed (Windows SmartScreen will warn),
  and the experimental TensorRT path and the GPU-fault history are Linux-only for now.
- **Under the hood:** everything that differs between the systems goes through one
  module (`buttereye.core.osapi`: named pipes, job objects, Windows folders and
  locks); the system checks read the installer's record and suggest Windows fixes;
  the plugins are cross-built on Fedora (`packaging/windows/`, with a fix for RIFE's
  model loading on Windows); `tests/windows/` runs under Wine and on GitHub's Windows
  machines. The source code of everything built for Windows is published with it.

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
