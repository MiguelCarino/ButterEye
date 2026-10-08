#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# M0(f) spike: repack the RIFE-ncnn-Vulkan source tag into the two tarballs
# the COPR SRPMs carry (SCOPE.md §8.1, §9):
#
#   buttereye-vs-rife-ncnn-<tag>.tar.gz      plugin source, WITHOUT models/ and
#                                            the subprojects/ncnn submodule
#   buttereye-rife-ncnn-models-<tag>.tar.gz  LICENSE + README + the curated models
#
# A blobless sparse clone is used so the ~1.4 GB of upstream models is never
# downloaded. The tarballs are reproducible (sorted, fixed mtime/owner, gzip -n).
#
# Usage: repack-rife-ncnn.sh <output-dir>
set -euo pipefail

TAG=r9_mod_v33
COMMIT=c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f
URL=https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan.git
# Curated subset per SCOPE.md §5.4; v4.25-lite dropped 2026-10-07 after two
# Xid 109 GPU faults on the bundled build (docs/spikes/m0f.md). Directory names
# must stay intact: the plugin picks padding from them.
MODELS=(
  rife-v4.26_ensembleFalse
  rife-v4.22_lite_ensembleFalse
  rife-v4.18_ensembleFalse
)

mkdir -p "${1:?usage: $0 <output-dir>}"
out=$(realpath "$1")
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

git clone --quiet --filter=blob:none --no-checkout --depth 1 --branch "$TAG" "$URL" "$work/src"
got=$(git -C "$work/src" rev-parse HEAD)
[[ $got == "$COMMIT" ]] || { echo "tag $TAG moved: expected $COMMIT, got $got" >&2; exit 1; }
epoch=$(git -C "$work/src" log -1 --format=%ct)

pack() {  # pack <dir-in-work> <tarball-name>
  tar --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner \
      --exclude=.git -C "$work" -cf - "$1" | gzip -n -9 > "$out/$2"
}

# 1. Plugin source without models/ or subprojects/.
git -C "$work/src" sparse-checkout set --no-cone '/*' '!/models/' '!/subprojects/'
git -C "$work/src" checkout --quiet
cp -a "$work/src" "$work/buttereye-vs-rife-ncnn-$TAG"
cat > "$work/buttereye-vs-rife-ncnn-$TAG/BUTTEREYE-REPACK.txt" <<EOF
Repacked by ButterEye repack-rife-ncnn.sh from $URL
tag $TAG, commit $COMMIT.
Removed: models/ (shipped separately as buttereye-rife-ncnn-models) and the
subprojects/ncnn submodule (Fedora's system ncnn is used instead).
Nothing else was changed.
EOF
pack "buttereye-vs-rife-ncnn-$TAG" "buttereye-vs-rife-ncnn-$TAG.tar.gz"

# 2. Curated models + licence.
patterns=('/LICENSE' '/README.md')
for m in "${MODELS[@]}"; do patterns+=("/models/$m/"); done
git -C "$work/src" sparse-checkout set --no-cone "${patterns[@]}"
mkdir "$work/buttereye-rife-ncnn-models-$TAG"
cp -a "$work/src/LICENSE" "$work/src/README.md" "$work/src/models" "$work/buttereye-rife-ncnn-models-$TAG/"
for m in "${MODELS[@]}"; do
  [[ -f "$work/buttereye-rife-ncnn-models-$TAG/models/$m/flownet.param" ]] \
    || { echo "model $m missing flownet.param" >&2; exit 1; }
done
{
  echo "Repacked by ButterEye repack-rife-ncnn.sh from $URL"
  echo "tag $TAG, commit $COMMIT."
  echo "Contains only these model directories, unmodified:"
  printf '  %s\n' "${MODELS[@]}"
} > "$work/buttereye-rife-ncnn-models-$TAG/BUTTEREYE-REPACK.txt"
pack "buttereye-rife-ncnn-models-$TAG" "buttereye-rife-ncnn-models-$TAG.tar.gz"

cd "$out" && sha256sum "buttereye-vs-rife-ncnn-$TAG.tar.gz" "buttereye-rife-ncnn-models-$TAG.tar.gz"
