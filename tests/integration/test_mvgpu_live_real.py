# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mvgpu as a live engine in the real mpv on a real GPU: a profile pinned to it
plays a generated clip through ButterEye's generated script, the filter runs
mvgpu at 2x, frames flow, and the OSD line names it.

The plugin comes from the RPM's private directory, else the dev build
(``~/.cache/buttereye-dev/mvgpu-build``, mvgpu/README.md); the test points
``Paths.rpm_plugin_dir`` at a temporary directory holding a symlink to it, so
nothing else from the RPM directory is loaded. mpv runs with ``--vo=null
--ao=null --display-fps-override=60``."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import signal
import sys
import types as pytypes
from collections.abc import AsyncIterator, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.api import ButterEye
from buttereye.core.events import Event
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import Thresholds
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchRequest,
    Config,
    ConfigLoad,
    DetachPolicy,
    FilterState,
    Paths,
    TargetKind,
)
from tests.integration.test_live_play import Live, _alive, _make_clip, _profile, _raw

pytestmark = [pytest.mark.integration, pytest.mark.gpu]

#: what the rpm installs. An rpm payload is Fedora's, so this name stays ``.so``.
RPM_PLUGIN = Path("/usr/lib64/buttereye/vapoursynth/libmvgpu.so")
#: a plugin built here instead, under the name this platform builds it as
DEV_PLUGIN = Path.home() / ".cache/buttereye-dev/mvgpu-build" / osapi.plugin_file("libmvgpu.so")
PLUGIN = next((p for p in (RPM_PLUGIN, DEV_PLUGIN) if p.is_file()), None)

if PLUGIN is None:
    pytest.skip(
        f"mvgpu plugin not installed or built ({RPM_PLUGIN}, {DEV_PLUGIN})",
        allow_module_level=True,
    )


@pytest.fixture
def config_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    base = sc.default_config()
    pinned = _profile("gpumv", BackendId.MVGPU, TargetKind.X2)
    # the small mpv line stays up ("always"): with the default 1 s it can come and go
    # before the test looks at it on a loaded machine
    general = dataclasses.replace(base.general, osd_seconds=-1)
    cfg: Config = dataclasses.replace(base, general=general, profiles=base.profiles + (pinned,))

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-mvgpu")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("live play must not save the config")

    mod = pytypes.ModuleType("buttereye.core.profiles.config")
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.profiles.config", mod)
    yield


@pytest.fixture
async def mvgpu_env(xdg_env: Any, config_provider: None, tmp_path: Path) -> AsyncIterator[Live]:
    assert PLUGIN is not None
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    # the private plugin directory the generated script loads by name, so the link
    # must carry the name VapourSynth looks for here
    (plugins / osapi.plugin_file("libmvgpu.so")).symlink_to(PLUGIN)
    paths = dataclasses.replace(sc.fake_paths(env=os.environ), rpm_plugin_dir=plugins)
    core = await ButterEye.open(paths)
    reg = live.registry(core._ctx)
    reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")
    reg.options.thresholds = Thresholds(init_grace_s=8.0, frozen_s=2.0, lag_s=1.0, lag_hold_s=1.0)
    events: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            events.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    env = Live(core, paths, events, [])
    try:
        yield env
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)
        task.cancel()
        for pid in env.pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        for pid in env.pids:
            for _ in range(100):
                if not _alive(pid):
                    break
                await asyncio.sleep(0.02)
            assert not _alive(pid), f"mpv {pid} survived the test"


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _make_clip(tmp_path_factory.mktemp("mvgpu") / "moving.mkv", rate=24)


async def test_mvgpu_smooths_live_in_mpv(mvgpu_env: Live, clip: Path) -> None:
    env = mvgpu_env
    sid = await env.play(clip, "gpumv")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE, timeout=30)
    assert snap.backend is BackendId.MVGPU and snap.model is None
    assert snap.target_fps == 48 and snap.multiplier == 2
    # the filter mpv runs is ButterEye's script with mvgpu in its user-data
    vf = await env.vf(sid)
    entry = next(e for e in vf if e.get("label") == "buttereye")
    data = json.loads(entry["params"]["user-data"])
    assert data["backend"] == "mvgpu" and data["sc_threshold"] == 0.12
    assert entry["params"]["concurrent-frames"] == "8"
    # frames keep flowing at the doubled rate
    sock = env.s(sid).socket
    fps = 0.0
    for _ in range(50):
        r = _raw(sock, "get_property", "estimated-vf-fps")
        fps = float((r or {}).get("data") or 0.0)
        if fps > 40:
            break
        await asyncio.sleep(0.2)
    assert 40 < fps < 56, fps
    await asyncio.sleep(1.0)
    assert (await env.snap(sid)).filter is FilterState.ACTIVE
    # the small mpv line names the engine
    s = env.s(sid)
    for _ in range(50):
        if s.osd_shown:
            break
        await asyncio.sleep(0.1)
    assert any("motion vectors" in str(p) for p in s.osd_shown), s.osd_shown


async def test_speed_test_measures_mvgpu_in_vspipe_and_mpv(mvgpu_env: Live) -> None:
    """The speed test's mvgpu candidate in the real harness: vspipe on the moving
    pattern, then mpv on lavfi testsrc2, reported like the other engines."""
    result = await mvgpu_env.core.bench(BenchRequest(1920, 1080, Fraction(24000, 1001))).result()
    (m,) = result.measurements
    assert (m.label, m.backend, m.model) == ("mvgpu", BackendId.MVGPU, None)
    # RTX 4090: about 1000 fps in vspipe and several hundred in mpv at 1080p 2x
    assert m.vspipe_fps > 60 and m.mpv_fps > 60, m
    assert m.realtime and result.recommended == "mvgpu"
    assert not m.gpu_faults
