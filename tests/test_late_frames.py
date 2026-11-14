# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Frames the screen showed late are not the engine's fault (docs/spikes/m0v.md).

With display sync, a refresh the output misses (mpv's ``vo-delayed-frame-count``:
the renderer, the compositor, or the GPU busy with something else) costs the next
frame its turn, and mpv counts that frame as dropped although smoothing delivered
it on time. The health check now splits the two: what the missed refreshes explain
is reported as frames shown late, and only the rest counts against the engine. No
mpv, no GPU: the detector and the session are real."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.events import HealthChanged, Notice
from buttereye.core.i18n import Msg
from buttereye.core.mpvctl import health, osd
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.health import HealthDetector, Sample, Thresholds, Verdict
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId, BenchResult, Health
from buttereye.gui import simple_window as sw
from buttereye.gui.pages import sessions as sp
from tests.test_live_decisions import _devbox, _engine, _running, _simple
from tests.test_live_decisions import bench_history as _bench_history_fixture

bench_history = _bench_history_fixture

TH = Thresholds()
FPS = 144.0


def _play(drops_per_s: float, delayed_per_s: float, seconds: float = 30.0) -> Verdict:
    """Steady play at 144 fps with that many dropped frames and missed refreshes."""
    d = HealthDetector(TH)
    d.filter_started(0.0, FPS)
    v = d.update(Sample(0.5, 0.5, None, False, False, False, False, 0, 0))
    t = 1.0
    while t <= seconds:
        v = d.update(
            Sample(t, t, None, False, False, False, False, int(drops_per_s * t),
                   int(delayed_per_s * t))
        )  # fmt: skip
        t += 0.5
    return v


# ---------------------------------------------------------------- the detector
def test_drops_the_screen_explains_are_frames_shown_late() -> None:
    """The dev box at 144 fps: about 2 frames a second dropped, each one after a
    refresh the output missed."""
    v = _play(drops_per_s=2.0, delayed_per_s=2.4)
    assert v.health is Health.DROPPING and v.at_screen
    assert v.drop_rate == pytest.approx(2.0 / FPS, rel=0.15)
    assert v.engine_rate == 0.0
    assert v.reason is not None and v.reason.key.startswith("The screen showed frames late")


def test_drops_nothing_at_the_output_explains_are_the_engines() -> None:
    v = _play(drops_per_s=8.0, delayed_per_s=0.0)
    assert v.health is Health.DROPPING and not v.at_screen
    assert v.engine_rate == pytest.approx(v.drop_rate)
    assert v.reason is not None and v.reason.key.startswith("Dropping frames")


def test_both_at_once_only_the_rest_counts_against_the_engine() -> None:
    v = _play(drops_per_s=10.0, delayed_per_s=2.0)
    assert v.health is Health.DROPPING and not v.at_screen
    assert v.drop_rate == pytest.approx(10.0 / FPS, rel=0.1)
    assert v.engine_rate == pytest.approx(8.0 / FPS, rel=0.1)


def test_missed_refreshes_that_cost_no_frame_are_not_a_problem() -> None:
    """Film doubled on a 144 Hz screen: each frame is up for three refreshes, so a
    late one rarely costs a frame (measured: 111 missed refreshes, 30 drops, in 60 s
    at 48 fps: 1 %). Under the limit nothing is reported."""
    d = HealthDetector(TH)
    d.filter_started(0.0, 48.0)
    v = d.update(Sample(0.5, 0.5, None, False, False, False, False, 0, 0))
    for i in range(2, 61):
        t = i * 0.5
        v = d.update(Sample(t, t, None, False, False, False, False, int(0.3 * t), int(1.9 * t)))
    assert v.health is Health.OK and v.drop_rate is not None and v.drop_rate < TH.drop_ratio


def test_sample_reads_the_missed_refreshes() -> None:
    s = health.sample_from({"frame-drop-count": 7, "vo-delayed-frame-count": 9}, 1.0)
    assert (s.frame_drops, s.vo_delayed) == (7, 9)
    assert health.sample_from({"vo-delayed-frame-count": True}, 0.0).vo_delayed == 0
    assert health.sample_from({}, 0.0).vo_delayed == 0


# ---------------------------------------------------------------- the session
def _late(rate: float = 0.02) -> Verdict:
    return Verdict(
        Health.DROPPING,
        None,
        Msg("The screen showed frames late ({percent}% over 10 s); smoothing is on time.",
            {"percent": round(rate * 100, 1)}),
        (),
        True,
        rate,
        0.0,
        True,
    )  # fmt: skip


async def test_frames_shown_late_are_said_so_and_nothing_is_stepped_down(
    tmp_path: Path, bench_history: list[BenchResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    bench_history.append(_devbox())
    ctx, s = await _running(tmp_path, _simple())
    monkeypatch.setattr(s.detector, "update", lambda _sample: _late())
    await s.step()
    snap = s.snapshot()
    assert snap.health is Health.DROPPING and snap.drops_at_screen
    assert snap.step_down_to is None and snap.drop_rate_10s == 0.02
    changed = [e for e in ctx.events if isinstance(e, HealthChanged)]
    assert changed and changed[-1].reason.key.startswith("The screen showed frames late")
    # the window and the line in mpv say what it is, and offer no lighter engine
    view = sw.row_view(snap)
    assert view.word == "Smooth, some frames late"
    assert view.notice.startswith("Some frames are shown late: the screen missed refreshes")
    assert osd.parts(snap, ("health",)) == ["screen showing frames late"]
    assert sp.health_text(snap.health, snap.drop_rate_10s, snap.drops_at_screen) == (
        "Frames late at the screen (2% over 10 s)"
    )


async def test_frames_shown_late_never_replace_or_teach_the_engine(
    tmp_path: Path, bench_history: list[BenchResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a screen that misses a fifth of its refreshes for a long time: the
    engine is on time, so Automatic keeps it and learns nothing about its speed."""
    bench_history.append(_devbox())
    ctx, s = await _running(tmp_path, _simple())
    assert _engine(s) is BackendId.RIFE_NCNN and s.auto_chosen
    monkeypatch.setattr(s.detector, "update", lambda _sample: _late(0.2))
    s.heavy_since = time.monotonic() - live.HEAVY_HOLD_S - 1
    s.steady_since = time.monotonic() - live.STEADY_HOLD_S - 1
    await s.step()
    assert _engine(s) is BackendId.RIFE_NCNN
    assert s.heavy_since is None and s.steady_since is None
    assert not [e for e in ctx.events if isinstance(e, Notice)]
    assert not (ctx.paths.state_dir / live.livelimits.FILE).exists()


async def test_the_side_is_followed_while_frames_keep_dropping(
    tmp_path: Path, bench_history: list[BenchResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    bench_history.append(_devbox())
    ctx, s = await _running(tmp_path, _simple())
    monkeypatch.setattr(s.detector, "update", lambda _sample: _late())
    await s.step()
    assert s.snapshot().drops_at_screen
    engine = Verdict(Health.DROPPING, None, Msg("Dropping frames."), (), True, 0.03, 0.03, False)
    monkeypatch.setattr(s.detector, "update", lambda _sample: engine)
    await s.step()
    snap = s.snapshot()
    assert snap.health is Health.DROPPING and not snap.drops_at_screen
    assert sw.row_view(snap).word == "Smooth, some drops"
    assert len([e for e in ctx.events if isinstance(e, HealthChanged)]) == 2
    ok = Verdict(Health.OK, None, None, (), True, 0.0)
    monkeypatch.setattr(s.detector, "update", lambda _sample: ok)
    await s.step()
    assert s.snapshot().health is Health.OK and not s.snapshot().drops_at_screen


# ---------------------------------------------------------------- the words
def test_the_sessions_page_offers_no_step_down_for_frames_shown_late() -> None:
    snap: Any = dataclasses.replace(
        sc.session(1), health=Health.DROPPING, drop_rate_10s=0.012, drops_at_screen=True
    )
    assert sp.health_text(snap.health, snap.drop_rate_10s, True) == (
        "Frames late at the screen (1% over 10 s)"
    )
    assert sp.health_text(Health.DROPPING, None, True) == "Frames late at the screen"
    assert sp.health_text(Health.DROPPING, 0.012) == "Dropping frames (1% over 10 s)"
    assert sw.health_words(Health.DROPPING).startswith("Some frames are being dropped")
    assert sw.health_words(Health.OK, True) == ""
