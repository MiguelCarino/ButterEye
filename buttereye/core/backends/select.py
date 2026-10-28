# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Backend availability and selection (SCOPE §5.3, F13). Pure over the report.

``backends(report, cfg)`` says which backends can run and why (not).
``select(report, cfg, bench)`` picks one, deterministically for identical inputs:

1. A blocking report selects nothing.
2. No non-CPU Vulkan device -> only MVTools ("CPU-only: MVTools interpolation,
   RIFE unavailable" + the reason). RIFE is never returned in that case.
3. Rank: TensorRT (unless ``trt_experimental`` is false, and every TRT check passing),
   RIFE-ncnn, MVTools. Without the opt-in TensorRT is never ranked. mvgpu (the
   optional GPU motion-vector engine) has a status for the Profiles editor but
   is never ranked or offered by Setup: live play picks it per file (§5.3).
4. A benchmark recommendation (no GPU faults, keeping up by the live bar,
   ``bench.verdict.live_ok``) wins. When it names an
   engine that is not ranked (mvgpu always; TensorRT once switched off), the
   same run's best real-time rankable measurement stands in for it, so an
   engine that run measured too slow for live play is not picked over one it
   measured fast enough (:func:`_from_bench`). Otherwise the
   §5.3 conservative defaults - except on macOS and on a GPU that is not
   discrete, where an unbenchmarked RIFE is never the live default and MVTools
   is proposed instead (``decide.rife_live_needs_proof``): RIFE v4.26 on a
   discrete GPU with >= 16 GiB, else v4.22-lite (the packaged models in the
   report decide what exists).
   Quality above that default comes from the benchmark, not from the device
   list: see :func:`_high_end_gpu` for why an integrated GPU's heap size is no
   evidence, on an Apple Silicon Mac as on a Linux iGPU.
5. ``general.backend_override`` wins when that backend is available.
"""

from __future__ import annotations

from collections.abc import Sequence

from buttereye.core import osapi
from buttereye.core.bench import verdict
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.mpvctl import decide
from buttereye.core.types import (
    BackendId,
    BackendStatus,
    BenchMeasurement,
    BenchResult,
    Config,
    DoctorReport,
    Finding,
    Msg,
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
    if not osapi.HAS_VSTRT:
        # macOS, and the answer is the same whatever the opt-in says and whatever
        # the doctor's TensorRT section holds: there is no CUDA and no Apple
        # Silicon Mac has an NVIDIA GPU. This arm has to come first, ahead of
        # ``trt_problems``, because that section is a single INFO row there and an
        # INFO row is not a problem - keying on the findings alone would fall
        # through to the implementation check and offer TensorRT on the one
        # platform where it is permanently impossible. The doctor's own row says
        # the same (``checks_trt.trt_macos_finding``).
        trt = BackendStatus(
            BackendId.RIFE_TRT,
            False,
            True,
            M("TensorRT can't run on macOS: it needs CUDA and an NVIDIA GPU."),
            ErrorCode.TRT_UNSUPPORTED,
        )
    elif osapi.LINUX_APP is not None:
        # the AppImage's VapourSynth (R81) refuses vstrt's API 3, whatever the
        # opt-in says; the doctor's row is INFO, so this has to come first too
        trt = BackendStatus(
            BackendId.RIFE_TRT,
            False,
            True,
            M("TensorRT isn't available in the AppImage: its VapourSynth can't load vstrt."),
            ErrorCode.TRT_UNSUPPORTED,
        )
    elif not cfg.general.trt_experimental:
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
    return (rife, mv, trt, _mvgpu_status(report, dev))


def _mvgpu_status(report: DoctorReport, dev: VulkanDevice | None) -> BackendStatus:
    """mvgpu needs its optional package and a usable GPU, like RIFE."""
    pkg = _finding(report, "packages.mvgpu")
    if report.blocking:
        return BackendStatus(
            BackendId.MVGPU,
            False,
            False,
            M("Setup isn't finished: fix the blocking problems first."),
        )
    if dev is None:
        return BackendStatus(
            BackendId.MVGPU,
            False,
            False,
            M(
                "CPU-only: GPU motion vectors unavailable ({why})",
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
    if pkg is None:
        return BackendStatus(
            BackendId.MVGPU, False, False, M("GPU motion vectors were not checked")
        )
    if pkg.severity is not Severity.OK:
        return BackendStatus(
            BackendId.MVGPU,
            False,
            False,
            M("The GPU motion-vector plugin is not installed"),
            pkg.code,
        )
    return BackendStatus(
        BackendId.MVGPU, True, False, M("GPU motion vectors on {gpu}", {"gpu": dev.name})
    )


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


def _high_end_gpu(dev: VulkanDevice | None) -> bool:
    """SCOPE §5.3's "high-end GPU", decided from the device list alone.

    Both halves are needed, and ``device_type`` is not the loose half: it is what
    makes ``heap_bytes`` mean anything. A discrete card's device-local heap is its
    own fast memory, so 24 GiB of it says something. An integrated GPU's heap is
    system RAM shared with everything else, so the number says nothing about the
    GPU: a Linux iGPU and MoltenVK both report close to the whole machine's RAM,
    which would make a 16 GB MacBook Air or a desktop with an Intel UHD pass a
    16 GiB threshold. Widening this to "integrated" would therefore not be kinder
    to Apple Silicon, it would be wrong on three platforms at once.

    So a machine with unified memory keeps the conservative default until it has
    been measured. That is not a dead end: the in-mpv benchmark walks candidates
    best quality first (``bench.runner.recommend``), so the full model is reached
    by a measurement on this machine rather than by a guess from a device name --
    which is what SCOPE F14 asks for ("we do not quote published fps figures").
    """
    if dev is None or dev.device_type != "discrete":
        return False
    return (dev.heap_bytes or 0) >= HIGH_END_HEAP


def _default_model(report: DoctorReport) -> str | None:
    models = packaged_models(report)
    high = _high_end_gpu(chosen_device(report))
    order = (MODEL_HIGH, MODEL_MID, MODEL_LOW) if high else (MODEL_MID, MODEL_LOW, MODEL_HIGH)
    for m in order:
        if m in models:
            return m
    return models[0] if models else None


def _usable(m: BenchMeasurement, ranked: Sequence[BackendId], res: BenchResult) -> bool:
    """A measurement the chooser may act on: an engine it can rank, real time in
    mpv by the bar live playback uses (``verdict.live_ok``: mpv fps / 1.25, not the
    benchmark's 1.15, or Setup proposes an engine playback then calls too slow),
    and no GPU fault (None = the fault log was not read, as on a Mac)."""
    return (
        m.backend in ranked
        and m.realtime
        and m.vspipe_fps > 0
        and (m.gpu_faults or 0) == 0
        and verdict.live_ok(m, verdict.target_of(res.request))
    )


def _from_bench(
    bench: Sequence[BenchResult], ranked: Sequence[BackendId], gpu: str | None
) -> tuple[BenchMeasurement, BenchMeasurement | None] | None:
    """The measurement to act on from the newest benchmark on this GPU, and the
    recommendation it stands in for (None when it *is* the recommendation).

    The recommendation wins when the chooser can rank it. When it cannot, the
    same run is still evidence, and throwing it away whole was measured to be
    wrong (docs/macos-release-bar.md, items 4 and 18): ``bench.runner.recommend``
    names mvgpu whenever mvgpu holds real time, which on an Apple M2 is every
    720p and 1080p run, and mvgpu is never ranked here (module docstring, step 3).
    Discarding that run fell through to ``ranked[0]`` - so a Mac with RIFE
    installed was handed live ``rife-ncnn`` on the strength of a run that had just
    measured RIFE at 20.07 fps in mpv at 720p and 8.33 at 1080p, against a
    55.14 fps bar. So the run is asked again, restricted to what can be ranked,
    with ``recommend``'s own rule: quality order, the first that keeps up
    (``repeatable`` is shown, never a tie-breaker: B6).
    An engine the run measured below real time is therefore never the answer
    while another rankable engine in the same run held it.

    Nothing rankable held real time (a 4K run, where nothing does on a Mac): the
    run says nothing about which engine to prefer, and the next older one is
    asked, as before. ``recommended`` None is respected rather than re-derived:
    the runner may have skipped a model for repeated GPU faults
    (``runner.repeat_faulters``), which the stored result does not record.
    """
    for res in sorted(bench, key=lambda r: r.when, reverse=True):
        if res.recommended is None or (gpu and res.gpu_uuid and res.gpu_uuid != gpu):
            continue
        rec = next((m for m in res.measurements if m.label == res.recommended), None)
        if rec is not None and _usable(rec, ranked, res):
            return rec, None
        # rows at the run's own size only: the live engines are also measured at
        # bigger sizes ("mvtools @ 2160p"), which says nothing more for Setup
        usable = [m for m in res.measurements if " @ " not in m.label and _usable(m, ranked, res)]
        if usable:
            return usable[0], rec
    return None


def _bench_reason(m: BenchMeasurement, instead_of: BenchMeasurement | None) -> Msg:
    if instead_of is None:
        return M("Fastest real-time configuration in your benchmark ({label}).", {"label": m.label})
    if instead_of.backend is BackendId.MVGPU:
        # what Automatic then does per video (B28): GPU motion vectors wherever the
        # benchmark measured them keeping up, this engine where they don't
        return M(
            "Videos play with GPU motion vectors, the fastest in your benchmark, "
            "wherever they keep up; {label}, which keeps up too, is used for the rest.",
            {"label": m.label},
        )
    return M(
        "{label} keeps up in real time in your benchmark; its first choice "
        "({recommended}) isn't available.",
        {"label": m.label, "recommended": instead_of.label},
    )


def select(report: DoctorReport, cfg: Config, bench: tuple[BenchResult, ...]) -> Selection:
    statuses = backends(report, cfg)
    avail = {s.id: s for s in statuses if s.available}
    trt_on = cfg.general.trt_experimental is not False  # unset = on when set up (§5.2)
    order = ([BackendId.RIFE_TRT] if trt_on else []) + [
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
        m, instead_of = picked
        return Selection(
            m.backend,
            m.model,
            _profile_for(cfg, m.backend, m.model),
            _bench_reason(m, instead_of),
            True,
            ranked,
        )

    first = ranked[0]
    proof_dev = chosen_device(report)
    if (
        first is BackendId.RIFE_NCNN
        and BackendId.MVTOOLS in ranked
        and decide.rife_live_needs_proof(proof_dev)
    ):
        # no benchmark here showed RIFE holding real time, and RIFE on an M2 was
        # measured offline-only at every size (decide.rife_live_needs_proof); a
        # GPU that is not discrete is given the same caution. MVTools is the
        # measured live default on a Mac - real time at 720p, 1080p and 1440p in
        # docs/spikes/m0s.md, level with mvgpu, which is never ranked. RIFE stays
        # ranked: offline render and an explicit choice still use it.
        if osapi.IS_MACOS:
            why = M(
                "MVTools on the CPU: RIFE is too slow for live playback on this Mac "
                "unless a benchmark here shows otherwise. RIFE is still used to convert videos."
            )
        else:
            why = M(
                "MVTools on the CPU: RIFE may be too slow for live playback on {gpu} "
                "unless a benchmark here shows otherwise. RIFE is still used to convert videos.",
                {"gpu": proof_dev.name if proof_dev is not None else "?"},
            )
        return Selection(
            BackendId.MVTOOLS,
            None,
            _profile_for(cfg, BackendId.MVTOOLS, None),
            why,
            False,
            ranked,
        )
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
