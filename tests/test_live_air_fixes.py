# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live playback on the fanless M2 Air (review of 0.0.5): even pacing on a 60 Hz
panel, live limits that recover and ignore heat, the controller's error paths,
fewer per-frame IPC events, the upscaling shaders, and the picture, debanding,
LUT and A/B settings mpv applies after smoothing. No mpv or GPU needed; the
macOS power APIs are replaced by fixed answers.

The numbers mirror the Air: 23.976 fps 1080p film on its 60 Hz panel, mvgpu
about 72 and MVTools about 76 fps of live capacity at 1080p."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import sys
import time
import types as pytypes
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import commands, osapi
from buttereye.core.errors import ErrorCode
from buttereye.core.events import Notice
from buttereye.core.mpvctl import decide, health, ipc, launcher, livelimits, osd, shaders
from buttereye.core.mpvctl import session as live
from buttereye.core.scriptgen import generator
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchResult,
    Config,
    DetachPolicy,
    FilterState,
    Msg,
    PictureAdjust,
    PowerSnapshot,
    SessionId,
    Target,
    TargetKind,
)
from tests.test_live_decisions import (
    NTSC,
    FakeCtx,
    FakeIpc,
    _config,
    _load,
    _paths,
    _result,
    _session,
    _settle,
    _simple,
)
from tests.test_live_decisions import bench_history as bench_history  # noqa: F401 (fixture)
from tests.test_live_decisions import (
    only_the_planted_plugins as only_the_planted_plugins,  # noqa: F401 (fixture)
)

AIR_HZ = 60.0
MVGPU_1080 = 71.8 * live.LIVE_HEADROOM  # in mpv; 71.8 live
MVTOOLS_1080 = 76.0 * live.LIVE_HEADROOM


@pytest.fixture(autouse=True)
def cool_on_mains(monkeypatch: pytest.MonkeyPatch) -> dict[str, PowerSnapshot]:
    """The machine's power state, fixed (the real one is read on a Mac)."""
    state = {"now": PowerSnapshot()}
    monkeypatch.setattr(osapi, "power_snapshot", lambda: state["now"])
    return state


class StoreIpc(FakeIpc):
    """FakeIpc that remembers what was set and answers ``get`` from it."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.values: dict[str, Any] = dict(values or {})

    async def command(self, *args: object, timeout_s: float = 5.0) -> Any:
        self.commands.append(args)
        if args[0] == "get_property":
            return self.values.get(str(args[1]))
        if args[0] == "set_property":
            ipc.check_command(args)  # what ButterEye sends must pass the allowlist
            self.values[str(args[1])] = args[2]
        return None

    async def get(self, prop: str, *, timeout_s: float = 5.0) -> Any:
        self.commands.append(("get_property", prop))
        return self.values.get(prop)

    def sets(self) -> list[tuple[object, ...]]:
        return [c[1:] for c in self.commands if c[0] == "set_property"]


def _mv(backend: BackendId, mpv: float) -> BenchMeasurement:
    return BenchMeasurement(
        label=backend.value, backend=backend, model=None, vspipe_fps=mpv * 1.4, mpv_fps=mpv,
        cov=0.01, repeatable=True, startup_s=0.5, reload_s=0.4, vram_bytes=None,
        realtime=True, gpu_faults=0,
    )  # fmt: skip


def _air(mvgpu: float = MVGPU_1080, mvtools: float = MVTOOLS_1080) -> BenchResult:
    return _result(_mv(BackendId.MVGPU, mvgpu), _mv(BackendId.MVTOOLS, mvtools))


def _air_paths(tmp_path: Path) -> Any:
    paths = _paths(tmp_path, rife=False)
    (paths.rpm_plugin_dir / decide.MVGPU_PLUGIN).write_bytes(b"")
    return paths


def _general(cfg: Config, **extra: Any) -> Config:
    """``cfg`` with general settings replaced, also ones GeneralSettings does not
    have yet (``even_pacing``, ``cadence_first``: read with getattr)."""
    g = cfg.general
    fields = {f.name: getattr(g, f.name) for f in dataclasses.fields(g)}
    known = {k: v for k, v in extra.items() if k in fields}
    if len(known) == len(extra):
        return dataclasses.replace(cfg, general=dataclasses.replace(g, **known))
    return dataclasses.replace(cfg, general=pytypes.SimpleNamespace(**{**fields, **extra}))  # type: ignore[arg-type]


def _user_data(s: live._Session) -> list[dict[str, Any]]:
    out = []
    for c in s.ipc.commands:  # type: ignore[attr-defined]
        if c[:2] == ("vf", "add"):
            raw = str(c[2]).split(":user-data=", 1)[1]
            out.append(json.loads(raw[raw.index("%", 1) + 1 :]))
    return out


def _running_vf(s: live._Session) -> None:
    s.props["vf"] = [{"name": "vapoursynth", "label": "buttereye", "enabled": True}]


# ---------------------------------------------------------------------------
# B3: cadence on the 60 Hz panel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "hz", "uneven"),
    [
        (NTSC * 2, 60.0, True),  # 1.2513 refreshes a frame: 1-1-1-2
        (NTSC * 2, 60.0024, True),  # the Air's panel as mpv reports it
        (Fraction(60), 60.0024, False),
        (Fraction(30), 60.0, False),
        (NTSC * 2, 143.998, False),  # 144 Hz: 3.003, a 0.1 % resample
        (Fraction(60000, 1001), 60.0, False),  # within display-resample's 1 %
        (NTSC * 2, None, False),  # unknown: nothing said
        (None, 60.0, False),
    ],
)
def test_uneven_pacing(target: Fraction | None, hz: float | None, uneven: bool) -> None:
    assert decide.uneven_pacing(target, hz) is uneven


def test_cadence_is_refreshes_per_frame() -> None:
    assert decide.cadence(NTSC * 2, 60.0) == Fraction(60) / (NTSC * 2)
    assert float(decide.cadence(NTSC * 2, 60.0) or 0) == pytest.approx(1.2513, abs=1e-4)
    assert decide.cadence(NTSC * 2, None) is None


def test_osd_line_says_the_pacing_is_uneven() -> None:
    from buttereye.core.testing import scenarios as sc

    snap = sc.session(1)
    assert snap.source is not None
    uneven = dataclasses.replace(
        snap, source=dataclasses.replace(snap.source, fps=NTSC), target_fps=NTSC * 2,
        display_fps=60.0024,
    )  # fmt: skip
    assert osd.parts(uneven, ("rates",)) == ["23.976 → 47.952 fps (uneven on 60 Hz)"]
    even = dataclasses.replace(uneven, display_fps=144.0)
    assert osd.parts(even, ("rates",)) == ["23.976 → 47.952 fps"]


async def test_status_line_flags_uneven_pacing(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    s = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=False)
    s.filter = FilterState.ACTIVE
    assert s.target == NTSC * 2
    assert s.status_text().endswith("· uneven pacing on 60 Hz")
    s.display_fps = 144.0
    assert "uneven" not in s.status_text()


def test_pick_even_size_finds_60_at_720p_with_mvgpu() -> None:
    """24 -> 60 infers almost every frame (load ~120): out of reach at 1080p
    (72-76), within mvgpu's ~139 at 720p (``shrink_cap``)."""
    cap720 = decide.shrink_cap(71.8 * 2.25, 1920, 1080)
    assert cap720 == pytest.approx(139, abs=1)
    sized = [
        ((1280, 720), [decide.EngineOption(BackendId.MVGPU, cap720),
                       decide.EngineOption(BackendId.MVTOOLS, 160.0)]),
    ]  # fmt: skip
    found = decide.pick_even_size(sized, NTSC, AIR_HZ, TargetKind.DISPLAY, auto=True)
    assert found is not None
    pick, size = found
    assert pick.backend is BackendId.MVGPU and pick.target.target == 60 and size == (1280, 720)
    # MVTools alone (CPU) is never the cadence answer: that is the GPU's job
    cpu_only = [((1280, 720), [decide.EngineOption(BackendId.MVTOOLS, 160.0)])]
    assert decide.pick_even_size(cpu_only, NTSC, AIR_HZ, TargetKind.DISPLAY, auto=True) is None


async def test_cadence_first_smooths_smaller_for_even_pacing(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    profile = _simple(target=Target(TargetKind.DISPLAY))
    base = _config(profile)
    # resolution first (the default): 2x at 1080p, uneven
    s = _session(FakeCtx(_air_paths(tmp_path), base), tmp_path)
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=False)
    assert s.target == NTSC * 2 and s.run_size is None
    # cadence first: 60 at 720p, which every refresh shows once
    s2 = _session(FakeCtx(_air_paths(tmp_path), _general(base, cadence_first=True)), tmp_path)
    _load(s2, fps=float(NTSC), hz=AIR_HZ)
    await s2.apply(raise_on_fail=False)
    assert s2.backend is BackendId.MVGPU and s2.target == 60 and s2.run_size == (1280, 720)
    assert s2.notice is not None and s2.notice.key.startswith("Smoothing at {height}p so motion")


async def test_even_pacing_turns_mpv_blending_on_and_back(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    cfg = _general(_config(_simple()), even_pacing=True)
    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    s.ipc = StoreIpc({"interpolation": False, "tscale": "mitchell"})  # type: ignore[assignment]
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=False)
    assert s.ipc.values["interpolation"] is True and s.ipc.values["tscale"] == "oversample"  # type: ignore[attr-defined]
    await s.restore_player()
    assert s.ipc.values["interpolation"] is False and s.ipc.values["tscale"] == "mitchell"  # type: ignore[attr-defined]
    # turned off (Alt+b, the GUI) or rolled back: mpv's own values come back too
    for off in ("set_off", "rollback_failed"):
        await s.apply(raise_on_fail=False)
        assert s.ipc.values["interpolation"] is True  # type: ignore[attr-defined]
        if off == "set_off":
            await s.set_off(FilterState.OFF)
        else:
            await s.rollback_failed(())
        assert s.ipc.values["interpolation"] is False, off  # type: ignore[attr-defined]
        assert s.ipc.values["tscale"] == "mitchell", off  # type: ignore[attr-defined]
        s.want, s.filter = True, FilterState.PENDING
    # off by default: ButterEye's profile never blends on its own
    s3 = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    s3.ipc = StoreIpc({"interpolation": False})
    _load(s3, fps=float(NTSC), hz=AIR_HZ)
    await s3.apply(raise_on_fail=False)
    assert s3.ipc.sets() == []


# ---------------------------------------------------------------------------
# B4: live limits
# ---------------------------------------------------------------------------

KEY = livelimits.LimitKey("gpu", "mvgpu", None, 1920, 1080)


def test_raised_and_recovered() -> None:
    assert livelimits.raised(0.585) == pytest.approx(0.65)
    assert livelimits.raised(0.95) == 1.0
    assert livelimits.recovered(0.585, livelimits.RECOVER_AGE_S - 1) == 0.585
    assert livelimits.recovered(0.585, livelimits.RECOVER_AGE_S) == pytest.approx(0.65)
    assert livelimits.recovered(0.585, 6 * livelimits.RECOVER_AGE_S) == 1.0


def test_records_recover_with_age_and_fair_ones_expire_fast(tmp_path: Path) -> None:
    path = tmp_path / livelimits.FILE
    limits: dict[str, dict[str, float]] = {}
    livelimits.record(path, limits, KEY, 0.585, now=0.0, power=PowerSnapshot(False, False, 0))
    fair = livelimits.LimitKey("gpu", "mvgpu", None, 1280, 720)
    livelimits.record(path, limits, fair, 0.7, now=0.0, power=PowerSnapshot(True, False, 1))
    stored = json.loads(path.read_text())
    assert stored[KEY.text()]["thermal"] == 0 and stored[KEY.text()]["battery"] is False
    assert stored[fair.text()]["v"] == livelimits.VERSION == 4
    soon = livelimits.load(path, now=3600.0)
    assert livelimits.factor(soon, KEY, now=3600.0) == 0.585
    assert livelimits.factor(soon, fair, now=3600.0) == 0.7
    late = livelimits.FAIR_MAX_AGE_S + 1
    later = livelimits.load(path, now=late)
    assert livelimits.factor(later, fair, now=late) == 1.0, "a warm record is kept only hours"
    # a core that loaded them in the morning lets the warm one go by evening too
    assert livelimits.factor(soon, fair, now=late) == 1.0
    four_days = 2 * livelimits.RECOVER_AGE_S
    aged = livelimits.load(path, now=four_days)
    assert livelimits.factor(aged, KEY, now=four_days) == pytest.approx(0.585 / 0.9**2)
    assert livelimits.factor(soon, KEY, now=four_days) == pytest.approx(0.585 / 0.9**2)
    assert livelimits.load(path, now=12 * livelimits.RECOVER_AGE_S) == {}
    # a factor of 1 is no record
    livelimits.record(path, limits, KEY, 1.0, now=0.0)
    assert KEY.text() not in json.loads(path.read_text())


def test_writing_another_record_does_not_recover_one_twice(tmp_path: Path) -> None:
    """B4: the age is applied when a factor is read; what ``record`` writes back
    is the factor as learned, next to when it was learned."""
    path = tmp_path / livelimits.FILE
    livelimits.record(path, {}, KEY, 0.585, now=0.0)
    four_days = 2 * livelimits.RECOVER_AGE_S
    limits = livelimits.load(path, now=four_days)
    assert livelimits.factor(limits, KEY, now=four_days) == pytest.approx(0.585 / 0.9**2)
    other = livelimits.LimitKey("gpu", "mvtools", None, 1920, 1080)
    livelimits.record(path, limits, other, 0.5, now=four_days)
    again = livelimits.load(path, now=four_days)
    assert livelimits.factor(again, KEY, now=four_days) == pytest.approx(0.585 / 0.9**2)
    assert json.loads(path.read_text())[KEY.text()]["factor"] == 0.585


def test_version_3_records_are_forgotten(tmp_path: Path) -> None:
    path = tmp_path / livelimits.FILE
    path.write_text(json.dumps({KEY.text(): {"factor": 0.42, "when": 100.0, "v": 3}}))
    assert livelimits.load(path, now=200.0) == {}


async def _air_running(tmp_path: Path, cfg: Config | None = None) -> tuple[FakeCtx, live._Session]:
    ctx = FakeCtx(_air_paths(tmp_path), cfg or _config(_simple()))
    s = _session(ctx, tmp_path)
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=False)
    await _settle(ctx)
    _running_vf(s)
    return ctx, s


def _drops(rate: float) -> Any:
    from buttereye.core.mpvctl.health import Verdict
    from buttereye.core.types import Health, Msg

    return Verdict(Health.DROPPING, None, Msg("Dropping frames."), (), True, rate)


async def test_heavy_drops_while_hot_are_not_learned(
    tmp_path: Path,
    bench_history: list[BenchResult],
    monkeypatch: pytest.MonkeyPatch,
    cool_on_mains: dict[str, PowerSnapshot],
) -> None:
    bench_history.append(_air())
    ctx, s = await _air_running(tmp_path)
    assert s.backend is BackendId.MVGPU and s.auto_chosen
    cool_on_mains["now"] = PowerSnapshot(on_battery=True, low_power=False, thermal=2)
    monkeypatch.setattr(s.detector, "update", lambda _sample: _drops(0.4))
    s.heavy_since = time.monotonic() - live.HEAVY_HOLD_S - 1
    await s.step()
    await _settle(ctx)
    # replaced for this video (mvgpu at 720p on a Mac, P3; MVTools elsewhere)
    assert (s.backend, s.run_size) != (BackendId.MVGPU, None)
    assert not (ctx.paths.state_dir / livelimits.FILE).exists(), "heat is not the engine"
    assert any("mvgpu" in k for k in s.video_limits)


async def test_heavy_drops_when_cool_are_learned_with_the_power_state(
    tmp_path: Path,
    bench_history: list[BenchResult],
    monkeypatch: pytest.MonkeyPatch,
    cool_on_mains: dict[str, PowerSnapshot],
) -> None:
    bench_history.append(_air())
    ctx, s = await _air_running(tmp_path)
    cool_on_mains["now"] = PowerSnapshot(on_battery=False, low_power=False, thermal=1)
    monkeypatch.setattr(s.detector, "update", lambda _sample: _drops(0.4))
    s.heavy_since = time.monotonic() - live.HEAVY_HOLD_S - 1
    await s.step()
    await _settle(ctx)
    (rec,) = json.loads((ctx.paths.state_dir / livelimits.FILE).read_text()).values()
    assert rec["thermal"] == 1 and rec["battery"] is False and rec["factor"] < 1


async def test_a_clean_run_raises_a_lowered_estimate(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    ctx, s = await _air_running(tmp_path)
    assert s.backend is BackendId.MVGPU
    key = s.limit_key(BackendId.MVGPU, None, (1920, 1080), 1.0, s.gpu_device(ctx.cfg))
    limits = s.live_limits()
    livelimits.record(ctx.paths.state_dir / livelimits.FILE, limits, key, 0.81)
    s.detector.flowing = True
    s.clean_s = live.CLEAN_RECOVER_S - 0.5
    now = time.monotonic()
    s.note_clean(now, None)
    s.props["pause"] = True  # a pause stops the count, it does not reset it
    s.note_clean(now + 1, None)
    assert livelimits.factor(limits, key) == 0.81
    s.props["pause"] = False
    s.note_clean(now + 2, None)
    assert livelimits.factor(limits, key) == pytest.approx(0.9)
    # drops start the count again
    s.clean_s = 100.0
    s.note_clean(now + 3, 0.05)
    assert s.clean_s == 0.0


async def test_live_caps_leave_room_when_the_mac_is_hot(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    from tests.test_live_decisions import _facts

    bench_history.append(_air())
    s = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    cool = await s.sustainable_fps(BackendId.MVGPU, None, _facts(), None)
    s.power = PowerSnapshot(thermal=2)
    hot = await s.sustainable_fps(BackendId.MVGPU, None, _facts(), None)
    assert cool == pytest.approx(71.8)
    assert hot == pytest.approx(71.8 / live.PRESSURE_HEADROOM)


# ---------------------------------------------------------------------------
# P3: mvgpu at a smaller size before CPU MVTools at full size (macOS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mac", [True, False])
async def test_mvgpu_smaller_beats_mvtools_full_size_on_a_mac(
    tmp_path: Path, bench_history: list[BenchResult], monkeypatch: pytest.MonkeyPatch, mac: bool
) -> None:
    """mvgpu's 1080p estimate below the 47.95 a 2x needs (55 / 1.25 = 44): 0.0.5
    went to MVTools at 1080p and never asked mvgpu at 720p."""
    monkeypatch.setattr(decide, "prefer_gpu_smaller", lambda: mac)
    bench_history.append(_air(mvgpu=55.0))
    s = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=False)
    assert s.target == NTSC * 2
    if mac:
        assert s.backend is BackendId.MVGPU and s.run_size == (1280, 720)
    else:
        assert s.backend is BackendId.MVTOOLS and s.run_size is None


# ---------------------------------------------------------------------------
# B14, B22: the controller's error paths
# ---------------------------------------------------------------------------


async def test_unwritable_script_rolls_back_instead_of_killing_the_controller(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    ctx, s = await _air_running(tmp_path)
    assert s.active_gen is not None
    s.script_path = tmp_path / "removed-runtime-dir" / "mpv-s1.vpy"
    await s.apply(raise_on_fail=False)
    assert s.filter is FilterState.ROLLED_BACK and s.active_gen is None
    notes = [e for e in ctx.events if isinstance(e, Notice)]
    assert notes[-1].code is not None and notes[-1].code.name == "VF_ROLLED_BACK"
    assert ("vf", "remove", "@buttereye") in s.ipc.commands


async def test_run_survives_an_unexpected_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    s = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    s.opts.tick_s = 0.01
    calls = []

    async def step() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        s.ended = True

    async def probe() -> None:
        return None

    s.step = step  # type: ignore[method-assign]
    s.probe = probe  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await asyncio.wait_for(s.run(), 5)
    assert len(calls) == 2
    assert any("controller step failed" in r.message for r in caplog.records)


async def test_mpv_quitting_during_verify_is_not_a_failed_filter(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)

    async def verify(gen: int, target_fps: float) -> bool:
        s.ipc.closed.set()  # the user closed mpv in the 4 s window
        return False

    s.verify = verify  # type: ignore[method-assign]
    _load(s, fps=float(NTSC), hz=AIR_HZ)
    await s.apply(raise_on_fail=True)  # no FilterFailed
    assert s.filter is not FilterState.ROLLED_BACK
    assert not [e for e in ctx.events if isinstance(e, Notice)]


# ---------------------------------------------------------------------------
# B15: GPU fault scan on macOS
# ---------------------------------------------------------------------------


async def test_no_journal_wait_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = pytypes.ModuleType(health.GPUFAULT_MODULE)

    async def scan_xid(**kw: Any) -> Any:
        return pytypes.SimpleNamespace(readable=False, lines=())

    mod.scan_xid = scan_xid  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, health.GPUFAULT_MODULE, mod)
    monkeypatch.setattr(health, "JOURNAL_SETTLE_S", 30.0)
    for mac, win in ((True, False), (False, True)):
        monkeypatch.setattr(osapi, "IS_MACOS", mac)
        monkeypatch.setattr(osapi, "IS_WINDOWS", win)
        assert not health.journal_settles()
        assert await asyncio.wait_for(health.gpu_fault_scan(since_monotonic=0.0), 1) is None
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    assert health.journal_settles()


async def test_unknown_fault_state_is_said_once_and_never_at_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(health, "GPUFAULT_MODULE", "buttereye.core.doctor.no_such_module")
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    s.gpu_used = BackendId.MVGPU
    await s.check_gpu_faults(final=True)
    assert not ctx.events
    await s.check_gpu_faults()
    await s.check_gpu_faults()
    notes = [e for e in ctx.events if isinstance(e, Notice)]
    assert len(notes) == 1 and "can't be checked" in notes[0].message.key


# ---------------------------------------------------------------------------
# B19, B23, B24, B27
# ---------------------------------------------------------------------------


async def test_toggle_ack_does_not_wait_for_the_lock(tmp_path: Path) -> None:
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    async with s.lock:  # verify() can hold it for 4 s
        s.on_event({"event": "client-message", "args": ["buttereye-request", "toggle"]})
        await asyncio.wait_for(_settle(ctx), 1)
        assert ("script-message-to", "buttereye", "ack", "toggle") in s.ipc.commands  # type: ignore[attr-defined]
    assert s.toggle_requested
    await s.step()
    assert not s.toggle_requested
    acks = [c for c in s.ipc.commands if c[:3] == ("script-message-to", "buttereye", "ack")]
    assert len(acks) == 1


async def test_discover_skips_the_player_being_launched(tmp_path: Path) -> None:
    paths = _air_paths(tmp_path)
    ctx = FakeCtx(paths, _config(_simple()))
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    sock = paths.runtime_dir / "mpv-abc.sock"
    sock.write_text("")  # not a socket yet: refused, but listed
    assert len(await live.discover(ctx)) == 1  # type: ignore[arg-type]
    live.registry(ctx).launching.add(sock)  # type: ignore[arg-type]
    assert await live.discover(ctx) == ()  # type: ignore[arg-type]


async def test_a_slow_detach_that_finished_is_not_reported_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    done = _session(ctx, tmp_path)
    stuck = _session(ctx, tmp_path)
    stuck.sid = SessionId("s2")
    reg = live.registry(ctx)  # type: ignore[arg-type]
    reg.sessions[done.sid] = done
    reg.sessions[stuck.sid] = stuck

    async def slow_detach(_ctx: Any, sid: SessionId, policy: DetachPolicy) -> None:
        if sid == done.sid:
            done.ended = True
        await asyncio.sleep(10)

    monkeypatch.setattr(live, "detach", slow_detach)
    report = await live.close(ctx, DetachPolicy.KEEP_FILTER, timeout_s=0.05)  # type: ignore[arg-type]
    assert report.detached == (done.sid,)
    assert [sid for sid, _ in report.failed] == [stuck.sid]


async def test_turning_smoothing_off_counts_only_when_the_filter_went(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B24: detach always ends the session, so a timeout that cut "turn
    smoothing off" short before vf remove must not be reported as done."""
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    cut = _session(ctx, tmp_path)
    cut.active_gen = 3  # the filter runs
    removed = _session(ctx, tmp_path)
    removed.sid = SessionId("s2")
    removed.active_gen = 4
    reg = live.registry(ctx)  # type: ignore[arg-type]
    reg.sessions.update({cut.sid: cut, removed.sid: removed})

    async def slow_detach(_ctx: Any, sid: SessionId, policy: DetachPolicy) -> None:
        s = reg.sessions[sid]
        try:
            if s is removed:
                s.active_gen, s.filter_removed = None, True
            await asyncio.sleep(10)  # stuck before (or in) vf remove
        finally:
            s.ended = True  # as detach's own finally does

    monkeypatch.setattr(live, "detach", slow_detach)
    report = await live.close(ctx, DetachPolicy.DISABLE_FILTER, timeout_s=0.05)  # type: ignore[arg-type]
    assert report.detached == (removed.sid,)
    assert report.failed == ((cut.sid, ErrorCode.IPC_LOST),)
    # keeping the filter: a late detach is still a detach
    for s in (cut, removed):
        s.ended = False
    report = await live.close(ctx, DetachPolicy.KEEP_FILTER, timeout_s=0.05)  # type: ignore[arg-type]
    assert set(report.detached) == {cut.sid, removed.sid}


def test_mpv_missing_hint_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osapi, "MACOS_APP", Path("/Applications/ButterEye.app/x"))
    hint, cmds = launcher.mpv_missing_hint()
    assert "reinstall ButterEye" in hint.key and cmds == ()
    monkeypatch.setattr(osapi, "MACOS_APP", None)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    hint, cmds = launcher.mpv_missing_hint()
    assert cmds == (commands.install_command("mpv"),) and "Homebrew" in hint.key
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    assert launcher.mpv_missing_hint()[1] == (commands.install_command("mpv"),)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    assert launcher.mpv_missing_hint() == (Msg("Install mpv."), ())


# ---------------------------------------------------------------------------
# P8 (hwdec), P9 (per-frame properties)
# ---------------------------------------------------------------------------


def test_videotoolbox_first_on_macos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    assert launcher.hwdec() == "videotoolbox-copy,auto-copy"
    text = launcher.include_text()
    assert "\nhwdec=videotoolbox-copy,auto-copy\n" in text and "hwdec=nvdec" not in text
    argv = launcher.build_argv("mpv", include=tmp_path / "i", socket=tmp_path / "s", file=Path("f"))
    assert "--hwdec=videotoolbox-copy,auto-copy" in argv
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    assert launcher.hwdec() == launcher.HWDEC == "nvdec-copy,auto-copy"
    assert launcher.include_text() == launcher.CONF_TEMPLATE.read_text(encoding="utf-8")


def test_per_frame_properties_are_not_observed() -> None:
    assert set(live.TICK_PROPS) <= ipc.OBSERVABLE <= ipc.READABLE
    assert not set(live.TICK_PROPS) & live.OBSERVED
    assert {"frame-drop-count", "display-sync-active", "vf"} <= live.OBSERVED


async def test_tick_reads_the_per_frame_properties(tmp_path: Path) -> None:
    s = _session(FakeCtx(_air_paths(tmp_path), _config(_simple())), tmp_path)
    s.ipc = StoreIpc({"playback-time": 12.5, "video-frame-info": {"interlaced": False}})  # type: ignore[assignment]
    s.props["audio-pts"] = 12.4  # no reply for it: the last value stays
    await s.step()
    assert s.props["playback-time"] == 12.5 and s.props["audio-pts"] == 12.4
    asked = {c[1] for c in s.ipc.commands if c[0] == "get_property"}  # type: ignore[attr-defined]
    assert set(live.TICK_PROPS) <= asked


# ---------------------------------------------------------------------------
# P5 and RAVU: the upscaling shaders
# ---------------------------------------------------------------------------

RAVU = shaders.UPSCALERS["ravu"]


def test_ravu_is_the_recorded_file_and_allowed() -> None:
    readme = (shaders.SHADER_DIR / "README.md").read_text()
    assert hashlib.sha256(RAVU.read_bytes()).hexdigest() in readme
    assert "b3f0a59d68f33b7162051ea5970a5169558f0ea2" in readme
    head = RAVU.read_text()[:600]
    assert "GNU Lesser General Public License" in head and "version 3" in head
    assert shaders.shader_for("ravu") == RAVU and str(RAVU) in shaders.ALLOWED
    ipc.check_command(["change-list", "glsl-shaders", "append", str(RAVU)])


def test_video_scale_from_osd_dimensions() -> None:
    # 1080p fullscreen on the Air's 2560x1664 panel: letterboxed, 1.333x
    dims = {"w": 2560, "h": 1664, "mt": 112, "mb": 112, "ml": 0, "mr": 0}
    assert shaders.video_scale(dims, 1920, 1080) == pytest.approx(2560 / 1920)
    assert shaders.video_scale(dims, 1280, 720) == pytest.approx(2.0)
    assert shaders.video_scale(None, 1920, 1080) is None
    assert shaders.video_scale({"w": 0, "h": 0}, 1920, 1080) is None


@pytest.mark.parametrize(
    ("ratio", "reduced", "power", "allowed"),
    [
        (2.0, False, None, True),
        (1.333, False, None, False),  # most of its 2x output thrown away
        (None, False, None, True),  # unknown: the shader's own 1.3x condition
        (2.0, True, None, False),  # smoothing at a smaller size
        (2.0, False, PowerSnapshot(on_battery=True), False),
        (2.0, False, PowerSnapshot(low_power=True), False),
        (2.0, False, PowerSnapshot(thermal=2), False),
        (2.0, False, PowerSnapshot(on_battery=False, low_power=False, thermal=1), True),
    ],
)
def test_sharper_allowed(
    ratio: float | None, reduced: bool, power: PowerSnapshot | None, allowed: bool
) -> None:
    assert shaders.sharper_allowed(ratio, reduced_size=reduced, power=power) is allowed


async def test_sharper_is_dropped_at_a_smaller_size_and_on_battery(tmp_path: Path) -> None:
    cfg = _general(_config(_simple()), upscaling="sharper")
    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    await s.sync_shader(cfg)
    assert s.shader == shaders.UPSCALERS["sharper"]
    s.run_size = (1280, 720)
    await s.sync_shader(cfg)
    assert s.shader is None
    s.run_size = None
    s.power = PowerSnapshot(on_battery=True)
    await s.sync_shader(cfg)
    assert s.shader is None
    # RAVU is not gated
    ravu = _general(cfg, upscaling="ravu")
    await s.sync_shader(ravu)
    assert s.shader == RAVU


async def test_sharper_reads_how_much_mpv_enlarges_the_picture(tmp_path: Path) -> None:
    """P5: ``osd-dimensions`` is readable, so a 1080p film in a 1440p-high window
    (1.33x) leaves FSRCNNX off and a 2160p one (2x) turns it on."""
    assert "osd-dimensions" in ipc.READABLE
    cfg = _general(_config(_simple()), upscaling="sharper")
    from buttereye.core.testing import scenarios as sc

    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    s.source = sc.session(1).source  # 1920 x 1080
    store = StoreIpc({"osd-dimensions": {"w": 2560, "h": 1440, "ml": 0, "mr": 0, "mt": 0, "mb": 0}})
    s.ipc = store  # type: ignore[assignment]
    await s.sync_shader(cfg)
    assert s.shader is None
    store.values["osd-dimensions"] = {"w": 3840, "h": 2160, "ml": 0, "mr": 0, "mt": 0, "mb": 0}
    await s.sync_shader(cfg)
    assert s.shader == shaders.UPSCALERS["sharper"]


async def test_users_own_shaders_and_ewa_scaler_are_pointed_out_once(tmp_path: Path) -> None:
    ctx = FakeCtx(_air_paths(tmp_path), _config(_simple()))
    s = _session(ctx, tmp_path)
    s.ipc = StoreIpc({  # type: ignore[assignment]
        "glsl-shaders": ["~~/shaders/FSR.glsl", str(RAVU)], "scale": "ewa_lanczossharp",
    })  # fmt: skip
    await s.check_user_render(BackendId.MVTOOLS)  # CPU engine: not the point
    assert not ctx.events
    await s.check_user_render(BackendId.MVGPU)
    await s.check_user_render(BackendId.MVGPU)
    notes = [e.message for e in ctx.events if isinstance(e, Notice)]
    assert len(notes) == 2
    assert notes[0].params["count"] == 1 and notes[1].params["scale"] == "ewa_lanczossharp"


# ---------------------------------------------------------------------------
# picture, deband strength and grain, LUT
# ---------------------------------------------------------------------------


async def test_picture_settings_are_applied_and_put_back(tmp_path: Path) -> None:
    cfg = _config(_simple())
    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    s.ipc = StoreIpc({"brightness": 0.0, "contrast": 3.0, "saturation": 7.6})  # type: ignore[assignment]
    adjusted = _general(cfg, picture=PictureAdjust(brightness=10, contrast=-5))
    await s.sync_picture(adjusted)
    await s.sync_picture(adjusted)  # nothing changed: nothing sent
    assert s.ipc.sets() == [("brightness", 10), ("contrast", -5)]  # type: ignore[attr-defined]
    await s.sync_picture(_general(cfg, picture=PictureAdjust(contrast=-5)))
    assert s.ipc.values["brightness"] == 0  # type: ignore[attr-defined]
    await s.restore_player()  # detach
    assert s.ipc.values["contrast"] == 3 and s.ipc.values["saturation"] == 7.6  # type: ignore[attr-defined]
    assert not s.picture_restore


async def test_detach_restores_the_picture(tmp_path: Path) -> None:
    cfg = _general(_config(_simple()), picture=PictureAdjust(gamma=20))
    ctx = FakeCtx(_air_paths(tmp_path), cfg)
    s = _session(ctx, tmp_path)
    s.ipc = StoreIpc({"gamma": 0.0})  # type: ignore[assignment]
    live.registry(ctx).sessions[s.sid] = s  # type: ignore[arg-type]
    await s.sync_picture(cfg)
    assert s.ipc.values["gamma"] == 20  # type: ignore[attr-defined]
    await live.detach(ctx, s.sid, DetachPolicy.KEEP_FILTER)  # type: ignore[arg-type]
    assert s.ipc.values["gamma"] == 0 and s.ended  # type: ignore[attr-defined]


async def test_deband_strength_and_grain(tmp_path: Path) -> None:
    cfg = _config(_simple())
    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    s.ipc = StoreIpc({"deband": False, "deband-iterations": 1.0, "deband-grain": 32.0})  # type: ignore[assignment]
    strong = _general(cfg, deband=True, deband_strength="strong", deband_grain=40)
    await s.sync_deband(strong)
    v = s.ipc.values  # type: ignore[attr-defined]
    assert v["deband"] is True and v["deband-iterations"] == 3 and v["deband-threshold"] == 64
    assert v["deband-range"] == 24 and v["deband-grain"] == 40
    await s.sync_deband(cfg)  # off: mpv's own values back (its defaults where it had none)
    assert v["deband"] is False and v["deband-iterations"] == 1 and v["deband-grain"] == 32
    assert v["deband-threshold"] == 48 and v["deband-range"] == 16
    # light without grain is mpv's own setting: the deband-* values are left alone
    s.ipc = StoreIpc({"deband": False})
    await s.sync_deband(_general(cfg, deband=True))
    assert s.ipc.sets() == [("deband", True)]


async def test_lut_is_set_changed_and_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from buttereye.core.profiles import luts

    lut_file = tmp_path / "luts" / "film.cube"
    monkeypatch.setattr(
        luts, "resolve_lut", lambda paths, name: lut_file if name == "film.cube" else None
    )
    monkeypatch.setattr(luts, "is_lut_path", lambda value: value in ("", str(lut_file)))
    cfg = _config(_simple())
    s = _session(FakeCtx(_air_paths(tmp_path), cfg), tmp_path)
    s.ipc = StoreIpc({"lut": ""})  # type: ignore[assignment]
    await s.sync_lut(_general(cfg, lut="film.cube"))
    assert s.ipc.values["lut"] == str(lut_file) and s.lut == str(lut_file)  # type: ignore[attr-defined]
    await s.sync_lut(cfg)
    assert s.ipc.values["lut"] == "" and s.lut is None  # type: ignore[attr-defined]
    # a LUT from the user's own mpv.conf is never replaced (nor could it be put back)
    s.ipc = StoreIpc({"lut": "/home/u/grade.cube"})
    await s.sync_lut(_general(cfg, lut="film.cube"))
    assert s.ipc.sets() == []


def test_lut_at_launch(tmp_path: Path) -> None:
    lut = tmp_path / "luts" / "film.cube"
    argv = launcher.build_argv(
        "mpv", include=tmp_path / "i", socket=tmp_path / "s", file=Path("f"), lut=lut
    )
    assert f"--lut={lut}" in argv and "--lut-type=auto" in argv
    assert argv.index("--lut-type=auto") < argv.index("--")


# ---------------------------------------------------------------------------
# A/B split view, anti-aliasing, HDR
# ---------------------------------------------------------------------------


def test_user_data_carries_compare_aa_and_hdr_only_when_set() -> None:
    base = generator.FilterParams(
        gen=1, backend=BackendId.MVGPU, src_fps=NTSC, target_fps=NTSC * 2,
        buffered_frames=4, concurrent_frames=8,
    )  # fmt: skip
    plain = json.loads(generator.user_data(base))
    assert not {"compare", "aa", "hdr"} & set(plain)
    full = json.loads(
        generator.user_data(dataclasses.replace(base, compare="split", aa=True, hdr=True))
    )
    assert full["compare"] == "split" and full["aa"] is True and full["hdr"] is True


async def test_alt_c_turns_the_split_view_on_for_this_session(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    ctx, s = await _air_running(tmp_path)
    assert "compare" not in _user_data(s)[-1]
    s.on_event({"event": "client-message", "args": ["buttereye-request", "compare"]})
    await s.step()
    assert _user_data(s)[-1]["compare"] == "split" and s.compare is True
    s.on_event({"event": "client-message", "args": ["buttereye-request", "compare"]})
    _running_vf(s)
    await s.step()
    assert "compare" not in _user_data(s)[-1]


async def test_compare_setting_and_aa_reach_the_script(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    profile = dataclasses.replace(_simple(), aa=True)
    cfg = _general(_config(profile), compare="split")
    ctx, s = await _air_running(tmp_path, cfg)
    ud = _user_data(s)[-1]
    assert ud["backend"] == "mvgpu" and ud["compare"] == "split" and ud["aa"] is True
    # AA runs on the CPU: never with the CPU engine
    pinned = dataclasses.replace(_simple(BackendId.MVTOOLS), aa=True)
    ctx2, s2 = await _air_running(tmp_path, _config(pinned))
    assert _user_data(s2)[-1]["backend"] == "mvtools" and "aa" not in _user_data(s2)[-1]


async def test_hdr10_passthrough_keeps_ten_bits(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_air())
    profile = dataclasses.replace(_simple(), hdr="passthrough")
    s = _session(FakeCtx(_air_paths(tmp_path), _config(profile)), tmp_path)
    _load(s, fps=24.0, hz=AIR_HZ)
    s.props["video-params"] = {
        **s.props["video-params"], "gamma": "pq", "colormatrix": "bt.2020-ncl",
        "pixelformat": "p010",
    }  # fmt: skip
    await s.apply(raise_on_fail=False)
    assert _user_data(s)[-1]["hdr"] is True
