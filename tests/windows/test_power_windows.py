# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Power on Windows: ``_win32.begin_activity``/``end_activity`` (a power request)
and ``power_status``, through the real API.

These only check that the calls are made correctly and answer in range; whether
sleep is really held needs a real Windows (``powercfg /requests`` while a render
runs). Under Wine the request is a stub that succeeds and the power status reads
as a machine on mains.

Skipped everywhere but Windows (``tools/wine-tests.sh`` runs them under Wine).
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if sys.platform == "win32":
    from buttereye.core import osapi
    from buttereye.core.osapi import _win32 as w


def test_a_power_request_is_made_and_ended() -> None:
    token = w.begin_activity("Saving a smooth copy")
    assert token is None or token > 0
    if token is not None:
        w.end_activity(token)


def test_the_facade_holds_an_activity() -> None:
    with osapi.activity("Saving a smooth copy") as token:
        assert token is None or token > 0
    assert osapi.keep_awake(1) is None


def test_power_status_answers_in_range() -> None:
    status = w.power_status()
    assert status is not None
    ac_line, battery_flag, saver = status
    assert ac_line in (0, 1, 255)
    assert 0 <= battery_flag <= 255 and 0 <= saver <= 255
    snap = osapi.power_snapshot()
    assert snap.on_battery in (True, False, None)
    assert snap.low_power in (True, False)
    assert snap.thermal is None
    assert osapi.has_system_battery() is (battery_flag not in (128, 255))
