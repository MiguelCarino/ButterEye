# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""In-mpv probe (SCOPE F1) and plugin status (which copy of each plugin is active).

``mpv --no-config --vo=null --ao=null --frames=1 --vf=vapoursynth=file=probe.vpy``
on a ``testsrc2`` clip. The script writes a JSON report into the 0700 runtime
directory; it is read and deleted at once. Plugin code only ever runs inside
mpv, never in ButterEye's process (§8.1).

On macOS there is one more way for this to fail that is nothing to do with
ButterEye's script: mpv has to ``dlopen`` VapourSynth's VSScript library and
cannot find it by itself. ``osapi.use_bundled_runtime`` hands it the file, so what
is left here is reporting the cases where that cannot work -- no VapourSynth, or an
mpv the system protects from the variables that carry the answer
(``_vsscript_finding``).
"""

from __future__ import annotations

import glob
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from buttereye.core import osapi
from buttereye.core.commands import install_command
from buttereye.core.doctor import brew
from buttereye.core.doctor.checks_mpv import dyld_restricted
from buttereye.core.doctor.checks_pkgs import (
    MVTOOLS_SO,
    PKG_MVTOOLS,
    PKG_RIFE,
    RIFE_SO,
    RpmDb,
    platform_fix,
    rife_variant,
    tool_fix,
)
from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.paths import ensure_runtime_dir
from buttereye.core.types import (
    Finding,
    Msg,
    Paths,
    PluginStatus,
    ProbeResult,
    Section,
    Severity,
    _BREW_AUTOLOAD_GLOB,
)

PROBE_VPY = Path(__file__).with_name("probe.vpy")
PROBE_SOURCE = "av://lavfi:testsrc2=size=320x240:rate=24,format=yuv420p"
#: Distro autoload directories (R72 on F44; R79 uses site-packages/vapoursynth/plugins).
#: Windows: the per-user plugins64 folder, ``vs-plugins`` beside an installed
#: VapourSynth, and the same site-packages folder in any Python. macOS: Homebrew's,
#: derived from its prefix.
if osapi.IS_WINDOWS:
    _APPDATA = os.environ.get("APPDATA", "")
    AUTOLOAD_DIRS: tuple[str, ...] = (
        (os.path.join(_APPDATA, "VapourSynth", "plugins64"),) if _APPDATA else ()
    )
    AUTOLOAD_GLOBS: tuple[str, ...] = tuple(
        os.path.join(base, pattern)
        for base in filter(None, (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")))
        for pattern in (
            os.path.join("VapourSynth", "vs-plugins"),
            os.path.join("Programs", "Python", "Python3*", "Lib", "site-packages",
                         "vapoursynth", "plugins"),
        )
    )  # fmt: skip
elif osapi.IS_MACOS:
    # Homebrew has no fixed autoload directory to list: VapourSynth's own is a
    # directory under its prefix that every plugin formula drops its .dylib into
    # (measured: mvtools.dylib and libffms2.dylib there), and both the prefix (it
    # moves with the architecture) and the python3.x in the path (whichever
    # python@3.x the formula depends on today) have to be derived. The glob is
    # imported from core.types rather than repeated, because that is the pattern
    # phase 1 gives the generated scripts: a doctor that looked in a different
    # directory from the one a script loads out of would be a worse bug than a
    # private import.
    AUTOLOAD_DIRS = ()
    AUTOLOAD_GLOBS = (str(brew.prefix() / _BREW_AUTOLOAD_GLOB),)
else:
    AUTOLOAD_DIRS = ("/usr/lib64/vapoursynth", "/usr/lib/vapoursynth")
    AUTOLOAD_GLOBS = ("/usr/lib*/python3*/site-packages/vapoursynth/plugins",)

#: mpv's ``vapoursynth`` filter does not link VapourSynth: it loads VSScript at run
#: time, from ``VSSCRIPT_PATH`` when that is set and otherwise by two bare library
#: names it dlopens in turn (``osapi._darwin.VSSCRIPT_NAMES``), and it reads that
#: variable on every platform rather than only on Windows. This is the second of the
#: two names, which is the one mpv's failure message quotes, because it is the one it
#: tried last. Measured on Homebrew's mpv 0.41.0 (``strings`` has "VSSCRIPT_PATH" and
#: both names; ``otool -L`` lists no VapourSynth library).
MPV_VSSCRIPT_NAME = "libvapoursynth-script.dylib"
#: What Homebrew's VapourSynth really ships, and where: a file of the *first* name,
#: inside the virtual environment the formula builds under ``libexec``, which the
#: dynamic loader never searches. So mpv's dlopen fails before any ButterEye script
#: runs (BE-1005) until something hands it the file, which is what
#: ``osapi.use_bundled_runtime`` now does for every player ButterEye starts.
#: ``lib/`` comes first for an installation that puts it where the loader does look -
#: a from-source VapourSynth, or a Homebrew tap that links it there.
BREW_VSSCRIPT_NAME = "libvsscript.dylib"
BREW_VSSCRIPT_GLOBS = (
    f"lib/{BREW_VSSCRIPT_NAME}",
    f"Cellar/vapoursynth/*/libexec/lib/python3.*/site-packages/vapoursynth/{BREW_VSSCRIPT_NAME}",
)
#: mpv's own words when the dlopen above failed (it says this and carries on
#: without the filter, exiting 0, so the probe's only trace is this line)
_MPV_VSSCRIPT_FAILED = "Failed to load VapourSynth VSScript library"

ActiveCopy = Literal["buttereye", "distro", "user-vstrt", "none"]


@dataclass(frozen=True, slots=True)
class PluginSpec:
    name: str  # display name
    ns: str  # VapourSynth namespace
    package: str | None
    path: Path | None  # absolute path ButterEye loads it from
    distro_names: tuple[str, ...]  # file names of distro/av-rpm copies


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    raw: Mapping[str, Any] | None
    cmd: CmdResult | None
    error: str | None  # why there is no report


def mpv_quote(value: str) -> str:
    """mpv's length-prefixed option quoting: %<bytes>%value."""
    return f"%{len(value.encode('utf-8'))}%{value}"


def user_vstrt(paths: Paths) -> Path | None:
    """The newest user-built vstrt (``$XDG_DATA_HOME/buttereye/plugins/vstrt/<ver>/``),
    chosen as ``backends.trt`` chooses it."""
    from buttereye.core.backends.trt import _vstrt_dirs

    try:
        found = _vstrt_dirs(paths)
    except OSError:
        return None
    return found[0] / osapi.VSTRT_LIB if found else None


def plugin_specs(paths: Paths, *, trt: bool) -> tuple[PluginSpec, ...]:
    specs = [
        PluginSpec(
            "RIFE-ncnn-Vulkan",
            "rife",
            PKG_RIFE,
            paths.rpm_plugin_dir / RIFE_SO,
            tuple(map(osapi.plugin_file, ("librife.so", "librife_ncnn_vulkan.so"))),
        ),
        PluginSpec(
            "MVTools",
            "mv",
            PKG_MVTOOLS,
            paths.rpm_plugin_dir / MVTOOLS_SO,
            tuple(map(osapi.plugin_file, ("libmvtools.so", "mvtools.so"))),
        ),
    ]
    if trt:
        specs.append(PluginSpec("vstrt", "trt", None, user_vstrt(paths), (osapi.VSTRT_LIB,)))
    return tuple(specs)


def vsmlrt_paths(paths: Paths) -> tuple[str, ...]:
    """Where ``vsmlrt.py`` and its fp16 helpers may be: next to the user's vstrt
    (contrib/build-vstrt.sh), ButterEye's own Python folder, the packaged one."""
    out: list[str] = []
    vstrt = user_vstrt(paths)
    if vstrt is not None:
        out.append(str(vstrt.parent))
    out += [str(paths.data_dir / "python"), str(paths.rpm_data_dir / "python")]
    return tuple(out)


def version_text(raw: object) -> str | None:
    """A plugin's reported version as one line (vstrt reports a map)."""
    if isinstance(raw, dict):
        tv = trt_version_of(raw)
        name = str(raw.get("version") or "vstrt")
        return f"{name} (TensorRT {tv[0]}.{tv[1]})" if tv else name
    return str(raw) if raw else None


def trt_version_of(raw: object) -> tuple[int, int] | None:
    """(major, minor) of TensorRT from vstrt's ``core.trt.Version()`` (e.g. 110300)."""
    if isinstance(raw, dict):
        text = str(raw.get("tensorrt_version") or raw.get("tensorrt_version_build") or "")
    else:  # older probes stored str(dict)
        m = re.search(r"tensorrt_version'?\"?:\s*b?'?\"?(\d+)", str(raw or ""))
        text = m.group(1) if m else ""
    if not text.isdigit():
        return None
    n = int(text)
    return (n // 10000, (n // 100) % 100) if n >= 10000 else (n // 1000, (n // 100) % 10)


async def run_probe(
    paths: Paths,
    mpv: str,
    *,
    trt: bool = False,
    timeout_s: float = 8.0,
    script: Path = PROBE_VPY,
) -> ProbeOutcome:
    runtime = ensure_runtime_dir(paths)
    out = runtime / f"probe-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    modules = ["vsmlrt"] + (["onnx", "onnxconverter_common"] if trt else [])
    cfg = {
        "out": str(out),
        "plugins": [
            {"ns": s.ns, "path": str(s.path) if s.path else None}
            for s in plugin_specs(paths, trt=trt)
        ],
        "pythonpath": list(vsmlrt_paths(paths)),
        "modules": modules,
    }
    vf = (
        "vapoursynth=file="
        + mpv_quote(str(script))
        + ":user-data="
        + mpv_quote(json.dumps(cfg, separators=(",", ":")))
    )
    argv = (
        mpv,
        "--no-config",
        "--vo=null",
        "--ao=null",
        "--frames=1",
        "--msg-level=all=warn",
        "--vf=" + vf,
        PROBE_SOURCE,
    )
    try:
        res = await run(argv, timeout_s=timeout_s, cwd=runtime)
        try:
            raw = json.loads(out.read_text(encoding="utf-8"))
        except FileNotFoundError:
            why = "timed out" if res.timed_out else "the probe script failed or did not run"
            return ProbeOutcome(None, res, why)
        except ValueError as exc:
            return ProbeOutcome(None, res, f"unreadable probe output: {exc}")
        if not isinstance(raw, dict):
            return ProbeOutcome(None, res, "unexpected probe output")
        return ProbeOutcome(raw, res, None)
    finally:
        for p in (out, out.with_name(out.name + ".tmp")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def _distro_copy(spec: PluginSpec) -> str | None:
    dirs = list(AUTOLOAD_DIRS)
    for g in AUTOLOAD_GLOBS:
        dirs += glob.glob(g)
    for d in dirs:
        for n in spec.distro_names:
            p = Path(d) / n
            if p.is_file():
                return str(p)
    return None


def _record(raw: Mapping[str, Any] | None, ns: str) -> Mapping[str, Any] | None:
    if raw is None:
        return None
    for rec in raw.get("plugins", []):
        if isinstance(rec, dict) and rec.get("ns") == ns:
            return rec
    return None


def plugin_statuses(
    db: RpmDb, paths: Paths, raw: Mapping[str, Any] | None
) -> tuple[PluginStatus, ...]:
    out: list[PluginStatus] = []
    vstrt_dir = str(paths.data_dir / "plugins" / "vstrt")
    for spec in plugin_specs(paths, trt=True):
        info = db.get(spec.package) if spec.package else None
        rec = _record(raw, spec.ns)
        active: ActiveCopy = "none"
        loads: bool | None = None
        if rec is not None:
            loads = bool(rec.get("loaded"))
            src = str(rec.get("loaded_from") or "")
            if rec.get("preloaded"):
                active = "distro"
            elif loads and src.startswith(str(paths.rpm_plugin_dir)):
                active = "buttereye"
            elif loads and src.startswith(vstrt_dir):
                active = "user-vstrt"
        else:
            if spec.ns == "trt":
                active = "user-vstrt" if spec.path is not None else "none"
            elif info is not None and spec.path is not None and spec.path.is_file():
                # macOS: ButterEye's plugin directory can *be* Homebrew's autoload
                # directory, because ``types.macos_plugin_dir`` falls back to it
                # until a tap fills ButterEye's own (phase 4). A plugin that sits
                # in an autoload directory is the system's copy whatever directory
                # ButterEye reads it from -- VapourSynth has already loaded it, and
                # the version is Homebrew's choice, not one ButterEye measured.
                # Linux and Windows never put their own directory on that list, so
                # the test is False there and the answer is unchanged.
                autoloaded = osapi.IS_MACOS and _distro_copy(spec) == str(spec.path)
                active = "distro" if autoloaded else "buttereye"
            elif _distro_copy(spec) is not None:
                active = "distro"
        variant = rife_variant(info)[0] if spec.ns == "rife" else None
        version = (
            info.evr if info else (version_text(rec.get("version")) if rec is not None else None)
        )
        out.append(
            PluginStatus(
                name=spec.name,
                package=spec.package if info is not None else None,
                version=version,
                active_copy=active,
                loads_in_mpv=loads,
                variant=variant,
                licence_files=info.licence_files if info else (),
            )
        )
    return tuple(out)


def probe_result(raw: Mapping[str, Any], plugins: Sequence[PluginStatus]) -> ProbeResult:
    mods = raw.get("modules") or {}
    names = {"rife", "mv"} | ({"trt"} if _record(raw, "trt") is not None else set())
    keep = [p for p in plugins if _ns_of(p) in names]
    return ProbeResult(
        vs_core=f"VapourSynth {raw.get('vs_core', '?')}",
        python=str(raw.get("python", "?")),
        plugins=tuple(keep),
        vsmlrt_importable=bool(mods.get("vsmlrt")) if isinstance(mods, dict) else False,
    )


def _ns_of(p: PluginStatus) -> str:
    return {"RIFE-ncnn-Vulkan": "rife", "MVTools": "mv", "vstrt": "trt"}.get(p.name, p.name)


def _f(
    fid: str,
    sev: Severity,
    title: Msg,
    *,
    code: ErrorCode | None = None,
    cause: Msg | None = None,
    fix: Msg | None = None,
    commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> Finding:
    return Finding(
        fid, Section.PROBE, sev, code, title, cause or M(""), fix or M(""), commands, evidence
    )


def vsscript_library(prefix: Path | None = None) -> Path | None:
    """The VSScript library this Mac has, or None if it has none.

    The seam answers first (``osapi.vsscript_library``), and that is the point: the
    run-time fix that makes mpv able to load VSScript asks the same function, so the
    doctor cannot pass a file ButterEye would not use, or blame one it would. The
    Cellar globs below are the second answer, for the fixture half of this check -
    ``prefix`` is a planted keg, and on a machine that is not a Mac the seam has
    nothing to say, which is the only way the macOS row can be tested in Fedora CI.
    """
    if prefix is None:
        from_seam = osapi.vsscript_library()
        if from_seam is not None:
            return from_seam
    base = brew.prefix() if prefix is None else prefix
    for pattern in BREW_VSSCRIPT_GLOBS:
        try:
            found = sorted(base.glob(pattern))
        except OSError:
            found = []
        if found:
            return found[-1]  # the newest keg of the formula
    return None


def vsscript_load_failed(text: str) -> bool:
    """Does mpv's output say it could not load VSScript at all? Then nothing in the
    script ran, and no package is missing: the library has a different name here."""
    return _MPV_VSSCRIPT_FAILED in text or MPV_VSSCRIPT_NAME in text


def _vsscript_evidence(text: str, lib: Path | None = None) -> tuple[str, ...]:
    """mpv's failure line, shortened -- dyld appends every directory it tried, a
    kilobyte of paths that say nothing the cause does not already say -- and then what
    this process would have handed the player, which is the fact that tells "the fix
    did not reach mpv" apart from "it reached mpv and mpv refused the file"."""
    out: list[str] = []
    for ln in text.splitlines():
        if _MPV_VSSCRIPT_FAILED in ln:
            short = ln.strip()
            out.append(short if len(short) <= 160 else short[:160] + " …")
            break
    if lib is not None:
        for name in ("VSSCRIPT_PATH", osapi.DYLD_FALLBACK):
            value = os.environ.get(name)
            out.append(f"{name}={value}" if value else f"{name} is not set")
    return tuple(out)


def _app_vsscript_finding(resources: Path, text: str) -> Finding:
    """ButterEye.app's mpv could not start the app's VapourSynth.

    Nothing is missing that a package manager could supply: the app carries the
    library and hands it to mpv by full path (``osapi.point_at_app``). What can
    still go wrong is VSScript's own configuration line, which tells it which
    Python to start and which ButterEye writes when it opens
    (``osapi.register_vsscript``): a configuration folder that cannot be written,
    or a line that names another copy of the app. Failing that, the app itself is
    damaged."""
    config = osapi.vsscript_config(os.environ)
    lib = osapi.app_runtime(resources)["vsscript"]
    return _f(
        "probe.run",
        Severity.BLOCKING,
        M("mpv can't start ButterEye.app's VapourSynth"),
        code=ErrorCode.PROBE_FAILED,
        cause=M(
            "VapourSynth learns which Python to start from {config}, and ButterEye "
            "writes the line for its own copy there when it starts. The line is "
            "missing, could not be written, or names another copy of the app.",
            {"config": str(config) if config is not None else VSSCRIPT_CONFIG_TEXT},
        ),
        fix=M(
            "Make sure that file and its folder belong to you and can be written, start "
            "ButterEye again, and run the checks. If it still fails, download ButterEye "
            "again and replace ButterEye.app."
        ),
        commands=(),
        evidence=_vsscript_evidence(text, lib),
    )


#: VSScript's configuration, for a message when no folder can be named
VSSCRIPT_CONFIG_TEXT = "~/.config/vapoursynth/vapoursynth.toml"


def _vsscript_finding(outcome: ProbeOutcome) -> Finding:
    """BE-1005 on macOS, where the probe fails for one measured reason that is not
    a missing package: mpv looks for VSScript by two bare leaf names, and the file
    Homebrew installs is on no path the dynamic loader searches. Saying "install
    python3-vapoursynth" there is wrong twice over -- the package exists only on
    Fedora, and VapourSynth is installed.

    Phase 4 is what answers it, so this row no longer tells the user to do ButterEye's
    job for it: ``osapi.use_bundled_runtime`` hands the file to every player ButterEye
    starts. What is left for the doctor is the cases where that cannot work, and there
    are three. VapourSynth is not installed, so there is no file to hand over. Or this
    mpv is a program the system protects, which never receives a ``DYLD_*`` variable
    (``checks_mpv.dyld_restricted``) -- the silent failure worth naming, because
    everything is installed and correct and the filter still cannot load. Or neither,
    and the player did not get this process's environment at all, which the evidence
    says by naming what this process would have passed it.
    """
    res = outcome.cmd
    text = f"{res.stderr}\n{res.stdout}" if res is not None else ""
    if osapi.MACOS_APP is not None:
        return _app_vsscript_finding(osapi.MACOS_APP, text)
    lib = vsscript_library()
    if lib is None:
        return _f(
            "probe.run",
            Severity.BLOCKING,
            M("mpv can't load VapourSynth's VSScript library"),
            code=ErrorCode.PROBE_FAILED,
            cause=M(
                "mpv's vapoursynth filter loads VSScript at run time by the bare name "
                "{name}, and no VapourSynth is installed for it to load.",
                {"name": MPV_VSSCRIPT_NAME},
            ),
            fix=M(
                "Install VapourSynth with Homebrew. It ships VSScript as {shipped}, and "
                "ButterEye hands that file to the players it starts.",
                {"shipped": BREW_VSSCRIPT_NAME},
            ),
            commands=(install_command("vapoursynth"),),
            evidence=_vsscript_evidence(text),
        )
    export = f"export VSSCRIPT_PATH={lib}"
    binary = res.argv[0] if res is not None and res.argv else None
    restricted = dyld_restricted(binary) if binary is not None else None
    if restricted is not None:
        return _f(
            "probe.run",
            Severity.BLOCKING,
            M("The system stops ButterEye handing this mpv the VSScript library"),
            code=ErrorCode.PROBE_FAILED,
            cause=M(
                "{mpv} is a protected program, so the system removes the library-path "
                "variable ButterEye sets to lead a player to VSScript at {path}. Nothing "
                "is wrong with your installation: this mpv cannot be given the file that "
                "way.",
                {"mpv": binary or "", "path": str(lib)},
            )
            if restricted == "flag"
            else M(
                "{mpv} runs from a directory the system protects, so the system removes "
                "the library-path variable ButterEye sets to lead a player to VSScript at "
                "{path}. Nothing is wrong with your installation: this mpv cannot be given "
                "the file that way.",
                {"mpv": binary or "", "path": str(lib)},
            ),
            fix=M(
                "Use Homebrew's mpv, which is not protected, and make sure it comes first "
                "on PATH. Or export VSSCRIPT_PATH, which mpv reads and the system does not "
                "remove, in the shell you start ButterEye from.",
            ),
            commands=(export,),
            evidence=_vsscript_evidence(text, lib),
        )
    return _f(
        "probe.run",
        Severity.BLOCKING,
        M("mpv did not load the VSScript library ButterEye pointed it at"),
        code=ErrorCode.PROBE_FAILED,
        cause=M(
            "mpv's vapoursynth filter loads VSScript at run time by the bare name {name}, "
            "which no file on this Mac has: VSScript is {path}. ButterEye hands that file "
            "to the players it starts, this mpv did not load it, and nothing about this mpv "
            "says the system stopped it. The details below say what this ButterEye would "
            "have passed it.",
            {"name": MPV_VSSCRIPT_NAME, "path": str(lib)},
        ),
        fix=M(
            "Export VSSCRIPT_PATH in the shell you start ButterEye from and run the doctor "
            "again. If that makes it pass, the player was started without ButterEye's own "
            "environment.",
        ),
        commands=(export,),
        evidence=_vsscript_evidence(text, lib),
    )


def probe_findings(
    outcome: ProbeOutcome | None,
    plugins: Sequence[PluginStatus],
    *,
    vspipe_core: str | None,
    vulkan_loader: bool = True,
) -> list[Finding]:
    """``outcome`` None: mpv itself is unusable (the mpv section already blocks).
    ``vulkan_loader`` False (Windows): RIFE failing to load is the missing loader,
    which a graphics driver installs, not a broken install."""
    if outcome is None:
        return [
            _f(
                "probe.run",
                Severity.INFO,
                M("In-mpv probe not run"),
                cause=M("mpv must pass its own checks first."),
            )
        ]
    if outcome.raw is None:
        cmd_text = f"{outcome.cmd.stderr}\n{outcome.cmd.stdout}" if outcome.cmd else ""
        if osapi.IS_MACOS and vsscript_load_failed(cmd_text):
            return [_vsscript_finding(outcome)]
        return [
            _f(
                "probe.run",
                Severity.BLOCKING,
                M("The in-mpv probe failed"),
                code=ErrorCode.PROBE_FAILED,
                cause=M(
                    "mpv could not run a VapourSynth script: {why}.",
                    {"why": outcome.error or "unknown"},
                ),
                **tool_fix(
                    M("Make sure python3-vapoursynth matches the VapourSynth that mpv uses."),
                    "sudo dnf install python3-vapoursynth vapoursynth-tools",
                    "vapoursynth",
                    windows=M("Install VapourSynth for Windows; mpv loads it from there."),
                    macos=M(
                        "Make sure Homebrew's VapourSynth is the one mpv loads, and look at "
                        "the details below for what mpv said."
                    ),
                ),
                evidence=outcome.cmd.tail() if outcome.cmd else (),
            )
        ]
    raw = outcome.raw
    out: list[Finding] = []
    core = str(raw.get("vs_core", "?"))
    py = str(raw.get("python", "?"))
    ev = (
        f"VapourSynth {core} (API {raw.get('vs_api', '?')})",
        f"Python {py}",
        f"python: {raw.get('executable', '?')}",
    )
    if vspipe_core is not None and vspipe_core != core:
        out.append(
            _f(
                "probe.vs_version",
                Severity.DEGRADED,
                M("mpv and vspipe use different VapourSynth versions"),
                code=ErrorCode.PROBE_FAILED,
                cause=M(
                    "mpv embeds VapourSynth {a}, vspipe is {b}.", {"a": core, "b": vspipe_core}
                ),
                **tool_fix(
                    M("Update both so they come from the same Fedora release."),
                    "sudo dnf upgrade mpv vapoursynth-tools python3-vapoursynth",
                    "vapoursynth",
                    windows=M("Update VapourSynth; mpv and vspipe then use the same one."),
                    macos=M("Update mpv and VapourSynth together, so both come from one brew."),
                    verb="upgrade",
                ),
                evidence=ev,
            )
        )
    else:
        out.append(
            _f(
                "probe.vs_version",
                Severity.OK,
                M("mpv runs VapourSynth {core} with Python {py}", {"core": core, "py": py}),
                evidence=ev,
            )
        )
    loaded = [p for p in plugins if p.loads_in_mpv]
    failed = [p for p in plugins if p.loads_in_mpv is False and p.package is not None]
    recs = {str(r.get("ns")): r for r in raw.get("plugins", []) if isinstance(r, dict)}
    evid = tuple(
        f"{r.get('ns')}: "
        + (
            f"loaded from {r.get('loaded_from')}"
            if r.get("loaded") and not r.get("preloaded")
            else "already loaded (distro copy)"
            if r.get("preloaded")
            else f"not loaded ({r.get('error')})"
        )
        for r in recs.values()
    )
    if (
        failed
        and osapi.IS_WINDOWS
        and not vulkan_loader
        and {_ns_of(p) for p in failed} == {"rife"}
    ):
        out.append(
            _f(
                "probe.plugins",
                Severity.DEGRADED,
                M("RIFE can't load inside mpv without the Vulkan loader"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M(
                    "RIFE needs vulkan-1.dll, which a graphics driver installs. "
                    "MVTools (CPU) works without it."
                ),
                fix=M("Install or update your graphics driver from AMD, Intel or NVIDIA."),
                evidence=evid,
            )
        )
    elif failed:
        out.append(
            _f(
                "probe.plugins",
                Severity.DEGRADED,
                M("{names} did not load inside mpv", {"names": ", ".join(p.name for p in failed)}),
                code=ErrorCode.PROBE_FAILED,
                cause=M("The package is installed but mpv's VapourSynth could not load it."),
                **platform_fix(
                    M("Reinstall the plugin package; it must match this Fedora release."),
                    tuple(f"sudo dnf reinstall {p.package}" for p in failed),
                ),
                evidence=evid,
            )
        )
    elif loaded:
        names = " and ".join(p.name for p in loaded)
        out.append(
            _f(
                "probe.plugins",
                Severity.OK,
                M("{names} load inside mpv", {"names": names}),
                evidence=evid,
            )
        )
    else:
        out.append(
            _f(
                "probe.plugins",
                Severity.INFO,
                M("No plugin loaded inside mpv"),
                cause=M("No ButterEye plugin package is installed."),
                evidence=evid,
            )
        )
    distro = [p for p in plugins if p.active_copy == "distro"]
    if distro:
        out.append(
            _f(
                "probe.distro_copy",
                Severity.INFO,
                M(
                    "A distro copy of {names} is active",
                    {"names": ", ".join(p.name for p in distro)},
                ),
                cause=M(
                    "Another package autoloads this plugin into VapourSynth, so "
                    "ButterEye uses that copy instead of its own."
                ),
                evidence=evid,
            )
        )
    return out


def parse_vspipe_core(text: str) -> str | None:
    m = re.search(r"Core\s+(R\d+)", text)
    return m.group(1) if m else None


__all__ = [
    "BREW_VSSCRIPT_NAME",
    "MPV_VSSCRIPT_NAME",
    "PROBE_VPY",
    "PluginSpec",
    "ProbeOutcome",
    "mpv_quote",
    "plugin_specs",
    "run_probe",
    "plugin_statuses",
    "probe_result",
    "probe_findings",
    "parse_vspipe_core",
    "trt_version_of",
    "user_vstrt",
    "version_text",
    "vsscript_library",
    "vsscript_load_failed",
]
