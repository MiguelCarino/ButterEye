<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Packaging (SCOPE §9)

| Spec | Package | Licence |
|---|---|---|
| `buttereye.spec` | `buttereye` (noarch application) | AGPL-3.0-or-later AND LGPL-3.0-or-later (bundled FSRCNNX shader) |
| `plugins/buttereye-vs-rife-ncnn.spec` | RIFE on ncnn/Vulkan (bundled static ncnn 305837fd + glslang a9ac7d5) | MIT AND LGPL-2.1-or-later AND WTFPL AND BSD-3-Clause AND BSD-2-Clause AND Zlib AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2 |
| `plugins/buttereye-rife-ncnn-models.spec` | RIFE v4.26, v4.22-lite, v4.18 ncnn models (noarch) | MIT |
| `plugins/buttereye-vs-mvtools.spec` | MVTools v29.2 | GPL-2.0-or-later AND ISC |

Plugins install into `%{_libdir}/buttereye/vapoursynth/` (not autoloaded) and models into
`%{_datadir}/buttereye/rife-ncnn-models/`. Every downloaded source is SHA-256-checked;
the RIFE tarballs are repacked reproducibly by `plugins/repack-rife-ncnn.sh` (no
upstream models beyond the curated three, no submodules). `plugins/licenses/` holds the
licence texts SCOPE §9 asks for that the upstream tarballs don't carry, pinned to
upstream commits; `plugins/PROVENANCE-rife-ncnn-models.md` records where each model
comes from, with checksums.

## Build

```sh
packaging/build.sh                    # all four, fedora-44 and fedora-45, mock, network off
packaging/build.sh --srpm-only        # just the SRPMs (for copr-cli build)
SPECS="buttereye" packaging/build.sh fedora-44-x86_64
```

Work and results: `~/.cache/buttereye/packaging/` (`SRPMS/`, `results/<chroot>/<package>/`).
Needs `mock` (and membership of the `mock` group), `rpm-build`, `rpmdevtools` (`spectool`)
and `python3 -m build`. Uploading to the COPR (`copr-cli build <owner>/buttereye *.src.rpm`
in a project created with `--enable-net off`) is the owner's step.
