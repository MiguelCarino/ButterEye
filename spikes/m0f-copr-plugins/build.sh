#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# M0(f) spike: build the three plugin SRPMs and rebuild each in mock for the
# COPR chroots, network off (mock's default, same as the planned COPR policy).
#
# Usage: [SPECS="..."] [MOCK_OPTS="--with system_ncnn"] ./build.sh [chroot ...]
#        default chroots: fedora-44-x86_64 fedora-45-x86_64
# Output: work/SRPMS/*.src.rpm, results/<chroot>/<package>/{*.rpm,build.log,root.log}
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
chroots=("$@")
(( ${#chroots[@]} )) || chroots=(fedora-44-x86_64 fedora-45-x86_64)

command -v mock >/dev/null || { echo "mock is not installed: sudo dnf install mock && sudo usermod -aG mock \$USER (then log in again)" >&2; exit 1; }
id -nG | grep -qw mock || { echo "you are not in the 'mock' group yet (log out and back in after usermod)" >&2; exit 1; }

top="$here/work"
mkdir -p "$top/SOURCES" "$top/SRPMS"
rpmdef=(--define "_topdir $top" --define "_sourcedir $top/SOURCES" --define "_srcrpmdir $top/SRPMS")

# Sources: repacked RIFE tarballs (skip if already made), MVTools from GitHub.
if [[ ! -f $top/SOURCES/buttereye-vs-rife-ncnn-r9_mod_v33.tar.gz ]]; then
  "$here/repack-rife-ncnn.sh" "$top/SOURCES"
fi
cp "$here/repack-rife-ncnn.sh" "$here/patches/"*.patch "$top/SOURCES/"
spectool -g -C "$top/SOURCES" "$here/specs/buttereye-vs-mvtools.spec" >/dev/null
spectool -g -C "$top/SOURCES" "$here/specs/buttereye-vs-rife-ncnn.spec" >/dev/null
sha256sum -c --quiet <<EOF
65578c649776ac2da6cfa32b7306841076ae184cac607b914649dfc1237498ce  $top/SOURCES/vapoursynth-mvtools-v29_2.tar.gz
d0dd1cbea993381fe40657b379835a2c3bedccbed8db6c79ffb8d222b25102db  $top/SOURCES/ncnn-305837fd4a722ebc47c5d72e72d8ec9ae970e932.tar.gz
0aa8276b27998fbf22f924279d72b5abd48c5802c234b57d5316567b547aa55f  $top/SOURCES/glslang-a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc.tar.gz
EOF

# SPECS="buttereye-vs-rife-ncnn" ./build.sh  rebuilds a subset.
read -ra specs <<<"${SPECS:-buttereye-vs-rife-ncnn buttereye-rife-ncnn-models buttereye-vs-mvtools}"
for s in "${specs[@]}"; do
  rpmbuild -bs "${rpmdef[@]}" "$here/specs/$s.spec" >/dev/null
done

declare -A status
for c in "${chroots[@]}"; do
  for s in "${specs[@]}"; do
    srpm=$(ls -t "$top/SRPMS/$s-"[0-9]*.src.rpm | head -1)
    res="$here/results/$c/$s"
    rm -rf "$res"; mkdir -p "$res"
    echo "==> $s on $c"
    if mock -r "$c" ${MOCK_OPTS:-} --resultdir "$res" --rebuild "$srpm" >"$res/mock.out" 2>&1; then
      status[$c/$s]=OK
    else
      status[$c/$s]="FAILED (see $res/build.log, root.log)"
    fi
  done
done

echo
echo "Summary"
for c in "${chroots[@]}"; do for s in "${specs[@]}"; do
  printf '  %-18s %-28s %s\n' "$c" "$s" "${status[$c/$s]}"
done; done
