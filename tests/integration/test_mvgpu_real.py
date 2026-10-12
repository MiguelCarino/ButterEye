# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The mvgpu Vulkan plugin (spike M0(r)) on a real GPU: a textured square moving
16 px per frame must come out shifted 8 px in the in-between frame; source frames
pass through untouched; frame counts follow the rate; a scene-change mark repeats
the frame. Runs in the system Python (VapourSynth) against a built plugin:
$MVGPU_PLUGIN, else the dev build in ~/.cache/buttereye-dev/mvgpu-build."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

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


@pytest.mark.skipif(not PLUGIN.is_file(), reason=f"mvgpu plugin not built ({PLUGIN})")
@pytest.mark.skipif(shutil.which("python3") is None, reason="needs the system Python")
def test_mvgpu_moves_a_square_and_keeps_sources(tmp_path: Path) -> None:
    script = tmp_path / "mvgpu_check.py"
    script.write_text(SCRIPT)
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", os.fspath(script), os.fspath(PLUGIN)],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr[-2000:]
    r = json.loads(proc.stdout.strip().splitlines()[-1])
    assert (r["len2"], r["len5"], r["fps2"]) == (12, 30, [48, 1])
    assert r["version"] == [0, 1], "0.0.1 is reported as (0, 1) (meson.build)"
    assert r["n_errors"] == 20 and r["errors"] == ["mvgpu.Interpolate: no Vulkan device 99"]
    assert r["source_kept"], "source positions must be the source frames, untouched"
    assert r["scene_repeat"], "a frame marked _SceneChangeNext is repeated"
    # the square lands where it should: far closer than a blend of the two frames
    assert r["mid_err"] < 0.15 * r["blend_err"], r
