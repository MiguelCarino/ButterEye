# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``mpvctl.session.play`` hands mpv an absolute path whatever the frontend
passed: a name relative to the caller's working folder, or one under ``~``."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.api import ButterEye
from buttereye.core.mpvctl import launcher
from buttereye.core.mpvctl import session as live
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import DetachPolicy


class _Stop(Exception):
    pass


@pytest.fixture
async def core(xdg_env: Any) -> AsyncIterator[ButterEye]:
    c = await ButterEye.open(sc.fake_paths(env=os.environ))
    try:
        yield c
    finally:
        await c.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=1.0)


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Record the file ``build_argv`` is given, and stop there: no mpv runs."""
    seen: list[Path] = []

    def build_argv(*_a: Any, file: Path, **_k: Any) -> list[str]:
        seen.append(file)
        raise _Stop

    monkeypatch.setattr(launcher, "build_argv", build_argv)
    return seen


@pytest.mark.parametrize("given", ["clip.mkv", "./sub/../clip.mkv"])
async def test_a_relative_file_reaches_mpv_absolute(
    core: ButterEye, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, given: str
) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "clip.mkv").write_bytes(b"")
    monkeypatch.chdir(tmp_path)
    seen = _capture(monkeypatch)
    op: Any = _Op()
    with pytest.raises(_Stop):
        await live.play(core._ctx, Path(given), profile_id=None, op=op)
    assert len(seen) == 1 and seen[0].is_absolute()
    assert seen[0].resolve() == (tmp_path / "clip.mkv").resolve()


async def test_a_home_relative_file_is_expanded(
    core: ButterEye, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "clip.mkv").write_bytes(b"")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    seen = _capture(monkeypatch)
    op: Any = _Op()
    with pytest.raises(_Stop):
        await live.play(core._ctx, Path("~/clip.mkv"), profile_id=None, op=op)
    assert seen == [tmp_path / "clip.mkv"]


class _Op:
    """The one ``OpContext`` method ``play`` uses before mpv starts."""

    def report(self, *_a: Any, **_k: Any) -> None:
        pass
