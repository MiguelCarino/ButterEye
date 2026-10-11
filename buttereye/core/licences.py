# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Licence surfacing (SCOPE F18, §8.2, §8.4).

``legal_notices()`` returns ButterEye's own Appropriate Legal Notices (pure; it
only checks that the shipped files exist and lists the missing ones, never hides
them). ``third_party(paths)`` lists components with their SPDX expression, how
ButterEye uses them, whether ButterEye conveys them (the COPR rows of §8.2) and
the licence text files found on this system.

Providers (GUI.md §1)::

    legal_notices() -> LegalNotices
    async third_party(paths) -> tuple[ComponentLicence, ...]
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path

import buttereye
from buttereye.core import osapi
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import (
    ComponentLicence,
    Finding,
    LegalNotices,
    Msg,
    Paths,
    Section,
    Severity,
)

COPYRIGHT = "Copyright (C) 2026 The ButterEye contributors"
LICENCE_NAME = "GNU Affero General Public License v3.0 or later"
OUTPUT_PERMISSION = "LICENSES/AdditionRef-ButterEye-generated-output.txt"
#: Until a public repository exists the source is the COPR SRPM (SCOPE §9, F18).
SOURCE_FALLBACK = "dnf download --source buttereye"
NO_WARRANTY = M(
    "This program comes with ABSOLUTELY NO WARRANTY. It is free software, and you are "
    "welcome to redistribute it under the terms of the GNU Affero General Public License, "
    "version 3 or (at your option) any later version."
)

_SRC_ROOT = Path(__file__).resolve().parents[2]  # source tree / sdist root
if osapi.IS_WINDOWS:  # the installer puts ButterEye's own notices in <install>\licenses
    from buttereye.core.paths import install_dir as _install_dir

    _DOC_DIRS: tuple[Path, ...] = (_install_dir() / "licenses" / "buttereye", _install_dir())
else:
    _DOC_DIRS = (Path("/usr/share/licenses/buttereye"), Path("/usr/share/doc/buttereye"))
_SHADERS = Path(__file__).resolve().parents[1] / "data" / "shaders"  # bundled mpv shaders


def _shipped(rel: str, extra_roots: Sequence[Path] = ()) -> tuple[Path, bool]:
    """Where a shipped file is; (first candidate, False) when it is nowhere."""
    name = Path(rel).name
    candidates = [r / rel for r in extra_roots] + [_SRC_ROOT / rel]
    candidates += [d / name for d in _DOC_DIRS] + [d / rel for d in _DOC_DIRS]
    for c in candidates:
        if c.is_file():
            return c, True
    return candidates[0], False


def _source_link(windows: bool = osapi.IS_WINDOWS, install: Path | None = None) -> str:
    """Where the source is: on Windows the installer's source offer
    (licenses\\SOURCES.txt: the sources tar of what it built, the upstream sources
    of what it bundles unchanged), elsewhere the project's source URL."""
    if windows:
        if install is None:
            from buttereye.core.paths import install_dir

            install = install_dir()
        offer = install / "licenses" / "SOURCES.txt"
        if offer.is_file():
            return str(offer)
    try:
        urls = metadata.metadata("buttereye").get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        urls = []
    for u in urls:
        label, _, url = u.partition(",")
        if label.strip().lower() in ("source", "source code", "repository") and url.strip():
            return str(url.strip())
    return SOURCE_FALLBACK


def legal_notices(roots: Sequence[Path] = ()) -> LegalNotices:
    """Pure (filesystem existence checks only). ``roots`` lets tests add places."""
    agpl, agpl_ok = _shipped("COPYING", roots)
    perm, perm_ok = _shipped(OUTPUT_PERMISSION, roots)
    missing = tuple(p for p, ok in ((agpl, agpl_ok), (perm, perm_ok)) if not ok)
    return LegalNotices(
        name="ButterEye",
        version=buttereye.__version__,
        copyright=COPYRIGHT,
        licence_name=LICENCE_NAME,
        no_warranty=NO_WARRANTY,
        agpl_text=agpl,
        output_permission_text=perm,
        source_link=_source_link(),
        missing=missing,
    )


# ---------------------------------------------------------------------------
# Third-party components (§8.2)
# ---------------------------------------------------------------------------

REL_SEPARATE = M("separate program")
REL_LOADED = M("loaded by mpv/vspipe")
REL_STATIC = M("statically linked into the RIFE plugin")
REL_IN_PROCESS = M("in-process")
REL_DATA = M("data (model weights)")
REL_IMPORTED = M("imported by generated scripts inside mpv/vspipe")


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    package: str | None  # rpm name (None = bundled inside another package)
    spdx: str  # §8.2 expression (conveyed rows) or fallback for detected ones
    relation: Msg
    conveyed: bool
    min_texts: int = 0  # licence texts SCOPE §9 requires in %license
    #: picks this component's files out of the package's %license list
    pick: Callable[[Path], bool] | None = None
    #: always listed, even when not installed (else only when detected)
    always: bool = True
    spdx_from_rpm: bool = False
    #: shipped inside ButterEye itself: its licence texts, and the version shown
    bundled_files: tuple[Path, ...] = ()
    bundled_version: str | None = None
    #: linked into another package's binary: the provide that names its version
    #: (``bundled(ffmpeg)``); the version shown is that provide's
    provide: str | None = None


def _has(*words: str) -> Callable[[Path], bool]:
    return lambda p: any(w in p.name.lower() for w in words)


def _not(*words: str) -> Callable[[Path], bool]:
    return lambda p: not any(w in p.name.lower() for w in words)


REL_IN_FFMS2 = M("statically linked into ffms2")
REL_IN_MVTOOLS = M("statically linked into MVTools")
REL_IN_VULKANINFO = M("statically linked into vulkaninfo")


def components(windows: bool) -> tuple[Component, ...]:
    """The rows of the Licences tab. ``windows``: what the Windows installer
    conveys (its install record, buttereye-install.json, lists it like rpm would):
    PySide6, VapourSynth and ffms2 count as conveyed there (on Fedora they come
    from the distribution), and FFmpeg is the LGPL build inside ffms2.dll rather
    than a separate program."""
    if not windows:
        return _COMMON + _LINUX + _TAIL
    rows = _COMMON + _WINDOWS_FFMS2 + _TAIL + _WINDOWS
    return tuple(
        replace(c, conveyed=True) if c.package in _CONVEYED_ON_WINDOWS else c for c in rows
    )


#: on Windows the installer conveys these (on Fedora the distribution does)
_CONVEYED_ON_WINDOWS = ("python3-pyside6", "vapoursynth-libs", "ffms2")

_COMMON: tuple[Component, ...] = (
    Component(
        "RIFE-ncnn-Vulkan",
        pk.PKG_RIFE,
        "MIT AND WTFPL AND LGPL-2.1-or-later AND BSD-3-Clause",
        REL_LOADED,
        True,
        min_texts=2,
        pick=_not("ncnn", "glslang"),
    ),
    Component(
        "ncnn",
        pk.PKG_RIFE,
        "BSD-3-Clause AND BSD-2-Clause AND Zlib",
        REL_STATIC,
        True,
        min_texts=1,
        pick=_has("ncnn"),
    ),
    Component(
        "glslang",
        pk.PKG_RIFE,
        "BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND "
        "GPL-3.0-or-later WITH Bison-exception-2.2",
        REL_STATIC,
        True,
        min_texts=1,
        pick=_has("glslang"),
    ),
    Component(
        "MVTools",
        pk.PKG_MVTOOLS,
        "GPL-2.0-or-later AND ISC",
        REL_LOADED,
        True,
        min_texts=1,
        pick=_not("fftw"),
    ),
    Component("RIFE ncnn models", pk.PKG_MODELS, "MIT", REL_DATA, True, min_texts=4),
    Component(
        "mvgpu", pk.PKG_MVGPU, "AGPL-3.0-or-later", REL_LOADED, True, min_texts=1, always=False
    ),
    Component(
        "vsmlrt.py", pk.PKG_VSMLRT, "GPL-3.0-only", REL_IMPORTED, True, min_texts=1, always=False
    ),
    Component(
        "onnxconverter-common",
        pk.PKG_ONNXCONV,
        "MIT",
        REL_IMPORTED,
        True,
        min_texts=1,
        always=False,
    ),
    Component(
        "FSRCNNX x2 8-0-4-1 shader",
        None,
        "LGPL-3.0-or-later",
        REL_LOADED,
        True,
        bundled_files=(_SHADERS / "LGPL-3.0.txt", _SHADERS / "GPL-3.0.txt"),
        bundled_version="1.1 (bundled; Upscaling: Sharper)",
    ),
    Component(
        "PySide6 / Qt",
        "python3-pyside6",
        "LGPL-3.0-only OR GPL-3.0-only WITH Qt-GPL-exception-1.0",
        REL_IN_PROCESS,
        False,
        spdx_from_rpm=True,
    ),
    Component("mpv", "mpv", "GPL-2.0-or-later", REL_SEPARATE, False, spdx_from_rpm=True),
    Component(
        "VapourSynth",
        "vapoursynth-libs",
        "LGPL-2.1-or-later",
        REL_SEPARATE,
        False,
        spdx_from_rpm=True,
    ),
)

_LINUX: tuple[Component, ...] = (
    Component("FFmpeg", "ffmpeg", "GPL-3.0-or-later", REL_SEPARATE, False, spdx_from_rpm=True),
    Component("ffms2", "ffms2", "MIT", REL_LOADED, False, spdx_from_rpm=True),
)

_TAIL: tuple[Component, ...] = (
    Component(
        "mkvtoolnix",
        "mkvtoolnix",
        "GPL-2.0-or-later AND LGPL-2.1-or-later",
        REL_SEPARATE,
        False,
        spdx_from_rpm=True,
    ),
)

#: What the Windows installer adds that Fedora provides itself. ffms2.dll links
#: FFmpeg (an LGPL build), dav1d and zlib; mvtools.dll links fftw; vulkaninfo.exe
#: links volk: each is a row of its own, its version from the record's provides.
_WINDOWS_FFMS2: tuple[Component, ...] = (
    Component("ffms2", "ffms2", "MIT", REL_LOADED, True, min_texts=1, pick=_has("ffms2")),
    Component(
        "FFmpeg",
        "ffms2",
        "LGPL-2.1-or-later",
        REL_IN_FFMS2,
        True,
        min_texts=2,
        pick=_has("ffmpeg"),
        provide="bundled(ffmpeg)",
    ),
    Component(
        "dav1d",
        "ffms2",
        "BSD-2-Clause",
        REL_IN_FFMS2,
        True,
        min_texts=1,
        pick=_has("dav1d"),
        provide="bundled(dav1d)",
    ),
    Component(
        "zlib",
        "ffms2",
        "Zlib",
        REL_IN_FFMS2,
        True,
        min_texts=1,
        pick=_has("zlib"),
        provide="bundled(zlib)",
    ),
)

_WINDOWS: tuple[Component, ...] = (
    Component(
        "fftw",
        pk.PKG_MVTOOLS,
        "GPL-2.0-or-later",
        REL_IN_MVTOOLS,
        True,
        min_texts=1,
        pick=_has("fftw"),
        provide="bundled(fftw)",
    ),
    Component("Python", "python3", "PSF-2.0", REL_IN_PROCESS, True, spdx_from_rpm=True),
    Component(
        "Microsoft Visual C++ runtime (Python's copy)",
        "microsoft-vcruntime",
        "LicenseRef-Microsoft-Distributable-Code",
        REL_IN_PROCESS,
        True,
        min_texts=1,
        spdx_from_rpm=True,
    ),
    Component(
        "Shiboken6",
        "python3-shiboken6",
        "LGPL-3.0-only",
        REL_IN_PROCESS,
        True,
        spdx_from_rpm=True,
    ),
    Component(
        "Vulkan-Tools (vulkaninfo)",
        "vulkan-tools",
        "Apache-2.0",
        REL_SEPARATE,
        True,
        min_texts=1,
        pick=_not("volk"),
    ),
    Component(
        "volk",
        "vulkan-tools",
        "MIT",
        REL_IN_VULKANINFO,
        True,
        min_texts=1,
        pick=_has("volk"),
        provide="bundled(volk)",
    ),
    Component(
        "MinGW-w64 runtime",
        "mingw-w64-runtime",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        M("statically linked into the plugins and tools"),
        True,
        spdx_from_rpm=True,
    ),
)

COMPONENTS: tuple[Component, ...] = components(osapi.IS_WINDOWS)


def _licence_finding(
    c: Component, files: Sequence[Path], declared: Sequence[Path]
) -> Finding | None:
    missing = [p for p in declared if not p.is_file()]
    if missing:
        return Finding(
            f"licences.{c.name}",
            Section.PACKAGES,
            Severity.DEGRADED,
            ErrorCode.LICENCE_FILE_MISSING,
            M("Licence file not found for {name}", {"name": c.name}),
            M(
                "{n} licence file(s) listed by the package are missing. This is a packaging bug.",
                {"n": len(missing)},
            ),
            pk.REPAIR if osapi.IS_WINDOWS else M("Reinstall {pkg}.", {"pkg": c.package or ""}),
            (f"sudo dnf reinstall {c.package}",) if c.package and not osapi.IS_WINDOWS else (),
            tuple(str(p) for p in missing),
        )
    if c.conveyed and len(files) < c.min_texts:
        return Finding(
            f"licences.{c.name}",
            Section.PACKAGES,
            Severity.DEGRADED,
            ErrorCode.LICENCE_FILE_MISSING,
            M("Licence texts incomplete for {name}", {"name": c.name}),
            M(
                "{pkg} ships {have} licence text(s) for {name}; {need} are required. "
                "This is a packaging bug.",
                {"pkg": c.package or "", "have": len(files), "need": c.min_texts, "name": c.name},
            ),
            M("Report it to the ButterEye packagers."),
            (),
            tuple(str(p) for p in files),
        )
    return None


def build_third_party(
    db: pk.RpmDb,
    ffmpeg_owner: pk.RpmInfo | None,
    rows: Sequence[Component] = COMPONENTS,
) -> tuple[ComponentLicence, ...]:
    """Pure over the rpm query result (plus licence-file existence)."""
    out: list[ComponentLicence] = []
    for c in rows:
        if c.bundled_files:
            texts = tuple(p for p in c.bundled_files if p.is_file())
            out.append(
                ComponentLicence(
                    name=c.name,
                    version=c.bundled_version,
                    spdx=c.spdx,
                    relation=c.relation,
                    conveyed=c.conveyed,
                    detected=bool(texts),
                    text_files=texts,
                    finding=None,
                )
            )
            continue
        info = db.get(c.package) if c.package else None
        if c.package == "ffmpeg":
            info = ffmpeg_owner or db.get("ffmpeg") or db.get("ffmpeg-free")
        if info is None and not c.always:
            continue
        declared = info.licence_files if info else ()
        files = tuple(p for p in declared if c.pick is None or c.pick(p))
        version: str | None = info.evr if info else None
        if c.name == "ncnn" and info is not None:
            version = info.provide("bundled(ncnn)") or version
        if c.name == "glslang" and info is not None:
            version = info.provide("bundled(glslang)") or version
        if c.provide and info is not None:
            version = info.provide(c.provide)
        spdx = info.license if (info is not None and c.spdx_from_rpm and info.license) else c.spdx
        finding = _licence_finding(c, files, [p for p in declared if p in files]) if info else None
        out.append(
            ComponentLicence(
                name=c.name,
                version=version,
                spdx=spdx,
                relation=c.relation,
                conveyed=c.conveyed,
                detected=info is not None,
                text_files=tuple(p for p in files if p.is_file()),
                finding=finding,
            )
        )
    return tuple(out)


async def third_party(paths: Paths) -> tuple[ComponentLicence, ...]:
    names = tuple(dict.fromkeys(c.package for c in COMPONENTS if c.package))
    db, owner = await asyncio.gather(
        pk.rpm_query(names + ("ffmpeg-free",)), pk.rpm_owner("/usr/bin/ffmpeg")
    )
    return build_third_party(db, owner)


__all__ = [
    "COPYRIGHT",
    "LICENCE_NAME",
    "OUTPUT_PERMISSION",
    "SOURCE_FALLBACK",
    "COMPONENTS",
    "legal_notices",
    "build_third_party",
    "third_party",
]
