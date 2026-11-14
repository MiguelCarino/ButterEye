# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The generated script through vspipe with the real engines, on synthetic
flat-shaded 2D clips: hold fill turns drawings on twos/threes into even motion,
leaves motion on ones and live-action-like footage byte for byte as it was, and
an engine repeats the frame at a cut the relative rule finds (a same-backdrop
cut the absolute threshold misses). The clip is drawn by a small wrapper
script that hands it to the template as mpv would (``video_in``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import paths as paths_mod
from buttereye.core.backends import trt
from buttereye.core.scriptgen import generator as g

np = pytest.importorskip("numpy")

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

W, H, N = 320, 192, 36
MODEL = Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse")

WRAPPER = r"""
import json
import numpy as np
import vapoursynth as vs
core = vs.core
W, H, N = %(W)d, %(H)d, %(N)d
SKY, GROUND, INK, FILL = (170, 128, 120), (110, 110, 120), (16, 128, 128), (150, 60, 200)
yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
rng = np.random.default_rng(3)
tex = rng.integers(40, 200, (H, W + 4 * N)).astype(np.float64)

def draw(x, bg=0.0):
    planes = [np.empty((H, W)) for _ in range(3)]
    for p in range(3):
        planes[p][:] = SKY[p]
        planes[p][yy >= 130] = GROUND[p]
        planes[p][(yy > 90) & (yy < 130) & (((xx + bg) %% 32) < 3)] = INK[p]
        d = np.hypot(xx - x, yy - 100)
        planes[p][d < 25] = INK[p]
        planes[p][d < 22] = FILL[p]
    return planes

def frame(k):
    if KIND == 'live':  # textured pan, fresh grain every frame: nothing repeats
        planes = draw(60 + 5 * k)
        planes[0] = tex[:, 3 * k:3 * k + W] + rng.normal(0, 2, (H, W))
        return planes
    if KIND == 'mixed':  # the backdrop pans on every frame, the disc is drawn on threes
        return draw(60 + 12 * (k // 3), bg=4.0 * k)
    if KIND == 'cut':  # one backdrop, two shots
        return draw(60 + 3 * k) if k < 15 else draw(260 - 3 * k, bg=9)
    hold = {'ones': 1, 'twos': 2, 'threes': 3}[KIND]
    return draw(60 + 4 * hold * (k // hold))

frames = [frame(k) for k in range(N)]
base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, length=N, fpsnum=24, fpsden=1)

def paint(n, f):
    out = f.copy()
    y, u, v = frames[n]
    np.asarray(out[0])[:] = np.clip(y + 0.5, 0, 255).astype(np.uint8)
    np.asarray(out[1])[:] = np.clip(u[::2, ::2] + 0.5, 0, 255).astype(np.uint8)
    np.asarray(out[2])[:] = np.clip(v[::2, ::2] + 0.5, 0, 255).astype(np.uint8)
    return out

KIND = KIND.decode() if isinstance(KIND, bytes) else KIND
SCRIPT = SCRIPT.decode() if isinstance(SCRIPT, bytes) else SCRIPT
env = {'user_data': user_data, 'video_in': core.std.ModifyFrame(base, base, paint),
       '__name__': '__vapoursynth__'}
exec(compile(open(SCRIPT).read(), SCRIPT, 'exec'), env)
""" % {"W": W, "H": H, "N": N}


@pytest.fixture(scope="module")
def scripts(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("vspipe") is None:
        pytest.skip("vspipe missing")
    folder = tmp_path_factory.mktemp("template")
    script = folder / "gen.vpy"
    g.write_script(script)
    wrapper = folder / "clip.vpy"
    wrapper.write_text(WRAPPER, encoding="utf-8")
    return script, wrapper


def _render(scripts: tuple[Path, Path], backend: str, kind: str, **cfg: Any) -> list[Any]:
    script, wrapper = scripts
    data: dict[str, Any] = {"v": 1, "gen": 1, "backend": backend, "src_fps": [24, 1],
                            "target_fps": [48, 1], "matrix": "709", "range": "limited",
                            "sc_threshold": 0.12, "title": kind}  # fmt: skip
    if backend == "rife-ncnn":
        if not MODEL.is_dir():
            pytest.skip("RIFE models not installed")
        data.update(model_path=os.fspath(MODEL), gpu_thread=4)
    if backend == "rife-trt":
        data.update(trt=_trt(script.parent, cfg.pop("model", trt.DEFAULT_MODEL)))
    frames = cfg.pop("frames", 2 * N)
    data.update(cfg)
    res = subprocess.run(
        ["vspipe", "-c", "y4m", "-a", "user_data=" + json.dumps(data), "-a", f"KIND={kind}",
         "-a", f"SCRIPT={script}", os.fspath(wrapper), "-"],
        capture_output=True, timeout=300, check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr.decode(errors="replace")[-2000:]
    raw = res.stdout
    size = W * H * 3 // 2
    pos = raw.index(b"\n") + 1
    out = []
    while pos < len(raw):
        pos = raw.index(b"\n", pos) + 1
        out.append(np.frombuffer(raw, np.uint8, size, pos))
        pos += size
    assert len(out) == frames
    return out


def _trt(folder: Path, model: str = trt.DEFAULT_MODEL) -> dict[str, Any]:
    """The user's TensorRT setup with an engine folder of this test's own: the
    script builds the small engine there on first use (about half a minute)."""
    paths = paths_mod.resolve()
    if trt.problems(paths):
        pytest.skip("TensorRT is not set up")
    inst = trt.find_install(paths)
    assert inst is not None
    if model not in trt.available_models(inst):
        pytest.skip(f"{model} is not installed")
    key = trt.EngineKey("test", "test", inst.vstrt_version, inst.trt_version, model, W, H)
    engines = folder / ("engines-" + model)
    engines.mkdir(exist_ok=True)
    return trt.script_settings(inst, key, engines, streams=2, build=True)


def _step_cv(frames: list[Any]) -> float:
    """How uneven the disc's motion is per output frame: 0 = perfectly even,
    ~1 = still, still, still, jump (from its fill colour in the V plane)."""
    xs = []
    for f in frames:
        v = f[W * H * 5 // 4 :].reshape(H // 2, W // 2)
        cols = np.nonzero(v > 180)[1]
        xs.append(float(cols.mean()) if cols.size else float("nan"))
    steps = np.diff(xs)[: 2 * N - 8]  # the last drawing has nothing to move to
    return float(np.std(steps) / np.mean(steps))


ENGINES = [
    pytest.param("mvtools", id="mvtools"),
    pytest.param("rife-ncnn", id="rife-ncnn", marks=pytest.mark.gpu),
    pytest.param("mvgpu", id="mvgpu", marks=pytest.mark.gpu),
    pytest.param("rife-trt", id="rife-trt", marks=pytest.mark.gpu),
]


@pytest.mark.parametrize("backend", ENGINES)
def test_hold_fill_evens_out_twos_and_threes(scripts: tuple[Path, Path], backend: str) -> None:
    for kind in ("twos", "threes"):
        off = _step_cv(_render(scripts, backend, kind))
        on = _step_cv(_render(scripts, backend, kind, hold_fill=True))
        assert off > 0.9, (kind, off)  # every drawing: still, still, ..., jump
        if backend == "mvgpu":
            # its block search still loses much of a flat object's motion
            # across a whole drawing (measured: twos 1.13 -> 0.64, threes 1.9 -> 1.7)
            assert on < off * (0.75 if kind == "twos" else 1.0), (kind, on, off)
        else:
            assert on < 0.15, (kind, on)  # measured 0.01-0.05


@pytest.mark.parametrize("backend", ENGINES)
def test_hold_fill_changes_nothing_without_held_drawings(
    scripts: tuple[Path, Path], backend: str
) -> None:
    for kind in ("ones", "live"):
        off = _render(scripts, backend, kind)
        on = _render(scripts, backend, kind, hold_fill=True)
        assert all(np.array_equal(a, b) for a, b in zip(off, on, strict=True)), kind


@pytest.mark.parametrize("backend", ENGINES)
def test_engines_repeat_the_frame_at_a_same_backdrop_cut(
    scripts: tuple[Path, Path], backend: str
) -> None:
    out = _render(scripts, backend, "cut")
    # the slot between source frames 14 and 15 (the cut) repeats frame 14
    assert np.array_equal(out[29], out[28])
    assert not np.array_equal(out[27], out[26])  # an ordinary pair is interpolated


@pytest.mark.gpu
def test_cascade_passes_rife_frames_through_and_repeats_at_a_cut(
    scripts: tuple[Path, Path],
) -> None:
    """RIFE (TensorRT) to 48, mvgpu to 144 (docs/spikes/m0u.md): the source frames
    and RIFE's frames come out as they were, mvgpu fills the two slots between
    each, and at a cut every slot repeats the frame before it."""
    if not (g.DEFAULT_CONSTANTS.plugin_dir / "libmvgpu.so").is_file():
        pytest.skip("mvgpu plugin not installed")
    rife = _render(scripts, "rife-trt", "cut")
    out = _render(scripts, "rife-trt", "cut", target_fps=[144, 1], cascade_fps=[48, 1],
                  frames=6 * N)  # fmt: skip
    for k in (3, 10, 20, 30):
        assert np.array_equal(out[6 * k], rife[2 * k])  # a source frame
        assert np.array_equal(out[6 * k + 3], rife[2 * k + 1])  # RIFE's frame
        for j in (1, 2, 4, 5):  # mvgpu's: between its neighbours, not a copy of one
            assert not np.array_equal(out[6 * k + j], out[6 * k])
            assert not np.array_equal(out[6 * k + j], out[6 * k + 3])
    # source frames 14 and 15 are two shots: frame 14 six times, then frame 15
    for j in range(1, 6):
        assert np.array_equal(out[84 + j], out[84])
    assert not np.array_equal(out[90], out[84])


@pytest.mark.parametrize("backend", ENGINES)
def test_still_skip_is_invisible(scripts: tuple[Path, Path], backend: str) -> None:
    off = _render(scripts, backend, "twos")
    on = _render(scripts, backend, "twos", still_skip=True)
    for a, b in zip(off, on, strict=True):
        err = float(np.mean((a.astype(np.float64) - b) ** 2))
        assert err == 0 or 10 * np.log10(255**2 / err) > 45


# ---- DRBA (contrib/build-drba.sh): a model for 2D animation on TensorRT
DRBA = [pytest.param("drba-v1", id="v1"), pytest.param("drba-v2_lite", id="v2_lite")]


def _disc_steps(frames: list[Any]) -> Any:
    xs = []
    for f in frames:
        v = f[W * H * 5 // 4 :].reshape(H // 2, W // 2)
        xs.append(2 * float(np.nonzero(v > 180)[1].mean()))
    return np.diff(xs)[: len(frames) - 8]


def _backdrop_steps(frames: list[Any]) -> Any:
    """The backdrop's stripes (32 px apart) from their phase in rows the disc
    never reaches."""
    wave = np.exp(-2j * np.pi * np.arange(W) / 32)
    phase = []
    for f in frames:
        row = f[: W * H].reshape(H, W)[126:130].mean(0).astype(np.float64)
        phase.append(np.angle(np.sum((row - row.mean()) * wave)))
    return (np.diff(np.unwrap(phase)) * 32 / (2 * np.pi))[: len(frames) - 8]


@pytest.mark.gpu
@pytest.mark.parametrize("model", DRBA)
def test_drba_smooths_the_backdrop_and_keeps_the_drawing(
    scripts: tuple[Path, Path], model: str
) -> None:
    out = _render(scripts, "rife-trt", "mixed", model=model)
    # the backdrop, 4 px a source frame: about 2 px on every output frame
    backdrop = _backdrop_steps(out)
    assert abs(float(backdrop.mean()) - 2.0) < 0.1 and float(backdrop.std()) < 1.2
    # the disc, 12 px every third frame: still for four output frames in six,
    # as it was drawn (RIFE spreads the step over two output frames)
    disc = np.abs(_disc_steps(out))
    assert float(np.mean(disc < 0.5)) > 0.6
    assert abs(float(disc.sum()) / len(disc) - 2.0) < 0.3  # and it arrives


@pytest.mark.gpu
@pytest.mark.parametrize("model", DRBA)
def test_drba_at_a_cut_and_at_two_and_a_half_times(scripts: tuple[Path, Path], model: str) -> None:
    out = _render(scripts, "rife-trt", "cut", model=model)
    assert np.array_equal(out[29], out[28])  # the slot across the cut repeats frame 14
    assert not np.array_equal(out[27], out[26])
    # every second source frame is an output frame at 2.5x, and stays the source's
    plain = _render(scripts, "rife-trt", "mixed", model=model)
    out = _render(scripts, "rife-trt", "mixed", model=model, target_fps=[60, 1],
                  frames=N * 5 // 2)  # fmt: skip
    assert len(out) == 90
    for k in (2, 8, 20):  # source frame k is output frame 2.5 k
        err = float(np.mean((out[k * 5 // 2].astype(np.float64) - plain[2 * k]) ** 2))
        assert err < 2.0, (k, err)
    backdrop = _backdrop_steps(out)
    assert abs(float(backdrop.mean()) - 1.6) < 0.1
