#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Run the Windows tests (tests/windows) under Wine with a Windows Python, from
# the Linux dev box. Uses its own Wine prefix so the user's default one is never
# touched. One-time setup (docs/PLATFORMS.md, "Testing the Windows code"):
#
#   WIN=~/.cache/buttereye-dev/win            # or set BUTTEREYE_WIN_DIR
#   WINEPREFIX=$WIN/prefix wineboot -i
#   WINEPREFIX=$WIN/prefix wine python-3.14.x-amd64.exe /quiet InstallAllUsers=0 \
#       'TargetDir=C:\Python314' Include_test=0 Include_tcltk=0 Shortcuts=0
#   WINEPREFIX=$WIN/prefix wine 'C:\Python314\python.exe' -m pip install pytest pytest-asyncio \
#       vapoursynth numpy                   # phase 3: the plugin tests
#   WINEPREFIX=$WIN/prefix wine vc_redist.x64.exe /install /quiet   # VapourSynth needs it
#   # optional, for the end-to-end test: a Windows mpv build unpacked in $WIN/mpv
#
# Usage: tools/wine-tests.sh [pytest args...]
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
win=${BUTTEREYE_WIN_DIR:-$HOME/.cache/buttereye-dev/win}
export WINEPREFIX=$win/prefix
export WINEDEBUG=${WINEDEBUG:--all}
python='C:\Python314\python.exe'
# vspipe.exe (pip's VapourSynth) loads python3.dll, which forwards to python314.dll;
# Windows finds that next to python3.dll, Wine only on PATH. Wine-only workaround.
export WINEPATH='C:\Python314'

# Wine maps / to Z:
winpath() { printf 'Z:%s' "${1//\//\\}"; }

if [[ -f $win/mpv/mpv.exe ]]; then
    video=$win/test.mkv
    if [[ ! -f $video ]] && command -v ffmpeg >/dev/null; then
        ffmpeg -loglevel error -f lavfi -i testsrc=size=320x240:rate=24:duration=5 \
            -c:v libx264 -pix_fmt yuv420p "$video" || true
    fi
    if [[ -f $video ]]; then
        export BUTTEREYE_TEST_MPV; BUTTEREYE_TEST_MPV=$(winpath "$win/mpv/mpv.exe")
        export BUTTEREYE_TEST_VIDEO; BUTTEREYE_TEST_VIDEO=$(winpath "$video")
    fi
fi

# phase 3: the Windows plugin builds (packaging/windows/build-plugins.sh)
winout=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/windows
if [[ -d $winout/vapoursynth ]]; then
    export BUTTEREYE_TEST_PLUGINS; BUTTEREYE_TEST_PLUGINS=$(winpath "$winout/vapoursynth")
fi
if [[ -d $winout/share/rife-ncnn-models ]]; then
    export BUTTEREYE_TEST_MODELS; BUTTEREYE_TEST_MODELS=$(winpath "$winout/share/rife-ncnn-models")
fi

cd "$repo"
# --noconftest: the root conftest caps memory with systemd-run, which is Linux-only.
# -B: no .pyc from the Windows Python in the tree (Linux would reuse them).
# BUTTEREYE_TEST_WINDOWED=1 also runs the test that opens a real mpv window
exec wine "$python" -B -m pytest tests/windows --noconftest -p no:cacheprovider -q "$@"
