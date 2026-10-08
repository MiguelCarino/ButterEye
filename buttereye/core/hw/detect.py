# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""GPU, Vulkan device and display detection (SCOPE §4.5, F9).

Sources, all read-only: ``/sys/class/drm/card*/device`` (+ hwdata ``pci.ids``),
``vulkaninfo`` text output (subprocess) and ``nvidia-smi`` (subprocess, optional).
No NVIDIA or Vulkan library is ever loaded into this process (§8.1).

CPU Vulkan devices (llvmpipe/lavapipe) are listed but never chosen as the
interpolation device. With several non-CPU devices the discrete device with the
largest device-local heap wins; ties go to the lowest Vulkan index, so the
choice is deterministic. ``choose_device`` honours an override (config ``gpu``).

The parsing functions are pure so that fixtures can test them.
"""

from __future__ import annotations

import asyncio
import functools
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.types import GpuInfo, HardwareInfo, Paths, VulkanDevice

SYS_ROOT = Path("/sys")
PCI_IDS = (Path("/usr/share/hwdata/pci.ids"), Path("/usr/share/misc/pci.ids"))

VENDOR_NAMES: dict[int, str] = {
    0x10DE: "NVIDIA",
    0x1002: "AMD",
    0x1022: "AMD",
    0x8086: "Intel",
    0x10005: "Mesa",
    0x13B5: "ARM",
    0x5143: "Qualcomm",
    0x1AF4: "virtio",
}

DeviceType = Literal["discrete", "integrated", "virtual", "cpu", "other"]
_TYPES: dict[str, DeviceType] = {
    "DISCRETE_GPU": "discrete",
    "INTEGRATED_GPU": "integrated",
    "VIRTUAL_GPU": "virtual",
    "CPU": "cpu",
}


# ---------------------------------------------------------------------------
# Vulkan
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VkParsed:
    """One Vulkan device plus the facts that do not fit ``VulkanDevice``."""

    device: VulkanDevice
    vendor_id: int | None
    pci: str | None  # "0000:08:00.0" from VkPhysicalDevicePCIBusInfoPropertiesEXT
    driver_info: str | None


_GPU_HDR = re.compile(r"^GPU(\d+):\s*$")
_KV = re.compile(r"^\s*([A-Za-z]+)\s*=\s*(.*?)\s*$")
_HEAP_SIZE = re.compile(r"^\s*size\s*=\s*(\d+)")


def _int(text: str | None) -> int | None:
    if text is None:
        return None
    m = re.match(r"\s*(0x[0-9a-fA-F]+|\d+)", text)
    if not m:
        return None
    return int(m.group(1), 0)


def _device_type(raw: str | None) -> DeviceType:
    if not raw:
        return "other"
    key = raw.replace("VK_", "").replace("PHYSICAL_DEVICE_TYPE_", "")
    return _TYPES.get(key, "other")


def _parse_block(index: int, lines: Sequence[str]) -> VkParsed:
    first: dict[str, str] = {}
    heaps: list[tuple[int, bool]] = []
    in_mem = False
    cur_size: int | None = None
    cur_local = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("VkPhysicalDeviceMemoryProperties"):
            in_mem = True
            continue
        if in_mem:
            if stripped.startswith("memoryTypes"):
                if cur_size is not None:
                    heaps.append((cur_size, cur_local))
                cur_size, in_mem = None, False
                continue
            if stripped.startswith("memoryHeaps["):
                if cur_size is not None:
                    heaps.append((cur_size, cur_local))
                cur_size, cur_local = None, False
                continue
            m = _HEAP_SIZE.match(ln)
            if m:
                cur_size = int(m.group(1))
                continue
            if "MEMORY_HEAP_DEVICE_LOCAL_BIT" in stripped:
                cur_local = True
            continue
        m = _KV.match(ln)
        if m and m.group(1) not in first:
            first[m.group(1)] = m.group(2)
    if in_mem and cur_size is not None:
        heaps.append((cur_size, cur_local))

    vendor_id = _int(first.get("vendorID"))
    vendor = VENDOR_NAMES.get(vendor_id, f"0x{vendor_id:04x}") if vendor_id is not None else ""
    dtype = _device_type(first.get("deviceType"))
    if dtype == "cpu" and vendor_id is not None and vendor_id >= 0x10000:
        vendor = "Mesa"
    local = [s for s, is_local in heaps if is_local]
    heap = max(local) if local else (max(s for s, _ in heaps) if heaps else None)
    if dtype == "cpu":
        heap = None  # system RAM, not VRAM
    pci = None
    parts = [_int(first.get(k)) for k in ("pciDomain", "pciBus", "pciDevice", "pciFunction")]
    if all(p is not None for p in parts):
        d, b, dev, f = (p or 0 for p in parts)
        pci = f"{d:04x}:{b:02x}:{dev:02x}.{f:x}"
    uuid = first.get("deviceUUID", "").lower() or f"vulkan-index-{index}"
    return VkParsed(
        VulkanDevice(
            index=index,
            uuid=uuid,
            name=first.get("deviceName", f"Vulkan device {index}"),
            vendor=vendor,
            device_type=dtype,
            heap_bytes=heap,
        ),
        vendor_id,
        pci,
        first.get("driverInfo"),
    )


def parse_vulkaninfo(text: str) -> tuple[VkParsed, ...]:
    """Parse ``vulkaninfo`` full text or ``--summary`` output (all devices)."""
    lines = text.splitlines()
    start = 0
    for marker in ("Device Properties and Extensions:", "Devices:"):
        idx = next((i for i, ln in enumerate(lines) if ln.strip() == marker), None)
        if idx is not None:
            start = idx + 1
            break
    blocks: list[tuple[int, list[str]]] = []
    for ln in lines[start:]:
        m = _GPU_HDR.match(ln)
        if m:
            blocks.append((int(m.group(1)), []))
        elif blocks:
            blocks[-1][1].append(ln)
    return tuple(_parse_block(i, body) for i, body in blocks)


def choose_device(devices: Sequence[VulkanDevice], override: str | None = None) -> str | None:
    """The interpolation device's UUID; never a CPU device. ``override`` is a
    Vulkan UUID or index (config ``gpu``) and wins when it names a usable device."""
    usable = [d for d in devices if d.device_type != "cpu"]
    if not usable:
        return None
    if override:
        o = override.strip().lower()
        for d in usable:
            if d.uuid == o or str(d.index) == o:
                return d.uuid
    rank = {"discrete": 0, "integrated": 1, "virtual": 2, "other": 3}
    best = sorted(
        usable, key=lambda d: (rank.get(d.device_type, 3), -(d.heap_bytes or 0), d.index)
    )[0]
    return best.uuid


# ---------------------------------------------------------------------------
# sysfs / nvidia-smi
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SysGpu:
    card: str  # "card1"
    pci: str  # "0000:08:00.0"
    vendor_id: int
    device_id: int
    driver: str | None  # kernel driver ("nvidia", "amdgpu", "i915", ...)
    vram_bytes: int | None  # amdgpu mem_info_vram_total
    display_connected: bool  # a connector of this card reports "connected"


@dataclass(frozen=True, slots=True)
class SmiGpu:
    pci: str
    name: str
    driver: str
    vram_bytes: int | None
    compute_cap: str | None
    uuid: str  # "GPU-bd54..."; the Vulkan deviceUUID is the same without "GPU-"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def norm_pci(pci: str) -> str:
    """'00000000:08:00.0' / '0000:08:00.0' -> '0000:08:00.0' (lowercase)."""
    pci = pci.strip().lower()
    m = re.match(r"^(?:([0-9a-f]+):)?([0-9a-f]{1,2}):([0-9a-f]{1,2})\.([0-7])$", pci)
    if not m:
        return pci
    dom = int(m.group(1) or "0", 16)
    return f"{dom:04x}:{int(m.group(2), 16):02x}:{int(m.group(3), 16):02x}.{m.group(4)}"


def read_sysfs_gpus(sys_root: Path | None = None) -> tuple[SysGpu, ...]:
    drm = (sys_root or SYS_ROOT) / "class" / "drm"
    try:
        names = sorted(os.listdir(drm))
    except OSError:
        return ()
    out: list[SysGpu] = []
    seen: set[str] = set()
    for name in names:
        if not re.fullmatch(r"card\d+", name):
            continue
        dev = drm / name / "device"
        uevent = _read(dev / "uevent") or ""
        info = dict(ln.split("=", 1) for ln in uevent.splitlines() if "=" in ln)
        pci = info.get("PCI_SLOT_NAME")
        vendor = _int(_read(dev / "vendor"))
        device = _int(_read(dev / "device"))
        if not pci or vendor is None or device is None:
            continue
        pci = norm_pci(pci)
        if pci in seen:
            continue
        seen.add(pci)
        connected = any(
            _read(drm / c / "status") == "connected" for c in names if c.startswith(name + "-")
        )
        out.append(
            SysGpu(
                card=name,
                pci=pci,
                vendor_id=vendor,
                device_id=device,
                driver=info.get("DRIVER"),
                vram_bytes=_int(_read(dev / "mem_info_vram_total")),
                display_connected=connected,
            )
        )
    return tuple(out)


SMI_QUERY = (
    "nvidia-smi",
    "--query-gpu=pci.bus_id,name,driver_version,memory.total,compute_cap,uuid",
    "--format=csv,noheader,nounits",
)


def parse_nvidia_smi(text: str) -> tuple[SmiGpu, ...]:
    out: list[SmiGpu] = []
    for ln in text.splitlines():
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 6 or not parts[0]:
            continue
        mib = _int(parts[3])
        cc = parts[4] if re.fullmatch(r"\d+\.\d+", parts[4]) else None
        out.append(
            SmiGpu(
                pci=norm_pci(parts[0]),
                name=parts[1],
                driver=parts[2],
                vram_bytes=mib * 2**20 if mib is not None else None,
                compute_cap=cc,
                uuid=parts[5],
            )
        )
    return tuple(out)


@functools.cache
def _pci_ids_file() -> Path | None:
    return next((p for p in PCI_IDS if p.is_file()), None)


def pci_name(vendor_id: int, device_id: int, ids_file: Path | None = None) -> str | None:
    """Device name from hwdata ``pci.ids`` (None when not listed)."""
    path = ids_file or _pci_ids_file()
    if path is None:
        return None
    v, d = f"{vendor_id:04x}", f"{device_id:04x}"
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            in_vendor = False
            for ln in fh:
                if ln.startswith("C "):
                    return None  # device classes follow the vendor list
                if not ln.strip() or ln.startswith("#"):
                    continue
                if not ln.startswith("\t"):
                    in_vendor = ln[:4].lower() == v
                    continue
                if in_vendor and not ln.startswith("\t\t") and ln[1:5].lower() == d:
                    return ln[5:].strip() or None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HwProbe:
    """Everything the doctor needs from detection (``hardware()`` returns ``.info``)."""

    info: HardwareInfo
    vulkan: tuple[VkParsed, ...]
    vulkaninfo: CmdResult | None  # None: not run
    smi: CmdResult | None
    sysfs: tuple[SysGpu, ...]
    display_pci: str | None


def _vendor_name(vendor_id: int) -> str:
    return VENDOR_NAMES.get(vendor_id, f"0x{vendor_id:04x}")


def build_hardware(
    sysfs: Sequence[SysGpu],
    vulkan: Sequence[VkParsed],
    smi: Sequence[SmiGpu],
    *,
    cpu_threads: int,
    override: str | None = None,
    ids_file: Path | None = None,
) -> tuple[HardwareInfo, str | None]:
    """Pure assembly. Returns (HardwareInfo, display GPU pci hint)."""
    smi_by_pci = {g.pci: g for g in smi}
    vk_by_pci = {v.pci: v for v in vulkan if v.pci}
    vk_by_uuid = {v.device.uuid: v for v in vulkan}
    gpus: list[GpuInfo] = []
    for g in sysfs:
        s = smi_by_pci.get(g.pci)
        vk = vk_by_pci.get(g.pci)
        if vk is None and s is not None:
            vk = vk_by_uuid.get(s.uuid.lower().removeprefix("gpu-"))
        name = (
            (s.name if s else None)
            or (vk.device.name if vk else None)
            or pci_name(g.vendor_id, g.device_id, ids_file)
            or f"PCI device {g.vendor_id:04x}:{g.device_id:04x}"
        )
        if s is not None:
            driver: str | None = s.driver
        elif vk is not None and vk.driver_info:
            driver = vk.driver_info
        else:
            driver = g.driver
        vram = s.vram_bytes if s and s.vram_bytes else g.vram_bytes
        if vram is None and vk is not None:
            vram = vk.device.heap_bytes
        gpus.append(
            GpuInfo(
                pci=g.pci,
                vendor=_vendor_name(g.vendor_id),
                name=name,
                driver=driver,
                vram_bytes=vram,
                compute_cap=s.compute_cap if s else None,
            )
        )
    devices = tuple(v.device for v in vulkan)
    interp = choose_device(devices, override)
    connected = [g.pci for g in sysfs if g.display_connected]
    display_pci = connected[0] if len(connected) == 1 else None
    if display_pci is None and len(sysfs) == 1:
        display_pci = sysfs[0].pci
    info = HardwareInfo(
        gpus=tuple(gpus),
        vulkan=devices,
        interpolation_device=interp,
        decode_device=display_pci,
        cpu_threads=cpu_threads,
    )
    return info, display_pci


def interpolation_pci(probe: HwProbe) -> str | None:
    """PCI address of the chosen interpolation device (None if unknown)."""
    uuid = probe.info.interpolation_device
    for v in probe.vulkan:
        if v.device.uuid == uuid:
            if v.pci:
                return v.pci
            for g in probe.sysfs:
                if v.vendor_id == g.vendor_id:
                    return g.pci
    return None


def cpu_threads() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError, OSError:
        return os.cpu_count() or 1


async def probe_hardware(
    paths: Paths | None = None,
    *,
    sys_root: Path | None = None,
    override: str | None = None,
    timeout_s: float = 6.0,
) -> HwProbe:
    """Run the detection subprocesses concurrently and assemble the result."""
    sysfs = read_sysfs_gpus(sys_root)
    want_smi = any(g.vendor_id == 0x10DE for g in sysfs) or Path("/dev/nvidia0").exists()
    vk_task = asyncio.ensure_future(run(("vulkaninfo",), timeout_s=timeout_s))
    smi_res: CmdResult | None = None
    try:
        if want_smi:
            smi_res = await run(SMI_QUERY, timeout_s=min(timeout_s, 4.0))
        vk_res = await vk_task
    finally:
        if not vk_task.done():
            vk_task.cancel()
    vulkan = parse_vulkaninfo(vk_res.stdout) if vk_res.ok else ()
    smi = parse_nvidia_smi(smi_res.stdout) if smi_res is not None and smi_res.ok else ()
    info, display = build_hardware(sysfs, vulkan, smi, cpu_threads=cpu_threads(), override=override)
    return HwProbe(info, vulkan, vk_res, smi_res, sysfs, display)


async def hardware(paths: Paths) -> HardwareInfo:
    """Provider (GUI.md §1): GPUs, Vulkan devices, chosen and decode devices."""
    return (await probe_hardware(paths)).info


__all__ = [
    "VkParsed",
    "SysGpu",
    "SmiGpu",
    "HwProbe",
    "parse_vulkaninfo",
    "parse_nvidia_smi",
    "read_sysfs_gpus",
    "choose_device",
    "build_hardware",
    "pci_name",
    "norm_pci",
    "interpolation_pci",
    "probe_hardware",
    "hardware",
]
