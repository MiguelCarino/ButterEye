# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Regenerate ``docs/design/screenshots/*.png`` over the real core (dev box).

The simple window (GUI.md §12, ``simple-*.png``): the silent first run (real
setup and smoke test, then the background speed test starting), the empty
window, a real live MVTools session row, its dark variant, and every tab of
the Details dialog. Then the classic multi-page window (skip it with
``--simple-only``): every page, the setup wizard's first page, dark variants of Play and System,
a Play page with a real live MVTools session (mpv runs with ``--vo=null
--ao=null``) and a Benchmark page after a real short 720p run (MVTools and one
RIFE model).

HOME and every XDG directory point at fresh temporary directories before
anything from ButterEye or Qt is imported; the script refuses to run if a
ButterEye folder would resolve under the developer's own (``~/.config`` and
``~/.local/state`` on Linux, the three ``~/Library`` folders on macOS, which is
where ButterEye keeps its settings there -- ``_refuse_real_dirs``). Every mpv it
starts is killed before it exits.

Platform differences, both of them deliberate: a Mac is asked for the real
``cocoa`` Qt platform and falls back to ``offscreen``, and prints which it got,
because an offscreen render is not proof of what a user sees; and the macOS shots
are written to ``docs/design/screenshots/macos/``, so the Fedora set next to it
stays the reference one. Linux is unchanged in both.

Usage: ``.venv/bin/python tools/screenshots.py [--out DIR] [--rife-model NAME]``
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REAL_HOME = Path.home()
IS_MACOS = sys.platform == "darwin"
#: the folder of models under ``Paths.rpm_data_dir``, which is where every
#: platform keeps them: ``/usr/share/buttereye`` on Fedora, Homebrew's
#: ``<prefix>/share/buttereye`` on macOS (``buttereye.core.types``)
MODELS_SUBDIR = "rife-ncnn-models"
#: ``buttereye.core.paths.MACOS_APP``, copied rather than imported: the refusal in
#: ``_refuse_real_dirs`` has to run before anything from ButterEye is imported
MACOS_APP = "ButterEye"
#: the Fedora shots are the reference set and are never overwritten by another
#: platform's run, so macOS writes into a folder of its own next to them
SHOTS = REPO / "docs" / "design" / "screenshots"
DEFAULT_OUT = SHOTS / "macos" if IS_MACOS else SHOTS
#: where this script's own temporary directories go: ``tempfile``'s choice
#: everywhere but macOS, where two things make ``$TMPDIR`` the wrong one. It is
#: ``/var/folders/<2>/<28-character per-user id>/T/``, and the Storage page prints
#: the paths it is handed, so a shot taken from a home under it carries that
#: identifier into an image that gets committed. It is also 49 bytes before
#: ``mkdtemp`` has added a name, and a live session's control channel is
#: ``<root>/runtime/buttereye/mpv-<12 chars>.sock`` where macOS binds at most 103
#: bytes of ``sun_path`` -- from ``$TMPDIR`` that overruns and mpv never gets a
#: socket. ``/tmp`` is on every Mac, identifies nothing, and leaves the socket at
#: 69 bytes. It is what ``tempfile`` picks on Linux anyway, so Linux is unchanged.
TEMP_BASE = "/tmp" if IS_MACOS else None
SIZE = (1180, 760)


def _qt_platform() -> str:
    """``cocoa`` on a Mac when it really starts here, ``offscreen`` otherwise.

    An offscreen render is not proof of what a user sees, so a Mac is asked for
    the real platform first. It cannot simply be tried in-process: a Qt platform
    plugin that fails to start calls ``qFatal`` and aborts, which no ``except``
    catches, so a child process finds out instead. On Linux the answer stays
    ``offscreen`` whatever the session is, because the display socket lives under
    the real ``XDG_RUNTIME_DIR`` that ``isolate`` is about to replace.
    """
    if not IS_MACOS:
        return "offscreen"  # the display socket is under the real runtime dir
    probe = "from PySide6.QtWidgets import QApplication; QApplication([])"
    try:
        done = subprocess.run(
            [sys.executable, "-c", probe],
            env={**os.environ, "QT_QPA_PLATFORM": "cocoa"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "offscreen"
    return "cocoa" if done.returncode == 0 else "offscreen"


def _refuse_real_dirs() -> None:
    """Refuse to run if a ButterEye folder would resolve under the developer's own.

    The isolation above is the whole reason this script is safe to run on a dev
    box over the real core, which writes config, saves profiles and cleans
    storage; this is the check that the isolation took. The folders are the
    platform's own ones. Linux: the XDG variables, which is what ``paths.resolve``
    reads. macOS: the three ``~/Library`` folders ``paths._resolve_macos`` builds
    out of HOME -- it does not read ``XDG_CONFIG_HOME`` or ``XDG_STATE_HOME`` at
    all, so checking those on a Mac would check nothing at all.
    """
    if IS_MACOS:
        home = Path(os.environ["HOME"])
        for what, rel in (
            ("the settings, data and state folder", Path("Application Support") / MACOS_APP),
            ("the cache folder", Path("Caches") / MACOS_APP),
            ("the log folder", Path("Logs") / MACOS_APP),
        ):
            real = (REAL_HOME / "Library" / rel).resolve()
            p = (home / "Library" / rel).resolve()
            if p == real or real in p.parents:
                sys.exit(f"refusing to run: {what} resolves under {real}")
        return
    for real_xdg in (REAL_HOME / ".config", REAL_HOME / ".local" / "state"):
        for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            p = Path(os.environ[var]).resolve()
            if p == real_xdg.resolve() or real_xdg.resolve() in p.parents:
                sys.exit(f"refusing to run: {var} resolves under {real_xdg}")


def isolate() -> tuple[Path, Path]:
    """Point HOME/XDG at temporary dirs; return (root, runtime)."""
    root = Path(tempfile.mkdtemp(prefix="buttereye-shots-", dir=TEMP_BASE))
    for name in ("home", "config", "data", "state", "cache"):
        (root / name).mkdir()
    real_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if real_runtime and os.access(real_runtime, os.W_OK):
        runtime = Path(tempfile.mkdtemp(prefix="buttereye-shots-", dir=real_runtime))
    else:
        runtime = root / "runtime"
        runtime.mkdir()
    runtime.chmod(0o700)
    # chosen before HOME moves, so the probe sees the session the user is in
    platform = _qt_platform()
    os.environ.update(
        HOME=str(root / "home"),
        XDG_CONFIG_HOME=str(root / "config"),
        XDG_DATA_HOME=str(root / "data"),
        XDG_STATE_HOME=str(root / "state"),
        XDG_CACHE_HOME=str(root / "cache"),
        XDG_RUNTIME_DIR=str(runtime),
        QT_QPA_PLATFORM=platform,
        QT_ACCESSIBILITY="1",
    )
    print("Qt platform:", platform)
    _refuse_real_dirs()
    return root, runtime


def make_clip(out: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24000/1001",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "120", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", os.fspath(out),
        ],
        check=True,
        timeout=300,
    )  # fmt: skip
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--rife-model", default="rife-v4.26_ensembleFalse")
    ap.add_argument("--no-bench", action="store_true", help="keep the existing bench.png")
    ap.add_argument("--simple-only", action="store_true", help="only the simple-*.png shots")
    ns = ap.parse_args()
    out: Path = ns.out
    out.mkdir(parents=True, exist_ok=True)
    root, runtime = isolate()
    mpv_pids: set[int] = set()
    try:
        return _run(out, root, None if ns.no_bench else ns.rife_model, mpv_pids, ns.simple_only)
    finally:
        for pid in mpv_pids:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError, PermissionError:
                pass
        shutil.rmtree(runtime, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _run(
    out: Path, root: Path, rife_model: str | None, mpv_pids: set[int], simple_only: bool
) -> int:
    sys.path.insert(0, os.fspath(REPO))

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication, QWidget

    from buttereye.core.api import DetachPolicy
    from buttereye.core.i18n import set_language
    from buttereye.gui import app as gui_app
    from buttereye.gui import theme
    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.main_window import MainWindow

    qapp = QApplication([sys.argv[0]])
    gui_app.configure_app(qapp)
    set_language(None)
    gui_app.install_translators(qapp)
    controller = theme.install(qapp)

    def pump(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            qapp.processEvents()
            time.sleep(0.01)

    def wait(cond: Callable[[], bool], timeout: float, what: str) -> None:
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise TimeoutError(what)
            pump(0.05)

    def shot(w: QWidget, name: str) -> None:
        pump(0.3)
        path = out / f"{name}.png"
        w.grab().save(os.fspath(path))
        print("wrote", path)

    def call(bridge: CoreBridge, owner: Any, fn: Callable[[Any], Any]) -> Any:
        box: list[Any] = []
        errs: list[Any] = []
        bridge.call(fn, owner=owner, ok=box.append, err=errs.append)
        wait(lambda: bool(box or errs), 30, "core call")
        if errs:
            raise RuntimeError(errs[0])
        return box[0]

    bridges: list[CoreBridge] = []

    def window(paths: Any) -> tuple[MainWindow, CoreBridge]:
        bridge = CoreBridge(paths)
        bridges.append(bridge)
        settings = QSettings(os.fspath(root / "gui.ini"), QSettings.Format.IniFormat)
        win = MainWindow(bridge, settings, log_path=root / "gui.log", open_setup_when_missing=False)
        bridge.start()
        wait(lambda: bridge.is_ready, 20, "core open")
        win.resize(*SIZE)
        win.show()
        return win, bridge

    def close(win: MainWindow, bridge: CoreBridge) -> None:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)
        win._may_close = True  # the quit prompt is for people, not this script
        win.close()
        pump(0.2)

    try:
        _simple(call, pump, wait, shot, controller, root, mpv_pids, bridges)
        if not simple_only:
            _phases(window, close, call, pump, wait, shot, controller, root, rife_model, mpv_pids)
    finally:
        for b in bridges:
            if not b.is_closed:
                b.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)
    return 0


SIMPLE_SIZE = (460, 560)


def _simple(
    call: Any, pump: Any, wait: Any, shot: Any, controller: Any, root: Path,
    mpv_pids: set[int], bridges: list[Any],
) -> None:  # fmt: skip
    """The simple window over the real core, from a fresh (temporary) home."""
    from PySide6.QtCore import QSettings, Qt

    from buttereye.core.api import DetachPolicy, FilterState
    from buttereye.core.paths import resolve
    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.simple_window import SimpleWindow

    paths = resolve(os.environ)
    settings = QSettings(os.fspath(root / "simple.ini"), QSettings.Format.IniFormat)

    def open_window(auto_bench: bool) -> tuple[Any, CoreBridge]:
        bridge = CoreBridge(paths)
        bridges.append(bridge)
        win = SimpleWindow(bridge, settings, log_path=root / "gui.log", auto_bench=auto_bench)
        bridge.start()
        wait(lambda: bridge.is_ready, 20, "core open")
        win.resize(*SIMPLE_SIZE)
        win.show()
        return win, bridge

    def close(win: Any, bridge: CoreBridge) -> None:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)  # cancels a running bench
        win._may_close = True
        win.close()
        pump(0.3)

    # First run: no config. Setup runs silently, then the speed test starts.
    win, bridge = open_window(auto_bench=True)
    wait(lambda: win.bench_started or win.problem.isVisible(), 180, "silent setup")
    pump(1.5)
    shot(win, "simple-first-run")
    close(win, bridge)

    # Configured: the empty window, then a real live session.
    win, bridge = open_window(auto_bench=False)
    wait(lambda: win.status_text() == "Ready.", 120, "ready")
    shot(win, "simple-empty")

    async def live_options(core: Any) -> None:
        from buttereye.core.mpvctl import session as live

        reg = live.registry(core._ctx)
        reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")

    call(bridge, win, live_options)
    clip = make_clip(root / "holiday.mkv")

    def active() -> bool:
        for s in win.live_sessions:
            if s.pid:
                mpv_pids.add(s.pid)
        return any(s.filter is FilterState.ACTIVE for s in win.live_sessions)

    win.play_file(clip)
    try:
        wait(active, 60, "live session")
    except TimeoutError:
        print("auto did not become active; trying CPU only")
        win.smooth_combo.setCurrentIndex(win.smooth_combo.findData("cpu"))
        pump(1.0)
        win.play_file(clip)
        wait(active, 60, "live MVTools session")
    pump(4.0)
    shot(win, "simple-playing")
    controller.apply_scheme(Qt.ColorScheme.Dark)
    pump(0.5)
    shot(win, "simple-playing-dark")
    controller.apply_scheme(Qt.ColorScheme.Light)
    pump(0.3)

    win.open_details()
    dlg: Any = win.details
    for page_id, name in (
        ("system", "simple-details-system"),
        ("bench", "simple-details-speed"),
        ("storage", "simple-details-storage"),
        ("about", "simple-details-about"),
    ):
        dlg.go(page_id)
        pump(2.0)
        shot(dlg, name)
    dlg.close()
    for pid in list(mpv_pids):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    pump(2.0)
    close(win, bridge)


def _phases(
    window: Any, close: Any, call: Any, pump: Any, wait: Any, shot: Any, controller: Any,
    root: Path, rife_model: str | None, mpv_pids: set[int],
) -> None:  # fmt: skip
    import dataclasses

    from PySide6.QtCore import Qt

    from buttereye.core.api import BackendId, FilterState, GeneralSettings
    from buttereye.core.paths import resolve
    from buttereye.core.testing import scenarios as sc

    # ---------------------------------------------------------------- phase A
    paths = resolve(os.environ)
    win, bridge = window(paths)

    # The setup wizard's first page, before any config exists.
    win.open_setup()
    pump(0.5)
    dlg: Any = win._setup_dialog
    if dlg is not None:
        shot(dlg, "setup-wizard")
        dlg.reject()
        pump(0.3)

    # Setup chose MVTools (no config otherwise: Play shows "run Setup").
    cfg = dataclasses.replace(
        sc.default_config(), general=GeneralSettings(backend_override=BackendId.MVTOOLS)
    )

    # the revision the core has loaded, not "": _simple ran first and its silent
    # first run really does write a config.toml, so "" (meaning "there is none
    # yet") is a BE-2003 conflict on any platform once that shot exists
    async def save(core: Any) -> str:
        load = await core.load_config()
        return str(await core.save_config(cfg, expected_revision=load.revision))

    call(bridge, win, save)
    close(win, bridge)
    win, bridge = window(paths)
    win.go("system")
    system: Any = win.page("system")
    pump(1.0)
    if system.report is None and not system.is_running():
        system.run_checks()
    wait(lambda: system.report is not None and not system.is_running(), 90, "doctor")

    for page_id, name in (
        ("profiles", "profiles"),
        ("render", "render"),
        ("system", "system"),
        ("storage", "storage"),
        ("about", "about"),
    ):
        win.go(page_id)
        pump(1.0)
        shot(win, name)

    # Play with a real live MVTools session (mpv without a window or sound).
    async def live_options(core: Any) -> None:
        from buttereye.core.mpvctl import session as live

        reg = live.registry(core._ctx)
        reg.options.extra_args = ("--vo=null", "--ao=null", "--display-fps-override=60")

    call(bridge, win, live_options)
    clip = make_clip(root / "test pattern 720p.mkv")
    win.go("sessions")
    pump(0.5)
    page: Any = win.page("sessions")
    page.play_file(clip)

    def active() -> bool:
        snaps = [t.snap for t in page._tracks.values()]
        for s in snaps:
            if s.pid:
                mpv_pids.add(s.pid)
        return any(s.filter is FilterState.ACTIVE for s in snaps)

    wait(active, 60, "live MVTools session")
    pump(4.0)  # counters and drop rate settle
    shot(win, "sessions")
    controller.apply_scheme(Qt.ColorScheme.Dark)
    pump(0.5)
    shot(win, "sessions-dark")
    win.go("system")
    pump(0.5)
    shot(win, "system-dark")
    controller.apply_scheme(Qt.ColorScheme.Light)
    pump(0.3)
    for pid in list(mpv_pids):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    pump(2.0)
    close(win, bridge)

    # ---------------------------------------------------------------- phase B
    if rife_model is None:
        return
    # A real short benchmark: 1280x720 23.976 -> 2x, MVTools and one RIFE model.
    share = root / "rpm-share"
    (share / MODELS_SUBDIR).mkdir(parents=True)
    # the platform's own model folder: /usr/share/buttereye on Fedora, Homebrew's
    # <prefix>/share/buttereye on a Mac, where only a tap would have put models
    models = paths.rpm_data_dir / MODELS_SUBDIR
    if (models / rife_model).is_dir():
        (share / MODELS_SUBDIR / rife_model).symlink_to(models / rife_model)
    else:
        print("no RIFE model at", models / rife_model, "- the bench shot is MVTools only")
    win, bridge = window(dataclasses.replace(paths, rpm_data_dir=share))
    win.go("bench")
    pump(1.0)
    bench: Any = win.page("bench")
    bench.width_spin.setValue(1280)
    bench.height_spin.setValue(720)
    bench.rate_edit.setText("24000/1001")
    bench.run()
    wait(lambda: bench.is_running(), 30, "bench start")
    wait(lambda: not bench.is_running(), 600, "bench finish")
    pump(1.0)
    shot(win, "bench")
    close(win, bridge)


if __name__ == "__main__":
    raise SystemExit(main())
