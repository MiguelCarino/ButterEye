#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build ButterEye as an x86_64 AppImage - A TRIAL, NOT A RELEASE
# (docs/appimage-trial.md says what it is for, what was measured and what was
# not). The Linux peer of packaging/macos/build-app.sh and
# packaging/windows/build-installer.sh, and self-contained in the same sense: the
# machine it runs on needs a desktop's libraries (host-libs.txt lists exactly
# which) and nothing else - no mpv, no VapourSynth, no Python.
#
# Runs inside an ubuntu:22.04 container (.github/workflows/appimage.yml; or by
# hand: podman run --platform linux/amd64 -v <repo>:/src ubuntu:22.04
# /src/packaging/appimage/build-appimage.sh --install-build-deps, then again
# without it). Why that base, and not manylinux_2_28 / AlmaLinux 8: the PySide6
# wheel is manylinux_2_34, so the AppImage cannot start on a glibc older than
# 2.34 whatever it is built on; Ubuntu 22.04 (glibc 2.35) is the oldest
# mainstream base at that floor, and it has every -dev package mpv needs, where
# AlmaLinux 8 would have meant building the X11 stack too, for no wider reach.
#
#   1. build from pinned source (pins.sh): dav1d, FFmpeg (shared, GPL: x264,
#      Vulkan and NVDEC decoding), glslang (static), libplacebo (static, OpenGL and
#      Vulkan), mpv with the vapoursynth filter on X11 and Wayland, FFMS2, and
#      MVTools with the RPM's patch; libwayland and wayland-protocols for mpv's
#      build only (the host's libwayland is the one that runs)
#   2. the AppDir:
#        AppRun                      the entry point (GUI, or CLI with arguments)
#        python/                     python-build-standalone + site-packages
#                                    (PySide6-Essentials trimmed, VapourSynth's
#                                    manylinux wheel, ButterEye)
#        bin/                        mpv, ffmpeg, ffprobe
#        lib/                        FFmpeg's libraries and every other library
#                                    something needs that is not the host's
#        vapoursynth/                mvtools.so, libffms2.so (not autoloaded)
#        licenses/, buttereye-install.json, buttereye.desktop, buttereye.svg
#   3. closure and check (stage.py): every library outside host-libs.txt is in
#      the AppDir and found through RUNPATH, never through LD_LIBRARY_PATH
#   4. the AppImage: the static type-2 runtime (no libfuse2 on the host) and a
#      zstd SquashFS of the AppDir, concatenated - what appimagetool does, minus
#      appimagetool
#   5. the Corresponding Source tar
#
# Output in $XDG_CACHE_HOME/buttereye/appimage:
#   ButterEye-<version>-x86_64.AppImage, ButterEye-<version>-appimage-sources.tar,
#   SHA256SUMS, build-logs/
# The build tree is /var/tmp/buttereye-appimage (BUTTEREYE_APPIMAGE_WORK): a
# fixed, neutral path on purpose, because FFmpeg's configure line and some
# __FILE__ strings end up inside the binaries, and a path under somebody's home
# folder would carry their user name into the AppImage (scan, below, checks).
#
# Usage: build-appimage.sh [--install-build-deps] [--no-sources]
#   --install-build-deps: apt-get the build dependencies (as root) and exit
#   --no-sources: skip the sources tar, for a quick rebuild, never for a release
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
out=$cache/appimage
work=${BUTTEREYE_APPIMAGE_WORK:-/var/tmp/buttereye-appimage}
jobs=${JOBS:-$(nproc)}
# shellcheck source=pins.sh
source "$here/pins.sh"

# ---------------------------------------------------------------- build dependencies
# The -dev packages of everything mpv and FFmpeg link, and the runtime packages
# the Qt wheel's plugins load, so that `closure` can find on this system each
# library that is not on the host allow-list. deb-src lines are switched on for
# apt-get source (the sources tar).
BUILD_DEPS=(
    build-essential pkg-config nasm ninja-build git ca-certificates curl xz-utils
    zstd patchelf squashfs-tools file dpkg-dev binutils cmake
    zlib1g-dev libx11-dev libxext-dev libxss-dev libxpresent-dev libxrandr-dev
    libegl-dev libgl-dev libdrm-dev libva-dev libasound2-dev libpulse-dev
    libass-dev liblua5.2-dev libx264-dev libfftw3-dev
    libvulkan-dev libxkbcommon-dev libffi-dev libexpat1-dev
    libfontconfig1 libfreetype6 libharfbuzz0b libglib2.0-0 libdbus-1-3
    libegl1 libgl1 libopengl0 libxkbcommon0 libxkbcommon-x11-0
    libwayland-client0 libwayland-cursor0 libwayland-egl1
    libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
    libxcb-render-util0 libxcb-randr0 libxcb-shape0 libxcb-shm0 libxcb-sync1
    libxcb-xfixes0 libxcb-xkb1 libxcb-xinput0 libxcb-render0 libxcb-glx0
    libx11-xcb1 libsm6 libice6
)
if [[ ${1:-} == --install-build-deps ]]; then
    [[ $(id -u) -eq 0 ]] || { echo "--install-build-deps runs apt-get: run it as root" >&2; exit 1; }
    sed -i 's/^# deb-src /deb-src /' /etc/apt/sources.list
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends "${BUILD_DEPS[@]}" >/dev/null
    echo "installed ${#BUILD_DEPS[@]} build dependencies"
    exit 0
fi

sources=1
for arg in "$@"; do
    case $arg in
        --no-sources) sources=0 ;;
        *) echo "usage: $(basename "$0") [--install-build-deps] [--no-sources]" >&2; exit 2 ;;
    esac
done

# ---------------------------------------------------------------- preflight
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 22.04 && $(uname -m) == x86_64 ]] \
    || { echo "build-appimage.sh builds in an x86_64 ubuntu:22.04 container" >&2; exit 1; }
for tool in gcc g++ nasm ninja pkg-config patchelf readelf mksquashfs git curl zstd dpkg cmake; do
    command -v "$tool" >/dev/null || { echo "$tool is missing (--install-build-deps)" >&2; exit 1; }
done
version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/buttereye/__init__.py")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "unexpected version '$version'" >&2; exit 1; }
pymm=${PYTHON_VER%.*}
appimage=ButterEye-$version-x86_64.AppImage
srctar=ButterEye-$version-appimage-sources.tar
epoch=$(git -C "$repo" log -1 --format=%ct 2>/dev/null || date +%s)
export SOURCE_DATE_EPOCH=$epoch

[[ $work == /*/buttereye-appimage* ]] || { echo "bad work dir $work" >&2; exit 1; }
rm -rf "$work" "$out"
mkdir -p "$work/src" "$work/tools" "$out/build-logs"
deps=$work/deps
logs=$out/build-logs
logged() {  # logged <name> <command...>: quiet unless it fails
    local name=$1
    shift
    if ! "$@" >"$logs/$name.log" 2>&1; then
        tail -60 "$logs/$name.log" >&2
        echo "error: $name failed; the whole log is $logs/$name.log" >&2
        exit 1
    fi
}

# ---------------------------------------------------------------- downloads
py_tgz=$(fetch "$PYTHON_URL" "$PYTHON_SHA256")
py_full=$(fetch "$PYTHON_FULL_URL" "$PYTHON_FULL_SHA256")
wheels=(
    "$(fetch "$PYSIDE_URL" "$PYSIDE_SHA256")"
    "$(fetch "$SHIBOKEN_URL" "$SHIBOKEN_SHA256")"
    "$(fetch "$VAPOURSYNTH_URL" "$VAPOURSYNTH_SHA256")"
)
setuptools_whl=$(fetch "$SETUPTOOLS_URL" "$SETUPTOOLS_SHA256")
meson_whl=$(fetch "$MESON_URL" "$MESON_SHA256")
ffmpeg_tar=$(fetch "$FFMPEG_URL" "$FFMPEG_SHA256")
dav1d_tar=$(fetch "$DAV1D_URL" "$DAV1D_SHA256")
ffms2_tar=$(fetch "$FFMS2_URL" "$FFMS2_SHA256")
mpv_tar=$(fetch "$MPV_URL" "$MPV_SHA256")
mvtools_tar=$(fetch "$MVTOOLS_URL" "$MVTOOLS_SHA256")
glslang_tar=$(fetch "$GLSLANG_URL" "$GLSLANG_SHA256")
wayland_tar=$(fetch "$WAYLAND_URL" "$WAYLAND_SHA256")
wlprotos_tar=$(fetch "$WAYLAND_PROTOCOLS_URL" "$WAYLAND_PROTOCOLS_SHA256")
nvcodec_tar=$(fetch "$NVCODEC_URL" "$NVCODEC_SHA256")
runtime=$(fetch "$RUNTIME_URL" "$RUNTIME_SHA256")
qt_srcs=("$(fetch "$QTBASE_URL" "$QTBASE_SHA256")" "$(fetch "$QTSVG_URL" "$QTSVG_SHA256")")
pyside_src=$(fetch "$PYSIDE_SRC_URL" "$PYSIDE_SRC_SHA256")
vs_src=$(fetch "$VAPOURSYNTH_SRC_URL" "$VAPOURSYNTH_SRC_SHA256")

# ---------------------------------------------------------------- build tools
# A copy of the bundled Python outside the AppDir runs the build's helpers and
# meson, so nothing the build does writes into the AppDir by accident.
tar -xzf "$py_tgz" -C "$work/tools"
tar --zstd -xf "$py_full" -C "$work/tools" python/licenses python/PYTHON.json
hostpy=$work/tools/python/bin/python$pymm
stagepy() { "$hostpy" -I -B "$here/stage.py" "$@"; }
stagepy unwheel "$meson_whl" "$work/tools/meson" >/dev/null
mkdir -p "$work/tools/bin"
cat >"$work/tools/bin/meson" <<EOF
#!$hostpy
import sys
sys.path.insert(0, "$work/tools/meson")
from mesonbuild.mesonmain import main
sys.exit(main())
EOF
chmod +x "$work/tools/bin/meson"
export PATH=$work/tools/bin:$PATH
export PKG_CONFIG_PATH=$deps/lib/pkgconfig:$deps/share/pkgconfig:$work/pc
mkdir -p "$work/pc"
echo "==> meson $(meson --version), $(gcc --version | head -1)"

# ---------------------------------------------------------------- 1. dav1d
d=$work/src/dav1d
mkdir -p "$d"
tar -xJf "$dav1d_tar" -C "$d" --strip-components=1
echo "==> dav1d $DAV1D_VER (static, into libavcodec)"
logged dav1d-setup meson setup "$d/build" "$d" --prefix="$deps" --libdir=lib \
    -Dbuildtype=release -Ddefault_library=static -Db_staticpic=true \
    -Denable_tools=false -Denable_tests=false -Denable_examples=false
logged dav1d-build ninja -C "$d/build" -j"$jobs" install

# ---------------------------------------------------------------- 1. libplacebo's checkout, the Vulkan headers
# A git checkout at a pinned commit: the 3rdparty/ submodules it builds with
# (glad for OpenGL, jinja and markupsafe for its shader templates, fast_float,
# and Vulkan-Headers) are in no release archive. Its Vulkan-Headers (1.4.x) are
# also the Vulkan headers FFmpeg and mpv compile against: Ubuntu 22.04's are
# 1.3.204, older than either accepts. vulkan.pc names them, and links the
# loader through libvulkan-dev's libvulkan.so - which is 1.3.204, the oldest a
# host can have, so a symbol missing there fails this link rather than a user's
# start. At run time the loader is the host's (host-libs.txt).
p=$work/src/libplacebo
git init -q "$p"
git -C "$p" fetch -q --depth 1 "$LIBPLACEBO_GIT" "$LIBPLACEBO_COMMIT"
git -C "$p" -c advice.detachedHead=false checkout -q FETCH_HEAD
[[ $(git -C "$p" rev-parse HEAD) == "$LIBPLACEBO_COMMIT" ]] || { echo "libplacebo: wrong commit" >&2; exit 1; }
git -C "$p" config remote.origin.url "$LIBPLACEBO_GIT" 2>/dev/null \
    || git -C "$p" remote add origin "$LIBPLACEBO_GIT"
logged libplacebo-submodules git -C "$p" submodule update --init --depth 1 \
    3rdparty/glad 3rdparty/jinja 3rdparty/markupsafe 3rdparty/fast_float 3rdparty/Vulkan-Headers
vkinc=$p/3rdparty/Vulkan-Headers/include
vkh=$vkinc/vulkan/vulkan_core.h
vkpatch=$(sed -n 's/^#define VK_HEADER_VERSION \([0-9]*\)$/\1/p' "$vkh")
vkmm=$(sed -n 's/^#define VK_HEADER_VERSION_COMPLETE VK_MAKE_API_VERSION(0, \([0-9]*\), \([0-9]*\), VK_HEADER_VERSION)$/\1.\2/p' "$vkh")
[[ -n $vkpatch && -n $vkmm ]] || { echo "could not read the Vulkan header version from $vkh" >&2; exit 1; }
cat >"$work/pc/vulkan.pc" <<EOF
includedir=$vkinc

Name: vulkan
Description: Vulkan headers from libplacebo's 3rdparty/Vulkan-Headers; the loader is the host's
Version: $vkmm.$vkpatch
Libs: -lvulkan
Cflags: -I\${includedir}
EOF
echo "==> Vulkan headers $vkmm.$vkpatch (libplacebo's submodule)"

# ---------------------------------------------------------------- 1. nv-codec-headers
# Headers and ffnvcodec.pc only, into the build prefix: FFmpeg's NVDEC and mpv's
# cuda hwdec compile against them and load the driver's libcuda/libnvcuvid at
# run time (checked below: nothing links them).
nv=$work/src/nv-codec-headers
mkdir -p "$nv"
tar -xzf "$nvcodec_tar" -C "$nv" --strip-components=1
echo "==> nv-codec-headers $NVCODEC_TAG (headers only)"
logged nvcodec-install make -C "$nv" PREFIX="$deps" LIBDIR=lib install

# ---------------------------------------------------------------- 1. FFmpeg
# Shared libraries, because mpv, ffmpeg, ffprobe and FFMS2 all link the same
# ones. Nothing is auto-detected, so the result does not depend on what the
# container happens to have: zlib, iconv, dav1d (AV1), x264 (the encoder render
# falls back to without NVENC: --enable-gpl, which makes these libraries GPL),
# VA-API through libdrm (the host's libva and drivers, never bundled), and
# Vulkan video decoding (mpv's hwdec=vulkan) and NVDEC (hwdec=nvdec), which
# need only headers: FFmpeg dlopens the host's Vulkan loader and NVIDIA's
# libcuda/libnvcuvid, so no library gains a DT_NEEDED for either.
f=$work/src/ffmpeg
mkdir -p "$f"
tar -xJf "$ffmpeg_tar" -C "$f" --strip-components=1
ffmpeg_flags=(
    --prefix="$deps" --libdir="$deps/lib" --enable-shared --disable-static
    --enable-gpl --disable-autodetect --enable-pthreads
    --enable-zlib --enable-iconv --enable-libdav1d --enable-libx264
    --enable-vaapi --enable-libdrm --enable-vulkan --enable-ffnvcodec --enable-nvdec
    --disable-doc --disable-ffplay --disable-debug
)
echo "==> FFmpeg $FFMPEG_VER"
(cd "$f" && logged ffmpeg-configure ./configure "${ffmpeg_flags[@]}")
for want in CONFIG_GPL CONFIG_LIBX264 CONFIG_LIBDAV1D CONFIG_VAAPI CONFIG_VULKAN CONFIG_FFNVCODEC CONFIG_NVDEC CONFIG_ZLIB; do
    grep -q "^$want=yes" "$f/ffbuild/config.mak" || { echo "FFmpeg: $want is not enabled" >&2; exit 1; }
done
if grep -Eq '^CONFIG_(NONFREE)=yes' "$f/ffbuild/config.mak"; then
    echo "FFmpeg was configured with non-free parts" >&2
    exit 1
fi
logged ffmpeg-build make -C "$f" -j"$jobs"
logged ffmpeg-install make -C "$f" install
for so in "$deps"/lib/libav*.so.*; do
    if readelf -dW "$so" | grep -Eq 'Shared library: \[lib(cuda|nvcuvid|vulkan)\.so'; then
        echo "${so##*/} links a GPU driver library; FFmpeg must dlopen those" >&2
        exit 1
    fi
done

# ---------------------------------------------------------------- 1. Wayland, for mpv's build only
# libwayland 1.26 and wayland-protocols 1.49 into the build prefix: their
# headers, wayland-scanner and the protocol XML are what mpv compiles against.
# None of their libraries goes into the AppImage - libwayland-client, -cursor
# and -egl are the host's (host-libs.txt) - and the check after mpv's build
# makes sure mpv asks those for nothing newer than Ubuntu 22.04's 1.20 has.
wl=$work/src/wayland
mkdir -p "$wl"
tar -xJf "$wayland_tar" -C "$wl" --strip-components=1
echo "==> wayland $WAYLAND_VER (build only)"
logged wayland-setup meson setup "$wl/build" "$wl" --prefix="$deps" --libdir=lib \
    -Dbuildtype=release -Dlibraries=true -Dscanner=true -Dtests=false \
    -Ddocumentation=false -Ddtd_validation=false
logged wayland-build ninja -C "$wl/build" -j"$jobs" install
wp=$work/src/wayland-protocols
mkdir -p "$wp"
tar -xJf "$wlprotos_tar" -C "$wp" --strip-components=1
echo "==> wayland-protocols $WAYLAND_PROTOCOLS_VER (build only)"
logged wlprotos-setup meson setup "$wp/build" "$wp" --prefix="$deps" --libdir=lib -Dtests=false
logged wlprotos-build ninja -C "$wp/build" -j"$jobs" install
export PATH=$deps/bin:$PATH

# ---------------------------------------------------------------- 1. glslang
# libplacebo's Vulkan renderer compiles its shaders to SPIR-V at run time, with
# glslang (static, into mpv): no SPIRV-Tools optimiser, no HLSL, no programs.
g=$work/src/glslang
mkdir -p "$g"
tar -xzf "$glslang_tar" -C "$g" --strip-components=1
echo "==> glslang $GLSLANG_VER (static)"
logged glslang-setup cmake -S "$g" -B "$g/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$deps" -DCMAKE_INSTALL_LIBDIR=lib \
    -DBUILD_SHARED_LIBS=OFF -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DENABLE_OPT=OFF -DENABLE_HLSL=OFF -DENABLE_GLSLANG_BINARIES=OFF \
    -DGLSLANG_TESTS=OFF -DBUILD_EXTERNAL=OFF -DGLSLANG_ENABLE_INSTALL=ON
logged glslang-build ninja -C "$g/build" -j"$jobs" install

# ---------------------------------------------------------------- 1. libplacebo
# Static, so mpv carries it; OpenGL and Vulkan (through the host's loader:
# vk-proc-addr links vkGetInstanceProcAddr from it), with glslang found static
# in the build prefix.
echo "==> libplacebo $LIBPLACEBO_VER (static, OpenGL and Vulkan)"
# meson's find_library searches the compiler's own directories, which GCC takes
# from LIBRARY_PATH (LDFLAGS are not searched), and glslang's headers are needed
# when ninja compiles too, not only at setup: both for this step only
export CPATH=$deps/include LIBRARY_PATH=$deps/lib
logged libplacebo-setup meson setup "$p/build" "$p" --prefix="$deps" --libdir=lib \
    -Dbuildtype=release -Ddefault_library=static -Db_staticpic=true -Dprefer_static=true \
    -Dvulkan=enabled -Dvk-proc-addr=enabled -Dopengl=enabled -Dd3d11=disabled \
    -Dglslang=enabled -Dshaderc=disabled -Dlcms=disabled -Ddovi=disabled \
    -Dlibdovi=disabled -Dunwind=disabled -Dxxhash=disabled \
    -Ddemos=false -Dtests=false -Dbench=false -Dfuzz=false
logged libplacebo-build ninja -C "$p/build" -j"$jobs" install
unset CPATH LIBRARY_PATH
for want in vulkan glslang; do
    grep -q "^pl_has_$want=1" "$deps/lib/pkgconfig/libplacebo.pc" \
        || { echo "libplacebo was built without $want" >&2; exit 1; }
done
# a static library's own dependencies are in Libs.private, which mpv's (not
# static) dependency() does not read: glslang's archives, the loader, and
# libstdc++ for glslang's and fast_float's C++. Grouped, because the glslang
# archives refer to each other in no single order.
pl_private=$(sed -n 's/^Libs.private: //p' "$deps/lib/pkgconfig/libplacebo.pc")
sed -i "s|^Libs: .*|& -Wl,--start-group $pl_private -Wl,--end-group -lstdc++|" \
    "$deps/lib/pkgconfig/libplacebo.pc"

# ---------------------------------------------------------------- 1. mpv
# The vapoursynth filter links VSScript from VapourSynth's wheel, which ships
# the headers but no vapoursynth-script.pc: this writes one, pointing at the
# wheel's own folder. The wheel is unpacked here for the build only; the AppDir
# gets its own copy below. Everything else is off unless named: X11 and Wayland,
# each with EGL (OpenGL) and Vulkan contexts, VA-API (copy-back; no DRM
# interop) through X11, Vulkan and NVDEC decoding through FFmpeg (nvdec and
# nvdec-copy: cuda-interop hands NVDEC frames to OpenGL or Vulkan without a
# copy), ALSA and PulseAudio, Lua
# for the on-screen controller. Not vaapi-wayland: it links libva-wayland.so.2,
# which Debian and Ubuntu ship in a package of its own that a desktop need not
# have (measured: mpv would not start on Ubuntu 22.04 and 24.04 or Debian 12 and
# 13). Not vaapi-drm either: it needs mpv's drm feature, which needs
# libdisplay-info, which Ubuntu 22.04 does not have. On native Wayland the GPU
# decoder is Vulkan's, and mpv falls back to software where that is missing.
vsbuild=$work/vs-build
mkdir -p "$vsbuild"
stagepy unwheel "${wheels[2]}" "$vsbuild" >/dev/null
vsdir=$vsbuild/vapoursynth
cat >"$work/pc/vapoursynth-script.pc" <<EOF
libdir=$vsdir
includedir=$vsdir/include

Name: vapoursynth-script
Description: VapourSynth's VSScript, from the R$VAPOURSYNTH_VER wheel
Version: $VAPOURSYNTH_VER
Requires: vapoursynth
Libs: -L\${libdir} -lvsscript
Cflags: -I\${includedir}
EOF
export PKG_CONFIG_PATH=$PKG_CONFIG_PATH:$vsdir/pkgconfig
m=$work/src/mpv
mkdir -p "$m"
tar -xzf "$mpv_tar" -C "$m" --strip-components=1
echo "==> mpv $MPV_VER"
logged mpv-setup meson setup "$m/build" "$m" --prefix="$deps" --libdir=lib \
    -Dbuildtype=release -Dauto_features=disabled -Dbuild-date=false \
    -Dgpl=true -Dcplayer=true -Dlibmpv=false -Dtests=false \
    -Dvapoursynth=enabled -Dlua=enabled -Diconv=enabled -Dzlib=enabled \
    -Dlibavdevice=enabled \
    -Dx11=enabled -Dgl=enabled -Degl=enabled -Degl-x11=enabled \
    -Dwayland=enabled -Degl-wayland=enabled -Dvulkan=enabled \
    -Dcuda-hwaccel=enabled -Dcuda-interop=enabled \
    -Dvaapi=enabled -Dvaapi-x11=enabled \
    -Dalsa=enabled -Dpulse=enabled
logged mpv-build ninja -C "$m/build" -j"$jobs"
logged mpv-install ninja -C "$m/build" install
readelf -dW "$deps/bin/mpv" | grep -q 'Shared library: \[libvsscript.so\]' \
    || { echo "mpv was built without the vapoursynth filter" >&2; exit 1; }
if readelf -dW "$deps/bin/mpv" | grep -Eq 'Shared library: \[lib(cuda|nvcuvid)\.so'; then
    echo "mpv links NVIDIA's driver libraries; they must be loaded at run time" >&2
    exit 1
fi
if readelf -dW "$deps/bin/mpv" | grep -qF 'Shared library: [libva-wayland.so.2]'; then
    echo "mpv links libva-wayland.so.2, which Debian and Ubuntu desktops may lack" >&2
    exit 1
fi
for want in libvulkan.so.1 libwayland-client.so.0 libwayland-cursor.so.0 libwayland-egl.so.1; do
    readelf -dW "$deps/bin/mpv" | grep -qF "Shared library: [$want]" \
        || { echo "mpv does not link $want" >&2; exit 1; }
done
# libwayland is the host's at run time, and the oldest a host can have is this
# container's (Ubuntu 22.04: 1.20; the glibc floor rules out anything older).
# mpv was compiled against 1.26, so every libwayland function it calls has to
# exist in 1.20 too, or it would not start on such a host.
wl_missing=$(comm -23 \
    <(nm -D --undefined-only "$deps/bin/mpv" | awk '{print $2}' | sed 's/@.*//' | grep '^wl_' | sort -u) \
    <(nm -D --defined-only /usr/lib/x86_64-linux-gnu/libwayland-{client,cursor}.so.0 \
        /usr/lib/x86_64-linux-gnu/libwayland-egl.so.1 | awk '{print $3}' | sort -u))
[[ -z $wl_missing ]] || { echo "mpv calls libwayland functions Ubuntu 22.04's lacks:" $wl_missing >&2; exit 1; }

# ---------------------------------------------------------------- 1. FFMS2
# Compiled the way packaging/windows/build-tools-in-chroot.sh does (FFMS2 5.0's
# autotools add nothing the plugin needs), as one shared library that is both
# the VapourSynth plugin and FFMS2's C API.
s=$work/src/ffms2
mkdir -p "$s"
tar -xzf "$ffms2_tar" -C "$s" --strip-components=1
: >"$s/config.h"
echo "==> FFMS2 $FFMS2_VER"
# shellcheck disable=SC2046  # pkg-config's word list is meant to split
logged ffms2-build g++ -O2 -fPIC -fvisibility=hidden -shared -o "$s/libffms2.so" \
    -Wl,-soname,libffms2.so.5 -Wl,--as-needed \
    -DFFMS_EXPORTS -D__STDC_CONSTANT_MACROS -D_FILE_OFFSET_BITS=64 \
    -I"$s/include" -I"$deps/include" -include "$s/config.h" \
    "$s"/src/core/*.cpp "$s"/src/vapoursynth/*.cpp \
    $(pkg-config --libs libavformat libavcodec libswscale libswresample libavutil) -lz
nm -D --defined-only "$s/libffms2.so" | grep -q ' VapourSynthPluginInit2$' \
    || { echo "libffms2.so does not export VapourSynthPluginInit2" >&2; exit 1; }

# ---------------------------------------------------------------- 1. MVTools
# The tag and patch packaging/plugins/buttereye-vs-mvtools.spec builds (the
# patch takes the VapourSynth headers from pkg-config: the wheel's .pc here),
# against Ubuntu's FFTW.
v=$work/src/mvtools
mkdir -p "$v"
tar -xzf "$mvtools_tar" -C "$v" --strip-components=1
patch -d "$v" -p1 -s <"$repo/packaging/plugins/buttereye-vs-mvtools-pkgconfig.patch"
echo "==> MVTools $MVTOOLS_TAG"
# -include math.h: MVDepan.cpp calls isfinite() unqualified, which Fedora's GCC
# accepts through <cmath> and Ubuntu 22.04's GCC 11 does not; libstdc++'s
# <math.h> puts std::isfinite into the global namespace. A compiler flag, not a
# source patch, so the source is still exactly the spec's.
logged mvtools-setup meson setup "$v/build" "$v" --prefix="$deps" --libdir=lib -Dbuildtype=release \
    -Dcpp_args="-include math.h"
logged mvtools-build ninja -C "$v/build" -j"$jobs" install
mvtools_so=$deps/lib/buttereye/vapoursynth/mvtools.so
readelf -dW "$mvtools_so" | grep -q 'Shared library: \[libfftw3f.so.3\]' \
    || { echo "mvtools.so does not link libfftw3f" >&2; exit 1; }

# ---------------------------------------------------------------- 2. the AppDir
appdir=$work/AppDir
mkdir -p "$appdir"/{bin,lib,vapoursynth,share,licenses}
tar -xzf "$py_tgz" -C "$appdir"
py=$appdir/python/bin/python$pymm
site=$appdir/python/lib/python$pymm/site-packages
pystd=$appdir/python/lib/python$pymm
# what ButterEye never runs: Tcl/Tk and tkinter, IDLE, pip, the test suite,
# headers, man pages, the static library, and the interpreter's other names
rm -rf "$appdir/python/include" "$appdir/python/share" \
    "$appdir/python/lib/"{itcl*,tcl*,tk*,thread*,libtcl*,libtk*,pkgconfig} \
    "$pystd/"{tkinter,idlelib,turtledemo,test,ensurepip,turtle.py} \
    "$site/"pip "$site/"pip-*.dist-info \
    "$pystd/lib-dynload/"_tkinter*.so "$pystd/config-$pymm-x86_64-linux-gnu"
find "$appdir/python/bin" -mindepth 1 ! -name "python$pymm" -exec rm -f {} +

for w in "${wheels[@]}"; do stagepy unwheel "$w" "$site"; done
stagepy trim-qt "$site"

# ButterEye itself: the files git knows (committed or not), as a wheel, unpacked
app_src=$work/src/buttereye-$version
mkdir -p "$app_src"
git -C "$repo" ls-files -z --cached --others --exclude-standard -- \
        buttereye pyproject.toml README.md COPYING LICENSES MANIFEST.in CHANGELOG.md \
        THIRD-PARTY.md packaging mvgpu |
    (cd "$repo" && while IFS= read -r -d '' f; do [[ -f $f ]] && printf '%s\0' "$f"; done) |
    (cd "$repo" && tar --null -T - -cf -) | tar -xf - -C "$app_src"
cp -R "$app_src" "$work/src/pristine"
mkdir -p "$work/tools/setuptools"
stagepy unwheel "$setuptools_whl" "$work/tools/setuptools" >/dev/null
(cd "$work/tools" && PYTHONPATH=$work/tools/setuptools "$hostpy" -B -m pip wheel -q \
    --disable-pip-version-check --no-deps --no-build-isolation --no-index \
    -w "$work/wheel" "$app_src")
stagepy unwheel "$work/wheel/buttereye-$version-py3-none-any.whl" "$site"

# the programs, FFmpeg's libraries (one file each, under its soname), the plugins
cp "$deps/bin/mpv" "$deps/bin/ffmpeg" "$deps/bin/ffprobe" "$appdir/bin/"
for so in "$deps"/lib/lib{avcodec,avformat,avutil,avfilter,avdevice,swscale,swresample}.so.*; do
    [[ -L $so ]] && continue
    soname=$(readelf -dW "$so" | sed -n 's/.*(SONAME).*\[\(.*\)\]/\1/p')
    cp "$so" "$appdir/lib/$soname"
done
cp "$mvtools_so" "$appdir/vapoursynth/mvtools.so"
cp "$s/libffms2.so" "$appdir/vapoursynth/libffms2.so"
strip --strip-unneeded "$appdir"/bin/* "$appdir"/lib/*.so* "$appdir"/vapoursynth/*.so
# RUNPATHs of what was built here: mpv also finds VSScript in the wheel's folder
vsrel=python/lib/python$pymm/site-packages/vapoursynth
patchelf --set-rpath "\$ORIGIN/../lib:\$ORIGIN/../$vsrel" "$appdir/bin/mpv"
for b in ffmpeg ffprobe; do patchelf --set-rpath '$ORIGIN/../lib' "$appdir/bin/$b"; done
for l in "$appdir"/lib/*.so*; do patchelf --set-rpath '$ORIGIN' "$l"; done
for plug in "$appdir"/vapoursynth/*.so; do patchelf --set-rpath '$ORIGIN/../lib' "$plug"; done

install -m 755 "$here/AppRun" "$appdir/AppRun"
cp "$repo/buttereye/gui/icons/buttereye.svg" "$appdir/buttereye.svg"
ln -s buttereye.svg "$appdir/.DirIcon"
sed -e 's/^Exec=.*/Exec=buttereye %F/' -e 's/^Icon=.*/Icon=buttereye/' \
    "$repo/packaging/io.github.buttereye.ButterEye.desktop" >"$appdir/buttereye.desktop"
printf 'X-AppImage-Version=%s\n' "$version" >>"$appdir/buttereye.desktop"

# ---------------------------------------------------------------- 3. closure and check
echo "==> closure (host allow-list: packaging/appimage/host-libs.txt)"
stagepy closure "$appdir" "$work/closure.json"

# ---------------------------------------------------------------- licences and the record
lic=$appdir/licenses
mkdir -p "$lic"/{buttereye/LICENSES,python,pyside6,vapoursynth,mpv,ffmpeg,dav1d,libplacebo,glslang,vulkan-headers,wayland,nv-codec-headers,ffms2,buttereye-vs-mvtools,appimage-runtime}
cp "$repo/COPYING" "$repo/THIRD-PARTY.md" "$lic/buttereye/"
cp "$repo"/LICENSES/*.txt "$lic/buttereye/LICENSES/"
cp "$pystd/LICENSE.txt" "$lic/python/LICENSE.txt"
cp "$work/tools/python/licenses/"*.txt "$lic/python/"
cp "$work/tools/python/PYTHON.json" "$lic/python/PYTHON.json"
qt_out=$(stagepy qt-notices "$lic/pyside6" "${qt_srcs[@]}")
[[ $(wc -l <<<"$qt_out" | tr -d ' ') -eq 2 ]] || { echo "qt-notices failed: $qt_out" >&2; exit 1; }
qt_expr=$(sed -n 1p <<<"$qt_out")
qt_provides=$(sed -n 2p <<<"$qt_out")
qt_files=$(cd "$appdir" && find licenses/pyside6 -type f | LC_ALL=C sort | paste -sd, -)
cp "$site/vapoursynth-$VAPOURSYNTH_VER.dist-info/licenses/COPYING.LESSER" "$lic/vapoursynth/"
cp "$m/LICENSE.GPL" "$m/LICENSE.LGPL" "$m/Copyright" "$lic/mpv/"
cp "$f/COPYING.GPLv2" "$f/COPYING.LGPLv2.1" "$f/LICENSE.md" "$lic/ffmpeg/"
{
    echo "FFmpeg $FFMPEG_VER, configured with:"
    printf '  %s\n' "${ffmpeg_flags[@]}"
} >"$lic/ffmpeg/FFMPEG-CONFIGURATION.txt"
cp "$d/COPYING" "$lic/dav1d/COPYING"
cp "$p/LICENSE" "$lic/libplacebo/LICENSE"
cp "$g/LICENSE.txt" "$lic/glslang/LICENSE.txt"
cp "$p/3rdparty/Vulkan-Headers/LICENSE.md" "$lic/vulkan-headers/LICENSE.md"
cp "$p"/3rdparty/Vulkan-Headers/LICENSES/*.txt "$lic/vulkan-headers/"
# mpv carries the code wayland-scanner generated from the protocol XML: wayland's
# own wayland.xml and wayland-protocols' (both MIT)
cp "$wl/COPYING" "$lic/wayland/COPYING.wayland"
cp "$wp/COPYING" "$lic/wayland/COPYING.wayland-protocols"
# nv-codec-headers has no licence file: each header opens with its own MIT notice
for h in "$nv"/include/ffnvcodec/*.h; do
    printf '== %s\n' "${h##*/}"
    sed -n '1,/\*\//p' "$h"
    echo
done >"$lic/nv-codec-headers/NOTICES.txt"
cp "$s/COPYING" "$lic/ffms2/COPYING"
cp "$v/LICENSE" "$lic/buttereye-vs-mvtools/LICENSE"
cp "$v/src/asm/include/x86inc.asm" "$lic/buttereye-vs-mvtools/x86inc.asm"
cat >"$lic/appimage-runtime/NOTICE.txt" <<EOF
The first $(stat -c %s "$runtime") bytes of the AppImage file are the static
AppImage type-2 runtime $RUNTIME_VER (https://github.com/AppImage/type2-runtime),
MIT licence, unmodified (SHA-256 $RUNTIME_SHA256). It links squashfuse, libfuse 3
and zstd statically; their licences are listed in that project's repository.
EOF
echo "==> the Ubuntu libraries the AppImage carries"
stagepy debs "$work/closure.json" "$appdir" "$work/deb-specs.txt" "$work/deb-sources.txt"
deb_specs=()
while IFS= read -r line; do [[ -n $line ]] && deb_specs+=("$line"); done <"$work/deb-specs.txt"
py_lics=$(cd "$appdir" && find licenses/python -type f | LC_ALL=C sort | paste -sd, -)
files_of() { (cd "$appdir" && find "licenses/$1" -type f | LC_ALL=C sort | paste -sd, -); }
echo '{"version": 1, "components": []}' >"$work/components.json"
stagepy record "$work/components.json" "$appdir/buttereye-install.json" \
    "buttereye=$version=AGPL-3.0-or-later AND LGPL-3.0-or-later=licenses/buttereye/COPYING,licenses/buttereye/LICENSES/AdditionRef-ButterEye-generated-output.txt" \
    "python3=$PYTHON_VER=PSF-2.0 AND 0BSD AND Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND MIT AND OpenSSL AND Zlib AND bzip2-1.0.6 AND blessing=$py_lics" \
    "python3-pyside6=$PYSIDE_VER=LGPL-3.0-only AND $qt_expr=$qt_files=bundled(qt6)($PYSIDE_VER),$qt_provides" \
    "python3-shiboken6=$PYSIDE_VER=LGPL-3.0-only=licenses/pyside6/LGPL-3.0-only.txt,licenses/pyside6/GPL-3.0-only.txt" \
    "vapoursynth-libs=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER" \
    "vapoursynth-tools=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER" \
    "mpv=$MPV_VER=GPL-2.0-or-later AND LGPL-2.1-or-later=$(files_of mpv)=bundled(libplacebo)($LIBPLACEBO_VER),bundled(glslang)($GLSLANG_VER)" \
    "libplacebo=$LIBPLACEBO_VER=LGPL-2.1-or-later=$(files_of libplacebo)" \
    "glslang=$GLSLANG_VER=BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2=$(files_of glslang)" \
    "vulkan-headers=$vkmm.$vkpatch=Apache-2.0 OR MIT=$(files_of vulkan-headers)" \
    "wayland-protocols=$WAYLAND_PROTOCOLS_VER=MIT=$(files_of wayland)" \
    "nv-codec-headers=${NVCODEC_TAG#n}=MIT=$(files_of nv-codec-headers)" \
    "ffmpeg=$FFMPEG_VER=GPL-2.0-or-later=$(files_of ffmpeg)=bundled(dav1d)($DAV1D_VER)" \
    "dav1d=$DAV1D_VER=BSD-2-Clause=$(files_of dav1d)" \
    "ffms2=$FFMS2_VER=MIT=$(files_of ffms2)" \
    "buttereye-vs-mvtools=${MVTOOLS_TAG#v}=GPL-2.0-or-later AND ISC=$(files_of buttereye-vs-mvtools)" \
    "appimage-runtime=$RUNTIME_VER=MIT=$(files_of appimage-runtime)" \
    "${deb_specs[@]}"
stagepy sources-txt "$lic/SOURCES.txt" version="$version" repo=https://github.com/MiguelCarino/ButterEye \
    python="$PYTHON_VER" pbs="$PYTHON_PBS" pyside="$PYSIDE_VER" vapoursynth="$VAPOURSYNTH_VER" \
    ffmpeg="$FFMPEG_VER" dav1d="$DAV1D_VER" mpv="$MPV_VER" libplacebo="$LIBPLACEBO_VER" \
    libplacebo_commit="$LIBPLACEBO_COMMIT" ffms2="$FFMS2_VER" mvtools="$MVTOOLS_TAG" \
    glslang="$GLSLANG_VER" wayland="$WAYLAND_VER" wayland_protocols="$WAYLAND_PROTOCOLS_VER" \
    nvcodec="$NVCODEC_TAG" \
    runtime="$RUNTIME_VER" debs="$work/deb-sources.txt"

# bytecode, compiled now because the AppImage is read-only and AppRun passes
# -B: unchecked hashes, relative paths (no .pyc names the build folder)
find "$appdir/python" -name __pycache__ -prune -exec rm -rf {} +
"$hostpy" -I -B -m compileall -q -j0 --invalidation-mode unchecked-hash \
    -s "$appdir/python" -p "python" -x '/(test|tests)/' "$pystd" >/dev/null

echo "==> check"
stagepy check "$appdir" | tee "$work/check.txt"
needles=("$(hostname)")
[[ $(id -un) != root ]] && needles+=("$(id -un)")
[[ -n ${HOME:-} && $HOME != / && $HOME != /root ]] && needles+=("$HOME")
stagepy scan "$appdir" "${needles[@]}"

# ---------------------------------------------------------------- 4. the AppImage
chmod -R u+rwX,go+rX,go-w "$appdir"
find "$appdir" -exec touch -h -d "@$epoch" {} +
mksquashfs "$appdir" "$work/AppDir.squashfs" -root-owned -noappend -no-xattrs \
    -comp zstd -Xcompression-level 19 -b 1M -quiet >/dev/null  # times: SOURCE_DATE_EPOCH
cat "$runtime" "$work/AppDir.squashfs" >"$out/$appimage"
chmod 755 "$out/$appimage"
echo "AppDir: $(du -sm "$appdir" | cut -f1) MiB in $(find "$appdir" -type f | wc -l) files;" \
    "AppImage: $(du -m "$out/$appimage" | cut -f1) MiB"

# ---------------------------------------------------------------- 5. Corresponding Source
if ((sources)); then
    src=$work/srctar/ButterEye-$version-appimage-sources
    mkdir -p "$src/ubuntu"
    cp -R "$work/src/pristine" "$src/buttereye-$version"
    cp "$ffmpeg_tar" "$dav1d_tar" "$mpv_tar" "$ffms2_tar" "$mvtools_tar" "$vs_src" \
        "$glslang_tar" "$wayland_tar" "$wlprotos_tar" "$nvcodec_tar" \
        "${qt_srcs[@]}" "$pyside_src" "$lic/SOURCES.txt" "$src/"
    # libplacebo: the pinned commit with the submodules it was built with
    (cd "$p" && git ls-files -z --recurse-submodules) |
        (cd "$p" && tar --null -T - --transform "s,^,libplacebo-$LIBPLACEBO_VER/," \
            --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner -czf - ) \
        >"$src/libplacebo-$LIBPLACEBO_VER.tar.gz"
    while IFS='=' read -r pkg ver; do
        [[ -n $pkg ]] || continue
        mkdir -p "$src/ubuntu/$pkg"
        (cd "$src/ubuntu/$pkg" && logged "apt-source-$pkg" apt-get source -q --download-only "$pkg=$ver")
    done <"$work/deb-sources.txt"
    find "$src" -exec touch -h -d "@$epoch" {} +
    tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
        -cf "$out/$srctar" -C "$work/srctar" "ButterEye-$version-appimage-sources"
    stagepy scan "$work/srctar" "${needles[@]}"
fi

cp "$work/check.txt" "$work/closure.json" "$appdir/buttereye-install.json" "$logs/"
(cd "$out" && find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z \
    | xargs -0 sha256sum) >"$out/SHA256SUMS"
echo
ls -l "$out"
