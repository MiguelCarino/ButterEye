# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv user shaders ButterEye can add to the player (SCOPE §15.2, spike M0(o)).

Only the bundled files in ``buttereye/data/shaders`` are ever added, by their
exact path: at launch with ``--glsl-shaders-append`` and while playing with
``change-list glsl-shaders append|remove``, so shaders from the user's own
mpv.conf stay in place. FSRCNNX hooks the luma plane only when mpv upscales
by more than 1.3x, so it costs nothing when the video fills the window.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

SHADER_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "shaders"
#: upscaling setting -> bundled shader ("standard": mpv's own scaler only)
UPSCALERS: dict[str, Path] = {"sharper": SHADER_DIR / "FSRCNNX_x2_8-0-4-1.glsl"}
#: the only shader paths the IPC allowlist accepts
ALLOWED: frozenset[str] = frozenset(str(p) for p in UPSCALERS.values())


def shader_for(upscaling: Literal["standard", "sharper"] | str) -> Path | None:
    """The shader for an upscaling setting, or None (also when the file is missing)."""
    path = UPSCALERS.get(upscaling)
    return path if path is not None and path.is_file() else None


__all__ = ["ALLOWED", "SHADER_DIR", "UPSCALERS", "shader_for"]
