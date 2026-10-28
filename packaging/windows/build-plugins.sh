#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build ButterEye's VapourSynth plugins for 64-bit Windows (docs/PLATFORMS.md,
# phase 3) on the Fedora dev box: a mock chroot with Fedora's MinGW-w64 toolchain
# cross-compiles them from the very sources the COPR packages use (fetched and
# checksum-verified by packaging/build.sh), plus one Windows-only patch.
#
#   vapoursynth/mvtools.dll      MVTools 29.2 (fftw linked in)
#   vapoursynth/librife.dll      RIFE-ncnn-Vulkan r9_mod_v33 (ncnn + glslang linked in)
#   vapoursynth/libmvgpu.dll     ButterEye's GPU motion-vector engine
#   vapoursynth/ffms2.dll        FFMS2 5.0 with a decode-only LGPL FFmpeg (offline render)
#   vulkaninfo.exe               Vulkan-Tools' vulkaninfo (the GPU list)
#   share/rife-ncnn-models/      the packaged RIFE models
#   licenses/<component>/        every licence text the DLLs need
#   components.json              install-record entries (checks_pkgs.manifest_db)
#   sources/                     Corresponding Source of what was built (with fftw's
#                                source package), and the models archive
#   SHA256SUMS
#
# ffms2 and vulkaninfo come from pinned upstream releases (pins.sh), built by
# build-tools-in-chroot.sh. The installer (build-installer.sh) packs all of it.
#
# Usage: packaging/windows/build-plugins.sh [--no-mock | --inputs-digest]
#   --no-mock: build right here, as root in a fedora:44 container (CI), instead
#   of in a mock chroot (chroot.sh). The steps are the same.
#   --inputs-digest: build nothing; print the digest of what a build would be made
#   from (build_inputs below). components.json records it as build_inputs.sha256,
#   and build-installer.sh refuses a cache whose digest is not today's.
# Output: $XDG_CACHE_HOME/buttereye/windows (never the repository). JOBS caps
# the compile (default 8; the ncnn build is heavy and the machine may be shared).
# Test the result under Wine with tools/wine-tests.sh.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
src=$cache/packaging/SOURCES
out=$cache/windows
jobs=${JOBS:-8}
chroot_cfg=fedora-44-x86_64
specs=$repo/packaging/plugins
# shellcheck source=pins.sh
source "$here/pins.sh"
# shellcheck source=chroot.sh
source "$here/chroot.sh"

q() { rpmspec -q --srpm --define 'dist %{nil}' --qf "$2" "$specs/$1.spec"; }
bundled() { rpmspec -q --define 'dist %{nil}' --provides "$specs/$1.spec" | sed -n "s/^bundled($2) = //p"; }

# Everything the DLLs are built from, one line each, for a digest that changes when
# any of it does: the packages' version-release (which also pins the mvtools, RIFE
# and models archives) and the ncnn and glslang commits, the pinned ffms2, FFmpeg,
# dav1d, Vulkan-Tools and volk archives, the Windows build's patches and chroot
# scripts, and every file of mvgpu/. A version alone is not enough: mvgpu's source
# changed (766e149) while its version still read 0.0.1, so mvgpu/ is hashed file by
# file, with the same file list build.sh packs into its tarball.
# Every value is checked here rather than left to set -e, which does not reach into
# $(...): without that, a host with no rpmspec printed a digest of empty versions.
build_inputs() {
    local s v
    for s in buttereye-vs-mvtools buttereye-vs-rife-ncnn buttereye-vs-mvgpu buttereye-rife-ncnn-models; do
        v=$(q "$s" '%{version}-%{release}') && [[ -n $v ]] || { echo "rpmspec can't read $s.spec" >&2; return 1; }
        printf 'package %s %s\n' "$s" "$v"
    done
    for s in ncnn glslang; do
        v=$(bundled buttereye-vs-rife-ncnn "$s") && [[ -n $v ]] \
            || { echo "buttereye-vs-rife-ncnn.spec names no bundled($s)" >&2; return 1; }
        printf 'bundled %s %s\n' "$s" "$v"
    done
    printf 'pin %s\n' "$FFMS2_SHA256" "$FFMPEG_SHA256" "$DAV1D_SHA256" \
        "$VULKAN_TOOLS_SHA256" "$VOLK_SHA256"
    (cd "$repo" && sha256sum \
        packaging/plugins/buttereye-vs-mvtools-pkgconfig.patch \
        packaging/plugins/buttereye-vs-rife-ncnn-host-pool.patch \
        packaging/plugins/buttereye-vs-rife-ncnn-windows-paths.patch \
        packaging/windows/build-in-chroot.sh packaging/windows/build-tools-in-chroot.sh \
        packaging/windows/vulkaninfo-mingw-user32.patch) || return 1
    if git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
        git -C "$repo" ls-files -z -co --exclude-standard -- mvgpu COPYING
    else  # an unpacked source tar (the Windows installer's): it holds only tracked files
        (cd "$repo" && find mvgpu COPYING -type f -print0)
    fi | (cd "$repo" && while IFS= read -r -d '' f; do
            if [[ -f $f && $f != mvgpu/build* ]]; then printf '%s\0' "$f"; fi
         done) \
       | LC_ALL=C sort -z | (cd "$repo" && xargs -0 -r sha256sum)
}
inputs_digest() {
    local lines
    lines=$(build_inputs) || return 1
    printf '%s\n' "$lines" | sha256sum | cut -d' ' -f1
}
if [[ ${1:-} == --inputs-digest ]]; then
    (( $# == 1 )) || { echo "--inputs-digest takes no other argument" >&2; exit 2; }
    inputs_digest
    exit 0
fi

win_mode_from_args "$@"
# what this script itself runs outside the chroot (--no-mock: install them here)
win_host_install rpm-build rpmdevtools git-core curl python3 dnf5-plugins tar gzip

# ---------------------------------------------------------------- sources
# taken before anything is built, so a tree edited during the build reads as stale
inputs=$(inputs_digest)
# the same fetch, repack and checksum check as the Fedora packages (no SRPM upload)
SPECS="buttereye-vs-mvtools buttereye-vs-rife-ncnn buttereye-vs-mvgpu buttereye-rife-ncnn-models" \
    "$repo/packaging/build.sh" --srpm-only "$chroot_cfg" >/dev/null
# by version, not a glob: SOURCES keeps every earlier mvgpu tarball as well
mvgpu_tar="$src/buttereye-vs-mvgpu-$(q buttereye-vs-mvgpu '%{version}').tar.gz"
[[ -f $mvgpu_tar ]] || { echo "missing $mvgpu_tar" >&2; exit 1; }

# ffms2 (+ FFmpeg, dav1d) and vulkaninfo (+ volk): pinned upstream releases
tool_srcs=(
    "$(fetch "$FFMS2_URL" "$FFMS2_SHA256")"
    "$(fetch "$FFMPEG_URL" "$FFMPEG_SHA256")"
    "$(fetch "$DAV1D_URL" "$DAV1D_SHA256")"
    "$(fetch "$VULKAN_TOOLS_URL" "$VULKAN_TOOLS_SHA256")"
    "$(fetch "$VOLK_URL" "$VOLK_SHA256")"
)

# ---------------------------------------------------------------- chroot
win_init
win_install mingw64-gcc-c++ mingw64-libgomp mingw64-winpthreads-static \
    mingw64-fftw-static mingw64-fftw mingw64-vulkan-headers mingw64-vulkan-loader \
    meson ninja-build cmake nasm glslang vapoursynth-devel python3 patch tar gzip xz \
    gcc mingw64-zlib-static
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/in"
cp "$src"/vapoursynth-mvtools-v*.tar.gz "$src"/buttereye-vs-rife-ncnn-r*.tar.gz \
   "$src"/ncnn-*.tar.gz "$src"/glslang-*.tar.gz "$mvgpu_tar" \
   "$specs"/buttereye-vs-mvtools-pkgconfig.patch "$specs"/buttereye-vs-rife-ncnn-host-pool.patch \
   "$specs"/buttereye-vs-rife-ncnn-windows-paths.patch \
   "$specs"/licenses/LICENSE.rife-ncnn-vulkan "$specs"/licenses/LGPL-2.1.txt "$specs"/licenses/WTFPL.txt \
   "$here/build-in-chroot.sh" "$stage/in/"
mkdir "$stage/tin"
cp "${tool_srcs[@]}" "$here/vulkaninfo-mingw-user32.patch" "$here/build-tools-in-chroot.sh" "$stage/tin/"
win_run 'rm -rf /builddir/in /builddir/tin /builddir/out'
win_copyin "$stage/in" /builddir/in
win_copyin "$stage/tin" /builddir/tin
win_run "env JOBS=$jobs \
    MVTOOLS_VR='$(q buttereye-vs-mvtools '%{version}-%{release}')' \
    MVTOOLS_LICENSE='$(q buttereye-vs-mvtools '%{license}')' \
    RIFE_VR='$(q buttereye-vs-rife-ncnn '%{version}-%{release}')' \
    RIFE_LICENSE='$(q buttereye-vs-rife-ncnn '%{license}')' \
    MVGPU_VR='$(q buttereye-vs-mvgpu '%{version}-%{release}')' \
    MVGPU_LICENSE='$(q buttereye-vs-mvgpu '%{license}')' \
    NCNN_BUNDLED='$(bundled buttereye-vs-rife-ncnn ncnn)' \
    GLSLANG_BUNDLED='$(bundled buttereye-vs-rife-ncnn glslang)' \
    bash /builddir/in/build-in-chroot.sh"
win_run "env JOBS=$jobs FFMS2_VER=$FFMS2_VER FFMPEG_VER=$FFMPEG_VER \
    DAV1D_VER=$DAV1D_VER VULKAN_TOOLS_VER=$VULKAN_TOOLS_VER bash /builddir/tin/build-tools-in-chroot.sh"
fftw_nvr=$(win_run "rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}' mingw64-fftw-static" | tail -1)

rm -rf "$out"
mkdir -p "$(dirname "$out")"
win_copyout /builddir/out "$out"  # this creates $out (mock won't copy into one)

# ---------------------------------------------------------------- models
models=$out/share/rife-ncnn-models
mkdir -p "$models" "$out/licenses/buttereye-rife-ncnn-models"
tar -xzf "$src"/buttereye-rife-ncnn-models-r*.tar.gz -C "$models" --strip-components=2 --wildcards '*/models/*'
tar -xzf "$src"/buttereye-rife-ncnn-models-r*.tar.gz -C "$out/licenses/buttereye-rife-ncnn-models" \
    --strip-components=1 --wildcards '*/LICENSE'
cp "$specs"/licenses/LICENSE.Practical-RIFE "$specs"/licenses/LICENSE.ECCV2022-RIFE \
   "$specs"/licenses/LICENSE.rife-ncnn-vulkan "$specs"/PROVENANCE-rife-ncnn-models.md \
   "$out/licenses/buttereye-rife-ncnn-models/"
python3 - "$out" "$(q buttereye-rife-ncnn-models '%{version}-%{release}')" \
    "$(q buttereye-rife-ncnn-models '%{license}')" "$inputs" <<'EOF'
import json, os, sys
out, vr, lic, inputs = sys.argv[1:5]
path = os.path.join(out, "components.json")
data = json.load(open(path))
tools = os.path.join(out, "components-tools.json")  # ffms2, vulkan-tools
data["components"] += json.load(open(tools))["components"]
os.remove(tools)
root = os.path.join(out, "licenses", "buttereye-rife-ncnn-models")
data["components"].append({
    "name": "buttereye-rife-ncnn-models", "version": vr, "license": lic, "provides": [],
    "licence_files": sorted(f"licenses/buttereye-rife-ncnn-models/{f}" for f in os.listdir(root)),
})
# what build-installer.sh checks this cache against; it leaves the key out of the
# install record
data["build_inputs"] = {"sha256": inputs}
json.dump(data, open(path, "w"), indent=2)
EOF

# ---------------------------------------------------------------- Corresponding Source
# GPL parts are linked into the DLLs (MVTools, fftw): ship exactly what was built,
# and the models archive the installer's models come from
mkdir -p "$out/sources"
cp "$src"/vapoursynth-mvtools-v*.tar.gz "$src"/buttereye-vs-rife-ncnn-r*.tar.gz \
   "$src"/ncnn-*.tar.gz "$src"/glslang-*.tar.gz "$mvgpu_tar" \
   "$src"/buttereye-rife-ncnn-models-r*.tar.gz \
   "$specs"/*.patch "$here"/build-plugins.sh "$here"/build-in-chroot.sh "$here"/chroot.sh "$out/sources/"
# ffms2.dll links FFmpeg (LGPL) statically: its source and the exact build steps
cp "${tool_srcs[@]}" "$here"/vulkaninfo-mingw-user32.patch "$here"/build-tools-in-chroot.sh \
   "$here"/pins.sh "$out/sources/"
# fftw is GPL and linked into mvtools.dll: its source must go with it, so no
# source package, no build
if ! (cd "$out/sources" && dnf download -q --source "$fftw_nvr") \
    || ! compgen -G "$out/sources/mingw-fftw-*.src.rpm" >/dev/null; then
    echo "error: could not download the $fftw_nvr source package (Corresponding Source of mvtools.dll)" >&2
    rm -rf "$out"  # an output without it must not be packed into an installer
    exit 1
fi

(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) >"$out/SHA256SUMS"
echo "Windows plugins in $out"
ls -l "$out/vapoursynth" "$out/vulkaninfo.exe"
