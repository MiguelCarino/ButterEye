# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv user shaders ButterEye can add to the player (SCOPE §15.2, spike M0(o)).

Only the bundled files in ``buttereye/data/shaders`` are ever added, by their
exact path: at launch with ``--glsl-shaders-append`` and while playing with
``change-list glsl-shaders append|remove``, so shaders from the user's own
mpv.conf stay in place.

Both hook the luma plane only while mpv upscales (FSRCNNX above 1.3x, RAVU above
1.414x, their ``//!WHEN`` lines), but neither is free when it runs: mpv applies
it to every frame it shows, and with smoothing on that is every interpolated
frame too (48 a second at 2x of 23.976). FSRCNNX runs a full 2x network per
frame and mpv then scales that back down to the window (a 1080p film fullscreen
on a 2560x1664 panel is a 1.33x upscale, so 3840x2160 is drawn and thrown away
to 2560x1440), on the GPU the GPU engines use, and the speed test (``--vo=null``)
never sees it. So "sharper" is turned off where it buys little
(``sharper_allowed``); RAVU-lite is a small fixed-kernel pass and is left on.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from buttereye.core.types import PowerSnapshot

SHADER_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "shaders"
#: upscaling setting -> bundled shader ("standard": mpv's own scaler only)
UPSCALERS: dict[str, Path] = {
    "sharper": SHADER_DIR / "FSRCNNX_x2_8-0-4-1.glsl",
    "ravu": SHADER_DIR / "ravu-lite-ar-r3.hook",
}
#: the only shader paths the IPC allowlist accepts
ALLOWED: frozenset[str] = frozenset(str(p) for p in UPSCALERS.values())
#: "sharper" only from this upscale ratio (window / smoothed picture) up: below
#: it most of FSRCNNX's 2x output is thrown away by mpv's downscale to the window
SHARPER_MIN_RATIO = 1.8


def shader_for(upscaling: Literal["standard", "sharper", "ravu"] | str) -> Path | None:
    """The shader for an upscaling setting, or None (also when the file is missing)."""
    path = UPSCALERS.get(upscaling)
    return path if path is not None and path.is_file() else None


def video_scale(osd: object, width: int, height: int) -> float | None:
    """How much mpv enlarges a ``width``×``height`` picture into the window:
    mpv's ``osd-dimensions`` (window size and the black-bar margins) against the
    picture's size, the smaller of the two axes. None when unknown."""
    if not isinstance(osd, Mapping) or width <= 0 or height <= 0:
        return None

    def num(key: str) -> float:
        v = osd.get(key)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0

    shown_w = num("w") - num("ml") - num("mr")
    shown_h = num("h") - num("mt") - num("mb")
    if shown_w <= 0 or shown_h <= 0:
        return None
    return min(shown_w / width, shown_h / height)


def sharper_allowed(
    ratio: float | None, *, reduced_size: bool, power: PowerSnapshot | None = None
) -> bool:
    """Whether "sharper" (FSRCNNX) is worth its GPU time now.

    Off while smoothing runs at a smaller size than the video (the picture is
    enlarged more, and the size was lowered because the GPU had no time to
    spare), on battery, in Low Power Mode or when macOS says the machine is hot
    (``PowerSnapshot.constrained``), and below ``SHARPER_MIN_RATIO``. An unknown
    ratio is left to the shader's own 1.3x condition."""
    if reduced_size:
        return False
    if power is not None and (power.on_battery is True or power.constrained):
        return False
    return ratio is None or ratio >= SHARPER_MIN_RATIO


__all__ = [
    "ALLOWED",
    "SHADER_DIR",
    "SHARPER_MIN_RATIO",
    "UPSCALERS",
    "shader_for",
    "sharper_allowed",
    "video_scale",
]
