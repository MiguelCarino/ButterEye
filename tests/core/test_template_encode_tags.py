# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""An offline render tags the chroma siting the template writes: the source's,
read by ffprobe, or left. The y4m pipe cannot carry it: ``C420`` (8 bits) reads
as center in ffmpeg, so without the flag every 8-bit render of a normal
left-sited video shows its chroma half a pixel off."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from buttereye.core.render import pipeline
from buttereye.core.render import probe as rp

ENCODERS = ("hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1", "libx264")


def _info(**video: Any) -> rp.SourceInfo:
    v: dict[str, Any] = {
        "index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
        "height": 1080, "pix_fmt": "yuv420p", "r_frame_rate": "24000/1001",
        "avg_frame_rate": "24000/1001", "duration": "60.0", "color_space": "bt709",
        "color_range": "tv",
    }  # fmt: skip
    v.update(video)
    info = rp.source_info({"streams": [v], "format": {"duration": "60.0"}})
    assert info is not None
    return info


def _flag(argv: list[str]) -> str:
    assert argv.count("-chroma_sample_location") == 1
    return argv[argv.index("-chroma_sample_location") + 1]


@pytest.mark.parametrize("encoder", ENCODERS)
def test_every_encoder_gets_the_source_siting(encoder: str) -> None:
    out = Path("/o/v.mkv")
    assert _flag(pipeline.encode_argv("ffmpeg", encoder, _info(), out)) == "left"
    hdr = _info(chroma_location="topleft", pix_fmt="yuv420p10le", color_transfer="smpte2084")
    argv = pipeline.encode_argv("ffmpeg", encoder, hdr, out)
    assert _flag(argv) == "topleft"
    assert argv.index("-chroma_sample_location") > argv.index("-i")  # an output option


def test_probe_reads_the_siting_and_maps_it_for_the_script() -> None:
    assert rp.vs_chromaloc(_info()) is None
    assert "chroma_location" not in _info(chroma_location="unspecified").color
    for ffprobe, zimg in (("left", "left"), ("center", "center"), ("topleft", "top_left"),
                          ("bottomleft", "bottom_left")):  # fmt: skip
        info = _info(chroma_location=ffprobe)
        assert info.color["chroma_location"] == ffprobe
        assert rp.vs_chromaloc(info) == zimg
        assert rp.chroma_sample_location(info) == ffprobe
    assert rp.chroma_sample_location(_info()) == "left"


def test_an_rgb_source_is_tagged_with_the_matrix_the_template_converts_with() -> None:
    # gbrp (an RGB VP9 or FFV1): the smoothed copy is YUV, so ffprobe's "gbr" would
    # tell players to show its Y, U and V as G, B and R
    info = _info(pix_fmt="gbrp", color_space="gbr", color_range="pc", width=320, height=240)
    assert (rp.vs_matrix(info), rp.vs_range(info)) == ("709", "full")
    argv = pipeline.encode_argv("ffmpeg", "libx264", info, Path("/o/v.mkv"))
    assert argv[argv.index("-colorspace") + 1] == "bt709"
