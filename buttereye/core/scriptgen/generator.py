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
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.mpvctl.ipc import LABEL, VF_ADD_PREFIX, quote
from buttereye.core.types import BackendId

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
BUFFERED_FRAMES_DEFAULT = 4
#: MVTools in mpv: concurrent-frames up to 16, never above the CPU count. On the
#: dev box (Ryzen 9 5900X, 24 threads) 8 / 16 / 24 give 128 / 148 / 137 fps at
#: 1080p 2x and 28 / 35 / 34 at 2160p (spike M0(m)); 16 matches vspipe.
MVTOOLS_MAX_CONCURRENT = 16


@dataclass(frozen=True, slots=True)
class ScriptConstants:
    """The only values that appear as code in a generated script."""

    plugin_dir: Path = Path("/usr/lib64/buttereye/vapoursynth")
    # Fedora's ffms2 is not in VapourSynth's autoload directory (SCOPE §7.1)
    ffms2_lib: Path = Path("/usr/lib64/libffms2.so.5")


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
    gpu_id: int | None = None
    gpu_thread: int | None = None
    uhd: bool = False
    sc_threshold: float | None = None
    title: str = ""  # diagnostics only; never code
    size: tuple[int, int] | None = None  # smooth at this w×h instead of the source size
    #: 8-bit RIFE output dither (zimg name); None = "ordered" (10-bit: none).
    #: Offline renders ask for "error_diffusion" (§4.4).
    dither: str | None = None
    #: MVTools Super pel; None = 1 from 720p up, else 2 (renders ask for 2)
    mv_pel: int | None = None
    #: TensorRT settings (``backends.trt.script_settings``): folders, model,
    #: precision, streams, build mode. Data only, never code (§4.4).
    trt: Mapping[str, Any] | None = None


def default_frames(backend: BackendId, *, cpu_count: int | None = None) -> tuple[int, int]:
    """``(buffered_frames, concurrent_frames)`` per §4.4."""
    if backend is BackendId.MVTOOLS:
        n = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
        return BUFFERED_FRAMES_DEFAULT, max(1, min(n, MVTOOLS_MAX_CONCURRENT))
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
    }
    if params.size is not None:
        data["size"] = [int(params.size[0]), int(params.size[1])]
    if params.backend is BackendId.RIFE_NCNN:
        data["model_path"] = os.fspath(params.model_path) if params.model_path else None
        data["gpu_id"] = params.gpu_id
        data["gpu_thread"] = params.gpu_thread or RIFE_GPU_THREAD_DEFAULT
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
    elif params.backend is BackendId.MVTOOLS and params.mv_pel is not None:
        data["mv_pel"] = int(params.mv_pel)
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
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
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
    "BUFFERED_FRAMES_DEFAULT",
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
