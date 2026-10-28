# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Application start-up (docs/design/GUI.md §1 ``app.py``, §4.0, R14).

``QApplication`` with the Wayland desktop file name, a single-instance
an exclusive lock on ``$XDG_RUNTIME_DIR/buttereye/gui.lock``, the log file, the
translators, the theme hooks, the core bridge and the main window.

The default window is the simple window (GUI.md §12). The ``--fake
<scenario>`` development flag runs the GUI over FakeCore and ``--classic``
opens the earlier multi-page window; both are accepted only when
``BUTTEREYE_DEV=1``. The shipped default path never imports the test doubles.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import logging
import logging.handlers
import os
import shutil
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QLibraryInfo,
    QLocale,
    QObject,
    QSettings,
    QTranslator,
)
from PySide6.QtGui import QFileOpenEvent
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from buttereye.core import api as core_api
from buttereye.core.api import (
    ButterEye,
    DetachPolicy,
    ErrorCode,
    Paths,
    legal_notices,
    osapi,
    render,
    set_language,
)
from buttereye.gui import a11y, theme
from buttereye.gui.bridge import CoreBridge, Opener
from buttereye.gui.simple_window import SimpleWindow

_log = logging.getLogger(__name__)

APP_ID = "io.github.buttereye.ButterEye"
DEV_ENV = "BUTTEREYE_DEV"
I18N_DIR = Path(__file__).resolve().parent.parent / "data" / "i18n"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 4  # 5 files x 5 MB (SCOPE §4.6)


def _t(text: str) -> str:
    return QCoreApplication.translate("App", text)


# ---------------------------------------------------------------------------
# arguments
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:  # pragma: no cover - argparse exits
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {message}\n")


def dev_mode(env: Mapping[str, str]) -> bool:
    return env.get(DEV_ENV) == "1"


def parse_args(args: Sequence[str], env: Mapping[str, str]) -> tuple[argparse.Namespace, list[str]]:
    """Our options, plus the remaining arguments for ``QApplication``.

    ``--fake`` is rejected (exit 2) unless ``BUTTEREYE_DEV=1``.
    """
    parser = _Parser(prog="buttereye-gui", description="ButterEye control center for mpv.")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    parser.add_argument("--fake", metavar="SCENARIO", help=argparse.SUPPRESS)
    parser.add_argument("--classic", action="store_true", help=argparse.SUPPRESS)
    ns, rest = parser.parse_known_args(list(args))
    # videos to play ("Open with ButterEye", desktop Exec %F): existing files only, so
    # the value of a Qt option (``-platform offscreen``) is never taken for a video.
    # Absolute, because mpv and the core run with their own working folder and a
    # name typed in a terminal ("buttereye-gui clip.mkv") means this one's.
    ns.files = [
        Path(a).expanduser().absolute()
        for a in rest
        if not a.startswith("-") and Path(a).expanduser().is_file()
    ]
    rest = [a for a in rest if a.startswith("-") or not Path(a).expanduser().is_file()]
    if ns.fake is not None and not dev_mode(env):
        parser.error(f"--fake needs {DEV_ENV}=1 (development only)")
    if ns.classic and not dev_mode(env):
        parser.error(f"--classic needs {DEV_ENV}=1 (development only)")
    return ns, rest


def fake_opener(scenario: str) -> Opener:
    """FakeCore opener for ``--fake`` (development only; imported on demand)."""
    testing = importlib.import_module("buttereye.core.testing")
    testing.get(scenario)  # KeyError for an unknown name, before any window opens
    fake_core = testing.FakeCore.for_scenario(scenario)

    async def open_fake(paths: Paths | None) -> ButterEye:
        return cast(ButterEye, await fake_core.open(paths))

    return open_fake


# ---------------------------------------------------------------------------
# the GUI process's own files: gui.ini and gui.log
# ---------------------------------------------------------------------------


def _xdg(env: Mapping[str, str], var: str, default: str) -> Path:
    value = env.get(var, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(env.get("HOME") or os.path.expanduser("~")) / default


def state_dir(env: Mapping[str, str]) -> Path:
    """``$XDG_STATE_HOME/buttereye``: where 0.0.5 kept both files on every
    platform, and where Linux still does."""
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / "buttereye"


class GuiFiles(NamedTuple):
    """``core.paths.GuiDirs``, as the GUI holds it."""

    settings_file: Path
    log_file: Path
    legacy_settings_file: Path | None = None
    legacy_log_file: Path | None = None


def gui_dirs(env: Mapping[str, str]) -> GuiFiles:
    """``core.paths.gui_dirs`` through ``core.api``: ``~/Library`` on macOS,
    ``%APPDATA%`` and ``%LOCALAPPDATA%`` on Windows, ``state_dir`` on Linux. A
    ``core.api`` that does not export it yet gets what 0.0.5 answered,
    ``state_dir`` on every platform."""
    found = getattr(core_api, "gui_dirs", None)
    if found is None:
        state = state_dir(env)
        return GuiFiles(state / "gui.ini", state / "logs" / "gui.log")
    d = found(env)
    return GuiFiles(d.settings_file, d.log_file, d.legacy_settings_file, d.legacy_log_file)


def log_file(env: Mapping[str, str]) -> Path:
    return gui_dirs(env).log_file


def settings_file(env: Mapping[str, str]) -> Path:
    """Window size and other per-user GUI conveniences (never ``config.toml``)."""
    return gui_dirs(env).settings_file


def _move(old: Path, new: Path) -> bool:
    """``old`` to ``new`` unless ``new`` exists already (a newer run wrote it):
    then the old copy is left alone. ``os.replace`` first, a copy-and-delete
    across file systems (``~/.local`` on another volume)."""
    if new.exists() or not old.is_file() or old.is_symlink():
        return False
    new.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.replace(old, new)
    except OSError:
        shutil.copy2(old, new)
        old.unlink()
    return True


def migrate_gui_files(dirs: GuiFiles) -> list[Path]:
    """Move ``gui.ini`` and ``gui.log``, ``.1`` to ``.4`` from where 0.0.5 kept
    them (``dirs.legacy_*``, macOS and Windows) to where they live now, before the log
    is opened. Each file moves once and only when nothing is at its new place;
    the old folders are removed when that leaves them empty. Never raises: a
    file that can't be moved stays where it was, and the GUI starts with a fresh
    one. Returns the new paths of what moved, for the log."""
    pairs: list[tuple[Path, Path]] = []
    if dirs.legacy_settings_file is not None:
        pairs.append((dirs.legacy_settings_file, dirs.settings_file))
    old_log = dirs.legacy_log_file
    if old_log is not None:
        pairs.append((old_log, dirs.log_file))
        for i in range(1, LOG_BACKUPS + 1):
            pairs.append(
                (
                    old_log.with_name(f"{old_log.name}.{i}"),
                    dirs.log_file.with_name(f"{dirs.log_file.name}.{i}"),
                )
            )
    moved: list[Path] = []
    for old, new in pairs:
        try:
            if _move(old, new):
                moved.append(new)
        except OSError as exc:
            print(f"buttereye-gui: cannot move {old} to {new}: {exc}", file=sys.stderr)
    if old_log is not None:
        # logs/ and then the buttereye folder above it, only when empty
        for d in (old_log.parent, old_log.parent.parent):
            with contextlib.suppress(OSError):
                d.rmdir()
    return moved


def lock_path(env: Mapping[str, str]) -> Path | None:
    """Where the single-instance lock lives, or None to run without one (R14).

    ``None`` is every platform but Linux today: neither Windows nor macOS sets
    ``XDG_RUNTIME_DIR``, and the GUI resolves its own paths from the environment
    rather than calling ``core.paths.resolve`` (it needs the lock before the core
    is open, and ``resolve`` raises BE-2010 rather than answering). Giving the
    other two a lock means teaching this function the runtime directory
    ``core.paths`` picks for them -- the user's profile on Windows, the per-user
    temporary directory on macOS -- and that is one decision in two files, not a
    second copy of the rule here.
    """
    runtime = env.get("XDG_RUNTIME_DIR", "")
    if not runtime or not os.path.isabs(runtime):
        return None
    return Path(runtime) / "buttereye" / "gui.lock"


# ---------------------------------------------------------------------------
# single instance (R14)
# ---------------------------------------------------------------------------


class AlreadyRunning(RuntimeError):
    """Another ButterEye window holds the lock."""


class UnsafeRuntimeDir(RuntimeError):
    """The runtime dir is not a 0700 directory owned by this user (BE-2010)."""


def _check_private_dir(path: Path) -> None:
    st = os.lstat(path)
    if osapi.is_link(st) or not stat.S_ISDIR(st.st_mode):
        raise UnsafeRuntimeDir(f"{path} is not a directory")
    if not osapi.owned_by_me(path, st):
        raise UnsafeRuntimeDir(f"{path} is owned by another user")
    if osapi.shared_mode_bits(st):
        raise UnsafeRuntimeDir(f"{path} has mode {stat.S_IMODE(st.st_mode):o}, expected 700")


class InstanceLock:
    """An exclusive lock (``osapi.try_lock``) held for the life of the window.
    Never chmods anything.

    ``flock`` behaves the same on macOS as on Linux, including the part that
    matters: a holder that is killed releases it. It is not available on every
    file system, though -- an SMB or AFP mount can refuse it with an ``OSError``
    rather than a ``BlockingIOError`` -- which is why ``main`` treats a plain
    ``OSError`` as "run unlocked". The lock itself is never on such a mount,
    since it lives in the runtime directory, which is local on all three
    platforms.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=osapi.IS_WINDOWS)
        except FileExistsError:
            pass
        _check_private_dir(parent)
        flags = os.O_RDWR | os.O_CREAT | osapi.O_CLOEXEC | osapi.O_NOFOLLOW | osapi.O_BINARY
        fd = os.open(self.path, flags, 0o600)
        try:
            if not osapi.try_lock(fd):
                os.close(fd)
                raise AlreadyRunning(str(self.path))
        except AlreadyRunning:
            raise
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        # the pid goes after the locked byte: Windows bars even this process's
        # other handles from a locked range, and readers only want the number
        os.ftruncate(fd, 0)
        os.lseek(fd, 1 if osapi.IS_WINDOWS else 0, os.SEEK_SET)
        os.write(fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._fd is not None:
            try:
                osapi.unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


# ---------------------------------------------------------------------------
# logging, translators, application
# ---------------------------------------------------------------------------


def setup_logging(path: Path) -> Path | None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        handler: logging.Handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
        )
    except OSError as exc:
        print(f"buttereye-gui: cannot open log {path}: {exc}", file=sys.stderr)
        return None
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return path


def install_translators(app: QCoreApplication, locale: QLocale | None = None) -> list[QTranslator]:
    """Qt's own catalogue plus ButterEye's ``buttereye_<lang>.qm`` when present.

    No ``.qm`` ships yet (no ``lrelease`` on the dev box, R3); English source
    strings are shown then.
    """
    loc = locale or QLocale.system()
    out: list[QTranslator] = [a11y.install_source_plurals(app)]  # lowest priority
    qt = QTranslator(app)
    if qt.load(loc, "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(qt)
        out.append(qt)
    ours = QTranslator(app)
    if ours.load(loc, "buttereye", "_", str(I18N_DIR)):
        app.installTranslator(ours)
        out.append(ours)
    return out


def version_text() -> str:
    """``buttereye-gui --version``: the version plus the Appropriate Legal Notices
    (F18): copyright, licence name, no-warranty statement, where the licence
    texts are (or that they are missing) and where the source is.

    Until a ``buttereye`` command-line tool ships this is where ``buttereye
    licence`` points people (the mpv helper's notice says so too).
    """
    set_language(None)
    n = legal_notices()

    def place(path: Path) -> str:
        return str(path) if path not in n.missing else f"{path} (missing from this installation)"

    return "\n".join(
        (
            f"{n.name} {n.version}",
            n.copyright,
            f"Licence: {n.licence_name}",
            render(n.no_warranty),
            f"Licence text: {place(n.agpl_text)}",
            f"Generated-output permission: {place(n.output_permission_text)}",
            f"Source: {n.source_link}",
        )
    )


class FileOpenRouter(QObject):
    """Finder's Open With and a file dropped on the Dock icon (macOS): Cocoa
    hands the GUI each file as a ``QEvent.FileOpen`` on the application, never
    in ``argv``, and the first one can arrive before ``main`` has built the
    window. Files are kept until ``attach``, which gives them to
    ``play_when_ready`` as the command line's are; after that one is played at
    once once the core is open, the way a drop on the window is, and queued
    with the rest before then. A window without ``play_file`` (the classic one)
    only has them logged."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pending: list[Path] = []
        self._window: QObject | None = None
        self._bridge: CoreBridge | None = None

    @property
    def pending(self) -> tuple[Path, ...]:
        return tuple(self._pending)

    def attach(self, window: QObject, bridge: CoreBridge, files: Sequence[Path] = ()) -> None:
        self._window = window
        self._bridge = bridge
        self._pending = [*files, *(f for f in self._pending if f not in files)]
        self._hand_over()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.FileOpen or not isinstance(event, QFileOpenEvent):
            return super().eventFilter(watched, event)
        name = event.file() or event.url().toLocalFile()
        path = Path(name) if name else None
        if path is None or not path.is_file():
            _log.info("open request for something that is not a file: %r", name)
            return True
        self.open(path)
        return True

    def open(self, path: Path) -> None:
        window, bridge = self._window, self._bridge
        play = getattr(window, "play_file", None)
        if bridge is not None and bridge.is_ready and callable(play):
            play(path)
            return
        self._pending.append(path)
        self._hand_over()

    def _hand_over(self) -> None:
        """Before the core is open: the window's ``play_when_ready`` replaces
        its list with the one it is given, so it always gets every file so far."""
        if self._window is None or not self._pending:
            return
        when_ready = getattr(self._window, "play_when_ready", None)
        if callable(when_ready):
            when_ready(list(self._pending))
        else:
            _log.info("this window does not play files; not opening %s", self._pending)
            self._pending.clear()


def configure_app(app: QApplication) -> None:
    app.setApplicationName("ButterEye")
    app.setApplicationDisplayName("ButterEye")
    app.setDesktopFileName(APP_ID)
    app.setQuitOnLastWindowClosed(True)


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    env = os.environ
    ns, qt_args = parse_args(argv[1:], env)
    if ns.version:
        if sys.platform == "win32":
            # a redirected stdout gets the ANSI code page on Windows, which can't
            # spell every folder name (the Source line): UTF-8, like the CLI
            reconfigure = getattr(sys.stdout, "reconfigure", None)
            if reconfigure is not None:
                with contextlib.suppress(ValueError, OSError):
                    reconfigure(encoding="utf-8", errors="replace")
        try:
            print(version_text())
        except Exception:
            _log.exception("legal notices unavailable")
            print("ButterEye")
        return 0
    opener: Opener | None = fake_opener(ns.fake) if ns.fake is not None else None

    existing = QApplication.instance()
    app = existing if isinstance(existing, QApplication) else QApplication([argv[0], *qt_args])
    configure_app(app)
    # before anything that can spin the event loop: a FileOpen from Finder may
    # already be queued (the "already open" box below would deliver it)
    file_open = FileOpenRouter(app)
    app.installEventFilter(file_open)

    lock: InstanceLock | None = None
    lp = lock_path(env)
    if lp is not None:
        lock = InstanceLock(lp)
        try:
            lock.acquire()
        except AlreadyRunning:
            text = _t("ButterEye is already open.")
            print("ButterEye is already open.", file=sys.stderr)
            box = a11y.message_box(
                None, icon=QMessageBox.Icon.Information, title="ButterEye", text=text
            )
            box.exec()
            return ErrorCode.GUI_ALREADY_RUNNING.exit_code
        except (UnsafeRuntimeDir, OSError) as exc:
            # The core reports BE-2010 through the fatal panel; run unlocked.
            _log.warning("single-instance lock unavailable: %s", exc)
            lock = None

    dirs = gui_dirs(env)
    moved = migrate_gui_files(dirs)
    log_path = setup_logging(dirs.log_file)
    if moved:
        _log.info(
            "moved the GUI's files from where 0.0.5 kept them: %s", ", ".join(map(str, moved))
        )
    set_language(None)  # core gettext follows the environment, before any render()
    install_translators(app)
    theme.install(app)

    bridge = CoreBridge(opener=opener)
    settings = QSettings(str(dirs.settings_file), QSettings.Format.IniFormat)
    window: QMainWindow
    if ns.classic:
        from buttereye.gui.main_window import MainWindow

        window = MainWindow(bridge, settings, log_path=log_path)
    else:
        window = SimpleWindow(bridge, settings, log_path=log_path)
    file_open.attach(window, bridge, ns.files)

    def last_resort_shutdown() -> None:
        if not bridge.is_closed:
            bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)

    app.aboutToQuit.connect(last_resort_shutdown)
    bridge.start()
    window.show()
    try:
        return app.exec()
    finally:
        last_resort_shutdown()
        app.removeEventFilter(file_open)
        if lock is not None:
            lock.release()


__all__ = [
    "APP_ID",
    "AlreadyRunning",
    "DEV_ENV",
    "FileOpenRouter",
    "GuiFiles",
    "InstanceLock",
    "UnsafeRuntimeDir",
    "configure_app",
    "dev_mode",
    "fake_opener",
    "gui_dirs",
    "install_translators",
    "lock_path",
    "log_file",
    "main",
    "migrate_gui_files",
    "parse_args",
    "settings_file",
    "setup_logging",
    "state_dir",
    "version_text",
]
