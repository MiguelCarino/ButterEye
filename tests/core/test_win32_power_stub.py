# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``_win32.begin_activity``/``end_activity`` over stub DLLs, on any platform.

``_win32`` is imported fresh (not into ``sys.modules``) with ``ctypes.WinDLL``
replaced by stubs whose functions answer what each test says, so the branches
the real API rarely takes -- a refused request, a refused ``SystemRequired``,
``ExecutionRequired`` missing before Windows 8 -- run here too.
``tests/windows/test_power_windows.py`` makes a real request.
"""

from __future__ import annotations

import ctypes
import importlib.util
import sys
import types
from collections.abc import Callable
from typing import Any

import pytest

from buttereye.core import osapi


class _Fn:
    """A stub DLL function: records each call and answers ``answer(*args)``."""

    def __init__(self, name: str, calls: list[tuple[Any, ...]]) -> None:
        self.name, self.calls = name, calls
        self.answer: Callable[..., Any] = lambda *_a: 1

    def __call__(self, *args: Any) -> Any:
        self.calls.append((self.name, *args))
        return self.answer(*args)


class _DLL:
    def __init__(self, calls: list[tuple[Any, ...]]) -> None:
        self._calls = calls
        self._fns: dict[str, _Fn] = {}

    def __getattr__(self, name: str) -> _Fn:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._fns.setdefault(name, _Fn(name, self._calls))


@pytest.fixture
def win(monkeypatch: pytest.MonkeyPatch) -> tuple[types.ModuleType, _DLL, list[tuple[Any, ...]]]:
    """A fresh ``_win32`` over stub DLLs, its kernel32 stub, and every call made."""
    calls: list[tuple[Any, ...]] = []
    k32 = _DLL(calls)
    dlls = {"kernel32": k32}
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda name, **_k: dlls.setdefault(name, _DLL(calls)), raising=False
    )
    if not hasattr(ctypes, "WINFUNCTYPE"):
        monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    origin = osapi.__file__.replace("__init__.py", "_win32.py")
    spec = importlib.util.spec_from_file_location("_win32_stubbed", origin)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.undo()  # pytest asks sys.platform too; the module has what it needs
    calls.clear()
    return module, k32, calls


def _sets(calls: list[tuple[Any, ...]], fn: str) -> list[int]:
    return [c[2] for c in calls if c[0] == fn]


def test_a_request_sets_both_and_ends_clearing_both(win: Any) -> None:
    w, k32, calls = win
    k32.PowerCreateRequest.answer = lambda _ctx: 0x2A
    assert w.begin_activity("Saving a smooth copy") == 0x2A
    ctx = calls[0][1]._obj  # the byref'd REASON_CONTEXT
    assert ctx.Version == 0 and ctx.Flags == 1
    assert _sets(calls, "PowerSetRequest") == [1, 3]
    assert not any(c[0] == "CloseHandle" for c in calls)
    calls.clear()
    w.end_activity(0x2A)
    assert _sets(calls, "PowerClearRequest") == [1, 3]
    assert calls[-1] == ("CloseHandle", 0x2A)


@pytest.mark.parametrize("refused", [None, 0, ctypes.c_void_p(-1).value])
def test_a_refused_request_is_none(win: Any, refused: int | None) -> None:
    w, k32, calls = win
    k32.PowerCreateRequest.answer = lambda _ctx: refused
    assert w.begin_activity("x") is None
    assert _sets(calls, "PowerSetRequest") == []


def test_a_refused_system_required_closes_the_request(win: Any) -> None:
    w, k32, calls = win
    k32.PowerCreateRequest.answer = lambda _ctx: 0x2A
    k32.PowerSetRequest.answer = lambda _h, _kind: 0
    assert w.begin_activity("x") is None
    assert calls[-1] == ("CloseHandle", 0x2A)


def test_execution_required_missing_keeps_the_request(win: Any) -> None:
    # before Windows 8 PowerSetRequest(ExecutionRequired) fails; SystemRequired holds
    w, k32, calls = win
    k32.PowerCreateRequest.answer = lambda _ctx: 0x2A
    k32.PowerSetRequest.answer = lambda _h, kind: int(kind == 1)
    assert w.begin_activity("x") == 0x2A
    assert not any(c[0] == "CloseHandle" for c in calls)
