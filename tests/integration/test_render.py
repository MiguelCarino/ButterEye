# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Offline render through the real core (SCOPE §7, §7.7, §7.8; spike M0(e)).

Generated clips only (H.264 + AAC + SRT + chapters). MVTools (CPU) keeps it quick
and off the GPU. ffms2 comes from ``/usr/lib64/libffms2.so.5`` or, before it is
installed, from ``$BUTTEREYE_TEST_FFMS2`` (an unpacked ``dnf download ffms2``).
HOME and every XDG dir are temporary.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import types as pytypes
from collections.abc import AsyncIterator, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.events import JobChanged
from buttereye.core.render import jobs as rjobs
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigLoad,
    DetachPolicy,
    JobId,
    OpState,
    Paths,
    Profile,
    RenderJobSpec,
    RenderJobState,
    Target,
    TargetKind,
)

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")


@pytest.fixture
def ffms2(monkeypatch: pytest.MonkeyPatch) -> Path:
    lib = Path(os.environ.get("BUTTEREYE_TEST_FFMS2") or "/usr/lib64/libffms2.so.5")
    if not lib.is_file():
        pytest.skip("ffms2 is not installed (or set BUTTEREYE_TEST_FFMS2)")
    for tool in ("vspipe", "ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} missing")
    if not (PLUGIN_DIR / "mvtools.so").is_file():
        pytest.skip("buttereye-vs-mvtools not installed")
    monkeypatch.setattr(rjobs, "_ffms2_lib", lambda: lib)
    return lib


def _config() -> Config:
    base = sc.default_config()
    mv = Profile(
        id="mv2x",
        name="mv2x",
        backend=BackendId.MVTOOLS,
        model=None,
        scale=None,
        target=Target(TargetKind.X2),
        sc_threshold=0.12,
        buffered_frames=None,
        concurrent_frames=None,
    )
    return dataclasses.replace(base, profiles=(*base.profiles, mv))


@pytest.fixture
def config_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cfg = _config()

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-test")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("render must not save the config")

    mod = pytypes.ModuleType("buttereye.core.profiles.config")
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.profiles.config", mod)
    yield


@dataclasses.dataclass
class Env:
    core: ButterEye
    paths: Paths
    states: list[RenderJobState]

    async def wait(self, job: JobId, *finals: OpState, timeout: float = 120.0) -> RenderJobState:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            for s in await self.core.render_jobs():
                if s.id == job and s.state in finals:
                    return s
            await asyncio.sleep(0.1)
        raise AssertionError(f"job {job} never reached {finals}: {await self.core.render_jobs()}")


@pytest.fixture
async def env(xdg_env: Any, config_provider: None, ffms2: Path) -> AsyncIterator[Env]:
    paths = sc.fake_paths(env=os.environ)
    core = await ButterEye.open(paths)
    states: list[RenderJobState] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            if isinstance(ev, JobChanged):
                states.append(ev.job)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    try:
        yield Env(core, paths, states)
    finally:
        await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=5.0)
        task.cancel()


def _clip(out: Path, *, size: str, seconds: int) -> Path:
    srt = out.with_suffix(".srt")
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
    meta = out.with_suffix(".meta")
    meta.write_text(
        ";FFMETADATA1\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=One\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Two\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=24000/1001",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-i", os.fspath(srt), "-i", os.fspath(meta),
            "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map_chapters", "3",
            "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "srt", "-shortest",
            os.fspath(out),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    return out


def _probe(path: Path) -> dict[str, Any]:
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_chapters",
         "-of", "json", os.fspath(path)],
        capture_output=True, text=True, check=True, timeout=120,
    )  # fmt: skip
    return json.loads(res.stdout)


def _encoder(encoders: tuple[str, ...]) -> str:
    return "libx264" if "libx264" in encoders else encoders[0]


async def test_convert_keeps_streams_and_doubles_frames(env: Env, tmp_path: Path) -> None:
    src = _clip(tmp_path / "in put's.mkv", size="640x360", seconds=4)
    probe = await env.core.render_probe(src, profile_id="mv2x")
    assert probe.refusal is None, probe.refusal
    assert (probe.width, probe.height) == (640, 360) and probe.fps == Fraction(24000, 1001)
    assert [s.kind for s in probe.streams] == ["video", "audio", "subtitle"]
    assert probe.chapters == 2 and probe.sizes[0].width == 640
    out = tmp_path / "in put's.smooth.mkv"
    job = await env.core.render_enqueue(RenderJobSpec(src, out, "mv2x", _encoder(probe.encoders)))
    done = await env.wait(job, OpState.SUCCEEDED, OpState.FAILED)
    assert done.state is OpState.SUCCEEDED, done.error
    info = _probe(out)
    kinds = [s["codec_type"] for s in info["streams"]]
    assert kinds == ["video", "audio", "subtitle"]
    src_frames = int(_probe(src)["streams"][0]["nb_read_frames"])
    frames = int(info["streams"][0]["nb_read_frames"])
    assert abs(frames - 2 * src_frames) <= 1, (frames, src_frames)
    assert len(info["chapters"]) == 2
    # progress never went backwards; nothing hidden left behind; manifest gone
    done_frames = [s.done_frames for s in env.states if s.id == job and s.done_frames is not None]
    assert done_frames == sorted(done_frames)
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []
    assert not list(env.paths.jobs_dir.glob("render-*.json"))


async def test_convert_at_a_smaller_size(env: Env, tmp_path: Path) -> None:
    src = _clip(tmp_path / "big.mkv", size="1920x1080", seconds=2)
    probe = await env.core.render_probe(src, profile_id="mv2x")
    assert [(s.width, s.height) for s in probe.sizes] == [(1920, 1080), (1280, 720)]
    out = tmp_path / "big.smooth.mkv"
    job = await env.core.render_enqueue(
        RenderJobSpec(src, out, "mv2x", _encoder(probe.encoders), size=(1280, 720))
    )
    done = await env.wait(job, OpState.SUCCEEDED, OpState.FAILED)
    assert done.state is OpState.SUCCEEDED, done.error
    v = _probe(out)["streams"][0]
    assert (v["width"], v["height"]) == (1280, 720)


async def test_cancel_leaves_nothing(env: Env, tmp_path: Path) -> None:
    src = _clip(tmp_path / "long.mkv", size="1920x1080", seconds=40)
    probe = await env.core.render_probe(src, profile_id="mv2x")
    out = tmp_path / "long.smooth.mkv"
    job = await env.core.render_enqueue(RenderJobSpec(src, out, "mv2x", _encoder(probe.encoders)))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 60
    while loop.time() < deadline:
        cur = next(s for s in await env.core.render_jobs() if s.id == job)
        if cur.done_frames:
            break
        await asyncio.sleep(0.1)
    manifest = next(env.paths.jobs_dir.glob("render-*.json"))
    pgid = json.loads(manifest.read_text())["pgid"]
    await env.core.render_cancel(job)
    done = await env.wait(job, OpState.CANCELLED, OpState.SUCCEEDED, OpState.FAILED)
    assert done.state is OpState.CANCELLED
    left = sorted(p.name for p in tmp_path.iterdir() if p.name != "xdg")
    assert left == ["long.meta", "long.mkv", "long.srt"]
    assert not manifest.exists()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


async def test_existing_output_is_refused(env: Env, tmp_path: Path) -> None:
    src = _clip(tmp_path / "a.mkv", size="640x360", seconds=1)
    out = tmp_path / "a.smooth.mkv"
    out.write_bytes(b"keep me")
    with pytest.raises(rjobs.RenderRefused):
        await env.core.render_enqueue(RenderJobSpec(src, out, "mv2x", "libx264"))
    assert out.read_bytes() == b"keep me"
