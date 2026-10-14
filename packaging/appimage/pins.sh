# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# shellcheck shell=bash disable=SC2034  # sourced: build-appimage.sh uses these
#
# Sourced by build-appimage.sh: every file the AppImage trial downloads from
# outside Ubuntu's archive, pinned by SHA-256 (or, for the one git checkout, by
# commit), and the one function that fetches them. The peer of
# packaging/macos/pins.sh and packaging/windows/pins.sh, with the same rule: each
# file lands in its own folder under $XDG_CACHE_HOME/buttereye/appimage-dl and is
# used only once its checksum matches; a mismatch stops the build.
#
# What comes from Ubuntu 22.04's archive instead (the compilers, the -dev
# packages, and the handful of runtime libraries the AppImage carries: libass,
# FriBidi, Lua 5.2, x264, FFTW and four small X11 extension libraries) is pinned
# differently, and more loosely - this is a trial: build-appimage.sh records the
# exact Debian version of every package it copies a file out of, in the install
# record and in the sources tar, and the sources tar carries those packages' own
# source (apt-get source <package>=<version>, which apt checks against the
# signed Sources index). The archive moves with security updates, so two builds
# a month apart can carry different patch releases of those libraries. A
# snapshot.ubuntu.com pin would close that; it is not done here.
#
# Where each checksum was checked against its publisher (2026-10-14):
#   Python        python-build-standalone release 20261009: the asset digest
#                 GitHub's API reports (the same release packaging/macos pins)
#   wheels        PyPI's JSON API digests (the same PySide6/Shiboken6 version as
#                 the macOS and Windows builds, the manylinux_2_34 x86_64 wheels;
#                 VapourSynth R81's manylinux_2_28 x86_64 wheel)
#   VapourSynth   the sdist: PyPI's JSON API digest (as packaging/macos/pins.sh)
#   meson         PyPI's JSON API digest
#   FFmpeg, dav1d, FFMS2
#                 the same three tarballs and hashes packaging/windows/pins.sh
#                 pins (FFmpeg's .asc and dav1d's .sha256 were checked there)
#   mpv, MVTools  GitHub's tag archives: no published digest exists, so these
#                 are the SHA-256 of the archive as downloaded on 2026-10-14
#                 (MVTools' is the Source0 of packaging/plugins/buttereye-vs-mvtools.spec)
#   glslang, wayland, wayland-protocols, nv-codec-headers
#                 no published digest either: as downloaded on 2026-10-14
#   libplacebo    a git commit, not an archive: its 3rdparty/ submodules (glad,
#                 jinja, markupsafe, fast_float) are part of the build and no
#                 archive carries them; the commit pins them too
#   runtime       AppImage/type2-runtime release 20251108: the asset digest
#                 GitHub's API reports

# ---------------------------------------------------------------- the runtime
# python-build-standalone's relocatable CPython, x86_64 glibc flavour: statically
# linked against everything but glibc, with libpython3.14.so.1.0 beside it - the
# library VSScript dlopens inside mpv and vspipe.
PYTHON_VER=3.14.8
PYTHON_PBS=20261009
PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_PBS/cpython-$PYTHON_VER%2B$PYTHON_PBS-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz
PYTHON_SHA256=ddc902fd26460f728758c67d80417c1031edf97589f1f162c4ba3aa158a35998
# the "full" archive of the same build, for python/licenses/ and PYTHON.json only
PYTHON_FULL_URL=https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_PBS/cpython-$PYTHON_VER%2B$PYTHON_PBS-x86_64-unknown-linux-gnu-pgo%2Blto-full.tar.zst
PYTHON_FULL_SHA256=d4d83a2dbb9b7ee12373219113e9aa59ad943b8d381a43837209945bb5596e66

# PySide6-Essentials only (SCOPE F17). manylinux_2_34: these two wheels are what
# sets the AppImage's glibc floor at 2.34, not the build base (README in this
# folder: "Why Ubuntu 22.04").
PYSIDE_VER=6.11.2
PYSIDE_URL=https://files.pythonhosted.org/packages/0e/1a/84a84bbc75835ee88abdcb66eb875b8e1a21834af9b833bc1893f2c1bfaf/pyside6_essentials-6.11.2-cp310-abi3-manylinux_2_34_x86_64.whl
PYSIDE_SHA256=aaf9f25f0f324874085fa5b26a610318db8a8e243cf85bb3e5400595191c7778
SHIBOKEN_URL=https://files.pythonhosted.org/packages/a5/9a/f41bea4316d0c1bec4800842dcb4bd8fd3e29d6a79c496bc32e1aec42583/shiboken6-6.11.2-cp310-abi3-manylinux_2_34_x86_64.whl
SHIBOKEN_SHA256=7a7a0a72a9ed26c9bf77d42246b1c736486befb8f31aa2fb29957ea4cdd1c1c2

# VapourSynth R81's official manylinux wheel: libvapoursynth.so.4, libvsscript.so,
# vspipe, the Python module and the C headers in one folder. It exists, so
# VapourSynth is not built here.
VAPOURSYNTH_VER=81
VAPOURSYNTH_URL=https://files.pythonhosted.org/packages/97/47/0d0cf0d1028e7cb7a15ea888161e2c7ceaaccaf8c24c3d276f0f7fa8e946/vapoursynth-81-cp312-abi3-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
VAPOURSYNTH_SHA256=2e3429524728889328897945564235440910a032ebbc879620ba3ff900758b52

# build-time only: setuptools turns this source tree into a wheel, meson builds
# dav1d, libplacebo, mpv and MVTools (mpv 0.41 wants meson >= 1.3; Ubuntu 22.04
# has 0.61). Neither is in the AppImage.
SETUPTOOLS_URL=https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/setuptools-84.0.0-py3-none-any.whl
SETUPTOOLS_SHA256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670
MESON_URL=https://files.pythonhosted.org/packages/57/99/36cd25f9598eef70db97577440a70a360af505f6f01c2338a18d1556079c/meson-1.12.1-py3-none-any.whl
MESON_SHA256=930bc7542cbd9f57009e182fd014eba48cf1a0180a7b9006d2d32d8f168d8b02

# ---------------------------------------------------------------- built from source
FFMPEG_VER=8.1.3
FFMPEG_URL=https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VER.tar.xz
FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3

DAV1D_VER=1.5.4
DAV1D_URL=https://downloads.videolan.org/pub/videolan/dav1d/$DAV1D_VER/dav1d-$DAV1D_VER.tar.xz
DAV1D_SHA256=686616b7c69eb88d44459391ab25cac13b6647a3b288835c5784e71c1514a5c5

FFMS2_VER=5.0
FFMS2_URL=https://github.com/FFMS/ffms2/archive/$FFMS2_VER/ffms2-$FFMS2_VER.tar.gz
FFMS2_SHA256=7770af0bbc0063f9580a6a5c8e7c51f1788f171d7da0b352e48a1e60943a8c3c

MPV_VER=0.41.0
MPV_URL=https://github.com/mpv-player/mpv/archive/v$MPV_VER/mpv-$MPV_VER.tar.gz
MPV_SHA256=ee21092a5ee427353392360929dc64645c54479aefdb5babc5cfbb5fad626209

# the tag packaging/plugins/buttereye-vs-mvtools.spec builds, and its Source0
MVTOOLS_TAG=v29_2
MVTOOLS_URL=https://github.com/dubhatervapoursynth/vapoursynth-mvtools/archive/$MVTOOLS_TAG/vapoursynth-mvtools-$MVTOOLS_TAG.tar.gz
MVTOOLS_SHA256=65578c649776ac2da6cfa32b7306841076ae184cac607b914649dfc1237498ce

# libplacebo v7.360.1 (the tag object 719cc952 resolves to this commit). Not
# 7.351.0 any more: its Vulkan code generator (src/vulkan/utils_gen.py) fails
# under Python 3.14, which runs meson here; 12509c0f "vulkan/utils_gen: fix for
# python 3.14" is in 7.360. It is also the libplacebo Fedora 44's mpv uses.
LIBPLACEBO_VER=7.360.1
LIBPLACEBO_GIT=https://github.com/haasn/libplacebo.git
LIBPLACEBO_COMMIT=cee9b076f2c63104ccfd497fa79c39a867293ec4

# glslang: libplacebo's GLSL -> SPIR-V compiler, which its Vulkan renderer needs
# (static, into mpv). GitHub's tag archive: no published digest, so the SHA-256
# of the archive as downloaded on 2026-10-14. 16.x is newer than Ubuntu 22.04's
# 11.8, and its CMake floor (3.22.1) is exactly that Ubuntu's.
GLSLANG_VER=16.6.0
GLSLANG_URL=https://github.com/KhronosGroup/glslang/archive/$GLSLANG_VER/glslang-$GLSLANG_VER.tar.gz
GLSLANG_SHA256=9c09b901149c729df745057dafa815278aaa101b84d2b6e14f16a42de52f97f2

# Wayland, build-time only: mpv 0.41 wants libwayland-client >= 1.21 and
# wayland-protocols >= 1.31, Ubuntu 22.04 has 1.20 and 1.25. mpv is compiled
# against these and their wayland-scanner output, but runs with the HOST's
# libwayland (host-libs.txt), so build-appimage.sh checks that every libwayland
# function mpv calls exists in 1.20 as well. freedesktop.org's release pages
# publish no digest: the SHA-256 of each archive as downloaded on 2026-10-14.
WAYLAND_VER=1.26.0
WAYLAND_URL=https://gitlab.freedesktop.org/wayland/wayland/-/releases/$WAYLAND_VER/downloads/wayland-$WAYLAND_VER.tar.xz
WAYLAND_SHA256=64176eaa46e4969903e286f8e5ef8331affc17fdf03ac9b58381d2b23162b7a3
WAYLAND_PROTOCOLS_VER=1.49
WAYLAND_PROTOCOLS_URL=https://gitlab.freedesktop.org/wayland/wayland-protocols/-/releases/$WAYLAND_PROTOCOLS_VER/downloads/wayland-protocols-$WAYLAND_PROTOCOLS_VER.tar.xz
WAYLAND_PROTOCOLS_SHA256=ec4c8f74942d6dff7ace8b4ce4764f0ef9ff618a935d974ea77edee2ad240b14

# nv-codec-headers: the header-only NVIDIA interface FFmpeg's NVDEC (and mpv's
# cuda hwdec) are compiled against; FFmpeg dlopens libcuda and libnvcuvid from
# the host's driver, so nothing NVIDIA is linked or carried. n12.1.14.1 is the
# oldest release FFmpeg 8.1 takes without falling back (configure:
# "ffnvcodec >= 12.1.14.0"), which keeps the driver floor at its lowest: Linux
# 530.41.03 (README of the tag). GitHub's tag archive, no published digest: the
# SHA-256 as downloaded on 2026-10-14.
NVCODEC_TAG=n12.1.14.1
NVCODEC_URL=https://github.com/FFmpeg/nv-codec-headers/archive/$NVCODEC_TAG/nv-codec-headers-$NVCODEC_TAG.tar.gz
NVCODEC_SHA256=f40c8a27417eb2688412da32985e382e811196311c4d103d1fcaafad2876cfc1

# ---------------------------------------------------------------- the AppImage itself
# the static type-2 runtime: no libfuse2 on the host, squashfuse built in; it
# mounts the image with the host's fusermount3 when it can and extracts it
# (APPIMAGE_EXTRACT_AND_RUN=1, or --appimage-extract-and-run) when it cannot
RUNTIME_VER=20251108
RUNTIME_URL=https://github.com/AppImage/type2-runtime/releases/download/$RUNTIME_VER/runtime-x86_64
RUNTIME_SHA256=2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d

# ---------------------------------------------------------------- sources only (the sources tar)
# The same Qt and PySide6 source archives packaging/macos/pins.sh and
# packaging/windows/pins.sh pin (download.qt.io's SHA-256): the licence notices
# of the third-party code inside the wheel's Qt come out of qtbase and qtsvg
# (winstage.qt_notices), and all three go into the sources tar.
QT_SRC_URL=https://download.qt.io/official_releases/qt/${PYSIDE_VER%.*}/$PYSIDE_VER/submodules
QTBASE_URL=$QT_SRC_URL/qtbase-everywhere-src-$PYSIDE_VER.tar.xz
QTBASE_SHA256=5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22
QTSVG_URL=$QT_SRC_URL/qtsvg-everywhere-src-$PYSIDE_VER.tar.xz
QTSVG_SHA256=d594337feca84c26fb67fe87b85e6a5c12fda404b611d905f9d138210c311876
PYSIDE_SRC_URL=https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-$PYSIDE_VER-src/pyside-setup-everywhere-src-$PYSIDE_VER.tar.xz
PYSIDE_SRC_SHA256=cba47efbaad1bedd529725cbc14e21f156c7a19366f07b3edfbb076ffd7afdf8
VAPOURSYNTH_SRC_URL=https://files.pythonhosted.org/packages/69/6a/f9441173c91a3355a32b870522726ed2252cd3c7b04b45e499f30b1a2c98/vapoursynth-81.tar.gz
VAPOURSYNTH_SRC_SHA256=3bf6c90ad737e8ffc5605b05a1ff29cd884f8632fbe0eb49a2705942073a96be

# ---------------------------------------------------------------- fetch
appimage_dl=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/appimage-dl

# fetch <url> <sha256>: print the local path of the verified file. Downloads go
# to a new empty folder first and move into place only once the checksum matches.
fetch() {
    local url=$1 want=$2 name dir tmp got
    name=${url##*/}
    name=${name//%2B/+}
    dir=$appimage_dl/${name%.tar.*}
    dir=${dir%.whl}
    if [[ ! -f $dir/$name ]]; then
        mkdir -p "$appimage_dl"
        tmp=$(mktemp -d "$appimage_dl/.fetch-XXXXXX")
        if ! curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$tmp/$name" "$url"; then
            rm -rf "$tmp"
            echo "error: could not download $url" >&2
            return 1
        fi
        got=$(sha256sum "$tmp/$name" | cut -d' ' -f1)
        if [[ $got != "$want" ]]; then
            rm -rf "$tmp"
            echo "error: $name has SHA-256 $got, expected $want; refusing it" >&2
            return 1
        fi
        rm -rf "$dir"
        mv "$tmp" "$dir"
    fi
    got=$(sha256sum "$dir/$name" | cut -d' ' -f1)
    if [[ $got != "$want" ]]; then
        echo "error: $dir/$name has SHA-256 $got, expected $want (delete it to download again)" >&2
        return 1
    fi
    printf '%s\n' "$dir/$name"
}
