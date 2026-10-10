# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Experimental TensorRT section (SCOPE F1 items (a)-(g), §5.2).

Shown only after the user opts in. Every row is ``experimental`` and never
BLOCKING, so it can never block the RIFE-ncnn or MVTools paths.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.hw.detect import HwProbe
from buttereye.core.types import Finding, Msg, Section, Severity

PINNED_TRT = (11, 3)
MIN_DRIVER = 580
RECOMMENDED_DRIVER = 615
MAX_VS_CORE = 79
TRTEXEC_DEFAULT = Path("/usr/bin/trtexec")
_NVINFER = re.compile(r"libnvinfer\.so\.11\b.*=>\s*(\S+)")


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
        fid,
        Section.TRT,
        sev,
        code,
        title,
        cause or M(""),
        fix or M(""),
        commands,
        evidence,
        experimental=True,
    )


def trt_off_finding() -> Finding:
    return Finding(
        "trt.off",
        Section.TRT,
        Severity.INFO,
        None,
        M("Experimental NVIDIA TensorRT: off"),
        M("It uses pre-release software you install yourself and may break on updates."),
        M("Turn it on in setup if you want to try it."),
    )


def libnvinfer_version(ldconfig: CmdResult) -> tuple[int, ...] | None:
    """Version of libnvinfer.so.11 from ``ldconfig -p`` (resolving the symlink)."""
    if not ldconfig.ok:
        return None
    m = _NVINFER.search(ldconfig.stdout)
    if not m:
        return None
    real = os.path.realpath(m.group(1))
    v = re.search(r"\.so\.(\d+(?:\.\d+)*)$", real)
    if not v:
        return (11,)
    return tuple(int(x) for x in v.group(1).split("."))


async def ldconfig_p() -> CmdResult:
    exe = shutil.which("ldconfig") or "/usr/sbin/ldconfig"
    return await run((exe, "-p"), timeout_s=4.0)


def trt_findings(
    hw: HwProbe,
    ldconfig: CmdResult,
    probe_raw: Mapping[str, Any] | None,
    *,
    trtexec: str | None,
) -> list[Finding]:
    out: list[Finding] = []
    dnf_rt = "sudo dnf install libnvinfer-bin"
    nv = [g for g in hw.info.gpus if g.vendor == "NVIDIA"]
    # (a) compute capability
    cc = next((g.compute_cap for g in nv if g.compute_cap), None)
    if not nv:
        out.append(
            _f(
                "trt.gpu",
                Severity.DEGRADED,
                M("TRT 11 needs an NVIDIA GPU"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("No NVIDIA GPU was found."),
            )
        )
    elif cc is None:
        out.append(
            _f(
                "trt.gpu",
                Severity.DEGRADED,
                M("The GPU's compute capability is unknown"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("nvidia-smi did not report compute_cap."),
                fix=M("Install the NVIDIA driver from RPM Fusion."),
            )
        )
    elif tuple(int(x) for x in cc.split(".")) < (7, 5):
        out.append(
            _f(
                "trt.gpu",
                Severity.DEGRADED,
                M("TRT 11 does not support this GPU"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("Compute capability {cc}; TRT 11 needs 7.5 or newer.", {"cc": cc}),
            )
        )
    else:
        out.append(_f("trt.gpu", Severity.OK, M("Compute capability {cc}", {"cc": cc})))
    # (b) driver
    drv = next((g.driver for g in nv if g.driver), None)
    major = int(drv.split(".")[0]) if drv and drv.split(".")[0].isdigit() else None
    if nv and (major is None or major < MIN_DRIVER):
        out.append(
            _f(
                "trt.driver",
                Severity.DEGRADED,
                M("The NVIDIA driver is too old for TRT 11"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("Driver {d}; CUDA 13 needs 580 or newer.", {"d": drv or "unknown"}),
                fix=M("Update the RPM Fusion NVIDIA driver."),
            )
        )
    elif nv and major is not None:
        sev = Severity.OK if major >= RECOMMENDED_DRIVER else Severity.INFO
        out.append(
            _f(
                "trt.driver",
                sev,
                M("NVIDIA driver {d}", {"d": drv or ""}),
                cause=M("R615 or newer is recommended for CUDA 13.4 builds.")
                if sev is Severity.INFO
                else None,
            )
        )
    # (c) libnvinfer
    ver = libnvinfer_version(ldconfig)
    if ver is None:
        out.append(
            _f(
                "trt.libnvinfer",
                Severity.DEGRADED,
                M("libnvinfer.so.11 was not found"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("TensorRT 11 is not installed (ldconfig -p)."),
                fix=M("Install TensorRT 11 from NVIDIA's rhel10 repository yourself."),
                commands=(dnf_rt,),
            )
        )
    elif ver[:2] != PINNED_TRT:
        out.append(
            _f(
                "trt.libnvinfer",
                Severity.DEGRADED,
                M("TensorRT {v} is not the pinned 11.3", {"v": ".".join(map(str, ver))}),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M(
                    "Engines will rebuild, and vstrt may have been built against a "
                    "different minor version."
                ),
            )
        )
    else:
        out.append(
            _f("trt.libnvinfer", Severity.OK, M("TensorRT {v}", {"v": ".".join(map(str, ver))}))
        )
    # (d) trtexec
    if trtexec:
        out.append(_f("trt.trtexec", Severity.OK, M("trtexec found"), evidence=(trtexec,)))
    else:
        out.append(
            _f(
                "trt.trtexec",
                Severity.DEGRADED,
                M("trtexec is not installed"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("Engines are built with trtexec from libnvinfer-bin."),
                fix=M("Install libnvinfer-bin from NVIDIA's rhel10 repository."),
                commands=(dnf_rt,),
            )
        )
    # (e) vstrt in mpv, (f) VS core, (g) python modules
    rec: Mapping[str, Any] | None = None
    core_n: int | None = None
    mods: Mapping[str, Any] = {}
    if probe_raw is not None:
        rec = next(
            (
                r
                for r in probe_raw.get("plugins", [])
                if isinstance(r, dict) and r.get("ns") == "trt"
            ),
            None,
        )
        m = re.match(r"R(\d+)", str(probe_raw.get("vs_core", "")))
        core_n = int(m.group(1)) if m else None
        raw_mods = probe_raw.get("modules")
        mods = raw_mods if isinstance(raw_mods, dict) else {}
    if rec is not None and rec.get("loaded"):
        from buttereye.core.doctor.probe import trt_version_of, version_text

        built = trt_version_of(rec.get("version"))
        tv = f"{built[0]}.{built[1]}" if built else (version_text(rec.get("version")) or "")
        lib = ".".join(map(str, ver)) if ver else ""
        if built is not None and ver and tuple(ver[:2]) != built:
            out.append(
                _f(
                    "trt.vstrt",
                    Severity.DEGRADED,
                    M("vstrt was built for TensorRT {a}, not {b}", {"a": tv, "b": lib}),
                    code=ErrorCode.TRT_UNSUPPORTED,
                    fix=M("Rebuild vstrt with contrib/build-vstrt.sh."),
                )
            )
        else:
            out.append(
                _f(
                    "trt.vstrt",
                    Severity.OK,
                    M("vstrt loads inside mpv"),
                    evidence=(str(rec.get("loaded_from") or ""),),
                )
            )
    else:
        why = str(rec.get("error")) if rec is not None else "probe not run"
        out.append(
            _f(
                "trt.vstrt",
                Severity.DEGRADED,
                M("vstrt is not available"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("No user-built vstrt loads in mpv ({why}).", {"why": why}),
                fix=M("Build it locally with contrib/build-vstrt.sh."),
            )
        )
    if core_n is not None and core_n > MAX_VS_CORE:
        out.append(
            _f(
                "trt.vs_api3",
                Severity.DEGRADED,
                M("vstrt and akarin are API3 plugins and cannot load on R80+"),
                code=ErrorCode.TRT_UNSUPPORTED,
                cause=M("mpv uses VapourSynth R{n}.", {"n": core_n}),
            )
        )
    elif core_n is not None:
        out.append(
            _f(
                "trt.vs_api3",
                Severity.OK,
                M("VapourSynth R{n} can load API3 plugins", {"n": core_n}),
            )
        )
    if mods.get("onnxconverter_common"):
        out.append(
            _f("trt.fp16", Severity.OK, M("onnxconverter-common found: fp16 engines possible"))
        )
    else:
        out.append(
            _f(
                "trt.fp16",
                Severity.INFO,
                M("Only fp32 engines are offered"),
                cause=M("onnxconverter_common is not importable in the vspipe Python."),
                commands=("sudo dnf install buttereye-onnxconverter-common",),
            )
        )
    if mods.get("onnx"):
        out.append(_f("trt.onnx", Severity.OK, M("python3-onnx found")))
    else:
        out.append(
            _f(
                "trt.onnx",
                Severity.INFO,
                M("RIFE scale profiles below 1 are disabled"),
                cause=M("onnx is not importable in the Python that mpv and vspipe use."),
                commands=("sudo dnf install python3-onnx",),
            )
        )
    return out


def find_trtexec() -> str | None:
    found = shutil.which("trtexec")
    if found:
        return found
    return str(TRTEXEC_DEFAULT) if TRTEXEC_DEFAULT.is_file() else None


__all__ = ["trt_off_finding", "trt_findings", "libnvinfer_version", "ldconfig_p", "find_trtexec"]
