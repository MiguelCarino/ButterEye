# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Is this video hand-drawn 2D animation? (``mpvctl.content``, docs/spikes/m0w.md)

An Auto profile lets ButterEye look: where drawings are held for two or three
frames the video is smoothed with the 2D rules (RIFE first, motion vectors last,
no cascade, held drawings re-timed). The user's own choice, 2D or 3D, always wins.
No mpv, no GPU and no ffmpeg here: the judging, the remembered answers and the
session's decisions are real, the probe's answer is given."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import content, ipc
from buttereye.core.mpvctl import session as live
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId, BenchResult, ContentKind, Profile, Target, TargetKind
from buttereye.gui import simple_window as sw
from tests.test_live_cascade import _ctx, _devbox, _display
from tests.test_live_decisions import _load, _session
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture
from tests.test_live_trt import TrtFake, _filters, _ready
from tests.test_live_trt import fake_trt as _fake_trt_fixture

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture
fake_trt = _fake_trt_fixture

FILM = "/videos/film.mkv"  # the path tests.test_live_decisions._load gives mpv
MOVE, HELD = 6.0, 0.05  # a frame that changed, a drawing shown again (0..255)


# ---------------------------------------------------------------- the judging
def test_drawings_on_twos_are_2d_animation() -> None:
    run = [MOVE, HELD] * 24
    found = content.judge([run, run])
    assert found.kind is ContentKind.ANIMATION and found.judged == 96
    assert found.share == pytest.approx(0.5)


def test_drawings_on_threes_are_2d_animation() -> None:
    found = content.judge([[MOVE, HELD, HELD] * 16] * 2)
    assert found.kind is ContentKind.ANIMATION and found.share == pytest.approx(2 / 3)


def test_footage_that_changes_on_every_frame_is_not() -> None:
    found = content.judge([[MOVE + (i % 5) for i in range(48)]] * 3)
    assert found.kind is ContentKind.LIVE and found.share == 0.0 and found.judged == 144


def test_a_still_shot_says_nothing() -> None:
    """Nothing moves near these pairs: any kind of video repeats frames there."""
    found = content.judge([[0.02] * 48] * 4)
    assert found.kind is None and found.judged == 0 and found.share == 0.0


def test_too_few_moving_pairs_cannot_tell() -> None:
    run = [0.02] * 30 + [MOVE, HELD] * 9
    found = content.judge([run])
    assert found.kind is None and 0 < found.judged < content.MIN_JUDGED


def test_a_noisy_held_drawing_still_counts() -> None:
    """Film grain or a soft encode: the repeat is not exact, but it is under a
    quarter of the change around it."""
    found = content.judge([[12.0, 2.0] * 24] * 2)
    assert found.kind is ContentKind.ANIMATION
    busy = content.judge([[12.0, 4.0] * 24] * 2)  # a third of the change: it moved
    assert busy.kind is ContentKind.LIVE


def test_the_share_decides_at_the_threshold() -> None:
    def run(held: int) -> list[float]:
        # spread through the run in pairs, as drawings on twos come (evenly spaced
        # single repeats would be a frame-rate conversion, content.converted)
        starts = [round(j * 100 / ((held + 1) // 2)) for j in range((held + 1) // 2)]
        at = {i for s in starts for i in (s, s + 1)}
        at = set(sorted(at)[:held])
        return [HELD if i in at else MOVE for i in range(100)]

    below = content.judge([run(int(content.HELD_SHARE * 100) - 3)])
    above = content.judge([run(int(content.HELD_SHARE * 100) + 3)])
    assert below.kind is ContentKind.LIVE and above.kind is ContentKind.ANIMATION


def test_parse_reads_what_ffmpeg_prints() -> None:
    text = (
        "frame:0    pts:0       pts_time:0\n"
        "lavfi.signalstats.YAVG=5.861146\n"
        "frame:1    pts:1001    pts_time:0.0417083\n"
        "lavfi.signalstats.YAVG=0.031250\n"
        "lavfi.signalstats.YMIN=0\n"
    )
    assert content.parse(text) == [5.861146, 0.03125]
    assert content.parse("") == []


def test_the_command_reads_a_short_run_and_writes_nothing() -> None:
    cmd = content.command("ffmpeg", FILM, 312.5)
    assert cmd[0] == "ffmpeg" and cmd[-3:] == ["-f", "null", "-"]
    assert cmd[cmd.index("-ss") + 1] == "312.50" and cmd[cmd.index("-i") + 1] == FILM
    assert "-noaccurate_seek" in cmd and cmd[cmd.index("-frames:v") + 1] == str(content.FRAMES)
    chain = cmd[cmd.index("-vf") + 1]
    assert "tblend=all_mode=difference" in chain and "signalstats" in chain
    assert "-an" in cmd and "-nostdin" in cmd


def test_only_local_films_are_looked_at(tmp_path: Path) -> None:
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    assert content.local_film(str(film), 23.976) and content.local_film(str(film), 29.97)
    assert not content.local_film(str(film), 59.94)  # held frames mean nothing there
    assert not content.local_film("https://example.org/film.mkv", 24.0)
    assert not content.local_film(str(tmp_path / "missing.mkv"), 24.0)
    assert not content.local_film("", 24.0)
    assert content.worth_probing(str(film), 24.0, 1500.0)
    assert not content.worth_probing(str(film), 24.0, 8.0)  # no room for the runs
    assert not content.worth_probing(str(film), 24.0, None)


# ---------------------------------------------------------------- remembered answers
def test_answers_are_remembered_without_the_files_name(tmp_path: Path) -> None:
    film = tmp_path / "A Very Private Title.mkv"
    film.write_bytes(b"picture")
    key = content.file_key(str(film))
    assert key is not None and len(key) == 32
    store = tmp_path / "state" / content.CACHE_FILE
    cache = content.Cache(store)
    assert cache.get(key) is None
    cache.put(key, content.Found(ContentKind.ANIMATION, 0.4321, 150))
    again = content.Cache(store)  # a new start reads the file
    assert again.get(key) == content.Found(ContentKind.ANIMATION, 0.4321, 150)
    text = store.read_text()
    assert "Private" not in text and str(tmp_path) not in text
    assert json.loads(text)["schema"] == 1
    # the same name with another picture is another file
    film.write_bytes(b"another picture, longer")
    assert content.file_key(str(film)) != key
    assert content.file_key(str(tmp_path / "missing.mkv")) is None


def test_the_store_survives_damage_and_stays_small(tmp_path: Path) -> None:
    store = tmp_path / content.CACHE_FILE
    store.write_text("{ not json")
    cache = content.Cache(store)
    assert cache.entries == {}
    for i in range(content.CACHE_MAX + 20):
        cache.entries[f"k{i:04d}"] = {"kind": "live", "share": 0.0, "judged": 100, "at": i}
    cache.put("newest", content.Found(None, 0.0, 3))
    assert len(cache.entries) == content.CACHE_MAX
    assert "k0000" not in cache.entries and "newest" in cache.entries
    assert content.Cache(store).get("newest") == content.Found(None, 0.0, 3)
    cache.put(None, content.Found(ContentKind.LIVE, 0.0, 100))  # no key: nothing kept


def test_the_file_length_may_be_read_from_mpv() -> None:
    assert "duration" in ipc.READABLE


# ---------------------------------------------------------------- the session
def _auto() -> Profile:
    return _display()  # the shipped profile: content Auto, "Match your display"


def _knows(s: live._Session, kind: ContentKind | None, share: float = 0.4) -> None:
    """The probe has already answered for this file."""
    s.content_file = FILM
    s.content_found = content.Found(kind, share, 150)


async def test_a_2d_video_gets_the_2d_rules_by_itself(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """1080p film on a 144 Hz screen is the cascade's case (tests.test_live_cascade).
    Found to be hand-drawn, it is not: motion vectors lose large flat shapes, so
    RIFE makes the frames alone."""
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _auto())
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    _knows(s, ContentKind.ANIMATION)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and "cascade_fps" not in f
    assert s.cascade is None
    assert s.run_content is ContentKind.ANIMATION and s.run_content_found
    snap = s.snapshot()
    assert snap.content is ContentKind.ANIMATION and snap.content_detected


async def test_other_video_plays_as_before(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    for kind in (ContentKind.LIVE, None):
        bench_history.clear()
        bench_history.append(_devbox())
        (tmp_path / str(kind)).mkdir()
        ctx = _ctx(tmp_path / str(kind), _auto())
        s = _session(ctx, tmp_path / str(kind))
        _ready(ctx, s, 1920, 1080)
        _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
        _knows(s, kind, 0.03)
        await s.apply(raise_on_fail=False)
        (f,) = _filters(s)
        assert f["cascade_fps"] == [48, 1], kind
        assert s.run_content is ContentKind.AUTO and not s.run_content_found
        assert not s.snapshot().content_detected


async def test_the_users_choice_wins_over_the_probe(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    as_3d = dataclasses.replace(_auto(), content=ContentKind.LIVE)
    ctx = _ctx(tmp_path, as_3d)
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    _knows(s, ContentKind.ANIMATION)  # even a remembered "2D"
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["cascade_fps"] == [48, 1]
    assert s.run_content is ContentKind.LIVE and not s.run_content_found
    assert not s.content_wait(as_3d, dataclasses.replace(s.source, path=FILM))  # type: ignore[type-var]
    assert s.content_task is None  # nothing was looked at


async def test_2d_chosen_by_the_user_needs_no_probe(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, dataclasses.replace(_auto(), content=ContentKind.ANIMATION))
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert "cascade_fps" not in f and s.content_task is None
    snap = s.snapshot()
    assert snap.content is ContentKind.ANIMATION and not snap.content_detected


async def test_the_first_decision_waits_briefly_then_a_late_answer_decides_again(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: Any,
    fake_trt: TrtFake,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _auto())
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    film = tmp_path / "real.mkv"
    film.write_bytes(b"x")  # a local file: worth a look
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    s.props["path"] = str(film)
    looking = asyncio.Event()

    async def slow_probe(path: str, key: str | None, fps: float) -> None:
        await looking.wait()

    monkeypatch.setattr(s, "probe_content", slow_probe)
    await s.apply(raise_on_fail=False)
    assert _filters(s) == [], "the decision waits for the answer"
    assert s.content_task is not None and s.content_file == str(film)
    # the answer does not come in time: the video plays, as Auto always did
    s.content_since = time.monotonic() - s.opts.content_wait_s - 1
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["cascade_fps"] == [48, 1] and s.run_content is ContentKind.AUTO
    # now it comes, and says 2D: the tick decides again
    s.content_found = content.Found(ContentKind.ANIMATION, 0.5, 120)
    s.content_changed = True
    s.props["vf"] = [{"name": "vapoursynth", "label": "buttereye", "enabled": True}]
    await s.step()
    assert not s.content_changed
    assert "cascade_fps" not in _filters(s)[-1]
    assert s.run_content is ContentKind.ANIMATION and s.run_content_found
    looking.set()
    await asyncio.sleep(0)


async def test_another_file_is_looked_at_afresh(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _auto())
    s = _session(ctx, tmp_path)
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    _knows(s, ContentKind.ANIMATION)
    await s.apply(raise_on_fail=False)
    assert s.run_content is ContentKind.ANIMATION
    s.props["path"] = "/videos/another film.mkv"  # not on disk: nothing to look at
    assert s.source is not None
    facts = dataclasses.replace(s.source, path="/videos/another film.mkv")
    assert not s.content_wait(_auto(), facts)
    assert s.content_found is None and s.content_file == "/videos/another film.mkv"
    assert s.content_kind(_auto()) == (ContentKind.AUTO, False)


async def test_a_remembered_answer_is_used_at_once(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    ctx = _ctx(tmp_path, _auto())
    s = _session(ctx, tmp_path)
    film = tmp_path / "real.mkv"
    film.write_bytes(b"x")
    s.content_cache().put(
        content.file_key(str(film)), content.Found(ContentKind.ANIMATION, 0.3, 90)
    )
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, w=1920, h=1080, hz=144.0)
    s.props["path"] = str(film)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert "cascade_fps" not in f and s.content_task is None
    assert s.run_content is ContentKind.ANIMATION and s.run_content_found


# ---------------------------------------------------------------- the window's words
def test_the_simple_profile_carries_the_video_type() -> None:
    assert sw.simple_profile("auto", "display").content is ContentKind.AUTO
    for kind in ContentKind:
        assert sw.simple_profile("auto", "display", content=kind).content is kind


def test_the_row_names_2d_animation_only() -> None:
    active = sc.session(1)
    assert "2D animation" not in sw.row_view(active).engine
    chosen = dataclasses.replace(active, content=ContentKind.ANIMATION)
    found = dataclasses.replace(chosen, content_detected=True)
    assert sw.row_view(chosen).engine.split(", ")[1] == "2D animation"
    assert "detected" not in sw.row_view(chosen).engine
    assert "2D animation, detected" in sw.row_view(found).engine
    as_3d = dataclasses.replace(active, content=ContentKind.LIVE)
    assert sw.row_view(as_3d).engine == sw.row_view(active).engine


def test_backends_named_in_this_file_exist() -> None:
    assert BackendId.RIFE_TRT.value == "rife-trt" and Target(TargetKind.DISPLAY).kind


def test_frames_repeated_by_a_rate_conversion_are_not_drawings() -> None:
    # 24 fps footage made 30 by repeating every fifth frame: one pair in five is
    # an exact repeat inside motion, as a held drawing would be, but always alone
    # and at the same step
    every_fifth = [0.0 if i % 5 == 4 else 6.0 for i in range(content.FRAMES)]
    found = content.judge([every_fifth] * 4)
    assert found.kind is ContentKind.LIVE and found.share == 0.0
    every_sixth = [0.0 if i % 6 == 5 else 6.0 for i in range(content.FRAMES)]  # 25 -> 30
    assert content.judge([every_sixth] * 4).kind is ContentKind.LIVE
    # drawings on twos stay 2D, and so does animation whose holds come irregularly
    twos = [0.0 if i % 2 else 6.0 for i in range(content.FRAMES)]
    assert content.judge([twos] * 4).kind is ContentKind.ANIMATION
    mixed = [0.0 if (i % 5 == 4 or i % 7 == 3) else 6.0 for i in range(content.FRAMES)]
    assert content.judge([mixed] * 4).kind is ContentKind.ANIMATION
    assert not content.converted([3, 5, 7, 9])  # a step of two: drawings on twos
    assert content.converted([4, 9, 14, 19]) and content.converted([4, 9, 13, 18])
    assert not content.converted([4, 9, 14])  # too few to call it a cadence


def test_two_cores_keep_each_others_answers(tmp_path: Path) -> None:
    # the GUI's core and a `buttereye play` each load the file once, then save
    file = tmp_path / content.CACHE_FILE
    gui, cli = content.Cache(file), content.Cache(file)
    cli.put("a" * 32, content.Found(ContentKind.LIVE, 0.01, 100))
    gui.put("b" * 32, content.Found(ContentKind.ANIMATION, 0.4, 100))
    again = content.Cache(file)
    assert again.get("a" * 32) is not None and again.get("b" * 32) is not None
    assert not list(tmp_path.glob("*.tmp"))  # each writer's own temporary file is gone
