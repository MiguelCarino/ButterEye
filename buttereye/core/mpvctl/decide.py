# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Pure decisions for live playback: source facts, behaviour matrix (§4.11),
target rate and multiplier (§5.6), backend/model resolution (§5.3, M1 subset).

No I/O except the ``Path.is_dir``/``is_file`` checks in ``installed_backends``
and ``resolve_model``, which take the directories as arguments.

RIFE-ncnn models are looked up by directory name in the packaged directory
first, then in the user's ``data_dir/models`` (the Models page lists both); a
user directory counts only if it is a real directory holding an ncnn model
(``flownet.param``) and no ONNX files (those are TensorRT models).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.types import (
    BackendId,
    BypassReason,
    HdrClass,
    Msg,
    Profile,
    SourceFacts,
    TargetKind,
)

#: Preferred multipliers are 2-5x (§5.6); above this, divide the display rate.
MAX_MULTIPLIER = Fraction(5)

#: Auto counts a target as "doubling" from 1.98x (24.0031 → 48 still doubles).
AUTO_DOUBLING_TOLERANCE = Fraction(99, 100)

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


def matrix_name(colormatrix: object, height: int) -> str:
    """VapourSynth matrix for mpv's colormatrix; by height when unknown (never fixed 709)."""
    if isinstance(colormatrix, str) and colormatrix in MPV_MATRIX:
        return MPV_MATRIX[colormatrix]
    return "709" if height >= 720 else "170m"


def range_name(colorlevels: object) -> str | None:
    return colorlevels if colorlevels in ("limited", "full") else None


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

    * ``DISPLAY`` (efficient): the lowest refresh/k that at least doubles the
      source (180 Hz: 23.976 → 60; 144 Hz → 48).
    * ``DISPLAY_MAX`` (max smoothness): the highest such refresh/k
      (180 Hz: 23.976 → 90).

    When no refresh/k ≥ 2× fits the cap, both take plain 2× if it fits below the
    display rate, then the highest refresh/k that fits (50 fps on 60 Hz → 60).
    """
    cap = Fraction(sustainable_fps).limit_denominator(1001) if sustainable_fps else None

    def fits(t: Fraction) -> bool:
        return cap is None or load_rate(src, t) <= cap

    if kind is TargetKind.X2:
        t = src * 2
        if not fits(t):
            return TargetChoice(None, BypassReason.NO_REALTIME)
        return TargetChoice(t, None)
    if kind is TargetKind.FPS:
        if fixed is None or fixed <= src:
            return TargetChoice(None, BypassReason.ALREADY_AT_RATE)
        if not fits(fixed):
            return TargetChoice(None, BypassReason.NO_REALTIME)
        return TargetChoice(fixed, None)
    display = fps_fraction(display_hz)
    if display is None:
        return TargetChoice(None, None)  # pending: no display rate yet
    if display <= src:
        return TargetChoice(None, BypassReason.ALREADY_AT_RATE)
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


def fmt_rate(r: Fraction) -> str:
    """A rate for people: ``48``, ``47.952``."""
    return f"{float(r):.3f}".rstrip("0").rstrip(".")


# §11.9: plain-language notices for the live engine decision (the GUI shows them).
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
GPU_BACKENDS = frozenset({BackendId.RIFE_TRT, BackendId.RIFE_NCNN})


def pick_engine(
    options: Sequence[EngineOption],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None = None,
    *,
    auto: bool,
    forced: bool = False,
) -> EnginePick:
    """§11.9: the engine is chosen after the target, against each engine's live cap.

    ``options`` are in preference order (RIFE-ncnn, then MVTools).

    * ``auto``: RIFE-ncnn is eligible only with benchmark data for it (an
      unmeasured GPU engine is never trusted with a live target); MVTools is
      eligible with or without data. The first eligible engine whose target
      at least doubles the source rate (or equals the best target any engine
      reaches) wins, so on a display target a GPU engine capped to e.g.
      24 → 30 fps loses to a CPU engine that reaches 60. When the only
      installed engine is an unmeasured RIFE-ncnn, it is used uncapped (the
      health detector still guards it).
    * pinned (``options[0]`` is the engine the profile asks for): used when it
      reaches a target; a pinned RIFE-ncnn that can't falls back to MVTools for
      this file with ``cpu_fallback_notice``.

    Nothing reaches a target → ``NO_REALTIME`` with ``no_realtime_notice``;
    with ``forced`` (the user asked to smooth anyway) the first engine then runs
    uncapped instead, with ``forced_notice``.
    ``ALREADY_AT_RATE`` and "waiting for the display rate" do not depend on the
    engine and are returned at once.
    """

    pick = _pick_engine(options, src, display_hz, kind, fixed, auto=auto)
    if not forced or not options or pick.target.bypass is not BypassReason.NO_REALTIME:
        return pick
    first = options[0]
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

    None: nothing works at any smaller size (and not forced, or no size)."""

    def runs(p: EnginePick) -> bool:
        return p.backend is not None and p.target.bypass is None and p.target.target is not None

    for size, opts in sized:
        gpu = [o for o in opts if o.backend in GPU_BACKENDS and o.sustainable_fps is not None]
        if not gpu:
            continue
        p = pick_engine(gpu, src, display_hz, kind, fixed, auto=auto)
        target = p.target.target
        if (
            runs(p)
            and target is not None
            and (not auto or target >= src * 2 * AUTO_DOUBLING_TOLERANCE)
        ):
            return p, size
    for size, opts in sized:
        p = pick_engine(opts, src, display_hz, kind, fixed, auto=auto)
        if runs(p):
            return p, size
    if forced and sized:
        size, opts = sized[-1]
        p = pick_engine(opts, src, display_hz, kind, fixed, auto=auto, forced=True)
        if runs(p):
            return dataclasses.replace(p, notice=forced_smaller_notice(size[1])), size
    return None


def _pick_engine(
    options: Sequence[EngineOption],
    src: Fraction,
    display_hz: float | None,
    kind: TargetKind,
    fixed: Fraction | None,
    *,
    auto: bool,
) -> EnginePick:
    def target_for(o: EngineOption) -> TargetChoice:
        return choose_target(src, display_hz, kind, fixed, sustainable_fps=o.sustainable_fps)

    if not options:
        return EnginePick(None, TargetChoice(None, BypassReason.NO_REALTIME), no_realtime_notice())
    if auto:
        eligible = [
            o for o in options if o.backend not in GPU_BACKENDS or o.sustainable_fps is not None
        ] or [options[0]]
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
        for o, tc in picks:
            if tc.target is not None and tc.target >= enough:
                return EnginePick(o.backend, tc, None)
        return EnginePick(picks[-1][0].backend, picks[-1][1], None)  # not reached
    first = options[0]
    tc = target_for(first)
    if tc.bypass is not BypassReason.NO_REALTIME:
        return EnginePick(first.backend if tc.bypass is None else None, tc, None)
    if first.backend in GPU_BACKENDS:
        # a pinned GPU engine that can't keep up: the next engine that can
        # (TensorRT -> RIFE-ncnn -> MVTools), with a notice
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
    plugin_dir: Path, model_dir: Path, user_model_dirs: Sequence[Path] = (), *, trt: bool = False
) -> frozenset[BackendId]:
    """Backends whose plugin file (and, for RIFE, a model directory) is installed.
    ``trt``: the TensorRT path is opted in and set up (``backends.trt``)."""
    found: set[BackendId] = {BackendId.RIFE_TRT} if trt else set()
    if (plugin_dir / "librife.so").is_file() and (
        model_dir.is_dir() or user_models(user_model_dirs)
    ):
        found.add(BackendId.RIFE_NCNN)
    if (plugin_dir / "mvtools.so").is_file():
        found.add(BackendId.MVTOOLS)
    return frozenset(found)


@dataclass(frozen=True, slots=True)
class BackendChoice:
    backend: BackendId | None
    notice: Msg | None


def resolve_backend(
    wanted: BackendId | None,
    installed: frozenset[BackendId],
    *,
    ranked: Sequence[BackendId] = (BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVTOOLS),
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
    if facts.hdr_class is not HdrClass.SDR:
        # F7: "passthrough" stays off until spike M0(d) passes.
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
) -> str | None:
    """§5.3 step 5: the next profile that sheds load, MVTools last.

    Shipped chain: lite model ("balanced"), 2× ("fast"), MVTools ("cpu");
    ``skip_lite`` (this machine's benchmark shows the lite model no faster than
    the full one) goes from "quality" straight to "fast". A custom GPU profile
    running above 2× (``multiplier``) first steps to "fast": the same engine at a
    lower multiplier sheds far more than MVTools costs the CPU (~9.5 cores for
    1920×800 at 60 fps). A chain step that would run the same engine and model
    (lite counts as the full model under ``skip_lite``) at the 2× already running
    is skipped: it sheds nothing (the "display" rule often lands near 2×)."""
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
    if current is not None and current.backend is BackendId.MVTOOLS:
        return None
    if multiplier is not None and multiplier > STEP_DOWN_ABOVE and "fast" in ids:
        return "fast"
    return "cpu" if "cpu" in ids and profile_id != "cpu" else None


__all__ = [
    "MAX_MULTIPLIER",
    "AUTO_DOUBLING_TOLERANCE",
    "DEFAULT_MODELS",
    "MPV_MATRIX",
    "BACKEND_NAMES",
    "TargetChoice",
    "interp_rate",
    "load_rate",
    "as_2x_rate",
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
