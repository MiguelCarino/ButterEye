# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""template.vpy's A/B split view and anti-aliasing step, in-process on synthetic
clips (VapourSynth's own filters only): the source repeated to the target rate,
the halves at even crops, mpv's placeholder length, and AA without znedi3."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from buttereye.core.scriptgen import generator as g

vs = pytest.importorskip("vapoursynth")
core = vs.core
NTSC = Fraction(24000, 1001)


def _ns() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    text = g.render_script().replace("build().set_output()", "")
    exec(compile(text, "gen.vpy", "exec"), ns)
    return ns


NS = _ns()


def _numbered(length: int = 50) -> Any:
    """A 23.976 fps clip whose frame n carries the property N = n."""
    base = core.std.BlankClip(format=vs.YUV420P8, width=64, height=32, length=length,
                              fpsnum=NTSC.numerator, fpsden=NTSC.denominator)  # fmt: skip
    return core.std.FrameEval(base, lambda n: core.std.SetFrameProps(base, N=n))


@pytest.mark.parametrize(
    ("target", "first"),
    [
        (NTSC * 2, [0, 0, 1, 1, 2, 2, 3, 3]),
        (NTSC * 3, [0, 0, 0, 1, 1, 1, 2, 2]),
        (Fraction(60), [0, 0, 0, 1, 1, 1, 2, 2, 3, 3, 3, 4]),  # 2.5x, as mpv shows it
    ],
)
def test_source_is_repeated_to_the_target_rate(target: Fraction, first: list[int]) -> None:
    rep = NS["_repeat"](_numbered(), target)
    assert Fraction(rep.fps_num, rep.fps_den) == target
    assert [rep.get_frame(i).props["N"] for i in range(len(first))] == first


def test_mpv_placeholder_length_still_fits() -> None:
    big = core.std.BlankClip(format=vs.YUV420P8, width=64, height=32, length=134217727,
                             fpsnum=NTSC.numerator, fpsden=NTSC.denominator)  # fmt: skip
    assert NS["_repeat"](big, Fraction(60)).num_frames < 2**31
    assert NS["_repeat"](big, NTSC * 2).num_frames == 2 * 134217727


def test_split_puts_the_source_left_and_the_smoothed_picture_right() -> None:
    src = _numbered()
    out = core.std.BlankClip(src, color=[235, 128, 128], length=125, fpsnum=60, fpsden=1)
    out = core.std.SetFrameProps(out, Smoothed=1)
    stack = NS["_compare"](src, out, Fraction(60), {})
    assert (stack.width, stack.height, stack.format.id) == (64, 32, out.format.id)
    f = stack.get_frame(3)
    plane = f[0]
    assert plane[0, 31] == 0 and plane[0, 32] == 235  # the seam at an even column
    assert f.props["Smoothed"] == 1  # the smoothed frame's properties
    ten = core.resize.Point(out, format=vs.YUV420P10)
    assert NS["_compare"](src, ten, Fraction(60), {}).format.id == ten.format.id
    # nothing to compare when the target is the source rate
    same = core.std.AssumeFPS(out, fpsnum=NTSC.numerator, fpsden=NTSC.denominator)
    assert NS["_compare"](src, same, NTSC, {}) is same


def test_aa_without_znedi3_leaves_the_clip() -> None:
    if hasattr(core, "znedi3"):
        pytest.skip("znedi3 is installed here")
    src = _numbered()
    assert NS["_aa"](src) is src
