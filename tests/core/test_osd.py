# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The small mpv line (GUI.md §12.1.5): its text, what it never contains, and the
one overlay command the IPC allowlist lets through."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from buttereye.core.mpvctl import ipc, osd
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BackendId, BypassReason, FilterState, Health


def _active() -> object:
    snap = sc.session(1)
    assert snap.source is not None
    return dataclasses.replace(
        snap,
        backend=BackendId.RIFE_TRT,
        model="rife-v4.26_ensembleFalse",
        target_fps=Fraction(60),
        source=dataclasses.replace(snap.source, fps=Fraction(24), width=3840, height=2160),
        smooth_size=(1920, 1080),
        flow_scale=0.5,
    )


def test_parts_follow_the_chosen_items() -> None:
    snap = _active()
    assert osd.parts(snap, ("rates", "engine")) == ["24 → 60 fps", "GPU · RIFE v4.26 TensorRT"]  # type: ignore[arg-type]
    full = osd.parts(snap, ("rates", "engine", "size", "health"))  # type: ignore[arg-type]
    assert full[2] == "at 1920×1080 · half-res motion" and full[3] == "frames OK"
    cpu = dataclasses.replace(
        snap, backend=BackendId.MVTOOLS, mv_block=True, health=Health.DROPPING
    )  # type: ignore[type-var]
    assert osd.parts(cpu, ("engine", "health")) == ["CPU · MVTools block mode", "dropping frames"]


def test_unsmoothed_and_starting_states() -> None:
    snap = sc.session(1)
    hdr = dataclasses.replace(snap, filter=FilterState.BYPASSED, bypass=BypassReason.HDR_SKIP)
    assert osd.parts(hdr, ("rates",)) == ["not smoothed: HDR (Smooth HDR is off)"]
    assert osd.parts(dataclasses.replace(snap, filter=FilterState.PENDING), ("rates",)) == []


def test_ass_is_small_gold_named_and_escaped() -> None:
    text = osd.ass(["a{\\b1}b", "c"])
    assert text.startswith("{\\an7\\fs16") and "ButterEye" in text
    assert "{\\b1}b" not in text and "a(/b1)b · c" in text  # no injected override blocks


def test_ipc_allows_only_buttereye_overlay() -> None:
    ipc.check_command(["osd-overlay", osd.OVERLAY_ID, "ass-events", "x", 0, osd.RES_Y])
    ipc.check_command(["osd-overlay", osd.OVERLAY_ID, "none", "", 0, 0])
    for bad in (
        ["osd-overlay", 1, "ass-events", "x", 0, 720],  # another id
        ["osd-overlay", osd.OVERLAY_ID, "rgba", "x", 0, 720],  # another format
        ["osd-overlay", osd.OVERLAY_ID, "ass-events", "x"],  # other arguments
    ):
        with pytest.raises(ipc.CommandRefused):
            ipc.check_command(bad)
