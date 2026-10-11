# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A small on-screen line in mpv saying what smoothing does (GUI.md §12.1.5).

Shown through mpv's ``osd-overlay`` (its own layer: mpv's messages don't replace
it, it stays small, and it can stay up), one fixed overlay id, ASS text that
ButterEye builds from the session's state only (never a file name or anything
else a user or file controls). Pure functions; ``session`` sends and times it.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from buttereye.core.types import BackendId, BypassReason, FilterState, Health, SessionSnapshot

#: the one overlay id ButterEye uses (ipc.check_command allows only this one)
OVERLAY_ID = 4171
#: virtual height the overlay is laid out in; with FONT_SIZE about 2.5 % of it
RES_Y = 720
FONT_SIZE = 16
#: Carino Sharp Gold (#eab308) in ASS's &HBBGGRR& order, for the name
GOLD = "&H08B3EA&"

_BYPASS = {
    BypassReason.ALREADY_AT_RATE: "already at the target rate",
    BypassReason.INTERLACED: "interlaced video",
    BypassReason.HDR_SKIP: "HDR (Smooth HDR is off)",
    BypassReason.UNSUPPORTED_FORMAT: "unsupported picture format",
    BypassReason.NO_VIDEO: "no video",
    BypassReason.NO_REALTIME: "this computer can't keep up",
}


def _rate(r: Fraction) -> str:
    return f"{float(r):.3f}".rstrip("0").rstrip(".")


def _model(model: str | None) -> str:
    if not model:
        return ""
    name = model.removeprefix("rife-").removesuffix("_ensembleFalse").removesuffix("_ensembleTrue")
    return " " + name.replace("_lite", " lite").replace("_", " ")


def parts(snap: SessionSnapshot, items: Sequence[str]) -> list[str]:
    """The pieces of the line for ``snap`` (empty: nothing worth showing)."""
    if snap.filter is FilterState.BYPASSED:
        why = _BYPASS.get(snap.bypass, "") if snap.bypass is not None else ""
        return [f"not smoothed: {why}" if why else "not smoothed"]
    if snap.filter is not FilterState.ACTIVE or snap.backend is None:
        return []
    out: list[str] = []
    src = snap.source
    if "rates" in items and src is not None and snap.target_fps is not None:
        out.append(f"{_rate(src.fps)} → {_rate(snap.target_fps)} fps")
    if "engine" in items:
        if snap.backend is BackendId.RIFE_TRT:
            out.append(f"GPU · RIFE{_model(snap.model)} TensorRT")
        elif snap.backend is BackendId.RIFE_NCNN:
            out.append(f"GPU · RIFE{_model(snap.model)} Vulkan")
        elif snap.backend is BackendId.MVGPU:
            out.append("GPU · motion vectors")
        else:
            out.append("CPU · MVTools" + (" block mode" if snap.mv_block else ""))
    if "size" in items:
        if snap.smooth_size is not None:
            w, h = snap.smooth_size
            size = f"at {w}×{h}"
        elif src is not None:
            size = f"full {src.width}×{src.height}"
        else:
            size = ""
        if snap.flow_scale != 1.0:
            size += " · half-res motion"
        if size:
            out.append(size.strip(" ·"))
    if "health" in items:
        out.append("dropping frames" if snap.health is Health.DROPPING else "frames OK")
    return out


def _escape(text: str) -> str:
    """ASS-safe: no override blocks or line breaks from the text itself."""
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def ass(pieces: Sequence[str]) -> str:
    """One small top-left line: the gold name, then the pieces."""
    body = " · ".join(_escape(p) for p in pieces)
    return (
        f"{{\\an7\\fs{FONT_SIZE}\\bord1.2\\shad0\\3c&H000000&}}"
        f"{{\\1c{GOLD}\\b1}}ButterEye{{\\b0\\1c&HFFFFFF&}}  {body}"
    )


def signature(snap: SessionSnapshot, items: Sequence[str]) -> tuple[object, ...]:
    """What the line depends on: it is shown again only when this changes."""
    return tuple(parts(snap, items))


__all__ = ["FONT_SIZE", "OVERLAY_ID", "RES_Y", "ass", "parts", "signature"]
