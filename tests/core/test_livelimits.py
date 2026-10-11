# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What live playback learned about an engine's speed (mpvctl/livelimits.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from buttereye.core.mpvctl import livelimits as ll

KEY = ll.LimitKey("gpu-1", "rife-trt", "rife-v4.26_ensembleFalse", 3840, 1616, 1.0)


def test_record_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state" / ll.FILE
    limits: dict[str, dict[str, float]] = {}
    ll.record(path, limits, KEY, 0.4, now=1000.0)
    again = ll.load(path, now=1000.0 + 60)
    assert ll.factor(again, KEY) == 0.4
    assert ll.factor(again, ll.LimitKey("gpu-2", "rife-trt", None, 3840, 1616)) == 1.0
    # another GPU, size or flow scale is another key
    assert KEY.text() != ll.LimitKey("gpu-1", "rife-trt", KEY.model, 3840, 1616, 0.5).text()


def test_old_and_broken_records_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / ll.FILE
    path.write_text(json.dumps({
        KEY.text(): {"factor": 0.5, "when": 0.0},  # expired
        "x": {"factor": 2.0, "when": 100.0},  # not a slowdown
        "y": "nonsense",
    }))  # fmt: skip
    assert ll.load(path, now=ll.MAX_AGE_S + 1) == {}
    path.write_text("{not json")
    assert ll.load(path) == {}
    assert ll.load(tmp_path / "missing.json") == {}


@pytest.mark.parametrize(
    ("current", "drops", "expected"),
    [
        (1.0, 0.81, 0.171),  # the HDR10 film: 19 % got through -> floor 0.2
        (1.0, 0.35, 0.585),
        (0.5, 0.20, 0.36),
        (0.3, 0.0, 0.27),  # still only the margin below
    ],
)
def test_lowered(current: float, drops: float, expected: float) -> None:
    assert ll.lowered(current, drops) == pytest.approx(max(ll.FLOOR, expected))
