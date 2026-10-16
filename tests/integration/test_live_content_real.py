# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A 2D animation profile in the real mpv of the dev box: hold fill on, mpv's
buffer raised so the script may read the frames it needs, and seeking back and
forth while it runs never asks for a frame mpv has already dropped.

Same harness as test_live_play.py (``--vo=null``, display 60 Hz), on a clip
whose every drawing is held for two frames (12 fps drawings at 24 fps).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import types as pytypes
from collections.abc import Iterator
from pathlib import Path

import pytest

from buttereye.core.mpvctl import session as live
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigLoad,
    ContentKind,
    FilterState,
    Health,
    Paths,
    Profile,
    Target,
    TargetKind,
)
from tests.integration.test_live_play import (
    MVTOOLS_SO,
    PLUGIN_DIR,
    Live,
    _make_clip,
    _ours,
    _raw,
)
from tests.integration.test_live_play import live_env as _live_env_fixture

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

live_env = _live_env_fixture


def _config() -> Config:
    base = sc.default_config()
    anime = Profile(
        id="anime", name="anime", backend=BackendId.MVTOOLS, model=None, scale=None,
        target=Target(TargetKind.X2), sc_threshold=0.12, buffered_frames=None,
        concurrent_frames=None, content=ContentKind.ANIMATION,
    )  # fmt: skip
    return dataclasses.replace(base, profiles=base.profiles + (anime,))


@pytest.fixture
def config_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cfg = _config()

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-test")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("live play must not save the config")

    name = "buttereye.core.profiles.config"
    mod = pytypes.ModuleType(name)
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, name, mod)
    yield


@pytest.fixture(scope="module")
def twos(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("twos") / "twos.mkv"
    return _make_clip(out, rate=12, seconds=30, vf="fps=24")


async def test_animation_fills_holds_and_survives_seeks(live_env: Live, twos: Path) -> None:
    if not (PLUGIN_DIR / MVTOOLS_SO).is_file():
        pytest.skip("buttereye-vs-mvtools not installed")
    env = live_env
    sid = await env.play(twos, "anime")
    await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE, timeout=20)
    entry = _ours(await env.vf(sid))
    assert entry is not None
    ud = json.loads(entry["params"]["user-data"])
    assert ud["hold_fill"] is True and ud["still_skip"] is True
    assert int(entry["params"]["buffered-frames"]) >= live.hold_fill_buffer(live.HOLD_MAX)
    sock = env.s(sid).socket
    for target in (12, 3, 20, 8, 25, 1):
        _raw(sock, "seek", target, "absolute")
        await asyncio.sleep(0.6)
    _raw(sock, "seek", -2, "relative")
    await asyncio.sleep(1.5)
    snap = await env.snap(sid)
    assert snap.filter is FilterState.ACTIVE and snap.health is not Health.STALLED, snap
    text = env.s(sid).log_path.read_text(encoding="utf-8", errors="replace")
    assert os.path.getsize(env.s(sid).log_path) > 0
    # mpv's vf_vapoursynth: "Frame N requested, but only have frames starting
    # from M. Try increasing the buffered-frames suboption."
    assert "buffered-frames suboption" not in text
    assert "Filter error" not in text and "Python exception" not in text
