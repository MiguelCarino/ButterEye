# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: hardware detection from recorded vulkaninfo / nvidia-smi / sysfs output (F9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from buttereye.core.hw.detect import (
    SysGpu,
    build_hardware,
    choose_device,
    norm_pci,
    parse_nvidia_smi,
    parse_vulkaninfo,
    pci_name,
    read_sysfs_gpus,
)

GiB = 2**30


def vk_block(
    idx: int,
    name: str,
    dtype: str,
    vendor: str,
    uuid: str,
    heaps: list[tuple[int, bool]],
    pci: tuple[int, int, int, int] | None = None,
    driver: str = "drv",
) -> str:
    lines = [
        f"GPU{idx}:",
        "VkPhysicalDeviceProperties:",
        "---------------------------",
        "\tapiVersion        = 1.4.351 (4211039)",
        f"\tvendorID          = {vendor}",
        "\tdeviceID          = 0x2684",
        f"\tdeviceType        = PHYSICAL_DEVICE_TYPE_{dtype}",
        f"\tdeviceName        = {name}",
        "\tpipelineCacheUUID = 7c7a36c8-0dfc-92f0-6d93-0a8d810e373f",
        "",
        "VkPhysicalDeviceLimits:",
        "\tmaxImageDimension1D = 32768",
    ]
    if pci:
        lines += [
            "VkPhysicalDevicePCIBusInfoPropertiesEXT:",
            f"\tpciDomain   = {pci[0]}",
            f"\tpciBus      = {pci[1]}",
            f"\tpciDevice   = {pci[2]}",
            f"\tpciFunction = {pci[3]}",
        ]
    lines += [
        f"\tdeviceUUID                        = {uuid}",
        f"\tdriverInfo                                           = {driver}",
        "",
        "VkPhysicalDeviceMemoryProperties:",
        "=================================",
        f"memoryHeaps: count = {len(heaps)}",
    ]
    for i, (size, local) in enumerate(heaps):
        lines += [
            f"\tmemoryHeaps[{i}]:",
            f"\t\tsize   = {size} (0x0) (x GiB)",
            f"\t\tbudget = {size} (0x0) (x GiB)",
            "\t\tusage  = 0 (0x00000000) (0.00 B)",
            "\t\tflags: count = 1" if local else "\t\tflags:",
            "\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT" if local else "\t\t\tNone",
        ]
    lines += ["memoryTypes: count = 1", "\tmemoryTypes[0]:", "\t\theapIndex     = 0", ""]
    return "\n".join(lines)


HEADER = (
    "==========\nVULKANINFO\n==========\n\nVulkan Instance Version: 1.4.341\n\n"
    "Presentable Surfaces:\n=====================\nGPU id = 0 (NVIDIA GeForce RTX 4090)\n"
    "\tsize   = 99999999999 (bogus surface line that must be ignored)\n\n"
    "Device Properties and Extensions:\n=================================\n"
)
UUID_NV = "bd54b9b9-d1a6-77a1-3aca-8d9ccd682c4c"
UUID_LVP = "6d657361-3236-2e32-2e33-000000000000"

DEVBOX_VK = HEADER + "\n".join(
    [
        vk_block(
            0,
            "NVIDIA GeForce RTX 4090",
            "DISCRETE_GPU",
            "0x10de",
            UUID_NV,
            [(25757220864, True), (25153591296, False)],
            (0, 8, 0, 0),
            "615.71.09",
        ),
        vk_block(
            1,
            "llvmpipe (LLVM 22.1.8, 256 bits)",
            "CPU",
            "0x10005",
            UUID_LVP,
            [(33538121728, True)],
            None,
            "Mesa 26.2.3 (LLVM 22.1.8)",
        ),
    ]
)

SUMMARY = """Devices:
========
GPU0:
\tapiVersion         = 1.4.351
\tvendorID           = 0x10de
\tdeviceType         = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName         = NVIDIA GeForce RTX 4090
\tdeviceUUID         = bd54b9b9-d1a6-77a1-3aca-8d9ccd682c4c
GPU1:
\tvendorID           = 0x10005
\tdeviceType         = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName         = llvmpipe (LLVM 22.1.8, 256 bits)
\tdeviceUUID         = 6d657361-3236-2e32-2e33-000000000000
"""

SMI = (
    "00000000:08:00.0, NVIDIA GeForce RTX 4090, 615.71.09, 24564, 8.9, "
    "GPU-bd54b9b9-d1a6-77a1-3aca-8d9ccd682c4c\n"
)


def test_devbox_vulkaninfo() -> None:
    devs = parse_vulkaninfo(DEVBOX_VK)
    assert [d.device.index for d in devs] == [0, 1]
    nv, lvp = devs
    assert nv.device.device_type == "discrete"
    assert nv.device.vendor == "NVIDIA"
    assert nv.device.heap_bytes == 25757220864  # largest DEVICE_LOCAL heap
    assert nv.device.uuid == UUID_NV
    assert nv.pci == "0000:08:00.0"
    assert lvp.device.device_type == "cpu"
    assert lvp.device.vendor == "Mesa"
    assert lvp.device.heap_bytes is None
    assert choose_device([d.device for d in devs]) == UUID_NV


def test_summary_format() -> None:
    devs = parse_vulkaninfo(SUMMARY)
    assert [(d.device.name, d.device.device_type) for d in devs] == [
        ("NVIDIA GeForce RTX 4090", "discrete"),
        ("llvmpipe (LLVM 22.1.8, 256 bits)", "cpu"),
    ]


def test_llvmpipe_never_selected() -> None:
    only_cpu = parse_vulkaninfo(
        HEADER + vk_block(0, "llvmpipe", "CPU", "0x10005", UUID_LVP, [(8 * GiB, True)])
    )
    devices = [d.device for d in only_cpu]
    assert choose_device(devices) is None
    assert choose_device(devices, override=UUID_LVP) is None
    assert choose_device(devices, override="0") is None
    assert choose_device([]) is None


TWO_GPU = HEADER + "\n".join(
    [
        vk_block(
            0,
            "Intel(R) Graphics (RPL-S)",
            "INTEGRATED_GPU",
            "0x8086",
            "aaaa-intel",
            [(16 * GiB, True)],
            (0, 0, 2, 0),
        ),
        vk_block(
            1,
            "AMD Radeon RX 7800 XT (RADV NAVI32)",
            "DISCRETE_GPU",
            "0x1002",
            "bbbb-amd",
            [(16 * GiB, True), (32 * GiB, False)],
            (0, 3, 0, 0),
        ),
        vk_block(2, "llvmpipe", "CPU", "0x10005", UUID_LVP, [(64 * GiB, True)]),
    ]
)


def test_two_gpu_deterministic_and_overridable() -> None:
    devices = [d.device for d in parse_vulkaninfo(TWO_GPU)]
    picks = {choose_device(devices) for _ in range(5)}
    assert picks == {"bbbb-amd"}  # discrete wins over the integrated GPU
    assert choose_device(list(reversed(devices))) == "bbbb-amd"  # order-independent
    assert choose_device(devices, override="aaaa-intel") == "aaaa-intel"
    assert choose_device(devices, override="0") == "aaaa-intel"  # index form
    assert choose_device(devices, override="nonexistent") == "bbbb-amd"


def test_two_discrete_largest_heap_then_index() -> None:
    text = HEADER + "\n".join(
        [
            vk_block(0, "A", "DISCRETE_GPU", "0x1002", "u-a", [(8 * GiB, True)]),
            vk_block(1, "B", "DISCRETE_GPU", "0x10de", "u-b", [(24 * GiB, True)]),
            vk_block(2, "C", "DISCRETE_GPU", "0x10de", "u-c", [(24 * GiB, True)]),
        ]
    )
    devices = [d.device for d in parse_vulkaninfo(text)]
    assert choose_device(devices) == "u-b"


def test_nvidia_smi() -> None:
    (g,) = parse_nvidia_smi(SMI)
    assert g.pci == "0000:08:00.0"
    assert g.driver == "615.71.09"
    assert g.vram_bytes == 24564 * 2**20
    assert g.compute_cap == "8.9"
    assert norm_pci("00000000:08:00.0") == "0000:08:00.0"
    assert parse_nvidia_smi("garbage\n") == ()


def _fake_sys(root: Path) -> Path:
    drm = root / "class/drm"
    card = drm / "card1" / "device"
    card.mkdir(parents=True)
    (card / "vendor").write_text("0x10de\n")
    (card / "device").write_text("0x2684\n")
    (card / "uevent").write_text(
        "DRIVER=nvidia\nPCI_CLASS=30000\nPCI_ID=10DE:2684\nPCI_SLOT_NAME=0000:08:00.0\n"
    )
    for conn, status in (("card1-DP-1", "connected"), ("card1-HDMI-A-1", "disconnected")):
        (drm / conn).mkdir()
        (drm / conn / "status").write_text(status + "\n")
    (drm / "renderD128").mkdir()
    return root


def test_sysfs_and_build(tmp_path: Path) -> None:
    sysfs = read_sysfs_gpus(_fake_sys(tmp_path))
    assert sysfs == (SysGpu("card1", "0000:08:00.0", 0x10DE, 0x2684, "nvidia", None, True),)
    info, display = build_hardware(
        sysfs, parse_vulkaninfo(DEVBOX_VK), parse_nvidia_smi(SMI), cpu_threads=24
    )
    (gpu,) = info.gpus
    assert gpu.name == "NVIDIA GeForce RTX 4090"
    assert gpu.driver == "615.71.09"
    assert gpu.vram_bytes == 24564 * 2**20
    assert gpu.compute_cap == "8.9"
    assert info.interpolation_device == UUID_NV
    assert info.decode_device == display == "0000:08:00.0"
    assert info.cpu_threads == 24
    assert len(info.vulkan) == 2  # llvmpipe listed, not chosen


def test_build_without_nvidia_smi_amd(tmp_path: Path) -> None:
    sysfs = (
        SysGpu("card0", "0000:00:02.0", 0x8086, 0xA780, "i915", None, True),
        SysGpu("card1", "0000:03:00.0", 0x1002, 0x747E, "amdgpu", 16 * GiB, False),
    )
    info, display = build_hardware(sysfs, parse_vulkaninfo(TWO_GPU), (), cpu_threads=8)
    amd = next(g for g in info.gpus if g.vendor == "AMD")
    assert amd.vram_bytes == 16 * GiB
    assert amd.name.startswith("AMD Radeon")
    assert amd.compute_cap is None
    assert info.interpolation_device == "bbbb-amd"
    assert display == "0000:00:02.0"  # the iGPU drives the screen: decode != interpolation


def test_pci_ids(tmp_path: Path) -> None:
    ids = tmp_path / "pci.ids"
    ids.write_text(
        "# comment\n10de  NVIDIA Corporation\n\t2684  AD102 [GeForce RTX 4090]\n"
        "\t\t10de 167c  sub\n1002  AMD\n\t747e  Navi 32\nC 00  Unclassified\n"
    )
    assert pci_name(0x10DE, 0x2684, ids) == "AD102 [GeForce RTX 4090]"
    assert pci_name(0x1002, 0x747E, ids) == "Navi 32"
    assert pci_name(0x1002, 0x0001, ids) is None


@pytest.mark.devbox
@pytest.mark.gpu
async def test_real_hardware_no_nvidia_lib_in_process(xdg_env: object) -> None:
    from buttereye.core.hw.detect import hardware
    from buttereye.core.paths import resolve

    info = await hardware(resolve())
    assert any(d.device_type != "cpu" for d in info.vulkan)
    assert info.interpolation_device is not None
    chosen = next(d for d in info.vulkan if d.uuid == info.interpolation_device)
    assert chosen.device_type != "cpu"
    maps = Path("/proc/self/maps").read_text()
    for lib in ("libnvidia-ml", "libcuda.so", "libnvinfer"):
        assert lib not in maps, lib
