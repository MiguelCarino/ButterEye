# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# shellcheck shell=bash disable=SC2034  # sourced: build-app.sh uses these
#
# Sourced by build-app.sh: every file the macOS app build downloads from outside
# Homebrew, pinned by SHA-256, and the one function that fetches them. The peer
# of packaging/windows/pins.sh, with the same rule: each file lands in its own
# folder under $XDG_CACHE_HOME/buttereye/macos-dl and is used only once its
# checksum matches; a mismatch stops the build.
#
# What comes from Homebrew instead (mpv, FFmpeg, ffms2, MVTools, MoltenVK, the
# Vulkan loader, vulkaninfo and the libraries they link) is pinned differently:
# build-app.sh takes the bottles installed on the build machine and records the
# exact keg version of each in the app's install record and its source offer, and
# the Corresponding Source tar carries those formulae's own source archives
# (`brew fetch --build-from-source`, which checks each against the formula's
# sha256). A keg whose version is not the formula's current one is refused,
# because then the source Homebrew would fetch is not the source of the binary.
#
# Where each checksum was checked against its publisher (2026-10-13):
#   Python        python-build-standalone release 20261009: the asset digest
#                 GitHub's API reports and the release's own SHA256SUMS agree
#   wheels        PyPI's JSON API digests (the same PySide6/Shiboken6 version
#                 as the Windows build, the macOS universal2 wheels)
#   VapourSynth   PyPI's JSON API digests (wheel and sdist); the sdist is the
#                 one Homebrew's vapoursynth formula builds from, same sha256
#   Qt sources    download.qt.io's SHA-256 (the .mirrorlist page); the same two
#                 tarballs packaging/windows/pins.sh pins
#   PySide6 src   download.qt.io's SHA-256 (the .mirrorlist page)
#   setuptools    PyPI's JSON API digest

# ---------------------------------------------------------------- the runtime
# python-build-standalone's relocatable CPython, the "install_only_stripped"
# flavour: the same build as install_only without debug symbols. Its python3.14
# is statically linked; libpython3.14.dylib beside it is what VSScript dlopens
# inside mpv and vspipe, and what the app's launcher embeds.
PYTHON_VER=3.14.8
PYTHON_PBS=20261009
PYTHON_URL=https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_PBS/cpython-$PYTHON_VER%2B$PYTHON_PBS-aarch64-apple-darwin-install_only_stripped.tar.gz
PYTHON_SHA256=936217c0e6893f51af939474bee18ecf5e6fe47b986d2c163f19b57c682e4a83
# The "full" archive of the same build, for two files the install_only one
# leaves out: python/licenses/ (the licence texts of everything statically
# linked into it - OpenSSL 3, SQLite, libffi, xz, bzip2, zlib, expat, mpdecimal,
# libedit, ncurses, libuuid) and PYTHON.json, which names them. Nothing else of
# it goes into the app.
PYTHON_FULL_URL=https://github.com/astral-sh/python-build-standalone/releases/download/$PYTHON_PBS/cpython-$PYTHON_VER%2B$PYTHON_PBS-aarch64-apple-darwin-pgo%2Blto-full.tar.zst
PYTHON_FULL_SHA256=6888111560a89c16e471894cca0f50d66f3bc00e5a16e91fab24c50e567ef5ee

# PySide6-Essentials only (SCOPE F17). universal2 is the only macOS wheel Qt
# publishes; build-app.sh thins every Mach-O in it to arm64.
PYSIDE_VER=6.11.2
PYSIDE_URL=https://files.pythonhosted.org/packages/6f/16/0b7ecf89ebada82ed0430be33809ff325761ece83104f8635b1bd101fcd0/pyside6_essentials-6.11.2-cp310-abi3-macosx_13_0_universal2.whl
PYSIDE_SHA256=77795c145202e65a78d88f7cd409d186e3ba23d159bdb3ba2dcd159ae5e5f0d9
SHIBOKEN_URL=https://files.pythonhosted.org/packages/47/44/11bf71c36e71936ab4e923e5dd23599dab527c4488b01860b0f689ce9947/shiboken6-6.11.2-cp310-abi3-macosx_13_0_universal2.whl
SHIBOKEN_SHA256=53659683b1f7a08e9f87eff9b1065f1ceb7110cd7a4bc09fdf5efe43d286604d

# VapourSynth's own arm64 wheel: libvapoursynth, libvsscript, vspipe and the
# Python module in one folder, linked by @loader_path and to nothing outside
# macOS itself (measured: otool -L). Homebrew's R81 is the same release, but it
# lives inside a virtualenv of Homebrew's framework Python and would drag that
# Python in with it.
VAPOURSYNTH_VER=81
VAPOURSYNTH_URL=https://files.pythonhosted.org/packages/63/61/2183a243f51f43d03019203f5040d050b6732a2b8293bb692de3c7f9943e/vapoursynth-81-cp312-abi3-macosx_13_0_arm64.whl
VAPOURSYNTH_SHA256=4cfaea20f819a7772829bf83d34ce2db3b9e6aeed5825ea4b42791a22bc724f2

# setuptools, the build backend pyproject.toml names: only to turn this source
# tree into a wheel at build time (the bundled Python ships without it, as
# python.org's 3.12+ builds do). Not in the app.
SETUPTOOLS_URL=https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/setuptools-84.0.0-py3-none-any.whl
SETUPTOOLS_SHA256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670

# ---------------------------------------------------------------- sources only (the sources tar)
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
macos_dl=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/macos-dl

# fetch <url> <sha256>: print the local path of the verified file. Downloads go
# to a new empty folder first and move into place only once the checksum matches.
fetch() {
    local url=$1 want=$2 name dir tmp got
    name=${url##*/}
    name=${name//%2B/+}
    dir=$macos_dl/${name%.tar.*}
    dir=${dir%.whl}
    if [[ ! -f $dir/$name ]]; then
        mkdir -p "$macos_dl"
        tmp=$(mktemp -d "$macos_dl/.fetch-XXXXXX")
        if ! curl -fsSL --retry 3 --proto '=https' --tlsv1.2 -o "$tmp/$name" "$url"; then
            rm -rf "$tmp"
            echo "error: could not download $url" >&2
            return 1
        fi
        got=$(shasum -a 256 "$tmp/$name" | cut -d' ' -f1)
        if [[ $got != "$want" ]]; then
            rm -rf "$tmp"
            echo "error: $name has SHA-256 $got, expected $want; refusing it" >&2
            return 1
        fi
        rm -rf "$dir"
        mv "$tmp" "$dir"
    fi
    got=$(shasum -a 256 "$dir/$name" | cut -d' ' -f1)
    if [[ $got != "$want" ]]; then
        echo "error: $dir/$name has SHA-256 $got, expected $want (delete it to download again)" >&2
        return 1
    fi
    printf '%s\n' "$dir/$name"
}
