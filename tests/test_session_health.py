# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live health detector, behaviour-matrix decisions and session provider edges
(no real mpv; the end-to-end runs are in tests/integration/test_live_play.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import types as pytypes
from collections.abc import AsyncIterator, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.mpvctl import decide, health, launcher
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import HealthDetector, Sample, Thresholds
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BypassReason,
    DetachPolicy,
    Feature,
    HdrClass,
    Health,
    SessionId,
    Target,
    TargetKind,
)

TH = Thresholds(init_grace_s=5.0, frozen_s=2.0, lag_s=1.0, lag_hold_s=1.0)


def _s(t: float, pt: float | None, *, audio: float | None = None, paused: bool = False,
       drops: int = 0, seeking: bool = False) -> Sample:  # fmt: skip
    return Sample(t, pt, audio, paused, False, False, seeking, drops)


def _feed(d: HealthDetector, samples: list[Sample]) -> health.Verdict:
    v = d.update(samples[0])
    for s in samples[1:]:
        v = d.update(s)
    return v


# ---------------------------------------------------------------------------
# HealthDetector
# ---------------------------------------------------------------------------


def test_flowing_after_playback_advances() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    assert not d.update(_s(0.0, 1.0)).flowing
    v = _feed(d, [_s(0.25 * i, 1.0 + 0.25 * i) for i in range(1, 4)])
    assert v.flowing and v.health is Health.OK


def test_inactive_detector_is_ok() -> None:
    d = HealthDetector(TH)
    assert d.update(_s(100.0, None)).health is Health.OK


def test_frozen_after_flowing_stalls() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    _feed(d, [_s(0.1 * i, 0.1 * i) for i in range(1, 60)])  # 6 s of play, past the grace
    v = _feed(d, [_s(6.0 + 0.25 * i, 5.9) for i in range(1, 12)])
    assert v.health is Health.STALLED and v.code is ErrorCode.FILTER_STALLED
    assert v.evidence and "unchanged" in v.evidence[0]
    # latched until the next filter start
    assert d.update(_s(20.0, 9.0)).health is Health.STALLED
    d.filter_started(21.0, 60.0)
    assert d.update(_s(21.0, 9.0)).health is Health.OK


def test_never_flowing_stalls_after_grace_only() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    assert _feed(d, [_s(0.5 * i, 1.0) for i in range(1, 9)]).health is Health.OK  # 4 s < 5 s
    assert _feed(d, [_s(4.0 + 0.5 * i, 1.0) for i in range(1, 4)]).health is Health.STALLED


def test_pause_and_seek_never_stall() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    _feed(d, [_s(0.1 * i, 0.1 * i) for i in range(1, 60)])
    v = _feed(d, [_s(6.0 + 0.5 * i, 5.9, paused=True) for i in range(1, 40)])
    assert v.health is Health.OK
    v = _feed(d, [_s(26.0 + 0.5 * i, 5.9, seeking=True) for i in range(1, 10)])
    assert v.health is Health.OK


def test_restart_gives_reinit_grace() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    _feed(d, [_s(0.1 * i, 0.1 * i) for i in range(1, 60)])
    d.note_restart(6.0)  # seek: the VS core is recreated (§4.4)
    assert _feed(d, [_s(6.0 + 0.5 * i, 5.9) for i in range(1, 9)]).health is Health.OK
    assert _feed(d, [_s(10.0 + 0.5 * i, 5.9) for i in range(1, 4)]).health is Health.STALLED


def test_audio_over_stalled_picture() -> None:
    """The M0(f) failure: audio keeps running while video crawls."""
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    samples = [_s(0.25 * i, 0.05 * i, audio=0.25 * i) for i in range(1, 12)]
    v = _feed(d, samples)
    assert v.health is Health.STALLED
    assert v.reason is not None and "audio" in v.reason.key
    assert v.evidence and "audio-pts" in v.evidence[0]


def test_short_lag_is_tolerated() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    samples = [_s(0.25 * i, 0.25 * i, audio=0.25 * i + (1.5 if i in (4, 5) else 0.0))
               for i in range(1, 20)]  # fmt: skip
    assert _feed(d, samples).health is Health.OK


def test_drops_over_one_percent() -> None:
    d = HealthDetector(TH)
    d.filter_started(0.0, 60.0)
    # 60 fps target, 3 drops per second = 5 %
    v = _feed(d, [_s(0.5 * i, 0.5 * i, drops=int(1.5 * i)) for i in range(1, 22)])
    assert v.health is Health.DROPPING and v.drop_rate is not None
    assert 0.04 < v.drop_rate < 0.06
    assert v.reason is not None and v.reason.params["percent"] == pytest.approx(5.0, abs=0.6)
    # 0.5 % is fine
    d2 = HealthDetector(TH)
    d2.filter_started(0.0, 60.0)
    v2 = _feed(d2, [_s(0.5 * i, 0.5 * i, drops=int(0.15 * i)) for i in range(1, 22)])
    assert v2.health is Health.OK and v2.drop_rate is not None and v2.drop_rate < 0.01


def test_sample_from_props() -> None:
    s = health.sample_from(
        {"playback-time": 3.5, "audio-pts": 3.4, "pause": False, "core-idle": True,
         "frame-drop-count": 7, "seeking": None},
        1.0,
    )  # fmt: skip
    assert (s.playback_time, s.audio_pts, s.core_idle, s.frame_drops) == (3.5, 3.4, True, 7)
    assert health.sample_from({"frame-drop-count": True}, 0.0).frame_drops == 0


# ---------------------------------------------------------------------------
# mpv log classification and GPU fault hook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg", "ours", "expected"),
    [
        ({"prefix": "vf", "level": "error",
          "text": "Disabling filter buttereye because it has failed.\n"}, True, "filter_failed"),
        ({"prefix": "vf", "level": "error",
          "text": "Disabling filter other because it has failed.\n"}, True, None),
        ({"prefix": "vapoursynth", "level": "fatal", "text": "Script evaluation failed:\n"},
         True, "filter_failed"),
        ({"prefix": "vapoursynth", "level": "fatal", "text": "Script evaluation failed:\n"},
         False, None),
        ({"prefix": "vapoursynth", "level": "error", "text": "Filter error at frame 3: \n"},
         True, "filter_failed"),
        ({"prefix": "vapoursynth", "level": "warn",
          "text": "Frame requested during init! This is unsupported.\n"}, True, None),
        ({"prefix": "vapoursynth", "level": "error", "text": "vkQueueSubmit failed -4\n"},
         False, "device_lost"),
        ({"prefix": "cplayer", "level": "warn", "text": "Audio/Video desynchronisation"},
         True, None),
    ],
)  # fmt: skip
def test_classify_log(msg: dict[str, Any], ours: bool, expected: str | None) -> None:
    assert health.classify_log(msg, only_our_vapoursynth=ours) == expected


def test_device_lost_lines(tmp_path: Path) -> None:
    log = tmp_path / "mpv.log"
    log.write_text("ok\n[0 NVIDIA] vkWaitForFences failed -4\nmore\nvkQueueSubmit failed -4\n")
    assert health.device_lost_lines(log) == (
        "[0 NVIDIA] vkWaitForFences failed -4",
        "vkQueueSubmit failed -4",
    )
    assert health.device_lost_lines(tmp_path / "missing.log") == ()


class _Scan:
    def __init__(self, readable: bool, lines: tuple[str, ...]) -> None:
        self.readable = readable
        self.lines = lines


@pytest.fixture
def gpufault(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"scan": _Scan(True, ()), "calls": []}

    async def scan_xid(**kw: Any) -> _Scan:
        state["calls"].append(kw)
        return state["scan"]  # type: ignore[no-any-return]

    mod = pytypes.ModuleType(health.GPUFAULT_MODULE)
    mod.scan_xid = scan_xid  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, health.GPUFAULT_MODULE, mod)
    monkeypatch.setattr(health, "JOURNAL_SETTLE_S", 0.0)
    yield state


async def test_gpu_fault_lines(gpufault: dict[str, Any]) -> None:
    assert await health.gpu_fault_lines(since_monotonic=12.0) == ()
    assert gpufault["calls"][0]["since_monotonic"] == 12.0
    gpufault["scan"] = _Scan(True, ("NVRM: Xid (PCI:0000:01:00): 13, pid=9, name=vo",))
    assert await health.gpu_fault_lines(since_monotonic=12.0) == (
        "NVRM: Xid (PCI:0000:01:00): 13, pid=9, name=vo",
    )
    gpufault["scan"] = _Scan(False, ())  # unreadable journal is unknown, never "no faults"
    assert await health.gpu_fault_lines(since_monotonic=12.0) is None


async def test_gpu_fault_lines_without_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "GPUFAULT_MODULE", "buttereye.core.doctor.no_such_module")
    assert await health.gpu_fault_lines(since_monotonic=0.0) is None


# ---------------------------------------------------------------------------
# Decisions (§4.11, §5.6)
# ---------------------------------------------------------------------------

NTSC = Fraction(24000, 1001)


@pytest.mark.parametrize(
    ("src", "hz", "expected"),
    [
        (NTSC, 119.88, Fraction(60000, 1001)),  # the lowest refresh/k that doubles
        (NTSC, 180.0, Fraction(60)),  # refresh/3 (2.5x), not refresh/2 (3.75x)
        (Fraction(24), 144.0, Fraction(48)),  # 144/3, an exact 2x
        (NTSC, 60.0, Fraction(60)),  # 2.5x
        (Fraction(50), 60.0, Fraction(60)),  # no refresh/k doubles: the highest one
    ],
)
def test_display_target(src: Fraction, hz: float, expected: Fraction) -> None:
    tc = decide.choose_target(src, hz, TargetKind.DISPLAY)
    assert tc.bypass is None and tc.target == expected


@pytest.mark.parametrize(
    ("src", "hz", "expected"),
    [
        (NTSC, 119.88, Fraction(120000, 1001)),  # 5x on 120 Hz
        (NTSC, 180.0, Fraction(90)),  # refresh/2 (3.75x) rather than 7.5x
        (Fraction(24), 144.0, Fraction(72)),  # 6x too high → 144/2
        (NTSC, 60.0, Fraction(60)),  # 2.5x
        (Fraction(50), 60.0, Fraction(60)),
    ],
)
def test_display_max_target(src: Fraction, hz: float, expected: Fraction) -> None:
    tc = decide.choose_target(src, hz, TargetKind.DISPLAY_MAX)
    assert tc.bypass is None and tc.target == expected


def test_targets_bypass_and_caps() -> None:
    assert decide.choose_target(Fraction(60), 60.0, TargetKind.DISPLAY).bypass is (
        BypassReason.ALREADY_AT_RATE
    )
    assert decide.choose_target(Fraction(60), 59.94, TargetKind.DISPLAY).bypass is (
        BypassReason.ALREADY_AT_RATE
    )
    pending = decide.choose_target(NTSC, None, TargetKind.DISPLAY)
    assert pending.target is None and pending.bypass is None
    assert decide.choose_target(NTSC, 60.0, TargetKind.X2).target == NTSC * 2
    assert decide.choose_target(NTSC, None, TargetKind.FPS, Fraction(60)).target == 60
    assert decide.choose_target(Fraction(60), None, TargetKind.FPS, Fraction(30)).bypass is (
        BypassReason.ALREADY_AT_RATE
    )
    # benchmark cap (a 2x rate): 23.976 → 60 infers almost every frame (~120 at
    # 2x), so 72 sustainable keeps plain 2x
    capped = decide.choose_target(NTSC, 120.0, TargetKind.DISPLAY, sustainable_fps=72.0)
    assert capped.target == NTSC * 2
    roomy = decide.choose_target(NTSC, 120.0, TargetKind.DISPLAY_MAX, sustainable_fps=125.0)
    assert roomy.target == Fraction(60)  # 120 needs 2x 96 inferences/s; 60 fits
    # 24 → 30/40 costs as many inferences as 2x: a lower refresh/k does not help
    low = decide.choose_target(NTSC, 120.0, TargetKind.DISPLAY, sustainable_fps=30.0)
    assert low.bypass is BypassReason.NO_REALTIME
    none = decide.choose_target(NTSC, 120.0, TargetKind.DISPLAY, sustainable_fps=20.0)
    assert none.bypass is BypassReason.NO_REALTIME
    assert decide.choose_target(NTSC, None, TargetKind.X2, sustainable_fps=40.0).bypass is (
        BypassReason.NO_REALTIME
    )


def test_fps_fraction() -> None:
    assert decide.fps_fraction(23.976) == NTSC
    assert decide.fps_fraction(23.976023976) == NTSC
    assert decide.fps_fraction(29.97) == Fraction(30000, 1001)
    assert decide.fps_fraction(59.94) == Fraction(60000, 1001)
    assert decide.fps_fraction(24.000000384) == 24
    assert decide.fps_fraction(25.0) == 25
    assert decide.fps_fraction(12.5) == Fraction(25, 2)
    assert decide.fps_fraction(0) is None and decide.fps_fraction(None) is None


def test_matrix_and_formats() -> None:
    assert decide.matrix_name("bt.709", 360) == "709"
    assert decide.matrix_name("bt.601", 1080) == "170m"
    assert decide.matrix_name("bt.2020-ncl", 2160) == "2020ncl"
    assert decide.matrix_name(None, 1080) == "709"  # by height, never fixed
    assert decide.matrix_name("weird", 480) == "170m"
    vp = {"pixelformat": "yuv420p", "w": 1920, "h": 1080}
    assert decide.format_supported(vp)
    assert decide.format_supported({**vp, "pixelformat": "nv12"})
    assert decide.format_supported({**vp, "pixelformat": "p010"})
    assert not decide.format_supported({**vp, "pixelformat": "rgb24"})
    assert not decide.format_supported({**vp, "pixelformat": "gray"})
    assert not decide.format_supported({**vp, "pixelformat": "yuv444p", "w": 1919})
    assert decide.hdr_class({"gamma": "pq"}) is HdrClass.HDR10
    assert decide.hdr_class({"gamma": "hlg"}) is HdrClass.HLG
    assert decide.hdr_class({"gamma": "bt.1886"}) is HdrClass.SDR


def test_bypass_rows() -> None:
    p = live.FALLBACK_PROFILE
    facts = decide.source_facts(
        {"video-params": {"w": 1920, "h": 1080, "pixelformat": "yuv420p", "gamma": "pq"},
         "container-fps": 23.976, "display-fps": 60.0},
        path="/x.mkv",
    )  # fmt: skip
    assert facts is not None and facts.fps == NTSC and facts.hdr_class is HdrClass.HDR10
    vp = {"w": 1920, "h": 1080, "pixelformat": "yuv420p", "gamma": "pq"}
    assert decide.bypass_reason(facts, vp, p) is BypassReason.HDR_SKIP  # F7 default
    opt_in = dataclasses.replace(p, hdr="passthrough")  # F7 opt-in, M0(d) passed for PQ
    assert decide.bypass_reason(facts, vp, opt_in) is None
    hlg = dataclasses.replace(facts, hdr_class=HdrClass.HLG)
    assert decide.bypass_reason(hlg, {**vp, "gamma": "hlg"}, opt_in) is BypassReason.HDR_SKIP
    passthrough = Target(TargetKind.DISPLAY)
    assert passthrough.kind is TargetKind.DISPLAY
    assert decide.no_video({"current-tracks/video": {"image": True}})
    assert decide.no_video({"current-tracks/video": {"albumart": True}})
    assert not decide.no_video({"current-tracks/video": None})  # judged after a grace period


def test_backend_resolution(tmp_path: Path) -> None:
    plugins, models = tmp_path / "plugins", tmp_path / "models"
    plugins.mkdir()
    assert decide.installed_backends(plugins, models) == frozenset()
    (plugins / "mvtools.so").write_bytes(b"")
    assert decide.installed_backends(plugins, models) == {BackendId.MVTOOLS}
    (plugins / "librife.so").write_bytes(b"")
    models.mkdir()
    both = decide.installed_backends(plugins, models)
    assert both == {BackendId.MVTOOLS, BackendId.RIFE_NCNN}
    assert decide.resolve_backend(None, both).backend is BackendId.RIFE_NCNN
    trt = decide.resolve_backend(BackendId.RIFE_TRT, both)
    assert trt.backend is BackendId.RIFE_NCNN and trt.notice is not None
    only_mv = decide.resolve_backend(BackendId.RIFE_NCNN, frozenset({BackendId.MVTOOLS}))
    assert only_mv.backend is BackendId.MVTOOLS and only_mv.notice is not None
    nothing = decide.resolve_backend(None, frozenset())
    assert nothing.backend is None and nothing.notice is not None
    # models by directory name only
    assert decide.resolve_model("rife-v4.26_ensembleFalse", models)[0] is None
    (models / "rife-v4.22_lite_ensembleFalse").mkdir()
    name, note = decide.resolve_model("rife-v4.26_ensembleFalse", models)
    assert name == "rife-v4.22_lite_ensembleFalse" and note is not None
    assert decide.resolve_model("../etc", models)[0] == "rife-v4.22_lite_ensembleFalse"
    assert decide.uhd_mode(3840, 2160) and not decide.uhd_mode(2560, 1440)


def test_step_down_chain() -> None:
    profiles = sc.builtin_profiles()
    assert decide.step_down_target("quality", profiles) == "balanced"
    assert decide.step_down_target("balanced", profiles) == "fast"
    assert decide.step_down_target("fast", profiles) == "cpu"
    assert decide.step_down_target("cpu", profiles) is None
    assert decide.step_down_target("custom", profiles) == "cpu"
    # a custom GPU profile above 2x first sheds load at 2x, then MVTools
    assert decide.step_down_target("custom", profiles, multiplier=Fraction(5, 2)) == "fast"
    assert decide.step_down_target("custom", profiles, multiplier=Fraction(2)) == "cpu"
    assert decide.step_down_target("cpu", profiles, multiplier=Fraction(5, 2)) is None
    # the lite model no faster here: "quality" goes straight to 2x
    assert decide.step_down_target("quality", profiles, skip_lite=True) == "fast"
    assert decide.step_down_target("balanced", profiles, skip_lite=True) == "fast"
    # already at ~2x: "fast" (lite at 2x) would shed nothing, so go to MVTools
    two = Fraction(2)
    assert decide.step_down_target("quality", profiles, multiplier=two, skip_lite=True) == "cpu"
    assert decide.step_down_target("balanced", profiles, multiplier=two) == "cpu"
    assert decide.step_down_target("quality", profiles, multiplier=two) == "balanced"
    assert decide.step_down_target("balanced", profiles, multiplier=Fraction(5, 2)) == "fast"
    assert (
        decide.step_down_target("quality", profiles, multiplier=Fraction(5, 2), skip_lite=True)
        == "fast"
    )


def test_interpolation_work() -> None:
    assert decide.interp_rate(Fraction(24), Fraction(48)) == 24
    assert decide.interp_rate(Fraction(24), Fraction(60)) == 48  # 4 of 5 inferred
    assert decide.interp_rate(Fraction(24), Fraction(90)) == 84  # 14 of 15
    assert decide.interp_rate(NTSC, Fraction(60)) == Fraction(60) * Fraction(1000, 1001)
    assert decide.interp_rate(NTSC, NTSC) == 0
    assert decide.load_rate(NTSC, NTSC * 2) == NTSC * 2  # 2x costs its own rate
    assert decide.load_rate(Fraction(24), Fraction(60)) == 96
    # a bench measured at 2x stays; one measured at 2.5x is converted
    assert decide.as_2x_rate(61.2, NTSC, None) == pytest.approx(61.2)
    assert decide.as_2x_rate(48.0, Fraction(24), Fraction(60)) == pytest.approx(76.8)
    assert decide.as_2x_rate(61.2, None, None) == 61.2


def test_dev_box_cap_rejects_ntsc_to_60_at_1920x800() -> None:
    # bench.json on the dev box: v4.26 61.2 fps in mpv at 1080p (2x); a 1920x800
    # video has 1.35x fewer pixels. 23.976 → 60 dropped 864 of ~1500 frames live.
    cap = 61.2 * (1920 * 1080) / (1920 * 800) / 1.25
    fixed = decide.choose_target(NTSC, 180.0, TargetKind.FPS, Fraction(60), sustainable_fps=cap)
    assert fixed.bypass is BypassReason.NO_REALTIME
    shown = decide.choose_target(NTSC, 180.0, TargetKind.DISPLAY, sustainable_fps=cap)
    assert shown.target == NTSC * 2  # 2x played with 0 drops at ~33 % GPU
    assert decide.choose_target(Fraction(24), None, TargetKind.FPS, Fraction(48),
                                sustainable_fps=cap).target == 48  # fmt: skip


# ---------------------------------------------------------------------------
# Provider edges (no real mpv)
# ---------------------------------------------------------------------------


@pytest.fixture
async def core(xdg_env: Any) -> AsyncIterator[ButterEye]:
    c = await ButterEye.open(sc.fake_paths(env=os.environ))
    try:
        yield c
    finally:
        await c.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=1.0)


async def test_live_features_are_implemented(core: ButterEye) -> None:
    caps = await core.capabilities()
    for f in (Feature.LIVE, Feature.DISCOVER):
        assert caps.ok(f), caps.states[f]
    assert not caps.ok(Feature.ATTACH) and not caps.ok(Feature.ORPHANS)  # reserved, not U10


async def test_unknown_session_is_ipc_lost(core: ButterEye) -> None:
    for call in (
        core.set_interpolation(SessionId("nope"), True),
        core.apply_profile(SessionId("nope"), None),
        core.step_down(SessionId("nope")),
        core.detach(SessionId("nope")),
    ):
        with pytest.raises(ButterEyeError) as info:
            await call
        assert info.value.code is ErrorCode.IPC_LOST
    assert await core.sessions() == ()
    assert await core.discover() == ()


def _fake_mpv(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-mpv"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    stat = Path(f"/proc/{pid}/stat").read_text()
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


async def test_mpv_without_socket_is_killed(core: ButterEye, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    reg = live.registry(core._ctx)
    reg.options.mpv = os.fspath(_fake_mpv(tmp_path, f'echo $$ > "{pidfile}"\nexec sleep 30\n'))
    reg.options.connect_timeout_s = 0.5
    clip = tmp_path / "x.mkv"
    clip.write_bytes(b"")
    with pytest.raises(ButterEyeError) as info:
        await core.play(clip).result()
    assert info.value.code is ErrorCode.IPC_LOST
    pid = int(pidfile.read_text())
    for _ in range(100):
        if _gone(pid):
            break
        await asyncio.sleep(0.02)
    assert _gone(pid)
    assert await core.sessions() == ()


async def test_cancelled_play_kills_mpv(core: ButterEye, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    reg = live.registry(core._ctx)
    reg.options.mpv = os.fspath(_fake_mpv(tmp_path, f'echo $$ > "{pidfile}"\nexec sleep 30\n'))
    clip = tmp_path / "x.mkv"
    clip.write_bytes(b"")
    op = core.play(clip)
    for _ in range(200):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.01)
    op.cancel()
    await op.wait()
    pid = int(pidfile.read_text())
    for _ in range(150):
        if _gone(pid):
            break
        await asyncio.sleep(0.02)
    assert _gone(pid)


async def test_missing_mpv_binary(core: ButterEye, tmp_path: Path) -> None:
    live.registry(core._ctx).options.mpv = os.fspath(tmp_path / "no-such-mpv")
    clip = tmp_path / "x.mkv"
    clip.write_bytes(b"")
    with pytest.raises(ButterEyeError) as info:
        await core.play(clip).result()
    assert info.value.code is ErrorCode.MPV_NOT_FOUND
    assert info.value.commands == ("sudo dnf install mpv",)


async def test_unknown_profile_refused_before_launch(core: ButterEye, tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    live.registry(core._ctx).options.mpv = os.fspath(_fake_mpv(tmp_path, f'touch "{pidfile}"\n'))
    clip = tmp_path / "x.mkv"
    clip.write_bytes(b"")
    with pytest.raises(ButterEyeError) as info:
        await core.play(clip, profile_id="no-such-profile").result()
    assert info.value.code is ErrorCode.CONFIG_VALUE
    assert not pidfile.exists()


async def test_unsafe_runtime_dir_refused(core: ButterEye, tmp_path: Path) -> None:
    rt = core.paths().runtime_dir
    rt.mkdir(mode=0o755)
    rt.chmod(0o755)
    clip = tmp_path / "x.mkv"
    clip.write_bytes(b"")
    with pytest.raises(ButterEyeError) as info:
        await core.play(clip).result()
    assert info.value.code is ErrorCode.RUNTIME_DIR_UNSAFE
    assert (rt.stat().st_mode & 0o777) == 0o755  # never chmod-ed


def test_include_and_argv(tmp_path: Path) -> None:
    inc = tmp_path / "cfg" / "buttereye" / "mpv" / "buttereye.conf"
    assert launcher.write_include(inc) is True
    assert launcher.write_include(inc) is False
    text = inc.read_text()
    settings = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert settings[0] == "[buttereye]"
    assert not any(ln.startswith("vf") for ln in settings)  # filter only over IPC
    assert "hr-seek-framedrop=no" in text and "interpolation=no" in text
    assert "hr-seek=default" in text and "hr-seek=yes" not in text  # no pre-roll through RIFE
    argv = launcher.build_argv("mpv", include=inc, socket=tmp_path / "s.sock",
                               file=Path("-weird name.mkv"))  # fmt: skip
    assert argv[1] == f"--include={inc}" and argv[2] == "--profile=buttereye"
    assert argv[3] == f"--script={launcher.LUA_HELPER}"
    assert "--hwdec=nvdec-copy,auto-copy" in argv and "--video-sync=display-resample" in argv
    assert argv[-2:] == ["--", "-weird name.mkv"]
    assert not any(a.startswith("--vf") for a in argv)
    lua = launcher.LUA_HELPER.read_text()
    assert lua.startswith("-- SPDX-License-Identifier: AGPL-3.0-or-later\n")
    assert '"buttereye-request", "toggle"' in lua and "vf toggle @buttereye" in lua
