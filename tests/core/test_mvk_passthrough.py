# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""P12: MoltenVK's ``MVK_CONFIG_*`` settings reach mpv and vspipe untouched, so
they can be A/B-tested with the benchmark (packaging/README.md, "MoltenVK
settings, A/B") without a rebuild. The app sets none of them itself: what wins
has to be measured first. Pure: ``point_at_app`` over a planted tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from buttereye.core import osapi

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a macOS bundle's paths")

MVK = {
    "MVK_CONFIG_USE_MTLHEAP": "1",
    "MVK_CONFIG_USE_METAL_ARGUMENT_BUFFERS": "0",
    "MVK_CONFIG_PREFILL_METAL_COMMAND_BUFFERS": "2",
    "MVK_CONFIG_LOG_LEVEL": "3",
}


def _res(root: Path) -> Path:
    res = root / "ButterEye.app" / "Contents" / "Resources"
    (res / "bin").mkdir(parents=True)
    return res


def test_mvk_settings_pass_through_to_the_app_s_children(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin", **MVK}
    osapi.point_at_app(env, _res(tmp_path), sep=":")
    assert {k: env[k] for k in MVK} == MVK


def test_the_app_sets_no_mvk_setting_of_its_own(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin"}
    osapi.point_at_app(env, _res(tmp_path), sep=":")
    assert not [k for k in env if k.startswith("MVK_")]
    assert not [k for k in osapi.APP_UNSET if k.startswith("MVK_")]
