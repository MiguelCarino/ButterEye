# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""MVTools' BlockFPS as the live fallback when FlowFPS can't keep up (spike M0(p)).

The caps mirror the dev box in mpv: MVTools FlowFPS 31.75 fps at 2160p 2x (BlockFPS
72), RIFE-ncnn 65 fps at 1080p. No mpv: the session's decisions and the filter it
adds are real."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import decide
from buttereye.core.types import BackendId, BenchMeasurement, BenchResult
from tests.test_live_decisions import (
    M26,
    FakeCtx,
    _config,
    _load,
    _paths,
    _result,
    _session,
    _simple,
)
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture


def _m(label: str, backend: BackendId, mpv: float, model: str | None = None) -> BenchMeasurement:
    return BenchMeasurement(
        label=label, backend=backend, model=model, vspipe_fps=mpv + 5, mpv_fps=mpv, cov=0.01,
        repeatable=True, startup_s=1.0, reload_s=1.0, vram_bytes=None, realtime=True,
        gpu_faults=0,
    )  # fmt: skip


def _filters(s: Any) -> list[dict[str, Any]]:
    out = []
    for c in s.ipc.commands:
        if c[:2] == ("vf", "add"):
            raw = str(c[2]).split(":user-data=", 1)[1]
            out.append(json.loads(raw[raw.index("%", 1) + 1 :] if raw.startswith("%") else raw))
    return out


@pytest.fixture
def mv_4k(bench_history: list[BenchResult]) -> Iterator[list[BenchResult]]:
    bench_history.append(_result(_m("mvtools", BackendId.MVTOOLS, 31.75), size=(3840, 2160)))
    yield bench_history


async def test_4k_mvtools_uses_block_mode_at_full_size(
    tmp_path: Path, mv_4k: list[BenchResult], no_xid: Any
) -> None:
    ctx = FakeCtx(_paths(tmp_path, rife=False), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "mvtools" and f["mv_mode"] == "block"
    assert "size" not in f  # the video's own size
    assert s.notice == decide.blockfps_notice()


async def test_1080p_mvtools_keeps_flow_mode(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any
) -> None:
    bench_history.append(_result(_m("mvtools", BackendId.MVTOOLS, 140.0)))
    ctx = FakeCtx(_paths(tmp_path, rife=False), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "mvtools" and "mv_mode" not in f


async def test_gpu_at_a_smaller_size_beats_mvtools_blocks(
    tmp_path: Path, mv_4k: list[BenchResult], no_xid: Any
) -> None:
    mv_4k.append(_result(_m("rife-v4.26", BackendId.RIFE_NCNN, 65.0, M26)))
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=23.976, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-ncnn" and "size" in f


def test_block_cap_is_flow_cap_times_the_speedup() -> None:
    opts = [decide.EngineOption(BackendId.MVTOOLS, 25.0)]
    block = decide.blockfps_option(opts)
    assert block is not None and block.sustainable_fps == 25.0 * decide.BLOCKFPS_SPEEDUP
    assert decide.blockfps_option([decide.EngineOption(BackendId.MVTOOLS, None)]) is None
    assert decide.blockfps_option([decide.EngineOption(BackendId.RIFE_NCNN, 30.0)]) is None
