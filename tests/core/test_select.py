# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: backend availability + selection (SCOPE §5.3, F13) over report fixtures."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from fractions import Fraction

import pytest

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


def test_trt_opted_in_all_ok_is_available(cfg: Config) -> None:
    # backends.trt.build_engine exists (spike M0(l)): an opted-in, clean report offers TRT
    c = with_general(cfg, trt_experimental=True)
    st = {b.id: b for b in backends(report(trt=True, extra=_trt_ok()), c)}
    assert st[BackendId.RIFE_TRT].available
    assert st[BackendId.RIFE_TRT].experimental
    assert st[BackendId.RIFE_TRT].code is None


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
