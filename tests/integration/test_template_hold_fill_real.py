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
    assert len(out) == 2 * N
    return out


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


@pytest.mark.parametrize("backend", ENGINES)
def test_still_skip_is_invisible(scripts: tuple[Path, Path], backend: str) -> None:
    off = _render(scripts, backend, "twos")
    on = _render(scripts, backend, "twos", still_skip=True)
    for a, b in zip(off, on, strict=True):
        err = float(np.mean((a.astype(np.float64) - b) ** 2))
        assert err == 0 or 10 * np.log10(255**2 / err) > 45
