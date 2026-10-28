# SPDX-License-Identifier: AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye benchmark script (SCOPE F14). Static: every setting arrives as JSON in
# ``user_data`` (vspipe: ``-a user_data=<json>``; mpv: ``vf=vapoursynth:user-data=``).
# Both entry points interpolate the same generated moving pattern (no files are
# read): under vspipe it is the source; inside mpv ``video_in`` (the decoded lavfi
# test source) is still pulled frame by frame, its pixels replaced by the pattern.
#
# user_data keys:
#   backend       "rife" | "mvtools" | "mvgpu"
#   plugin_dir    absolute directory holding librife.so / mvtools.so / libmvgpu.so
#   model_path    absolute RIFE ncnn model directory (rife only)
#   gpu_thread    RIFE parallel GPU jobs (rife only)
#   gpu_id        Vulkan device index or null (rife, mvgpu)
#   uhd           RIFE uhd mode (rife only)
#   matrix        YUV<->RGB matrix name, e.g. "709"
#   src_num/src_den   source frame rate
#   out_num/out_den   output frame rate
#   width/height/frames   size and length of the generated pattern
import json
import os
import sys

import vapoursynth as vs

core = vs.core

_raw = globals().get('user_data', '{}')
if isinstance(_raw, bytes):
    _raw = _raw.decode('utf-8')
cfg = json.loads(_raw)


def load(namespace, filename):
    if os.name == 'nt' and filename.endswith('.so'):  # Windows builds are .dll
        filename = filename[:-3] + '.dll'
    elif sys.platform == 'darwin' and filename.endswith('.so'):  # macOS builds are .dylib
        filename = filename[:-3] + '.dylib'
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(cfg['plugin_dir'], filename))


def _cells(columns, rows, seed):
    """columns x rows pseudo-random 1-pixel cells (YUV 4:4:4), and the next seed."""
    stacked = []
    for _y in range(rows):
        cells = []
        for _x in range(columns):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            cells.append(core.std.BlankClip(format=vs.YUV444P8, width=1, height=1, length=1,
                                            color=[16 + seed % 220, 96 + seed % 64, 160 - seed % 64]))
        stacked.append(core.std.StackHorizontal(cells))
    return core.std.StackVertical(stacked), seed


def _path(length, speeds, segment, margin):
    """Even offsets in [0, margin] moving by speeds[k] px a frame in segment k,
    bouncing at the ends (4:2:0 crops need even offsets)."""
    pos, step, out = 0, 1, []
    for i in range(length):
        out.append(pos)
        pos += step * speeds[(i // segment) % len(speeds)]
        if pos > margin:
            pos, step = 2 * margin - pos, -1
        elif pos < 0:
            pos, step = -pos, 1
    return out


def pattern(width, height, length, fpsnum, fpsden):
    # A pseudo-random texture that pans: real motion for the motion search, no
    # scene cuts, nothing read from disk. Two layers: smooth 16x9 shapes, and a
    # tile of 4-pixel cells with hard edges (fine detail, where block matching
    # and flow go wrong first). The pan cycles 2, 6, fast and 6 px a frame every
    # 24 frames, fast ~ width / 120 (16 at 1080p, 32 at 4K: a quick camera pan),
    # with a slower vertical drift; a gentle 2 px pan alone flattered MVTools.
    fast = max(8, width // 120 // 2 * 2)
    margin_x = max(64, 12 * fast)
    margin_y = 64
    base, seed = _cells(16, 9, 12345)
    base = core.resize.Bicubic(base, width=width + margin_x, height=height + margin_y)
    tile, _ = _cells(24, 16, seed)
    tile = core.resize.Point(tile, width=96, height=64)
    tile = core.std.StackHorizontal([tile] * (-(-(width + margin_x) // 96)))
    tile = core.std.StackVertical([tile] * (-(-(height + margin_y) // 64)))
    tile = core.std.CropAbs(tile, width + margin_x, height + margin_y)
    full = core.resize.Bicubic(core.std.Merge(base, tile, weight=0.35), format=vs.YUV420P8)
    xs = _path(length, (2, 6, fast, 6), 24, margin_x)
    ys = _path(length, (2, 0, 4, 2), 24, margin_y)
    frames = [core.std.CropAbs(full, width, height, left=x, top=y) for x, y in zip(xs, ys)]
    return core.std.AssumeFPS(core.std.Splice(frames), fpsnum=fpsnum, fpsden=fpsden)


src_num = int(cfg['src_num'])
src_den = int(cfg['src_den'])
out_num = int(cfg['out_num'])
out_den = int(cfg['out_den'])

clip = pattern(int(cfg['width']), int(cfg['height']), int(cfg['frames']), src_num, src_den)
if 'video_in' in globals():
    # the same content in both passes (vspipe's numbers and mpv's must describe
    # the same work): mpv's decoded frames are still fetched one for one, so
    # decoding and uploading them stay in the cost, but their pixels (testsrc2,
    # much easier to match) are not what is interpolated. Bounded: mpv's
    # generated source has no end, so video_in is INT_MAX frames long, and every
    # engine's output length (2x that) overflows - mvgpu refused it as an invalid
    # VSVideoInfo and mpv aborted (measured, this M2). Four times the timed
    # frames is past the --frames cut-off at any multiplier the bench runs.
    n = min(video_in.num_frames, 4 * int(cfg['frames']))
    clip = core.std.Loop(clip, times=-(-n // clip.num_frames))[:n]
    clip = core.std.CopyFrameProps(clip, video_in[:n])
    clip = core.std.AssumeFPS(clip, fpsnum=src_num, fpsden=src_den)

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
    # as template.vpy plays it: pel 2 up to 1080p, pel 1 above
    sup = core.mv.Super(clip, pel=1 if clip.width > 1920 or clip.height > 1080 else 2)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    out = core.mv.FlowFPS(clip, sup, bv, fv, num=out_num, den=out_den)
elif backend == 'mvgpu':
    load('mvgpu', 'libmvgpu.so')
    # as template.vpy plays it: YUV 4:2:0 straight in, no scene marks (the
    # pattern has no cuts; the marks cost <= 0.5 %, SCOPE 15.1 P1)
    args = dict(fps_num=out_num, fps_den=out_den)
    if cfg.get('gpu_id') is not None:
        args['device'] = int(cfg['gpu_id'])
    out = core.mvgpu.Interpolate(clip, **args)
else:
    raise vs.Error('unknown backend ' + repr(backend))

out.set_output()
