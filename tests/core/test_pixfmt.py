# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Bit depth and chroma layout from ffmpeg's and mpv's pixel format names
(``core.pixfmt``), and a stream's depth when ``bits_per_raw_sample`` is there."""

from __future__ import annotations

import pytest

from buttereye.core import pixfmt


@pytest.mark.parametrize(
    ("name", "bits", "chroma"),
    [
        # ffmpeg names
        ("yuv420p", 8, "420"),
        ("yuvj420p", 8, "420"),
        ("yuv420p10le", 10, "420"),
        ("yuv420p12le", 12, "420"),  # was read as 10 (every 12-bit HEVC/AV1 file)
        ("yuv420p9le", 9, "420"),  # was read as 8
        ("yuv420p14le", 14, "420"),  # was read as 8
        ("yuv422p10le", 10, "422"),
        ("yuv444p10le", 10, "444"),
        ("yuv444p12le", 12, "444"),
        ("yuva444p16le", 16, "444"),  # was read as 10
        ("yuv444p10msble", 10, "444"),
        ("nv12", 8, "420"),  # was read as 10
        ("nv21", 8, "420"),
        ("nv16", 8, "422"),
        ("nv24", 8, "444"),
        ("nv20le", 10, "422"),
        ("p010le", 10, "420"),
        ("p016le", 16, "420"),  # was read as 10
        ("p210le", 10, "422"),
        ("p416le", 16, "444"),
        ("gray", 8, "other"),
        ("gray12le", 12, "other"),  # was read as 10
        ("gbrp", 8, "other"),
        ("gbrp16le", 16, "other"),  # was read as 10
        ("gbrap10le", 10, "other"),
        ("rgb48le", 16, "other"),  # was read as 8
        ("x2rgb10le", 10, "other"),
        ("bgra", 8, "other"),
        ("y210le", 10, "422"),
        ("xv36le", 12, "444"),
        ("grayf32le", 16, "other"),
        # mpv names: no byte order
        ("yuv420p10", 10, "420"),
        ("yuv444p12", 12, "444"),
        ("p010", 10, "420"),
        ("nv12", 8, "420"),
    ],
)
def test_depth_and_chroma(name: str, bits: int, chroma: str) -> None:
    assert pixfmt.known(name)
    assert pixfmt.depth(name) == bits
    assert pixfmt.chroma(name) == chroma


def test_unknown_names() -> None:
    for name in ("", "cuda", "vaapi", "videotoolbox_vld", "nonsense42"):
        assert not pixfmt.known(name)
        assert pixfmt.depth(name) == 8 and pixfmt.chroma(name) == "other"


def test_the_pixel_format_is_trusted_over_the_raw_value() -> None:
    # HEVC, AV1 and VP9 streams carry no bits_per_raw_sample
    assert pixfmt.source_bits("yuv420p12le", None) == 12
    assert pixfmt.source_bits("yuv420p10le", "10") == 10  # h264 Hi10P: both agree
    # a 9-bit stream decoded to a 10-bit format: the raw value is the depth
    assert pixfmt.source_bits("yuv420p10le", "9") == 9
    # a raw value larger than the format holds is not believed
    assert pixfmt.source_bits("yuv420p", "10") == 8
    # ProRes 4444 decodes to 12 bits whatever it was made from: the decoder's depth
    assert pixfmt.source_bits("yuv444p12le", "12") == 12
    # no format name (or one this doesn't know): the raw value, else 8
    assert pixfmt.source_bits("", "10") == 10
    assert pixfmt.source_bits("weird", 12) == 12
    assert pixfmt.source_bits("", None) == 8
    assert pixfmt.source_bits("nv12", "N/A") == 8


def test_chroma_text() -> None:
    assert pixfmt.chroma_text("420") == "4:2:0"
    assert pixfmt.chroma_text("444") == "4:4:4"
    assert pixfmt.chroma_text("other") == ""
