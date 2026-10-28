# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The GUI's own files on Windows, moved once from where 0.0.5 kept them, and
command-line videos made absolute before anything else sees them (gui/app.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from buttereye.core import paths as core_paths
from buttereye.gui import app as gui_app


def _windows(tmp_path: Path) -> gui_app.GuiFiles:
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }
    d = core_paths.gui_dirs(env, macos=False, windows=True)
    return gui_app.GuiFiles(d.settings_file, d.log_file, d.legacy_settings_file, d.legacy_log_file)


# ---------------------------------------------------------------- Windows migration
def test_windows_moves_the_settings_and_every_log_once(tmp_path: Path) -> None:
    dirs = _windows(tmp_path)
    assert dirs.legacy_settings_file is not None and dirs.legacy_log_file is not None
    dirs.legacy_log_file.parent.mkdir(parents=True)
    dirs.legacy_settings_file.write_text("[window]\n")
    dirs.legacy_log_file.write_text("current\n")
    dirs.legacy_log_file.with_name("gui.log.2").write_text("old 2\n")

    moved = gui_app.migrate_gui_files(dirs)
    assert set(moved) == {dirs.settings_file, dirs.log_file, dirs.log_file.with_name("gui.log.2")}
    assert dirs.settings_file == tmp_path / "Roaming" / "ButterEye" / "gui.ini"
    assert dirs.settings_file.read_text() == "[window]\n"
    assert dirs.log_file.read_text() == "current\n"
    assert dirs.log_file.with_name("gui.log.2").read_text() == "old 2\n"
    assert not (tmp_path / "state" / "buttereye").exists()
    assert gui_app.migrate_gui_files(dirs) == []


def test_windows_with_nothing_to_move_creates_nothing(tmp_path: Path) -> None:
    dirs = _windows(tmp_path)
    assert gui_app.migrate_gui_files(dirs) == []
    assert not (tmp_path / "Roaming").exists() and not (tmp_path / "Local").exists()


# ---------------------------------------------------------------- B9: absolute paths
def test_a_relative_video_on_the_command_line_becomes_absolute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "clip.mkv").write_bytes(b"x")
    monkeypatch.chdir(tmp_path)
    ns, rest = gui_app.parse_args(["clip.mkv", "-platform", "offscreen"], {})
    assert ns.files == [tmp_path / "clip.mkv"]
    assert ns.files[0].is_absolute()
    assert rest == ["-platform", "offscreen"]


def test_a_home_relative_video_is_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "clip.mkv").write_bytes(b"x")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    ns, rest = gui_app.parse_args(["~/clip.mkv"], {})
    assert ns.files == [tmp_path / "clip.mkv"]
    assert rest == []
