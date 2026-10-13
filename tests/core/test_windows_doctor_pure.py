# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The platform-independent parts of the Windows doctor (docs/PLATFORMS.md,
phase 2), tested on Linux: the install record that stands in for rpm, the GPU
list built from Vulkan devices, the per-platform install commands and setup's
collection of winget lines. The Windows half runs in tests/windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from buttereye.core import commands
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.doctor.text import M
from buttereye.core.hw.detect import VkParsed, gpus_from_vulkan
from buttereye.core.setup import dnf_lines
from buttereye.core.types import (
    DoctorReport,
    Finding,
    HardwareInfo,
    Section,
    Severity,
    VulkanDevice,
)


def _record(install: Path, components: object) -> None:
    (install / pk.INSTALL_MANIFEST).write_text(
        json.dumps({"version": 1, "components": components}), encoding="utf-8"
    )


def test_install_record_reads_like_rpm(tmp_path: Path) -> None:
    (tmp_path / "licenses").mkdir()
    _record(
        tmp_path,
        [
            {
                "name": pk.PKG_RIFE,
                "version": "9.33-1",
                "license": "MIT",
                "provides": [["bundled(ncnn)", "20250916"]],
                "licence_files": ["licenses/rife/LICENSE"],
            },
            {"name": pk.PKG_MVTOOLS, "version": "29.2-1"},
            {"not": "a component"},
        ],
    )
    db = pk.manifest_db(tmp_path)
    assert db.available
    rife = db.get(pk.PKG_RIFE)
    assert rife is not None and rife.evr == "9.33-1" and rife.license == "MIT"
    assert pk.rife_variant(rife) == ("bundled ncnn 20250916", False)
    assert rife.licence_files == (tmp_path / "licenses/rife/LICENSE",)
    assert set(db.packages) == {pk.PKG_RIFE, pk.PKG_MVTOOLS}


@pytest.mark.parametrize("body", [None, "{not json", json.dumps({"components": "x"})])
def test_a_missing_or_broken_record_reads_as_not_checkable(
    tmp_path: Path, body: str | None
) -> None:
    if body is not None:
        (tmp_path / pk.INSTALL_MANIFEST).write_text(body, encoding="utf-8")
    db = pk.manifest_db(tmp_path)
    assert not db.available and db.packages == {} and db.error


def _vk(index: int, kind: str, pci: str | None, vendor: int | None = 0x10DE) -> VkParsed:
    dev = VulkanDevice(index, f"uuid{index}", f"GPU {index}", "NVIDIA", kind, None)  # type: ignore[arg-type]
    return VkParsed(dev, vendor, pci, None)


def test_gpu_list_from_vulkan_skips_cpu_devices_and_duplicates() -> None:
    gpus = gpus_from_vulkan(
        [
            _vk(0, "discrete", "0000:08:00.0"),
            _vk(1, "cpu", None, 0x10005),
            _vk(2, "discrete", "0000:08:00.0"),  # the same card twice (two drivers)
            _vk(3, "integrated", None, 0x8086),
        ]
    )
    assert [g.pci for g in gpus] == ["0000:08:00.0", "vk:uuid3"]
    assert [g.vendor_id for g in gpus] == [0x10DE, 0x8086]
    assert not any(g.display_connected for g in gpus)


def test_install_commands_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    # Say which platform each answer belongs to. There are three arms now, so the
    # host's own platform is never the right thing to leave unpinned.
    monkeypatch.setattr(commands.sys, "platform", "linux")
    assert commands.install_command("mkvtoolnix") == "sudo dnf install mkvtoolnix"
    monkeypatch.setattr(commands.sys, "platform", "win32")
    assert (
        commands.install_command("mkvtoolnix")
        == "winget install --exact --id MoritzBunkus.MKVToolNix"
    )
    assert commands.install_command("mpv", verb="upgrade").startswith("winget upgrade ")
    assert set(commands.WINGET) == set(commands.DNF) == set(commands.BREW)


def _finding(*cmds: str) -> Finding:
    return Finding("x", Section.MPV, Severity.DEGRADED, None, M("t"), M(""), M(""), cmds)


def test_setup_collects_winget_lines_and_their_packages() -> None:
    hw = HardwareInfo((), (), None, None, 1)
    report = DoctorReport(
        (
            _finding("winget install --exact --id shinchiro.mpv"),
            _finding("winget upgrade --exact --id VapourSynth.VapourSynth"),  # not an install
            _finding("echo not a package line"),
        ),
        hw,
        None,
        0.0,
        False,
    )
    pkgs, lines = dnf_lines(report, trt=False)
    assert pkgs == ("shinchiro.mpv",)
    assert lines == ("winget install --exact --id shinchiro.mpv",)
