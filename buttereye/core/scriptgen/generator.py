# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Generated script contract (SCOPE §4.4).

One static template (``template.vpy``). A generated script is the template with
only its data block filled in (constants written with ``repr()``); every other
value travels as JSON in mpv's ``user-data`` (or ``vspipe -a user_data=``).
Scripts are written atomically (0600 temp file + rename), because mpv re-reads
the file on every seek.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core import osapi
from buttereye.core.mpvctl.ipc import LABEL, VF_ADD_PREFIX, quote
from buttereye.core.types import BackendId, homebrew_prefix, macos_plugin_dir, macos_plugin_dirs

TEMPLATE = Path(__file__).with_name("template.vpy")
BLOCK_BEGIN = "# --- ButterEye data block (generated) ---"
BLOCK_END = "# --- end of data block ---"
USER_DATA_VERSION = 1

#: §4.4 defaults (owner decision 2026-10-07). RIFE-ncnn: mpv concurrent-frames 8,
#: plugin gpu_thread 4, set independently. gpu_thread 4 is the soak-tested value
#: for the bundled ncnn (M0(f): 0 Xid 13 in 42 runs; 2 gives only ~52 fps). Inside
#: mpv at 1080p 2x (RTX 4090, v4.26) concurrent-frames 4 reaches ~45-47 fps, below
#: real time; 8 reaches ~56-58 fps (startup included). gpu_thread 8 was no faster
#: and less steady, so it is not tied to concurrent-frames. buffered-frames 4.
RIFE_CONCURRENT_FRAMES_DEFAULT = 8
RIFE_GPU_THREAD_DEFAULT = 4
#: macOS: 1. MoltenVK exposes one compute queue on Apple Silicon (``queueC=0[1]``),
#: and ncnn refuses more with "gpu_thread is recommended to be between 1 and 1";
#: measured on an M2 at 1280x720 (packaging/macos/RIFE-SPIKE.md, "Throughput"):
#: gt=1 18.81 fps, gt=2 19.66, gt=4 17.62 - nothing gained, a warning per load.
RIFE_GPU_THREAD_MACOS = 1


def rife_gpu_thread() -> int:
    """RIFE-ncnn's ``gpu_thread`` on this platform, read at call time."""
    return RIFE_GPU_THREAD_MACOS if osapi.IS_MACOS else RIFE_GPU_THREAD_DEFAULT


BUFFERED_FRAMES_DEFAULT = 4
#: MVTools in mpv: concurrent-frames up to 16, never above the CPU count. On the
#: dev box (Ryzen 9 5900X, 24 threads) 8 / 16 / 24 give 128 / 148 / 137 fps at
#: 1080p 2x and 28 / 35 / 34 at 2160p (spike M0(m)); 16 matches vspipe.
MVTOOLS_MAX_CONCURRENT = 16
#: mvgpu in mpv: its GPU work is serialised inside the plugin, so requests in
#: flight only overlap the CPU copies of frames. 4K 23.976 -> 119.88 untimed on
#: the dev box (RTX 4090, 2026-10-10): 164 / 171 fps at 4 / 8 concurrent frames
#: over 1500 frames (120 / 150 / 154 at 2 / 8 / 16 over 600); 8, like RIFE-ncnn.
MVGPU_CONCURRENT_FRAMES_DEFAULT = 8


@dataclass(frozen=True, slots=True)
class ScriptConstants:
    """The only values that appear as code in a generated script."""

    plugin_dir: Path = field(default_factory=lambda: _default_plugin_dir())
    # Fedora's ffms2 is not in VapourSynth's autoload directory (SCOPE §7.1); on
    # Windows the installer puts ffms2.dll with ButterEye's other plugins, and on
    # macOS Homebrew's bottle is both the library and the plugin (_macos_ffms2)
    ffms2_lib: Path = field(default_factory=lambda: _default_ffms2())


def _default_plugin_dir() -> Path:
    if osapi.IS_WINDOWS:
        from buttereye.core.paths import install_dir

        return install_dir() / "vapoursynth"
    if osapi.IS_MACOS:
        # one directory, not the two-entry search path the doctor asks
        # (``types.macos_plugin_dirs``): ``PLUGIN_DIR`` is a single literal in the
        # data block and ``_load`` joins one absolute path per plugin onto it. That
        # is enough, because the directory this leaves out on a Mac is Homebrew's
        # autoloaded one, and ``_load`` skips the ``LoadPlugin`` for a namespace
        # VapourSynth has already autoloaded -- so the plugin the doctor found
        # there is the plugin the clip goes through either way.
        return macos_plugin_dir()
    if osapi.LINUX_APP is not None:
        # the AppImage trial: the one folder its VapourSynth loads plugins from
        return osapi.LINUX_APP / "vapoursynth"
    return Path("/usr/lib64/buttereye/vapoursynth")


def _macos_ffms2() -> Path:
    """ffms2 on macOS: Homebrew's ``ffms2`` bottle, whose shared library is also
    the VapourSynth plugin, as Fedora's ``libffms2.so.5`` is. Measured, because
    one file doing both jobs is the kind of thing that is only true until it
    isn't: VapourSynth answered a ``LoadPlugin`` of
    ``/opt/homebrew/lib/libffms2.5.dylib`` with "already loaded
    (com.vapoursynth.ffms2)", so it read the plugin out of that very file.

    A copy in the directory ButterEye loads its plugins from first, the way the
    Windows installer puts ``ffms2.dll`` next to the other plugins -- and that is
    also the hit on a plain Homebrew Mac, where that directory is VapourSynth's
    autoload one and holds the very ``libffms2.dylib`` the core autoloads. Then
    the SOVERSION 5 name under ``<prefix>/lib``, the exact analogue of Fedora's
    and the one that cannot turn into a newer, incompatible library under
    ButterEye. Then every other directory a plugin can be in
    (``macos_plugin_dirs``): with the tap installed, ``_default_plugin_dir`` is
    ButterEye's own directory and the autoload one is no longer on this list by
    accident, so without this the doctor could report ffms2 present -- its globs
    search both -- while the script named a file that is not there and raised
    "the ffms2 source plugin is not installed". Last, the unversioned link brew
    always makes, which is the name worth reporting as missing when there is no
    Homebrew ffms2 at all.
    """
    name = osapi.plugin_file("libffms2.so")
    if osapi.MACOS_APP is not None:
        # ButterEye.app carries ffms2 with its other plugins, as the Windows
        # installer does, and asks nowhere else
        return _default_plugin_dir() / name
    lib = homebrew_prefix() / "lib"
    candidates = (
        _default_plugin_dir() / name,
        lib / "libffms2.5.dylib",
        *(d / name for d in macos_plugin_dirs()),
    )
    return next((p for p in candidates if p.is_file()), lib / "libffms2.dylib")


def _default_ffms2() -> Path:
    if osapi.IS_WINDOWS:
        return _default_plugin_dir() / "ffms2.dll"
    if osapi.IS_MACOS:
        return _macos_ffms2()
    if osapi.LINUX_APP is not None:
        # the AppImage carries FFMS2 with its other plugins, as Windows does
        return _default_plugin_dir() / "libffms2.so"
    return Path("/usr/lib64/libffms2.so.5")


@dataclass(frozen=True, slots=True)
class FilterParams:
    """Everything one filter generation needs; serialised to ``user_data``."""

    gen: int
    backend: BackendId
    src_fps: Fraction
    target_fps: Fraction
    buffered_frames: int
    concurrent_frames: int
    matrix: str | None = None  # VapourSynth matrix name ("709", "170m", ...)
    range: str | None = None  # "limited" | "full"
    model_path: Path | None = None  # RIFE-ncnn model directory
    gpu_id: int | None = None  # Vulkan device index (RIFE-ncnn, mvgpu)
    gpu_thread: int | None = None
    uhd: bool = False
    sc_threshold: float | None = None
    title: str = ""  # diagnostics only; never code
    size: tuple[int, int] | None = None  # smooth at this w×h instead of the source size
    #: 8-bit RIFE output dither (zimg name); None = "ordered" (10-bit: none).
    #: Offline renders ask for "error_diffusion" (§4.4).
    dither: str | None = None
    #: MVTools Super pel; None = 2 up to 1080p, 1 above (renders ask for 2)
    mv_pel: int | None = None
    #: MVTools "block" = BlockFPS (live fallback when FlowFPS can't keep up, M0(p))
    mv_mode: str | None = None
    #: TensorRT settings (``backends.trt.script_settings``): folders, model,
    #: precision, streams, build mode. Data only, never code (§4.4).
    trt: Mapping[str, Any] | None = None
    #: animation on twos/threes: re-time drawings held 2..``hold_max`` frames so
    #: motion is spread evenly instead of still, still, jump. Decided per frame,
    #: so live action never triggers it. RIFE-ncnn, MVTools and mvgpu (not
    #: TensorRT). Reach of one output frame between source frames n and n+1
    #: (measured, hold_max 3): with scene detection or still skip on, n-4..n+5;
    #: with hold fill n-hold_max-2..n+hold_max+3 (-5..+6); MVTools one more on
    #: each side. buffered-frames: at least 2 * hold_max + 4 with hold fill; mpv's
    #: default 4 otherwise (VapourSynth's frame cache serves the rest: no "frame
    #: too far back" in mpv seek tests at 4; a profile's 1 failed 2 runs in 14).
    hold_fill: bool = False
    hold_max: int = 3
    #: copy instead of interpolating identical frame pairs (speed only, nothing
    #: visible). Off for speed tests: their blank source is all identical pairs.
    still_skip: bool = False
    #: chroma siting, zimg name ("left", "center", "top_left", "top",
    #: "bottom_left", "bottom"), from mpv's video-params; None = what the source
    #: frames say when the script reads them itself (offline), else left
    chroma_loc: str | None = None
    #: A/B view (``GeneralSettings.compare``): "split" shows the source, repeated
    #: to the target rate, on the left half and the smoothed picture on the right
    compare: str | None = None
    #: anti-aliasing before smoothing (``Profile.aa``): znedi3 doubles the luma
    #: both ways and it is scaled back, which smooths jagged edges the engine
    #: would otherwise carry into every in-between frame. GPU engines only (the
    #: CPU is free there); skipped when the znedi3 plugin is not installed
    aa: bool = False
    #: HDR10 pass-through (``Profile.hdr``): the output keeps at least 10 bits,
    #: whatever the decoder handed over, so PQ is never cut to 8
    hdr: bool = False
    #: the depth the script outputs (offline copies: the encoder's,
    #: ``render.pipeline.output_bits``). RIFE's float result is written at it
    #: directly; a shallower one than the engine's is reduced with ``dither`` at
    #: the end. None = the source's (live play), at least 10 with ``hdr``
    out_bits: int | None = None


def default_frames(backend: BackendId, *, cpu_count: int | None = None) -> tuple[int, int]:
    """``(buffered_frames, concurrent_frames)`` per §4.4."""
    if backend is BackendId.MVTOOLS:
        n = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
        return BUFFERED_FRAMES_DEFAULT, max(1, min(n, MVTOOLS_MAX_CONCURRENT))
    if backend is BackendId.MVGPU:
        return BUFFERED_FRAMES_DEFAULT, MVGPU_CONCURRENT_FRAMES_DEFAULT
    return BUFFERED_FRAMES_DEFAULT, RIFE_CONCURRENT_FRAMES_DEFAULT


def template_text() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


DEFAULT_CONSTANTS = ScriptConstants()


def render_script(
    consts: ScriptConstants = DEFAULT_CONSTANTS, *, template: str | None = None
) -> str:
    """The template with its data block filled in."""
    text = template_text() if template is None else template
    begin = text.index(BLOCK_BEGIN) + len(BLOCK_BEGIN)
    end = text.index(BLOCK_END, begin)
    block = (
        f"\nPLUGIN_DIR = {os.fspath(consts.plugin_dir)!r}\n"
        f"FFMS2_LIB = {os.fspath(consts.ffms2_lib)!r}\n"
    )
    return text[:begin] + block + text[end:]


def _rate(r: Fraction) -> list[int]:
    return [r.numerator, r.denominator]


def user_data(params: FilterParams) -> str:
    """Compact, ASCII-only JSON (byte length == character length)."""
    data: dict[str, Any] = {
        "v": USER_DATA_VERSION,
        "gen": params.gen,
        "backend": params.backend.value,
        "src_fps": _rate(params.src_fps),
        "target_fps": _rate(params.target_fps),
        "matrix": params.matrix,
        "range": params.range,
        "sc_threshold": params.sc_threshold,
        "title": params.title,
        "hold_fill": bool(params.hold_fill),
        "hold_max": int(params.hold_max),
        "still_skip": bool(params.still_skip),
    }
    if params.chroma_loc is not None:
        data["chromaloc"] = params.chroma_loc
    if params.compare == "split":
        data["compare"] = "split"
    if params.aa:
        data["aa"] = True
    if params.hdr:
        data["hdr"] = True
    if params.size is not None:
        data["size"] = [int(params.size[0]), int(params.size[1])]
    if params.out_bits is not None:
        data["out_bits"] = int(params.out_bits)
        if params.dither is not None:  # the reduction at the end, every engine
            data["dither"] = params.dither
    if params.backend is BackendId.RIFE_NCNN:
        data["model_path"] = os.fspath(params.model_path) if params.model_path else None
        data["gpu_id"] = params.gpu_id
        data["gpu_thread"] = params.gpu_thread or rife_gpu_thread()
        data["uhd"] = params.uhd
        if params.dither is not None:
            data["dither"] = params.dither
    elif params.backend is BackendId.RIFE_TRT:
        if params.trt is None:
            raise ValueError("RIFE_TRT needs FilterParams.trt")
        data["trt"] = dict(params.trt)
        data["gpu_id"] = params.gpu_id
        if params.dither is not None:
            data["dither"] = params.dither
    elif params.backend is BackendId.MVGPU:
        data["gpu_id"] = params.gpu_id
    elif params.backend is BackendId.MVTOOLS:
        if params.mv_pel is not None:
            data["mv_pel"] = int(params.mv_pel)
        if params.mv_mode == "block":
            data["mv_mode"] = "block"
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def vf_argument(script: Path, params: FilterParams) -> str:
    """``@buttereye:vapoursynth=file=%n%…:buffered-frames=…:concurrent-frames=…:user-data=%n%…``."""
    return (
        f"{VF_ADD_PREFIX}file={quote(os.fspath(script))}"
        f":buffered-frames={int(params.buffered_frames)}"
        f":concurrent-frames={int(params.concurrent_frames)}"
        f":user-data={quote(user_data(params))}"
    )


def write_atomic(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write ``text`` to ``path`` via a ``mode`` temp file in the same directory + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        osapi.set_mode(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        osapi.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_script(path: Path, consts: ScriptConstants = DEFAULT_CONSTANTS) -> None:
    """Write the generated script unless an identical one is already there."""
    text = render_script(consts)
    with contextlib.suppress(OSError):
        if path.read_text(encoding="utf-8") == text:
            return
    write_atomic(path, text)


def filter_entry(vf: object) -> Mapping[str, Any] | None:
    """The ``@buttereye`` entry of an mpv ``vf`` property value, if any."""
    if not isinstance(vf, list):
        return None
    for entry in vf:
        if isinstance(entry, dict) and entry.get("label") == LABEL:
            return entry
    return None


def entry_gen(entry: Mapping[str, Any] | None) -> int | None:
    """``gen`` from an entry's ``user-data`` JSON (None if absent or not ours)."""
    if entry is None:
        return None
    params = entry.get("params")
    raw = params.get("user-data") if isinstance(params, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    gen = data.get("gen") if isinstance(data, dict) else None
    return gen if type(gen) is int else None


__all__ = [
    "TEMPLATE",
    "BLOCK_BEGIN",
    "BLOCK_END",
    "USER_DATA_VERSION",
    "RIFE_CONCURRENT_FRAMES_DEFAULT",
    "RIFE_GPU_THREAD_DEFAULT",
    "RIFE_GPU_THREAD_MACOS",
    "rife_gpu_thread",
    "BUFFERED_FRAMES_DEFAULT",
    "MVGPU_CONCURRENT_FRAMES_DEFAULT",
    "ScriptConstants",
    "FilterParams",
    "default_frames",
    "template_text",
    "render_script",
    "user_data",
    "vf_argument",
    "write_atomic",
    "write_script",
    "filter_entry",
    "entry_gen",
]
