# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The memory failsafes the suite and the dev tools run under (tools/memguard.py)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from typing import Any

import pytest

from tools import memguard


def test_the_suite_runs_capped_or_says_why() -> None:
    # conftest re-runs pytest in a capped scope unless it is disabled or impossible
    if os.environ.get("BUTTEREYE_NO_MEMCAP") == "1" or not memguard._scope_works():
        pytest.skip("no capped scope on this machine")
    assert memguard.capped()
    cgroup = open("/proc/self/cgroup").read()
    assert "buttereye-tests-" in cgroup


def test_preflight_refuses_when_memory_is_short(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memguard, "available_mib", lambda: 1000)
    monkeypatch.setenv("BUTTEREYE_MEM_START_MIB", "4096")
    reason = memguard.preflight()
    assert reason is not None and "1000 MiB" in reason
    monkeypatch.setenv("BUTTEREYE_MEM_START_MIB", "500")
    assert memguard.preflight() is None


def test_bad_overrides_fall_back_to_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUTTEREYE_MEM_FLOOR_MIB", "lots")
    assert memguard.floor_mib() == memguard.DEFAULT_FLOOR_MIB
    monkeypatch.setenv("BUTTEREYE_MEM_MAX_MIB", "2048")
    assert memguard.max_mib(8192) == 2048


def test_reexec_does_nothing_when_already_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUTTEREYE_MEMCAP", "8192")
    monkeypatch.setattr(memguard.subprocess, "call", lambda *_a, **_k: pytest.fail("ran twice"))
    assert memguard.reexec_capped(["true"], 1024, "x") == "already inside a capped scope"


def test_watchdog_trips_below_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memguard, "available_mib", lambda: 100)
    tripped = threading.Event()
    seen: list[int] = []

    def breach(avail: int) -> None:
        seen.append(avail)
        tripped.set()

    with memguard.Watchdog(2048, interval_s=0.01, on_breach=breach):
        assert tripped.wait(2)
    assert seen == [100]


def test_watchdog_stays_quiet_above_the_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(memguard, "available_mib", lambda: 9000)
    called: list[Any] = []
    dog = memguard.Watchdog(2048, interval_s=0.01, on_breach=called.append).start()
    threading.Event().wait(0.1)
    dog.stop()
    assert called == [] and dog.lowest == 9000


def test_descendants_and_kill_reach_grandchildren() -> None:
    # a shell whose child sleeps: both are found and killed
    proc = subprocess.Popen(
        [sys.executable, "-c", "import subprocess; subprocess.run(['sleep', '30'])"]
    )
    try:
        for _ in range(100):
            if len(memguard.descendants(proc.pid)) == 1:
                break
            threading.Event().wait(0.02)
        found = memguard.descendants(os.getpid())
        assert proc.pid in found and len(memguard.descendants(proc.pid)) == 1
        assert memguard.kill_descendants(proc.pid) == 1
        proc.kill()
        assert proc.wait(5) != 0
    finally:
        proc.kill()
