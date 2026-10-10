# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Quality-gate helper for tools/perf/trt11_quality.py: rebuilds the middle frame
# of a triple through vs-mlrt's vstrt. Two RGB float32 frames (A and C, planar,
# shape (3, h, w)) arrive as .npy files; output frame 1 is RIFE's frame between
# them (timestep 0.5), as RGBS. Settings in ``user_data`` (see trt_common.py,
# plus a_npy, c_npy, perf_dir). With ``engine: "ncnn"`` the RIFE-ncnn plugin
# (librife.so from plugin_dir, model_path) runs instead, as ButterEye plays today.
import json
import sys

import numpy as np
import vapoursynth as vs

core = vs.core
_raw = globals().get('user_data', '{}')
cfg = json.loads(_raw.decode('utf-8') if isinstance(_raw, bytes) else _raw)
sys.path.insert(0, cfg['perf_dir'])
import trt_common  # noqa: E402

a = np.load(cfg['a_npy'])
c = np.load(cfg['c_npy'])
_, h, w = a.shape
blank = core.std.BlankClip(format=vs.RGBS, width=w, height=h, length=2)


def fill(n, f):
    out = f.copy()
    src = a if n == 0 else c
    for p in range(3):
        np.copyto(np.asarray(out[p]), src[p])
    return out


pair = core.std.ModifyFrame(blank, blank, fill)
if cfg.get('engine') == 'ncnn':
    if not hasattr(core, 'rife'):
        core.std.LoadPlugin(cfg['plugin_dir'] + '/librife.so')
    mid = core.rife.RIFE(pair, model_path=cfg['model_path'], factor_num=2, factor_den=1,
                         gpu_thread=1)
else:
    if cfg['half_io']:
        pair = core.resize.Point(pair, format=vs.RGBH)
    mid = trt_common.rife(core, pair, cfg, 2)
core.resize.Point(mid, format=vs.RGBS).set_output()
