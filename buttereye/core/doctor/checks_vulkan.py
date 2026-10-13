# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Vulkan checks (SCOPE F1 "Also reports", §4.5, §5.3.2): loader, ICDs, devices
(CPU devices filtered out), chosen device and the decode-vs-interpolation GPU."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from buttereye.core import osapi
from buttereye.core.doctor import brew
from buttereye.core.doctor.checks_pkgs import Fix, platform_fix
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.hw.detect import HwProbe, interpolation_pci
from buttereye.core.types import Finding, Msg, Section, Severity, homebrew_prefix

if osapi.IS_WINDOWS:
    # the GPU driver installs the loader; driver manifests are found through the
    # registry, so there are no ICD folders to read (the device list covers it)
    _SYSTEM = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    LOADER_PATHS: tuple[Path, ...] = (_SYSTEM / "vulkan-1.dll",)
    ICD_DIRS: tuple[Path, ...] = ()
elif osapi.IS_MACOS:
    # macOS has no Vulkan of its own: the loader is Homebrew's vulkan-loader and the
    # only driver is MoltenVK, whose manifest Homebrew puts in <prefix>/etc. The
    # prefix moves (/opt/homebrew on arm64, /usr/local on Intel), so it is derived
    # through the one helper the generated scripts and Paths already agree with -
    # never hardcoded, and never asked of `brew` (see doctor/brew.py on why not).
    # /usr/local is also where LunarG's macOS SDK installs a loader of its own.
    _BREW = homebrew_prefix()
    LOADER_PATHS = (
        _BREW / "lib/libvulkan.1.dylib",
        _BREW / "lib/libvulkan.dylib",
        Path("/usr/local/lib/libvulkan.1.dylib"),
    )
    ICD_DIRS = (
        _BREW / "etc/vulkan/icd.d",
        _BREW / "share/vulkan/icd.d",
        Path("/usr/local/share/vulkan/icd.d"),
    )
else:
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
#: Windows: every Vulkan problem is fixed by the GPU vendor's driver
_DRIVER = M("Install or update your graphics driver from AMD, Intel or NVIDIA.")
#: macOS: there is no vendor driver to install or update - the Metal driver is part
#: of the system, and MoltenVK is what turns it into a Vulkan device.
_MOLTEN = M(
    "Install MoltenVK, which runs Vulkan on Apple's Metal. The GPU driver itself is "
    "part of macOS, so there is nothing else to update."
)
#: macOS with MoltenVK installed and Vulkan running: the driver is not what is
#: missing, so the only honest thing to say is what the run actually measured.
_MOLTEN_PRESENT = M(
    "A MoltenVK driver manifest is installed, so MoltenVK is not what is missing: "
    "Vulkan started and still offered no GPU. Run vulkaninfo yourself to see what it "
    "reports, and check that the loader and MoltenVK are the versions Homebrew "
    "installed."
)
#: macOS with MoltenVK installed and Vulkan refusing to start: the driver is
#: accounted for, so the loader is the piece left to repair.
_LOADER_REPAIR = M(
    "MoltenVK is installed, so it is not what is missing: reinstall the Vulkan loader "
    "and run the checks again."
)
#: The two variables the Vulkan loader lets the environment put in place of the
#: driver manifests it would have found. Read here only to recognise the mock-ICD
#: trap in ``_mock_icd_env``: ButterEye's own scan reads ``ICD_DIRS``, so when one
#: of these is set the loader and ``gpu_icds`` are looking at different drivers.
_ICD_ENV = ("VK_DRIVER_FILES", "VK_ICD_FILENAMES")
#: Homebrew's name for MoltenVK, asked of the Cellar in ``_driver_installed``.
_FORMULA_MOLTEN = "molten-vk"
#: ICD manifests that only provide CPU, software or translation devices.
#:
#: ``VkICD_mock_icd`` is the mock driver the vulkan-tools package ships (on macOS at
#: ``<brew prefix>/opt/vulkan-tools/lib/mock_icd/``, and its brew caveat invites the
#: user to point ``VK_ICD_FILENAMES`` at it). It is a software stub that answers
#: VIRTUAL_GPU, named "Vulkan Mock Device", so counting it would report a software
#: device as the GPU. MoltenVK is deliberately *not* here: it is a translation layer
#: like dzn, but on macOS it is the only route to the real GPU.
CPU_ICDS = ("lvp_icd", "dzn_icd", "VkICD_mock_icd")


def _fix(
    fix: Msg,
    commands: tuple[str, ...],
    *,
    windows: Msg | None = None,
    macos: Msg | None = None,
    macos_commands: tuple[str, ...] = (),
) -> Fix:
    """``fix=``/``commands=`` with a macOS arm ahead of ``checks_pkgs.platform_fix``:
    the Homebrew lines there, otherwise byte for byte what platform_fix already said.
    The brew lines are written out here as the ``dnf`` ones are - ``commands.BREW``
    maps the tools ButterEye drives, not the Vulkan pieces it only reports on."""
    if osapi.IS_MACOS:
        return {"fix": macos or fix, "commands": macos_commands}
    if windows is None:
        return platform_fix(fix, commands)
    return platform_fix(fix, commands, windows=windows)


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
    """Driver manifests in ``dirs`` that can offer a real GPU (``CPU_ICDS`` left out)."""
    if dirs is None:
        dirs = ICD_DIRS
    out: list[Path] = []
    for d in dirs:
        try:
            out += [p for p in sorted(d.glob("*.json")) if not p.name.startswith(CPU_ICDS)]
        except OSError:
            continue
    return tuple(out)


def _driver_installed(icds: Sequence[Path]) -> bool:
    """macOS: is a real Vulkan driver installed, whatever Vulkan then answered?

    Two routes, because either one alone is blind in a case the other sees.
    ``icds`` is the manifest scan, which finds a MoltenVK that LunarG's macOS SDK
    or a hand install put in a directory Homebrew does not own; Homebrew's Cellar
    finds the formula even when its manifest sits somewhere ``ICD_DIRS`` does not
    list. Either answer means "installed", and that is the fact the fix below
    needs: an installed driver is not a driver to install again.
    """
    if not osapi.IS_MACOS:
        # Linux and Windows never reach the macOS arm of _fix, so there is no
        # reason to walk a Cellar that is not there to fill in a discarded answer
        return False
    return bool(icds) or brew.keg(_FORMULA_MOLTEN) is not None


def _mock_icd_env() -> tuple[str, str] | None:
    """``(variable, manifest name)`` when the environment has pointed the Vulkan
    loader at nothing but CPU drivers, else None.

    Nothing in ButterEye sets or reads these for its own scan - ``gpu_icds``
    reads the directories - but the loader obeys them and they *replace* what it
    would have found. ``vulkan-tools``'s own brew caveat invites the user to
    export ``VK_ICD_FILENAMES=<prefix>/opt/vulkan-tools/lib/mock_icd/VkICD_mock_icd.json``,
    and a user who did has a Vulkan that answers only the mock device while the
    MoltenVK they installed is never loaded. That is a real misconfiguration with
    a one-line fix, and naming it beats advising a reinstall.

    Only the manifest's file name is reported, never the value: the path can run
    through a home directory, and SCOPE keeps machine identifiers out of
    findings and out of ``doctor --report``.
    """
    for var in _ICD_ENV:
        files = [p for p in (os.environ.get(var) or "").split(os.pathsep) if p]
        if not files:
            continue
        # the first one that is set is the one the loader obeys: VK_DRIVER_FILES
        # replaced VK_ICD_FILENAMES and silences it, so stop here either way
        # rather than blaming a variable the loader is ignoring
        if all(Path(p).name.startswith(CPU_ICDS) for p in files):
            return var, Path(files[0]).name
        return None
    return None


def _macos_no_gpu_fix(icds: Sequence[Path], *, started: bool) -> tuple[Msg, tuple[str, ...]]:
    """The macOS fix for "Vulkan offered no GPU", which depends on what is there.

    "brew install molten-vk" is true advice only while MoltenVK is absent. With a
    driver installed - and the same doctor run proves it is, by leaving the
    ``vulkan.icd`` row silent - offering to install it again sends the user off
    to repair something that is not broken. What is known then is narrower and
    far more useful: the driver is there, and Vulkan gave nothing back anyway.

    ``started`` is whether vulkaninfo ran at all, which is what separates a
    broken loader from a loader that works and found no device.
    """
    if not _driver_installed(icds):
        if started:
            return _MOLTEN, ("brew install molten-vk",)
        return (
            M("Install or repair the Vulkan loader and MoltenVK with Homebrew."),
            ("brew install vulkan-loader molten-vk",),
        )
    mock = _mock_icd_env()
    if mock is not None:
        var, manifest = mock
        return (
            M(
                "{var} points the Vulkan loader at {icd}, a software driver, so the "
                "MoltenVK you have installed is never loaded. Unset {var} in the shell "
                "you start ButterEye from and run the checks again.",
                {"var": var, "icd": manifest},
            ),
            (),
        )
    return (_MOLTEN_PRESENT, ()) if started else (_LOADER_REPAIR, ("brew reinstall vulkan-loader",))


def vulkan_findings(
    hw: HwProbe,
    *,
    gpu_override: str | None = None,
    loader_paths: Sequence[Path] | None = None,
    icd_dirs: Sequence[Path] | None = None,
) -> list[Finding]:
    out: list[Finding] = []
    # the name is taken from the paths this call is really using, not from a
    # constant frozen at import: that way the message names the file that was
    # looked for (and a test that passes loader_paths= drives the wording too)
    searched = tuple(loader_paths or LOADER_PATHS)
    loader = next((p for p in searched if p.exists()), None)
    if loader is None:
        out.append(
            _f(
                "vulkan.loader",
                Severity.DEGRADED,
                M("The Vulkan loader is not installed"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M(
                    "RIFE needs {lib}. Without it only MVTools (CPU) works.",
                    {"lib": searched[0].name},
                ),
                **_fix(
                    M("Install vulkan-loader."),
                    ("sudo dnf install vulkan-loader",),
                    windows=_DRIVER,
                    macos=M("Install the Vulkan loader and MoltenVK with Homebrew."),
                    macos_commands=("brew install vulkan-loader molten-vk",),
                ),
            )
        )
    icds = gpu_icds(icd_dirs)
    if not icds and not osapi.IS_WINDOWS:
        out.append(
            _f(
                "vulkan.icd",
                Severity.DEGRADED,
                M("No GPU Vulkan driver is installed"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M("No Vulkan driver manifest (ICD) for a GPU was found."),
                **_fix(
                    M(
                        "AMD and Intel: install mesa-vulkan-drivers. NVIDIA: install the "
                        "RPM Fusion driver (xorg-x11-drv-nvidia)."
                    ),
                    ("sudo dnf install mesa-vulkan-drivers",),
                    macos=_MOLTEN,
                    macos_commands=("brew install molten-vk",),
                ),
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
                **_fix(
                    M("Install vulkan-tools, then run the checks again."),
                    ("sudo dnf install vulkan-tools",),
                    macos_commands=("brew install vulkan-tools",),
                ),
            )
        )
        return out
    if not vk.ok:
        mac_fix, mac_commands = _macos_no_gpu_fix(icds, started=False)
        out.append(
            _f(
                "vulkan.devices",
                Severity.DEGRADED,
                M("Vulkan did not start"),
                code=ErrorCode.VULKAN_LOADER_MISSING,
                cause=M("vulkaninfo failed, so no GPU can run RIFE. Only MVTools (CPU) works."),
                **_fix(
                    M("Install or repair the Vulkan loader and your GPU's Vulkan driver."),
                    ("sudo dnf install vulkan-loader mesa-vulkan-drivers",),
                    windows=_DRIVER,
                    macos=mac_fix,
                    macos_commands=mac_commands,
                ),
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
        # a Mac has no llvmpipe: a software-only answer there means MoltenVK is absent
        software = "only a software device" if osapi.IS_MACOS else "llvmpipe only"
        why = software if devs else "no Vulkan device"
        mac_fix, mac_commands = _macos_no_gpu_fix(icds, started=True)
        out.append(
            _f(
                "vulkan.devices",
                Severity.DEGRADED,
                M("CPU-only: MVTools interpolation, RIFE unavailable"),
                code=ErrorCode.VULKAN_CPU_ONLY,
                cause=M(
                    "Vulkan reports {why}; RIFE never runs on a software device.", {"why": why}
                ),
                **_fix(
                    M(
                        "Install your GPU's Vulkan driver (mesa-vulkan-drivers for AMD/Intel, "
                        "the RPM Fusion driver for NVIDIA)."
                    ),
                    ("sudo dnf install mesa-vulkan-drivers",),
                    windows=_DRIVER,
                    macos=mac_fix,
                    macos_commands=mac_commands,
                ),
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
