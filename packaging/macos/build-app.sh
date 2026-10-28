#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Build ButterEye.app and its disk image for Apple Silicon (docs/PLATFORMS.md,
# "The .app"). The macOS peer of packaging/windows/build-installer.sh, and
# self-contained in the same sense: the Mac it is opened on needs nothing else -
# no Homebrew, no Python, no VapourSynth.
#
#   1. ButterEye's own plugin builds, from build-plugins.sh (run if missing):
#      libmvgpu.dylib, librife.dylib and the RIFE models
#   2. the pinned downloads (pins.sh, SHA-256-checked): python-build-standalone's
#      CPython, the PySide6-Essentials, Shiboken6 and VapourSynth wheels, and the
#      Qt, PySide6 and VapourSynth source archives for the sources tar
#   3. the bundle, staged:
#        Contents/MacOS/ButterEye     launcher.c: the window, or the command line
#                                     when given arguments (also as .../buttereye)
#        Contents/Resources/
#          python/                    CPython + site-packages (PySide6 thinned to
#                                     arm64 and trimmed, VapourSynth, ButterEye)
#          bin/                       mpv, ffmpeg, ffprobe, vulkaninfo (and
#                                     mkvmerge with BUTTEREYE_BUNDLE_MKVMERGE=1)
#          lib/                       every library those load, MoltenVK and the
#                                     Vulkan loader: Homebrew's bottles, relocated
#          vapoursynth/               the plugins: mvtools, ffms2, mvgpu, rife, and
#                                     znedi3 + nnedi3_weights.bin when built
#          share/rife-ncnn-models/, share/vulkan/icd.d/MoltenVK_icd.json
#          licenses/<part>/, licenses/SOURCES.txt, buttereye-install.json
#   4. relocation: every Homebrew Mach-O gets @rpath load commands, and
#      appstage.py check refuses the bundle if anything still names a file
#      outside it but macOS's own
#   5. Info.plist (with the video types Finder may offer it for), the icon (from
#      buttereye/gui/icons/buttereye.svg, drawn by the bundled Qt), local symbols
#      stripped from every Mach-O, an ad-hoc signature over all of it
#   6. the disk image and the Corresponding Source tar
#
# What it takes from Homebrew, and why that is not a dependency of the result:
# mpv is the only mpv for macOS with the vapoursynth filter compiled in, so the
# app carries Homebrew's bottle - with FFmpeg, ffms2, MVTools, MoltenVK and the
# libraries they link - copied out of the Cellar and relocated. Homebrew is a
# build input here, like Fedora's mock is for the Windows build; the formulae
# below must be installed on the build Mac:
#   mpv ffmpeg ffms2 vapoursynth-mvtools molten-vk vulkan-loader vulkan-tools
#   (and ncnn, libomp, glslang for build-plugins.sh; mkvtoolnix for
#   BUTTEREYE_BUNDLE_MKVMERGE=1)
#
# mkvmerge is carried only with BUTTEREYE_BUNDLE_MKVMERGE=1. Without it the offline render remuxes with
# ffmpeg, which fails on MP4 mov_text subtitles and writes no container-level
# HDR10 mastering metadata (review B2, B21); a Finder-started app never sees a
# Homebrew mkvmerge, since its PATH has no /opt/homebrew/bin. Measured on
# mkvtoolnix 103.0: 25 files from 19 kegs, 61 MiB, of which ICU's data library
# is 32 MiB - the bottle links Homebrew's QtCore, and QtCore links ICU. The
# relocated copy ran with no file loaded from /opt/homebrew (DYLD_PRINT_LIBRARIES).
# Its licences (GPL-2.0-or-later, Qt's LGPL) and sources go the same way as
# every other formula's: licences, copyleft and the sources tar below.
# It is opt-in because 61 MiB is a quarter of the whole app, and the render no
# longer needs it: mov_text is converted to SRT for the ffmpeg remux and a
# failed remux keeps the encode (core/render/pipeline.py). A Homebrew mkvmerge
# is still used when one is installed. Measured with mkvtoolnix 103.0, boost's
# keg has no licence text, so appstage.py licences refuses the opt-in build.
#
# Signing: ad hoc (`codesign -s -`), because there is no Apple Developer ID
# (an owner decision, docs/PLATFORMS.md). arm64 code must carry a signature to
# run at all; an ad-hoc one satisfies that and identifies nobody. A copy that
# arrived through a browser is quarantined and Gatekeeper refuses it until the
# user says otherwise - README.md's macOS section has the exact steps.
#
# Output in $XDG_CACHE_HOME/buttereye/macos (never the repository):
#   ButterEye-<version>-macos-arm64.dmg      the disk image (ButterEye.app and a
#                                            link to /Applications)
#   ButterEye-<version>-macos-sources.tar    Corresponding Source
#   SHA256SUMS
# The stage stays in $XDG_CACHE_HOME/buttereye/macos-app/stage.
#
# Test the result: BUTTEREYE_TEST_APP=<stage>/ButterEye.app tools/macos-tests.sh
#
# Usage: packaging/macos/build-app.sh [--no-sources]
#   --no-sources: skip the sources tar (and the brew fetch it needs); for a
#   quick local rebuild, never for a release
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
cache=${XDG_CACHE_HOME:-$HOME/.cache}/buttereye
mac=$cache/macos
work=$cache/macos-app
# shellcheck source=pins.sh
source "$here/pins.sh"

sources=1
for arg in "$@"; do
    case $arg in
        --no-sources) sources=0 ;;
        *) echo "usage: $(basename "$0") [--no-sources]" >&2; exit 2 ;;
    esac
done

[[ $(uname -s) == Darwin && $(uname -m) == arm64 ]] \
    || { echo "build-app.sh builds on an Apple Silicon Mac" >&2; exit 1; }
for tool in brew clang install_name_tool codesign hdiutil iconutil lipo otool strip git shasum; do
    command -v "$tool" >/dev/null || { echo "$tool is missing" >&2; exit 1; }
done
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1
prefix=$(brew --prefix)
bundle_mkvmerge=${BUTTEREYE_BUNDLE_MKVMERGE:-0}
formulae=(mpv ffmpeg ffms2 vapoursynth-mvtools molten-vk vulkan-loader vulkan-tools)
if [[ $bundle_mkvmerge == 1 ]]; then formulae+=(mkvtoolnix); fi
for f in "${formulae[@]}"; do
    [[ -d $prefix/opt/$f ]] || { echo "brew install $f first (a build input)" >&2; exit 1; }
done

version=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$repo/buttereye/__init__.py")
[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "unexpected version '$version'" >&2; exit 1; }
pymm=${PYTHON_VER%.*}                                    # 3.14.8 -> 3.14
dmg=ButterEye-$version-macos-arm64.dmg
srctar=ButterEye-$version-macos-sources.tar
epoch=$(git -C "$repo" log -1 --format=%ct 2>/dev/null || date +%s)
stamp=$(date -u -r "$epoch" +%Y%m%d%H%M.%S)

# ---------------------------------------------------------------- 1. ButterEye's plugins
if [[ ! -f $mac/components.json || ! -f $mac/vapoursynth/librife.dylib \
      || ! -f $mac/vapoursynth/libmvgpu.dylib || ! -d $mac/share/rife-ncnn-models ]]; then
    "$here/build-plugins.sh"
fi
(cd "$mac" && shasum -a 256 --quiet --strict --ignore-missing -c SHA256SUMS) \
    || { echo "$mac doesn't match its SHA256SUMS; run build-plugins.sh again" >&2; exit 1; }

# ---------------------------------------------------------------- 2. downloads
py_tgz=$(fetch "$PYTHON_URL" "$PYTHON_SHA256")
py_full=$(fetch "$PYTHON_FULL_URL" "$PYTHON_FULL_SHA256")
wheels=(
    "$(fetch "$PYSIDE_URL" "$PYSIDE_SHA256")"
    "$(fetch "$SHIBOKEN_URL" "$SHIBOKEN_SHA256")"
    "$(fetch "$VAPOURSYNTH_URL" "$VAPOURSYNTH_SHA256")"
)
setuptools_whl=$(fetch "$SETUPTOOLS_URL" "$SETUPTOOLS_SHA256")
qt_srcs=("$(fetch "$QTBASE_URL" "$QTBASE_SHA256")" "$(fetch "$QTSVG_URL" "$QTSVG_SHA256")")
pyside_src=$(fetch "$PYSIDE_SRC_URL" "$PYSIDE_SRC_SHA256")
vs_src=$(fetch "$VAPOURSYNTH_SRC_URL" "$VAPOURSYNTH_SRC_SHA256")

# ---------------------------------------------------------------- 3. the bundle
[[ -n $work && $work == */buttereye/macos-app ]] || { echo "bad work dir $work" >&2; exit 1; }
rm -rf "$work"
app=$work/stage/ButterEye.app
contents=$app/Contents
res=$contents/Resources
mkdir -p "$contents/MacOS" "$res" "$work/tools" "$work/src"

# Python, and a second copy of it outside the bundle that runs the build's own
# helpers: nothing the build does may write into the bundle by accident
tar -xzf "$py_tgz" -C "$res"
tar -xzf "$py_tgz" -C "$work/tools"
tar -xf "$py_full" -C "$work/tools" python/licenses python/PYTHON.json
hostpy=$work/tools/python/bin/python$pymm
stagepy() { "$hostpy" -I -B "$here/appstage.py" "$@"; }
py=$res/python/bin/python$pymm
site=$res/python/lib/python$pymm/site-packages
pystd=$res/python/lib/python$pymm

# the launcher, linked against the bundled libpython (built while the headers
# are still in the stage)
clang -arch arm64 -mmacosx-version-min=13.0 -O2 -Wall -Wextra -Werror \
    -DPY_EXE="\"python$pymm\"" -I"$res/python/include/python$pymm" \
    "$here/launcher.c" -L"$res/python/lib" -lpython$pymm \
    -Wl,-rpath,@executable_path/../Resources/python/lib \
    -o "$contents/MacOS/ButterEye"

# what ButterEye never runs: Tcl/Tk and tkinter, IDLE, pip, the test suite, the
# headers and man pages, and the interpreter's other names (sys.executable is
# python3.14 by its full name; VSScript is handed the same path)
rm -rf "$res/python/include" "$res/python/share" \
    "$res/python/lib/"{itcl*,tcl*,tk*,thread*,libtcl*,libtk*,pkgconfig} \
    "$pystd/"{tkinter,idlelib,turtledemo,test,ensurepip,turtle.py} \
    "$site/"pip "$site/"pip-*.dist-info \
    "$pystd/lib-dynload/"_tkinter*.so "$pystd/config-$pymm-darwin"
find "$res/python/bin" -mindepth 1 ! -name "python$pymm" -exec rm -f {} +

# PySide6-Essentials, Shiboken6, VapourSynth (wheels unpacked, see unwheel)
for w in "${wheels[@]}"; do stagepy unwheel "$w" "$site"; done
stagepy thin "$site"
stagepy trim-qt "$site"

# ButterEye itself: the files git knows (committed or not), as a wheel, unpacked.
# The sources tar gets packaging/ and mvgpu/ too, so it can rebuild the plugins.
app_src=$work/src/buttereye-$version
mkdir -p "$app_src"
git -C "$repo" ls-files -z --cached --others --exclude-standard -- \
        buttereye pyproject.toml README.md COPYING LICENSES MANIFEST.in CHANGELOG.md \
        THIRD-PARTY.md packaging mvgpu tools/macos-tests.sh |
    (cd "$repo" && while IFS= read -r -d '' f; do [[ -f $f ]] && printf '%s\0' "$f"; done) |
    (cd "$repo" && tar --null -T - -cf -) | tar -xf - -C "$app_src"
cp -R "$app_src" "$work/src/pristine"   # before the wheel build adds files
mkdir -p "$work/tools/setuptools"
"$hostpy" -I -B "$here/appstage.py" unwheel "$setuptools_whl" "$work/tools/setuptools"
(cd "$work/tools" && PYTHONPATH=$work/tools/setuptools "$hostpy" -B -m pip wheel -q \
    --disable-pip-version-check --no-deps --no-build-isolation --no-index \
    -w "$work/wheel" "$app_src")
stagepy unwheel "$work/wheel/buttereye-$version-py3-none-any.whl" "$site"

# the native parts: Homebrew's bottles and ButterEye's plugins, with every
# library they load
plugin_autoload=$(ls -d "$prefix"/lib/python3.*/site-packages/vapoursynth/plugins | tail -1)
# znedi3 (Profile.aa) only when build-plugins.sh built it (BUTTEREYE_ZNEDI3=1);
# its weights go beside it, where the plugin looks for them
optional=()
if [[ $bundle_mkvmerge == 1 ]]; then
    optional+=("bin/mkvmerge=$prefix/bin/mkvmerge")
fi
if [[ -f $mac/vapoursynth/libznedi3.dylib ]]; then
    optional+=("vapoursynth/libznedi3.dylib=$mac/vapoursynth/libznedi3.dylib")
fi
stagepy collect "$prefix" "$res" "$work/collected.json" \
    "bin/mpv=$prefix/bin/mpv" \
    "bin/ffmpeg=$prefix/bin/ffmpeg" \
    "bin/ffprobe=$prefix/bin/ffprobe" \
    "bin/vulkaninfo=$prefix/bin/vulkaninfo" \
    "lib/libMoltenVK.dylib=$prefix/opt/molten-vk/lib/libMoltenVK.dylib" \
    "lib/libvulkan.1.dylib=$prefix/opt/vulkan-loader/lib/libvulkan.1.dylib" \
    "vapoursynth/mvtools.dylib=$plugin_autoload/mvtools.dylib" \
    "vapoursynth/libffms2.dylib=$prefix/opt/ffms2/lib/libffms2.dylib" \
    "vapoursynth/libmvgpu.dylib=$mac/vapoursynth/libmvgpu.dylib" \
    "vapoursynth/librife.dylib=$mac/vapoursynth/librife.dylib" \
    ${optional[@]+"${optional[@]}"}
stagepy relocate "$res" "$work/collected.json"
if [[ -f $mac/vapoursynth/nnedi3_weights.bin ]]; then
    install -m 0644 "$mac/vapoursynth/nnedi3_weights.bin" "$res/vapoursynth/nnedi3_weights.bin"
fi
mkdir -p "$res/share/vulkan/icd.d"
# the loader resolves a relative library_path against the manifest's folder:
# share/vulkan/icd.d -> Resources/lib (osapi.point_at_app hands mpv this file)
cat >"$res/share/vulkan/icd.d/MoltenVK_icd.json" <<'EOF'
{
    "file_format_version": "1.0.0",
    "ICD": {
        "library_path": "../../../lib/libMoltenVK.dylib",
        "api_version": "1.4.0",
        "is_portability_driver": true
    }
}
EOF
cp -R "$mac/share/rife-ncnn-models" "$res/share/"

# ---------------------------------------------------------------- licences and the record
lic=$res/licenses
mkdir -p "$lic/buttereye/LICENSES" "$lic/python" "$lic/pyside6" "$lic/vapoursynth"
cp -R "$mac/licenses/"* "$lic/"
rm -f "$lic/SOURCES.txt"   # the plugin build's offer; this app writes its own
cp "$repo/COPYING" "$repo/THIRD-PARTY.md" "$lic/buttereye/"
cp "$repo"/LICENSES/*.txt "$lic/buttereye/LICENSES/"
cp "$pystd/LICENSE.txt" "$lic/python/LICENSE.txt"
cp "$work/tools/python/licenses/"*.txt "$lic/python/"
cp "$work/tools/python/PYTHON.json" "$lic/python/PYTHON.json"
qt_out=$(stagepy qt-notices "$lic/pyside6" "${qt_srcs[@]}")
[[ $(wc -l <<<"$qt_out" | tr -d ' ') -eq 2 ]] || { echo "qt-notices failed" >&2; exit 1; }
qt_expr=$(sed -n 1p <<<"$qt_out")
qt_provides=$(sed -n 2p <<<"$qt_out")
qt_files=$(cd "$res" && find licenses/pyside6 -type f | LC_ALL=C sort | paste -sd, -)
cp "$site/vapoursynth-$VAPOURSYNTH_VER.dist-info/licenses/COPYING.LESSER" "$lic/vapoursynth/"
stagepy licences "$prefix" "$res" "$work/collected.json" "$work/brew-specs.txt"
stagepy formulae "$work/collected.json" >"$work/formulae.txt"
stagepy copyleft "$prefix" "$work/collected.json" >"$work/copyleft.txt"
brew_specs=()
while IFS= read -r line; do brew_specs+=("$line"); done <"$work/brew-specs.txt"
py_lics=$(cd "$res" && find licenses/python -type f | LC_ALL=C sort | paste -sd, -)
stagepy app-components "$mac/components.json" "$work/collected.json" "$work/components.json"
stagepy record "$work/components.json" "$res/buttereye-install.json" \
    "buttereye=$version=AGPL-3.0-or-later AND LGPL-3.0-or-later=licenses/buttereye/COPYING,licenses/buttereye/LICENSES/AdditionRef-ButterEye-generated-output.txt" \
    "python3=$PYTHON_VER=PSF-2.0 AND 0BSD AND Apache-2.0 AND BSD-2-Clause AND BSD-3-Clause AND MIT AND OpenSSL AND Zlib AND bzip2-1.0.6 AND blessing=$py_lics" \
    "python3-pyside6=$PYSIDE_VER=LGPL-3.0-only AND $qt_expr=$qt_files=bundled(qt6)($PYSIDE_VER),$qt_provides" \
    "python3-shiboken6=$PYSIDE_VER=LGPL-3.0-only=licenses/pyside6/LGPL-3.0-only.txt,licenses/pyside6/GPL-3.0-only.txt" \
    "vapoursynth-libs=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER" \
    "vapoursynth-tools=$VAPOURSYNTH_VER=LGPL-2.1-or-later=licenses/vapoursynth/COPYING.LESSER" \
    "${brew_specs[@]}"
stagepy sources-txt "$lic/SOURCES.txt" version="$version" repo=https://github.com/MiguelCarino/ButterEye \
    python="$PYTHON_VER" pbs="$PYTHON_PBS" pyside="$PYSIDE_VER" vapoursynth="$VAPOURSYNTH_VER" \
    macos_bottles="$(sw_vers -productVersion | cut -d. -f1)" formulae="$work/formulae.txt"

# bytecode, compiled now because the app never writes any (launcher.c): unchecked
# hashes, so a copy whose timestamps changed on the way still uses them, and
# relative paths, so no .pyc names the folder this was built in
find "$res/python" -name __pycache__ -prune -exec rm -rf {} +
"$hostpy" -I -B -m compileall -q -j0 --invalidation-mode unchecked-hash \
    -s "$res/python" -p "ButterEye.app/Contents/Resources/python" -x '/(test|tests)/' \
    "$res/python/lib/python$pymm" >/dev/null

# ---------------------------------------------------------------- 4. the gate
gate=$(stagepy check "$app")
printf '%s\n' "$gate"
minos=$(sed -n 's/^minos \([0-9.]*\) .*/\1/p' <<<"$gate")
[[ -n $minos ]] || { echo "no minimum macOS version found" >&2; exit 1; }
# minos is the newest LC_BUILD_VERSION any binary in the bundle asks for: dyld
# refuses a library whose minos is above the running macOS, so this is the
# oldest macOS the app can start on, not a guess. Homebrew bottles carry the
# macOS they were built on.

# ---------------------------------------------------------------- 5. plist, icon, signature
iconset=$work/ButterEye.iconset
mkdir -p "$iconset"
QT_QPA_PLATFORM=offscreen "$hostpy" -I -B "$here/appstage-icon.py" \
    "$repo/buttereye/gui/icons/buttereye.svg" "$iconset" "$site"
iconutil -c icns -o "$res/ButterEye.icns" "$iconset"
cat >"$contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key><string>en</string>
    <key>CFBundleDisplayName</key><string>ButterEye</string>
    <key>CFBundleDocumentTypes</key>
    <array>
        <dict>
            <key>CFBundleTypeName</key><string>Video</string>
            <key>CFBundleTypeRole</key><string>Viewer</string>
            <key>LSHandlerRank</key><string>Alternate</string>
            <key>LSItemContentTypes</key>
            <array>
                <string>public.movie</string>
                <string>org.matroska.mkv</string>
                <string>org.webmproject.webm</string>
            </array>
        </dict>
    </array>
    <key>CFBundleExecutable</key><string>ButterEye</string>
    <key>CFBundleIconFile</key><string>ButterEye</string>
    <key>CFBundleIdentifier</key><string>io.github.buttereye.ButterEye</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleName</key><string>ButterEye</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>$version</string>
    <key>CFBundleVersion</key><string>$version</string>
    <key>LSApplicationCategoryType</key><string>public.app-category.video</string>
    <key>LSArchitecturePriority</key><array><string>arm64</string></array>
    <key>LSMinimumSystemVersion</key><string>$minos</string>
    <key>LSRequiresNativeExecution</key><true/>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSHumanReadableCopyright</key><string>Copyright (C) 2026 The ButterEye contributors. AGPL-3.0-or-later.</string>
    <key>NSPrincipalClass</key><string>NSApplication</string>
    <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
    <key>UTImportedTypeDeclarations</key>
    <array>
        <dict>
            <key>UTTypeIdentifier</key><string>org.matroska.mkv</string>
            <key>UTTypeDescription</key><string>Matroska video</string>
            <key>UTTypeConformsTo</key><array><string>public.movie</string></array>
            <key>UTTypeTagSpecification</key>
            <dict>
                <key>public.filename-extension</key><array><string>mkv</string></array>
                <key>public.mime-type</key><array><string>video/x-matroska</string></array>
            </dict>
        </dict>
        <dict>
            <key>UTTypeIdentifier</key><string>org.webmproject.webm</string>
            <key>UTTypeDescription</key><string>WebM video</string>
            <key>UTTypeConformsTo</key><array><string>public.movie</string></array>
            <key>UTTypeTagSpecification</key>
            <dict>
                <key>public.filename-extension</key><array><string>webm</string></array>
                <key>public.mime-type</key><array><string>video/webm</string></array>
            </dict>
        </dict>
    </array>
</dict>
</plist>
EOF
# Open With and a drop on the Dock icon: macOS offers ButterEye for any movie
# (public.movie covers MP4, QuickTime, AVI, MPEG...) as a Viewer that never
# takes over as the default player (Alternate). macOS declares no Matroska or
# WebM type of its own, so the two are imported here, as conforming to
# public.movie; an app that exports one (VLC, IINA) wins over an import. The
# files arrive as QEvent.FileOpen (gui/app.py FileOpenRouter), not in argv.
plutil -lint "$contents/Info.plist" >/dev/null
printf 'APPL????' >"$contents/PkgInfo"

# Local symbols out of every Mach-O (strip -x keeps the exported ones: a
# VapourSynth plugin's VapourSynthPluginInit2, a Python module's PyInit_*, every
# dylib's API). install_name_tool and lipo already invalidated the signatures,
# which the signing loop below redoes. Measured on parts of the bundle, each
# re-signed and run afterwards: PySide6 + shiboken6 (237 Mach-O) 348 -> 329 MiB
# and still building a QApplication; mkvmerge with its 24 libraries 60.6 ->
# 56.4 MiB and still running; mvtools.dylib keeps VapourSynthPluginInit2. The
# whole bundle's saving was not measured. Homebrew's files are read-only
# (0444), hence the chmod; the modes are set for the whole app just below.
while IFS= read -r -d '' f; do
    if [[ $(head -c 4 "$f" | xxd -p) =~ ^(cffaedfe|cafebabe|cafebabf)$ ]]; then
        chmod u+w "$f"
        strip -x "$f" 2>/dev/null || { echo "strip -x failed on $f" >&2; exit 1; }
    fi
done < <(find "$contents" -type f -print0)

# no extended attributes or AppleDouble files from the build machine
xattr -cr "$app"
find "$app" -name .DS_Store -delete
chmod -R u+rwX,go+rX,go-w "$app"
find "$app" -exec touch -h -t "$stamp" {} +

# Ad hoc, inside out: every Mach-O on its own (install_name_tool and lipo
# invalidated the signatures they had), then the bundle, whose seal covers
# every file in Resources. --deep is not used: it signs only the nested code
# it recognises, and says nothing about the rest.
while IFS= read -r -d '' f; do
    if [[ $(head -c 4 "$f" | xxd -p) =~ ^(cffaedfe|cafebabe|cafebabf)$ ]]; then
        codesign -s - --force --timestamp=none "$f" 2>/dev/null
    fi
done < <(find "$res" -type f -print0)
codesign -s - --force --timestamp=none "$app"
codesign --verify --deep --strict --verbose=1 "$app"

stagepy scan "$app" "$HOME" "$(id -un)" "$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
echo "ButterEye.app: $(du -sm "$app" | cut -f1) MiB in $(find "$app" -type f | wc -l | tr -d ' ') files, macOS $minos or later"

# ---------------------------------------------------------------- 6. the disk image
dmgroot=$work/dmg
mkdir -p "$dmgroot"
ditto "$app" "$dmgroot/ButterEye.app"
ln -s /Applications "$dmgroot/Applications"
rm -f "$mac/$dmg"
hdiutil create -quiet -volname "ButterEye $version" -srcfolder "$dmgroot" -fs HFS+ \
    -format ULFO -ov "$mac/$dmg"
hdiutil verify -quiet "$mac/$dmg"

# ---------------------------------------------------------------- 7. Corresponding Source
if ((sources)); then
    src=$work/srctar/ButterEye-$version-macos-sources
    mkdir -p "$src/plugins" "$src/homebrew"
    cp -R "$work/src/pristine" "$src/buttereye-$version"
    cp -R "$mac/sources/." "$src/plugins/"
    cp "$vs_src" "${qt_srcs[@]}" "$pyside_src" "$src/"
    cp "$lic/SOURCES.txt" "$src/"
    cache_dl=$(brew --cache)/downloads
    while read -r formula kver; do
        keg=$prefix/Cellar/$formula/$kver
        mkdir -p "$src/homebrew/$formula"
        cp "$keg/.brew/$formula.rb" "$src/homebrew/$formula/"
        # the recipe the bottle was built from must be the one brew fetches by
        # today: otherwise its source is not this binary's source
        if ! diff -q <(brew cat "$formula") "$keg/.brew/$formula.rb" >/dev/null; then
            echo "$formula: the installed keg ($kver) was built from another recipe than" \
                 "today's; brew reinstall $formula and build again" >&2
            exit 1
        fi
        grep -qx "$formula" "$work/copyleft.txt" || continue
        # brew also "verifies" the formula file it read from the API (<name>.rb,
        # kept in another cache): the recipe copied above is the keg's own, so
        # only what is in downloads/ - source archive, patches, resources - counts
        got=()
        while IFS= read -r line; do
            [[ -f $cache_dl/$line ]] && got+=("$line")
        done < <(brew fetch --build-from-source -v "$formula" 2>&1 |
            sed -n "s/^==> Verifying checksum for '\(.*\)'.*/\1/p")
        if ((${#got[@]} == 0)); then
            # a git checkout (x264): no archive and no checksum, so archive it
            gitdir=$(brew --cache -s "$formula")
            [[ -d $gitdir/.git ]] || { echo "$formula: brew fetched nothing" >&2; exit 1; }
            git -C "$gitdir" archive --format=tar.gz --prefix="$formula/" \
                -o "$src/homebrew/$formula/$formula-$(git -C "$gitdir" rev-parse --short=12 HEAD).tar.gz" HEAD
            continue
        fi
        for g in "${got[@]}"; do cp "$cache_dl/$g" "$src/homebrew/$formula/${g#*--}"; done
        # patches that live in homebrew-core itself (`patch do file "Patches/..."`,
        # glib's for one) are not downloads and brew fetch does not list them:
        # take each from homebrew-core at the commit this recipe came from
        head=$(brew info --json=v2 "$formula" | "$hostpy" -I -c \
            'import json, sys; print(json.load(sys.stdin)["formulae"][0]["tap_git_head"])')
        while IFS= read -r rel; do
            [[ -n $rel ]] || continue
            curl -fsSL --retry 3 --proto '=https' -o "$src/homebrew/$formula/${rel##*/}" \
                "https://raw.githubusercontent.com/Homebrew/homebrew-core/$head/$rel"
            echo "$rel from homebrew-core $head" >>"$src/homebrew/$formula/PATCHES-FROM-HOMEBREW-CORE.txt"
        done < <(sed -n 's/^ *file "\(Patches\/[^"]*\)".*/\1/p' "$keg/.brew/$formula.rb")
    done <"$work/formulae.txt"
    find "$src" -name .DS_Store -delete
    xattr -cr "$src"
    find "$src" -exec touch -h -t "$stamp" {} +
    (cd "$work/srctar" && find "ButterEye-$version-macos-sources" -print | LC_ALL=C sort >"$work/srclist")
    COPYFILE_DISABLE=1 tar --uid 0 --gid 0 --uname root --gname wheel --no-mac-metadata --no-xattrs \
        -n -cf "$mac/$srctar" -C "$work/srctar" -T "$work/srclist"
    stagepy scan "$work/srctar" "$HOME" "$(id -un)" "$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
fi

(cd "$mac" && find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256) >"$mac/SHA256SUMS"
echo
ls -l "$mac/$dmg"
if ((sources)); then ls -l "$mac/$srctar"; fi
