# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""template.vpy's output depth, in-process without engine plugins: the format
RIFE's float output is written at (``out_bits``, else the source's with no cap
at 10), the source frames passed through at another depth, and the reduction
at the end for an engine that works at the source's depth. The real engines
are in tests/integration/test_template_depth_real.py."""

from __future__ import annotations

import json
from fractions import Fraction
from typing import Any

import pytest

from buttereye.core.scriptgen import generator as g
from buttereye.core.types import BackendId

vs = pytest.importorskip("vapoursynth")
np = pytest.importorskip("numpy")
core = vs.core


def _ns() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    text = g.render_script().replace("build().set_output()", "")
    exec(compile(text, "gen.vpy", "exec"), ns)
    return ns


NS = _ns()


def _blank(fmt: int, length: int = 4) -> Any:
    return core.std.BlankClip(format=fmt, width=64, height=32, length=length, fpsnum=24,
                              fpsden=1)  # fmt: skip


@pytest.mark.parametrize(
    ("fmt", "cfg", "bits"),
    [
        (vs.YUV420P8, {}, 8),
        (vs.YUV420P10, {}, 10),
        (vs.YUV420P12, {}, 12),  # was cut to 10 (bug A)
        (vs.YUV444P12, {}, 12),
        (vs.YUV420P8, {"hdr": True}, 10),
        (vs.YUV420P12, {"hdr": True}, 12),
        (vs.YUV420P8, {"out_bits": 10}, 10),  # 10-bit chosen: not dithered to 8 first (bug B)
        (vs.YUV420P12, {"out_bits": 10}, 10),
        (vs.YUV420P10, {"out_bits": 8}, 8),
        (vs.YUV420P8, {"out_bits": 10, "hdr": True}, 10),
    ],
)
def test_output_format(fmt: int, cfg: dict[str, Any], bits: int) -> None:
    clip = _blank(fmt)
    out = NS["_output_format"](clip, cfg)
    assert out.bits_per_sample == bits
    assert (out.subsampling_w, out.subsampling_h) == (
        clip.format.subsampling_w, clip.format.subsampling_h,
    )  # fmt: skip


def test_source_frames_pass_through_at_12_bits_and_at_another_depth() -> None:
    src = core.std.BlankClip(_blank(vs.YUV420P12), color=[3000, 2048, 2048])
    interp = core.std.BlankClip(src, length=8, fpsnum=48, color=[256, 2048, 2048])
    out = NS["_passthrough"](src, interp, Fraction(48))
    assert out is not interp and out.format.id == vs.YUV420P12
    assert [int(out.get_frame(n)[0][0, 0]) for n in range(4)] == [3000, 256, 3000, 256]
    # the copy is written at 10 bits: the source frames are reduced to it
    ten = core.std.BlankClip(interp, format=vs.YUV420P10, color=[64, 512, 512])
    down = NS["_passthrough"](src, ten, Fraction(48), cfg={"dither": "none"})
    assert down.format.id == vs.YUV420P10 and int(down.get_frame(0)[0][0, 0]) == 750


def _build(src: Any, **cfg: Any) -> Any:
    """``build()`` with a stand-in engine that repeats each frame (it keeps the
    source's format, as MVTools and mvgpu do)."""
    params = g.FilterParams(gen=1, backend=BackendId.MVTOOLS, src_fps=Fraction(24),
                            target_fps=Fraction(48), buffered_frames=4, concurrent_frames=8,
                            matrix="709", range="limited", **cfg)  # fmt: skip
    ns = _ns()  # its own globals: the template's functions read them
    ns.update(video_in=src, user_data=g.user_data(params), container_fps=24.0)
    ns["_mvtools"] = lambda clip, _cfg, _target: core.std.Interleave([clip, clip])
    exec("out = build()", ns)
    return ns["out"]


def _ramp(fmt: int, bits: int) -> Any:
    base = _blank(fmt, 2)
    top = (1 << bits) - 1

    def paint(n: int, f: Any) -> Any:
        out = f.copy()
        y = np.asarray(out[0])
        y[:] = (np.arange(y.shape[1]) * (top // y.shape[1]))[None, :] + n
        return out

    return core.std.ModifyFrame(base, base, paint)


def test_the_chain_reduces_a_shallower_copy_itself() -> None:
    src = _ramp(vs.YUV420P10, 10)
    kept = _build(src)
    assert kept.format.id == vs.YUV420P10  # no out_bits (live play): as it came
    down = _build(src, out_bits=8, dither="error_diffusion")
    assert down.format.id == vs.YUV420P8
    up = _build(_ramp(vs.YUV420P8, 8), out_bits=10)
    assert up.format.id == vs.YUV420P10
    y8 = np.asarray(_ramp(vs.YUV420P8, 8).get_frame(0)[0]).astype(int)
    assert (np.asarray(up.get_frame(0)[0]).astype(int) == y8 * 4).all()  # exact going up
    assert _build(src, out_bits=10).format.id == vs.YUV420P10  # nothing to do


def test_user_data_carries_out_bits_and_the_dither_for_every_engine() -> None:
    base = dict(gen=1, src_fps=Fraction(24), target_fps=Fraction(48), buffered_frames=4,
                concurrent_frames=8, dither="error_diffusion")  # fmt: skip
    live = json.loads(g.user_data(g.FilterParams(backend=BackendId.MVGPU, **base)))  # type: ignore[arg-type]
    assert "out_bits" not in live and "dither" not in live  # live play: unchanged
    copy = json.loads(g.user_data(g.FilterParams(backend=BackendId.MVGPU, out_bits=8, **base)))  # type: ignore[arg-type]
    assert copy["out_bits"] == 8 and copy["dither"] == "error_diffusion"


def test_a_blank_source_can_be_12_bits() -> None:
    ns = _ns()
    cfg = {"source": {"kind": "blank", "fps": [24, 1], "width": 64, "height": 32,
                      "bits": 12, "length": 2}}  # fmt: skip
    clip, _props = ns["_source"](cfg)
    assert clip.format.id == vs.YUV420P12
