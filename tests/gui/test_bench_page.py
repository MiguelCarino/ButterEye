# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark page (docs/design/GUI.md §4.6, §6 ``test_bench_page``; U9).

Table and plot show the same values; a faulting configuration is never
recommended (even when a result claims it); empty and unavailable texts; a run
goes through the bridge with progress rows and can be cancelled; apply passes
the freshly loaded config revision.
"""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from typing import Any

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

from buttereye.core.api import (
    BenchRequest,
    ButterEyeError,
    ErrorCode,
    Msg,
    render,
)
from buttereye.core.testing import scenarios as sc
from buttereye.gui.pages.bench import (
    COL_CONFIG,
    COL_FAULTS,
    COL_MPV,
    COL_VSPIPE,
    VALUE_ROLE,
    BenchPage,
    recommended_measurement,
)
from tests.gui.helpers import fake_core


def _page(win: Any, qtbot: Any) -> BenchPage:
    win.go("bench")
    page = win.page("bench")
    assert isinstance(page, BenchPage)
    qtbot.waitUntil(lambda: page.panel.state != "loading", timeout=3000)
    return page


def _scenario(base: str = "all_ready", **changes: Any) -> sc.Scenario:
    return dataclasses.replace(sc.get(base), **changes)


def _calls(win: Any, name: str) -> list[Any]:
    return [c for c in fake_core(win.bridge).fake_calls if c.name == name]


# ---------------------------------------------------------------- unavailable / empty
def test_unavailable_on_devbox_shows_reason_code_and_no_rows(make_window: Any, qtbot: Any) -> None:
    win = make_window("devbox_now")
    page = _page(win, qtbot)
    assert page.panel.state == "unavailable"
    head, _body, code = page.panel.unavailable_texts()
    assert head == "The benchmark isn't in this build yet."
    assert code == ErrorCode.NOT_IMPLEMENTED.code
    assert page.model.rowCount() == 0
    assert page.plot.rows() == ()
    assert page.panel.unavailable_hint.text() == "buttereye bench"
    assert not _calls(win, "bench_history")  # nothing fetched for an unavailable feature


def test_empty_history_shows_empty_text(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario(bench_history=()))
    page = _page(win, qtbot)
    assert page.panel.state == "content"
    assert page.results_panel.state == "empty"
    assert page.results_panel.empty_label.text().startswith("No measurements yet.")
    assert "none of your videos are used" in page.results_panel.empty_label.text()
    assert not page.apply_button.isEnabled()
    assert page.model.rowCount() == 0


def test_blocked_by_doctor_gates_with_open_system(make_window: Any, qtbot: Any) -> None:
    win = make_window("blocking_mpv")
    page = _page(win, qtbot)
    caps = win.bridge.capabilities
    st = caps.states[page.features[0]]
    if st.available:
        pytest.skip("scenario does not block the benchmark")
    assert page.panel.state == "unavailable"
    assert page.panel.unavailable_texts()[0] == "Setup isn't finished."


# ---------------------------------------------------------------- results
def test_table_and_plot_show_the_same_values(make_window: Any, qtbot: Any) -> None:
    win = make_window("bench_results")
    page = _page(win, qtbot)
    result = page.shown_result()
    assert result is not None
    assert page.results_panel.state == "content"
    rows = page.plot.rows()
    assert rows == result.measurements
    assert page.model.rowCount() == len(rows)
    for r, m in enumerate(rows):
        assert page.model.item(r, COL_CONFIG).data(VALUE_ROLE) == m.label
        assert page.model.item(r, COL_VSPIPE).data(VALUE_ROLE) == m.vspipe_fps
        assert page.model.item(r, COL_MPV).data(VALUE_ROLE) == m.mpv_fps
        assert page.model.item(r, COL_VSPIPE).text() == f"{m.vspipe_fps:.1f}"
        assert page.model.item(r, COL_MPV).text() == f"{m.mpv_fps:.1f}"
    req = result.request
    assert page.plot.target_fps() == pytest.approx(float(req.source_fps * 2))
    assert "1920×1080" in page.result_title.text()


def test_faults_are_flagged_in_words_and_codes(make_window: Any, qtbot: Any) -> None:
    win = make_window("bench_results")
    page = _page(win, qtbot)
    texts = {
        page.model.item(r, COL_CONFIG).data(VALUE_ROLE): page.model.item(r, COL_FAULTS).text()
        for r in range(page.model.rowCount())
    }
    assert texts["rife-v4.26"] == "0"
    assert texts["rife-v4.18"] == "1 — unstable"
    assert texts["mvtools"] == "unknown (log not readable)"
    configs = [page.model.item(r, COL_CONFIG).text() for r in range(page.model.rowCount())]
    assert "rife-v4.18 — GPU fault" in configs
    assert "rife-v4.26 — recommended" in configs
    codes = {b.code for b in page.fault_banners}
    assert codes == {ErrorCode.RIFE_GPU_FAULT.code, ErrorCode.JOURNAL_UNREADABLE.code}
    fault = next(b for b in page.fault_banners if b.code == ErrorCode.RIFE_GPU_FAULT.code)
    assert "rife-v4.18" in fault.title()
    assert fault.kind == "blocking"  # glyph + word, not colour
    assert "rife-v4.26" in page.recommend_label.text()
    assert page.apply_button.isEnabled()
    assert "v4.26" in page.plot.accessibleDescription()


def test_faulting_config_is_never_recommended(make_window: Any, qtbot: Any) -> None:
    bad = dataclasses.replace(sc.bench_result_4090(), recommended="rife-v4.18")
    win = make_window(_scenario(bench_history=(bad,)))
    page = _page(win, qtbot)
    assert recommended_measurement(bad) is None
    assert not page.apply_button.isEnabled()
    assert page.recommend_label.text().startswith("Nothing is recommended")
    configs = [page.model.item(r, COL_CONFIG).text() for r in range(page.model.rowCount())]
    assert not any("recommended" in c for c in configs)
    assert "rife-v4.18 — GPU fault" in configs
    page.apply_recommended()  # no-op, never reaches the core
    qtbot.wait(50)
    assert not _calls(win, "apply_bench")


def test_history_combo_switches_results(make_window: Any, qtbot: Any) -> None:
    older = dataclasses.replace(
        sc.bench_result_4090(BenchRequest(1280, 720, Fraction(30))),
        when=sc.bench_result_4090().when.replace(year=2025),
    )
    win = make_window(_scenario(bench_history=(older, sc.bench_result_4090())))
    page = _page(win, qtbot)
    assert page.history_combo.count() == 2
    assert page.history_combo.currentIndex() == 0  # newest first
    first = page.shown_result()
    assert first is not None and first.request.width == 1920
    page.history_combo.setCurrentIndex(1)
    shown = page.shown_result()
    assert shown is not None and shown.request.width == 1280
    assert page.plot.target_fps() == pytest.approx(60.0)


# ---------------------------------------------------------------- running
def test_run_shows_rows_per_configuration_and_the_result(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario(bench_history=()))
    page = _page(win, qtbot)
    announced: list[str] = []
    from buttereye.gui.a11y import add_announcement_listener

    remove = add_announcement_listener(lambda _w, text, _a: announced.append(text))
    try:
        page.run_button.click()
        assert page.is_running()
        assert not page.run_button.isEnabled() and page.cancel_button.isEnabled()
        qtbot.waitUntil(lambda: not page.is_running(), timeout=5000)
    finally:
        remove()
    (call,) = _calls(win, "bench")
    req = call.args[0]
    assert req == BenchRequest(1920, 1080, Fraction(24000, 1001), None, False, None)
    labels = {m.label for m in sc.bench_result_4090().measurements}
    assert set(page.op_rows()) == labels
    assert all(r.is_finished() for r in page.op_rows().values())
    assert page.results_panel.state == "content"
    assert page.model.rowCount() == len(labels)
    assert page.history_combo.count() == 1
    assert any("Benchmark finished" in a for a in announced)
    assert page.run_button.isEnabled() and not page.cancel_button.isEnabled()
    # success folds the finished rows into one summary line and shows the results
    assert not any(r.isVisibleTo(page) for r in page.op_rows().values())
    assert page.overall is not None and page.overall.isVisibleTo(page)
    assert page.overall.result_badge.text().startswith(f"Measured {len(labels)} configurations in ")
    qtbot.waitUntil(lambda: QApplication.focusWidget() is page.table, timeout=2000)


def test_config_rows_do_not_repeat_their_name() -> None:
    phase = Msg("{config} — run 2 of 3 — in mpv", {"config": "rife-v4.26"})
    assert render(BenchPage.phase_without_config(phase, "rife-v4.26")) == "run 2 of 3 — in mpv"
    literal = Msg("rife-v4.26 — warm-up — vspipe")
    assert render(BenchPage.phase_without_config(literal, "rife-v4.26")) == "warm-up — vspipe"
    other = Msg("Benchmark finished")
    assert BenchPage.phase_without_config(other, "rife-v4.26") is other


def test_cancel_stops_the_run_and_keeps_old_results(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario("bench_results", op_step_s=0.2))
    page = _page(win, qtbot)
    before = page.shown_result()
    page.run()
    qtbot.waitUntil(lambda: bool(page.op_rows()), timeout=3000)
    page.cancel_button.click()
    assert not page.is_running()
    assert page.run_button.isEnabled()
    assert all(r.is_finished() for r in page.op_rows().values())
    assert page.overall is not None and page.overall.result_badge.kind() == "off"
    # cancel keeps the rows: they show where the run stopped
    assert all(r.isVisibleTo(page) for r in page.op_rows().values())
    assert page.cancel_button.text() == "Ca&ncel"
    qtbot.wait(300)
    assert page.shown_result() == before
    assert page.history_combo.count() == 1


def test_run_failure_shows_the_error_with_code(make_window: Any, qtbot: Any) -> None:
    err = ButterEyeError(ErrorCode.BENCH_FAILED, Msg("No configuration could be measured."))
    win = make_window(_scenario(bench_history=(), errors={"bench": err}))
    page = _page(win, qtbot)
    page.run()
    qtbot.waitUntil(lambda: not page.is_running(), timeout=5000)
    assert page.run_error is not None
    assert page.run_error.code == ErrorCode.BENCH_FAILED.code
    assert page.run_error.title() == "No configuration could be measured."
    assert page.panel.state == "content"  # the form stays usable


def test_invalid_form_never_calls_the_core(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario(bench_history=()))
    page = _page(win, qtbot)
    page.target_edit.setText("20")  # below the 23.976 source
    page.run()
    assert page.form_error.isVisibleTo(page)
    assert "higher than the source" in page.form_error.text()
    page.rate_edit.setText("")
    page.target_edit.setText("")
    page.run()
    assert "source frame rate" in page.form_error.text()
    qtbot.wait(50)
    assert not _calls(win, "bench")


def test_custom_size_full_matrix_and_hint(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario(bench_history=()))
    page = _page(win, qtbot)
    page.width_spin.setValue(1281)  # odd sizes are rounded down to even
    page.height_spin.setValue(720)
    page.rate_edit.setText("30")
    page.target_edit.setText("60")
    page.full_check.setChecked(True)
    hint = page.cli_hint()
    assert hint is not None and hint.text() == "buttereye bench --full"
    req = page.request()
    assert req == BenchRequest(1280, 720, Fraction(30), Fraction(60), True, None)


def test_current_session_source(make_window: Any, qtbot: Any) -> None:
    facts = sc.facts()
    win = make_window(_scenario(bench_history=()))
    win._facts = facts  # what SessionsPage.facts_changed sets
    page = _page(win, qtbot)
    page.refresh()
    assert page.session_radio.isEnabled()
    assert f"{facts.width}×{facts.height}" in page.session_radio.text()
    page.session_radio.setChecked(True)
    assert not page.width_spin.isEnabled()
    req = page.request()
    assert req is not None
    assert (req.width, req.height, req.source_fps) == (facts.width, facts.height, facts.fps)


def test_own_file_choice_goes_into_the_request(make_window: Any, qtbot: Any, tmp_path: Any) -> None:
    win = make_window(_scenario(bench_history=()))
    page = _page(win, qtbot)
    clip = tmp_path / "clip.mkv"
    page.choose_file = lambda: clip
    page.file_check.setChecked(True)
    assert page.file_label.isVisibleTo(page) and str(clip) in page.file_label.text()
    req = page.request()
    assert req is not None and req.user_file == clip
    hint = page.cli_hint()
    assert hint is not None and str(clip) in hint.text()
    page.choose_file = lambda: None
    page.file_check.setChecked(False)
    page.file_check.setChecked(True)  # chooser cancelled -> unchecked again
    assert not page.file_check.isChecked()


# ---------------------------------------------------------------- apply
def test_use_recommended_applies_with_the_loaded_revision(make_window: Any, qtbot: Any) -> None:
    win = make_window("bench_results")
    page = _page(win, qtbot)
    page.apply_button.click()
    qtbot.waitUntil(lambda: page.apply_status.isVisibleTo(page), timeout=3000)
    (call,) = _calls(win, "apply_bench")
    assert call.args[1] == "rife-v4.26"
    assert call.kwargs["expected_revision"] == sc.get("bench_results").config_load.revision
    assert page.apply_status.text().startswith("Saved: rife-v4.26")
    assert page.apply_hint.text() == "buttereye bench --apply rife-v4.26"


def test_apply_failure_is_shown(make_window: Any, qtbot: Any) -> None:
    err = ButterEyeError(ErrorCode.BENCH_FAILED, Msg("That configuration can't be used."))
    win = make_window(_scenario("bench_results", errors={"apply_bench": err}))
    page = _page(win, qtbot)
    page.apply_recommended()
    qtbot.waitUntil(lambda: page.apply_status.isVisibleTo(page), timeout=3000)
    assert "BE-5001" in page.apply_status.text()
    assert page.apply_button.isEnabled()


def test_table_is_keyboard_friendly(make_window: Any, qtbot: Any) -> None:
    win = make_window("bench_results")
    page = _page(win, qtbot)
    assert not page.table.tabKeyNavigation()
    assert page.table.accessibleName() == "Benchmark results"
    assert page.plot.focusPolicy() == Qt.FocusPolicy.NoFocus


# ---------------------------------------------------------------- real core (dev box)
RPM_MODELS = "/usr/share/buttereye/rife-ncnn-models"
REAL_MODEL = "rife-v4.22_lite_ensembleFalse"


@pytest.mark.integration
@pytest.mark.gpu
@pytest.mark.devbox
def test_real_bench_through_the_gui(qtbot: Any, xdg_env: Any, tmp_path: Any) -> None:
    """The shipped bridge + real providers: one RIFE model and MVTools at 640×360.

    Kept short (two configurations, tiny size). Values must come from the real
    run, appear identically in table and plot, and land in the temporary bench.json.
    """
    import os
    from pathlib import Path

    from PySide6.QtCore import QSettings

    from buttereye.core.api import DetachPolicy
    from buttereye.core.paths import resolve
    from buttereye.gui.bridge import CoreBridge
    from buttereye.gui.main_window import MainWindow

    if not (Path(RPM_MODELS) / REAL_MODEL).is_dir() or not Path("/dev/nvidia0").exists():
        pytest.skip("needs the dev box GPU and the packaged RIFE models")
    share = tmp_path / "rpm-share"
    (share / "rife-ncnn-models").mkdir(parents=True)
    (share / "rife-ncnn-models" / REAL_MODEL).symlink_to(Path(RPM_MODELS) / REAL_MODEL)
    paths = dataclasses.replace(resolve(os.environ), rpm_data_dir=share)

    bridge = CoreBridge(paths)
    settings = QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat)
    win = MainWindow(bridge, settings, log_path=tmp_path / "gui.log", open_setup_when_missing=False)
    qtbot.addWidget(win, before_close_func=lambda w: setattr(w, "_may_close", True))
    try:
        with qtbot.waitSignal(bridge.ready, timeout=10_000):
            bridge.start()
        win.show()
        page = _page(win, qtbot)
        assert page.panel.state == "content", page.panel.unavailable_texts()
        assert page.results_panel.state == "empty"
        page.width_spin.setValue(640)
        page.height_spin.setValue(360)
        page.rate_edit.setText("24")
        page.run()
        qtbot.waitUntil(lambda: bool(page.op_rows()), timeout=60_000)
        qtbot.waitUntil(lambda: not page.is_running(), timeout=240_000)
        assert page.run_error is None, page.run_error and page.run_error.title()
        result = page.shown_result()
        assert result is not None
        labels = [m.label for m in result.measurements]
        assert labels == ["rife-v4.22-lite", "mvtools"]
        assert set(page.op_rows()) == set(labels)
        assert page.plot.rows() == result.measurements
        for r, m in enumerate(result.measurements):
            assert m.vspipe_fps > 0, m
            assert page.model.item(r, COL_MPV).data(VALUE_ROLE) == m.mpv_fps
            assert page.model.item(r, COL_FAULTS).text()  # "0", "n — unstable" or "unknown …"
        rec = recommended_measurement(result)
        assert rec is None or not rec.gpu_faults
        assert paths.bench_file.is_file()
    finally:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)


def test_target_rate_label_fits_and_explains_empty(make_window: Any, qtbot: Any) -> None:
    win = make_window(_scenario())
    win.resize(1180, 760)
    page = _page(win, qtbot)
    qtbot.wait(50)
    assert page.target_edit.placeholderText() == "2× source rate"
    assert page.target_help.isVisibleTo(page)
    assert page.target_help.text() == page.target_edit.accessibleDescription()
    label = next(lab for lab in page.findChildren(QLabel) if lab.buddy() is page.target_edit)
    assert not label.wordWrap()
    assert label.width() >= label.sizeHint().width()  # "Target rate (fps)" not clipped


def test_run_warns_that_the_gpu_is_pushed_hard(make_window: Any, qtbot: Any) -> None:
    """A RIFE model can briefly stall the GPU (Xid 109, M0(f)); the page says so
    next to Run, visibly and in Run's accessible description."""
    win = make_window(_scenario())
    page = _page(win, qtbot)
    assert page.gpu_warning.isVisibleTo(page)
    assert "Save your work first." in page.gpu_warning.text()
    assert page.run_button.accessibleDescription() == page.gpu_warning.text()
