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
| RAVU-lite-AR r3 mpv shader (bjin/mpv-prescalers) | commit `b3f0a59d68f33b7162051ea5970a5169558f0ea2`, unmodified | LGPL-3.0-or-later | data file mpv loads for Picture ▸ Sharp edges (RAVU) | `buttereye`, bundled in `buttereye/data/shaders/` (`LGPL-3.0.txt`, `GPL-3.0.txt`) |
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
provide. Their licence texts are installed in `licenses\<component>\`,
`buttereye-install.json` lists them (the Licences tab reads it), and
`licenses\SOURCES.txt` says where each part's source is.

| Component | Version | Licence | How ButterEye uses it |
|---|---|---|---|
| Python (embeddable build, unmodified) | 3.14.8 | PSF-2.0 AND 0BSD AND bzip2-1.0.6 AND MIT AND BSD-3-Clause AND Apache-2.0 (the parts its `LICENSE.txt` lists: bzip2, libffi, Zstandard, OpenSSL) | runs ButterEye |
| Microsoft Visual C++ runtime, Python's copy (`python\vcruntime140.dll`, `vcruntime140_1.dll`) | 14.51 | Microsoft Distributable Code, under the "Additional Conditions for this Windows binary build" in Python's `LICENSE.txt`; not free software | part of Python's embeddable build, unmodified |
| PySide6 / Qt 6 (Core, Gui, Widgets, Svg, SvgWidgets; the plugins `qwindows`, `qoffscreen`, `qminimal`, `qmodernwindowsstyle`, `qsvgicon` and `qsvg`; all from qtbase and qtsvg, unmodified) | 6.11.2 | LGPL-3.0-only (conveyed under it) | the window, in-process |
| Third-party code inside those Qt binaries (FreeType, HarfBuzz, PCRE2, zlib, libpng, md4c, double-conversion, TinyCBOR, BLAKE2, Unicode data, Vulkan and D3D12 memory allocators, XSVG and others; the list is `licenses\pyside6\third-party\README.txt`) | as in Qt 6.11.2 | each its own (MIT, BSD, Zlib, FTL, Libpng, Unicode-3.0, CC0-1.0, Apache-2.0, ...), notices from Qt's `qt_attribution.json` | inside Qt |
| Shiboken6 | 6.11.2 | LGPL-3.0-only (conveyed under it) | PySide6's binding runtime, in-process |
| VapourSynth (official wheel, unmodified) | R81 | LGPL-2.1-or-later | `vspipe` and mpv's filter |
| FFMS2 (`ffms2.dll`) | 5.0 | MIT | loaded by vspipe (smooth copies) |
| FFmpeg, decoders only, in `ffms2.dll` | 8.1.3 | LGPL-2.1-or-later | statically linked into ffms2 |
| dav1d, zlib, in `ffms2.dll` | 1.5.4, 1.3.2 | BSD-2-Clause, Zlib | statically linked into ffms2 |
| Vulkan-Tools `vulkaninfo.exe`, with volk | 1.4.328.1 | Apache-2.0 AND MIT | separate program (the GPU list) |
| fftw (in `mvtools.dll`) | 3.3.8 | GPL-2.0-or-later | statically linked into MVTools |
| MinGW-w64 runtime (GCC runtime libraries, winpthreads, CRT start-up code) | GCC 16.2.1, mingw-w64 13.0.0 | GPL-3.0-or-later WITH GCC-exception-3.1 AND BSD-3-Clause AND MIT AND ZPL-2.1 | statically linked into the plugins and tools |

The rest of Microsoft's Visual C++ runtime (`msvcp140.dll` and the others Qt and
VapourSynth need) is not shipped: the PySide6 and Shiboken6 wheels' copies are left
out, and the system's is used, installed from Microsoft's own redistributable (the
installer offers to download it from Microsoft when you agree; `buttereye doctor`
checks it). Qt's image-format plugins other than SVG (JPEG, TIFF, WebP, GIF, ...) are
not shipped either: ButterEye draws only SVG icons.

## Shipped by the macOS app (experimental, not released yet)

`ButterEye.app` (`docs/PLATFORMS.md`, "The .app"; built by
`packaging/macos/build-app.sh`) conveys, besides ButterEye, the plugins and the
models above (built for arm64 macOS), everything it runs. Its licence texts are in
`ButterEye.app/Contents/Resources/licenses/<component>/`, `buttereye-install.json` next
to them lists every component with its version and licence (the Licences tab reads
it), and `licenses/SOURCES.txt` is the source offer. The
`ButterEye-<version>-macos-sources.tar` published beside the disk image carries the
Corresponding Source of every GPL, LGPL and MPL component, and the build recipe of
every Homebrew bottle.

| Component | Version | Licence | How ButterEye uses it |
|---|---|---|---|
| Python (python-build-standalone, unmodified) | 3.14.8 (build 20261009) | PSF-2.0 AND 0BSD AND Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND MIT AND OpenSSL AND Zlib AND bzip2-1.0.6 AND blessing (CPython and what it links statically: OpenSSL 3, SQLite, libffi, xz, bzip2, zlib, expat, mpdecimal, libedit, ncurses) | runs ButterEye; VSScript starts it inside mpv and vspipe |
| PySide6 / Qt 6 (Core, Gui, Widgets, Svg, SvgWidgets, D-Bus; the plugins `qcocoa`, `qoffscreen`, `qminimal`, `qmacstyle`, `qsvgicon`, `qsvg`; the macOS wheel thinned to arm64, otherwise unmodified) | 6.11.2 | LGPL-3.0-only (conveyed under it), with the third-party notices in `licenses/pyside6/third-party/` | the window, in-process |
| Shiboken6 | 6.11.2 | LGPL-3.0-only | PySide6's binding runtime, in-process |
| VapourSynth (official macOS arm64 wheel, unmodified) | R81 | LGPL-2.1-or-later | `vspipe` and mpv's filter |
| mpv (Homebrew bottle) | 0.41.0 | GPL-2.0-or-later AND LGPL-2.1-or-later | separate program, driven over JSON IPC |
| FFmpeg (Homebrew bottle: `ffmpeg`, `ffprobe` and its libraries, which mpv also links) | 9.0.2 | GPL-3.0-or-later | separate program; libraries of mpv and ffms2 |
| ffms2 (Homebrew bottle) | 5.0 | GPL-2.0-or-later (links GPL FFmpeg) | loaded by vspipe (smooth copies) |
| MVTools (Homebrew `vapoursynth-mvtools`) and fftw | 29; 3.3.11 | GPL-2.0-or-later; GPL-2.0-or-later AND BSD-2-Clause | loaded by mpv and vspipe |
| ncnn, libomp, glslang, SPIRV-Tools (Homebrew bottles) | as recorded | BSD-3-Clause; MIT; BSD-3-Clause AND GPL-3.0-or-later AND MIT AND Apache-2.0; Apache-2.0 | shared libraries the RIFE plugin links |
| MoltenVK, Vulkan loader, Vulkan-Tools `vulkaninfo` (Homebrew bottles) | 1.4.2; 1.4.363.0; 1.4.363.0 | Apache-2.0 | the Vulkan driver, the loader, the GPU list |
| The other libraries mpv and FFmpeg link (libplacebo, libass, HarfBuzz, FreeType, fontconfig, FriBidi, GLib, gettext's libintl, LuaJIT, x264, x265, SVT-AV1, dav1d, libvpx, Opus, LAME, mpg123, rubberband, libbluray, OpenSSL and more: 50 Homebrew bottles in all, each listed in `buttereye-install.json`) | as recorded | each its own (the bottle's SPDX expression, in the record) | shared libraries |

Every Homebrew binary is the bottle's own, changed in exactly two ways by
`build-app.sh`: its load commands point inside the app (`install_name_tool`), and it
is signed again ad hoc because that change invalidates the old signature.

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
