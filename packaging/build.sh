#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build every ButterEye COPR package (SCOPE §9): SRPMs, then a mock rebuild per
# chroot with networking off (mock's default, the same policy as the COPR).
#
#   buttereye                   the application (packaging/buttereye.spec)
#   buttereye-vs-rife-ncnn      plugins/buttereye-vs-rife-ncnn.spec
#   buttereye-rife-ncnn-models  plugins/buttereye-rife-ncnn-models.spec
#   buttereye-vs-mvtools        plugins/buttereye-vs-mvtools.spec
#
# Usage: packaging/build.sh [--srpm-only] [chroot ...]
#        SPECS="buttereye buttereye-vs-mvtools" packaging/build.sh fedora-44-x86_64
#        default chroots: fedora-44-x86_64 fedora-45-x86_64
# Work and results go to $XDG_CACHE_HOME/buttereye/packaging (never into the
# repository). Compile jobs are capped (JOBS, default 8): the static ncnn and
# glslang build is heavy and the machine may be shared.
# Uploading the SRPMs to the COPR (copr-cli build) is the owner's step.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
srpm_only=0
chroots=()
for a in "$@"; do
    case "$a" in
        --srpm-only) srpm_only=1 ;;
        -h|--help) sed -n '4,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) chroots+=("$a") ;;
    esac
done
(( ${#chroots[@]} )) || chroots=(fedora-44-x86_64 fedora-45-x86_64)
read -ra specs <<<"${SPECS:-buttereye buttereye-vs-mvtools buttereye-vs-rife-ncnn buttereye-rife-ncnn-models}"

top="${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/packaging"
mkdir -p "$top/SOURCES" "$top/SRPMS" "$top/results"
rpmdef=(--define "_topdir $top" --define "_sourcedir $top/SOURCES" --define "_srcrpmdir $top/SRPMS")

# ---------------------------------------------------------------- sources
cp "$here"/plugins/*.patch "$here/plugins/repack-rife-ncnn.sh" \
   "$here"/plugins/licenses/* "$here/plugins/PROVENANCE-rife-ncnn-models.md" \
   "$here"/io.github.buttereye.ButterEye.{desktop,metainfo.xml} "$top/SOURCES/"
if [[ " ${specs[*]} " == *" buttereye "* ]]; then
    python3 -m build --version >/dev/null 2>&1 \
        || { echo "python3 -m build is missing (pip install build, or python3-build)" >&2; exit 1; }
    rm -f "$top"/SOURCES/buttereye-[0-9]*.tar.gz
    python3 -m build --sdist --outdir "$top/SOURCES" "$repo" >/dev/null
fi
if [[ " ${specs[*]} " == *"rife-ncnn"* && ! -f $top/SOURCES/buttereye-vs-rife-ncnn-r9_mod_v33.tar.gz ]]; then
    "$here/plugins/repack-rife-ncnn.sh" "$top/SOURCES"
fi
for s in buttereye-vs-mvtools buttereye-vs-rife-ncnn; do
    [[ " ${specs[*]} " == *" $s "* ]] && spectool -g -C "$top/SOURCES" "$here/plugins/$s.spec" >/dev/null
done
sha256sum -c --quiet --ignore-missing <<EOF
65578c649776ac2da6cfa32b7306841076ae184cac607b914649dfc1237498ce  $top/SOURCES/vapoursynth-mvtools-v29_2.tar.gz
d0dd1cbea993381fe40657b379835a2c3bedccbed8db6c79ffb8d222b25102db  $top/SOURCES/ncnn-305837fd4a722ebc47c5d72e72d8ec9ae970e932.tar.gz
0aa8276b27998fbf22f924279d72b5abd48c5802c234b57d5316567b547aa55f  $top/SOURCES/glslang-a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc.tar.gz
EOF

# ---------------------------------------------------------------- SRPMs
spec_path() { [[ $1 == buttereye ]] && echo "$here/buttereye.spec" || echo "$here/plugins/$1.spec"; }
for s in "${specs[@]}"; do
    rpmbuild -bs "${rpmdef[@]}" "$(spec_path "$s")" | sed 's/^Wrote: /SRPM: /'
done
(( srpm_only )) && exit 0

# ---------------------------------------------------------------- mock
command -v mock >/dev/null || { echo "mock is not installed (sudo dnf install mock)" >&2; exit 1; }
id -nG | grep -qw mock || { echo "add yourself to the 'mock' group first" >&2; exit 1; }
declare -A status
for c in "${chroots[@]}"; do
    for s in "${specs[@]}"; do
        # shellcheck disable=SC2012  # our own SRPM names, newest first
        srpm=$(ls -t "$top/SRPMS/$s-"[0-9]*.src.rpm | head -1)
        res="$top/results/$c/$s"
        rm -rf "$res"; mkdir -p "$res"
        echo "==> $s on $c"
        if mock -r "$c" --resultdir "$res" --define "_smp_ncpus_max ${JOBS:-8}" \
            --rebuild "$srpm" >"$res/mock.out" 2>&1; then
            status[$c/$s]=OK
        else
            status[$c/$s]="FAILED (see $res/build.log and root.log)"
        fi
    done
done

echo
echo "Summary ($top/results)"
rc=0
for c in "${chroots[@]}"; do for s in "${specs[@]}"; do
    printf '  %-18s %-28s %s\n' "$c" "$s" "${status[$c/$s]}"
    [[ ${status[$c/$s]} == OK ]] || rc=1
done; done
exit $rc
