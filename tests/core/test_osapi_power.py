# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``osapi``'s power and heat seam, with macOS's answers mocked.

Runs everywhere. Off macOS every answer is None and nothing is kept awake; on
macOS the ``_darwin`` calls are replaced, so what is checked is how the facade
puts them together, not what this machine's battery says
(``tests/macos/test_osapi_macos.py`` asks the real API).
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.types import PowerSnapshot

on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
off_macos = pytest.mark.skipif(sys.platform == "darwin", reason="not macOS")


@off_macos
def test_nothing_is_known_off_macos() -> None:
    assert osapi.thermal_state() is None
    assert osapi.low_power_mode() is None
    assert osapi.on_battery() is None
    assert osapi.power_snapshot() == PowerSnapshot()
    assert osapi.keep_awake(1) is None
    assert osapi.begin_activity("x") is None
    osapi.end_activity(None)
    with osapi.activity("x") as token:
        assert token is None


@on_macos
def test_snapshot_combines_the_three_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osapi.d, "thermal_state", lambda: 2)
    monkeypatch.setattr(osapi.d, "low_power_mode", lambda: False)
    monkeypatch.setattr(osapi.d, "on_battery", lambda: True)
    snap = osapi.power_snapshot()
    assert snap == PowerSnapshot(on_battery=True, low_power=False, thermal=2)
    assert snap.constrained


@on_macos
def test_snapshot_passes_unknowns_through(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("thermal_state", "low_power_mode", "on_battery"):
        monkeypatch.setattr(osapi.d, name, lambda: None)
    assert osapi.power_snapshot() == PowerSnapshot()


@pytest.mark.parametrize(
    ("snap", "constrained"),
    [
        (PowerSnapshot(), False),
        (PowerSnapshot(thermal=1), False),
        (PowerSnapshot(thermal=2), True),
        (PowerSnapshot(thermal=3), True),
        (PowerSnapshot(low_power=True, thermal=0), True),
        (PowerSnapshot(on_battery=True), False),  # battery alone is not throttling
    ],
)
def test_constrained(snap: PowerSnapshot, constrained: bool) -> None:
    assert snap.constrained is constrained


@on_macos
def test_keep_awake_runs_caffeinate_for_the_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen.append((argv, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    proc = osapi.keep_awake(4242)
    assert isinstance(proc, FakePopen)
    argv, kwargs = seen[0]
    assert argv == ["/usr/bin/caffeinate", "-i", "-w", "4242"]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["close_fds"] is True


@on_macos
def test_keep_awake_failing_to_start_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("/usr/bin/caffeinate")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert osapi.keep_awake(4242) is None


@on_macos
def test_activity_ends_what_it_began(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(osapi.d, "begin_activity", lambda r: calls.append(("begin", r)) or 77)
    monkeypatch.setattr(osapi.d, "end_activity", lambda t: calls.append(("end", t)))
    with pytest.raises(RuntimeError), osapi.activity("Rendering film.mkv") as token:
        assert token == 77
        raise RuntimeError("the render failed")
    assert calls == [("begin", "Rendering film.mkv"), ("end", 77)]


@on_macos
def test_refused_activity_ends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[object] = []
    monkeypatch.setattr(osapi.d, "begin_activity", lambda _r: None)
    monkeypatch.setattr(osapi.d, "end_activity", ended.append)
    with osapi.activity("x") as token:
        assert token is None
    assert ended == []
