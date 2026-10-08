# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.5/§6: the facade matches the frozen text; FakeCore has every public member
with identical signatures; a shared contract suite runs against FakeCore
scenarios and against the real core."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import inspect
import time
from collections.abc import Awaitable, Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import api
from buttereye.core.api import ButterEye
from buttereye.core.errors import ButterEyeError, ConfigConflict, ConfigError, NotAvailable
from buttereye.core.events import (
    ConfigChanged,
    Event,
    OpFinished,
    OpStarted,
    SessionAdded,
    SessionChanged,
    SessionEnded,
)
from buttereye.core.ops import Operation
from buttereye.core.testing import REQUIRED, SCENARIOS, FakeCore, open_fake
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchRequest,
    Capabilities,
    CleanTarget,
    DetachPolicy,
    Feature,
    FilterState,
    OpState,
    Paths,
    Reason,
    RenderJobSpec,
    SessionId,
    SetupChoices,
    ShutdownReport,
)


def _public(cls: type) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        if klass is object:
            continue
        for name, value in vars(klass).items():
            if not name.startswith("_"):
                out[name] = value
    return out


_Fn = ast.FunctionDef | ast.AsyncFunctionDef

# ---------------------------------------------------------------------------
# Transcription of §2.5
# ---------------------------------------------------------------------------


def _doc_facade(design_doc: Any) -> tuple[dict[str, _Fn], dict[str, _Fn]]:
    tree = ast.parse(design_doc.python("### 2.5"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ButterEye")
    methods = {
        n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    funcs = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return methods, funcs


def _doc_params(node: _Fn) -> list[tuple[str, str, str | None]]:
    a = node.args
    out: list[tuple[str, str, str | None]] = []
    pos = a.posonlyargs + a.args
    defaults = [None] * (len(pos) - len(a.defaults)) + list(a.defaults)
    for arg, d in zip(pos, defaults, strict=True):
        out.append((arg.arg, "POSITIONAL_OR_KEYWORD", ast.unparse(d) if d is not None else None))
    for arg, d in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        out.append((arg.arg, "KEYWORD_ONLY", ast.unparse(d) if d is not None else None))
    if a.kwarg:
        out.append((a.kwarg.arg, "VAR_KEYWORD", None))
    return [p for p in out if p[0] not in ("self", "cls")]


def _real_params(fn: Callable[..., Any]) -> list[tuple[str, str, str | None]]:
    sig = inspect.signature(fn)
    out = []
    for p in sig.parameters.values():
        if p.name in ("self", "cls"):
            continue
        default = None
        if p.default is not inspect.Parameter.empty:
            d = p.default
            default = f"{type(d).__name__}.{d.name}" if hasattr(d, "name") and hasattr(
                type(d), "__members__") else repr(d)  # fmt: skip
        out.append((p.name, p.kind.name, default))
    return out


def test_facade_matches_design(design_doc: Any) -> None:
    methods, funcs = _doc_facade(design_doc)
    for name, node in methods.items():
        real = getattr(ButterEye, name)
        assert _real_params(real) == _doc_params(node), name
        is_async = isinstance(node, ast.AsyncFunctionDef)
        assert inspect.iscoroutinefunction(real) == is_async, name
    for name, node in funcs.items():
        real = getattr(api, name)
        assert _real_params(real) == _doc_params(node), name
        assert name in api.__all__
    public = {n for n in _public(ButterEye) if not n.startswith("_")}
    assert public == set(methods), public ^ set(methods)


def test_api_reexports(design_doc: Any) -> None:
    from buttereye.core import errors, events, ops, types

    for mod in (types, errors, events, ops):
        for name in mod.__all__:
            assert name in api.__all__, name
            assert getattr(api, name) is getattr(mod, name), name


# ---------------------------------------------------------------------------
# FakeCore surface
# ---------------------------------------------------------------------------

FAKE_EXTRAS = {
    "fake_emit", "fake_set_session", "fake_set_capability", "fake_block_loop",
    "fake_call_count", "for_scenario", "scenario_name", "scenario_override",
}  # fmt: skip


def test_fakecore_has_identical_signatures() -> None:
    real = _public(ButterEye)
    for name, member in real.items():
        assert name in vars(FakeCore), f"FakeCore does not override {name}"
        fake = vars(FakeCore)[name]
        rf = member.__func__ if isinstance(member, classmethod) else member
        ff = fake.__func__ if isinstance(fake, classmethod) else fake
        assert type(member) is type(fake), name
        rs, fs = inspect.signature(rf), inspect.signature(ff)
        assert list(rs.parameters.values()) == list(fs.parameters.values()), name
        if name != "open":  # open returns its own class
            assert rs.return_annotation == fs.return_annotation, name
        assert inspect.iscoroutinefunction(rf) == inspect.iscoroutinefunction(ff), name
    extras = set(_public(FakeCore)) - set(real)
    assert extras <= FAKE_EXTRAS, extras - FAKE_EXTRAS
    assert issubclass(FakeCore, ButterEye)


def test_required_scenarios_exist() -> None:
    assert set(REQUIRED) <= set(SCENARIOS)
    for name in SCENARIOS:
        s = sc.get(name)
        assert s.name == name
        assert set(s.capabilities) == set(Feature)
    with pytest.raises(KeyError):
        sc.get("nope")


def _assert_real_types(value: Any, path: str = "") -> None:
    if id(value) in _assert_real_types.seen:  # type: ignore[attr-defined]
        return
    _assert_real_types.seen.add(id(value))  # type: ignore[attr-defined]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        mod = type(value).__module__
        assert mod.startswith(("buttereye.core.types", "buttereye.core.events")) or (
            mod == "buttereye.core.testing.scenarios" and type(value).__name__ == "Scenario"
        ), (path, type(value))
        for f in dataclasses.fields(value):
            _assert_real_types(getattr(value, f.name), f"{path}.{f.name}")
    elif isinstance(value, (tuple, frozenset, list)):
        for i, v in enumerate(value):
            _assert_real_types(v, f"{path}[{i}]")
    elif hasattr(value, "items") and not isinstance(value, str):
        for k, v in value.items():
            _assert_real_types(v, f"{path}[{k!r}]")


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_scenario_data_uses_real_dataclasses(name: str) -> None:
    _assert_real_types.seen = set()  # type: ignore[attr-defined]
    _assert_real_types(sc.get(name), name)


# ---------------------------------------------------------------------------
# Shared contract suite: FakeCore scenarios and the real core
# ---------------------------------------------------------------------------


#: method -> (feature, call factory) for the contract suite
def _calls(core: ButterEye, tmp: Path) -> dict[str, tuple[Feature, Callable[[], Any]]]:
    sid = SessionId("s1")
    return {
        "hardware": (Feature.HARDWARE, core.hardware),
        "plugins": (Feature.DOCTOR, core.plugins),
        "discover": (Feature.DISCOVER, core.discover),
        "sessions": (Feature.LIVE, core.sessions),
        "set_interpolation": (Feature.LIVE, lambda: core.set_interpolation(sid, True)),
        "bench_history": (Feature.BENCH, core.bench_history),
        "render_jobs": (Feature.RENDER, core.render_jobs),
        "stale_jobs": (Feature.RENDER, core.stale_jobs),
        "storage": (Feature.STORAGE, core.storage),
        "models": (Feature.MODELS, core.models),
        "model_downloads": (Feature.MODEL_DOWNLOADS, core.model_downloads),
        "third_party": (Feature.LICENCES, core.third_party),
        "report_bundle": (Feature.REPORT, lambda: core.report_bundle(tmp / "r.tar.zst")),
    }


CoreFactory = Callable[[Path], Awaitable[ButterEye]]


async def _real(tmp: Path) -> ButterEye:
    return await ButterEye.open(sc.fake_paths(tmp / "real"))


def _fake(name: str) -> CoreFactory:
    async def make(tmp: Path) -> ButterEye:
        return await open_fake(name, sc.fake_paths(tmp / "fake"))

    return make


CORES: list[tuple[str, CoreFactory]] = [
    ("fake-devbox_now", _fake("devbox_now")),
    ("fake-all_ready", _fake("all_ready")),
    ("fake-live_active", _fake("live_active")),
    ("real", _real),
]


@pytest.mark.parametrize(("label", "factory"), CORES, ids=[c[0] for c in CORES])
async def test_contract_suite(label: str, factory: CoreFactory, tmp_path: Path) -> None:
    core = await factory(tmp_path)
    try:
        caps = await core.capabilities()
        assert isinstance(caps, Capabilities)
        assert set(caps.states) == set(Feature)
        assert isinstance(await core.ping(), float)
        assert isinstance(core.paths(), Paths)
        assert (await core.include_line()).startswith("include=")
        for name, (feature, call) in _calls(core, tmp_path).items():
            st = caps.states[feature]
            gated = not st.available and st.reason in (
                Reason.NOT_IMPLEMENTED,
                Reason.NOTHING_LISTED,
            )
            if gated:
                with pytest.raises(NotAvailable) as exc:
                    res = call()
                    if inspect.isawaitable(res):
                        await res
                assert exc.value.feature is feature, name
            elif label.startswith("fake"):
                res = call()
                if isinstance(res, Operation):
                    await res.result()
                else:
                    try:
                        await res
                    except ButterEyeError:
                        pass  # e.g. unknown session id; the call itself was not gated
    finally:
        rep = await core.close(cancel_jobs=True)
    assert isinstance(rep, ShutdownReport)
    assert await core.close(cancel_jobs=True) is rep  # idempotent


@pytest.mark.parametrize(("label", "factory"), CORES, ids=[c[0] for c in CORES])
async def test_subscribe_ends_on_close(label: str, factory: CoreFactory, tmp_path: Path) -> None:
    core = await factory(tmp_path)
    it = core.subscribe()
    got: list[Event] = []

    async def drain() -> None:
        async for ev in it:
            got.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0.01)
    await core.close(cancel_jobs=True)
    await asyncio.wait_for(task, 2)


# ---------------------------------------------------------------------------
# FakeCore behaviours the GUI units rely on
# ---------------------------------------------------------------------------


async def _collect(core: ButterEye) -> tuple[list[Event], asyncio.Task[None]]:
    got: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            got.append(ev)

    task = asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    return got, task


async def test_fake_doctor_progress_and_events() -> None:
    core = await open_fake("devbox_now")
    events, task = await _collect(core)
    op = core.doctor()
    seen: list[Any] = []
    op.add_progress_sink(seen.append)
    rep = await op.result()
    assert rep is not None and await core.last_report() == rep
    assert seen and seen[-1].done == seen[-1].total and seen[-1].unit == "checks"
    await asyncio.sleep(0)
    kinds = [type(e) for e in events]
    assert OpStarted in kinds and OpFinished in kinds
    await core.close(cancel_jobs=True)
    await task


async def test_fake_setup_cancel_saves_nothing() -> None:
    core = await open_fake("fresh_box")
    assert not (await core.load_config()).exists
    rep = await core.doctor().result()
    plan = await core.setup_plan(rep, trt_experimental=False)
    op = core.setup_apply(plan, SetupChoices(plan.proposed.backend or BackendId.MVTOOLS, False,
                                             frozenset()))  # fmt: skip
    await asyncio.sleep(0.015)
    op.cancel()
    assert await op.wait() is OpState.CANCELLED
    assert not (await core.load_config()).exists
    op2 = core.setup_apply(plan, SetupChoices(BackendId.RIFE_NCNN, False, frozenset()))
    res = await op2.result()
    load = await core.load_config()
    assert load.exists and load.revision == res.config_revision
    assert isinstance(core, FakeCore) and core.fake_call_count("setup_apply") == 2
    await core.close(cancel_jobs=True)


async def test_fake_config_conflict_flow() -> None:
    core = await open_fake("config_conflict")
    load = await core.load_config()
    assert len(load.config.unknown) == 3
    assert load.revision == "rev-1"  # loaded before someone else edited the file
    with pytest.raises(ConfigConflict) as exc:
        await core.save_config(load.config, expected_revision=load.revision)
    assert exc.value.on_disk_revision == "rev-disk"
    assert (await core.load_config()).revision == "rev-disk"  # "Reload theirs"
    events, task = await _collect(core)
    rev = await core.save_config(load.config, expected_revision="rev-disk")  # "Overwrite"
    await asyncio.sleep(0)
    assert ConfigChanged(rev) in events
    await core.close(cancel_jobs=True)
    await task


async def test_fake_config_newer_is_read_only() -> None:
    core = await open_fake("config_newer")
    load = await core.load_config()
    with pytest.raises(ConfigError) as exc:
        await core.save_config(load.config, expected_revision=load.revision)
    assert exc.value.code.code == "BE-2002"
    await core.close(cancel_jobs=True)


async def test_fake_play_then_active_then_detach() -> None:
    core = await open_fake("all_ready")
    events, task = await _collect(core)
    sid = await core.play(Path("/videos/film.mkv")).result()
    await asyncio.sleep(0.1)
    snaps = {s.sid: s for s in await core.sessions()}
    assert snaps[sid].filter is FilterState.ACTIVE
    assert any(isinstance(e, SessionAdded) for e in events)
    assert any(isinstance(e, SessionChanged) for e in events)
    with pytest.raises(ButterEyeError) as exc:
        core.play(Path("https://example.org/v.mkv"))
    assert exc.value.code.code == "BE-3007"
    await core.detach(sid, DetachPolicy.DISABLE_FILTER)
    await asyncio.sleep(0)
    assert SessionEnded(sid, "detached") in events
    rep = await core.close(cancel_jobs=True)
    assert sid not in rep.detached
    await task


async def test_fake_render_queue_runs() -> None:
    core = await open_fake("render_refusals")
    probe = await core.render_probe(Path("/videos/hlg.mkv"))
    assert probe.refusal is not None and probe.refusal.code is not None
    spec = RenderJobSpec(Path("/videos/a.mkv"), Path("/videos/a.out.mkv"), "fast", "libx265")
    jid = await core.render_enqueue(spec)
    for _ in range(100):
        await asyncio.sleep(0.01)
        jobs = {j.id: j for j in await core.render_jobs()}
        if jobs[jid].state is OpState.SUCCEEDED:
            break
    jobs = {j.id: j for j in await core.render_jobs()}
    # the pre-existing queued job j3 runs first; both finish
    assert jobs[jid].state is OpState.SUCCEEDED
    assert len(await core.stale_jobs()) == 1
    await core.close(cancel_jobs=True)


async def test_fake_bench_and_clean() -> None:
    core = await open_fake("bench_results")
    req = BenchRequest(1920, 1080, Fraction(24000, 1001))
    res = await core.bench(req).result()
    assert res.request == req and res.recommended == "rife-v4.26"
    assert len(await core.bench_history()) == 2
    cleaned = await core.clean(frozenset({CleanTarget.ENGINES})).result()
    assert all("engines" in str(p) for p in cleaned.removed)
    await core.close(cancel_jobs=True)


async def test_fake_slow_core_blocks_first_ping() -> None:
    scen = dataclasses.replace(sc.get("slow_core"), block_first_ping_s=0.2)
    core = await open_fake(scen)
    t0 = time.monotonic()
    await core.ping()
    assert time.monotonic() - t0 >= 0.2
    t1 = time.monotonic()
    await core.ping()
    assert time.monotonic() - t1 < 0.1
    assert sc.get("slow_core").block_first_ping_s == 6.0
    await core.close(cancel_jobs=True)


async def test_fake_scripted_health_event() -> None:
    core = await open_fake("stalled")
    events, task = await _collect(core)
    await asyncio.sleep(0.05)
    assert any(type(e).__name__ == "HealthChanged" for e in events)
    await core.close(cancel_jobs=True)
    await task


async def test_fake_trt_optin_changes_capability() -> None:
    core = await open_fake("devbox_now")
    load = await core.load_config()
    assert (await core.capabilities()).states[Feature.TRT].reason is Reason.OPT_IN_REQUIRED
    await core.set_trt_experimental(True, expected_revision=load.revision)
    st = (await core.capabilities()).states[Feature.TRT]
    assert st.reason is Reason.MISSING_DEPENDENCY
    await core.close(cancel_jobs=True)


async def test_fake_hash_mismatch() -> None:
    scen = dataclasses.replace(sc.get("all_ready"), hash_mismatch=frozenset({"rife_v4.26.onnx"}))
    core = await open_fake(scen)
    with pytest.raises(ButterEyeError) as exc:
        await core.model_install("rife_v4.26.onnx").result()
    assert exc.value.code.code == "BE-6001"
    await core.close(cancel_jobs=True)
