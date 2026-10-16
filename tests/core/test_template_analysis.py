# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""template.vpy's frame analysis, in-process on synthetic clips (VapourSynth's
own filters only, no engine plugins): the relative scene-cut rule, held
drawings and still pairs, the hold-fill bookkeeping, the marks that make an
engine repeat a frame, and the chroma siting of the RGB round trip."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from fractions import Fraction
from typing import Any

import pytest

from buttereye.core.scriptgen import generator as g

vs = pytest.importorskip("vapoursynth")
np = pytest.importorskip("numpy")
core = vs.core

W, H = 320, 192
SKY, GROUND, INK, FILL = (170, 128, 120), (110, 110, 120), (16, 128, 128), (150, 60, 200)


def _ns() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    text = g.render_script().replace("build().set_output()", "")
    exec(compile(text, "gen.vpy", "exec"), ns)
    return ns


NS = _ns()


def _draw(x: float, bg: float = 0.0, scale: float = 1.0) -> tuple[Any, Any, Any]:
    """A flat-shaded 2D frame (Y, U, V): sky, ground, fence posts that pan with
    ``bg``, and an outlined disc at ``x``; ``scale`` darkens it (fades)."""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    planes = [np.empty((H, W)) for _ in range(3)]
    for p in range(3):
        planes[p][:] = SKY[p]
        planes[p][yy >= 130] = GROUND[p]
        planes[p][(yy > 90) & (yy < 130) & (((xx + bg) % 32) < 3)] = INK[p]
        d = np.hypot(xx - x, yy - 100)
        planes[p][d < 25] = INK[p]
        planes[p][d < 22] = FILL[p]
    planes[0] = 16 + (planes[0] - 16) * scale
    y = np.clip(planes[0] + 0.5, 0, 255).astype(np.uint8)
    u, v = (np.clip(pl[::2, ::2] + 0.5, 0, 255).astype(np.uint8) for pl in planes[1:])
    return y, u, v


def _clip(frames: Sequence[tuple[Any, Any, Any]]) -> Any:
    base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, length=len(frames),
                              fpsnum=24, fpsden=1)  # fmt: skip

    def paint(n: int, f: Any) -> Any:
        out = f.copy()
        for p in range(3):
            np.asarray(out[p])[:] = frames[n][p]
        return out

    return core.std.ModifyFrame(base, base, paint)


def _cadence(hold: int, n: int = 36, step: float = 6.0) -> Any:
    """Motion drawn on ones / twos / threes: each drawing held ``hold`` frames."""
    return _clip([_draw(60 + step * hold * (k // hold)) for k in range(n)])


def _marks(clip: Any, cfg: dict[str, Any] | None = None, threshold: float = 0.12) -> list[int]:
    out, marked = NS["_prepare"](clip, cfg or {}, threshold, None, True)
    assert marked
    return [n for n, f in enumerate(out.frames()) if f.props["_SceneChangeNext"]]


# ---- scene cuts ----------------------------------------------------------------


def test_same_backdrop_cut_is_found_by_the_relative_rule_only() -> None:
    frames = [_draw(60 + 3 * k) for k in range(16)] + [_draw(260 - 3 * k, bg=9) for k in range(16)]
    clip = _clip(frames)
    stats = core.std.PlaneStats(clip, clip[1:] + clip[-1])
    full = [f.props["PlaneStatsDiff"] for f in stats.frames()]
    assert max(full) < 0.12  # the absolute threshold alone misses it
    assert _marks(clip) == [15]


def test_fade_flash_and_cadences_add_no_cuts() -> None:
    fade = _clip([_draw(60 + 3 * k, scale=1 - k / 24) for k in range(24)])
    assert _marks(fade) == []
    flash = [_draw(60 + 3 * k) for k in range(20)]
    flash[9] = tuple(np.full_like(p, 235 if i == 0 else 128) for i, p in enumerate(flash[9]))
    assert _marks(_clip(flash)) == [8, 9]  # into and out of the white frame: absolute rule
    for hold in (1, 2, 3, 4):
        assert _marks(_cadence(hold)) == [], hold
    mixed = _clip([_draw(60 + 12 * (k // 2), bg=4 * k) for k in range(36)])
    assert _marks(mixed) == []


def test_no_marks_without_scene_detection_or_still_skip() -> None:
    clip = _cadence(2)
    out, marked = NS["_prepare"](clip, {}, 0)
    assert out is clip and not marked
    out, marked = NS["_prepare"](clip, {}, None)
    assert out is clip and not marked


# ---- held drawings and still pairs ----------------------------------------------


def _flags(clip: Any) -> tuple[list[int], list[int], list[int]]:
    dups, cuts = NS["_analyse"](clip, 0.12)
    d = [(f.props["_BEDup"], f.props["_BEStill"]) for f in dups.frames()]
    return [a for a, _ in d], [b for _, b in d], [f.props["_BECut"] for f in cuts.frames()]


def test_twos_flag_every_repeat_and_ones_none() -> None:
    dup, still, cut = _flags(_cadence(2, n=12))
    assert dup[:11] == [1, 0] * 5 + [1] and still[:11] == dup[:11] and not any(cut)
    dup, still, _ = _flags(_cadence(1, n=12))
    assert not any(dup[:11]) and not any(still[:11])


def test_a_small_moving_object_is_never_a_still_pair() -> None:
    # a 6 px dot moving 2 px per frame over a flat field: tiny mean difference
    def dot(x: int) -> tuple[Any, Any, Any]:
        y, u, v = _draw(-100)
        y = y.copy()
        y[50:56, x : x + 6] = 16
        return y, u, v

    _, still, _ = _flags(_clip([dot(40 + 2 * k) for k in range(10)]))
    assert not any(still[:9])


def test_a_tiny_slow_object_is_not_copied_by_still_skip() -> None:
    # 2 px, 2 px per frame at 720p: at quarter size every other pair looks
    # identical; the full-size check keeps it from stalling
    w, h = 1280, 720
    bg = core.std.BlankClip(format=vs.YUV420P8, width=w, height=h, length=1, color=[110, 128, 128])
    dot = core.std.BlankClip(bg, width=2, height=2, color=[170, 128, 128])
    clip = core.std.Splice([core.std.MaskedMerge(
        bg, core.std.AddBorders(dot, left=200 + 2 * k, right=w - 202 - 2 * k, top=300,
                                bottom=h - 302),
        core.std.AddBorders(core.std.BlankClip(width=2, height=2, format=vs.GRAY8, color=255),
                            left=200 + 2 * k, right=w - 202 - 2 * k, top=300, bottom=h - 302),
        first_plane=True) for k in range(10)])  # fmt: skip
    _, still, _ = _flags(clip)
    assert any(still[:9])  # what the quarter-size test alone would copy
    assert _marks(clip, {"still_skip": True})[:-1] == []
    # a real still pair is still copied
    assert _marks(core.std.Splice([clip[0]] * 4), {"still_skip": True}) == [0, 1, 2, 3]


def _props(dups: str, cuts: str = "") -> tuple[list[dict[str, int]], list[dict[str, int]]]:
    return ([{"_BEDup": int(c)} for c in dups],
            [{"_BECut": int(c)} for c in cuts.ljust(len(dups), "0")])  # fmt: skip


@pytest.mark.parametrize(
    ("n", "dups", "cuts", "want"),
    [
        # dups[k] is frame n-3+k (hold 3); cuts[k] is frame n+k
        (5, "00100", "", (4, 6)),  # second frame of a drawing on twos
        (5, "00010", "", None),  # first frame of a drawing: never filled
        (5, "01100", "", (3, 6)),  # third frame of threes
        (5, "00110", "", (4, 7)),  # second frame of threes
        (5, "11100", "", None),  # a hold of four: deliberate
        (5, "00111", "", None),  # also four, seen from its second frame
        (5, "00100", "1", None),  # the drawing ends in a cut: no morph across it
        (1, "00100", "", (0, 2)),  # at the start of the clip
    ],
)
def test_run_finds_short_holds_only(
    n: int, dups: str, cuts: str, want: tuple[int, int] | None
) -> None:
    d, c = _props(dups, cuts)
    assert NS["_run"](n, d, c, 3, 100) == want


def test_run_stops_at_the_end_of_the_clip() -> None:
    d, c = _props("00100")
    assert NS["_run"](5, d, c, 3, 5) is None  # q = 6 is past the last frame


def _fake_pairs(record: list[int]) -> Callable[[Any, int], dict[int, Any]]:
    # "interpolates" pair i at j/K as a flat frame with luma 100 + j
    def pairs(pair_clip: Any, steps: int) -> dict[int, Any]:
        record.append(steps)
        half = pair_clip.num_frames // 2
        return {
            j: core.std.BlankClip(pair_clip, length=half, color=[100 + j, 128, 128])
            for j in range(1, steps)
        }

    return pairs


def _fill(clip: Any, hold: int = 3) -> list[int]:
    """Per frame: 0 = untouched, else the step j the fill took."""
    dups, cuts = NS["_analyse"](clip, 0.12)
    steps: list[int] = []
    out = NS["_hold_fill"](clip, dups, cuts, hold, _fake_pairs(steps))
    src = list(clip.frames())
    got = []
    for n, f in enumerate(out.frames()):
        same = np.array_equal(np.asarray(f[0]), np.asarray(src[n][0]))
        got.append(0 if same else int(np.asarray(f[0])[0, 0]) - 100)
    return got


def test_hold_fill_retimes_twos_and_threes_and_leaves_the_rest() -> None:
    assert _fill(_cadence(2, n=12))[:10] == [0, 3] * 5  # K = 6: half way is j = 3
    assert _fill(_cadence(3, n=12))[:9] == [0, 2, 4] * 3  # thirds
    assert not any(_fill(_cadence(1, n=12)))
    assert not any(_fill(_cadence(4, n=12)))  # longer than hold_max 3
    assert _fill(_cadence(4, n=12), hold=4)[:8] == [0, 3, 6, 9] * 2  # K = 12


def test_hold_fill_never_bridges_a_cut() -> None:
    a = [_draw(60 + 6 * (k // 2)) for k in range(16)]
    b = [_draw(260 - 6 * (k // 2), bg=9) for k in range(16)]
    got = _fill(_clip(a + b))
    assert got[13] == 3 and got[15] == 0  # the drawing held into the cut stays as it is
    assert got[17] == 3


def test_still_marks_with_hold_fill_only_inside_long_holds() -> None:
    clip = _cadence(5, n=20)  # every drawing held 5 frames: never filled
    pairs = _fake_pairs([])
    still = _marks(clip, {"still_skip": True}, 0)
    # (the last frame pairs with itself)
    assert still == [k for k in range(19) if k % 5 != 4] + [19]
    out, _ = NS["_prepare"](clip, {"still_skip": True, "hold_fill": True}, 0, pairs, True)
    marked = [n for n, f in enumerate(out.frames()) if f.props["_SceneChangeNext"]]
    # from the third frame of each hold on (hold_max 3): a pair inside a hold
    # already longer than any filled one
    assert marked == [k for k in range(19) if k % 5 in (2, 3)] + [19]
    twos = _cadence(2, n=12)
    out, _ = NS["_prepare"](twos, {"still_skip": True, "hold_fill": True}, 0, pairs, True)
    assert not any(f.props["_SceneChangeNext"] for f in out.frames())  # every pair filled


def test_still_skip_is_off_unless_asked() -> None:
    clip = _cadence(5, n=10)
    assert _marks(clip) == []  # scene detection alone: holds are not cuts
    out, marked = NS["_prepare"](clip, {"still_skip": True}, 0, None, False)
    assert not marked  # an engine that does not offer it (TensorRT)


# ---- the end of the video in mpv ------------------------------------------------


def _mpv_source(clip: Any, message: str = "EOF or filter EOF/reinit") -> Any:
    """``clip`` as mpv's filter hands it over: a placeholder length, and a request
    past the real end fails (playback would end with the output frame asking)."""
    real = clip.num_frames
    padded = clip + core.std.BlankClip(clip, length=200 - real)

    def frame(n: int) -> Any:
        if n >= real:
            raise vs.Error(message)
        return clip

    return core.std.FrameEval(padded, frame)


def _luma(f: Any) -> Any:
    return np.asarray(f[0]).copy()


def test_ahead_repeats_the_last_frame_past_the_end_in_mpv(monkeypatch: Any) -> None:
    clip = _cadence(1, n=8)
    src = _mpv_source(clip)
    assert NS["_ahead"](src) is src  # offline: the clip, whose length is real
    monkeypatch.setitem(NS, "video_in", src)
    view = NS["_ahead"](src)
    want = [_luma(clip.get_frame(min(n, 7))) for n in range(14)]
    # out of order, as VapourSynth's threads ask: past the end first
    for n in (12, 9, 3, 8, 13, 0, 7, 10, 1, 2, 4, 5, 6, 11):
        assert np.array_equal(_luma(view.get_frame(n)), want[n]), n
    with pytest.raises(vs.Error):
        src.get_frame(8)
    # any other failure is not the end, and fails as before
    view = NS["_ahead"](_mpv_source(clip, "Frame 0 requested, but only have frames from 3"))
    with pytest.raises(vs.Error, match="only have frames"):
        view.get_frame(9)


@pytest.mark.parametrize("hold", [1, 2, 3, 5])
@pytest.mark.parametrize("end", ["moving", "held"])
def test_analysis_in_mpv_matches_offline_to_the_last_frame(
    monkeypatch: Any, hold: int, end: str
) -> None:
    # Every frame up to the last one is there (none of them reads past the end)
    # and is what the offline script gives, whose clip has its real length:
    # marks, still pairs and hold fill near the end decided the same way
    frames = [_draw(60 + 6 * hold * (k // hold)) for k in range(14)]
    if end == "held":
        frames += [frames[-1]] * 4
    clip = _clip(frames)
    cfg = {"still_skip": True, "hold_fill": True}
    pairs = _fake_pairs([])
    offline, _ = NS["_prepare"](clip, cfg, 0.12, pairs, True)
    want = [(_luma(f), f.props["_SceneChangeNext"]) for f in offline.frames()]
    src = _mpv_source(clip)
    monkeypatch.setitem(NS, "video_in", src)
    out, _ = NS["_prepare"](src, cfg, 0.12, pairs, True)
    for n in reversed(range(clip.num_frames)):  # the end first, as after a seek
        f = out.get_frame(n)
        assert np.array_equal(_luma(f), want[n][0]), n
        assert f.props["_SceneChangeNext"] == want[n][1], n


# ---- marked slots and chroma siting ---------------------------------------------


def test_passthrough_repeats_the_source_after_a_marked_frame() -> None:
    src = core.std.BlankClip(format=vs.YUV420P8, width=64, height=32, length=4,
                             fpsnum=24, fpsden=1, color=[200, 128, 128])  # fmt: skip
    src = core.std.ModifyFrame(src, src, lambda n, f: _mark_copy(f, n == 1))
    interp = core.std.BlankClip(src, length=8, fpsnum=48, color=[16, 128, 128])
    out = NS["_passthrough"](src, interp, Fraction(48), True)
    luma = [int(np.asarray(f[0])[0, 0]) for f in out.frames()]
    assert luma == [200, 16, 200, 200, 200, 16, 200, 16]


def _mark_copy(f: Any, mark: bool) -> Any:
    out = f.copy()
    out.props["_SceneChangeNext"] = int(mark)
    return out


def test_chromaloc_from_user_data_then_props_then_left() -> None:
    loc = NS["_chromaloc"]
    assert loc({"chromaloc": "top_left"}, {"_ChromaLocation": 1}) == "top_left"
    assert loc({}, {"_ChromaLocation": 1}) == "center"
    assert loc({"chromaloc": "bogus"}, {"_ChromaLocation": 2}) == "top_left"
    assert loc({}, None) == "left" and loc({}, {"_ChromaLocation": 9}) == "left"


@pytest.mark.parametrize("loc", ["left", "center", "top_left"])
def test_rgb_round_trip_keeps_the_chroma_where_it_was(loc: str) -> None:
    y, u, v = _draw(150)
    clip = _clip([(y, u, v)])
    clip = core.std.SetFrameProps(clip, _ChromaLocation=NS["CHROMA_LOCS"].index(loc))
    rgb = NS["_to_rgb"](clip, vs.RGBS, "709", "limited", loc)
    back = NS["_to_yuv"](rgb, clip.format, "709", "limited", loc, {})
    f = back.get_frame(0)
    assert f.props["_ChromaLocation"] == NS["CHROMA_LOCS"].index(loc)
    err = np.abs(np.asarray(f[2]).astype(int) - v.astype(int))[4:-4, 4:-4]
    assert err.mean() < 1.0  # a half-pixel shift would leave edges ~10x worse
