#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Run packaging/build.sh, unchanged, inside a Fedora x86_64 container, for a
# build host that is not Fedora (or not x86_64): the same SRPMs, the same mock
# rebuild per chroot with networking off, the same results directory.
#
# Usage: packaging/build-in-container.sh [build.sh arguments]
#        packaging/build-in-container.sh                         # all five, fc44 and fc45
#        SPECS="buttereye" packaging/build-in-container.sh       # the application only
#        SPECS="buttereye-vs-mvgpu" JOBS=4 packaging/build-in-container.sh fedora-44-x86_64
# Needs podman. On Apple Silicon the amd64 container runs under qemu-user in the
# podman machine; leave the machine's Rosetta off: under it GNU tar 1.35 fails
# every file with "Cannot stat: Function not implemented" (measured on the
# machine's 6.19 kernel), and build.sh and mock both run tar. Results land where build.sh puts them,
# $XDG_CACHE_HOME/buttereye/packaging on the host (never in the repository);
# mock's package cache lives in the podman volume buttereye-mock-cache.
#
# The repository is mounted read-only and copied into the container (git
# metadata included, uncommitted edits included, as build.sh itself would see
# them), so the sdist build cannot write into the working tree. mock runs in a
# --privileged container with isolation=simple (no systemd-nspawn, no nested
# podman bootstrap image), as root in the mock group.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
# fedora:44 pinned by the digest of its multi-arch index, the same pin as
# .github/workflows/windows.yml; bump the two together
image=${BUTTEREYE_BUILD_IMAGE:-docker.io/library/fedora:44@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80}
cache="${XDG_CACHE_HOME:-$HOME/.cache}/buttereye"
mkdir -p "$cache"
command -v podman >/dev/null || { echo "podman is not installed" >&2; exit 1; }

exec podman run --rm -i --platform linux/amd64 --privileged \
    -v "$repo:/src:ro" -v "$cache:/cache/buttereye" -v buttereye-mock-cache:/var/cache/mock \
    -e SPECS -e JOBS \
    "$image" bash -s -- "$@" <<'EOF'
set -euo pipefail
dnf -y -q install --setopt=install_weak_deps=False \
    mock rpm-build rpmdevtools python3-build python3-pip git-core tar gzip findutils util-linux \
    </dev/null >/tmp/dnf.log 2>&1 || { cat /tmp/dnf.log >&2; exit 1; }
cat >>/etc/mock/site-defaults.cfg <<'CFG'
config_opts['isolation'] = 'simple'
config_opts['use_bootstrap_image'] = False
CFG
cp -a /src /root/src
# build.sh insists on the mock group; give it to root rather than add a user,
# because the bind-mounted cache keeps the host's ownership and only root can
# write it without chmodding the host's directory
exec runuser -u root -g root -G mock -- env XDG_CACHE_HOME=/cache \
    ${SPECS:+SPECS="$SPECS"} ${JOBS:+JOBS="$JOBS"} /root/src/packaging/build.sh "$@" </dev/null
EOF
