# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""One benchmark pass: run vspipe or mpv on a generated clip and parse its timing.

* vspipe (F14 a): ``vspipe -p -a user_data=<json> -e <N-1> bench.vpy --``. Throughput
  is vspipe's own "Output N frames in X seconds" line, which excludes script
  evaluation (plugin load, Vulkan device and model creation). That evaluation
  time ("Script evaluation done in S seconds") is what repeats on every seek in
  mpv (SCOPE §4.4), so it is reported as the reload time.
* mpv (F14 b): ``mpv --no-config --untimed --vo=null --ao=null
  --vf=vapoursynth=file=bench.vpy:...:user-data=<json>
  av://lavfi:testsrc2=size=WxH:rate=R,format=nv12 --frames=N``. ``--msg-time``
  timestamps give the first filtered frame (startup) and video EOF; throughput is
  ``(N - 1) / (t_eof - t_first)``. A run where mpv disabled the filter (it then
  plays unfiltered, much faster) is a failure, never a number.

Processes are started with ``OpContext.spawn`` (own process group, killed when
the op ends); a timed-out pass terminates its group at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import shutil
import statistics
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core.ops import OpContext, process_group, terminate_group

_log = logging.getLogger(__name__)

TAIL_LINES = 25

_VS_OUTPUT = re.compile(r"Output (\d+) frames in ([0-9.]+) seconds \(([0-9.]+) fps\)")
_VS_EVAL = re.compile(r"Script evaluation done in ([0-9.]+) seconds")
_MPV_TIME = re.compile(r"^\[\s*([0-9]+\.[0-9]+)\]\s?(.*)$")
_MPV_VO = re.compile(r"^VO: \[null\] (\d+)x(\d+) (\S+)")
_MPV_FILTER_FAILED = (
    "Disabling filter vapoursynth",
    "could not init VS",
    "Script evaluation failed",
)


class MeasureFailed(Exception):
    """A pass produced no trustworthy number. ``detail`` is a raw log tail."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class VspipeRun:
    frames: int
    output_s: float
    fps: float
    eval_s: float | None
    wall_s: float


@dataclass(frozen=True, slots=True)
class MpvRun:
    frames: int
    fps: float
    startup_s: float  # mpv start -> first filtered frame shown


# ---------------------------------------------------------------- parsing (pure)
def tail(text: str, n: int = TAIL_LINES) -> str:
    lines = [ln for ln in re.split(r"[\r\n]+", text) if ln.strip()]
    return "\n".join(lines[-n:])


def parse_vspipe(stderr: str, *, wall_s: float = 0.0) -> VspipeRun:
    """Parse vspipe ``-p`` stderr. Raises ``MeasureFailed`` without an Output line."""
    matches = list(_VS_OUTPUT.finditer(stderr))
    if not matches:
        raise MeasureFailed("vspipe printed no result", tail(stderr))
    out = matches[-1]
    frames, secs, fps = int(out.group(1)), float(out.group(2)), float(out.group(3))
    if frames <= 0 or fps <= 0 or not math.isfinite(fps):
        raise MeasureFailed("vspipe output no frames", tail(stderr))
    if secs > 0:
        fps = frames / secs  # the printed fps is rounded to 2 decimals
    ev = _VS_EVAL.search(stderr)
    return VspipeRun(frames, secs, fps, float(ev.group(1)) if ev else None, wall_s)


def parse_mpv(log: str, frames: int) -> MpvRun:
    """Parse ``mpv --msg-time --msg-level=all=v`` output of an ``--frames=N`` run."""
    t_first: float | None = None
    t_eof: float | None = None
    vo_fmt: str | None = None
    for raw in re.split(r"[\r\n]+", log):
        m = _MPV_TIME.match(raw)
        if m is None:
            continue
        t, msg = float(m.group(1)), m.group(2)
        if any(s in msg for s in _MPV_FILTER_FAILED):
            raise MeasureFailed("mpv could not run the benchmark filter", tail(log))
        vo = _MPV_VO.match(msg)
        if vo is not None and vo_fmt is None:
            vo_fmt = vo.group(3)
        if t_first is None and "first video frame after restart shown" in msg:
            t_first = t
        elif "video EOF reached" in msg:
            t_eof = t
    if vo_fmt is None or t_first is None or t_eof is None:
        raise MeasureFailed("mpv showed no filtered video", tail(log))
    if vo_fmt == "nv12":
        # The source is nv12 and the script returns planar YUV: an nv12 VO means
        # the filter is not in the chain.
        raise MeasureFailed("mpv played without the benchmark filter", tail(log))
    span = t_eof - t_first
    if frames < 2 or span <= 0:
        raise MeasureFailed("mpv run too short to time", tail(log))
    return MpvRun(frames, (frames - 1) / span, t_first)


def cov(values: Sequence[float]) -> float:
    """Coefficient of variation (sample stdev / mean); ``inf`` when undefined."""
    if len(values) < 2:
        return math.inf
    mean = statistics.fmean(values)
    if mean <= 0:
        return math.inf
    return statistics.stdev(values) / mean


def mpv_quote(value: str) -> str:
    """mpv's length-prefixed quoting (``%<bytes>%value``) for sub-option values."""
    return f"%{len(value.encode('utf-8'))}%{value}"


def user_data_json(data: Mapping[str, Any]) -> str:
    return json.dumps(dict(data), separators=(",", ":"), sort_keys=True)


def frac_text(f: Fraction) -> str:
    return f"{f.numerator}/{f.denominator}"


# ---------------------------------------------------------------- argv (pure)
def vspipe_argv(vspipe: str, vpy: Path, user_data: Mapping[str, Any], frames: int) -> list[str]:
    return [
        vspipe,
        "-p",
        "-a",
        f"user_data={user_data_json(user_data)}",
        "-e",
        str(frames - 1),
        str(vpy),
        "--",
    ]


def mpv_argv(
    mpv: str,
    vpy: Path,
    user_data: Mapping[str, Any],
    *,
    width: int,
    height: int,
    source_fps: Fraction,
    frames: int,
    concurrent_frames: int,
    buffered_frames: int = 4,
) -> list[str]:
    vf = (
        f"vapoursynth=file={mpv_quote(str(vpy))}"
        f":buffered-frames={buffered_frames}:concurrent-frames={concurrent_frames}"
        f":user-data={mpv_quote(user_data_json(user_data))}"
    )
    src = f"av://lavfi:testsrc2=size={width}x{height}:rate={frac_text(source_fps)},format=nv12"
    return [
        mpv,
        "--no-config",
        "--untimed",
        "--vo=null",
        "--ao=null",
        "--no-input-terminal",
        "--msg-time",
        "--msg-level=all=v",
        "--term-status-msg=",
        f"--frames={frames}",
        f"--vf={vf}",
        "--",
        src,
    ]


# ---------------------------------------------------------------- running
async def _run(
    op: OpContext,
    argv: Sequence[str],
    timeout_s: float,
    env: Mapping[str, str] | None,
    vram: VramSampler | None = None,
    pids: set[int] | None = None,
) -> tuple[int, str, float]:
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    proc = await op.spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if pids is not None:
        pids.add(proc.pid)
    sampler = asyncio.ensure_future(vram.watch(proc.pid)) if vram is not None else None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        await terminate_group(proc)
        name = Path(argv[0]).name
        raise MeasureFailed(f"{name} did not finish within {timeout_s:.0f} s") from None
    finally:
        if sampler is not None:
            sampler.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sampler
    text = out.decode("utf-8", "replace") if out else ""
    rc = proc.returncode if proc.returncode is not None else -1
    return rc, text, loop.time() - t0


async def run_vspipe(
    op: OpContext,
    argv: Sequence[str],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    vram: VramSampler | None = None,
    pids: set[int] | None = None,
) -> VspipeRun:
    rc, text, wall = await _run(op, argv, timeout_s, env, vram, pids)
    if rc != 0:
        raise MeasureFailed(f"vspipe exited with status {rc}", tail(text))
    return parse_vspipe(text, wall_s=wall)


async def run_mpv(
    op: OpContext,
    argv: Sequence[str],
    *,
    frames: int,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    pids: set[int] | None = None,
) -> MpvRun:
    rc, text, _wall = await _run(op, argv, timeout_s, env, None, pids)
    if rc != 0:
        raise MeasureFailed(f"mpv exited with status {rc}", tail(text))
    return parse_mpv(text, frames)


# ---------------------------------------------------------------- VRAM (NVIDIA only)
def parse_compute_apps(text: str, pid: int) -> int | None:
    """``nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits``
    -> bytes used by ``pid`` (None when not listed)."""
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2:
            continue
        try:
            if int(parts[0]) == pid:
                return int(parts[1]) * 1024 * 1024
        except ValueError:
            continue
    return None


class VramSampler:
    """Peak VRAM of one process via the ``nvidia-smi`` subprocess (SCOPE §4.5).

    Optional: without ``nvidia-smi`` the value stays None (not measured). AMD/Intel
    VRAM per process is not measured in v1.
    """

    def __init__(self, nvidia_smi: str | None, *, interval_s: float = 0.5) -> None:
        self._bin = nvidia_smi
        self._interval = interval_s
        self.peak: int | None = None

    @classmethod
    def detect(cls) -> VramSampler | None:
        exe = shutil.which("nvidia-smi")
        return cls(exe) if exe else None

    async def _sample(self, pid: int) -> int | None:
        assert self._bin is not None
        argv = [self._bin, "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"]
        async with process_group(argv, stdout=subprocess.PIPE, grace_s=1.0) as proc:
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            except TimeoutError:
                return None
        return parse_compute_apps(out.decode("utf-8", "replace"), pid) if out else None

    async def watch(self, pid: int) -> None:
        if self._bin is None:
            return
        while True:
            await asyncio.sleep(self._interval)
            try:
                used = await self._sample(pid)
            except OSError as exc:
                _log.info("nvidia-smi failed: %s", exc)
                return
            if used is not None and (self.peak is None or used > self.peak):
                self.peak = used


__all__ = [
    "MeasureFailed",
    "VspipeRun",
    "MpvRun",
    "VramSampler",
    "tail",
    "parse_vspipe",
    "parse_mpv",
    "parse_compute_apps",
    "cov",
    "mpv_quote",
    "user_data_json",
    "frac_text",
    "vspipe_argv",
    "mpv_argv",
    "run_vspipe",
    "run_mpv",
]
