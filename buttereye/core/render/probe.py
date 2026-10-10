# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What a source file is and what can be made from it (SCOPE §7.1-7.5, §7.8).

Pure functions over ``ffprobe -of json`` output and benchmark results; the
subprocess calls live in ``jobs``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal

from buttereye.core.mpvctl import decide
from buttereye.core.types import BackendId, HdrClass, RenderSize, StreamInfo

#: Offline RIFE/MVTools run at about this share of the in-mpv benchmark rate
#: (spike M0(e): 45 fps at 1080p and 36 fps for 4K → 1080p against 61 in mpv;
#: decoding through ffms2, scene marks and the encoder share the machine).
OFFLINE_FACTOR = 0.8

#: Encoders ButterEye knows how to drive, in default order per case (§7.5).
KNOWN_ENCODERS: tuple[str, ...] = (
    "hevc_nvenc",
    "av1_nvenc",
    "libx265",
    "libsvtav1",
    "libx264",
)
NVIDIA_ENCODERS = frozenset({"hevc_nvenc", "av1_nvenc"})
TEN_BIT_ENCODERS = frozenset({"hevc_nvenc", "av1_nvenc", "libx265", "libsvtav1"})
#: The only encoders that take HDR10 mastering data on the command line (§7.4);
#: NVENC/VAAPI read it from frame side data, which the y4m pipe doesn't carry.
HDR10_ENCODERS: tuple[str, ...] = ("libx265", "libsvtav1")
#: Dolby Vision base-layer compatibility ids that are an HDR10 stream (§7.4)
DV_HDR10_BASE = frozenset({1, 6})

# rough bits per pixel per frame at the default quality, for the free-space check
_BPP: Mapping[str, float] = {
    "hevc_nvenc": 0.06,
    "av1_nvenc": 0.045,
    "libx265": 0.05,
    "libsvtav1": 0.04,
    "libx264": 0.09,
}

_STREAM_KINDS: Mapping[str, Literal["video", "audio", "subtitle", "attachment", "data"]] = {
    "video": "video",
    "audio": "audio",
    "subtitle": "subtitle",
    "attachment": "attachment",
    "data": "data",
}


@dataclass(frozen=True, slots=True)
class Mastering:
    """SMPTE ST 2086 mastering display: CIE xy of red, green, blue and the white
    point, and the luminance range in cd/m²."""

    red: tuple[Fraction, Fraction]
    green: tuple[Fraction, Fraction]
    blue: tuple[Fraction, Fraction]
    white: tuple[Fraction, Fraction]
    max_luminance: Fraction
    min_luminance: Fraction


@dataclass(frozen=True, slots=True)
class HdrMeta:
    """HDR10 static metadata from the first frame (§7.4); ``max_cll``/``max_fall``
    0 means unknown, as in the source."""

    mastering: Mastering | None = None
    max_cll: int = 0
    max_fall: int = 0
    dv_profile: int | None = None
    dv_base: int | None = None  # dv_bl_signal_compatibility_id


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """The facts a render needs about its source (from ffprobe)."""

    width: int
    height: int
    fps: Fraction  # constant output rate (avg for VFR sources)
    vfr: bool
    duration_s: float
    codec: str
    bits: int  # 8 or 10+
    hdr_class: HdrClass
    color: Mapping[str, str]  # ffmpeg -color_* values that ffprobe reported
    start_s: float  # video start time relative to the file (0 for most files)
    streams: tuple[StreamInfo, ...]
    chapters: int
    hdr: HdrMeta = HdrMeta()


def _rate(text: object) -> Fraction | None:
    if not isinstance(text, str) or "/" not in text:
        return None
    num, _, den = text.partition("/")
    try:
        n, d = int(num), int(den)
    except ValueError:
        return None
    if n <= 0 or d <= 0:
        return None
    return Fraction(n, d)


def _float(v: object, default: float = 0.0) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return default
    return f if math.isfinite(f) else default


def _side(*holders: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    out: list[Mapping[str, Any]] = []
    for h in holders:
        for sd in (h or {}).get("side_data_list") or ():
            if isinstance(sd, Mapping):
                out.append(sd)
    return out


def hdr_class(stream: Mapping[str, Any], frame: Mapping[str, Any] | None = None) -> HdrClass:
    """SDR / HDR10 / HLG / HDR10+ / DV from a video stream and its first frame's
    side data (§7.4: HDR10+ is per-frame metadata)."""
    types = {str(s.get("side_data_type", "")) for s in _side(stream, frame)}
    if any("DOVI" in t or "Dolby Vision" in t for t in types):
        return HdrClass.DV
    trc = str(stream.get("color_transfer") or "")
    if trc == "arib-std-b67":
        return HdrClass.HLG
    if trc == "smpte2084":
        if any("HDR10+" in t or "Dynamic Metadata SMPTE2094-40" in t for t in types):
            return HdrClass.HDR10PLUS
        return HdrClass.HDR10
    return HdrClass.SDR


def _frac(v: object) -> Fraction | None:
    if isinstance(v, int) and not isinstance(v, bool):
        return Fraction(v)
    if not isinstance(v, str) or not v:
        return None
    num, _, den = v.partition("/")
    try:
        f = Fraction(int(num), int(den or 1))
    except ValueError, ZeroDivisionError:
        return None
    return f if f >= 0 else None


def _int(v: object) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return max(0, v)
    return int(v) if isinstance(v, str) and v.isdigit() else 0


def hdr_meta(stream: Mapping[str, Any], frame: Mapping[str, Any] | None = None) -> HdrMeta:
    """Mastering display, content light level and Dolby Vision configuration from
    the stream's and the first frame's side data (the frame wins)."""
    mastering: Mastering | None = None
    cll = fall = 0
    dv_profile = dv_base = None
    for sd in _side(stream, frame):  # frame entries come last and win
        kind = str(sd.get("side_data_type", ""))
        if kind == "Mastering display metadata":
            keys = ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y",
                    "white_point_x", "white_point_y", "max_luminance", "min_luminance")  # fmt: skip
            vals = [_frac(sd.get(k)) for k in keys]
            if all(v is not None for v in vals):
                v = [x for x in vals if x is not None]
                mastering = Mastering((v[0], v[1]), (v[2], v[3]), (v[4], v[5]),
                                      (v[6], v[7]), v[8], v[9])  # fmt: skip
        elif kind == "Content light level metadata":
            cll, fall = _int(sd.get("max_content")), _int(sd.get("max_average"))
        elif "DOVI" in kind or "Dolby Vision" in kind:
            if sd.get("dv_profile") is not None:
                dv_profile = _int(sd.get("dv_profile"))
            if sd.get("dv_bl_signal_compatibility_id") is not None:
                dv_base = _int(sd.get("dv_bl_signal_compatibility_id"))
    return HdrMeta(mastering, cll, fall, dv_profile, dv_base)


def _bits(stream: Mapping[str, Any]) -> int:
    raw = stream.get("bits_per_raw_sample")
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    pix = str(stream.get("pix_fmt") or "")
    return 10 if ("10" in pix or "12" in pix or "16" in pix) else 8


def source_info(
    data: Mapping[str, Any], frame: Mapping[str, Any] | None = None
) -> SourceInfo | None:
    """``ffprobe -show_streams -show_format -show_chapters -of json`` (and the first
    video frame from ``-show_frames -read_intervals %+#1``) → facts; None when there
    is no video stream."""
    raw_streams = [s for s in data.get("streams") or () if isinstance(s, Mapping)]
    video = next(
        (
            s
            for s in raw_streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        return None
    r = _rate(video.get("r_frame_rate"))
    avg = _rate(video.get("avg_frame_rate"))
    fps = avg or r
    if fps is None:
        return None
    fmt = data.get("format") or {}
    duration = _float(video.get("duration")) or _float(fmt.get("duration"))
    start = _float(video.get("start_time")) - _float(fmt.get("start_time"))
    color = {
        key: str(video[name])
        for key, name in (
            ("color_primaries", "color_primaries"),
            ("color_trc", "color_transfer"),
            ("colorspace", "color_space"),
            ("color_range", "color_range"),
        )
        if video.get(name) and video.get(name) != "unknown"
    }
    streams = tuple(
        StreamInfo(
            index=int(s.get("index", i)),
            kind=_STREAM_KINDS.get(str(s.get("codec_type")), "data"),
            codec=str(s.get("codec_name") or "?"),
            title=(s.get("tags") or {}).get("title") or (s.get("tags") or {}).get("filename"),
        )
        for i, s in enumerate(raw_streams)
    )
    return SourceInfo(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps.limit_denominator(1001) if fps.denominator > 1001 else fps,
        vfr=r is not None and avg is not None and r != avg,
        duration_s=duration,
        codec=str(video.get("codec_name") or ""),
        bits=_bits(video),
        hdr_class=hdr_class(video, frame),
        color=color,
        start_s=max(0.0, start) if abs(start) > 0.0005 else 0.0,
        streams=streams,
        chapters=len(data.get("chapters") or ()),
        hdr=hdr_meta(video, frame),
    )


#: ffprobe ``color_space`` → VapourSynth matrix name
_VS_MATRIX: Mapping[str, str] = {
    "bt709": "709", "smpte170m": "170m", "bt470bg": "470bg", "smpte240m": "240m",
    "bt2020nc": "2020ncl", "bt2020c": "2020cl", "fcc": "fcc", "ycgco": "ycgco",
}  # fmt: skip


def vs_matrix(info: SourceInfo) -> str | None:
    """The VapourSynth matrix for the YUV↔RGB round trip, when ffprobe knows it."""
    return _VS_MATRIX.get(info.color.get("colorspace", ""))


def vs_range(info: SourceInfo) -> str | None:
    return {"tv": "limited", "pc": "full"}.get(info.color.get("color_range", ""))


def first_video_frame(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first video frame of ``ffprobe -show_frames`` output, if any."""
    for f in data.get("frames") or ():
        if isinstance(f, Mapping) and f.get("media_type") == "video":
            return f
    return None


def renders_as_hdr10(info: SourceInfo) -> bool:
    """HDR10, HDR10+, or Dolby Vision with an HDR10 base layer (not profile 5):
    the sources an HDR10 copy is made from (§7.4)."""
    if info.hdr_class in (HdrClass.HDR10, HdrClass.HDR10PLUS):
        return True
    return (
        info.hdr_class is HdrClass.DV
        and info.hdr.dv_profile != 5
        and info.hdr.dv_base in DV_HDR10_BASE
        and info.color.get("color_trc") == "smpte2084"
    )


def _xy(v: Fraction, unit: int) -> int:
    return round(v * unit)


def x265_hdr_params(meta: HdrMeta) -> str:
    """``-x265-params`` for HDR10: chromaticities in 0.00002, luminance in 0.0001
    cd/m² units, G B R WP order; ``max-cll=0,0`` keeps "unknown" (§7.4)."""
    parts = ["hdr10=1", "hdr10-opt=1", "repeat-headers=1"]
    m = meta.mastering
    if m is not None:
        parts.append(
            "master-display="
            f"G({_xy(m.green[0], 50000)},{_xy(m.green[1], 50000)})"
            f"B({_xy(m.blue[0], 50000)},{_xy(m.blue[1], 50000)})"
            f"R({_xy(m.red[0], 50000)},{_xy(m.red[1], 50000)})"
            f"WP({_xy(m.white[0], 50000)},{_xy(m.white[1], 50000)})"
            f"L({_xy(m.max_luminance, 10000)},{_xy(m.min_luminance, 10000)})"
        )
    parts.append(f"max-cll={meta.max_cll},{meta.max_fall}")
    return ":".join(parts)


def _dec(v: Fraction, places: int) -> str:
    return f"{float(v):.{places}f}"


def svtav1_hdr_params(meta: HdrMeta) -> str | None:
    """``-svtav1-params`` for HDR10 (decimal xy and cd/m²), or None without
    metadata to carry."""
    parts: list[str] = []
    m = meta.mastering
    if m is not None:
        parts.append(
            "mastering-display="
            f"G({_dec(m.green[0], 4)},{_dec(m.green[1], 4)})"
            f"B({_dec(m.blue[0], 4)},{_dec(m.blue[1], 4)})"
            f"R({_dec(m.red[0], 4)},{_dec(m.red[1], 4)})"
            f"WP({_dec(m.white[0], 4)},{_dec(m.white[1], 4)})"
            f"L({_dec(m.max_luminance, 4)},{_dec(m.min_luminance, 4)})"
        )
    if meta.max_cll or meta.max_fall:
        parts.append(f"content-light={meta.max_cll},{meta.max_fall}")
    return ":".join(parts) or None


def parse_decoders(text: str) -> frozenset[str]:
    """Decoder names from ``ffmpeg -hide_banner -decoders`` (ffms2 uses the same
    libavcodec, so a codec missing here can't be read, §7.7 #7)."""
    names: set[str] = set()
    started = False
    for line in text.splitlines():
        if line.strip().startswith("------"):
            started = True
            continue
        parts = line.split()
        if started and len(parts) >= 2:
            names.add(parts[1])
    return frozenset(names)


def usable_encoders(listed: Sequence[str], *, nvidia: bool, hdr10: bool = False) -> tuple[str, ...]:
    """The encoders this ffmpeg has that ButterEye drives, default first (§7.5:
    NVENC on NVIDIA, else SVT-AV1 in software; for HDR10 only x265, then SVT-AV1)."""
    if hdr10:
        return tuple(e for e in HDR10_ENCODERS if e in listed)
    have = [e for e in KNOWN_ENCODERS if e in listed]
    if not nvidia:
        have = [e for e in have if e not in NVIDIA_ENCODERS]
        if "libsvtav1" in have:
            have.remove("libsvtav1")
            have.insert(0, "libsvtav1")
    return tuple(have)


def offline_fps(
    bench_fps: float | None,
    size: tuple[int, int],
    src: tuple[int, int],
    *,
    src_fps: Fraction | None = None,
    target: Fraction | None = None,
) -> float | None:
    """Estimated output frames per second at ``size`` for a ``src``-sized video,
    from an in-mpv benchmark rate already scaled to ``size`` (a 2× rate,
    ``decide.load_rate`` units). Without ``src_fps``/``target`` the estimate is
    for 2×; another multiplier is converted by its interpolated frames
    (23.976 → 60 infers almost every frame, so it runs at about half the 2× rate)."""
    if bench_fps is None or bench_fps <= 0:
        return None
    capped = decide.shrink_cap(bench_fps, *src) if size != src else bench_fps
    if capped is None:
        return None
    if src_fps is not None and target is not None:
        work = decide.load_rate(src_fps, target)
        if work > 0:
            capped *= float(target / work)
    return round(capped * OFFLINE_FACTOR, 1)


def render_sizes(
    width: int, height: int, rate_at: Mapping[tuple[int, int], float | None]
) -> tuple[RenderSize, ...]:
    """The source size first, then the smaller ones (§7.8), each with its estimate."""
    sizes = ((width, height), *decide.smaller_sizes(width, height))
    return tuple(
        RenderSize(w, h, offline_fps(rate_at.get((w, h)), (w, h), (width, height)))
        for w, h in sizes
    )


def estimate_bytes(
    encoder: str,
    size: tuple[int, int],
    target: Fraction,
    duration_s: float,
    source_bytes: int,
) -> int:
    """Upper-bound-ish size of the finished file plus the temporary video track
    that exists during the remux (§7.6 free-space check)."""
    bpp = _BPP.get(encoder, 0.08)
    video = size[0] * size[1] * float(target) * duration_s * bpp / 8
    return int(2 * video + source_bytes)


#: Automatic offline order: time is the cost offline, so the fastest GPU engine
#: first (TensorRT only when opted in and set up, §5.2), then RIFE-ncnn, then MVTools
OFFLINE_RANKING = (BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVTOOLS)


def engine_for(
    backend: BackendId | Literal["auto"], installed: frozenset[BackendId]
) -> BackendId | None:
    """§7.8: the profile's engine when installed; Automatic, or a pinned engine that
    isn't installed, takes the first installed of ``OFFLINE_RANKING`` (never a
    TensorRT the profile didn't ask for when it pinned another engine)."""
    if backend != "auto" and backend in installed:
        return backend
    for b in OFFLINE_RANKING:
        if b in installed and (backend == "auto" or b is not BackendId.RIFE_TRT):
            return b
    return None


__all__ = [
    "OFFLINE_FACTOR",
    "KNOWN_ENCODERS",
    "HDR10_ENCODERS",
    "Mastering",
    "HdrMeta",
    "SourceInfo",
    "hdr_class",
    "hdr_meta",
    "first_video_frame",
    "vs_matrix",
    "vs_range",
    "renders_as_hdr10",
    "x265_hdr_params",
    "svtav1_hdr_params",
    "source_info",
    "parse_decoders",
    "usable_encoders",
    "offline_fps",
    "render_sizes",
    "estimate_bytes",
    "engine_for",
]
