# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Bit depth and chroma layout from a pixel format name. Pure.

Reads ffmpeg's names (``yuv420p12le``, ``p010le``, ``nv12``) and mpv's, which
drop the byte order (``yuv420p10``, ``p010``). The name is what decides: a
stream's ``bits_per_raw_sample`` is missing for HEVC, AV1 and VP9, and is used
only when it is lower (``source_bits``).
"""

from __future__ import annotations

import re
from typing import Literal

Chroma = Literal["420", "422", "444", "other"]

#: 8-bit semi-planar and packed names without a depth in them
_EIGHT_BIT: dict[str, Chroma] = {
    "nv12": "420", "nv21": "420", "nv16": "422", "nv61": "422", "nv24": "444", "nv42": "444",
    "yuyv422": "422", "uyvy422": "422", "yvyu422": "422", "ayuv": "444", "vuya": "444",
    "vuyx": "444", "uyyvyy411": "other", "rgb24": "other", "bgr24": "other",
    "rgba": "other", "bgra": "other", "argb": "other", "abgr": "other", "rgb0": "other",
    "bgr0": "other", "0rgb": "other", "0bgr": "other", "gray": "other", "pal8": "other",
}  # fmt: skip
#: other packed names: (depth, chroma)
_PACKED: dict[str, tuple[int, Chroma]] = {
    "nv20": (10, "422"), "v210": (10, "422"), "y210": (10, "422"), "y212": (12, "422"),
    "y216": (16, "422"), "xv30": (10, "444"), "xv36": (12, "444"), "xv48": (16, "444"),
    "v30x": (10, "444"), "x2rgb10": (10, "other"), "x2bgr10": (10, "other"),
    "rgb48": (16, "other"), "bgr48": (16, "other"), "rgba64": (16, "other"),
    "bgra64": (16, "other"),
}  # fmt: skip
_ENDIAN = re.compile(r"(le|be)$")
#: p010, p210, p416: semi-planar 4:2:0 / 4:2:2 / 4:4:4 with the depth in the digits
_SEMI = re.compile(r"^p([024])(\d\d)(msb)?$")
#: yuv420p10, yuvj444p, yuva422p16, gbrp12, gbrap10, gray12, grayf32
_PLANAR = re.compile(r"^(yuva?|yuvj|gbra?p|gray|ya)(\d{3})?p?(f?)(\d+)?(msb)?$")
_CHROMA: dict[str, Chroma] = {"420": "420", "422": "422", "444": "444"}


def _name(pix_fmt: str) -> str:
    return _ENDIAN.sub("", pix_fmt.strip().lower())


def _parse(pix_fmt: str) -> tuple[int, Chroma] | None:
    name = _name(pix_fmt)
    if not name:
        return None
    if name in _EIGHT_BIT:
        return 8, _EIGHT_BIT[name]
    if name in _PACKED:
        return _PACKED[name]
    m = _SEMI.match(name)
    if m is not None:
        layout = {"0": "420", "2": "422", "4": "444"}[m.group(1)]
        return int(m.group(2)), _CHROMA[layout]
    m = _PLANAR.match(name)
    if m is not None:
        family, layout, flt, digits = m.group(1), m.group(2), m.group(3), m.group(4)
        bits = 8 if not digits else int(digits)
        if flt:  # float planes: more than any integer output keeps
            bits = 16
        if family.startswith("yuv"):
            return bits, _CHROMA.get(layout or "", "other")
        return bits, "other"
    return None


def depth(pix_fmt: str) -> int:
    """Bits per sample of ``pix_fmt``; 8 for a name it doesn't know."""
    parsed = _parse(pix_fmt)
    return parsed[0] if parsed is not None else 8


def chroma(pix_fmt: str) -> Chroma:
    """``"420"``, ``"422"``, ``"444"``, or ``"other"`` (RGB, grey, unknown)."""
    parsed = _parse(pix_fmt)
    return parsed[1] if parsed is not None else "other"


def known(pix_fmt: str) -> bool:
    return _parse(pix_fmt) is not None


def source_bits(pix_fmt: str, raw: object = None) -> int:
    """A stream's depth: the pixel format's, or ``bits_per_raw_sample`` when that
    is a number no larger (a 9-bit stream decoded to a 10-bit format), or alone
    when the format name is missing or unknown."""
    raw_bits = int(raw) if isinstance(raw, str) and raw.isdigit() else None
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw_bits = raw
    parsed = _parse(pix_fmt) if pix_fmt else None
    if parsed is None:
        return raw_bits if raw_bits and raw_bits > 0 else 8
    if raw_bits and 0 < raw_bits <= parsed[0]:
        return raw_bits
    return parsed[0]


def chroma_text(layout: str) -> str:
    """``"4:2:0"`` for ``"420"``; "" for ``"other"``."""
    return f"{layout[0]}:{layout[1]}:{layout[2]}" if layout in _CHROMA else ""


__all__ = ["Chroma", "chroma", "chroma_text", "depth", "known", "source_bits"]
