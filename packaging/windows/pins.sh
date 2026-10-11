# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# shellcheck shell=bash disable=SC2034  # sourced: the scripts that source it use these
#
# Sourced by build-plugins.sh and build-installer.sh: every third-party file the
# Windows build downloads, pinned by SHA-256, and the one function that fetches
# them. Each file lands in its own folder under $XDG_CACHE_HOME/buttereye/windows-dl
# and is used only once its checksum matches; a mismatch stops the build.
#
# Where each checksum was checked against its publisher (2026-10-11):
#   Python        python.org release page (the .sigstore bundle is published too)
#   wheels        PyPI's JSON API digests
#   Qt sources    download.qt.io's SHA-256 (the .mirrorlist page) and its md5sums.txt
#   FFmpeg        the release's .asc signature, FFmpeg release key FCF986EA15E6E293A5644F10B4322F04D67658D8
#   dav1d         the release's .asc signature (VideoLAN release key 2018) and its .sha256
#   FFMS2, Vulkan-Tools, volk
#                 identical to the tarballs in Fedora's signed source packages
#                 (ffms2-5.0-8.fc44, mingw-vulkan-tools-1.4.328.1-2.fc44)

# ---------------------------------------------------------------- the runtime (build-installer.sh)
PYTHON_VER=3.14.8
PYTHON_URL=https://www.python.org/ftp/python/$PYTHON_VER/python-$PYTHON_VER-embed-amd64.zip
PYTHON_SHA256=a93abe456ab01bd96d7a085b3cdb6566b3063f4241360d114142fbdb07f0a310

PYSIDE_VER=6.11.2
PYSIDE_URL=https://files.pythonhosted.org/packages/dc/09/d2662684aee4d3f027ff9b1e965f22301bc28ffc5b7f167658508e4475dc/pyside6_essentials-6.11.2-cp310-abi3-win_amd64.whl
PYSIDE_SHA256=c8a29def77032773a30879f7f24415b5395ad08592d147c170824ef4c735dfc1
SHIBOKEN_URL=https://files.pythonhosted.org/packages/ce/6b/fee1b4814317e4664e318644c9c007676c65a71e5b2b9d3ac4121a94dc0a/shiboken6-6.11.2-cp310-abi3-win_amd64.whl
SHIBOKEN_SHA256=6ab0eba1c904455df621f9a6df3ca2bb896bab8670572d2bc4e37804ae91f19a

# Qt's own sources for the PySide6 wheel's Qt (same version): build-installer.sh
# takes the licence texts of the third-party code inside the shipped Qt binaries
# from them (qt_attribution.json), and the sources tar carries them
QT_SRC_URL=https://download.qt.io/official_releases/qt/${PYSIDE_VER%.*}/$PYSIDE_VER/submodules
QTBASE_URL=$QT_SRC_URL/qtbase-everywhere-src-$PYSIDE_VER.tar.xz
QTBASE_SHA256=5b2e00eccaf5a4d8c14134ffa0ea8dfd0a35ae1ffc7f8d87fa4305a1ed23cf22
QTSVG_URL=$QT_SRC_URL/qtsvg-everywhere-src-$PYSIDE_VER.tar.xz
QTSVG_SHA256=d594337feca84c26fb67fe87b85e6a5c12fda404b611d905f9d138210c311876

VAPOURSYNTH_VER=81
VAPOURSYNTH_URL=https://files.pythonhosted.org/packages/6c/c1/d1448dd3a34ac2d3f439496167b3ffe14d6c0b7f9647d468c2292a06e4c2/vapoursynth-81-cp312-abi3-win_amd64.whl
VAPOURSYNTH_SHA256=8de011b9d372ca1593ccbe6ea164c5d093ef44905a69597fa3d8099d38ce381c

# ---------------------------------------------------------------- built from source (build-plugins.sh)
FFMS2_VER=5.0
FFMS2_URL=https://github.com/FFMS/ffms2/archive/$FFMS2_VER/ffms2-$FFMS2_VER.tar.gz
FFMS2_SHA256=7770af0bbc0063f9580a6a5c8e7c51f1788f171d7da0b352e48a1e60943a8c3c

FFMPEG_VER=8.1.3
FFMPEG_URL=https://ffmpeg.org/releases/ffmpeg-$FFMPEG_VER.tar.xz
FFMPEG_SHA256=7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3

DAV1D_VER=1.5.4
DAV1D_URL=https://downloads.videolan.org/pub/videolan/dav1d/$DAV1D_VER/dav1d-$DAV1D_VER.tar.xz
DAV1D_SHA256=686616b7c69eb88d44459391ab25cac13b6647a3b288835c5784e71c1514a5c5

VULKAN_TOOLS_VER=1.4.328.1
VULKAN_TOOLS_URL=https://github.com/KhronosGroup/Vulkan-Tools/archive/vulkan-sdk-$VULKAN_TOOLS_VER/Vulkan-Tools-$VULKAN_TOOLS_VER.tar.gz
VULKAN_TOOLS_SHA256=617e91b396ac771869d62cd131f05dcc0b82d038744dab7f575bcf213852f8bf
VOLK_URL=https://github.com/zeux/volk/archive/vulkan-sdk-$VULKAN_TOOLS_VER/volk-vulkan-sdk-$VULKAN_TOOLS_VER.tar.gz
VOLK_SHA256=8d6a4092d5de62d0c6290394cf81cf7a99e36875934b1b75d595e76d08fcadd1

# ---------------------------------------------------------------- fetch
windows_dl=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/windows-dl

# fetch <url> <sha256>: print the local path of the verified file. Downloads go
# to a new empty folder first and move into place only once the checksum matches.
fetch() {
    local url=$1 want=$2 name dir tmp got
    name=${url##*/}
    dir=$windows_dl/${name%.tar.*}
    dir=${dir%.zip}
    dir=${dir%.whl}
    if [[ ! -f $dir/$name ]]; then
        mkdir -p "$windows_dl"
        tmp=$(mktemp -d "$windows_dl/.fetch-XXXXXX")
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
