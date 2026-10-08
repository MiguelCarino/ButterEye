# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U11 benchmark against the real vspipe, mpv and the installed buttereye-* plugins.

Kept short: MVTools at 640x360 (CPU), and one RIFE-ncnn model at 1280x720 on the
GPU. The XDG dirs are temporary; the plugin/model directories are the real RPM
ones, narrowed to one model through a temporary symlink tree.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import types as pytypes
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.bench import faults, measure, runner
from buttereye.core.events import Progress
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId, BenchRequest, Config, ConfigLoad, Paths

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

RPM_PLUGINS = Path("/usr/lib64/buttereye/vapoursynth")
RPM_MODELS = Path("/usr/share/buttereye/rife-ncnn-models")
MODEL = "rife-v4.22_lite_ensembleFalse"


def _paths(tmp: Path, models: tuple[str, ...]) -> Paths:
    data = tmp / "rpm-share"
    (data / runner.MODEL_SUBDIR).mkdir(parents=True)
    for m in models:
        (data / runner.MODEL_SUBDIR / m).symlink_to(RPM_MODELS / m)
    return dataclasses.replace(
        sc.fake_paths(tmp / "xdg"), rpm_plugin_dir=RPM_PLUGINS, rpm_data_dir=data
    )


@pytest.fixture
def config_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config provider stand-in: these tests are about measuring, not config files."""

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(sc.default_config(), exists=False)

    def save(paths: Paths, cfg: Config, *, expected_revision: str) -> str:
        raise AssertionError("bench must not save the config")

    monkeypatch.setitem(
        sys.modules,
        "buttereye.core.profiles.config",
        pytypes.SimpleNamespace(load=load, save=save),
    )


async def test_vspipe_runs_the_shipped_script_mvtools(tmp_path: Path) -> None:
    """The template itself, through real vspipe (MVTools, CPU, tiny)."""
    tools = runner.Tools("vspipe", None, runner.VPY, RPM_PLUGINS, RPM_MODELS, vram=False)
    cand = runner.Candidate("mvtools", BackendId.MVTOOLS, None, None, 4)
    data = runner.user_data(
        cand,
        tools,
        width=320,
        height=180,
        source=Fraction(24),
        target=Fraction(48),
        src_frames=26,
        gpu_id=None,
    )

    class _Op:
        async def spawn(self, argv: list[str], **kw: object) -> object:
            from buttereye.core.ops import spawn_group

            return await spawn_group(argv, **kw)  # type: ignore[arg-type]

    argv = measure.vspipe_argv("vspipe", runner.VPY, data, 48)
    run = await measure.run_vspipe(_Op(), argv, timeout_s=60)  # type: ignore[arg-type]
    assert run.frames == 48 and run.fps > 0 and run.eval_s is not None


async def test_bench_mvtools_real(tmp_path: Path, config_stub: None) -> None:
    paths = _paths(tmp_path, ())  # no models -> MVTools only
    core = await ButterEye.open(paths)
    progress: list[Progress] = []
    op = core.bench(BenchRequest(640, 360, Fraction(24000, 1001)))
    op.add_progress_sink(progress.append)
    result = await op.result()
    (m,) = result.measurements
    assert m.label == "mvtools" and m.backend is BackendId.MVTOOLS
    assert m.vspipe_fps > 48 and m.mpv_fps > 48  # 360p MVTools is far above 2x real time
    assert m.realtime and result.recommended == "mvtools"
    assert 0 < m.startup_s < 30 and m.reload_s >= 0
    assert m.cov < 1
    assert progress[-1].done == progress[-1].total
    doc = json.loads(paths.bench_file.read_text())
    assert doc["results"][0]["measurements"][0]["label"] == "mvtools"
    await core.close(cancel_jobs=True)


@pytest.mark.gpu
async def test_bench_rife_one_model_real(
    tmp_path: Path, config_stub: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One RIFE-ncnn model at 720p, gpu_thread=4 (M0(f): ~72 fps at 1080p 2x on the 4090)."""
    if not (RPM_MODELS / MODEL).is_dir():
        pytest.skip(f"{MODEL} not installed")
    monkeypatch.setattr(runner, "MVTOOLS_PLUGIN", "absent-in-this-test.so")
    paths = _paths(tmp_path, (MODEL,))
    core = await ButterEye.open(paths)
    result = await core.bench(BenchRequest(1280, 720, Fraction(24000, 1001))).result()
    (m,) = result.measurements
    assert m.label == "rife-v4.22-lite" and m.backend is BackendId.RIFE_NCNN
    assert m.vspipe_fps > 30, m  # far below what the 4090 does at 720p
    assert m.mpv_fps > 0
    assert m.reload_s > 0.1  # Vulkan device + model creation happen at script evaluation
    if faults._scanner() is not None:
        # U3 scanner present: an int when the journal is readable, never a fake 0
        assert m.gpu_faults is None or m.gpu_faults >= 0
    else:
        assert m.gpu_faults is None
    if m.gpu_faults:
        assert result.recommended is None
    await core.close(cancel_jobs=True)
