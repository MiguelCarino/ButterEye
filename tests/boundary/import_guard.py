# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A ``sys.meta_path`` finder that refuses banned imports and names the importer.

Used by every GUI test (autouse fixture in ``tests/gui/conftest.py``) and by the
boundary subprocess checks (docs/design/GUI.md §6, §8 risk 4). PySide6 modules
outside the Essentials set the GUI may use, NVIDIA/VapourSynth Python modules
and Qt modules banned by F17/F20 all raise ``BannedImport`` at import time.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import traceback
from collections.abc import Iterable, Sequence
from types import ModuleType

#: Top-level Python modules the GUI process must never load (F17, SCOPE §8.1).
BANNED_TOP = frozenset({"vapoursynth", "vsmlrt", "tensorrt", "pynvml"})
#: PySide6 modules the GUI may use; QtTest is allowed only inside tests.
ALLOWED_QT = frozenset({"QtCore", "QtGui", "QtWidgets"})
TEST_ONLY_QT = frozenset({"QtTest"})
#: Native libraries that must never be mapped into the GUI process.
BANNED_LIBS = ("libnvinfer", "libcuda.so", "libnvidia-ml", "libvapoursynth")

_SKIP_FRAMES = ("importlib", "<frozen")


class BannedImport(ImportError):
    pass


def banned_reason(name: str, *, allow_test_qt: bool = False) -> str | None:
    """Why ``name`` may not be imported in the GUI process, or ``None``."""
    top, _, rest = name.partition(".")
    if top in BANNED_TOP:
        return f"{top} is banned in the GUI process (F17, SCOPE §8.1)"
    if top == "PySide6" and rest:
        sub = rest.partition(".")[0]
        if sub.startswith("Qt"):
            allowed = ALLOWED_QT | (TEST_ONLY_QT if allow_test_qt else frozenset())
            if sub not in allowed:
                return f"PySide6.{sub} is outside {{QtCore, QtGui, QtWidgets}} (F17, R10)"
    return None


def importer_frame() -> str:
    """``file:line in function`` of the frame that triggered the import."""
    for frame in reversed(traceback.extract_stack()[:-1]):
        if frame.filename == __file__ or frame.filename.endswith("/import_guard.py"):
            continue
        if not any(s in frame.filename for s in _SKIP_FRAMES):
            return f"{frame.filename}:{frame.lineno} in {frame.name}"
    return "<unknown>"


class ImportGuard(importlib.abc.MetaPathFinder):
    def __init__(self, *, allow_test_qt: bool = True) -> None:
        self.allow_test_qt = allow_test_qt
        self.violations: list[str] = []

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        reason = banned_reason(fullname, allow_test_qt=self.allow_test_qt)
        if reason is None:
            return None
        where = importer_frame()
        msg = f"banned import {fullname!r} from {where}: {reason}"
        self.violations.append(msg)
        raise BannedImport(msg, name=fullname)

    def install(self) -> ImportGuard:
        if self not in sys.meta_path:
            sys.meta_path.insert(0, self)
        return self

    def remove(self) -> None:
        while self in sys.meta_path:
            sys.meta_path.remove(self)


def loaded_violations(
    modules: Iterable[str] | None = None, *, allow_test_qt: bool = False
) -> list[str]:
    names = sys.modules.keys() if modules is None else modules
    return sorted(n for n in names if banned_reason(n, allow_test_qt=allow_test_qt) is not None)


def mapped_violations(maps_text: str) -> list[str]:
    out: set[str] = set()
    for line in maps_text.splitlines():
        parts = line.split()
        if len(parts) >= 6:
            lib = parts[-1].rsplit("/", 1)[-1]
            if any(lib.startswith(b) for b in BANNED_LIBS):
                out.add(parts[-1])
    return sorted(out)


__all__ = [
    "ALLOWED_QT",
    "BANNED_LIBS",
    "BANNED_TOP",
    "BannedImport",
    "ImportGuard",
    "banned_reason",
    "importer_frame",
    "loaded_violations",
    "mapped_violations",
]
