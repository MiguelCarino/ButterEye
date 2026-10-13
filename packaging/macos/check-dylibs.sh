#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Check every VapourSynth plugin in a directory for arm64 macOS (docs/PLATFORMS.md,
# the macOS phase table, phase 3). This is the macOS peer of the import check the
# Windows build makes on each DLL (packaging/windows/build-in-chroot.sh:
# check_dll, which refuses any Microsoft DLL but msvcrt and KERNEL32): the one
# question both ask is "would this file still work on a machine that is not the
# build machine?".
#
# For each *.dylib (and *.so, which a hand-rolled build may leave behind):
#   1. arm64 Mach-O, per lipo -archs and otool -hv. The Mach-O file type is
#      reported too: meson's shared_module gives a BUNDLE (MH_BUNDLE), not a
#      DYLIB, and that is fine - VapourSynth dlopens its plugins and dlopen
#      takes either. It would only matter to something that linked the plugin
#      at build time, which nothing does.
#   2. exports _VapourSynthPluginInit2 (nm -gU), the API4 entry point. Without
#      it VapourSynth rejects the file; with a stripped build it is still there,
#      because it is an exported symbol rather than debug information.
#   3. links nothing a user would not have. Allowed:
#        /usr/lib/... and /System/Library/Frameworks/...  - macOS itself, in the
#          dyld shared cache on every Mac of this version.
#        <brew prefix>/opt/<formula>/...  - but only for the formulae below,
#          which are the formulae a macOS build may link, and not a list of
#          declared dependencies: of the six, only vulkan-loader is declared and
#          checked today (checks_pkgs.BREW_FORMULAE, read off the Cellar by
#          brew.py). The list below marks each one, and says what phase 4 owes.
#          The path is the versionless symlink Homebrew keeps pointing at the
#          current version, so the plugin survives a brew upgrade.
#        <brew prefix>/lib/<file>  - resolved through its symlink to the Cellar
#          to find which formula it belongs to, then judged as above.
#      Refused, with the reason named:
#        a Cellar path (<brew prefix>/Cellar/<formula>/<version>/...), because
#          the version is baked in and the next brew upgrade breaks the plugin;
#        @rpath, @loader_path or @executable_path, because they resolve against
#          LC_RPATH entries or the loading binary, and a VapourSynth plugin is
#          dlopened by whatever mpv or vspipe happens to be;
#        any LC_RPATH at all: nothing built here needs one, and the one that
#          turns up in practice is meson's build-tree rpath into the Cellar
#          (`meson install` strips it, copying out of the build tree does not);
#        anything else absolute, /usr/local included - that is an Intel-Mac
#          Homebrew prefix or a stray local build, not anything a plugin of ours
#          may link.
#
# What it does NOT check: the code signature. Everything built here carries only
# the ad-hoc signature Apple's linker applies - measured on libmvgpu.dylib,
# `codesign -dvvv` reports flags=0x20002(adhoc,linker-signed), Signature=adhoc
# and TeamIdentifier=not set - which is what arm64 needs in order to load at all
# and says nothing about who built the file. Nothing here is signed or notarised;
# see build-plugins.sh's header for why a phase-3 build does not need to be.
#
# Usage: packaging/macos/check-dylibs.sh <directory> [<directory>...]
#   packaging/macos/build-plugins.sh runs it on its own output; it is also a
#   standalone check of an installed plugin directory, Homebrew's own included
#   (there every plugin is a symlink into the Cellar, which is followed).
#   The allowlist is what a *ButterEye* plugin may link, which is narrower than
#   what a bottle may link, so a bottle that links more is refused for that:
#   Homebrew's libffms2.dylib links its own ffmpeg, which ButterEye only ever
#   runs as a separate program and never links. That refusal is a true answer to
#   "would ButterEye ship this?", not a fault in the bottle, and
#   BUTTEREYE_EXTRA_FORMULAE='ffmpeg ...' widens the allowlist for such a run.
# Exit 0 if every plugin passed, 1 otherwise, naming the file and the library
# that offended. An empty directory is a failure, not a pass: it almost always
# means the build wrote somewhere else.
set -euo pipefail

if (($# == 0)); then
    echo "usage: $(basename "$0") <directory> [<directory>...]" >&2
    exit 2
fi

brew_prefix=$(brew --prefix 2>/dev/null || echo /opt/homebrew)

# Homebrew formulae a macOS build may link. This is NOT the list of formulae
# ButterEye declares: only vulkan-loader is declared today, and it is the only
# one the doctor checks for (it is in checks_pkgs.BREW_FORMULAE, which brew.py
# looks up in the Cellar; nothing in buttereye/ asks about ncnn, libomp, glslang,
# spirv-tools or fftw). Phase 4's tap has to declare the rest, and the doctor has
# to check them, before ButterEye ships anything that links them - the gap has a
# concrete cost rather than a theoretical one: phase 4 is expected to build RIFE
# against Homebrew's ncnn with the libomp link flags RIFE-SPIKE.md measured, so
# librife.dylib would link <prefix>/opt/ncnn/lib/libncnn.1.dylib and
# <prefix>/opt/libomp/lib/libomp.dylib - both allowed here, neither declared
# anywhere - and a user without those formulae would get a plugin that cannot
# load, past a gate that had passed it.
# The list is not tightened here on purpose. It passes everything built today
# (libmvgpu.dylib links the loader and nothing else), and which formulae
# ButterEye declares is phase 4's decision to make with its tap in hand, not this
# gate's to pre-empt; what this comment must not do is call the list something it
# is not.
#   vulkan-loader  the Vulkan entry point. There is no native Vulkan on macOS,
#                  so libmvgpu.dylib links the loader and the loader finds
#                  MoltenVK as a portability driver. DECLARED and checked.
#   ncnn           RIFE's inference library; Homebrew's bottle is built against
#                  molten-vk, glslang and spirv-tools. NOT DECLARED.
#   libomp         clang's OpenMP runtime. ncnn's bottle links it, and a RIFE
#                  build has to link it itself (RIFE-SPIKE.md, "The OpenMP time
#                  bomb"). NOT DECLARED.
#   glslang        shader compilation at run time, inside ncnn. NOT DECLARED.
#   spirv-tools    glslang's SPIR-V tools, same path. NOT DECLARED.
#   fftw           MVTools' FFT. Nothing built here links it, but Homebrew's own
#                  mvtools.dylib does (measured: it links
#                  <prefix>/opt/fftw/lib/libfftw3f.3.dylib and nothing else
#                  outside /usr/lib), and the header invites running this script
#                  over Homebrew's plugin directory. NOT DECLARED.
# Deliberately not allowed: molten-vk (a plugin should reach it through the
# loader's ICD, not link it) and vapoursynth itself (plugins use its C API and
# link nothing - the Windows build does not even give the cross toolchain an
# import library). molten-vk shows that this list runs in neither direction:
# ButterEye does declare and check it (checks_pkgs.BREW_FORMULAE,
# checks_vulkan.py) and a plugin still may not link it.
# BUTTEREYE_EXTRA_FORMULAE widens this for a standalone check of a plugin that is
# not ButterEye's (see the header); the build step passes nothing and gets the
# list as it stands here.
# shellcheck disable=SC2206  # word splitting is the point: it is a plain list
allowed_formulae=(vulkan-loader ncnn libomp glslang spirv-tools fftw
                  ${BUTTEREYE_EXTRA_FORMULAE:-})

is_allowed_formula() {
    local want=$1 f
    for f in "${allowed_formulae[@]}"; do
        [[ $f == "$want" ]] && return 0
    done
    return 1
}

# judge <dependency path>: print nothing and return 0 if a user's Mac has it,
# otherwise print why not.
judge() {
    local dep=$1 formula target version
    case $dep in
        /usr/lib/*|/System/Library/Frameworks/*)
            return 0 ;;
        @*)
            echo "$dep: resolved against the loading binary, which for a plugin is any mpv or vspipe"
            return 1 ;;
        "$brew_prefix"/Cellar/*)
            formula=${dep#"$brew_prefix"/Cellar/}
            version=${formula#*/}
            formula=${formula%%/*}
            echo "${dep}: a versioned Cellar path ($formula ${version%%/*}), which the next"\
                 "brew upgrade breaks; link \$prefix/opt/$formula instead"
            return 1 ;;
        "$brew_prefix"/opt/*)
            formula=${dep#"$brew_prefix"/opt/}
            formula=${formula%%/*}
            is_allowed_formula "$formula" && return 0
            echo "$dep: Homebrew formula '$formula' is not one a ButterEye plugin may link"
            return 1 ;;
        "$brew_prefix"/lib/*)
            # Homebrew's own symlink farm: follow it to the Cellar for the name
            target=$(readlink "$dep" 2>/dev/null || true)
            if [[ $target == *Cellar/* ]]; then
                formula=${target#*Cellar/}
                formula=${formula%%/*}
                is_allowed_formula "$formula" && return 0
                echo "$dep: Homebrew formula '$formula' is not one a ButterEye plugin may link"
            else
                echo "$dep: under the Homebrew prefix but not a formula's symlink"\
                     "(it is $([[ -e $dep ]] && echo "a plain file" || echo missing))"
            fi
            return 1 ;;
        *)
            echo "$dep: not macOS's own, and not a Homebrew formula a ButterEye plugin may link"
            return 1 ;;
    esac
}

check() {  # check <file>: 0 if it passed
    local f=$1 archs header filetype id deps dep why bad=0 rpaths
    echo "-- $f"

    if ! header=$(otool -hv "$f" 2>/dev/null) || ! grep -q MH_MAGIC <<<"$header"; then
        echo "   not a Mach-O file ($(file -b "$f"))"
        return 1
    fi
    filetype=$(awk 'NR==4 {print $5}' <<<"$header")
    archs=$(lipo -archs "$f")
    echo "   $filetype, $(file -b "$f")"
    if ! grep -qw arm64 <<<"$archs"; then
        echo "   not arm64: lipo -archs says '$archs'"
        bad=1
    elif [[ $archs != arm64 ]]; then
        # A universal binary is not wrong, only more than we build; say so.
        echo "   note: more than arm64 in this file ('$archs')"
    fi

    if nm -gU "$f" 2>/dev/null | grep -q ' _VapourSynthPluginInit2$'; then
        echo "   exports _VapourSynthPluginInit2"
    else
        echo "   does not export _VapourSynthPluginInit2: VapourSynth will refuse it"
        bad=1
    fi

    # otool -L repeats the file's own install name first (for a dylib; a bundle
    # has none), so drop whatever otool -D reports as the id.
    id=$(otool -D "$f" | sed -n '2p')
    deps=$(otool -L "$f" | sed -n 's/^\t\([^ ]*\).*/\1/p')
    while read -r dep; do
        [[ -z $dep || $dep == "$id" ]] && continue
        if why=$(judge "$dep"); then
            echo "   links $dep"
        else
            echo "   REFUSED $why"
            bad=1
        fi
    done <<<"$deps"

    rpaths=$(otool -l "$f" | awk '/^ *cmd LC_RPATH/ {want=1} want && /^ *path / {print $2; want=0}')
    if [[ -n $rpaths ]]; then
        while read -r dep; do
            echo "   REFUSED LC_RPATH $dep: nothing built here needs an rpath"\
                 "(install with 'meson install', which strips the build-tree one)"
        done <<<"$rpaths"
        bad=1
    fi
    return "$bad"
}

failed=()
found=0
for dir in "$@"; do
    if [[ ! -d $dir ]]; then
        echo "error: $dir is not a directory" >&2
        exit 1
    fi
    echo "== $dir"
    while read -r f; do
        [[ -z $f ]] && continue
        found=$((found + 1))
        check "$f" || failed+=("$f")
    # -type l as well as -type f: an installed plugin directory is Homebrew's
    # symlink farm, where every plugin is a link into the Cellar. otool, nm and
    # lipo all follow a symlink, so they judge the file it points at.
    done < <(find "$dir" \( -type f -o -type l \) \( -name '*.dylib' -o -name '*.so' \) | LC_ALL=C sort)
done

echo
if ((found == 0)); then
    echo "error: no .dylib (or .so) in $*; a build that produced nothing is not a pass" >&2
    exit 1
fi
if ((${#failed[@]})); then
    echo "error: $((${#failed[@]})) of $found plugins would not work on a user's Mac:" >&2
    printf '  - %s\n' "${failed[@]}" >&2
    exit 1
fi
echo "$found plugin(s) checked: arm64, the VapourSynth entry point, and nothing linked that a Mac lacks"
