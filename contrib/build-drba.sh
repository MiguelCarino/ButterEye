#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Set up the DRBA models for ButterEye's TensorRT engine (optional, experimental).
#
# DistilDRBA (MIT, routineLife1) interpolates 2D animation differently from
# RIFE: what moves on every frame (pans, zooms) is smoothed, and a drawing held
# on twos or threes keeps the pace it was drawn at instead of being bent
# between poses. A profile uses it by naming the model (`model = "drba-v1"`
# with `backend = "rife-trt"`); nothing takes it on its own.
#
# Upstream ships PyTorch weights. This script downloads them and upstream's
# network definition at a pinned commit, exports ONNX files with
# contrib/drba_export.py and installs them into
#   $XDG_DATA_HOME/buttereye/models/vsmlrt/drba/
# PyTorch is needed for the export only: it goes into a temporary folder
# (CPU build, about 1 GiB of download) that is removed at the end. Playback
# needs TensorRT as set up by contrib/build-vstrt.sh, and no PyTorch.
#
# Usage: contrib/build-drba.sh [--models v1,v2_lite] [--keep-work]
set -euo pipefail

COMMIT="db75f98455ddf06fff4d14fafa3347c7b5f38db2"
REPO="https://github.com/routineLife1/VS-DistilDRBA"
WEIGHTS_URL="$REPO/releases/download/weights"
declare -A SHA256=(
    [v1]="3096c7228f2159628184ab9dc78484e29b1c031eb68ff7c17ebb23f72726a96f"
    [v2_lite]="9de2437c40928d13c48fff0d5e1021b78c8446d544143a964d56c8a2b387c3ed"
)
MODELS="v1,v2_lite"
KEEP=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --models) MODELS="$2"; shift 2 ;;
        --keep-work) KEEP=1; shift ;;
        -h|--help) sed -n '4,22p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DEST="$DATA_HOME/buttereye/models/vsmlrt/drba"
for tool in git curl python3 sha256sum; do
    command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 4; }
done
avail_mib=$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)
if (( avail_mib < 3072 )); then
    echo "Only ${avail_mib} MiB of memory is available; 3072 MiB is needed for the export." >&2
    exit 3
fi

WORK=$(mktemp -d "${TMPDIR:-/var/tmp}/buttereye-drba.XXXXXX")
cleanup() { [[ -n "$KEEP" ]] && echo "Work folder kept: $WORK" || rm -rf "$WORK"; }
trap cleanup EXIT

echo "Fetching upstream's network definition ($COMMIT)"
git init -q "$WORK/src"
git -C "$WORK/src" fetch -q --depth 1 "$REPO" "$COMMIT"
git -C "$WORK/src" checkout -q FETCH_HEAD

echo "Installing PyTorch (CPU) and onnx into a temporary folder"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --index-url https://download.pytorch.org/whl/cpu torch
"$WORK/venv/bin/pip" install --quiet onnx numpy

mkdir -p "$DEST"
IFS=',' read -r -a names <<< "$MODELS"
for name in "${names[@]}"; do
    [[ -n "${SHA256[$name]:-}" ]] || { echo "unknown model: $name (v1, v2_lite)" >&2; exit 2; }
    echo "Downloading $name.pkl"
    curl -fsSL -o "$WORK/$name.pkl" "$WEIGHTS_URL/$name.pkl"
    echo "${SHA256[$name]}  $WORK/$name.pkl" | sha256sum --check --quiet
    echo "Exporting drba_$name.onnx"
    "$WORK/venv/bin/python" "$HERE/drba_export.py" --source "$WORK/src/vsdrba_distilled" \
        --weights "$WORK/$name.pkl" --out "$WORK/drba_$name.onnx" 2>"$WORK/export-$name.log" \
        || { tail -n 20 "$WORK/export-$name.log" >&2; exit 5; }
    install -m 0644 "$WORK/drba_$name.onnx" "$DEST/drba_$name.onnx"
done
install -m 0644 "$WORK/src/LICENSE" "$DEST/LICENSE.DistilDRBA"

echo "Models in $DEST:"
ls -l "$DEST"
echo "Use one in a profile: backend = \"rife-trt\", model = \"drba-v1\" (or \"drba-v2_lite\")."
