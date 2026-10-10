# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: every doctor check from recorded command output -> expected finding codes.

A scripted runner replaces ``doctor.proc.run`` in every module that spawns a
program, so these tests start no process. Recorded outputs come from the dev
box (Fedora 44, mpv 0.41.0, VS R72, RTX 4090, spike RPMs).
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.doctor import (
    checks_mpv,
    checks_pkgs,
    checks_trt,
    checks_vulkan,
    gpufault,
    probe,
)
from buttereye.core.doctor import run as doctor_run
from buttereye.core.doctor.proc import CmdResult
from buttereye.core.errors import ErrorCode
from buttereye.core.events import Progress
from buttereye.core.hw import detect
from buttereye.core.paths import resolve
from buttereye.core.profiles.defaults import default_config
from buttereye.core.types import DoctorReport, Finding, Paths, Section, Severity

MPV_VERSION = (
    "mpv v0.41.0 Copyright © 2000-2025 mpv/MPlayer/mplayer2 projects\n"
    "libplacebo version: v7.360.1\nFFmpeg version: 8.0.1 (runtime 8.1.3)\n"
)
VF_HELP = (
    "Available video filters:\n  format           force output format\n"
    "  lavfi            libavfilter bridge\n  vapoursynth      VapourSynth bridge\n"
    "  gpu              vo_gpu as filter\n\nAvailable libavfilter filters:\n"
    "  vapoursynth_fake  not the real one\n"
)
VSPIPE = "VapourSynth Video Processing Library\nCore R72\nAPI R4.1\nAPI R3.6\n"
ENCODERS = (
    "Encoders:\n V..... = Video\n ------\n"
    " V....D libx264              libx264 H.264\n"
    " V....D libx265              libx265 H.265 / HEVC (codec hevc)\n"
    " V..... libsvtav1            SVT-AV1 (codec av1)\n"
    " V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)\n"
    " A....D aac                  AAC\n"
)
SMI = "00000000:08:00.0, NVIDIA GeForce RTX 4090, 615.71.09, 24564, 8.9, GPU-bd54b9b9-d1a6\n"
VULKANINFO = """Device Properties and Extensions:
=================================
GPU0:
\tvendorID          = 0x10de
\tdeviceType        = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
\tdeviceName        = NVIDIA GeForce RTX 4090
\tpciDomain   = 0
\tpciBus      = 8
\tpciDevice   = 0
\tpciFunction = 0
\tdeviceUUID                        = bd54b9b9-d1a6
VkPhysicalDeviceMemoryProperties:
memoryHeaps: count = 1
\tmemoryHeaps[0]:
\t\tsize   = 25757220864 (0x5ff400000) (23.99 GiB)
\t\tflags: count = 1
\t\t\tMEMORY_HEAP_DEVICE_LOCAL_BIT
memoryTypes: count = 1
GPU1:
\tvendorID          = 0x10005
\tdeviceType        = PHYSICAL_DEVICE_TYPE_CPU
\tdeviceName        = llvmpipe (LLVM 22.1.8, 256 bits)
\tdeviceUUID                        = 6d657361-3236
"""
VULKANINFO_CPU = VULKANINFO.split("GPU0:")[0] + "GPU0:" + VULKANINFO.split("GPU1:")[1]
XID_JSON = "\n".join(
    json.dumps(
        {
            "MESSAGE": m,
            "__MONOTONIC_TIMESTAMP": str(3_800_000_000 + i),
            "__REALTIME_TIMESTAMP": str(1_791_365_941_000_000 + i),
            "_BOOT_ID": "b",
        }
    )
    for i, m in enumerate(
        (
            "NVRM: Xid (PCI:0000:08:00): 13, Graphics Exception: SKEDCHECK36 failed",
            "NVRM: Xid (PCI:0000:08:00): 13, pid=152333, name=vspipe, Graphics Exception",
        )
    )
)


def rpm_block(
    name: str,
    evr: str,
    lic: str,
    *,
    provides: Sequence[tuple[str, str]] = (),
    requires: Sequence[str] = (),
    files: Sequence[tuple[str, str]] = (),
    installtime: int | None = None,
) -> str:
    out = [f"@@PKG\t{name}\t{evr}\t{lic}" + (f"\t{installtime}" if installtime else "")]
    out += [f"@@PROV\t{n}\t{v}" for n, v in provides]
    out += [f"@@REQ\t{r}" for r in requires]
    out += [f"@@FILE\t{fl}\t{p}" for fl, p in files]
    return "\n".join(out)


@dataclasses.dataclass
class Box:
    """A scripted machine. Tests mutate fields to model each case."""

    root: Path
    mpv_version: str = MPV_VERSION
    vf_help: str = VF_HELP
    vulkaninfo: str | None = VULKANINFO  # None: not installed
    vulkaninfo_rc: int = 0
    journal: str | None = XID_JSON  # None: permission denied
    encoders: str = ENCODERS
    probe_plugins: Mapping[str, bool] = dataclasses.field(
        default_factory=lambda: {"rife": True, "mv": True}
    )
    probe_writes: bool = True
    rpm_packages: dict[str, str] = dataclasses.field(default_factory=dict)
    which: dict[str, str | None] = dataclasses.field(default_factory=dict)
    calls: list[tuple[str, ...]] = dataclasses.field(default_factory=list)

    # ---- the fake runner ----
    async def run(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float,
        env: object = None,
        cwd: object = None,
        grace_s: float = 1.0,
    ) -> CmdResult:
        a = tuple(argv)
        self.calls.append(a)
        exe = Path(a[0]).name

        def ok(out: str, err: str = "", rc: int = 0) -> CmdResult:
            return CmdResult(a, rc, out, err, pid=4242)

        if exe == "mpv":
            if "--version" in a:
                return ok(self.mpv_version)
            if "--vf=help" in a:
                return ok(self.vf_help)
            return self._probe(a)
        if exe == "vspipe":
            return ok(VSPIPE)
        if exe == "vulkaninfo":
            if self.vulkaninfo is None:
                return CmdResult(a, None, "", "", missing=True)
            return ok(self.vulkaninfo, "", self.vulkaninfo_rc)
        if exe == "nvidia-smi":
            return ok(SMI)
        if exe == "journalctl":
            if self.journal is None:
                return ok("", "No journal files were opened due to insufficient permissions.", 1)
            return ok(self.journal, "", 0 if self.journal else 1)
        if exe == "rpm":
            if "-qf" in a:
                return ok(rpm_block("ffmpeg", "8.1.3-1.fc44", "GPLv3+"))
            names = a[4:]
            out = [self.rpm_packages[n] for n in names if n in self.rpm_packages]
            out += [f"package {n} is not installed" for n in names if n not in self.rpm_packages]
            return ok("\n".join(out) + "\n", "", 0 if len(out) == len(names) else 1)
        if exe == "ffmpeg":
            return ok(self.encoders)
        if exe == "ldconfig":
            return ok("\tlibvulkan.so.1 (libc6,x86-64) => /lib64/libvulkan.so.1\n")
        raise AssertionError(f"unexpected command {a}")

    def _probe(self, a: tuple[str, ...]) -> CmdResult:
        vf = next(x for x in a if x.startswith("--vf="))
        i = vf.index(":user-data=%") + len(":user-data=%")
        n_end = vf.index("%", i)
        n = int(vf[i:n_end])
        cfg = json.loads(vf[n_end + 1 : n_end + 1 + n])
        if not self.probe_writes:
            return CmdResult(
                a,
                0,
                "[vapoursynth] Script evaluation failed:\n"
                "[vapoursynth] ModuleNotFoundError: No module named 'vapoursynth'\n",
                "",
                pid=4243,
            )
        plugins = []
        for p in cfg["plugins"]:
            loaded = self.probe_plugins.get(p["ns"], False)
            plugins.append(
                {
                    "ns": p["ns"],
                    "path": p["path"],
                    "preloaded": False,
                    "loaded": loaded,
                    "loaded_from": p["path"] if loaded else None,
                    "error": None if loaded else "not found",
                    "version": None,
                }
            )
        raw = {
            "vs_core": "R72",
            "vs_api": "4.1",
            "python": "3.14.7",
            "executable": "/usr/bin/python3",
            "sys_path": [],
            "plugins": plugins,
            "modules": {m: False for m in cfg["modules"]},
        }
        Path(cfg["out"]).write_text(json.dumps(raw))
        return CmdResult(a, 0, "", "", pid=4243)


@pytest.fixture
def box(xdg_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    b = Box(tmp_path / "box")
    usr = b.root / "usr"
    plug = usr / "lib64/buttereye/vapoursynth"
    data = usr / "share/buttereye"
    lic = usr / "share/licenses"
    for d in (
        plug,
        data / "rife-ncnn-models/rife-v4.26_ensembleFalse",
        data / "rife-ncnn-models/rife-v4.22_lite_ensembleFalse",
        lic / "buttereye-vs-rife-ncnn",
        lic / "buttereye-vs-mvtools",
        lic / "buttereye-rife-ncnn-models",
    ):
        d.mkdir(parents=True, exist_ok=True)
    (plug / "librife.so").write_bytes(b"\x7fELF")
    (plug / "mvtools.so").write_bytes(b"\x7fELF")
    rl = [
        lic / "buttereye-vs-rife-ncnn" / n
        for n in ("LICENSE", "LICENSE.ncnn.txt", "LICENSE.glslang.txt")
    ]
    ml = [lic / "buttereye-vs-mvtools/LICENSE"]
    dl = [lic / "buttereye-rife-ncnn-models/LICENSE"]
    for p in rl + ml + dl:
        p.write_text("licence text")
    b.rpm_packages = {
        "buttereye-vs-rife-ncnn": rpm_block(
            "buttereye-vs-rife-ncnn",
            "9.33-0.2.spike.fc44",
            "MIT AND BSD-3-Clause",
            provides=[
                ("bundled(ncnn)", "0^20250503git305837fd"),
                ("bundled(glslang)", "0^gita9ac7d5f"),
            ],
            requires=["libgomp.so.1()(64bit)", "vulkan-loader"],
            files=[("", str(plug / "librife.so"))] + [("l", str(p)) for p in rl],
        ),
        "buttereye-vs-mvtools": rpm_block(
            "buttereye-vs-mvtools",
            "29.2-0.1.spike.fc44",
            "GPL-2.0-or-later AND ISC",
            files=[("l", str(p)) for p in ml],
        ),
        "buttereye-rife-ncnn-models": rpm_block(
            "buttereye-rife-ncnn-models",
            "9.33-0.1.spike.fc44",
            "MIT",
            files=[("l", str(p)) for p in dl],
        ),
        "mpv": rpm_block("mpv", "0.41.0-5.fc44", "GPL-2.0-or-later"),
        "vapoursynth-libs": rpm_block("vapoursynth-libs", "72-1.fc44", "LGPL-2.1-only"),
        "7zip": rpm_block("7zip", "26.03-1.fc44", "LGPL-2.1-or-later"),
    }
    b.which = {
        "mpv": "/usr/bin/mpv",
        "rpm": "/usr/bin/rpm",
        "7z": "/usr/bin/7z",
        "mkvmerge": None,
        "trtexec": None,
        "ldconfig": "/usr/sbin/ldconfig",
    }

    for mod in (checks_mpv, checks_pkgs, probe, gpufault, detect, doctor_run, checks_trt):
        monkeypatch.setattr(mod, "run", b.run)
    real_which = shutil.which

    def which(name: str, *a: Any, **k: Any) -> str | None:
        if name in b.which:
            return b.which[name]
        return real_which(name, *a, **k)

    monkeypatch.setattr(shutil, "which", which)
    monkeypatch.setattr(checks_pkgs, "ffms2_present", lambda db, globs=None: False)
    monkeypatch.setattr(gpufault, "current_boot_id", lambda: "b")
    monkeypatch.setattr(probe, "AUTOLOAD_DIRS", ())
    monkeypatch.setattr(probe, "AUTOLOAD_GLOBS", ())
    monkeypatch.setattr(checks_mpv, "MPVSOCKETS_DIR", b.root / "no-mpvsockets")
    sysroot = b.root / "sys"
    card = sysroot / "class/drm/card1/device"
    card.mkdir(parents=True)
    (card / "vendor").write_text("0x10de")
    (card / "device").write_text("0x2684")
    (card / "uevent").write_text("DRIVER=nvidia\nPCI_SLOT_NAME=0000:08:00.0\n")
    monkeypatch.setattr(detect, "SYS_ROOT", sysroot)
    loader = b.root / "libvulkan.so.1"
    loader.write_text("")
    icd = b.root / "icd.d"
    icd.mkdir()
    (icd / "nvidia_icd.x86_64.json").write_text("{}")
    (icd / "lvp_icd.x86_64.json").write_text("{}")
    monkeypatch.setattr(checks_vulkan, "LOADER_PATHS", (loader,))
    monkeypatch.setattr(checks_vulkan, "ICD_DIRS", (icd,))
    return b


def paths_for(box: Box) -> Paths:
    return dataclasses.replace(
        resolve(),
        rpm_plugin_dir=box.root / "usr/lib64/buttereye/vapoursynth",
        rpm_data_dir=box.root / "usr/share/buttereye",
    )


async def run_doctor(
    box: Box, *, trt: bool = False, sink: Callable[[Progress], None] | None = None
) -> DoctorReport:
    return await doctor_run.doctor(
        paths_for(box), default_config(), trt=trt, progress=sink or (lambda p: None)
    )


def by_id(r: DoctorReport) -> dict[str, Finding]:
    return {f.id: f for f in r.findings}


def codes(r: DoctorReport) -> set[ErrorCode]:
    return {f.code for f in r.findings if f.code is not None}


# ---------------------------------------------------------------------------


async def test_devbox_now(box: Box) -> None:
    seen: list[Progress] = []
    t0 = time.monotonic()
    r = await run_doctor(box, sink=seen.append)
    assert time.monotonic() - t0 < 10.0 and r.duration_s < 10.0
    f = by_id(r)
    assert not r.blocking
    assert f["render.ffms2"].code is ErrorCode.FFMS2_MISSING
    assert f["render.ffms2"].section is Section.RENDER
    assert f["render.ffms2"].commands == ("sudo dnf install ffms2",)
    # offline render isn't in this build: missing render tools are notes, not warnings
    assert f["render.ffms2"].severity is Severity.DEGRADED  # render exists now
    assert f["render.mkvmerge"].severity is Severity.DEGRADED
    assert f["render.mkvmerge"].commands == ("sudo dnf install mkvtoolnix",)
    assert f["render.hdr10_encoder"].severity is Severity.OK
    assert f["gpu_fault.xid"].code is ErrorCode.RIFE_GPU_FAULT
    assert f["packages.rife"].severity is Severity.OK
    assert f["packages.rife"].title.params["variant"] == "bundled ncnn 0^20250503git305837fd"
    assert f["packages.licence_files"].severity is Severity.OK
    assert f["probe.plugins"].severity is Severity.OK
    assert f["vulkan.loader"].severity is Severity.OK
    assert "trt.trtexec" not in f  # trtexec absent only inside the TRT section
    assert f["trt.off"].severity is Severity.INFO
    assert r.probe is not None and r.probe.vs_core == "VapourSynth R72"
    assert {p.name: p.loads_in_mpv for p in r.probe.plugins} == {
        "RIFE-ncnn-Vulkan": True,
        "MVTools": True,
    }
    assert r.hardware.interpolation_device == "bd54b9b9-d1a6"
    # progress: one per section, monotonic, ends at total
    assert [p.done for p in seen] == list(range(1, len(seen) + 1))
    assert seen[-1].done == seen[-1].total == len(doctor_run.SECTION_ORDER)
    # sections come out in display order
    order = [f.section for f in r.findings]
    assert order == sorted(order, key=list(doctor_run.SECTION_ORDER).index)
    # nothing written outside the (empty) runtime dir
    p = paths_for(box)
    assert not p.config_file.parent.exists() and not p.state_dir.exists()
    assert list(p.runtime_dir.iterdir()) == []


async def test_mpv_missing(box: Box) -> None:
    box.which["mpv"] = None
    r = await run_doctor(box)
    f = by_id(r)
    assert r.blocking
    assert f["mpv.binary"].code is ErrorCode.MPV_NOT_FOUND
    assert f["probe.run"].severity is Severity.INFO
    assert r.probe is None


async def test_mpv_without_vapoursynth(box: Box) -> None:
    box.vf_help = VF_HELP.replace("  vapoursynth      VapourSynth bridge\n", "")
    r = await run_doctor(box)
    assert by_id(r)["mpv.vf_vapoursynth"].code is ErrorCode.MPV_NO_VS_FILTER
    assert r.blocking and r.probe is None


async def test_mpv_too_old(box: Box) -> None:
    box.mpv_version = "mpv v0.40.0 Copyright\n"
    r = await run_doctor(box)
    assert by_id(r)["mpv.version"].code is ErrorCode.MPV_TOO_OLD
    assert r.blocking


def test_sandbox_detection(tmp_path: Path) -> None:
    wrapper = tmp_path / "mpv"
    wrapper.write_text('#!/bin/sh\nexec flatpak run io.mpv.Mpv "$@"\n')
    assert checks_mpv.sandbox_kind(str(wrapper)) == "flatpak"
    snap = tmp_path / "snap" / "bin"
    snap.mkdir(parents=True)
    assert checks_mpv.sandbox_kind("/snap/bin/mpv") == "snap"
    host = tmp_path / "host-mpv"
    host.write_bytes(b"\x7fELF\x02\x01")
    assert checks_mpv.sandbox_kind(str(host)) is None


async def test_flatpak_mpv_blocks(box: Box, tmp_path: Path) -> None:
    (tmp_path / "fp").mkdir()
    wrapper = tmp_path / "fp" / "mpv"
    wrapper.write_text('#!/bin/sh\nexec flatpak run io.mpv.Mpv "$@"\n')
    box.which["mpv"] = str(wrapper)
    r = await run_doctor(box)
    assert by_id(r)["mpv.host_binary"].code is ErrorCode.MPV_SANDBOXED
    assert r.blocking


def test_version_and_filter_parsers() -> None:
    assert checks_mpv.parse_version(MPV_VERSION) == (0, 41, 0)
    assert checks_mpv.parse_version("mpv 0.41.0-dev-g1234abc") == (0, 41, 0)
    assert checks_mpv.parse_version("mpv v0.42 Copyright") == (0, 42, 0)
    assert checks_mpv.parse_version("garbage") is None
    assert checks_mpv.has_vs_filter(VF_HELP)
    assert not checks_mpv.has_vs_filter("  vapoursynth_fake  x\n")


async def test_probe_failure_blocks(box: Box) -> None:
    box.probe_writes = False
    r = await run_doctor(box)
    f = by_id(r)["probe.run"]
    assert f.code is ErrorCode.PROBE_FAILED and f.severity is Severity.BLOCKING
    assert any("ModuleNotFoundError" in e for e in f.evidence)


async def test_plugin_installed_but_not_loading(box: Box) -> None:
    box.probe_plugins = {"rife": False, "mv": True}
    r = await run_doctor(box)
    f = by_id(r)["probe.plugins"]
    assert f.severity is Severity.DEGRADED and f.code is ErrorCode.PROBE_FAILED
    assert f.commands == ("sudo dnf reinstall buttereye-vs-rife-ncnn",)


async def test_system_ncnn_variant(box: Box) -> None:
    box.rpm_packages["buttereye-vs-rife-ncnn"] = rpm_block(
        "buttereye-vs-rife-ncnn",
        "9.33-0.1.spike.fc44",
        "MIT",
        requires=["libncnn.so.1()(64bit)"],
        files=[("l", str(box.root / "usr/share/licenses/buttereye-vs-rife-ncnn/LICENSE"))],
    )
    r = await run_doctor(box)
    assert by_id(r)["packages.rife"].code is ErrorCode.VARIANT_SYSTEM_NCNN


async def test_licence_file_missing(box: Box) -> None:
    (box.root / "usr/share/licenses/buttereye-vs-rife-ncnn/LICENSE.ncnn.txt").unlink()
    r = await run_doctor(box)
    f = by_id(r)["packages.licence_files"]
    assert f.code is ErrorCode.LICENCE_FILE_MISSING
    assert any("LICENSE.ncnn.txt" in e for e in f.evidence)


async def test_no_plugin_packages(box: Box) -> None:
    for n in ("buttereye-vs-rife-ncnn", "buttereye-vs-mvtools", "buttereye-rife-ncnn-models"):
        del box.rpm_packages[n]
    box.probe_plugins = {}
    r = await run_doctor(box)
    f = by_id(r)
    assert f["packages.plugins"].severity is Severity.BLOCKING
    assert f["packages.plugins"].code is ErrorCode.PKG_MISSING
    assert any(c.startswith("sudo dnf copr enable ") for c in f["packages.plugins"].commands)
    assert f["packages.rife"].code is ErrorCode.PKG_MISSING


async def test_vulkan_cases(box: Box) -> None:
    box.vulkaninfo = VULKANINFO_CPU
    f = by_id(await run_doctor(box))
    assert f["vulkan.devices"].code is ErrorCode.VULKAN_CPU_ONLY
    assert "CPU-only" in f["vulkan.devices"].title.key
    box.vulkaninfo = None
    f = by_id(await run_doctor(box))
    assert f["vulkan.devices"].code is ErrorCode.PKG_MISSING
    assert f["vulkan.devices"].commands == ("sudo dnf install vulkan-tools",)
    box.vulkaninfo, box.vulkaninfo_rc = "ERROR: Cannot create Vulkan instance.", 1
    f = by_id(await run_doctor(box))
    assert f["vulkan.devices"].code is ErrorCode.VULKAN_LOADER_MISSING


def test_vulkan_loader_and_icd_missing(tmp_path: Path) -> None:
    hw = detect.HwProbe(
        detect.build_hardware((), (), (), cpu_threads=4)[0], (), None, None, (), None
    )
    f = {
        x.id: x
        for x in checks_vulkan.vulkan_findings(
            hw, loader_paths=(tmp_path / "none",), icd_dirs=(tmp_path,)
        )
    }
    assert f["vulkan.loader"].code is ErrorCode.VULKAN_LOADER_MISSING
    assert f["vulkan.icd"].code is ErrorCode.VULKAN_LOADER_MISSING


async def test_journal_unreadable_never_ok(box: Box) -> None:
    box.journal = None
    f = by_id(await run_doctor(box))["gpu_fault.xid"]
    assert f.severity is Severity.INFO and f.code is ErrorCode.JOURNAL_UNREADABLE
    box.journal = ""
    f = by_id(await run_doctor(box))["gpu_fault.xid"]
    assert f.severity is Severity.OK


def _rife_installed_at(box: Box, when: int) -> None:
    old = box.rpm_packages["buttereye-vs-rife-ncnn"].split("\n")
    old[0] = "\t".join(old[0].split("\t")[:4]) + f"\t{when}"
    box.rpm_packages["buttereye-vs-rife-ncnn"] = "\n".join(old)


async def test_xid_before_rife_install_is_not_blamed(box: Box) -> None:
    # XID_JSON is at 1_791_365_941 s; the plugin was installed 288 s later
    _rife_installed_at(box, 1_791_366_229)
    f = by_id(await run_doctor(box))
    assert f["gpu_fault.xid"].severity is Severity.OK
    assert f["gpu_fault.xid_older"].severity is Severity.INFO
    assert f["gpu_fault.xid_older"].cause.params["n"] == 1
    # an Xid after the install time is DEGRADED BE-1030 again
    _rife_installed_at(box, 1_791_365_000)
    f = by_id(await run_doctor(box))
    assert f["gpu_fault.xid"].severity is Severity.DEGRADED
    assert f["gpu_fault.xid"].code is ErrorCode.RIFE_GPU_FAULT
    assert f["gpu_fault.xid"].title.key == "GPU fault in RIFE-ncnn"
    assert "gpu_fault.xid_older" not in f


async def test_session_marker_written_then_doctor_scans_from_it(box: Box) -> None:
    p = paths_for(box)
    f = by_id(await run_doctor(box))
    assert f["gpu_fault.xid"].severity is Severity.DEGRADED  # no marker: whole boot
    assert f["gpu_fault.xid"].cause.params["since"] == "since this boot"
    gpufault.write_session_marker(p, pids={4242}, started=1_791_365_999.0)  # after XID_JSON
    f = by_id(await run_doctor(box))
    assert f["gpu_fault.xid"].severity is Severity.OK
    assert "since the last RIFE-ncnn session started" in str(
        f["gpu_fault.xid"].cause.params["since"]
    )
    assert f["gpu_fault.xid_older"].cause.params["before"] == (
        "before the last RIFE-ncnn session started"
    )


async def test_hdr10_encoder_missing(box: Box) -> None:
    box.encoders = " ------\n V....D libx264  x\n V....D hevc_nvenc  y\n"
    f = by_id(await run_doctor(box))["render.hdr10_encoder"]
    assert f.severity is Severity.DEGRADED


async def test_conflicting_user_settings(box: Box) -> None:
    p = paths_for(box)
    p.user_mpv_conf.parent.mkdir(parents=True)
    (p.user_mpv_conf.parent / "extra.conf").write_text("save-position-on-quit\n")
    p.user_mpv_conf.write_text(
        "vo=gpu-next\nhwdec=auto-safe\n--interpolation=yes  # comment\n"
        "include=~~/extra.conf\ninclude=extra.conf\n"
        "[protocol.https]\nhwdec=auto-copy\n"
    )
    scripts = p.user_mpv_conf.parent / "scripts"
    scripts.mkdir()
    (scripts / "mpvSockets.lua").write_text("--")
    f = by_id(await run_doctor(box))
    assert f["conflicts.hwdec"].code is ErrorCode.USER_CONF_CONFLICT
    assert f["conflicts.interpolation"].code is ErrorCode.USER_CONF_CONFLICT
    assert f["conflicts.save_position"].code is ErrorCode.USER_CONF_CONFLICT
    assert f["conflicts.mpvsockets"].code is ErrorCode.MPVSOCKETS_IN_USE
    assert "conflicts.user_conf" not in f
    # the file is only read
    assert "hwdec=auto-safe" in p.user_mpv_conf.read_text()


def test_conflict_parser_edge_cases(xdg_env: Any) -> None:
    p = resolve()
    p.user_mpv_conf.parent.mkdir(parents=True)
    p.user_mpv_conf.write_text(
        "hwdec=auto-copy\nno-interpolation\nsave-position-on-quit=yes\n"
        "watch-later-options-remove=vf,sid\n[default]\nvo=gpu\n"
    )
    f = {x.id: x for x in checks_mpv.conflict_findings(p)}
    assert set(f) == {"conflicts.user_conf"}
    assert checks_mpv.hwdec_is_copy("nvdec-copy,vaapi-copy")
    assert checks_mpv.hwdec_is_copy("no")
    assert not checks_mpv.hwdec_is_copy("auto-safe")
    assert not checks_mpv.hwdec_is_copy(None)


async def test_trt_section_after_optin(box: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    # the fixture box has no TensorRT, even when the machine running the tests does
    monkeypatch.setattr(checks_trt, "find_trtexec", lambda: None)
    r = await run_doctor(box, trt=True)
    f = by_id(r)
    assert r.trt_included
    assert f["trt.trtexec"].code is ErrorCode.TRT_UNSUPPORTED
    assert f["trt.libnvinfer"].code is ErrorCode.TRT_UNSUPPORTED
    assert f["trt.vstrt"].code is ErrorCode.TRT_UNSUPPORTED
    assert f["trt.gpu"].severity is Severity.OK
    trt_rows = [x for x in r.findings if x.section is Section.TRT]
    assert trt_rows and all(x.experimental for x in trt_rows)
    assert all(x.severity is not Severity.BLOCKING for x in trt_rows)
    assert not r.blocking
    assert "trt.off" not in f


def test_libnvinfer_version(tmp_path: Path) -> None:
    lib = tmp_path / "libnvinfer.so.11.3.0"
    lib.write_text("")
    link = tmp_path / "libnvinfer.so.11"
    link.symlink_to(lib)
    res = CmdResult(("ldconfig", "-p"), 0, f"\tlibnvinfer.so.11 (libc6,x86-64) => {link}\n", "")
    assert checks_trt.libnvinfer_version(res) == (11, 3, 0)
    assert checks_trt.libnvinfer_version(CmdResult(("ldconfig",), 0, "", "")) is None


def test_rpm_parser() -> None:
    text = "\n".join(
        [
            rpm_block(
                "a",
                "1-1",
                "MIT",
                provides=[("bundled(ncnn)", "0^x")],
                files=[("l", "/l/a/LICENSE"), ("d", "/d/a/README"), ("", "/x")],
            ),
            "package b is not installed",
            rpm_block("c", "2-1", "GPL", requires=["libncnn.so.1()(64bit)"]),
        ]
    )
    db = checks_pkgs.parse_rpm_query(text)
    assert set(db) == {"a", "c"}
    assert db["a"].licence_files == (Path("/l/a/LICENSE"),)
    assert checks_pkgs.rife_variant(db["a"]) == ("bundled ncnn 0^x", False)
    assert checks_pkgs.rife_variant(db["c"]) == ("Fedora ncnn", True)
    assert checks_pkgs.parse_encoders(ENCODERS)[:2] == ("libx264", "libx265")


async def test_budget_with_slow_fakes(box: Box) -> None:
    import asyncio

    inner = box.run

    async def slow(argv: Sequence[str], **kw: Any) -> CmdResult:
        await asyncio.sleep(0.2)  # every program takes 0.2 s; sections overlap
        return await inner(argv, **kw)

    for mod in (checks_mpv, checks_pkgs, probe, gpufault, detect, doctor_run, checks_trt):
        mod.run = slow  # type: ignore[attr-defined]  # restored by the fixture
    t0 = time.monotonic()
    r = await run_doctor(box, trt=True)
    assert time.monotonic() - t0 < 2.0
    assert r.duration_s < doctor_run.BUDGET_S


def test_mpvsockets_wording_follows_attach_capability(xdg_env: Any, tmp_path: Path) -> None:
    p = resolve()
    sockets = tmp_path / "mpvSockets"
    sockets.mkdir()
    only_dir = checks_mpv.mpvsockets_finding(p, sockets, attach_available=False)
    assert only_dir is not None and only_dir.severity is Severity.INFO
    assert only_dir.cause.params["attach"] == (
        "Attaching to running players isn't in this build yet."
    )
    scripts = p.user_mpv_conf.parent / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "mpvSockets.lua").write_text("--")
    f = checks_mpv.mpvsockets_finding(p, sockets, attach_available=False)
    # Play follows the moved socket; only attaching needs a warning (errors.md BE-1041)
    assert f is not None and f.severity is Severity.INFO
    assert f.code is ErrorCode.MPVSOCKETS_IN_USE
    assert "can attach" not in f.cause.key and "isn't in this build" in str(
        f.cause.params["attach"]
    )
    assert str(scripts) in str(f.fix.params["scripts"])
    later = checks_mpv.mpvsockets_finding(p, sockets, attach_available=True)
    assert later is not None and "checking each socket" in str(later.cause.params["attach"])
    assert later.severity is Severity.DEGRADED
    # this build has no attach provider: the default follows capabilities
    default = checks_mpv.mpvsockets_finding(p, sockets)
    assert default is not None and "isn't in this build" in str(default.cause.params["attach"])


def test_attach_only_conflicts_are_notes_until_attach_exists(xdg_env: Any) -> None:
    """User mpv.conf settings only break attached players; players ButterEye starts
    override them, so they are notes while attaching isn't in the build."""
    p = resolve()
    opts = {"hwdec": "auto-safe", "interpolation": "yes", "save-position-on-quit": "yes"}
    now = checks_mpv.conflict_findings(p, opts, attach_available=False)
    assert {x.id for x in now} == {
        "conflicts.hwdec",
        "conflicts.interpolation",
        "conflicts.save_position",
    }
    assert all(x.severity is Severity.INFO for x in now)
    assert all("isn't in this build" in str(x.cause.params["tail"]) for x in now)
    later = checks_mpv.conflict_findings(p, opts, attach_available=True)
    assert all(x.severity is Severity.DEGRADED and x.cause.params["tail"] == "" for x in later)


def test_render_tools_are_notes_until_render_exists() -> None:
    db = checks_pkgs.RpmDb(available=True, packages={})
    enc = CmdResult(("ffmpeg", "-encoders"), 0, "", "")
    absent = {"mkvmerge": None, "7z": None}
    now = {
        f.id: f
        for f in checks_pkgs.render_findings(
            db, enc, which=absent, ffms2=False, render_available=False
        )
    }
    assert now["render.ffms2"].severity is Severity.INFO
    assert now["render.mkvmerge"].severity is Severity.INFO
    later = {
        f.id: f
        for f in checks_pkgs.render_findings(
            db, enc, which=absent, ffms2=False, render_available=True
        )
    }
    assert later["render.ffms2"].severity is Severity.DEGRADED
    assert later["render.mkvmerge"].severity is Severity.DEGRADED
