# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Pure decisions for live playback: source facts, behaviour matrix (§4.11),
target rate and multiplier (§5.6), backend/model resolution (§5.3, M1 subset).

No I/O except the ``Path.is_dir``/``is_file`` checks in ``installed_backends``
and ``resolve_model``, which take the directories as arguments (one plugin
directory, or the search path ``paths.plugin_dirs`` answers).

RIFE-ncnn models are looked up by directory name in the packaged directory
first, then in the user's ``data_dir/models`` (the Models page lists both); a
user directory counts only if it is a real directory holding an ncnn model
(``flownet.param``) and no ONNX files (those are TensorRT models).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core import osapi
from buttereye.core.paths import plugin_in
from buttereye.core.types import (
    BackendId,
    BypassReason,
    HdrClass,
    Msg,
    Profile,
    SourceFacts,
    TargetKind,
    VulkanDevice,
)

#: Preferred multipliers are 2-5x (§5.6); above this, divide the display rate.
MAX_MULTIPLIER = Fraction(5)
#: "Match your display" tries the display rate itself up to this multiplier
#: (24 fps on a 180 Hz display is 7.5x)
DISPLAY_MULTIPLIER = Fraction(8)

#: Auto counts a target as "doubling" from 1.98x (24.0031 → 48 still doubles).
AUTO_DOUBLING_TOLERANCE = Fraction(99, 100)

#: How far a video rate may sit above the display rate and still be shown
#: frame for frame: mpv's ``video-sync=display-resample`` (ButterEye's profile)
#: changes the playback speed by up to ``video-sync-max-video-change``, 1 % by
#: default, so 60 fps on a 59.94 Hz display is resampled, not dropped.
DISPLAY_RESAMPLE_TOLERANCE = Fraction(101, 100)


def over_display(target: Fraction | None, display_hz: float | None) -> bool:
    """``target`` is more frames per second than a known display can show.

    mpv then drops the surplus at the video output - every one of them, by
    design - and counts each in ``frame-drop-count``, the counter the health
    detector reads. Measured on an Apple M2 at 60 Hz with mpv 0.41 and a filter
    that only changes the rate (``--vf=fps=100``): 40 of every 100 frames
    dropped, every second, with no interpolation at all; at ``fps=60``, none.
    So an output above the refresh rate is pure cost - the frames are computed
    and thrown away - and it reads as "Dropping frames" even when the engine
    keeps up. False when either rate is unknown."""
    display = display_rate(display_hz)
    if target is None or display is None:
        return False
    return Fraction(target) > display * DISPLAY_RESAMPLE_TOLERANCE


def cadence(target: Fraction | None, display_hz: float | None) -> Fraction | None:
    """Refreshes per output frame: ``display_rate / target`` (60 Hz at 47.952 fps
    is 1.2513). None when either rate is unknown."""
    display = display_rate(display_hz)
    if target is None or display is None or target <= 0:
        return None
    return display / Fraction(target)


def uneven_pacing(target: Fraction | None, display_hz: float | None) -> bool:
    """``target`` can't be shown for the same number of refreshes every frame.

    ButterEye's profile runs ``interpolation=no`` with ``video-sync=display-resample``,
    which changes the playback speed by up to 1 % (``DISPLAY_RESAMPLE_TOLERANCE``)
    to land on whole refreshes; anything further off is shown frame by frame on
    the nearest refresh. 2x of 23.976 on a 60 Hz panel (cadence 1.2513) then holds
    three frames for one refresh and the fourth for two: 1-1-1-2, a 12 Hz judder on
    motion that was smoothed to be even. ``mistimed-frame-count`` does not count it
    (each frame is on a refresh). 144 Hz (3.003) and 30 fps on 60 Hz (2) pace
    evenly. False when either rate is unknown."""
    r = cadence(target, display_hz)
    if r is None:
        return False
    n = max(1, round(r))
    return abs(r - n) > n * (DISPLAY_RESAMPLE_TOLERANCE - 1)


def uneven_pacing_notice(target: Fraction, display_hz: float) -> Msg:
    return Msg(
        "{target} fps doesn't divide evenly into your {hz} Hz screen, so some frames "
        "stay up longer than others and motion can judder slightly.",
        {"target": fmt_rate(target), "hz": fmt_rate(display_rate(display_hz) or Fraction(0))},
    )


def even_pacing_notice(height: int, display_hz: float) -> Msg:
    return Msg(
        "Smoothing at {height}p so motion paces evenly on your {hz} Hz screen.",
        {"height": height, "hz": fmt_rate(display_rate(display_hz) or Fraction(0))},
    )


#: Packaged RIFE ncnn models, best first (§5.3 step 4 conservative defaults).
DEFAULT_MODELS: tuple[str, ...] = (
    "rife-v4.26_ensembleFalse",
    "rife-v4.22_lite_ensembleFalse",
    "rife-v4.18_ensembleFalse",
)

#: mpv ``video-params/colormatrix`` → VapourSynth ``matrix_s``.
MPV_MATRIX: Mapping[str, str] = {
    "bt.601": "170m",
    "bt.709": "709",
    "bt.2020-ncl": "2020ncl",
    "bt.2020-cl": "2020cl",
    "smpte-240m": "240m",
    "fcc": "fcc",
    "ycgco": "ycgco",
}
_SUPPORTED_SEMIPLANAR = frozenset({"nv12", "nv21", "nv16", "nv24", "p010", "p016", "p210", "p410"})

BACKEND_NAMES: Mapping[BackendId, str] = {
    BackendId.RIFE_NCNN: "RIFE (Vulkan)",
    BackendId.MVTOOLS: "MVTools (CPU)",
    BackendId.RIFE_TRT: "RIFE · TensorRT",
    BackendId.MVGPU: "GPU motion vectors",
}

#: mvgpu's plugin file in the private plugin directory (``buttereye-vs-mvgpu``)
MVGPU_PLUGIN = osapi.plugin_file("libmvgpu.so")

#: The plugin file a generated script loads for each engine (``_load`` in
#: scriptgen/template.vpy). Only one of them is loaded per generation, which is
#: what lets a script name one directory: ``paths.plugin_dir_for`` resolves it to
#: the directory that holds this file. RIFE-TensorRT is absent on purpose - it
#: loads vstrt from the install's own directory and never reads ``PLUGIN_DIR``.
BACKEND_PLUGIN: Mapping[BackendId, str] = {
    BackendId.RIFE_NCNN: osapi.plugin_file("librife.so"),
    BackendId.MVTOOLS: osapi.plugin_file("mvtools.so"),
    BackendId.MVGPU: MVGPU_PLUGIN,
}


def fps_fraction(value: float | int | None) -> Fraction | None:
    """A float rate as a Fraction: integer rates exact, NTSC rates as n*1000/1001
    (23.976 and 23.976024 → 24000/1001), anything else to 1/1000."""
    if value is None or not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value <= 0:
        return None
    x = float(value)
    if abs(x - round(x)) < 1e-3:
        return Fraction(round(x))
    n = round(x * 1.001)
    if abs(x - n * 1000 / 1001) < 1.5e-3:
        return Fraction(n * 1000, 1001)
    return Fraction(x).limit_denominator(1000)


#: A display rate this close to a whole number is that number (``display_rate``)
DISPLAY_SNAP = Fraction(1, 2000)


def display_rate(value: float | int | None) -> Fraction | None:
    """mpv's ``display-fps`` as a target rate: ``fps_fraction``, and a rate within
    0.05 % of a whole number as that number.

    mpv reports the rate it measures or the mode's own, which is rarely exact:
    the same Apple M2 panel read 60.000131 in one run and 60.0024 in the next.
    ``fps_fraction`` keeps 60.0024 as 49982/833, and a 50 fps video then goes to
    a target that shares no frame with it, so every output frame is inferred:
    measured (MVTools, 50 fps at 720p, untimed) 1.66× real time against 1.92× for
    50 -> 60, and no source frame passes through untouched. display-resample
    absorbs the 0.004 % (up to 1 %, ``DISPLAY_RESAMPLE_TOLERANCE``). NTSC rates
    (59.94) are matched first, as ``fps_fraction`` does."""
    r = fps_fraction(value)
    if r is None or r.denominator in (1, 1001):
        return r
    n = round(r)
    if n > 0 and abs(r - n) <= n * DISPLAY_SNAP:
        return Fraction(n)
    return r


def matrix_name(colormatrix: object, height: int) -> str:
    """VapourSynth matrix for mpv's colormatrix; by height when unknown (never fixed 709)."""
    if isinstance(colormatrix, str) and colormatrix in MPV_MATRIX:
        return MPV_MATRIX[colormatrix]
    return "709" if height >= 720 else "170m"


def range_name(colorlevels: object) -> str | None:
    return colorlevels if colorlevels in ("limited", "full") else None


#: mpv ``video-params/chroma-location`` → zimg ``chromaloc_s``. mpv 0.41 names
#: them as listed by ``--vf=format=chroma-location=help``; libplacebo's own
#: spellings are read too. "unknown" (or anything else) leaves zimg's default.
MPV_CHROMA_LOC: Mapping[str, str] = {
    "mpeg2/4/h264": "left",
    "left": "left",
    "mpeg1/jpeg": "center",
    "center": "center",
    "uhd": "top_left",
    "top-left": "top_left",
    "top_left": "top_left",
    "top": "top",
    "bottom-left": "bottom_left",
    "bottom_left": "bottom_left",
    "bottom": "bottom",
}


def chroma_loc_name(chroma_location: object) -> str | None:
    """zimg chroma location for mpv's ``chroma-location``; None = unknown."""
    if isinstance(chroma_location, str):
        return MPV_CHROMA_LOC.get(chroma_location)
    return None


def hdr_class(video_params: Mapping[str, Any]) -> HdrClass:
    gamma = video_params.get("gamma")
    if gamma == "pq":
        return HdrClass.HDR10
    if gamma == "hlg":
        return HdrClass.HLG
    return HdrClass.SDR


def interlaced(video_params: Mapping[str, Any], frame_info: object) -> bool:
    if isinstance(frame_info, dict) and frame_info.get("interlaced") is True:
        return True
    return False


def params_ready(video_params: Mapping[str, Any]) -> bool:
    """mpv has published the full ``video-params`` (it can briefly show only w/h
    while a file opens; that is "not known yet", never "unsupported format")."""
    fmt = video_params.get("pixelformat") or video_params.get("hw-pixelformat")
    return isinstance(fmt, str) and bool(fmt)


def format_supported(video_params: Mapping[str, Any]) -> bool:
    """Planar or semi-planar YUV with even dimensions (§4.11 "unsupported input format")."""
    fmt = video_params.get("pixelformat") or video_params.get("hw-pixelformat")
    if not isinstance(fmt, str):
        return False
    if not (fmt.startswith("yuv") or fmt in _SUPPORTED_SEMIPLANAR):
        return False
    if fmt.startswith("yuva"):
        return False
    w, h = video_params.get("w"), video_params.get("h")
    if not isinstance(w, int) or not isinstance(h, int):
        return False
    return w > 0 and h > 0 and w % 2 == 0 and h % 2 == 0


def source_facts(
    props: Mapping[str, Any], *, path: str, fallback_fps: float | None = None
) -> SourceFacts | None:
    """``SourceFacts`` from observed properties; None until video-params and a rate exist."""
    vp = props.get("video-params")
    if not isinstance(vp, dict):
        return None
    fps = fps_fraction(props.get("container-fps")) or fps_fraction(fallback_fps)
    w, h = vp.get("w"), vp.get("h")
    if fps is None or not isinstance(w, int) or not isinstance(h, int):
        return None
    display = props.get("display-fps")
    return SourceFacts(
        fps=fps,
        width=w,
        height=h,
        hdr_class=hdr_class(vp),
        display_hz=float(display) if isinstance(display, (int, float)) else 0.0,
        interlaced=interlaced(vp, props.get("video-frame-info")),
        path=path,
        vfr=False,
    )


def no_video(props: Mapping[str, Any]) -> bool:
    """A still image or cover art as the video track (§4.11 "Audio-only or image files").

    A missing video track is judged by the session after a grace period, because
    ``current-tracks/video`` is briefly unset while a file opens."""
    track = props.get("current-tracks/video")
    if isinstance(track, dict):
        return bool(track.get("image") or track.get("albumart"))
    return False


@dataclass(frozen=True, slots=True)
class TargetChoice:
    target: Fraction | None
    bypass: BypassReason | None


def interp_rate(src: Fraction, target: Fraction) -> Fraction:
    """Interpolated (inferred) frames per second for ``src`` → ``target``.

    The interpolators make only the output frames that do not land on a source
    frame: with ``target / src = p / q`` in lowest terms, one output frame in
    ``p`` is a source frame. 2× → ``target / 2``; 24 → 60 → ``0.8 × 60``;
    23.976 → 60 (``1001/400``) → almost every output frame. Both rates are read
    to 1/1001 so a float-derived rate does not invent a huge ``p``."""
    s = Fraction(src).limit_denominator(1001)
    t = Fraction(target).limit_denominator(1001)
    if s <= 0 or t <= s:
        return Fraction(0)
    return t * (1 - Fraction(1, (t / s).numerator))


def load_rate(src: Fraction, target: Fraction) -> Fraction:
    """The interpolation work of ``src`` → ``target`` as the output rate that
    costs the same at exactly 2× (``2 × interp_rate``).

    Live caps (``sustainable_fps``) are in this unit: the benchmark measures 2×,
    and at 2× ``load_rate == target``, so 2× decisions compare the target itself.
    Interpolation cost follows the inferred frames, not the output frames: on the
    dev box (RTX 4090, v4.26) 1080p 2× ran 53.7 output fps untimed and 3.75×
    only 30.1, both about 27-28 inferences per second."""
    return 2 * interp_rate(src, target)


#: Hold fill (``FilterParams.hold_fill``) is costed for drawings held this many
#: frames, the longest hold it re-times (the session's ``HOLD_MAX``): every held
#: frame is then inferred too, 1.67x the work at 2x (1.5x on twos). A run longer
#: than ``hold_max`` is left alone, so no content costs more.
HOLD_FILL_GROUP = 3


def hold_fill_factor(src: Fraction, target: Fraction, group: int = HOLD_FILL_GROUP) -> float:
    """How much more interpolation work hold fill can add at ``src`` → ``target``.

    With multiplier ``m = target / src`` the engines infer ``1 - 1/m`` of the
    output frames; when every drawing is held ``group`` frames and each is
    re-timed, only one output frame in ``group × m`` is a real one:
    ``(1 - 1/(g·m)) / (1 - 1/m)``. 2× on twos is 1.5×, on threes 1.67×. The
    per-frame detection never fires on live action, so this is a worst case, not
    the usual cost."""
    if src <= 0 or target <= src or group < 2:
        return 1.0
    m = float(Fraction(target) / Fraction(src))
    return (1 - 1 / (group * m)) / (1 - 1 / m)


def hold_fill_fits(src: Fraction, target: Fraction, cap: float | None) -> bool:
    """Whether a live cap (``load_rate`` units) absorbs hold fill's worst case.

    No cap (no benchmark, or smoothing forced past it) answers False: there is
    nothing to say the extra work would keep up."""
    if cap is None or cap <= 0:
        return False
    return float(load_rate(src, target)) * hold_fill_factor(src, target) <= cap


def as_2x_rate(fps: float, src: object, target: object = None) -> float:
    """A rate measured at ``src`` → ``target`` (None = 2×) in ``load_rate`` units:
    the 2× output rate with the same interpolation work."""
    if not isinstance(src, Fraction) or src <= 0:
        return fps
    t = target if isinstance(target, Fraction) else src * 2
    work = load_rate(src, t)
    return fps * float(work / t) if work > 0 else fps


def choose_target(
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None = None,
    *,
    sustainable_fps: float | None = None,
) -> TargetChoice:
    """§5.6: the target rate for ``kind``, multiplier ≤ 5×.

    ``sustainable_fps`` is the engine's live cap as a 2× output rate (see
    ``load_rate``); a target fits when its ``load_rate`` does. Without a
    benchmark (None) only the multiplier cap applies; the health detector then
    reports drops (§4.11 step-down).

    * ``DISPLAY`` ("Match your display"): the display rate itself (180 Hz → 180)
      when it fits and is at most ``DISPLAY_MULTIPLIER`` × the source; else 60 fps
      (owner decision 2026-10-10); else the rules below (2×, then refresh/k).
    * ``DISPLAY_MAX`` (max smoothness): the highest such refresh/k
      (180 Hz: 23.976 → 90).

    When no refresh/k ≥ 2× fits the cap, both take plain 2× if it fits below the
    display rate, then the highest refresh/k that fits (50 fps on 60 Hz → 60).

    ``X2`` and ``FPS`` never go above a known display rate (``over_display``):
    there the highest refresh/k above the source that fits is used instead
    (50 fps at 2× on 60 Hz → 60; 60 fps on 60 Hz → ``ALREADY_AT_RATE``), since
    mpv would drop every frame above the refresh rate. With the display rate
    unknown (None or 0) they keep their own rate, as before.
    """
    cap = Fraction(sustainable_fps).limit_denominator(1001) if sustainable_fps else None

    def fits(t: Fraction) -> bool:
        return cap is None or load_rate(src, t) <= cap

    if kind is TargetKind.X2 or kind is TargetKind.FPS:
        if kind is TargetKind.X2:
            t = src * 2
        elif fixed is None or fixed <= src:
            return TargetChoice(None, BypassReason.ALREADY_AT_RATE)
        else:
            t = fixed
        if over_display(t, display_hz):
            return _within_display(src, display_hz, fits)
        if not fits(t):
            return TargetChoice(None, BypassReason.NO_REALTIME)
        return TargetChoice(t, None)
    display = display_rate(display_hz)
    if display is None:
        return TargetChoice(None, None)  # pending: no display rate yet
    if display <= src:
        return TargetChoice(None, BypassReason.ALREADY_AT_RATE)
    if kind is TargetKind.DISPLAY:
        if display / src <= DISPLAY_MULTIPLIER and fits(display):
            return TargetChoice(display, None)
        sixty = Fraction(60)
        if src < sixty < display and fits(sixty):
            return TargetChoice(sixty, None)
    # refresh/k above the source and within the multiplier cap, highest first;
    # every candidate is costed on its own (24 → 30 needs as many inferences as
    # 24 → 48), so the scan never stops at the first rate that is too high
    rates: list[Fraction] = []
    k = 1
    while (t := display / k) > src:
        if t / src <= MAX_MULTIPLIER:
            rates.append(t)
        k += 1
    doubling = src * 2 * AUTO_DOUBLING_TOLERANCE
    doubles = [t for t in rates if t >= doubling]
    if kind is not TargetKind.DISPLAY_MAX:
        doubles.reverse()  # lowest first
    for t in doubles:
        if fits(t):
            return TargetChoice(t, None)
    if src * 2 < display and fits(src * 2):
        return TargetChoice(src * 2, None)
    for t in rates:
        if t < doubling and fits(t):
            return TargetChoice(t, None)
    return TargetChoice(None, BypassReason.NO_REALTIME if rates else BypassReason.ALREADY_AT_RATE)


def _within_display(
    src: Fraction, display_hz: float | None, fits: Callable[[Fraction], bool]
) -> TargetChoice:
    """A 2× or fixed target the display can't show (``over_display``): the
    highest refresh/k above the source that ``fits``, so 50 fps at 2× on a 60 Hz
    panel runs 50 → 60, which infers as many frames as 50 → 100 (``load_rate``)
    but sends none to be dropped. A source the display already shows frame for
    frame - its rate within ``DISPLAY_RESAMPLE_TOLERANCE`` of the refresh rate,
    or above it - is ``ALREADY_AT_RATE``: 59.94 → 60 would infer almost every
    output frame for a 0.1 % change that display-resample makes for free."""
    display = display_rate(display_hz)
    if display is None or display <= src * DISPLAY_RESAMPLE_TOLERANCE:
        return TargetChoice(None, BypassReason.ALREADY_AT_RATE)
    k = 1
    while (t := display / k) > src * DISPLAY_RESAMPLE_TOLERANCE:
        if fits(t):
            return TargetChoice(t, None)
        k += 1
    return TargetChoice(None, BypassReason.NO_REALTIME)


def fmt_rate(r: Fraction) -> str:
    """A rate for people: ``48``, ``47.952``."""
    return f"{float(r):.3f}".rstrip("0").rstrip(".")


# §11.9: plain-language notices for the live engine decision (the GUI shows them).
#: MVTools' BlockFPS against FlowFPS inside mpv (spike M0(p): 2.2x at 1080p, 2.3x at
#: 2160p, same fidelity, visible blocks only in very fast motion); a little under it
BLOCKFPS_SPEEDUP = 2.0


def blockfps_option(options: Sequence[EngineOption]) -> EngineOption | None:
    """MVTools with BlockFPS: its live cap estimated from FlowFPS's (None without
    MVTools or without a measured FlowFPS cap)."""
    for o in options:
        if o.backend is BackendId.MVTOOLS and o.sustainable_fps is not None:
            return dataclasses.replace(o, sustainable_fps=o.sustainable_fps * BLOCKFPS_SPEEDUP)
    return None


def blockfps_notice() -> Msg:
    return Msg("Using MVTools' faster block mode to keep up; very fast motion may look blocky.")


def gpu_fallback_notice(engine: BackendId, fallback: BackendId) -> Msg:
    return Msg(
        "{engine} can't keep up with this video; using {fallback}.",
        {"engine": BACKEND_NAMES[engine], "fallback": BACKEND_NAMES[fallback]},
    )


def engine_building_notice(width: int, height: int, fallback: BackendId | None) -> Msg:
    if fallback is None:
        return Msg(
            "Preparing TensorRT for {w} × {h} (about a minute, once); "
            "smoothing starts when it is ready.",
            {"w": width, "h": height},
        )
    return Msg(
        "Preparing TensorRT for {w} × {h} (about a minute, once); "
        "using {fallback} until it is ready.",
        {"w": width, "h": height, "fallback": BACKEND_NAMES[fallback]},
    )


def no_realtime_notice() -> Msg:
    return Msg(
        "This video is too demanding to smooth in real time on this computer, "
        "so it plays without smoothing."
    )


def cpu_fallback_notice(target: Fraction) -> Msg:
    return Msg(
        "The GPU engine can't keep up with {target} fps at this size, so ButterEye is "
        "using CPU smoothing for this video.",
        {"target": fmt_rate(target)},
    )


# Smaller sizes tried, largest first, when a video is too demanding at its own size.
SMOOTH_HEIGHTS: tuple[int, ...] = (1440, 1080, 720)


def smaller_sizes(width: int, height: int) -> tuple[tuple[int, int], ...]:
    """Sizes to smooth at instead of ``width``×``height`` (same shape, even sides),
    largest first; mpv scales the result to the window like any video."""
    if width <= 0 or height <= 0:
        return ()
    out: list[tuple[int, int]] = []
    for h in SMOOTH_HEIGHTS:
        if h >= height:
            continue
        w = max(2, round(width * h / height / 2) * 2)
        out.append((w, h))
    return tuple(out)


# Decoding a big video and shrinking it costs time the speed test (run at the
# smaller size) never saw: about 4 ms per output frame for 4K 10-bit HEVC on the
# dev box (RTX 4090, mpv copy-back decoding; RIFE v4.26 24 → 48 ran 47.5 fps at
# 1080p and 86 fps at 720p against a 61 fps 1080p benchmark). Scaled by pixels.
SHRINK_COST_S_PER_UHD_FRAME = 0.004
UHD_PIXELS = 3840 * 2160


def shrink_cap(cap: float | None, src_width: int, src_height: int) -> float | None:
    """A live cap measured at a smaller size, less the cost of decoding and
    shrinking a ``src_width``×``src_height`` video first."""
    if cap is None or cap <= 0:
        return cap
    cost = SHRINK_COST_S_PER_UHD_FRAME * (src_width * src_height) / UHD_PIXELS
    return 1.0 / (1.0 / cap + cost)


def smaller_size_notice(height: int) -> Msg:
    return Msg(
        "Smoothing at {height}p so this computer can keep up; the picture is scaled "
        "back up to your window.",
        {"height": height},
    )


#: TensorRT RIFE with its optical flow at half resolution (spike M0(q)): about
#: 8-11 % faster at 4K (v4.26 spends most of its time in full-resolution layers),
#: within ~0.5 dB of full flow; used only when full flow can't keep up and the
#: frame is at least this large, before smoothing a smaller picture
HALF_FLOW_GAIN = 1.08
HALF_FLOW_MIN_PIXELS = 2560 * 1440


def half_flow_notice() -> Msg:
    return Msg("Estimating motion at half resolution so TensorRT can keep up at full size.")


def full_size_notice() -> Msg:
    return Msg(
        "Smoothing at full size as you chose; this computer may not keep up, so some "
        "frames may drop."
    )


def forced_smaller_notice(height: int) -> Msg:
    return Msg(
        "Smoothing anyway at {height}p, past what the speed test says this computer "
        "can keep up with, so it may stutter.",
        {"height": height},
    )


def forced_notice() -> Msg:
    return Msg(
        "Smoothing anyway, past what the speed test says this computer can keep up "
        "with, so it may stutter."
    )


def switched_to_cpu_notice() -> Msg:
    return Msg("The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing.")


@dataclass(frozen=True, slots=True)
class EngineOption:
    """One engine the session may run, with its live rate cap (None = no benchmark)."""

    backend: BackendId
    sustainable_fps: float | None


@dataclass(frozen=True, slots=True)
class EnginePick:
    """``backend`` None with ``target.bypass`` set: nothing can run (or nothing to do)."""

    backend: BackendId | None
    target: TargetChoice
    notice: Msg | None


#: Engines that run on the GPU and need benchmark data before Auto trusts them
GPU_BACKENDS = frozenset({BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVGPU})
#: The RIFE engines (picture quality first; mvgpu is the fast GPU engine after them)
RIFE_BACKENDS = frozenset({BackendId.RIFE_TRT, BackendId.RIFE_NCNN})


def rife_live_needs_proof(dev: VulkanDevice | None) -> bool:
    """True where RIFE-ncnn may run live only on a benchmark that measured it.

    ``dev`` is the interpolation device (``backends.select.chosen_device``,
    ``Session.gpu_device``), None when there is no report or no GPU.

    macOS, whatever ``dev`` says. On an Apple M2 through MoltenVK, RIFE-ncnn was
    measured offline-only at every size (docs/macos-release-bar.md, item 18;
    docs/spikes/m0s.md): 17.9-20.1 fps in mpv at 720p, 8.3-9.3 at 1080p and
    4.6-5.4 at 1440p against the 55.14 fps a real-time 2x of 23.976 needs, and
    a 4K run did not finish at all. The fresh-install measurement of the
    self-contained app confirmed what that means without a benchmark on file:
    the shipped Quality profile pinned RIFE v4.26, nothing capped it, and mpv
    ran at 6-8 % of real time at 1080p (silently, with no drops, because mpv
    slows the clock instead of dropping) and stalled at 4K. So on a Mac RIFE is
    never chosen for live playback by default, by a shipped profile or by
    "the only engine there is" - only by a benchmark whose in-mpv measurement
    caps it (Automatic then weighs it like any other engine) or by the user's
    own explicit choice. Offline render is untouched: RIFE is the right engine
    there at any speed.

    On Linux and Windows, any device that is not a discrete GPU: an integrated
    GPU, a virtual one or anything else the driver reports. Nothing measured
    there says an iGPU does better than the M2's, which shares its design (one
    memory pool, a GPU sized for the display rather than for compute), and
    trusting it would cost the same stall, fallback and rollback at the start of
    playback, or silent slow motion on a file with no audio. A discrete GPU is
    not asked: an RTX 4090 runs RIFE v4.26 live at 1080p (M0(f), 56-58 fps in
    mpv), and the shipped Quality profile is that machine's default before any
    benchmark. No device (None) is not asked either, because RIFE-ncnn does not
    run without one. Read at call time, so a test can ask any platform.
    """
    if osapi.IS_MACOS:
        return True
    return dev is not None and dev.device_type != "discrete"


def _unproven_rife(o: EngineOption, needs_proof: bool) -> bool:
    return o.backend is BackendId.RIFE_NCNN and o.sustainable_fps is None and needs_proof


def _rife_proof(given: bool | None) -> bool:
    """``rife_proof`` as the pick functions take it: None (a caller that knows no
    device) asks :func:`rife_live_needs_proof` without one, which is macOS only."""
    return rife_live_needs_proof(None) if given is None else given


def _display_tier(
    t: Fraction | None, src: Fraction, display_hz: float | None, kind: TargetKind
) -> int:
    """How well ``t`` answers "Match your display" (§5.6, owner decision
    2026-10-10): 0 = the display rate itself, 1 = 60 fps, 2 = any other rate.
    Other target kinds: always 0 (only the multiplier rules apply)."""
    if kind is not TargetKind.DISPLAY or t is None:
        return 0
    display = display_rate(display_hz)
    if display is not None and t == display and display / src <= DISPLAY_MULTIPLIER:
        return 0
    if t == Fraction(60) and display is not None and src < t < display:
        return 1
    return 2


def pick_engine(
    options: Sequence[EngineOption],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None = None,
    *,
    auto: bool,
    forced: bool = False,
    animation: bool = False,
    rife_proof: bool | None = None,
) -> EnginePick:
    """§11.9: the engine is chosen after the target, against each engine's live cap.

    ``options`` are in preference order (TensorRT, RIFE-ncnn, mvgpu, MVTools).

    * ``auto``: GPU engines (RIFE and mvgpu) are eligible only with benchmark
      data for them (an unmeasured GPU engine is never trusted with a live
      target); MVTools is eligible with or without data. The first eligible
      engine whose target at least doubles the source rate (or equals the best
      target any engine reaches) wins, so on a display target a GPU engine
      capped to e.g. 24 → 30 fps loses to a CPU engine that reaches 60, and a
      RIFE that can't keep up at this size loses to mvgpu at full size. On
      "Match your display" a RIFE pick that misses the display rate (then 60
      fps) gives way to mvgpu when mvgpu reaches it. When the only installed
      engines are unmeasured GPU engines, the first is used uncapped (the
      health detector still guards it) - except an unmeasured RIFE-ncnn when
      ``rife_proof`` (:func:`rife_live_needs_proof` for the interpolation
      device: macOS, or a GPU that is not discrete), which is skipped for the
      next one, or gives ``NO_REALTIME`` when it is the only engine.
    * pinned (``options[0]`` is the engine the profile asks for): used when it
      reaches a target; a pinned GPU engine that can't falls back to the next
      option that can for this file: mvgpu (after RIFE) with
      ``gpu_fallback_notice``, MVTools with ``cpu_fallback_notice``.

    ``animation`` (a profile for 2D animation, ``ContentKind.ANIMATION``): the
    caller ranks MVTools before mvgpu, and mvgpu no longer takes over from a RIFE
    that misses the display rate - mvgpu loses large flat shapes, and drawings
    held on twos double each step it has to find.

    Nothing reaches a target → ``NO_REALTIME`` with ``no_realtime_notice``;
    with ``forced`` (the user asked to smooth anyway) the first engine then runs
    uncapped instead, with ``forced_notice``.
    ``ALREADY_AT_RATE`` and "waiting for the display rate" do not depend on the
    engine and are returned at once.

    ``rife_proof`` is computed by the caller, because an ``EngineOption`` names
    no device; None asks :func:`rife_live_needs_proof` without one (macOS only).
    """

    proof = _rife_proof(rife_proof)
    pick = _pick_engine(
        options, src, display_hz, kind, fixed, auto=auto, animation=animation, rife_proof=proof
    )
    if not forced or not options or pick.target.bypass is not BypassReason.NO_REALTIME:
        return pick
    # "smooth anyway" on Automatic is not a request for an unmeasured RIFE where
    # it needs proof: the first engine that is not one, if there is one
    first = (
        next((o for o in options if not _unproven_rife(o, proof)), options[0])
        if auto
        else options[0]
    )
    tc = choose_target(src, display_hz, kind, fixed)  # the speed test's cap is ignored
    if tc.bypass is not None or tc.target is None:
        return pick
    return EnginePick(first.backend, tc, forced_notice())


def pick_size(
    sized: Sequence[tuple[tuple[int, int], Sequence[EngineOption]]],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None = None,
    *,
    auto: bool,
    forced: bool = False,
    animation: bool = False,
    rife_proof: bool | None = None,
) -> tuple[EnginePick, tuple[int, int]] | None:
    """§11.9: a video too demanding at its own size is smoothed at a smaller one.

    ``sized`` lists the smaller sizes, largest first, each with the engine
    options (live caps) at that size. Picture quality comes from the engine
    more than from the size, so:

    1. the GPU engine (measured) at the largest size where it keeps up — and,
       in auto, at least doubles the rate;
    2. otherwise any engine at the largest size where ``pick_engine`` finds one
       (a pinned RIFE-ncnn may fall back to MVTools there);
    3. otherwise, with ``forced``, the first engine uncapped at the smallest size.

    With ``animation`` step 1 asks only RIFE: mvgpu at a smaller size does not
    beat MVTools for 2D animation (``pick_engine``). ``rife_proof`` is passed to
    every ``pick_engine`` here, as there.

    None: nothing works at any smaller size (and not forced, or no size).

    "Doubles" means at least the doubling rate or the best target the display
    allows, whichever is lower: on a 60 Hz display a 50 fps video can go no
    higher than 60, and a GPU engine that reaches 60 must not lose to the CPU
    for missing a 100 the screen could never show."""

    def runs(p: EnginePick) -> bool:
        return p.backend is not None and p.target.bypass is None and p.target.target is not None

    proof = _rife_proof(rife_proof)
    reach = choose_target(src, display_hz, kind, fixed).target  # speed aside
    need = src * 2 * AUTO_DOUBLING_TOLERANCE
    if reach is not None:
        need = min(need, reach)
    for size, opts in sized:
        first = RIFE_BACKENDS if animation else GPU_BACKENDS
        gpu = [o for o in opts if o.backend in first and o.sustainable_fps is not None]
        if not gpu:
            continue
        p = pick_engine(
            gpu, src, display_hz, kind, fixed, auto=auto, animation=animation, rife_proof=proof
        )
        target = p.target.target
        if runs(p) and target is not None and (not auto or target >= need):
            return p, size
    for size, opts in sized:
        p = pick_engine(
            opts, src, display_hz, kind, fixed, auto=auto, animation=animation, rife_proof=proof
        )
        if runs(p):
            return p, size
    if forced and sized:
        size, opts = sized[-1]
        p = pick_engine(
            opts,
            src,
            display_hz,
            kind,
            fixed,
            auto=auto,
            forced=True,
            animation=animation,
            rife_proof=proof,
        )
        if runs(p):
            return dataclasses.replace(p, notice=forced_smaller_notice(size[1])), size
    return None


def pick_even_size(
    sized: Sequence[tuple[tuple[int, int], Sequence[EngineOption]]],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None = None,
    *,
    auto: bool,
    animation: bool = False,
) -> tuple[EnginePick, tuple[int, int]] | None:
    """B3, cadence first: the largest smaller size where a measured GPU engine
    reaches a target that paces evenly on the display (``uneven_pacing``) and at
    least doubles the source (or reaches the best rate the display allows).

    For "Match your display" on a 60 Hz panel, 23.976 fps at 1080p gets 2x
    (47.952, 1-1-1-2) because 24 -> 60 infers almost every output frame (a load
    of about 120 against caps of 72-76 on an M2); mvgpu at 720p (about 139 after
    ``shrink_cap``) reaches 60, which every refresh shows once. Whether that
    beats 1080p at 48 is a matter of taste, so the session asks only when the
    user put cadence first. None: no size gives even pacing."""
    reach = choose_target(src, display_hz, kind, fixed).target
    need = src * 2 * AUTO_DOUBLING_TOLERANCE
    if reach is not None:
        need = min(need, reach)
    first = RIFE_BACKENDS if animation else GPU_BACKENDS
    for size, opts in sized:
        gpu = [o for o in opts if o.backend in first and o.sustainable_fps is not None]
        if not gpu:
            continue
        p = pick_engine(gpu, src, display_hz, kind, fixed, auto=auto, animation=animation)
        t = p.target.target
        if (
            p.backend is not None
            and p.target.bypass is None
            and t is not None
            and t >= need
            and not uneven_pacing(t, display_hz)
        ):
            return p, size
    return None


def prefer_gpu_smaller() -> bool:
    """P3: on a Mac, mvgpu at a smaller size beats CPU MVTools at the video's own.

    The M2 Air has no fan and four performance cores: MVTools at 1080p keeps them
    busy (and heats the GPU's package with them), while mvgpu at 720p measured
    about 139 fps of capacity against MVTools' 76 at 1080p. Elsewhere the old
    order stays (MVTools at full size first, which a desktop CPU cools)."""
    return osapi.IS_MACOS


def _pick_engine(
    options: Sequence[EngineOption],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None,
    *,
    auto: bool,
    animation: bool = False,
    rife_proof: bool = False,
) -> EnginePick:
    def target_for(o: EngineOption) -> TargetChoice:
        return choose_target(src, display_hz, kind, fixed, sustainable_fps=o.sustainable_fps)

    if not options:
        return EnginePick(None, TargetChoice(None, BypassReason.NO_REALTIME), no_realtime_notice())
    if auto:
        # an unmeasured GPU engine is trusted only when it is all there is - and
        # never RIFE-ncnn where it needs proof (a Mac, a GPU that is not
        # discrete: rife_live_needs_proof): the next engine, or nothing, rather
        # than a live 2x at a fraction of real time
        eligible = [
            o for o in options if o.backend not in GPU_BACKENDS or o.sustainable_fps is not None
        ] or [o for o in options if not _unproven_rife(o, rife_proof)][:1]
        if not eligible:
            return EnginePick(
                None, TargetChoice(None, BypassReason.NO_REALTIME), no_realtime_notice()
            )
        picks: list[tuple[EngineOption, TargetChoice]] = []
        for o in eligible:
            tc = target_for(o)
            if tc.bypass is not BypassReason.NO_REALTIME and (tc.bypass or tc.target is None):
                return EnginePick(None, tc, None)  # already at rate / waiting: any engine
            if tc.bypass is None and tc.target is not None:
                picks.append((o, tc))
        if not picks:
            return EnginePick(
                None, TargetChoice(None, BypassReason.NO_REALTIME), no_realtime_notice()
            )
        best = max(tc.target for _, tc in picks if tc.target is not None)
        enough = min(best, src * 2 * AUTO_DOUBLING_TOLERANCE)
        chosen = next(
            ((o, tc) for o, tc in picks if tc.target is not None and tc.target >= enough),
            picks[-1],  # not reached
        )
        if chosen[0].backend in RIFE_BACKENDS and not animation:
            # "Match your display": mvgpu at the display rate (then 60) beats a
            # RIFE that reaches only a lower rate
            tier = _display_tier(chosen[1].target, src, display_hz, kind)
            for o, tc in picks:
                if (
                    o.backend is BackendId.MVGPU
                    and _display_tier(tc.target, src, display_hz, kind) < tier
                ):
                    return EnginePick(o.backend, tc, None)
        return EnginePick(chosen[0].backend, chosen[1], None)
    first = options[0]
    tc = target_for(first)
    if tc.bypass is not BypassReason.NO_REALTIME:
        return EnginePick(first.backend if tc.bypass is None else None, tc, None)
    if first.backend in GPU_BACKENDS:
        # a pinned GPU engine that can't keep up: the next engine that can
        # (TensorRT -> RIFE-ncnn -> mvgpu -> MVTools), with a notice
        for o in options[1:]:
            tc2 = target_for(o)
            if tc2.bypass is None and tc2.target is not None:
                wanted = choose_target(src, display_hz, kind, fixed).target or tc2.target
                if o.backend is BackendId.MVTOOLS:
                    return EnginePick(o.backend, tc2, cpu_fallback_notice(wanted))
                return EnginePick(o.backend, tc2, gpu_fallback_notice(first.backend, o.backend))
    return EnginePick(None, tc, no_realtime_notice())


def _ncnn_model_dir(d: Path, *, user: bool) -> bool:
    """A usable RIFE-ncnn model directory (user dirs: real dir, ncnn files, no ONNX)."""
    try:
        if d.is_symlink() or not d.is_dir():
            return False
        if not user:
            return True
        return (d / "flownet.param").is_file() and not any(d.glob("*.onnx"))
    except OSError:
        return False


def user_models(user_dirs: Sequence[Path]) -> tuple[Path, ...]:
    """RIFE-ncnn model directories found in the user's model folders."""
    found: list[Path] = []
    for base in user_dirs:
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        found += [d for d in entries if _ncnn_model_dir(d, user=True)]
    return tuple(found)


def installed_backends(
    plugin_dirs: Path | Sequence[Path],
    model_dir: Path,
    user_model_dirs: Sequence[Path] = (),
    *,
    trt: bool = False,
) -> frozenset[BackendId]:
    """Backends whose plugin file (and, for RIFE, a model directory) is installed.

    ``plugin_dirs``: one directory, or every directory a plugin can be in, in
    load order (``paths.plugin_dirs``). Fedora and Windows have exactly one, and
    pass it as the single ``Path`` they always did. macOS has two at once - a Mac
    with the ButterEye tap installed has mvgpu and RIFE in ButterEye's own
    directory and Homebrew's MVTools in VapourSynth's autoload one - and asking
    only the first was measured to answer ``[mvgpu, rife-ncnn]`` on such a Mac,
    hiding the engine that reaches real time at every size anything does there
    (720p, 1080p and 1440p; nothing reaches it at 4K).

    ``trt``: the TensorRT path is opted in and set up (``backends.trt``).
    """
    found: set[BackendId] = {BackendId.RIFE_TRT} if trt else set()
    if plugin_in(plugin_dirs, osapi.plugin_file("librife.so")) is not None and (
        model_dir.is_dir() or user_models(user_model_dirs)
    ):
        found.add(BackendId.RIFE_NCNN)
    if plugin_in(plugin_dirs, osapi.plugin_file("mvtools.so")) is not None:
        found.add(BackendId.MVTOOLS)
    if plugin_in(plugin_dirs, MVGPU_PLUGIN) is not None:
        found.add(BackendId.MVGPU)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class BackendChoice:
    backend: BackendId | None
    notice: Msg | None


def resolve_backend(
    wanted: BackendId | None,
    installed: frozenset[BackendId],
    *,
    ranked: Sequence[BackendId] = (
        BackendId.RIFE_TRT,
        BackendId.RIFE_NCNN,
        BackendId.MVGPU,
        BackendId.MVTOOLS,
    ),
) -> BackendChoice:
    """``wanted`` None = automatic (the first installed of ``ranked``)."""
    order = [b for b in ranked if b in installed]
    if wanted is None:
        if order:
            return BackendChoice(order[0], None)
        return BackendChoice(None, Msg("No interpolation engine is installed."))
    if wanted in installed:
        return BackendChoice(wanted, None)
    order = [b for b in order if b is not BackendId.RIFE_TRT]  # never a silent TRT pick
    fallback = order[0] if order else None
    trt = wanted is BackendId.RIFE_TRT
    if fallback is None:
        if trt:
            why = Msg("TensorRT isn't turned on or set up (see the System checks).")
        else:
            why = Msg("{engine} isn't installed.", {"engine": BACKEND_NAMES[wanted]})
        return BackendChoice(None, why)
    if trt:
        return BackendChoice(
            fallback,
            Msg(
                "TensorRT isn't turned on or set up; using {fallback}.",
                {"fallback": BACKEND_NAMES[fallback]},
            ),
        )
    return BackendChoice(
        fallback,
        Msg(
            "{engine} isn't installed; using {fallback}.",
            {"engine": BACKEND_NAMES[wanted], "fallback": BACKEND_NAMES[fallback]},
        ),
    )


@dataclass(frozen=True, slots=True)
class ModelChoice:
    name: str | None
    path: Path | None
    notice: Msg | None


def _valid_name(name: str | None) -> bool:
    return bool(name) and "/" not in (name or "") and name not in (".", "..")


def find_model(
    wanted: str | None, model_dir: Path, user_model_dirs: Sequence[Path] = ()
) -> ModelChoice:
    """A RIFE-ncnn model directory by name (never the integer ``model`` index, §5.4):
    packaged first, then the user's model folders; otherwise the best packaged
    default with a notice."""
    if wanted is not None and _valid_name(wanted):
        if _ncnn_model_dir(model_dir / wanted, user=False):
            return ModelChoice(wanted, model_dir / wanted, None)
        for base in user_model_dirs:
            if _ncnn_model_dir(base / wanted, user=True):
                return ModelChoice(wanted, base / wanted, None)
    for name in DEFAULT_MODELS:
        if _ncnn_model_dir(model_dir / name, user=False):
            if wanted:
                return ModelChoice(
                    name,
                    model_dir / name,
                    Msg(
                        "Model {model} isn't installed; using {fallback}.",
                        {"model": wanted, "fallback": name},
                    ),
                )
            return ModelChoice(name, model_dir / name, None)
    return ModelChoice(None, None, Msg("No RIFE model is installed."))


def resolve_model(
    wanted: str | None, model_dir: Path, user_model_dirs: Sequence[Path] = ()
) -> tuple[str | None, Msg | None]:
    """``find_model`` as ``(name, notice)``."""
    choice = find_model(wanted, model_dir, user_model_dirs)
    return choice.name, choice.notice


def uhd_mode(width: int, height: int) -> bool:
    """RIFE-ncnn's ``uhd`` flag above 1440p (§5.3).

    The plugin reads ``uhd`` only for RIFE v1-v3 models (a user's own model);
    v4.x models, all the packaged ones, ignore it and compute flow at full size,
    so a 4K source costs about 4× 1080p. What lowers 4K cost is a smaller
    processing size (``smaller_sizes``/``pick_size``, the render size choice)."""
    return width * height > 2560 * 1440


@dataclass(frozen=True, slots=True)
class Plan:
    """What the session should do with the filter."""

    bypass: BypassReason | None
    backend: BackendId | None
    model: str | None
    target: Fraction | None
    multiplier: Fraction | None
    notice: Msg | None
    pending: bool = False  # waiting for the display rate


def bypass_reason(
    facts: SourceFacts, video_params: Mapping[str, Any], profile: Profile
) -> BypassReason | None:
    """§4.11 rows that skip interpolation regardless of the rate."""
    if not format_supported(video_params):
        return BypassReason.UNSUPPORTED_FORMAT
    if facts.interlaced:
        return BypassReason.INTERLACED
    if facts.hdr_class is HdrClass.HDR10 and profile.hdr == "passthrough":
        # F7, opt-in and experimental: spike M0(d) passed for PQ (mpv's output
        # colour parameters are identical with the filter on and off). HLG is
        # untested and stays skipped.
        return None
    if facts.hdr_class is not HdrClass.SDR:
        return BypassReason.HDR_SKIP
    return None


#: Above this multiplier a GPU profile first steps down to RIFE at 2× ("fast").
STEP_DOWN_ABOVE = Fraction(2) / AUTO_DOUBLING_TOLERANCE


def _same_work(
    cur: Profile | None, nxt: Profile, multiplier: Fraction | None, skip_lite: bool
) -> bool:
    """``nxt`` (a 2× profile) would redo what ``cur`` already runs at about 2×."""
    if cur is None or multiplier is None or multiplier > STEP_DOWN_ABOVE:
        return False
    if nxt.target.kind is not TargetKind.X2 or nxt.backend is not cur.backend:
        return False
    return nxt.model == cur.model or skip_lite


def step_down_target(
    profile_id: str | None,
    profiles: Sequence[Profile],
    *,
    multiplier: Fraction | None = None,
    skip_lite: bool = False,
    running: BackendId | None = None,
) -> str | None:
    """§5.3 step 5: the next profile that sheds load, MVTools last.

    Shipped chain: lite model ("balanced"), 2× ("fast"), MVTools ("cpu");
    ``skip_lite`` (this machine's benchmark shows the lite model no faster than
    the full one) goes from "quality" straight to "fast". A custom GPU profile
    running above 2× (``multiplier``) first steps to "fast": the same engine at a
    lower multiplier sheds far more than MVTools costs the CPU (~9.5 cores for
    1920×800 at 60 fps). A chain step that would run the same engine and model
    (lite counts as the full model under ``skip_lite``) at the 2× already running
    is skipped: it sheds nothing (the "display" rule often lands near 2×). A
    profile on MVTools or mvgpu has no step down, and neither has any profile
    whose file is ``running`` on mvgpu (a RIFE profile that fell back to it, or
    Automatic): every other step would fall back to the same mvgpu run or add
    load."""
    if running is BackendId.MVGPU:
        return None
    chain = ("quality", "balanced", "fast", "cpu")
    ids = [p.id for p in profiles]
    by_id = {p.id: p for p in profiles}
    if profile_id in chain:
        rest = chain[chain.index(profile_id) + 1 :]
        if skip_lite and profile_id == "quality":
            rest = tuple(n for n in rest if n != "balanced")
        cur = by_id.get(profile_id)
        for nxt in rest:
            if nxt not in ids:
                continue
            if _same_work(cur, by_id[nxt], multiplier, skip_lite):
                continue
            return nxt
        return None
    current = next((p for p in profiles if p.id == profile_id), None)
    if current is not None and current.backend in (BackendId.MVTOOLS, BackendId.MVGPU):
        # mvgpu is the lightest engine per frame on a GPU: RIFE ("fast") or
        # MVTools would only add load, so nothing is offered
        return None
    if multiplier is not None and multiplier > STEP_DOWN_ABOVE and "fast" in ids:
        return "fast"
    return "cpu" if "cpu" in ids and profile_id != "cpu" else None


__all__ = [
    "MAX_MULTIPLIER",
    "AUTO_DOUBLING_TOLERANCE",
    "DISPLAY_RESAMPLE_TOLERANCE",
    "over_display",
    "cadence",
    "uneven_pacing",
    "uneven_pacing_notice",
    "even_pacing_notice",
    "pick_even_size",
    "prefer_gpu_smaller",
    "display_rate",
    "DISPLAY_SNAP",
    "DEFAULT_MODELS",
    "MPV_MATRIX",
    "BACKEND_NAMES",
    "MVGPU_PLUGIN",
    "BACKEND_PLUGIN",
    "GPU_BACKENDS",
    "RIFE_BACKENDS",
    "rife_live_needs_proof",
    "TargetChoice",
    "interp_rate",
    "load_rate",
    "as_2x_rate",
    "HOLD_FILL_GROUP",
    "hold_fill_factor",
    "hold_fill_fits",
    "EngineOption",
    "EnginePick",
    "pick_engine",
    "fmt_rate",
    "no_realtime_notice",
    "cpu_fallback_notice",
    "switched_to_cpu_notice",
    "BackendChoice",
    "ModelChoice",
    "Plan",
    "fps_fraction",
    "matrix_name",
    "range_name",
    "MPV_CHROMA_LOC",
    "chroma_loc_name",
    "hdr_class",
    "interlaced",
    "format_supported",
    "params_ready",
    "source_facts",
    "no_video",
    "choose_target",
    "installed_backends",
    "resolve_backend",
    "resolve_model",
    "find_model",
    "user_models",
    "uhd_mode",
    "bypass_reason",
    "step_down_target",
    "STEP_DOWN_ABOVE",
]
