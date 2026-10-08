# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Vulkan checks (SCOPE F1 "Also reports", §4.5, §5.3.2): loader, ICDs, devices
(CPU devices filtered out), chosen device and the decode-vs-interpolation GPU."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.hw.detect import HwProbe, interpolation_pci
from buttereye.core.types import Finding, Msg, Section, Severity

LOADER_PATHS = (
    Path("/usr/lib64/libvulkan.so.1"),
    Path("/lib64/libvulkan.so.1"),
    Path("/usr/lib/libvulkan.so.1"),
    Path("/usr/lib/x86_64-linux-gnu/libvulkan.so.1"),
)
ICD_DIRS = (
    Path("/usr/share/vulkan/icd.d"),
    Path("/etc/vulkan/icd.d"),
    Path("/usr/local/share/vulkan/icd.d"),
)
#: ICD manifests that only provide CPU or translation devices.
CPU_ICDS = ("lvp_icd", "dzn_icd")


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
        fid, Section.VULKAN, sev, code, title, cause or M(""), fix or M(""), commands, evidence
    )


def gpu_icds(dirs: Sequence[Path] | None = None) -> tuple[Path, ...]:
    if dirs is None:
        dirs = ICD_DIRS
    out: list[Path] = []
    for d in dirs:
        try:
            out += [p for p in sorted(d.glob("*.json")) if not p.name.startswith(CPU_ICDS)]
        except OSError:
            continue
    return tuple(out)


def vulkan_findings(
    hw: HwProbe,
    *,
    gpu_override: str | None = None,
    loader_paths: Sequence[Path] | None = None,
    icd_dirs: Sequence[Path] | None = None,
) -> list[Finding]:
    out: list[Finding] = []
    loader = next((p for p in (loader_paths or LOADER_PATHS) if p.exists()), None)
    if loader is None:
        out.append(
            _f(
                "vulkan.loader",
                Severity.DEGRADED,
                M("The Vulkan loader is not installed"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M("RIFE needs libvulkan.so.1. Without it only MVTools (CPU) works."),
                fix=M("Install vulkan-loader."),
                commands=("sudo dnf install vulkan-loader",),
            )
        )
    icds = gpu_icds(icd_dirs)
    if not icds:
        out.append(
            _f(
                "vulkan.icd",
                Severity.DEGRADED,
                M("No GPU Vulkan driver is installed"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M("No Vulkan driver manifest (ICD) for a GPU was found."),
                fix=M(
                    "AMD and Intel: install mesa-vulkan-drivers. NVIDIA: install the "
                    "RPM Fusion driver (xorg-x11-drv-nvidia)."
                ),
                commands=("sudo dnf install mesa-vulkan-drivers",),
            )
        )

    vk = hw.vulkaninfo
    if vk is None or vk.missing:
        out.append(
            _f(
                "vulkan.devices",
                Severity.DEGRADED,
                M("vulkaninfo is not installed, so Vulkan devices can't be listed"),
                code=ErrorCode.PKG_MISSING,
                cause=M("ButterEye lists Vulkan GPUs with vulkaninfo from vulkan-tools."),
                fix=M("Install vulkan-tools, then run the checks again."),
                commands=("sudo dnf install vulkan-tools",),
            )
        )
        return out
    if not vk.ok:
        out.append(
            _f(
                "vulkan.devices",
                Severity.DEGRADED,
                M("Vulkan did not start"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M("vulkaninfo failed, so no GPU can run RIFE. Only MVTools (CPU) works."),
                fix=M("Install or repair the Vulkan loader and your GPU's Vulkan driver."),
                commands=("sudo dnf install vulkan-loader mesa-vulkan-drivers",),
                evidence=vk.tail(),
            )
        )
        return out

    devs = hw.info.vulkan
    lines = tuple(
        f"{d.index}: {d.name} ({d.device_type}"
        + (", software, not used)" if d.device_type == "cpu" else ")")
        for d in devs
    )
    gpus = [d for d in devs if d.device_type != "cpu"]
    if not gpus:
        why = "llvmpipe only" if devs else "no Vulkan device"
        out.append(
            _f(
                "vulkan.devices",
                Severity.DEGRADED,
                M("CPU-only: MVTools interpolation, RIFE unavailable"),
                code=ErrorCode.VULKAN_CPU_ONLY,
                cause=M(
                    "Vulkan reports {why}; RIFE never runs on a software device.", {"why": why}
                ),
                fix=M(
                    "Install your GPU's Vulkan driver (mesa-vulkan-drivers for AMD/Intel, "
                    "the RPM Fusion driver for NVIDIA)."
                ),
                commands=("sudo dnf install mesa-vulkan-drivers",),
                evidence=lines,
            )
        )
        return out
    chosen = next((d for d in devs if d.uuid == hw.info.interpolation_device), gpus[0])
    out.append(
        _f(
            "vulkan.loader",
            Severity.OK,
            M("Vulkan: {name}", {"name": chosen.name}),
            cause=M(
                "RIFE will run on {name} (Vulkan device {i}).",
                {"name": chosen.name, "i": chosen.index},
            ),
            evidence=lines,
        )
    )
    if gpu_override:
        o = gpu_override.strip().lower()
        if not any(d.uuid == o or str(d.index) == o for d in gpus):
            out.append(
                _f(
                    "vulkan.override",
                    Severity.INFO,
                    M("The GPU chosen in settings was not found"),
                    cause=M(
                        "config gpu = {gpu} matches no Vulkan GPU; using {name}.",
                        {"gpu": gpu_override, "name": chosen.name},
                    ),
                    fix=M("Pick a GPU again in the settings."),
                    evidence=lines,
                )
            )
    interp = interpolation_pci(hw)
    decode = hw.info.decode_device
    if interp and decode and interp != decode:
        out.append(
            _f(
                "vulkan.decode_device",
                Severity.INFO,
                M("Video is decoded and interpolated on different GPUs"),
                cause=M(
                    "The display GPU is {d}, interpolation runs on {i}; every frame "
                    "is copied across PCIe.",
                    {"d": decode, "i": interp},
                ),
                fix=M("Choose the GPU that drives the display if it is fast enough."),
            )
        )
    return out


__all__ = ["vulkan_findings", "gpu_icds", "LOADER_PATHS", "ICD_DIRS"]
