#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build vs-mlrt's TensorRT plugin (vstrt) for your own use (SCOPE §5.2, F11).
#
# ButterEye never ships vstrt: it links NVIDIA's TensorRT, so a local build for
# your own use is the supported route (SCOPE §8.3). You install the NVIDIA
# packages yourself first; this script only checks for them.
#
#   NVIDIA rhel10 repo:   libnvinfer-bin libnvinfer-devel        (TensorRT 11.3.0.99)
#   NVIDIA fedora44 repo: cuda-nvcc-13-4 cuda-cudart-devel-13-4  (CUDA 13.4)
#   Fedora:               vapoursynth-devel cmake ninja-build gcc-c++ git
#
# It clones upstream vs-mlrt at the pinned tag (vstrt's CMake needs git
# describe), builds against Fedora's VapourSynth headers, and installs
# libvstrt.so plus the matching vsmlrt.py into
#   $XDG_DATA_HOME/buttereye/plugins/vstrt/<tag>/
# The build runs inside a memory-capped systemd scope (8 GiB, no swap) with a
# job count sized to the available memory, so it cannot exhaust the machine.
#
# Optional extras, both downloads you start yourself (SCOPE §9):
#   --with-python-deps  onnx, onnxconverter-common and protobuf for fp16 engines,
#                       pip-installed into $XDG_DATA_HOME/buttereye/python
#                       (Fedora's python3-protobuf is too old for onnx)
#   --with-models       vs-mlrt's RIFE v4.26 and v4.22-lite ONNX models into
#                       $XDG_DATA_HOME/buttereye/models/vsmlrt/rife_v2 (needs 7z)
#   --skip-build        only the extras
#
# Usage: contrib/build-vstrt.sh [--tag v16.3.test1] [--march-v3] [--jobs N]
#        [--with-python-deps] [--with-models] [--skip-build]
set -euo pipefail

TAG="v16.3.test1"
CUDA_ROOT="/usr/local/cuda-13.4"
TRT_VERSION="11.3.0.99"
MEM_MAX_MIB="${BUTTEREYE_MEM_MAX_MIB:-8192}"
MARCH=""
JOBS=""
PYDEPS=""
MODELS=""
SKIP_BUILD=""
MODEL_URL="https://github.com/AmusementClub/vs-mlrt/releases/download/external-models"
MODEL_NAMES=(rife_v4.26 rife_v4.22_lite)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tag) TAG="$2"; shift 2 ;;
        --march-v3) MARCH="-march=x86-64-v3"; shift ;;
        --jobs) JOBS="$2"; shift 2 ;;
        --with-python-deps) PYDEPS=1; shift ;;
        --with-models) MODELS=1; shift ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        -h|--help) sed -n '4,31p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
CACHE_HOME="${XDG_CACHE_HOME:-$HOME/.cache}"
PREFIX="$DATA_HOME/buttereye/plugins/vstrt/$TAG"
WORK="$CACHE_HOME/buttereye/build/vs-mlrt-$TAG"

# ------------------------------------------------------------ memory failsafe
if [[ -z "${BUTTEREYE_MEMCAP:-}" && "${BUTTEREYE_NO_MEMCAP:-}" != "1" ]] \
    && command -v systemd-run >/dev/null \
    && systemd-run --user --scope -q --collect -- true 2>/dev/null; then
    export BUTTEREYE_MEMCAP="$MEM_MAX_MIB"
    exec systemd-run --user --scope -q --collect --unit="buttereye-vstrt-$$" \
        -p "MemoryMax=${MEM_MAX_MIB}M" -p MemorySwapMax=0 -- "$0" \
        --tag "$TAG" ${MARCH:+--march-v3} ${JOBS:+--jobs "$JOBS"} \
        ${PYDEPS:+--with-python-deps} ${MODELS:+--with-models} ${SKIP_BUILD:+--skip-build}
fi
avail_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if (( avail_mib < 4096 )); then
    echo "Only ${avail_mib} MiB of memory is available; 4096 MiB is needed to build." >&2
    exit 3
fi
if [[ -z "$JOBS" ]]; then
    # ~1.5 GiB per compiler job, never more than the CPUs or the cap allows
    by_mem=$(( (avail_mib < MEM_MAX_MIB ? avail_mib : MEM_MAX_MIB) / 1536 ))
    JOBS=$(( by_mem < $(nproc) ? by_mem : $(nproc) ))
    (( JOBS >= 1 )) || JOBS=1
fi

install_python_deps() {
    local target="$DATA_HOME/buttereye/python"
    echo "Installing onnx, onnxconverter-common and protobuf into $target"
    mkdir -p "$target"
    python3 -m pip install --quiet --upgrade --target "$target" \
        onnx onnxconverter-common protobuf
}

install_models() {
    command -v 7z >/dev/null || { echo "7z is missing (sudo dnf install 7zip)" >&2; exit 4; }
    local dest="$DATA_HOME/buttereye/models/vsmlrt" tmp
    tmp=$(mktemp -d)
    mkdir -p "$dest"
    for name in "${MODEL_NAMES[@]}"; do
        echo "Downloading $name from vs-mlrt's external-models release"
        curl -fsSL -o "$tmp/$name.7z" "$MODEL_URL/$name.7z"
        7z x -y -bso0 -bsp0 -o"$tmp/$name" "$tmp/$name.7z"
        mkdir -p "$dest/rife_v2"
        install -m 0644 "$tmp/$name/rife_v2/$name.onnx" "$dest/rife_v2/$name.onnx"
    done
    rm -rf "$tmp"
    echo "Models in $dest/rife_v2:"
    ls -l "$dest/rife_v2"
}

[[ -n "$PYDEPS" ]] && install_python_deps
[[ -n "$MODELS" ]] && install_models
[[ -n "$SKIP_BUILD" ]] && exit 0

# ------------------------------------------------------------ checks
missing=()
need_rpm() { rpm -q "$1" >/dev/null 2>&1 || missing+=("$1"); }
for p in vapoursynth-devel cmake ninja-build gcc-c++ git libnvinfer-devel libnvinfer-bin \
         cuda-nvcc-13-4 cuda-cudart-devel-13-4; do
    need_rpm "$p"
done
if (( ${#missing[@]} )); then
    echo "Missing packages: ${missing[*]}" >&2
    echo "See the NVIDIA repository steps in docs/spikes/m0k.md (or SCOPE §5.2)." >&2
    exit 4
fi
have_trt=$(rpm -q --qf '%{VERSION}' libnvinfer-devel)
if [[ "$have_trt" != "$TRT_VERSION" ]]; then
    echo "warning: libnvinfer-devel is $have_trt, the pinned version is $TRT_VERSION" >&2
fi
[[ -x "$CUDA_ROOT/bin/nvcc" ]] || { echo "nvcc not found in $CUDA_ROOT/bin" >&2; exit 4; }
[[ -f /usr/include/vapoursynth/VapourSynth.h ]] \
    || { echo "VapourSynth API3 header missing (vapoursynth-devel)" >&2; exit 4; }

# ------------------------------------------------------------ fetch
mkdir -p "$(dirname "$WORK")"
if [[ ! -d "$WORK/.git" ]]; then
    git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$TAG" https://github.com/AmusementClub/vs-mlrt.git "$WORK"
    git -C "$WORK" fetch --quiet --tags --depth 1 origin "refs/tags/$TAG:refs/tags/$TAG"
fi

# ------------------------------------------------------------ build
echo "Building vstrt $TAG with $JOBS job(s) (memory cap ${BUTTEREYE_MEMCAP:-none} MiB)"
cmake -S "$WORK/vstrt" -B "$WORK/vstrt/build" -G Ninja \
    -D CMAKE_BUILD_TYPE=Release \
    -D VAPOURSYNTH_INCLUDE_DIRECTORY=/usr/include/vapoursynth \
    -D CUDAToolkit_ROOT="$CUDA_ROOT" \
    -D CMAKE_CXX_FLAGS="-Wall -ffast-math $MARCH" >/dev/null
cmake --build "$WORK/vstrt/build" -j "$JOBS"

# ------------------------------------------------------------ install
mkdir -p "$PREFIX"
install -m 0755 "$WORK"/vstrt/build/libvstrt.so "$PREFIX/libvstrt.so"
install -m 0644 "$WORK/scripts/vsmlrt.py" "$PREFIX/vsmlrt.py"
install -m 0644 "$WORK/LICENSE" "$PREFIX/LICENSE"
git -C "$WORK" describe --tags --long > "$PREFIX/VERSION" 2>/dev/null || echo "$TAG" > "$PREFIX/VERSION"
echo "Installed: $PREFIX"
ls -l "$PREFIX"
