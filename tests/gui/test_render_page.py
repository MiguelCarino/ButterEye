# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Render page and add-job dialog (docs/design/GUI.md §4.5, §6 ``test_render_page``; U9).

A refusal disables Add with the reason beside it; the encoder combo lists only
probed encoders; progress never goes backwards; the stale-job banner; no Pause
or "stop and keep" control exists; the unavailable panels (BE-9001 today,
BE-4001 + the dnf line once render exists and ffms2 is missing).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import MappingProxyType
from typing import Any

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QAbstractButton

from buttereye.core.api import (
    ButterEyeError,
    ErrorCode,
    Feature,
    JobChanged,
    JobId,
    Msg,
    OpState,
    Reason,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
)
from buttereye.core.capabilities import unavailable_state
from buttereye.core.testing import scenarios as sc
from buttereye.gui.dialogs.render_job import RenderJobDialog, default_output
from buttereye.gui.pages.render import COL_PROGRESS, COL_STATUS, PROGRESS_ROLE, RenderPage
from tests.gui.helpers import fake_core


def _page(win: Any, qtbot: Any) -> RenderPage:
    win.go("render")
    page = win.page("render")
    assert isinstance(page, RenderPage)
    qtbot.waitUntil(lambda: page.panel.state != "loading", timeout=3000)
    return page


def _scenario(base: str = "render_refusals", **changes: Any) -> sc.Scenario:
    return dataclasses.replace(sc.get(base), **changes)


def _calls(win: Any, name: str) -> list[Any]:
    return [c for c in fake_core(win.bridge).fake_calls if c.name == name]


def _job(
    n: int, state: OpState, done: int | None = None, total: int | None = 1000
) -> RenderJobState:
    spec = RenderJobSpec(
        Path(f"/videos/ep{n}.mkv"), Path(f"/videos/ep{n}.buttereye.mkv"), "quality", "hevc_nvenc"
    )
    return RenderJobState(JobId(f"j{n}"), spec, state, None, done, total, None, None, None, None)


def _buttons_text(w: Any) -> list[str]:
    return [b.text().replace("&", "").lower() for b in w.findChildren(QAbstractButton)]


def _row_of(page: RenderPage, jid: str) -> int:
    for r in range(page.model.rowCount()):
        if page.model.item(r, 0).data(PROGRESS_ROLE + 1) == jid:
            return r
    raise AssertionError(jid)


# ---------------------------------------------------------------- unavailable
def test_unavailable_today_shows_be9001_and_no_rows(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    page = _page(win, qtbot)
    assert page.panel.state == "unavailable"
    head, _body, code = page.panel.unavailable_texts()
    assert head == "Offline render isn't in this build yet."
    assert code == ErrorCode.NOT_IMPLEMENTED.code
    assert page.model.rowCount() == 0
    hint_text = page.panel.unavailable_hint.text()
    assert hint_text is not None and hint_text.startswith("buttereye render ")
    assert page.add_job() is None
    assert not _calls(win, "stale_jobs")  # the page fetched nothing (the shell seeds jobs)


def test_ffms2_missing_shows_be4001_and_the_dnf_line(make_window: Any, qtbot: Any) -> None:
    caps = dict(sc.get("all_ready").capabilities)
    st = unavailable_state(
        Feature.RENDER,
        Reason.MISSING_DEPENDENCY,
        ErrorCode.FFMS2_MISSING,
        commands=("sudo dnf install ffms2",),
    )
    caps[Feature.RENDER] = st
    caps[Feature.RENDER_HDR10] = st
    win = make_window(_scenario("all_ready", capabilities=MappingProxyType(caps)))
    page = _page(win, qtbot)
    assert page.panel.state == "unavailable"
    head, body, code = page.panel.unavailable_texts()
    assert head == "Render needs ffms2."
    assert "Install it, then press F5" in body
    assert code == ErrorCode.FFMS2_MISSING.code
    assert [f.text() for f in page.panel.unavailable_commands] == ["sudo dnf install ffms2"]
    assert page.model.rowCount() == 0


def test_capability_change_switches_to_content(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    page = _page(win, qtbot)
    assert page.panel.state == "unavailable"
    core = fake_core(win.bridge)
    win.bridge.call(
        lambda c: _set_cap(c, Feature.RENDER), owner=win, ok=lambda _v: None, err=lambda _e: None
    )
    qtbot.waitUntil(lambda: _state(page) == "content", timeout=3000)
    assert core is not None


def _state(page: RenderPage) -> str:
    return page.panel.state


async def _set_cap(core: Any, f: Feature) -> None:
    from buttereye.core.api import CapState

    core.fake_set_capability(f, CapState(True))


# ---------------------------------------------------------------- queue
def test_queue_rows_status_words_and_no_pause(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    page = _page(win, qtbot)
    assert page.panel.state == "content"
    assert page.queue_panel.state == "content"
    assert page.model.rowCount() == 3
    words = [page.model.item(r, COL_STATUS).text() for r in range(3)]
    assert words == ["Finished", "Failed (BE-4006)", "Queued"]
    assert all(not page.model.item(r, COL_STATUS).icon().isNull() for r in range(3))
    texts = _buttons_text(page)
    assert not any("pause" in t or "stop and keep" in t or "resume" in t for t in texts)
    page.select_job(JobId("j2"))
    assert page.detail_banner is not None
    assert page.detail_banner.code == ErrorCode.ENCODER_FAILED.code
    page.select_job(JobId("j1"))
    assert page.folder_button.isEnabled()
    assert page.remove_button.text() == "&Remove from list"
    page.select_job(JobId("j3"))
    assert page.remove_button.text() == "&Cancel render"
    hint = page.cli_hint()
    assert hint is not None
    assert hint.text() == (
        "buttereye render /videos/ep3.mkv -o /videos/ep3.buttereye.mkv --profile quality "
        "--encoder hevc_nvenc"
    )


def test_progress_never_goes_backwards(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario(render_jobs=(), stale_jobs=()))
    page = _page(win, qtbot)
    seen: list[int] = []
    for done in (100, 400, 250, 600, None, 500):
        job = dataclasses.replace(
            _job(9, OpState.RUNNING, done), phase=RenderPhase.RENDER, fps=61.3
        )
        page.on_event(JobChanged(job))
        seen.append(page.model.item(0, COL_PROGRESS).data(PROGRESS_ROLE))
    assert seen == [100, 400, 400, 600, 600, 600]
    assert page.model.item(0, COL_PROGRESS).text() == "60% · 61.3 fps"
    unknown = dataclasses.replace(_job(8, OpState.RUNNING, None, None))
    page.on_event(JobChanged(unknown))
    r = _row_of(page, "j8")
    assert page.model.item(r, COL_PROGRESS).data(PROGRESS_ROLE) == -1  # indeterminate
    assert page.model.item(r, COL_PROGRESS).text() == "Working…"
    page.on_event(JobChanged(dataclasses.replace(_job(9, OpState.SUCCEEDED, 600))))
    assert page.model.item(_row_of(page, "j9"), COL_PROGRESS).data(PROGRESS_ROLE) == 1000


def test_real_queue_run_is_monotonic_and_announced(make_window: Any, qtbot: Any) -> None:
    from buttereye.gui.a11y import add_announcement_listener

    win = make_window(_scenario("all_ready", op_step_s=0.03, op_steps=6))
    page = _page(win, qtbot)
    values: list[int] = []
    announced: list[str] = []
    orig = page._job_changed

    def spy(job: RenderJobState) -> None:
        orig(job)
        values.append(page.progress_of(job.id))

    page._job_changed = spy  # type: ignore[method-assign]
    remove = add_announcement_listener(lambda _w, t, _a: announced.append(t))
    try:
        dlg = page.add_job(Path("/videos/film.mkv"))
        assert dlg is not None
        qtbot.waitUntil(lambda: dlg.add_button.isEnabled(), timeout=3000)
        dlg.add_button.click()
        qtbot.waitUntil(
            lambda: any(j.state is OpState.SUCCEEDED for j in page.jobs()), timeout=5000
        )
    finally:
        remove()
    assert values == sorted(values)
    assert values[-1] == 1000
    assert any(a.startswith("Render finished: film.mkv") for a in announced)
    (call,) = _calls(win, "render_enqueue")
    spec = call.args[0]
    assert spec.output == Path("/videos/film.buttereye.mkv")
    assert spec.encoder in sc.render_probe(Path("/videos/film.mkv")).encoders


def test_delete_cancels_with_confirmation_or_forgets(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    page = _page(win, qtbot)
    asked: list[str] = []

    def refuse(_w: Any, _title: str, text: str, _yes: str, _no: str) -> bool:
        asked.append(text)
        return False

    page.confirm = refuse
    page.select_job(JobId("j3"))
    page.shortcuts["delete"].activated.emit()
    qtbot.wait(50)
    assert asked and "ep3.mkv" in asked[0]
    assert not _calls(win, "render_cancel")
    page.confirm = lambda *_a: True
    page.cancel_or_forget()
    qtbot.waitUntil(lambda: bool(_calls(win, "render_cancel")), timeout=2000)
    qtbot.waitUntil(lambda: page.jobs()[2].state is OpState.CANCELLED, timeout=2000)
    assert page.model.item(2, COL_STATUS).text() == "Cancelled — partial files removed."
    page.select_job(JobId("j1"))
    page.cancel_or_forget()  # finished: removed without asking
    qtbot.waitUntil(lambda: page.model.rowCount() == 2, timeout=2000)
    assert _calls(win, "render_forget")[0].args[0] == "j1"


def test_alt_up_down_reorders_queued_jobs(make_window: Any, qtbot: Any) -> None:
    jobs = (_job(1, OpState.SUCCEEDED, 1000), _job(2, OpState.QUEUED), _job(3, OpState.QUEUED))
    win = make_window(_scenario(render_jobs=jobs, stale_jobs=()))
    page = _page(win, qtbot)
    page.select_job(JobId("j2"))
    assert not page.up_button.isEnabled() and page.down_button.isEnabled()
    page.shortcuts["down"].activated.emit()
    qtbot.waitUntil(lambda: [j.id for j in page.jobs()] == ["j1", "j3", "j2"], timeout=2000)
    assert _calls(win, "render_move")[0].args == ("j2", 1)
    sel = page.selected_job()
    assert sel is not None and sel.id == "j2"
    page.shortcuts["up"].activated.emit()
    qtbot.waitUntil(lambda: [j.id for j in page.jobs()] == ["j1", "j2", "j3"], timeout=2000)


def test_stale_job_banner_and_stop(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    page = _page(win, qtbot)
    qtbot.waitUntil(lambda: bool(page.stale_banners()), timeout=2000)
    banner = page.stale_banners()[JobId("j0")]
    assert banner.title() == "A render from a previous session is still running (PID 1234)."
    assert "old.buttereye.mkv.part" in banner.body()
    stop = next(b for b in banner.buttons if "Stop it" in b.text())
    stop.click()
    qtbot.waitUntil(lambda: not page.stale_banners(), timeout=2000)
    assert _calls(win, "stale_job_stop")[0].args[0] == "j0"


def test_playback_notice_when_a_session_is_active(make_window: Any, qtbot: Any) -> None:
    win = make_window("live_active")
    page = _page(win, qtbot)
    qtbot.waitUntil(lambda: page.playback_notice.isVisibleTo(page), timeout=2000)
    assert page.playback_notice.text() == "Playback is running: renders use lower priority."


def test_show_in_folder_and_open_log(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win = make_window("render_refusals")
    page = _page(win, qtbot)
    opened: list[QUrl] = []
    page.open_url = opened.append
    qtbot.waitUntil(lambda: page._paths is not None, timeout=2000)
    assert page._paths is not None
    page._paths = dataclasses.replace(page._paths, jobs_dir=tmp_path)
    (tmp_path / "j1").mkdir()
    page.select_job(JobId("j1"))
    page.show_in_folder()
    page.shortcuts["log"].activated.emit()
    assert [u.toLocalFile() for u in opened] == ["/videos", str(tmp_path / "j1")]
    page.select_job(JobId("j3"))
    page.open_log()  # no folder yet: status text, nothing opened
    assert len(opened) == 2


# ---------------------------------------------------------------- add dialog
def _dialog(win: Any, qtbot: Any, name: str) -> RenderJobDialog:
    page = _page(win, qtbot)
    dlg = page.add_job(Path(f"/videos/{name}"))
    assert dlg is not None
    qtbot.waitUntil(lambda: dlg.panel.state in ("content", "error"), timeout=3000)
    qtbot.waitUntil(lambda: dlg.profile_combo.count() > 0, timeout=3000)
    return dlg


def test_refusals_disable_add_with_the_reason_beside_it(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    for name, code in (
        ("hlg.mkv", ErrorCode.HDR_CLASS_REFUSED),
        ("dv.mkv", ErrorCode.HDR_CLASS_REFUSED),
        ("undecodable.mkv", ErrorCode.CODEC_NOT_DECODABLE),
        ("nospace.mkv", ErrorCode.NO_SPACE),
    ):
        dlg = _dialog(win, qtbot, name)
        assert not dlg.add_button.isEnabled(), name
        assert code.code in dlg.reason_label.text(), (name, dlg.reason_label.text())
        assert dlg.reason_label.isVisibleTo(dlg)
        assert dlg.refusal_banner is not None and dlg.refusal_banner.code == code.code
        assert dlg.add_button.accessibleDescription() == dlg.reason_label.text()
        dlg.add()  # refused before any core call
        dlg.reject()
    qtbot.wait(50)
    assert not _calls(win, "render_enqueue")


def test_encoder_combo_lists_only_probed_encoders(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    dlg = _dialog(win, qtbot, "hdr10.mkv")
    items = [dlg.encoder_combo.itemText(i) for i in range(dlg.encoder_combo.count())]
    assert items == ["libx265", "libsvtav1"]
    assert dlg.encoder_combo.currentText() == "libx265"
    assert dlg.add_button.isEnabled()
    assert dlg.hdr_badge.text().startswith("HDR10 video")
    dlg.reject()
    dlg = _dialog(win, qtbot, "film.mkv")
    items = [dlg.encoder_combo.itemText(i) for i in range(dlg.encoder_combo.count())]
    assert tuple(items) == sc.render_probe(Path("/videos/film.mkv")).encoders
    assert dlg.encoder_combo.currentText() == "hevc_nvenc"
    dlg.reject()


def test_hdr10_needs_its_capability(make_window: Any, qtbot: Any) -> None:
    caps = dict(sc.get("render_refusals").capabilities)
    caps[Feature.RENDER_HDR10] = unavailable_state(
        Feature.RENDER_HDR10, Reason.MISSING_DEPENDENCY, None
    )
    win = make_window(_scenario(capabilities=MappingProxyType(caps)))
    dlg = _dialog(win, qtbot, "hdr10.mkv")
    assert not dlg.add_button.isEnabled()
    assert "x265 or SVT-AV1" in dlg.reason_label.text()
    dlg.reject()


def test_vfr_streams_and_ffmpeg_remux_notes(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    dlg = _dialog(win, qtbot, "vfr.mkv")
    assert dlg.vfr_label.isVisibleTo(dlg)
    assert dlg.vfr_label.text() == "Will be converted to constant frame rate."
    assert dlg.streams_label.text() == ("Kept: 2 audio tracks, 3 subtitles, chapters, 1 attachment")
    assert dlg.remux_label.text() == "Remux: mkvmerge not installed — using ffmpeg"
    assert dlg.remux_command.isVisibleTo(dlg)
    assert dlg.remux_command.text() == "sudo dnf install mkvtoolnix"
    assert dlg.output_edit.text() == str(default_output(Path("/videos/vfr.mkv")))
    assert dlg.output_edit.text().endswith("vfr.buttereye.mkv")
    dlg.reject()
    dlg = _dialog(win, qtbot, "film.mkv")
    assert not dlg.vfr_label.isVisibleTo(dlg)
    assert dlg.remux_label.text() == "Remux: mkvmerge"
    assert not dlg.remux_command.isVisibleTo(dlg)
    dlg.reject()


def test_output_must_be_mkv_and_not_the_source(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    dlg = _dialog(win, qtbot, "film.mkv")
    dlg.output_edit.setText("/videos/out.mp4")
    assert not dlg.add_button.isEnabled()
    assert "Matroska" in dlg.reason_label.text()
    dlg.output_edit.setText("/videos/film.mkv")
    assert not dlg.add_button.isEnabled()
    dlg.choose_output = lambda: Path("/elsewhere/new.mkv")
    dlg.output_choose.click()
    assert dlg.output_edit.text() == "/elsewhere/new.mkv"
    assert dlg.add_button.isEnabled()
    dlg.reject()


def test_existing_output_needs_confirmation(make_window: Any, qtbot: Any) -> None:
    err = ButterEyeError(ErrorCode.OUTPUT_EXISTS, Msg("The output file already exists."))
    win = make_window(_scenario(errors={"render_enqueue": err}))
    dlg = _dialog(win, qtbot, "film.mkv")
    asked: list[str] = []

    def refuse(_w: Any, _title: str, text: str) -> bool:
        asked.append(text)
        return False

    dlg.confirm = refuse
    dlg.add_button.click()
    qtbot.waitUntil(lambda: bool(asked), timeout=2000)
    assert len(_calls(win, "render_enqueue")) == 1
    assert "exists" in dlg.reason_label.text()
    dlg.confirm = lambda *_a: True
    dlg.add()
    qtbot.waitUntil(lambda: len(_calls(win, "render_enqueue")) == 3, timeout=2000)
    assert _calls(win, "render_enqueue")[2].args[0].overwrite is True
    assert dlg.job_id is None  # the fake keeps refusing; the dialog stays open
    dlg.reject()


def test_dialog_has_no_pause_and_names_its_inputs(make_window: Any, qtbot: Any) -> None:
    win = make_window("render_refusals")
    dlg = _dialog(win, qtbot, "film.mkv")
    assert not any("pause" in t for t in _buttons_text(dlg))
    for w in (dlg.profile_combo, dlg.encoder_combo, dlg.output_edit):
        assert w.accessibleName()
    assert dlg.profile_combo.currentData() == "balanced"
    dlg.reject()


def test_dialog_a11y_names_buddies_and_tab_cycle(make_window: Any, qtbot: Any) -> None:
    from PySide6.QtWidgets import QApplication

    from tests.gui.test_a11y_sweep import check_names_and_buddies, check_tab_cycle

    win = make_window("render_refusals")
    dlg = _dialog(win, qtbot, "vfr.mkv")
    qtbot.waitExposed(dlg)
    dlg.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is dlg, timeout=2000)
    problems = check_names_and_buddies(dlg, dlg)
    problems += check_tab_cycle(dlg, dlg.drop, qtbot)
    assert not problems, "\n".join(problems)
    dlg.reject()


# ---------------------------------------------------------------- Ctrl+N scope
def test_ctrl_n_works_with_focus_in_the_sidebar(make_window: Any, qtbot: Any) -> None:
    """The empty-queue text promises Ctrl+N; it must work right after switching to
    Render from the sidebar, and only while Render is the current page."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    win = make_window("render_refusals")
    win.activateWindow()
    qtbot.waitUntil(lambda: QApplication.activeWindow() is win, timeout=2000)
    page = win.page("render")
    win.go("system")
    win.sidebar.setFocus()
    QTest.keyClick(win.sidebar, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    qtbot.wait(50)
    assert page.dialog is None  # hidden page: its window keys are off
    page = _page(win, qtbot)
    win.sidebar.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is win.sidebar, timeout=2000)
    QTest.keyClick(win.sidebar, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: isinstance(page.dialog, RenderJobDialog), timeout=2000)
    assert page.dialog is not None
    page.dialog.reject()
