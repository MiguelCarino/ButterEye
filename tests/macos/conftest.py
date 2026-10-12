# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Fixtures the macOS tests share. Collect-safe on every platform: nothing here
imports a macOS module, and the tests themselves skip off Darwin.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory a Unix socket can be bound in.

    ``sun_path`` is 104 bytes on macOS, so 103 is the longest path ``bind``
    accepts. ``tmp_path`` is 119 bytes before a file name is added here, under
    ``/var/folders/<hash>/T/pytest-of-<user>/...``, and binding in it fails with
    ``OSError: AF_UNIX path too long`` - which reads like a bug in the test
    rather than a limit of the platform. ``/tmp`` keeps the whole path near 20.

    ``mkdtemp`` makes it 0700 and owned by us, which is what a runtime directory
    holding an mpv socket has to be for ``ipc.check_socket_path`` to pass.
    """
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="be-t-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
