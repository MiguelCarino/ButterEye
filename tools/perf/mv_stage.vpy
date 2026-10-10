# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# MVTools at 4K (performance track P3, spike M0(p)) stage script for
# tools/perf/mvtools_spike.py. Settings in ``user_data``:
#   mode       "speed": the panning texture of stage.vpy at width x height
#              "quality": frames A and C (planar RGB float32 .npy, (3, h, w)) as a
#              2-frame clip; output frame 1 is MVTools' frame between them
#   blksize, overlap, pel, chroma, search, searchparam, fps ("flow" | "block")
import json

import vapoursynth as vs

core = vs.core
_raw = globals().get('user_data', '{}')
cfg = json.loads(_raw.decode('utf-8') if isinstance(_raw, bytes) else _raw)
if not hasattr(core, 'mv'):
    core.std.LoadPlugin(cfg['plugin_dir'] + '/mvtools.so')


def pattern(width, height, length):
    margin = 64
    seed = 12345
    rows = []
    for _y in range(9):
        cells = []
        for _x in range(16):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            cells.append(core.std.BlankClip(format=vs.YUV444P8, width=1, height=1, length=1,
                                            color=[16 + seed % 220, 96 + seed % 64,
                                                   160 - seed % 64]))
        rows.append(core.std.StackHorizontal(cells))
    base = core.resize.Bicubic(core.std.StackVertical(rows), width=width + margin,
                               height=height + margin, format=vs.YUV420P8)
    frames = [core.std.CropAbs(base, width, height, left=2 * min(i % 32, 32 - i % 32),
                               top=2 * min(i % 16, 16 - i % 16)) for i in range(length)]
    return core.std.AssumeFPS(core.std.Splice(frames), fpsnum=24000, fpsden=1001)


def smooth(clip):
    pel = int(cfg.get('pel') or (1 if clip.width >= 1280 else 2))
    sup = core.mv.Super(clip, pel=pel)
    kw = dict(blksize=int(cfg['blksize']), overlap=int(cfg['overlap']),
              chroma=bool(cfg.get('chroma', True)))
    if cfg.get('search') is not None:
        kw['search'] = int(cfg['search'])
    if cfg.get('searchparam') is not None:
        kw['searchparam'] = int(cfg['searchparam'])
    bv = core.mv.Analyse(sup, isb=True, **kw)
    fv = core.mv.Analyse(sup, isb=False, **kw)
    num, den = clip.fps_num * 2, clip.fps_den
    if cfg.get('fps') == 'block':
        return core.mv.BlockFPS(clip, sup, bv, fv, num=num, den=den)
    return core.mv.FlowFPS(clip, sup, bv, fv, num=num, den=den)


if cfg['mode'] == 'speed':
    out = smooth(pattern(int(cfg['width']), int(cfg['height']), int(cfg['frames'])))
else:
    import numpy as np

    a, c = np.load(cfg['a_npy']), np.load(cfg['c_npy'])
    _, h, w = a.shape
    blank = core.std.BlankClip(format=vs.RGBS, width=w, height=h, length=2,
                               fpsnum=24000, fpsden=1001)

    def fill(n, f):
        out = f.copy()
        for p in range(3):
            np.copyto(np.asarray(out[p]), (a if n == 0 else c)[p])
        return out

    rgb = core.std.ModifyFrame(blank, blank, fill)
    yuv = core.resize.Bicubic(rgb, format=vs.YUV420P8, matrix_s='709', dither_type='ordered')
    out = core.resize.Bicubic(smooth(yuv), format=vs.RGBS, matrix_in_s='709')
out.set_output()
