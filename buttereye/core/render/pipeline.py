# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Command lines for one render (SCOPE §7.1, §7.2, §7.5). Pure functions.

``vspipe -c y4m -p`` (the live script with a file source) | ``ffmpeg`` encode to a
video-only MKV, then ``mkvmerge`` (or ``ffmpeg``) copies every other stream of the
source next to it. The y4m header carries no colour, so colour flags (and the
chroma siting) are always passed from ffprobe's answer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core import pixfmt
from buttereye.core.render.probe import (
    TEN_BIT_ENCODERS,
    SourceInfo,
    chroma_sample_location,
    pick_bits,
    renders_as_hdr10,
    svtav1_hdr_params,
    x265_hdr_params,
)
from buttereye.core.types import ColourDepth, RenderJobSpec

_FRAME_RE = re.compile(r"Frame:\s*(\d+)/(\d+)(?:\s*\(([\d.]+)\s*fps\))?")

# quality settings per encoder, SDR (§7.5); the depth is ``output_bits``.
# Software presets are one step lighter than the encoders'
# defaults: at the doubled frame rate x265 medium kept ~9 cores and SVT-AV1 6
# ~8 cores busy; fast / 8 are ~1.5-2x lighter for files a few % larger at the
# same CRF.
_ENCODER_ARGS: Mapping[str, tuple[str, ...]] = {
    "hevc_nvenc": ("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0"),
    "av1_nvenc": ("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "28", "-b:v", "0"),
    "libx265": ("-preset", "fast", "-crf", "20"),
    "libsvtav1": ("-preset", "8", "-crf", "28"),
    "libx264": ("-preset", "medium", "-crf", "18"),
    # the Mac's media engine: constant quality (1-100), hardware only (-allow_sw
    # is off by default); 60 is not calibrated against x265's CRF 20 yet. hvc1
    # is the tag Apple's players want, and Matroska accepts it
    "hevc_videotoolbox": ("-q:v", "60", "-tag:v", "hvc1"),
    # ProRes 422 HQ, always 4:2:2 10-bit (ffmpeg converts the 4:2:0 frames)
    "prores_videotoolbox": ("-profile:v", "hq"),
    # the same on the CPU: profile 3 is 422 HQ; the apl0 vendor tag marks the
    # stream as Apple's, which some Apple software checks for
    "prores_ks": ("-profile:v", "3", "-vendor", "apl0"),
}
_TEN_BIT_PIX = {"hevc_nvenc": "p010le", "av1_nvenc": "p010le", "hevc_videotoolbox": "p010le"}
#: encoders whose 10-bit output needs ``-profile:v main10``
_MAIN10 = frozenset({"hevc_nvenc", "hevc_videotoolbox"})
#: encoders with one fixed pixel format of their own
_FIXED_PIX = {"prores_videotoolbox": "p210le", "prores_ks": "yuv422p10le"}
#: ProRes 4444 on the CPU (profile 4), for 4:4:4 and deeper than 10-bit sources:
#: its 12-bit coding keeps more of them than 422 HQ
_PRORES_4444 = ("yuv444p10le", "4")

#: ffprobe ``color_space`` → the matrix name of ffmpeg's scale filter
_SCALE_MATRIX: Mapping[str, str] = {
    "bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470", "smpte240m": "smpte240m",
    "bt2020nc": "bt2020", "fcc": "fcc",
}  # fmt: skip


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int
    fps: float | None


def parse_progress(chunk: str) -> Progress | None:
    """The last ``Frame: N/T (X fps)`` in a stretch of vspipe's stderr (lines end in
    ``\\r``)."""
    last = None
    for m in _FRAME_RE.finditer(chunk):
        last = m
    if last is None:
        return None
    fps = float(last.group(3)) if last.group(3) else None
    return Progress(int(last.group(1)), int(last.group(2)), fps)


def vspipe_argv(
    vspipe: str, script: Path, user_data: Mapping[str, Any], *, requests: int | None = None
) -> list[str]:
    """``vspipe -c y4m -p -a user_data=… [-r N] script -``; ``requests`` bounds the
    frames in flight (vspipe's default is one per CPU thread)."""
    data = json.dumps(user_data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    argv = [vspipe, "-c", "y4m", "-p", "-a", f"user_data={data}"]
    if requests:
        argv += ["-r", str(int(requests))]
    return [*argv, os.fspath(script), "-"]


def resolved_depth(spec: RenderJobSpec, default: ColourDepth = "source") -> ColourDepth:
    """The spec's depth choice: ``depth``, else "10" for the older ``ten_bit``
    option, else ``default`` (the settings' ``RenderDefaults.depth``)."""
    if spec.depth is not None:
        return spec.depth
    return "10" if spec.ten_bit else default


def output_bits(
    src: SourceInfo,
    encoder: str,
    depth: ColourDepth = "source",
    depths: Sequence[int] | None = None,
) -> int:
    """The depth the copy is written with (``probe.pick_bits``): the source's or
    the chosen one, at least 10 for HDR10, as the encoder can (``depths``, from
    its ``ffmpeg -h encoder=`` answer). 10 bits on an 8-bit source means less
    banding in gradients after encoding, at about the same speed."""
    return pick_bits(src.bits, encoder, depth, depths, hdr10=renders_as_hdr10(src))


def ten_bit_output(src: SourceInfo, encoder: str, *, ten_bit: bool = False) -> bool:
    """Whether the copy is written with 10 or more bits by one of the encoders
    the older 10-bit option was offered for (``TEN_BIT_ENCODERS``)."""
    depth: ColourDepth = "10" if ten_bit else "source"
    return encoder in TEN_BIT_ENCODERS and output_bits(src, encoder, depth) > 8


def prores_4444(src: SourceInfo, encoder: str) -> bool:
    """Whether the copy is ProRes 4444 (``_PRORES_4444``), not 422 HQ."""
    return encoder == "prores_ks" and (src.bits > 10 or src.chroma == "444")


def pix_fmt(src: SourceInfo, encoder: str, bits: int) -> str:
    """The ``-pix_fmt`` for a copy written with ``bits`` (``output_bits``)."""
    if prores_4444(src, encoder):
        return _PRORES_4444[0]
    if encoder in _FIXED_PIX:
        return _FIXED_PIX[encoder]
    if bits >= 12:
        return "yuv420p12le"
    if bits >= 10:
        return _TEN_BIT_PIX.get(encoder, "yuv420p10le")
    return "yuv420p"


def depth_text(bits: int, pix: str) -> str:
    """For the log: "10-bit 4:2:0"."""
    layout = pixfmt.chroma_text(pixfmt.chroma(pix))
    return f"{bits}-bit {layout}".strip()


def filter_value(text: str) -> str:
    """``text`` as one option value inside an ``-vf`` graph: escaped for the
    option parser (``' \\ :``) and then for the graph parser (``\\ ' [ ] , ;``),
    the two levels ffmpeg-filters(1) describes. A path with a space or a quote
    (``~/Library/Application Support``) stays one value."""
    level1 = re.sub(r"([\\':])", r"\\\1", text)
    return re.sub(r"([\\'\[\],;])", r"\\\1", level1)


def lut_filter(src: SourceInfo, lut: Path, pix_fmt: str) -> str:
    """``-vf`` that applies a 3D LUT (.cube) to the smoothed frames: YUV to float
    RGB with the source's own matrix and range (the y4m pipe carries neither, and
    the scaler would guess BT.601), the LUT with tetrahedral interpolation, then
    back with the same matrix to ``pix_fmt``. Float in between keeps 10 bits and
    loses nothing on the way: through an identity LUT a BT.709 gradient came back
    bit-exact (16-bit RGB: 66-88 dB), colour bars 84 dB (16-bit: 69 dB)."""
    c = src.color
    matrix = _SCALE_MATRIX.get(c.get("colorspace", ""))
    if matrix is None:  # the usual guess: HD and up is BT.709
        matrix = "bt709" if src.height >= 720 or src.width >= 1280 else "smpte170m"
    rng = "pc" if c.get("color_range") == "pc" else "tv"
    return ",".join((
        f"scale=in_color_matrix={matrix}:in_range={rng}",
        "format=gbrpf32le",
        f"lut3d=file={filter_value(os.fspath(lut))}:interp=tetrahedral",
        f"scale=out_color_matrix={matrix}:out_range={rng}",
        f"format={pix_fmt}",
    ))  # fmt: skip


def encode_argv(
    ffmpeg: str,
    encoder: str,
    src: SourceInfo,
    out: Path,
    *,
    threads: int | None = None,
    ten_bit: bool = False,
    lut: Path | None = None,
    bits: int | None = None,
) -> list[str]:
    """``ffmpeg`` reading vspipe's y4m and writing the video-only track.
    ``bits``: the depth to write (``output_bits``); without it, ``ten_bit``
    asks for 10 bits on an 8-bit source (the older option). ``lut``: a .cube
    file applied before encoding (SDR only: ``jobs`` never passes one for an
    HDR10 copy, whose PQ values a display-referred LUT would misread)."""
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    argv += ["-f", "yuv4mpegpipe", "-i", "-"]
    if bits is None:
        bits = output_bits(src, encoder, "10" if ten_bit else "source")
    pix = pix_fmt(src, encoder, bits)
    if lut is not None:
        argv += ["-vf", lut_filter(src, lut, pix)]
    for key in ("color_primaries", "color_trc", "colorspace", "color_range"):
        value = src.color.get(key)
        if value:
            argv += [f"-{key}", value]
    # the y4m header says C420 for 8 bits, which ffmpeg reads as center
    argv += ["-chroma_sample_location", chroma_sample_location(src)]
    argv += ["-c:v", encoder, *_ENCODER_ARGS.get(encoder, ())]
    hdr10 = renders_as_hdr10(src)
    if hdr10 and encoder == "libx265":
        argv += ["-x265-params", x265_hdr_params(src.hdr)]
    elif hdr10 and encoder == "libsvtav1":
        params = svtav1_hdr_params(src.hdr)
        if params:
            argv += ["-svtav1-params", params]
    if prores_4444(src, encoder):  # in place of profile 3 (422 HQ)
        i = argv.index("-profile:v", argv.index(encoder))
        argv[i + 1] = _PRORES_4444[1]
    argv += ["-pix_fmt", pix]
    if encoder in _MAIN10 and bits == 10:
        argv += ["-profile:v", "main10"]
    if threads:
        argv += ["-threads", str(threads)]
    argv += ["-an", "-sn", "-dn", os.fspath(out)]
    return argv


def encoder_test_argv(ffmpeg: str, encoder: str, *, bits: int = 8) -> list[str]:
    """A quarter second of a small test pattern through ``encoder`` with the
    options a render passes, to nowhere: does it encode on this machine, and
    with ``bits`` (a GPU without 10-bit encoding fails at 10)?"""
    pix = _FIXED_PIX.get(encoder, "yuv420p")
    if bits == 10 and encoder not in _FIXED_PIX:
        pix = _TEN_BIT_PIX.get(encoder, "yuv420p10le")
    profile = ["-profile:v", "main10"] if encoder in _MAIN10 and bits == 10 else []
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=s=256x144:r=24:d=0.25",
        "-c:v", encoder, *_ENCODER_ARGS.get(encoder, ()),
        "-pix_fmt", pix, *profile, "-f", "null", "-",
    ]  # fmt: skip


def mkvmerge_argv(
    mkvmerge: str,
    video: Path,
    source: Path,
    out: Path,
    *,
    start_s: float,
    src: SourceInfo | None = None,
) -> list[str]:
    """``mkvmerge -o out [--sync 0:ms] [colour] video -D source`` (§7.2); an HDR10
    copy also gets container-level colour and mastering metadata (§7.4)."""
    argv = [mkvmerge, "--quiet", "-o", os.fspath(out)]
    if start_s:
        argv += ["--sync", f"0:{round(start_s * 1000)}"]
    if src is not None and renders_as_hdr10(src):
        argv += hdr10_colour_args(src)
    argv += [os.fspath(video), "-D", os.fspath(source)]
    return argv


#: ffprobe colour names → ISO/IEC 23091-4 code points (what Matroska stores)
_PRIMARIES = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "bt2020": 9, "smpte432": 12}
_TRANSFER = {"bt709": 1, "smpte170m": 6, "smpte2084": 16, "arib-std-b67": 18}
_MATRIX = {"bt709": 1, "bt470bg": 5, "smpte170m": 6, "bt2020nc": 9, "bt2020c": 10}


def _num(v: Fraction) -> str:
    return f"{float(v):.5f}".rstrip("0").rstrip(".")


def hdr10_colour_args(src: SourceInfo) -> list[str]:
    """mkvmerge options for track 0 of the encoded video: PQ, BT.2020 (or what the
    source says), limited range, mastering display and content light level."""
    c = src.color
    argv = [
        "--colour-matrix-coefficients", f"0:{_MATRIX.get(c.get('colorspace', ''), 9)}",
        "--colour-transfer-characteristics", "0:16",
        "--colour-primaries", f"0:{_PRIMARIES.get(c.get('color_primaries', ''), 9)}",
        "--colour-range", "0:2" if c.get("color_range") == "pc" else "0:1",
    ]  # fmt: skip
    if src.hdr.max_cll or src.hdr.max_fall:
        argv += ["--max-content-light", f"0:{src.hdr.max_cll}"]
        argv += ["--max-frame-light", f"0:{src.hdr.max_fall}"]
    m = src.hdr.mastering
    if m is not None:
        xy = ",".join(_num(v) for p in (m.red, m.green, m.blue) for v in p)
        argv += ["--chromaticity-coordinates", f"0:{xy}"]
        argv += ["--white-colour-coordinates", f"0:{_num(m.white[0])},{_num(m.white[1])}"]
        argv += ["--max-luminance", f"0:{_num(m.max_luminance)}"]
        argv += ["--min-luminance", f"0:{_num(m.min_luminance)}"]
    return argv


#: subtitle codecs Matroska can't hold, and what ffmpeg converts them to
_MKV_SUBTITLE = {"mov_text": "srt"}


def ffmpeg_remux_argv(
    ffmpeg: str,
    video: Path,
    source: Path,
    out: Path,
    *,
    start_s: float,
    src: SourceInfo | None = None,
) -> list[str]:
    """The fallback without mkvtoolnix (§7.2): one ffmpeg copy pass. MP4 text
    subtitles (``mov_text``) have no Matroska form, so each of them, and only
    them, becomes SubRip (copying it fails the whole pass; a global ``-c:s srt``
    would break PGS and ASS). Output subtitle N is the source's N-th subtitle,
    the order ``-map 1:s?`` keeps."""
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    if start_s:
        argv += ["-itsoffset", f"{start_s:.3f}"]
    argv += ["-i", os.fspath(video), "-i", os.fspath(source)]
    argv += ["-map", "0:v", "-map", "1:a?", "-map", "1:s?", "-map", "1:t?"]
    argv += ["-map_chapters", "1", "-map_metadata", "1", "-c", "copy"]
    subs = [s for s in src.streams if s.kind == "subtitle"] if src is not None else []
    for n, s in enumerate(subs):
        if s.codec in _MKV_SUBTITLE:
            argv += [f"-c:s:{n}", _MKV_SUBTITLE[s.codec]]
    argv += ["-f", "matroska", os.fspath(out)]
    return argv


def low_priority(
    argv: Sequence[str],
    *,
    nice: str | None,
    ionice: str | None,
    level: int,
    taskpolicy: str | None = None,
) -> list[str]:
    """``argv`` behind ``nice -n level`` and ``ionice -c2 -n7`` (best effort, not
    idle: idle can starve under other disk IO), each only when installed (§7.6).
    macOS has no ionice; ``taskpolicy -b`` (``PRIO_DARWIN_BG``) is its disk
    throttle, used for the remux, a copy pass where the efficiency cores it
    keeps the process on are no loss."""
    out = list(argv)
    if ionice:
        out = [ionice, "-c2", "-n7", *out]
    if taskpolicy:
        out = [taskpolicy, "-b", *out]
    if nice:
        out = [nice, "-n", str(int(level)), *out]
    return out


def target_frames(src: SourceInfo, target: Fraction) -> int:
    """How many frames the render writes (for progress before vspipe says)."""
    return max(1, round(src.duration_s * float(target)))


def temp_paths(output: Path) -> tuple[Path, Path]:
    """(video-only track, unfinished output), both hidden next to the output so they
    are on the same filesystem and the final step is a rename."""
    stem = output.name
    return (
        output.with_name(f".{stem}.buttereye-video.mkv"),
        output.with_name(f".{stem}.buttereye-part"),
    )


def tail(lines: Sequence[str], n: int = 12) -> str:
    return "\n".join(x for x in lines[-n:] if x.strip())


__all__ = [
    "Progress",
    "parse_progress",
    "vspipe_argv",
    "encode_argv",
    "resolved_depth",
    "output_bits",
    "pix_fmt",
    "depth_text",
    "ten_bit_output",
    "encoder_test_argv",
    "lut_filter",
    "filter_value",
    "mkvmerge_argv",
    "hdr10_colour_args",
    "ffmpeg_remux_argv",
    "low_priority",
    "target_frames",
    "temp_paths",
    "tail",
]
