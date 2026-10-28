# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""An unbenchmarked RIFE-ncnn never runs live on a GPU that is not discrete
(``decide.rife_live_needs_proof``), and the four places that ask stay in step:
Automatic's engine pick, the smaller-size pick, Setup's default selection and
the shipped profiles' wanted engine, and the live session asks for its own
device."""

from __future__ import annotations

import dataclasses
import os
import types as pytypes
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.backends.select import select
from buttereye.core.mpvctl import decide
from buttereye.core.mpvctl import session as live
from buttereye.core.profiles.defaults import default_config
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId, BypassReason, SessionId, TargetKind, VulkanDevice
from tests import test_live_decisions as ld
from tests.core.test_select import LVP, NV, report
from tests.test_live_decisions import bench_history, no_xid  # noqa: F401 (fixtures)

GiB = 2**30
IGPU = VulkanDevice(0, "uuid-igpu", "AMD Radeon 780M", "AMD", "integrated", 16 * GiB)
VIRT = VulkanDevice(0, "uuid-virt", "virtio-gpu", "Red Hat", "virtual", None)
NTSC = Fraction(24000, 1001)


@pytest.fixture
def off_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux and Windows: this suite also runs on a Mac, where every device needs
    proof."""
    monkeypatch.setattr(osapi, "IS_MACOS", False)


# ---------------------------------------------------------------- the rule
@pytest.mark.parametrize(
    ("dev", "needs"),
    [(NV, False), (IGPU, True), (VIRT, True), (None, False)],
    ids=["discrete", "integrated", "virtual", "no-device"],
)
def test_only_a_discrete_gpu_runs_rife_live_unmeasured(
    off_mac: None, dev: VulkanDevice | None, needs: bool
) -> None:
    assert decide.rife_live_needs_proof(dev) is needs


@pytest.mark.parametrize("dev", [NV, IGPU, None])
def test_a_mac_always_needs_proof(monkeypatch: pytest.MonkeyPatch, dev: VulkanDevice) -> None:
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    assert decide.rife_live_needs_proof(dev) is True


# ---------------------------------------------------------------- decide
def test_pick_engine_skips_an_unmeasured_rife_when_told_to(off_mac: None) -> None:
    rife = decide.EngineOption(BackendId.RIFE_NCNN, None)
    gpumv = decide.EngineOption(BackendId.MVGPU, None)
    # all there is: an unmeasured RIFE runs on a discrete GPU ...
    on_nv = decide.pick_engine([rife], NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=False)
    assert on_nv.backend is BackendId.RIFE_NCNN
    # ... but not on an integrated one, which gets the next engine, or nothing
    on_igpu = decide.pick_engine(
        [rife, gpumv], NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=True
    )
    assert on_igpu.backend is BackendId.MVGPU
    alone = decide.pick_engine([rife], NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=True)
    assert alone.backend is None and alone.target.bypass is BypassReason.NO_REALTIME
    # "smooth anyway" there is not a request for that RIFE either
    forced = decide.pick_engine(
        [rife, gpumv], NTSC, 60.0, TargetKind.X2, auto=True, forced=True, rife_proof=True
    )
    assert forced.backend is not BackendId.RIFE_NCNN
    # a measured RIFE is weighed like any engine
    fast = decide.EngineOption(BackendId.RIFE_NCNN, 200.0)
    assert (
        decide.pick_engine([fast], NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=True).backend
        is BackendId.RIFE_NCNN
    )


def test_pick_engine_without_rife_proof_asks_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None (a caller that knows no device) is ``rife_live_needs_proof(None)``."""
    asked: list[object] = []

    def needs(dev: object) -> bool:
        asked.append(dev)
        return True

    monkeypatch.setattr(decide, "rife_live_needs_proof", needs)
    rife = decide.EngineOption(BackendId.RIFE_NCNN, None)
    pick = decide.pick_engine([rife], NTSC, 60.0, TargetKind.X2, auto=True)
    assert pick.backend is None and asked == [None]


def test_pick_size_passes_rife_proof_to_every_pick(off_mac: None) -> None:
    rife = decide.EngineOption(BackendId.RIFE_NCNN, None)
    sized = [((1280, 720), [rife])]
    assert decide.pick_size(sized, NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=True) is None
    found = decide.pick_size(sized, NTSC, 60.0, TargetKind.X2, auto=True, rife_proof=False)
    assert found is not None and found[0].backend is BackendId.RIFE_NCNN
    # the forced step, too: another engine before the unmeasured RIFE
    both = [((1280, 720), [rife, decide.EngineOption(BackendId.MVGPU, 1.0)])]
    forced = decide.pick_size(
        both, NTSC, 60.0, TargetKind.X2, auto=True, forced=True, rife_proof=True
    )
    assert forced is not None and forced[0].backend is BackendId.MVGPU
    assert forced[0].notice == decide.forced_smaller_notice(720)


# ---------------------------------------------------------------- Setup's default
def test_setup_proposes_mvtools_on_an_integrated_gpu_and_names_it(off_mac: None) -> None:
    s = select(report(vulkan=(IGPU, LVP)), default_config(), ())
    assert s.backend is BackendId.MVTOOLS
    assert s.reason.params is not None and s.reason.params["gpu"] == "AMD Radeon 780M"
    assert "this Mac" not in s.reason.key
    # RIFE stays ranked for offline render and an explicit choice
    assert BackendId.RIFE_NCNN in s.ranked


def test_setup_keeps_rife_on_a_discrete_gpu(off_mac: None) -> None:
    assert select(report(vulkan=(NV, LVP)), default_config(), ()).backend is BackendId.RIFE_NCNN


# ---------------------------------------------------------------- the shipped profiles
class _Ctx:
    def __init__(self, devices: tuple[VulkanDevice, ...]) -> None:
        self.paths = None
        self.report = pytypes.SimpleNamespace(hardware=pytypes.SimpleNamespace(vulkan=devices))

    def last_report(self) -> Any:
        return self.report


def _session(tmp_path: Path, devices: tuple[VulkanDevice, ...]) -> live._Session:
    proc = pytypes.SimpleNamespace(pid=os.getpid(), returncode=None)
    ipc = pytypes.SimpleNamespace(add_handler=lambda *_a: None)
    return live._Session(
        _Ctx(devices),  # type: ignore[arg-type]
        live.LiveOptions(),
        sid=SessionId("s1"),
        file=tmp_path / "film.mkv",
        proc=proc,  # type: ignore[arg-type]
        ipc=ipc,  # type: ignore[arg-type]
        socket=tmp_path / "mpv-s1.sock",
        log_path=tmp_path / "mpv-s1.log",
        script_path=tmp_path / "mpv-s1.vpy",
        profile_request=None,
    )


def test_a_shipped_rife_profile_asks_for_automatic_on_an_integrated_gpu(
    off_mac: None, tmp_path: Path
) -> None:
    cfg = sc.default_config()
    quality = next(p for p in cfg.profiles if p.id == "quality")
    assert quality.backend == BackendId.RIFE_NCNN.value
    assert _session(tmp_path, (IGPU,)).wanted_backend(quality, cfg) == (None, None)
    assert _session(tmp_path, (NV,)).wanted_backend(quality, cfg) == (BackendId.RIFE_NCNN, None)
    # RIFE chosen in Setup, or a profile the user pinned, still pins it there
    chosen = sc.default_config(backend_override=BackendId.RIFE_NCNN)
    igpu = _session(tmp_path, (IGPU,))
    assert igpu.wanted_backend(quality, chosen) == (BackendId.RIFE_NCNN, None)
    mine = dataclasses.replace(quality, id="mine", name="Mine", builtin=False)
    assert igpu.wanted_backend(mine, cfg) == (BackendId.RIFE_NCNN, None)


# ---------------------------------------------------------------- the live session
@pytest.mark.parametrize(
    ("dev", "engine"),
    [(NV, BackendId.RIFE_NCNN), (IGPU, None)],
    ids=["discrete", "integrated"],
)
@pytest.mark.usefixtures("bench_history", "no_xid")
async def test_automatic_asks_the_interpolation_device_before_an_unmeasured_rife(
    off_mac: None,
    tmp_path: Path,
    dev: VulkanDevice,
    engine: BackendId | None,
) -> None:
    """Session.apply passes ``rife_proof`` for its own device to the engine pick:
    with RIFE the only engine and no benchmark, a discrete GPU runs it uncapped
    and an integrated one plays the video unsmoothed instead."""
    ctx = ld.FakeCtx(ld._paths(tmp_path, mvtools=False), ld._config(ld._simple()))
    ctx.report = pytypes.SimpleNamespace(hardware=pytypes.SimpleNamespace(vulkan=(dev,)))
    s = ld._session(ctx, tmp_path)
    ld._load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert ld._engine(s) is engine
    if engine is None:
        assert s.bypass is BypassReason.NO_REALTIME
