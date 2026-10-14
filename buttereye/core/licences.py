# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Licence surfacing (SCOPE F18, §8.2, §8.4).

``legal_notices()`` returns ButterEye's own Appropriate Legal Notices (pure; it
only checks that the shipped files exist and lists the missing ones, never hides
them). ``third_party(paths)`` lists components with their SPDX expression, how
ButterEye uses them, whether ButterEye conveys them and the licence text files
found on this system. What is conveyed differs by platform, because the three
distributions do: the COPR rows of §8.2, what the Windows installer bundles, and
what the macOS Homebrew tap builds (``components``).

Providers (GUI.md §1)::

    legal_notices() -> LegalNotices
    async third_party(paths) -> tuple[ComponentLicence, ...]
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from importlib import metadata
from pathlib import Path
from types import MappingProxyType

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
    homebrew_prefix,
)

COPYRIGHT = "Copyright (C) 2026 The ButterEye contributors"
LICENCE_NAME = "GNU Affero General Public License v3.0 or later"
OUTPUT_PERMISSION = "LICENSES/AdditionRef-ButterEye-generated-output.txt"
#: Until a public repository exists the source is the COPR SRPM (SCOPE §9, F18).
SOURCE_FALLBACK = "dnf download --source buttereye"
#: macOS has no source RPM to download and no installer source offer to point at:
#: the offer is the repository itself, which is what SCOPE F18 asks for first ("the
#: public repository once it exists") and what the Project-URL below answers with
#: once ButterEye is installed as a distribution. Keep it in step with
#: pyproject.toml's ``Source``. Everything else ButterEye drives on a Mac comes from
#: Homebrew, where a formula is itself the build recipe and names the upstream source
#: that ``brew fetch --build-from-source <formula>`` fetches, next to the licence the
#: keg installs.
MACOS_SOURCE = "https://github.com/MiguelCarino/ButterEye"
#: What the macOS distribution adds (docs/PLATFORMS.md, macOS phase 4): a Homebrew
#: tap, whose own formulae build RIFE-ncnn-Vulkan, mvgpu and the models. A tap is a
#: source offer in a way a binary installer is not -- the formula is the build
#: recipe, it pins the exact upstream archive by checksum, and
#: ``brew fetch --build-from-source <formula>`` fetches that archive without building
#: it -- so the Corresponding Source of every tap component is named once, in
#: ``packaging/macos/SOURCES.md`` beside the formulae, and not duplicated here.
#: ButterEye prefers the copy the tap installs into its keg (below) to the repository
#: link, exactly as it prefers the Windows installer's ``SOURCES.txt``: a user who
#: has the binary should not have to find the project first to find its source.
MACOS_TAP_SOURCES = "share/doc/buttereye/SOURCES.md"
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


def _source_link(
    windows: bool = osapi.IS_WINDOWS,
    install: Path | None = None,
    macos: bool = osapi.IS_MACOS,
    prefix: Path | None = None,
) -> str:
    """Where the source is: on Windows and in ButterEye.app the build's source offer
    (licenses\\SOURCES.txt: the sources tar of what it built, the upstream sources
    of what it bundles unchanged), on macOS the one the Homebrew tap installs into
    its keg (``MACOS_TAP_SOURCES``, which names every tap component's source and the
    ``brew fetch`` line that retrieves it), elsewhere the project's source URL - and
    where there is no offer file and no distribution metadata to read a URL from (a
    checkout, or a Mac without the tap), the platform's own answer: the source RPM on
    Fedora, the repository on macOS, which has no SRPM to download.

    ``prefix``: Homebrew's, for a test with a planted keg."""
    if (
        windows
        or (macos and prefix is None and install is None and osapi.MACOS_APP is not None)
        or (not windows and not macos and install is None and osapi.LINUX_APP is not None)
    ):
        # ButterEye.app and the AppImage keep their offer where the Windows
        # installer does
        if install is None:
            from buttereye.core.paths import install_dir

            install = install_dir()
        offer = install / "licenses" / "SOURCES.txt"
        if offer.is_file():
            return str(offer)
    elif macos:
        base = homebrew_prefix() if prefix is None else prefix
        offer = base / MACOS_TAP_SOURCES
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
    return MACOS_SOURCE if macos else SOURCE_FALLBACK


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


def components(windows: bool, macos: bool = False) -> tuple[Component, ...]:
    """The rows of the Licences tab. ``windows``: what the Windows installer
    conveys (its install record, buttereye-install.json, lists it like rpm would):
    PySide6, VapourSynth and ffms2 count as conveyed there (on Fedora they come
    from the distribution), and FFmpeg is the LGPL build inside ffms2.dll rather
    than a separate program. ``macos``: what the Homebrew tap conveys, which is
    less than either (``_macos_rows``).

    ``macos`` defaults to False so that ``components(windows=False)`` is the Fedora
    table on every platform; the running platform's table is ``COMPONENTS``."""
    if macos:
        return _macos_rows()
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

# ---------------------------------------------------------------------------
# macOS: what the Homebrew tap conveys (packaging/macos/SOURCES.md)
# ---------------------------------------------------------------------------

#: The tap statically links nothing. Its RIFE formula passes
#: ``-Duse_system_ncnn=true`` and links Homebrew's ncnn and libomp as shared
#: libraries (``packaging/macos/homebrew/buttereye-vs-rife-ncnn.rb``,
#: ``packaging/macos/build-rife.sh``), so ``REL_STATIC`` - what the same two names
#: get on Fedora and Windows, where the bytes really are inside the plugin - would
#: be a false statement here.
REL_LINKED_RIFE = M("shared library the RIFE plugin links")

#: The Fedora rows, by name, so the macOS table can reuse the ones that are already
#: true there instead of copying them (a copy drifts; the same object cannot).
_BY_NAME: Mapping[str, Component] = MappingProxyType({c.name: c for c in _COMMON + _LINUX + _TAIL})


def _macos_rows() -> tuple[Component, ...]:
    """The rows of the Licences tab on macOS. ``packaging/macos/SOURCES.md`` is the
    authority for every one of them.

    What the tap conveys is what its own formulae build: ButterEye itself (with the
    mpv shader it bundles), librife.dylib, libmvgpu.dylib and the models. Everything
    else is a bottle the *user* installs from Homebrew's own formula, under that
    formula's licence and with its own upstream source, which
    ``brew fetch --build-from-source <formula>`` retrieves: Homebrew is the
    distributor and carries the source duty, and ButterEye only detects and drives
    it. Three Fedora rows have no macOS peer at all and are left out rather than
    re-related: glslang (the tap's RIFE build neither links nor ships it - it is the
    bundled variant's build-time shader compiler and nothing else), and vsmlrt.py
    and onnxconverter-common (both exist for the TensorRT engine, ``osapi.HAS_VSTRT``
    is False on macOS, and no tap formula provides either).
    """
    r = _BY_NAME
    return (
        # The tap's formula declares `license all_of: ["MIT", "LGPL-2.1-or-later",
        # "WTFPL"]`: the plugin's own MIT over the two headers it bundles. Fedora's
        # expression carries BSD-3-Clause on top of those, for the pinned ncnn that
        # package bundles and links in; here ncnn is a keg the user installs (next
        # row) rather than bytes in this binary. min_texts is 0 rather than Fedora's
        # 2 because the formula installs its four texts under
        # <keg>/share/doc/<formula>/licenses, while brew.BrewKeg.licence_files reads
        # the keg's root only: counting them would report a packaging bug against a
        # keg that has them. ``pick`` goes with the count - with no ncnn or glslang
        # row of their own, a bundled-ncnn build's two extra texts belong under the
        # plugin that linked them.
        replace(
            r["RIFE-ncnn-Vulkan"],
            spdx="MIT AND LGPL-2.1-or-later AND WTFPL",
            min_texts=0,
            pick=None,
        ),
        # Homebrew's ncnn keg: measured BSD-3-Clause on an arm64 Mac (2026-10-12),
        # and read from the keg itself whenever it is detected. The package name is
        # the formula, because that is what a user installs here.
        replace(
            r["ncnn"],
            package="ncnn",
            spdx="BSD-3-Clause",
            relation=REL_LINKED_RIFE,
            conveyed=False,
            min_texts=0,
            pick=None,
            spdx_from_rpm=True,
        ),
        # libomp has no Fedora row: Fedora's RIFE picks up libgomp from the base
        # system, while the tap has to link Homebrew's keg-only libomp explicitly
        # (build-rife.sh, the OpenMP flags). Measured MIT, which is what Homebrew
        # declares for it.
        Component("libomp", "libomp", "MIT", REL_LINKED_RIFE, False, spdx_from_rpm=True),
        # Homebrew's vapoursynth-mvtools, autoloaded: ButterEye builds no MVTools on
        # a Mac and conveys none, so this is the biggest "shipped" claim the Fedora
        # table would have made falsely - and the one row a working Mac really uses.
        # Homebrew declares GPL-2.0-or-later without Fedora's "AND ISC" (the ISC file
        # is x86inc.asm, which an arm64 build never compiles), and the keg's own
        # expression wins once it is detected, exactly as mpv's and ffms2's already do.
        replace(
            r["MVTools"],
            spdx="GPL-2.0-or-later",
            conveyed=False,
            min_texts=0,
            spdx_from_rpm=True,
        ),
        # Conveyed, and the source duty with them: both are ButterEye's own build
        # (buttereye-rife-ncnn-models.rb, buttereye-vs-mvgpu.rb). min_texts is 0 for
        # the same keg-layout reason as the RIFE row, not because the texts are
        # optional - the formulae install them, and SOURCES.md names the source of
        # each.
        replace(r["RIFE ncnn models"], min_texts=0),
        replace(r["mvgpu"], min_texts=0),
        # True on Fedora and still true here, so these are the Fedora objects. The
        # shader is bundled inside ButterEye itself, which is why the tap's buttereye
        # formula keeps LGPL-3.0-or-later. PySide6 stays "used": the tap depends on
        # Homebrew's pyside rather than a wheel. ffms2 and FFmpeg are already
        # conveyed=False on this arm, and on a Mac Homebrew's ffms2 keg reports
        # GPL-2.0-or-later (it links Homebrew's GPL FFmpeg) through spdx_from_rpm.
        r["FSRCNNX x2 8-0-4-1 shader"],
        r["PySide6 / Qt"],
        r["mpv"],
        r["VapourSynth"],
        r["FFmpeg"],
        r["ffms2"],
        r["mkvtoolnix"],
    )


REL_LINKED_MVTOOLS = M("shared library MVTools links")
REL_VULKAN_DRIVER = M("the Vulkan driver, loaded by the Vulkan loader")
REL_LINKED_VULKAN = M("shared library mpv and the GPU plugins link")


def _macos_app_rows() -> tuple[Component, ...]:
    """The rows of the Licences tab in ButterEye.app (packaging/macos/build-app.sh).

    The app *is* the distributor of everything in it, so every row it carries is
    conveyed - the reverse of the tap's table above, where Homebrew conveyed the
    bottles. The packages are the names in the app's install record
    (buttereye-install.json, written by build-app.sh): the Homebrew formula each
    bottle came from, and ButterEye's own names for what it built. Each row's
    expression is read from that record, which took it from the bottle's own SBOM.
    Every library mpv, FFmpeg and the plugins link is in the record and in
    ``licenses/`` too; the rows below are the ones a user drives or would look for,
    not all fifty.
    """
    r = _BY_NAME
    w: Mapping[str, Component] = {c.name: c for c in _WINDOWS}
    return (
        replace(
            r["RIFE-ncnn-Vulkan"], spdx="MIT AND LGPL-2.1-or-later AND WTFPL", pick=None
        ),
        replace(
            r["ncnn"],
            package="ncnn",
            spdx="BSD-3-Clause",
            relation=REL_LINKED_RIFE,
            pick=None,
            spdx_from_rpm=True,
        ),
        Component("libomp", "libomp", "MIT", REL_LINKED_RIFE, True, 1, spdx_from_rpm=True),
        replace(r["MVTools"], spdx="GPL-2.0-or-later", pick=None, spdx_from_rpm=True),
        Component(
            "fftw",
            "fftw",
            "GPL-2.0-or-later AND BSD-2-Clause",
            REL_LINKED_MVTOOLS,
            True,
            1,
            spdx_from_rpm=True,
        ),
        r["RIFE ncnn models"],
        r["mvgpu"],
        r["FSRCNNX x2 8-0-4-1 shader"],
        replace(r["PySide6 / Qt"], conveyed=True),
        w["Shiboken6"],
        w["Python"],
        replace(r["mpv"], conveyed=True, min_texts=1),
        replace(r["VapourSynth"], conveyed=True),
        Component(
            "FFmpeg", "ffmpeg", "GPL-3.0-or-later", REL_SEPARATE, True, 1, spdx_from_rpm=True
        ),
        Component("ffms2", "ffms2", "GPL-2.0-or-later", REL_LOADED, True, 1, spdx_from_rpm=True),
        Component(
            "MoltenVK", "molten-vk", "Apache-2.0", REL_VULKAN_DRIVER, True, 1, spdx_from_rpm=True
        ),
        Component(
            "Vulkan loader",
            "vulkan-loader",
            "Apache-2.0",
            REL_LINKED_VULKAN,
            True,
            1,
            spdx_from_rpm=True,
        ),
        replace(w["Vulkan-Tools (vulkaninfo)"], pick=None, spdx_from_rpm=True),
        r["mkvtoolnix"],
    )


COMPONENTS: tuple[Component, ...] = (
    _macos_app_rows()
    if osapi.MACOS_APP is not None
    else components(osapi.IS_WINDOWS, osapi.IS_MACOS)
)


#: Rows whose ``Component.package`` *is* a Homebrew formula name, so the advice can
#: carry a line to paste. The tap's own formulae are named after the rpms they stand
#: in for (``packaging/macos/homebrew/``), and the two shared libraries the macOS
#: table adds name their formula directly (``_macos_rows``). No other row can: MVTools
#: comes from Homebrew's ``vapoursynth-mvtools`` and Qt from its ``pyside``, and a
#: guessed formula name would send a user to a command that fails.
_BREW_FORMULA_ROWS = frozenset({pk.PKG_RIFE, pk.PKG_MVGPU, pk.PKG_MODELS, "ncnn", "libomp"})


def _reinstall(c: Component) -> tuple[Msg, tuple[str, ...]]:
    """How to get a component's licence texts back. Fedora reinstalls the rpm that
    declared them; on Windows the installer repairs itself. On macOS the texts live
    in the Homebrew keg the component came from: the rows whose package name is a
    formula name can be named (``_BREW_FORMULA_ROWS``), and for the rest the advice
    names the step without a line to paste rather than guessing which formula
    installed the component."""
    if osapi.IS_WINDOWS:
        return pk.REPAIR, ()
    if osapi.MACOS_APP is not None:
        return pk.APP_REPAIR, ()
    if osapi.LINUX_APP is not None:
        return pk.APPIMAGE_REPAIR, ()
    if osapi.IS_MACOS:
        if c.package in _BREW_FORMULA_ROWS:
            return (
                M("Reinstall {pkg}; its formula installs the licence texts.", {"pkg": c.package}),
                (f"brew reinstall {c.package}",),
            )
        return (
            M(
                "Reinstall the Homebrew formula that provides {name} (brew reinstall).",
                {"name": c.name},
            ),
            (),
        )
    return (
        M("Reinstall {pkg}.", {"pkg": c.package or ""}),
        (f"sudo dnf reinstall {c.package}",) if c.package else (),
    )


def _licence_finding(
    c: Component, files: Sequence[Path], declared: Sequence[Path]
) -> Finding | None:
    missing = [p for p in declared if not p.is_file()]
    if missing:
        fix, cmds = _reinstall(c)
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
            fix,
            cmds,
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
    "MACOS_SOURCE",
    "MACOS_TAP_SOURCES",
    "COMPONENTS",
    "legal_notices",
    "build_third_party",
    "third_party",
]
