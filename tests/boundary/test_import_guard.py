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
