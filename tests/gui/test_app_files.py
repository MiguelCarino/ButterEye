# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The GUI process's own files and the files Finder hands it (gui/app.py):
``gui.ini`` and ``gui.log*`` moved to their macOS places once (B13), and
``QEvent.FileOpen`` from Open With and the Dock icon, kept until the window
exists and then played."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QObject, QUrl
from PySide6.QtGui import QFileOpenEvent

from buttereye.core import api as core_api
from buttereye.gui import app as gui_app


def _legacy(tmp_path: Path) -> gui_app.GuiFiles:
    old = tmp_path / "state" / "buttereye"
    lib = tmp_path / "home" / "Library"
    return gui_app.GuiFiles(
        settings_file=lib / "Application Support" / "ButterEye" / "config" / "gui.ini",
        log_file=lib / "Logs" / "ButterEye" / "gui.log",
        legacy_settings_file=old / "gui.ini",
        legacy_log_file=old / "logs" / "gui.log",
    )


def _plant(dirs: gui_app.GuiFiles, *rotated: int) -> None:
    assert dirs.legacy_settings_file is not None and dirs.legacy_log_file is not None
    dirs.legacy_log_file.parent.mkdir(parents=True)
    dirs.legacy_settings_file.write_text("[window]\nsize=@Size(900 600)\n")
    dirs.legacy_log_file.write_text("current\n")
    for i in rotated:
        dirs.legacy_log_file.with_name(f"gui.log.{i}").write_text(f"old {i}\n")


# ---------------------------------------------------------------- B13: migration
def test_the_settings_and_every_log_move_once_and_the_old_folders_go(tmp_path: Path) -> None:
    dirs = _legacy(tmp_path)
    _plant(dirs, 1, 2, 3, 4)
    moved = gui_app.migrate_gui_files(dirs)
    logs = dirs.log_file.parent
    assert set(moved) == {
        dirs.settings_file,
        dirs.log_file,
        *(logs / f"gui.log.{i}" for i in range(1, 5)),
    }
    assert dirs.settings_file.read_text().startswith("[window]")
    assert (logs / "gui.log").read_text() == "current\n"
    assert (logs / "gui.log.4").read_text() == "old 4\n"
    assert not (tmp_path / "state" / "buttereye").exists()
    # a second start has nothing left to move
    assert gui_app.migrate_gui_files(dirs) == []


def test_a_file_already_at_its_new_place_is_never_overwritten(tmp_path: Path) -> None:
    dirs = _legacy(tmp_path)
    _plant(dirs, 1)
    dirs.log_file.parent.mkdir(parents=True)
    dirs.log_file.write_text("newer\n")
    moved = gui_app.migrate_gui_files(dirs)
    assert dirs.log_file not in moved
    assert dirs.log_file.read_text() == "newer\n"
    assert dirs.legacy_log_file is not None and dirs.legacy_log_file.read_text() == "current\n"
    # the folder still holding that file stays
    assert dirs.legacy_log_file.parent.is_dir()


def test_a_symlinked_legacy_file_is_left_where_it_is(tmp_path: Path) -> None:
    dirs = _legacy(tmp_path)
    assert dirs.legacy_log_file is not None
    dirs.legacy_log_file.parent.mkdir(parents=True)
    target = tmp_path / "elsewhere.log"
    target.write_text("x")
    dirs.legacy_log_file.symlink_to(target)
    assert gui_app.migrate_gui_files(dirs) == []
    assert not dirs.log_file.exists()


def test_nothing_moves_where_nothing_changed_place(tmp_path: Path) -> None:
    state = tmp_path / "state" / "buttereye"
    dirs = gui_app.GuiFiles(state / "gui.ini", state / "logs" / "gui.log")
    state.mkdir(parents=True)
    (state / "gui.ini").write_text("x")
    assert gui_app.migrate_gui_files(dirs) == []
    assert (state / "gui.ini").exists()


def test_a_move_that_fails_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    dirs = _legacy(tmp_path)
    _plant(dirs)

    def refuse(*_a: Any, **_kw: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(gui_app.os, "replace", refuse)
    monkeypatch.setattr(gui_app.shutil, "copy2", refuse)
    assert gui_app.migrate_gui_files(dirs) == []
    assert "cannot move" in capsys.readouterr().err
    assert dirs.legacy_settings_file is not None and dirs.legacy_settings_file.exists()


def test_the_gui_asks_core_paths_through_the_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    want = _legacy(tmp_path)
    monkeypatch.setattr(core_api, "gui_dirs", lambda env: want, raising=False)
    assert gui_app.gui_dirs({}) == want
    assert gui_app.log_file({}) == want.log_file
    assert gui_app.settings_file({}) == want.settings_file


def test_without_the_export_the_gui_keeps_0_0_5_s_places(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(core_api, "gui_dirs", raising=False)
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "s")}
    d = gui_app.gui_dirs(env)
    assert d == gui_app.GuiFiles(
        tmp_path / "s/buttereye/gui.ini", tmp_path / "s/buttereye/logs/gui.log"
    )


# ---------------------------------------------------------------- Open With / Dock drop
class _Window(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.when_ready: list[list[Path]] = []
        self.played: list[Path] = []

    def play_when_ready(self, files: list[Path]) -> None:
        self.when_ready.append(list(files))

    def play_file(self, path: Path) -> None:
        self.played.append(path)


class _Bridge:
    def __init__(self, ready: bool) -> None:
        self.is_ready = ready


def _open(router: gui_app.FileOpenRouter, path: Path, *, url: bool = False) -> bool:
    ev = QFileOpenEvent(QUrl.fromLocalFile(str(path))) if url else QFileOpenEvent(str(path))
    return router.eventFilter(QObject(), ev)


def test_files_opened_before_the_window_exists_are_kept_for_it(qapp: Any, tmp_path: Path) -> None:
    a, b, cli = tmp_path / "a.mkv", tmp_path / "b.mp4", tmp_path / "c.mov"
    for f in (a, b, cli):
        f.write_bytes(b"")
    router = gui_app.FileOpenRouter()
    assert _open(router, a) and _open(router, b, url=True)
    assert router.pending == (a, b)
    win = _Window()
    router.attach(win, _Bridge(False), [cli])  # type: ignore[arg-type]
    # the command line's first, then Finder's, all through play_when_ready
    assert win.when_ready == [[cli, a, b]]
    assert win.played == []


def test_before_the_core_is_open_a_new_file_joins_the_queue(qapp: Any, tmp_path: Path) -> None:
    a, b = tmp_path / "a.mkv", tmp_path / "b.mkv"
    a.write_bytes(b"")
    b.write_bytes(b"")
    router = gui_app.FileOpenRouter()
    win = _Window()
    bridge = _Bridge(False)
    router.attach(win, bridge, [a])  # type: ignore[arg-type]
    _open(router, b)
    # play_when_ready replaces its list, so it is handed every file so far
    assert win.when_ready[-1] == [a, b]
    bridge.is_ready = True
    c = tmp_path / "c.mkv"
    c.write_bytes(b"")
    _open(router, c)
    assert win.played == [c]  # a Dock drop on a running ButterEye plays at once
    assert win.when_ready[-1] == [a, b]


def test_something_that_is_not_a_file_is_swallowed(qapp: Any, tmp_path: Path) -> None:
    router = gui_app.FileOpenRouter()
    win = _Window()
    router.attach(win, _Bridge(True), [])  # type: ignore[arg-type]
    assert _open(router, tmp_path / "missing.mkv")
    assert _open(router, tmp_path)  # a folder
    assert win.played == [] and win.when_ready == []


def test_other_events_pass_through(qapp: Any) -> None:
    from PySide6.QtCore import QEvent

    router = gui_app.FileOpenRouter()
    assert router.eventFilter(QObject(), QEvent(QEvent.Type.Show)) is False


def test_a_window_that_cannot_play_only_logs(qapp: Any, tmp_path: Path) -> None:
    a = tmp_path / "a.mkv"
    a.write_bytes(b"")
    router = gui_app.FileOpenRouter()
    _open(router, a)
    router.attach(QObject(), _Bridge(False), [])  # type: ignore[arg-type]
    assert router.pending == ()


def test_main_installs_the_router_before_the_window(qapp: Any) -> None:
    source = Path(gui_app.__file__).read_text(encoding="utf-8")
    main = source[source.index("def main(") :]
    assert main.index("installEventFilter(file_open)") < main.index("SimpleWindow(")
    assert "file_open.attach(window, bridge, ns.files)" in main
    assert main.index("migrate_gui_files(dirs)") < main.index("setup_logging(dirs.log_file)")
