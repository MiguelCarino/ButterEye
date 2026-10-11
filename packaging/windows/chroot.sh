# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# shellcheck shell=bash
#
# Sourced by build-plugins.sh and build-installer.sh: where their Fedora build
# steps run. Two modes, the same steps:
#
#   mock (default)  a mock chroot, fedora-44-x86_64 --uniqueext=buttereye-win, on
#                   the Fedora dev box (needs mock and the 'mock' group)
#   --no-mock       right here, as root: for a fedora:44 container that IS the
#                   chroot (GitHub Actions, .github/workflows/windows.yml). Refused
#                   outside a container, so a dev box's / is never written to.
#
# Both see the same layout: /builddir/{in,tin,out,nsis}, and the build's work
# folders under /builddir.
#
#   win_mode_from_args "$@"    --no-mock -> direct (BUTTEREYE_WIN_NO_MOCK=1 also)
#   win_init                    create the chroot (mock) / check the container
#   win_install PKG...          install Fedora packages into it
#   win_host_install PKG...     --no-mock only: the tools the scripts use outside
#                               the chroot (the dev box has them; see packaging/README.md)
#   win_run 'COMMAND'           run a shell command in it
#   win_copyin SRC DEST         copy a folder in (DEST must not exist)
#   win_copyout SRC DEST        copy a folder out (DEST must not exist)

win_mock=(mock -r fedora-44-x86_64 --uniqueext=buttereye-win -q)
win_direct=${BUTTEREYE_WIN_NO_MOCK:-0}

win_mode_from_args() {
    local a
    for a in "$@"; do
        case $a in
            --no-mock) win_direct=1 ;;
            -h|--help) sed -n '/^# Usage:/,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
            *) echo "unknown argument: $a (only --no-mock)" >&2; exit 2 ;;
        esac
    done
    export BUTTEREYE_WIN_NO_MOCK=$win_direct  # build-installer.sh -> build-plugins.sh
    win_check_direct
}

# --no-mock: refused unless root inside a container, before anything runs (dnf
# included); every function that writes to / checks again
win_check_direct() {
    if (( win_direct )); then
        if [[ $(id -u) != 0 ]] || [[ ! -e /.dockerenv && ! -e /run/.containerenv ]]; then
            echo "--no-mock builds right in /, so only as root inside a container" >&2
            exit 1
        fi
    fi
}

win_init() {
    if (( win_direct )); then
        win_check_direct
        mkdir -p /builddir
    else
        "${win_mock[@]}" --init
    fi
}

win_install() {
    if (( win_direct )); then
        win_check_direct
        # tsflags: container images skip docs, and the licence texts the build
        # copies include some of those (fftw's COPYING)
        dnf -y -q install --setopt=install_weak_deps=False --setopt=tsflags= "$@"
    else
        "${win_mock[@]}" --install "$@"
    fi
}

win_host_install() {
    if (( win_direct )); then
        win_install "$@"
    fi
}

win_run() {
    if (( win_direct )); then
        (cd /builddir && bash -c "$1")
    else
        "${win_mock[@]}" --chroot "$1"
    fi
}

win_copyin() {
    if (( win_direct )); then
        cp -a "$1" "$2"
    else
        "${win_mock[@]}" --copyin "$1" "$2"
    fi
}

win_copyout() {
    if (( win_direct )); then
        cp -a "$1" "$2"
    else
        "${win_mock[@]}" --copyout "$1" "$2"
    fi
}
