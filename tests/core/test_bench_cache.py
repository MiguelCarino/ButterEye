# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The speed test runs once per hardware (§5.2, GUI.md §12): TensorRT is measured
when it is set up and not turned off, results are reused while the GPU that was
measured is still in the computer, and first-run setup never turns TensorRT off."""

from __future__ import annotations

import dataclasses
import types as pytypes
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.backends import trt
from buttereye.core.bench import runner
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId
from buttereye.gui.simple_window import current_results


def _ctx(trt_setting: bool | None, tmp_path: Path) -> Any:
    cfg = sc.default_config()
    cfg = dataclasses.replace(
        cfg, general=dataclasses.replace(cfg.general, trt_experimental=trt_setting)
    )
    return pytypes.SimpleNamespace(config=lambda: cfg, paths=sc.fake_paths(tmp_path))


@pytest.mark.parametrize(("setting", "measured"), [(None, True), (True, True), (False, False)])
def test_tensorrt_is_measured_unless_turned_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setting: bool | None, measured: bool
) -> None:
    inst = trt.TrtInstall(
        vstrt_dir=tmp_path, vsmlrt_dir=tmp_path, python_dirs=(), models_dir=tmp_path,
        trtexec=Path("/usr/bin/trtexec"), vstrt_version="v16.3.test1", trt_version="11.3.0",
    )  # fmt: skip
    monkeypatch.setattr(trt, "find_install", lambda paths, which=None: inst)
    monkeypatch.setattr(trt, "available_models", lambda i: frozenset({"rife-v4.26_ensembleFalse"}))
    cands = runner.trt_candidates(_ctx(setting, tmp_path))
    assert bool(cands) is measured
    assert all(c.backend is BackendId.RIFE_TRT for c in cands)


def test_tensorrt_not_measured_without_vstrt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(trt, "find_install", lambda paths, which=None: None)
    assert runner.trt_candidates(_ctx(None, tmp_path)) == ()


def test_results_are_reused_while_the_gpu_is_there() -> None:
    report = sc.report(())
    gpus = [d for d in report.hardware.vulkan if d.device_type != "cpu"]
    assert gpus, "the dev-box fixture has a GPU"
    here = gpus[0].uuid
    mine = dataclasses.replace(sc.bench_result_4090(), gpu_uuid=here.upper())  # case-insensitive
    other = dataclasses.replace(sc.bench_result_4090(), gpu_uuid="00000000-old-card")
    assert current_results((mine, other), report) == (mine,)
    assert current_results((other,), report) == ()  # a new graphics card: measure again
    assert current_results((other,), None) == (other,)  # no report yet: trust the history
    no_gpu = dataclasses.replace(report, hardware=dataclasses.replace(report.hardware, vulkan=()))
    cpu_only = dataclasses.replace(sc.bench_result_4090(), gpu_uuid=None)
    assert current_results((cpu_only, mine), no_gpu) == (cpu_only,)
