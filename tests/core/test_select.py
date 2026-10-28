# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: backend availability + selection (SCOPE §5.3, F13) over report fixtures."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core.backends import select as select_mod
from buttereye.core.backends.select import backends, select
from buttereye.core.errors import ErrorCode
from buttereye.core.profiles.defaults import default_config
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    Config,
    DoctorReport,
    Finding,
    GpuInfo,
    HardwareInfo,
    Msg,
    PluginStatus,
    ProbeResult,
    Section,
    Severity,
    VulkanDevice,
)

GiB = 2**30
NV = VulkanDevice(0, "uuid-nv", "NVIDIA GeForce RTX 4090", "NVIDIA", "discrete", 24 * GiB)
LVP = VulkanDevice(1, "uuid-lvp", "llvmpipe", "Mesa", "cpu", None)
MODELS = (
    "rife-v4.18_ensembleFalse",
    "rife-v4.22_lite_ensembleFalse",
    "rife-v4.26_ensembleFalse",
)


def f(
    fid: str,
    sev: Severity = Severity.OK,
    code: ErrorCode | None = None,
    section: Section | None = None,
    evidence: tuple[str, ...] = (),
) -> Finding:
    sec = section or Section(fid.split(".")[0])
    return Finding(fid, sec, sev, code, Msg(fid), Msg(""), Msg(""), (), evidence)


def plugin(name: str, loads: bool | None = True) -> PluginStatus:
    return PluginStatus(name, "pkg", "1", "buttereye", loads, None, ())


def report(
    *,
    vulkan: tuple[VulkanDevice, ...] = (NV, LVP),
    findings: tuple[Finding, ...] | None = None,
    rife_loads: bool | None = True,
    mv_loads: bool | None = True,
    trt: bool = False,
    extra: tuple[Finding, ...] = (),
) -> DoctorReport:
    gpu_dev = next((d for d in vulkan if d.device_type != "cpu"), None)
    hw = HardwareInfo(
        gpus=(GpuInfo("0000:08:00.0", "NVIDIA", "RTX 4090", "615.71.09", 24 * GiB, "8.9"),),
        vulkan=vulkan,
        interpolation_device=gpu_dev.uuid if gpu_dev else None,
        decode_device="0000:08:00.0",
        cpu_threads=24,
    )
    base: tuple[Finding, ...] = (
        f("mpv.version"),
        f("packages.rife"),
        f("packages.mvtools"),
        f("packages.models", evidence=MODELS),
    )
    if gpu_dev is None:
        base += (
            f(
                "vulkan.devices",
                Severity.DEGRADED,
                ErrorCode.VULKAN_CPU_ONLY,
                evidence=("1: llvmpipe (cpu, software, not used)",),
            ),
        )
    probe = ProbeResult(
        "VapourSynth R72",
        "3.14.7",
        (plugin("RIFE-ncnn-Vulkan", rife_loads), plugin("MVTools", mv_loads)),
        False,
    )
    return DoctorReport((findings if findings is not None else base) + extra, hw, probe, 1.0, trt)


@pytest.fixture(autouse=True)
def linux_rife_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """These fixtures are a Linux box with an RTX 4090, where an unbenchmarked
    RIFE is the right default. Pinned, because this suite also runs on a Mac,
    where ``decide.rife_live_needs_proof`` says otherwise (tested in
    tests/core/test_macos_doctor_pure.py)."""
    monkeypatch.setattr(select_mod.decide, "rife_live_needs_proof", lambda: False)


@pytest.fixture
def cfg() -> Config:
    return default_config()


def with_general(cfg: Config, **kw: object) -> Config:
    return dataclasses.replace(cfg, general=dataclasses.replace(cfg.general, **kw))  # type: ignore[arg-type]


def test_devbox_picks_rife_v426(cfg: Config) -> None:
    s = select(report(), cfg, ())
    assert s.backend is BackendId.RIFE_NCNN
    assert s.model == "rife-v4.26_ensembleFalse"
    assert s.profile_id == "quality"
    assert s.ranked == (BackendId.RIFE_NCNN, BackendId.MVTOOLS)
    assert not s.from_benchmark


def test_deterministic(cfg: Config) -> None:
    r = report()
    first = select(r, cfg, ())
    assert all(select(r, cfg, ()) == first for _ in range(5))


def test_mid_range_gpu_uses_lite(cfg: Config) -> None:
    small = dataclasses.replace(NV, heap_bytes=8 * GiB, name="Mid GPU")
    s = select(report(vulkan=(small, LVP)), cfg, ())
    assert s.model == "rife-v4.22_lite_ensembleFalse"
    assert s.profile_id == "balanced"


@pytest.mark.parametrize("vulkan", [(LVP,), ()])
def test_cpu_only_never_rife(cfg: Config, vulkan: tuple[VulkanDevice, ...]) -> None:
    r = report(vulkan=vulkan)
    st = {b.id: b for b in backends(r, cfg)}
    assert not st[BackendId.RIFE_NCNN].available
    assert "CPU-only" in st[BackendId.RIFE_NCNN].reason.key
    s = select(r, cfg, ())
    assert s.backend is BackendId.MVTOOLS
    assert BackendId.RIFE_NCNN not in s.ranked
    assert "CPU-only: MVTools interpolation, RIFE unavailable" in s.reason.key
    assert s.profile_id == "cpu"


def test_cpu_only_without_mvtools_is_none(cfg: Config) -> None:
    r = report(vulkan=(LVP,), mv_loads=False)
    s = select(r, cfg, ())
    assert s.backend is None and s.ranked == ()
    assert s.reason.key


def _trt_ok() -> tuple[Finding, ...]:
    return tuple(f(f"trt.{n}", section=Section.TRT) for n in ("gpu", "driver", "trtexec", "vstrt"))


def test_no_trt_without_optin_even_when_present(cfg: Config) -> None:
    r = report(trt=True, extra=_trt_ok())
    st = {b.id: b for b in backends(r, cfg)}
    assert not st[BackendId.RIFE_TRT].available
    assert st[BackendId.RIFE_TRT].experimental
    s = select(r, cfg, ())
    assert s.backend is not BackendId.RIFE_TRT
    assert BackendId.RIFE_TRT not in s.ranked


def test_trt_opted_in_but_not_set_up(cfg: Config) -> None:
    bad = (f("trt.trtexec", Severity.DEGRADED, ErrorCode.TRT_UNSUPPORTED, Section.TRT),)
    r = report(trt=True, extra=bad)
    c = with_general(cfg, trt_experimental=True)
    st = {b.id: b for b in backends(r, c)}
    assert not st[BackendId.RIFE_TRT].available
    assert st[BackendId.RIFE_TRT].code is ErrorCode.TRT_UNSUPPORTED
    assert select(r, c, ()).backend is BackendId.RIFE_NCNN


def test_trt_opted_in_all_ok_is_available(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    # backends.trt.build_engine exists (spike M0(l)): an opted-in, clean report offers TRT
    # on a platform a TensorRT plugin can exist on. macOS is not one of those and has
    # its own case below, so say which kind of machine this is.
    monkeypatch.setattr(select_mod.osapi, "HAS_VSTRT", True)
    c = with_general(cfg, trt_experimental=True)
    st = {b.id: b for b in backends(report(trt=True, extra=_trt_ok()), c)}
    assert st[BackendId.RIFE_TRT].available
    assert st[BackendId.RIFE_TRT].experimental
    assert st[BackendId.RIFE_TRT].code is None


def test_trt_is_never_offered_without_vstrt(cfg: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS (``osapi.HAS_VSTRT`` False): TensorRT needs CUDA and an NVIDIA GPU, so
    it is refused however clean the report is and whatever the opt-in says.

    This is the case the doctor's single INFO ``trt.off`` row does NOT cover: an
    INFO row is not a ``_problem``, so without this arm the ladder falls through
    to the implementation check - which passes, because ``backends.trt`` imports -
    and offers TensorRT on the one platform where it can never run.
    """
    monkeypatch.setattr(select_mod.osapi, "HAS_VSTRT", False)
    r = report(trt=True, extra=_trt_ok())
    for c in (with_general(cfg, trt_experimental=True), cfg):
        st = {b.id: b for b in backends(r, c)}
        assert not st[BackendId.RIFE_TRT].available
        assert st[BackendId.RIFE_TRT].code is ErrorCode.TRT_UNSUPPORTED
        s = select(r, c, ())
        assert s.backend is not BackendId.RIFE_TRT
        assert BackendId.RIFE_TRT not in s.ranked


def test_trt_is_never_offered_in_the_appimage(
    cfg: Config, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The Linux AppImage: its VapourSynth (R81) refuses vstrt's API 3, so a vstrt
    built for the host can never load there. Offered, it made every live session
    fail and roll back to unsmoothed playback (0.0.5, an RTX 4090 with vstrt set up
    for Fedora's R72)."""
    monkeypatch.setattr(select_mod.osapi, "LINUX_APP", tmp_path)
    r = report(trt=True, extra=_trt_ok())
    for c in (with_general(cfg, trt_experimental=True), cfg):
        st = {b.id: b for b in backends(r, c)}
        assert not st[BackendId.RIFE_TRT].available
        assert st[BackendId.RIFE_TRT].code is ErrorCode.TRT_UNSUPPORTED
        assert select(r, c, ()).backend is not BackendId.RIFE_TRT
    monkeypatch.setattr(select_mod.osapi, "LINUX_APP", None)
    st = {b.id: b for b in backends(r, with_general(cfg, trt_experimental=True))}
    assert st[BackendId.RIFE_TRT].available


def test_blocking_selects_nothing(cfg: Config) -> None:
    r = report(extra=(f("mpv.vf_vapoursynth", Severity.BLOCKING, ErrorCode.MPV_NO_VS_FILTER),))
    s = select(r, cfg, ())
    assert s.backend is None
    assert s.reason.params["n"] == 1
    assert not any(b.available for b in backends(r, cfg))


def test_rife_missing_package_or_load_failure(cfg: Config) -> None:
    missing = report(
        findings=(
            f("packages.rife", Severity.DEGRADED, ErrorCode.PKG_MISSING),
            f("packages.mvtools"),
        ),
        rife_loads=None,
    )
    st = {b.id: b for b in backends(missing, cfg)}
    assert not st[BackendId.RIFE_NCNN].available
    assert st[BackendId.RIFE_NCNN].code is ErrorCode.PKG_MISSING
    assert select(missing, cfg, ()).backend is BackendId.MVTOOLS
    noload = report(rife_loads=False)
    assert select(noload, cfg, ()).backend is BackendId.MVTOOLS


def test_override(cfg: Config) -> None:
    s = select(report(), with_general(cfg, backend_override=BackendId.MVTOOLS), ())
    assert s.backend is BackendId.MVTOOLS and s.profile_id == "cpu"
    # an unavailable override is ignored
    s2 = select(report(vulkan=(LVP,)), with_general(cfg, backend_override=BackendId.RIFE_NCNN), ())
    assert s2.backend is BackendId.MVTOOLS


def _bench(
    label: str,
    backend: BackendId,
    model: str | None,
    *,
    faults: int | None = 0,
    realtime: bool = True,
    when: int = 1,
) -> BenchResult:
    m = BenchMeasurement(
        label, backend, model, 80.0, 75.0, 0.01, True, 1.0, 0.5, None, realtime, faults
    )
    return BenchResult(
        BenchRequest(1920, 1080, Fraction(24000, 1001)),
        (m,),
        label,
        datetime(2026, 10, when, tzinfo=UTC),
        "uuid-nv",
    )


def test_bench_recommendation_wins(cfg: Config) -> None:
    b = _bench("lite", BackendId.RIFE_NCNN, "rife-v4.22_lite_ensembleFalse")
    s = select(report(), cfg, (b,))
    assert s.from_benchmark
    assert s.model == "rife-v4.22_lite_ensembleFalse" and s.profile_id == "balanced"


def test_faulting_or_slow_bench_never_used(cfg: Config) -> None:
    faulty = _bench("x", BackendId.RIFE_NCNN, "rife-v4.22_lite_ensembleFalse", faults=2)
    slow = _bench("y", BackendId.RIFE_NCNN, "rife-v4.22_lite_ensembleFalse", realtime=False)
    for b in (faulty, slow):
        s = select(report(), cfg, (b,))
        assert not s.from_benchmark and s.model == "rife-v4.26_ensembleFalse"


def test_gpu_fault_noted_but_available(cfg: Config) -> None:
    r = report(extra=(f("gpu_fault.xid", Severity.DEGRADED, ErrorCode.RIFE_GPU_FAULT),))
    st = {b.id: b for b in backends(r, cfg)}
    assert st[BackendId.RIFE_NCNN].available
    assert "GPU faults" in st[BackendId.RIFE_NCNN].reason.params["note"]  # type: ignore[operator]


def test_mvgpu_status_follows_its_package_and_the_gpu(cfg: Config) -> None:
    """mvgpu has a status for the Profiles editor (installed + a GPU), independent
    of RIFE, and is never ranked: live play picks it per file (§5.3)."""
    mvgpu = f("packages.mvgpu")
    ok = {b.id: b for b in backends(report(extra=(mvgpu,)), cfg)}[BackendId.MVGPU]
    assert ok.available and not ok.experimental
    assert ok.reason.params == {"gpu": "NVIDIA GeForce RTX 4090"}
    assert BackendId.MVGPU not in select(report(extra=(mvgpu,)), cfg, ()).ranked
    no_rife = report(
        findings=(f("packages.rife", Severity.DEGRADED, ErrorCode.PKG_MISSING), mvgpu),
        rife_loads=None,
    )
    assert {b.id: b for b in backends(no_rife, cfg)}[BackendId.MVGPU].available
    missing = report(extra=(f("packages.mvgpu", Severity.INFO),))
    st = {b.id: b for b in backends(missing, cfg)}[BackendId.MVGPU]
    assert not st.available and st.reason.key == "The GPU motion-vector plugin is not installed"
    unchecked = {b.id: b for b in backends(report(), cfg)}[BackendId.MVGPU]
    assert not unchecked.available
    cpu = {b.id: b for b in backends(report(vulkan=(LVP,), extra=(mvgpu,)), cfg)}
    assert not cpu[BackendId.MVGPU].available
    assert "CPU-only" in cpu[BackendId.MVGPU].reason.key
    assert cpu[BackendId.MVGPU].code is ErrorCode.VULKAN_CPU_ONLY
    blocked = report(
        extra=(mvgpu, f("mpv.vf_vapoursynth", Severity.BLOCKING, ErrorCode.MPV_NO_VS_FILTER))
    )
    assert not {b.id: b for b in backends(blocked, cfg)}[BackendId.MVGPU].available


def _m(label: str, backend: BackendId, model: str | None, *, realtime: bool) -> BenchMeasurement:
    return BenchMeasurement(
        label, backend, model, 80.0, 75.0, 0.01, True, 1.0, 0.5, None, realtime, 0
    )


def _run(recommended: str | None, *ms: BenchMeasurement) -> BenchResult:
    return BenchResult(
        BenchRequest(1920, 1080, Fraction(24000, 1001)),
        ms,
        recommended,
        datetime(2026, 10, 1, tzinfo=UTC),
        "uuid-nv",
    )


def test_an_mvgpu_recommendation_stands_in_with_the_best_rankable_real_time_row(
    cfg: Config,
) -> None:
    """mvgpu is never ranked, so a run recommending it used to be thrown away whole
    and the choice fell to ``ranked[0]`` whatever the run measured. The run now
    answers among the rankable engines, in its own quality order.

    On a discrete GPU where RIFE also holds real time that is RIFE, with the
    measured model - the answer this machine gets with no mvgpu installed, so a
    Linux box keeps its RIFE (``from_benchmark`` now True, the model measured
    rather than defaulted). Where the run measured RIFE too slow it is MVTools,
    not the RIFE the old fallback picked (docs/macos-release-bar.md, item 4)."""
    lite = "rife-v4.22_lite_ensembleFalse"
    mvgpu = _m("mvgpu", BackendId.MVGPU, None, realtime=True)
    mvtools = _m("mvtools", BackendId.MVTOOLS, None, realtime=True)
    r = report(extra=(f("packages.mvgpu"),))

    rife_ok = _run("mvgpu", _m("lite", BackendId.RIFE_NCNN, lite, realtime=True), mvgpu, mvtools)
    s = select(r, cfg, (rife_ok,))
    assert s.backend is BackendId.RIFE_NCNN and s.model == lite and s.from_benchmark
    assert s.reason.params == {"label": "lite"}

    rife_slow = _run("mvgpu", _m("lite", BackendId.RIFE_NCNN, lite, realtime=False), mvgpu, mvtools)
    assert select(r, cfg, ()).backend is BackendId.RIFE_NCNN  # what discarding it gave
    s = select(r, cfg, (rife_slow,))
    assert s.backend is BackendId.MVTOOLS and s.from_benchmark and s.profile_id == "cpu"

    # nothing rankable kept up: no evidence either way, the unmeasured default
    only_mvgpu = _run("mvgpu", _m("lite", BackendId.RIFE_NCNN, lite, realtime=False), mvgpu)
    assert select(r, cfg, (only_mvgpu,)) == select(r, cfg, ())


def test_the_stand_in_is_the_best_quality_one_that_keeps_up(cfg: Config) -> None:
    """``bench.runner.recommend``'s rule, applied to the rankable rows: quality
    order. ``repeatable`` is shown, never a tie-breaker (B6): an unrepeatable
    better engine used to lose to a worse one that passed a 5 % spread test."""
    full, lite = "rife-v4.26_ensembleFalse", "rife-v4.22_lite_ensembleFalse"
    shaky = dataclasses.replace(
        _m("full", BackendId.RIFE_NCNN, full, realtime=True), repeatable=False
    )
    steady = _m("lite", BackendId.RIFE_NCNN, lite, realtime=True)
    mvgpu = _m("mvgpu", BackendId.MVGPU, None, realtime=True)
    s = select(report(extra=(f("packages.mvgpu"),)), cfg, (_run("mvgpu", shaky, mvgpu, steady),))
    assert s.model == full


def test_bench_rows_below_the_live_bar_are_not_used(cfg: Config) -> None:
    """B5: 57 fps in mpv at 23.976 -> 47.95 is the benchmark's "real time" (55.14)
    but under what playback needs (59.94): Setup must not propose it."""
    lite = "rife-v4.22_lite_ensembleFalse"
    between = dataclasses.replace(
        _m("lite", BackendId.RIFE_NCNN, lite, realtime=True), mpv_fps=57.0
    )
    mvtools = _m("mvtools", BackendId.MVTOOLS, None, realtime=True)
    s = select(report(), cfg, (_run("lite", between, mvtools),))
    assert s.backend is BackendId.MVTOOLS and s.from_benchmark
    alone = select(report(), cfg, (_run("lite", between),))
    assert not alone.from_benchmark


def test_setup_wording_matches_what_playback_picks(cfg: Config) -> None:
    """B28: a run recommending mvgpu proposes a rankable stand-in; the reason says
    playback uses GPU motion vectors where they keep up, not that they are never
    used."""
    mvgpu = _m("mvgpu", BackendId.MVGPU, None, realtime=True)
    mvtools = _m("mvtools", BackendId.MVTOOLS, None, realtime=True)
    s = select(report(extra=(f("packages.mvgpu"),)), cfg, (_run("mvgpu", mvgpu, mvtools),))
    assert s.backend is BackendId.MVTOOLS
    assert "Videos play with GPU motion vectors" in s.reason.key
    assert "never" not in s.reason.key
    assert s.reason.params == {"label": "mvtools"}


def test_bigger_size_rows_never_stand_in(cfg: Config) -> None:
    """The live engines are measured at 1440p and 2160p too; Setup answers from
    the rows at the run's own size."""
    lite = "rife-v4.22_lite_ensembleFalse"
    slow = _m("lite", BackendId.RIFE_NCNN, lite, realtime=False)
    big = _m("mvtools @ 2160p", BackendId.MVTOOLS, None, realtime=True)
    mvgpu = _m("mvgpu", BackendId.MVGPU, None, realtime=True)
    s = select(report(extra=(f("packages.mvgpu"),)), cfg, (_run("mvgpu", slow, mvgpu, big),))
    assert not s.from_benchmark
