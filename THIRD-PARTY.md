<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Third-party components

ButterEye itself is free software under the GNU Affero General Public License v3.0 or
later (`COPYING`), with an additional permission for the files it generates
(`LICENSES/AdditionRef-ButterEye-generated-output.txt`). Copyright (C) 2026 The
ButterEye contributors.

This file lists the third-party components ButterEye **conveys** (ships in its source
or in its RPM packages) and the ones it **requires or uses** from your system. It
matches the Licences tab in the app (Details ▸ About ▸ Licences, or `buttereye
licence`), which also shows the versions found on your computer. Licence expressions
are SPDX. For the reasoning behind each row, see `docs/SCOPE.md` §8.2.

## Shipped by ButterEye

Each package carries the full licence texts of what it contains; on an installed system
they are in `/usr/share/licenses/<package>/`. The complete corresponding source of every
package is its source RPM (and, for ButterEye and mvgpu, this repository).

| Component | Version | Licence | How ButterEye uses it | Package (licence texts) |
|---|---|---|---|---|
| FSRCNNX x2 8-0-4-1 mpv shader (igv/FSRCNN-TensorFlow) | release 1.1, unmodified | LGPL-3.0-or-later | data file mpv loads for Picture ▸ Sharper | `buttereye`, bundled in `buttereye/data/shaders/` (`LGPL-3.0.txt`, `GPL-3.0.txt`) |
| VapourSynth-RIFE-ncnn-Vulkan plugin (styler00dollar, HolyWu; from nihui/rife-ncnn-vulkan) | r9_mod_v33 | MIT AND WTFPL AND LGPL-2.1-or-later AND BSD-3-Clause | loaded by mpv and vspipe | `buttereye-vs-rife-ncnn` (`LICENSE`, `LICENSE.rife-ncnn-vulkan`, `LGPL-2.1.txt`, `WTFPL.txt`) |
| ncnn (Tencent) | commit 305837fd (2025-05-03) | BSD-3-Clause AND BSD-2-Clause AND Zlib | statically linked into the RIFE plugin | `buttereye-vs-rife-ncnn` (`LICENSE.ncnn.txt`) |
| glslang (nihui fork) | commit a9ac7d5f | BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2 | statically linked into the RIFE plugin | `buttereye-vs-rife-ncnn` (`LICENSE.glslang.txt`) |
| RIFE ncnn models (hzwer/Practical-RIFE, hzwer/ECCV2022-RIFE, nihui/rife-ncnn-vulkan) | curated subset of r9_mod_v33 | MIT | model weights (data) | `buttereye-rife-ncnn-models` (`LICENSE`, `LICENSE.Practical-RIFE`, `LICENSE.ECCV2022-RIFE`, `LICENSE.rife-ncnn-vulkan`; provenance note) |
| MVTools (dubhatervapoursynth/vapoursynth-mvtools) | v29.2 | GPL-2.0-or-later AND ISC | loaded by mpv and vspipe | `buttereye-vs-mvtools` (`LICENSE`, `x86inc.asm` header) |
| mvgpu (ButterEye's own Vulkan motion engine, `mvgpu/`) | 0.0.1 | AGPL-3.0-or-later | loaded by mpv and vspipe | `buttereye-vs-mvgpu` (`COPYING`) |

Optional packages for the experimental TensorRT path, when published in the same
repository: `buttereye-vsmlrt-py` (vsmlrt.py from vs-mlrt, unmodified, GPL-3.0-only) and
`buttereye-onnxconverter-common` (MIT). They are not part of the 0.0.1 release assets.

## Shipped by the Windows installer (not released yet)

The Windows installer (`docs/PLATFORMS.md`, phase 4) conveys, besides ButterEye, the
plugins and the models above (built for Windows), the parts Fedora would otherwise
provide. Their licence texts are installed in `licenses\<component>\`, and
`licenses\SOURCES.txt` says where each part's source is.

| Component | Version | Licence | How ButterEye uses it |
|---|---|---|---|
| Python (embeddable build, unmodified) | 3.14.8 | PSF-2.0 (and the licences in its `LICENSE.txt`) | runs ButterEye |
| PySide6 / Shiboken6 / Qt 6 (Core, Gui, Widgets, Svg; unmodified) | 6.11.2 | LGPL-3.0-only (conveyed under it) | the window, in-process |
| VapourSynth (official wheel, unmodified) | R81 | LGPL-2.1-or-later | `vspipe` and mpv's filter |
| FFMS2 (`ffms2.dll`) | 5.0 | MIT | loaded by vspipe (smooth copies) |
| FFmpeg, decoders only, in `ffms2.dll` | 8.1.3 | LGPL-2.1-or-later | statically linked into ffms2 |
| dav1d, zlib, in `ffms2.dll` | 1.5.4, 1.3.2 | BSD-2-Clause, Zlib | statically linked into ffms2 |
| Vulkan-Tools `vulkaninfo.exe`, with volk | 1.4.328.1 | Apache-2.0 AND MIT | separate program (the GPU list) |
| fftw (in `mvtools.dll`) | 3.3.8 | GPL-2.0-or-later | statically linked into MVTools |
| MinGW-w64 runtime (GCC runtime libraries, winpthreads, CRT start-up code) | GCC 16.2.1, mingw-w64 13.0.0 | GPL-3.0-or-later WITH GCC-exception-3.1 AND BSD-3-Clause AND MIT AND ZPL-2.1 | statically linked into the plugins and tools |

Microsoft's Visual C++ runtime is not shipped: the installer downloads it from
Microsoft when you agree.

## Used from your system, not shipped

These come from Fedora (or, for NVIDIA software, from NVIDIA) under their own licences.
ButterEye runs them as separate programs or has them loaded by mpv or vspipe; only
PySide6/Qt runs inside ButterEye's own process.

| Component | Licence (as Fedora packages it; the app shows the installed package's) | How ButterEye uses it |
|---|---|---|
| PySide6 / Qt 6 (Essentials modules only) | LGPL-3.0-only OR GPL-3.0-only WITH Qt-GPL-exception-1.0 | the window, in-process |
| mpv | GPL-2.0-or-later AND LGPL-2.1-or-later AND BSD-2-Clause AND BSD-3-Clause AND ISC AND MIT | separate program (IPC) |
| VapourSynth (and zimg inside it) | LGPL-2.1-or-later upstream, LGPL-2.1-only in Fedora's metadata (zimg: WTFPL) | separate program (`vspipe`) and mpv's filter |
| FFmpeg (`ffmpeg-free`, or RPM Fusion's) | GPL-3.0-or-later | separate program (smooth copies) |
| ffms2 | MIT AND LGPL-2.1-or-later (GPL when linked to a GPL FFmpeg) | loaded by vspipe (smooth copies) |
| mkvtoolnix | GPL-2.0-or-later AND LGPL-2.1-or-later | separate program (smooth copies), optional |
| fftw (single precision) | GPL-2.0-or-later AND MIT AND BSD-2-Clause | shared library used by MVTools |
| Vulkan loader | Apache-2.0 | used by the GPU engines |
| IBM Plex Sans and Mono; Red Hat Display fonts | OFL-1.1; OFL-1.1-RFN AND CC-BY-SA-4.0 | the interface's typefaces |
| TensorRT, CUDA, `trtexec`, and a `vstrt` plugin you build yourself | proprietary NVIDIA licences (`vstrt`: GPL-3.0) | experimental TensorRT path, loaded only by mpv or vspipe; never shipped, downloaded or loaded in ButterEye's process |

ButterEye never loads, imports or redistributes closed-source interpolation software.
