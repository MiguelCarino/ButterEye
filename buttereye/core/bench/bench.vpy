# SPDX-License-Identifier: AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye benchmark script (SCOPE F14). Static: every setting arrives as JSON in
# ``user_data`` (vspipe: ``-a user_data=<json>``; mpv: ``vf=vapoursynth:user-data=``).
# Two entry points: inside mpv ``video_in`` is the decoded lavfi test source; under
# vspipe the source is a generated moving pattern (no files are read).
#
# user_data keys:
#   backend       "rife" | "mvtools"
#   plugin_dir    absolute directory holding librife.so / mvtools.so
#   model_path    absolute RIFE ncnn model directory (rife only)
#   gpu_thread    RIFE parallel GPU jobs (rife only)
#   gpu_id        Vulkan device index or null (rife only)
#   uhd           RIFE uhd mode (rife only)
#   matrix        YUV<->RGB matrix name, e.g. "709"
#   src_num/src_den   source frame rate
#   out_num/out_den   output frame rate
#   width/height/frames   size and length of the generated vspipe source
import json
import os

import vapoursynth as vs

core = vs.core

_raw = globals().get('user_data', '{}')
if isinstance(_raw, bytes):
    _raw = _raw.decode('utf-8')
cfg = json.loads(_raw)


def load(namespace, filename):
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(cfg['plugin_dir'], filename))


def pattern(width, height, length, fpsnum, fpsden):
    # A smooth pseudo-random texture that pans back and forth: real motion for
    # the motion search, no scene cuts, nothing read from disk.
    margin = 64
    seed = 12345
    rows = []
    for _y in range(9):
        cells = []
        for _x in range(16):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            cells.append(core.std.BlankClip(format=vs.YUV444P8, width=1, height=1, length=1,
                                            color=[16 + seed % 220, 96 + seed % 64, 160 - seed % 64]))
        rows.append(core.std.StackHorizontal(cells))
    base = core.resize.Bicubic(core.std.StackVertical(rows), width=width + margin,
                               height=height + margin, format=vs.YUV420P8)
    frames = []
    for i in range(length):
        left = 2 * min(i % 32, 32 - i % 32)
        top = 2 * min(i % 16, 16 - i % 16)
        frames.append(core.std.CropAbs(base, width, height, left=left, top=top))
    return core.std.AssumeFPS(core.std.Splice(frames), fpsnum=fpsnum, fpsden=fpsden)


src_num = int(cfg['src_num'])
src_den = int(cfg['src_den'])
out_num = int(cfg['out_num'])
out_den = int(cfg['out_den'])

if 'video_in' in globals():
    clip = core.std.AssumeFPS(video_in, fpsnum=src_num, fpsden=src_den)
else:
    clip = pattern(int(cfg['width']), int(cfg['height']), int(cfg['frames']), src_num, src_den)

backend = cfg['backend']
if backend == 'rife':
    load('rife', 'librife.so')
    matrix = cfg['matrix']
    rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s=matrix)
    # factor = out / src
    factor_num = out_num * src_den
    factor_den = out_den * src_num
    args = dict(factor_num=factor_num, factor_den=factor_den, model_path=cfg['model_path'],
                gpu_thread=int(cfg['gpu_thread']), uhd=bool(cfg.get('uhd', False)))
    if cfg.get('gpu_id') is not None:
        args['gpu_id'] = int(cfg['gpu_id'])
    rgb = core.rife.RIFE(rgb, **args)
    # as template.vpy plays it: no dither for 10-bit output, ordered for 8-bit
    out = core.resize.Bicubic(rgb, format=clip.format.id, matrix_s=matrix,
                              dither_type='none' if clip.format.bits_per_sample > 8 else 'ordered')
elif backend == 'mvtools':
    load('mv', 'mvtools.so')
    # as template.vpy plays it: pel 1 from 720p up
    sup = core.mv.Super(clip, pel=1 if clip.width >= 1280 or clip.height >= 720 else 2)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    out = core.mv.FlowFPS(clip, sup, bv, fv, num=out_num, den=out_den)
else:
    raise vs.Error('unknown backend ' + repr(backend))

out.set_output()
