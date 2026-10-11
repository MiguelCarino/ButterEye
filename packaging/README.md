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
RIFE's Windows model loading for MinGW builds (Windows-only code).

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
(the Corresponding Source, with Fedora's `mingw-fftw` source package) and `SHA256SUMS`.
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
(Qt trimmed to Core, Gui, Widgets and Svg, with a check that every bundled DLL a
binary imports is still there; `winstage.py`), and makes the icon and the installer in
the chroot (`rsvg-convert`, `icotool`, Fedora's `makensis` from `mingw32-nsis`;
`windows/buttereye.nsi`). Output in `~/.cache/buttereye/windows/`:

| File | What |
|---|---|
| `ButterEye-<version>-setup.exe` | the installer (65 MiB; installs 165 MiB per user, no administrator rights) |
| `ButterEye-<version>-windows-sources.tar` | the Corresponding Source of everything built for it: ButterEye, the plugin and tool sources and patches, the build scripts |
| `SHA256SUMS` | every file above and the plugin build's |

Publish the sources tar next to the setup.exe: the installed `licenses\SOURCES.txt`
points there for the parts built here, and to the official source pages (with a written
offer) for Python, Qt/PySide6 and VapourSynth, which are bundled unmodified. ButterEye's
version comes from `buttereye/__init__.py`. The output is reproducible in its inputs
(everything pinned), not byte for byte (NSIS and the PE files carry build times).

Microsoft's Visual C++ runtime is not bundled: the installer offers to download it from
Microsoft when it is missing. Microsoft lets only licensed Visual Studio users
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
