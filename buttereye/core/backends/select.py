# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Backend availability and selection (SCOPE §5.3, F13). Pure over the report.

``backends(report, cfg)`` says which backends can run and why (not).
``select(report, cfg, bench)`` picks one, deterministically for identical inputs:

1. A blocking report selects nothing.
2. No non-CPU Vulkan device -> only MVTools ("CPU-only: MVTools interpolation,
   RIFE unavailable" + the reason). RIFE is never returned in that case.
3. Rank: TensorRT (only with ``trt_experimental`` and every TRT check passing),
   RIFE-ncnn, MVTools. Without the opt-in TensorRT is never ranked.
4. A benchmark recommendation (no GPU faults, real time) wins; otherwise the
   §5.3 conservative defaults: RIFE v4.26 on a discrete GPU with >= 16 GiB,
   else v4.22-lite (the packaged models in the report decide what exists).
5. ``general.backend_override`` wins when that backend is available.
"""

from __future__ import annotations

from collections.abc import Sequence

from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import (
    BackendId,
    BackendStatus,
    BenchResult,
    Config,
    DoctorReport,
    Finding,
    Section,
    Selection,
    Severity,
    VulkanDevice,
)

MODEL_HIGH = "rife-v4.26_ensembleFalse"
MODEL_MID = "rife-v4.22_lite_ensembleFalse"
MODEL_LOW = "rife-v4.18_ensembleFalse"  # fallback; v4.25-lite dropped (Xid 109, M0(f))
HIGH_END_HEAP = 16 * 2**30


def _finding(report: DoctorReport, fid: str) -> Finding | None:
    return next((f for f in report.findings if f.id == fid), None)


def _problem(f: Finding | None) -> bool:
    return f is not None and f.severity in (Severity.BLOCKING, Severity.DEGRADED)


def _plugin_loads(report: DoctorReport, name: str) -> bool | None:
    if report.probe is None:
        return None
    p = next((p for p in report.probe.plugins if p.name == name), None)
    return bool(p and p.loads_in_mpv)


def gpu_devices(report: DoctorReport) -> tuple[VulkanDevice, ...]:
    return tuple(d for d in report.hardware.vulkan if d.device_type != "cpu")


def chosen_device(report: DoctorReport) -> VulkanDevice | None:
    uuid = report.hardware.interpolation_device
    return next(
        (d for d in report.hardware.vulkan if d.uuid == uuid and d.device_type != "cpu"), None
    )


def packaged_models(report: DoctorReport) -> tuple[str, ...]:
    """Model directory names the doctor found (``packages.models`` evidence)."""
    f = _finding(report, "packages.models")
    if f is None or f.severity is not Severity.OK:
        return ()
    return f.evidence


def _cpu_only_reason(report: DoctorReport) -> str:
    vk = _finding(report, "vulkan.devices")
    if vk is not None and vk.evidence:
        return "llvmpipe only"
    if _problem(_finding(report, "vulkan.loader")) or _problem(_finding(report, "vulkan.icd")):
        return "no Vulkan driver"
    return "llvmpipe only" if report.hardware.vulkan else "no Vulkan driver"


def backends(report: DoctorReport, cfg: Config) -> tuple[BackendStatus, ...]:
    blocked = report.blocking
    dev = chosen_device(report)

    # RIFE-ncnn
    rife_loads = _plugin_loads(report, "RIFE-ncnn-Vulkan")
    rife_pkg = _finding(report, "packages.rife")
    models = packaged_models(report)
    if blocked:
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            False,
            False,
            M("Setup isn't finished: fix the blocking problems first."),
        )
    elif dev is None:
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            False,
            False,
            M(
                "CPU-only: MVTools interpolation, RIFE unavailable ({why})",
                {"why": _cpu_only_reason(report)},
            ),
            next(
                (
                    f.code
                    for f in report.findings
                    if f.section is Section.VULKAN and _problem(f) and f.code
                ),
                None,
            ),
        )
    elif rife_pkg is not None and rife_pkg.code is ErrorCode.PKG_MISSING:
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            False,
            False,
            M("The RIFE plugin is not installed"),
            rife_pkg.code,
        )
    elif rife_loads is False:
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            False,
            False,
            M("The RIFE plugin does not load inside mpv"),
            ErrorCode.PROBE_FAILED,
        )
    elif not models:
        f = _finding(report, "packages.models")
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            False,
            False,
            M("No RIFE models are installed"),
            f.code if f else None,
        )
    else:
        note = ""
        if _problem(_finding(report, "gpu_fault.xid")):
            note = "; the kernel log shows recent GPU faults"
        rife = BackendStatus(
            BackendId.RIFE_NCNN,
            True,
            False,
            M("RIFE (Vulkan) on {gpu}{note}", {"gpu": dev.name, "note": note}),
        )

    # MVTools
    mv_loads = _plugin_loads(report, "MVTools")
    mv_pkg = _finding(report, "packages.mvtools")
    if blocked:
        mv = BackendStatus(
            BackendId.MVTOOLS,
            False,
            False,
            M("Setup isn't finished: fix the blocking problems first."),
        )
    elif _problem(mv_pkg):
        mv = BackendStatus(
            BackendId.MVTOOLS,
            False,
            False,
            M("The MVTools plugin is not installed"),
            mv_pkg.code if mv_pkg else None,
        )
    elif mv_loads is False:
        mv = BackendStatus(
            BackendId.MVTOOLS, False, False, M("The MVTools plugin does not load inside mpv")
        )
    elif mv_pkg is None and mv_loads is None:
        mv = BackendStatus(BackendId.MVTOOLS, False, False, M("MVTools was not checked"))
    else:
        mv = BackendStatus(BackendId.MVTOOLS, True, False, M("MVTools on the CPU"))

    # TensorRT (experimental, opt-in)
    trt_problems = [f for f in report.findings if f.section is Section.TRT and _problem(f)]
    if not cfg.general.trt_experimental:
        trt = BackendStatus(BackendId.RIFE_TRT, False, True, M("Experimental NVIDIA TensorRT: off"))
    elif not report.trt_included:
        trt = BackendStatus(
            BackendId.RIFE_TRT, False, True, M("Run the checks again to test TensorRT.")
        )
    elif trt_problems:
        trt = BackendStatus(
            BackendId.RIFE_TRT,
            False,
            True,
            M(
                "TensorRT isn't set up: {n} item(s) need attention on the System page.",
                {"n": len(trt_problems)},
            ),
            trt_problems[0].code,
        )
    elif blocked or dev is None:
        trt = BackendStatus(BackendId.RIFE_TRT, False, True, M("No usable NVIDIA GPU."))
    else:
        from buttereye.core.capabilities import static_state
        from buttereye.core.types import Feature

        st = static_state(Feature.TRT)
        if st.available:
            trt = BackendStatus(
                BackendId.RIFE_TRT,
                True,
                True,
                M("RIFE · TensorRT (experimental) on {gpu}", {"gpu": dev.name}),
            )
        else:
            trt = BackendStatus(
                BackendId.RIFE_TRT,
                False,
                True,
                M("TensorRT engine building isn't in this build yet."),
                st.code,
            )
    return (rife, mv, trt)


def _profile_for(cfg: Config, backend: BackendId | None, model: str | None) -> str:
    profiles = cfg.profiles
    if backend is not None:
        for p in profiles:
            if p.backend == backend and p.model == model:
                return p.id
        for p in profiles:
            if p.backend == backend:
                return p.id
    return profiles[0].id if profiles else ""


def _default_model(report: DoctorReport) -> str | None:
    models = packaged_models(report)
    dev = chosen_device(report)
    high = (
        dev is not None and dev.device_type == "discrete" and (dev.heap_bytes or 0) >= HIGH_END_HEAP
    )
    order = (MODEL_HIGH, MODEL_MID, MODEL_LOW) if high else (MODEL_MID, MODEL_LOW, MODEL_HIGH)
    for m in order:
        if m in models:
            return m
    return models[0] if models else None


def _from_bench(
    bench: Sequence[BenchResult], ranked: Sequence[BackendId], gpu: str | None
) -> tuple[BackendId, str | None, str] | None:
    for res in sorted(bench, key=lambda r: r.when, reverse=True):
        if res.recommended is None or (gpu and res.gpu_uuid and res.gpu_uuid != gpu):
            continue
        m = next((m for m in res.measurements if m.label == res.recommended), None)
        if m is None or not m.realtime or (m.gpu_faults or 0) > 0:
            continue
        if m.backend in ranked:
            return m.backend, m.model, m.label
    return None


def select(report: DoctorReport, cfg: Config, bench: tuple[BenchResult, ...]) -> Selection:
    statuses = backends(report, cfg)
    avail = {s.id: s for s in statuses if s.available}
    order = ([BackendId.RIFE_TRT] if cfg.general.trt_experimental else []) + [
        BackendId.RIFE_NCNN,
        BackendId.MVTOOLS,
    ]
    ranked = tuple(b for b in order if b in avail)

    if report.blocking:
        n = sum(1 for f in report.findings if f.severity is Severity.BLOCKING)
        return Selection(
            None,
            None,
            _profile_for(cfg, None, None),
            M("Setup isn't finished.\n{n} problem(s) need fixing on the System page.", {"n": n}),
            False,
            (),
        )
    if not ranked:
        mv = next(s for s in statuses if s.id is BackendId.MVTOOLS)
        return Selection(None, None, _profile_for(cfg, None, None), mv.reason, False, ())

    override = cfg.general.backend_override
    if override is not None and override in avail:
        model = _default_model(report) if override is BackendId.RIFE_NCNN else None
        return Selection(
            override,
            model,
            _profile_for(cfg, override, model),
            M("Chosen in the settings."),
            False,
            ranked,
        )

    picked = _from_bench(bench, ranked, report.hardware.interpolation_device)
    if picked is not None:
        backend, model, label = picked
        return Selection(
            backend,
            model,
            _profile_for(cfg, backend, model),
            M("Fastest real-time configuration in your benchmark ({label}).", {"label": label}),
            True,
            ranked,
        )

    first = ranked[0]
    if first is BackendId.RIFE_NCNN:
        model = _default_model(report)
        dev = chosen_device(report)
        reason = M(
            "RIFE (Vulkan) on {gpu}; model chosen without a benchmark.",
            {"gpu": dev.name if dev else "?"},
        )
    elif first is BackendId.MVTOOLS:
        model = None
        if chosen_device(report) is None:
            reason = M(
                "CPU-only: MVTools interpolation, RIFE unavailable ({why})",
                {"why": _cpu_only_reason(report)},
            )
        else:
            reason = M("MVTools on the CPU: RIFE can't run here.")
    else:
        model = None
        reason = M("Experimental TensorRT, which you turned on.")
    return Selection(first, model, _profile_for(cfg, first, model), reason, False, ranked)


__all__ = ["backends", "select", "packaged_models", "chosen_device", "gpu_devices"]
