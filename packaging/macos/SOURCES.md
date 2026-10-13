<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (C) 2026 The ButterEye contributors -->
# macOS: where the source code is

ButterEye is free software under the GNU Affero General Public License, version 3
or later. This page is the written offer for the macOS distribution — the
Homebrew tap in `packaging/macos/homebrew/` (`docs/PLATFORMS.md`, the macOS phase
table, phase 4). It is the macOS peer of the Windows installer's
`licenses\SOURCES.txt` and of the offer
`packaging/macos/build-plugins.sh` writes into its own output, and it says the
same things those do, for the same reasons.

Its shape differs from the Windows one in a way that matters, and the difference
is not a convenience: **Homebrew installs every third-party component from its own
formula, with that formula's own licence and its own upstream source URL**, and
`brew fetch --build-from-source <formula>` retrieves exactly that source.
ButterEye therefore conveys no third-party binary on macOS. The Windows installer
conveys several (it links the C++ runtime, OpenMP, fftw and winpthreads into its
DLLs and bundles Python, Qt and VapourSynth unmodified), which is why
`packaging/README.md`'s Windows section has to publish a sources tar. Here there
is nothing of the sort to publish.

What the tap does convey is **ButterEye's own code and ButterEye's own builds of
two components**, and that is what the table below is about.

## The app (0.0.3) is the other way round

Everything on this page is about the **Homebrew tap**. The experimental
`ButterEye.app` disk image that 0.0.3 adds (`build-app.sh`, `docs/PLATFORMS.md` "The
.app") is the opposite case: it carries mpv, FFmpeg, ffms2, MVTools, MoltenVK, the
Vulkan loader, every library they link, Python, Qt and VapourSynth inside itself, so
**ButterEye conveys all of them and owes their source**. Its offer is the app's own
`Contents/Resources/licenses/SOURCES.txt`, and `ButterEye-<version>-macos-sources.tar`,
published beside the image, is the Corresponding Source: the recipe of every Homebrew
bottle in the app exactly as it was built, the source archives and patches of every one
under a GPL, LGPL or MPL licence, VapourSynth's, Qt's and PySide6's sources, and this
repository with the scripts that built the app. It is ad-hoc signed and not notarised,
so the "nothing needs to be signed" reasoning below does not apply to it; what
Gatekeeper does with it is measured in `docs/PLATFORMS.md`.

## Nothing is signed or notarised, and nothing needs to be

`brew` fetches over curl and sets no `com.apple.quarantine` attribute on what it
fetches — measured on this port: `xattr -l` prints nothing for either a
downloaded bottle tarball in `~/Library/Caches/Homebrew/downloads/` or an
installed plugin dylib in the Cellar. Gatekeeper is never asked, so no Apple
Developer ID, no code signature and no notarisation are involved anywhere in this
distribution.

That is also the reason the tap exists instead of an `.app` bundle. An unsigned
bundle downloaded from a GitHub release *does* carry the quarantine attribute and
current macOS refuses it outright — the right-click bypass is gone from the UI —
and notarising one needs a paid Apple Developer ID. **That is an owner decision
about money, not an engineering gap**: a signed bundle is not written because
nobody bought the certificate, and a tap needs neither.

The only signature anywhere in the output is the ad-hoc one Apple's linker
applies to every arm64 Mach-O it produces (measured on `libmvgpu.dylib`:
`flags=0x20002(adhoc,linker-signed)`, `Signature=adhoc`,
`TeamIdentifier=not set`). It is what arm64 needs in order to load a file at all
and says nothing about who built it.

Apple Silicon only. The formulae declare `depends_on arch: :arm64` and refuse an
Intel Mac with "The arm64 architecture is required for this software." rather
than failing later and obscurely: the Homebrew prefix there is `/usr/local`, and
nothing on that hardware has been measured.

## What the tap conveys, and where its source is

| Formula | What it installs | Licence | Corresponding Source |
|---|---|---|---|
| `buttereye` | ButterEye itself, into the formula's own virtual environment | AGPL-3.0-or-later AND LGPL-3.0-or-later | the formula's `url`: the tagged source archive of <https://github.com/MiguelCarino/ButterEye>, which is the complete source of this program. `brew fetch buttereye` retrieves it; `brew edit buttereye` shows the recipe |
| `buttereye-vs-mvgpu` | `<prefix>/lib/buttereye/vapoursynth/libmvgpu.dylib` | AGPL-3.0-or-later | the same archive — `mvgpu/` in it is the whole plugin. The formula also installs a copy as `<keg>/share/buttereye-vs-mvgpu/sources/mvgpu`, so the source is present rather than promised |
| `buttereye-vs-rife-ncnn` | `<prefix>/lib/buttereye/vapoursynth/librife.dylib` | MIT AND LGPL-2.1-or-later AND WTFPL | the formula's `url`: upstream tag `r9_mod_v33`, commit `c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f` of <https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan>, repacked by `packaging/plugins/repack-rife-ncnn.sh` without `models/` and without the `subprojects/ncnn` submodule, plus the three ButterEye patches the formula fetches by URL with checksums. Those are the complete sources of this binary |
| `buttereye-rife-ncnn-models` | `<prefix>/share/buttereye/rife-ncnn-models/` | MIT | model weights, which are data and not a program. The upstream release, the converter and a checksum for every file are recorded in `packaging/plugins/PROVENANCE-rife-ncnn-models.md` |

The licence expression of each is the one the matching RPM spec declares, and
**the spec stays the authority for it** (`packaging/plugins/*.spec`,
`packaging/buttereye.spec`). Two of those expressions are shorter here than on
Fedora, and only for a reason that is written down:

- `buttereye-vs-rife-ncnn` is built against Homebrew's `ncnn` formula
  (`-Duse_system_ncnn=true`), so it bundles and statically links nothing. The
  Fedora package bundles pinned ncnn and glslang sources and its expression
  therefore also carries BSD-3-Clause, BSD-2-Clause, Zlib, Apache-2.0 and
  GPL-3.0-or-later WITH Bison-exception-2.2. Here those are a separate keg the
  user installs. **"MIT" alone is not the answer for either build.**
- `buttereye` keeps LGPL-3.0-or-later because it still bundles the FSRCNNX x2
  8-0-4-1 mpv shader unmodified, exactly as the RPM does.

### Two things in that table are not filled in yet

Honesty before tidiness: the tap is written, not published.

1. **Every `sha256` in the four formulae is zeroed and every `url` points at a
   `v0.0.3` that does not exist.** That is deliberate, not an oversight. `v0.0.2`
   is the newest tag and the whole macOS port lands after it — phases 0 and 1 are
   commits on top of it, phases 2 to 4 are not committed at all — so a formula
   pinned to `v0.0.2` would install a ButterEye with no macOS platform layer, and
   a `buttereye-vs-mvgpu` built from it would produce a `libmvgpu.dylib` that
   cannot create a Vulkan instance on any Mac (the MoltenVK portability retry is
   not in that tag). The release step fills the versions and the checksums. Until
   it does, `brew install --HEAD` is the only path that can work, and only the
   two formulae built out of this repository have a `head` to install.
2. **The two RIFE tarballs do not exist yet either.** They are
   `repack-rife-ncnn.sh`'s output, and that script needs GNU `tar --sort`; the Mac
   this was written on has only `bsdtar` (measured), so neither tarball could be
   produced or checksummed here. They are the same bytes the COPR SRPM already
   carries, and the script prints their SHA-256. The RIFE formula's third patch
   — `buttereye-vs-rife-ncnn-model-path.patch`, new in this phase and not
   committed yet — carries the checksum of the working-tree file and is marked
   in the formula as the one checksum the release step must recompute; the other
   two are checksums of committed blobs, verified against `v0.0.2`.

   Publishing the tarballs **needs an owner decision that this page does not
   pretend to have made.** `docs/SCOPE.md` §8.1 says "the COPR is the one
   distribution channel for third-party code"; a tap that conveys repacked RIFE
   source adds a second one. The *source duty* is met either way — the tarball is
   the Corresponding Source and travels with a checksum — but the policy sentence
   is the owner's to change.

   The alternative is worse rather than cleaner. The un-repacked upstream tag
   archive carries all 74 model directories, about 1.4 GB, several of them with
   no recorded provenance. `packaging/macos/build-rife.sh` avoids both by doing a
   blobless sparse `git clone` that takes the plugin plus the three curated model
   directories, verifies the commit before using any of it and checks every model
   file against `PROVENANCE-rife-ncnn-models.md` — and **no Homebrew download
   strategy can do that**, which is exactly why the formula needs a tarball
   someone has already repacked.

## What the tap does not convey, and who does

Everything below is a Homebrew bottle the **user** installs. Homebrew is the
distributor, carries the source duty, and ships each formula's own licence text in
its keg; `brew fetch --build-from-source <formula>` fetches the source it was
built from. ButterEye detects these and drives them, and never ships them.

| Component | Homebrew formula | Licence Homebrew declares | Relationship to ButterEye |
|---|---|---|---|
| mpv | `mpv` | GPL-2.0-or-later AND LGPL-2.1-or-later | separate program, driven over JSON IPC |
| VapourSynth and `vspipe` | `vapoursynth` | LGPL-2.1-or-later | separate program; its Python module is imported only in subprocesses |
| MVTools | `vapoursynth-mvtools` | GPL-2.0-or-later | plugin loaded inside mpv or vspipe |
| fftw | `fftw` | GPL-2.0-or-later AND BSD-2-Clause | shared library MVTools links. Measured: Homebrew's `mvtools.dylib` links `<prefix>/opt/fftw/lib/libfftw3f.3.dylib`. Both kegs are Homebrew's; ButterEye links neither |
| ffms2 | `ffms2` | GPL-2.0-or-later | the offline render's source filter, loaded inside vspipe. Its own source is MIT; Homebrew declares GPL because the bottle links Homebrew's GPL FFmpeg, which is the same reading `docs/SCOPE.md` §8.2 gives |
| FFmpeg | `ffmpeg` | GPL-3.0-or-later | separate program at the end of the render pipe |
| ncnn | `ncnn` | BSD-3-Clause | shared library `librife.dylib` links |
| libomp | `libomp` | MIT | shared library `librife.dylib` links |
| Vulkan loader | `vulkan-loader` | Apache-2.0 | shared library `libmvgpu.dylib` links |
| MoltenVK | `molten-vk` | Apache-2.0 | the Vulkan driver, reached through the loader's ICD at run time |
| PySide6 / Qt | `pyside` | (GPL-3.0-only WITH Qt-GPL-exception-1.0) AND (LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only) | imported into ButterEye's own process, which is the one in-process third-party import ButterEye has (`docs/SCOPE.md` §8.1). AGPL-3.0 §13 permits the combination |
| mkvtoolnix, vulkan-tools | `mkvtoolnix`, `vulkan-tools` | GPL-2.0-or-later …; Apache-2.0 | optional separate programs (`mkvmerge`, `vulkaninfo`) |

Two notes on that table rather than claims beyond it:

- The licence column is **what each Homebrew formula declares**, read from the
  formula metadata on an arm64 Mac on 2026-10-12. It is not an independent
  audit of those projects, and where it disagrees with `docs/SCOPE.md` §8.2 the
  difference is itself informative (ffms2 above is the example).
- **Not conveyed, permanently:** TensorRT, CUDA, `trtexec` and any prebuilt
  `vstrt`. On macOS this is not even a policy any more but a fact — there is no
  CUDA for Apple Silicon, `osapi.HAS_VSTRT` is `False`, and the generated script
  refuses the TensorRT branch before it looks for an engine.

## The VSScript library, and why the tap ships no symlink for it

Worth recording here because it looks like a packaging decision and is really a
measurement.

mpv 0.41.0 from Homebrew has the `vapoursynth` filter compiled in, but as shipped
the filter **fails**: it loads VSScript by `dlopen`-ing the bare name
`libvapoursynth-script.dylib`, while Homebrew's only copy of that library is
called `libvsscript.dylib` and lives inside VapourSynth's `libexec` virtual
environment.

The obvious packaging fix — have a formula drop a symlink named
`libvapoursynth-script.dylib` into `<prefix>/lib` — **does not work on Apple
Silicon, measured.** For a bare name, dyld searches `DYLD_LIBRARY_PATH`, then the
calling image's own `LC_RPATH` entries, then `/usr/lib` and the dyld cache. mpv's
only rpaths are two Swift toolchain paths, so `<prefix>/lib` is never looked at;
the full list mpv prints on failure contains no Homebrew path at all. (A Python
`ctypes` process *does* try `<prefix>/lib`, for the uninteresting reason that
`_ctypes.so` carries `LC_RPATH <prefix>/lib`. mpv does not.) On an Intel Mac
the prefix *is* `/usr/local`, so the answer there might differ — unmeasured, and
out of scope while the tap is arm64 only.

So the tap ships no such symlink, deliberately: it would be a fix that looks like
one without being one, and it would also mean one formula linking a file that
belongs to another formula's keg, which breaks on a `brew upgrade` of VapourSynth
and needs `link_overwrite` to install at all.

What does work, measured, is mpv's own `VSSCRIPT_PATH`, pointed at the library
file itself:

```sh
vs=$(brew --prefix vapoursynth)/libexec/lib/python3.14/site-packages/vapoursynth
export VSSCRIPT_PATH="$vs/libvsscript.dylib"
```

`brew --prefix` gives the versionless `opt` path, so this survives a VapourSynth
upgrade. ButterEye sets it itself for the mpv and `vspipe` processes it starts
(`osapi.use_bundled_runtime`, the macOS branch — the same variable the Windows
branch already sets), alongside `DYLD_FALLBACK_LIBRARY_PATH` carrying the
directory that library is already in, for an mpv that does not read
`VSSCRIPT_PATH`. Both point at files Homebrew owns: **ButterEye creates no
symlink and installs no library of its own**, so there is nothing here that a
`brew upgrade` of VapourSynth could leave dangling, and nothing extra in this
distribution to account for in a source offer. A user of `buttereye play` and
`buttereye render` therefore needs nothing; `buttereye.rb`'s caveats print the
two shell lines for anyone running `mpv --vf=vapoursynth=…` by hand.

One sharp edge, measured: when `VSSCRIPT_PATH` is set, mpv uses it and does
**not** fall back to the bare names — set to a path that does not exist, or to a
directory, it fails where leaving it unset would at least have tried. So it is
set only when the file has actually been found, which is what
`osapi.vsscript_library()` answers and what the doctor reports.

The honest long-term answer is upstream and not in this repository: either
Homebrew's `mpv` gains an rpath into its own prefix and Homebrew's `vapoursynth`
installs the conventional library name, or mpv learns to look in the prefix it was
built against. **A pull request to homebrew-core is the right fix**; ButterEye
sets the variable in the meantime.

## RIFE on a Mac is for offline render

Stated here because a source offer is also where someone checks what they are
getting. Measured on an M2 at 1080p: 8.5–9.8 fps, against roughly 48 fps for live
2× 24p playback. ncnn exposes one compute queue on this hardware, so `gpu_thread`
must be 1. `buttereye render` uses RIFE; live playback should not, and nothing in
the tap promises that it will.

And it is **not the same binary as the Linux one**. Built against Homebrew's
`ncnn` rather than the pinned static ncnn the Fedora package bundles, the output
differs: 21.3 % of samples differ at all and 1.8 % by more than one 8-bit step —
a thin halo on moving edges, not a wrong frame. `packaging/macos/RIFE-SPIKE.md`
has the full comparison and proves the cause is the ncnn version rather than any
ButterEye patch. Reproducibility is weaker than Fedora's for the same reason: the
tap pins a formula version, not bytes, so a Homebrew `ncnn` bump changes the
output with no change on ButterEye's side.

## Rebuilding any of it

```sh
brew tap <owner>/buttereye                    # once the tap is published
brew fetch --build-from-source buttereye      # the sources of this formula
brew edit buttereye                           # the recipe, which is also the build log
brew install --build-from-source buttereye    # build it yourself, no bottle
```

For the two plugins built out of this repository, without Homebrew at all:

```sh
packaging/macos/build-plugins.sh              # libmvgpu.dylib, then build-rife.sh
packaging/macos/check-dylibs.sh ~/.cache/buttereye/macos/vapoursynth
```

Those scripts write their own `licenses/`, `sources/` and `licenses/SOURCES.txt`
into the output, and `build-plugins.sh` refuses to finish a build that has a
`librife.dylib` without them.

**The tap and those scripts build the same plugin**, deliberately and checkably:
`buttereye-vs-rife-ncnn.rb` applies the same three patches in the same order (and
leaves out the Windows-only one), makes the same two `meson.build` edits, passes
the same `libomp` include and link flags, defaults to the same
`-Duse_system_ncnn=true`, and omits `-DVulkan_LIBRARY` for the same measured
reason. If one of them changes, the other has to. The one difference that cannot
be closed from a formula: `build-rife.sh` applies its patches with `patch -F0`,
so a hunk that only nearly matches fails instead of landing somewhere plausible,
and Homebrew's patch DSL has no way to ask for that.
