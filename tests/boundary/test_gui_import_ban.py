# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The GUI process loads only Essentials Qt modules and no NVIDIA/VapourSynth
code (docs/design/GUI.md §6 ``test_gui_import_ban``, F17, SCOPE §8.1).

A clean ``python -I`` subprocess builds the main window, shows every page of
``PAGE_REGISTRY`` (so new pages are covered automatically), the shell's
dialogs, the simple window and every tab of its Details dialog (§12), spins
the event loop for 500 ms, then reports ``sys.modules`` and ``/proc/self/maps``.
Driver GL/EGL libraries are allowed (Q18).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import json, os, sys
sys.path.insert(0, os.environ["BE_REPO"])
from tests.boundary.import_guard import ImportGuard, loaded_violations, mapped_violations
guard = ImportGuard(allow_test_qt=False).install()

from PySide6.QtCore import QEventLoop, QSettings, QTimer
from PySide6.QtWidgets import QApplication

from buttereye.core.api import DetachPolicy
from buttereye.gui import theme
from buttereye.gui.app import configure_app, fake_opener
from buttereye.gui.bridge import CoreBridge
from buttereye.gui.main_window import MainWindow
from buttereye.gui.quit_dialog import DetachProgress, LiveSessionsDialog

app = QApplication(["buttereye-gui"])
configure_app(app)
theme.install(app)
scenario = os.environ.get("BE_SCENARIO", "")
opener = fake_opener(scenario) if scenario else None
bridge = CoreBridge(opener=opener)
settings = QSettings(os.path.join(os.environ["BE_TMP"], "gui.ini"), QSettings.Format.IniFormat)
win = MainWindow(bridge, settings, open_setup_when_missing=False)


def spin(ms):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


bridge.start()
win.show()
spin(300)
for page in win.pages:
    win.sidebar.setCurrentRow(win.pages.index(page))
    spin(100)
for dlg in (LiveSessionsDialog((), win), DetachProgress(1, win)):
    dlg.show()
    spin(50)
    dlg.hide()
from buttereye.gui.simple_window import SimpleWindow
simple = SimpleWindow(bridge, settings, auto_setup=False, auto_bench=False)
simple.show()
spin(200)
simple.open_details()
for i in range(simple.details.tabs.count()):
    simple.details.tabs.setCurrentIndex(i)
    spin(100)
simple.details.hide()
simple._may_close = True
spin(500)
fatal = bridge.startup_error
maps = open("/proc/self/maps", encoding="utf-8", errors="replace").read()
report = {
    "modules": loaded_violations(),
    "guard": guard.violations,
    "maps": mapped_violations(maps),
    "testing_loaded": "buttereye.core.testing" in sys.modules,
    "ready": bridge.is_ready,
    "fatal": None if fatal is None else fatal.code.code,
    "qt": sorted(m for m in sys.modules if m.startswith("PySide6.Qt")),
}
win._may_close = True
bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)
win.close()
print("REPORT " + json.dumps(report))
"""


def _run(tmp_path: Path, xdg: Any, scenario: str | None) -> dict[str, Any]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(xdg.home),
        "XDG_CONFIG_HOME": str(xdg.config),
        "XDG_DATA_HOME": str(xdg.data),
        "XDG_STATE_HOME": str(xdg.state),
        "XDG_CACHE_HOME": str(xdg.cache),
        "XDG_RUNTIME_DIR": str(xdg.runtime),
        "QT_QPA_PLATFORM": "offscreen",
        "QT_ACCESSIBILITY": "1",
        "LANG": "C.UTF-8",
        "BE_REPO": str(REPO),
        "BE_TMP": str(tmp_path),
        "BE_SCENARIO": scenario or "",
    }
    if scenario:
        env["BUTTEREYE_DEV"] = "1"
    res = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        env=env,
        cwd=tmp_path,
    )
    lines = [ln for ln in res.stdout.splitlines() if ln.startswith("REPORT ")]
    assert res.returncode == 0 and lines, f"rc={res.returncode}\n{res.stdout}\n{res.stderr}"
    out: dict[str, Any] = json.loads(lines[-1].removeprefix("REPORT "))
    return out


def _assert_clean(report: dict[str, Any]) -> None:
    assert report["modules"] == [], report["modules"]
    assert report["guard"] == [], report["guard"]
    assert report["maps"] == [], report["maps"]
    assert set(report["qt"]) <= {"PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"}


@pytest.mark.parametrize("scenario", ["devbox_now", "live_active", "all_ready"])
def test_gui_process_over_fakecore(tmp_path: Path, xdg_env: Any, scenario: str) -> None:
    report = _run(tmp_path, xdg_env, scenario)
    assert report["ready"], report
    _assert_clean(report)


def test_shipped_default_path_with_real_core(tmp_path: Path, xdg_env: Any) -> None:
    """No ``--fake``: the real core (whatever providers exist) and no test doubles."""
    report = _run(tmp_path, xdg_env, None)
    _assert_clean(report)
    assert report["testing_loaded"] is False
    assert report["ready"] or report["fatal"], report
