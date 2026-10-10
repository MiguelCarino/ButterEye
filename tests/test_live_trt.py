# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live playback with the experimental TensorRT engine (SCOPE §5.2, spike M0(l)).

No mpv, no GPU: TensorRT discovery, the NVIDIA identity and the engine build are
faked; the session's decisions and the filter it adds are real.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.backends import trt
from buttereye.core.mpvctl import decide
from buttereye.core.types import BackendId, BenchMeasurement, BenchResult, Config, Profile
from tests.test_live_decisions import (
    GPU,
    M26,
    FakeCtx,
    _config,
    _load,
    _paths,
    _result,
    _session,
    _settle,
    _simple,
)
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture

# the same fixtures, under the names the tests ask for
bench_history = _bench_history_fixture
no_xid = _no_xid_fixture


def _trt_m(mpv: float, model: str = M26) -> BenchMeasurement:
    return BenchMeasurement(
        label="rife-trt v4.26 fp16", backend=BackendId.RIFE_TRT, model=model,
        vspipe_fps=mpv + 20, mpv_fps=mpv, cov=0.01, repeatable=True, startup_s=1.0,
        reload_s=1.0, vram_bytes=None, realtime=True, gpu_faults=0,
    )  # fmt: skip


def _ncnn_m(mpv: float) -> BenchMeasurement:
    return BenchMeasurement(
        label="rife-v4.26", backend=BackendId.RIFE_NCNN, model=M26, vspipe_fps=mpv + 8,
        mpv_fps=mpv, cov=0.01, repeatable=True, startup_s=1.1, reload_s=0.9,
        vram_bytes=None, realtime=True, gpu_faults=0,
    )  # fmt: skip


def _opted(profile: Profile, on: bool = True) -> Config:
    cfg = _config(profile)
    return dataclasses.replace(cfg, general=dataclasses.replace(cfg.general, trt_experimental=on))


@dataclasses.dataclass
class TrtFake:
    install: trt.TrtInstall
    builds: list[trt.EngineKey]
    fail: bool = False


@pytest.fixture
def fake_trt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TrtFake]:
    vstrt = tmp_path / "vstrt"
    inst = trt.TrtInstall(
        vstrt_dir=vstrt, vsmlrt_dir=vstrt, python_dirs=(tmp_path / "py",),
        models_dir=tmp_path / "onnx", trtexec=Path("/usr/bin/trtexec"),
        vstrt_version="v16.3.test1-0-g44033da", trt_version="11.3.0",
    )  # fmt: skip
    fake = TrtFake(inst, [])

    async def identity(vk: str | None) -> tuple[str, str]:
        return f"GPU-{GPU}", "615.71.09"

    async def build(paths: Any, install: Any, key: trt.EngineKey, **kw: Any) -> Path:
        fake.builds.append(key)
        if fake.fail:
            raise RuntimeError("trtexec failed")
        folder = trt.engine_dir(paths, key)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "x.engine").write_bytes(b"engine")
        (folder / trt.READY).write_text("{}")
        return folder

    monkeypatch.setattr(trt, "find_install", lambda paths, which=None: inst)
    monkeypatch.setattr(trt, "available_models", lambda install: frozenset({M26}))
    monkeypatch.setattr(trt, "nvidia_identity", identity)
    monkeypatch.setattr(trt, "build_engine", build)
    yield fake


def _filters(s: Any) -> list[dict[str, Any]]:
    """The user-data of every ``vf add``."""
    out = []
    for c in s.ipc.commands:
        if c[:2] == ("vf", "add"):
            arg = str(c[2])
            raw = arg[arg.index(":user-data=") + len(":user-data=") :]
            raw = raw[raw.index("%", 1) + 1 :] if raw.startswith("%") else raw
            out.append(json.loads(raw))
    return out


def _ready(ctx: FakeCtx, s: Any, w: int = 1920, h: int = 1080) -> Path:
    key = trt.EngineKey(f"GPU-{GPU}", "615.71.09", "v16.3.test1-0-g44033da", "11.3.0", M26, w, h)
    folder = trt.engine_dir(ctx.paths, key)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "x.engine").write_bytes(b"engine")
    (folder / trt.READY).write_text("{}")
    return folder


async def test_not_opted_in_never_uses_tensorrt(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple(), on=False))
    s = _session(ctx, tmp_path)
    _ready(ctx, s)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn"]
    assert fake_trt.builds == []


async def test_auto_prefers_a_ready_measured_tensorrt_engine(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple()))
    s = _session(ctx, tmp_path)
    folder = _ready(ctx, s)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt"
    assert f["trt"]["engine_dir"] == str(folder)
    assert f["trt"]["build"] is False and f["trt"]["fp16"] and f["trt"]["half_io"]
    assert f["trt"]["streams"] == 2 and f["trt"]["model"] == "v4_26"
    assert f["sc_threshold"] == 0.12
    assert s.backend is BackendId.RIFE_TRT and s.model == M26
    assert fake_trt.builds == []


async def test_unmeasured_tensorrt_is_not_trusted_in_auto(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple()))
    s = _session(ctx, tmp_path)
    _ready(ctx, s)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn"]


async def test_missing_engine_plays_ncnn_builds_then_switches(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn"]
    assert s.notice == decide.engine_building_notice(1920, 1080, BackendId.RIFE_NCNN)
    assert s.trt_waiting is not None and (s.trt_waiting.width, s.trt_waiting.height) == (1920, 1080)
    await _settle(ctx)
    assert len(fake_trt.builds) == 1 and s.trt_built
    # a second apply while nothing changed does not start another build
    # the session's tick re-applies once the engine it waits for is ready
    s.trt_built = False
    await s.apply(raise_on_fail=False)
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn", "rife-trt"]
    assert len(fake_trt.builds) == 1 and s.trt_waiting is None


async def test_one_build_per_engine_across_applies(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    await s.apply(raise_on_fail=False)  # still building: no second build
    await _settle(ctx)
    assert len(fake_trt.builds) == 1


async def test_failed_build_keeps_ncnn_with_a_notice_and_no_retry_loop(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    fake_trt.fail = True
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    await _settle(ctx)
    await s.apply(raise_on_fail=False)
    await _settle(ctx)
    assert len(fake_trt.builds) == 1
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn", "rife-ncnn"]
    assert s.notice is not None and "couldn't be built" in s.notice.key
    assert s.trt_waiting is None


async def test_pinned_tensorrt_without_setup_falls_back_with_notice(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(trt, "find_install", lambda paths, which=None: None)
    bench_history.append(_result(_ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple(BackendId.RIFE_TRT)))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn"]
    assert s.notice is not None and "TensorRT isn't turned on or set up" in s.notice.key


async def test_large_video_uses_four_streams(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_result(_trt_m(260.0), _ncnn_m(65.0)))
    ctx = FakeCtx(_paths(tmp_path), _opted(_simple(BackendId.RIFE_TRT)))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 2560, 1440)
    _load(s, fps=23.976, w=2560, h=1440)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["trt"]["streams"] == 4


def test_pinned_tensorrt_that_cannot_keep_up_falls_back_to_ncnn_first() -> None:
    src = decide.fps_fraction(23.976)
    assert src is not None
    opts = [
        decide.EngineOption(BackendId.RIFE_TRT, 20.0),
        decide.EngineOption(BackendId.RIFE_NCNN, 60.0),
        decide.EngineOption(BackendId.MVTOOLS, 80.0),
    ]
    pick = decide.pick_engine(opts, src, None, decide.TargetKind.X2, auto=False)
    assert pick.backend is BackendId.RIFE_NCNN
    assert pick.notice == decide.gpu_fallback_notice(BackendId.RIFE_TRT, BackendId.RIFE_NCNN)
