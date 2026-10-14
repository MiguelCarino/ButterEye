# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Package checks (SCOPE F1 "Packages", F10) and render tools (ffms2, mkvmerge, 7z).

ButterEye never runs dnf: missing packages are reported with the exact lines to
run. ``rpm -q`` output is parsed by pure functions so fixtures can test them.

On Windows there is no package manager to ask: the installer writes an install
record (``buttereye-install.json``, see ``manifest_db``) next to ButterEye, and
it takes rpm's place. Fixes there say to run the installer again (Repair), and
tools ButterEye doesn't ship come with the ``winget`` line that installs them.

On macOS the install database is Homebrew's Cellar, read as files and never by
running ``brew`` (``brew.py`` says why, and what it measured); ``brew_db`` turns
it into the same ``RpmDb`` the rest of these checks already take, so a formula
answers for the package name that asks about it (``BREW_FORMULAE``). Fixes there
are ``brew install`` lines, and where Homebrew has no formula at all - RIFE-ncnn
and mvgpu, which ButterEye has to build itself - the finding says so instead of
offering a line that cannot work. No macOS finding carries a dnf, rpm, sudo,
copr or journal word (a test checks that, as one does for Windows).

Inside the Linux AppImage (a trial, ``osapi.LINUX_APP``) the install database is
the AppImage's own install record, as in ButterEye.app, and no finding offers a
dnf or COPR line: the AppImage may be running on any distribution, and what it
carries is repaired by downloading it again (``APPIMAGE_REPAIR``).
"""

from __future__ import annotations

import asyncio
import glob
import json
import re
import shlex
import shutil
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TypedDict

from buttereye.core import commands as _commands
from buttereye.core import osapi
from buttereye.core import paths as _paths
from buttereye.core.doctor import brew
from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import (
    Finding,
    Msg,
    Paths,
    Section,
    Severity,
    macos_plugin_dirs,
)

#: COPR project (SCOPE §9, owner decision Q5 pending: the FAS owner is not chosen yet).
COPR_PROJECT = "<owner>/buttereye"
COPR_ENABLE = f"sudo dnf copr enable {COPR_PROJECT}"

PKG_RIFE = "buttereye-vs-rife-ncnn"
PKG_MVTOOLS = "buttereye-vs-mvtools"
PKG_MODELS = "buttereye-rife-ncnn-models"
PKG_VSMLRT = "buttereye-vsmlrt-py"
PKG_ONNXCONV = "buttereye-onnxconverter-common"
PKG_MVGPU = "buttereye-vs-mvgpu"
BUTTEREYE_PKGS = (PKG_RIFE, PKG_MVTOOLS, PKG_MODELS, PKG_VSMLRT, PKG_ONNXCONV, PKG_MVGPU)
#: Other packages whose versions/licences doctor and the licence pane report.
OTHER_PKGS = (
    "mpv",
    "vapoursynth-libs",
    "vapoursynth-tools",
    "python3-pyside6",
    "ffms2",
    "mkvtoolnix",
    "vulkan-loader",
    "vulkan-tools",
    "7zip",
)

#: The Windows install record (``manifest_db``), in the install folder.
INSTALL_MANIFEST = "buttereye-install.json"
#: winget package ids (shared with ``commands.install_command``)
WINGET = _commands.WINGET

#: macOS: Homebrew's MVTools. It is not ButterEye's build - the version is
#: Homebrew's choice and the plugin is autoloaded - but it is a real MVTools that
#: interpolates on arm64 (docs/PLATFORMS.md), and it is the only interpolation
#: plugin a Mac user can install today, so it answers for ``PKG_MVTOOLS``.
FORMULA_MVTOOLS = "vapoursynth-mvtools"
#: macOS: the Homebrew formula that answers for each package name the checks ask
#: about. Read off the Cellar of an arm64 Mac (2026-10-12), not guessed; the tool
#: rows come from ``commands.BREW`` so there is one spelling of each. Absent on
#: purpose: ``PKG_RIFE``, ``PKG_MVGPU``, ``PKG_MODELS``, ``PKG_VSMLRT``,
#: ``PKG_ONNXCONV`` (Homebrew has no formula for any of them - ButterEye builds
#: those itself, phase 3) and ``python3-pyside6`` (Qt comes from the wheel, not
#: from Homebrew).
BREW_FORMULAE: Mapping[str, str] = MappingProxyType(
    {
        PKG_MVTOOLS: FORMULA_MVTOOLS,
        "mpv": _commands.BREW["mpv"],
        "vapoursynth-libs": _commands.BREW["vapoursynth"],
        "vapoursynth-tools": _commands.BREW["vapoursynth"],
        "ffmpeg": _commands.BREW["ffmpeg"],
        "mkvtoolnix": _commands.BREW["mkvtoolnix"],
        "7zip": _commands.BREW["7zip"],
        "ffms2": "ffms2",
        "vulkan-loader": "vulkan-loader",
        "vulkan-tools": "vulkan-tools",
        "molten-vk": "molten-vk",
    }
)

RIFE_SO = osapi.plugin_file("librife.so")
MVTOOLS_SO = osapi.plugin_file("mvtools.so")
MVGPU_SO = osapi.plugin_file("libmvgpu.so")
MODELS_SUBDIR = "rife-ncnn-models"


def plugin_dirs(paths: Paths) -> tuple[Path, ...]:
    """Every directory to look for an interpolation plugin in, in load order.

    The search path moved to ``paths.plugin_dirs`` (which says what it answers
    and why), because the engine chooser, the benchmark and the generated scripts
    all need the same answer and none of them may import the doctor. The two
    names stay here: eight checks below ask them, and the doctor is where the
    question was first asked.
    """
    return _paths.plugin_dirs(paths)


def installed_plugin(paths: Paths, name: str) -> Path | None:
    """``name`` where a load would find it, or None when no ``plugin_dirs`` has it.

    The same function the generated scripts resolve their one ``PLUGIN_DIR``
    with (``paths.plugin_dir_for``), so the doctor cannot report one copy while a
    script loads another.
    """
    return _paths.installed_plugin(paths, name)


def plugin_dir_text(paths: Paths) -> str:
    """``plugin_dirs`` for a message. Naming only the first would be the
    half-truth the search path exists to remove."""
    return ", ".join(str(d) for d in plugin_dirs(paths))


_QF = (
    "@@PKG\t%{NAME}\t%{VERSION}-%{RELEASE}\t%{LICENSE}\t%{INSTALLTIME}\n"
    "[@@PROV\t%{PROVIDENAME}\t%{PROVIDEVERSION}\n]"
    "[@@REQ\t%{REQUIRENAME}\n]"
    "[@@FILE\t%{FILEFLAGS:fflags}\t%{FILENAMES}\n]"
)


@dataclass(frozen=True, slots=True)
class RpmInfo:
    name: str
    evr: str  # version-release
    license: str
    provides: tuple[tuple[str, str], ...]
    requires: tuple[str, ...]
    licence_files: tuple[Path, ...]  # %license entries
    installtime: int | None = None  # %{INSTALLTIME}, epoch seconds (None: unknown)

    def provide(self, name: str) -> str | None:
        return next((v for n, v in self.provides if n == name), None)


@dataclass(frozen=True, slots=True)
class RpmDb:
    """Result of one ``rpm -q`` batch. ``available`` False: no rpm on this system."""

    available: bool
    packages: Mapping[str, RpmInfo]
    error: str | None = None

    def get(self, name: str) -> RpmInfo | None:
        return self.packages.get(name)


@dataclass(slots=True)
class _Builder:
    name: str
    evr: str
    license: str
    provides: list[tuple[str, str]]
    requires: list[str]
    lic: list[Path]
    installtime: int | None = None

    def build(self) -> RpmInfo:
        return RpmInfo(
            self.name,
            self.evr,
            self.license,
            tuple(self.provides),
            tuple(self.requires),
            tuple(self.lic),
            self.installtime,
        )


def parse_rpm_query(text: str) -> dict[str, RpmInfo]:
    """Parse the ``_QF`` output of ``rpm -q`` ("not installed" lines are ignored)."""
    out: dict[str, RpmInfo] = {}
    cur: _Builder | None = None
    for ln in text.splitlines():
        parts = ln.split("\t")
        tag = parts[0]
        if tag == "@@PKG" and len(parts) >= 4:
            if cur is not None:
                out.setdefault(cur.name, cur.build())
            when = parts[4].strip() if len(parts) >= 5 else ""
            cur = _Builder(
                parts[1],
                parts[2],
                parts[3],
                [],
                [],
                [],
                int(when) if when.isdigit() and int(when) > 0 else None,
            )
        elif cur is None:
            continue
        elif tag == "@@PROV" and len(parts) >= 2:
            cur.provides.append((parts[1], parts[2] if len(parts) > 2 else ""))
        elif tag == "@@REQ" and len(parts) >= 2:
            cur.requires.append(parts[1])
        elif tag == "@@FILE" and len(parts) >= 3 and "l" in parts[1]:
            cur.lic.append(Path(parts[2]))
    if cur is not None:
        out.setdefault(cur.name, cur.build())
    return out


def manifest_db(install: Path) -> RpmDb:
    """The Windows install record as a package database.

    ``buttereye-install.json``, written by the installer::

        {"version": 1, "components": [
            {"name": "buttereye-vs-rife-ncnn", "version": "9.33-1", "license": "MIT",
             "provides": [["bundled(ncnn)", "20250916"]],
             "licence_files": ["licenses/buttereye-vs-rife-ncnn/LICENSE"]}, ...]}

    ``licence_files`` are relative to the install folder. A missing or unreadable
    record reads as "can't be checked", like a system without rpm."""
    path = install / INSTALL_MANIFEST
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RpmDb(False, {}, f"no install record ({INSTALL_MANIFEST}) in {install}")
    except (OSError, ValueError) as exc:
        return RpmDb(False, {}, f"the install record can't be read: {exc}")
    if not isinstance(data, dict) or not isinstance(data.get("components"), list):
        return RpmDb(False, {}, "the install record has no component list")
    out: dict[str, RpmInfo] = {}
    for c in data["components"]:
        if not isinstance(c, dict) or not isinstance(c.get("name"), str):
            continue
        provides = tuple(
            (str(x[0]), str(x[1]))
            for x in c.get("provides") or ()
            if isinstance(x, list) and len(x) == 2
        )
        files = tuple(install / str(f) for f in c.get("licence_files") or () if isinstance(f, str))
        out[c["name"]] = RpmInfo(
            c["name"], str(c.get("version", "")), str(c.get("license", "")), provides, (), files
        )
    return RpmDb(True, out)


def brew_db(names: Iterable[str] = BUTTEREYE_PKGS + OTHER_PKGS) -> RpmDb:
    """macOS: Homebrew's Cellar as a package database (``brew.installed``).

    Each name is looked up through ``BREW_FORMULAE``, so ``buttereye-vs-mvtools``
    answers with the ``vapoursynth-mvtools`` keg and a name Homebrew has no
    formula for is simply not installed - the same answer rpm gives for a package
    that isn't there. ``RpmInfo.name`` is the formula, because that is what the
    user would install and what a finding should name; the key is the package name
    the check asked about.

    ``evr`` is the keg's version (Homebrew has no release field), ``license`` the
    SPDX expression from the keg's SBOM, ``licence_files`` the licence texts in
    the keg's root, ``requires`` its runtime dependencies and ``installtime`` the
    receipt's install time, which is what ``gpufault`` splits the log at.
    ``provides`` stays empty: Homebrew has no provides, and the only one these
    checks read (``bundled(ncnn)``) belongs to a plugin ButterEye builds itself.

    No Cellar at all means no Homebrew, which is "can't be checked" rather than
    "nothing installed": a Mac without Homebrew has no mpv or VapourSynth either,
    and saying every package is missing would bury that one fact in nine rows.
    """
    if not brew.cellar().is_dir():
        return RpmDb(False, {}, f"Homebrew is not installed in {brew.prefix()}")
    wanted = {n: BREW_FORMULAE[n] for n in names if n in BREW_FORMULAE}
    kegs = brew.installed(dict.fromkeys(wanted.values()))
    out: dict[str, RpmInfo] = {}
    for name, formula in wanted.items():
        keg = kegs.get(formula)
        if keg is None:
            continue
        out[name] = RpmInfo(
            name=keg.name,
            evr=keg.version,
            license=keg.licence(),
            provides=(),
            requires=tuple(d for d, _ in keg.deps),
            licence_files=keg.licence_files(),
            installtime=keg.installed_at,
        )
    return RpmDb(True, out)


async def rpm_query(names: Iterable[str] = BUTTEREYE_PKGS + OTHER_PKGS) -> RpmDb:
    """What is installed: ``rpm -q`` on Linux, the install record on Windows,
    Homebrew's Cellar on macOS (no subprocess there, so nothing to await)."""
    if osapi.IS_WINDOWS:
        from buttereye.core.paths import install_dir

        return manifest_db(install_dir())
    if osapi.IS_MACOS:
        if osapi.MACOS_APP is not None:
            # ButterEye.app: its own install record, as on Windows. Homebrew,
            # if this Mac has it, installed nothing the app runs.
            from buttereye.core.paths import install_dir

            return manifest_db(install_dir())
        return brew_db(names)
    if osapi.LINUX_APP is not None:
        # the AppImage: its own install record. An rpm database on the same
        # machine (Fedora) describes nothing the AppImage runs.
        from buttereye.core.paths import install_dir

        return manifest_db(install_dir())
    if shutil.which("rpm") is None:
        return RpmDb(False, {}, "rpm is not installed")
    res = await run(("rpm", "-q", "--qf", _QF, *names), timeout_s=6.0)
    if res.missing or res.timed_out:
        return RpmDb(False, {}, "rpm timed out" if res.timed_out else "rpm is not installed")
    return RpmDb(True, parse_rpm_query(res.stdout))


async def rpm_owner(path: str) -> RpmInfo | None:
    """Package that owns ``path`` (e.g. /usr/bin/ffmpeg -> ffmpeg-free)."""
    if shutil.which("rpm") is None:
        return None
    res = await run(("rpm", "-qf", "--qf", _QF, path), timeout_s=4.0)
    if not res.ok:
        return None
    pkgs = parse_rpm_query(res.stdout)
    return next(iter(pkgs.values()), None)


def rife_variant(info: RpmInfo | None) -> tuple[str | None, bool]:
    """('bundled ncnn <ver>' | 'Fedora ncnn' | None, is_system_ncnn)."""
    if info is None:
        return None, False
    bundled = info.provide("bundled(ncnn)")
    if bundled is not None:
        return f"bundled ncnn {bundled}".strip(), False
    if any(r.startswith("libncnn") for r in info.requires):
        return "Fedora ncnn", True
    return None, False


def installed_models(paths: Paths) -> tuple[str, ...]:
    d = paths.rpm_data_dir / MODELS_SUBDIR
    try:
        return tuple(sorted(p.name for p in d.iterdir() if p.is_dir()))
    except OSError:
        return ()


def _dnf(*pkgs: str) -> tuple[str, ...]:
    return (COPR_ENABLE, "sudo dnf install " + " ".join(pkgs))


def _brew(*formulae: str, verb: str = "install") -> tuple[str, ...]:
    """The one Homebrew line that installs (or reinstalls) these formulae.
    ButterEye only shows it; the formula names are read off a real Cellar
    (``BREW_FORMULAE``), never built from a package name."""
    return (f"brew {verb} " + " ".join(formulae),)


#: Windows: ButterEye's own pieces come only from its installer
REPAIR = M("Run the ButterEye installer again and choose Repair.")
#: macOS, running from ButterEye.app: everything the doctor checks came inside the
#: app, so the one repair is a fresh copy of it - there is no package manager to
#: name and nothing to install beside it (``osapi.MACOS_APP``)
APP_REPAIR = M("Download ButterEye again and replace ButterEye.app with the new copy.")
#: the tools ``tool_fix`` names that ButterEye.app carries itself
APP_CARRIES = frozenset({"mpv", "vapoursynth", "ffmpeg"})
#: the Linux AppImage's peer of ``APP_REPAIR``: it carries mpv, VapourSynth,
#: FFmpeg and MVTools, so the one repair is a fresh copy (``osapi.LINUX_APP``)
APPIMAGE_REPAIR = M("Download the ButterEye AppImage again and replace the old file with it.")
#: the AppImage never carries RIFE or mvgpu, so a fresh copy cannot bring them:
#: say where they are instead of a repair that does nothing
APPIMAGE_NO_GPU = M(
    "The AppImage smooths with MVTools only. For RIFE and mvgpu on the GPU, use "
    "ButterEye's Fedora packages."
)
#: macOS: ButterEye has no macOS package of its own yet (phase 3 builds the
#: plugins, phase 4 taps them), and Homebrew has no formula for RIFE-ncnn-Vulkan
#: or mvgpu, so for ButterEye's own pieces there is nothing to type. Saying that
#: is the honest fix; offering an install line that cannot work is not.
NO_MACOS_PACKAGE = M(
    "ButterEye has no macOS package yet, so there is nothing to install or repair here."
)
#: macOS: the one interpolation plugin a Mac user can install today. Homebrew has
#: no RIFE-ncnn-Vulkan formula (``brew search`` finds none), so the advice names
#: MVTools and says what the other half is, instead of a line that would fail.
MACOS_MVTOOLS_FIX = M("Install MVTools from Homebrew; RIFE-ncnn has no macOS package yet.")


class Fix(TypedDict):
    fix: Msg
    commands: tuple[str, ...]


def platform_fix(
    fix: Msg,
    commands: tuple[str, ...],
    *,
    windows: Msg = REPAIR,
    macos: Msg = NO_MACOS_PACKAGE,
    macos_commands: tuple[str, ...] = (),
    appimage: Msg = APPIMAGE_REPAIR,
) -> Fix:
    """``fix=``/``commands=`` for a finding: the Fedora advice with its commands,
    on Windows ``windows`` (by default the installer's Repair) with nothing to
    type, and on macOS ``macos`` with ``macos_commands`` - ``brew`` lines where
    Homebrew has the formula, and by default nothing to type, because what
    ButterEye packages itself has no macOS package yet (``NO_MACOS_PACKAGE``).
    The Fedora tail is last and untouched, so Linux keeps its exact words."""
    if osapi.IS_WINDOWS:
        return {"fix": windows, "commands": ()}
    if osapi.IS_MACOS:
        if osapi.MACOS_APP is not None:
            return {"fix": APP_REPAIR, "commands": ()}
        return {"fix": macos, "commands": macos_commands}
    if osapi.LINUX_APP is not None:
        return {"fix": appimage, "commands": ()}
    return {"fix": fix, "commands": commands}


def tool_fix(
    fix: Msg,
    dnf_line: str,
    winget_key: str,
    *,
    windows: Msg | None = None,
    macos: Msg | None = None,
    verb: str = "install",
) -> Fix:
    """``fix=``/``commands=`` for a tool ButterEye uses but doesn't ship: the
    ``dnf`` line, on Windows the ``winget`` one and on macOS the ``brew`` one
    (``verb``: install/upgrade), with ``windows``/``macos`` as the advice there
    when the Linux words don't fit.

    The line itself comes from ``commands.install_command``, the one table that
    names each of these tools on each platform, so the three agree by
    construction and ``winget_key`` is the key for all of them."""
    if osapi.IS_WINDOWS:
        line = f"winget {verb} --exact --id {WINGET[winget_key]}"
        return {"fix": windows or fix, "commands": (line,)}
    if osapi.IS_MACOS:
        if osapi.MACOS_APP is not None and winget_key in APP_CARRIES:
            return {"fix": APP_REPAIR, "commands": ()}
        return {
            "fix": macos or fix,
            "commands": (_commands.install_command(winget_key, verb=verb),),
        }
    if osapi.LINUX_APP is not None:
        # what the AppImage carries comes back with a fresh copy; anything else
        # is the host distribution's, and the AppImage may be running on any of
        # them: the advice stays, the Fedora command line does not
        if winget_key in APP_CARRIES:
            return {"fix": APPIMAGE_REPAIR, "commands": ()}
        return {"fix": fix, "commands": ()}
    return {"fix": fix, "commands": (dnf_line,)}


def _f(
    fid: str,
    sev: Severity,
    title: Msg,
    *,
    section: Section = Section.PACKAGES,
    code: ErrorCode | None = None,
    cause: Msg | None = None,
    fix: Msg | None = None,
    commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    experimental: bool = False,
) -> Finding:
    return Finding(
        id=fid,
        section=section,
        severity=sev,
        code=code,
        title=title,
        cause=cause or M(""),
        fix=fix or M(""),
        commands=commands,
        evidence=evidence,
        experimental=experimental,
    )


def mvgpu_finding(db: RpmDb, paths: Paths) -> Finding:
    """mvgpu, the optional fast GPU engine: OK when its plugin file is in the
    private plugin directory, a note (INFO) when it isn't; RIFE and MVTools
    cover everything without it."""
    so = installed_plugin(paths, MVGPU_SO)
    if so is not None:
        info = db.get(PKG_MVGPU)
        return _f(
            "packages.mvgpu",
            Severity.OK,
            M("GPU motion-vector plugin (mvgpu) installed"),
            cause=M(
                "{pkg} {ver}",
                # the installed package's own name: on macOS a plugin may come
                # from a Homebrew formula, which is not the RPM's name
                {"pkg": info.name if info else PKG_MVGPU, "ver": info.evr if info else "found"},
            ),
            evidence=(str(so),),
        )
    return _f(
        "packages.mvgpu",
        Severity.INFO,
        M("The GPU motion-vector plugin (mvgpu) is not installed"),
        cause=M(
            "mvgpu smooths 4K at high rates on the GPU where RIFE can't keep up; "
            "without it, ButterEye smooths a smaller picture or uses MVTools there.",
        ),
        **platform_fix(
            M("Install it from the ButterEye COPR if you want it."),
            (f"sudo dnf install {PKG_MVGPU}",),
            windows=M("Run the ButterEye installer again and include it."),
            # mvgpu is ButterEye's own Vulkan plugin and there is no Homebrew
            # formula for it; it runs on MoltenVK bar one instance flag
            # (docs/PLATFORMS.md), so this is "not yet", not "never".
            macos=M("ButterEye doesn't build mvgpu for macOS yet, so it can't be installed."),
            appimage=APPIMAGE_NO_GPU,
        ),
    )


def _unreadable_db_finding(db: RpmDb) -> Finding:
    """The note for "the install database can't be read at all": no rpm on
    Linux, no install record on Windows, no Homebrew on macOS. Each platform
    says what is missing and what would fix it, and nothing else."""
    if osapi.IS_WINDOWS or osapi.MACOS_APP is not None or osapi.LINUX_APP is not None:
        return _f(
            "packages.rpm",
            Severity.INFO,
            M("ButterEye's install record can't be read"),
            cause=M("{why}", {"why": db.error or "unknown"}),
            fix=REPAIR
            if osapi.IS_WINDOWS
            else APP_REPAIR
            if osapi.MACOS_APP is not None
            else APPIMAGE_REPAIR,
        )
    if osapi.IS_MACOS:
        return _f(
            "packages.rpm",
            Severity.INFO,
            M("Installed packages can't be checked: Homebrew isn't installed"),
            cause=M("{why}", {"why": db.error or "unknown"}),
            fix=M(
                "mpv, VapourSynth and the MVTools plugin come from Homebrew on macOS. "
                "Install Homebrew, then run the checks again."
            ),
        )
    return _f(
        "packages.rpm",
        Severity.INFO,
        M("Installed packages can't be checked on this system"),
        cause=M("rpm is not available: {why}", {"why": db.error or "unknown"}),
        fix=M("ButterEye supports Fedora 44 and 45 with its COPR packages."),
    )


def app_quarantine_finding(resources: Path) -> Finding | None:
    """ButterEye.app, downloaded and not yet cleared: macOS holds each of its
    programs at first start until the user answers a prompt
    (``osapi.app_quarantined``), mpv and vspipe included. One command clears
    the whole app; it is the same one the README gives for a first start."""
    held = osapi.app_quarantined(resources)
    if not held:
        return None
    app = resources.parent.parent
    return _f(
        "packages.app_quarantine",
        Severity.BLOCKING,
        M("macOS is still holding ButterEye's programs back"),
        cause=M(
            "ButterEye.app came from a download, so macOS asks before each program in "
            "it runs for the first time - mpv, vspipe and ffmpeg too, which ButterEye "
            "starts in the background, where the question can go unseen and playback "
            "waits for it."
        ),
        fix=M("Run this once in Terminal, then start ButterEye again."),
        commands=(f"xattr -dr com.apple.quarantine {shlex.quote(str(app))}",),
        evidence=tuple(str(p) for p in held),
    )


def package_findings(db: RpmDb, paths: Paths) -> list[Finding]:
    """Pure over the rpm result plus file existence in the private directories."""
    out: list[Finding] = []
    if osapi.MACOS_APP is not None:
        held = app_quarantine_finding(osapi.MACOS_APP)
        if held is not None:
            out.append(held)
    if not db.available:
        rife_file = installed_plugin(paths, RIFE_SO) is not None
        mv_file = installed_plugin(paths, MVTOOLS_SO) is not None
        out.append(_unreadable_db_finding(db))
        if not rife_file and not mv_file:
            out.append(
                _f(
                    "packages.plugins",
                    Severity.BLOCKING,
                    M("No interpolation plugin is installed"),
                    code=ErrorCode.PKG_MISSING,
                    cause=M(
                        "Neither {a} nor {b} was found in {d}.",
                        {"a": RIFE_SO, "b": MVTOOLS_SO, "d": plugin_dir_text(paths)},
                    ),
                    **platform_fix(
                        M("Install the ButterEye plugin packages."),
                        _dnf(PKG_MVTOOLS, PKG_RIFE, PKG_MODELS),
                        macos=MACOS_MVTOOLS_FIX,
                        macos_commands=_brew(FORMULA_MVTOOLS),
                    ),
                )
            )
        out.append(mvgpu_finding(db, paths))
        return out

    rife = db.get(PKG_RIFE)
    mv = db.get(PKG_MVTOOLS)
    models_pkg = db.get(PKG_MODELS)
    # the file each one would actually load, so the row's evidence names the copy
    # in play rather than a directory the plugin only might be in
    rife_so = installed_plugin(paths, RIFE_SO)
    mv_so = installed_plugin(paths, MVTOOLS_SO)
    rife_ok = rife is not None and rife_so is not None
    mv_ok = mv is not None and mv_so is not None

    if rife is not None and rife_so is not None:
        variant, system = rife_variant(rife)
        if system:
            out.append(
                _f(
                    "packages.rife",
                    Severity.DEGRADED,
                    M("RIFE plugin is the Fedora-ncnn build"),
                    code=ErrorCode.VARIANT_SYSTEM_NCNN,
                    cause=M(
                        "{pkg} {ver} links Fedora's ncnn, which faulted the GPU in testing "
                        "whenever more than one GPU thread was used.",
                        {"pkg": PKG_RIFE, "ver": rife.evr},
                    ),
                    fix=M("Install the default (bundled ncnn) build from the ButterEye COPR."),
                    commands=(f"sudo dnf reinstall {PKG_RIFE}",),
                )
            )
        else:
            out.append(
                _f(
                    "packages.rife",
                    Severity.OK,
                    M("RIFE plugin: {variant}", {"variant": variant or "variant unknown"}),
                    cause=M("{pkg} {ver}", {"pkg": rife.name, "ver": rife.evr}),
                    evidence=(str(rife_so),),
                )
            )
    else:
        out.append(
            _f(
                "packages.rife",
                # the AppImage is MVTools by design: a note, not a fault to fix
                Severity.INFO if osapi.LINUX_APP is not None else Severity.DEGRADED,
                M("The RIFE plugin is not installed"),
                code=ErrorCode.PKG_MISSING,
                # the Fedora row names the RPM, which is what a user would install;
                # on macOS there is no package of any name to name, so saying one
                # would send them looking for something that does not exist
                cause=(
                    M("Without a RIFE plugin, only MVTools (CPU) interpolation is possible.")
                    if osapi.IS_MACOS
                    else M(
                        "Without {pkg}, only MVTools (CPU) interpolation is possible.",
                        {"pkg": PKG_RIFE},
                    )
                ),
                **platform_fix(
                    M("Install the RIFE plugin and its models from the ButterEye COPR."),
                    _dnf(PKG_RIFE, PKG_MODELS),
                    # no Homebrew formula for RIFE-ncnn-Vulkan and none for the
                    # models; ButterEye has to build both (phase 3)
                    macos=M(
                        "RIFE has no macOS package yet, so only MVTools (CPU) smoothing "
                        "works here."
                    ),
                    appimage=APPIMAGE_NO_GPU,
                ),
            )
        )

    if mv is not None and mv_so is not None:
        out.append(
            _f(
                "packages.mvtools",
                Severity.OK,
                M("MVTools plugin installed"),
                cause=M("{pkg} {ver}", {"pkg": mv.name, "ver": mv.evr}),
                evidence=(str(mv_so),),
            )
        )
    else:
        out.append(
            _f(
                "packages.mvtools",
                Severity.DEGRADED,
                M("The MVTools plugin is not installed"),
                code=ErrorCode.PKG_MISSING,
                cause=M("MVTools is the CPU fallback when RIFE can't run."),
                **platform_fix(
                    M("Install MVTools from the ButterEye COPR."),
                    _dnf(PKG_MVTOOLS),
                    macos=M("Install MVTools from Homebrew."),
                    macos_commands=_brew(FORMULA_MVTOOLS),
                ),
            )
        )

    models = installed_models(paths)
    if rife_ok:
        if models_pkg is not None and models:
            out.append(
                _f(
                    "packages.models",
                    Severity.OK,
                    M("{n} RIFE models installed", {"n": len(models)}),
                    cause=M("{pkg} {ver}", {"pkg": models_pkg.name, "ver": models_pkg.evr}),
                    evidence=models,
                )
            )
        else:
            out.append(
                _f(
                    "packages.models",
                    Severity.DEGRADED,
                    M("No RIFE models are installed"),
                    code=ErrorCode.PKG_MISSING,
                    cause=M(
                        "The RIFE plugin needs the packaged models in {d}.",
                        {"d": str(paths.rpm_data_dir / MODELS_SUBDIR)},
                    ),
                    **platform_fix(
                        M("Install the RIFE model package."),
                        _dnf(PKG_MODELS),
                        macos=M("The RIFE models come with ButterEye's own macOS package."),
                    ),
                    evidence=models,
                )
            )

    out.append(mvgpu_finding(db, paths))

    if not rife_ok and not mv_ok:
        out.append(
            _f(
                "packages.plugins",
                Severity.BLOCKING,
                M("No interpolation plugin is installed"),
                code=ErrorCode.PKG_MISSING,
                cause=M("ButterEye needs RIFE-ncnn or MVTools to interpolate."),
                **platform_fix(
                    M("Enable the ButterEye COPR and install the plugin packages."),
                    _dnf(PKG_MVTOOLS, PKG_RIFE, PKG_MODELS),
                    macos=MACOS_MVTOOLS_FIX,
                    macos_commands=_brew(FORMULA_MVTOOLS),
                ),
            )
        )

    out.append(licence_files_finding(db))
    return out


# ---------------------------------------------------------------------------
# Windows: Microsoft's Visual C++ runtime
# ---------------------------------------------------------------------------

#: VapourSynth's own minimum (its ``_check_visual_studio_runtime``)
VCRUNTIME_MIN = (14, 50, 35719)
VCRUNTIME_KEY = r"SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
VCREDIST_WINGET = "Microsoft.VCRedist.2015+.x64"


def read_vcruntime() -> tuple[int, int, int] | None:
    """Windows: the installed x64 Visual C++ runtime (major, minor, build), read
    from the 64-bit registry view; None when it isn't installed (or elsewhere)."""
    if sys.platform != "win32":
        return None
    import winreg

    access = winreg.KEY_READ | winreg.KEY_WOW64_64KEY
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, VCRUNTIME_KEY, 0, access) as key:
            if winreg.QueryValueEx(key, "Installed")[0] != 1:
                return None
            major, minor, build = (
                int(winreg.QueryValueEx(key, n)[0]) for n in ("Major", "Minor", "Bld")
            )
    except OSError, TypeError, ValueError:
        return None
    return major, minor, build


def vcruntime_finding(version: tuple[int, int, int] | None) -> Finding:
    """Windows: VapourSynth (and so every smoothing path) needs Microsoft's Visual
    C++ runtime, at least ``VCRUNTIME_MIN``; the window's Qt uses it too (the
    installer ships no copy of its own but Python's vcruntime140). ButterEye's
    installer offers to install it; it can't be bundled inside ButterEye's own
    folder because mpv loads VapourSynth into its own process."""
    need = ".".join(map(str, VCRUNTIME_MIN))
    if version is not None and version >= VCRUNTIME_MIN:
        return _f(
            "packages.vcruntime",
            Severity.OK,
            M("Microsoft Visual C++ runtime installed"),
            cause=M("{v}", {"v": ".".join(map(str, version))}),
        )
    verb = "install" if version is None else "upgrade"
    return _f(
        "packages.vcruntime",
        Severity.BLOCKING,
        M("Microsoft's Visual C++ runtime is missing")
        if version is None
        else M("Microsoft's Visual C++ runtime is too old"),
        code=ErrorCode.PKG_MISSING,
        cause=M(
            "VapourSynth needs the Visual C++ 2015-2026 runtime {need} or newer, "
            "and it isn't installed.",
            {"need": need},
        )
        if version is None
        else M(
            "VapourSynth needs the Visual C++ 2015-2026 runtime {need} or newer; "
            "this PC has {have}.",
            {"need": need, "have": ".".join(map(str, version))},
        ),
        fix=M("Install it (Windows asks for permission once), then run the checks again."),
        commands=(f"winget {verb} --exact --id {VCREDIST_WINGET}",),
    )


def licence_files_finding(db: RpmDb) -> Finding:
    """The licence texts of the installed ButterEye packages are really on disk
    (SCOPE F10). On macOS the packages are Homebrew formulae and the texts are the
    ``LICENSE``/``COPYING`` files in the keg's root (``brew.BrewKeg.licence_files``),
    so the check has the same shape; only rpm's ``%license`` word is dropped,
    because Homebrew has no such tag."""
    missing: list[str] = []
    checked = 0
    for name in BUTTEREYE_PKGS:
        info = db.get(name)
        if info is None:
            continue
        checked += 1
        if not info.licence_files:
            missing.append(
                f"{info.name}: no licence file in the keg"
                if osapi.IS_MACOS
                else f"{name}: no %license files"
            )
        for p in info.licence_files:
            if not p.is_file():
                missing.append(str(p))
    if missing:
        installed = [n for n in BUTTEREYE_PKGS if db.get(n) is not None]
        formulae = tuple(BREW_FORMULAE[n] for n in installed if n in BREW_FORMULAE)
        return _f(
            "packages.licence_files",
            Severity.DEGRADED,
            M("Licence files are missing"),
            code=ErrorCode.LICENCE_FILE_MISSING,
            cause=M(
                "{n} licence file(s) of installed ButterEye packages are not on disk.",
                {"n": len(missing)},
            ),
            **platform_fix(
                M("This is a packaging bug or a damaged install; reinstall the packages."),
                ("sudo dnf reinstall " + " ".join(installed),),
                macos=M("This is a packaging bug or a damaged install; reinstall it."),
                # nothing to type when the package with the missing text is one
                # ButterEye built itself: Homebrew has no formula to reinstall
                macos_commands=_brew(*formulae, verb="reinstall") if formulae else (),
            ),
            evidence=tuple(missing),
        )
    return _f(
        "packages.licence_files",
        Severity.OK,
        M("Licence files present"),
        cause=M("Checked the licence files of {n} installed package(s).", {"n": checked})
        if osapi.IS_MACOS
        else M("Checked the %license files of {n} ButterEye package(s).", {"n": checked}),
    )


# ---------------------------------------------------------------------------
# Render tools (Section.RENDER)
# ---------------------------------------------------------------------------

FFMS2_GLOBS = ("/usr/lib64/libffms2.so*", "/usr/lib/libffms2.so*", "/usr/lib64/vapoursynth/*ffms2*")


def macos_ffms2_globs() -> tuple[str, ...]:
    """Where ffms2 is on a Mac: Homebrew's ``ffms2`` bottle installs
    ``libffms2.dylib`` (and the SOVERSION link) in ``<prefix>/lib`` and the same
    library into VapourSynth's autoload directory, where it is both the library
    render links and the VapourSynth plugin that reads the file (measured, see
    ``scriptgen.generator._macos_ffms2``).

    The plugin directories come from ``types.macos_plugin_dirs``, the same list
    the plugin rows search, rather than being written out again here: a second
    copy of the autoload layout is a second thing to get wrong, and the whole
    point of the search path is that one module knows where plugins can be.

    The prefix is derived, so these are built per call rather than frozen at
    import: a test can point ``HOMEBREW_PREFIX`` somewhere else.

    ButterEye.app: its own plugin directory only - a Homebrew ffms2 on the same
    Mac is not one the app would load."""
    plugins = tuple(f"{d}/*ffms2*" for d in macos_plugin_dirs())
    if osapi.MACOS_APP is not None:
        return plugins
    return (*plugins, f"{brew.prefix()}/lib/libffms2*.dylib")


def ffms2_present(db: RpmDb, globs: Sequence[str] | None = None) -> bool:
    if db.get("ffms2") is not None:
        return True
    if osapi.IS_WINDOWS and globs is None:
        from buttereye.core.scriptgen.generator import ScriptConstants

        return ScriptConstants().ffms2_lib.is_file()
    if globs is None and osapi.IS_MACOS:
        globs = macos_ffms2_globs()
    return any(glob.glob(g) for g in (FFMS2_GLOBS if globs is None else globs))


HDR10_ENCODERS = ("libx265", "libsvtav1")


def parse_encoders(text: str) -> tuple[str, ...]:
    """Encoder names from ``ffmpeg -hide_banner -encoders`` (rows after the legend)."""
    lines = text.splitlines()
    sep = next((i for i, ln in enumerate(lines) if ln.strip().startswith("------")), None)
    out: list[str] = []
    for ln in lines[sep + 1 if sep is not None else 0 :]:
        parts = ln.split()
        if len(parts) >= 2 and re.fullmatch(r"[VAS][.A-Z]{5}", parts[0]) and parts[1] != "=":
            out.append(parts[1])
    return tuple(out)


async def ffmpeg_encoders() -> CmdResult:
    return await run(("ffmpeg", "-hide_banner", "-encoders"), timeout_s=5.0)


def _render_available() -> bool:
    from buttereye.core.capabilities import static_state
    from buttereye.core.types import Feature

    return static_state(Feature.RENDER).available


def render_findings(
    db: RpmDb,
    encoders: CmdResult,
    *,
    which: Mapping[str, str | None] | None = None,
    ffms2: bool | None = None,
    render_available: bool | None = None,
) -> list[Finding]:
    """ffms2, mkvmerge, ffmpeg + HDR10 encoders, 7z. ``which`` overrides PATH lookup.
    A missing ffms2/mkvmerge is Degraded only while ``Feature.RENDER`` is in the
    build; until then it is a note (INFO)."""

    def w(name: str) -> str | None:
        if which is not None:
            return which.get(name)
        return shutil.which(name)

    out: list[Finding] = []
    sec = Section.RENDER
    # ffms2 and mkvmerge only serve offline render: notes until RENDER is in the build.
    render = _render_available() if render_available is None else render_available
    tool_level = Severity.DEGRADED if render else Severity.INFO
    not_yet = "" if render else " Offline render isn't in this build yet, so nothing needs it now."
    has_ffms2 = ffms2_present(db) if ffms2 is None else ffms2
    if has_ffms2:
        info = db.get("ffms2")
        out.append(
            _f(
                "render.ffms2",
                Severity.OK,
                M("ffms2 is installed"),
                section=sec,
                cause=M("{v}", {"v": info.evr if info else "found"}),
            )
        )
    else:
        out.append(
            _f(
                "render.ffms2",
                tool_level,
                M("ffms2 is not installed"),
                section=sec,
                code=ErrorCode.FFMS2_MISSING,
                cause=M("Offline render reads your video through ffms2.{tail}", {"tail": not_yet}),
                **platform_fix(
                    M("Install ffms2, then run the checks again."),
                    ("sudo dnf install ffms2",),
                    macos=M("Install ffms2 from Homebrew, then run the checks again."),
                    macos_commands=_brew("ffms2"),
                ),
            )
        )
    if w("mkvmerge"):
        out.append(_f("render.mkvmerge", Severity.OK, M("mkvmerge is installed"), section=sec))
    else:
        out.append(
            _f(
                "render.mkvmerge",
                tool_level,
                M("mkvmerge is not installed"),
                section=sec,
                code=ErrorCode.PKG_MISSING,
                cause=M("mkvmerge isn't installed, so ffmpeg will remux.{tail}", {"tail": not_yet}),
                **tool_fix(
                    M("Install mkvtoolnix for the preferred path."),
                    "sudo dnf install mkvtoolnix",
                    "mkvtoolnix",
                ),
            )
        )
    if encoders.missing:
        out.append(
            _f(
                "render.ffmpeg",
                Severity.DEGRADED,
                M("ffmpeg is not installed"),
                section=sec,
                code=ErrorCode.PKG_MISSING,
                cause=M("Offline render encodes with ffmpeg."),
                **tool_fix(M("Install ffmpeg."), "sudo dnf install /usr/bin/ffmpeg", "ffmpeg"),
            )
        )
    elif not encoders.ok:
        out.append(
            _f(
                "render.ffmpeg",
                Severity.DEGRADED,
                M("ffmpeg did not list its encoders"),
                section=sec,
                code=ErrorCode.ENCODER_FAILED,
                cause=M("ffmpeg -encoders failed."),
                fix=M("Check that ffmpeg runs from a terminal."),
                evidence=encoders.tail(),
            )
        )
    else:
        names = set(parse_encoders(encoders.stdout))
        hdr = [e for e in HDR10_ENCODERS if e in names]
        if hdr:
            out.append(
                _f(
                    "render.hdr10_encoder",
                    Severity.OK,
                    M("HDR10 encoders available: {list}", {"list": ", ".join(hdr)}),
                    section=sec,
                )
            )
        else:
            out.append(
                _f(
                    "render.hdr10_encoder",
                    Severity.DEGRADED,
                    M("HDR10 render needs the x265 or SVT-AV1 encoder"),
                    section=sec,
                    code=ErrorCode.PKG_MISSING,
                    cause=M("Your ffmpeg doesn't list libx265 or libsvtav1."),
                    fix=M("SDR render still works with the other encoders."),
                )
            )
    seven = next((n for n in ("7z", "7za", "7zz") if w(n)), None)
    if seven:
        out.append(
            _f(
                "render.7z",
                Severity.INFO,
                M("7z is installed"),
                section=sec,
                cause=M("Only needed for experimental TensorRT model archives."),
            )
        )
    else:
        out.append(
            _f(
                "render.7z",
                Severity.INFO,
                M("7z is not installed"),
                section=sec,
                code=ErrorCode.SEVENZIP_MISSING,
                cause=M("Only needed for experimental TensorRT model archives."),
                # macOS: there is no experimental TensorRT path to turn on (no CUDA,
                # so osapi.HAS_VSTRT is False), so nothing here would ever read a
                # .7z and an install line would be advice for a job that can't exist
                **(
                    {
                        "fix": M("Nothing on macOS needs it: the TensorRT path can't run here."),
                        "commands": (),
                    }
                    if osapi.IS_MACOS
                    else tool_fix(
                        M("Install 7zip if you turn on the experimental TensorRT path."),
                        "sudo dnf install 7zip",
                        "7zip",
                    )
                ),
            )
        )
    return out


async def gather_rpm_and_ffmpeg() -> tuple[RpmDb, CmdResult, RpmInfo | None]:
    db, enc, ff = await asyncio.gather(rpm_query(), ffmpeg_encoders(), rpm_owner("/usr/bin/ffmpeg"))
    return db, enc, ff


__all__ = [
    "VCREDIST_WINGET",
    "VCRUNTIME_MIN",
    "read_vcruntime",
    "vcruntime_finding",
    "COPR_PROJECT",
    "INSTALL_MANIFEST",
    "APP_CARRIES",
    "app_quarantine_finding",
    "APP_REPAIR",
    "REPAIR",
    "WINGET",
    "BREW_FORMULAE",
    "FORMULA_MVTOOLS",
    "MACOS_MVTOOLS_FIX",
    "NO_MACOS_PACKAGE",
    "brew_db",
    "macos_ffms2_globs",
    "Fix",
    "manifest_db",
    "platform_fix",
    "tool_fix",
    "PKG_RIFE",
    "PKG_MVTOOLS",
    "PKG_MVGPU",
    "mvgpu_finding",
    "PKG_MODELS",
    "PKG_VSMLRT",
    "BUTTEREYE_PKGS",
    "RpmInfo",
    "RpmDb",
    "parse_rpm_query",
    "rpm_query",
    "rpm_owner",
    "rife_variant",
    "installed_models",
    "plugin_dirs",
    "plugin_dir_text",
    "installed_plugin",
    "package_findings",
    "licence_files_finding",
    "parse_encoders",
    "render_findings",
    "ffms2_present",
]
