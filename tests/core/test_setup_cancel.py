# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: setup (F0, §4.7). Cancel at each phase leaves only logs; config is written
last; re-running is idempotent. The smoke test and probe are scripted here."""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import setup
from buttereye.core.api import ButterEye
from buttereye.core.doctor.probe import ProbeOutcome
from buttereye.core.doctor.smoke import SmokeOutcome
from buttereye.core.errors import BlockingIssue, DependencyMissing, ErrorCode
from buttereye.core.paths import resolve
from buttereye.core.profiles import config as cfgmod
from buttereye.core.types import (
    BackendId,
    DoctorReport,
    Finding,
    GpuInfo,
    HardwareInfo,
    Msg,
    OpState,
    Paths,
    PluginStatus,
    ProbeResult,
    Section,
    SetupChoices,
    Severity,
    VulkanDevice,
)

GiB = 2**30


def _f(
    fid: str,
    sev: Severity = Severity.OK,
    code: ErrorCode | None = None,
    commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
) -> Finding:
    return Finding(
        fid, Section(fid.split(".")[0]), sev, code, Msg(fid), Msg(""), Msg(""), commands, evidence
    )


def devbox_report(*, blocking: bool = False) -> DoctorReport:
    findings: tuple[Finding, ...] = (
        _f("mpv.version"),
        _f("packages.rife"),
        _f("packages.mvtools"),
        _f(
            "packages.models",
            evidence=("rife-v4.22_lite_ensembleFalse", "rife-v4.26_ensembleFalse"),
        ),
        _f("render.ffms2", Severity.DEGRADED, ErrorCode.FFMS2_MISSING, ("sudo dnf install ffms2",)),
        _f(
            "render.mkvmerge",
            Severity.DEGRADED,
            ErrorCode.PKG_MISSING,
            ("sudo dnf install mkvtoolnix",),
        ),
    )
    if blocking:
        findings += (_f("mpv.vf_vapoursynth", Severity.BLOCKING, ErrorCode.MPV_NO_VS_FILTER),)
    hw = HardwareInfo(
        (GpuInfo("0000:08:00.0", "NVIDIA", "RTX 4090", "615.71.09", 24 * GiB, "8.9"),),
        (VulkanDevice(0, "u-nv", "NVIDIA GeForce RTX 4090", "NVIDIA", "discrete", 24 * GiB),),
        "u-nv",
        "0000:08:00.0",
        24,
    )
    probe = ProbeResult(
        "VapourSynth R72",
        "3.14.7",
        (
            PluginStatus("RIFE-ncnn-Vulkan", "p", "1", "buttereye", True, None, ()),
            PluginStatus("MVTools", "p", "1", "buttereye", True, None, ()),
        ),
        False,
    )
    return DoctorReport(findings, hw, probe, 1.0, False)


class Script:
    """Scripted smoke/probe: optionally blocks at a phase until cancelled."""

    def __init__(self) -> None:
        self.block_at: str | None = None
        self.reached: dict[str, asyncio.Event] = {}
        self.calls: list[str] = []

    def event(self, name: str) -> asyncio.Event:
        return self.reached.setdefault(name, asyncio.Event())

    async def _phase(self, name: str) -> None:
        self.calls.append(name)
        self.event(name).set()
        if self.block_at == name:
            await asyncio.sleep(3600)

    async def smoke(self, paths: Paths, backend: BackendId, **kw: Any) -> SmokeOutcome:
        await self._phase("smoke")
        return SmokeOutcome(True, 143.8, None, ("vspipe -p ...", "Output 96 frames"), 0)

    async def probe(self, paths: Paths, mpv: str, **kw: Any) -> ProbeOutcome:
        await self._phase("probe")
        raw = {
            "plugins": [
                {"ns": "rife", "loaded": True, "loaded_from": "/x/librife.so"},
                {"ns": "mv", "loaded": True, "loaded_from": "/x/mvtools.so"},
            ]
        }
        return ProbeOutcome(raw, None, None)


@pytest.fixture
def script(xdg_env: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Script]:
    s = Script()
    monkeypatch.setattr(setup, "smoke_test", s.smoke)
    monkeypatch.setattr(setup, "run_probe", s.probe)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: f"/usr/bin/{n}")
    yield s


def snapshot(xdg: Any) -> set[str]:
    out: set[str] = set()
    for base in (xdg.home, xdg.config, xdg.data, xdg.cache, xdg.state):
        for root, dirs, files in os.walk(base):
            for n in files + dirs:
                out.add(os.path.relpath(os.path.join(root, n), base.parent))
    return out


def only_logs(xdg: Any) -> bool:
    allowed = {"state/buttereye", "state/buttereye/logs"}
    return all(p in allowed or p.startswith("state/buttereye/logs/") for p in snapshot(xdg))


async def test_plan_is_in_memory(script: Script, xdg_env: Any) -> None:
    before = snapshot(xdg_env)
    plan = await setup.plan(resolve(), devbox_report(), trt_experimental=False)
    assert snapshot(xdg_env) == before == set()
    assert plan.proposed.backend is BackendId.RIFE_NCNN
    assert plan.proposed.model == "rife-v4.26_ensembleFalse"
    assert plan.downloads == ()
    assert "sudo dnf install ffms2" in plan.dnf_lines
    assert {"ffms2", "mkvtoolnix"} <= set(plan.missing_packages)
    assert {b.id for b in plan.backends if b.available} == {BackendId.RIFE_NCNN, BackendId.MVTOOLS}


@pytest.mark.parametrize("phase", ["smoke", "probe"])
async def test_cancel_at_each_phase_leaves_only_logs(
    script: Script, xdg_env: Any, phase: str
) -> None:
    paths = resolve()
    plan = await setup.plan(paths, devbox_report(), trt_experimental=False)
    script.block_at = phase
    task = asyncio.ensure_future(
        setup.apply(
            paths, plan, SetupChoices(BackendId.RIFE_NCNN, False, frozenset()), lambda p: None
        )
    )
    await asyncio.wait_for(script.event(phase).wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths.config_file.exists()
    assert only_logs(xdg_env), snapshot(xdg_env)
    logs = setup.log_files(paths)
    assert logs and "setup stopped: CancelledError" in logs[-1].read_text()


async def test_cancel_through_facade(script: Script, xdg_env: Any) -> None:
    core = await ButterEye.open()
    try:
        plan = await core.setup_plan(devbox_report(), trt_experimental=False)
        script.block_at = "smoke"
        op = core.setup_apply(plan, SetupChoices(BackendId.MVTOOLS, False, frozenset()))
        await asyncio.wait_for(script.event("smoke").wait(), 5)
        op.cancel()
        assert await op.wait() is OpState.CANCELLED
        assert only_logs(xdg_env), snapshot(xdg_env)
    finally:
        await core.close(cancel_jobs=True)


async def test_apply_writes_config_last_and_is_idempotent(script: Script, xdg_env: Any) -> None:
    paths = resolve()
    report = devbox_report()
    plan = await setup.plan(paths, report, trt_experimental=False)
    steps: list[int] = []
    choices = SetupChoices(BackendId.RIFE_NCNN, False, frozenset())
    res = await setup.apply(paths, plan, choices, lambda p: steps.append(p.done or 0))
    assert steps == [1, 2, 3, 4]
    assert script.calls == ["smoke", "probe"]
    assert res.smoke_ok and res.smoke_fps == 143.8 and res.smoke_finding is None
    assert res.include_line == f"include={paths.mpv_include}"  # XDG config is outside HOME
    loaded = cfgmod.load(paths)
    assert loaded.exists and loaded.revision == res.config_revision
    assert loaded.config.general.backend_override is None  # = the proposed backend
    first = paths.config_file.read_bytes()

    plan2 = await setup.plan(paths, report, trt_experimental=False)
    res2 = await setup.apply(paths, plan2, choices, lambda p: None)
    assert res2.config_revision == res.config_revision
    assert paths.config_file.read_bytes() == first

    # choosing another backend records it as the override
    res3 = await setup.apply(
        paths, plan2, SetupChoices(BackendId.MVTOOLS, True, frozenset()), lambda p: None
    )
    cfg3 = cfgmod.load(paths).config
    assert cfg3.general.backend_override is BackendId.MVTOOLS
    assert cfg3.general.trt_experimental is True
    assert res3.config_revision != res.config_revision


async def test_smoke_skipped(script: Script, xdg_env: Any) -> None:
    paths = resolve()
    plan = await setup.plan(paths, devbox_report(), trt_experimental=False)
    res = await setup.apply(
        paths,
        plan,
        SetupChoices(BackendId.MVTOOLS, False, frozenset(), run_smoke_test=False),
        lambda p: None,
    )
    assert script.calls == []
    assert res.smoke_ok and res.smoke_fps is None
    assert paths.config_file.exists()


async def test_smoke_failure_reported_config_still_written(
    script: Script, xdg_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = Finding(
        "setup.smoke",
        Section.PROBE,
        Severity.DEGRADED,
        ErrorCode.RIFE_GPU_FAULT,
        Msg("GPU fault in RIFE-ncnn"),
        Msg(""),
        Msg(""),
    )

    async def smoke(paths: Paths, backend: BackendId, **kw: Any) -> SmokeOutcome:
        return SmokeOutcome(False, 70.0, bad, (), 1)

    monkeypatch.setattr(setup, "smoke_test", smoke)
    paths = resolve()
    plan = await setup.plan(paths, devbox_report(), trt_experimental=False)
    res = await setup.apply(
        paths, plan, SetupChoices(BackendId.RIFE_NCNN, False, frozenset()), lambda p: None
    )
    assert not res.smoke_ok
    assert res.smoke_finding is not None and res.smoke_finding.code is ErrorCode.RIFE_GPU_FAULT


async def test_blocking_and_unavailable_refused(script: Script, xdg_env: Any) -> None:
    paths = resolve()
    plan = await setup.plan(paths, devbox_report(blocking=True), trt_experimental=False)
    with pytest.raises(BlockingIssue) as ei:
        await setup.apply(
            paths, plan, SetupChoices(BackendId.MVTOOLS, False, frozenset()), lambda p: None
        )
    assert ei.value.findings[0].code is ErrorCode.MPV_NO_VS_FILTER
    plan = await setup.plan(paths, devbox_report(), trt_experimental=False)
    with pytest.raises(DependencyMissing):
        await setup.apply(
            paths, plan, SetupChoices(BackendId.RIFE_TRT, True, frozenset()), lambda p: None
        )
    assert script.calls == []
    assert not paths.config_file.exists()
    assert not Path(paths.logs_dir).exists()  # refused before anything ran
