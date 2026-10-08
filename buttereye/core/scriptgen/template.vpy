# SPDX-License-Identifier: AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye frame-interpolation script (SCOPE §4.4). Written from scratch.
#
# Generated from one static template: copies differ only in the data block below,
# whose values are Python literals written with repr(). Every other parameter
# (rates, colour, backend settings, paths of data files) arrives as JSON in
# ``user_data`` and is parsed with json.loads; nothing from a file name or a title
# ever becomes code.
#
# Two entry points:
#   mpv     the vapoursynth filter provides ``video_in`` and ``user_data``
#   vspipe  ``vspipe -a user_data=<json> script.vpy -`` with a ``source`` entry
import json
import os
from fractions import Fraction

import vapoursynth as vs

# --- ButterEye data block (generated) ---
PLUGIN_DIR = '/usr/lib64/buttereye/vapoursynth'
FFMS2_LIB = '/usr/lib64/libffms2.so.5'
# --- end of data block ---

core = vs.core

USER_DATA_VERSION = 1
MATRICES = ('709', '170m', '470bg', '240m', '2020ncl', '2020cl', 'fcc', 'ycgco')
MATRIX_PROP = {1: '709', 4: 'fcc', 5: '470bg', 6: '170m', 7: '240m', 8: 'ycgco',
               9: '2020ncl', 10: '2020cl'}


def _settings():
    raw = globals().get('user_data')
    if isinstance(raw, bytes):
        raw = raw.decode('utf-8')
    if not raw:
        raise vs.Error('ButterEye: user_data is missing')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('v') != USER_DATA_VERSION:
        raise vs.Error('ButterEye: unsupported user_data version')
    return data


def _load(namespace, filename):
    # Absolute path from the private, non-autoload directory; skip if a copy of
    # the plugin is already loaded (distro or autoload), which would fail.
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(PLUGIN_DIR, filename))


def _rate(value, fallback=None):
    if isinstance(value, list) and len(value) == 2:
        return Fraction(int(value[0]), int(value[1]))
    if fallback:
        return Fraction(fallback).limit_denominator(1001)
    raise vs.Error('ButterEye: no frame rate')


def _matrix(cfg, clip, props):
    # 1. explicit matrix from user_data (the core passes mpv's video-params);
    # 2. the _Matrix frame property, when the source sets one;
    # 3. by frame height. Never a hard-coded 709.
    name = cfg.get('matrix')
    if name in MATRICES:
        return name
    if props is not None:
        name = MATRIX_PROP.get(props.get('_Matrix'))
        if name is not None:
            return name
    return '709' if clip.height >= 720 else '170m'


def _range(cfg, props):
    name = cfg.get('range')
    if name in ('limited', 'full'):
        return name
    if props is not None and props.get('_ColorRange') == 0:
        return 'full'
    return 'limited'


def _source(cfg):
    if 'video_in' in globals():
        clip = globals()['video_in']
        rate = _rate(cfg.get('src_fps'), globals().get('container_fps'))
        clip = core.std.AssumeFPS(clip, fpsnum=rate.numerator, fpsden=rate.denominator)
        return clip, None
    src = cfg.get('source') or {}
    kind = src.get('kind')
    if kind == 'blank':
        rate = _rate(src.get('fps'))
        fmt = vs.YUV420P10 if int(src.get('bits', 8)) > 8 else vs.YUV420P8
        clip = core.std.BlankClip(format=fmt, width=int(src['width']), height=int(src['height']),
                                  fpsnum=rate.numerator, fpsden=rate.denominator,
                                  length=int(src.get('length', 48)))
    elif kind == 'file':
        if not hasattr(core, 'ffms2'):
            if not os.path.exists(FFMS2_LIB):
                raise vs.Error('ButterEye: the ffms2 source plugin is not installed')
            core.std.LoadPlugin(FFMS2_LIB)
        opts = {}
        if src.get('cache'):
            opts['cachefile'] = src['cache']  # never beside the user's video
        rate = _rate(cfg.get('src_fps')) if cfg.get('src_fps') else None
        if rate is not None and src.get('cfr'):
            # variable frame rate: ffms2 converts to constant (SCOPE 7.3)
            opts['fpsnum'], opts['fpsden'] = rate.numerator, rate.denominator
        clip = core.ffms2.Source(src['path'], **opts)
        if rate is not None and not src.get('cfr'):
            clip = core.std.AssumeFPS(clip, fpsnum=rate.numerator, fpsden=rate.denominator)
    else:
        raise vs.Error('ButterEye: no source')
    return clip, clip.get_frame(0).props


def _mark_scene_changes(clip, threshold):
    # PlaneStats difference between each frame and the next; sets
    # _SceneChangeNext, which RIFE (sc=True) uses to repeat instead of blending.
    nxt = clip[1:] + clip[-1]
    stats = core.std.PlaneStats(clip, nxt, plane=0)

    def mark(n, f):
        out = f[0].copy()
        out.props['_SceneChangeNext'] = 1 if f[1].props['PlaneStatsDiff'] > threshold else 0
        return out

    return core.std.ModifyFrame(clip, [clip, stats], mark)


def _output_format(clip, cfg):
    bits = 10 if clip.format.bits_per_sample > 8 or cfg.get('hdr') else 8
    return core.query_video_format(vs.YUV, vs.INTEGER, bits,
                                   clip.format.subsampling_w, clip.format.subsampling_h)


def _dither(fmt, cfg):
    # Float to 10 bits shows no banding (and mpv dithers to the display itself):
    # no dither. 8 bits: ordered, unless the caller asks for another (offline
    # renders ask for error_diffusion, zimg's serial path at ~2x the cost).
    if fmt.bits_per_sample > 8:
        return 'none'
    name = cfg.get('dither')
    return name if name in ('none', 'ordered', 'random', 'error_diffusion') else 'ordered'


def _passthrough(clip, interpolated, target):
    # At an integer multiplier m every m-th output frame is a source frame: take
    # it from the source untouched (no YUV->RGB->YUV round trip, no dither) and
    # only the others from RIFE. Evaluation is lazy, so the RIFE output frames at
    # source positions are never requested. Same format and length, or no change.
    ratio = target / Fraction(clip.fps_num, clip.fps_den) if clip.fps_num else None
    if ratio is None or ratio.denominator != 1 or ratio.numerator < 2:
        return interpolated
    if clip.format.id != interpolated.format.id:
        return interpolated
    m = ratio.numerator
    out = core.std.Interleave([clip] + [core.std.SelectEvery(interpolated, m, i)
                                        for i in range(1, m)])
    if out.num_frames != interpolated.num_frames:
        return interpolated
    return out


def _rife(clip, cfg, props, target):
    _load('rife', 'librife.so')
    matrix = _matrix(cfg, clip, props)
    rng = _range(cfg, props)
    threshold = cfg.get('sc_threshold')
    sc = isinstance(threshold, (int, float)) and threshold > 0
    if sc:
        clip = _mark_scene_changes(clip, float(threshold))
    rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s=matrix, range_in_s=rng)
    # uhd is read by RIFE v1-v3 models only; v4.x ignore it (SCOPE 5.3)
    opts = {'model_path': cfg['model_path'], 'fps_num': target.numerator,
            'fps_den': target.denominator, 'gpu_thread': int(cfg.get('gpu_thread') or 4),
            'uhd': bool(cfg.get('uhd')), 'sc': sc}
    if cfg.get('gpu_id') is not None:
        opts['gpu_id'] = int(cfg['gpu_id'])
    rgb = core.rife.RIFE(rgb, **opts)
    fmt = _output_format(clip, cfg)
    out = core.resize.Bicubic(rgb, format=fmt.id, matrix_s=matrix, range_s=rng,
                              dither_type=_dither(fmt, cfg))
    return _passthrough(clip, out, target)


def _mvtools(clip, cfg, target):
    _load('mv', 'mvtools.so')
    # pel 1 (full-pixel search) from 720p up: ~20-30 % less CPU, and vectors are
    # already fine-grained at that size; offline renders ask for pel 2
    pel = cfg.get('mv_pel')
    if pel not in (1, 2, 4):
        pel = 1 if clip.width >= 1280 or clip.height >= 720 else 2
    sup = core.mv.Super(clip, pel=pel)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    return core.mv.FlowFPS(clip, sup, bv, fv, num=target.numerator, den=target.denominator)


def _shrink(clip, cfg):
    # Smooth at a smaller size when the core asks for one (too demanding at the
    # source size); mpv scales the output to the window like any video.
    size = cfg.get('size')
    if not (isinstance(size, list) and len(size) == 2):
        return clip
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0 or (width, height) == (clip.width, clip.height):
        return clip
    return core.resize.Spline36(clip, width=width, height=height)


def build():
    cfg = _settings()
    clip, props = _source(cfg)
    clip = _shrink(clip, cfg)
    target = _rate(cfg.get('target_fps'))
    backend = cfg.get('backend')
    if backend == 'rife-ncnn':
        out = _rife(clip, cfg, props, target)
    elif backend == 'mvtools':
        out = _mvtools(clip, cfg, target)
    else:
        raise vs.Error('ButterEye: unknown backend')
    return core.std.AssumeFPS(out, fpsnum=target.numerator, fpsden=target.denominator)


build().set_output()
