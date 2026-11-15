# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The content probe against real ffmpeg (``mpvctl.content``): two clips made here,
one that holds every picture for two frames and one that changes on every frame.
No mpv and no GPU; skipped where ffmpeg is not installed."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from buttereye.core.mpvctl import content
from buttereye.core.types import ContentKind

pytestmark = [pytest.mark.integration]

FFMPEG = shutil.which("ffmpeg")
SECONDS = 32


def _clip(path: Path, source: str) -> None:
    """A small moving test picture, as motion JPEG (every ffmpeg has it)."""
    assert FFMPEG is not None
    subprocess.run(
        [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", source, "-t", str(SECONDS),
         "-c:v", "mjpeg", "-q:v", "4", "-an", str(path)],
        check=True, timeout=120,
    )  # fmt: skip


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
async def test_the_probe_tells_held_drawings_from_frames_that_all_change(tmp_path: Path) -> None:
    twos, ones = tmp_path / "on twos.mkv", tmp_path / "on ones.mkv"
    # 12 pictures a second shown at 24: each one is up for two frames
    _clip(twos, "testsrc2=s=640x360:r=12,fps=24")
    _clip(ones, "testsrc2=s=640x360:r=24")
    assert content.worth_probing(str(twos), 24.0, float(SECONDS))
    drawn = await content.probe(str(twos), float(SECONDS))
    assert drawn is not None and drawn.kind is ContentKind.ANIMATION
    assert drawn.judged >= content.MIN_JUDGED and 0.4 < drawn.share < 0.6
    filmed = await content.probe(str(ones), float(SECONDS))
    assert filmed is not None and filmed.kind is ContentKind.LIVE
    assert filmed.share < content.HELD_SHARE


@pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")
async def test_a_file_ffmpeg_cannot_read_gives_no_answer(tmp_path: Path) -> None:
    junk = tmp_path / "not a video.mkv"
    junk.write_bytes(b"this is not a video" * 100)
    assert await content.probe(str(junk), 600.0) is None


async def test_without_ffmpeg_there_is_no_answer(tmp_path: Path) -> None:
    film = tmp_path / "film.mkv"
    film.write_bytes(b"x")
    assert await content.probe(str(film), 600.0, ffmpeg=str(tmp_path / "no-such-ffmpeg")) is None
