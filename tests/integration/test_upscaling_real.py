# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Sharper upscaling against the real mpv (SCOPE §15.2): the bundled shader is
appended at launch, removed and re-added while playing, and a shader the user
set in mpv's own options stays. mpv runs with ``--vo=null --ao=null``."""

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

from buttereye.core.api import ButterEye
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl import shaders
from buttereye.core.mpvctl.health import Thresholds
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import Config, ConfigLoad, DetachPolicy, Paths
from tests.integration.test_live_play import Live, _alive, _make_clip

pytestmark = [pytest.mark.integration]

SHADER = str(shaders.UPSCALERS["sharper"])
STATE: dict[str, Config] = {}


@pytest.fixture
def sharp_config(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    base = sc.default_config()
    STATE["cfg"] = dataclasses.replace(
        base, general=dataclasses.replace(base.general, upscaling="sharper")
    )

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(STATE["cfg"], revision="rev-up")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("not saved here")

    mod = pytypes.ModuleType("buttereye.core.profiles.config")
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.profiles.config", mod)
    yield


@pytest.fixture
async def env(xdg_env: Any, sharp_config: None, tmp_path: Path) -> AsyncIterator[Live]:
    paths = sc.fake_paths(env=os.environ)
    core = await ButterEye.open(paths)
    reg = live.registry(core._ctx)
    user_shader = tmp_path / "user.glsl"
    user_shader.write_text("// a user's own shader\n")
    # the user's own mpv.conf (in the temporary XDG config dir) sets a shader;
    # mpv reads it before the command line, where ButterEye appends its own
    user_conf = Path(os.environ["XDG_CONFIG_HOME"]) / "mpv" / "mpv.conf"
    user_conf.parent.mkdir(parents=True, exist_ok=True)
    user_conf.write_text(f"glsl-shaders={user_shader}\n")
    reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")
    reg.options.thresholds = Thresholds(init_grace_s=8.0, frozen_s=2.0, lag_s=1.0, lag_hold_s=1.0)
    e = Live(core, paths, [], [])
    e.user_shader = user_shader  # type: ignore[attr-defined]
    try:
        yield e
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)
        for pid in e.pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pid, signal.SIGKILL)
        for pid in e.pids:
            for _ in range(100):
                if not _alive(pid):
                    break
                await asyncio.sleep(0.02)


async def test_shader_appended_toggled_and_user_shader_kept(env: Live, tmp_path: Path) -> None:
    clip = _make_clip(tmp_path / "up.mkv", rate=24, seconds=30)
    sid = await env.play(clip, None)
    s = env.s(sid)
    user = str(env.user_shader)  # type: ignore[attr-defined]

    async def current() -> list[str]:
        return [str(p) for p in (await s.ipc.get("glsl-shaders") or [])]

    assert await current() == [user, SHADER]  # appended after the user's list
    base = STATE["cfg"]
    standard = dataclasses.replace(
        base, general=dataclasses.replace(base.general, upscaling="standard")
    )
    async with s.lock:  # as the session's own operations hold it
        await s.sync_shader(standard)
    assert await current() == [user]
    async with s.lock:
        await s.sync_shader(base)
        await s.sync_shader(base)  # unchanged: nothing more is added
    assert await current() == [user, SHADER]
