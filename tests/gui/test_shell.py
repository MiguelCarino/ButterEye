# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shell: registry, key map, close flow, single instance, dev flag
(docs/design/GUI.md §4.1, §3 "Close flow", R14, §7 U4)."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings, QSize, Qt
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import QAbstractButton, QApplication, QMessageBox

from buttereye.core.api import (
    CandidateId,
    DetachPolicy,
    ErrorCode,
    Feature,
    InstanceCandidate,
    JobChanged,
    JobId,
    Msg,
    OpState,
    OrphansFound,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
)
from buttereye.gui import app as gui_app
from buttereye.gui.main_window import GLOBAL_KEYS, MainWindow
from buttereye.gui.pages import PAGE_REGISTRY, PageSpec, module_present
from buttereye.gui.pages.base import MissingPage, PageAction
from tests.gui.helpers import click_modal, fake_core

PAGE_IDS = ("sessions", "profiles", "render", "bench", "system", "storage", "about")

COUNTING = PageSpec("counting", "tests.gui.shell_pages:CountingPage", "Counting", ())
DIRTY = PageSpec("dirty", "tests.gui.shell_pages:DirtyPage", "Dirty", ())


def _activate(qtbot: Any, win: MainWindow) -> None:
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)


def _spy_shutdown(win: MainWindow) -> list[tuple[DetachPolicy, bool]]:
    calls: list[tuple[DetachPolicy, bool]] = []
    real = win.bridge.shutdown

    def spy(policy: DetachPolicy, *, cancel_jobs: bool, **kw: Any) -> Any:
        calls.append((policy, cancel_jobs))
        return real(policy, cancel_jobs=cancel_jobs, **kw)

    win.bridge.shutdown = spy  # type: ignore[method-assign]
    return calls


def _emit(win: MainWindow, qtbot: Any, ev: Any) -> None:
    async def go(core: Any) -> None:
        core.fake_emit(ev)

    done: list[bool] = []
    win.bridge.call(go, owner=win, ok=lambda _v: done.append(True))
    qtbot.waitUntil(lambda: bool(done), timeout=2000)
    qtbot.wait(150)  # one drain


def _running_job() -> RenderJobState:
    spec = RenderJobSpec(
        Path("/videos/a.mkv"), Path("/videos/a.buttereye.mkv"), "balanced", "libx265"
    )
    return RenderJobState(
        JobId("job-1"), spec, OpState.RUNNING, RenderPhase.RENDER, 10, 100, 30.0, 3.0, None, None
    )


# ---------------------------------------------------------------- registry / pages


def test_registry_lists_every_page_in_order() -> None:
    assert tuple(s.page_id for s in PAGE_REGISTRY) == PAGE_IDS
    for spec in PAGE_REGISTRY:
        assert spec.module.startswith("buttereye.gui.pages.")
        assert spec.attr


def test_missing_and_broken_pages_get_placeholders(make_window: Any) -> None:
    registry = (
        PageSpec("nope", "buttereye.gui.pages.does_not_exist:Nope", "Nope", ()),
        PageSpec("broken", "tests.gui.shell_pages:BrokenPage", "Broken", ()),
        PageSpec("renamed", "tests.gui.shell_pages:WrongName", "Renamed", ()),
        COUNTING,
    )
    win = make_window("all_ready", registry=registry)
    nope, broken, renamed, counting = win.pages
    assert isinstance(nope, MissingPage)
    assert nope.code.text() == ErrorCode.NOT_IMPLEMENTED.code
    assert isinstance(broken, MissingPage)
    panel = broken.state_panel()
    assert panel is not None and panel.state == "error"
    assert type(renamed).__name__ == "Renamed"  # found by page_id
    assert type(counting).__name__ == "CountingPage"


def test_real_registry_builds_every_page(make_window: Any) -> None:
    win = make_window("devbox_now")
    assert len(win.pages) == 7
    for spec, page in zip(PAGE_REGISTRY, win.pages, strict=True):
        if not module_present(spec):
            assert isinstance(page, MissingPage)
        scroll = page.parentWidget().parentWidget()  # viewport -> QScrollArea
        assert scroll.objectName() == f"scroll.{spec.page_id}"


def test_refresh_called_on_first_show_only(make_window: Any) -> None:
    other = PageSpec("other", "tests.gui.shell_pages:CountingPage", "Other", ())
    win = make_window("all_ready", registry=(COUNTING, other))
    first, second = win.pages
    assert first.refreshes == 1 and second.refreshes == 0
    win.go("other")
    win.go("counting")
    win.go("other")
    assert first.refreshes == 1 and second.refreshes == 1


def test_events_routed_to_every_page(make_window: Any, qtbot: Any) -> None:
    other = PageSpec("other", "tests.gui.shell_pages:CountingPage", "Other", ())
    win = make_window("all_ready", registry=(COUNTING, other))
    from buttereye.core.api import Notice

    _emit(win, qtbot, Notice(Msg("hello")))
    for page in win.pages:
        assert any(isinstance(e, Notice) for e in page.events)


# ---------------------------------------------------------------- keys


def test_global_shortcuts_are_bound() -> None:
    expected = {
        "act.open": "Ctrl+O",
        "act.attach": "Ctrl+Shift+A",
        "act.quit": "Ctrl+Q",
        "act.toggle": "Ctrl+T",
        "act.detach": "Ctrl+D",
        "act.disableDetach": "Ctrl+Shift+D",
        "act.refresh": "F5",
        "act.copyCommand": "Ctrl+Shift+C",
        "act.about": "F1",
        "act.cycleFocus": "F6",
    }
    assert set(expected.values()) | {f"Ctrl+{i}" for i in range(1, 8)} == set(GLOBAL_KEYS)


def test_shortcut_objects_match_key_map(make_window: Any) -> None:
    win = make_window("devbox_now")
    by_name = {a.objectName(): a for a in win.findChildren(QAction)}
    assert by_name["act.open"].shortcut() == QKeySequence("Ctrl+O")
    assert by_name["act.disableDetach"].shortcut() == QKeySequence("Ctrl+Shift+D")
    for i, pid in enumerate(PAGE_IDS, start=1):
        assert by_name[f"act.page.{pid}"].shortcut() == QKeySequence(f"Ctrl+{i}")
    # every menu label carries a mnemonic
    for menu in win.menus:
        assert "&" in menu.title()
        for act in menu.actions():
            if not act.isSeparator():
                assert "&" in act.text(), act.text()


def test_ctrl_digits_switch_pages(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    _activate(qtbot, win)
    for i, pid in reversed(list(enumerate(PAGE_IDS, start=1))):
        qtbot.keyClick(win, getattr(Qt.Key, f"Key_{i}"), Qt.KeyboardModifier.ControlModifier)
        assert win.current_page_id() == pid
        assert win.sidebar.currentRow() == i - 1


def test_f5_refreshes_current_page(make_window: Any, qtbot: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    _activate(qtbot, win)
    page = win.pages[0]
    before = page.refreshes
    qtbot.keyClick(win, Qt.Key.Key_F5)
    assert page.refreshes == before + 1


def test_f1_opens_about(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    _activate(qtbot, win)
    qtbot.keyClick(win, Qt.Key.Key_F1)
    assert win.current_page_id() == "about"


def test_f6_cycles_sidebar_page_status(make_window: Any, qtbot: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    _activate(qtbot, win)
    win.sidebar.setFocus()
    qtbot.keyClick(win, Qt.Key.Key_F6)
    page = win.pages[0]
    assert QApplication.focusWidget() is page.edit
    qtbot.keyClick(win, Qt.Key.Key_F6)
    assert QApplication.focusWidget() is win.status_label
    qtbot.keyClick(win, Qt.Key.Key_F6)
    assert QApplication.focusWidget() is win.sidebar


def test_menu_actions_dispatch_to_pages(make_window: Any) -> None:
    sessions = PageSpec("sessions", "tests.gui.shell_pages:CountingPage", "Play", ())
    system = PageSpec("system", "tests.gui.shell_pages:CountingPage", "System", ())
    about = PageSpec("about", "tests.gui.shell_pages:CountingPage", "About", ())
    win = make_window("live_active", registry=(sessions, system, about))
    play_page, system_page, about_page = win.pages
    win.act_toggle.trigger()
    win.act_detach.trigger()
    win.act_disable_detach.trigger()
    win.act_open.trigger()
    win.act_attach.trigger()
    assert play_page.handled == [
        PageAction.TOGGLE_INTERPOLATION,
        PageAction.DETACH,
        PageAction.DISABLE_AND_DETACH,
        PageAction.OPEN_AND_PLAY,
        PageAction.ATTACH,
    ]
    win.act_report.trigger()
    win.act_checks.trigger()
    assert system_page.handled == [PageAction.CREATE_REPORT, PageAction.RUN_CHECKS]
    assert win.current_page_id() == "system"
    win.act_licences.trigger()
    assert about_page.handled == [PageAction.SHOW_LICENCES]
    assert win.current_page_id() == "about"


def test_playback_actions_follow_live_capability(make_window: Any) -> None:
    win = make_window("devbox_now")
    assert not win.act_toggle.isEnabled()
    assert "isn't in this build" in win.act_toggle.statusTip()
    win2 = make_window("live_active")
    assert win2.act_toggle.isEnabled()


def test_no_page_binds_a_global_key(make_window: Any) -> None:
    """Ambiguous shortcuts fire nothing: pages use ``handle_action`` instead."""
    win = make_window("all_ready")
    taken = {QKeySequence(k) for k in GLOBAL_KEYS}
    clashes: list[str] = []
    for page in win.pages:
        for act in page.findChildren(QAction):
            if any(s in taken for s in act.shortcuts()):
                clashes.append(f"{type(page).__name__}: action {act.text()!r}")
        for btn in page.findChildren(QAbstractButton):
            if btn.shortcut() in taken:
                clashes.append(f"{type(page).__name__}: button {btn.text()!r}")
        for sc in page.findChildren(QShortcut):
            if sc.key() in taken:
                clashes.append(f"{type(page).__name__}: shortcut {sc.key().toString()}")
    assert not clashes, clashes


def test_copy_equivalent_command(make_window: Any, qtbot: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    win.act_copy_cmd.trigger()
    assert "no equivalent command" in win.status_label.text()
    from buttereye.core.api import command_hint
    from buttereye.gui.widgets.cli_hint import CliHint

    page = win.pages[0]
    hint = CliHint(command_hint("doctor"), page)
    page.layout().addWidget(hint)
    hint.show()
    win.act_copy_cmd.trigger()
    assert QGuiApplication.clipboard().text() == "buttereye doctor"


# ---------------------------------------------------------------- status / banners


def test_announcements_mirror_into_status_bar(make_window: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    win.ctx.announce("Applied (gen 8)")
    assert win.status_label.text() == "Applied (gen 8)"
    win.ctx.status("Just text")
    assert win.status_label.text() == "Just text"
    from buttereye.gui import a11y

    a11y.announce(win.pages[0], "From a page")
    assert win.status_label.text() == "From a page"


def test_orphans_banner(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    cand = InstanceCandidate(
        CandidateId("c1"), tmp_path / "mpv.sock", 4211, "film.mkv", None, None, True, True
    )
    _emit(win, qtbot, OrphansFound((cand,)))
    banner = win.banner("orphans")
    assert banner is not None
    assert "1 player" in banner.title()


def test_fatal_panel_shows_code_and_quit(qtbot: Any, tmp_path: Path) -> None:
    from buttereye.core.api import ButterEyeError
    from buttereye.gui.bridge import CoreBridge

    async def failing(paths: Any) -> Any:
        raise ButterEyeError(
            ErrorCode.RUNTIME_DIR_UNSAFE, Msg("Runtime dir is unsafe."), Msg("Fix permissions.")
        )

    bridge = CoreBridge(opener=failing)
    settings = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    win = MainWindow(bridge, settings, log_path=tmp_path / "gui.log")
    qtbot.addWidget(win)
    with qtbot.waitSignal(bridge.fatal, timeout=3000):
        bridge.start()
    assert win.root.currentWidget() is win.fatal_panel
    banner = win.fatal_panel.findChild(QAbstractButton, "fatalQuit")
    assert banner is not None
    texts = " ".join(w.text() for w in win.fatal_panel.findChildren(type(win.status_label)))
    assert "BE-2010" in texts and "gui.log" in texts
    win.show()
    banner.click()
    assert not win.isVisible()
    assert bridge.is_closed


def test_setup_offered_when_config_missing(make_window: Any, qtbot: Any) -> None:
    win = make_window("fresh_box", open_setup_when_missing=True)
    qtbot.waitUntil(lambda: win.config_load is not None, timeout=3000)
    assert win.config_load is not None and not win.config_load.exists
    if module_present(PageSpec("x", "buttereye.gui.dialogs.setup_wizard:X", "x", ())):
        qtbot.waitUntil(lambda: win._setup_dialog is not None, timeout=3000)
        win._setup_dialog.reject()
    else:
        banner = win.banner("setup")
        assert banner is not None and "isn't in this build" in banner.title()


def test_unsaved_changes_guard(make_window: Any) -> None:
    win = make_window("all_ready", registry=(DIRTY, COUNTING))
    assert win.current_page_id() == "dirty"
    log = click_modal(None, "stay")
    win.go("counting")
    assert log == ["discardChanges"]
    assert win.current_page_id() == "dirty"
    assert win.sidebar.currentRow() == 0
    click_modal(None, "discard")
    win.go("counting")
    assert win.current_page_id() == "counting"


def test_window_size_saved_but_not_position(make_window: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    win.resize(QSize(900, 640))
    win._may_close = True
    win.close()
    size = win.settings.value("window/size")
    assert isinstance(size, QSize) and size == QSize(900, 640)
    assert not [k for k in win.settings.allKeys() if "pos" in k.lower() or "geometry" in k.lower()]


# ---------------------------------------------------------------- close flow


def test_quit_without_sessions_closes_at_once(make_window: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    calls = _spy_shutdown(win)
    win.act_quit.trigger()
    assert not win.isVisible()
    assert calls == [(DetachPolicy.KEEP_FILTER, False)]
    assert win.last_shutdown is not None


def test_quit_with_live_sessions_keep_window_open(make_window: Any) -> None:
    win = make_window("live_active", registry=(COUNTING,))
    assert win.live_sessions
    calls = _spy_shutdown(win)
    log = click_modal(None, "quit.keepOpen")
    win.act_quit.trigger()
    assert log == ["quit.liveSessions"]
    assert win.isVisible()
    assert calls == []
    assert not win.bridge.is_closed


@pytest.mark.parametrize(
    ("check", "policy"),
    [(None, DetachPolicy.KEEP_FILTER), ("quit.disableFilter", DetachPolicy.DISABLE_FILTER)],
)
def test_quit_with_live_sessions_policy(
    make_window: Any, check: str | None, policy: DetachPolicy
) -> None:
    win = make_window("live_active", registry=(COUNTING,))
    calls = _spy_shutdown(win)
    core = fake_core(win.bridge)
    click_modal(None, "quit.close", check=check)
    win.act_quit.trigger()
    assert not win.isVisible()
    assert calls == [(policy, False)]
    close = [c for c in core.fake_calls if c.name == "close"]
    assert close and close[0].args == (policy,)
    assert win.last_shutdown is not None and win.last_shutdown.detached


def test_quit_with_running_render(make_window: Any, qtbot: Any) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    _emit(win, qtbot, JobChanged(_running_job()))
    calls = _spy_shutdown(win)
    log = click_modal(None, "quit.keepOpen")
    win.act_quit.trigger()
    assert log == ["quit.renderRunning"]
    assert win.isVisible() and calls == []
    click_modal(None, "quit.cancelRender")
    win.act_quit.trigger()
    assert not win.isVisible()
    assert calls == [(DetachPolicy.KEEP_FILTER, True)]


def test_quit_detach_failure_lists_players(make_window: Any, monkeypatch: Any) -> None:
    win = make_window("live_active", registry=(COUNTING,))
    titles = [s.title for s in win.live_sessions]
    real = win.bridge.shutdown

    def timing_out(policy: DetachPolicy, *, cancel_jobs: bool, **kw: Any) -> None:
        real(policy, cancel_jobs=cancel_jobs, **kw)
        return None

    monkeypatch.setattr(win.bridge, "shutdown", timing_out)
    shown: list[str] = []
    from buttereye.gui import quit_dialog

    monkeypatch.setattr(quit_dialog, "show_detach_failed", lambda t, parent: shown.extend(t))
    click_modal(None, "quit.close")
    win.act_quit.trigger()
    assert not win.isVisible()
    assert shown == titles


def test_quit_needs_ignores_ended_sessions_and_finished_jobs() -> None:
    import dataclasses

    from buttereye.core.testing import scenarios
    from buttereye.gui.quit_dialog import quit_needs

    live = scenarios.get("live_active").sessions
    ended = tuple(dataclasses.replace(s, ended=True) for s in live)
    done = dataclasses.replace(_running_job(), state=OpState.SUCCEEDED)
    assert quit_needs(ended, (done,)).trivial
    queued = dataclasses.replace(_running_job(), state=OpState.QUEUED)
    needs = quit_needs(live, (queued,))
    assert needs.live and not needs.running_jobs and needs.queued_jobs


# ---------------------------------------------------------------- single instance / dev flag


def test_single_instance_lock(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    path = runtime / "buttereye" / "gui.lock"
    first = gui_app.InstanceLock(path)
    first.acquire()
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    second = gui_app.InstanceLock(path)
    with pytest.raises(gui_app.AlreadyRunning):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_lock_refuses_unsafe_dir_without_chmod(tmp_path: Path) -> None:
    unsafe = tmp_path / "run" / "buttereye"
    unsafe.mkdir(parents=True)
    unsafe.chmod(0o755)
    with pytest.raises(gui_app.UnsafeRuntimeDir):
        gui_app.InstanceLock(unsafe / "gui.lock").acquire()
    assert stat.S_IMODE(os.stat(unsafe).st_mode) == 0o755  # never chmod-ed


def test_second_launch_says_already_open(
    xdg_env: Any, monkeypatch: Any, capsys: Any, qapp: Any
) -> None:
    lock_path = gui_app.lock_path(os.environ)
    assert lock_path is not None and str(xdg_env.runtime) in str(lock_path)
    holder = gui_app.InstanceLock(lock_path)
    holder.acquire()
    shown: list[str] = []
    formats: list[Any] = []

    def fake_exec(box: QMessageBox) -> int:
        shown.append(box.text())
        formats.append(box.textFormat())
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        rc = gui_app.main(["buttereye-gui"])
    finally:
        holder.release()
    assert rc == ErrorCode.GUI_ALREADY_RUNNING.exit_code
    assert shown == ["ButterEye is already open."]
    assert formats == [Qt.TextFormat.PlainText]
    assert "ButterEye is already open." in capsys.readouterr().err


def test_fake_flag_needs_dev_env() -> None:
    with pytest.raises(SystemExit) as exc:
        gui_app.parse_args(["--fake", "devbox_now"], {})
    assert exc.value.code == 2
    ns, rest = gui_app.parse_args(
        ["--fake", "devbox_now", "-platform", "offscreen"], {"BUTTEREYE_DEV": "1"}
    )
    assert ns.fake == "devbox_now" and rest == ["-platform", "offscreen"]
    ns, _ = gui_app.parse_args([], {})
    assert ns.fake is None


def test_default_path_does_not_load_fakes() -> None:
    """The shipped modules never import buttereye.core.testing statically."""
    import ast

    root = Path(gui_app.__file__).resolve().parent
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("buttereye.core.testing"), path
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith("buttereye.core.testing") for a in node.names)


def test_desktop_file_name(qapp: Any) -> None:
    gui_app.configure_app(qapp)
    assert QGuiApplication.desktopFileName() == "io.github.buttereye.ButterEye"


def test_unknown_feature_states_do_not_break_shell(make_window: Any) -> None:
    win = make_window("cpu_only")
    assert win.bridge.capabilities is not None
    assert Feature.LIVE in win.bridge.capabilities.states


# ---------------------------------------------------------------- review fixes


DECORATED = PageSpec("decorated", "tests.gui.shell_pages:DecoratedDirtyPage", "Dirty", ())


def _modal_text_then_click(object_name: str) -> list[Any]:
    """Record the next modal QMessageBox's label (format, text), then click."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QLabel

    seen: list[Any] = []

    def attempt(tries: int = 0) -> None:
        modal = QApplication.activeModalWidget()
        if modal is None:
            if tries < 100:
                QTimer.singleShot(20, lambda: attempt(tries + 1))
            return
        label = modal.findChild(QLabel, "qt_msgbox_label")
        if isinstance(modal, QMessageBox) and label is not None:
            seen.append((modal.textFormat(), label.textFormat(), label.text()))
        btn = modal.findChild(QAbstractButton, object_name)
        if btn is None:
            modal.close()
        else:
            btn.click()

    QTimer.singleShot(0, attempt)
    return seen


def test_discard_prompt_uses_plain_page_name(make_window: Any) -> None:
    """Not "Discard unsaved changes to Dirty — unsaved changes *?"."""
    win = make_window("all_ready", registry=(DECORATED, COUNTING))
    assert win.windowTitle().startswith("Dirty — unsaved changes *")  # kept there
    seen = _modal_text_then_click("stay")
    win.go("counting")
    assert seen, "no discard prompt"
    text = seen[0][2]
    assert text == "Discard unsaved changes to Dirty?"
    assert "*" not in text and "unsaved changes *" not in text


def test_message_boxes_are_plain_text(qapp: Any) -> None:
    """A media title with markup must show literally (finding: rich-text spoofing)."""
    from buttereye.gui import quit_dialog

    hostile = "<b>x</b><!--"
    seen = _modal_text_then_click("quit.ok")
    quit_dialog.show_detach_failed((hostile,), None)
    assert seen
    box_fmt, label_fmt, text = seen[0]
    assert box_fmt == Qt.TextFormat.PlainText and label_fmt == Qt.TextFormat.PlainText
    assert hostile in text
    seen = _modal_text_then_click("quit.keepOpen")
    assert quit_dialog.ask_render_running(None) is False
    assert seen and seen[0][0] == Qt.TextFormat.PlainText


#: QMessageBox sites in other areas still to move to a11y.message_box (reported).
_MSGBOX_PENDING: frozenset[str] = frozenset()


def test_every_message_box_goes_through_the_plain_helper() -> None:
    import re

    repo = Path(__file__).resolve().parents[2]
    pattern = re.compile(r"QMessageBox\(|QMessageBox\.(information|warning|question|critical)\(")
    offenders = []
    for path in sorted((repo / "buttereye" / "gui").rglob("*.py")):
        rel = path.relative_to(repo).as_posix()
        if rel == "buttereye/gui/a11y.py" or rel in _MSGBOX_PENDING:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "use a11y.message_box():\n" + "\n".join(offenders)


def _caps_with(win: MainWindow, feature: Feature, available: bool) -> Any:
    from buttereye.core.api import Capabilities, CapState

    caps = win.bridge.capabilities
    assert caps is not None
    states = dict(caps.states)
    states[feature] = CapState(available, code=None if available else ErrorCode.NOT_IMPLEMENTED)
    return Capabilities(states)


def test_file_attach_is_disabled_while_attach_is_unavailable(make_window: Any, qtbot: Any) -> None:
    from buttereye.core.api import Capabilities, CapabilitiesChanged, Reason
    from buttereye.core.capabilities import unavailable_state

    win = make_window("all_ready", registry=(COUNTING,))
    caps = win.bridge.capabilities
    assert caps is not None
    states = dict(caps.states)
    states[Feature.ATTACH] = unavailable_state(
        Feature.ATTACH, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED
    )
    _emit(win, qtbot, CapabilitiesChanged(Capabilities(states)))
    assert not win.act_attach.isEnabled()
    assert win.act_attach.toolTip().startswith("Attaching to an mpv you started yourself")
    _emit(win, qtbot, CapabilitiesChanged(_caps_with(win, Feature.ATTACH, True)))
    assert win.act_attach.isEnabled() and win.act_attach.statusTip() == ""


@pytest.mark.parametrize("attach", [True, False])
def test_orphans_banner_follows_attach(
    make_window: Any, qtbot: Any, tmp_path: Path, attach: bool
) -> None:
    from buttereye.core.api import CapabilitiesChanged

    win = make_window("all_ready", registry=(COUNTING,))
    _emit(win, qtbot, CapabilitiesChanged(_caps_with(win, Feature.ATTACH, attach)))
    cands = tuple(
        InstanceCandidate(
            CandidateId(f"c{i}"),
            tmp_path / f"mpv{i}.sock",
            4211 + i,
            "film.mkv",
            None,
            None,
            True,
            True,
        )
        for i in range(2)
    )
    _emit(win, qtbot, OrphansFound(cands))
    banner = win.banner("orphans")
    assert banner is not None
    assert banner.title() == "ButterEye found 2 players it started earlier."
    names = [b.objectName() or b.text() for b in banner.buttons]
    if attach:
        assert banner.body() == "They keep playing. Attach to them from the Play page."
        assert any("Play page" in n for n in names)
    else:
        assert "can't reconnect" in banner.body() and "press q" in banner.body()
        assert "Attach" not in banner.body()
        assert len(banner.buttons) == 1 and "Dismiss" in banner.buttons[0].text()
    # Capability flips re-word the banner by themselves.
    _emit(win, qtbot, CapabilitiesChanged(_caps_with(win, Feature.ATTACH, not attach)))
    banner = win.banner("orphans")
    assert banner is not None
    assert ("Attach to them" in banner.body()) is (not attach)


def test_orphans_banner_singular(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win = make_window("all_ready", registry=(COUNTING,))
    cand = InstanceCandidate(
        CandidateId("c1"), tmp_path / "mpv.sock", 4211, "film.mkv", None, None, True, True
    )
    _emit(win, qtbot, OrphansFound((cand,)))
    banner = win.banner("orphans")
    assert banner is not None
    assert banner.title() == "ButterEye found 1 player it started earlier."


def test_tab_goes_sidebar_then_page_never_scroll_area(make_window: Any, qtbot: Any) -> None:
    from PySide6.QtWidgets import QScrollArea

    win = make_window("all_ready")
    _activate(qtbot, win)
    for row in range(len(win.pages)):
        win.sidebar.setCurrentRow(row)
        win.sidebar.setFocus()
        qtbot.keyClick(QApplication.focusWidget(), Qt.Key.Key_Tab)
        fw = QApplication.focusWidget()
        assert not isinstance(fw, QScrollArea), f"page {row}: Tab landed on {fw.objectName()}"
        assert fw is not win.status_label, f"page {row}: Tab went to the status bar"
        current = win.page_stack.currentWidget()
        if fw is not win.sidebar:  # a page with nothing focusable keeps the cycle short
            assert current.isAncestorOf(fw), f"page {row}: {type(fw).__name__}"
    for slot in win._slots:
        assert slot.scroll.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert not (win.status_label.focusPolicy() & Qt.FocusPolicy.TabFocus)
    assert all(
        not (w.focusPolicy() & Qt.FocusPolicy.TabFocus)
        for w in win.page_stack.findChildren(QScrollArea)
        if w.objectName().startswith("scroll.")
    )


def test_first_launch_size_is_usable(make_window: Any) -> None:
    from buttereye.gui.main_window import DEFAULT_SIZE

    win = make_window("all_ready")  # fresh QSettings: no window/size
    screen = win.screen() or QGuiApplication.primaryScreen()
    avail = screen.availableGeometry().size()
    assert win.width() >= min(DEFAULT_SIZE.width(), int(avail.width() * 0.9))
    assert win.height() >= min(DEFAULT_SIZE.height(), int(avail.height() * 0.9))
    assert win.width() <= avail.width() and win.height() <= avail.height()
    assert win.minimumWidth() >= min(640, int(avail.width() * 0.9))


def test_saved_size_is_clamped_to_the_screen(make_bridge: Any, tmp_path: Path, qtbot: Any) -> None:
    settings = QSettings(str(tmp_path / "big.ini"), QSettings.Format.IniFormat)
    settings.setValue("window/size", QSize(9000, 7000))
    win = MainWindow(make_bridge("all_ready"), settings, open_setup_when_missing=False)
    win._may_close = True
    qtbot.addWidget(win)
    avail = (win.screen() or QGuiApplication.primaryScreen()).availableGeometry().size()
    assert win.width() <= avail.width() and win.height() <= avail.height()


def test_sidebar_fits_its_items(make_window: Any) -> None:
    win = make_window("all_ready")
    sb = win.sidebar
    assert sb.width() >= sb.sizeHintForColumn(0)
    assert sb.width() < 256  # QListWidget's generic hint wasted a third of the window
    assert sb.minimumWidth() == sb.maximumWidth()
