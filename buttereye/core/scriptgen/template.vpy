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
import sys
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
#: zimg names, in _ChromaLocation order (0..5)
CHROMA_LOCS = ('left', 'center', 'top_left', 'top', 'bottom_left', 'bottom')
#: the profile default scene threshold (profiles/defaults.py), for what needs
#: cuts even when the core sends none: MVTools, and hold fill on every engine
DEFAULT_SC_THRESHOLD = 0.12


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
    # Plugins are named for Linux ('librife.so'); the Windows builds are .dll and
    # the macOS ones .dylib, because that is the extension VapourSynth's own autoload
    # scan accepts on each (R81, src/core/vscore.cpp:65-69). LoadPlugin itself takes
    # any name, so without this the .so simply is not on disk and dlopen says so.
    if os.name == 'nt' and filename.endswith('.so'):
        filename = filename[:-3] + '.dll'
    elif sys.platform == 'darwin' and filename.endswith('.so'):
        filename = filename[:-3] + '.dylib'
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


def _chromaloc(cfg, props):
    # Where the chroma samples sit: 1. from user_data (the core passes mpv's
    # video-params); 2. the _ChromaLocation frame property (ffms2 sets it);
    # 3. left, the MPEG-2/H.264/HEVC default. Every conversion of the frame
    # names it both ways, or zimg writes left and interpolated frames shift
    # half a pixel against the source frames between them.
    name = cfg.get('chromaloc')
    if name in CHROMA_LOCS:
        return name
    if props is not None:
        loc = props.get('_ChromaLocation')
        if isinstance(loc, int) and 0 <= loc < len(CHROMA_LOCS):
            return CHROMA_LOCS[loc]
    return 'left'


def _to_rgb(clip, fmt, matrix, rng, loc):
    # Spline36 both ways: ~2.5 dB better chroma than Bicubic through the round
    # trip, at the same cost; interpolated frames alternate with source frames,
    # so softer chroma would pulse
    return core.resize.Spline36(clip, format=fmt, matrix_in_s=matrix, range_in_s=rng,
                                chromaloc_in_s=loc)


def _to_yuv(rgb, fmt, matrix, rng, loc, cfg):
    return core.resize.Spline36(rgb, format=fmt.id, matrix_s=matrix, range_s=rng,
                                chromaloc_s=loc, dither_type=_dither(fmt, cfg))


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


# ---- what changes between frames: scene cuts, held drawings, still pairs ----
# Measured on quarter-size 8-bit luma: d[n] is the mean absolute difference of
# frames n and n+1 (0..1), dmax[n] the largest one in code values. Thresholds
# from 2D and live-action test clips (synthetic ones/twos/threes/mixed, cuts,
# a flash, a fade; three real clips).
#: a relative cut: above CUT_MIN and CUT_RATIO x the largest change among the
#: neighbours CUT_BACK before to CUT_AHEAD after (held ones count as CUT_FLOOR).
#: Dark and same-backdrop cuts in animation stay far below the absolute
#: threshold; fades, flashes and drawings on twos/threes/fours stay unmarked.
#: It also marks a large pose change after a long hold inside a shot (12 on one
#: real clip: a mouth, an eye, an arm): that slot then shows the source frame,
#: as without interpolation, instead of a morph between the two poses.
CUT_MIN, CUT_RATIO, CUT_FLOOR = 0.02, 5.0, 0.003
CUT_BACK, CUT_AHEAD = 4, 3
#: a held drawing: below DUP_ABS, or (noisy sources) below DUP_NOISY and under a
#: DUP_RATIO-th of the change around it (two frames each side)
DUP_ABS, DUP_NOISY, DUP_RATIO = 0.0015, 0.012, 4.0
#: a still pair, copied instead of interpolated: no visible difference at all.
#: The largest-difference test keeps a small moving object out (its mean is tiny);
#: a pair that passes is confirmed at full size on every plane (STILL_FULL, 8-bit
#: code values), where a 2 px object moving 2 px can vanish at quarter size.
#: Real still pairs differ by 2-9 there (compression noise)
STILL_MEAN, STILL_MAX, STILL_FULL = 0.0003, 4, 12
#: hold fill re-times drawings held 2..hold_max frames; longer is a deliberate hold
HOLD_RANGE = (2, 4)


def _shift(clip, k):
    # frame n of the result is clip[n + k], clamped at both ends
    k = max(-clip.num_frames + 1, min(k, clip.num_frames - 1))
    if k > 0:
        return clip[k:] + clip[-1] * k
    if k < 0:
        return clip[0] * (-k) + clip[:k]
    return clip


def _ahead(clip):
    # The clip as the frame analysis looks ahead in it. Offline it is the clip:
    # its length is real and _shift repeats the last frame past it. In mpv the
    # length is a placeholder (134217727), and a request past the real end fails
    # ("EOF or filter EOF/reinit"); the output frame that needs it fails with it
    # and playback ends there, up to 6 source frames early. Here such a frame is
    # the last one repeated, as offline. Not a frame count from the core: mpv
    # numbers frames from 0 again at every seek and start, so only a request
    # can tell where the end is. Each frame is asked for here first (on a
    # VapourSynth thread, which the core sets free while it waits), and the
    # filters reading it are given either the clip or the repeat. The repeat
    # reads as one drawing held to the end, longer than any hold fill re-times:
    # the fill, which reads the clip itself, never asks for a frame past it.
    # Only the analysis' quarter-size clip goes through here (and full-size
    # frames only for a still pair): each request waits for its frame, and a
    # FrameEval on every full-size frame cost a quarter of MVTools' rate (4K
    # 10-bit film smoothed at 1080p: 55 fps out of 74; here 70).
    if 'video_in' not in globals():
        return clip
    #: 'end': (the first frame past the end, the last one repeated), once found
    state = {}

    def real(n):
        try:
            clip.get_frame(n)
        except vs.Error as e:
            if 'EOF' not in str(e):
                raise
            return False
        return True

    def probe(n):
        end = state.get('end')
        if end is not None and n >= end[0]:
            return end[1]
        if real(n):
            return clip
        # threads ask out of order: the end may be a few frames further back.
        # Each candidate is asked for again, never assumed: while it starts, mpv
        # serves a few frames that a short file doesn't have once it plays
        last = n - 1
        while last >= 0 and not real(last):
            last -= 1
        if last < 0:
            raise vs.Error('ButterEye: no source frames')
        if 'end' not in state:
            state['end'] = (last + 1, core.std.Loop(clip[last], clip.num_frames))
        return state['end'][1]

    return core.std.FrameEval(clip, probe)


def _analyse(clip, threshold):
    # Two small clips whose frames carry the flags of the source frame: ``dups``
    # _BEDup (n+1 repeats the drawing of n) and _BEStill (n and n+1 are the same
    # picture), ``cuts`` _BECut (a cut between n and n+1: above ``threshold`` or
    # by the relative rule). A dups frame reads source frames n-2..n+3, a cuts
    # frame n-CUT_BACK..n+CUT_AHEAD+1; kept apart so nothing reaches further back
    # than it has to.
    w = max(16, clip.width // 4 // 2 * 2)
    h = max(16, clip.height // 4 // 2 * 2)
    small = _ahead(core.resize.Bilinear(clip, w, h, format=vs.GRAY8))
    stats = core.std.PlaneStats(core.std.Expr([small, _shift(small, 1)], 'x y - abs'))

    def dup(n, f):
        lo2, lo, here, hi, hi2 = (x.props['PlaneStatsAverage'] for x in f)
        motion = max(min(lo, hi), min(lo2, hi2), min(lo, hi2), min(lo2, hi))
        out = f[2].copy()
        out.props['_BEDup'] = int(here < DUP_ABS
                                  or (here < DUP_NOISY and here * DUP_RATIO < motion))
        out.props['_BEStill'] = int(here < STILL_MEAN and f[2].props['PlaneStatsMax'] <= STILL_MAX)
        return out

    def cut(n, f):
        d = [x.props['PlaneStatsAverage'] for x in f]
        here = d[CUT_BACK]
        around = max([CUT_FLOOR] + d[:CUT_BACK] + d[CUT_BACK + 1:])
        out = f[CUT_BACK].copy()
        out.props['_BECut'] = int(here > threshold
                                  or (here > CUT_MIN and here > CUT_RATIO * around))
        return out

    dups = core.std.ModifyFrame(stats, [_shift(stats, k) for k in range(-2, 3)], dup)
    cuts = core.std.ModifyFrame(stats, [_shift(stats, k) for k in range(-CUT_BACK, CUT_AHEAD + 1)],
                                cut)
    return dups, cuts


def _stills(clip, dups):
    # dups with _BEStill confirmed at full size, on all three planes. Lazy: the
    # full-size difference of frames n and n+1 is computed only for a pair that
    # passed the quarter-size test
    if clip.format.sample_type == vs.FLOAT:
        limit = STILL_FULL / 255
    else:
        limit = STILL_FULL << max(0, clip.format.bits_per_sample - 8)
    diff = core.std.Expr([clip, _shift(clip, 1)], 'x y - abs')
    planes = [core.std.PlaneStats(diff, plane=p, prop='_BEP%d' % p)
              for p in range(clip.format.num_planes)]

    def check(n, f):
        out = f[0].copy()
        out.props['_BEStill'] = int(all(x.props['_BEP%dMax' % p] <= limit
                                        for p, x in enumerate(f[1:])))
        return out

    full = core.std.ModifyFrame(dups, [dups] + planes, check)

    def pick(n, f):
        return full if f.props['_BEStill'] else dups

    return core.std.FrameEval(dups, pick, prop_src=dups)


def _window(dups, cuts, hold):
    # The flags _run reads for frame n, as a prop_src list: dups of
    # n-hold .. n+hold-2, then cuts of n .. n+hold-2
    return ([_shift(dups, k) for k in range(-hold, hold - 1)]
            + [_shift(cuts, k) for k in range(hold - 1)])


def _split(f, hold):
    # the props of a _window list's frames, as (dups, cuts)
    props = [x.props for x in f]
    return props[:2 * hold - 1], props[2 * hold - 1:]


def _run(n, dups, cuts, hold, last):
    # The held drawing frame n is part of, as (p, q): frames p..q-1 show one
    # drawing, q the next. None when frame n starts a drawing, or the drawing is
    # held longer than ``hold`` frames (a deliberate hold), ends in a cut
    # (filling would morph one shot into the next) or at the end of the clip.
    # dups[k]: the flags of frame n-hold+k; cuts[k]: of frame n+k.
    def dup(k):
        return dups[hold + k]['_BEDup']

    back = 0
    while back < hold and n - back > 0 and dup(-back - 1):
        back += 1
    if back == 0 or back == hold:
        return None
    ahead = 0
    while back + ahead < hold and dup(ahead):
        ahead += 1
    p, q = n - back, n + ahead + 1
    if q - p > hold or q > last or cuts[ahead]['_BECut']:
        return None
    return p, q


def _hold_fill(clip, dups, cuts, hold, pairs):
    # Animation drawn on twos or threes repeats each drawing 2-3 frames; every
    # engine interpolates frame to frame, so at 2x it plays still, still, still,
    # jump. Replace frame p+k of a drawing held p..q-1 by the in-between of
    # drawings p and q at k/(q-p): the engine then interpolates smooth motion.
    # Frames that are not in such a hold (live action, motion on ones, long
    # holds, cuts) are passed on untouched. ``pairs(P, K)`` interpolates each
    # pair (P[2i], P[2i+1]) at j/K for j = 1..K-1 and returns {j: clip}.
    # A filled frame n reads source frames n-hold-2 .. n+hold+2.
    steps = {2: 2, 3: 6, 4: 12}[hold]  # lcm(2..hold): every k/(q-p) is j/steps
    last = clip.num_frames - 1
    window = _window(dups, cuts, hold)
    near = {k: _shift(clip, k) for k in range(-hold + 1, hold)}

    def end(i):
        def pick(n, f):
            r = _run(n, *_split(f, hold), hold, last)
            return near[r[i] - n] if r else clip
        return core.std.FrameEval(clip, pick, prop_src=window)

    between = pairs(core.std.Interleave([end(0), end(1)]), steps)

    def fill(n, f):
        r = _run(n, *_split(f, hold), hold, last)
        if r is None:
            return clip
        p, q = r
        return between[(n - p) * steps // (q - p)]

    return core.std.FrameEval(clip, fill, prop_src=window)


def _prepare(clip, cfg, threshold, pairs=None, still=False):
    # The clip an engine interpolates: held drawings filled in (hold_fill, when
    # the engine gives ``pairs``), and _SceneChangeNext on each frame that ends a
    # shot (threshold > 0) or, with still_skip, an identical pair, so the engine
    # repeats the frame instead of blending across the cut or computing a copy.
    # Returns (clip, marked). Hold fill needs cuts even with scene detection off.
    # One output frame between source frames n and n+1 then reads source frames
    # n-CUT_BACK .. n+CUT_AHEAD+2 (-4..+5), with hold fill n-hold-2 .. n+hold+3
    # where that is further (-5..+6 at hold 3); MVTools one more on each side
    # (measured). Before this, n..n+1 (MVTools n-1..n+2).
    sc = isinstance(threshold, (int, float)) and threshold > 0
    fill = bool(cfg.get('hold_fill')) and pairs is not None
    still = still and bool(cfg.get('still_skip'))
    if not (sc or fill or still):
        return clip, False
    # the analysis reads ahead of the engine's own frames through _ahead: near
    # the end in mpv, a frame past it would end playback (hold fill reads the
    # clip itself, but only inside a run the analysis found, never past the end)
    dups, cuts = _analyse(clip, float(threshold) if sc else DEFAULT_SC_THRESHOLD)
    # still pairs confirmed on the source frames, not the filled ones
    stills = _stills(_ahead(clip), dups) if still else dups
    hold = int(cfg.get('hold_max') or 3)
    hold = max(HOLD_RANGE[0], min(hold, HOLD_RANGE[1]))
    if fill:
        clip = _hold_fill(clip, dups, cuts, hold, pairs)
    if not (sc or still):
        return clip, False
    # A still pair with hold fill on: only inside a hold already longer than
    # ``hold`` (never filled), which needs no flags beyond frame n; asking
    # whether frame n+1 is filled would reach further ahead than the fill does
    head = [cuts] if sc else []
    held = [_shift(stills, k) for k in range(-hold + 1, 1)] if fill else [stills]

    def mark(n, f):
        change = sc and f[1].props['_BECut']
        flags = f[1 + len(head):]  # dups up to frame n
        if still and not change and flags[-1].props['_BEStill']:
            change = not fill or (n >= hold - 1 and all(x.props['_BEDup'] for x in flags))
        out = f[0].copy()
        out.props['_SceneChangeNext'] = int(bool(change))
        return out

    return core.std.ModifyFrame(clip, [clip] + head + held, mark), True


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


def _passthrough(clip, interpolated, target, marked=False):
    # At an integer multiplier m every m-th output frame is a source frame: take
    # it from the source untouched (no YUV->RGB->YUV round trip, no dither) and
    # only the others from the engine. Evaluation is lazy, so the engine's output
    # frames at source positions are never requested. ``marked``: the slots after
    # a frame with _SceneChangeNext (a cut or a still pair) repeat that source
    # frame too, which is what the engine would give, minus the round trip.
    # Same format and length, or no change.
    ratio = target / Fraction(clip.fps_num, clip.fps_den) if clip.fps_num else None
    if ratio is None or ratio.denominator != 1 or ratio.numerator < 2:
        return interpolated
    if clip.format.id != interpolated.format.id:
        return interpolated
    m = ratio.numerator
    slots = [core.std.SelectEvery(interpolated, m, i) for i in range(1, m)]
    if marked:
        def repeat(slot):
            def pick(n, f):
                return clip if f.props.get('_SceneChangeNext') else slot
            return core.std.FrameEval(slot, pick, prop_src=clip)
        slots = [repeat(s) for s in slots]
    out = core.std.Interleave([clip] + slots)
    if out.num_frames != interpolated.num_frames:
        return interpolated
    return out


def _rife(clip, cfg, props, target):
    _load('rife', 'librife.so')
    matrix = _matrix(cfg, clip, props)
    rng = _range(cfg, props)
    loc = _chromaloc(cfg, props)
    # uhd is read by RIFE v1-v3 models only; v4.x ignore it (SCOPE 5.3)
    opts = {'model_path': cfg['model_path'], 'gpu_thread': int(cfg.get('gpu_thread') or 4),
            'uhd': bool(cfg.get('uhd'))}
    if cfg.get('gpu_id') is not None:
        opts['gpu_id'] = int(cfg['gpu_id'])

    def pairs(pair_clip, steps):
        # Hold fill: a second RIFE at fixed steps of 1/steps between the pairs.
        # Not one instance for both: the single pass would ask RIFE for t = 1/12
        # or 5/6 across a whole drawing's motion, where it snaps to an end
        # (threes at 2x: step_cv 0.62 against 0.16 in two passes). Half the
        # threads: same speed, half the extra VRAM (720p: +0.8 GB, not +1.7)
        half = dict(opts, gpu_thread=max(1, opts['gpu_thread'] // 2))
        rgb = core.rife.RIFE(_to_rgb(pair_clip, vs.RGBS, matrix, rng, loc), factor_num=steps,
                             factor_den=1, sc=False, **half)
        out = _to_yuv(rgb, pair_clip.format, matrix, rng, loc, cfg)
        return {j: core.std.SelectEvery(out, 2 * steps, j) for j in range(1, steps)}

    clip, marked = _prepare(clip, cfg, cfg.get('sc_threshold'), pairs, still=True)
    rgb = core.rife.RIFE(_to_rgb(clip, vs.RGBS, matrix, rng, loc), fps_num=target.numerator,
                         fps_den=target.denominator, sc=marked, **opts)
    fmt = _output_format(clip, cfg)
    out = _to_yuv(rgb, fmt, matrix, rng, loc, cfg)
    return _passthrough(clip, out, target, marked)


def _fp16_model(network_path, target_network_path, fp16=False, bf16=False,
                input_format=0, output_format=0):
    # Replaces vsmlrt.convert_model (SCOPE §5.2, spike M0(l)): vsmlrt 3.23.2
    # retypes the outputs of RIFE's Cast nodes but not their 'to' attribute, and
    # TensorRT 11 rejects the result. Same conversion with the Cast nodes left out.
    import onnx
    from onnxconverter_common.float16 import convert_float_to_float16

    if bf16 or not fp16:
        raise vs.Error('ButterEye: only fp16 TensorRT engines are supported')
    model = onnx.load(network_path)
    casts = [n.name for n in model.graph.node if n.op_type == 'Cast']
    model = convert_float_to_float16(model, keep_io_types=output_format != 1,
                                     node_block_list=casts)
    onnx.save(model, target_network_path)


# RIFE IFNet flow blocks (v4.x, vs-mlrt v2 export): down-resize factors and
# output-flow multipliers at scale 1, and the flow rescale for blocks 1-3
_FLOW_BLOCKS = (16.0, 8.0, 4.0, 2.0)
_FLOW_NEXT = (0.125, 0.25, 0.5)


def _scale_flow(model, k):
    # RIFE with its optical flow at k x the resolution, warping and blending at
    # full size (Practical-RIFE's 'scale'; spike M0(q)). vs-mlrt patches its v1
    # exports the same way and refuses v4.7+; the v2 export of v4.26 has the
    # same structure. Any constant that isn't exactly as expected stops here.
    from onnx.numpy_helper import from_array, to_array

    g = model.graph
    users = {}
    for n in g.node:
        for x in n.input:
            users[x] = users.get(x, 0) + 1
    const = {n.output[0]: n for n in g.node if n.op_type == 'Constant'}
    produced = {o: n for n in g.node for o in n.output}
    named = {n.name: n for n in g.node}

    def private(name):
        if name not in const or users.get(name) != 1:
            raise vs.Error('ButterEye: unexpected RIFE graph (%s)' % name)
        return const[name]

    def rewrite(cnode, fn):
        t = cnode.attribute[0].t
        t.CopyFrom(from_array(fn(to_array(t).copy()), t.name))

    for i, s in enumerate(_FLOW_BLOCKS):
        down, up = named.get('/block%d/Resize' % i), named.get('/block%d/Resize_1' % i)
        mul = named.get('/block%d/Mul' % i)
        if down is None or up is None or mul is None:
            raise vs.Error('ButterEye: unexpected RIFE graph (block %d)' % i)
        cd, cu = private(down.input[2]), private(up.input[2])
        cm = private(next(x for x in mul.input if x in const))
        if (list(to_array(cd.attribute[0].t)) != [1, 1, 1 / s, 1 / s]
                or list(to_array(cu.attribute[0].t)) != [1, 1, s, s]
                or float(to_array(cm.attribute[0].t)) != s):
            raise vs.Error('ButterEye: unexpected RIFE graph (block %d constants)' % i)
        rewrite(cd, lambda a: (a * [1, 1, k, k]).astype(a.dtype))
        rewrite(cu, lambda a: (a / [1, 1, k, k]).astype(a.dtype))
        rewrite(cm, lambda a: (a / k).astype(a.dtype))
    found = []
    for n in g.node:
        if n.op_type != 'Mul' or n.name.startswith('/block'):
            continue
        cs = [x for x in n.input if x in const]
        others = [x for x in n.input if x not in const]
        if len(cs) != 1 or len(others) != 1:
            continue
        v = to_array(const[cs[0]].attribute[0].t)
        src = produced.get(others[0])
        if (v.size == 1 and float(v) in _FLOW_NEXT and src is not None
                and (src.name == '/block0/Mul' or src.op_type == 'Add')):
            found.append((float(v), n))
    if sorted(v for v, _ in found) != sorted(_FLOW_NEXT):
        raise vs.Error('ButterEye: unexpected RIFE graph (flow rescale)')
    for _, n in found:
        rewrite(private(next(x for x in n.input if x in const)), lambda a: (a * k).astype(a.dtype))
    return model


def _flow_models(trt):
    # vs-mlrt reads models_path/rife_v2/<onnx>.onnx; at a flow scale other than 1
    # that is a patched copy kept with the engine (written when it is built)
    k = float(trt.get('flow_scale') or 1.0)
    if k == 1.0:
        return trt['models_dir']
    folder = os.path.join(trt['engine_dir'], 'models')
    dst = os.path.join(folder, 'rife_v2', trt['onnx'] + '.onnx')
    if not os.path.isfile(dst):
        if not trt.get('build'):
            raise vs.Error('ButterEye: the TensorRT engine for this size is not built yet')
        import onnx

        model = _scale_flow(onnx.load(os.path.join(trt['models_dir'], 'rife_v2',
                                                   trt['onnx'] + '.onnx')), k)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        onnx.save(model, dst + '.part')
        os.replace(dst + '.part', dst)
    return folder


def _rife_trt(clip, cfg, props, target):
    # vs-mlrt's vstrt (SCOPE §5.2). Engines are built out of process before
    # playback; inside mpv a missing engine fails fast instead of freezing the
    # player for minutes ('build' is set only for that out-of-process build).
    if sys.platform == 'darwin':
        # Ahead of the engine check below, which would otherwise blame a missing
        # engine for something no build can produce: there is no CUDA and no
        # TensorRT for macOS, and no Apple Silicon Mac has an NVIDIA GPU.
        raise vs.Error('ButterEye: TensorRT is not available on macOS')
    trt = cfg.get('trt') or {}
    engine_dir = trt['engine_dir']
    if not trt.get('build') and not os.path.isfile(os.path.join(engine_dir, 'ready.json')):
        raise vs.Error('ButterEye: the TensorRT engine for this size is not built yet')
    sys.dont_write_bytecode = True  # nothing is written next to vsmlrt.py
    # vsmlrt looks for a loaded plugin when it is imported: load vstrt first
    if not hasattr(core, 'trt') and os.name == 'nt':  # vs-mlrt's Windows build
        core.std.LoadPlugin(os.path.join(trt['vstrt_dir'], 'vstrt.dll'))
    if not hasattr(core, 'trt'):
        core.std.LoadPlugin(os.path.join(trt['vstrt_dir'], 'libvstrt.so'))
    if trt['vsmlrt_dir'] not in sys.path:
        sys.path.insert(0, trt['vsmlrt_dir'])
    # ButterEye's Python folder first: onnx needs the protobuf installed with it,
    # and Fedora's python3-protobuf (3.19) is too old for it (spike M0(l))
    for extra in reversed(trt.get('python_dirs') or ()):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import vsmlrt

    vsmlrt.trtexec_path = trt['trtexec']
    vsmlrt.models_path = _flow_models(trt)
    vsmlrt.convert_model = _fp16_model
    matrix = _matrix(cfg, clip, props)
    rng = _range(cfg, props)
    loc = _chromaloc(cfg, props)
    # cuts only: vsmlrt reads _SceneChangeNext; no hold fill, no still skip here
    clip, marked = _prepare(clip, cfg, cfg.get('sc_threshold'))
    half = bool(trt.get('half_io'))
    rgb = _to_rgb(clip, vs.RGBH if half else vs.RGBS, matrix, rng, loc)
    backend = vsmlrt.Backend.TRT(fp16=bool(trt.get('fp16')), num_streams=int(trt.get('streams') or 2),
                                 output_format=1 if half else 0, engine_folder=engine_dir,
                                 device_id=int(cfg.get('gpu_id') or 0))
    multi = target / Fraction(clip.fps_num, clip.fps_den)
    rgb = vsmlrt.RIFE(rgb, multi=int(multi) if multi.denominator == 1 else multi,
                      model=vsmlrt.RIFEModel[trt['model']], backend=backend,
                      video_player=True, _implementation=2)
    fmt = _output_format(clip, cfg)
    out = _to_yuv(rgb, fmt, matrix, rng, loc, cfg)
    return _passthrough(clip, out, target, marked)


def _mvtools(clip, cfg, target):
    _load('mv', 'mvtools.so')
    # pel 2 (half-pixel vectors) up to 1080p: at pel 1 a pan of 1 px per frame
    # comes out as exact repeats (judder) and 3 px as uneven steps. Above 1080p
    # pel 1, where a pixel is small and pel 2 costs ~1.35x the CPU. Offline
    # renders ask for 2 (mv_pel)
    pel = cfg.get('mv_pel')
    if pel not in (1, 2, 4):
        pel = 1 if clip.width > 1920 or clip.height > 1080 else 2

    def vectors(c):
        sup = core.mv.Super(c, pel=pel)
        return (sup, core.mv.Analyse(sup, isb=True, blksize=16, overlap=4),
                core.mv.Analyse(sup, isb=False, blksize=16, overlap=4))

    def pairs(pair_clip, steps):
        sup, bv, fv = vectors(pair_clip)
        return {j: core.std.SelectEvery(core.mv.FlowInter(pair_clip, sup, bv, fv,
                                                          time=100.0 * j / steps), 2, 0)
                for j in range(1, steps)}

    # MVTools has its own scene detection, which blends across the cut: the
    # marks repeat the frame instead (integer multipliers), as the other engines do
    threshold = cfg.get('sc_threshold')
    if threshold is None:
        threshold = DEFAULT_SC_THRESHOLD
    clip, marked = _prepare(clip, cfg, threshold, pairs, still=True)
    sup, bv, fv = vectors(clip)
    # BlockFPS: ~2.2x faster in mpv with the same fidelity; blocks show only in very
    # fast motion, so live play uses it only when FlowFPS can't keep up (M0(p))
    fps = core.mv.BlockFPS if cfg.get('mv_mode') == 'block' else core.mv.FlowFPS
    out = fps(clip, sup, bv, fv, num=target.numerator, den=target.denominator)
    return _passthrough(clip, out, target, marked) if marked else out


def _mvgpu(clip, cfg, target):
    # ButterEye's GPU block motion-vector engine (Vulkan, mvgpu/). It takes YUV
    # 4:2:0 at 8-16 bits as it comes (no RGB round trip) and returns source
    # frames untouched; a frame marked _SceneChangeNext is repeated instead of
    # interpolated across the cut, like RIFE.
    _load('mvgpu', 'libmvgpu.so')
    if clip.format.subsampling_w != 1 or clip.format.subsampling_h != 1:
        # mpv hands over what it decoded; 4:2:2 and 4:4:4 go to 4:2:0 here
        fmt = core.query_video_format(vs.YUV, vs.INTEGER, clip.format.bits_per_sample, 1, 1)
        loc = _chromaloc(cfg, None)
        clip = core.resize.Bicubic(clip, format=fmt.id, chromaloc_in_s=loc, chromaloc_s=loc)
    opts = {}
    if cfg.get('gpu_id') is not None:
        opts['device'] = int(cfg['gpu_id'])

    def pairs(pair_clip, steps):
        out = core.mvgpu.Interpolate(pair_clip, fps_num=pair_clip.fps_num * steps,
                                     fps_den=pair_clip.fps_den, **opts)
        return {j: core.std.SelectEvery(out, 2 * steps, j) for j in range(1, steps)}

    clip, _ = _prepare(clip, cfg, cfg.get('sc_threshold'), pairs, still=True)
    return core.mvgpu.Interpolate(clip, fps_num=target.numerator, fps_den=target.denominator,
                                  **opts)


def _shrink(clip, cfg):
    # Smooth at a smaller size when the core asks for one (too demanding at the
    # source size); mpv scales the output to the window like any video.
    size = cfg.get('size')
    if not (isinstance(size, list) and len(size) == 2):
        return clip
    width, height = int(size[0]), int(size[1])
    if width <= 0 or height <= 0 or (width, height) == (clip.width, clip.height):
        return clip
    opts = {}
    if clip.format.color_family == vs.YUV:
        loc = _chromaloc(cfg, None)
        opts = {'chromaloc_in_s': loc, 'chromaloc_s': loc}
    return core.resize.Spline36(clip, width=width, height=height, **opts)


#: engines that run on the GPU, so znedi3 (CPU) anti-aliasing has the CPU to itself
GPU_ENGINES = ('rife-ncnn', 'rife-trt', 'mvgpu')
#: znedi3's cheapest useful network (8x6 neighbourhood, 32 neurons): it runs on
#: every source frame, twice, on the CPU
AA_NNEDI3 = {'nsize': 0, 'nns': 1, 'qual': 1}


def _aa(clip):
    # Anti-aliasing before smoothing (Profile.aa): stair-stepped edges (game
    # captures, cheap upscales) would otherwise be carried into every in-between
    # frame, where they crawl. znedi3 doubles the luma height, the clip is
    # transposed and doubled again, and Spline36 scales it back; field=1 keeps
    # the source rows at odd positions, half a doubled pixel off, which
    # src_left/src_top take back. Luma only: chroma is half size and aliasing
    # shows in luma. Without the znedi3 plugin (or its nnedi3_weights.bin) the
    # clip goes on as it is: AA is an extra, never a reason to stop smoothing.
    fmt = clip.format
    if fmt.color_family not in (vs.YUV, vs.GRAY) or fmt.sample_type != vs.INTEGER:
        return clip
    try:
        if not hasattr(core, 'znedi3'):
            _load('znedi3', 'libznedi3.so')
        y = core.std.ShufflePlanes(clip, 0, vs.GRAY) if fmt.color_family == vs.YUV else clip
        up = core.znedi3.nnedi3(y, field=1, dh=True, **AA_NNEDI3)
        up = core.std.Transpose(up)
        up = core.znedi3.nnedi3(up, field=1, dh=True, **AA_NNEDI3)
        up = core.std.Transpose(up)
        y = core.resize.Spline36(up, y.width, y.height, src_left=0.5, src_top=0.5)
    except (vs.Error, AttributeError):
        return clip
    if fmt.color_family == vs.GRAY:
        return y
    return core.std.ShufflePlanes([y, clip], [0, 1, 2], vs.YUV)


def _repeat(clip, target):
    # ``clip`` at ``target`` fps by repeating frames, as mpv shows it unsmoothed:
    # output frame n is source frame floor(n * src / target). An integer
    # multiplier interleaves the clip with itself; any other is picked per frame
    # (a 2x Interleave of mpv's placeholder length still fits a frame count, a
    # 1001x one for 23.976 -> 60 would not)
    ratio = target / Fraction(clip.fps_num, clip.fps_den)
    if ratio.denominator == 1:
        return core.std.Interleave([clip] * ratio.numerator) if ratio.numerator > 1 else clip
    p, q = ratio.numerator, ratio.denominator
    base = core.std.BlankClip(clip, length=min(clip.num_frames * p // q, 2 ** 31 - 1),
                              fpsnum=target.numerator, fpsden=target.denominator)

    def pick(n):
        return _shift(clip, n * q // p - n)

    return core.std.FrameEval(base, pick)


def _compare(clip, out, target, cfg):
    # A/B view (compare="split"): the source as it plays unsmoothed on the left
    # half, the smoothed picture on the right, from the same position in the
    # film, so the seam shows what interpolation adds. Crops are whole chroma
    # samples (even for 4:2:0). The right half is still computed at full width.
    if target <= Fraction(clip.fps_num, clip.fps_den):
        return out
    left = _repeat(clip, target)
    if left.format.id != out.format.id:
        loc = _chromaloc(cfg, None)
        left = core.resize.Spline36(left, format=out.format.id, chromaloc_in_s=loc,
                                    chromaloc_s=loc)
    n = min(left.num_frames, out.num_frames)
    left, out = left[:n], out[:n]
    step = 1 << out.format.subsampling_w
    half = out.width // 2 // step * step
    if half <= 0 or half >= out.width or left.width != out.width or left.height != out.height:
        return out
    stack = core.std.StackHorizontal([
        core.std.CropAbs(left, width=half, height=out.height, left=0, top=0),
        core.std.CropAbs(out, width=out.width - half, height=out.height, left=half, top=0),
    ])
    # the smoothed frame's properties (StackHorizontal keeps the first clip's)
    return core.std.CopyFrameProps(stack, out) if hasattr(core.std, 'CopyFrameProps') else stack


def build():
    cfg = _settings()
    clip, props = _source(cfg)
    # settled once for every conversion, and stamped on the frames: zimg takes
    # a frame's _ChromaLocation over a chromaloc_in_s argument
    cfg['chromaloc'] = _chromaloc(cfg, props)
    clip = core.std.SetFrameProps(clip, _ChromaLocation=CHROMA_LOCS.index(cfg['chromaloc']))
    clip = _shrink(clip, cfg)
    plain = clip  # what the A/B view shows unsmoothed
    target = _rate(cfg.get('target_fps'))
    backend = cfg.get('backend')
    if cfg.get('aa') and backend in GPU_ENGINES:
        clip = _aa(clip)
    if backend == 'rife-ncnn':
        out = _rife(clip, cfg, props, target)
    elif backend == 'rife-trt':
        out = _rife_trt(clip, cfg, props, target)
    elif backend == 'mvtools':
        out = _mvtools(clip, cfg, target)
    elif backend == 'mvgpu':
        out = _mvgpu(clip, cfg, target)
    else:
        raise vs.Error('ButterEye: unknown backend')
    if cfg.get('compare') == 'split':
        out = _compare(plain, out, target, cfg)
    out = core.std.AssumeFPS(out, fpsnum=target.numerator, fpsden=target.denominator)
    # MVTools reads frames 0 and 1 while the graph is built, before mpv can serve
    # any: mpv hands over black ones, and every filter between the source and
    # MVTools (the analysis, hold fill, Super) would keep what it made of them,
    # half a second of black output at each start. Built: forget all of it
    core.clear_cache()
    return out


build().set_output()
