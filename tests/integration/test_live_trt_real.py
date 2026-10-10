# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live play with the experimental TensorRT engine on the dev box (SCOPE §5.2, M0(l)).

Needs the user's TensorRT setup (vstrt from contrib/build-vstrt.sh, vs-mlrt's RIFE
ONNX models, onnx + onnxconverter-common, TensorRT 11 with trtexec); skipped
otherwise. HOME and every XDG dir point at temporary directories: the user's
vstrt, models and Python folder are linked in read-only, engines are built into
the temporary cache, and the user's config, history and engine cache are never
touched. mpv runs with ``--vo=null --ao=null``; every mpv started here is killed.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import signal
import sys
import types as pytypes
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import paths as paths_mod
from buttereye.core.api import ButterEye
from buttereye.core.backends import trt
from buttereye.core.mpvctl import decide
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import Thresholds
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigLoad,
    DetachPolicy,
    FilterState,
    Paths,
    Profile,
    Target,
    TargetKind,
)
from tests.integration.test_live_play import Live, _alive, _make_clip

pytestmark = [pytest.mark.integration, pytest.mark.devbox, pytest.mark.gpu]

#: the user's real data folder, resolved before the fixtures move HOME
REAL_DATA = paths_mod.resolve().data_dir
SETUP_PROBLEMS = trt.problems(paths_mod.resolve())


def _config() -> Config:
    base = sc.default_config()
    pinned = Profile(
        id="trt", name="trt", backend=BackendId.RIFE_TRT, model="rife-v4.26_ensembleFalse",
        scale=None, target=Target(TargetKind.X2), sc_threshold=0.12, buffered_frames=None,
        concurrent_frames=None,
    )  # fmt: skip
    return dataclasses.replace(
        base,
        general=dataclasses.replace(base.general, trt_experimental=True),
        profiles=base.profiles + (pinned,),
    )


@pytest.fixture
def trt_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cfg = _config()

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-trt")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("live play must not save the config")

    name = "buttereye.core.profiles.config"
    mod = pytypes.ModuleType(name)
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, mod)
    yield


@pytest.fixture
async def trt_live(xdg_env: Any, trt_config: None) -> AsyncIterator[Live]:
    if SETUP_PROBLEMS:
        pytest.skip("TensorRT isn't set up: " + "; ".join(SETUP_PROBLEMS))
    paths = sc.fake_paths(env=os.environ)
    for sub in ("plugins", "models", "python"):
        dest = paths.data_dir / sub
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(REAL_DATA / sub, target_is_directory=True)
    core = await ButterEye.open(paths)
    reg = live.registry(core._ctx)
    reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")
    reg.options.thresholds = Thresholds(init_grace_s=8.0, frozen_s=2.0, lag_s=1.0, lag_hold_s=1.0)
    env = Live(core, paths, [], [])
    try:
        yield env
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)
        for pid in env.pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        for pid in env.pids:
            for _ in range(100):
                if not _alive(pid):
                    break
                await asyncio.sleep(0.02)
            assert not _alive(pid), f"mpv {pid} survived the test"


async def test_pinned_tensorrt_builds_then_switches_from_ncnn(
    trt_live: Live, tmp_path: Path
) -> None:
    clip = _make_clip(tmp_path / "trt.mkv", rate=24, seconds=120)
    sid = await trt_live.play(clip, "trt")
    # no engine yet: plays with RIFE-ncnn and says TensorRT is being prepared
    first = await trt_live.wait(
        sid, lambda s: s.backend is BackendId.RIFE_NCNN and s.filter is FilterState.ACTIVE
    )
    assert first.notice == decide.engine_building_notice(640, 360, BackendId.RIFE_NCNN)
    # the engine builds out of process, then the session hot-reloads onto TensorRT
    snap = await trt_live.wait(
        sid,
        lambda s: s.backend is BackendId.RIFE_TRT and s.filter is FilterState.ACTIVE,
        timeout=300.0,
    )
    assert snap.target_fps is not None and snap.multiplier == 2
    vf = await trt_live.vf(sid)
    entry = next(e for e in vf if e.get("label") == "buttereye")
    assert '"backend":"rife-trt"' in entry["params"]["user-data"]
    engines = list((trt_live.paths.cache_dir / trt.ENGINES_SUBDIR).glob("*/ready.json"))
    assert len(engines) == 1 and "640x360" in engines[0].parent.name
    # nothing was written next to vsmlrt.py
    assert not any((REAL_DATA / "plugins" / "vstrt").glob("*/__pycache__"))
