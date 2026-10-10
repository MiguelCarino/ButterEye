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
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import buttereye
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


def _source_link() -> str:
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


def _has(*words: str) -> Callable[[Path], bool]:
    return lambda p: any(w in p.name.lower() for w in words)


def _not(*words: str) -> Callable[[Path], bool]:
    return lambda p: not any(w in p.name.lower() for w in words)


COMPONENTS: tuple[Component, ...] = (
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
    Component("MVTools", pk.PKG_MVTOOLS, "GPL-2.0-or-later AND ISC", REL_LOADED, True, min_texts=1),
    Component("RIFE ncnn models", pk.PKG_MODELS, "MIT", REL_DATA, True, min_texts=4),
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
    Component("FFmpeg", "ffmpeg", "GPL-3.0-or-later", REL_SEPARATE, False, spdx_from_rpm=True),
    Component("ffms2", "ffms2", "MIT", REL_LOADED, False, spdx_from_rpm=True),
    Component(
        "mkvtoolnix",
        "mkvtoolnix",
        "GPL-2.0-or-later AND LGPL-2.1-or-later",
        REL_SEPARATE,
        False,
        spdx_from_rpm=True,
    ),
)


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
            M("Reinstall {pkg}.", {"pkg": c.package or ""}),
            (f"sudo dnf reinstall {c.package}",) if c.package else (),
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
    db: pk.RpmDb, ffmpeg_owner: pk.RpmInfo | None
) -> tuple[ComponentLicence, ...]:
    """Pure over the rpm query result (plus licence-file existence)."""
    out: list[ComponentLicence] = []
    for c in COMPONENTS:
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
        if c.name == "FFmpeg":
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
