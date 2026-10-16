<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Packaging (SCOPE §9)

| Spec | Package | Licence |
|---|---|---|
| `buttereye.spec` | `buttereye` (noarch application) | AGPL-3.0-or-later AND LGPL-3.0-or-later (bundled FSRCNNX shader) |
| `plugins/buttereye-vs-rife-ncnn.spec` | RIFE on ncnn/Vulkan (bundled static ncnn 305837fd + glslang a9ac7d5) | MIT AND LGPL-2.1-or-later AND WTFPL AND BSD-3-Clause AND BSD-2-Clause AND Zlib AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2 |
| `plugins/buttereye-rife-ncnn-models.spec` | RIFE v4.26, v4.22-lite, v4.18 ncnn models (noarch) | MIT |
| `plugins/buttereye-vs-mvtools.spec` | MVTools v29.2 | GPL-2.0-or-later AND ISC |
| `plugins/buttereye-vs-mvgpu.spec` | mvgpu 0.0.1, ButterEye's GPU motion-vector engine (Vulkan), packed from `mvgpu/` | AGPL-3.0-or-later |

Plugins install into `%{_libdir}/buttereye/vapoursynth/` (not autoloaded) and models into
`%{_datadir}/buttereye/rife-ncnn-models/`. Every downloaded source is SHA-256-checked;
the RIFE tarballs are repacked reproducibly by `plugins/repack-rife-ncnn.sh` (no
upstream models beyond the curated three, no submodules). `plugins/licenses/` holds the
licence texts SCOPE §9 asks for that the upstream tarballs don't carry, pinned to
upstream commits; `plugins/PROVENANCE-rife-ncnn-models.md` records where each model
comes from, with checksums.

## Build

```sh
packaging/build.sh                    # all five, fedora-44 and fedora-45, mock, network off
packaging/build.sh --srpm-only        # just the SRPMs (for copr-cli build)
SPECS="buttereye" packaging/build.sh fedora-44-x86_64
```

Work and results: `~/.cache/buttereye/packaging/` (`SRPMS/`, `results/<chroot>/<package>/`).
Needs `mock` (and membership of the `mock` group), `rpm-build`, `rpmdevtools` (`spectool`)
and `python3 -m build`. Uploading to the COPR (`copr-cli build <owner>/buttereye *.src.rpm`
in a project created with `--enable-net off`) is the owner's step.

From a host that is not Fedora x86_64 (an Apple Silicon Mac, say), the same script runs
unchanged inside a pinned `fedora:44` amd64 container (podman, `--privileged`, mock with
`isolation=simple`), with the same arguments and `SPECS`/`JOBS`, into the same results
directory:

```sh
packaging/build-in-container.sh                          # all five, fedora-44 and fedora-45
SPECS="buttereye" packaging/build-in-container.sh        # the application only
```

On Apple Silicon the container runs under qemu-user in the podman machine. Rosetta is
**not** usable for this: on the machine's 6.19 kernel GNU tar 1.35 fails every file with
`Cannot stat: Function not implemented` under it (measured), so leave the machine's
Rosetta off. Give the machine 6 CPUs and 8 GiB (`podman machine set --cpus 6 --memory 8192`).

## Windows plugins (docs/PLATFORMS.md, phases 3 and 4)

```sh
packaging/windows/build-plugins.sh    # ~6 minutes; mock chroot with Fedora's MinGW-w64
tools/wine-tests.sh                   # loads them into VapourSynth for Windows under Wine
```

The same sources and patches as the Fedora packages (fetched and checked by
`build.sh --srpm-only`), cross-compiled into self-contained DLLs: the C++ runtime,
OpenMP, fftw and winpthreads are linked in, and the build fails if a DLL imports
anything but `KERNEL32.dll`, `msvcrt.dll` or `vulkan-1.dll` (which the GPU driver
installs). One extra patch, `plugins/buttereye-vs-rife-ncnn-windows-paths.patch`, fixes
RIFE's Windows model loading for MinGW builds (Windows-only code). One Fedora patch is
not applied yet: `plugins/buttereye-vs-rife-ncnn-keep-instance.patch` (the GPU instance
and model kept across mpv's seeks) waits for a check that mpv still quits cleanly on
real Windows hardware, since its GPU teardown would then run while the DLL unloads.

The same chroot also builds, with `build-tools-in-chroot.sh`, the two Windows pieces
the installer adds that Windows lacks, from pinned upstream releases (`windows/pins.sh`,
SHA-256-checked; downloads cached in `~/.cache/buttereye/windows-dl/`):

- `ffms2.dll`: FFMS2 5.0 with a decode-only FFmpeg 8.1.3 (no encoders, filters,
  network or GPL parts: LGPL-2.1-or-later), dav1d 1.5.4 and zlib linked in;
- `vulkaninfo.exe`: Vulkan-Tools 1.4.328.1 with volk, plus
  `windows/vulkaninfo-mingw-user32.patch` (upstream's user32 setup is MSVC-only, so
  every MinGW build crashes on Windows; worth reporting upstream and to Fedora).

Output in `~/.cache/buttereye/windows/`: `vapoursynth/{mvtools,librife,libmvgpu,ffms2}.dll`,
`vulkaninfo.exe`, `share/rife-ncnn-models/`, `licenses/<component>/` (including fftw,
FFmpeg, dav1d, zlib, volk and the MinGW-w64 runtime, which are linked in),
`components.json` (entries for the installer's `buttereye-install.json`), `sources/`
(the Corresponding Source, with Fedora's `mingw-fftw` source package, without which
the build fails, and the models archive) and `SHA256SUMS`.
Needs `mock` and network for the chroot packages and the downloads.

## Windows installer (docs/PLATFORMS.md, phase 4)

```sh
packaging/windows/build-installer.sh  # ~1-2 minutes once the plugins are built
tools/wine-tests.sh                   # also installs, checks and uninstalls it under Wine
```

Runs `build-plugins.sh` first if its output is missing, checks it against its
`SHA256SUMS`, fetches the pinned runtime (Python 3.14.8's embeddable build and the
PySide6-Essentials, Shiboken6 and VapourSynth R81 wheels for Windows; `pip` only unpacks
them, `--no-index`), stages the install folder in `~/.cache/buttereye/windows-installer/`
(Qt trimmed to Core, Gui, Widgets, Svg and SvgWidgets and the platform, style and SVG
plugins, without the wheels' copies of Microsoft's C++ runtime, with a check that
every bundled DLL a binary imports is still there and that no Microsoft DLL but
Python's own `vcruntime140*.dll` is; the notices of the third-party code inside Qt
from the pinned qtbase and qtsvg sources; `winstage.py`), and makes the icon and the installer in
the chroot (`rsvg-convert`, `icotool`, Fedora's `makensis` from `mingw32-nsis`;
`windows/buttereye.nsi`). Output in `~/.cache/buttereye/windows/`:

| File | What |
|---|---|
| `ButterEye-<version>-setup.exe` | the installer (63 MiB; installs 159 MiB per user, no administrator rights) |
| `ButterEye-<version>-windows-sources.tar` | the Corresponding Source of everything built for it: ButterEye with `packaging/` and `mvgpu/`, the plugin and tool sources and patches, fftw's Fedora source package, the models archive, the build scripts |
| `SHA256SUMS` | every file above and the plugin build's |

Publish the sources tar next to the setup.exe: the installed `licenses\SOURCES.txt`
points there for the parts built here, and to the official source pages (with a written
offer) for Python, Qt/PySide6 and VapourSynth, which are bundled unmodified. ButterEye's
version comes from `buttereye/__init__.py`. The output is reproducible in its inputs
(everything pinned), not byte for byte (NSIS and the PE files carry build times).

Microsoft's Visual C++ runtime is not bundled (but for the two `vcruntime140*.dll`
that are part of Python's embeddable build, under Python's `LICENSE.txt`): the
installer offers to download it from Microsoft when it is missing, and Qt and
VapourSynth use it. Microsoft lets only licensed Visual Studio users
redistribute `vc_redist.x64.exe`; with that licence,
`VCREDIST=/path/to/vc_redist.x64.exe packaging/windows/build-installer.sh` embeds it.
The installer is not code-signed, so Windows SmartScreen will warn about it.

### In CI (`.github/workflows/windows.yml`)

GitHub Actions runs the same two scripts with `--no-mock`: inside a `fedora:44`
container, which then *is* the chroot (`windows/chroot.sh` holds both modes; it refuses
`--no-mock` outside a container, as root only, so a dev box's `/` is never written to).
The `test` job installs the artifact silently on `windows-latest`, runs the installed
`buttereye doctor --json` and `tests/windows` (the installer test with GitHub's Python,
the rest with the installed ButterEye's own Python, MVTools and a pinned mpv build),
then uninstalls it. The runners have no GPU: RIFE and mvgpu skip there with the reason.
Triggers: pushes to `main` touching `packaging/windows/`, `buttereye/core/`,
`tests/windows/` or `mvgpu/`, and "Run workflow" by hand. To try the CI build locally:

```sh
podman run --rm -v "$PWD:/src:ro" -v ~/.cache/buttereye-ci:/cache -e XDG_CACHE_HOME=/cache \
    fedora:44 bash -c 'dnf -y install git-core && git config --global --add safe.directory /src &&
    cd /src && packaging/windows/build-plugins.sh --no-mock && packaging/windows/build-installer.sh --no-mock'
```

## macOS plugins (docs/PLATFORMS.md, the macOS phase table, phases 3 and 4)

```sh
packaging/macos/build-plugins.sh                              # mvgpu in ~3 seconds, then RIFE
packaging/macos/check-dylibs.sh ~/.cache/buttereye/macos/vapoursynth
BUTTEREYE_TEST_PLUGINS=~/.cache/buttereye/macos/vapoursynth \
    python -m pytest tests/macos/test_plugins_macos.py        # the opt-in gate
```

`build-plugins.sh` runs `packaging/macos/build-rife.sh` when it is there, which
since phase 4 it is, so one command now builds both plugins. `build-rife.sh` also
runs on its own, and takes `BUTTEREYE_RIFE_NCNN=system|bundled` (default
`system`: Homebrew's `ncnn`).

Nothing here goes through `build.sh`, which wants `mock`, `rpmbuild`, `spectool`
and GNU `stat -c`; a Mac has none of them, so this is a new script in a new
directory, exactly as the Windows one was. It is not a cross build either: no
chroot, no container, nothing downloaded. Apple's clang compiles `mvgpu/` out of
this repository against Homebrew's VapourSynth and Vulkan headers, `glslang`
turns the seven shaders into SPIR-V, and that is the whole build.

Needs, all from Homebrew and installed by the user rather than by the script —
which checks and prints the `brew install` line for anything missing: `meson`,
`ninja`, `glslang`, `pkgconf`, `vapoursynth`, `vulkan-headers` and
`vulkan-loader` to build, plus Apple's Command Line Tools
(`xcode-select --install`) for clang. To *run* the result it also needs
`molten-vk`: macOS has no native Vulkan, so `libmvgpu.dylib` reaches the GPU
through MoltenVK as a portability driver. A missing `molten-vk` does not stop a
build. The full Xcode is not needed and is not present on the machine this was
written on — nothing here compiles a `.metal` file.

Output in `~/.cache/buttereye/macos/` (`$XDG_CACHE_HOME` if set; never the
repository), with the build tree beside it in `macos-build/`:

| File | What |
|---|---|
| `vapoursynth/libmvgpu.dylib` | ButterEye's GPU motion-vector engine, 124 416 bytes, arm64 Mach-O |
| `vapoursynth/librife.dylib` | RIFE-ncnn-Vulkan, 162 528 bytes, arm64 Mach-O bundle, linking `opt/ncnn` and `opt/libomp` and nothing else a Mac lacks (phase 4) |
| `share/rife-ncnn-models/<model>/` | the three curated RIFE models, checked against `plugins/PROVENANCE-rife-ncnn-models.md` (phase 4) |
| `licenses/<component>/`, `licenses/SOURCES.txt` | the licence text of what was built, and the written offer saying where its source is. `build-rife.sh` writes RIFE's paragraph as `licenses/SOURCES.rife.txt`; `build-plugins.sh` appends it to `SOURCES.txt` and removes the fragment, so the offer exists once and `SHA256SUMS` does not record two copies of it |
| `components.json` | install-record entries (`checks_pkgs.manifest_db`). `build-rife.sh`'s `components-rife.json` is merged into it and removed the same way, so a finished directory holds neither fragment |
| `sources/` | the Corresponding Source of what was built: `mvgpu/`, `COPYING`, both scripts, and for RIFE the patched plugin source with the three patches beside it |
| `SHA256SUMS` | every file above (`shasum -a 256`, in `sha256sum -c` line format) |

`build-plugins.sh` runs `check-dylibs.sh` on its own output and fails the build
if it refuses anything. That check is the macOS peer of the Windows import check
(`check_dll` in `windows/build-in-chroot.sh`, which refuses any Microsoft DLL
but `msvcrt` and `KERNEL32`): both ask whether the file would still work on a
machine that is not the build machine. Here that means arm64 Mach-O, an exported
`_VapourSynthPluginInit2`, and nothing linked beyond macOS itself and a short
list of Homebrew formulae a macOS build may link — through the versionless
`opt/<formula>` symlink, so a `brew upgrade` does not break the plugin. A
baked-in Cellar path, an `@rpath`/`@loader_path` reference, any `LC_RPATH` and
anything else absolute (`/usr/local` included) are refused with the reason named.
The check does not look at code signatures at all (see below).

That allowlist — `vulkan-loader`, `ncnn`, `libomp`, `glslang`, `spirv-tools`,
`fftw` — **is not a list of declared dependencies, and only `vulkan-loader` is
declared and checked today**: it is the one of the six in
`checks_pkgs.BREW_FORMULAE`, which is what `doctor/brew.py` looks up in the
Cellar, and nothing in `buttereye/` asks about the other five. The gap is
concrete rather than theoretical, and phase 4 made it real: `librife.dylib` as
built here links `<prefix>/opt/ncnn/lib/libncnn.1.dylib` and
`<prefix>/opt/libomp/lib/libomp.dylib` (`otool -L` on the output, beside
`libc++` and `libSystem` and nothing else) — both allowed by the check, neither
declared anywhere — so a user without those two formulae gets a plugin that
cannot load, past a gate that passed it. **Phase 4's tap has to declare them
and the doctor has to check them before ButterEye ships anything that links
them.** Phase 4 did half of that: `macos/homebrew/buttereye-vs-rife-ncnn.rb`
declares `ncnn` and `libomp`, so a tap install gets them, but
`checks_pkgs.BREW_FORMULAE` still lists neither, so the doctor cannot explain a
hand-built `librife.dylib` that will not load on a Mac without those two
formulae. The list is deliberately still not tightened: it passes everything
built today (`libmvgpu.dylib` links the loader and nothing else, `librife.dylib`
links `opt/ncnn` and `opt/libomp`). Note that
the allowlist runs in neither direction — `molten-vk` *is* declared and checked
(`checks_pkgs.BREW_FORMULAE`, `checks_vulkan.py`) and a plugin still may not link
it, because a plugin should reach MoltenVK through the loader's ICD.

**Two of the four plugins the Windows build produces are not built here, and
the script says so out loud on every run**, because a silent omission is how a
packager ships three plugins believing it shipped four. MVTools and ffms2 are
Homebrew bottles (`vapoursynth-mvtools` 29 and `ffms2` 5.0, both arm64, both
installed into VapourSynth's own autoload directory), so the user installs them
and **Homebrew is the distributor that carries their source duty**. That is also
why the Windows ffms2 problem does not arise: the Windows build compiles its own
because the official Windows binary is GPLv3 through its FFmpeg with source
nobody can reconstruct, and ButterEye could not meet a source offer for a binary
it conveyed — here it conveys nothing. `vulkaninfo` likewise comes from
`vulkan-tools`.

**RIFE-ncnn: measured in phase 3, built in phase 4.** `macos/RIFE-SPIKE.md` built
the pinned tag natively on arm64 — the commit `repack-rife-ncnn.sh` pins,
ButterEye's two existing patches applying with no fuzz, `-Duse_system_ncnn=true`
against Homebrew's `ncnn` — loaded it into VapourSynth R81, found the GPU through
MoltenVK and rendered a correct in-between frame. It also found what a real build
must carry: two `libomp` flags (`-Dcpp_args=-I<prefix>/opt/libomp/include` and
`-Dcpp_link_args="-L<prefix>/opt/libomp/lib -lomp"`, because the plugin's
`meson.build` passes `-Xpreprocessor -fopenmp` with no link args and
`-undefined dynamic_lookup` hides it), and a new patch for a stack buffer
overflow in RIFE's model-path loader — two `char[256]` buffers written with
`sprintf`, which `SIGABRT`s the whole process at a `model_path` of 242
characters, and which is reachable on Fedora too, not just here.

`macos/build-rife.sh` is the step that fills the hook, and it carries all three:
the two `libomp` flags, and `plugins/buttereye-vs-rife-ncnn-model-path.patch`
beside the other two (that patch is applied unconditionally here *and* in the
spec, so it changes the Fedora build — on purpose, because it turns a `SIGABRT`
into a path that works). It is also the one macOS step that **downloads**, which
`build-plugins.sh` does not and says so: neither the plugin source nor the model
weights are in this repository. Everything fetched is pinned — a blobless sparse
clone of the pinned commit, verified by hash before anything is built (the sparse
checkout is what keeps upstream's ~1.4 GB of models out: the plain tarball of
that commit is 1.37 GB), the models checked against
`plugins/PROVENANCE-rife-ncnn-models.md`, and the bundled variant's two tarballs
against the spec's `Source2`/`Source3` sums, with `shasum -a 256` because macOS
has no `sha256sum`. Downloads are cached, so a rerun fetches nothing.

**Which ncnn is a decision, not a detail.** Default and tap:
`-Duse_system_ncnn=true` against Homebrew's `ncnn`;
`BUTTEREYE_RIFE_NCNN=bundled` builds the pinned static one instead and is kept
working rather than merely described. The spike measured that the two are not the
same product — 21.3 % of output samples differ, 1.8 % by more than one 8-bit
step — so the trade is written down in the script's header and in
`macos/SOURCES.md`: the bottle gives 167 KB against 5.9 MB, 4 ninja targets
against 466, and Homebrew's own security updates; it costs reproducibility,
because a tap pins a formula version and not bytes, so a `brew` bump of `ncnn`
moves the output with no change here. The other reason Fedora pins — NVIDIA GPU
faults at `gpu_thread >= 2` — does not carry over, because an M2 exposes one
compute queue and `gpu_thread` is 1 either way. **Nothing here may be described
as "the same build as Linux."**

**RIFE on a Mac is for offline render**: 8.5–9.8 fps at 1080p on an M2 against
the ≈48 fps live 2× 24p needs, so `buttereye render` uses it and live playback
should not, and no user-facing string may promise otherwise. `-DVulkan_LIBRARY`
is deliberately not passed: measured unnecessary, and passing it would mean a
third edit into upstream source that Fedora does not make, to silence one
cosmetic CMake warning.

**Licence duties.** For `libmvgpu.dylib` they are simple, because it is
ButterEye's own AGPL-3.0-or-later code: `licenses/buttereye-vs-mvgpu/COPYING` is
the repository's own, `sources/mvgpu/` is the complete Corresponding Source, and
`licenses/SOURCES.txt` states both — along with what is *not* conveyed here and
who does convey it (the bottles, the Vulkan loader, MoltenVK, `ncnn` and
`libomp`). The written offer names only what the run actually produced: it makes
no claim about a plugin this script did not build.

RIFE is the reason the hook contract asks for the **duties and not just the
artefact**, and since phase 4 those duties are met rather than hypothetical:
`build-rife.sh` writes `licenses/buttereye-vs-rife-ncnn/`,
`licenses/buttereye-rife-ncnn-models/`, `licenses/SOURCES.rife.txt`,
`sources/buttereye-vs-rife-ncnn-<tag>/` with the three patches, and
`components-rife.json`. `librife.dylib` is not ButterEye's own code:
it is HolyWu's MIT plugin over a bundled `VapourSynth4.h` (LGPL-2.1-or-later) and
`VSHelper4.h` (WTFPL), built against `ncnn` (BSD-3-Clause, BSD-2-Clause, Zlib —
plus glslang's MIT, Apache-2.0 and GPL-3.0-or-later WITH Bison-exception-2.2 when
they are linked statically), and the models are a separate MIT component with the
chain in `plugins/PROVENANCE-rife-ncnn-models.md`. `plugins/buttereye-vs-rife-ncnn.spec`
is the authority for the expression; **"MIT" on its own is not it** — the macOS
build links nothing statically, so what it writes is the spec's
`--with system_ncnn` expression, `MIT AND LGPL-2.1-or-later AND WTFPL`.
`build-plugins.sh` **concatenates** the RIFE fragment onto `SOURCES.txt` rather
than letting anything overwrite it, and refuses outright to finish a build that
has a `librife.dylib` without those files. A hook that asked only for a dylib
would let a RIFE step satisfy the script while breaking the AGPL.

**Nothing is signed or notarised**, and nothing here needs to be.
The only signature on a plugin built here is the ad-hoc one Apple's linker
applies — measured, `codesign -dvvv` reports
`flags=0x20002(adhoc,linker-signed)`, `Signature=adhoc` and
`TeamIdentifier=not set` — which is what arm64 needs in order to load the file at
all and says nothing about who built it. The output is a cache directory on the
machine that built it; it is not distributed, and macOS asks no questions about a
file it did not receive from somewhere else. The distribution phase 4 wrote is a
**Homebrew tap** for the same reason: `brew` does not mark what it fetches with
`com.apple.quarantine` (measured — `xattr -l` on both a downloaded bottle
tarball in `~/Library/Caches/Homebrew/downloads/` and an installed keg dylib
prints nothing), so Gatekeeper never gets involved, while an unsigned `.app`
downloaded from a GitHub release does carry the quarantine attribute and is
refused outright (measured: an ad-hoc-signed binary with that attribute set is
`rejected` by `spctl -a -t exec`). A signed and notarised bundle needs a paid
Apple Developer ID, which is an owner decision about money and not a
prerequisite for anything in this directory.

## macOS app and disk image (docs/PLATFORMS.md, "The .app"; 0.0.3, experimental)

```sh
packaging/macos/build-app.sh                  # runs build-plugins.sh first if needed
BUTTEREYE_TEST_APP=~/.cache/buttereye/macos-app/stage/ButterEye.app \
    python -m pytest tests/macos/test_app_bundle.py   # the opt-in gate, from outside
```

On an Apple Silicon Mac with the Xcode command-line tools and, as build inputs,
Homebrew's `mpv ffmpeg ffms2 vapoursynth-mvtools molten-vk vulkan-loader vulkan-tools`
(plus what `build-plugins.sh` needs). Downloads are pinned in `macos/pins.sh`;
`macos/appstage.py` does the copying, relocation (`install_name_tool`), checking and
licence work, `macos/launcher.c` is the app's main executable, and
`macos/appstage-icon.py` draws the icon with the bundled Qt. Output in
`~/.cache/buttereye/macos/` (`$XDG_CACHE_HOME` if set):
`ButterEye-<version>-macos-arm64.dmg`, `ButterEye-<version>-macos-sources.tar`
(Corresponding Source of every GPL/LGPL/MPL part and every Homebrew recipe; needs
network for `brew fetch --build-from-source`) and `SHA256SUMS`; `--no-sources` skips
the tar for a quick local build, never for a release. Ad-hoc signed only: the README's
macOS section has the one Terminal command a user runs after downloading it.

## macOS: the Homebrew tap (docs/PLATFORMS.md, the macOS phase table, phase 4)

`macos/homebrew/` holds four formulae and `macos/SOURCES.md` is the tap's source
and licence statement — the macOS peer of the Windows installer's
`licenses\SOURCES.txt`, and a shorter one, because a tap conveys **source**:
`brew` downloads each upstream archive and builds it on the user's machine, so
the Corresponding Source is what it fetched.

| Formula | Builds and installs | Licence |
|---|---|---|
| `buttereye.rb` | ButterEye in a virtualenv with the `buttereye` and `buttereye-gui` commands; `COPYING` and `LICENSES/` into `doc` and symlinked into `site-packages`, where `licences.py` looks for the texts (unverified: nothing has been installed, and the pane's macOS rows and source offer did need code — `licences._macos_rows`, `licences._source_link`); `build-plugins.sh` and `check-dylibs.sh` into `pkgshare` | AGPL-3.0-or-later AND LGPL-3.0-or-later (the FSRCNNX shader), the same expression as `buttereye.spec` |
| `buttereye-vs-mvgpu.rb` | `libmvgpu.dylib` from `mvgpu/` into `<prefix>/lib/buttereye/vapoursynth`, private and not autoloaded — the analogue of `%{_libdir}/buttereye/vapoursynth` | AGPL-3.0-or-later |
| `buttereye-vs-rife-ncnn.rb` | `librife.dylib` against Homebrew's `ncnn`, with the three patches and the two `libomp` flags, into the same private directory | MIT AND LGPL-2.1-or-later AND WTFPL |
| `buttereye-rife-ncnn-models.rb` | the three curated models into `<prefix>/share/buttereye/rife-ncnn-models`, where `types._packaged_data_dir` looks | MIT |

What a user would type once phase 5 has tagged a release and filled the checksums
in. **Nobody has run these lines**, here or anywhere:

```sh
brew tap <owner>/buttereye
brew install buttereye                                           # the application
brew install buttereye-vs-mvgpu                                  # optional: GPU motion vectors
brew install buttereye-vs-rife-ncnn buttereye-rife-ncnn-models   # optional: RIFE, offline render only
buttereye doctor
```

`buttereye.rb` depends on `ffmpeg`, `ffms2`, `molten-vk`, `mpv`, `pyside`,
`python@3.14`, `vapoursynth`, `vapoursynth-mvtools` and `vulkan-loader`, and on
`:macos` with `arch: :arm64`. `mkvtoolnix` and `vulkan-tools` are deliberately
**not** dependencies: Homebrew has no peer of rpm's `Recommends:`, each is wanted
for one thing, ButterEye degrades without it and already prints the
`brew install` line when it wants it (`commands.install_command`). The engine
formulae are separate for the same reason: ButterEye runs with MVTools alone.
`system_site_packages` must stay Homebrew's default of true — VapourSynth's
Python module is a `.pth` in `python@3.14`'s `site-packages`, not a wheel a
formula could install, and turning it off would hide Homebrew's PySide6 too.

**What is not verified.** No formula has been installed, bottled, `brew audit`ed
or `brew style`d, and nothing has been published. `ruby -c` passes on all four;
`brew style` was not run because rubocop is not vendored in this Homebrew
installation and running it would install gems into the user's own Homebrew,
which no part of this port does. **Nothing it names can be fetched**: every
formula's `url` points into a `v0.0.3` release that does not exist (the newest
tag here is `v0.0.2`) and every `sha256` is 64 zeros, a placeholder that fails
the download rather than installing something unverified. `v0.0.2` predates the
macOS port anyway, so a formula built from that tag would install a ButterEye
with no macOS platform layer and a `libmvgpu.dylib` that cannot create a Vulkan
instance on any Mac. `--HEAD` has something behind it for only two of the four —
`buttereye.rb` and `buttereye-vs-mvgpu.rb` have a `head` line, the RIFE plugin
and models formulae have none — so RIFE cannot be installed from this tap today
by any route. Two licence debts are named in `macos/SOURCES.md`
rather than hidden: the RIFE formula still has to add `plugins/licenses/`' pinned
texts as `resource`s, and the repacked RIFE tarball its `url` names does not
exist — it cannot even be produced on a Mac, because `repack-rife-ncnn.sh` needs
GNU `tar --sort` and macOS ships bsdtar.
