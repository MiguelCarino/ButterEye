# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""i18n.render fallback (U1 acceptance) and EventBus ordering/coalescing."""

from __future__ import annotations

import asyncio

import pytest

from buttereye.core import i18n
from buttereye.core.events import (
    EventBus,
    EventsDropped,
    HealthChanged,
    Notice,
    SessionChanged,
    SessionEnded,
)
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import FilterState, Health, Msg


@pytest.fixture(autouse=True)
def _reset_language() -> None:
    i18n.set_language("en")


def test_render_falls_back_to_msgid_with_params() -> None:
    assert i18n.render(Msg("fps {fps} > fps_max {max}", {"fps": 60, "max": 30})) == (
        "fps 60 > fps_max 30"
    )
    assert i18n.render(Msg("{n} problem(s)", {"n": 2})) == "2 problem(s)"


def test_render_missing_param_stays_visible() -> None:
    assert i18n.render(Msg("{a} and {b}", {"a": "x"})) == "x and {b}"


def test_render_without_params_keeps_braces() -> None:
    assert i18n.render(Msg("literal {braces}")) == "literal {braces}"


def test_unknown_language_falls_back() -> None:
    i18n.set_language("xx-YY")
    assert i18n.current_language() == "xx-YY"
    assert i18n.render(Msg("Hello")) == "Hello"
    i18n.set_language(None)
    assert i18n.render(Msg("Hello")) == "Hello"


async def test_bus_order_and_close() -> None:
    bus = EventBus()
    it = bus.subscribe()
    for i in range(5):
        bus.publish(Notice(Msg(str(i))))
    bus.close()
    out = [ev async for ev in it]
    assert [e.message.key for e in out if isinstance(e, Notice)] == ["0", "1", "2", "3", "4"]
    assert [ev async for ev in bus.subscribe()] == []  # subscribing after close ends at once


async def test_coalescing_latest_wins() -> None:
    bus = EventBus()
    it = bus.subscribe()
    snaps = [sc.session(1), sc.session(2), sc.session(3)]
    for s in snaps:
        bus.publish_coalesced("s", SessionChanged(s), min_interval_s=0.05)
    await asyncio.sleep(0.1)
    bus.close()
    out = [ev async for ev in it]
    assert out == [SessionChanged(snaps[0]), SessionChanged(snaps[2])]


async def test_publish_rejects_non_events() -> None:
    bus = EventBus()
    with pytest.raises(TypeError):
        bus.publish("nope")  # type: ignore[arg-type]


async def test_dropped_marker_counts() -> None:
    bus = EventBus()
    it = bus.subscribe(maxlen=1)
    for i in range(3):
        bus.publish_coalesced(f"k{i}", SessionChanged(sc.session(i)))
    bus.close()
    out = [ev async for ev in it]
    assert isinstance(out[0], EventsDropped) and out[0].count == 2


async def test_transition_never_followed_by_older_coalesced_update() -> None:
    """§2.3: a pending coalesced update for a session is delivered before (not
    after) a newer transition for that session, so it cannot overwrite it."""
    bus = EventBus()
    it = bus.subscribe()
    t0, t1 = sc.session(1, drop_rate=0.0), sc.session(1, drop_rate=0.1)
    t2 = sc.session(1, filter=FilterState.BYPASSED, drop_rate=0.2)
    sid = t0.sid
    assert t0 != t1 != t2 and t1.sid == t2.sid == sid
    bus.publish_coalesced(sid, SessionChanged(t0), min_interval_s=0.05)  # delivered
    bus.publish_coalesced(sid, SessionChanged(t1), min_interval_s=0.05)  # pending
    bus.publish(SessionChanged(t2))  # transition with the newest state
    health = HealthChanged(sid, Health.OK, None, Msg("ok"), (), None)
    bus.publish_coalesced(sid, SessionChanged(t2), min_interval_s=0.05)  # pending again
    bus.publish(health)
    bus.publish(Notice(Msg("other session"), sid=None))
    await asyncio.sleep(0.15)  # any stale flush timer would have fired by now
    bus.close()
    out = [ev async for ev in it]
    assert out == [
        SessionChanged(t0),
        SessionChanged(t1),
        SessionChanged(t2),
        SessionChanged(t2),
        health,
        Notice(Msg("other session"), sid=None),
    ]


async def test_transition_for_other_key_keeps_pending_update() -> None:
    bus = EventBus()
    it = bus.subscribe()
    a, b = sc.session(1), sc.session(2)
    assert a.sid != b.sid
    bus.publish_coalesced(a.sid, SessionChanged(a), min_interval_s=0.05)
    bus.publish_coalesced(a.sid, SessionChanged(a), min_interval_s=0.05)  # pending
    bus.publish(SessionEnded(b.sid, "mpv_exited"))
    await asyncio.sleep(0.15)
    bus.close()
    out = [ev async for ev in it]
    assert out == [SessionChanged(a), SessionEnded(b.sid, "mpv_exited"), SessionChanged(a)]
