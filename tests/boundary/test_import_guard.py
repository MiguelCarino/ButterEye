# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The meta-path import guard (docs/design/GUI.md §6 ``test_import_guard``)."""

from __future__ import annotations

import importlib
import sys

import pytest

from tests.boundary.import_guard import (
    BannedImport,
    ImportGuard,
    banned_reason,
    loaded_violations,
    mapped_images,
    mapped_violations,
)


def _import_banned_from_here(name: str) -> None:
    importlib.import_module(name)


@pytest.mark.parametrize(
    "name",
    ["PySide6.QtNetwork", "PySide6.QtCharts", "PySide6.QtDBus", "vapoursynth", "tensorrt.foo"],
)
def test_guard_names_the_importing_frame(name: str) -> None:
    saved = sys.modules.pop(name, None)
    top = name.split(".")[0]
    saved_top = sys.modules.pop(top, None) if top != "PySide6" else None
    guard = ImportGuard(allow_test_qt=True).install()
    try:
        with pytest.raises(BannedImport) as exc:
            _import_banned_from_here(name)
    finally:
        guard.remove()
        if saved is not None:
            sys.modules[name] = saved
        if saved_top is not None:
            sys.modules[top] = saved_top
    msg = str(exc.value)
    assert name.split(".")[0] in msg
    assert "test_import_guard.py" in msg and "_import_banned_from_here" in msg
    assert guard.violations == [msg]


def test_allowed_modules_pass() -> None:
    guard = ImportGuard(allow_test_qt=True).install()
    try:
        for name in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets", "PySide6.QtTest"):
            importlib.import_module(name)
        importlib.import_module("PySide6.support")
    finally:
        guard.remove()
    assert guard.violations == []


def test_qttest_banned_outside_tests() -> None:
    assert banned_reason("PySide6.QtTest", allow_test_qt=False) is not None
    assert banned_reason("PySide6.QtTest", allow_test_qt=True) is None
    assert banned_reason("PySide6.support.signature") is None
    assert banned_reason("vsmlrt") is not None
    assert banned_reason("pynvml") is not None


def test_loaded_and_mapped_violations() -> None:
    assert loaded_violations(["PySide6.QtCore", "PySide6.QtQuick", "json"]) == ["PySide6.QtQuick"]
    maps = (
        "7f00-7f01 r-xp 00000000 00:1f 123 /usr/lib64/libEGL_nvidia.so.615.71.09\n"
        "7f02-7f03 r-xp 00000000 00:1f 124 /usr/lib64/libcuda.so.615.71.09\n"
        "7f04-7f05 r-xp 00000000 00:1f 125 /usr/lib64/libvapoursynth-script.so.0\n"
    )
    assert mapped_violations(maps) == [
        "/usr/lib64/libcuda.so.615.71.09",
        "/usr/lib64/libvapoursynth-script.so.0",
    ]


def test_mapped_violations_reads_a_dyld_image_list() -> None:
    """macOS gives one path per line, so the six-field rule alone would drop every
    line and report no violation without having checked anything. The names differ
    too: ``.dylib``, and VapourSynth's extension module without a ``lib`` prefix."""
    images = "\n".join(
        (
            "/usr/lib/libSystem.B.dylib",
            "/opt/homebrew/lib/libvapoursynth-script.dylib",
            "/opt/homebrew/lib/python3.14/site-packages/vapoursynth.so",
            "/Users/me/My Frameworks/libnvinfer.9.dylib",
            "/System/Library/Frameworks/OpenGL.framework/Versions/A/OpenGL",
        )
    )
    assert mapped_violations(images) == [
        "/Users/me/My Frameworks/libnvinfer.9.dylib",
        "/opt/homebrew/lib/libvapoursynth-script.dylib",
        "/opt/homebrew/lib/python3.14/site-packages/vapoursynth.so",
    ]


def test_mapped_violations_sees_a_deleted_mapping() -> None:
    """Linux appends " (deleted)" to a mapping whose file has been unlinked, so the
    path sits one field from the end. Reading only the last field made a process with
    TensorRT still mapped look clean, which is the one thing F17 must never do."""
    maps = (
        "7f5555444000-7f5555445000 r-xp 00000000 fd:00 6665  /tmp/libfoo.so (deleted)\n"
        "7f5555555000-7f5555666000 r-xp 00000000 fd:00 6666  /tmp/libnvinfer.so.9 (deleted)\n"
        "7f5555777000-7f5555778000 r-xp 00000000 fd:00 6667  /usr/lib64/libcuda.so.1\n"
    )
    assert mapped_violations(maps) == ["/tmp/libnvinfer.so.9", "/usr/lib64/libcuda.so.1"]


_BANNED_BASENAME = "libcuda.so.615.71.09"


def _rename_mapped_file(line: str, basename: str) -> str:
    """``line`` with the file it names renamed, leaving the rest of its shape alone."""
    head, slash, _ = line.rstrip().rpartition("/")
    return f"{head}{slash}{basename}"


def test_mapped_images_is_in_the_shape_mapped_violations_reads() -> None:
    """The two halves have to agree on this platform, or ``test_gui_import_ban``'s
    maps check passes without checking (it feeds one into the other).

    So the assertion has to be about a line that came out of the producer: every line
    naming a file is renamed to a banned library and must then be reported. Appending
    a path of our own instead would hold for any producer shape at all -- including
    shapes the parser drops on the floor -- which is how the mismatch stayed hidden.
    """
    text = mapped_images()
    assert text.strip(), text
    named = [line for line in text.splitlines() if "/" in line]
    assert named, text
    for line in named:
        renamed = _rename_mapped_file(line, _BANNED_BASENAME)
        # Compare basenames, not whole paths: a path holding spaces survives the ban
        # check (it reads the basename) but is reported clipped to its last field.
        found = [p for p in mapped_violations(renamed) if p.rsplit("/", 1)[-1] == _BANNED_BASENAME]
        assert found, (line, renamed, mapped_violations(renamed))
