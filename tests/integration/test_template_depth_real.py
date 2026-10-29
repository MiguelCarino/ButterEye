# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The colour depth each engine's chain puts out, with the real engines: a
dark ramp with no dither (a sky), moving 3 px a frame, quantised to the
source's depth, through the template as mpv hands it over (``video_in``).
Counts the distinct luma codes of a synthesised frame: a 12-bit source keeps
12 bits on every engine (RIFE cut it to 10), and an 8-bit source saved as 10-bit
gets RIFE's float precision instead of 8 bits padded (44 codes against ~165)."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.scriptgen.generator import FilterParams, render_script, user_data
from buttereye.core.types import BackendId

vs = pytest.importorskip("vapoursynth")
np = pytest.importorskip("numpy")
core = vs.core

pytestmark = [pytest.mark.integration, pytest.mark.devbox, pytest.mark.gpu]

W, H, N = 640, 368, 8
MODEL = Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse")
ENGINES = {"mvtools": BackendId.MVTOOLS, "mvgpu": BackendId.MVGPU, "rife": BackendId.RIFE_NCNN}


def _source(fmt_id: int) -> Any:
    base = core.std.BlankClip(width=W, height=H, format=vs.RGBS, length=N, fpsnum=24, fpsden=1)

    def mk(n: int, f: Any) -> Any:
        out = f.copy()
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        ramp = ((xx + 0.37 * yy + 3.0 * n) % 400) / 400.0
        for p in range(3):
            np.asarray(out[p])[:] = 0.02 + 0.22 * ramp * (1.0 - 0.15 * p)
        return out

    rgb = core.std.ModifyFrame(base, base, mk)
    return core.resize.Point(rgb, format=fmt_id, matrix_s="709", range_s="limited",
                             dither_type="none")  # fmt: skip


def _run(engine: str, src: Any, **extra: Any) -> Any:
    be = ENGINES[engine]
    if be is BackendId.RIFE_NCNN and not MODEL.is_dir():
        pytest.skip("RIFE models not installed")
    p = FilterParams(gen=1, backend=be, src_fps=Fraction(24), target_fps=Fraction(48),
                     buffered_frames=4, concurrent_frames=8, matrix="709", range="limited",
                     model_path=MODEL if be is BackendId.RIFE_NCNN else None,
                     gpu_id=0 if be is not BackendId.MVTOOLS else None,
                     gpu_thread=2 if be is BackendId.RIFE_NCNN else None,
                     sc_threshold=0.12, dither="error_diffusion", chroma_loc="left",
                     mv_pel=2 if be is BackendId.MVTOOLS else None, **extra)  # fmt: skip
    vs.clear_outputs()
    ns: dict[str, Any] = {"__name__": "__vapoursynth__", "video_in": src,
                          "user_data": user_data(p), "container_fps": 24.0}  # fmt: skip
    try:
        exec(compile(render_script(), "gen.vpy", "exec"), ns)
    except vs.Error as e:
        if "LoadPlugin" in str(e) or "No entry point" in str(e):
            pytest.skip(f"{engine} plugin not loadable: {e}")
        raise
    out = vs.get_output(0)
    return out.clip if isinstance(out, vs.VideoOutputTuple) else out


def _codes(clip: Any, n: int) -> int:
    y = np.asarray(clip.get_frame(n)[0]).astype(np.int64)
    return len(np.unique(y[40 : H - 40, 40 : W - 40]))


@pytest.mark.parametrize("engine", list(ENGINES))
@pytest.mark.parametrize(("fmt", "bits"), [("YUV420P8", 8), ("YUV420P10", 10), ("YUV420P12", 12)])
def test_every_engine_keeps_the_source_depth(engine: str, fmt: str, bits: int) -> None:
    src = _source(getattr(vs, fmt))
    out = _run(engine, src)
    assert out.format.bits_per_sample == bits
    # source frame 2 is output frame 4; 5 is synthesised
    assert _codes(out, 4) >= 0.95 * _codes(src, 2)
    assert _codes(out, 5) >= 0.9 * _codes(src, 2)


def test_rife_writes_ten_bits_of_an_eight_bit_source_when_asked() -> None:
    src = _source(vs.YUV420P8)
    eight = _run("rife", src)
    ten = _run("rife", src, out_bits=10)
    assert ten.format.bits_per_sample == 10
    assert _codes(ten, 5) > 2 * _codes(eight, 5)  # ~165 codes, not 44 padded
    # the source frames are the source's, shifted up exactly
    assert (np.asarray(ten.get_frame(4)[0]) == np.asarray(src.get_frame(2)[0]) * 4).all()


@pytest.mark.parametrize("engine", ["mvtools", "mvgpu"])
def test_a_ten_bit_source_saved_as_eight_bit_is_reduced_in_the_chain(engine: str) -> None:
    out = _run(engine, _source(vs.YUV420P10), out_bits=8)
    assert out.format.id == vs.YUV420P8
