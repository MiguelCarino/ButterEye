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

## Windows plugins (docs/PLATFORMS.md, phase 3)

```sh
packaging/windows/build-plugins.sh    # ~3 minutes; mock chroot with Fedora's MinGW-w64
tools/wine-tests.sh                   # loads them into VapourSynth for Windows under Wine
```

The same sources and patches as the Fedora packages (fetched and checked by
`build.sh --srpm-only`), cross-compiled into self-contained DLLs: the C++ runtime,
OpenMP, fftw and winpthreads are linked in, and the build fails if a DLL imports
anything but `KERNEL32.dll`, `msvcrt.dll` or `vulkan-1.dll` (which the GPU driver
installs). One extra patch, `plugins/buttereye-vs-rife-ncnn-windows-paths.patch`, fixes
RIFE's Windows model loading for MinGW builds (Windows-only code).

Output in `~/.cache/buttereye/windows/`: `vapoursynth/{mvtools,librife,libmvgpu}.dll`,
`share/rife-ncnn-models/`, `licenses/<component>/` (including fftw and the MinGW-w64
runtime, which are linked in), `components.json` (entries for the installer's
`buttereye-install.json`), `sources/` (the Corresponding Source, with Fedora's
`mingw-fftw` source package) and `SHA256SUMS`. Needs `mock` and network for the chroot
packages and the fftw source package.
