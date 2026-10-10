# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Experimental NVIDIA TensorRT path through vs-mlrt's ``vstrt`` (SCOPE §5.2, F12).

ButterEye never loads TensorRT, CUDA or ``vsmlrt`` in its own process (§8.1):
this module only looks at files, and engines are built by ``vspipe`` running the
generated script in build mode (``trt.build``), in its own process group.

What the user installs (none of it ships with ButterEye, §8.3):

- ``vstrt`` and the matching ``vsmlrt.py`` from ``contrib/build-vstrt.sh``, in
  ``$XDG_DATA_HOME/buttereye/plugins/vstrt/<tag>/``;
- ``onnx`` and ``onnxconverter-common`` for fp16 engines, in
  ``$XDG_DATA_HOME/buttereye/python`` or the packaged
  ``/usr/share/buttereye/python``;
- vs-mlrt's RIFE ONNX models in ``$XDG_DATA_HOME/buttereye/models/vsmlrt/rife_v2/``;
- TensorRT 11 (``trtexec``) from NVIDIA's repository.

Engines live in ``$XDG_CACHE_HOME/buttereye/engines/<key>/``, one folder per
GPU, driver, vstrt build, model, frame size and precision (§4.6). A folder is
usable only once the build has written ``ready.json``; the playback script
refuses to build inside mpv (§5.2), so a missing engine is built here first.
Settings measured in spike M0(l): fp16 engines with half-precision (RGBH)
frames, 2 streams up to ~1080p and 4 above.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.doctor.text import M
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.ops import process_group
from buttereye.core.types import BackendId, Paths

_log = logging.getLogger(__name__)

VSTRT_SUBDIR = Path("plugins") / "vstrt"
PYTHON_SUBDIR = Path("python")
MODELS_SUBDIR = Path("models") / "vsmlrt"
ENGINES_SUBDIR = "engines"
READY = "ready.json"
LIBNVINFER = Path("/usr/lib64/libnvinfer.so.11")
BUILD_TIMEOUT_S = 900.0
#: ~1080p and below: 2 CUDA streams fill an RTX 4090 (M0(l)); larger frames gain from 4
STREAMS_SMALL, STREAMS_LARGE = 2, 4
LARGE_PIXELS = 2560 * 1440

#: ButterEye model name (the ncnn directory name profiles use) ->
#: (vsmlrt.RIFEModel member, ONNX file stem under ``rife_v2/``)
MODELS: dict[str, tuple[str, str]] = {
    "rife-v4.26_ensembleFalse": ("v4_26", "rife_v4.26"),
    "rife-v4.25_lite_ensembleFalse": ("v4_25_lite", "rife_v4.25_lite"),
    "rife-v4.22_lite_ensembleFalse": ("v4_22_lite", "rife_v4.22_lite"),
    "rife-v4.22_ensembleFalse": ("v4_22", "rife_v4.22"),
    "rife-v4.18_ensembleFalse": ("v4_18", "rife_v4.18"),
}
DEFAULT_MODEL = "rife-v4.26_ensembleFalse"


@dataclass(frozen=True, slots=True)
class TrtInstall:
    """A usable vstrt installation (everything found on disk)."""

    vstrt_dir: Path  # holds libvstrt.so
    vsmlrt_dir: Path  # holds vsmlrt.py (normally the same folder)
    python_dirs: tuple[Path, ...]  # onnx + onnxconverter-common + protobuf, first on sys.path
    models_dir: Path  # holds rife_v2/<model>.onnx
    trtexec: Path
    vstrt_version: str  # VERSION file (git describe) or the folder name
    trt_version: str  # libnvinfer.so.11 realpath version, e.g. "11.3.0"


def _vstrt_dirs(paths: Paths) -> list[Path]:
    root = paths.data_dir / VSTRT_SUBDIR
    found = [p.parent for p in root.glob("*/libvstrt.so") if p.is_file()]
    # newest build first (by modification time, then name)
    return sorted(found, key=lambda d: ((d / "libvstrt.so").stat().st_mtime, d.name), reverse=True)


def python_dirs(paths: Paths) -> tuple[Path, ...]:
    """Folders that may hold ``onnx`` and ``onnxconverter_common`` (user's first)."""
    cands = (paths.data_dir / PYTHON_SUBDIR, paths.rpm_data_dir / "python")
    return tuple(p for p in cands if (p / "onnx").is_dir() or (p / "onnxconverter_common").is_dir())


def models_dir(paths: Paths) -> Path:
    return paths.data_dir / MODELS_SUBDIR


def trt_version() -> str:
    real = os.path.realpath(LIBNVINFER)
    m = re.search(r"\.so\.(\d+(?:\.\d+)*)$", real)
    return m.group(1) if m else ""


def find_trtexec(which: Callable[[str], str | None] = shutil.which) -> Path | None:
    exe = which("trtexec") or (
        "/usr/bin/trtexec" if os.access("/usr/bin/trtexec", os.X_OK) else None
    )
    return Path(exe) if exe else None


def problems(paths: Paths, which: Callable[[str], str | None] = shutil.which) -> tuple[str, ...]:
    """What is missing for the TensorRT path, in plain words (empty = usable)."""
    out: list[str] = []
    dirs = _vstrt_dirs(paths)
    if not dirs:
        out.append(
            "vstrt isn't built (contrib/build-vstrt.sh installs it in "
            f"{paths.data_dir / VSTRT_SUBDIR})"
        )
    elif not (dirs[0] / "vsmlrt.py").is_file():
        out.append(f"vsmlrt.py is missing next to {dirs[0] / 'libvstrt.so'}")
    if find_trtexec(which) is None:
        out.append("trtexec isn't installed (libnvinfer-bin from NVIDIA's repository)")
    if not trt_version():
        out.append(f"{LIBNVINFER} isn't installed (TensorRT 11)")
    if not any(
        (models_dir(paths) / "rife_v2" / f"{stem}.onnx").is_file() for _, stem in MODELS.values()
    ):
        out.append(f"no vs-mlrt RIFE models in {models_dir(paths) / 'rife_v2'}")
    if not python_dirs(paths):
        out.append(
            "onnx and onnxconverter-common aren't in "
            f"{paths.data_dir / PYTHON_SUBDIR} (needed for fp16 engines)"
        )
    return tuple(out)


def find_install(
    paths: Paths, which: Callable[[str], str | None] = shutil.which
) -> TrtInstall | None:
    """The installation to use, or None when anything in ``problems`` is missing."""
    if problems(paths, which):
        return None
    vstrt = _vstrt_dirs(paths)[0]
    trtexec = find_trtexec(which)
    assert trtexec is not None
    version_file = vstrt / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip() or vstrt.name
    except OSError:
        version = vstrt.name
    return TrtInstall(
        vstrt_dir=vstrt,
        vsmlrt_dir=vstrt,
        python_dirs=python_dirs(paths),
        models_dir=models_dir(paths),
        trtexec=trtexec,
        vstrt_version=version,
        trt_version=trt_version(),
    )


def model_entry(name: str | None) -> tuple[str, str] | None:
    return MODELS.get(name or DEFAULT_MODEL)


def available_models(install: TrtInstall) -> frozenset[str]:
    """ButterEye model names with an ONNX file present."""
    return frozenset(
        name
        for name, (_, stem) in MODELS.items()
        if (install.models_dir / "rife_v2" / f"{stem}.onnx").is_file()
    )


def streams_for(width: int, height: int) -> int:
    return STREAMS_LARGE if width * height >= LARGE_PIXELS else STREAMS_SMALL


# ---------------------------------------------------------------- engines
@dataclass(frozen=True, slots=True)
class EngineKey:
    """Everything an engine depends on (§4.6). Streams are not part of it."""

    gpu_uuid: str
    driver: str
    vstrt_version: str
    trt_version: str
    model: str  # ButterEye model name
    width: int
    height: int
    fp16: bool = True
    half_io: bool = True

    def folder_name(self) -> str:
        entry = model_entry(self.model)
        short = entry[0] if entry else "model"
        prec = ("fp16" if self.fp16 else "fp32") + ("-h" if self.half_io else "")
        digest = hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()[:12]
        return f"{short}-{self.width}x{self.height}-{prec}-{digest}"


def engine_dir(paths: Paths, key: EngineKey) -> Path:
    return paths.cache_dir / ENGINES_SUBDIR / key.folder_name()


def is_ready(folder: Path) -> bool:
    return (folder / READY).is_file() and any(folder.glob("*.engine"))


def script_settings(
    install: TrtInstall, key: EngineKey, folder: Path, *, streams: int, build: bool
) -> dict[str, Any]:
    """The ``trt`` object of the generated script's ``user_data`` (template.vpy)."""
    entry = model_entry(key.model)
    if entry is None:
        raise ValueError(f"no TensorRT model for {key.model!r}")
    return {
        "vstrt_dir": os.fspath(install.vstrt_dir),
        "vsmlrt_dir": os.fspath(install.vsmlrt_dir),
        "python_dirs": [os.fspath(p) for p in install.python_dirs],
        "models_dir": os.fspath(install.models_dir),
        "engine_dir": os.fspath(folder),
        "trtexec": os.fspath(install.trtexec),
        "model": entry[0],
        "fp16": key.fp16,
        "half_io": key.half_io,
        "streams": int(streams),
        "build": bool(build),
    }


async def nvidia_identity(vk_uuid: str | None) -> tuple[str, str] | None:
    """``(uuid, driver version)`` of the NVIDIA GPU with this Vulkan device UUID
    (the first NVIDIA GPU when None), via an ``nvidia-smi`` subprocess (§8.1)."""
    from buttereye.core.hw.detect import SMI_QUERY, parse_nvidia_smi

    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *SMI_QUERY[1:], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except OSError, TimeoutError:
        return None
    gpus = parse_nvidia_smi(out.decode("utf-8", "replace"))
    want = (vk_uuid or "").lower().removeprefix("gpu-")
    for g in gpus:
        if not want or g.uuid.lower().removeprefix("gpu-") == want:
            return g.uuid, g.driver
    return None


def build_user_data(install: TrtInstall, key: EngineKey, folder: Path) -> str:
    """``user_data`` for a build run: a short blank clip of the key's size."""
    from buttereye.core.scriptgen.generator import FilterParams, user_data

    rate = Fraction(24000, 1001)
    params = FilterParams(
        gen=0,
        backend=BackendId.RIFE_TRT,
        src_fps=rate,
        target_fps=rate * 2,
        buffered_frames=1,
        concurrent_frames=1,
        trt=script_settings(install, key, folder, streams=1, build=True),
    )
    data = json.loads(user_data(params))
    data["source"] = {
        "kind": "blank",
        "width": key.width,
        "height": key.height,
        "fps": [rate.numerator, rate.denominator],
        "length": 2,
    }
    return json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


async def build_engine(
    paths: Paths,
    install: TrtInstall,
    key: EngineKey,
    *,
    script: Path,
    vspipe: str = "vspipe",
    timeout_s: float = BUILD_TIMEOUT_S,
) -> Path:
    """Build the engine for ``key`` and return its folder.

    ``vspipe`` runs ``script`` (a generated template, ``scriptgen.write_script``)
    in build mode on a blank clip of the key's size, in its own process group;
    vsmlrt then calls ``trtexec``. The log goes to
    ``logs_dir/engine-<folder>.log``. Raises ``ButterEyeError(TRT_UNSUPPORTED)``
    when vspipe fails or leaves no engine.
    """
    folder = engine_dir(paths, key)
    if is_ready(folder):
        return folder
    folder.mkdir(parents=True, exist_ok=True)
    user_data = build_user_data(install, key, folder)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = paths.logs_dir / f"engine-{folder.name}.log"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    argv = [vspipe, "-a", f"user_data={user_data}", "-e", "0", os.fspath(script), "--"]
    started = time.monotonic()
    _log.info("building TensorRT engine %s", folder.name)
    async with process_group(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env
    ) as proc:
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            raise ButterEyeError(
                ErrorCode.TRT_UNSUPPORTED,
                M("Building the TensorRT engine took longer than {s} s.", {"s": int(timeout_s)}),
                M("See {log}.", {"log": os.fspath(log_path)}),
            ) from None
    text = out.decode("utf-8", "replace") if out else ""
    with contextlib.suppress(OSError):
        log_path.write_text(text, encoding="utf-8")
    engines = list(folder.glob("*.engine"))
    if proc.returncode != 0 or not engines:
        tail = " | ".join(ln for ln in text.strip().splitlines()[-3:] if ln.strip())
        raise ButterEyeError(
            ErrorCode.TRT_UNSUPPORTED,
            M("The TensorRT engine could not be built: {tail}", {"tail": tail or "no output"}),
            M("See {log}.", {"log": os.fspath(log_path)}),
        )
    seconds = round(time.monotonic() - started, 1)
    ready = {"key": asdict(key), "engine": engines[0].name, "built_s": seconds,
             "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}  # fmt: skip
    (folder / READY).write_text(json.dumps(ready, indent=1) + "\n", encoding="utf-8")
    _log.info("TensorRT engine %s built in %.1f s", folder.name, seconds)
    return folder


__all__ = [
    "DEFAULT_MODEL",
    "MODELS",
    "EngineKey",
    "TrtInstall",
    "available_models",
    "build_engine",
    "build_user_data",
    "engine_dir",
    "find_install",
    "is_ready",
    "model_entry",
    "nvidia_identity",
    "problems",
    "python_dirs",
    "script_settings",
    "streams_for",
]
