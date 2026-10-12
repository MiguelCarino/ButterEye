#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build ButterEye's Windows installer (docs/PLATFORMS.md, phase 4) on the Fedora
# dev box. Self-contained: no Python, VapourSynth or anything else has to be on
# the Windows PC first (Microsoft's Visual C++ runtime excepted, see below).
#
#   1. the plugins, ffms2.dll and vulkaninfo.exe from build-plugins.sh (run if missing)
#   2. the pinned runtime (pins.sh, SHA-256-checked): Python's embeddable build,
#      and the PySide6-Essentials, Shiboken6 and VapourSynth wheels for Windows;
#      Qt's qtbase and qtsvg sources, for the licence notices of the third-party
#      code inside the Qt binaries
#   3. the install folder, staged:
#        python\                     Python + site-packages (PySide6 trimmed to the
#                                    Qt modules and plugins ButterEye uses, without
#                                    the wheels' Microsoft C++ runtime; VapourSynth)
#        buttereye\, buttereye-<v>.dist-info\   ButterEye from this source tree
#        vapoursynth\*.dll           the plugins and ffms2.dll
#        share\rife-ncnn-models\     the RIFE models
#        vulkaninfo.exe, buttereye.cmd (the command line), buttereye.ico
#        licenses\<component>\       every licence text, and SOURCES.txt
#        buttereye-install.json      the install record (checks_pkgs.manifest_db)
#   4. the icon (from buttereye/gui/icons/buttereye.svg) and the installer
#      (buttereye.nsi), made with Fedora's rsvg-convert, icotool and makensis in
#      the same mock chroot as the plugins
#
# Output in $XDG_CACHE_HOME/buttereye/windows (never the repository):
#   ButterEye-<version>-setup.exe             the installer
#   ButterEye-<version>-windows-sources.tar   Corresponding Source of what was built
#   SHA256SUMS
# The stage stays in $XDG_CACHE_HOME/buttereye/windows-installer/payload.
#
# VCREDIST=<vc_redist.x64.exe> embeds Microsoft's runtime installer instead of
# downloading it at install time; only if you may redistribute it (packaging/README.md).
# Test the result under Wine: tools/wine-tests.sh (BUTTEREYE_TEST_INSTALLER).
#
# Usage: packaging/windows/build-installer.sh [--no-mock]
#   --no-mock: build right here, as root in a fedora:44 container (CI), instead
#   of in a mock chroot (chroot.sh). The steps are the same.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
win=$cache/windows
work=$cache/windows-installer
# shellcheck source=pins.sh
source "$here/pins.sh"
# shellcheck source=chroot.sh
source "$here/chroot.sh"
win_mode_from_args "$@"
# what this script itself runs outside the chroot (--no-mock: install them here)
win_host_install git-core curl python3 python3-pip python3-setuptools tar
stagepy() { python3 -I "$here/winstage.py" "$@"; }

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/buttereye/__init__.py")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "unexpected version '$version'" >&2; exit 1; }
pyabi=$(cut -d. -f1-2 <<<"$PYTHON_VER" | tr -d .)       # 3.14.8 -> 314
pyver=$(cut -d. -f1-2 <<<"$PYTHON_VER")                  # 3.14
setup=ButterEye-$version-setup.exe
srctar=ButterEye-$version-windows-sources.tar
epoch=$(git -C "$repo" log -1 --format=%ct 2>/dev/null || date +%s)

# ---------------------------------------------------------------- 1. native parts
if [[ ! -f $win/components.json || ! -f $win/vapoursynth/ffms2.dll || ! -f $win/vulkaninfo.exe ]]; then
    "$here/build-plugins.sh"
fi
(cd "$win" && sha256sum --quiet --strict --ignore-missing -c SHA256SUMS) \
    || { echo "$win doesn't match its SHA256SUMS; run build-plugins.sh again" >&2; exit 1; }
# the Corresponding Source of mvtools.dll's fftw, and the models' archive
for need in 'sources/mingw-fftw-*.src.rpm' 'sources/buttereye-rife-ncnn-models-r*.tar.gz'; do
    compgen -G "$win/$need" >/dev/null \
        || { echo "$win/$need is missing; run build-plugins.sh again" >&2; exit 1; }
done

# ---------------------------------------------------------------- 2. the runtime
py_zip=$(fetch "$PYTHON_URL" "$PYTHON_SHA256")
wheels=(
    "$(fetch "$PYSIDE_URL" "$PYSIDE_SHA256")"
    "$(fetch "$SHIBOKEN_URL" "$SHIBOKEN_SHA256")"
    "$(fetch "$VAPOURSYNTH_URL" "$VAPOURSYNTH_SHA256")"
)
qt_srcs=("$(fetch "$QTBASE_URL" "$QTBASE_SHA256")" "$(fetch "$QTSVG_URL" "$QTSVG_SHA256")")

# ---------------------------------------------------------------- 3. the install folder
rm -rf "$work"
stage=$work/payload
site=$stage/python/Lib/site-packages
mkdir -p "$stage/python" "$work/src"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
    "$py_zip" "$stage/python"
# The embeddable Python's search path: its stdlib, site-packages and the install
# folder itself (where the buttereye package sits, so paths.install_dir() is it).
# A ._pth file also makes Python ignore PYTHONPATH/PYTHONHOME (isolated).
printf '%s\r\n' "python$pyabi.zip" . 'Lib\site-packages' .. >"$stage/python/python$pyabi._pth"
python3 -m pip install -q --disable-pip-version-check --no-deps --no-index --no-compile \
    --only-binary=:all: --platform win_amd64 --python-version "$pyver" --implementation cp \
    --target "$site" "${wheels[@]}"
stagepy trim "$site"

# ButterEye itself: the files git knows (committed or not), as a wheel, unpacked
# (the sources tar gets packaging/ and mvgpu/ too, so it can rebuild the plugins)
app_src=$work/src/buttereye-$version
mkdir -p "$app_src"
git -C "$repo" ls-files -z --cached --others --exclude-standard -- \
        buttereye pyproject.toml README.md COPYING LICENSES MANIFEST.in CHANGELOG.md \
        THIRD-PARTY.md packaging mvgpu |
    (cd "$repo" && while IFS= read -r -d '' f; do [[ -f $f ]] && printf '%s\0' "$f"; done) |
    (cd "$repo" && tar --null -T - -cf -) | tar -xf - -C "$app_src"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
    -C "$work/src" -cf "$work/app-src.tar" "buttereye-$version"  # before the build adds files
python3 -m pip wheel -q --disable-pip-version-check --no-deps --no-build-isolation --no-index \
    -w "$work/wheel" "$app_src"
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
    "$work/wheel/buttereye-$version-py3-none-any.whl" "$stage"

# the native parts
mkdir -p "$stage/vapoursynth" "$stage/share"
cp "$win"/vapoursynth/*.dll "$stage/vapoursynth/"
cp -r "$win/share/rife-ncnn-models" "$stage/share/"
cp "$win/vulkaninfo.exe" "$stage/"
cp -r "$win/licenses" "$stage/licenses"

# licences of ButterEye and of the runtime
lic=$stage/licenses
mkdir -p "$lic/buttereye/LICENSES" "$lic/python" "$lic/pyside6" "$lic/vapoursynth"
cp "$repo/COPYING" "$repo/THIRD-PARTY.md" "$lic/buttereye/"
cp "$repo"/LICENSES/*.txt "$lic/buttereye/LICENSES/"
cp "$stage/python/LICENSE.txt" "$lic/python/LICENSE.txt"
# Python's own copies of Microsoft's C++ runtime, the only Microsoft DLLs shipped
# (check-imports below makes sure): part of the embeddable build, redistributed
# under the conditions in its LICENSE.txt
vcver=$(stagepy vc-note "$lic/python/MICROSOFT-VCRUNTIME.txt" "$PYTHON_VER" "$stage/python/vcruntime140.dll")
# the end-user terms the installer has users accept for those files (buttereye.nsi)
cp "$here/MICROSOFT-TERMS.txt" "$lic/python/MICROSOFT-TERMS.txt"
# Qt's licences (the wheels carry only Qt's commercial licence; ButterEye conveys Qt,
# PySide6 and Shiboken6 under the LGPL-3.0, with the GPL-3.0 it builds on) and the
# notices of the third-party code inside the shipped Qt binaries, from Qt's sources
mapfile -t qt_out < <(stagepy qt-notices "$lic/pyside6" "${qt_srcs[@]}")
[[ ${#qt_out[@]} -eq 2 ]] || { echo "qt-notices failed" >&2; exit 1; }
qt_files=$(cd "$stage" && find licenses/pyside6 -type f | LC_ALL=C sort | paste -sd,)
pyside_lic=python/Lib/site-packages/pyside6_essentials-$PYSIDE_VER.dist-info/licenses
shiboken_lic=python/Lib/site-packages/shiboken6-$PYSIDE_VER.dist-info/licenses
cp "$site/vapoursynth-$VAPOURSYNTH_VER.dist-info/licenses/COPYING.LESSER" "$lic/vapoursynth/"
zlib_ver=$(python3 -I -c 'import json, sys
print(next(v for c in json.load(open(sys.argv[1]))["components"]
           for n, v in c["provides"] if n == "bundled(zlib)"))' "$win/components.json")
stagepy sources-txt "$lic/SOURCES.txt" version="$version" repo=https://github.com/MiguelCarino/ButterEye \
    ffms2="$FFMS2_VER" ffmpeg="$FFMPEG_VER" dav1d="$DAV1D_VER" vulkan_tools="$VULKAN_TOOLS_VER" \
    python="$PYTHON_VER" pyside="$PYSIDE_VER" vapoursynth="$VAPOURSYNTH_VER" zlib="$zlib_ver" \
    fftw_srpm="$(cd "$win/sources" && echo mingw-fftw-*.src.rpm)" \
    models="$(cd "$win/sources" && echo buttereye-rife-ncnn-models-r*.tar.gz)"

stagepy record "$win/components.json" "$stage/buttereye-install.json" \
    "buttereye=$version=AGPL-3.0-or-later AND LGPL-3.0-or-later=licenses/buttereye/COPYING,licenses/buttereye/LICENSES/AdditionRef-ButterEye-generated-output.txt,buttereye/data/shaders/LGPL-3.0.txt,buttereye/data/shaders/GPL-3.0.txt" \
    "python3=$PYTHON_VER=PSF-2.0 AND 0BSD AND bzip2-1.0.6 AND MIT AND BSD-2-Clause AND BSD-3-Clause AND Apache-2.0 AND Zlib AND Unlicense AND blessing=licenses/python/LICENSE.txt" \
    "microsoft-vcruntime=$vcver=LicenseRef-Microsoft-Distributable-Code=licenses/python/LICENSE.txt,licenses/python/MICROSOFT-VCRUNTIME.txt,licenses/python/MICROSOFT-TERMS.txt" \
    "python3-pyside6=$PYSIDE_VER=LGPL-3.0-only AND ${qt_out[0]}=$qt_files,$pyside_lic/LicenseRef-Qt-Commercial.txt=bundled(qt6)($PYSIDE_VER),${qt_out[1]}" \
    "python3-shiboken6=$PYSIDE_VER=LGPL-3.0-only=licenses/pyside6/LGPL-3.0-only.txt,licenses/pyside6/GPL-3.0-only.txt,$shiboken_lic/LicenseRef-Qt-Commercial.txt" \
    "vapoursynth-libs=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER" \
    "vapoursynth-tools=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER"

# the command line, next to the install folder's python
printf '%s\r\n' '@echo off' \
    'rem SPDX-License-Identifier: AGPL-3.0-or-later' \
    'rem ButterEye on the command line: buttereye doctor, buttereye play VIDEO, buttereye --help' \
    '"%~dp0python\python.exe" -m buttereye.cli %*' \
    'exit /b %ERRORLEVEL%' >"$stage/buttereye.cmd"

stagepy check-imports "$stage"
find "$stage" -name __pycache__ -prune -exec rm -rf {} +
echo "install folder: $(du -sm "$stage" | cut -f1) MiB in $(find "$stage" -type f | wc -l) files"

# ---------------------------------------------------------------- 4. icon and installer
nsis=$work/nsis
mkdir -p "$nsis/out"
cp "$here/buttereye.nsi" "$repo/buttereye/gui/icons/buttereye.svg" "$nsis/"
sed 's/$/\r/' "$repo/COPYING" >"$nsis/COPYING.txt"
sed 's/$/\r/' "$here/MICROSOFT-TERMS.txt" >"$nsis/MICROSOFT-TERMS.txt"
defs=(-DVERSION="$version" -DPAYLOAD=/builddir/nsis/payload -DICON=/builddir/nsis/buttereye.ico
      -DLICENSE_TXT=/builddir/nsis/COPYING.txt -DMSTERMS_TXT=/builddir/nsis/MICROSOFT-TERMS.txt
      -DOUTFILE="/builddir/nsis/out/$setup")
if [[ -n ${VCREDIST:-} ]]; then
    cp "$VCREDIST" "$nsis/vc_redist.x64.exe"
    defs+=(-DVCREDIST=/builddir/nsis/vc_redist.x64.exe)
fi
mv "$stage" "$nsis/payload"
win_init
win_install mingw32-nsis librsvg2-tools icoutils
win_run 'rm -rf /builddir/nsis'
win_copyin "$nsis" /builddir/nsis
win_run "set -e; cd /builddir/nsis
    for s in 16 24 32 48 64 128 256; do rsvg-convert -w \$s -h \$s buttereye.svg -o icon-\$s.png; done
    icotool -c -o buttereye.ico icon-16.png icon-24.png icon-32.png icon-48.png icon-64.png icon-128.png -r icon-256.png
    cp buttereye.ico payload/buttereye.ico && cp buttereye.ico out/
    makensis -V2 -NOCD $(printf "'%s' " "${defs[@]}") buttereye.nsi"
rm -rf "$work/out"
win_copyout /builddir/nsis/out "$work/out"
win_run 'rm -rf /builddir/nsis'
mv "$nsis/payload" "$stage"
cp "$work/out/buttereye.ico" "$stage/"
cp "$work/out/$setup" "$win/$setup"

# ---------------------------------------------------------------- 5. Corresponding Source
src=$work/srctar/ButterEye-$version-windows-sources
mkdir -p "$src"
tar -xf "$work/app-src.tar" -C "$src"
cp -r "$win/sources" "$src/sources"
cp "$lic/SOURCES.txt" "$src/"
tar --sort=name --owner=0 --group=0 --numeric-owner --mtime="@$epoch" \
    -C "$work/srctar" -cf "$win/$srctar" "ButterEye-$version-windows-sources"

(cd "$win" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) >"$win/SHA256SUMS"
echo
ls -l "$win/$setup" "$win/$srctar"
