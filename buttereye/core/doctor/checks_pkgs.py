# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Package checks (SCOPE F1 "Packages", F10) and render tools (ffms2, mkvmerge, 7z).

ButterEye never runs dnf: missing packages are reported with the exact lines to
run. ``rpm -q`` output is parsed by pure functions so fixtures can test them.
"""

from __future__ import annotations

import asyncio
import glob
import re
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import Finding, Msg, Paths, Section, Severity

#: COPR project (SCOPE §9, owner decision Q5 pending: the FAS owner is not chosen yet).
COPR_PROJECT = "<owner>/buttereye"
COPR_ENABLE = f"sudo dnf copr enable {COPR_PROJECT}"

PKG_RIFE = "buttereye-vs-rife-ncnn"
PKG_MVTOOLS = "buttereye-vs-mvtools"
PKG_MODELS = "buttereye-rife-ncnn-models"
PKG_VSMLRT = "buttereye-vsmlrt-py"
PKG_ONNXCONV = "buttereye-onnxconverter-common"
BUTTEREYE_PKGS = (PKG_RIFE, PKG_MVTOOLS, PKG_MODELS, PKG_VSMLRT, PKG_ONNXCONV)
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

RIFE_SO = "librife.so"
MVTOOLS_SO = "mvtools.so"
MODELS_SUBDIR = "rife-ncnn-models"

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


async def rpm_query(names: Iterable[str] = BUTTEREYE_PKGS + OTHER_PKGS) -> RpmDb:
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


def package_findings(db: RpmDb, paths: Paths) -> list[Finding]:
    """Pure over the rpm result plus file existence in the private directories."""
    out: list[Finding] = []
    if not db.available:
        rife_file = (paths.rpm_plugin_dir / RIFE_SO).is_file()
        mv_file = (paths.rpm_plugin_dir / MVTOOLS_SO).is_file()
        out.append(
            _f(
                "packages.rpm",
                Severity.INFO,
                M("Installed packages can't be checked on this system"),
                cause=M("rpm is not available: {why}", {"why": db.error or "unknown"}),
                fix=M("ButterEye supports Fedora 44 and 45 with its COPR packages."),
            )
        )
        if not rife_file and not mv_file:
            out.append(
                _f(
                    "packages.plugins",
                    Severity.BLOCKING,
                    M("No interpolation plugin is installed"),
                    code=ErrorCode.PKG_MISSING,
                    cause=M(
                        "Neither {a} nor {b} was found in {d}.",
                        {"a": RIFE_SO, "b": MVTOOLS_SO, "d": str(paths.rpm_plugin_dir)},
                    ),
                    fix=M("Install the ButterEye plugin packages."),
                    commands=_dnf(PKG_MVTOOLS, PKG_RIFE, PKG_MODELS),
                )
            )
        return out

    rife = db.get(PKG_RIFE)
    mv = db.get(PKG_MVTOOLS)
    models_pkg = db.get(PKG_MODELS)
    rife_ok = rife is not None and (paths.rpm_plugin_dir / RIFE_SO).is_file()
    mv_ok = mv is not None and (paths.rpm_plugin_dir / MVTOOLS_SO).is_file()

    if rife_ok and rife is not None:
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
                    cause=M("{pkg} {ver}", {"pkg": PKG_RIFE, "ver": rife.evr}),
                    evidence=(str(paths.rpm_plugin_dir / RIFE_SO),),
                )
            )
    else:
        out.append(
            _f(
                "packages.rife",
                Severity.DEGRADED,
                M("The RIFE plugin is not installed"),
                code=ErrorCode.PKG_MISSING,
                cause=M(
                    "Without {pkg}, only MVTools (CPU) interpolation is possible.",
                    {"pkg": PKG_RIFE},
                ),
                fix=M("Install the RIFE plugin and its models from the ButterEye COPR."),
                commands=_dnf(PKG_RIFE, PKG_MODELS),
            )
        )

    if mv_ok and mv is not None:
        out.append(
            _f(
                "packages.mvtools",
                Severity.OK,
                M("MVTools plugin installed"),
                cause=M("{pkg} {ver}", {"pkg": PKG_MVTOOLS, "ver": mv.evr}),
                evidence=(str(paths.rpm_plugin_dir / MVTOOLS_SO),),
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
                fix=M("Install MVTools from the ButterEye COPR."),
                commands=_dnf(PKG_MVTOOLS),
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
                    cause=M("{pkg} {ver}", {"pkg": PKG_MODELS, "ver": models_pkg.evr}),
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
                    fix=M("Install the RIFE model package."),
                    commands=_dnf(PKG_MODELS),
                    evidence=models,
                )
            )

    if not rife_ok and not mv_ok:
        out.append(
            _f(
                "packages.plugins",
                Severity.BLOCKING,
                M("No interpolation plugin is installed"),
                code=ErrorCode.PKG_MISSING,
                cause=M("ButterEye needs RIFE-ncnn or MVTools to interpolate."),
                fix=M("Enable the ButterEye COPR and install the plugin packages."),
                commands=_dnf(PKG_MVTOOLS, PKG_RIFE, PKG_MODELS),
            )
        )

    out.append(licence_files_finding(db))
    return out


def licence_files_finding(db: RpmDb) -> Finding:
    missing: list[str] = []
    checked = 0
    for name in BUTTEREYE_PKGS:
        info = db.get(name)
        if info is None:
            continue
        checked += 1
        if not info.licence_files:
            missing.append(f"{name}: no %license files")
        for p in info.licence_files:
            if not p.is_file():
                missing.append(str(p))
    if missing:
        return _f(
            "packages.licence_files",
            Severity.DEGRADED,
            M("Licence files are missing"),
            code=ErrorCode.LICENCE_FILE_MISSING,
            cause=M(
                "{n} licence file(s) of installed ButterEye packages are not on disk.",
                {"n": len(missing)},
            ),
            fix=M("This is a packaging bug or a damaged install; reinstall the packages."),
            commands=(
                "sudo dnf reinstall "
                + " ".join(n for n in BUTTEREYE_PKGS if db.get(n) is not None),
            ),
            evidence=tuple(missing),
        )
    return _f(
        "packages.licence_files",
        Severity.OK,
        M("Licence files present"),
        cause=M("Checked the %license files of {n} ButterEye package(s).", {"n": checked}),
    )


# ---------------------------------------------------------------------------
# Render tools (Section.RENDER)
# ---------------------------------------------------------------------------

FFMS2_GLOBS = ("/usr/lib64/libffms2.so*", "/usr/lib/libffms2.so*", "/usr/lib64/vapoursynth/*ffms2*")


def ffms2_present(db: RpmDb, globs: Sequence[str] | None = None) -> bool:
    if db.get("ffms2") is not None:
        return True
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
                fix=M("Install ffms2, then run the checks again."),
                commands=("sudo dnf install ffms2",),
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
                fix=M("Install mkvtoolnix for the preferred path."),
                commands=("sudo dnf install mkvtoolnix",),
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
                fix=M("Install ffmpeg."),
                commands=("sudo dnf install /usr/bin/ffmpeg",),
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
                fix=M("Install 7zip if you turn on the experimental TensorRT path."),
                commands=("sudo dnf install 7zip",),
            )
        )
    return out


async def gather_rpm_and_ffmpeg() -> tuple[RpmDb, CmdResult, RpmInfo | None]:
    db, enc, ff = await asyncio.gather(rpm_query(), ffmpeg_encoders(), rpm_owner("/usr/bin/ffmpeg"))
    return db, enc, ff


__all__ = [
    "COPR_PROJECT",
    "PKG_RIFE",
    "PKG_MVTOOLS",
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
    "package_findings",
    "licence_files_finding",
    "parse_encoders",
    "render_findings",
    "ffms2_present",
]
