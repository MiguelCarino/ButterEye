# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Imported 3D LUTs (``.cube``) and the one folder they may be loaded from.

``GeneralSettings.lut`` holds a file *name*, never a path: the file itself lives
in ``<data_dir>/luts``, a folder ButterEye made and only this user can write to.
Importing copies the user's file there (``import_lut``); playing hands mpv the
copy's full path (``resolve_lut``), and the IPC allowlist accepts a ``lut`` value
only when it is such a path (``is_lut_path``), the way ``shaders.ALLOWED`` stands
between ``change-list glsl-shaders`` and an arbitrary file. A LUT is data, not
code, but mpv reads whatever it is pointed at, and a config is something a user
may copy from somewhere else.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

from buttereye.core import osapi
from buttereye.core.types import LUT_SUFFIX, Paths

#: the folder under ``Paths.data_dir``
LUT_DIR_NAME = "luts"
#: a LUT file name: no separators, no leading dot, ``.cube`` (any case) at the end
LUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,122}\.[Cc][Uu][Bb][Ee]")
#: the largest file ``import_lut`` copies. A 65-point cube, the biggest grading
#: tools export by default, is about 8 MiB of text; 129 points is about 60 MiB.
MAX_LUT_BYTES = 64 * 1024 * 1024
#: a ``.cube`` names its size in a ``LUT_3D_SIZE`` line before the table; one that
#: does not within this much of its start is a 1D LUT or not a LUT at all
_HEADER_BYTES = 64 * 1024


class LutError(ValueError):
    """The file cannot be imported; ``str()`` says why, in plain words."""


def lut_dir(paths: Paths) -> Path:
    return paths.data_dir / LUT_DIR_NAME


def valid_lut_name(name: object) -> bool:
    return isinstance(name, str) and LUT_NAME_RE.fullmatch(name) is not None


def _private_dir(folder: Path) -> bool:
    try:
        st = folder.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and not osapi.is_link(st)
        and osapi.owned_by_me(folder, st)
        and not osapi.shared_mode_bits(st)
    )


def _own_file(path: Path) -> bool:
    try:
        st = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and not osapi.is_link(st) and osapi.owned_by_me(path, st)


def resolve_lut(paths: Paths, name: str | None) -> Path | None:
    """The imported LUT called ``name``, or None: no name, a name that is not a
    file name, a file that is missing, a symlink or not ours, or a folder that
    others can write to."""
    if name is None or not valid_lut_name(name):
        return None
    folder = lut_dir(paths)
    path = folder / name
    return path if _private_dir(folder) and _own_file(path) else None


def is_lut_path(value: str) -> bool:
    """May mpv's ``lut`` property be set to ``value``? ``""`` (no LUT) always; else
    an absolute path to an own regular file with a LUT name, directly inside an own,
    private folder called ``luts``. Pure but for two ``lstat`` calls."""
    if value == "":
        return True
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or not valid_lut_name(path.name):
        return False
    return path.parent.name == LUT_DIR_NAME and _private_dir(path.parent) and _own_file(path)


def import_lut(paths: Paths, source: Path) -> str:
    """Copy ``source`` into the LUT folder and return the name to store in
    ``GeneralSettings.lut``. Raises ``LutError`` (not a ``.cube``, too big, no
    ``LUT_3D_SIZE``) or ``OSError``.

    The copy keeps the file's own name when that is a valid LUT name and is not
    taken by a different file; otherwise ``-2``, ``-3``… goes before the suffix.
    Written to a temporary file in the folder and renamed into place, so a
    half-copied LUT is never what mpv loads.
    """
    if source.suffix.lower() != LUT_SUFFIX:
        raise LutError(f"{source.name} is not a .cube file.")
    too_big = LutError(f"{source.name} is larger than {MAX_LUT_BYTES // (1024 * 1024)} MiB.")
    # never more than the cap in memory: a mislabelled multi-GB file stops here,
    # and one that grows between the size check and the read stops at the read
    if source.stat().st_size > MAX_LUT_BYTES:
        raise too_big
    with source.open("rb") as f:
        data = f.read(MAX_LUT_BYTES + 1)
    if len(data) > MAX_LUT_BYTES:
        raise too_big
    if b"LUT_3D_SIZE" not in data[:_HEADER_BYTES]:
        raise LutError(f"{source.name} is not a 3D LUT (it has no LUT_3D_SIZE line).")
    folder = lut_dir(paths)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _private_dir(folder):
        raise LutError(f"{folder} is not a private folder of this user.")
    name = _free_name(folder, source.name, data)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".import-", suffix=LUT_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        osapi.replace(tmp, folder / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return name


def _free_name(folder: Path, original: str, data: bytes) -> str:
    stem = Path(original).stem
    stem = re.sub(r"[^A-Za-z0-9 ._+()-]", "_", stem)
    stem = re.sub(r"^[^A-Za-z0-9]+", "", stem)[:110] or "lut"
    for n in range(1, 1000):
        name = f"{stem}{LUT_SUFFIX}" if n == 1 else f"{stem}-{n}{LUT_SUFFIX}"
        path = folder / name
        try:
            if not path.exists() or (_own_file(path) and path.read_bytes() == data):
                return name
        except OSError:
            continue
    raise LutError(f"too many LUTs called {stem}.")


__all__ = [
    "LUT_DIR_NAME",
    "LUT_NAME_RE",
    "MAX_LUT_BYTES",
    "LutError",
    "import_lut",
    "is_lut_path",
    "lut_dir",
    "resolve_lut",
    "valid_lut_name",
]
