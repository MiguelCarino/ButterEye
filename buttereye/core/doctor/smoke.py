# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Setup smoke test (SCOPE §4.7 step 3): a short vspipe run of ``smoke.vpy`` on a
generated clip, bounded by 10 s, followed by an Xid scan for the run's pid (a
run can finish and still have faulted the GPU, M0(f))."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.doctor import gpufault
from buttereye.core.doctor.checks_pkgs import MODELS_SUBDIR
from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import BackendId, Finding, Msg, Paths, Section, Severity

SMOKE_VPY = Path(__file__).with_name("smoke.vpy")
TIMEOUT_S = 10.0
RIFE_THREADS = 4  # gpu_thread=4: ~72 fps at 1080p 2x on the dev box (M0(f))
_OUTPUT = re.compile(r"Output (\d+) frames in ([\d.]+) seconds \(([\d.]+) fps\)")


@dataclass(frozen=True, slots=True)
class SmokeOutcome:
    ok: bool
    fps: float | None
    finding: Finding | None
    log: tuple[str, ...]  # command line + output, for the setup log
    gpu_faults: int | None = None  # None: journal unreadable or not checked


def parse_fps(stderr: str) -> float | None:
    m = _OUTPUT.search(stderr)
    return float(m.group(3)) if m else None


def smoke_argv(
    paths: Paths,
    backend: BackendId,
    *,
    model: str | None,
    gpu_index: int,
    size: str = "1280x720",
    frames: int = 48,
    vspipe: str = "vspipe",
    script: Path = SMOKE_VPY,
) -> tuple[str, ...]:
    args = {
        "backend": "rife" if backend is BackendId.RIFE_NCNN else "mvtools",
        "plugin_dir": str(paths.rpm_plugin_dir),
        "model_dir": str(paths.rpm_data_dir / MODELS_SUBDIR),
        "model": model or "",
        "gpu": str(gpu_index),
        "threads": str(RIFE_THREADS),
        "size": size,
        "frames": str(frames),
    }
    argv = [vspipe, "-p"]
    for k, v in args.items():
        argv += ["-a", f"{k}={v}"]
    return (*argv, str(script), "--")


def _fail(title: Msg, cause: Msg, code: ErrorCode, res: CmdResult | None) -> Finding:
    return Finding(
        id="setup.smoke",
        section=Section.PROBE,
        severity=Severity.DEGRADED,
        code=code,
        title=title,
        cause=cause,
        fix=M("Choose another backend, or run the checks again and look at the details."),
        evidence=res.tail(10) if res is not None else (),
    )


async def smoke_test(
    paths: Paths,
    backend: BackendId,
    *,
    model: str | None,
    gpu_index: int = 0,
    timeout_s: float = TIMEOUT_S,
    size: str = "1280x720",
    frames: int = 48,
    vspipe: str = "vspipe",
    script: Path = SMOKE_VPY,
) -> SmokeOutcome:
    if backend is BackendId.RIFE_TRT:
        f = _fail(
            M("The TensorRT smoke test isn't in this build yet"),
            M("TensorRT engines can't be built by this build."),
            ErrorCode.NOT_IMPLEMENTED,
            None,
        )
        return SmokeOutcome(False, None, f, ())
    argv = smoke_argv(
        paths,
        backend,
        model=model,
        gpu_index=gpu_index,
        size=size,
        frames=frames,
        vspipe=vspipe,
        script=script,
    )
    t0 = time.monotonic()
    res = await run(argv, timeout_s=timeout_s, grace_s=2.0)
    log = (" ".join(argv), *res.stderr.splitlines()[-40:])
    faults: int | None = None
    if backend is BackendId.RIFE_NCNN and res.pid is not None:
        scan = await gpufault.scan_xid(since_monotonic=t0 - 0.5, pids={res.pid})
        faults = scan.count if scan.readable else None
        if scan.readable and scan.count:
            f = gpufault.xid_finding(scan, since_text="during the smoke test")
            f = Finding(
                "setup.smoke",
                Section.PROBE,
                f.severity,
                f.code,
                f.title,
                f.cause,
                f.fix,
                f.commands,
                f.evidence,
            )
            return SmokeOutcome(False, parse_fps(res.stderr), f, log, faults)
    if res.missing:
        f = _fail(
            M("vspipe is not installed"),
            M("The smoke test runs VapourSynth through vspipe."),
            ErrorCode.PKG_MISSING,
            None,
        )
        f = Finding(
            f.id,
            f.section,
            f.severity,
            f.code,
            f.title,
            f.cause,
            f.fix,
            ("sudo dnf install vapoursynth-tools",),
            f.evidence,
        )
        return SmokeOutcome(False, None, f, log, faults)
    if res.timed_out:
        f = _fail(
            M("The smoke test did not finish in {s} s", {"s": int(timeout_s)}),
            M("Interpolating 2 seconds of video took too long."),
            ErrorCode.VSPIPE_FRAME_ERROR,
            res,
        )
        return SmokeOutcome(False, None, f, log, faults)
    fps = parse_fps(res.stderr)
    if not res.ok or fps is None:
        f = _fail(
            M("The smoke test failed"),
            M("vspipe exited with status {rc}.", {"rc": res.returncode or 0}),
            ErrorCode.VSPIPE_FRAME_ERROR,
            res,
        )
        return SmokeOutcome(False, fps, f, log, faults)
    return SmokeOutcome(True, fps, None, log, faults)


__all__ = ["SMOKE_VPY", "SmokeOutcome", "smoke_argv", "smoke_test", "parse_fps"]
