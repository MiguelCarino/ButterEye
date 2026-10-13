# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The platform-independent parts of macOS hardware detection (docs/PLATFORMS.md,
phase 2), tested anywhere: the two IOKit parsers, the vendor token table and the
mock-ICD exclusion. The subprocesses they read are run by ``read_iokit_gpus``,
which only macOS calls.

The ``system_profiler`` and ``ioreg`` texts below are trimmed copies of what an
Apple M2 really answered; the display block keeps its serial number on purpose,
because no field of the parsed record may carry it (SCOPE §2).
"""

from __future__ import annotations

import json

from buttereye.core.hw.detect import (
    MOCK_VENDOR_ID,
    SysGpu,
    VkParsed,
    build_hardware,
    drop_mock_devices,
    parse_ioreg_gpus,
    parse_system_profiler_gpus,
)
from buttereye.core.types import VulkanDevice

SERIAL = "fd626d62"

SP_DISPLAYS = json.dumps(
    {
        "SPDisplaysDataType": [
            {
                "_name": "Apple M2",
                "spdisplays_mtlgpufamilysupport": "spdisplays_metal4",
                "spdisplays_ndrvs": [
                    {
                        "_name": "Color LCD",
                        "_spdisplays_display-serial-number": SERIAL,
                        "_spdisplays_resolution": "1920 x 1200 @ 60.00Hz",
                        "spdisplays_connection_type": "spdisplays_internal",
                        "spdisplays_main": "spdisplays_yes",
                        "spdisplays_online": "spdisplays_yes",
                    }
                ],
                "spdisplays_vendor": "sppci_vendor_Apple",
                "sppci_bus": "spdisplays_builtin",
                "sppci_cores": "8",
                "sppci_device_type": "spdisplays_gpu",
                "sppci_model": "Apple M2",
            }
        ]
    }
)

IOREG_AGX = """\
+-o AGXAcceleratorG14G  <class AGXAcceleratorG14G, id 0x10000041b, registered>
    {
      "vendor-id" = <6b100000>
      "PerformanceStatistics" = {"In use system memory"=209682432,"recoveryCount"=0}
      "model" = "Apple M2"
      "gpu-core-count" = 8
      "IOClass" = "AGXAcceleratorG14G"
    }

"""


def test_system_profiler_gives_one_integrated_gpu_with_honest_unknowns() -> None:
    (gpu,) = parse_system_profiler_gpus(SP_DISPLAYS)
    assert gpu.name == "Apple M2" and gpu.vendor_id == 0x106B
    assert gpu.display_connected
    # no PCI bus, no PCI device id, no driver version, and no VRAM of its own:
    # unified memory is not a number this can know
    assert gpu.pci == "iokit:0" and gpu.device_id == 0
    assert gpu.driver is None and gpu.vram_bytes is None


def test_the_display_serial_number_reaches_no_field() -> None:
    (gpu,) = parse_system_profiler_gpus(SP_DISPLAYS)
    assert SERIAL in SP_DISPLAYS  # the parser was given it
    assert SERIAL not in repr(gpu)


def test_a_display_that_is_offline_is_not_a_connected_one() -> None:
    doc = json.loads(SP_DISPLAYS)
    doc["SPDisplaysDataType"][0]["spdisplays_ndrvs"][0]["spdisplays_online"] = "spdisplays_no"
    (gpu,) = parse_system_profiler_gpus(json.dumps(doc))
    assert not gpu.display_connected


def test_entries_that_are_not_gpus_are_skipped() -> None:
    doc = json.loads(SP_DISPLAYS)
    doc["SPDisplaysDataType"].append({"_name": "x", "sppci_device_type": "spdisplays_display"})
    assert len(parse_system_profiler_gpus(json.dumps(doc))) == 1


def test_a_report_that_is_not_the_report_reads_as_no_gpu() -> None:
    for text in ("", "not json", "[]", "{}", '{"SPDisplaysDataType": "x"}'):
        assert parse_system_profiler_gpus(text) == ()


def test_ioreg_is_the_same_gpu_without_the_display() -> None:
    (gpu,) = parse_ioreg_gpus(IOREG_AGX)
    assert gpu.name == "Apple M2"  # the quoted string, not a data property
    assert gpu.vendor_id == 0x106B  # <6b100000>, little-endian
    assert not gpu.display_connected  # ioreg does not say; build_hardware decides
    assert gpu.vram_bytes is None and gpu.driver is None
    assert parse_ioreg_gpus("") == () and parse_ioreg_gpus("+-o Nothing\n") == ()


def test_the_mock_icd_is_never_a_gpu() -> None:
    real = VkParsed(
        VulkanDevice(0, "0000106b-1b00", "Apple M2", "Apple", "integrated", 17179869184),
        0x106B,
        None,
        "1.4.2",
    )
    mock = VkParsed(
        VulkanDevice(1, "00000000-0000", "Vulkan Mock Device", "0xba5eba11", "virtual", 8 * 10**9),
        MOCK_VENDOR_ID,
        "0000:00:00.0",
        "",
    )
    assert drop_mock_devices((real, mock)) == (real,)
    assert drop_mock_devices((mock,)) == ()


def test_the_name_a_source_carried_wins_over_a_pci_id_it_has_not_got() -> None:
    gpu = SysGpu("gpu0", "iokit:0", 0x106B, 0, None, None, True, "Apple M2")
    info, display = build_hardware((gpu,), (), (), cpu_threads=8)
    (one,) = info.gpus
    assert one.name == "Apple M2" and one.vendor == "Apple"
    assert one.vram_bytes is None and one.driver is None
    assert display == "iokit:0" and info.decode_device == "iokit:0"
