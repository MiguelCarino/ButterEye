# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3 on the dev box: the real doctor, plugins, setup smoke test (devbox/integration).

These start real mpv/vspipe/vulkaninfo/rpm/journalctl subprocesses but write
only into the test's temporary XDG folders and its buttereye-test-* runtime dir.
The GPU smoke test is short (48 source frames at 720p).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from typing import Any

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.doctor.run import doctor, plugins
from buttereye.core.doctor.smoke import smoke_test
from buttereye.core.errors import ErrorCode
from buttereye.core.paths import resolve
from buttereye.core.profiles.defaults import default_config
from buttereye.core.types import BackendId, Feature, OpState, Section, Severity

pytestmark = [pytest.mark.devbox, pytest.mark.integration]


def _rpm_installed(name: str) -> bool:
    return subprocess.run(["rpm", "-q", name], capture_output=True, check=False).returncode == 0


async def test_real_doctor(xdg_env: Any) -> None:
    paths = resolve()
    t0 = time.monotonic()
    report = await doctor(paths, default_config(), trt=False, progress=lambda p: None)
    assert time.monotonic() - t0 < 10.0
    f = {x.id: x for x in report.findings}
    assert not report.blocking, [x.id for x in report.findings if x.severity is Severity.BLOCKING]
    # in-mpv probe loads both COPR plugins from the private directory
    assert report.probe is not None
    loads = {p.name: (p.loads_in_mpv, p.active_copy) for p in report.probe.plugins}
    assert loads["RIFE-ncnn-Vulkan"] == (True, "buttereye")
    assert loads["MVTools"] == (True, "buttereye")
    assert str(f["packages.rife"].title.params["variant"]).startswith("bundled ncnn ")
    assert f["packages.licence_files"].severity is Severity.OK
    if not _rpm_installed("ffms2"):
        assert f["render.ffms2"].code is ErrorCode.FFMS2_MISSING
    if shutil.which("mkvmerge") is None:
        assert f["render.mkvmerge"].severity is Severity.DEGRADED
    assert "trt.trtexec" not in f and f["trt.off"].severity is Severity.INFO
    xid = f["gpu_fault.xid"]
    assert xid.code in (None, ErrorCode.RIFE_GPU_FAULT, ErrorCode.JOURNAL_UNREADABLE)
    assert any(d.device_type != "cpu" for d in report.hardware.vulkan)
    # runtime dir left clean, nothing in the XDG folders
    assert list(paths.runtime_dir.iterdir()) == []
    assert not paths.config_file.exists() and not paths.state_dir.exists()


async def test_real_doctor_through_facade_with_trt(xdg_env: Any) -> None:
    core = await ButterEye.open()
    try:
        op = core.doctor(trt=True)
        report = await op.result()
        assert op.state is OpState.SUCCEEDED
        trt = [x for x in report.findings if x.section is Section.TRT]
        assert trt and all(x.experimental for x in trt)
        if shutil.which("trtexec") is None:
            assert any(x.id == "trt.trtexec" and x.code is ErrorCode.TRT_UNSUPPORTED for x in trt)
        caps = await core.capabilities()
        assert caps.ok(Feature.DOCTOR) and caps.ok(Feature.SETUP)
        statuses = await core.plugins()
        assert {s.name for s in statuses} >= {"RIFE-ncnn-Vulkan", "MVTools", "vstrt"}
        sel = await core.select_backend(report)
        assert sel.backend is BackendId.RIFE_NCNN
    finally:
        await core.close(cancel_jobs=True)


async def test_real_plugins(xdg_env: Any) -> None:
    st = {s.name: s for s in await plugins(resolve())}
    assert st["RIFE-ncnn-Vulkan"].variant == "bundled ncnn 0^20250503git305837fd"
    assert st["MVTools"].loads_in_mpv is True
    assert st["vstrt"].active_copy == "none"


async def test_real_smoke_mvtools(xdg_env: Any) -> None:
    out = await smoke_test(resolve(), BackendId.MVTOOLS, model=None)
    assert out.ok, out.finding
    assert out.fps is not None and out.fps > 0


@pytest.mark.gpu
async def test_real_smoke_rife_short(xdg_env: Any) -> None:
    out = await smoke_test(
        resolve(),
        BackendId.RIFE_NCNN,
        model="rife-v4.22_lite_ensembleFalse",
        gpu_index=0,
        frames=24,
    )
    # a GPU fault during the run is reported as a finding, never as success
    if out.finding is not None:
        assert out.finding.code in (ErrorCode.RIFE_GPU_FAULT, ErrorCode.VSPIPE_FRAME_ERROR)
    else:
        assert out.ok and out.fps is not None and out.gpu_faults in (0, None)
