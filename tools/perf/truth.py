#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Ground-truth test for in-between frames: how wrong is each engine, and where.

Scenes are drawn at twice the source rate, each isolating one thing that breaks
interpolation (an object covering a background, thin lines, a repeating pattern,
motion past the search range, a fade, grain, a zoom). The engine gets every
second frame and has to rebuild the ones between; those are compared with the
drawn ones. Real footage is tested the same way with ``--clip PATH@t1,t2`` (or
``PATH@auto20`` for 20 moments spread over the file): frames n and n+2 go in,
frame n+1 is the truth.

The number to watch is **worst areas**: the mean SSIM of the worst 0.25 % of
the 16x16 blocks of luma (9 blocks at 720p, so a 20 px object counts). A torn
edge, a hole or a ghost is a small area that is very wrong; the mean (PSNR)
barely moves for it, and a soft blend scores well there while looking bad.
``mean`` is the mean block SSIM and ``psnr`` luma PSNR.

Runs in the system Python (VapourSynth and numpy), GPU engines included; no
model is loaded unless ``--engines`` names rife. Nothing is downloaded.

Usage:
  truth.py [--engines blend,mvgpu,mvtools,rife] [--cases pan,occlusion,...]
           [--mvgpu PATH/libmvgpu.so] [--clip PATH@t1,t2 ...] [--json OUT]
           [--reference FILE.py] [--size 1280x720]

``trt-<model>`` runs a RIFE model through TensorRT as playback does, for every
``rife_v<model>.onnx`` under ``--trt-models`` (``trt-4.26``, ``trt-4.25_heavy``).
The first run at a frame size builds the engine, which takes minutes.

``--reference FILE.py`` adds an engine of your own for comparison: a Python file
with ``interpolate(core, clip, num, den)`` returning the clip at num/den fps.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

PLUGINS = Path("/usr/lib64/buttereye/vapoursynth")
MODELS = Path("/usr/share/buttereye/rife-ncnn-models")
BLOCK = 16


# ---------------------------------------------------------------- drawing


def _texture(np: Any, rng: Any, h: int, w: int, cutoff: float, power: float = 1.5) -> Any:
    """Periodic band-limited noise in 0..1: detail down to ``cutoff`` cycles a
    pixel, so it can be shifted by a fraction of a pixel exactly (``_shift``)."""
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.rfftfreq(w)[None, :]
    r = np.sqrt(fy * fy + fx * fx)
    amp = np.where(r > 0, 1.0 / np.maximum(r, 1.0 / max(h, w)) ** power, 0.0)
    amp = amp * (r <= cutoff)
    spec = amp * np.exp(2j * np.pi * rng.random((h, w // 2 + 1)))
    img = np.fft.irfft2(spec, s=(h, w))
    img -= img.min()
    return img / img.max()


def _shift(np: Any, img: Any, dx: float, dy: float) -> Any:
    """``img`` moved by (dx, dy) pixels, wrapping; exact for band-limited input."""
    h, w = img.shape
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.rfftfreq(w)[None, :]
    return np.fft.irfft2(np.fft.rfft2(img) * np.exp(-2j * np.pi * (fx * dx + fy * dy)), s=(h, w))


def _soft(np: Any, mask: Any) -> Any:
    """A hard 0/1 mask with a 2 px soft edge, so it survives a sub-pixel shift."""
    k = np.array([1, 4, 6, 4, 1], dtype=np.float64) / 16.0
    for axis in (0, 1):
        mask = sum(k[i] * np.roll(mask, i - 2, axis=axis) for i in range(5))
    return mask


def _box(np: Any, h: int, w: int, x0: int, y0: int, bw: int, bh: int) -> Any:
    m = np.zeros((h, w))
    m[y0 : y0 + bh, x0 : x0 + bw] = 1.0
    return _soft(np, m)


class Layer:
    """A picture and its coverage, both moving ``v`` pixels a drawn frame."""

    def __init__(self, y: Any, u: Any, v_: Any, alpha: Any, v: tuple[float, float]) -> None:
        self.planes, self.alpha, self.v = (y, u, v_), alpha, v


def _scene(np: Any, name: str, h: int, w: int) -> tuple[list[Layer], Callable[[int, Any], Any]]:
    """The layers of a scene, back to front, and a per-frame change of the
    finished luma (a fade, grain). Speeds are per drawn frame: the engine sees
    twice that between its source frames."""
    rng = np.random.default_rng(sum(map(ord, name)))  # the same scene every run

    def tex(cut: float = 0.25, lo: float = 0.1, hi: float = 0.9) -> tuple[Any, Any, Any]:
        y = lo + (hi - lo) * _texture(np, rng, h, w, cut)
        u = 0.35 + 0.3 * _texture(np, rng, h, w, 0.06)
        v_ = 0.35 + 0.3 * _texture(np, rng, h, w, 0.06)
        return y, u, v_

    def flat(level: float) -> tuple[Any, Any, Any]:
        ramp = np.linspace(-0.04, 0.04, w)[None, :] + np.zeros((h, 1))
        return level + ramp, np.full((h, w), 0.5), np.full((h, w), 0.5)

    full = np.ones((h, w))
    same: Callable[[int, Any], Any] = lambda t, y: y  # noqa: E731
    post = same
    bg = Layer(*tex(), full, (0.0, 0.0))
    if name == "pan":
        layers = [Layer(*tex(), full, (3.0, 0.0))]
    elif name == "pan_odd":  # 7 x 3 px between source frames: half-pixel in-betweens
        layers = [Layer(*tex(), full, (3.5, 1.5))]
    elif name == "occlusion":  # a textured object over a still, textured background
        layers = [
            bg,
            Layer(
                *tex(0.25, 0.2, 1.0), _box(np, h, w, w // 8, h // 3, h // 4, h // 4), (12.0, 0.0)
            ),
        ]
    elif name == "occlusion_both":  # object one way, background the other
        bg.v = (-4.0, 0.0)
        layers = [
            bg,
            Layer(
                *tex(0.25, 0.2, 1.0), _box(np, h, w, w // 8, h // 3, h // 4, h // 4), (10.0, 2.0)
            ),
        ]
    elif name == "thin":  # thin bars over a nearly flat backdrop
        m = np.zeros((h, w))
        for i in range(6):
            x = w // 6 + i * 40
            m[h // 5 : h - h // 5, x : x + 3] = 1.0
        layers = [
            Layer(*flat(0.45), full, (0.0, 0.0)),
            Layer(*flat(0.92), _soft(np, m), (8.0, 0.0)),
        ]
    elif name == "repeat":  # a fence (period 16 px) passing a background going the other way
        bg.v = (-1.0, 0.0)
        m = np.zeros((h, w))
        m[h // 4 : h - h // 4, w // 4 : w - w // 4] = (np.arange(w // 2)[None, :] % 16 < 6) * 1.0
        # 6 px between source frames: under half the period, so not ambiguous
        layers = [bg, Layer(*flat(0.15), _soft(np, m), (3.0, 0.0))]
    elif name == "large":  # 96 px between source frames
        layers = [
            bg,
            Layer(
                *tex(0.25, 0.2, 1.0), _box(np, h, w, w // 10, h // 3, h // 5, h // 5), (48.0, 0.0)
            ),
        ]
    elif name == "small_fast":  # a small object, fast, over detail
        layers = [bg, Layer(*flat(0.95), _box(np, h, w, w // 8, h // 2, 20, 20), (20.0, 6.0))]
    elif name == "fade":  # a pan while the picture fades
        layers = [Layer(*tex(), full, (3.0, 0.0))]
        post = lambda t, y: y * (1.0 - 0.04 * t)  # noqa: E731
    elif name == "soft":  # a slow pan of soft, low-contrast shapes under light grain:
        # little to match on, so each pair of frames can settle on other vectors
        layers = [Layer(*tex(0.02, 0.4, 0.6), full, (2.5, 1.0))]
        noise = np.random.default_rng(11)
        post = lambda t, y: y + noise.normal(0.0, 0.01, y.shape)  # noqa: E731
    elif name == "grain":  # a pan under new grain every frame
        layers = [Layer(*tex(), full, (3.0, 1.0))]
        noise = np.random.default_rng(7)
        post = lambda t, y: y + noise.normal(0.0, 0.02, y.shape)  # noqa: E731
    elif name == "text":  # rows of small blocky glyphs rolling up, like credits
        m = np.zeros((h, w))
        g = np.random.default_rng(3)
        for row in range(h // 12, h - 40, 44):
            for col in range(w // 5, w - w // 5, 18):
                glyph = g.random((5, 3)) > 0.4
                m[row : row + 20, col : col + 12] = np.kron(glyph, np.ones((4, 4)))
        layers = [
            Layer(*flat(0.08), full, (0.0, 0.0)),
            Layer(*flat(0.9), _soft(np, m), (0.0, -2.5)),
        ]
    else:
        raise SystemExit(f"unknown case: {name}")
    return layers, post


CASES = ("pan", "pan_odd", "occlusion", "occlusion_both", "thin", "repeat", "large",
         "small_fast", "fade", "grain", "soft", "text")  # fmt: skip


def draw(np: Any, name: str, h: int, w: int, frames: int) -> list[tuple[Any, Any, Any]]:
    """``frames`` drawn frames of a scene as 8-bit (Y, U, V) 4:2:0."""
    layers, post = _scene(np, name, h, w)
    out = []
    for t in range(frames):
        acc = [np.zeros((h, w)) for _ in range(3)]
        for layer in layers:
            dx, dy = layer.v[0] * t, layer.v[1] * t
            a = np.clip(_shift(np, layer.alpha, dx, dy), 0.0, 1.0) if layer.alpha.min() < 1 else 1.0
            for i in range(3):
                acc[i] = acc[i] * (1.0 - a) + _shift(np, layer.planes[i], dx, dy) * a
        y = post(t, acc[0])
        planes = [np.clip(16 + 219 * y, 0, 255)]
        for c in acc[1:]:
            half = c.reshape(h // 2, 2, w // 2, 2).mean(axis=(1, 3))
            planes.append(np.clip(16 + 224 * half, 0, 255))
        out.append(tuple(np.round(p).astype(np.uint8) for p in planes))
    return out


# ---------------------------------------------------------------- measuring


def block_ssim(np: Any, a: Any, b: Any) -> Any:
    """SSIM of each whole 16x16 block of two 8-bit pictures."""
    h, w = (a.shape[0] // BLOCK) * BLOCK, (a.shape[1] // BLOCK) * BLOCK

    def blocks(x: Any) -> Any:
        x = x[:h, :w].astype(np.float64)
        return (
            x.reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK)
            .swapaxes(1, 2)
            .reshape(-1, BLOCK * BLOCK)
        )

    x, y = blocks(a), blocks(b)
    mx, my = x.mean(1), y.mean(1)
    vx, vy = x.var(1), y.var(1)
    cov = ((x - mx[:, None]) * (y - my[:, None])).mean(1)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    return ((2 * mx * my + c1) * (2 * cov + c2)) / ((mx * mx + my * my + c1) * (vx + vy + c2))


def _real_truths(lumas: list[Any]) -> list[tuple[int, Any]]:
    """The in-between frames a real moment is scored on. Five frames: both (each
    has a real frame on one side of its pair only). Seven (--around): the middle
    one, whose pair has a real frame before it and one after, as nearly every
    pair has in playback."""
    if len(lumas) >= 7:
        return [(3, lumas[3])]
    return [(1, lumas[1]), (3, lumas[3])]


def score(np: Any, got: Any, truth: Any) -> dict[str, float]:
    s = block_ssim(np, got, truth)
    mse = float(np.mean((got.astype(np.float64) - truth.astype(np.float64)) ** 2))
    return {
        "worst": round(float(np.sort(s)[: max(4, s.size // 400)].mean()), 4),
        "mean": round(float(s.mean()), 4),
        "psnr": round(10 * math.log10(255.0 * 255.0 / mse), 2) if mse > 0 else 99.0,
    }


def flicker(np: Any, outs: list[Any], truths: list[Any]) -> float:
    """How much the error changes from one in-between frame to the next, in its
    worst areas (8-bit levels; the mean over the worst 0.25 % of 16x16 blocks).

    An error that stays - a soft edge, a slightly wrong position - is one
    thing; an error that is there in one frame and gone or elsewhere in the
    next is seen as shimmer, and it is what vectors chosen afresh for every
    pair of frames produce where the picture gives them little to hold on to.
    The truth's own change is taken out, so motion is not counted."""
    worst = []
    for k in range(len(outs) - 1):
        e0 = outs[k].astype(np.float64) - truths[k]
        e1 = outs[k + 1].astype(np.float64) - truths[k + 1]
        d = np.abs(e1 - e0)
        h, w = (d.shape[0] // BLOCK) * BLOCK, (d.shape[1] // BLOCK) * BLOCK
        b = d[:h, :w].reshape(h // BLOCK, BLOCK, w // BLOCK, BLOCK).mean(axis=(1, 3)).ravel()
        worst.append(float(np.sort(b)[-max(4, b.size // 400) :].mean()))
    return round(max(worst), 3) if worst else 0.0


# ---------------------------------------------------------------- engines


def _load(core: Any, namespace: str, path: Path) -> None:
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.fspath(path))


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "buttereye"


def _grid_nodes(model: Any) -> list[str]:
    """As template.vpy: the nodes that work out where GridSample reads stay in
    full precision (half floats cannot hold a position in a 1080p frame)."""
    stop = {"Conv", "ConvTranspose", "DepthToSpace", "Resize", "Sigmoid", "LeakyRelu",
            "GridSample", "Pad", "Shape"}  # fmt: skip
    by_output = {o: n for n in model.graph.node for o in n.output}
    names, todo, seen = [], [], set()
    for n in model.graph.node:
        if n.op_type == "GridSample":
            names.append(n.name)
            todo.append(n.input[1])
    while todo:
        n = by_output.get(todo.pop())
        if n is None or n.name in seen or n.op_type in stop:
            continue
        seen.add(n.name)
        names.append(n.name)
        todo.extend(n.input)
    return names


def _vsmlrt(core: Any, vs: Any, args: argparse.Namespace) -> Any:
    """vs-mlrt's wrapper set up as template.vpy sets it up (the user's install)."""
    data = _data_home()
    folders = sorted((data / "plugins" / "vstrt").glob("*/libvstrt.so"))
    if not folders:
        raise SystemExit("no TensorRT plugin under " + os.fspath(data / "plugins" / "vstrt"))
    folder = folders[-1].parent
    _load(core, "trt", folder / "libvstrt.so")
    for extra in (folder, data / "python"):
        if os.fspath(extra) not in sys.path:
            sys.path.insert(0, os.fspath(extra))
    sys.dont_write_bytecode = True
    import vsmlrt  # type: ignore[import-not-found]

    def fp16_model(network_path: str, target_network_path: str, fp16: bool = False,
                   bf16: bool = False, input_format: int = 0,
                   output_format: int = 0) -> None:  # fmt: skip
        # as template.vpy: vsmlrt's own conversion breaks RIFE's Cast nodes
        import onnx  # type: ignore[import-not-found]
        from onnxconverter_common.float16 import (  # type: ignore[import-not-found]
            convert_float_to_float16,
        )

        model = onnx.load(network_path)
        casts = [n.name for n in model.graph.node if n.op_type == "Cast"]
        if "drba" in Path(network_path).name:
            casts += _grid_nodes(model)
        half = convert_float_to_float16(model, keep_io_types=True, node_block_list=casts)
        onnx.save(half, target_network_path)

    vsmlrt.trtexec_path = "trtexec"
    vsmlrt.models_path = os.fspath(args.trt_models)
    vsmlrt.convert_model = fp16_model
    args.trt_engines.mkdir(parents=True, exist_ok=True)
    return vsmlrt


def engines(core: Any, vs: Any, args: argparse.Namespace) -> dict[str, Callable[[Any], Any]]:
    """name -> f(clip at the source rate) = the clip at twice that."""

    def blend(clip: Any) -> Any:
        nxt = clip[1:] + clip[-1]
        mid = core.std.Merge(clip, nxt)
        return core.std.Interleave([clip, mid])

    def mvgpu(clip: Any) -> Any:
        _load(core, "mvgpu", Path(args.mvgpu) if args.mvgpu else PLUGINS / "libmvgpu.so")
        return core.mvgpu.Interpolate(clip, fps_num=clip.fps_num * 2, fps_den=clip.fps_den)

    def mvtools(clip: Any) -> Any:  # as ButterEye's script runs it
        _load(core, "mv", PLUGINS / "mvtools.so")
        sup = core.mv.Super(clip, pel=2)
        search = dict(blksize=16, overlap=4, searchparam=4, badsad=1000, badrange=-24)
        bv = core.mv.Analyse(sup, isb=True, **search)
        fv = core.mv.Analyse(sup, isb=False, **search)
        return core.mv.FlowFPS(clip, sup, bv, fv, num=clip.fps_num * 2, den=clip.fps_den)

    def rife(model: str) -> Callable[[Any], Any]:
        def run(clip: Any) -> Any:
            _load(core, "rife", PLUGINS / "librife.so")
            rgb = core.resize.Spline36(
                clip, format=vs.RGBS, matrix_in_s="709", range_in_s="limited"
            )
            out = core.rife.RIFE(rgb, model_path=os.fspath(MODELS / model), factor_num=2,
                                 factor_den=1, gpu_thread=2, sc=False)  # fmt: skip
            return core.resize.Spline36(out, format=clip.format.id, matrix_s="709",
                                        range_s="limited", dither_type="none")  # fmt: skip

        return run

    def trt(stem: str) -> Callable[[Any], Any]:
        def run(clip: Any) -> Any:
            vsmlrt = _vsmlrt(core, vs, args)
            rgb = core.resize.Spline36(
                clip, format=vs.RGBS, matrix_in_s="709", range_in_s="limited"
            )
            backend = vsmlrt.Backend.TRT(fp16=True, num_streams=1,
                                         engine_folder=os.fspath(args.trt_engines))  # fmt: skip
            member = "v" + stem.replace(".", "_")
            out = vsmlrt.RIFE(rgb, multi=2, model=vsmlrt.RIFEModel[member], backend=backend,
                              _implementation=2)  # fmt: skip
            return core.resize.Spline36(out, format=clip.format.id, matrix_s="709",
                                        range_s="limited", dither_type="none")  # fmt: skip

        return run

    def drba(onnx: Path) -> Callable[[Any], Any]:
        def run(clip: Any) -> Any:
            # DistilDRBA exported by contrib/drba_export.py: the frame before, the
            # nearest frame, the frame after and the time (0.5 = halfway back)
            vsmlrt = _vsmlrt(core, vs, args)
            rgb = core.resize.Spline36(
                clip, format=vs.RGBS, matrix_in_s="709", range_in_s="limited"
            )
            last = clip.num_frames - 1
            nearest = core.std.DuplicateFrames(rgb, last)[1:]
            after = core.std.DuplicateFrames(rgb, [last, last])[2:]
            time = core.std.BlankClip(rgb, format=vs.GRAYS, color=0.5)
            backend = vsmlrt.Backend.TRT(fp16=True, num_streams=1,
                                         engine_folder=os.fspath(args.trt_engines))  # fmt: skip
            mid = vsmlrt.inference([rgb, nearest, after, time], os.fspath(onnx), backend=backend)
            out = core.std.Interleave([rgb, mid])
            return core.resize.Spline36(out, format=clip.format.id, matrix_s="709",
                                        range_s="limited", dither_type="none")  # fmt: skip

        return run

    table: dict[str, Callable[[Any], Any]] = {
        "blend": blend,
        "mvgpu": mvgpu,
        "mvtools": mvtools,
        "rife": rife("rife-v4.26_ensembleFalse"),
        "rife-lite": rife("rife-v4.22_lite_ensembleFalse"),
    }
    for onnx in sorted((args.trt_models / "rife_v2").glob("rife_v*.onnx")):
        stem = onnx.stem.removeprefix("rife_v")
        table["trt-" + stem] = trt(stem)
    for onnx in sorted((args.trt_models / "drba").glob("drba_*.onnx")):
        table[onnx.stem.replace("_", "-", 1)] = drba(onnx)
    if args.reference:
        spec = importlib.util.spec_from_file_location("truth_reference", args.reference)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        table["reference"] = lambda clip: mod.interpolate(
            core, clip, clip.fps_num * 2, clip.fps_den
        )
    return table


def as_clip(core: Any, vs: Any, np: Any, frames: list[tuple[Any, Any, Any]]) -> Any:
    h, w = frames[0][0].shape
    blank = core.std.BlankClip(format=vs.YUV420P8, width=w, height=h, length=len(frames),
                               fpsnum=24, fpsden=1)  # fmt: skip

    def fill(n: int, f: Any) -> Any:
        out = f.copy()
        for p in range(3):
            np.copyto(np.asarray(out[p]), frames[n][p])
        return out

    return core.std.SetFrameProps(core.std.ModifyFrame(blank, blank, fill), _Matrix=1)


def luma(np: Any, clip: Any, n: int) -> Any:
    return np.asarray(clip.get_frame(n)[0]).copy()


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--engines", default="blend,mvgpu,mvtools")
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--pairs", type=int, default=3, help="in-between frames scored per scene")
    ap.add_argument("--mvgpu", help="a libmvgpu.so to test instead of the installed one")
    ap.add_argument("--trt-models", type=Path, default=_data_home() / "models" / "vsmlrt",
                    help="the folder holding rife_v2/rife_v*.onnx")  # fmt: skip
    ap.add_argument(
        "--trt-engines",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
        / "buttereye-dev" / "trt-engines",
        help="where built TensorRT engines are kept",
    )
    ap.add_argument("--reference", help="a Python file with interpolate(core, clip, num, den)")
    ap.add_argument("--clip", action="append", default=[], help="PATH@t1,t2 (real footage)")
    ap.add_argument(
        "--min-motion",
        type=float,
        default=1.0,
        help="skip a real triple whose neighbouring frames differ by less (mean, 8-bit)",
    )
    ap.add_argument("--save-set", type=Path, help="keep the real moments scored, decoded, here")
    ap.add_argument("--set", type=Path, help="score the moments kept by --save-set (no decoding)")
    ap.add_argument(
        "--around",
        action="store_true",
        help="real moments: seven frames, not five, and score the middle in-between "
        "frame only (engines that read the pairs before and after)",
    )
    ap.add_argument("--first", action="store_true",
                    help="real moments: score the first in-between frame only, the one "
                    "with a real frame after its pair (engines that read three frames)",
                    )  # fmt: skip
    ap.add_argument("--height", type=int, help="scale real footage down to this height")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--dump", type=Path, help="write PNGs of each engine's worst frame here")
    args = ap.parse_args()

    import numpy as np
    import vapoursynth as vs

    core = vs.core
    w, h = (int(x) for x in args.size.lower().split("x"))
    table = engines(core, vs, args)
    names = [e for e in args.engines.split(",") if e] + (["reference"] if args.reference else [])
    unknown = [e for e in names if e not in table]
    if unknown:
        raise SystemExit(f"unknown engine: {', '.join(unknown)} (have {', '.join(table)})")

    tests: list[
        tuple[str, Any, list[tuple[int, Any]]]
    ] = []  # (name, source clip, [(out index, truth)])
    for case in [c for c in args.cases.split(",") if c]:
        # one source frame more than is scored: an engine that reads the frame
        # after the pair (DRBA) must not find a repeat there
        drawn = draw(np, case, h, w, 2 * args.pairs + 3)
        src = as_clip(core, vs, np, drawn[0::2])
        tests.append((case, src, [(2 * i + 1, drawn[2 * i + 1][0]) for i in range(args.pairs)]))
    count_frames = 7 if args.around else 5
    for i, spec in enumerate(args.clip):
        path, _, times = spec.rpartition("@")
        if not hasattr(core, "ffms2"):
            core.std.LoadPlugin("/usr/lib64/libffms2.so.5")
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "buttereye-dev"
        cache.mkdir(parents=True, exist_ok=True)
        full = core.ffms2.Source(
            path, cachefile=os.fspath(cache / f"truth-{zlib.crc32(os.fsencode(path)):08x}.ffindex")
        )
        full = (
            core.resize.Bicubic(full, format=vs.YUV420P8) if full.format.id != vs.YUV420P8 else full
        )
        if args.height and full.height > args.height:
            wide = round(full.width * args.height / full.height / 8) * 8
            full = core.resize.Bicubic(full, wide, args.height)
        fps = full.fps_num / full.fps_den
        if times.startswith("auto"):
            # that many moments, evenly spread over the middle nine tenths
            count, length = int(times[4:] or 20), full.num_frames / fps
            times = ",".join(
                f"{length * (0.05 + 0.9 * (k + 0.5) / count):.2f}" for k in range(count)
            )
        for t in times.split(","):
            n = round(float(t) * fps)
            # footage that holds frames (a 24 fps film in a 60 fps recording, drawings
            # on twos) or does not move has no in-between frame to rebuild
            if n + count_frames - 1 >= full.num_frames:
                continue
            five = [luma(np, full, n + k).astype(np.int16) for k in range(count_frames)]
            steps = sorted(
                float(np.abs(five[k + 1] - five[k]).mean()) for k in range(count_frames - 1)
            )
            # ... and across a cut there is nothing to rebuild either: one step
            # far larger than the others
            if steps[0] < args.min_motion or steps[-1] > 4.0 * steps[1] + 2.0:
                continue
            src = full[n]
            for k in range(2, count_frames, 2):
                src += full[n + k]
            src = core.std.AssumeFPS(src, fpsnum=24, fpsden=1)
            truths = _real_truths([f.astype(np.uint8) for f in five])
            tests.append((f"clip{i + 1}@{t}", src, truths))
            if args.save_set is not None:
                # the frames as they are, so later runs need no decoding
                args.save_set.mkdir(parents=True, exist_ok=True)
                planes = [
                    np.stack(
                        [np.asarray(full.get_frame(n + k)[pl]) for k in range(count_frames)]
                    )
                    for pl in range(3)
                ]
                np.savez(
                    args.save_set / f"clip{i + 1}@{t}.npz", y=planes[0], u=planes[1], v=planes[2]
                )
    if args.set is not None:
        for path in sorted(
            args.set.glob("*.npz"), key=lambda q: (int(q.name[4:].split("@")[0]), q.name)
        ):
            d = np.load(path)
            frames = [(d["y"][k], d["u"][k], d["v"][k]) for k in range(d["y"].shape[0])]
            src = as_clip(core, vs, np, frames[0::2])
            tests.append((path.stem, src, _real_truths([f[0] for f in frames])))
    if args.first:
        tests = [(c, src, truths[:1] if c.startswith("clip") else truths)
                 for c, src, truths in tests]  # fmt: skip

    results: dict[str, dict[str, dict[str, float]]] = {}
    for case, src, truths in tests:
        results[case] = {}
        for name in names:
            try:
                out = table[name](src)
                outs = [luma(np, out, k) for k, _ in truths]
                rows = [score(np, o, truth) for o, (_, truth) in zip(outs, truths, strict=True)]
            except Exception as exc:  # noqa: BLE001 - one engine failing is a result
                print(f"{case}/{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            results[case][name] = {
                "worst": min(r["worst"] for r in rows),
                "mean": round(sum(r["mean"] for r in rows) / len(rows), 4),
                "psnr": round(sum(r["psnr"] for r in rows) / len(rows), 2),
                "flicker": flicker(np, outs, [t for _, t in truths]),
            }
            if args.dump is not None:
                args.dump.mkdir(parents=True, exist_ok=True)
                k = min(zip(rows, truths, strict=True), key=lambda rt: rt[0]["worst"])[1][0]
                rgb = core.resize.Bicubic(out[k], format=vs.RGB24, matrix_in_s="709")
                frame = rgb.get_frame(0)
                img = np.dstack([np.asarray(frame[p]) for p in range(3)])
                (args.dump / f"{case}-{name}.ppm").write_bytes(
                    b"P6 %d %d 255\n" % (img.shape[1], img.shape[0]) + img.tobytes()
                )
                # where it is wrong: the difference from the real frame, 8x
                i = [kk for kk, _ in truths].index(k)
                err = np.clip(np.abs(outs[i].astype(np.int16) - truths[i][1]) * 8, 0, 255)
                (args.dump / f"{case}-{name}-error.pgm").write_bytes(
                    b"P5 %d %d 255\n" % (err.shape[1], err.shape[0])
                    + err.astype(np.uint8).tobytes()
                )

    print(
        "worst areas (mean SSIM of the worst 0.25 % of 16x16 blocks; 1.0 = the real frame)"
        " / luma PSNR\n"
    )
    print("| scene | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for case, row in results.items():
        cells = [
            f"{row[n]['worst']:.3f} / {row[n]['psnr']:.1f}" if n in row else "-" for n in names
        ]
        print(f"| {case} | " + " | ".join(cells) + " |")
    totals = []
    for n in names:
        got = [row[n]["worst"] for row in results.values() if n in row]
        totals.append(f"{sum(got) / len(got):.3f}" if got else "-")
    print("| **mean of worst** | " + " | ".join(totals) + " |")
    print("\nflicker (how much the error changes between in-between frames, worst areas,")
    print("8-bit levels; 0 = none)\n")
    print("| scene | " + " | ".join(names) + " |")
    print("|---|" + "---|" * len(names))
    for case, row in results.items():
        cells = [f"{row[n]['flicker']:.2f}" if n in row else "-" for n in names]
        print(f"| {case} | " + " | ".join(cells) + " |")
    totals = []
    for n in names:
        got = [row[n]["flicker"] for row in results.values() if n in row]
        totals.append(f"{sum(got) / len(got):.2f}" if got else "-")
    print("| **mean** | " + " | ".join(totals) + " |")
    clips = sorted(
        {c.split("@")[0] for c in results if c.startswith("clip")}, key=lambda c: int(c[4:])
    )
    if len(clips) > 1:
        print("\nby clip: worst areas / flicker (mean over its moments), and moments scored\n")
        print("| clip | " + " | ".join(names) + " | n |")
        print("|---|" + "---|" * (len(names) + 1))
        for c in clips:
            rows = [row for case, row in results.items() if case.split("@")[0] == c]
            cells = []
            for n in names:
                got = [r[n] for r in rows if n in r]
                cells.append(
                    f"{sum(g['worst'] for g in got) / len(got):.3f} / "
                    f"{sum(g['flicker'] for g in got) / len(got):.1f}"
                    if got
                    else "-"
                )
            print(f"| {c} | " + " | ".join(cells) + f" | {len(rows)} |")
    if args.json is not None:
        args.json.write_text(json.dumps({"size": [w, h], "results": results}, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
