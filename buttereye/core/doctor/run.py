# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The doctor orchestrator (SCOPE F1) and the plugin-status provider.

Providers (GUI.md §1)::

    async doctor(paths, cfg, *, trt, progress) -> DoctorReport
    async plugins(paths) -> tuple[PluginStatus, ...]

Sections run concurrently; every subprocess has its own timeout so the whole
run stays under ``BUDGET_S`` (10 s, F1) with no network access. Nothing is
written except the probe's short-lived JSON in the 0700 runtime directory.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import Any

from buttereye.core.capabilities import ProviderRef, resolve
from buttereye.core.doctor import checks_mpv, checks_pkgs, checks_trt, checks_vulkan, gpufault
from buttereye.core.doctor.probe import (
    ProbeOutcome,
    parse_vspipe_core,
    plugin_statuses,
    probe_findings,
    probe_result,
    run_probe,
)
from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.events import Progress
from buttereye.core.hw.detect import HwProbe, probe_hardware
from buttereye.core.ops import ProgressSink
from buttereye.core.types import (
    Config,
    DoctorReport,
    Finding,
    OpId,
    Paths,
    PluginStatus,
    Section,
    Severity,
)

BUDGET_S = 10.0
SECTION_ORDER = (
    Section.MPV,
    Section.PROBE,
    Section.PACKAGES,
    Section.VULKAN,
    Section.GPU_FAULT,
    Section.CONFLICTS,
    Section.RENDER,
    Section.CONFIG,
    Section.TRT,
)
SECTION_NAMES: dict[Section, str] = {
    Section.MPV: "mpv",
    Section.PROBE: "In-mpv probe",
    Section.PACKAGES: "Packages",
    Section.VULKAN: "Vulkan",
    Section.GPU_FAULT: "GPU fault history",
    Section.CONFLICTS: "Conflicting mpv settings",
    Section.RENDER: "Render tools",
    Section.CONFIG: "Settings",
    Section.TRT: "TensorRT",
}
_VALIDATE = ProviderRef("buttereye.core.profiles.rules", "validate")


class _Progress:
    def __init__(self, sink: ProgressSink, total: int) -> None:
        self.sink, self.total, self.done = sink, total, 0

    def section(self, s: Section) -> None:
        self.done += 1
        self.sink(
            Progress(
                OpId(""),
                M("Checked: {section}", {"section": SECTION_NAMES[s]}),
                self.done,
                self.total,
                "checks",
                None,
                None,
            )
        )


def config_findings(cfg: Config) -> list[Finding]:
    validate = resolve(_VALIDATE)
    if validate is None:
        return []
    issues = tuple(validate(cfg))
    if not issues:
        return [
            Finding(
                "config.valid",
                Section.CONFIG,
                Severity.OK,
                None,
                M("Settings are valid"),
                M(""),
                M(""),
            )
        ]
    out: list[Finding] = []
    for i, issue in enumerate(issues):
        where = issue.field or (f"line {issue.line}" if issue.line else "config.toml")
        out.append(
            Finding(
                id=f"config.issue.{i}",
                section=Section.CONFIG,
                severity=Severity.DEGRADED,
                code=issue.code,
                title=issue.message,
                cause=M("In {where}.", {"where": where}),
                fix=M("Fix it on the Profiles page or in config.toml."),
            )
        )
    return out


def _sort(findings: list[Finding]) -> tuple[Finding, ...]:
    order = {s: i for i, s in enumerate(SECTION_ORDER)}
    return tuple(sorted(findings, key=lambda f: order.get(f.section, 99)))  # stable


async def doctor(paths: Paths, cfg: Config, *, trt: bool, progress: ProgressSink) -> DoctorReport:
    t0 = time.monotonic()
    prog = _Progress(progress, len(SECTION_ORDER))
    findings: list[Finding] = []

    def left() -> float:
        return BUDGET_S - 0.5 - (time.monotonic() - t0)

    async def mpv_and_probe() -> tuple[list[Finding], ProbeOutcome | None]:
        mpv_found, facts = await checks_mpv.mpv_checks()
        conflicts = checks_mpv.conflict_findings(paths)
        sockets = checks_mpv.mpvsockets_finding(paths)
        findings.extend(mpv_found)
        prog.section(Section.MPV)
        findings.extend(conflicts + ([sockets] if sockets else []))
        prog.section(Section.CONFLICTS)
        outcome: ProbeOutcome | None = None
        if facts.usable and facts.path:
            outcome = await run_probe(paths, facts.path, trt=trt, timeout_s=max(1.0, left()))
        return mpv_found, outcome

    async with asyncio.TaskGroup() as tg:
        t_mpv = tg.create_task(mpv_and_probe())
        t_hw = tg.create_task(probe_hardware(paths, override=cfg.general.gpu))
        t_rpm = tg.create_task(checks_pkgs.rpm_query())
        t_enc = tg.create_task(checks_pkgs.ffmpeg_encoders())
        # Whole boot; doctor_xid_findings splits it at max(marker, RIFE INSTALLTIME).
        marker = gpufault.read_session_marker(paths)
        t_xid = tg.create_task(gpufault.scan_xid())
        t_vsp = tg.create_task(run(("vspipe", "--version"), timeout_s=4.0))
        t_ld = tg.create_task(checks_trt.ldconfig_p()) if trt else None

        hw: HwProbe = await t_hw
        findings.extend(checks_vulkan.vulkan_findings(hw, gpu_override=cfg.general.gpu))
        prog.section(Section.VULKAN)

        xid = await t_xid
        db = await t_rpm
        rife_pkg = db.get(checks_pkgs.PKG_RIFE)
        findings.extend(
            gpufault.doctor_xid_findings(
                xid,
                marker=marker,
                installed_at=float(rife_pkg.installtime)
                if rife_pkg is not None and rife_pkg.installtime
                else None,
            )
        )
        prog.section(Section.GPU_FAULT)

        findings.extend(checks_pkgs.package_findings(db, paths))
        prog.section(Section.PACKAGES)

        enc = await t_enc
        findings.extend(checks_pkgs.render_findings(db, enc))
        prog.section(Section.RENDER)

        findings.extend(config_findings(cfg))
        prog.section(Section.CONFIG)

        _, outcome = await t_mpv
        vsp: CmdResult = await t_vsp
        raw: dict[str, Any] | None = dict(outcome.raw) if outcome and outcome.raw else None
        statuses = plugin_statuses(db, paths, raw)
        findings.extend(
            probe_findings(outcome, statuses, vspipe_core=parse_vspipe_core(vsp.stdout))
        )
        prog.section(Section.PROBE)

        if trt and t_ld is not None:
            ld = await t_ld
            findings.extend(checks_trt.trt_findings(hw, ld, raw, trtexec=checks_trt.find_trtexec()))
        else:
            findings.append(checks_trt.trt_off_finding())
        prog.section(Section.TRT)

    probe = probe_result(raw, statuses) if raw is not None else None
    return DoctorReport(
        findings=_sort(findings),
        hardware=hw.info,
        probe=probe,
        duration_s=round(time.monotonic() - t0, 3),
        trt_included=trt,
    )


async def plugins(paths: Paths) -> tuple[PluginStatus, ...]:
    """Installed plugins and which copy is active (runs the short in-mpv probe)."""
    db_task = asyncio.ensure_future(checks_pkgs.rpm_query())
    mpv = shutil.which("mpv")
    raw: dict[str, Any] | None = None
    try:
        if mpv is not None:
            outcome = await run_probe(paths, mpv, trt=True, timeout_s=6.0)
            raw = dict(outcome.raw) if outcome.raw else None
        db = await db_task
    finally:
        if not db_task.done():
            db_task.cancel()
    return plugin_statuses(db, paths, raw)


__all__ = ["BUDGET_S", "SECTION_ORDER", "SECTION_NAMES", "doctor", "plugins", "config_findings"]
