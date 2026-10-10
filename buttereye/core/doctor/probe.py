# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""In-mpv probe (SCOPE F1) and plugin status (which copy of each plugin is active).

``mpv --no-config --vo=null --ao=null --frames=1 --vf=vapoursynth=file=probe.vpy``
on a ``testsrc2`` clip. The script writes a JSON report into the 0700 runtime
directory; it is read and deleted at once. Plugin code only ever runs inside
mpv, never in ButterEye's process (§8.1).
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

from buttereye.core.doctor.checks_pkgs import (
    MVTOOLS_SO,
    PKG_MVTOOLS,
    PKG_RIFE,
    RIFE_SO,
    RpmDb,
    rife_variant,
)
from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.paths import ensure_runtime_dir
from buttereye.core.types import Finding, Msg, Paths, PluginStatus, ProbeResult, Section, Severity

PROBE_VPY = Path(__file__).with_name("probe.vpy")
PROBE_SOURCE = "av://lavfi:testsrc2=size=320x240:rate=24,format=yuv420p"
#: Distro autoload directories (R72 on F44; R79 uses site-packages/vapoursynth/plugins).
AUTOLOAD_DIRS = ("/usr/lib64/vapoursynth", "/usr/lib/vapoursynth")
AUTOLOAD_GLOBS = ("/usr/lib*/python3*/site-packages/vapoursynth/plugins",)

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
    return found[0] / "libvstrt.so" if found else None


def plugin_specs(paths: Paths, *, trt: bool) -> tuple[PluginSpec, ...]:
    specs = [
        PluginSpec(
            "RIFE-ncnn-Vulkan",
            "rife",
            PKG_RIFE,
            paths.rpm_plugin_dir / RIFE_SO,
            ("librife.so", "librife_ncnn_vulkan.so"),
        ),
        PluginSpec(
            "MVTools",
            "mv",
            PKG_MVTOOLS,
            paths.rpm_plugin_dir / MVTOOLS_SO,
            ("libmvtools.so", "mvtools.so"),
        ),
    ]
    if trt:
        specs.append(PluginSpec("vstrt", "trt", None, user_vstrt(paths), ("libvstrt.so",)))
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
                active = "buttereye"
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


def probe_findings(
    outcome: ProbeOutcome | None,
    plugins: Sequence[PluginStatus],
    *,
    vspipe_core: str | None,
) -> list[Finding]:
    """``outcome`` None: mpv itself is unusable (the mpv section already blocks)."""
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
                fix=M("Make sure python3-vapoursynth matches the VapourSynth that mpv uses."),
                commands=("sudo dnf install python3-vapoursynth vapoursynth-tools",),
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
                fix=M("Update both so they come from the same Fedora release."),
                commands=("sudo dnf upgrade mpv vapoursynth-tools python3-vapoursynth",),
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
    if failed:
        out.append(
            _f(
                "probe.plugins",
                Severity.DEGRADED,
                M("{names} did not load inside mpv", {"names": ", ".join(p.name for p in failed)}),
                code=ErrorCode.PROBE_FAILED,
                cause=M("The package is installed but mpv's VapourSynth could not load it."),
                fix=M("Reinstall the plugin package; it must match this Fedora release."),
                commands=tuple(f"sudo dnf reinstall {p.package}" for p in failed),
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
]
