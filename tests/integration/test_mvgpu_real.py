# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The mvgpu Vulkan plugin (spike M0(r)) on a real GPU: a textured square moving
16 px per frame must come out shifted 8 px in the in-between frame; source frames
pass through untouched; frame counts follow the rate; a scene-change mark repeats
the frame; a change in brightness between the pair does not move the square.
Motion of an odd number of pixels a frame (slow pans) lands between the pixels,
and a flat bar on a flat background keeps its own motion. Runs in the
system Python (VapourSynth) against a built plugin: $MVGPU_PLUGIN, else the dev
build in ~/.cache/buttereye-dev/mvgpu-build."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi

pytestmark = [pytest.mark.integration]

#: a locally built plugin carries this platform's extension, like any other
PLUGIN = Path(
    os.environ.get(
        "MVGPU_PLUGIN",
        Path.home() / ".cache/buttereye-dev/mvgpu-build" / osapi.plugin_file("libmvgpu.so"),
    )
)

SCRIPT = r"""
import json, sys
import numpy as np
import vapoursynth as vs
core = vs.core
core.std.LoadPlugin(sys.argv[1])
W, H, STEP, SIDE = 640, 368, 16, 96
rng = np.random.default_rng(7)
tex = (rng.random((SIDE, SIDE)) * 160 + 48).astype(np.uint8)  # a textured square

def frame_y(k, shift=0.0):
    y = np.full((H, W), 32, np.uint8)
    x0 = int(round(100 + STEP * k + shift))
    y[136:136 + SIDE, x0:x0 + SIDE] = tex
    return y

base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, fpsnum=24, fpsden=1,
                          length=6, color=[32, 128, 128])

def paint(n, f):
    out = f.copy()
    np.asarray(out[0])[:] = frame_y(n)
    if n == 4:
        out.props["_SceneChangeNext"] = 1
    return out

src = core.std.ModifyFrame(base, base, paint)
x2 = core.mvgpu.Interpolate(src, fps_num=48, fps_den=1)
x5 = core.mvgpu.Interpolate(src, fps_num=120, fps_den=1)
mid = np.asarray(x2.get_frame(3)[0]).astype(np.float64)  # between source 1 and 2
want = frame_y(1, STEP / 2).astype(np.float64)
blend = (frame_y(1).astype(np.float64) + frame_y(2)) / 2
def err(a, b):
    return float(np.mean(np.abs(a[136:136 + SIDE] - b[136:136 + SIDE])))
errors = []
for _ in range(20):  # a failed construction cleans up and fails the same way each time
    try:
        core.mvgpu.Interpolate(base, fps_num=48, fps_den=1, device=99)
    except vs.Error as e:
        errors.append(str(e))
print(json.dumps({
    "version": list(core.mvgpu.version), "errors": sorted(set(errors)), "n_errors": len(errors),
    "len2": x2.num_frames, "len5": x5.num_frames,
    "fps2": [x2.fps.numerator, x2.fps.denominator],
    "mid_err": err(mid, want), "blend_err": err(blend, want),
    "source_kept": bool(np.array_equal(np.asarray(x2.get_frame(2)[0]), frame_y(1))),
    "scene_repeat": bool(np.array_equal(np.asarray(x2.get_frame(9)[0]), frame_y(4))),
}))
"""

#: the in-between frame of pans whose truth is known at any sub-pixel position: mean
#: |error| against it, against a 50/50 blend of the two frames and a repeat of the first
MOTION_SCRIPT = r"""
import json, sys
import numpy as np
import vapoursynth as vs
core = vs.core
core.std.LoadPlugin(sys.argv[1])
W, H = 640, 368
xx = np.arange(W, dtype=np.float64)[None, :].repeat(H, 0)
yy = np.arange(H, dtype=np.float64)[:, None].repeat(W, 1)
rng = np.random.default_rng(7)
WAVES = [(rng.uniform(0, 2 * np.pi), 2 * np.pi / rng.uniform(6, 90), rng.uniform(0, 2 * np.pi))
         for _ in range(48)]

def texture(pos):  # band-limited random texture moved right by pos px (exact at any pos)
    x = xx - pos
    return 128 + sum(9 * np.sin(k * (x * np.cos(a) + yy * np.sin(a)) + ph) for a, k, ph in WAVES)

def bar(pos):  # a dark 24 px bar on flat grey moved right by pos px
    return np.where((xx >= 300 + pos) & (xx < 324 + pos), 40.0, 160.0)

def clip(fn):
    base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, fpsnum=24, fpsden=1,
                              length=6, color=[32, 128, 128])
    def paint(n, f):
        out = f.copy()
        np.asarray(out[0])[:] = np.clip(np.rint(fn(float(n))), 0, 255).astype(np.uint8)
        return out
    return core.std.ModifyFrame(base, base, paint)

def mid(fn, step):  # output frame 5 at 2x: half way from source 2 to source 3
    src = clip(lambda k: fn(step * k))
    out = core.mvgpu.Interpolate(src, fps_num=48, fps_den=1)
    got = np.asarray(out.get_frame(5)[0]).astype(np.float64)
    want, a, c = (np.rint(fn(step * k)) for k in (2.5, 2, 3))
    inner = (slice(16, -16), slice(16, -16))
    err = lambda x: float(np.mean(np.abs(x - want)[inner]))
    return {"err": err(got), "blend": err((a + c) / 2), "repeat": err(a)}

print(json.dumps({"pan1": mid(texture, 1), "pan3": mid(texture, 3), "bar4": mid(bar, 4)}))
"""


#: the moving square with every odd source frame brightened by LIFT: the in-between
#: frame must still put the square half way, at half the brightness change
FLASH_SCRIPT = r"""
import json, sys
import numpy as np
import vapoursynth as vs
core = vs.core
core.std.LoadPlugin(sys.argv[1])
W, H, STEP, SIDE, LIFT = 640, 368, 16, 96, 40.0
rng = np.random.default_rng(7)
tex = (rng.random((SIDE, SIDE)) * 160 + 48).astype(np.uint8).astype(np.float64)

def frame_y(k, shift=0.0, lift=0.0):
    y = np.full((H, W), 32.0)
    x0 = int(round(100 + STEP * k + shift))
    y[136:136 + SIDE, x0:x0 + SIDE] = tex
    return y + lift

base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, fpsnum=24, fpsden=1,
                          length=6, color=[32, 128, 128])

def paint(n, f):
    out = f.copy()
    np.asarray(out[0])[:] = frame_y(n, lift=LIFT if n % 2 else 0.0).astype(np.uint8)
    return out

src = core.std.ModifyFrame(base, base, paint)
x2 = core.mvgpu.Interpolate(src, fps_num=48, fps_den=1)
mid = np.asarray(x2.get_frame(3)[0]).astype(np.float64)  # between source 1 (lifted) and 2
want = frame_y(1, STEP / 2, LIFT / 2)
blend = (frame_y(1, lift=LIFT) + frame_y(2)) / 2
def err(a, b):
    return float(np.mean(np.abs(a[136:136 + SIDE] - b[136:136 + SIDE])))
print(json.dumps({"mid_err": err(mid, want), "blend_err": err(blend, want)}))
"""


def _meson_version() -> list[int]:
    """The plugin version mvgpu/meson.build gives the build, as VapourSynth reports it."""
    text = (Path(__file__).resolve().parents[2] / "mvgpu" / "meson.build").read_text()
    m = re.search(r"^project\('mvgpu'.*version: '(\d+)\.(\d+)\.(\d+)'", text, re.M)
    assert m is not None
    return [int(m[1]), int(m[2]) * 100 + int(m[3])]


def _run(tmp_path: Path, name: str, code: str) -> dict[str, Any]:
    script = tmp_path / name
    script.write_text(code)
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", os.fspath(script), os.fspath(PLUGIN)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-2000:]
    result: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return result


@pytest.mark.skipif(not PLUGIN.is_file(), reason=f"mvgpu plugin not built ({PLUGIN})")
@pytest.mark.skipif(shutil.which("python3") is None, reason="needs the system Python")
def test_mvgpu_moves_a_square_and_keeps_sources(tmp_path: Path) -> None:
    r = _run(tmp_path, "mvgpu_check.py", SCRIPT)
    assert (r["len2"], r["len5"], r["fps2"]) == (12, 30, [48, 1])
    assert r["version"] == _meson_version(), "x.y.z is reported as (x, y * 100 + z) (meson.build)"
    assert r["n_errors"] == 20 and r["errors"] == ["mvgpu.Interpolate: no Vulkan device 99"]
    assert r["source_kept"], "source positions must be the source frames, untouched"
    assert r["scene_repeat"], "a frame marked _SceneChangeNext is repeated"
    # the square lands where it should: far closer than a blend of the two frames
    assert r["mid_err"] < 0.15 * r["blend_err"], r


@pytest.mark.skipif(not PLUGIN.is_file(), reason=f"mvgpu plugin not built ({PLUGIN})")
@pytest.mark.skipif(shutil.which("python3") is None, reason="needs the system Python")
def test_mvgpu_odd_pixel_motion_and_flat_edges(tmp_path: Path) -> None:
    r = _run(tmp_path, "mvgpu_motion.py", MOTION_SCRIPT)
    # 1 and 3 px a frame put the in-between half a pixel off the source grid. Motion
    # in even pixels only matched them to 0/2 or 2/4 px: no better than a plain blend
    # at 1 px (measured 0.52 vs 0.43), 0.17 of the blend's error at 3 px
    assert r["pan1"]["err"] < 0.75 * r["pan1"]["blend"], r
    assert r["pan3"]["err"] < 0.12 * r["pan3"]["blend"], r
    # a flat bar on flat grey is seen by one column of blocks per edge: a plain
    # vector median outvoted it with the still background's vectors (error as large
    # as repeating the frame)
    assert r["bar4"]["err"] < 0.1 * r["bar4"]["repeat"], r


@pytest.mark.skipif(not PLUGIN.is_file(), reason=f"mvgpu plugin not built ({PLUGIN})")
@pytest.mark.skipif(shutil.which("python3") is None, reason="needs the system Python")
def test_mvgpu_brightness_change(tmp_path: Path) -> None:
    r = _run(tmp_path, "mvgpu_flash.py", FLASH_SCRIPT)
    # a fade or a flash shifts the whole frame's brightness between the pair. Without
    # taking that out of every A - C difference, the half-pixel refinement drifted
    # the vectors (measured 1.28 vs 0.04 with it)
    assert r["mid_err"] < 0.02 * r["blend_err"], r
