#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Test the AppImage trial on one distribution (.github/workflows/appimage.yml
# runs this in clean containers of several, and once on the runner itself).
#
#   1. install a desktop's baseline - the libraries every Linux desktop has and
#      packaging/appimage/host-libs.txt allows the AppImage to take from the
#      host - and NOTHING ButterEye needs on top: no mpv, no VapourSynth, no
#      FFmpeg, no Python. The package lists below are that baseline, per
#      distribution; a missing GUI/sound/GPU library here is a missing library
#      on a real desktop too, and the tests would say so.
#   2. extract the AppImage once (--appimage-extract) for the test harness's
#      own interpreter - the AppImage's Python, so the container needs none -
#      and for the static library check; every test below still starts the
#      AppImage itself, through its runtime
#   3. run packaging/appimage/appimage_tests.py
#
# Usage: test-appimage.sh <the .AppImage> <results folder> [--fuse]
#   without --fuse: APPIMAGE_EXTRACT_AND_RUN=1, because a container has no FUSE
#   with --fuse:    the runtime's own default (mount with fusermount3), for the
#                   run on a real host
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
img=$(readlink -f "$1")
results=$(mkdir -p "$2" && cd "$2" && pwd)
mode=extract
[[ ${3:-} == --fuse ]] && mode=fuse

. /etc/os-release
echo "== $PRETTY_NAME ($(uname -m)), glibc $(ldd --version 2>/dev/null | head -1 | grep -o '[0-9.]*$' || echo '?')"

sudo=
[[ $(id -u) -eq 0 ]] || sudo=sudo
case $ID in
    ubuntu|debian)
        export DEBIAN_FRONTEND=noninteractive
        $sudo apt-get update -qq
        # the 64-bit time_t transition renamed a few packages on 24.04 and 13
        # (libasound2 -> libasound2t64): take whichever name this release has
        pick() {
            local n info
            for n in "$@"; do
                # captured, not piped into grep -q: under pipefail an early grep
                # exit makes apt-cache's SIGPIPE fail the test
                info=$(apt-cache show "$n" 2>/dev/null || true)
                if grep -q '^Version:' <<<"$info"; then echo "$n"; return; fi
            done
            echo "$1"
        }
        $sudo apt-get install -y -qq --no-install-recommends \
            libgl1 libegl1 libopengl0 libvulkan1 libdrm2 libgbm1 libva2 libva-drm2 libva-x11-2 \
            libx11-6 libx11-xcb1 libxext6 libxfixes3 libxkbcommon0 libxkbcommon-x11-0 \
            libxcb1 libxcb-render0 libxcb-shape0 libxcb-shm0 libxcb-sync1 libxcb-xfixes0 \
            libxcb-randr0 libxcb-xkb1 libxcb-xinput0 libxcb-glx0 libxcb-present0 libxcb-dri3-0 \
            libwayland-client0 libwayland-cursor0 libwayland-egl1 \
            libfontconfig1 libfreetype6 libharfbuzz0b "$(pick libglib2.0-0t64 libglib2.0-0)" \
            libdbus-1-3 "$(pick libasound2t64 libasound2)" libpulse0 zlib1g libzstd1 \
            fonts-dejavu-core >/dev/null
        ;;
    fedora)
        $sudo dnf -y -q install --setopt=install_weak_deps=False \
            libglvnd-glx libglvnd-egl libglvnd-opengl vulkan-loader libdrm mesa-libgbm libva \
            libX11 libX11-xcb libXext libXfixes libxcb libxkbcommon libxkbcommon-x11 \
            libwayland-client libwayland-cursor libwayland-egl \
            fontconfig freetype harfbuzz glib2 dbus-libs alsa-lib pulseaudio-libs zlib-ng-compat \
            libzstd dejavu-sans-fonts findutils procps-ng >/dev/null
        ;;
    arch)
        pacman -Sy --noconfirm --needed --noprogressbar \
            libglvnd vulkan-icd-loader libdrm mesa libva \
            libx11 libxext libxfixes libxcb libxkbcommon libxkbcommon-x11 wayland \
            fontconfig freetype2 harfbuzz glib2 dbus alsa-lib libpulse zlib zstd \
            ttf-dejavu >/dev/null
        ;;
    *)
        echo "no desktop baseline written down for $ID" >&2
        exit 1
        ;;
esac
for tool in mpv vspipe ffmpeg ffprobe python3 python; do
    if command -v "$tool" >/dev/null; then
        echo "note: the host has $tool ($(command -v "$tool")); the tests check the AppImage never uses it"
    fi
done

work=$(mktemp -d /tmp/be-appimage-test-XXXXXX)
(cd "$work" && "$img" --appimage-extract >/dev/null)
tree=$work/squashfs-root
echo "extracted for the harness: $(du -sm "$tree" | cut -f1) MiB"

if [[ $mode == extract ]]; then
    export APPIMAGE_EXTRACT_AND_RUN=1
else
    unset APPIMAGE_EXTRACT_AND_RUN
    echo "fusermount: $(command -v fusermount3 || command -v fusermount || echo none); /dev/fuse: $(ls -l /dev/fuse 2>&1)"
fi
"$tree/python/bin/python3.14" -I -B "$here/appimage_tests.py" \
    --appimage "$img" --tree "$tree" --mode "$mode" --results "$results" \
    --host-libs "$here/host-libs.txt"
