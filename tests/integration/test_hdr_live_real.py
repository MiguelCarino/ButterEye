# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live HDR smoothing against the real mpv (SCOPE F7, spike M0(d)).

The gate: with ButterEye's filter smoothing an HDR10 video, the colour parameters
mpv hands to its video output (primaries, transfer, matrix, range, peak and
mastering luminance, light levels) are identical to the source's. Without the
opt-in the same video plays unsmoothed. mpv runs with ``--vo=null --ao=null``.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
import types as pytypes
from collections.abc import Iterator
from pathlib import Path

import pytest

from buttereye.core.testing import scenarios as sc
from buttereye.core.types import BypassReason, Config, ConfigLoad, FilterState, Paths, TargetKind
from tests.integration.test_live_play import Live, _profile, _raw
from tests.integration.test_live_play import live_env as live_env  # noqa: F401

pytestmark = [pytest.mark.integration]

#: what must come through the filter unchanged (the pixel format may change
#: layout, p010 → yuv420p10, which is the same 10-bit picture)
COLOUR_KEYS = (
    "colormatrix", "colorlevels", "primaries", "gamma", "sig-peak", "light",
    "max-cll", "max-fall", "min-luma", "max-luma", "w", "h",
)  # fmt: skip


PQ_TAGS = "setparams=color_trc=smpte2084:color_primaries=bt2020:colorspace=bt2020nc:range=tv"


@pytest.fixture
def config_provider(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    base = sc.default_config()
    skip = _profile("mvskip", sc.BackendId.MVTOOLS, TargetKind.X2)
    smooth = dataclasses.replace(
        _profile("mvhdr", sc.BackendId.MVTOOLS, TargetKind.X2), hdr="passthrough"
    )
    cfg: Config = dataclasses.replace(base, profiles=base.profiles + (skip, smooth))

    def load(paths: Paths) -> ConfigLoad:
        return sc.config_load(cfg, revision="rev-hdr")

    def save(paths: Paths, c: Config, *, expected_revision: str) -> str:
        raise AssertionError("live play must not save the config")

    mod = pytypes.ModuleType("buttereye.core.profiles.config")
    mod.load = load  # type: ignore[attr-defined]
    mod.save = save  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "buttereye.core.profiles.config", mod)
    yield


@pytest.fixture(scope="module")
def clip_hdr10(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """10-bit HEVC, PQ / BT.2020, with mastering display and light levels."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("needs ffmpeg to generate the test clip")
    encoders = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    ).stdout
    if "libx265" not in encoders:
        pytest.skip("needs ffmpeg with libx265 for an HDR10 test clip")
    out = tmp_path_factory.mktemp("hdr10") / "hdr10.mkv"
    x265 = (
        "hdr10=1:repeat-headers=1:max-cll=1000,400:"
        "master-display=G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    )
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24",
            "-t", "30",
            # tag the frames: the encoder takes its VUI from them
            "-vf", PQ_TAGS,
            "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", x265,
            "-pix_fmt", "yuv420p10le", "-color_primaries", "bt2020",
            "-color_trc", "smpte2084", "-colorspace", "bt2020nc", "-color_range", "tv",
            os.fspath(out),
        ],
        check=True,
        timeout=120,
    )  # fmt: skip
    return out


async def test_hdr10_plays_unsmoothed_without_the_opt_in(live_env: Live, clip_hdr10: Path) -> None:
    env = live_env
    sid = await env.play(clip_hdr10, "mvskip")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.BYPASSED, timeout=20)
    assert snap.bypass is BypassReason.HDR_SKIP


async def test_hdr10_colour_survives_smoothing(live_env: Live, clip_hdr10: Path) -> None:
    """M0(d): mpv's output colour parameters equal the source's with the filter on."""
    env = live_env
    sid = await env.play(clip_hdr10, "mvhdr")
    snap = await env.wait(sid, lambda x: x.filter is FilterState.ACTIVE, timeout=30)
    assert snap.bypass is None and snap.notice is not None
    assert snap.notice.key == "HDR smoothing is experimental."
    sock = env.s(sid).socket  # video-out-params is outside ButterEye's allowlist
    src = _raw(sock, "get_property", "video-params")["data"]
    out = _raw(sock, "get_property", "video-out-params")["data"]
    assert src["gamma"] == "pq" and src["primaries"] == "bt.2020"
    assert src["max-cll"] == 1000 and src["max-luma"] == 1000
    assert {k: out.get(k) for k in COLOUR_KEYS} == {k: src.get(k) for k in COLOUR_KEYS}
    assert out["pixelformat"] in ("p010", "yuv420p10")  # still 10-bit
