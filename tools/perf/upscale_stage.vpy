# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# ML upscaling spike (SCOPE §15.2, spike M0(n)) stage script for
# tools/perf/upscale_spike.py. Settings arrive as JSON in ``user_data`` (see
# trt_common.py for the TensorRT keys, plus the ones below).
#
# modes:
#   speed    a blank RGB clip of ``width`` x ``height`` through the upscaler
#   chain    RIFE (TensorRT) at the source size, then the upscaler on every
#            output frame: the order a live chain would use (SCOPE §15.2)
#   quality  one real frame (``orig_npy``, planar RGB float32, (3, H, W)) is
#            shrunk by ``factor`` with Bicubic, then brought back to H x W by
#            ``method``: a model, or "lanczos" / "spline36" as baselines
import json
import sys
from fractions import Fraction

import vapoursynth as vs

core = vs.core
_raw = globals().get('user_data', '{}')
cfg = json.loads(_raw.decode('utf-8') if isinstance(_raw, bytes) else _raw)
sys.path.insert(0, cfg['perf_dir'])
import trt_common  # noqa: E402

mode = cfg['mode']
half = vs.RGBH if cfg.get('half_io') else vs.RGBS

if mode in ('speed', 'chain'):
    clip = core.std.BlankClip(format=half, width=int(cfg['width']), height=int(cfg['height']),
                              length=int(cfg['frames']), fpsnum=24000, fpsden=1001,
                              color=[0.3, 0.5, 0.7])
    if mode == 'chain':
        clip = trt_common.rife(core, clip, cfg, Fraction(2))
    out = trt_common.upscale(core, clip, cfg)
elif mode == 'quality':
    import numpy as np

    orig = np.load(cfg['orig_npy'])
    _, h, w = orig.shape
    blank = core.std.BlankClip(format=vs.RGBS, width=w, height=h, length=1)

    def fill(n, f):
        out = f.copy()
        for p in range(3):
            np.copyto(np.asarray(out[p]), orig[p])
        return out

    full = core.std.ModifyFrame(blank, blank, fill)
    factor = int(cfg['factor'])
    small = core.resize.Bicubic(full, width=w // factor, height=h // factor)
    method = cfg['method']
    if method == 'lanczos':
        out = core.resize.Lanczos(small, width=w, height=h, filter_param_a=3)
    elif method == 'spline36':
        out = core.resize.Spline36(small, width=w, height=h)
    else:
        up = trt_common.upscale(core, core.resize.Point(small, format=half), cfg)
        if (up.width, up.height) != (w, h):  # a 4x model on a 2x test, or odd sizes
            up = core.resize.Spline36(up, width=w, height=h)
        out = up
    out = core.resize.Point(out, format=vs.RGBS)
else:
    raise vs.Error('unknown mode ' + repr(mode))

out.set_output()
