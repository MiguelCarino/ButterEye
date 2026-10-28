# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U11 benchmark runner without a GPU: parsers, statistics, candidates, history
store, apply, and the whole provider against fake ``vspipe``/``mpv`` executables
(real subprocesses in their own process groups, so cancellation is real too)."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import json
import math
import os
import stat
import subprocess
import sys
import time
import types as pytypes
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.api import ButterEye
from buttereye.core.bench import faults, measure, runner, store, verdict
from buttereye.core.doctor import gpufault
from buttereye.core.errors import ButterEyeError, ConfigConflict, ErrorCode, OperationCancelled
from buttereye.core.events import Event, Notice, Progress
from buttereye.core.mpvctl.decide import fps_fraction
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    Config,
    ConfigLoad,
    ContentKind,
    Feature,
    HdrClass,
    Msg,
    OpState,
    Paths,
    SourceFacts,
    TargetKind,
)

BENCH_DIR = Path(runner.__file__).parent
REQ = BenchRequest(1920, 1080, Fraction(24000, 1001))


# =============================================================== pure parts
VSPIPE_OK = (
    "[0 NVIDIA GeForce RTX 4090]  queueC=2[8]\n"
    "Script evaluation done in 0.97 seconds\r"
    "Frame: 1/192\rFrame: 192/192\n"
    "Output 192 frames in 2.71 seconds (70.94 fps)\n"
)


def test_parse_vspipe() -> None:
    r = measure.parse_vspipe(VSPIPE_OK, wall_s=4.1)
    assert r.frames == 192
    assert r.fps == pytest.approx(192 / 2.71)
    assert r.eval_s == pytest.approx(0.97)
    assert r.wall_s == 4.1
    with pytest.raises(measure.MeasureFailed):
        measure.parse_vspipe("Script evaluation failed:\nPython exception: boom\n")


def _mpv_log(fmt: str = "yuv420p", first: float = 1.0, eof: float = 4.82, extra: str = "") -> str:
    return (
        "[cplayer] Command line options: ...\n"
        "[  0.024966] [vapoursynth] using 4 concurrent requests.\n"
        f"{extra}"
        f"[  0.900000] VO: [null] 1920x1080 {fmt}\n"
        f"[  {first:.6f}] [cplayer] first video frame after restart shown\n"
        "[  1.000100] V: 00:00:00 / 00:00:00 (15%)\r"
        f"[  {eof:.6f}] [cplayer] video EOF reached\n"
        "[  5.000000] Exiting... (End of file)\n"
    )


def test_parse_mpv() -> None:
    r = measure.parse_mpv(_mpv_log(), 192)
    assert r.fps == pytest.approx(191 / 3.82)
    assert r.startup_s == pytest.approx(1.0)


def test_parse_mpv_leaves_the_pipeline_fill_out() -> None:
    """B26: with 8 frames in flight, the 7 behind the first shown one were computed
    before the clock started; counting them made the rate ~4 % too high at 1080p."""
    r = measure.parse_mpv(_mpv_log(), 192, prefilled=8)
    assert r.fps == pytest.approx(184 / 3.82)
    assert measure.parse_mpv(_mpv_log(), 192, prefilled=0).fps == pytest.approx(191 / 3.82)
    with pytest.raises(measure.MeasureFailed):
        measure.parse_mpv(_mpv_log(), 8, prefilled=8)


@pytest.mark.parametrize(
    "log",
    [
        _mpv_log(extra="[  0.066] Disabling filter vapoursynth.00 because it has failed.\n"),
        _mpv_log(extra="[  0.064] [vapoursynth] Script evaluation failed:\n"),
        _mpv_log(fmt="nv12"),  # filter not in the chain: unfiltered numbers are never used
        _mpv_log(eof=1.0),
        "[  0.1] [cplayer] nothing useful\n",
    ],
)
def test_parse_mpv_refuses_untrustworthy_runs(log: str) -> None:
    with pytest.raises(measure.MeasureFailed):
        measure.parse_mpv(log, 192)


def test_cov() -> None:
    assert measure.cov([70.0, 70.0, 70.0]) == 0.0
    assert measure.cov([60.0, 70.0, 80.0]) == pytest.approx(10.0 / 70.0)
    assert math.isinf(measure.cov([70.0]))
    assert math.isinf(measure.cov([0.0, 0.0]))


def test_mpv_quote_and_argv() -> None:
    assert measure.mpv_quote("a:b,c") == "%5%a:b,c"
    assert measure.mpv_quote("é") == "%2%é"  # bytes, not characters
    vpy = Path("/tmp/odd:dir,x/bench.vpy")
    argv = measure.mpv_argv(
        "mpv",
        vpy,
        {"k": "v:1,2"},
        width=1280,
        height=720,
        source_fps=Fraction(24000, 1001),
        frames=192,
        concurrent_frames=4,
    )
    vf = next(a for a in argv if a.startswith("--vf="))
    assert f"file={measure.mpv_quote(str(vpy))}:" in vf
    assert 'user-data=%13%{"k":"v:1,2"}' in vf
    assert "concurrent-frames=4" in vf and "buffered-frames=4" in vf
    assert argv[-1] == "av://lavfi:testsrc2=size=1280x720:rate=24000/1001,format=nv12"
    assert "--no-config" in argv and "--untimed" in argv and "--vo=null" in argv
    assert "--ao=null" in argv and "--frames=192" in argv
    vs = measure.vspipe_argv("vspipe", vpy, {"a": 1}, 192)
    assert vs == ["vspipe", "-p", "-a", 'user_data={"a":1}', "-e", "191", str(vpy), "--"]


def test_parse_compute_apps() -> None:
    text = "3403, 51\n225518, 3849\nbad line\n"
    assert measure.parse_compute_apps(text, 225518) == 3849 * 1024 * 1024
    assert measure.parse_compute_apps(text, 1) is None


def test_model_label_and_candidates(tmp_path: Path) -> None:
    assert runner.model_label("rife-v4.22_lite_ensembleFalse") == "rife-v4.22-lite"
    assert runner.model_label("rife-v4.26_ensembleFalse") == "rife-v4.26"
    plugins, models = _plugin_tree(
        tmp_path,
        [
            "rife-v4.18_ensembleFalse",
            "rife-v4.22_lite_ensembleFalse",
            "rife-v4.26_ensembleFalse",
        ],
    )
    (models / "not-a-model").mkdir()  # no *.param -> ignored
    cands = runner.discover_candidates(plugins, models, concurrency=24)
    assert [c.label for c in cands] == [
        "rife-v4.26",
        "rife-v4.18",
        "rife-v4.22-lite",
        "mvtools",
    ]
    assert all(c.concurrent_frames == 8 for c in cands if c.backend is BackendId.RIFE_NCNN)
    assert cands[-1].concurrent_frames == 16  # min(nproc, 16), spike M0(m)
    assert cands[0].model_path == models / "rife-v4.26_ensembleFalse"
    assert [c.label for c in runner.discover_candidates(plugins, models, rife=False)] == ["mvtools"]
    (plugins / osapi.plugin_file("librife.so")).unlink()
    assert [c.label for c in runner.discover_candidates(plugins, models)] == ["mvtools"]
    assert runner.discover_candidates(tmp_path / "none", tmp_path / "none") == ()


def _m(
    label: str,
    *,
    mpv: float = 70.0,
    rt: bool = True,
    rep: bool = True,
    faults_: int | None = 0,
    backend: BackendId = BackendId.RIFE_NCNN,
) -> BenchMeasurement:
    return BenchMeasurement(
        label, backend, label, mpv + 5, mpv, 0.01 if rep else 0.2, rep, 1.0, 0.9, None, rt, faults_
    )


def test_recommend_rules() -> None:
    # quality order is the measurement order; faulting configs are never recommended;
    # repeatable is shown, not a tie-breaker (B6)
    ms = (_m("a", faults_=1), _m("b", rep=False), _m("c"), _m("d", faults_=None))
    assert runner.recommend(ms) == "b"
    assert runner.recommend((_m("a", faults_=2), _m("b", rep=False))) == "b"
    assert runner.recommend((_m("a", rt=False),)) is None
    assert runner.recommend((_m("a", faults_=None),)) == "a"  # unknown is not a fault
    assert runner.recommend((_m("x @ 2160p"), _m("x", rt=False)), frozenset({"x"})) is None


def test_recommend_uses_the_live_bar_with_a_target() -> None:
    """B5: 23.976 -> 47.95 needs 55.14 in mpv for the bench's "real time" but 59.94
    for live playback; 57 fps is real time in the table and not recommended."""
    target = Fraction(48000, 1001)
    between = _m("rife", mpv=57.0)
    assert between.realtime and verdict.realtime(57.0, target)
    assert not verdict.live_ok(between, target)
    ms = (between, _m("mvgpu", mpv=61.0, backend=BackendId.MVGPU))
    assert runner.recommend(ms) == "rife"  # no target: the benchmark's own verdict
    assert runner.recommend(ms, target=target) == "mvgpu"
    assert runner.recommend((between,), target=target) is None


def test_request_helpers() -> None:
    assert runner.target_fps(REQ) == Fraction(48000, 1001)
    assert runner.target_fps(dataclasses.replace(REQ, target_fps=Fraction(120))) == 120
    assert runner.sizes_for(REQ) == ((1920, 1080),)
    full = runner.sizes_for(dataclasses.replace(REQ, full=True))
    assert full[0] == (1920, 1080) and len(full) == 4 and (3840, 2160) in full
    assert runner.size_label("mvtools", (3840, 2160), REQ) == "mvtools @ 2160p"
    assert runner.output_frames(1080) > runner.output_frames(2160)


# =============================================================== history store
def _result(**kw: Any) -> BenchResult:
    return dataclasses.replace(sc.bench_result_4090(REQ), **kw)


def test_store_roundtrip_and_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state" / "buttereye" / "bench.json"
    assert store.read(path) == ()
    r1 = _result()
    r2 = _result(
        request=BenchRequest(3840, 2160, Fraction(25), Fraction(50), full=True),
        recommended=None,
        gpu_uuid=None,
        when=datetime(2026, 10, 8, tzinfo=UTC),
    )
    store.append(path, r1)
    store.append(path, r2)
    assert store.read(path) == (r1, r2)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [p.name for p in path.parent.iterdir()] == ["bench.json"]  # no temp files left
    doc = json.loads(path.read_text())
    assert doc["schema"] == store.SCHEMA
    assert doc["results"][1]["request"]["target_fps"] == "50/1"


def test_store_keeps_newest(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    for i in range(store.MAX_RESULTS + 3):
        store.append(path, _result(gpu_uuid=f"g{i}"))
    got = store.read(path)
    assert len(got) == store.MAX_RESULTS
    assert got[-1].gpu_uuid == f"g{store.MAX_RESULTS + 2}"


def test_store_failed_measurement_roundtrips(tmp_path: Path) -> None:
    path = tmp_path / "bench.json"
    failed = BenchMeasurement(
        "x", BackendId.MVTOOLS, None, 0.0, 0.0, math.inf, False, 0.0, 0.0, None, False, None
    )
    store.append(path, _result(measurements=(failed,)))
    assert store.read(path)[0].measurements == (failed,)


def test_store_forgets_old_script_mvtools_once(tmp_path: Path) -> None:
    # bench.vpy before version 2 timed MVTools at pel 1 from 720p up (playback runs
    # pel 2 to 1080p): those speeds are dropped, everything else is kept
    path = tmp_path / "bench.json"
    r = _result()
    store.write_atomic(path, [r])
    doc = json.loads(path.read_text())
    assert doc["results"][0]["bench"] == store.BENCH
    assert store.read(path) == (r,)
    del doc["results"][0]["bench"]
    path.write_text(json.dumps(doc))
    old = store.read(path)[0]
    assert [m.backend for m in old.measurements] == [BackendId.RIFE_NCNN] * 3
    kept = tuple(m for m in r.measurements if m.backend is not BackendId.MVTOOLS)
    assert old.measurements == kept
    assert old.recommended == r.recommended
    store.append(path, _result(gpu_uuid="new"))  # the rewrite tags it: dropped once
    doc = json.loads(path.read_text())
    assert [x["bench"] for x in doc["results"]] == [store.BENCH] * 2
    assert store.read(path)[0] == old


def test_bench_script_times_mvtools_as_template_plays() -> None:
    bench = (BENCH_DIR / "bench.vpy").read_text()
    assert "pel=1 if clip.width > 1920 or clip.height > 1080 else 2" in bench


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b'{"schema": 99, "results": []}',
        b'{"schema": 1, "results": [{"x": 1}]}',
        b'{"schema": 1, "results": [{"request": {"width": true}}]}',
    ],
)
def test_store_corrupt_is_moved_aside(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bench.json"
    path.write_bytes(content)
    with pytest.raises(store.HistoryCorrupt):
        store.read(path)
    store.append(path, _result(), now=datetime(2026, 10, 7, 12, 0, 0))
    assert len(store.read(path)) == 1
    aside = tmp_path / "bench.json.bad-20261007-120000"
    assert aside.read_bytes() == content


# =============================================================== static rules
def test_sources_never_import_vapoursynth_or_qt() -> None:
    for py in BENCH_DIR.glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.keyword) and node.arg == "preexec_fn":
                pytest.fail(f"preexec_fn in {py.name}")
        for n in names:
            assert not n.startswith(("vapoursynth", "PySide6", "buttereye.gui")), (py.name, n)


def test_spdx_headers() -> None:
    for py in BENCH_DIR.glob("*.py"):
        assert py.read_text().startswith("# SPDX-License-Identifier: AGPL-3.0-or-later\n"), py
    vpy = (BENCH_DIR / "bench.vpy").read_text()
    assert vpy.startswith(
        "# SPDX-License-Identifier: AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output\n"
    )


def test_vpy_reads_settings_only_from_user_data() -> None:
    tree = ast.parse((BENCH_DIR / "bench.vpy").read_text())
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "loads" in calls  # json.loads(user_data)
    assert "getenv" not in calls and "environ" not in (BENCH_DIR / "bench.vpy").read_text()


# =============================================================== provider with fakes
FAKE_VSPIPE = """\
#!/bin/sh
# fake vspipe: records its pid, optionally hangs (with a child), prints a vspipe result
echo $$ >> "$FAKE_DIR/vspipe.pids"
case "$FAKE_VSPIPE_MODE" in
  hang) sleep 300 & echo $! >> "$FAKE_DIR/child.pids"; wait ;;
  fail) echo "Script evaluation failed:" >&2; echo "Python exception: boom" >&2; exit 1 ;;
esac
sleep "${FAKE_VSPIPE_SLEEP:-0.05}"
echo "Script evaluation done in 0.50 seconds" >&2
echo "${FAKE_VSPIPE_OUTPUT:-Output 192 frames in 2.00 seconds (96.00 fps)}" >&2
"""

FAKE_MPV = """\
#!/bin/sh
# fake mpv: prints the --msg-time lines the runner parses (191 frames in 1.91 s = 100 fps)
echo $$ >> "$FAKE_DIR/mpv.pids"
printf '%s\\n' "$@" > "$FAKE_DIR/mpv.argv"
case "$FAKE_MPV_MODE" in
  fail) echo "[   0.066] Disabling filter vapoursynth.00 because it has failed."; \
        echo "[   0.067] VO: [null] 1920x1080 nv12"; exit 0 ;;
  failonce) if [ ! -f "$FAKE_DIR/mpv.failed" ]; then touch "$FAKE_DIR/mpv.failed"; exit 1; fi ;;
esac
EOF_AT=2.110000
[ -f "$FAKE_DIR/mpv.eof" ] && EOF_AT=$(cat "$FAKE_DIR/mpv.eof")
echo "[   0.100000] VO: [null] 1920x1080 yuv420p"
echo "[   0.200000] [cplayer] first video frame after restart shown"
echo "[   $EOF_AT] [cplayer] video EOF reached"
"""

FAKE_NVIDIA_SMI = """\
#!/bin/sh
for p in $(cat "$FAKE_DIR/vspipe.pids" 2>/dev/null); do echo "$p, 512"; done
"""


def _plugin_tree(root: Path, models: list[str]) -> tuple[Path, Path]:
    plugins = root / "lib" / "vapoursynth"
    data = root / "share"
    model_dir = data / runner.MODEL_SUBDIR
    plugins.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    (plugins / osapi.plugin_file("librife.so")).write_bytes(b"")
    (plugins / osapi.plugin_file("mvtools.so")).write_bytes(b"")
    for m in models:
        (model_dir / m).mkdir()
        (model_dir / m / "flownet.param").write_text("")
    return plugins, model_dir


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


class _ConfigStore:
    def __init__(self) -> None:
        self.cfg = sc.default_config()
        self.revision = "r0"
        self.saved: list[Config] = []

    def load(self, paths: Paths) -> ConfigLoad:
        return sc.config_load(self.cfg, revision=self.revision)

    def save(self, paths: Paths, cfg: Config, *, expected_revision: str) -> str:
        if expected_revision != self.revision:
            raise ConfigConflict(
                ErrorCode.CONFIG_CONFLICT, Msg("changed"), on_disk_revision=self.revision
            )
        self.saved.append(cfg)
        self.cfg, self.revision = cfg, f"r{len(self.saved)}"
        return self.revision


@dataclasses.dataclass
class Env:
    root: Path
    fake: Path
    paths: Paths
    store: _ConfigStore
    scans: list[dict[str, Any]]
    fault_count: list[int | None]

    def pids(self, name: str) -> list[int]:
        f = self.fake / f"{name}.pids"
        return [int(x) for x in f.read_text().split()] if f.exists() else []


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    fake = tmp_path / "fake"
    bindir = fake / "bin"
    bindir.mkdir(parents=True)
    _exe(bindir / "vspipe", FAKE_VSPIPE)
    _exe(bindir / "mpv", FAKE_MPV)
    _exe(bindir / "nvidia-smi", FAKE_NVIDIA_SMI)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setenv("FAKE_DIR", str(fake))
    # The planted tree is the whole truth about what is installed. On macOS the
    # plugin search path asks Homebrew's autoload directory as well as
    # ``rpm_plugin_dir`` (``paths.plugin_dirs``), so on a developer's Mac the real
    # ``mvtools.dylib`` would answer a question about this tmp_path: point the
    # prefix at a directory with nothing in it. A no-op on Linux and Windows,
    # where the search path is ``rpm_plugin_dir`` alone.
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "no-homebrew"))
    for k in ("FAKE_VSPIPE_MODE", "FAKE_MPV_MODE", "FAKE_VSPIPE_SLEEP", "FAKE_VSPIPE_OUTPUT"):
        monkeypatch.delenv(k, raising=False)
    plugins, models = _plugin_tree(tmp_path / "rpm", ["rife-v4.26_ensembleFalse"])
    paths = dataclasses.replace(
        sc.fake_paths(tmp_path / "xdg"), rpm_plugin_dir=plugins, rpm_data_dir=models.parent
    )
    cfg = _ConfigStore()
    monkeypatch.setitem(
        sys.modules,
        "buttereye.core.profiles.config",
        pytypes.SimpleNamespace(load=cfg.load, save=cfg.save),
    )
    scans: list[dict[str, Any]] = []
    fault_count: list[int | None] = [0]

    async def scan_xid(*, since_monotonic: float, pids: frozenset[int]) -> Any:
        scans.append({"since": since_monotonic, "pids": pids})
        n = fault_count[0]
        return pytypes.SimpleNamespace(readable=n is not None, count=n or 0, error=None)

    monkeypatch.setattr(faults, "_scanner", lambda: scan_xid)
    monkeypatch.setattr(faults, "JOURNAL_SETTLE_S", 0.0)
    real_detect = measure.VramSampler.detect

    def fast_detect() -> measure.VramSampler | None:
        s = real_detect()
        return measure.VramSampler(s._bin, interval_s=0.01) if s else None

    monkeypatch.setattr(measure.VramSampler, "detect", staticmethod(fast_detect))
    # the sustained phase and the extra live sizes have tests of their own
    monkeypatch.setattr(runner, "SUSTAINED_S", 0.0)
    monkeypatch.setattr(runner, "AUTO_LIVE_SIZES", False)
    yield Env(tmp_path, fake, paths, cfg, scans, fault_count)


async def _open(env: Env) -> tuple[ButterEye, list[Event]]:
    core = await ButterEye.open(env.paths)
    got: list[Event] = []

    async def drain() -> None:
        async for ev in core.subscribe():
            got.append(ev)

    asyncio.ensure_future(drain())
    await asyncio.sleep(0)
    return core, got


async def test_capability_is_real(env: Env) -> None:
    core, _ = await _open(env)
    caps = await core.capabilities()
    assert caps.ok(Feature.BENCH), caps.states[Feature.BENCH]
    await core.close(cancel_jobs=True)


async def test_bench_end_to_end_with_fake_tools(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VSPIPE_SLEEP", "0.08")
    core, events = await _open(env)
    op = core.bench(REQ)
    progress: list[Progress] = []
    op.add_progress_sink(progress.append)
    result = await op.result()

    labels = [m.label for m in result.measurements]
    assert labels == ["rife-v4.26", "mvtools"]
    rife = result.measurements[0]
    assert rife.backend is BackendId.RIFE_NCNN and rife.model == "rife-v4.26_ensembleFalse"
    assert rife.vspipe_fps == pytest.approx(96.0)
    # 192 frames, 8 in flight when the clock starts (concurrent-frames), 1.91 s
    assert rife.mpv_fps == pytest.approx((192 - 8) / 1.91, abs=0.01)
    assert rife.cov == 0.0 and rife.repeatable
    assert rife.reload_s == pytest.approx(0.5) and rife.startup_s == pytest.approx(0.2)
    assert rife.realtime  # 96.3 >= 1.15 * 47.95
    assert rife.gpu_faults == 0
    assert rife.vram_bytes == 512 * 1024 * 1024
    assert result.recommended == "rife-v4.26"
    assert result.request == REQ
    # warm-up + 3 vspipe runs + 3 mpv runs per candidate, each in its own process group
    assert len(env.pids("vspipe")) == 8 and len(env.pids("mpv")) == 6
    assert len(env.scans) == 2
    # MVTools, a live engine, is measured first, while the machine is cool (B7);
    # the results are still reported in quality order
    assert set(env.pids("vspipe")[:4]) | set(env.pids("mpv")[:3]) == env.scans[0]["pids"]
    assert set(env.pids("vspipe")[4:]) | set(env.pids("mpv")[3:]) == env.scans[1]["pids"]
    # mpv got the RIFE script settings through user-data, gpu_thread=4 (§4.4, M0(f))
    argv = (env.fake / "mpv.argv").read_text().splitlines()
    assert "--frames=192" in argv
    # progress: one phase per pass, monotonic, ends at total
    phases = [p.phase.key for p in progress]
    assert progress[0].phase.params["config"] == "mvtools"
    assert "{config} — run {n} of {total} — in mpv" in phases
    assert "{config} — warm-up — vspipe" in phases
    dones = [p.done for p in progress if p.done is not None]
    assert dones == sorted(dones) and progress[-1].done == progress[-1].total == 14
    assert all(p.op_id == op.id for p in progress)
    # persisted
    assert await core.bench_history() == (result,)
    assert stat.S_IMODE(env.paths.bench_file.stat().st_mode) == 0o600
    assert not [e for e in events if isinstance(e, Notice)]
    await core.close(cancel_jobs=True)


async def test_user_data_reaches_vspipe(env: Env) -> None:
    core, _ = await _open(env)
    log = env.fake / "vspipe.args"
    _exe(
        env.fake / "bin" / "vspipe",
        FAKE_VSPIPE.replace("echo $$ >>", f'printf "%s\\\\n" "$@" >> "{log}"; echo $$ >>'),
    )
    await core.bench(dataclasses.replace(REQ, target_fps=Fraction(60000, 1001))).result()
    args = log.read_text().splitlines()
    data = [json.loads(a.split("=", 1)[1]) for a in args if a.startswith("user_data=")]
    rife = next(d for d in data if d["backend"] == "rife")
    # 4, the soak-tested value; 1 on a Mac, MoltenVK's single compute queue
    assert rife["gpu_thread"] == (1 if osapi.IS_MACOS else 4)
    assert (rife["src_num"], rife["src_den"], rife["out_num"], rife["out_den"]) == (
        24000,
        1001,
        60000,
        1001,
    )
    assert rife["model_path"] == str(
        env.paths.rpm_data_dir / "rife-ncnn-models" / "rife-v4.26_ensembleFalse"
    )
    assert rife["uhd"] is False and rife["matrix"] == "709"
    assert args[args.index("-e") + 1] == "191"
    # the blank clip is all identical frames: never re-timed or copied
    assert not any(d.get("hold_fill") or d.get("still_skip") for d in data)
    await core.close(cancel_jobs=True)


async def test_trt_speed_test_never_fills_or_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TensorRT is timed through the playback template, on a blank clip: hold fill
    and still skip must both be off there, or the run times copies."""
    from buttereye.core.backends import trt

    async def identity(uuid: str | None) -> tuple[str, str]:
        return "GPU-1", "615.71"

    async def build(paths: Any, inst: Any, key: Any, *, script: Path) -> Path:
        return tmp_path / "engine"

    monkeypatch.setattr(trt, "nvidia_identity", identity)
    monkeypatch.setattr(trt, "build_engine", build)
    inst = trt.TrtInstall(
        tmp_path / "vstrt", tmp_path / "vstrt", (tmp_path / "py",), tmp_path / "onnx",
        Path("/usr/bin/trtexec"), "v16.3.test1", "11.3.0",
    )  # fmt: skip
    c = runner.Candidate(
        "rife-v4.26 trt", BackendId.RIFE_TRT, "rife-v4.26_ensembleFalse", None, 2, inst
    )
    ctx = pytypes.SimpleNamespace(paths=sc.fake_paths(tmp_path))
    data, _ = await runner._trt_prepare(
        ctx,  # type: ignore[arg-type]
        c, None, width=1920, height=1080, source=REQ.source_fps,
        target=REQ.source_fps * 2, src_frames=97,
    )  # fmt: skip
    assert data["hold_fill"] is False and data["still_skip"] is False
    assert data["source"]["kind"] == "blank"


async def test_gpu_fault_is_never_recommended(env: Env) -> None:
    env.fault_count[0] = 1
    core, _ = await _open(env)
    result = await core.bench(REQ).result()
    assert all(m.gpu_faults == 1 for m in result.measurements)
    assert result.recommended is None
    load = await core.load_config()
    with pytest.raises(ButterEyeError) as ei:
        await core.apply_bench(result, "rife-v4.26", expected_revision=load.revision)
    assert ei.value.code is ErrorCode.BENCH_FAILED
    assert env.store.saved == []
    await core.close(cancel_jobs=True)


async def test_unreadable_journal_is_unknown_not_zero(env: Env) -> None:
    env.fault_count[0] = None
    core, _ = await _open(env)
    result = await core.bench(REQ).result()
    assert all(m.gpu_faults is None for m in result.measurements)
    assert result.recommended == "rife-v4.26"
    await core.close(cancel_jobs=True)


async def test_scanner_absent_gives_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(faults, "GPUFAULT_MODULE", "buttereye.core.no_such_module_here")
    assert faults._scanner() is None
    assert await faults.count_new(time.monotonic(), {1}) is None


async def test_mpv_failure_falls_back_with_notice(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_MPV_MODE", "fail")
    core, events = await _open(env)
    result = await core.bench(REQ).result()
    for m in result.measurements:
        assert m.mpv_fps == m.vspipe_fps == pytest.approx(96.0)
        assert not m.realtime  # nothing verified inside mpv
    assert result.recommended is None
    await asyncio.sleep(0)
    notes = [e for e in events if isinstance(e, Notice)]
    assert len(notes) == 2 and all("In-mpv speed" in n.message.key for n in notes)
    await core.close(cancel_jobs=True)


async def test_vspipe_failure_everywhere_fails_the_op(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_VSPIPE_MODE", "fail")
    core, _ = await _open(env)
    op = core.bench(REQ)
    with pytest.raises(ButterEyeError) as ei:
        await op.result()
    assert ei.value.code is ErrorCode.BENCH_FAILED
    assert ei.value.detail is not None and "boom" in ei.value.detail
    assert op.state is OpState.FAILED
    assert not env.paths.bench_file.exists()
    assert env.pids("mpv") == []  # no mpv pass after a failed vspipe pass
    await core.close(cancel_jobs=True)


async def test_missing_tools_and_plugins(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    core, _ = await _open(env)
    (env.fake / "bin" / "vspipe").unlink()
    monkeypatch.setenv("PATH", str(env.fake / "bin"))
    with pytest.raises(ButterEyeError) as ei:
        await core.bench(REQ).result()
    assert ei.value.commands == (runner.INSTALL_VSPIPE,)
    _exe(env.fake / "bin" / "vspipe", FAKE_VSPIPE)
    monkeypatch.setenv("PATH", f"{env.fake / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin")
    for so in env.paths.rpm_plugin_dir.iterdir():
        so.unlink()
    with pytest.raises(ButterEyeError) as ei:
        await core.bench(REQ).result()
    assert ei.value.commands == (runner.INSTALL_PLUGINS,)
    await core.close(cancel_jobs=True)


async def test_mpv_missing_falls_back(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    real = runner.find_tools

    def no_mpv(paths: Paths) -> runner.Tools:
        return dataclasses.replace(real(paths), mpv=None)

    monkeypatch.setattr(runner, "find_tools", no_mpv)
    core, _ = await _open(env)
    result = await core.bench(REQ).result()
    assert all(not m.realtime and m.mpv_fps == m.vspipe_fps for m in result.measurements)
    await core.close(cancel_jobs=True)


@pytest.mark.parametrize(
    "req",
    [
        dataclasses.replace(REQ, user_file=Path("/tmp/film.mkv")),
        dataclasses.replace(REQ, width=1921),
        dataclasses.replace(REQ, target_fps=Fraction(24000, 1001)),
    ],
)
async def test_bad_requests_refused_before_any_process(env: Env, req: BenchRequest) -> None:
    core, _ = await _open(env)
    with pytest.raises(ButterEyeError) as ei:
        await core.bench(req).result()
    assert ei.value.code is ErrorCode.BENCH_FAILED
    assert env.pids("vspipe") == []
    await core.close(cancel_jobs=True)


async def test_second_bench_refused_while_running(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_VSPIPE_SLEEP", "0.3")
    core, _ = await _open(env)
    first = core.bench(REQ)
    await asyncio.sleep(0.1)
    with pytest.raises(ButterEyeError, match="BE-5001"):
        await core.bench(REQ).result()
    first.cancel()
    assert await first.wait() is OpState.CANCELLED
    await core.close(cancel_jobs=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    if osapi.IS_MACOS:
        # No /proc here, so ask ps for the state letter instead; a missing pid prints
        # nothing. Same meaning as the Linux branch: 'Z' is a zombie, which is gone.
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)], capture_output=True, text=True, timeout=10
        )
        return not out.stdout.strip().startswith("Z")
    try:  # a zombie is gone for our purposes
        return "State:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False


async def test_cancel_kills_the_process_group(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VSPIPE_MODE", "hang")
    core, _ = await _open(env)
    op = core.bench(REQ)
    for _ in range(200):
        if env.pids("child"):
            break
        await asyncio.sleep(0.02)
    (vspipe,), (child,) = env.pids("vspipe"), env.pids("child")
    assert _alive(vspipe) and _alive(child)
    assert os.getpgid(vspipe) == vspipe  # own group (start_new_session)
    t0 = time.monotonic()
    op.cancel()
    with pytest.raises(OperationCancelled):
        await op.result()
    assert time.monotonic() - t0 < 5.5
    assert op.state is OpState.CANCELLED
    for _ in range(100):
        if not _alive(child):
            break
        await asyncio.sleep(0.02)
    assert not _alive(vspipe) and not _alive(child)
    assert not env.paths.bench_file.exists()
    await core.close(cancel_jobs=True)


async def test_timeout_terminates_pass(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_VSPIPE_MODE", "hang")

    class _Op:
        async def spawn(self, argv: list[str], **kw: Any) -> asyncio.subprocess.Process:
            from buttereye.core.ops import spawn_group

            return await spawn_group(argv, **kw)

    with pytest.raises(measure.MeasureFailed, match="did not finish"):
        await measure.run_vspipe(_Op(), ["vspipe"], timeout_s=0.3)  # type: ignore[arg-type]
    (vspipe,) = env.pids("vspipe")
    for _ in range(100):
        if not _alive(vspipe):
            break
        await asyncio.sleep(0.02)
    assert not _alive(vspipe)
    for _ in range(100):
        if not any(_alive(c) for c in env.pids("child")):
            break
        await asyncio.sleep(0.02)
    assert not any(_alive(c) for c in env.pids("child"))  # the whole group went


# =============================================================== apply
async def test_apply_writes_benchmark_profile_and_first_rule(env: Env) -> None:
    core, _ = await _open(env)
    result = _result()
    load = await core.load_config()
    rev = await core.apply_bench(result, "rife-v4.22-lite", expected_revision=load.revision)
    assert rev == "r1"
    cfg = env.store.saved[-1]
    prof = next(p for p in cfg.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert prof.backend is BackendId.RIFE_NCNN and prof.model == "rife-v4.22_lite_ensembleFalse"
    assert not prof.builtin and prof.target.kind is TargetKind.X2
    assert prof.concurrent_frames == 8 and prof.buffered_frames == 4
    assert cfg.rules[0].profile == runner.BENCH_PROFILE_ID
    match = cfg.rules[0].match
    assert (match.width_max, match.height_max) == (1920, 1080)
    assert match.fps_max == REQ.source_fps * Fraction(101, 100)  # +1 %, exact (§11.9)
    assert cfg.rules[1:] == sc.default_config().rules
    assert [p.id for p in cfg.profiles if p.builtin] == [p.id for p in sc.builtin_profiles()]
    # applying again replaces, never duplicates
    rev2 = await core.apply_bench(result, "mvtools", expected_revision=rev)
    cfg2 = env.store.saved[-1]
    assert rev2 == "r2"
    assert [p.id for p in cfg2.profiles].count(runner.BENCH_PROFILE_ID) == 1
    assert [r.profile for r in cfg2.rules].count(runner.BENCH_PROFILE_ID) == 1
    bench_prof = next(p for p in cfg2.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert bench_prof.backend is BackendId.MVTOOLS and bench_prof.concurrent_frames is None
    with pytest.raises(ConfigConflict):
        await core.apply_bench(result, "mvtools", expected_revision="stale")
    await core.close(cancel_jobs=True)


def test_bench_config_keeps_the_profiles_own_content() -> None:
    # re-applying a measured configuration keeps what the user set on it
    result = _result()
    cfg = runner.bench_config(sc.default_config(), result, "rife-v4.22-lite")
    prof = next(p for p in cfg.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert prof.content is ContentKind.AUTO
    anime = dataclasses.replace(prof, content=ContentKind.ANIMATION)
    cfg = dataclasses.replace(cfg, profiles=tuple(anime if p is prof else p for p in cfg.profiles))
    cfg2 = runner.bench_config(cfg, result, "mvtools")
    prof2 = next(p for p in cfg2.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert prof2.backend is BackendId.MVTOOLS and prof2.content is ContentKind.ANIMATION


async def test_apply_refusals(env: Env) -> None:
    core, _ = await _open(env)
    load = await core.load_config()
    result = _result()
    for label in ("nope", "rife-v4.18"):  # unknown; rife-v4.18 faulted in the fixture data
        with pytest.raises(ButterEyeError) as ei:
            await core.apply_bench(result, label, expected_revision=load.revision)
        assert ei.value.code is ErrorCode.BENCH_FAILED
    other = _result(measurements=(_m("mvtools @ 2160p", backend=BackendId.MVTOOLS),))
    with pytest.raises(ButterEyeError):
        await core.apply_bench(other, "mvtools @ 2160p", expected_revision=load.revision)
    assert env.store.saved == []
    await core.close(cancel_jobs=True)


@pytest.mark.parametrize(
    ("fps", "matches"),
    [
        (Fraction(24000, 1001), True),
        (fps_fraction(24.0031), True),  # the real file that missed the old exact bound
        (Fraction(24), True),
        (Fraction(25), False),
        (Fraction(30), False),
    ],
)
def test_bench_rule_tolerates_tagged_rates(fps: Fraction, matches: bool) -> None:
    """A 23.976 benchmark's rule also catches files tagged 24.0031 or 24, not 25."""
    from buttereye.core.profiles import rules

    cfg = runner.bench_config(sc.default_config(), _result(), "rife-v4.22-lite")
    facts = SourceFacts(fps, 1920, 1080, HdrClass.SDR, 180.0, False, "/f.mkv", False)
    trace = rules.explain(cfg, facts)
    assert (trace.profile_id == runner.BENCH_PROFILE_ID) is matches


def test_bench_config_fixed_target() -> None:
    req = BenchRequest(2560, 1440, Fraction(25), Fraction(100))
    m = _m("rife-v4.26")
    cfg = runner.bench_config(sc.default_config(), _result(request=req, measurements=(m,)), m.label)
    prof = next(p for p in cfg.profiles if p.id == runner.BENCH_PROFILE_ID)
    assert prof.target.kind is TargetKind.FPS and prof.target.fps == 100
    assert "2560×1440" in prof.name


async def test_history_corrupt_file_is_empty_not_fatal(env: Env) -> None:
    core, _ = await _open(env)
    env.paths.bench_file.parent.mkdir(parents=True)
    env.paths.bench_file.write_text("{broken")
    assert await core.bench_history() == ()
    await core.close(cancel_jobs=True)


# =============================================================== GPU faults: Xid 109, repeats
XID_109 = (
    "NVRM: Xid (PCI:0000:08:00): 109, pid={pid}, name=vspipe, channel 0x00000015, "
    "errorString CTX SWITCH TIMEOUT"
)
XID_13 = (
    "NVRM: Xid (PCI:0000:08:00): 13, pid={pid}, name=vspipe, Graphics Exception: "
    "channel 0x00000049, Class 0000c9c0 (compute)"
)


async def test_count_new_counts_xid_109_through_the_real_scanner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-10-07 06:35:40 bundled-build fault (Xid 109, CTX SWITCH TIMEOUT) is a
    GPU fault for the benchmark just like Xid 13; older faults and other processes'
    faults are not blamed on the candidate."""
    import functools

    since = time.monotonic()
    ours, other = 326356, 4242

    def rec(msg: str, at: float) -> str:
        return json.dumps({"MESSAGE": msg, "__MONOTONIC_TIMESTAMP": str(int(at * 1e6))})

    journal = tmp_path / "journal.json"
    journal.write_text(
        "\n".join(
            (
                rec(XID_13.format(pid=ours), since - 3000.0),  # old Fedora-ncnn soak fault
                rec(XID_109.format(pid=ours), since + 1.0),
                rec(XID_109.format(pid=other), since + 30.0),  # someone else's process
            )
        )
        + "\n"
    )
    fake = tmp_path / "journalctl"
    _exe(fake, f'#!/bin/sh\ncat "{journal}"\n')
    monkeypatch.setattr(
        faults, "_scanner", lambda: functools.partial(gpufault.scan_xid, journalctl=str(fake))
    )
    monkeypatch.setattr(faults, "JOURNAL_SETTLE_S", 0.0)
    # The fault history read here IS the Linux kernel log, down to the stub
    # journalctl above; macOS and Windows have no journal and short-circuit.
    monkeypatch.setattr(gpufault.osapi, "IS_MACOS", False)
    assert await faults.count_new(since, {ours}) == 1
    assert await faults.count_new(since, {12345}) == 0
    assert await faults.count_new(since - 4000.0, {ours}) == 2  # Xid 13 and Xid 109


def _faulty(label: str, n: int | None, *, gpu: str | None = None) -> BenchResult:
    return _result(
        measurements=(_m(label, faults_=n), _m("mvtools", backend=BackendId.MVTOOLS)),
        gpu_uuid=gpu,
    )


def test_repeat_faulters_needs_two_runs_on_the_same_gpu() -> None:
    key = (BackendId.RIFE_NCNN, "rife-v4.22-lite")
    one = (_faulty("rife-v4.22-lite", 1),)
    assert runner.repeat_faulters(one, None) == {}  # one isolated fault drops nothing
    two = one + (_faulty("rife-v4.22-lite", None), _faulty("rife-v4.22-lite", 3))
    assert runner.repeat_faulters(two, None) == {key: 2}
    assert runner.repeat_faulters(two, "GPU-other") == {}
    # a result counts once per model, even with several faulting sizes
    multi = _result(
        measurements=(_m("rife-v4.22-lite", faults_=1),) * 2,  # same model, two labels
        gpu_uuid=None,
    )
    assert runner.repeat_faulters((multi, _faulty("rife-v4.22-lite", 0)), None) == {}
    # only the newest FAULT_LOOKBACK results matter
    old = (_faulty("rife-v4.22-lite", 1),) * 2
    clean = (_faulty("rife-v4.22-lite", 0),) * runner.FAULT_LOOKBACK
    assert runner.repeat_faulters(old + clean, None) == {}
    assert runner.recommend((_m("a"), _m("b")), avoid=frozenset({"a"})) == "b"


async def test_repeat_faulter_is_not_recommended_even_when_clean(env: Env) -> None:
    model = "rife-v4.26_ensembleFalse"

    def past(n: int) -> BenchResult:
        m = dataclasses.replace(_m("rife-v4.26", faults_=n), model=model)
        return _result(measurements=(m,), gpu_uuid=None)

    store.append(env.paths.bench_file, past(1))
    core, events = await _open(env)
    result = await core.bench(REQ).result()
    assert result.recommended == "rife-v4.26"  # one earlier fault: still recommended
    store.append(env.paths.bench_file, past(1))
    result = await core.bench(REQ).result()
    assert result.measurements[0].gpu_faults == 0
    assert result.recommended == "mvtools"
    await asyncio.sleep(0)
    notes = [e for e in events if isinstance(e, Notice)]
    assert len(notes) == 1 and notes[0].code is ErrorCode.RIFE_GPU_FAULT
    assert notes[0].message.params == {"config": "rife-v4.26", "n": 2}
    # each RIFE measurement starts the doctor's BE-1030 window
    from buttereye.core.doctor import gpufault

    assert gpufault.read_session_marker(env.paths) is not None
    await core.close(cancel_jobs=True)


# =============================================================== review fixes (B5-B10, P4)
def test_plan_measures_live_engines_first_and_at_bigger_sizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B7: mvgpu and MVTools before RIFE (cool machine), reported in quality order;
    and the live engines at 1440p and 2160p too, so 4K is decided by a 4K number."""
    monkeypatch.setattr(runner, "AUTO_LIVE_SIZES", True)
    rife = runner.Candidate("rife", BackendId.RIFE_NCNN, "m", None, 8)
    mvgpu = runner.Candidate("mvgpu", BackendId.MVGPU, None, None, 8)
    mvtools = runner.Candidate("mvtools", BackendId.MVTOOLS, None, None, 4)
    tasks = runner.plan(REQ, (rife, mvgpu, mvtools))
    order = [(c.label, size[1]) for _, size, c in tasks]
    assert order == [
        ("mvgpu", 1080), ("mvtools", 1080), ("mvgpu", 1440), ("mvtools", 1440),
        ("mvgpu", 2160), ("mvtools", 2160), ("rife", 1080),
    ]  # fmt: skip
    keys = sorted(k for k, _, _ in tasks)
    assert [k for k, _, _ in sorted(tasks)][0] == (0, 0) and keys[:3] == [(0, 0), (0, 1), (0, 2)]
    full = runner.plan(dataclasses.replace(REQ, full=True), (rife, mvgpu))
    assert len(full) == 8 and full[-1][2] is rife  # every size for everything, RIFE last
    uhd = runner.plan(dataclasses.replace(REQ, width=3840, height=2160), (mvgpu,))
    assert [size for _, size, _ in uhd] == [(3840, 2160)]  # nothing bigger to measure
    monkeypatch.setattr(runner, "AUTO_LIVE_SIZES", False)
    assert len(runner.plan(REQ, (rife, mvgpu, mvtools))) == 3


async def test_live_engines_are_measured_at_bigger_sizes(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "AUTO_LIVE_SIZES", True)
    core, _ = await _open(env)
    result = await core.bench(REQ).result()
    labels = [m.label for m in result.measurements]
    assert labels == ["rife-v4.26", "mvtools", "mvtools @ 1440p", "mvtools @ 2160p"]
    assert result.recommended == "rife-v4.26"  # only labels at the requested size
    await core.close(cancel_jobs=True)


def test_repeatable_is_the_verdict_repeating() -> None:
    """B6: judged on the in-mpv runs against the bar, not on a 5 % spread."""
    target = Fraction(48000, 1001)  # bar 55.14
    assert verdict.holds_the_bar([90.0, 99.0, 95.0], target)  # spread ~5 %, far above
    assert verdict.holds_the_bar([20.0, 21.0, 22.0], target)  # reliably too slow
    assert not verdict.holds_the_bar([54.0, 56.0, 57.0], target)  # straddles the bar
    assert not verdict.holds_the_bar([], target)


async def test_hopeless_candidate_gets_one_short_pass(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P4: a warm-up under half the real-time bar (19.2 < 27.6) is measured once,
    at 96 frames, in vspipe and in mpv: the verdict is the same, minutes sooner."""
    monkeypatch.setenv("FAKE_VSPIPE_OUTPUT", "Output 192 frames in 10.00 seconds (19.20 fps)")
    core, _ = await _open(env)
    op = core.bench(REQ)
    progress: list[Progress] = []
    op.add_progress_sink(progress.append)
    result = await op.result()
    assert len(env.pids("vspipe")) == 4 and len(env.pids("mpv")) == 2  # 2 candidates
    assert "--frames=96" in (env.fake / "mpv.argv").read_text().splitlines()
    for m in result.measurements:
        assert not m.realtime and m.repeatable  # one run, reliably below the bar
        assert 40 < m.mpv_fps < 51  # (96 - concurrent frames) / 1.91 s
    assert result.recommended is None
    dones = [p.done for p in progress if p.done is not None]
    assert dones == sorted(dones) and progress[-1].done == progress[-1].total
    await core.close(cancel_jobs=True)


async def test_a_failed_mpv_pass_is_tried_once_more(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B10: one failed pass no longer stores the vspipe speed (which Automatic then
    ignores for good); the pass runs again."""
    monkeypatch.setenv("FAKE_MPV_MODE", "failonce")
    core, events = await _open(env)
    result = await core.bench(REQ).result()
    assert len(env.pids("mpv")) == 7  # 6 passes + the retry
    assert all(m.realtime and verdict.measured_in_mpv(m) for m in result.measurements)
    await asyncio.sleep(0)
    assert not [e for e in events if isinstance(e, Notice)]
    await core.close(cancel_jobs=True)


async def test_sustained_phase_keeps_the_lower_rate(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B7: the best live-ok configuration is held; when it falls below the live bar
    the next one is held too, and each keeps min(burst, sustained)."""
    monkeypatch.setattr(runner, "SUSTAINED_S", 8.0)
    monkeypatch.setattr(osapi, "thermal_state", lambda: 1)
    real = runner._sustain
    calls: list[str] = []

    async def sustain(*a: Any, **kw: Any) -> Any:
        out = a[1]
        calls.append(out.m.label)
        slow = env.fake / "mpv.eof"
        if len(calls) == 1:  # RIFE heats up: 184 frames in 3.228 s = 57 fps
            slow.write_text("3.428000")
        try:
            return await real(*a, **kw)
        finally:
            slow.unlink(missing_ok=True)

    monkeypatch.setattr(runner, "_sustain", sustain)
    core, _ = await _open(env)
    op = core.bench(REQ)
    progress: list[Progress] = []
    op.add_progress_sink(progress.append)
    result = await op.result()
    assert calls == ["rife-v4.26", "mvtools"]
    rife, mvtools = result.measurements
    assert rife.mpv_fps == pytest.approx(57.0, abs=0.05)
    assert rife.realtime and not verdict.live_ok(rife, runner.target_fps(REQ))
    assert result.recommended == "mvtools"
    (entry,) = store.read_entries(env.paths.bench_file)
    assert entry.meta.sustained["rife-v4.26"] == pytest.approx(57.0, abs=0.05)
    assert entry.meta.sustained["mvtools"] == pytest.approx(mvtools.mpv_fps, abs=0.05)
    assert entry.meta.thermal_start == entry.meta.thermal_max == 1
    assert "{config} — keeping it up for {s} s — in mpv" in [p.phase.key for p in progress]
    dones = [p.done for p in progress if p.done is not None]
    assert dones == sorted(dones) and progress[-1].done == progress[-1].total
    await core.close(cancel_jobs=True)


async def test_contended_run_is_kept_but_not_used(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: a video played during the run: the result is returned and stored
    (marked), and history() leaves it out, so playback never caps by it."""
    calls: list[int] = []

    async def busy(ctx: Any) -> bool:
        calls.append(1)
        return len(calls) == 2  # something started playing mid-run

    monkeypatch.setattr(runner, "others_running", busy)
    core, events = await _open(env)
    result = await core.bench(REQ).result()
    assert result.recommended == "rife-v4.26"
    (entry,) = store.read_entries(env.paths.bench_file)
    assert entry.meta.contended
    assert await core.bench_history() == ()
    await asyncio.sleep(0)
    assert any("played or converted" in e.message.key for e in events if isinstance(e, Notice))
    await core.close(cancel_jobs=True)


async def test_a_video_played_and_closed_mid_run_is_contention(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B8: a player that opens and closes between two looks (inside the
    sustained phase) is never seen by ``others_running``; the run is still
    marked, so the heat-soaked, shared number is never used as clean."""

    async def idle(ctx: Any) -> bool:
        return False

    looks: list[int] = []

    async def seen(ctx: Any) -> frozenset[object]:
        looks.append(1)
        return frozenset({"s1"}) if len(looks) == 1 else frozenset({"s1", "s2"})

    monkeypatch.setattr(runner, "others_running", idle)
    monkeypatch.setattr(runner, "sessions_seen", seen)
    core, _events = await _open(env)
    await core.bench(REQ).result()
    (entry,) = store.read_entries(env.paths.bench_file)
    assert entry.meta.contended and len(looks) == 2
    assert await core.bench_history() == ()
    await core.close(cancel_jobs=True)


async def test_sessions_seen_lists_live_and_ended_players(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = pytypes.SimpleNamespace(sid="a", ended=False)
    gone = pytypes.SimpleNamespace(sid="b", ended=True)

    async def sessions(ctx: Any) -> tuple[Any, ...]:
        return (live,)

    async def recent_sessions(ctx: Any) -> tuple[Any, ...]:
        return (gone,)

    monkeypatch.setitem(
        sys.modules,
        "buttereye.core.mpvctl.session",
        pytypes.SimpleNamespace(sessions=sessions, recent_sessions=recent_sessions),
    )
    assert await runner.sessions_seen(None) == {"a", "b"}  # type: ignore[arg-type]
    monkeypatch.delitem(sys.modules, "buttereye.core.mpvctl.session")
    assert await runner.sessions_seen(None) == frozenset()  # type: ignore[arg-type]


async def test_others_running_asks_loaded_providers_only(monkeypatch: pytest.MonkeyPatch) -> None:
    live = pytypes.SimpleNamespace(ended=False)
    done = pytypes.SimpleNamespace(state=OpState.SUCCEEDED)
    running = pytypes.SimpleNamespace(state=OpState.RUNNING)

    async def sessions(ctx: Any) -> tuple[Any, ...]:
        return answer["sessions"]

    async def jobs(ctx: Any) -> tuple[Any, ...]:
        return answer["jobs"]

    answer: dict[str, tuple[Any, ...]] = {"sessions": (), "jobs": (done,)}
    monkeypatch.setitem(
        sys.modules, "buttereye.core.mpvctl.session", pytypes.SimpleNamespace(sessions=sessions)
    )
    monkeypatch.setitem(
        sys.modules, "buttereye.core.render.jobs", pytypes.SimpleNamespace(jobs=jobs)
    )
    assert not await runner.others_running(None)  # type: ignore[arg-type]
    answer["jobs"] = (running,)
    assert await runner.others_running(None)  # type: ignore[arg-type]
    answer["jobs"], answer["sessions"] = (), (live,)
    assert await runner.others_running(None)  # type: ignore[arg-type]
    monkeypatch.delitem(sys.modules, "buttereye.core.mpvctl.session")
    monkeypatch.delitem(sys.modules, "buttereye.core.render.jobs")
    assert not await runner.others_running(None)  # type: ignore[arg-type]


async def test_a_render_paused_for_the_benchmark_is_not_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B8/B11: a RUNNING render stops itself within 0.5 s of the lock; ``busy``
    says whether it still runs, and the benchmark waits for that first."""
    running = pytypes.SimpleNamespace(state=OpState.RUNNING)
    looks: list[int] = []

    async def jobs(ctx: Any) -> tuple[Any, ...]:
        return (running,)  # still RUNNING while stopped

    async def busy(ctx: Any) -> bool:
        looks.append(1)
        return len(looks) < 3  # stops itself on the third look

    monkeypatch.setitem(
        sys.modules, "buttereye.core.render.jobs", pytypes.SimpleNamespace(jobs=jobs, busy=busy)
    )
    monkeypatch.setattr(runner, "_SETTLE_POLL_S", 0.0)
    await runner.settle_renders(None)  # type: ignore[arg-type]
    assert len(looks) == 3
    assert not await runner.others_running(None)  # type: ignore[arg-type]
    looks.clear()
    assert await runner.others_running(None)  # type: ignore[arg-type]
    # a render that never stops: the wait gives up after its timeout
    looks.clear()
    await runner.settle_renders(None, timeout_s=0.0)  # type: ignore[arg-type]
    assert len(looks) == 1


def _entry(meta: store.Meta, *ms: BenchMeasurement) -> store.Entry:
    return store.Entry(_result(measurements=ms) if ms else _result(), meta)


def test_current_result_judges_versions_and_contention() -> None:
    """B9: a result describes the software that measured it. Another app, script,
    mpv, vspipe or Vulkan driver: not used; another plugin: that engine's rows go."""
    now = {"app": "0.0.6", "bench": "3", "mpv": "m1", "vspipe": "v1", "vulkan": None}
    now |= {runner.plugin_key(b): f"{b.value}-1" for b in (BackendId.RIFE_NCNN, BackendId.MVTOOLS)}
    ok = store.Meta(versions=dict(now))
    e = _entry(ok)
    assert runner.current_result(e, now) == e.result
    assert runner.current_result(_entry(store.Meta()), now) is None  # schema 1: unknown
    assert runner.current_result(_entry(dataclasses.replace(ok, contended=True)), now) is None
    for k in runner.RESULT_KEYS:
        assert runner.current_result(e, now | {k: "other"}) is None, k
    newer_mvtools = now | {runner.plugin_key(BackendId.MVTOOLS): "mvtools-2"}
    got = runner.current_result(e, newer_mvtools)
    assert got is not None and got.measurements
    assert all(m.backend is not BackendId.MVTOOLS for m in got.measurements)
    only_mvtools = _entry(ok, _m("mvtools", backend=BackendId.MVTOOLS))
    assert runner.current_result(only_mvtools, newer_mvtools) is None


def test_fingerprint_follows_the_file(tmp_path: Path) -> None:
    f = tmp_path / "mpv"
    assert runner.fingerprint(f) is None and runner.fingerprint(None) is None
    f.write_bytes(b"one")
    a = runner.fingerprint(f)
    f.write_bytes(b"three")
    assert a is not None and runner.fingerprint(f) not in (None, a)


def test_the_same_bytes_copied_again_keep_their_fingerprint(tmp_path: Path) -> None:
    """A rebuild or a fresh copy from the disk image changes every mtime and no
    byte: the benchmark must still count (it was thrown away, measured on an M2)."""
    f = tmp_path / "mpv"
    f.write_bytes(b"same build")
    before = runner.fingerprint(f)
    os.utime(f, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    assert before is not None and runner.fingerprint(f) == before
    f.write_bytes(b"other build")  # same size
    assert runner.fingerprint(f) not in (None, before)


def test_a_moved_bundle_keeps_its_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A translocated ButterEye.app (and an AppImage) runs from a new folder on
    every launch: the same files must give the same versions."""

    def bundle(root: Path) -> dict[str, str | None]:
        contents = root / "ButterEye.app" / "Contents"
        (contents / "Resources" / "lib").mkdir(parents=True)
        (contents / "MacOS").mkdir()
        mpv = contents / "MacOS" / "mpv"
        mpv.write_bytes(b"mpv")
        lib = contents / "Resources" / "lib" / "libMoltenVK.dylib"
        lib.write_bytes(b"mvk")
        icd = contents / "Resources" / "MoltenVK_icd.json"
        icd.write_text(json.dumps({"ICD": {"library_path": "lib/libMoltenVK.dylib"}}))
        for f in (mpv, lib, icd):
            os.utime(f, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        monkeypatch.setattr(osapi, "MACOS_APP", contents / "Resources")
        monkeypatch.setattr(osapi, "LINUX_APP", None)
        return {
            "mpv": runner.fingerprint(mpv),
            "vulkan": runner._vulkan_fingerprint({"VK_DRIVER_FILES": str(icd)}),
        }

    first = bundle(tmp_path / "AppTranslocation" / "1111" / "d")
    second = bundle(tmp_path / "AppTranslocation" / "2222" / "d")
    assert first == second and first["mpv"] is not None and first["vulkan"] is not None
    assert first["mpv"].startswith("<bundle>/MacOS/mpv:")
    # an AppImage at a new mount point
    monkeypatch.setattr(osapi, "MACOS_APP", None)
    seen = []
    for mount in ("a", "b"):
        root = tmp_path / f".mount_{mount}"
        (root / "usr" / "bin").mkdir(parents=True)
        exe = root / "usr" / "bin" / "vspipe"
        exe.write_bytes(b"vspipe")
        os.utime(exe, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
        monkeypatch.setattr(osapi, "LINUX_APP", root)
        seen.append(runner.fingerprint(exe))
    digest = hashlib.sha256(b"vspipe").hexdigest()[:16]
    assert seen[0] == seen[1] == f"<bundle>/usr/bin/vspipe:6:{digest}"
    # outside the bundle the path still counts (a Homebrew version directory)
    other = tmp_path / "Cellar" / "mpv"
    other.parent.mkdir()
    other.write_bytes(b"mpv")
    digest = hashlib.sha256(b"mpv").hexdigest()[:16]
    assert runner.fingerprint(other) == f"{os.path.realpath(other)}:3:{digest}"


def test_vulkan_fingerprint_names_the_moltenvk_library(tmp_path: Path) -> None:
    lib = tmp_path / "lib" / "libMoltenVK.dylib"
    lib.parent.mkdir()
    lib.write_bytes(b"mvk")
    icd = tmp_path / "MoltenVK_icd.json"
    icd.write_text(json.dumps({"ICD": {"library_path": "lib/libMoltenVK.dylib"}}))
    fp = runner._vulkan_fingerprint({"VK_DRIVER_FILES": str(icd)})
    assert fp is not None and str(lib.resolve()) in fp
    assert runner._vulkan_fingerprint({}) is None


async def test_history_leaves_out_results_from_other_software(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner, "VERSIONS_TTL_S", 0.0)
    core, _ = await _open(env)
    result = await core.bench(REQ).result()
    assert await core.bench_history() == (result,)
    _exe(env.fake / "bin" / "mpv", FAKE_MPV + "\n# a newer mpv\n")
    assert await core.bench_history() == ()  # re-measured by the next background run
    assert store.read(env.paths.bench_file) == (result,)  # still on file
    await core.close(cancel_jobs=True)


def test_store_reads_schema_1_and_writes_schema_2(tmp_path: Path) -> None:
    """Existing bench.json files stay readable: results kept, nothing recorded
    about them (``Meta()``), rewritten as schema 2 at the next write."""
    path = tmp_path / "bench.json"
    r = _result()
    store.write_atomic(path, [r])
    doc = json.loads(path.read_text())
    doc["schema"] = 1
    for x in doc["results"]:
        del x["meta"]
    path.write_text(json.dumps(doc))
    (entry,) = store.read_entries(path)
    assert entry == store.Entry(r, store.Meta())
    meta = store.Meta(
        versions={"app": "0.0.6", "mpv": None},
        on_battery=True,
        low_power=False,
        thermal_start=0,
        thermal_max=2,
        contended=False,
        sustained={"mvgpu": 61.5},
    )
    store.append(path, _result(gpu_uuid="new"), meta=meta)
    doc = json.loads(path.read_text())
    assert doc["schema"] == 2 == store.SCHEMA
    old, new = store.read_entries(path)
    assert old == store.Entry(r, store.Meta())
    assert new.meta == meta
    # meta is advisory: wrong types read as unknown, not as a corrupt file
    doc["results"][1]["meta"] = {"on_battery": "yes", "thermal_max": True, "sustained": [1]}
    path.write_text(json.dumps(doc))
    assert store.read_entries(path)[1].meta == store.Meta()
