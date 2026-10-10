# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The experimental TensorRT backend without TensorRT (SCOPE §5.2, F12, spike M0(l)):
install discovery, engine keys and folders, script data, the template branch,
render engine choice and the doctor's vstrt version parsing."""

from __future__ import annotations

import dataclasses
import json
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core.backends import trt
from buttereye.core.doctor.probe import trt_version_of, version_text
from buttereye.core.render import probe as rprobe
from buttereye.core.scriptgen import generator
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId

M26 = "rife-v4.26_ensembleFalse"


def _install_tree(tmp_path: Path, *, onnx: bool = True, python: bool = True) -> object:
    paths = sc.fake_paths(tmp_path)
    vstrt = paths.data_dir / "plugins" / "vstrt" / "v16.3.test1"
    vstrt.mkdir(parents=True)
    (vstrt / "libvstrt.so").write_bytes(b"")
    (vstrt / "vsmlrt.py").write_text("")
    (vstrt / "VERSION").write_text("v16.3.test1-0-g44033da\n")
    if onnx:
        models = paths.data_dir / "models" / "vsmlrt" / "rife_v2"
        models.mkdir(parents=True)
        (models / "rife_v4.26.onnx").write_bytes(b"onnx")
    if python:
        (paths.data_dir / "python" / "onnx").mkdir(parents=True)
    return paths


@pytest.fixture
def trt_libs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trt, "trt_version", lambda: "11.3.0")


def test_find_install_reads_the_user_tree(tmp_path: Path, trt_libs: None) -> None:
    paths = _install_tree(tmp_path)
    inst = trt.find_install(paths, which=lambda _n: "/usr/bin/trtexec")  # type: ignore[arg-type]
    assert inst is not None
    assert inst.vstrt_version == "v16.3.test1-0-g44033da"
    assert inst.trt_version == "11.3.0"
    assert inst.python_dirs == (paths.data_dir / "python",)  # type: ignore[attr-defined]
    assert trt.available_models(inst) == {M26}


@pytest.mark.parametrize(
    ("kw", "word"),
    [({"onnx": False}, "RIFE models"), ({"python": False}, "onnxconverter-common")],
)
def test_problems_name_what_is_missing(
    tmp_path: Path, trt_libs: None, kw: dict[str, bool], word: str
) -> None:
    paths = _install_tree(tmp_path, **kw)
    probs = trt.problems(paths, which=lambda _n: "/usr/bin/trtexec")  # type: ignore[arg-type]
    assert len(probs) == 1 and word in probs[0]
    assert trt.find_install(paths, which=lambda _n: "/usr/bin/trtexec") is None  # type: ignore[arg-type]


def test_no_vstrt_and_no_trtexec(tmp_path: Path, trt_libs: None) -> None:
    paths = sc.fake_paths(tmp_path)
    probs = trt.problems(paths, which=lambda _n: None)
    assert any("contrib/build-vstrt.sh" in p for p in probs)


def test_engine_key_folder_changes_with_every_field() -> None:
    base = trt.EngineKey("GPU-1", "615.71.09", "v16", "11.3.0", M26, 1920, 1080)
    names = {base.folder_name()}
    for change in (
        {"gpu_uuid": "GPU-2"}, {"driver": "620.1"}, {"vstrt_version": "v17"},
        {"trt_version": "11.4.0"}, {"width": 1280}, {"fp16": False}, {"half_io": False},
        {"model": "rife-v4.22_lite_ensembleFalse"},
    ):  # fmt: skip
        names.add(dataclasses.replace(base, **change).folder_name())  # type: ignore[arg-type]
    assert len(names) == 9
    assert base.folder_name().startswith("v4_26-1920x1080-fp16-h-")


def test_ready_needs_marker_and_engine(tmp_path: Path) -> None:
    assert not trt.is_ready(tmp_path)
    (tmp_path / "a.engine").write_bytes(b"")
    assert not trt.is_ready(tmp_path)
    (tmp_path / trt.READY).write_text("{}")
    assert trt.is_ready(tmp_path)


def test_streams_follow_frame_size() -> None:
    assert trt.streams_for(1920, 1080) == 2
    assert trt.streams_for(2560, 1440) == 4
    assert trt.streams_for(3840, 2160) == 4


def _inst(tmp_path: Path) -> trt.TrtInstall:
    return trt.TrtInstall(
        tmp_path / "vstrt", tmp_path / "vstrt", (tmp_path / "py",), tmp_path / "onnx",
        Path("/usr/bin/trtexec"), "v16.3.test1", "11.3.0",
    )  # fmt: skip


def test_script_data_is_json_only_and_build_flagged(tmp_path: Path) -> None:
    key = trt.EngineKey("GPU-1", "615", "v16", "11.3.0", M26, 1920, 1080)
    settings = trt.script_settings(_inst(tmp_path), key, tmp_path / "e", streams=2, build=False)
    params = generator.FilterParams(
        gen=3, backend=BackendId.RIFE_TRT, src_fps=Fraction(24000, 1001),
        target_fps=Fraction(48000, 1001), buffered_frames=4, concurrent_frames=8,
        sc_threshold=0.12, trt=settings,
    )  # fmt: skip
    data = json.loads(generator.user_data(params))
    assert data["backend"] == "rife-trt"
    assert data["trt"]["model"] == "v4_26" and data["trt"]["build"] is False
    assert data["trt"]["engine_dir"] == str(tmp_path / "e")
    assert "model_path" not in data and "gpu_thread" not in data
    # the per-script code stays the data block only (§4.4)
    text = generator.render_script()
    assert str(tmp_path) not in text


def test_build_user_data_is_a_blank_clip_in_build_mode(tmp_path: Path) -> None:
    key = trt.EngineKey("GPU-1", "615", "v16", "11.3.0", M26, 1280, 720)
    data = json.loads(trt.build_user_data(_inst(tmp_path), key, tmp_path / "e"))
    assert data["source"] == {"kind": "blank", "width": 1280, "height": 720,
                              "fps": [24000, 1001], "length": 2}  # fmt: skip
    assert data["trt"]["build"] is True


def test_rife_trt_needs_its_settings() -> None:
    params = generator.FilterParams(
        gen=1, backend=BackendId.RIFE_TRT, src_fps=Fraction(24), target_fps=Fraction(48),
        buffered_frames=4, concurrent_frames=8,
    )  # fmt: skip
    with pytest.raises(ValueError, match="FilterParams.trt"):
        generator.user_data(params)


def test_template_branch_keeps_the_m0l_fixes() -> None:
    text = generator.template_text()
    body = text[text.index("def _rife_trt(") :]
    load = body.index("core.std.LoadPlugin(os.path.join(trt['vstrt_dir'], 'libvstrt.so'))")
    assert load < body.index("import vsmlrt")  # vstrt before vsmlrt
    assert "_implementation=2" in body
    assert "vsmlrt.convert_model = _fp16_model" in body
    assert "sys.dont_write_bytecode = True" in body
    assert "node_block_list=casts" in text
    assert "not built yet" in body  # fails fast inside mpv
    assert "elif backend == 'rife-trt':" in text


@pytest.mark.parametrize(
    ("backend", "installed", "expected"),
    [
        ("auto", {BackendId.RIFE_TRT, BackendId.RIFE_NCNN}, BackendId.RIFE_TRT),
        ("auto", {BackendId.RIFE_NCNN, BackendId.MVTOOLS}, BackendId.RIFE_NCNN),
        (BackendId.RIFE_TRT, {BackendId.RIFE_NCNN, BackendId.MVTOOLS}, BackendId.RIFE_NCNN),
        (BackendId.RIFE_NCNN, {BackendId.RIFE_TRT, BackendId.MVTOOLS}, BackendId.MVTOOLS),
        (BackendId.MVTOOLS, set(), None),
    ],
)
def test_render_engine_choice(
    backend: object, installed: set[BackendId], expected: BackendId | None
) -> None:
    # a pinned engine that isn't installed falls back instead of failing the copy,
    # but never to a TensorRT the profile didn't ask for
    assert rprobe.engine_for(backend, frozenset(installed)) is expected  # type: ignore[arg-type]


def test_vstrt_version_parsing() -> None:
    m = {"version": "v16.3.test1-0-g44033da", "tensorrt_version": "110300"}
    assert trt_version_of(m) == (11, 3)
    assert trt_version_of("{'tensorrt_version': b'110300', 'version': b'v16'}") == (11, 3)
    assert trt_version_of({"tensorrt_version": "8601"}) == (8, 6)
    assert trt_version_of({}) is None
    assert version_text(m) == "v16.3.test1-0-g44033da (TensorRT 11.3)"
