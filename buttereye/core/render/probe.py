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


def hdr_class(stream: Mapping[str, Any]) -> HdrClass:
    """SDR / HDR10 / HLG / HDR10+ / DV from a video stream (and its side data)."""
    side = stream.get("side_data_list") or ()
    types = {str(s.get("side_data_type", "")) for s in side if isinstance(s, Mapping)}
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


def _bits(stream: Mapping[str, Any]) -> int:
    raw = stream.get("bits_per_raw_sample")
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    pix = str(stream.get("pix_fmt") or "")
    return 10 if ("10" in pix or "12" in pix or "16" in pix) else 8


def source_info(data: Mapping[str, Any]) -> SourceInfo | None:
    """``ffprobe -show_streams -show_format -show_chapters -of json`` → facts;
    None when there is no video stream."""
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
        hdr_class=hdr_class(video),
        color=color,
        start_s=max(0.0, start) if abs(start) > 0.0005 else 0.0,
        streams=streams,
        chapters=len(data.get("chapters") or ()),
    )


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


def usable_encoders(listed: Sequence[str], *, nvidia: bool) -> tuple[str, ...]:
    """The encoders this ffmpeg has that ButterEye drives, default first (§7.5:
    NVENC on NVIDIA, else SVT-AV1 in software)."""
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


def engine_for(
    backend: BackendId | Literal["auto"], installed: frozenset[BackendId]
) -> BackendId | None:
    """§7.8: the profile's engine; Automatic = RIFE-ncnn when installed, else MVTools."""
    if backend == "auto":
        for b in (BackendId.RIFE_NCNN, BackendId.MVTOOLS):
            if b in installed:
                return b
        return None
    return backend if backend in installed else None


__all__ = [
    "OFFLINE_FACTOR",
    "KNOWN_ENCODERS",
    "SourceInfo",
    "hdr_class",
    "source_info",
    "parse_decoders",
    "usable_encoders",
    "offline_fps",
    "render_sizes",
    "estimate_bytes",
    "engine_for",
]
