#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Runs inside the mock chroot that packaging/windows/build-plugins.sh prepares,
# after build-in-chroot.sh. Cross-builds the two Windows pieces ButterEye ships
# next to its plugins that Windows doesn't have (docs/PLATFORMS.md, phase 4):
#
#   vapoursynth/ffms2.dll   FFMS2 (the source filter offline render reads video
#                           through), with a decode-only LGPL FFmpeg, dav1d (AV1)
#                           and zlib linked in: no GPL parts, no network code
#   vulkaninfo.exe          Vulkan-Tools' vulkaninfo (the GPU list), with volk
#
# Both are built from pinned, checksum-verified upstream releases (the same
# tarballs Fedora's own packages use, where Fedora has one) and are
# self-contained: they import only Windows system DLLs. vulkaninfo carries one
# patch (vulkaninfo-mingw-user32.patch): upstream only sets up its user32
# function table for MSVC builds, so every MinGW build crashes on Windows.
#
# In:  /builddir/tin   (tarballs and the patch; see build-plugins.sh)
# Out: /builddir/out/vapoursynth/ffms2.dll, /builddir/out/vulkaninfo.exe,
#      /builddir/out/licenses/{ffms2,vulkan-tools}/, /builddir/out/components-tools.json
# Env: JOBS, and FFMS2_VER, FFMPEG_VER, DAV1D_VER, VULKAN_TOOLS_VER (as pinned)
set -euo pipefail

in=/builddir/tin
out=/builddir/out
work=/builddir/twork
deps=$work/deps  # the static dav1d and FFmpeg, never installed into the sysroot
jobs=${JOBS:-8}
host=x86_64-w64-mingw32
sysroot=/usr/$host/sys-root/mingw
objdump=$host-objdump

rm -rf "$work"
mkdir -p "$work" "$deps" "$out/vapoursynth" "$out/licenses"

# Windows system DLLs only. FFmpeg uses bcrypt for random numbers and ADVAPI32/
# ole32/USER32 for its Windows helpers; vulkan-1.dll is loaded at run time (volk).
allowed='^(KERNEL32|msvcrt|bcrypt|ADVAPI32|USER32|GDI32|ole32|SHELL32|SHLWAPI)\.dll$'
check_imports() {
    local file=$1 info bad
    info=$("$objdump" -p "$file")
    bad=$(sed -n 's/^\s*DLL Name: //p' <<<"$info" | grep -Eiv "$allowed" || true)
    if [[ -n $bad ]]; then
        echo "$file imports DLLs Windows doesn't have: $bad" >&2
        exit 1
    fi
}
logged() {  # logged <name> <command...>: quiet unless it fails
    local name=$1
    shift
    "$@" >"$work/$name.log" 2>&1 || { tail -40 "$work/$name.log" >&2; exit 1; }
}

# ---------------------------------------------------------------- dav1d (AV1 decoding)
d=$work/dav1d
mkdir -p "$d"
tar -xJf "$in/dav1d-$DAV1D_VER.tar.xz" -C "$d" --strip-components=1
logged dav1d-setup meson setup "$d/build" "$d" --cross-file /usr/share/mingw/toolchain-mingw64.meson \
    --prefix="$deps" --libdir=lib -Dbuildtype=release -Ddefault_library=static \
    -Denable_tools=false -Denable_tests=false -Denable_examples=false
logged dav1d-build nice ninja -C "$d/build" -j"$jobs" install

# ---------------------------------------------------------------- FFmpeg, decode only, LGPL
f=$work/ffmpeg
mkdir -p "$f"
tar -xJf "$in/ffmpeg-$FFMPEG_VER.tar.xz" -C "$f" --strip-components=1
# Every demuxer, parser and decoder (ffms2 reads whatever mpv plays); no encoders,
# muxers, filters, devices, network or hardware decoding. Nothing is auto-detected,
# so the result doesn't depend on what the chroot happens to have, and without
# --enable-gpl / --enable-nonfree / --enable-version3 it stays LGPL-2.1-or-later.
ffmpeg_flags=(
    --prefix="$deps" --enable-cross-compile --target-os=mingw32 --arch=x86_64
    --cross-prefix="$host-" --pkg-config=pkg-config --pkg-config-flags=--static
    --enable-static --disable-shared --disable-autodetect --enable-w32threads
    --enable-zlib --enable-libdav1d
    --disable-programs --disable-doc --disable-network --disable-avdevice --disable-avfilter
    --disable-encoders --disable-muxers --disable-hwaccels --disable-debug
)
(cd "$f" && PKG_CONFIG_LIBDIR="$deps/lib/pkgconfig" logged ffmpeg-configure ./configure "${ffmpeg_flags[@]}")
if grep -Eq '^CONFIG_(GPL|NONFREE|GPLV3)=yes' "$f/ffbuild/config.mak"; then
    echo "FFmpeg was configured with GPL or non-free parts" >&2
    exit 1
fi
logged ffmpeg-build nice make -C "$f" -j"$jobs"
logged ffmpeg-install make -C "$f" install

# ---------------------------------------------------------------- FFMS2
s=$work/ffms2
mkdir -p "$s"
tar -xzf "$in/ffms2-$FFMS2_VER.tar.gz" -C "$s" --strip-components=1
: >"$s/config.h"  # autoconf's config.h: FFMS2 5.0 reads nothing from it
# shellcheck disable=SC2046  # pkg-config's word list is meant to split
logged ffms2-build "$host-g++" -O2 -fvisibility=hidden -shared -o "$s/ffms2.dll" \
    -DFFMS_EXPORTS -D__STDC_CONSTANT_MACROS -D_FILE_OFFSET_BITS=64 \
    -I"$s/include" -I"$deps/include" -include "$s/config.h" \
    "$s"/src/core/*.cpp "$s"/src/vapoursynth/*.cpp \
    $(PKG_CONFIG_LIBDIR="$deps/lib/pkgconfig" pkg-config --static --libs \
        libavformat libavcodec libswscale libswresample libavutil) \
    -static -static-libgcc -static-libstdc++ -Wl,--exclude-libs,ALL
"$host-strip" --strip-unneeded "$s/ffms2.dll"
check_imports "$s/ffms2.dll"
exports=$("$objdump" -p "$s/ffms2.dll")  # captured: grep -q would end objdump early (pipefail)
grep -q ' VapourSynthPluginInit2$' <<<"$exports" \
    || { echo "ffms2.dll does not export VapourSynthPluginInit2" >&2; exit 1; }
cp "$s/ffms2.dll" "$out/vapoursynth/"

lic=$out/licenses/ffms2
mkdir -p "$lic"
cp "$s/COPYING" "$lic/COPYING.ffms2"
cp "$f/COPYING.LGPLv2.1" "$lic/COPYING.LGPLv2.1.ffmpeg"
cp "$f/LICENSE.md" "$lic/LICENSE.ffmpeg.md"
cp "$d/COPYING" "$lic/COPYING.dav1d"
# Fedora's mingw64-zlib carries no licence file; zlib's licence is the notice in zlib.h
sed -n '/Copyright (C)/,/madler@alumni.caltech.edu/p' "$sysroot/include/zlib.h" >"$lic/LICENSE.zlib"
grep -q 'Permission is granted' "$lic/LICENSE.zlib" || { echo "zlib licence not found" >&2; exit 1; }
# what the FFmpeg inside was built with (LGPL: enough to rebuild and relink it)
{
    echo "FFmpeg $FFMPEG_VER, configured with:"
    printf '  %s\n' "${ffmpeg_flags[@]}"
} >"$lic/FFMPEG-CONFIGURATION.txt"
zlib_ver=$(rpm -q --qf '%{VERSION}' mingw64-zlib-static)

# ---------------------------------------------------------------- vulkaninfo
v=$work/vulkan-tools
mkdir -p "$v/volk"
tar -xzf "$in/volk-vulkan-sdk-$VULKAN_TOOLS_VER.tar.gz" -C "$v/volk" --strip-components=1
tar -xzf "$in/Vulkan-Tools-$VULKAN_TOOLS_VER.tar.gz" -C "$v" --strip-components=1
patch -d "$v" -p1 -s <"$in/vulkaninfo-mingw-user32.patch"
tc=/usr/share/mingw/toolchain-mingw64.cmake
logged volk-setup cmake -S "$v/volk" -B "$v/volk-build" -G Ninja -DCMAKE_TOOLCHAIN_FILE=$tc \
    -DCMAKE_BUILD_TYPE=Release -DVOLK_INSTALL=ON -DCMAKE_INSTALL_PREFIX="$deps"
logged volk-build ninja -C "$v/volk-build" -j"$jobs" install
logged vt-setup cmake -S "$v" -B "$v/build" -G Ninja -DCMAKE_TOOLCHAIN_FILE=$tc \
    -DCMAKE_BUILD_TYPE=Release -Dvolk_DIR="$deps/lib/cmake/volk" \
    -DBUILD_CUBE=OFF -DBUILD_ICD=OFF -DBUILD_VULKANINFO=ON \
    "-DCMAKE_EXE_LINKER_FLAGS=-static -static-libgcc -static-libstdc++"
logged vt-build nice ninja -C "$v/build" -j"$jobs" vulkaninfo
exe=$(find "$v/build" -name vulkaninfo.exe | head -1)
"$host-strip" --strip-unneeded "$exe"
check_imports "$exe"
cp "$exe" "$out/vulkaninfo.exe"
lic=$out/licenses/vulkan-tools
mkdir -p "$lic"
cp "$v/LICENSE.txt" "$lic/LICENSE.txt"
cp "$v/volk/LICENSE.md" "$lic/LICENSE.volk.md"

# ---------------------------------------------------------------- install-record entries
python3 - "$out" <<EOF
import json, os, subprocess, sys
out = sys.argv[1]
def files(name):
    return subprocess.run(["bash", "-c", f"cd {out} && find licenses/{name} -type f | LC_ALL=C sort"],
                          capture_output=True, text=True, check=True).stdout.split()
components = [
    {"name": "ffms2", "version": "$FFMS2_VER",
     "license": "MIT AND LGPL-2.1-or-later AND BSD-2-Clause AND Zlib",
     "provides": [["bundled(ffmpeg)", "$FFMPEG_VER"], ["bundled(dav1d)", "$DAV1D_VER"],
                  ["bundled(zlib)", "$zlib_ver"]],
     "licence_files": files("ffms2")},
    {"name": "vulkan-tools", "version": "$VULKAN_TOOLS_VER", "license": "Apache-2.0 AND MIT",
     "provides": [["bundled(volk)", "$VULKAN_TOOLS_VER"]], "licence_files": files("vulkan-tools")},
]
with open(os.path.join(out, "components-tools.json"), "w") as fh:
    json.dump({"version": 1, "components": components}, fh, indent=2)
    fh.write("\n")
EOF
ls -l "$out/vapoursynth/ffms2.dll" "$out/vulkaninfo.exe"
