# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Run a read-only helper program with a timeout (doctor, hardware, licences).

Every program runs through ``asyncio.create_subprocess_exec`` in its own process
group (``ops.process_group``): no shell, no ``preexec_fn``, the whole group is
terminated on timeout or cancellation. ``LC_ALL=C`` keeps output parseable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.ops import process_group


@dataclass(frozen=True, slots=True)
class CmdResult:
    argv: tuple[str, ...]
    returncode: int | None  # None: not started (missing) or killed on timeout
    stdout: str
    stderr: str
    missing: bool = False  # executable not found
    timed_out: bool = False
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, n: int = 8) -> tuple[str, ...]:
        """Last ``n`` non-empty stderr (else stdout) lines, for Finding.evidence."""
        text = self.stderr if self.stderr.strip() else self.stdout
        lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
        return tuple(lines[-n:])


def c_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env.pop("LANGUAGE", None)
    if extra:
        env.update(extra)
    return env


async def run(
    argv: Sequence[str],
    *,
    timeout_s: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    grace_s: float = 1.0,
) -> CmdResult:
    """Run ``argv`` and capture its output; never raises for a failing program."""
    args = tuple(argv)
    exe = args[0]
    if os.sep not in exe and shutil.which(exe) is None:
        return CmdResult(args, None, "", "", missing=True)
    try:
        async with process_group(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=c_env(env),
            cwd=cwd,
            grace_s=grace_s,
        ) as proc:
            try:
                out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
            except TimeoutError:
                return CmdResult(args, None, "", "", timed_out=True, pid=proc.pid)
            return CmdResult(
                args,
                proc.returncode,
                out.decode("utf-8", "replace"),
                err.decode("utf-8", "replace"),
                pid=proc.pid,
            )
    except FileNotFoundError, PermissionError:
        return CmdResult(args, None, "", "", missing=True)


__all__ = ["CmdResult", "run", "c_env"]
