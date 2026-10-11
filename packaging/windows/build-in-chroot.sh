#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Runs inside the mock chroot that packaging/windows/build-plugins.sh prepares
# (Fedora + its MinGW-w64 toolchain, network off). Cross-compiles ButterEye's
# VapourSynth plugins for 64-bit Windows from the same sources and patches as
# the Fedora packages, as self-contained DLLs: the C++ runtime, OpenMP, fftw and
# winpthreads are linked in, so each DLL imports only Windows system libraries
# (and vulkan-1.dll, which the GPU driver installs). The plugins only use
# VapourSynth's C API, so they load into the official (MSVC-built) VapourSynth.
#
# In:  /builddir/in  (sources, patches, licence texts; see build-plugins.sh)
# Out: /builddir/out/vapoursynth/*.dll, /builddir/out/licenses/<component>/,
#      /builddir/out/components.json (install-record entries, see
#      buttereye/core/doctor/checks_pkgs.py: manifest_db)
# Env: JOBS, MVTOOLS_VR, RIFE_VR, MVGPU_VR (version-release), the *_LICENSE
#      expressions and NCNN_BUNDLED / GLSLANG_BUNDLED (as the specs declare them)
set -euo pipefail

in=/builddir/in
out=/builddir/out
work=/builddir/work
jobs=${JOBS:-8}
sysroot=/usr/x86_64-w64-mingw32/sys-root/mingw
strip=x86_64-w64-mingw32-strip
objdump=x86_64-w64-mingw32-objdump

rm -rf "$work" "$out"
mkdir -p "$work" "$out/vapoursynth" "$out/licenses"

# ---------------------------------------------------------------- toolchain setup
# VapourSynth's headers (C API only, no import library: plugins never link it).
# The MinGW pkg-config prefixes every path with the sysroot, so they live there.
mkdir -p "$sysroot/include/vapoursynth" "$sysroot/lib/pkgconfig"
cp /usr/include/vapoursynth/*.h "$sysroot/include/vapoursynth/"
vs_ver=$(rpm -q --qf '%{VERSION}' vapoursynth-devel)
cat >"$sysroot/lib/pkgconfig/vapoursynth.pc" <<EOF
libdir=/lib
includedir=/include/vapoursynth
Name: vapoursynth
Description: VapourSynth headers for the Windows cross build
Version: $vs_ver
Cflags: -I\${includedir}
EOF
# ncnn's CMake OpenMP lookup takes the import library when it can see one, which
# would make librife.dll need libgomp-1.dll; hidden here, it links libgomp.a
if [[ -f $sysroot/lib/libgomp.dll.a ]]; then
    mv "$sysroot/lib/libgomp.dll.a" "$sysroot/lib/libgomp.dll.a.hidden"
fi
# meson hands CMake (ncnn) only the compilers its own project declares (C++)
cat >"$work/cross-extra.ini" <<'EOF'
[binaries]
cmake = 'cmake'

[cmake]
CMAKE_C_COMPILER = 'x86_64-w64-mingw32-gcc'
EOF
meson_cross=(
    --cross-file /usr/share/mingw/toolchain-mingw64.meson
    --cross-file "$work/cross-extra.ini"
    -Dbuildtype=release -Db_lto=false -Dprefer_static=true
    "-Dcpp_link_args=-static -static-libgcc -static-libstdc++ -Wl,--exclude-libs,ALL"
)

# Only these may be imported: anything else would be a DLL the user doesn't have
allowed_imports='^(KERNEL32|msvcrt|vulkan-1)\.dll$'
check_dll() {
    local dll=$1 info bad
    info=$("$objdump" -p "$dll")  # captured: grep -q would end objdump early (pipefail)
    bad=$(sed -n 's/^\s*DLL Name: //p' <<<"$info" | grep -Eiv "$allowed_imports" || true)
    if [[ -n $bad ]]; then
        echo "$dll imports DLLs Windows doesn't have: $bad" >&2
        exit 1
    fi
    grep -q ' VapourSynthPluginInit2$' <<<"$info" \
        || { echo "$dll does not export VapourSynthPluginInit2" >&2; exit 1; }
}

build() {  # build <dir> <dll> [meson options...]
    local dir=$1 dll=$2
    shift 2
    meson setup "$dir/build" "$dir" "${meson_cross[@]}" "$@" >"$work/$(basename "$dir")-setup.log" 2>&1 \
        || { tail -40 "$work/$(basename "$dir")-setup.log" >&2; exit 1; }
    nice ninja -C "$dir/build" -j"$jobs" >"$work/$(basename "$dir")-build.log" 2>&1 \
        || { grep -m5 -B2 -A8 'error' "$work/$(basename "$dir")-build.log" >&2; exit 1; }
    "$strip" --strip-unneeded "$dir/build/$dll"
    check_dll "$dir/build/$dll"
    cp "$dir/build/$dll" "$out/vapoursynth/"
}

# ---------------------------------------------------------------- MVTools
mvt=$work/mvtools
mkdir -p "$mvt"
tar -xzf "$in"/vapoursynth-mvtools-*.tar.gz -C "$mvt" --strip-components=1
patch -d "$mvt" -p1 -s <"$in/buttereye-vs-mvtools-pkgconfig.patch"
build "$mvt" mvtools.dll
lic=$out/licenses/buttereye-vs-mvtools
mkdir -p "$lic"
cp "$mvt/LICENSE" "$lic/LICENSE"
cp "$mvt/src/asm/include/x86inc.asm" "$lic/x86inc.asm"
cp /usr/share/doc/mingw64-fftw/COPYING "$lic/fftw-COPYING"
cp /usr/share/doc/mingw64-fftw/COPYRIGHT "$lic/fftw-COPYRIGHT"
fftw_ver=$(rpm -q --qf '%{VERSION}' mingw64-fftw-static)

# ---------------------------------------------------------------- RIFE-ncnn-Vulkan
rife=$work/rife
mkdir -p "$rife"
tar -xzf "$in"/buttereye-vs-rife-ncnn-*.tar.gz -C "$rife" --strip-components=1
patch -d "$rife" -p1 -s <"$in/buttereye-vs-rife-ncnn-host-pool.patch"     # as on Fedora
patch -d "$rife" -p1 -s <"$in/buttereye-vs-rife-ncnn-windows-paths.patch" # Windows only
mkdir -p "$rife/subprojects/ncnn/glslang"
tar -xzf "$in"/ncnn-*.tar.gz -C "$rife/subprojects/ncnn" --strip-components=1
tar -xzf "$in"/glslang-*.tar.gz -C "$rife/subprojects/ncnn/glslang" --strip-components=1
build "$rife" librife.dll -Duse_system_ncnn=false
lic=$out/licenses/buttereye-vs-rife-ncnn
mkdir -p "$lic"
cp "$rife/LICENSE" "$in/LICENSE.rife-ncnn-vulkan" "$in/LGPL-2.1.txt" "$in/WTFPL.txt" "$lic/"
cp "$rife/subprojects/ncnn/LICENSE.txt" "$lic/LICENSE.ncnn.txt"
cp "$rife/subprojects/ncnn/glslang/LICENSE.txt" "$lic/LICENSE.glslang.txt"

# ---------------------------------------------------------------- mvgpu
gpu=$work/mvgpu
mkdir -p "$gpu"
tar -xzf "$in"/buttereye-vs-mvgpu-*.tar.gz -C "$gpu" --strip-components=1
build "$gpu" libmvgpu.dll -Dglslang=glslangValidator
mkdir -p "$out/licenses/buttereye-vs-mvgpu"
cp "$gpu/COPYING" "$out/licenses/buttereye-vs-mvgpu/COPYING"

# ---------------------------------------------------------------- MinGW-w64 runtime
# linked into every DLL above: GCC's runtime libraries (GCC Runtime Library
# Exception), winpthreads and the CRT start-up code
lic=$out/licenses/mingw-w64-runtime
mkdir -p "$lic"
for f in COPYING3 COPYING3.LIB COPYING.RUNTIME; do cp "/usr/share/licenses/mingw64-gcc/$f" "$lic/gcc-$f"; done
cp /usr/share/licenses/mingw64-winpthreads/COPYING "$lic/winpthreads-COPYING"
cp /usr/share/licenses/mingw64-crt/COPYING "$lic/crt-COPYING"
cp /usr/share/licenses/mingw64-crt/DISCLAIMER "$lic/crt-DISCLAIMER"
gcc_ver=$(rpm -q --qf '%{VERSION}' mingw64-gcc-c++)
mingw_ver=$(rpm -q --qf '%{VERSION}' mingw64-crt)

# ---------------------------------------------------------------- install-record entries
python3 - "$out" <<EOF
import json, os, subprocess, sys
out = sys.argv[1]
def files(name):
    return subprocess.run(["bash", "-c", f"cd {out} && find licenses/{name} -type f | LC_ALL=C sort"],
                          capture_output=True, text=True, check=True).stdout.split()
components = [
    {"name": "buttereye-vs-mvtools", "version": "$MVTOOLS_VR",
     "license": "$MVTOOLS_LICENSE AND GPL-2.0-or-later",
     "provides": [["bundled(fftw)", "$fftw_ver"]], "licence_files": files("buttereye-vs-mvtools")},
    {"name": "buttereye-vs-rife-ncnn", "version": "$RIFE_VR", "license": "$RIFE_LICENSE",
     "provides": [["bundled(ncnn)", "$NCNN_BUNDLED"], ["bundled(glslang)", "$GLSLANG_BUNDLED"]],
     "licence_files": files("buttereye-vs-rife-ncnn")},
    {"name": "buttereye-vs-mvgpu", "version": "$MVGPU_VR", "license": "$MVGPU_LICENSE",
     "provides": [], "licence_files": files("buttereye-vs-mvgpu")},
    {"name": "mingw-w64-runtime", "version": "gcc $gcc_ver, mingw-w64 $mingw_ver",
     "license": "GPL-3.0-or-later WITH GCC-exception-3.1 AND BSD-3-Clause AND MIT AND ZPL-2.1",
     "provides": [], "licence_files": files("mingw-w64-runtime")},
]
with open(os.path.join(out, "components.json"), "w") as fh:
    json.dump({"version": 1, "components": components}, fh, indent=2)
    fh.write("\n")
EOF
ls -l "$out/vapoursynth"
