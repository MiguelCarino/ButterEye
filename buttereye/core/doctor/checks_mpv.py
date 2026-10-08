# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv checks (SCOPE F1): binary, host vs Flatpak/Snap, version, VapourSynth
filter, conflicting user settings, mpvSockets.

The user's ``mpv.conf`` is only read, never written.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.doctor.proc import run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import Finding, Msg, Paths, Section, Severity

MIN_MPV = (0, 41, 0)
_VERSION = re.compile(r"\bmpv\s+v?(\d+)\.(\d+)(?:\.(\d+))?")
_SANDBOX_MARKERS = ("/var/lib/flatpak/", "/.local/share/flatpak/", "/snap/", "/var/lib/snapd/")
MPVSOCKETS_DIR = Path("/tmp/mpvSockets")


@dataclass(frozen=True, slots=True)
class MpvFacts:
    path: str | None  # resolved mpv binary (None = not found)
    version: tuple[int, int, int] | None
    usable: bool  # host binary, new enough, has the vapoursynth filter


def _f(
    fid: str,
    sev: Severity,
    title: Msg,
    *,
    section: Section = Section.MPV,
    code: ErrorCode | None = None,
    cause: Msg | None = None,
    fix: Msg | None = None,
    commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
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
    )


def parse_version(text: str) -> tuple[int, int, int] | None:
    m = _VERSION.search(text)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def has_vs_filter(vf_help: str) -> bool:
    return any(re.match(r"^\s*vapoursynth\s", ln) for ln in vf_help.splitlines())


def sandbox_kind(binary: str) -> str | None:
    """'flatpak' / 'snap' when ``binary`` (resolved) is a sandboxed wrapper."""
    real = os.path.realpath(binary)
    for marker in _SANDBOX_MARKERS:
        if marker in real + "/":
            return "snap" if "snap" in marker else "flatpak"
    try:
        with open(real, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return None
    if head.startswith(b"#!"):
        if b"flatpak run" in head or b"flatpak-spawn" in head:
            return "flatpak"
        if b"snap run" in head:
            return "snap"
    return None


async def mpv_checks(
    *, mpv: str | None = None, which: Mapping[str, str | None] | None = None
) -> tuple[list[Finding], MpvFacts]:
    exe = mpv or (which.get("mpv") if which is not None else shutil.which("mpv"))
    if exe is None:
        return [
            _f(
                "mpv.binary",
                Severity.BLOCKING,
                M("mpv is not installed"),
                code=ErrorCode.MPV_NOT_FOUND,
                cause=M("ButterEye controls mpv; no mpv was found on PATH."),
                fix=M("Install the distro mpv package."),
                commands=("sudo dnf install mpv",),
            )
        ], MpvFacts(None, None, False)

    out: list[Finding] = []
    sandbox = sandbox_kind(exe)
    if sandbox:
        out.append(
            _f(
                "mpv.host_binary",
                Severity.BLOCKING,
                M("mpv is a {kind} package", {"kind": sandbox.capitalize()}),
                code=ErrorCode.MPV_SANDBOXED,
                cause=M(
                    "{path} runs mpv inside a {kind} sandbox, which is unsupported in "
                    "v1 (see SCOPE §9).",
                    {"path": exe, "kind": sandbox.capitalize()},
                ),
                fix=M("Install the distro mpv package and make sure it comes first on PATH."),
                commands=("sudo dnf install mpv",),
                evidence=(os.path.realpath(exe),),
            )
        )
    else:
        out.append(
            _f(
                "mpv.host_binary",
                Severity.OK,
                M("mpv is a host binary"),
                evidence=(os.path.realpath(exe),),
            )
        )

    ver_res, vf_res = await asyncio.gather(
        run((exe, "--no-config", "--version"), timeout_s=4.0),
        run((exe, "--no-config", "--vf=help"), timeout_s=4.0),
    )
    version = parse_version(ver_res.stdout) if ver_res.ok else None
    if version is None:
        out.append(
            _f(
                "mpv.version",
                Severity.BLOCKING,
                M("mpv did not report its version"),
                code=ErrorCode.MPV_TOO_OLD,
                cause=M("mpv --version failed or printed something unexpected."),
                fix=M("Reinstall the distro mpv package."),
                commands=("sudo dnf reinstall mpv",),
                evidence=ver_res.tail(),
            )
        )
    elif version < MIN_MPV:
        out.append(
            _f(
                "mpv.version",
                Severity.BLOCKING,
                M("mpv {v} is too old", {"v": ".".join(map(str, version))}),
                code=ErrorCode.MPV_TOO_OLD,
                cause=M("ButterEye needs mpv 0.41.0 or newer."),
                fix=M("Update mpv."),
                commands=("sudo dnf upgrade mpv",),
            )
        )
    else:
        out.append(
            _f(
                "mpv.version",
                Severity.OK,
                M("mpv {v}", {"v": ".".join(map(str, version))}),
                evidence=tuple(ver_res.stdout.splitlines()[:1]),
            )
        )

    vf_ok = vf_res.ok and has_vs_filter(vf_res.stdout)
    if vf_ok:
        out.append(_f("mpv.vf_vapoursynth", Severity.OK, M("mpv has the VapourSynth filter")))
    else:
        out.append(
            _f(
                "mpv.vf_vapoursynth",
                Severity.BLOCKING,
                M("mpv has no VapourSynth filter"),
                code=ErrorCode.MPV_NO_VS_FILTER,
                cause=M("Your mpv was built without VapourSynth."),
                fix=M("Install the distro mpv package."),
                commands=("sudo dnf install mpv",),
                evidence=vf_res.tail(3) if not vf_res.ok else (),
            )
        )
    usable = not sandbox and version is not None and version >= MIN_MPV and vf_ok
    return out, MpvFacts(os.path.realpath(exe), version, usable)


# ---------------------------------------------------------------------------
# Conflicting user settings
# ---------------------------------------------------------------------------


def _parse_line(line: str) -> tuple[str, str | None] | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    if s.startswith("--"):
        s = s[2:]
    if "=" in s:
        key, value = s.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split("#", 1)[0].strip()
        return key.strip(), value
    return s.split("#", 1)[0].strip(), None


def read_user_conf(path: Path, *, _depth: int = 0) -> dict[str, str | None]:
    """Top-level (and ``[default]``) options of an mpv.conf; follows ``include=``."""
    opts: dict[str, str | None] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return opts
    section: str | None = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if section not in (None, "default"):
            continue
        kv = _parse_line(line)
        if kv is None:
            continue
        key, value = kv
        if key == "include" and value and _depth < 3:
            if value.startswith("~~/"):  # mpv: relative to the config directory
                inc = path.parent / value[3:]
            else:
                inc = Path(os.path.expanduser(value))
            if not inc.is_absolute():
                inc = path.parent / inc
            opts.update(read_user_conf(inc, _depth=_depth + 1))
            continue
        if key.startswith("no-") and value is None:
            opts[key[3:]] = "no"
            continue
        opts[key] = value
    return opts


def _yes(v: str | None) -> bool:
    return v is None or v.lower() in ("yes", "1", "true")


def hwdec_is_copy(value: str | None) -> bool:
    if value is None:
        return False  # bare "hwdec" means auto
    parts = [p.strip().lower() for p in value.split(",") if p.strip()]
    return all(p == "no" or p.endswith("-copy") or p.endswith("-copy-safe") for p in parts)


#: Appended to user mpv.conf findings while attaching isn't in the build: players
#: ButterEye starts override hwdec, interpolation and watch-later-options-remove.
_ATTACH_ONLY = (
    " This only matters for attaching to an mpv you started yourself, which isn't in "
    "this build yet; players ButterEye starts already use their own setting."
)


_ATTACH_SHORT = " Attaching isn't in this build yet, so nothing needs to change now."


def conflict_findings(
    paths: Paths,
    opts: Mapping[str, str | None] | None = None,
    *,
    attach_available: bool | None = None,
) -> list[Finding]:
    """User mpv.conf settings that would break an *attached* player. Degraded only
    while ``Feature.ATTACH`` is available; otherwise a note (INFO), because the
    players ButterEye starts get their own copies of these settings."""
    conf = paths.user_mpv_conf
    o = read_user_conf(conf) if opts is None else dict(opts)
    out: list[Finding] = []
    sec = Section.CONFLICTS
    attach = _attach_available() if attach_available is None else attach_available
    level = Severity.DEGRADED if attach else Severity.INFO
    tail = "" if attach else _ATTACH_ONLY
    short_tail = "" if attach else _ATTACH_SHORT
    fix_file = M("Edit {path}. ButterEye never changes it for you.", {"path": str(conf)})
    if "hwdec" in o and not hwdec_is_copy(o["hwdec"]):
        out.append(
            _f(
                "conflicts.hwdec",
                level,
                M("mpv.conf uses hardware decoding without copy-back"),
                section=sec,
                code=ErrorCode.USER_CONF_CONFLICT,
                cause=M(
                    "hwdec={v} keeps frames on the GPU, where the VapourSynth filter can't "
                    "read them. Players ButterEye attaches to are affected; players it "
                    "starts get their own setting.{tail}",
                    {"v": o["hwdec"] or "auto", "tail": short_tail},
                ),
                fix=M(
                    "Use a copy-back mode such as hwdec=auto-copy in {path}.", {"path": str(conf)}
                ),
                commands=("hwdec=auto-copy",),
                evidence=(f"hwdec={o['hwdec']}",),
            )
        )
    if "interpolation" in o and _yes(o["interpolation"]):
        out.append(
            _f(
                "conflicts.interpolation",
                level,
                M("mpv.conf turns on mpv's own interpolation"),
                section=sec,
                code=ErrorCode.USER_CONF_CONFLICT,
                cause=M(
                    "interpolation=yes blends frames on top of ButterEye's motion "
                    "interpolation in players it attaches to.{tail}",
                    {"tail": tail},
                ),
                fix=fix_file,
                commands=("interpolation=no",),
                evidence=("interpolation=yes",),
            )
        )
    save = "save-position-on-quit" in o and _yes(o["save-position-on-quit"])
    remove = o.get("watch-later-options-remove") or ""
    if save and "vf" not in [x.strip() for x in remove.split(",")]:
        out.append(
            _f(
                "conflicts.save_position",
                level,
                M("mpv saves the video filter when you quit"),
                section=sec,
                code=ErrorCode.USER_CONF_CONFLICT,
                cause=M(
                    "save-position-on-quit stores the vf option, so a file can reopen "
                    "later with a stale ButterEye filter.{tail}",
                    {"tail": tail},
                ),
                fix=M("Add watch-later-options-remove=vf to {path}.", {"path": str(conf)}),
                commands=("watch-later-options-remove=vf",),
                evidence=("save-position-on-quit=" + (o["save-position-on-quit"] or "yes"),),
            )
        )
    if not out:
        out.append(
            _f(
                "conflicts.user_conf",
                Severity.OK,
                M("No conflicting mpv settings"),
                section=sec,
                cause=M("Checked {path}.", {"path": str(conf)})
                if conf.exists()
                else M("{path} does not exist.", {"path": str(conf)}),
            )
        )
    return out


def _attach_available() -> bool:
    from buttereye.core.capabilities import static_state
    from buttereye.core.types import Feature

    return static_state(Feature.ATTACH).available


def mpvsockets_finding(
    paths: Paths, sockets_dir: Path | None = None, *, attach_available: bool | None = None
) -> Finding | None:
    """BE-1041. The script moves *every* mpv's IPC socket, including the players
    ButterEye starts (user scripts load there too), so an installed script is
    Degraded (errors.md); a leftover socket folder alone is only a fact (INFO).
    The text never promises attaching while ``Feature.ATTACH`` is unavailable."""
    sockets_dir = MPVSOCKETS_DIR if sockets_dir is None else sockets_dir
    scripts = paths.user_mpv_conf.parent / "scripts"
    found_scripts: list[str] = []
    try:
        found_scripts = sorted(
            str(p) for p in scripts.iterdir() if p.name.lower().startswith("mpvsockets")
        )
    except OSError:
        pass
    evidence = list(found_scripts)
    if sockets_dir.is_dir():
        evidence.append(str(sockets_dir))
    if not evidence:
        return None
    attach = _attach_available() if attach_available is None else attach_available
    attach_text = (
        "ButterEye attaches to those players only after checking each socket."
        if attach
        else "Attaching to running players isn't in this build yet."
    )
    if found_scripts:
        return _f(
            "conflicts.mpvsockets",
            Severity.DEGRADED if attach else Severity.INFO,
            M("mpvSockets is installed"),
            section=Section.CONFLICTS,
            code=ErrorCode.MPVSOCKETS_IN_USE,
            cause=M(
                "The mpvSockets script gives every mpv window a socket in {d}, and it "
                "also moves the socket of players ButterEye starts. Play follows the "
                "moved socket after checking it belongs to that mpv. {attach}",
                {"d": str(sockets_dir), "attach": attach_text},
            ),
            fix=M(
                "Nothing to change for Play. If Play still can't connect to mpv, remove "
                "mpvSockets from {scripts} (or move it out of that folder).",
                {"scripts": str(scripts)},
            ),
            evidence=tuple(evidence),
        )
    return _f(
        "conflicts.mpvsockets",
        Severity.INFO,
        M("mpvSockets folder found"),
        section=Section.CONFLICTS,
        code=ErrorCode.MPVSOCKETS_IN_USE,
        cause=M(
            "mpv windows have sockets in {d} (made by the mpvSockets script), but the "
            "script isn't in your mpv scripts folder. {attach}",
            {"d": str(sockets_dir), "attach": attach_text},
        ),
        fix=M("Nothing to do."),
        evidence=tuple(evidence),
    )


async def all_mpv_checks(paths: Paths) -> tuple[list[Finding], MpvFacts]:
    found, facts = await mpv_checks()
    conflicts = conflict_findings(paths)
    sockets = mpvsockets_finding(paths)
    return found + conflicts + ([sockets] if sockets else []), facts


__all__ = [
    "MIN_MPV",
    "MpvFacts",
    "parse_version",
    "has_vs_filter",
    "sandbox_kind",
    "mpv_checks",
    "read_user_conf",
    "hwdec_is_copy",
    "conflict_findings",
    "mpvsockets_finding",
    "all_mpv_checks",
]
