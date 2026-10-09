# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Memory failsafes for the test suite and the dev tools (stdlib only).

The dev box is shared: a runaway test, mpv or vspipe must never push the whole
machine into swap or the OOM killer. Three layers:

1. **Pre-flight.** Refuse to start when the system has less than
   ``min_start_mib`` available (``MemAvailable`` in ``/proc/meminfo``).
2. **Hard cap.** Run the current command again inside a transient systemd user
   scope with ``MemoryMax`` (no ``MemoryHigh``: without swap, its throttling
   stalls a run instead of ending it) (and ``MemorySwapMax=0``, so it cannot spill into
   swap instead). The kernel then kills only this command and its children
   when they exceed the cap; nothing else on the machine is touched.
3. **Watchdog.** A thread that polls ``MemAvailable`` and, when the *system*
   falls below ``floor_mib`` (other programs count too), kills every process
   this command started and exits with status 137.

Environment overrides (all optional):

- ``BUTTEREYE_MEM_MAX_MIB``    cap for the scope (default: per caller)
- ``BUTTEREYE_MEM_FLOOR_MIB``  watchdog floor (default 3072)
- ``BUTTEREYE_MEM_START_MIB``  pre-flight minimum (default 4096)
- ``BUTTEREYE_NO_MEMCAP=1``    skip the scope (pre-flight and watchdog stay)
- ``BUTTEREYE_MEMCAP``         set inside the scope; prevents re-running twice
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

DEFAULT_FLOOR_MIB = 3072
DEFAULT_START_MIB = 4096
EXIT_KILLED = 137


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def meminfo_mib(key: str) -> int | None:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith(key + ":"):
                return int(line.split()[1]) // 1024
    except OSError, ValueError, IndexError:
        pass
    return None


def available_mib() -> int | None:
    return meminfo_mib("MemAvailable")


def floor_mib() -> int:
    return _env_int("BUTTEREYE_MEM_FLOOR_MIB", DEFAULT_FLOOR_MIB)


def start_mib() -> int:
    return _env_int("BUTTEREYE_MEM_START_MIB", DEFAULT_START_MIB)


def max_mib(default: int) -> int:
    return _env_int("BUTTEREYE_MEM_MAX_MIB", default)


# ---------------------------------------------------------------- pre-flight
def preflight() -> str | None:
    """None when there is room to start, else a one-line reason."""
    avail = available_mib()
    need = start_mib()
    if avail is not None and avail < need:
        return (
            f"only {avail} MiB of memory is available, {need} MiB is needed to start "
            "(close something, or set BUTTEREYE_MEM_START_MIB)"
        )
    return None


# ---------------------------------------------------------------- hard cap
def capped() -> bool:
    return bool(os.environ.get("BUTTEREYE_MEMCAP"))


def _scope_works() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    try:
        res = subprocess.run(
            ["systemd-run", "--user", "--scope", "-q", "--collect", "--", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return res.returncode == 0


def reexec_capped(argv: Sequence[str], cap_mib: int, label: str) -> str | None:
    """Run ``argv`` inside a capped scope, then exit with its status.

    Returns only when it did not run it: with the reason (already capped,
    disabled, or no systemd user session). Otherwise this process waits (it
    holds no test state, only the interpreter) and exits with the child's
    status, explaining a kill by the cap, which the kernel does silently.
    """
    if capped():
        return "already inside a capped scope"
    if os.environ.get("BUTTEREYE_NO_MEMCAP") == "1":
        return "BUTTEREYE_NO_MEMCAP=1"
    if not _scope_works():
        return "no systemd user session for a capped scope"
    env = dict(os.environ, BUTTEREYE_MEMCAP=str(cap_mib))
    cmd = [
        "systemd-run",
        "--user",
        "--scope",
        "-q",
        "--collect",
        f"--unit=buttereye-{label}-{os.getpid()}",
        "-p",
        f"MemoryMax={cap_mib}M",
        "-p",
        "MemorySwapMax=0",
        "--",
        *argv,
    ]
    sys.stdout.flush()
    sys.stderr.flush()
    # Ctrl+C reaches the child through the terminal's process group; wait for it
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    rc = subprocess.call(cmd, env=env)
    if rc in (EXIT_KILLED, -signal.SIGKILL):
        os.write(
            2,
            (
                f"\nmemory failsafe: the run was killed at its {cap_mib} MiB cap "
                "(raise it with BUTTEREYE_MEM_MAX_MIB if that use is expected).\n"
            ).encode(),
        )
        rc = EXIT_KILLED
    os._exit(rc if rc >= 0 else 128 - rc)


# ---------------------------------------------------------------- watchdog
def descendants(root: int) -> list[int]:
    """Every live process below ``root`` (by parent pid), deepest first."""
    children: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            ppid = int(stat[stat.rindex(")") + 2 :].split()[1])
        except OSError, ValueError, IndexError:
            continue
        children.setdefault(ppid, []).append(int(entry.name))
    out: list[int] = []
    stack = [root]
    while stack:
        pid = stack.pop()
        for child in children.get(pid, ()):
            out.append(child)
            stack.append(child)
    return out[::-1]


def kill_descendants(root: int | None = None) -> int:
    killed = 0
    for pid in descendants(root if root is not None else os.getpid()):
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed


class Watchdog:
    """Kills this process tree when the system's available memory drops too low."""

    def __init__(
        self,
        floor: int | None = None,
        *,
        interval_s: float = 0.5,
        label: str = "ButterEye",
        on_breach: Callable[[int], None] | None = None,
    ) -> None:
        self.floor = floor if floor is not None else floor_mib()
        self.interval_s = interval_s
        self.label = label
        self.on_breach = on_breach or self._abort
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="memguard", daemon=True)
        self.lowest: int | None = None

    def start(self) -> Watchdog:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2)

    def __enter__(self) -> Watchdog:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            avail = available_mib()
            if avail is None:
                continue
            if self.lowest is None or avail < self.lowest:
                self.lowest = avail
            if avail < self.floor:
                self.on_breach(avail)
                return

    def _abort(self, avail: int) -> None:
        killed = kill_descendants()
        msg = (
            f"\n{self.label}: memory failsafe tripped: only {avail} MiB available "
            f"(floor {self.floor} MiB); killed {killed} child process(es) and stopped.\n"
        )
        try:
            os.write(2, msg.encode())
        finally:
            os._exit(EXIT_KILLED)


__all__ = [
    "EXIT_KILLED",
    "Watchdog",
    "available_mib",
    "capped",
    "descendants",
    "kill_descendants",
    "max_mib",
    "preflight",
    "reexec_capped",
]
