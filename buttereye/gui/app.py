# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Application start-up (docs/design/GUI.md §1 ``app.py``, §4.0, R14).

``QApplication`` with the Wayland desktop file name, a single-instance
``flock`` on ``$XDG_RUNTIME_DIR/buttereye/gui.lock``, the log file, the
translators, the theme hooks, the core bridge and the main window.

The default window is the simple window (GUI.md §12). The ``--fake
<scenario>`` development flag runs the GUI over FakeCore and ``--classic``
opens the earlier multi-page window; both are accepted only when
``BUTTEREYE_DEV=1``. The shipped default path never imports the test doubles.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import logging
import logging.handlers
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QSettings, QTranslator
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from buttereye.core.api import (
    ButterEye,
    DetachPolicy,
    ErrorCode,
    Paths,
    legal_notices,
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
# XDG locations used by the GUI process itself
# ---------------------------------------------------------------------------


def _xdg(env: Mapping[str, str], var: str, default: str) -> Path:
    value = env.get(var, "")
    if value and os.path.isabs(value):
        return Path(value)
    return Path(env.get("HOME") or os.path.expanduser("~")) / default


def state_dir(env: Mapping[str, str]) -> Path:
    return _xdg(env, "XDG_STATE_HOME", ".local/state") / "buttereye"


def log_file(env: Mapping[str, str]) -> Path:
    return state_dir(env) / "logs" / "gui.log"


def settings_file(env: Mapping[str, str]) -> Path:
    """Window size and other per-user GUI conveniences (never ``config.toml``)."""
    return state_dir(env) / "gui.ini"


def lock_path(env: Mapping[str, str]) -> Path | None:
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
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeRuntimeDir(f"{path} is not a directory")
    if st.st_uid != os.getuid():
        raise UnsafeRuntimeDir(f"{path} is owned by uid {st.st_uid}")
    if st.st_mode & 0o077:
        raise UnsafeRuntimeDir(f"{path} has mode {stat.S_IMODE(st.st_mode):o}, expected 700")


class InstanceLock:
    """``fcntl.flock`` held for the life of the window. Never chmods anything."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _check_private_dir(parent)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AlreadyRunning(str(self.path)) from None
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
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

    log_path = setup_logging(log_file(env))
    set_language(None)  # core gettext follows the environment, before any render()
    install_translators(app)
    theme.install(app)

    bridge = CoreBridge(opener=opener)
    settings_path = settings_file(env)
    settings = QSettings(str(settings_path), QSettings.Format.IniFormat)
    window: QMainWindow
    if ns.classic:
        from buttereye.gui.main_window import MainWindow

        window = MainWindow(bridge, settings, log_path=log_path)
    else:
        window = SimpleWindow(bridge, settings, log_path=log_path)

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
        if lock is not None:
            lock.release()


__all__ = [
    "APP_ID",
    "AlreadyRunning",
    "DEV_ENV",
    "InstanceLock",
    "UnsafeRuntimeDir",
    "configure_app",
    "dev_mode",
    "fake_opener",
    "install_translators",
    "lock_path",
    "log_file",
    "main",
    "parse_args",
    "settings_file",
    "setup_logging",
    "state_dir",
    "version_text",
]
