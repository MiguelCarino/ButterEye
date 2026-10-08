# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Test doubles for the core facade. Never imported by shipped code paths."""

from buttereye.core.testing.fakecore import FakeCall, FakeCore, open_fake
from buttereye.core.testing.scenarios import REQUIRED, SCENARIOS, Scenario, fake_paths, get

__all__ = [
    "FakeCall",
    "FakeCore",
    "open_fake",
    "Scenario",
    "SCENARIOS",
    "REQUIRED",
    "get",
    "fake_paths",
]
