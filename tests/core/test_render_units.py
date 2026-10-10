# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Pure parts of offline render (SCOPE §7, §7.8): ffprobe parsing, refusals,
encoders, command lines, progress, estimates, the offline target."""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.errors import ErrorCode
from buttereye.core.render import jobs, pipeline
from buttereye.core.render import probe as rp
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    HdrClass,
    Target,
    TargetKind,
)


def _ffprobe(**video: Any) -> dict[str, Any]:
    v = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "hevc",
        "width": 3840,
        "height": 2160,
        "pix_fmt": "yuv420p10le",
        "r_frame_rate": "24000/1001",
        "avg_frame_rate": "24000/1001",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "start_time": "0.000000",
    }
    v.update(video)
    return {
        "streams": [
            v,
            {"index": 1, "codec_type": "audio", "codec_name": "aac", "tags": {"title": "Eng"}},
            {"index": 2, "codec_type": "subtitle", "codec_name": "subrip"},
            {
                "index": 3,
                "codec_type": "attachment",
                "codec_name": "ttf",
                "tags": {"filename": "f.ttf"},
            },
        ],  # fmt: skip
        "format": {"duration": "5820.5", "start_time": "0.000000"},
        "chapters": [{}, {}, {}],
    }


def test_source_info_reads_the_facts() -> None:
    info = rp.source_info(_ffprobe())
    assert info is not None
    assert (info.width, info.height, info.fps) == (3840, 2160, Fraction(24000, 1001))
    assert info.codec == "hevc" and info.bits == 10 and not info.vfr
    assert info.duration_s == pytest.approx(5820.5) and info.chapters == 3
    assert info.hdr_class is HdrClass.SDR and info.start_s == 0.0
    assert info.color == {
        "color_primaries": "bt709",
        "color_trc": "bt709",
        "colorspace": "bt709",
        "color_range": "tv",
    }
    assert [s.kind for s in info.streams] == ["video", "audio", "subtitle", "attachment"]
    assert info.streams[1].title == "Eng" and info.streams[3].title == "f.ttf"


def test_source_info_vfr_offset_and_cover_art() -> None:
    data = _ffprobe(r_frame_rate="60/1", avg_frame_rate="2997/100", start_time="0.042")
    data["streams"].insert(
        0, {"index": 9, "codec_type": "video", "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1}, "r_frame_rate": "90000/1"},
    )  # fmt: skip
    info = rp.source_info(data)
    assert info is not None and info.codec == "hevc"  # cover art is not the video
    assert info.vfr and info.fps == Fraction(2997, 100) and info.start_s == pytest.approx(0.042)
    assert rp.source_info({"streams": [{"codec_type": "audio"}]}) is None


@pytest.mark.parametrize(
    ("video", "expected"),
    [
        ({}, HdrClass.SDR),
        ({"color_transfer": "smpte2084"}, HdrClass.HDR10),
        ({"color_transfer": "arib-std-b67"}, HdrClass.HLG),
        (
            {"color_transfer": "smpte2084",
             "side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}]},
            HdrClass.HDR10PLUS,
        ),
        ({"side_data_list": [{"side_data_type": "DOVI configuration record"}]}, HdrClass.DV),
    ],
)  # fmt: skip
def test_hdr_classes(video: dict[str, Any], expected: HdrClass) -> None:
    assert rp.hdr_class(video) is expected


def test_refusals_in_the_sdr_first_build() -> None:
    sdr = rp.source_info(_ffprobe())
    hdr10 = rp.source_info(_ffprobe(color_transfer="smpte2084"))
    hlg = rp.source_info(_ffprobe(color_transfer="arib-std-b67"))
    dec = frozenset({"hevc", "h264"})
    assert jobs.refusal_for(sdr, dec, True) is None
    assert jobs.refusal_for(sdr, frozenset(), True) is None  # decoder list unknown: let ffms2 try
    missing = jobs.refusal_for(sdr, dec, False)
    assert missing is not None and missing.code is ErrorCode.FFMS2_MISSING
    assert missing.commands == ("sudo dnf install ffms2",)
    nodec = jobs.refusal_for(sdr, frozenset({"h264"}), True)
    assert nodec is not None and nodec.code is ErrorCode.CODEC_NOT_DECODABLE
    assert nodec.title.params == {"codec": "hevc"}
    for info in (hdr10, hlg):
        r = jobs.refusal_for(info, dec, True)
        assert r is not None and r.code is ErrorCode.HDR_CLASS_REFUSED
    r = jobs.refusal_for(None, dec, True)
    assert r is not None and r.code is ErrorCode.CODEC_NOT_DECODABLE


def test_encoder_order_follows_the_scope() -> None:
    listed = ("libx264", "libsvtav1", "hevc_nvenc", "av1_nvenc", "libx265", "h264_vaapi")
    assert rp.usable_encoders(listed, nvidia=True) == (
        "hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1", "libx264",
    )  # fmt: skip
    assert rp.usable_encoders(listed, nvidia=False) == ("libsvtav1", "libx265", "libx264")
    assert rp.usable_encoders((), nvidia=True) == ()


def test_parse_decoders() -> None:
    text = (
        "Decoders:\n V..... = Video\n ------\n V....D h264  H.264\n"
        " VFS..D hevc  HEVC\n A....D aac  AAC\n"
    )
    assert rp.parse_decoders(text) == frozenset({"h264", "hevc", "aac"})


def test_encode_argv_keeps_10_bit_and_colour() -> None:
    info = rp.source_info(_ffprobe())
    assert info is not None
    argv = pipeline.encode_argv("ffmpeg", "hevc_nvenc", info, Path("/o/v.mkv"))
    assert argv[argv.index("-f") + 1] == "yuv4mpegpipe"
    assert argv[argv.index("-color_primaries") + 1] == "bt709"
    assert argv[argv.index("-color_range") + 1] == "tv"
    assert argv[argv.index("-pix_fmt") + 1] == "p010le" and "main10" in argv
    x264 = pipeline.encode_argv("ffmpeg", "libx264", info, Path("/o/v.mkv"))
    assert x264[x264.index("-pix_fmt") + 1] == "yuv420p"  # x264 stays 8-bit here
    assert argv[-1] == "/o/v.mkv" and "-an" in argv


def test_software_encoder_presets_and_low_priority() -> None:
    info = rp.source_info(_ffprobe())
    assert info is not None
    x265 = pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"))
    assert x265[x265.index("-preset") + 1] == "fast"
    av1 = pipeline.encode_argv("ffmpeg", "libsvtav1", info, Path("/o/v.mkv"))
    assert av1[av1.index("-preset") + 1] == "8"
    argv = ["mkvmerge", "-o", "x"]
    assert pipeline.low_priority(argv, nice="/n", ionice="/i", level=10) == [
        "/n", "-n", "10", "/i", "-c2", "-n7", *argv
    ]  # fmt: skip
    assert pipeline.low_priority(argv, nice=None, ionice=None, level=10) == argv


def test_offline_fps_costs_the_interpolated_frames() -> None:
    assert rp.offline_fps(60.0, (1920, 1080), (1920, 1080)) == rp.offline_fps(
        60.0, (1920, 1080), (1920, 1080), src_fps=Fraction(24), target=Fraction(48)
    )
    two = rp.offline_fps(60.0, (1920, 1080), (1920, 1080))
    sixty = rp.offline_fps(60.0, (1920, 1080), (1920, 1080), src_fps=Fraction(24),
                           target=Fraction(60))  # fmt: skip
    assert two is not None and sixty is not None
    assert sixty == pytest.approx(two * 60 / 96, abs=0.1)  # 4 of 5 frames inferred


def test_remux_argv_keeps_everything_but_video() -> None:
    mk = pipeline.mkvmerge_argv("mkvmerge", Path("v.mkv"), Path("s.mkv"), Path("o"), start_s=0.042)
    assert mk == ["mkvmerge", "--quiet", "-o", "o", "--sync", "0:42", "v.mkv", "-D", "s.mkv"]
    ff = pipeline.ffmpeg_remux_argv("ffmpeg", Path("v.mkv"), Path("s.mkv"), Path("o"), start_s=0)
    for part in ("1:a?", "1:s?", "1:t?"):
        assert part in ff
    assert ff[ff.index("-map_chapters") + 1] == "1" and "-itsoffset" not in ff


def test_parse_progress_takes_the_last_frame_line() -> None:
    text = "Script evaluation done\rFrame: 10/958 (40.1 fps)\rFrame: 957/958 (45.32 fps)\r"
    p = pipeline.parse_progress(text)
    assert p == pipeline.Progress(957, 958, 45.32)
    assert pipeline.parse_progress("Output 958 frames") is None


def test_vspipe_argv_and_temp_paths() -> None:
    argv = pipeline.vspipe_argv("vspipe", Path("/c/job.vpy"), {"v": 1, "title": "a'b"})
    assert argv[:5] == ["vspipe", "-c", "y4m", "-p", "-a"]
    assert argv[5] == 'user_data={"title":"a\'b","v":1}' and argv[-1] == "-"
    assert "-r" not in argv
    bounded = pipeline.vspipe_argv("vspipe", Path("/c/job.vpy"), {"v": 1}, requests=8)
    assert bounded[6:8] == ["-r", "8"] and bounded[-2:] == ["/c/job.vpy", "-"]
    assert jobs.vspipe_requests(BackendId.RIFE_NCNN) == 8
    assert jobs.vspipe_requests(BackendId.MVTOOLS) is None
    video, part = pipeline.temp_paths(Path("/m/Film.smooth.mkv"))
    assert video == Path("/m/.Film.smooth.mkv.buttereye-video.mkv")
    assert part == Path("/m/.Film.smooth.mkv.buttereye-part")


def test_offline_target() -> None:
    src = Fraction(24000, 1001)
    assert jobs.offline_target(Target(TargetKind.X2), src) == src * 2
    assert jobs.offline_target(Target(TargetKind.DISPLAY), src) == src * 2
    assert jobs.offline_target(Target(TargetKind.FPS, Fraction(60)), src) == 60


def _bench(mpv: float, *, size: tuple[int, int] = (1920, 1080), faults: int = 0) -> BenchResult:
    m = BenchMeasurement(
        label="rife-v4.26", backend=BackendId.RIFE_NCNN, model="rife-v4.26_ensembleFalse",
        vspipe_fps=mpv + 8, mpv_fps=mpv, cov=0.01, repeatable=True, startup_s=1.1,
        reload_s=0.9, vram_bytes=None, realtime=True, gpu_faults=faults,
    )  # fmt: skip
    req = BenchRequest(size[0], size[1], Fraction(24000, 1001))
    return BenchResult(req, (m,), None, datetime(2026, 10, 7, tzinfo=UTC), None)


def test_bench_rate_scales_by_pixels() -> None:
    results = (_bench(61.2),)
    m = "rife-v4.26_ensembleFalse"
    assert jobs.bench_rate(results, BackendId.RIFE_NCNN, m, (1920, 1080)) == pytest.approx(61.2)
    assert jobs.bench_rate(results, BackendId.RIFE_NCNN, m, (3840, 2160)) == pytest.approx(15.3)
    assert jobs.bench_rate(results, BackendId.MVTOOLS, None, (1920, 1080)) is None
    assert jobs.bench_rate((_bench(61.2, faults=1),), BackendId.RIFE_NCNN, m, (1920, 1080)) is None


def test_render_sizes_and_estimates_match_the_spike() -> None:
    rate = {(3840, 2160): 15.3, (2560, 1440): 34.4, (1920, 1080): 61.2, (1280, 720): 137.7}
    sizes = rp.render_sizes(3840, 2160, rate)
    assert [(s.width, s.height) for s in sizes] == [
        (3840, 2160), (2560, 1440), (1920, 1080), (1280, 720),
    ]  # fmt: skip
    # M0(e): 4K → 1080p measured 36 fps offline; the estimate is close and not above
    by = {(s.width, s.height): s.est_fps for s in sizes}
    assert by[(1920, 1080)] == pytest.approx(39.4, abs=1.0)
    assert by[(3840, 2160)] == pytest.approx(12.2, abs=0.5)
    assert rp.render_sizes(1280, 720, {})[0].est_fps is None


def test_estimate_bytes_counts_the_temporary_track() -> None:
    one_hour = rp.estimate_bytes("hevc_nvenc", (1920, 1080), Fraction(48), 3600, 10**9)
    # ~6 Mbps of video an hour (2.7 GB), twice (temporary track), plus the source
    assert 5 * 2**30 < one_hour < 8 * 2**30


def test_engine_for() -> None:
    both = frozenset({BackendId.RIFE_NCNN, BackendId.MVTOOLS})
    assert rp.engine_for("auto", both) is BackendId.RIFE_NCNN
    assert rp.engine_for("auto", frozenset({BackendId.MVTOOLS})) is BackendId.MVTOOLS
    assert rp.engine_for(BackendId.MVTOOLS, both) is BackendId.MVTOOLS
    # a pinned engine that isn't installed falls back (the copy is still made)
    assert rp.engine_for(BackendId.RIFE_NCNN, frozenset({BackendId.MVTOOLS})) is BackendId.MVTOOLS
    # ... but never to TensorRT unless the profile asked for it
    assert rp.engine_for(BackendId.MVTOOLS, frozenset({BackendId.RIFE_TRT})) is None
    assert rp.engine_for("auto", frozenset()) is None
