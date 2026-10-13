#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build buttereye-vs-rife-ncnn for arm64 macOS: the RIFE step
# packaging/macos/build-plugins.sh reserves, now filled in (docs/PLATFORMS.md,
# the macOS phase table, phase 4). The phase-3 spike measured all of this
# working - packaging/macos/RIFE-SPIKE.md is the evidence for every decision
# below and the place to read before changing one.
#
# Run by build-plugins.sh with OUT, WORK and JOBS in the environment; runnable on
# its own, when it picks the same defaults build-plugins.sh would.
#
# WHAT IT WRITES, which is the hook's contract in build-plugins.sh and not a
# suggestion - that script refuses a librife.dylib that arrives without any of
# it, because librife.dylib is not ButterEye's own code:
#   $OUT/vapoursynth/librife.dylib               the plugin
#   $OUT/share/rife-ncnn-models/<model>/         the three models it needs
#   $OUT/licenses/buttereye-vs-rife-ncnn/        the plugin's licence texts
#   $OUT/licenses/buttereye-rife-ncnn-models/    the models' own, and their
#                                                provenance chain
#   $OUT/licenses/SOURCES.rife.txt               its paragraph of the written
#                                                offer, appended to SOURCES.txt
#   $OUT/sources/buttereye-vs-rife-ncnn-<tag>/   the Corresponding Source of
#                                                exactly what was built
#   $OUT/components-rife.json                    install-record entries
#
# THIS SCRIPT DOWNLOADS, and build-plugins.sh does not. That is the one promise
# of its header this step cannot keep: the plugin source is not in this
# repository (it is HolyWu's, repacked into the COPR SRPM by
# packaging/plugins/repack-rife-ncnn.sh) and neither are the ~20 MB of model
# weights. Everything fetched is pinned and checked:
#   - the plugin and the models come from the commit repack-rife-ncnn.sh pins,
#     by blobless sparse clone, and the commit hash is verified before anything
#     is built. The sparse checkout is what keeps the ~1.4 GB of upstream models
#     out of it; the plain GitHub tarball of this commit is 1.37 GB.
#   - the four model files are checked against the SHA-256 block in
#     packaging/plugins/PROVENANCE-rife-ncnn-models.md, so the bytes are the
#     bytes Fedora and the Windows installer ship. (Three directories, six
#     files; "four" is per directory.)
#   - the bundled-ncnn variant's two tarballs are checked against the sums in
#     buttereye-vs-rife-ncnn.spec's Source2/Source3, with shasum -a 256
#     (macOS has no sha256sum).
# Downloads are cached in $dl below, so a second run fetches nothing.
#
# WHICH ncnn, and why it is a decision rather than a detail. RIFE-SPIKE.md
# measured both and they are NOT the same product: on the same clip and model,
# 21.3 % of output samples differ, 1.8 % of them by more than one 8-bit step.
# Default here: Homebrew's ncnn (-Duse_system_ncnn=true), which is what the
# spike concluded for a tap.
#   - a tap that statically bundles a 2025 ncnn inside a 5.9 MB bundle, while
#     Homebrew ships ncnn as a maintained formula every other consumer links, is
#     fighting the platform. 167 KB against 5.9 MB, 4 ninja targets against 466,
#     and brew does the security updates.
#   - the two reasons Fedora pins (buttereye-vs-rife-ncnn.spec, SCOPE §8.2,
#     docs/spikes/m0f.md) do not both carry over. Reproducibility does, and is
#     the real cost of this default: the tap pins a formula version, not bytes,
#     so a brew ncnn bump changes ButterEye's output with no change on
#     ButterEye's side. The GPU faults do not: they were ncnn 20250916 on NVIDIA
#     at gpu_thread >= 2, and an M2 has one compute queue, so gpu_thread is 1
#     whatever we build.
#   - what the other choice would cost: BUTTEREYE_RIFE_NCNN=bundled builds it,
#     and it is kept working rather than described - two more pinned tarballs,
#     five more licence texts in the output, a 35x bigger plugin and a much
#     longer build, in exchange for byte-pinned input and 15-65 % more
#     throughput. Pick it if a Homebrew ncnn bump moving the pixels is
#     unacceptable.
# Either way the output is NOT the Linux output, and nothing here may say it is:
# licenses/SOURCES.rife.txt names the ncnn that was used, and so does the run.
#
# WHAT THE TAP STILL OWES, and it is this step that creates the debt: the
# default build links <prefix>/opt/ncnn/lib/libncnn.1.dylib and
# <prefix>/opt/libomp/lib/libomp.dylib. check-dylibs.sh allows both and its
# comment already says neither is DECLARED anywhere: ncnn and libomp are not in
# checks_pkgs.BREW_FORMULAE, so the doctor cannot tell a user why RIFE will not
# load. packaging/macos/homebrew/buttereye-vs-rife-ncnn.rb must declare them as
# dependencies and the doctor must check them. Until it does, a user without
# those two formulae gets a plugin that cannot load, past a gate that passed it.
# The run says this out loud for the same reason build-plugins.sh says what it
# did not build.
#
# RIFE ON A MAC IS OFFLINE-ONLY, measured: 8.5-9.8 fps at 1080p on an M2 against
# the ~48 fps a live 2x 24p needs (RIFE-SPIKE.md, "Throughput"). Nothing
# user-facing may promise live RIFE here, and gpu_thread must be 1 - the single
# compute queue MoltenVK exposes makes anything higher a warning that buys
# nothing. Offline render is unaffected: same engine, and the estimates scale
# from a measured benchmark.
#
# -DVulkan_LIBRARY is deliberately NOT passed. The spike measured it is not
# needed (RIFE's own dependency('vulkan') puts -lvulkan on the final link, which
# is what the static ncnn's simplevk symbols bind to); the only thing it buys is
# silencing one cosmetic CMake warning, and upstream sets the bundled ncnn's
# cmake defines inside its own meson.build, so passing it would mean a third sed
# into upstream source that Fedora does not do. A cosmetic warning is not worth
# making the macOS bundled build diverge from Fedora's.
#
# Assumes installed, all from Homebrew, none of it installed by this script:
#   always:  meson, ninja, pkgconf, vapoursynth, libomp, git, curl
#            (+ Apple's Command Line Tools for clang)
#   default: ncnn
#   bundled: cmake, vulkan-headers, vulkan-loader
#   to run the result: molten-vk, and ncnn + libomp for the default build.
#
# Usage: packaging/macos/build-rife.sh           (build-plugins.sh runs it)
#   BUTTEREYE_RIFE_NCNN=system|bundled   which ncnn (default: system)
#   OUT=<dir> WORK=<dir> JOBS=<n>        as build-plugins.sh passes them
# Nothing here is signed or notarised; see build-plugins.sh's header for why a
# development build does not need to be.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
specs=$repo/packaging/plugins
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
out=${OUT:-$cache/macos}
work=${WORK:-$cache/macos-build}
jobs=${JOBS:-}
dl=$cache/macos-rife-dl          # survives a build, so a rerun fetches nothing
variant=${BUTTEREYE_RIFE_NCNN:-system}

# The pins. The plugin's are repack-rife-ncnn.sh's, the ncnn and glslang ones are
# buttereye-vs-rife-ncnn.spec's Source2/Source3: one set of pins for all three
# platforms, so a macOS build is the same source as the COPR and the installer.
tag=r9_mod_v33
commit=c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f
url=https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan.git
version=9.33                     # buttereye-vs-rife-ncnn.spec's Version
ncnn_commit=305837fd4a722ebc47c5d72e72d8ec9ae970e932
ncnn_date=20250503
glslang_commit=a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc
ncnn_sha=d0dd1cbea993381fe40657b379835a2c3bedccbed8db6c79ffb8d222b25102db
glslang_sha=0aa8276b27998fbf22f924279d72b5abd48c5802c234b57d5316567b547aa55f
# repack-rife-ncnn.sh's curated set (SCOPE §5.4), whose bytes
# PROVENANCE-rife-ncnn-models.md records. The directory NAMES are load-bearing:
# the plugin reads the padding and the v2/v4 architecture out of them, so they
# may not be renamed.
models=(rife-v4.26_ensembleFalse rife-v4.22_lite_ensembleFalse rife-v4.18_ensembleFalse)

case $variant in
    system|bundled) ;;
    *) echo "error: BUTTEREYE_RIFE_NCNN must be 'system' or 'bundled', not '$variant'" >&2
       exit 2 ;;
esac

# ---------------------------------------------------------------- preflight
if [[ $(uname -s) != Darwin ]]; then
    echo "error: this is the macOS RIFE build; on Linux the RPM is packaging/plugins/" >&2
    exit 1
fi
if [[ $(uname -m) != arm64 ]]; then
    echo "warning: this machine is $(uname -m); RIFE-SPIKE.md only measured Apple Silicon" >&2
fi

brew_prefix=$(brew --prefix 2>/dev/null || echo /opt/homebrew)
export PATH=$brew_prefix/bin:$PATH

missing=()
need() {  # need <command> <brew formula>
    command -v "$1" >/dev/null || missing+=("$1 (brew install $2)")
}
need meson meson
need ninja ninja
need pkg-config pkgconf
need git git
need curl curl
need patch "Command Line Tools: xcode-select --install"
need clang "Command Line Tools: xcode-select --install"
pkg-config --exists vapoursynth 2>/dev/null || missing+=("pkg-config module vapoursynth (brew install vapoursynth)")
# libomp is keg-only, so it is never on a default include or library path and
# pkg-config does not see it: look for the files the two flags below use.
[[ -f $brew_prefix/opt/libomp/lib/libomp.dylib ]] || missing+=("libomp (brew install libomp)")
if [[ $variant == system ]]; then
    # upstream asks cmake, not pkg-config, for ncnn; the headers are enough to tell
    [[ -f $brew_prefix/opt/ncnn/include/ncnn/net.h ]] || missing+=("ncnn (brew install ncnn)")
else
    need cmake cmake
    pkg-config --exists vulkan 2>/dev/null \
        || missing+=("pkg-config module vulkan (brew install vulkan-headers vulkan-loader)")
fi
if ((${#missing[@]})); then
    echo "error: this build needs these, and this script installs nothing:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
fi

src=$work/rife-src
tree=$work/rife-tree
mkdir -p "$dl" "$work" "$out"
rm -rf "$src" "$tree"

# ---------------------------------------------------------------- the source
# A blobless sparse clone: the plugin without models/ or the ncnn submodule,
# plus the three model directories by name. The commit is verified before any of
# it is used, so a moved tag fails here rather than silently building something
# else. --depth 1 --branch <tag> with --filter=blob:none fetches a few MB.
echo "==> RIFE: source at $tag ($commit)"
sparse=('/*' '!/models/' '!/subprojects/')
for m in "${models[@]}"; do sparse+=("/models/$m/"); done
git clone --quiet --filter=blob:none --no-checkout --depth 1 --branch "$tag" "$url" "$src"
got=$(git -C "$src" rev-parse HEAD)
if [[ $got != "$commit" ]]; then
    echo "error: tag $tag moved: expected $commit, got $got" >&2
    echo "packaging/plugins/repack-rife-ncnn.sh pins that commit; it is the pin, not a hint" >&2
    exit 1
fi
git -C "$src" sparse-checkout set --no-cone "${sparse[@]}"
git -C "$src" checkout --quiet

# The models' bytes, against the sums PROVENANCE-rife-ncnn-models.md records.
# A model file that is not the packaged one is a silent difference in output, so
# it fails here: shasum -c on the block the provenance file publishes.
echo "==> RIFE: models against PROVENANCE-rife-ncnn-models.md"
sed -n '/^```$/,/^```$/p' "$specs/PROVENANCE-rife-ncnn-models.md" \
    | grep -E '^[0-9a-f]{64}  ' >"$work/rife-models.sha256"
(cd "$src/models" && shasum -a 256 -c "$work/rife-models.sha256")

# ---------------------------------------------------------------- %prep
# The two seds buttereye-vs-rife-ncnn.spec's %prep does, for the same two
# reasons, with its two checks after them. The first is not optional on macOS:
# Homebrew's vapoursynth.pc has no libdir at all (VapourSynth lives in a libexec
# virtualenv), so meson cannot even configure without it.
cp -a "$src" "$tree"
# models/ and the ncnn submodule are what repack-rife-ncnn.sh strips out of the
# plugin tarball, so strip them here too: the models are shipped as their own
# component out of $src, the meson sed above drops the install_subdir that would
# have installed them, and sources/ must then be the plugin source and nothing
# else - a second copy of 20 MB of weights in the written offer is noise.
rm -rf "$tree/.git" "$tree/models"
cd "$tree"
sed -i '' "s|vapoursynth_dep.get_variable(pkgconfig: 'libdir') / 'vapoursynth'|get_option('libdir') / 'buttereye' / 'vapoursynth'|" meson.build
sed -i '' "/^install_subdir('models',/,/^)/d" meson.build
grep -q "'buttereye' / 'vapoursynth'" meson.build
if grep -q "install_subdir('models'" meson.build; then
    echo "error: install_subdir('models') is still in meson.build" >&2
    exit 1
fi

# The patches, in the spec's order and numbering. All three apply here, with
# -F0 so a hunk that only nearly matches fails instead of landing somewhere
# plausible:
#   no-pack8   required against Homebrew's ncnn (20260526 has no
#              Option::use_shader_pack8 either, the same failure Fedora's
#              20250916 produced), and measured bit-identical against the pinned
#              one, so unlike the Fedora spec it is applied to both variants.
#   host-pool  the frame-buffer pool; output is bit-identical.
#   model-path the 242-character SIGABRT in load_param_model. Not macOS-specific
#              - it is the #else branch Fedora builds - which is why it lives in
#              packaging/plugins/ and the spec applies it unconditionally too.
# The Windows-paths patch is #if _WIN32 only and is not wanted here.
echo "==> RIFE: patches"
for p in no-pack8 host-pool model-path; do
    echo "    buttereye-vs-rife-ncnn-$p.patch"
    patch -p1 -F0 --no-backup-if-mismatch <"$specs/buttereye-vs-rife-ncnn-$p.patch" >/dev/null
done

# ---------------------------------------------------------------- build
# The OpenMP flags. warp.cpp's CPU warp path has one #pragma omp parallel for,
# and the plugin's own meson.build gives clang '-Xpreprocessor -fopenmp' as
# COMPILE args with no link args. Apple clang emits __kmpc_* calls from that, the
# shared_module links anyway (meson gives it -undefined dynamic_lookup) and it
# loads anyway, because the CPU warp is never reached while Vulkan works - so the
# missing symbols are never looked up. A build that ever did reach it would abort
# instead of falling back. libomp is keg-only, so the paths have to be explicit
# even though brew installed it as an ncnn dependency. Measured: with these two
# flags the module binds under RTLD_NOW, and without them the bundled variant
# fails with "symbol not found in flat namespace '___kmpc_for_static_fini'".
omp_cflags="-I$brew_prefix/opt/libomp/include"
omp_ldflags="-L$brew_prefix/opt/libomp/lib -lomp"

if [[ $variant == bundled ]]; then
    # The pinned ncnn and the glslang fork that ncnn commit wants, fetched and
    # checksummed the way packaging/windows/pins.sh does it, into the subproject
    # directory upstream's meson.build expects.
    fetch() {  # fetch <url> <sha256>
        local f=$dl/${1##*/}
        [[ -f $f ]] || curl -fsSL --retry 3 -o "$f" "$1"
        echo "$2  $f" | shasum -a 256 -c
    }
    echo "==> RIFE: pinned ncnn $ncnn_commit + glslang $glslang_commit"
    fetch "https://github.com/Tencent/ncnn/archive/$ncnn_commit/ncnn-$ncnn_commit.tar.gz" "$ncnn_sha"
    fetch "https://github.com/nihui/glslang/archive/$glslang_commit/glslang-$glslang_commit.tar.gz" "$glslang_sha"
    mkdir -p "$tree/subprojects/ncnn/glslang"
    tar -xzf "$dl/ncnn-$ncnn_commit.tar.gz" -C "$tree/subprojects/ncnn" --strip-components=1
    tar -xzf "$dl/glslang-$glslang_commit.tar.gz" -C "$tree/subprojects/ncnn/glslang" --strip-components=1
    # The spec copies these two out in %prep and ships them as %license; they
    # travel with a statically linked ncnn here for the same reason.
    cp -p "$tree/subprojects/ncnn/LICENSE.txt" "$tree/LICENSE.ncnn.txt"
    cp -p "$tree/subprojects/ncnn/glslang/LICENSE.txt" "$tree/LICENSE.glslang.txt"
fi

echo "==> RIFE: native arm64 build (ncnn: $variant)"
meson setup "$work/rife" "$tree" --prefix=/ -Dlibdir=lib -Dbuildtype=release \
    -Duse_system_ncnn="$([[ $variant == system ]] && echo true || echo false)" \
    -Dcpp_args="$omp_cflags" -Dcpp_link_args="$omp_ldflags" \
    >"$work/rife-setup.log" 2>&1 \
    || { tail -40 "$work/rife-setup.log" >&2; exit 1; }
ninja -C "$work/rife" ${jobs:+-j"$jobs"} >"$work/rife-build.log" 2>&1 \
    || { grep -m5 -B2 -A8 -i 'error' "$work/rife-build.log" >&2; exit 1; }
# Installed rather than copied out of the build tree, for the reason mvgpu is:
# meson records build-tree LC_RPATHs (here three, one of them a versioned
# vulkan-loader Cellar path) and strips them on install. check-dylibs.sh refuses
# any LC_RPATH, and is right to. --strip keeps _VapourSynthPluginInit2, which is
# an exported symbol and not debug information.
rm -rf "$work/rife-dest"
meson install -C "$work/rife" --destdir "$work/rife-dest" --strip --quiet >>"$work/rife-build.log" 2>&1
built=$work/rife-dest/lib/buttereye/vapoursynth/librife.dylib
[[ -f $built ]] || { echo "error: meson install left no $built" >&2; exit 1; }

# ---------------------------------------------------------------- the two checks
# The macOS peer of the spec's %check: the right ncnn is linked (or not).
linked=$(otool -L "$built" | sed -n 's/^\t\([^ ]*\).*/\1/p')
if [[ $variant == system ]]; then
    grep -q 'libncnn' <<<"$linked" \
        || { echo "error: the system-ncnn build links no libncnn" >&2; exit 1; }
else
    if grep -q 'libncnn' <<<"$linked"; then
        echo "error: the bundled ncnn is meant to be static, but libncnn is linked" >&2
        exit 1
    fi
fi

# And the OpenMP check, which is this platform's real one: bind every symbol at
# load time (RTLD_NOW) instead of waiting for the CPU warp path to look one up.
# Without the link flags above this is where the build fails, loudly, in the
# script that caused it.
python3 - "$built" <<'EOF'
import ctypes, sys
try:
    ctypes.CDLL(sys.argv[1], mode=ctypes.RTLD_LOCAL | 0x2)  # 0x2 is RTLD_NOW
except OSError as exc:
    sys.exit(f"error: librife.dylib has unresolved symbols under RTLD_NOW: {exc}")
print("    RTLD_NOW: every symbol resolves (the OpenMP runtime is linked)")
EOF

mkdir -p "$out/vapoursynth"
cp "$built" "$out/vapoursynth/librife.dylib"

# ---------------------------------------------------------------- models
# $OUT/share/rife-ncnn-models, the layout the Windows installer uses and the one
# the hook names. A tap installs it at <prefix>/share/buttereye/rife-ncnn-models,
# which is what types._packaged_data_dir() looks in on macOS.
echo "==> RIFE: models"
mkdir -p "$out/share/rife-ncnn-models"
for m in "${models[@]}"; do
    cp -R "$src/models/$m" "$out/share/rife-ncnn-models/$m"
done

# ---------------------------------------------------------------- licences
# The plugin's. MIT plugin over two bundled headers with their own licences, and
# five more texts when ncnn and glslang are linked in. The spec is the authority
# for which: these are its %license lines.
lic=$out/licenses/buttereye-vs-rife-ncnn
mkdir -p "$lic"
cp "$tree/LICENSE" "$lic/LICENSE"                       # the tag's own, MIT
cp "$specs/licenses/LICENSE.rife-ncnn-vulkan" "$lic/"   # the implementation it follows
cp "$specs/licenses/LGPL-2.1.txt" "$lic/"               # bundled RIFE/VapourSynth4.h
cp "$specs/licenses/WTFPL.txt" "$lic/"                  # bundled RIFE/VSHelper4.h
if [[ $variant == bundled ]]; then
    cp "$tree/LICENSE.ncnn.txt" "$tree/LICENSE.glslang.txt" "$lic/"
    licence="MIT AND LGPL-2.1-or-later AND WTFPL AND BSD-3-Clause AND BSD-2-Clause AND Zlib AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2"
    provides="[[\"bundled(ncnn)\", \"0^${ncnn_date}git${ncnn_commit:0:8}\"], [\"bundled(glslang)\", \"0^git${glslang_commit:0:8}\"]]"
else
    licence="MIT AND LGPL-2.1-or-later AND WTFPL"
    provides="[]"   # nothing is bundled: ncnn is the user's own Homebrew formula
fi

# The models'. A separate component with its own licence and its own chain, and
# "MIT" alone is not the answer for it: the four texts are the conversions'
# (the tag's own LICENSE), nihui's rife-ncnn-vulkan, hzwer's Practical-RIFE and
# Megvii's ECCV2022-RIFE, and the chain that links them is the PROVENANCE file,
# which ships beside them exactly as the Windows build ships it.
mlic=$out/licenses/buttereye-rife-ncnn-models
mkdir -p "$mlic"
cp "$tree/LICENSE" "$mlic/LICENSE"
cp "$specs/licenses/LICENSE.rife-ncnn-vulkan" "$specs/licenses/LICENSE.Practical-RIFE" \
   "$specs/licenses/LICENSE.ECCV2022-RIFE" "$specs/PROVENANCE-rife-ncnn-models.md" "$mlic/"

# ---------------------------------------------------------------- Corresponding Source
# Present in the output rather than promised, the way sources/mvgpu is: the
# plugin source exactly as it was built (after the seds and the three patches),
# the patches themselves, and this script. A directory, not a tar, for the reason
# build-plugins.sh gives - macOS's bsdtar has no --sort, so a reproducible
# archive would need GNU tar, and SHA256SUMS records every file one by one.
echo "==> RIFE: Corresponding Source"
srcout=$out/sources/buttereye-vs-rife-ncnn-$tag
mkdir -p "$srcout"
cp -R "$tree/." "$srcout/"
rm -rf "$srcout/subprojects/ncnn/.git" "$srcout/build" "$srcout"/build-*
cp "$specs"/buttereye-vs-rife-ncnn-no-pack8.patch \
   "$specs"/buttereye-vs-rife-ncnn-host-pool.patch \
   "$specs"/buttereye-vs-rife-ncnn-model-path.patch "$out/sources/"
cat >"$srcout/BUTTEREYE-MACOS-BUILD.txt" <<EOF
This is the source of $out/vapoursynth/librife.dylib, as built.

Upstream: $url
  tag $tag, commit $commit.
Removed before building: models/ (they are in share/rife-ncnn-models, with
  their licences and provenance in licenses/buttereye-rife-ncnn-models) and
  upstream's subprojects/ncnn submodule.
Already applied to these files: the two meson.build edits
  buttereye-vs-rife-ncnn.spec makes in %prep, and the three ButterEye patches
  in ../ (no-pack8, host-pool, model-path).
Built by packaging/macos/build-rife.sh (a copy is in ..) with:
  meson setup --prefix=/ -Dlibdir=lib -Dbuildtype=release \\
      -Duse_system_ncnn=$([[ $variant == system ]] && echo true || echo false) \\
      -Dcpp_args="$omp_cflags" -Dcpp_link_args="$omp_ldflags"
EOF

# ---------------------------------------------------------------- the written offer
# Appended to licenses/SOURCES.txt by build-plugins.sh, never written over it.
ncnn_used=$([[ $variant == system ]] \
    && echo "Homebrew's ncnn $(brew list --versions ncnn 2>/dev/null | awk '{print $2}' | tail -1)" \
    || echo "the pinned ncnn ${ncnn_commit:0:8} + glslang ${glslang_commit:0:8}, linked statically")
# The bundled variant's licence expression is one 200-character line; fold it
# into the document's second column the way build-plugins.sh folds $rife_note.
licence_wrapped=$(printf '%s\n' "$licence." | fold -s -w 48 \
    | sed -e 's/ *$//' -e 's/^/                               /' -e '1s/^ *//')
cat >"$out/licenses/SOURCES.rife.txt" <<EOF

  vapoursynth/librife.dylib    buttereye-vs-rife-ncnn $version,
                               $licence_wrapped
                               HolyWu's VapourSynth-RIFE-ncnn-Vulkan at tag
                               $tag (commit
                               $commit),
                               $url,
                               with the two meson.build edits and the three
                               ButterEye patches
                               packaging/plugins/buttereye-vs-rife-ncnn.spec
                               applies. Its complete Corresponding Source, as
                               built, is
                               sources/buttereye-vs-rife-ncnn-$tag,
                               with those patches beside it in sources/ and the
                               licence texts in
                               licenses/buttereye-vs-rife-ncnn.
                               Built against $ncnn_used.
                               This is NOT byte-for-byte the Linux build: the
                               two ncnn builds disagree about 21.3 % of output
                               samples, 1.8 % of them by more than one 8-bit
                               step (packaging/macos/RIFE-SPIKE.md).
  share/rife-ncnn-models/      buttereye-rife-ncnn-models $version, MIT. The
                               model directories from the same commit,
                               unmodified, which are their own complete source
                               (they are data, and the files shipped are the
                               files published). Their licence texts and the
                               provenance chain behind them - ncnn conversions
                               of hzwer's Practical-RIFE weights, continuing
                               ECCV2022-RIFE, following nihui's
                               rife-ncnn-vulkan - are in
                               licenses/buttereye-rife-ncnn-models.
EOF

# ---------------------------------------------------------------- install record
# Merged into components.json by build-plugins.sh and read by
# checks_pkgs.manifest_db. The licence expressions are
# buttereye-vs-rife-ncnn.spec's and buttereye-rife-ncnn-models.spec's, which are
# the authority for them; there is no rpm release part here, as for mvgpu.
python3 - "$out" "$version" "$licence" "$provides" <<'EOF'
import json, os, sys

out, version, licence, provides = sys.argv[1:5]
def files(component):
    root = os.path.join(out, "licenses", component)
    return sorted(f"licenses/{component}/{f}" for f in os.listdir(root))

components = [
    {"name": "buttereye-vs-rife-ncnn", "version": version, "license": licence,
     "provides": json.loads(provides), "licence_files": files("buttereye-vs-rife-ncnn")},
    {"name": "buttereye-rife-ncnn-models", "version": version, "license": "MIT",
     "provides": [], "licence_files": files("buttereye-rife-ncnn-models")},
]  # fmt: skip
for c in components:
    for f in c["licence_files"]:
        if not os.path.isfile(os.path.join(out, f)):
            sys.exit(f"{c['name']}: licence file {f} is not in {out}")
with open(os.path.join(out, "components-rife.json"), "w") as fh:
    json.dump({"version": 1, "components": components}, fh, indent=2)
    fh.write("\n")
EOF

# ---------------------------------------------------------------- what happened
echo
echo "RIFE-ncnn: librife.dylib ($(wc -c <"$out/vapoursynth/librife.dylib" | tr -d ' ') bytes), ncnn: $variant"
echo "           built against $ncnn_used"
echo "           models: ${models[*]}"
echo "           OFFLINE ONLY on a Mac: 8.5-9.8 fps at 1080p on an M2 against the ~48 fps"
echo "           live 2x 24p needs, and gpu_thread must be 1 (one compute queue through"
echo "           MoltenVK). Nothing user-facing may promise live RIFE here."
if [[ $variant == system ]]; then
    echo "           NOT THE LINUX OUTPUT: Homebrew's ncnn and the pinned one disagree about"
    echo "           21.3 % of output samples (1.8 % by more than one 8-bit step)."
    echo "           STILL OWED BY THE TAP: this links $brew_prefix/opt/ncnn and"
    echo "           $brew_prefix/opt/libomp, and neither formula is declared"
    echo "           (checks_pkgs.BREW_FORMULAE has no ncnn and no libomp), so the doctor"
    echo "           cannot tell a user why RIFE will not load. The tap formula must"
    echo "           depend on both."
fi
