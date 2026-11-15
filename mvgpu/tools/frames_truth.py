# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The present layer's motion-compensated path as an engine for tools/perf/truth.py.

    FRAMES_TEST=<build>/layer/frames-test \\
        python3 tools/perf/truth.py --engines mvgpu --reference mvgpu/tools/frames_truth.py ...

truth.py hands ``interpolate`` each scene's source clip (YUV, BT.709 video range); every
source frame is turned into the RGB a 10-bit swapchain holds, all of them go through
``frames-test`` in order (as the layer's images of consecutive presents), and the in-between
frame of each pair (t = 1/2) comes back as YUV for scoring. The engine therefore works as
it does in the layer: on RGB luma, with the GPU's brightness reduction and cut test, and
with the vectors of the pair before when there is one (seven-frame moments, ``--around``).

Environment:
  FRAMES_TEST        the frames-test binary (required)
  FRAMES_TRUTH_PQ=1  hand the frames over as an HDR10 swapchain holds SDR video (BT.2020,
                     PQ, SDR white at 203 cd/m2) and take them back the same way
  FRAMES_TRUTH_MODE  mc (default) or blend (the layer's integer blend: a check of what the
                     RGB round trip alone costs against truth.py's own blend)
  FRAMES_TRUTH_SCALE a display height: the frames are scaled up to it (Spline36), the layer
                     works at source scale (--src-h), and its output is scaled back down
  FRAMES_TRUTH_TMP   a folder for the frames passed to frames-test (default: a temporary one)
  FRAMES_TRUTH_ARGS  more frames-test arguments
The engine's MVGPU_* settings and BUTTEREYE_FRAMES_STILL / _CUT apply as in the layer.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

M709_2020 = [[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]]
PQ = (0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875)


def _to_pq(np: Any, rgb: Any) -> Any:
    lin = np.clip(rgb, 0.0, 1.0) ** 2.4
    y = np.clip(lin @ np.array(M709_2020).T * (203.0 / 10000.0), 0.0, 1.0)
    m1, m2, c1, c2, c3 = PQ
    return ((c1 + c2 * y**m1) / (1.0 + c3 * y**m1)) ** m2


def _from_pq(np: Any, e: Any) -> Any:
    m1, m2, c1, c2, c3 = PQ
    ep = np.clip(e, 0.0, 1.0) ** (1.0 / m2)
    y = (np.maximum(ep - c1, 0.0) / (c2 - c3 * ep)) ** (1.0 / m1) * (10000.0 / 203.0)
    lin = y @ np.linalg.inv(np.array(M709_2020)).T
    return np.clip(lin, 0.0, 1.0) ** (1.0 / 2.4)


def _write(np: Any, path: Path, rgb: Any) -> None:
    v = np.clip(np.round(rgb * 1023.0), 0, 1023).astype(">u2")
    path.write_bytes(b"P6\n%d %d\n1023\n" % (rgb.shape[1], rgb.shape[0]) + v.tobytes())


def _read(np: Any, path: Path) -> Any:
    raw = path.read_bytes()
    parts = raw.split(maxsplit=4)
    w, h, maxval = int(parts[1]), int(parts[2]), int(parts[3])
    dt = ">u2" if maxval > 255 else np.uint8
    return np.frombuffer(parts[4], dtype=dt).reshape(h, w, 3).astype(np.float64) / maxval


def interpolate(core: Any, clip: Any, num: int, den: int) -> Any:
    import numpy as np
    import vapoursynth as vs  # type: ignore[import-untyped]

    binary = os.environ.get("FRAMES_TEST")
    if not binary:
        raise RuntimeError("set FRAMES_TEST to the frames-test binary")
    if num * clip.fps_den != 2 * den * clip.fps_num:
        raise RuntimeError("frames_truth makes twice the source rate only")
    pq = os.environ.get("FRAMES_TRUTH_PQ") == "1"
    mode = os.environ.get("FRAMES_TRUTH_MODE", "mc")
    scale = int(os.environ.get("FRAMES_TRUTH_SCALE") or 0)
    w, h = clip.width, clip.height
    rgb = core.resize.Spline36(clip, format=vs.RGBS, matrix_in_s="709", range_in_s="limited")
    dw, dh = w, h
    if scale and scale != h:
        dh, dw = scale, round(w * scale / h / 2) * 2
        rgb = core.resize.Spline36(rgb, dw, dh)
    frames = []
    for n in range(clip.num_frames):
        f = rgb.get_frame(n)
        img = np.dstack([np.asarray(f[p], dtype=np.float64) for p in range(3)])
        frames.append(_to_pq(np, img) if pq else np.clip(img, 0.0, 1.0))

    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("VK_INSTANCE_LAYERS", "NVPRESENT"))
    }
    env["DISABLE_LAYER_NV_PRESENT_1"] = "1"
    base = os.environ.get("FRAMES_TRUTH_TMP")
    with tempfile.TemporaryDirectory(dir=base) as tmp:
        names = []
        for n, img in enumerate(frames):
            names.append(str(Path(tmp) / f"in{n}.ppm"))
            _write(np, Path(names[-1]), img)
        args = [binary, "--k", "2", "--mode", mode, "--out", str(Path(tmp) / "o")]
        if pq:
            args.append("--pq")
        if dh != h:
            args += ["--src-h", str(h)]
        args += shlex.split(os.environ.get("FRAMES_TRUTH_ARGS", ""))
        p = subprocess.run(args + names, env=env, capture_output=True, text=True, timeout=600)
        if p.returncode != 0:
            raise RuntimeError(f"frames-test failed: {p.stderr.strip()[-400:]}")
        mids = []
        for n in range(len(frames) - 1):
            img = _read(np, Path(tmp) / f"o-{n}-1.ppm")
            mids.append(_from_pq(np, img) if pq else img)
    back = [_from_pq(np, f) if pq else f for f in frames]
    out = []
    for n in range(len(frames)):
        out.append(back[n])
        out.append(mids[n] if n < len(mids) else back[n])

    blank = core.std.BlankClip(format=vs.RGBS, width=dw, height=dh, length=len(out),
                               fpsnum=num, fpsden=den)  # fmt: skip

    def fill(n: int, f: Any) -> Any:
        o = f.copy()
        for p in range(3):
            np.copyto(np.asarray(o[p]), out[n][:, :, p].astype(np.float32))
        return o

    res = core.std.ModifyFrame(blank, blank, fill)
    if dh != h:
        res = core.resize.Spline36(res, w, h)
    return core.resize.Spline36(res, format=clip.format.id, matrix_s="709", range_s="limited")
