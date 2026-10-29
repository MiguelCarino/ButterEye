# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""ProRes 422 HQ on the CPU (``prores_ks``) for Linux and Windows: where it is
offered, its command line and its size estimate. Pure functions."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

from buttereye.core.render import pipeline
from buttereye.core.render import probe as rp


def _sdr8() -> rp.SourceInfo:
    info = rp.source_info({
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
                     "height": 1080, "pix_fmt": "yuv420p", "r_frame_rate": "24/1",
                     "avg_frame_rate": "24/1"}],
        "format": {"duration": "10"},
    })  # fmt: skip
    assert info is not None
    return info


def test_cpu_prores_is_offered_last_and_never_for_hdr10() -> None:
    listed = ("prores_ks", "libx264", "libsvtav1", "libx265", "hevc_nvenc", "av1_nvenc")
    assert rp.usable_encoders(listed, nvidia=True) == (
        "hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1", "libx264", "prores_ks",
    )  # fmt: skip
    assert rp.usable_encoders(listed, nvidia=False)[-1] == "prores_ks"
    assert rp.usable_encoders(("prores_ks",), nvidia=False) == ("prores_ks",)
    # no HDR10 mastering data on its command line
    assert "prores_ks" not in rp.usable_encoders(listed, nvidia=True, hdr10=True)


def test_a_mac_offers_one_prores() -> None:
    listed = ("libx265", "hevc_videotoolbox", "prores_videotoolbox", "prores_ks")
    got = rp.usable_encoders(listed, nvidia=False, apple=True)
    assert got[-1] == "prores_videotoolbox" and "prores_ks" not in got
    # the media engine's ProRes failed its test (an Intel Mac): the CPU one stays
    no_engine = rp.usable_encoders(("libx265", "prores_ks"), nvidia=False, apple=True)
    assert no_engine == ("libx265", "prores_ks")


def test_cpu_prores_command_line() -> None:
    argv = pipeline.encode_argv("ffmpeg", "prores_ks", _sdr8(), Path("/o/v.mkv"))
    assert argv[argv.index("-c:v") + 1] == "prores_ks"
    assert argv[argv.index("-profile:v") + 1] == "3"  # 422 HQ
    assert argv[argv.index("-vendor") + 1] == "apl0"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv422p10le"  # 4:2:2 10-bit, always
    assert argv[-1] == "/o/v.mkv"  # kept in Matroska
    assert not pipeline.ten_bit_output(_sdr8(), "prores_ks")  # the 10-bit option is moot


def test_cpu_prores_is_estimated_as_large_as_the_media_engines() -> None:
    args = ((1920, 1080), Fraction(48), 60.0, 0)
    assert rp.estimate_bytes("prores_ks", *args) == rp.estimate_bytes("prores_videotoolbox", *args)
    assert rp.estimate_bytes("prores_ks", *args) > 20 * rp.estimate_bytes("libx265", *args)


def test_cpu_prores_4444_for_deep_and_full_chroma_sources() -> None:
    def source(pix: str) -> rp.SourceInfo:
        info = rp.source_info({
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc",
                         "width": 1920, "height": 1080, "pix_fmt": pix,
                         "r_frame_rate": "24/1", "avg_frame_rate": "24/1"}],
            "format": {"duration": "10"},
        })  # fmt: skip
        assert info is not None
        return info

    for pix in ("yuv420p12le", "yuv444p10le", "yuv444p"):
        argv = pipeline.encode_argv("ffmpeg", "prores_ks", source(pix), Path("/o/v.mkv"))
        assert argv[argv.index("-profile:v") + 1] == "4", pix  # 4444
        assert argv[argv.index("-pix_fmt") + 1] == "yuv444p10le", pix
        assert argv.count("-profile:v") == 1
    plain = pipeline.encode_argv("ffmpeg", "prores_ks", source("yuv420p10le"), Path("/o/v.mkv"))
    assert plain[plain.index("-profile:v") + 1] == "3"
    assert pipeline.output_bits(source("yuv420p12le"), "prores_ks") == 10
