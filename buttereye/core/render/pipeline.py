# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Command lines for one render (SCOPE §7.1, §7.2, §7.5). Pure functions.

``vspipe -c y4m -p`` (the live script with a file source) | ``ffmpeg`` encode to a
video-only MKV, then ``mkvmerge`` (or ``ffmpeg``) copies every other stream of the
source next to it. The y4m header carries no colour, so colour flags are always
passed from ffprobe's answer.
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

from buttereye.core.render.probe import (
    TEN_BIT_ENCODERS,
    SourceInfo,
    renders_as_hdr10,
    svtav1_hdr_params,
    x265_hdr_params,
)

_FRAME_RE = re.compile(r"Frame:\s*(\d+)/(\d+)(?:\s*\(([\d.]+)\s*fps\))?")

# quality settings per encoder, SDR (§7.5); 10-bit sources stay 10-bit when the
# encoder can do it. Software presets are one step lighter than the encoders'
# defaults: at the doubled frame rate x265 medium kept ~9 cores and SVT-AV1 6
# ~8 cores busy; fast / 8 are ~1.5-2x lighter for files a few % larger at the
# same CRF.
_ENCODER_ARGS: Mapping[str, tuple[str, ...]] = {
    "hevc_nvenc": ("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "20", "-b:v", "0"),
    "av1_nvenc": ("-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", "28", "-b:v", "0"),
    "libx265": ("-preset", "fast", "-crf", "20"),
    "libsvtav1": ("-preset", "8", "-crf", "28"),
    "libx264": ("-preset", "medium", "-crf", "18"),
}
_TEN_BIT_PIX = {"hevc_nvenc": "p010le", "av1_nvenc": "p010le"}


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


def encode_argv(
    ffmpeg: str, encoder: str, src: SourceInfo, out: Path, *, threads: int | None = None
) -> list[str]:
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    argv += ["-f", "yuv4mpegpipe", "-i", "-"]
    for key in ("color_primaries", "color_trc", "colorspace", "color_range"):
        value = src.color.get(key)
        if value:
            argv += [f"-{key}", value]
    argv += ["-c:v", encoder, *_ENCODER_ARGS.get(encoder, ())]
    hdr10 = renders_as_hdr10(src)
    if hdr10 and encoder == "libx265":
        argv += ["-x265-params", x265_hdr_params(src.hdr)]
    elif hdr10 and encoder == "libsvtav1":
        params = svtav1_hdr_params(src.hdr)
        if params:
            argv += ["-svtav1-params", params]
    if (src.bits > 8 or hdr10) and encoder in TEN_BIT_ENCODERS:
        argv += ["-pix_fmt", _TEN_BIT_PIX.get(encoder, "yuv420p10le")]
        if encoder == "hevc_nvenc":
            argv += ["-profile:v", "main10"]
    else:
        argv += ["-pix_fmt", "yuv420p"]
    if threads:
        argv += ["-threads", str(threads)]
    argv += ["-an", "-sn", "-dn", os.fspath(out)]
    return argv


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


def ffmpeg_remux_argv(
    ffmpeg: str, video: Path, source: Path, out: Path, *, start_s: float
) -> list[str]:
    """The fallback without mkvtoolnix (§7.2): one ffmpeg copy pass."""
    argv = [ffmpeg, "-hide_banner", "-nostats", "-loglevel", "error", "-y"]
    if start_s:
        argv += ["-itsoffset", f"{start_s:.3f}"]
    argv += ["-i", os.fspath(video), "-i", os.fspath(source)]
    argv += ["-map", "0:v", "-map", "1:a?", "-map", "1:s?", "-map", "1:t?"]
    argv += ["-map_chapters", "1", "-map_metadata", "1", "-c", "copy", "-f", "matroska"]
    argv += [os.fspath(out)]
    return argv


def low_priority(
    argv: Sequence[str], *, nice: str | None, ionice: str | None, level: int
) -> list[str]:
    """``argv`` behind ``nice -n level`` and ``ionice -c2 -n7`` (best effort, not
    idle: idle can starve under other disk IO), each only when installed (§7.6)."""
    out = list(argv)
    if ionice:
        out = [ionice, "-c2", "-n7", *out]
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
    "mkvmerge_argv",
    "hdr10_colour_args",
    "ffmpeg_remux_argv",
    "low_priority",
    "target_frames",
    "temp_paths",
    "tail",
]
