# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Setup wizard (docs/design/GUI.md §4.2, §6 ``test_setup_wizard``; U6; SCOPE F0, §4.7).

Blocking disables Next with the visible reason; the TensorRT opt-in dialog;
downloads are never pre-checked; cancelling at each page records no
``setup_apply`` before page 6 and no completed apply after a page-6 cancel.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWizard

from buttereye.core.api import BackendId, ConfigChanged, Feature, Section, Severity
from buttereye.core.testing import scenarios as sc
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs import setup_wizard as sw
from buttereye.gui.dialogs.setup_wizard import (
    PAGE_APPLY,
    PAGE_CHECKS,
    PAGE_DOWNLOADS,
    PAGE_ENGINE,
    PAGE_FINISH,
    PAGE_PACKAGES,
    PAGE_WELCOME,
    SetupWizard,
)
from tests.gui.helpers import click_modal, fake_core
from tests.gui.test_a11y_sweep import check_names_and_buddies

WRITES = ("setup_apply", "save_config", "set_trt_experimental", "add_include_to_mpv_conf")


class Harness:
    def __init__(self, bridge: Any, wizard: SetupWizard, gone: list[str]) -> None:
        self.bridge = bridge
        self.wizard = wizard
        self.gone = gone

    @property
    def core(self) -> Any:
        return fake_core(self.bridge)

    def writes(self) -> dict[str, int]:
        return {n: self.core.fake_call_count(n) for n in WRITES}


@pytest.fixture
def make_wizard(make_bridge: Any, qtbot: Any, tmp_path: Path) -> Any:
    def factory(scenario: Any = "fresh_box") -> Harness:
        bridge = make_bridge(scenario)
        gone: list[str] = []
        ctx = GuiContext(
            bridge,
            QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat),
            announce=lambda _t, _a: None,
            status=lambda _t: None,
            go=gone.append,
            facts=lambda: None,
        )
        wiz = SetupWizard(ctx, None)
        qtbot.addWidget(wiz)
        wiz.show()
        qtbot.waitExposed(wiz)
        return Harness(bridge, wiz, gone)

    return factory


def _next_btn(w: SetupWizard) -> Any:
    return w.button(QWizard.WizardButton.NextButton)


def _to_checks_done(h: Harness, qtbot: Any) -> None:
    w = h.wizard
    assert w.currentId() == PAGE_WELCOME
    w.next()
    assert w.currentId() == PAGE_CHECKS
    qtbot.waitUntil(lambda: w.plan is not None and not w.checks.busy, timeout=5000)


def test_welcome_page_f18(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard()
    w = h.wizard
    assert w.wizardStyle() == QWizard.WizardStyle.ClassicStyle
    assert w.isModal()
    page = w.welcome
    assert "never edits your mpv.conf" in page.intro.text()
    assert "no network" in page.intro.text()
    assert "The ButterEye contributors" in page.copyright.text()
    assert "GNU Affero General Public License" in page.licence.text()
    assert page.view_licence.isVisibleTo(page)
    assert page.hint.text() == "buttereye setup"
    assert check_names_and_buddies(page, w) == []


def test_blocking_disables_next_with_text(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard("blocking_mpv")
    w = h.wizard
    _to_checks_done(h, qtbot)
    page = w.checks
    assert not _next_btn(w).isEnabled()
    assert page.blocking_label.isVisibleTo(page)
    assert page.blocking_label.text() == "Fix the blocking items, then press Check again."
    assert page.findings.group_titles()[0].startswith("Blocking")
    assert page.summary.kind() == "blocking"
    assert check_names_and_buddies(page, w) == []
    # Check again re-runs doctor
    before = h.core.fake_call_count("doctor")
    page.again.click()
    qtbot.waitUntil(lambda: not page.busy, timeout=5000)
    assert h.core.fake_call_count("doctor") == before + 1
    assert not _next_btn(w).isEnabled()
    assert h.writes() == dict.fromkeys(WRITES, 0)


def test_happy_path_writes_only_on_apply(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard()
    w = h.wizard
    assert h.core.fake_call_count("doctor") == 0  # nothing runs on Welcome
    _to_checks_done(h, qtbot)
    assert _next_btn(w).isEnabled()
    assert w.checks.findings.group_titles()[0].startswith("Degraded")
    w.next()
    assert w.currentId() == PAGE_ENGINE
    e = w.engine
    assert e.selected() is BackendId.RIFE_NCNN  # proposed, pre-selected
    assert "RIFE (Vulkan) on NVIDIA GeForce RTX 4090" in e.recommended.text()
    assert BackendId.RIFE_TRT not in e.radios  # TRT listed only after opt-in
    assert not e.use_trt.isVisibleTo(e)
    assert check_names_and_buddies(e, w) == []
    assert h.writes() == dict.fromkeys(WRITES, 0)
    w.next()
    assert w.currentId() == PAGE_APPLY  # no packages missing, no downloads
    qtbot.waitUntil(lambda: w.setup_result is not None, timeout=5000)
    assert h.core.fake_call_count("setup_apply") == 1
    call = [c for c in h.core.fake_calls if c.name == "setup_apply"][0]
    choices = call.args[1]
    assert choices.backend is BackendId.RIFE_NCNN
    assert choices.trt_experimental is False
    assert choices.confirmed_downloads == frozenset()
    # progress is coalesced (latest wins), so early phases may be skipped; the last one shows
    assert w.apply_page.rows[-1].label.text() == "Writing settings"
    assert all(r.is_finished() for r in w.apply_page.rows)
    assert _next_btn(w).isEnabled()
    w.next()
    assert w.currentId() == PAGE_FINISH
    f = w.finish_page
    assert f.result_badge.text() == "Test run passed: 74 fps"
    assert not f.bench.isChecked() and not f.include.isChecked()
    assert "include=" in f.include_note.text()
    # the include line only makes the [buttereye] profile available: no smoothing claim
    assert "use ButterEye's settings" not in f.include.text()
    assert "does not smooth" in f.include_note.text() and "attaching" in f.include_note.text()
    assert h.core._load.exists  # written by apply, last
    assert check_names_and_buddies(f, w) == []
    w.accept()
    assert h.gone == []
    assert h.core.fake_call_count("add_include_to_mpv_conf") == 0


def test_finish_offers(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), capabilities=sc.caps_all_ready()))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    qtbot.waitUntil(lambda: w.setup_result is not None, timeout=5000)
    w.next()
    f = w.finish_page
    assert f.bench.isEnabled()
    f.bench.setChecked(True)
    f.include.setChecked(True)
    w.accept()
    assert h.gone == ["bench"]
    dlg = w.include_dialog
    assert dlg is not None
    assert dlg.isVisible()
    # copy-only fallback or the Play unit's dialog; neither writes without confirmation
    assert h.core.fake_call_count("add_include_to_mpv_conf") == 0
    if isinstance(dlg, sw.IncludeLineFallback):
        assert dlg.field.text().startswith("include=")
    dlg.close()


def test_bench_offer_disabled_when_unavailable(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard()  # devbox-shaped caps: BENCH not in this build
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    qtbot.waitUntil(lambda: w.setup_result is not None, timeout=5000)
    w.next()
    assert not w.finish_page.bench.isEnabled()
    assert "benchmark" in w.finish_page.bench.accessibleDescription().lower()


def _trt_scenario() -> Any:
    return dataclasses.replace(sc.get("all_ready"), config_load=sc.config_load(exists=False))


def test_trt_optin_dialog_and_downloads_unchecked(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(_trt_scenario())
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    e = w.engine
    assert not e.show_experimental.isChecked()
    e.show_experimental.setChecked(True)
    assert e.use_trt.isVisibleTo(e) and not e.use_trt.isChecked()
    seen = click_modal(qtbot, "trt.accept")
    e.use_trt.click()
    assert seen == ["trt_optin"]
    assert w.last_optin is not None
    assert "pre-release" in w.last_optin.body.text()
    assert w.last_optin.cancel_button.isDefault()
    qtbot.waitUntil(lambda: not e.busy, timeout=5000)
    assert w.trt is True
    assert [c for c in h.core.fake_calls if c.name == "doctor"][-1].kwargs == {"trt": True}
    plan_call = [c for c in h.core.fake_calls if c.name == "setup_plan"][-1]
    assert plan_call.kwargs == {"trt_experimental": True}
    assert BackendId.RIFE_TRT in e.radios  # listed after the opt-in
    assert h.writes() == dict.fromkeys(WRITES, 0)  # the opt-in is only in memory
    w.next()
    assert w.currentId() == PAGE_DOWNLOADS
    d = w.downloads
    assert d.table.rowCount() == 1
    assert d.table.checked_names() == frozenset()  # nothing pre-checked
    assert d.confirmed() == frozenset()
    assert check_names_and_buddies(d, w) == []
    w.next()
    assert w.currentId() == PAGE_APPLY
    qtbot.waitUntil(lambda: w.setup_result is not None, timeout=5000)
    choices = [c for c in h.core.fake_calls if c.name == "setup_apply"][0].args[1]
    assert choices.trt_experimental is True
    assert choices.confirmed_downloads == frozenset()


def test_trt_optin_cancel(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(_trt_scenario())
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    e = w.engine
    e.show_experimental.setChecked(True)
    doctors = h.core.fake_call_count("doctor")
    click_modal(qtbot, "trt.cancel")
    e.use_trt.click()
    qtbot.wait(50)
    assert not e.use_trt.isChecked()
    assert w.trt is False
    assert h.core.fake_call_count("doctor") == doctors
    w.next()
    assert w.currentId() == PAGE_APPLY  # no downloads page without the opt-in
    w.reject()


def test_downloads_page_when_downloads_not_listed(make_wizard: Any, qtbot: Any) -> None:
    caps = dict(sc.caps_all_ready())
    from buttereye.core.api import Reason
    from buttereye.core.capabilities import unavailable_state

    caps[Feature.MODEL_DOWNLOADS] = unavailable_state(
        Feature.MODEL_DOWNLOADS, Reason.NOTHING_LISTED, None
    )
    h = make_wizard(dataclasses.replace(_trt_scenario(), capabilities=caps))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.engine.show_experimental.setChecked(True)
    click_modal(qtbot, "trt.accept")
    w.engine.use_trt.click()
    qtbot.waitUntil(lambda: not w.engine.busy, timeout=5000)
    w.next()
    assert w.currentId() == PAGE_DOWNLOADS
    d = w.downloads
    assert not d.table.isVisibleTo(d)
    assert d.none_label.isVisibleTo(d)
    assert _next_btn(w).isEnabled()
    assert d.confirmed() == frozenset()


def _with_packages() -> Any:
    return dataclasses.replace(
        sc.get("fresh_box"),
        missing_packages=("ffms2", "mkvtoolnix"),
        dnf_lines=("sudo dnf install ffms2", "sudo dnf install mkvtoolnix"),
    )


def test_packages_page(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(_with_packages())
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    assert w.currentId() == PAGE_PACKAGES
    p = w.packages
    assert [f.text() for f in p.fields] == ["sudo dnf install ffms2", "sudo dnf install mkvtoolnix"]
    assert p.fields[0].accessibleName() == "Copy dnf command 1 of 2"
    assert "ButterEye never runs dnf" in p.body.text()
    assert check_names_and_buddies(p, w) == []
    before = h.core.fake_call_count("doctor")
    p.again.click()
    qtbot.waitUntil(lambda: not p.busy, timeout=5000)
    assert h.core.fake_call_count("doctor") == before + 1
    assert _next_btn(w).isEnabled()
    assert h.writes() == dict.fromkeys(WRITES, 0)


@pytest.mark.parametrize(
    "stop_at", [PAGE_WELCOME, PAGE_CHECKS, PAGE_ENGINE, PAGE_PACKAGES, PAGE_DOWNLOADS]
)
def test_cancel_before_apply_writes_nothing(make_wizard: Any, qtbot: Any, stop_at: int) -> None:
    scen = dataclasses.replace(_with_packages(), capabilities=sc.caps_all_ready())
    scen = dataclasses.replace(scen, trt_downloads=sc.get("all_ready").trt_downloads)
    h = make_wizard(scen)
    w = h.wizard
    if stop_at != PAGE_WELCOME:
        _to_checks_done(h, qtbot)
    if stop_at in (PAGE_ENGINE, PAGE_PACKAGES, PAGE_DOWNLOADS):
        w.next()
    if stop_at == PAGE_DOWNLOADS:
        w.engine.show_experimental.setChecked(True)
        click_modal(qtbot, "trt.accept")
        w.engine.use_trt.click()
        qtbot.waitUntil(lambda: not w.engine.busy, timeout=5000)
    if stop_at in (PAGE_PACKAGES, PAGE_DOWNLOADS):
        w.next()
    if stop_at == PAGE_DOWNLOADS:
        w.next()
        w.downloads.table.set_checked("rife_v4.26.onnx", True)  # ticked, then cancelled
    assert w.currentId() == stop_at
    w.reject()
    qtbot.wait(100)
    assert h.writes() == dict.fromkeys(WRITES, 0)
    assert h.core._load.exists is False
    assert not w.isVisible()


def test_cancel_during_checks(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), op_step_s=0.1))
    w = h.wizard
    w.next()
    assert w.checks.busy
    w.checks.op_row.cancel_button.click()
    assert not w.checks.busy
    assert w.checks.op_row.is_finished()
    assert not _next_btn(w).isEnabled()
    qtbot.wait(300)
    assert w.plan is None
    w.reject()
    assert h.writes() == dict.fromkeys(WRITES, 0)


def test_cancel_on_apply_page_saves_nothing(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), op_step_s=0.05))
    w = h.wizard
    _to_checks_done(h, qtbot)
    events: list[object] = []
    h.bridge.event.connect(events.append)
    w.next()
    w.next()
    assert w.currentId() == PAGE_APPLY
    qtbot.waitUntil(lambda: h.core.fake_call_count("setup_apply") == 1, timeout=3000)
    qtbot.waitUntil(lambda: bool(w.apply_page.rows), timeout=3000)
    w.apply_page.rows[-1].cancel_button.click()
    assert w.apply_page.status.text() == "Cancelled. Nothing was saved."
    assert w.apply_page.retry.isVisibleTo(w.apply_page)
    assert not _next_btn(w).isEnabled()
    qtbot.wait(800)  # longer than the whole apply would take
    assert w.setup_result is None
    assert h.core._load.exists is False
    assert not any(isinstance(ev, ConfigChanged) for ev in events)
    w.reject()


def test_reject_during_apply_cancels(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), op_step_s=0.05))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    assert w.is_applying()
    w.reject()
    assert not w.is_applying()
    qtbot.wait(800)
    assert h.core._load.exists is False


def test_back_from_apply_cancels(make_wizard: Any, qtbot: Any) -> None:
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), op_step_s=0.05))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    assert w.is_applying()
    w.back()
    assert w.currentId() == PAGE_ENGINE
    assert not w.is_applying()
    qtbot.wait(800)
    assert h.core._load.exists is False
    w.reject()


def test_apply_failure_shows_finding_and_retry(make_wizard: Any, qtbot: Any) -> None:
    from buttereye.core.api import BlockingIssue, ErrorCode, Msg

    f = sc.finding(
        "probe.plugins",
        Section.PROBE,
        Severity.BLOCKING,
        "The plugin did not load inside mpv",
        code=ErrorCode.PROBE_FAILED,
    )
    err = BlockingIssue(ErrorCode.PROBE_FAILED, Msg("The smoke test failed."), findings=(f,))
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), errors={"setup_apply": err}))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    qtbot.waitUntil(lambda: not w.apply_page.busy, timeout=5000)
    a = w.apply_page
    assert a.status.kind() == "blocking"
    assert a.findings.isVisibleTo(a)
    assert [x.id for x in a.findings.findings()] == ["probe.plugins"]
    assert a.retry.isVisibleTo(a)
    assert not _next_btn(w).isEnabled()
    a.retry.click()
    qtbot.waitUntil(lambda: not a.busy, timeout=5000)
    assert h.core.fake_call_count("setup_apply") == 2
    w.reject()


def test_smoke_finding_on_finish(make_wizard: Any, qtbot: Any) -> None:
    f = sc.finding(
        "smoke.slow",
        Section.PROBE,
        Severity.DEGRADED,
        "Too slow for real time at 1080p",
    )
    h = make_wizard(dataclasses.replace(sc.get("fresh_box"), smoke_finding=f))
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    w.next()
    qtbot.waitUntil(lambda: w.setup_result is not None, timeout=5000)
    w.next()
    fp = w.finish_page
    assert fp.result_badge.kind() == "degraded"
    assert "Too slow for real time" in fp.result_badge.text()
    assert fp.findings.isVisibleTo(fp)


def test_cpu_only_engine_choices(make_wizard: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("cpu_only"), config_load=sc.config_load(exists=False))
    h = make_wizard(scen)
    w = h.wizard
    _to_checks_done(h, qtbot)
    w.next()
    e = w.engine
    assert not e.radios[BackendId.RIFE_NCNN].isEnabled()
    assert "llvmpipe only" in e.reasons[BackendId.RIFE_NCNN].text()
    assert "BE-1011" in e.reasons[BackendId.RIFE_NCNN].text()
    assert e.selected() is BackendId.MVTOOLS
    assert _next_btn(w).isEnabled()


def test_shell_opens_wizard_on_first_run(make_window: Any, qtbot: Any) -> None:
    win = make_window("fresh_box", open_setup_when_missing=True)
    qtbot.waitUntil(lambda: win._setup_dialog is not None, timeout=3000)
    dlg = win._setup_dialog
    assert isinstance(dlg, SetupWizard)
    assert QApplication.activeModalWidget() is dlg or dlg.isVisible()
    dlg.reject()
    assert fake_core(win.bridge).fake_call_count("setup_apply") == 0


@pytest.mark.devbox
@pytest.mark.integration
@pytest.mark.gpu
def test_devbox_real_core_reaches_finish(qtbot: Any, xdg_env: Any, tmp_path: Path) -> None:
    """Real U2/U3 providers in temporary XDG dirs: real doctor, real 10 s smoke test."""
    from buttereye.core.api import DetachPolicy
    from buttereye.gui.bridge import CoreBridge

    bridge = CoreBridge()
    with qtbot.waitSignal(bridge.ready, timeout=10_000):
        bridge.start()
    try:
        gone: list[str] = []
        ctx = GuiContext(
            bridge,
            QSettings(str(tmp_path / "w.ini"), QSettings.Format.IniFormat),
            announce=lambda _t, _a: None,
            status=lambda _t: None,
            go=gone.append,
            facts=lambda: None,
        )
        w = SetupWizard(ctx, None)
        qtbot.addWidget(w)
        w.show()
        config_file = xdg_env.config / "buttereye" / "config.toml"
        w.next()
        qtbot.waitUntil(lambda: w.plan is not None and not w.checks.busy, timeout=20_000)
        assert w.plan is not None
        assert not w.plan.report.blocking, w.checks.findings.as_text()
        assert not config_file.exists()  # nothing written before Apply
        w.next()
        assert w.engine.selected() is not None
        while w.currentId() != PAGE_APPLY:
            w.next()
        assert not config_file.exists()
        qtbot.waitUntil(lambda: not w.apply_page.busy, timeout=60_000)
        if w.setup_result is None:
            # a real finding instead of a pass is an acceptable outcome (§7 U6)
            print("DEVBOX setup finding:", w.apply_page.status.text())
            assert w.apply_page.status.kind() == "blocking"
            assert w.apply_page.banner_box.count() >= 1
            assert not config_file.exists()
            return
        assert config_file.exists()
        w.next()
        assert w.currentId() == PAGE_FINISH
        text = w.finish_page.result_badge.text()
        print("DEVBOX setup result:", text, w.setup_result)
        assert text.startswith("Test run")
        w.accept()
    finally:
        bridge.shutdown(DetachPolicy.KEEP_FILTER, cancel_jobs=True)
