# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Pure parts of offline render (SCOPE §7, §7.8): ffprobe parsing, refusals,
encoders, command lines, progress, estimates, the offline target."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.errors import ErrorCode
from buttereye.core.render import jobs, pipeline
from buttereye.core.render import probe as rp
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    ContentKind,
    HdrClass,
    Profile,
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


#: the first frame of a real 2160p HDR10 WEB-DL (P3-D65 mastering, 1000 nits,
#: content light level unknown), as ffprobe -show_frames prints it
_HDR10_FRAME: dict[str, Any] = {
    "media_type": "video",
    "side_data_list": [
        {
            "side_data_type": "Mastering display metadata",
            "red_x": "35400/50000", "red_y": "14600/50000",
            "green_x": "8500/50000", "green_y": "39850/50000",
            "blue_x": "6550/50000", "blue_y": "2300/50000",
            "white_point_x": "15635/50000", "white_point_y": "16450/50000",
            "min_luminance": "500/10000", "max_luminance": "10000000/10000",
        },
        {"side_data_type": "Content light level metadata", "max_content": 0, "max_average": 0},
    ],
}  # fmt: skip
_PQ = {"color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc"}


def _hdr10(frame: dict[str, Any] | None = _HDR10_FRAME, **video: Any) -> rp.SourceInfo:
    info = rp.source_info(_ffprobe(**{**_PQ, **video}), frame)
    assert info is not None
    return info


def _dv(profile: int, base: int) -> rp.SourceInfo:
    record = {"side_data_type": "DOVI configuration record", "dv_profile": profile,
              "dv_bl_signal_compatibility_id": base}  # fmt: skip
    return _hdr10(side_data_list=[record])


def test_refusals() -> None:
    sdr = rp.source_info(_ffprobe())
    hlg = rp.source_info(_ffprobe(color_transfer="arib-std-b67"))
    dec = frozenset({"hevc", "h264"})
    assert jobs.refusal_for(sdr, dec, True) is None
    assert jobs.refusal_for(sdr, frozenset(), True) is None  # decoder list unknown: let ffms2 try
    missing = jobs.refusal_for(sdr, dec, False)
    assert missing is not None and missing.code is ErrorCode.FFMS2_MISSING
    # the platform's own line, spelled out so each one really is asserted:
    # ButterEye's installer brings ffms2 on Windows, so there is nothing to type
    want = (
        ()
        if osapi.IS_WINDOWS
        else ("brew install ffms2",)
        if osapi.IS_MACOS
        else ("sudo dnf install ffms2",)
    )
    assert missing.commands == want
    nodec = jobs.refusal_for(sdr, frozenset({"h264"}), True)
    assert nodec is not None and nodec.code is ErrorCode.CODEC_NOT_DECODABLE
    assert nodec.title.params == {"codec": "hevc"}
    r = jobs.refusal_for(hlg, dec, True)
    assert r is not None and r.code is ErrorCode.HDR_CLASS_REFUSED
    r = jobs.refusal_for(None, dec, True)
    assert r is not None and r.code is ErrorCode.CODEC_NOT_DECODABLE


def test_hdr10_and_its_relatives_are_converted() -> None:
    dec = frozenset({"hevc"})
    plus = {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}
    hdr10plus = _hdr10({"media_type": "video", "side_data_list": [plus]})
    assert hdr10plus.hdr_class is HdrClass.HDR10PLUS  # found in the frame, not the stream
    for info in (_hdr10(), hdr10plus, _dv(8, 1), _dv(7, 6)):
        assert rp.renders_as_hdr10(info), info.hdr_class
        assert jobs.refusal_for(info, dec, True) is None
    p5 = jobs.refusal_for(_dv(5, 0), dec, True)
    assert p5 is not None and p5.code is ErrorCode.HDR_CLASS_REFUSED and "5" in p5.title.key
    sdr_base = jobs.refusal_for(_dv(8, 2), dec, True)  # DV over an SDR base layer
    assert sdr_base is not None and sdr_base.code is ErrorCode.HDR_CLASS_REFUSED
    assert not rp.renders_as_hdr10(rp.source_info(_ffprobe()))  # type: ignore[arg-type]


def test_hdr_warnings_name_what_is_dropped() -> None:
    assert jobs.hdr_warnings(rp.source_info(_ffprobe())) == []
    assert [w.id for w in jobs.hdr_warnings(_hdr10())] == ["render.hdr10"]
    dv = jobs.hdr_warnings(_dv(8, 1))
    assert [w.id for w in dv] == ["render.hdr10", "render.hdr_dynamic"]
    assert dv[1].title.params == {"kind": "Dolby Vision"}


def test_hdr_metadata_from_the_first_frame() -> None:
    meta = _hdr10().hdr
    m = meta.mastering
    assert m is not None
    assert m.red == (Fraction(354, 500), Fraction(146, 500))
    assert m.white == (Fraction(15635, 50000), Fraction(16450, 50000))
    assert (m.max_luminance, m.min_luminance) == (Fraction(1000), Fraction(1, 20))
    assert (meta.max_cll, meta.max_fall) == (0, 0)
    assert _hdr10(frame=None).hdr.mastering is None  # no frame data: nothing invented


def test_x265_and_svtav1_hdr10_parameters() -> None:
    meta = _hdr10().hdr
    assert rp.x265_hdr_params(meta) == (
        "hdr10=1:hdr10-opt=1:repeat-headers=1:"
        "master-display=G(8500,39850)B(6550,2300)R(35400,14600)WP(15635,16450)"
        "L(10000000,500):max-cll=0,0"
    )
    assert rp.svtav1_hdr_params(meta) == (
        "mastering-display=G(0.1700,0.7970)B(0.1310,0.0460)R(0.7080,0.2920)"
        "WP(0.3127,0.3290)L(1000.0000,0.0500)"
    )
    bare = rp.HdrMeta(max_cll=1000, max_fall=400)
    assert rp.x265_hdr_params(bare) == "hdr10=1:hdr10-opt=1:repeat-headers=1:max-cll=1000,400"
    assert rp.svtav1_hdr_params(bare) == "content-light=1000,400"
    assert rp.svtav1_hdr_params(rp.HdrMeta()) is None


def test_hdr10_encode_argv() -> None:
    info = _hdr10(pix_fmt="yuv420p10le")
    x265 = pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"))
    assert x265[x265.index("-x265-params") + 1].startswith("hdr10=1:")
    assert x265[x265.index("-pix_fmt") + 1] == "yuv420p10le"
    assert x265[x265.index("-color_trc") + 1] == "smpte2084"
    assert x265[x265.index("-colorspace") + 1] == "bt2020nc"
    av1 = pipeline.encode_argv("ffmpeg", "libsvtav1", info, Path("/o/v.mkv"))
    assert av1[av1.index("-svtav1-params") + 1].startswith("mastering-display=")
    sdr = pipeline.encode_argv("ffmpeg", "libx265", rp.source_info(_ffprobe()), Path("/o"))  # type: ignore[arg-type]
    assert "-x265-params" not in sdr


def test_hdr10_encoders_and_matrix() -> None:
    listed = ("hevc_nvenc", "libsvtav1", "libx265", "libx264")
    assert rp.usable_encoders(listed, nvidia=True, hdr10=True) == ("libx265", "libsvtav1")
    assert rp.usable_encoders(("hevc_nvenc",), nvidia=True, hdr10=True) == ()
    info = _hdr10()
    assert (rp.vs_matrix(info), rp.vs_range(info)) == ("2020ncl", "limited")


def test_hdr10_remux_writes_container_colour() -> None:
    mk = pipeline.mkvmerge_argv(
        "mkvmerge", Path("v.mkv"), Path("s.mkv"), Path("o"), start_s=0, src=_hdr10()
    )
    opt = dict(zip(mk[4:-3:2], mk[5:-3:2], strict=True))
    assert opt["--colour-transfer-characteristics"] == "0:16"
    assert opt["--colour-primaries"] == "0:9" and opt["--colour-matrix-coefficients"] == "0:9"
    assert opt["--colour-range"] == "0:1"
    assert opt["--chromaticity-coordinates"] == "0:0.708,0.292,0.17,0.797,0.131,0.046"
    assert opt["--white-colour-coordinates"] == "0:0.3127,0.329"
    assert (opt["--max-luminance"], opt["--min-luminance"]) == ("0:1000", "0:0.05")
    assert "--max-content-light" not in opt  # unknown in the source: not invented
    assert mk[-3:] == ["v.mkv", "-D", "s.mkv"]


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
    assert jobs.vspipe_requests(BackendId.RIFE_NCNN) == (4 if osapi.IS_MACOS else 8)
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


@pytest.mark.parametrize(
    ("engine", "content", "on"),
    [
        (BackendId.RIFE_NCNN, ContentKind.ANIMATION, True),
        # a copy has no real-time limit: Automatic re-times held drawings too
        (BackendId.MVTOOLS, ContentKind.AUTO, True),
        (BackendId.MVGPU, ContentKind.AUTO, True),
        (BackendId.RIFE_NCNN, ContentKind.LIVE, False),
        (BackendId.RIFE_TRT, ContentKind.ANIMATION, False),  # TensorRT has none
    ],
)
def test_hold_fill_for_a_copy(engine: BackendId, content: ContentKind, on: bool) -> None:
    p = Profile(
        id="p", name="P", backend="auto", model=None, scale=None,
        target=Target(TargetKind.X2), sc_threshold=0.12, buffered_frames=None,
        concurrent_frames=None, content=content,
    )  # fmt: skip
    assert jobs._hold_fill(engine, p) is on
    assert jobs._hold_fill(BackendId.RIFE_NCNN, None) is True


@pytest.mark.parametrize(
    ("backend", "content", "engine"),
    [
        # TensorRT has no hold fill: 2D animation on Automatic takes RIFE-ncnn
        ("auto", ContentKind.ANIMATION, BackendId.RIFE_NCNN),
        ("auto", ContentKind.AUTO, BackendId.RIFE_TRT),  # time is the cost offline
        (BackendId.RIFE_TRT, ContentKind.ANIMATION, BackendId.RIFE_TRT),  # pinned
    ],
)
def test_animation_copy_prefers_an_engine_with_hold_fill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: Any, content: ContentKind,
    engine: BackendId,
) -> None:  # fmt: skip
    from types import SimpleNamespace

    from buttereye.core.mpvctl import decide

    installed = frozenset({BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVTOOLS})
    monkeypatch.setattr(jobs, "_installed", lambda ctx, profile=None: installed)
    monkeypatch.setattr(jobs, "_trt", lambda ctx, profile: (object(), "rife-trt-model"))
    model = decide.ModelChoice("rife-v4.26", tmp_path / "rife-v4.26", None)
    monkeypatch.setattr(decide, "find_model", lambda *a, **k: model)
    ctx = SimpleNamespace(paths=SimpleNamespace(rpm_data_dir=tmp_path, data_dir=tmp_path))
    p = Profile(
        id="p", name="P", backend=backend, model=None, scale=None,
        target=Target(TargetKind.X2), sc_threshold=0.12, buffered_frames=None,
        concurrent_frames=None, content=content,
    )  # fmt: skip
    got, mc = jobs._engine(ctx, p)  # type: ignore[arg-type]
    assert got is engine
    trt_model = got is BackendId.RIFE_TRT
    assert mc is not None and mc.name == ("rife-trt-model" if trt_model else "rife-v4.26")
    assert jobs._hold_fill(got, p) is (got is not BackendId.RIFE_TRT)
    # no RIFE-ncnn model: the usual order; 2D animation never takes TensorRT
    # (no hold fill), MVTools re-times held drawings
    none = decide.ModelChoice(None, None, None)
    monkeypatch.setattr(decide, "find_model", lambda *a, **k: none)
    if backend == "auto":
        want = BackendId.MVTOOLS if content is ContentKind.ANIMATION else BackendId.RIFE_TRT
        assert jobs._engine(ctx, p)[0] is want  # type: ignore[arg-type]


@pytest.mark.parametrize("macos", [False, True])
def test_rife_requests_are_lower_on_a_mac(monkeypatch: pytest.MonkeyPatch, macos: bool) -> None:
    monkeypatch.setattr(osapi, "IS_MACOS", macos)
    # P7: gpu_thread is 1 there (one MoltenVK queue); 8 only held frames that wait
    assert jobs.vspipe_requests(BackendId.RIFE_NCNN) == (jobs.RIFE_REQUESTS_MACOS if macos else 8)
    assert jobs.vspipe_requests(BackendId.MVGPU) == 8


# ---------------------------------------------------------------- B20: VFR
@pytest.mark.parametrize(
    ("r", "avg", "vfr", "fps"),
    [
        # an MP4 edit list skews the average by 0.13 %: one rate, the nominal one
        ("24000/1001", "2873400/120000", False, Fraction(24000, 1001)),
        ("25/1", "2499/100", False, Fraction(25)),
        # 24 against 23.976 can't be told apart from a skew: frames / duration wins
        ("24/1", "24000/1001", False, Fraction(24000, 1001)),
        ("60/1", "2997/100", True, Fraction(2997, 100)),  # a real variable rate
        ("30/1", "2970/100", True, Fraction(2970, 100)),  # 1 % apart
    ],
)
def test_vfr_needs_more_than_a_rounding_difference(
    r: str, avg: str, vfr: bool, fps: Fraction
) -> None:
    info = rp.source_info(_ffprobe(r_frame_rate=r, avg_frame_rate=avg))
    assert info is not None
    assert (info.vfr, info.fps) == (vfr, fps)
    assert rp.is_vfr(None, Fraction(24)) is False


# ---------------------------------------------------------------- P2: VideoToolbox
def test_videotoolbox_comes_first_on_a_mac_and_nowhere_else() -> None:
    listed = ("libx264", "libsvtav1", "libx265", "hevc_videotoolbox", "prores_videotoolbox")
    assert rp.usable_encoders(listed, nvidia=False, apple=True) == (
        "hevc_videotoolbox", "libsvtav1", "libx265", "libx264", "prores_videotoolbox",
    )  # fmt: skip
    assert rp.usable_encoders(listed, nvidia=False) == ("libsvtav1", "libx265", "libx264")
    # HDR10 needs mastering data on the command line: never VideoToolbox
    assert rp.usable_encoders(listed, nvidia=False, hdr10=True, apple=True) == (
        "libx265", "libsvtav1",
    )  # fmt: skip


def _sdr8() -> rp.SourceInfo:
    info = rp.source_info(_ffprobe(pix_fmt="yuv420p", codec_name="h264", width=1920, height=1080))
    assert info is not None and info.bits == 8
    return info


def test_videotoolbox_encode_argv() -> None:
    hevc = pipeline.encode_argv("ffmpeg", "hevc_videotoolbox", _sdr8(), Path("/o/v.mkv"))
    assert hevc[hevc.index("-q:v") + 1] == "60" and hevc[hevc.index("-tag:v") + 1] == "hvc1"
    assert hevc[hevc.index("-pix_fmt") + 1] == "yuv420p" and "-profile:v" not in hevc
    ten = pipeline.encode_argv("ffmpeg", "hevc_videotoolbox", rp.source_info(_ffprobe()),  # type: ignore[arg-type]
                               Path("/o/v.mkv"))  # fmt: skip
    assert ten[ten.index("-pix_fmt") + 1] == "p010le"
    assert ten[ten.index("-profile:v") + 1] == "main10"
    prores = pipeline.encode_argv("ffmpeg", "prores_videotoolbox", _sdr8(), Path("/o/v.mkv"))
    assert prores[prores.index("-profile:v") + 1] == "hq"
    assert prores[prores.index("-pix_fmt") + 1] == "p210le"  # 4:2:2 10-bit, always
    test = pipeline.encoder_test_argv("/ff", "prores_videotoolbox")
    assert test[0] == "/ff" and "p210le" in test and test[-3:] == ["-f", "null", "-"]


# ---------------------------------------------------------------- P13: 10-bit
def test_ten_bit_output_for_an_8_bit_source() -> None:
    info = _sdr8()
    x265 = pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"), ten_bit=True)
    assert x265[x265.index("-pix_fmt") + 1] == "yuv420p10le"
    nvenc = pipeline.encode_argv("ffmpeg", "hevc_nvenc", info, Path("/o/v.mkv"), ten_bit=True)
    assert nvenc[nvenc.index("-pix_fmt") + 1] == "p010le" and "main10" in nvenc
    # x264 has no 10-bit here: the option changes nothing
    x264 = pipeline.encode_argv("ffmpeg", "libx264", info, Path("/o/v.mkv"), ten_bit=True)
    assert x264[x264.index("-pix_fmt") + 1] == "yuv420p"
    off = pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"))
    assert off[off.index("-pix_fmt") + 1] == "yuv420p"
    assert pipeline.ten_bit_output(rp.source_info(_ffprobe()), "libx265")  # type: ignore[arg-type]


# ---------------------------------------------------------------- LUT
def test_filter_values_are_escaped_twice() -> None:
    # ffmpeg-filters(1)'s own example, both levels
    text = "this is a 'string': may contain one, or more, special characters"
    assert pipeline.filter_value(text) == (
        "this is a \\\\\\'string\\\\\\'\\\\: may contain one\\, or more\\, special characters"
    )
    assert pipeline.filter_value("/Users/a/Library/Application Support/x.cube") == (
        "/Users/a/Library/Application Support/x.cube"
    )


def test_lut_filter_keeps_the_matrix_range_and_bits() -> None:
    info = _sdr8()
    vf = pipeline.lut_filter(info, Path("/l/film.cube"), "yuv420p10le").split(",")
    assert vf == [
        "scale=in_color_matrix=bt709:in_range=tv",
        "format=gbrpf32le",
        "lut3d=file=/l/film.cube:interp=tetrahedral",
        "scale=out_color_matrix=bt709:out_range=tv",
        "format=yuv420p10le",
    ]
    sd = rp.source_info(_ffprobe(width=720, height=480, color_space="unknown", color_range="pc"))
    assert sd is not None
    sd_vf = pipeline.lut_filter(sd, Path("/l/a.cube"), "yuv420p")
    assert "in_color_matrix=smpte170m:in_range=pc" in sd_vf  # no matrix tag on SD: BT.601
    argv = pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"), lut=Path("/l/f.cube"))
    assert argv.index("-vf") < argv.index("-c:v")
    assert argv[argv.index("-vf") + 1].endswith("format=yuv420p")  # what -pix_fmt says
    assert "-vf" not in pipeline.encode_argv("ffmpeg", "libx265", info, Path("/o/v.mkv"))


# ---------------------------------------------------------------- B2: remux
def test_ffmpeg_remux_converts_only_mp4_text_subtitles() -> None:
    data = _ffprobe()
    data["streams"][2:2] = [
        {"index": 4, "codec_type": "subtitle", "codec_name": "mov_text"},
        {"index": 5, "codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        {"index": 6, "codec_type": "subtitle", "codec_name": "mov_text"},
    ]
    info = rp.source_info(data)
    assert info is not None
    ff = pipeline.ffmpeg_remux_argv(
        "ffmpeg", Path("v.mkv"), Path("s.mp4"), Path("o"), start_s=0, src=info
    )
    # subtitles in order: mov_text, pgs, mov_text, subrip -> outputs 0 and 2
    assert ff[ff.index("-c:s:0") + 1] == "srt" and ff[ff.index("-c:s:2") + 1] == "srt"
    assert "-c:s:1" not in ff and "-c:s:3" not in ff and "-c:s" not in ff
    assert ff.index("-c") < ff.index("-c:s:0") < ff.index("-f")
    plain = pipeline.ffmpeg_remux_argv("ffmpeg", Path("v"), Path("s"), Path("o"), start_s=0)
    assert not any(a.startswith("-c:s") for a in plain)


def test_remux_on_a_mac_is_throttled_with_taskpolicy() -> None:
    argv = ["ffmpeg", "-i", "x"]
    assert pipeline.low_priority(argv, nice="/n", ionice=None, level=10, taskpolicy="/tp") == [
        "/n", "-n", "10", "/tp", "-b", *argv,
    ]  # fmt: skip


def test_hdr10_note_without_mkvmerge_says_what_the_file_lacks() -> None:
    kept = jobs.hdr_warnings(_hdr10())[0]
    assert "mastering display and light levels;" in kept.cause.key and not kept.commands
    ff = jobs.hdr_warnings(_hdr10(), mkvmerge=False)[0]
    assert "doesn't list them" in ff.cause.key and ff.commands == (jobs.INSTALL_MKVTOOLNIX,)


def test_mkvmerge_is_looked_for_in_the_app_then_homebrew_then_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app, brew = tmp_path / "Resources", tmp_path / "brew"
    (app / "bin").mkdir(parents=True)
    brew.mkdir()
    monkeypatch.setattr(osapi, "MACOS_APP", app)
    monkeypatch.setattr(osapi, "LINUX_APP", None)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(jobs, "BREW_BINS", (brew,))
    monkeypatch.setattr("shutil.which", lambda name: f"/path/{name}")
    assert jobs._mkvmerge() == "/path/mkvmerge"
    (brew / "mkvmerge").write_text("#!/bin/sh\n")
    (brew / "mkvmerge").chmod(0o755)
    assert jobs._mkvmerge() == str(brew / "mkvmerge")  # Finder's PATH has no Homebrew
    (app / "bin" / "mkvmerge").write_text("")
    assert jobs._mkvmerge() == str(app / "bin" / "mkvmerge")


# ---------------------------------------------------------------- B18
def test_an_encoder_that_dies_first_is_the_encoders_failure() -> None:
    def code(vs_rc: int, ff_rc: int, vs_first: bool) -> ErrorCode | None:
        err = jobs._failure(vs_rc, ff_rc, vs_first, "libx265", ["vs line"], "No space left")
        return err.code if err is not None else None

    assert code(0, 0, True) is None
    # ffmpeg ended first (disk full); vspipe then failed on the broken pipe
    assert code(1, 1, False) is ErrorCode.ENCODER_FAILED
    assert code(-13, 1, True) is ErrorCode.ENCODER_FAILED  # killed by SIGPIPE
    assert code(0, 1, True) is ErrorCode.ENCODER_FAILED
    # the script failed first: ffmpeg's complaint about a short input follows from it
    assert code(1, 1, True) is ErrorCode.VSPIPE_FRAME_ERROR
    assert code(1, 0, True) is ErrorCode.VSPIPE_FRAME_ERROR
    err = jobs._failure(1, 1, False, "libx265", ["vs line"], "No space left")
    assert err is not None and err.detail == "No space left"


# ---------------------------------------------------------------- B1 / P1: engine by time
#: the M2 Air's measured 1080p in-mpv rates (2x units): RIFE v4.26 and mvgpu
_M2 = {BackendId.RIFE_NCNN: 8.37, BackendId.MVGPU: 71.8, BackendId.MVTOOLS: 76.0}


def _m2_rates(w: int, h: int) -> dict[BackendId, dict[tuple[int, int], float | None]]:
    from buttereye.core.mpvctl import decide

    sizes = ((w, h), *decide.smaller_sizes(w, h))
    return {b: {s: fps * 1920 * 1080 / (s[0] * s[1]) for s in sizes} for b, fps in _M2.items()}


def test_a_slow_gpu_switches_engine_before_shrinking() -> None:
    from buttereye.core.mpvctl import decide

    order = rp.offline_order("auto", frozenset(_M2))
    assert order == (BackendId.RIFE_NCNN, BackendId.MVGPU, BackendId.MVTOOLS)
    work = decide.load_rate(Fraction(24000, 1001), Fraction(48000, 1001))
    sizes = rp.plan_sizes(1920, 1080, _m2_rates(1920, 1080), order, work)
    first = sizes[0]
    assert isinstance(first, rp.EngineSize) and (first.width, first.height) == (1920, 1080)
    # RIFE: 6.7 fps, 7× the film's length; mvgpu fits at the full size
    assert dict(first.options)[BackendId.RIFE_NCNN] == pytest.approx(6.7, abs=0.1)
    assert first.engine is BackendId.MVGPU and first.est_fps == pytest.approx(57.4, abs=0.1)
    # nothing measured: the best picture, as before benchmarks existed
    none = rp.plan_sizes(1920, 1080, {}, order, work)
    assert none[0].engine is BackendId.RIFE_NCNN and none[0].est_fps is None
    # a fast GPU keeps RIFE
    fast = {b: {(1920, 1080): 200.0} for b in order}
    assert rp.plan_sizes(1920, 1080, fast, order, work)[0].engine is BackendId.RIFE_NCNN


def test_pick_offline_takes_the_fastest_when_nothing_fits() -> None:
    options = [(BackendId.RIFE_NCNN, 2.0), (BackendId.MVGPU, 3.0), (BackendId.MVTOOLS, 9.0)]
    assert rp.pick_offline(options, 48.0) == (BackendId.MVTOOLS, 9.0)
    assert rp.pick_offline(options, 4.0) == (BackendId.RIFE_NCNN, 2.0)  # 2 ≥ 4 / 2
    assert rp.pick_offline([], 48.0) is None
    # a pinned profile has one option, which it keeps however slow
    assert rp.pick_offline([(BackendId.RIFE_NCNN, 1.0)], 48.0) == (BackendId.RIFE_NCNN, 1.0)


def test_pick_offline_keeps_a_better_engine_with_no_estimate() -> None:
    # the profile's RIFE model was never in the speed test: RIFE has no estimate
    rife_unknown = [(BackendId.RIFE_NCNN, None), (BackendId.MVGPU, 57.4)]
    assert rp.pick_offline(rife_unknown, 48.0) == (BackendId.RIFE_NCNN, None)
    # an unmeasured mvgpu is not known to miss; mvtools is measured too slow
    options = [(BackendId.RIFE_NCNN, 2.0), (BackendId.MVGPU, None), (BackendId.MVTOOLS, 9.0)]
    assert rp.pick_offline(options, 48.0) == (BackendId.MVGPU, None)
    # but a measured engine that fits still wins over an unmeasured worse one
    fits = [(BackendId.RIFE_NCNN, 30.0), (BackendId.MVGPU, None)]
    assert rp.pick_offline(fits, 48.0) == (BackendId.RIFE_NCNN, 30.0)
    # an unmeasured TensorRT ahead of a measured RIFE
    trt = [(BackendId.RIFE_TRT, None), (BackendId.RIFE_NCNN, 30.0)]
    assert rp.pick_offline(trt, 48.0) == (BackendId.RIFE_TRT, None)


def test_offline_order() -> None:
    every = frozenset(BackendId)
    assert rp.offline_order("auto", every) == rp.OFFLINE_RANKING
    assert rp.offline_order("auto", every, animation=True) == rp.ANIMATION_RANKING
    assert rp.offline_order(BackendId.MVGPU, every) == (BackendId.MVGPU,)
    # pinned but not installed: the ranking, never a TensorRT nobody asked for
    assert rp.offline_order(BackendId.MVGPU, every - {BackendId.MVGPU}) == (
        BackendId.RIFE_NCNN, BackendId.MVTOOLS,
    )  # fmt: skip
    assert rp.engine_for("auto", frozenset({BackendId.MVGPU, BackendId.MVTOOLS})) is (
        BackendId.MVGPU
    )


def test_encode_key_follows_what_the_track_depends_on(tmp_path: Path) -> None:
    from buttereye.core.types import RenderJobSpec

    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    spec = RenderJobSpec(src, tmp_path / "a.smooth.mkv", "simple", "libx265")
    key = jobs.encode_key(src, spec, Fraction(48), (1920, 1080), None)
    assert key == jobs.encode_key(src, spec, Fraction(48), (1920, 1080), None)
    assert key != jobs.encode_key(src, spec, Fraction(60), (1920, 1080), None)
    assert key != jobs.encode_key(src, spec, Fraction(48), (1280, 720), None)
    other = RenderJobSpec(src, tmp_path / "a.smooth.mkv", "simple", "libsvtav1")
    assert key != jobs.encode_key(src, other, Fraction(48), (1920, 1080), None)
    src.write_bytes(b"xy")  # the source changed
    assert key != jobs.encode_key(src, spec, Fraction(48), (1920, 1080), None)


def test_encode_key_follows_the_profiles_smoothing(tmp_path: Path) -> None:
    from buttereye.core.profiles import defaults
    from buttereye.core.types import ContentKind, RenderJobSpec

    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    spec = RenderJobSpec(src, tmp_path / "a.smooth.mkv", "simple", "libx265")
    p = defaults.builtin_profiles()[0]

    def key(profile: Any) -> str:
        return jobs.encode_key(src, spec, Fraction(48), (1920, 1080), None, profile)

    assert key(p) == key(dataclasses.replace(p, name="renamed"))
    for change in (
        {"model": "rife-v4.6"},
        {"sc_threshold": 0.3},
        {"content": ContentKind.ANIMATION},
        {"aa": not p.aa},
    ):
        assert key(p) != key(dataclasses.replace(p, **change)), change
    assert key(None) == jobs.encode_key(src, spec, Fraction(48), (1920, 1080), None)
