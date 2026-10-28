# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Stopping a render on Windows (B11): ``_win32.suspend_pids`` and
``resume_pids``, and the render's ``_Pause`` over a real process tree and a
sibling, as vspipe and ffmpeg are. Each child counts ticks into a file; a
suspended one stops counting.

Skipped everywhere but Windows (``tools/wine-tests.sh`` runs them under Wine).
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if sys.platform == "win32":
    from buttereye.core import osapi
    from buttereye.core.osapi import _win32 as w
    from buttereye.core.render import jobs

#: write a growing count to argv[1] every 20 ms
TICKER = (
    "import sys, time\n"
    "n = 0\n"
    "while True:\n"
    "    n += 1\n"
    "    open(sys.argv[1], 'w').write(str(n))\n"
    "    time.sleep(0.02)\n"
)


def _ticks(path: Path) -> int:
    try:
        return int(path.read_text() or 0)
    except OSError, ValueError:
        return 0


def _counting(path: Path, timeout_s: float = 10.0) -> bool:
    """Whether the count at ``path`` moves within a second (or ``timeout_s`` for
    the first tick)."""
    deadline = time.monotonic() + timeout_s
    while _ticks(path) == 0 and time.monotonic() < deadline:
        time.sleep(0.05)
    before = _ticks(path)
    time.sleep(1.0)
    return _ticks(path) > before


def _ticker(path: Path, *, parent: bool = False) -> subprocess.Popen[bytes]:
    """A ticker, or (``parent``) a process that starts one and waits for it, so
    the ticker is a grandchild."""
    argv = [sys.executable, "-c", TICKER, str(path)]
    if parent:
        argv = [sys.executable, "-c",
                f"import subprocess; subprocess.Popen({argv!r}).wait()"]  # fmt: skip
    return subprocess.Popen(argv)


def _end(*procs: subprocess.Popen[bytes]) -> None:
    for p in procs:
        for pid in w.tree(p.pid):
            w.terminate(pid)
        p.wait(10)


def test_suspend_stops_a_process_and_resume_continues_it(tmp_path: Path) -> None:
    tick = tmp_path / "tick"
    proc = _ticker(tick)
    try:
        assert _counting(tick)
        held = w.suspend_pids([proc.pid, proc.pid])  # a pid named twice is suspended once
        assert [pid for pid, _ in held] == [proc.pid]
        time.sleep(0.2)
        assert not _counting(tick)
        assert w.resume_pids(held) == 1
        assert _counting(tick)
        # a process that isn't the one suspended (another creation time) is left alone
        assert w.resume_pids([(proc.pid, held[0][1] + 1)]) == 0
    finally:
        _end(proc)


def test_a_gone_pid_is_skipped(tmp_path: Path) -> None:
    proc = _ticker(tmp_path / "tick")
    _end(proc)
    assert w.suspend_pids([proc.pid]) == []


def test_pause_stops_vspipes_tree_and_ffmpeg(tmp_path: Path) -> None:
    grand, sibling = tmp_path / "grandchild", tmp_path / "sibling"
    vs = _ticker(grand, parent=True)  # vspipe and a process under it
    ff = _ticker(sibling)  # ffmpeg: started beside vspipe, not under it
    try:
        assert _counting(grand) and _counting(sibling)
        assert osapi.IS_WINDOWS
        pause = jobs._Pause(vs.pid, (ff.pid,))
        pause.stop()
        assert len(pause.held) == 3
        time.sleep(0.2)
        assert not _counting(grand) and not _counting(sibling)
        pause.resume()
        assert pause.held == []
        assert _counting(grand) and _counting(sibling)
    finally:
        _end(vs, ff)
