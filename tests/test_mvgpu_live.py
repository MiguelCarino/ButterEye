# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mvgpu (GPU block motion vectors, ``mvgpu/``) as a live engine, without a real mpv
or GPU: detection, the live decisions (Automatic ranks it after RIFE; it beats a
smaller picture when RIFE can't keep up at the video's own size, and reaches
"Match your display" where RIFE can't), the generated filter, the speed-test
candidate, the doctor finding, offline renders and profile validation.

Caps mirror the dev box (RTX 4090): RIFE-ncnn v4.26 61.2 fps at 1080p 2x in mpv;
mvgpu ~170 fps at 4K 23.976 -> 119.88 untimed in mpv (2026-10-10), stored here
as a 4K 2x measurement of 260 fps."""

from __future__ import annotations

import dataclasses
import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.bench import runner
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.events import Notice
from buttereye.core.mpvctl import decide, osd
from buttereye.core.profiles import rules
from buttereye.core.render import jobs
from buttereye.core.render import probe as rprobe
from buttereye.core.scriptgen import generator as g
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchResult,
    BypassReason,
    FilterState,
    Health,
    Profile,
    Severity,
    Target,
    TargetKind,
)
from tests.test_live_decisions import (
    NTSC,
    RIFE_CAP,
    FakeCtx,
    _changes,
    _config,
    _devbox,
    _engine,
    _load,
    _paths,
    _result,
    _session,
    _settle,
    _simple,
    _stall,
)
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture

MVGPU_4K = 260.0  # in mpv, 4K 2x
#: the plugin file as this platform names it (plain ``libmvgpu.so`` on Linux), which
#: is the name both the detection code and a planted fixture must use
MVGPU_SO = osapi.plugin_file("libmvgpu.so")


def _mvgpu_paths(tmp_path: Path, *, rife: bool = True, mvgpu: bool = True) -> Any:
    paths = _paths(tmp_path, rife=rife)
    if mvgpu:
        (paths.rpm_plugin_dir / MVGPU_SO).write_bytes(b"")
    return paths


def _mvgpu_m(mpv: float, label: str = "mvgpu") -> BenchMeasurement:
    return BenchMeasurement(
        label=label, backend=BackendId.MVGPU, model=None, vspipe_fps=mpv * 1.5, mpv_fps=mpv,
        cov=0.01, repeatable=True, startup_s=0.5, reload_s=0.4, vram_bytes=None, realtime=True,
        gpu_faults=0,
    )  # fmt: skip


def _mvgpu_4k() -> BenchResult:
    return _result(_mvgpu_m(MVGPU_4K), size=(3840, 2160))


def _filters(s: Any) -> list[dict[str, Any]]:
    """The user-data of every ``vf add`` the session sent."""
    out = []
    for c in s.ipc.commands:
        if c[:2] == ("vf", "add"):
            raw = str(c[2]).split(":user-data=", 1)[1]
            out.append(json.loads(raw[raw.index("%", 1) + 1 :] if raw.startswith("%") else raw))
    return out


def _vf(s: Any) -> str:
    return next(str(c[2]) for c in reversed(s.ipc.commands) if c[:2] == ("vf", "add"))


# ---------------------------------------------------------------------------
# detection and ranking
# ---------------------------------------------------------------------------


def test_installed_backends_finds_the_plugin_file(tmp_path: Path) -> None:
    paths = _mvgpu_paths(tmp_path, rife=False)
    model_dir = paths.rpm_data_dir / "rife-ncnn-models"
    found = decide.installed_backends(paths.rpm_plugin_dir, model_dir)
    assert found == {BackendId.MVGPU, BackendId.MVTOOLS}
    (paths.rpm_plugin_dir / MVGPU_SO).unlink()
    assert BackendId.MVGPU not in decide.installed_backends(paths.rpm_plugin_dir, model_dir)


def test_ranking_puts_mvgpu_after_rife_and_before_mvtools() -> None:
    from buttereye.core.mpvctl import session as live

    assert live.LIVE_RANKING == (
        BackendId.RIFE_TRT,
        BackendId.RIFE_NCNN,
        BackendId.MVGPU,
        BackendId.MVTOOLS,
    )
    every = frozenset(BackendId)
    assert decide.resolve_backend(None, every - {BackendId.RIFE_TRT, BackendId.RIFE_NCNN}) == (
        decide.BackendChoice(BackendId.MVGPU, None)
    )
    # never a silent TensorRT pick: a missing mvgpu falls back to RIFE-ncnn
    choice = decide.resolve_backend(BackendId.MVGPU, every - {BackendId.MVGPU})
    assert choice.backend is BackendId.RIFE_NCNN and choice.notice is not None
    assert choice.notice.params == {"engine": "GPU motion vectors", "fallback": "RIFE (Vulkan)"}
    assert BackendId.MVGPU in decide.GPU_BACKENDS
    assert BackendId.MVGPU not in decide.RIFE_BACKENDS


# ---------------------------------------------------------------------------
# pick_engine
# ---------------------------------------------------------------------------


def _opts(rife: float | None, mvgpu: float | None, mv: float | None = 80.0) -> list[Any]:
    return [
        decide.EngineOption(BackendId.RIFE_NCNN, rife),
        decide.EngineOption(BackendId.MVGPU, mvgpu),
        decide.EngineOption(BackendId.MVTOOLS, mv),
    ]


def test_auto_keeps_rife_when_it_keeps_up() -> None:
    p = decide.pick_engine(_opts(RIFE_CAP, 800.0), NTSC, 180.0, TargetKind.X2, auto=True)
    assert p.backend is BackendId.RIFE_NCNN and p.target.target == NTSC * 2


def test_auto_uses_mvgpu_when_rife_cannot_keep_up() -> None:
    # 30 -> 60 needs a 60 fps cap; RIFE has 49, mvgpu 800, MVTools 80
    p = decide.pick_engine(_opts(RIFE_CAP, 800.0), Fraction(30), 180.0, TargetKind.X2, auto=True)
    assert p.backend is BackendId.MVGPU and p.target.target == 60 and p.notice is None


def test_auto_never_trusts_an_unmeasured_mvgpu() -> None:
    p = decide.pick_engine(_opts(RIFE_CAP, None), Fraction(30), 180.0, TargetKind.X2, auto=True)
    assert p.backend is BackendId.MVTOOLS
    # the only engine installed: used uncapped (the health detector guards it)
    only = [decide.EngineOption(BackendId.MVGPU, None)]
    p = decide.pick_engine(only, Fraction(30), 180.0, TargetKind.X2, auto=True)
    assert p.backend is BackendId.MVGPU


def test_auto_mvgpu_too_slow_falls_to_mvtools() -> None:
    p = decide.pick_engine(_opts(RIFE_CAP, 50.0), Fraction(30), 180.0, TargetKind.X2, auto=True)
    assert p.backend is BackendId.MVTOOLS


def test_display_target_uses_mvgpu_for_the_display_rate() -> None:
    # 24 -> 180 Hz: RIFE reaches only 2x (48); mvgpu reaches the display rate
    p = decide.pick_engine(_opts(RIFE_CAP, 800.0), NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert p.backend is BackendId.MVGPU and p.target.target == 180
    # mvgpu reaching only 60 still beats RIFE's 48 (display rate, then 60)
    p = decide.pick_engine(_opts(RIFE_CAP, 120.0), NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert p.backend is BackendId.MVGPU and p.target.target == 60


def test_display_target_keeps_rife_when_rife_reaches_it() -> None:
    p = decide.pick_engine(_opts(400.0, 800.0), NTSC, 180.0, TargetKind.DISPLAY, auto=True)
    assert p.backend is BackendId.RIFE_NCNN and p.target.target == 180
    # 60 Hz display: RIFE reaches 60 itself
    p = decide.pick_engine(_opts(130.0, 800.0), NTSC, 60.0, TargetKind.DISPLAY, auto=True)
    assert p.backend is BackendId.RIFE_NCNN and p.target.target == 60
    # "maximize smoothness" and fixed targets keep the old rules (RIFE first)
    p = decide.pick_engine(_opts(RIFE_CAP, 800.0), NTSC, 180.0, TargetKind.DISPLAY_MAX, auto=True)
    assert p.backend is BackendId.RIFE_NCNN


def test_pinned_rife_falls_back_to_mvgpu_before_mvtools() -> None:
    p = decide.pick_engine(_opts(RIFE_CAP, 800.0), Fraction(30), 180.0, TargetKind.X2, auto=False)
    assert p.backend is BackendId.MVGPU and p.target.target == 60
    assert p.notice == decide.gpu_fallback_notice(BackendId.RIFE_NCNN, BackendId.MVGPU)


def test_pinned_mvgpu_falls_back_to_mvtools_with_the_cpu_notice() -> None:
    opts = [
        decide.EngineOption(BackendId.MVGPU, 50.0),
        decide.EngineOption(BackendId.MVTOOLS, 80.0),
    ]
    p = decide.pick_engine(opts, Fraction(30), 180.0, TargetKind.X2, auto=False)
    assert p.backend is BackendId.MVTOOLS and p.notice == decide.cpu_fallback_notice(Fraction(60))


def test_no_step_down_from_mvgpu() -> None:
    profile = dataclasses.replace(_simple(BackendId.MVGPU), id="mine")
    profiles = sc.default_config().profiles + (profile,)
    assert decide.step_down_target("mine", profiles, multiplier=Fraction(5)) is None


def test_no_step_down_while_mvgpu_runs_for_another_profile() -> None:
    """The offer follows the engine running, not the profile's pinned one: every
    step would fall back to the same mvgpu run ("balanced") or add load ("cpu")."""
    auto = dataclasses.replace(_simple(), id="mine")
    profiles = sc.default_config().profiles + (auto,)
    run = BackendId.MVGPU
    assert decide.step_down_target("quality", profiles, running=run) is None
    assert decide.step_down_target("mine", profiles, multiplier=Fraction(2), running=run) is None
    assert decide.step_down_target("mine", profiles, multiplier=Fraction(5), running=run) is None
    # the same profiles running RIFE keep their chain
    assert decide.step_down_target("quality", profiles, running=BackendId.RIFE_NCNN) == "balanced"
    assert decide.step_down_target("mine", profiles, multiplier=Fraction(2)) == "cpu"


# ---------------------------------------------------------------------------
# the session (filter really generated, no mpv)
# ---------------------------------------------------------------------------


async def test_session_4k_uses_mvgpu_at_full_size_before_a_smaller_picture(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.MVGPU and snap.target_fps == 48
    assert snap.smooth_size is None and snap.flow_scale == 1.0 and snap.notice is None
    (ud,) = _filters(s)
    assert ud["backend"] == "mvgpu" and "size" not in ud
    assert ud["gpu_id"] == 1  # the RTX 4090's Vulkan index
    assert ud["sc_threshold"] == 0.12  # scene cuts are marked for it
    assert "model_path" not in ud and "gpu_thread" not in ud
    assert ":concurrent-frames=8:" in _vf(s)


async def test_session_4k_without_mvgpu_smooths_a_smaller_picture(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path, mvgpu=False), _config(_simple())), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN and snap.smooth_size == (1280, 720)


async def test_session_unmeasured_mvgpu_is_not_trusted(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN and snap.smooth_size == (1280, 720)


async def test_session_1080p_keeps_rife(tmp_path: Path, bench_history: list[BenchResult]) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0)
    await s.apply(raise_on_fail=False)
    assert s.snapshot().backend is BackendId.RIFE_NCNN
    assert [f["backend"] for f in _filters(s)] == ["rife-ncnn"]


async def test_session_display_target_reaches_the_display_with_mvgpu(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    profile = _simple(target=Target(TargetKind.DISPLAY))
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(profile)), tmp_path)
    _load(s, fps=24.0, hz=120.0)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    # RIFE reaches only 48 at 1080p; mvgpu (4K cap scaled to 1080p) reaches 120
    assert snap.backend is BackendId.MVGPU and snap.target_fps == 120


async def test_session_quality_profile_on_mvgpu_offers_no_step_down(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), sc.default_config()), tmp_path)
    s.profile_request = "quality"  # pinned RIFE-ncnn, "Match your display"
    _load(s, fps=24.0, w=3840, h=2160, hz=60.0)
    await s.apply(raise_on_fail=False)
    assert s.profile_id == "quality" and _engine(s) is BackendId.MVGPU
    assert await s.step_down_choice() is None


async def test_session_auto_profile_on_mvgpu_offers_no_step_down(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVGPU
    assert await s.step_down_choice() is None  # not "cpu": MVTools at 4K is far heavier


async def test_session_mvgpu_stall_switches_to_cpu_once(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    ctx = FakeCtx(_mvgpu_paths(tmp_path), _config(_simple(BackendId.MVGPU)))
    s = _session(ctx, tmp_path)
    _load(s, fps=24.0)
    await s.apply(raise_on_fail=False)
    assert _engine(s) is BackendId.MVGPU and s.gpu_used is BackendId.MVGPU
    await s.stalled(_stall())
    await _settle(ctx)
    snap = s.snapshot()
    assert snap.backend is BackendId.MVTOOLS and s.active_gen is not None
    assert snap.health is Health.OK and snap.notice == decide.switched_to_cpu_notice()
    assert [c.health for c in _changes(ctx)] == [Health.STALLED, Health.OK]
    # the mvgpu run's failure is followed by the Xid scan
    assert any(t.get_name().endswith("-xid") for t in ctx.tasks)
    # MVTools stalls as well → off, never back to the GPU
    await s.stalled(_stall())
    await _settle(ctx)
    assert s.snapshot().filter is FilterState.ROLLED_BACK and not s.want


async def test_gpu_fault_during_mvgpu_names_mvgpu_and_switches_to_cpu(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: dict[str, Any]
) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    ctx = FakeCtx(_mvgpu_paths(tmp_path), _config(_simple(BackendId.MVGPU)))
    s = _session(ctx, tmp_path)
    _load(s, fps=24.0)
    await s.apply(raise_on_fail=False)
    xid = "NVRM: Xid (PCI:0000:01:00): 109, pid=9, name=vo, CTX SWITCH TIMEOUT"
    no_xid["lines"] = (xid,)
    await s.check_gpu_faults()
    assert _engine(s) is BackendId.MVTOOLS and s.active_gen is not None
    first = _changes(ctx)[0]
    assert first.health is Health.GPU_FAULT
    assert first.reason.params == {"engine": "GPU motion vectors"}
    # found again later: blamed on the earlier mvgpu run, not on MVTools
    no_xid["lines"] = (xid, xid)
    await s.check_gpu_faults()
    note = [e for e in ctx.events if isinstance(e, Notice)][-1]
    assert "earlier" in note.message.key
    assert note.message.params == {"earlier": "GPU motion vectors", "engine": "MVTools (CPU)"}


async def test_session_pinned_mvgpu(tmp_path: Path, bench_history: list[BenchResult]) -> None:
    bench_history.extend([_devbox(), _mvgpu_4k()])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple(BackendId.MVGPU))), tmp_path)
    _load(s, fps=24.0, w=3840, h=2160)
    await s.apply(raise_on_fail=False)
    assert s.snapshot().backend is BackendId.MVGPU and s.notice is None


async def test_session_pinned_mvgpu_missing_falls_back_with_a_notice(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    bench_history.append(_devbox())
    paths = _mvgpu_paths(tmp_path, mvgpu=False)
    s = _session(FakeCtx(paths, _config(_simple(BackendId.MVGPU))), tmp_path)
    _load(s, fps=24.0)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.backend is BackendId.RIFE_NCNN
    assert snap.notice is not None and snap.notice.params["engine"] == "GPU motion vectors"


async def test_session_mvgpu_too_slow_bypasses(
    tmp_path: Path, bench_history: list[BenchResult]
) -> None:
    # 240 -> 480 at 720p on a 480 Hz display: nothing keeps up, mvgpu included
    bench_history.extend([_devbox(), _result(_mvgpu_m(100.0), size=(1280, 720))])
    s = _session(FakeCtx(_mvgpu_paths(tmp_path), _config(_simple())), tmp_path)
    _load(s, fps=240.0, w=1280, h=720, hz=480.0)
    await s.apply(raise_on_fail=False)
    snap = s.snapshot()
    assert snap.filter is FilterState.BYPASSED and snap.bypass is BypassReason.NO_REALTIME


# ---------------------------------------------------------------------------
# generated script, OSD, speed test, doctor, renders, validation
# ---------------------------------------------------------------------------


def test_user_data_and_frames_for_mvgpu() -> None:
    params = g.FilterParams(
        gen=3, backend=BackendId.MVGPU, src_fps=NTSC, target_fps=NTSC * 5, buffered_frames=4,
        concurrent_frames=8, gpu_id=1, sc_threshold=0.12, model_path=Path("/nope"),
    )  # fmt: skip
    data = json.loads(g.user_data(params))
    assert data["backend"] == "mvgpu" and data["gpu_id"] == 1 and data["sc_threshold"] == 0.12
    assert "model_path" not in data and "uhd" not in data and "trt" not in data
    assert g.default_frames(BackendId.MVGPU) == (4, g.MVGPU_CONCURRENT_FRAMES_DEFAULT) == (4, 8)


def test_template_has_the_mvgpu_backend() -> None:
    text = g.render_script()
    assert "def _mvgpu(clip, cfg, target):" in text
    # the Linux name is what the script says; ``_load`` turns it into the platform's
    # own (.dll, .dylib) at load time, so the template text itself never changes
    assert "_load('mvgpu', 'libmvgpu.so')" in text  # by path from the private directory
    assert "elif backend == 'mvgpu':" in text


def test_template_mvgpu_marks_scene_cuts_and_passes_the_device() -> None:
    vs = pytest.importorskip("vapoursynth")
    text = g.render_script()
    ns: dict[str, Any] = {}
    exec(compile(text.replace("build().set_output()", ""), "gen.vpy", "exec"), ns)
    seen: dict[str, Any] = {}

    class _Mvgpu:
        @staticmethod
        def Interpolate(clip: Any, **kw: Any) -> Any:  # noqa: N802 (plugin API)
            seen.update(kw, clip=clip)
            return clip

    class _Core:
        def __getattr__(self, name: str) -> Any:
            return _Mvgpu if name == "mvgpu" else getattr(vs.core, name)

    ns["core"] = _Core()
    ns["_load"] = lambda namespace, filename: seen.setdefault("loaded", (namespace, filename))
    src = vs.core.std.BlankClip(format=vs.YUV422P10, width=64, height=32, length=4)
    ns["_mvgpu"](src, {"sc_threshold": 0.12, "gpu_id": 1}, Fraction(48))
    assert seen["loaded"] == ("mvgpu", "libmvgpu.so")  # as passed; the stub _load renames nothing
    assert (seen["fps_num"], seen["fps_den"], seen["device"]) == (48, 1, 1)
    clip = seen["clip"]
    assert clip.format.subsampling_h == 1 and clip.format.bits_per_sample == 10  # 4:2:0
    assert clip.get_frame(0).props["_SceneChangeNext"] in (0, 1)
    seen.clear()
    ns["_mvgpu"](src, {"sc_threshold": 0}, Fraction(48))
    assert "device" not in seen
    assert "_SceneChangeNext" not in seen["clip"].get_frame(0).props


def test_osd_names_gpu_motion_vectors() -> None:
    snap = dataclasses.replace(sc.session(1), backend=BackendId.MVGPU, model=None)
    assert osd.parts(snap, ("engine",)) == ["GPU · motion vectors"]


def test_bench_candidate_when_the_plugin_is_installed(tmp_path: Path) -> None:
    paths = _mvgpu_paths(tmp_path)
    model_dir = paths.rpm_data_dir / "rife-ncnn-models"
    for name in decide.DEFAULT_MODELS:
        (model_dir / name / "flownet.param").write_text("")
    cands = runner.discover_candidates(paths.rpm_plugin_dir, model_dir, concurrency=24)
    backends = [c.backend for c in cands]
    assert backends == [BackendId.RIFE_NCNN] * 3 + [BackendId.MVGPU, BackendId.MVTOOLS]
    mvgpu = cands[3]
    assert (mvgpu.label, mvgpu.model, mvgpu.concurrent_frames) == ("mvgpu", None, 8)
    # no usable GPU: no mvgpu candidate
    cpu = runner.discover_candidates(paths.rpm_plugin_dir, model_dir, rife=False)
    assert [c.backend for c in cpu] == [BackendId.MVTOOLS]
    tools = runner.Tools("vspipe", "mpv", runner.VPY, paths.rpm_plugin_dir, model_dir)
    data = runner.user_data(
        mvgpu, tools, width=3840, height=2160, source=NTSC, target=NTSC * 2, src_frames=50,
        gpu_id=1,
    )  # fmt: skip
    assert data["backend"] == "mvgpu" and data["gpu_id"] == 1
    assert data["plugin_dir"] == str(paths.rpm_plugin_dir)
    assert "elif backend == 'mvgpu':" in runner.VPY.read_text()


def test_bench_reports_and_saves_mvgpu_like_the_others() -> None:
    req = _mvgpu_4k().request
    ms = (_mvgpu_m(MVGPU_4K), dataclasses.replace(_mvgpu_m(30.0, "mvtools"),
                                                  backend=BackendId.MVTOOLS))  # fmt: skip
    assert runner.recommend(ms) == "mvgpu"
    result = BenchResult(req, ms, "mvgpu", _mvgpu_4k().when, None)
    cfg = runner.bench_config(sc.default_config(), result, "mvgpu")
    bench = next(p for p in cfg.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert bench.backend is BackendId.MVGPU and bench.model is None
    assert bench.concurrent_frames == 8


def test_doctor_reports_the_plugin(tmp_path: Path) -> None:
    paths = _mvgpu_paths(tmp_path)
    db = pk.RpmDb(True, {})
    f = pk.mvgpu_finding(db, paths)
    assert (f.id, f.severity) == ("packages.mvgpu", Severity.OK)
    assert f.evidence == (str(paths.rpm_plugin_dir / MVGPU_SO),)
    assert f in pk.package_findings(db, paths)
    (paths.rpm_plugin_dir / MVGPU_SO).unlink()
    f = pk.mvgpu_finding(db, paths)
    assert f.severity is Severity.INFO and f.code is None
    if osapi.IS_MACOS:
        # Homebrew has no mvgpu formula and ButterEye builds none for macOS yet,
        # so the row offers nothing to type rather than a line that cannot work.
        assert f.commands == ()
    else:
        assert f.commands == ("sudo dnf install buttereye-vs-mvgpu",)
    assert f in pk.package_findings(pk.RpmDb(False, {}, "no rpm"), paths)
    assert pk.PKG_MVGPU in pk.BUTTEREYE_PKGS


def test_render_keeps_rife_and_allows_a_pinned_mvgpu() -> None:
    every = frozenset(BackendId) - {BackendId.RIFE_TRT}
    assert rprobe.engine_for("auto", every) is BackendId.RIFE_NCNN
    # P1: Automatic offline ranks mvgpu above MVTools (a GPU engine at the source
    # size beats a CPU one); plan_sizes then takes the first that finishes in time
    mv = frozenset({BackendId.MVGPU, BackendId.MVTOOLS})
    assert rprobe.engine_for("auto", mv) is BackendId.MVGPU
    assert rprobe.offline_order("auto", mv) == (BackendId.MVGPU, BackendId.MVTOOLS)
    # 2D animation keeps live play's order: MVTools before mvgpu
    assert rprobe.offline_order("auto", mv, animation=True)[0] is BackendId.MVTOOLS
    assert rprobe.engine_for(BackendId.MVGPU, every) is BackendId.MVGPU
    assert rprobe.engine_for(BackendId.MVGPU, every - {BackendId.MVGPU}) is BackendId.RIFE_NCNN
    assert jobs.vspipe_requests(BackendId.MVGPU) == 8


def test_validation_rejects_a_model_or_scale_for_mvgpu() -> None:
    base = sc.default_config()
    bad: Profile = dataclasses.replace(
        _simple(BackendId.MVGPU), id="mv-gpu", model="rife-v4.26_ensembleFalse", scale=0.5
    )
    issues = rules.validate(dataclasses.replace(base, profiles=base.profiles + (bad,)))
    fields = {i.field for i in issues}
    n = len(base.profiles)
    assert {f"profiles[{n}].model", f"profiles[{n}].scale"} <= fields
    ok = dataclasses.replace(bad, model=None, scale=None)
    assert not rules.validate(dataclasses.replace(base, profiles=base.profiles + (ok,)))


# ---------------------------------------------------------------------------
# packaging: the plugin, its meson project and its RPM agree
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def test_mvgpu_versions_and_packaging_stay_in_step() -> None:
    meson = (ROOT / "mvgpu/meson.build").read_text()
    spec = (ROOT / "packaging/plugins/buttereye-vs-mvgpu.spec").read_text()
    build = (ROOT / "packaging/build.sh").read_text()
    cpp = (ROOT / "mvgpu/src/mvgpu.cpp").read_text()
    mv = re.search(r"project\('mvgpu'.*version: '([^']+)'", meson)
    sv = re.search(r"^Version:\s+(\S+)", spec, re.M)
    assert mv and sv and mv.group(1) == sv.group(1)
    # the plugin reports the meson version, never a hard-coded one
    assert "VS_MAKE_VERSION(MVGPU_VERSION_MAJOR, MVGPU_VERSION_MINOR)" in cpp
    assert "-DMVGPU_VERSION_MAJOR=" in meson and "-DMVGPU_VERSION_MINOR=" in meson
    # build.sh refuses a mismatch and packs only files git knows about
    assert "mvgpu version mismatch" in build and "ls-files" in build
    # libvulkan.so.1 is an automatic dependency (rpm's generator), not a Requires
    assert not re.search(r"^Requires:.*vulkan-loader", spec, re.M)
