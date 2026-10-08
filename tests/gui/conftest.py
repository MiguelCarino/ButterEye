# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shared GUI-test fixtures (U4): offscreen Qt, the import guard, FakeCore
bridges and main windows.

Every GUI test runs with ``QT_QPA_PLATFORM=offscreen QT_ACCESSIBILITY=1`` and
the meta-path import guard installed (docs/design/GUI.md §6, §8 risk 4). Core
loops run with asyncio debug and ``slow_callback_duration=0.1``; a slow-callback
warning fails the test unless it opted out with ``allow_slow_callbacks``.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_ACCESSIBILITY", "1")

import pytest  # noqa: E402

from tests.boundary.import_guard import ImportGuard  # noqa: E402


@pytest.fixture(autouse=True)
def import_guard() -> Iterator[ImportGuard]:
    guard = ImportGuard(allow_test_qt=True).install()
    try:
        yield guard
    finally:
        guard.remove()
    assert not guard.violations, guard.violations


@pytest.fixture(autouse=True)
def source_plurals(qapp: Any) -> None:
    """English numerus forms, as ``app.install_translators`` installs them."""
    from buttereye.gui.a11y import install_source_plurals

    install_source_plurals(qapp)


class _SlowCallbacks(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    #: asyncio debug logs callbacks over 0.1 s; on a loaded machine debug-mode
    #: overhead alone can cross that, so only clearly blocking callbacks fail.
    FAIL_OVER_S = 0.3

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "took" in msg and "Executing" in msg:
            m = re.search(r"took ([0-9.]+) seconds", msg)
            if m is None or float(m.group(1)) > self.FAIL_OVER_S:
                self.records.append(msg)


@pytest.fixture
def slow_callbacks(request: pytest.FixtureRequest) -> Iterator[_SlowCallbacks]:
    handler = _SlowCallbacks()
    log = logging.getLogger("asyncio")
    log.addHandler(handler)
    try:
        yield handler
    finally:
        log.removeHandler(handler)
    if handler.records and request.node.get_closest_marker("allow_slow_callbacks") is None:
        pytest.fail("slow core-loop callbacks: " + "; ".join(handler.records))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_slow_callbacks: the test blocks the core loop on purpose"
    )


@pytest.fixture
def make_bridge(qtbot: Any, slow_callbacks: _SlowCallbacks) -> Iterator[Callable[..., Any]]:
    """``make_bridge("live_active", start=True, **bridge_kwargs)`` -> started CoreBridge."""
    from buttereye.core.testing import FakeCore
    from buttereye.gui.bridge import CoreBridge

    made: list[CoreBridge] = []

    def factory(scenario: str | Any = "devbox_now", *, start: bool = True, **kw: Any) -> Any:
        holder: dict[str, Any] = {}
        cls = FakeCore.for_scenario(scenario)

        async def opener(paths: Any) -> Any:
            core = await cls.open(paths)
            holder["core"] = core
            return core

        kw.setdefault("debug", True)
        bridge = CoreBridge(opener=opener, **kw)
        bridge.fake = holder  # type: ignore[attr-defined]
        made.append(bridge)
        if start:
            with qtbot.waitSignal(bridge.ready, timeout=5000):
                bridge.start()
        return bridge

    yield factory
    from buttereye.core.api import DetachPolicy

    for b in made:
        if not b.is_closed:
            b.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=2.0)


@pytest.fixture
def make_window(qtbot: Any, make_bridge: Callable[..., Any], tmp_path: Path) -> Callable[..., Any]:
    """``make_window("all_ready")`` -> shown MainWindow over a ready FakeCore bridge."""
    from PySide6.QtCore import QSettings

    from buttereye.gui.main_window import MainWindow

    def factory(
        scenario: str | Any = "devbox_now",
        *,
        open_setup_when_missing: bool = False,
        show: bool = True,
        registry: Any = None,
        **bridge_kw: Any,
    ) -> Any:
        bridge = make_bridge(scenario, **bridge_kw)
        settings = QSettings(str(tmp_path / f"gui-{id(bridge)}.ini"), QSettings.Format.IniFormat)
        win = MainWindow(
            bridge,
            settings,
            log_path=tmp_path / "gui.log",
            open_setup_when_missing=open_setup_when_missing,
            **({"registry": registry} if registry is not None else {}),
        )
        win._test_allow_close = True  # type: ignore[attr-defined]
        qtbot.addWidget(win, before_close_func=_force_close)
        if show:
            win.show()
            qtbot.waitExposed(win)
        qtbot.wait(150)  # startup replies + one drain
        return win

    return factory


def _force_close(win: Any) -> None:
    """Teardown: never block on the quit dialogs."""
    win._may_close = True
