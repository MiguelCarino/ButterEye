# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The GUI over the real core on the dev box (docs/design/GUI.md §6 ``test_gui_devbox``).

Real findings on the System page, real sizes on Storage, real files on About.
Runs the real doctor once (< 10 s, includes the in-mpv probe), in temporary
XDG directories. Pages that are not in this build yet are skipped, not faked.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_ACCESSIBILITY", "1")

from buttereye.core.api import (  # noqa: E402
    ButterEyeError,
    DetachPolicy,
    DoctorReport,
    ErrorCode,
    Feature,
)

pytestmark = [pytest.mark.devbox, pytest.mark.integration, pytest.mark.gpu]


@pytest.fixture
def real_window(qtbot: Any, xdg_env: Any, tmp_path: Path) -> Any:
    from PySide6.QtCore import QSettings

    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.main_window import MainWindow

    bridge = CoreBridge()  # the shipped opener: ButterEye.open() with real providers
    settings = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    win = MainWindow(bridge, settings, log_path=tmp_path / "gui.log", open_setup_when_missing=False)
    qtbot.addWidget(win, before_close_func=lambda w: setattr(w, "_may_close", True))
    fatal: list[ButterEyeError] = []
    bridge.fatal.connect(fatal.append)
    bridge.start()
    qtbot.waitUntil(lambda: bridge.is_ready or bool(fatal), timeout=10_000)
    if fatal:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)
        pytest.fail(f"real core failed to open: {fatal[0].code.code} {fatal[0].cause}")
    win.show()
    yield win
    bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)


def _needs(win: Any, feature: Feature) -> None:
    caps = win.bridge.capabilities
    if caps is None or not caps.ok(feature):
        pytest.skip(f"{feature.value} is not in this build yet")


def test_real_doctor_through_the_bridge(real_window: Any, qtbot: Any) -> None:
    win = real_window
    _needs(win, Feature.DOCTOR)
    reports: list[DoctorReport] = []
    errors: list[ButterEyeError] = []
    win.bridge.run_op(lambda core: core.doctor(), owner=win, ok=reports.append, err=errors.append)
    qtbot.waitUntil(lambda: bool(reports or errors), timeout=20_000)
    assert not errors, errors
    report = reports[0]
    codes = {f.code for f in report.findings}
    # doctor reports ffms2 exactly when its library is absent on this machine
    ffms2 = Path("/usr/lib64/libffms2.so.5").exists()
    assert (ErrorCode.FFMS2_MISSING in codes) == (not ffms2)
    assert report.duration_s < 10.0
    caps = win.bridge.capabilities
    assert caps is not None
    if not ffms2:
        qtbot.waitUntil(
            lambda: win.bridge.capabilities.states[Feature.RENDER].available is False, timeout=2000
        )
    else:
        qtbot.wait(500)
        assert win.bridge.capabilities.states[Feature.RENDER].code is not ErrorCode.FFMS2_MISSING


@pytest.mark.parametrize("page_id", ["system", "storage", "about"])
def test_real_pages_show_real_data(real_window: Any, qtbot: Any, page_id: str) -> None:
    from buttereye.gui.pages import module_present, spec_for
    from buttereye.gui.pages.base import MissingPage

    win = real_window
    spec = spec_for(page_id)
    if not module_present(spec):
        pytest.skip(f"{spec.module} is not in this build yet")
    win.go(page_id)
    page = win.page(page_id)
    assert page is not None and not isinstance(page, MissingPage)
    qtbot.wait(3000 if page_id == "system" else 1000)
    panel = page.state_panel()
    if panel is not None:
        qtbot.waitUntil(lambda: panel.state != "loading", timeout=15_000)
        assert panel.state != "error", page_id
