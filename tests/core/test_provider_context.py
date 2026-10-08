# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""GUI.md §11: ProviderContext and the U10/U11 provider signatures, exercised with
stand-in provider modules injected into ``sys.modules`` under their real names.
This file doubles as a reference for the live (U10) and bench (U11) units."""

from __future__ import annotations

import asyncio
import dataclasses
import sys
import types as pytypes
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import capabilities
from buttereye.core.api import ButterEye, ProviderContext
from buttereye.core.errors import ButterEyeError, ConfigConflict, ErrorCode
from buttereye.core.events import (
    CapabilitiesChanged,
    ConfigChanged,
    Event,
    EventsDropped,
    SessionAdded,
    SessionChanged,
)
from buttereye.core.ops import OpContext
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BenchRequest,
    BenchResult,
    Config,
    ConfigLoad,
    DetachPolicy,
    Feature,
    InstanceCandidate,
    Msg,
    Paths,
    SessionId,
    SessionSnapshot,
    ShutdownReport,
)


def _module(name: str, **attrs: Any) -> pytypes.ModuleType:
    mod = pytypes.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def _reg(ctx: ProviderContext) -> dict[SessionId, SessionSnapshot]:
    return ctx.state("mpvctl.session", dict[SessionId, SessionSnapshot])


def _hist(ctx: ProviderContext) -> list[BenchResult]:
    return ctx.state("bench.runner", list[BenchResult])


class _ConfigStore:
    def __init__(self) -> None:
        self.cfg = sc.default_config()
        self.revision = "r0"
        self.saves = 0

    def load(self, paths: Paths) -> ConfigLoad:
        return sc.config_load(self.cfg, revision=self.revision)

    def save(self, paths: Paths, cfg: Config, *, expected_revision: str) -> str:
        if expected_revision != self.revision:
            raise ConfigConflict(ErrorCode.CONFIG_CONFLICT, Msg("changed"),
                                 on_disk_revision=self.revision)  # fmt: skip
        self.saves += 1
        self.cfg, self.revision = cfg, f"r{self.saves}"
        return self.revision


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    store = _ConfigStore()
    log: dict[str, Any] = {"store": store, "calls": []}

    # ---- U10 shapes: buttereye.core.mpvctl.session ----
    async def play(ctx: ProviderContext, file: Path, *, profile_id: str | None,
                   op: OpContext) -> SessionId:  # fmt: skip
        op.report(Msg("Starting mpv"))
        reg = _reg(ctx)
        sid = SessionId(f"s{len(reg) + 1}")
        snap = dataclasses.replace(sc.session(1, title=file.name), sid=sid, profile_id=profile_id)
        reg[sid] = snap
        ctx.emit(SessionAdded(snap))

        async def watch() -> None:
            n = 0
            while True:
                await asyncio.sleep(0.001)
                n += 1
                cur = dataclasses.replace(reg[sid], counters=sc.counters(n))
                reg[sid] = cur
                ctx.emit_coalesced(sid, SessionChanged(cur), min_interval_s=0.05)

        log["watch"] = ctx.spawn(watch(), name=f"watch-{sid}")
        return sid

    async def sessions(ctx: ProviderContext) -> tuple[SessionSnapshot, ...]:
        return tuple(_reg(ctx).values())

    async def set_interpolation(ctx: ProviderContext, sid: SessionId,
                                enabled: bool) -> SessionSnapshot:  # fmt: skip
        log["calls"].append(("set_interpolation", sid, enabled))
        return _reg(ctx)[sid]

    async def detach(ctx: ProviderContext, sid: SessionId, policy: DetachPolicy) -> None:
        log["calls"].append(("detach", sid, policy))

    async def apply_profile(ctx: ProviderContext, sid: SessionId,
                            profile_id: str | None) -> SessionSnapshot:  # fmt: skip
        assert ctx.config().profiles  # providers read the current config
        log["calls"].append(("apply_profile", sid, profile_id))
        return _reg(ctx)[sid]

    async def step_down(ctx: ProviderContext, sid: SessionId) -> SessionSnapshot:
        return _reg(ctx)[sid]

    async def discover(ctx: ProviderContext) -> tuple[InstanceCandidate, ...]:
        return sc.candidates_mixed(ctx.paths.runtime_dir)

    async def close(ctx: ProviderContext, policy: DetachPolicy, *,
                    timeout_s: float) -> ShutdownReport:  # fmt: skip
        assert ctx.closing
        log["calls"].append(("close", policy, timeout_s))
        sids = tuple(_reg(ctx))
        return ShutdownReport(sids, (), ())

    # ---- U11 shapes: buttereye.core.bench.runner ----
    async def bench(ctx: ProviderContext, req: BenchRequest, *, op: OpContext) -> BenchResult:
        op.report(Msg("RIFE (Vulkan) v4.26 — run 1 of 3 — in mpv"), done=1, total=3)
        result = sc.bench_result_4090(req)
        _hist(ctx).append(result)
        return result

    async def history(ctx: ProviderContext) -> tuple[BenchResult, ...]:
        return tuple(_hist(ctx))

    async def apply(ctx: ProviderContext, result: BenchResult, label: str, *,
                    expected_revision: str) -> str:  # fmt: skip
        cfg = ctx.config()
        new = dataclasses.replace(cfg, general=dataclasses.replace(cfg.general, gpu=label))
        return await ctx.save_config(new, expected_revision=expected_revision)

    mods = {
        "buttereye.core.mpvctl.session": _module(
            "buttereye.core.mpvctl.session", play=play, sessions=sessions,
            set_interpolation=set_interpolation, detach=detach, apply_profile=apply_profile,
            step_down=step_down, discover=discover, close=close),
        "buttereye.core.bench.runner": _module(
            "buttereye.core.bench.runner", bench=bench, history=history, apply=apply),
        "buttereye.core.profiles.config": _module(
            "buttereye.core.profiles.config", load=store.load, save=store.save),
    }  # fmt: skip
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    yield log


async def _open(tmp: Path) -> ButterEye:
    return await ButterEye.open(sc.fake_paths(tmp))


async def test_capabilities_follow_provider_presence(providers: dict[str, Any],
                                                     tmp_path: Path) -> None:  # fmt: skip
    core = await _open(tmp_path)
    caps = await core.capabilities()
    for f in (Feature.LIVE, Feature.DISCOVER, Feature.BENCH):
        assert caps.ok(f), (f, caps.states[f])
    for f in (Feature.ATTACH, Feature.ORPHANS, Feature.REPORT):
        assert not caps.ok(f)
    await core.close(cancel_jobs=True)


async def test_live_provider_flow(providers: dict[str, Any], tmp_path: Path) -> None:
    core = await _open(tmp_path)
    got: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            got.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    sid = await core.play(tmp_path / "film.mkv", profile_id="fast").result()
    assert [s.sid for s in await core.sessions()] == [sid]
    await core.set_interpolation(sid, False)
    await core.apply_profile(sid, None)
    await core.detach(sid, DetachPolicy.DISABLE_FILTER)
    assert len(await core.discover()) == 3
    await asyncio.sleep(0.2)
    changed = [e for e in got if isinstance(e, SessionChanged)]
    assert 1 <= len(changed) <= 8  # ~200 ms at <= 20 Hz (min_interval 50 ms)
    assert any(isinstance(e, SessionAdded) for e in got)
    rep = await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=False, timeout_s=1.0)
    assert rep.detached == (sid,)
    assert ("close", DetachPolicy.KEEP_FILTER, 1.0) in providers["calls"]
    assert providers["watch"].cancelled()
    await asyncio.wait_for(task, 2)
    with pytest.raises(ButterEyeError):
        await core.sessions()


async def test_bench_provider_and_config_save(providers: dict[str, Any], tmp_path: Path) -> None:
    core = await _open(tmp_path)
    got: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            got.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    req = BenchRequest(1920, 1080, Fraction(24000, 1001))
    op = core.bench(req)
    progress: list[Any] = []
    op.add_progress_sink(progress.append)
    result = await op.result()
    assert progress and progress[0].op_id == op.id
    assert await core.bench_history() == (result,)
    load = await core.load_config()
    rev = await core.apply_bench(result, "rife-v4.26", expected_revision=load.revision)
    assert rev == "r1"
    assert (await core.load_config()).config.general.gpu == "rife-v4.26"
    await asyncio.sleep(0)
    assert ConfigChanged("r1") in got
    assert any(isinstance(e, CapabilitiesChanged) for e in got)
    with pytest.raises(ConfigConflict):
        await core.apply_bench(result, "x", expected_revision="stale")
    await core.close(cancel_jobs=True)
    await task


async def test_provider_state_is_per_instance(providers: dict[str, Any], tmp_path: Path) -> None:
    a, b = await _open(tmp_path / "a"), await _open(tmp_path / "b")
    await a.play(tmp_path / "x.mkv").result()
    assert len(await a.sessions()) == 1
    assert await b.sessions() == ()
    await a.close(cancel_jobs=True)
    await b.close(cancel_jobs=True)


async def test_backpressure_drops_only_coalesced(tmp_path: Path) -> None:
    from buttereye.core.events import EventBus, Notice

    bus = EventBus()
    it = bus.subscribe(maxlen=4)
    snap = sc.session(1)
    for i in range(10):
        bus.publish_coalesced(f"k{i}", SessionChanged(snap))
    for i in range(6):
        bus.publish(Notice(Msg(f"n{i}")))
    bus.close()
    out = [ev async for ev in it]
    notices = [e for e in out if isinstance(e, Notice)]
    assert [n.message.key for n in notices] == [f"n{i}" for i in range(6)]
    assert any(isinstance(e, EventsDropped) for e in out)


def test_capability_table_names_documented(repo_root: Path) -> None:
    text = (repo_root / "docs" / "design" / "GUI.md").read_text(encoding="utf-8")
    section = text[text.index("## 11. Providers for U10/U11") :]
    for refs in capabilities.FEATURE_REQUIRES.values():
        for ref in refs:
            assert ref.dotted.removeprefix("buttereye.core.") in section, ref.dotted
