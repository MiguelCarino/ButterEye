# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The core imports no Qt at runtime (docs/design/GUI.md §6 ``test_core_no_qt``)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

SCRIPT = r"""
import importlib, json, pkgutil, sys
sys.path.insert(0, sys.argv[1])
import buttereye.core.api  # noqa: F401
QT = ("PySide6", "shiboken6", "PyQt5", "PyQt6")
after_api = sorted(m for m in sys.modules if m.split(".")[0] in QT)
subsystems_at_api = sorted(
    m for m in sys.modules
    if m.startswith("buttereye.core.") and m.split(".")[2] not in (
        "api", "types", "errors", "events", "ops", "i18n", "commands", "capabilities", "filelog"
    )
)
import buttereye.core as core
failed = {}
for info in pkgutil.walk_packages(core.__path__, "buttereye.core."):
    try:
        importlib.import_module(info.name)
    except Exception as exc:  # a broken subsystem is another unit's bug; report it
        failed[info.name] = repr(exc)
after_all = sorted(m for m in sys.modules if m.split(".")[0] in QT)
print(json.dumps({"after_api": after_api, "after_all": after_all,
                  "subsystems_at_api": subsystems_at_api, "failed": failed}))
"""


def _run() -> dict[str, object]:
    res = subprocess.run(
        [sys.executable, "-I", "-c", SCRIPT, str(REPO)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"},
    )
    assert res.returncode == 0, res.stderr
    out: dict[str, object] = json.loads(res.stdout.strip().splitlines()[-1])
    return out


def test_core_api_imports_no_qt_and_no_subsystem() -> None:
    out = _run()
    assert out["after_api"] == []
    assert out["subsystems_at_api"] == []


def test_no_core_module_pulls_in_qt() -> None:
    out = _run()
    assert out["after_all"] == [], out["after_all"]
