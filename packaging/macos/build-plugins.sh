#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build ButterEye's VapourSynth plugins for arm64 macOS (docs/PLATFORMS.md, the
# macOS phase table, phase 3). The Windows peer of this script
# (packaging/windows/build-plugins.sh) cross-compiles four plugins in a mock
# chroot with Fedora's MinGW-w64 toolchain; here there is no cross-compiler, no
# chroot and no container. This is a native Apple clang build on the developer's
# own Mac, out of this repository, and it builds one thing:
#
#   vapoursynth/libmvgpu.dylib   ButterEye's GPU motion-vector engine (Vulkan)
#   licenses/<component>/        the licence text of everything built here
#   licenses/SOURCES.txt         where that source is (the written offer)
#   components.json              install-record entries (checks_pkgs.manifest_db)
#   sources/                     the Corresponding Source of what was built
#   SHA256SUMS                   every file above
#
# What it does NOT build, and why. The run says all of this again, out loud,
# because a silent omission is how a packager ends up shipping three plugins and
# believing it shipped four:
#
#   MVTools      Homebrew ships vapoursynth-mvtools as an arm64 bottle (29 here),
#                and it really interpolates: a ButterEye .vpy took a 24 fps clip
#                to 48 through vspipe with mvtools.dylib autoloaded. Upstream's
#                meson.build already takes the aarch64 branch and bundles
#                sse2neon.h, and clang -O3 auto-vectorises the plain-C SAD into
#                NEON, so there is no arm64 performance reason to build our own.
#   ffms2        Homebrew ships ffms2 (5.0 here). The Windows build compiles its
#                own for a reason that does not exist on macOS: the official
#                Windows ffms2 binary is GPLv3 through its FFmpeg, with source
#                that cannot be reconstructed, so ButterEye could not meet the
#                source offer for a binary it conveyed. Here ButterEye conveys
#                nothing: the user installs the bottle, and Homebrew is the
#                distributor with the source duty.
#   RIFE-ncnn    NOT BUILT here, though it is now known to work:
#                packaging/macos/RIFE-SPIKE.md measured the pinned tag building
#                natively on arm64 against Homebrew's ncnn with ButterEye's two
#                existing patches, loading into VapourSynth R81, finding the GPU
#                through MoltenVK and rendering a correct in-between frame. What
#                does not exist is a build: this script has only the hook below,
#                so macOS still has no RIFE engine, and every run says so. If a
#                packaging/macos/build-rife.sh lands next to this script, this
#                script runs it as an extra step and holds it to the licence and
#                source duties the RIFE section spells out.
#   vulkaninfo   Homebrew's vulkan-tools provides it. The Windows build compiles
#                its own only because Windows ships none.
#
# Assumes installed, all from Homebrew, none of it installed by this script:
#   build:  meson, ninja, glslang, pkgconf, vapoursynth, vulkan-headers,
#           vulkan-loader, and Apple's Command Line Tools for clang
#   to run the result: molten-vk (there is no native Vulkan on macOS, so
#           libmvgpu.dylib reaches the GPU through MoltenVK as a portability
#           driver) and vapoursynth. A missing molten-vk does not stop a build.
# Nothing is downloaded: everything built here is in this repository, and
# everything else comes from a bottle the user installs. That is why this script
# has no pins.sh (packaging/windows/pins.sh's fetch() also calls sha256sum,
# which macOS does not have; the checksum tool here is shasum -a 256).
#
# Nothing here is signed or notarised, and a phase-3 build does not need to be.
# The only signature on what this builds is the ad-hoc one Apple's linker
# applies: measured on libmvgpu.dylib, `codesign -dvvv` reports
# flags=0x20002(adhoc,linker-signed), Signature=adhoc and TeamIdentifier=not set.
# That is what arm64 needs in order to load the file at all, and it says nothing
# about who built it. It is enough here because the output is a cache directory
# on the machine that built it and is not distributed - Gatekeeper asks about a
# file that arrived from somewhere else, and nothing arrived. Distribution is
# phase 4, and packaging/README.md says why the plan there is a Homebrew tap: brew
# does not set com.apple.quarantine on what it fetches, while an unsigned .app
# downloaded from a GitHub release carries it and is refused outright. A Developer
# ID signature and notarisation need a paid Apple Developer account, which is
# phase 4's decision and not a prerequisite for building a plugin.
#
# Usage: packaging/macos/build-plugins.sh
# Output: ${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/macos, never the repository.
#   The build tree is $XDG_CACHE_HOME/buttereye/macos-build. JOBS caps the
#   compile (default: ninja's own choice).
# Check the result: packaging/macos/check-dylibs.sh <that>/vapoursynth, which
#   this script runs itself, and tests/macos/test_plugins_macos.py with
#   BUTTEREYE_TEST_PLUGINS pointing at <that>/vapoursynth.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
out=$cache/macos
work=$cache/macos-build
jobs=${JOBS:-}

# ---------------------------------------------------------------- preflight
if [[ $(uname -s) != Darwin ]]; then
    echo "error: this is the macOS build; on Linux use packaging/build.sh" >&2
    exit 1
fi
if [[ $(uname -m) != arm64 ]]; then
    # Nothing here is arm64-only on purpose, but every measurement behind macOS
    # phase 3 was made on Apple Silicon, and MoltenVK is the only Vulkan driver
    # on a Mac: an Intel Mac is untested rather than refused on principle.
    echo "warning: this machine is $(uname -m), not arm64; macOS phase 3 was only measured on Apple Silicon" >&2
fi

# Homebrew's bin on PATH, so meson finds ninja, glslang and pkgconf even when
# the caller's PATH is bare (cron, a GUI-launched terminal, CI).
brew_prefix=$(brew --prefix 2>/dev/null || echo /opt/homebrew)
export PATH=$brew_prefix/bin:$PATH

missing=()
need() {  # need <command> <brew formula>
    command -v "$1" >/dev/null || missing+=("$1 (brew install $2)")
}
need meson meson
need ninja ninja
need glslang glslang
need pkg-config pkgconf
need clang "Command Line Tools: xcode-select --install"
for mod in vapoursynth vulkan; do
    pkg-config --exists "$mod" 2>/dev/null \
        || missing+=("pkg-config module $mod (brew install $([[ $mod == vulkan ]] && echo "vulkan-headers vulkan-loader" || echo vapoursynth))")
done
if ((${#missing[@]})); then
    echo "error: this build needs these, and this script installs nothing:" >&2
    printf '  - %s\n' "${missing[@]}" >&2
    exit 1
fi

rm -rf "$out" "$work"
mkdir -p "$out" "$work"

# ---------------------------------------------------------------- mvgpu
# A plain out-of-tree meson build of mvgpu/ as it stands in this repository:
# zero source changes, seven GLSL compute shaders through glslang to SPIR-V, one
# shared_module. Installed with `meson install` rather than copied out of the
# build tree, because that is what drops the build-tree LC_RPATH (meson records
# the versioned Homebrew Cellar path of libvulkan while building and strips it on
# install; check-dylibs.sh refuses such an rpath, and is right to).
# --prefix=/ with -Dplugindir=vapoursynth puts the file straight at the layout
# this script promises: $out/vapoursynth/libmvgpu.dylib.
echo "==> mvgpu: native arm64 build"
meson setup "$work/mvgpu" "$repo/mvgpu" --prefix=/ -Dplugindir=vapoursynth \
    -Dbuildtype=release >"$work/mvgpu-setup.log" 2>&1 \
    || { tail -40 "$work/mvgpu-setup.log" >&2; exit 1; }
ninja -C "$work/mvgpu" ${jobs:+-j"$jobs"} >"$work/mvgpu-build.log" 2>&1 \
    || { grep -m5 -B2 -A8 -i 'error' "$work/mvgpu-build.log" >&2; exit 1; }
# --strip keeps the plugin entry point (it is an exported symbol, not debug info)
meson install -C "$work/mvgpu" --destdir "$out" --strip --quiet >>"$work/mvgpu-build.log" 2>&1
mvgpu_ver=$(meson introspect "$work/mvgpu" --projectinfo \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])')

lic=$out/licenses/buttereye-vs-mvgpu
mkdir -p "$lic"
cp "$repo/COPYING" "$lic/COPYING"  # mvgpu is ButterEye's own, AGPL-3.0-or-later

# ---------------------------------------------------------------- RIFE-ncnn (optional step)
# The spike is done and it says yes: packaging/macos/RIFE-SPIKE.md measured the
# plugin building natively on arm64 from the commit
# packaging/plugins/repack-rife-ncnn.sh pins, with ButterEye's two existing
# patches, loading into VapourSynth R81 and rendering a correct frame through
# MoltenVK. No build script came out of it, so nothing here builds RIFE and macOS
# has no RIFE engine; the spike also found two things a real build must carry (the
# libomp link flags, and a new patch for a 256-byte sprintf in RIFE's model-path
# loader that aborts the process at 242 characters).
#
# If a packaging/macos/build-rife.sh lands next to this one it is run here, with
# the same output and work directories. The contract is DUTIES, not just a file,
# because librife.dylib is not ButterEye's own code the way libmvgpu.dylib is:
# it is HolyWu's MIT plugin over bundled LGPL-2.1-or-later and WTFPL headers,
# built against ncnn, and the models it needs are a curated MIT set with their own
# provenance chain (packaging/plugins/PROVENANCE-rife-ncnn-models.md). Conveying
# it means conveying those texts and a source offer. So the step must write all of
# this, the way packaging/windows/build-in-chroot.sh fills licenses/<component>/
# for the DLLs it builds, and the check below refuses a librife.dylib that arrives
# without them - a hook that asked only for a dylib would let a future RIFE step
# satisfy this script while breaking the AGPL:
#
#   $OUT/vapoursynth/librife.dylib        the plugin itself.
#   $OUT/licenses/buttereye-vs-rife-ncnn/ every licence text it needs: the tag's
#                                         own LICENSE, LICENSE.rife-ncnn-vulkan,
#                                         LGPL-2.1.txt and WTFPL.txt (all four are
#                                         in packaging/plugins/licenses/), plus
#                                         LICENSE.ncnn.txt and LICENSE.glslang.txt
#                                         when it links them statically instead of
#                                         using Homebrew's ncnn.
#   $OUT/licenses/buttereye-rife-ncnn-models/  and, when it also ships
#                                         $OUT/share/rife-ncnn-models, that set's
#                                         four MIT texts and the PROVENANCE file.
#   $OUT/licenses/SOURCES.rife.txt        its own paragraph of the written offer:
#                                         what it built, under which licence, and
#                                         where that source is. It is appended to
#                                         SOURCES.txt below, never written over it.
#   $OUT/sources/<path with rife in it>   the Corresponding Source of what it
#                                         built, present in the output rather than
#                                         promised, as sources/mvgpu is.
#   $OUT/components-rife.json             install-record entries, merged below the
#                                         way the Windows build merges
#                                         components-tools.json. Its licence
#                                         expression is the spec's
#                                         (packaging/plugins/buttereye-vs-rife-ncnn.spec),
#                                         which is the authority for it.
rife_note="NOT BUILT: no packaging/macos/build-rife.sh (the phase-3 spike measured that RIFE builds and renders on arm64 - packaging/macos/RIFE-SPIKE.md - but nothing here builds it); macOS has no RIFE engine"
rife_offer=$out/licenses/SOURCES.rife.txt
if [[ -x $here/build-rife.sh ]]; then
    echo "==> RIFE-ncnn: packaging/macos/build-rife.sh"
    if OUT=$out WORK=$work JOBS=${jobs:-} "$here/build-rife.sh"; then
        rife_note="built by packaging/macos/build-rife.sh"
    else
        # A failed RIFE must not throw away a good mvgpu: it is one engine of
        # several, and ButterEye runs without it (MVTools and mvgpu remain).
        rife_note="FAILED: packaging/macos/build-rife.sh exited $?; see the output above"
        echo "warning: $rife_note" >&2
    fi
fi

# The duties above, enforced. This runs whether or not build-rife.sh exists: the
# question is not "did the hook run" but "is there a librife.dylib in the output",
# which is the file ButterEye would convey. A missing licence text or a missing
# source offer is a licence failure, not a warning, so it fails the build the way
# a bad dylib does - and unlike a bad dylib it cannot be fixed downstream.
if [[ -e $out/vapoursynth/librife.dylib ]]; then
    rife_owed=()
    [[ -s $rife_offer ]] \
        || rife_owed+=("licenses/SOURCES.rife.txt: its paragraph of the written offer")
    compgen -G "$out/licenses/buttereye-vs-rife-ncnn/*" >/dev/null \
        || rife_owed+=("licenses/buttereye-vs-rife-ncnn/: its licence texts")
    compgen -G "$out/sources/*rife*" >/dev/null \
        || rife_owed+=("sources/*rife*: the Corresponding Source of what it built")
    [[ -s $out/components-rife.json ]] \
        || rife_owed+=("components-rife.json: the install-record entry")
    if [[ -d $out/share/rife-ncnn-models ]] \
        && ! compgen -G "$out/licenses/buttereye-rife-ncnn-models/*" >/dev/null; then
        rife_owed+=("licenses/buttereye-rife-ncnn-models/: the models' own licences")
    fi
    if ((${#rife_owed[@]})); then
        echo "error: librife.dylib is here without what ButterEye must convey with it:" >&2
        printf '  - missing %s\n' "${rife_owed[@]}" >&2
        echo "see the RIFE hook contract in $0; refusing to ship a plugin whose" >&2
        echo "licences and source offer are not in the output" >&2
        # Removed rather than left lying in the output, for the reason the Windows
        # build gives when fftw's source package will not download: a file nobody
        # may convey must not be sitting where the next step picks it up. Only
        # that file goes - a good libmvgpu.dylib is not the plugin at fault - and
        # the build tree in $work still has everything the step produced.
        rm -f "$out/vapoursynth/librife.dylib"
        echo "removed $out/vapoursynth/librife.dylib; its build tree is in $work" >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------- the check
# The same check that gates the Windows DLLs, in macOS terms. Run here so a bad
# dylib never reaches SHA256SUMS, and usable on its own afterwards.
echo "==> checking $out/vapoursynth"
"$here/check-dylibs.sh" "$out/vapoursynth"

# ---------------------------------------------------------------- install-record entries
# checks_pkgs.manifest_db reads this shape (packaging/windows/winstage.py: record).
# The licence expression is the one packaging/plugins/buttereye-vs-mvgpu.spec
# declares, which is the authority for it; there is no rpm release part here.
python3 - "$out" "$mvgpu_ver" <<'EOF'
import json, os, sys

out, ver = sys.argv[1:3]
root = os.path.join(out, "licenses", "buttereye-vs-mvgpu")
components = [{
    "name": "buttereye-vs-mvgpu", "version": ver, "license": "AGPL-3.0-or-later",
    "provides": [],
    "licence_files": sorted(f"licenses/buttereye-vs-mvgpu/{f}" for f in os.listdir(root)),
}]
extra = os.path.join(out, "components-rife.json")  # the spike's step, if it ran
if os.path.exists(extra):
    with open(extra) as fh:
        components += json.load(fh)["components"]
    os.remove(extra)
with open(os.path.join(out, "components.json"), "w") as fh:
    json.dump({"version": 1, "components": components}, fh, indent=2)
    fh.write("\n")
EOF

# ---------------------------------------------------------------- Corresponding Source
# libmvgpu.dylib is AGPL-3.0-or-later and its Corresponding Source is this
# repository, so ship it with the build rather than promising it: mvgpu/ exactly
# as it was built, the repository's licence, and the two scripts that did the
# building. A directory, not a tar: macOS's bsdtar has no --sort, so a
# reproducible archive would need GNU tar, and SHA256SUMS already records every
# file one by one.
#
# Note the asymmetry with the Windows build, which also ships FFmpeg's, fftw's
# and ncnn's source: those are linked into DLLs that ButterEye conveys. Nothing
# here is linked in. libmvgpu.dylib loads Homebrew's Vulkan loader at run time,
# and MVTools and ffms2 are bottles the user installs - conveyed by Homebrew,
# which carries the source duty for them, not by ButterEye. ButterEye's own
# obligation covers what ButterEye builds and ships, which is what is in
# sources/ below.
src=$out/sources
mkdir -p "$src"
cp -R "$repo/mvgpu" "$src/mvgpu"
find "$src/mvgpu" -name '*.pyc' -delete
rm -rf "$src/mvgpu/build" "$src/mvgpu"/build-*
cp "$repo/COPYING" "$src/COPYING"
cp "$here/build-plugins.sh" "$here/check-dylibs.sh" "$src/"
if [[ -x $here/build-rife.sh ]]; then
    cp "$here/build-rife.sh" "$src/"
fi

# Only what was actually built may appear under "Built here": a line about a
# plugin this script did not produce, with a licence nobody checked, is the
# written offer claiming something the output does not contain.
cat >"$out/licenses/SOURCES.txt" <<EOF
ButterEye's macOS VapourSynth plugins: where the source code is
===============================================================

Built here (ButterEye conveys these, so this is ButterEye's source duty):

  vapoursynth/libmvgpu.dylib   mvgpu $mvgpu_ver, AGPL-3.0-or-later, ButterEye's
                               own code. Its complete Corresponding Source is
                               sources/mvgpu (the same files as mvgpu/ in the
                               ButterEye repository,
                               https://github.com/MiguelCarino/ButterEye),
                               with the licence in licenses/buttereye-vs-mvgpu.
EOF

# The RIFE step's own paragraph, appended rather than written over this file: the
# step that built librife.dylib is the only thing that knows what it linked, under
# which licence and where that source went, and the hook above requires it to say
# so. The fragment is then removed, the way components-rife.json is, so the offer
# exists once and SHA256SUMS does not record two copies of the same text.
if [[ -s $rife_offer ]]; then
    cat "$rife_offer" >>"$out/licenses/SOURCES.txt"
    rm -f "$rife_offer"
else
    # $rife_note is a console line; fold it into this file's second column
    # instead of letting one 200-character line through a wrapped document.
    rife_why=$(printf '%s\n' "$rife_note" | fold -s -w 48 \
        | sed -e 's/ *$//' -e 's/^/                               /' -e '1s/^ *//')
    cat >>"$out/licenses/SOURCES.txt" <<EOF

  vapoursynth/librife.dylib    NOT BUILT. ButterEye conveys no RIFE-ncnn plugin on
                               macOS, so there is no RIFE source duty to meet here
                               yet, and no licence claim to make. This run's
                               reason:
                               $rife_why

                               When something does build it, its licences are its
                               own and not ButterEye's:
                               packaging/plugins/buttereye-vs-rife-ncnn.spec is the
                               authority, and it says MIT AND LGPL-2.1-or-later AND
                               WTFPL against a system ncnn (HolyWu's plugin is MIT;
                               the bundled RIFE/VapourSynth4.h is LGPL-2.1-or-later
                               and RIFE/VSHelper4.h is WTFPL), with BSD-3-Clause,
                               BSD-2-Clause, Zlib, Apache-2.0 and GPL-3.0-or-later
                               WITH Bison-exception-2.2 added when ncnn and glslang
                               are bundled statically. The models are a separate
                               component, MIT, whose chain is in
                               packaging/plugins/PROVENANCE-rife-ncnn-models.md.
                               "MIT" alone is not the answer for either of them.
EOF
fi

cat >>"$out/licenses/SOURCES.txt" <<EOF

To rebuild: run packaging/macos/build-plugins.sh (a copy is in sources/) from a
checkout of that repository on an arm64 Mac with the Homebrew formulae its
header lists. The build downloads nothing.

Nothing here is signed or notarised. The only signature on libmvgpu.dylib is the
ad-hoc one Apple's linker applies (codesign: adhoc, linker-signed, no Team
identifier), which is what arm64 needs in order to load it and is not a statement
about who built it.

Not built here, and not conveyed by ButterEye on macOS:

  MVTools, ffms2             Homebrew bottles (vapoursynth-mvtools, ffms2). The
                             user installs them, so Homebrew is the distributor
                             and carries the source duty for them. ButterEye
                             detects them; it never ships them.
  Vulkan loader, MoltenVK    Homebrew bottles (vulkan-loader, molten-vk).
                             libmvgpu.dylib links the loader's libvulkan.1.dylib
                             and reaches the GPU through MoltenVK at run time,
                             as a separate library on the user's own machine.
  ncnn, libomp               Homebrew bottles (ncnn, libomp). Nothing built here
                             links them today; a RIFE build would link them as
                             separate libraries on the user's own machine, and
                             they would stay the user's install, not ButterEye's.
  vulkaninfo                 Homebrew's vulkan-tools. ButterEye runs it as a
                             separate program to list GPUs.
EOF

# ---------------------------------------------------------------- SHA256SUMS
# shasum -a 256, not sha256sum (macOS has no sha256sum); the line format is the
# same, so the file reads the same as the Windows build's.
(cd "$out" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256) \
    >"$out/SHA256SUMS"

# ---------------------------------------------------------------- what happened
echo
echo "macOS plugins in $out"
ls -l "$out/vapoursynth"
echo
echo "built:      libmvgpu.dylib (mvgpu $mvgpu_ver, AGPL-3.0-or-later)"
echo "RIFE-ncnn:  $rife_note"
echo "MVTools:    NOT BUILT - Homebrew's vapoursynth-mvtools bottle"
echo "            ($brew_prefix/lib/python*/site-packages/vapoursynth/plugins/mvtools.dylib)"
echo "ffms2:      NOT BUILT - Homebrew's ffms2 bottle (libffms2.dylib, same directory)"
echo "vulkaninfo: NOT BUILT - Homebrew's vulkan-tools bottle"
echo
echo "those four are the user's own Homebrew install, conveyed by Homebrew and not by"
echo "ButterEye; licenses/SOURCES.txt says so and says where this build's source is."
echo
echo "nothing here is signed or notarised: the only signature on libmvgpu.dylib is the"
echo "ad-hoc one the linker applies, which is what arm64 needs to load it. That is"
echo "enough for a cache directory that is not distributed; signing, notarisation and"
echo "the Homebrew tap are phase 4's (packaging/README.md)."
