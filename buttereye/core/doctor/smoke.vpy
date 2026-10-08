# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye setup smoke test (SCOPE §4.7 step 3): a short 2x interpolation of a
# generated clip, run by vspipe in a subprocess:
#   vspipe -p -a backend=rife|mvtools -a plugin_dir=... -a model_dir=... \
#          -a model=<dir name> -a gpu=<index> -a threads=4 -a size=WxH -a frames=N smoke.vpy --
# Every setting arrives through `vspipe -a` (strings); nothing is put into code.
import os

import vapoursynth as vs

core = vs.core


def _arg(key, default):
    value = globals().get(key)
    if isinstance(value, bytes):
        value = value.decode()
    return value if value else default


def _load(namespace, filename):
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(_arg("plugin_dir", ""), filename))


backend = _arg("backend", "mvtools")
w, h = (int(x) for x in _arg("size", "1280x720").split("x"))
frames = int(_arg("frames", "48"))

_BG = [16, 128, 128]
clip = core.std.BlankClip(format=vs.YUV420P8, width=w, height=h, fpsnum=24000, fpsden=1001,
                          length=frames, color=_BG)
# A moving bright bar, so the interpolators do real work.
_bw = max(16, (w // 16) & ~1)
_bar = core.std.BlankClip(clip, width=_bw, color=[235, 128, 128])


def _shift(n):
    x = (n * 8) % (w - _bw)
    return core.std.AddBorders(_bar, left=x, right=w - _bw - x, color=_BG)


clip = core.std.FrameEval(clip, _shift)

if backend == "rife":
    _load("rife", "librife.so")
    model = _arg("model", "")
    if not model or os.sep in model or model.startswith("."):
        raise vs.Error("invalid model name")
    rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s="709")
    rgb = core.rife.RIFE(rgb, factor_num=2, factor_den=1,
                         model_path=os.path.join(_arg("model_dir", ""), model),
                         gpu_id=int(_arg("gpu", "0")), gpu_thread=int(_arg("threads", "4")))
    out = core.resize.Bicubic(rgb, format=vs.YUV420P8, matrix_s="709")
elif backend == "mvtools":
    _load("mv", "mvtools.so")
    sup = core.mv.Super(clip, pel=2)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    out = core.mv.FlowFPS(clip, sup, bv, fv, num=clip.fps.numerator * 2,
                          den=clip.fps.denominator)
else:
    raise vs.Error("unknown backend")

out.set_output()
