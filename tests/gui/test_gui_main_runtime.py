# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The window explains a missing Visual C++ runtime on Windows (buttereye/gui/__main__.py)."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from buttereye.gui import __main__ as gui_main


def test_qt_dll_failure_on_windows_explains_the_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gui_main.sys, "platform", "win32")
    exc = ImportError(
        "DLL load failed while importing QtCore: The specified module could not be found."
    )
    text = gui_main.missing_runtime_text(exc)
    assert text is not None and "Visual C++ runtime" in text and gui_main.VCREDIST_URL in text
    assert "Microsoft.VCRedist.2015+.x64" in text


def test_other_failures_are_not_dressed_up(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":  # only Windows has this failure to explain
        assert gui_main.missing_runtime_text(ImportError("DLL load failed")) is None
    monkeypatch.setattr(gui_main.sys, "platform", "win32")
    assert gui_main.missing_runtime_text(ImportError("No module named 'PySide6'")) is None
    assert gui_main.missing_runtime_text(RuntimeError("DLL load failed")) is None


def test_main_shows_the_message_and_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    monkeypatch.setattr(gui_main.sys, "platform", "win32")
    shown: list[str] = []
    monkeypatch.setattr(gui_main, "_message_box", shown.append)
    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    )

    def fake_import(name: str, *a: Any, **k: Any) -> Any:
        if name == "buttereye.gui.app":
            raise ImportError("DLL load failed while importing QtCore")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert gui_main.main([]) == 1
    assert shown and "Visual C++ runtime" in shown[0]
    assert "DLL load failed" in capsys.readouterr().err
