# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Phase 0 (SCOPE §15.1, spike M0(i)) stage script for tools/perf/breakdown.py.
# One stage per run, so each step of the live pipeline can be timed on its own.
# Settings arrive as JSON in ``user_data`` (vspipe ``-a user_data=``, mpv
# ``vf=vapoursynth:user-data=``). Under mpv the source is ``video_in``; under
# vspipe it is a generated panning texture (nothing is read from disk).
#
# stages:
#   source     the source alone
#   convert    YUV -> RGBS -> YUV, as template.vpy does around RIFE
#   rife       the full live RIFE path of template.vpy (convert, RIFE, convert,
#              source pass-through at integer multipliers)
#   rife-rgb   RIFE on a source made in RGBS, RGBS out: no per-frame colour
#              conversion, so it is RIFE plus transfers alone
#   mvtools    the live MVTools path of template.vpy
import json
import os
from fractions import Fraction

import vapoursynth as vs

core = vs.core

_raw = globals().get('user_data', '{}')
if isinstance(_raw, bytes):
    _raw = _raw.decode('utf-8')
cfg = json.loads(_raw)
# bounded frame cache (VapourSynth defaults to 4 GB per process); a 4K RGBS frame is ~100 MB
core.max_cache_size = int(cfg.get('cache_mb', 1024))

stage = cfg['stage']
src_rate = Fraction(int(cfg['src_num']), int(cfg['src_den']))
target = Fraction(int(cfg['out_num']), int(cfg['out_den']))
matrix = cfg.get('matrix', '709')


def load(namespace, filename):
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(cfg['plugin_dir'], filename))


def pattern(fmt, width, height, length):
    # Same idea as bench.vpy: a smooth pseudo-random texture that pans, so the
    # motion search has real motion and there are no scene cuts.
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
    if fmt != vs.YUV420P8:
        base = core.resize.Bicubic(base, format=fmt, matrix_in_s=matrix)
    frames = []
    for i in range(length):
        left = 2 * min(i % 32, 32 - i % 32)
        top = 2 * min(i % 16, 16 - i % 16)
        frames.append(core.std.CropAbs(base, width, height, left=left, top=top))
    clip = core.std.Splice(frames)
    return core.std.AssumeFPS(clip, fpsnum=src_rate.numerator, fpsden=src_rate.denominator)


def source(fmt=vs.YUV420P8):
    if 'video_in' in globals():
        clip = globals()['video_in']
        return core.std.AssumeFPS(clip, fpsnum=src_rate.numerator, fpsden=src_rate.denominator)
    return pattern(fmt, int(cfg['width']), int(cfg['height']), int(cfg['frames']))


def rife(rgb):
    load('rife', 'librife.so')
    return core.rife.RIFE(rgb, model_path=cfg['model_path'], fps_num=target.numerator,
                          fps_den=target.denominator, gpu_thread=int(cfg.get('gpu_thread', 4)))


def to_rgb(clip):
    return core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s=matrix)


def to_yuv(rgb, like):
    dither = 'none' if like.format.bits_per_sample > 8 else 'ordered'
    return core.resize.Bicubic(rgb, format=like.format.id, matrix_s=matrix, dither_type=dither)


def passthrough(clip, interpolated):
    ratio = target / src_rate
    if ratio.denominator != 1 or ratio.numerator < 2:
        return interpolated
    m = ratio.numerator
    out = core.std.Interleave([clip] + [core.std.SelectEvery(interpolated, m, i)
                                        for i in range(1, m)])
    return out if out.num_frames == interpolated.num_frames else interpolated


if stage == 'source':
    out = source()
elif stage == 'convert':
    clip = source()
    out = to_yuv(to_rgb(clip), clip)
elif stage == 'rife':
    clip = source()
    out = passthrough(clip, to_yuv(rife(to_rgb(clip)), clip))
elif stage == 'rife-rgb':
    if 'video_in' in globals():
        raise vs.Error('rife-rgb is a vspipe-only stage')
    out = rife(source(vs.RGBS))
elif stage == 'mvtools':
    load('mv', 'mvtools.so')
    clip = source()
    sup = core.mv.Super(clip, pel=1 if clip.width >= 1280 or clip.height >= 720 else 2)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    out = core.mv.FlowFPS(clip, sup, bv, fv, num=target.numerator, den=target.denominator)
else:
    raise vs.Error('unknown stage ' + repr(stage))

out.set_output()
