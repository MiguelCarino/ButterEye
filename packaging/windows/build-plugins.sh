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
#   share/rife-ncnn-models/      the packaged RIFE models
#   licenses/<component>/        every licence text the DLLs need
#   components.json              install-record entries (checks_pkgs.manifest_db)
#   sources/                     Corresponding Source for the GPL parts
#   SHA256SUMS
#
# Usage: packaging/windows/build-plugins.sh
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
mock=(mock -r "$chroot_cfg" --uniqueext=buttereye-win -q)
specs=$repo/packaging/plugins

# ---------------------------------------------------------------- sources
# the same fetch, repack and checksum check as the Fedora packages (no SRPM upload)
SPECS="buttereye-vs-mvtools buttereye-vs-rife-ncnn buttereye-vs-mvgpu buttereye-rife-ncnn-models" \
    "$repo/packaging/build.sh" --srpm-only "$chroot_cfg" >/dev/null

q() { rpmspec -q --srpm --define 'dist %{nil}' --qf "$2" "$specs/$1.spec"; }
bundled() { rpmspec -q --define 'dist %{nil}' --provides "$specs/$1.spec" | sed -n "s/^bundled($2) = //p"; }

# ---------------------------------------------------------------- chroot
"${mock[@]}" --init
"${mock[@]}" --install mingw64-gcc-c++ mingw64-libgomp mingw64-winpthreads-static \
    mingw64-fftw-static mingw64-fftw mingw64-vulkan-headers mingw64-vulkan-loader \
    meson ninja-build cmake nasm glslang vapoursynth-devel python3 patch tar gzip
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir "$stage/in"
cp "$src"/vapoursynth-mvtools-v*.tar.gz "$src"/buttereye-vs-rife-ncnn-r*.tar.gz \
   "$src"/ncnn-*.tar.gz "$src"/glslang-*.tar.gz "$src"/buttereye-vs-mvgpu-*.tar.gz \
   "$specs"/buttereye-vs-mvtools-pkgconfig.patch "$specs"/buttereye-vs-rife-ncnn-host-pool.patch \
   "$specs"/buttereye-vs-rife-ncnn-windows-paths.patch \
   "$specs"/licenses/LICENSE.rife-ncnn-vulkan "$specs"/licenses/LGPL-2.1.txt "$specs"/licenses/WTFPL.txt \
   "$here/build-in-chroot.sh" "$stage/in/"
"${mock[@]}" --chroot 'rm -rf /builddir/in /builddir/out'
"${mock[@]}" --copyin "$stage/in" /builddir/in
"${mock[@]}" --chroot "env JOBS=$jobs \
    MVTOOLS_VR='$(q buttereye-vs-mvtools '%{version}-%{release}')' \
    MVTOOLS_LICENSE='$(q buttereye-vs-mvtools '%{license}')' \
    RIFE_VR='$(q buttereye-vs-rife-ncnn '%{version}-%{release}')' \
    RIFE_LICENSE='$(q buttereye-vs-rife-ncnn '%{license}')' \
    MVGPU_VR='$(q buttereye-vs-mvgpu '%{version}-%{release}')' \
    MVGPU_LICENSE='$(q buttereye-vs-mvgpu '%{license}')' \
    NCNN_BUNDLED='$(bundled buttereye-vs-rife-ncnn ncnn)' \
    GLSLANG_BUNDLED='$(bundled buttereye-vs-rife-ncnn glslang)' \
    bash /builddir/in/build-in-chroot.sh"
fftw_nvr=$("${mock[@]}" --chroot "rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}' mingw64-fftw-static" | tail -1)

rm -rf "$out"
mkdir -p "$(dirname "$out")"
"${mock[@]}" --copyout /builddir/out "$out"  # mock creates $out (it won't copy into one)

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
    "$(q buttereye-rife-ncnn-models '%{license}')" <<'EOF'
import json, os, sys
out, vr, lic = sys.argv[1:4]
path = os.path.join(out, "components.json")
data = json.load(open(path))
root = os.path.join(out, "licenses", "buttereye-rife-ncnn-models")
data["components"].append({
    "name": "buttereye-rife-ncnn-models", "version": vr, "license": lic, "provides": [],
    "licence_files": sorted(f"licenses/buttereye-rife-ncnn-models/{f}" for f in os.listdir(root)),
})
json.dump(data, open(path, "w"), indent=2)
EOF

# ---------------------------------------------------------------- Corresponding Source
# GPL parts are linked into the DLLs (MVTools, fftw): ship exactly what was built
mkdir -p "$out/sources"
cp "$src"/vapoursynth-mvtools-v*.tar.gz "$src"/buttereye-vs-rife-ncnn-r*.tar.gz \
   "$src"/ncnn-*.tar.gz "$src"/glslang-*.tar.gz "$src"/buttereye-vs-mvgpu-*.tar.gz \
   "$specs"/*.patch "$here"/build-plugins.sh "$here"/build-in-chroot.sh "$out/sources/"
(cd "$out/sources" && dnf download -q --source "$fftw_nvr") \
    || echo "warning: could not download the $fftw_nvr source package; add it before release" >&2

(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) >"$out/SHA256SUMS"
echo "Windows plugins in $out"
ls -l "$out/vapoursynth"
