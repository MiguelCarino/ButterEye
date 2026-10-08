# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""System page (docs/design/GUI.md §4.7, §6 ``test_system_page``; U6).

Grouping, TensorRT hidden before the opt-in, an unreadable kernel log never
shown as OK, the bug-report unavailable state, and the actions.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from PySide6.QtGui import QGuiApplication

from buttereye.core.api import (
    ErrorCode,
    Feature,
    Reason,
    Section,
    Severity,
)
from buttereye.core.capabilities import unavailable_state
from buttereye.core.testing import scenarios as sc
from buttereye.gui.widgets.findings_view import severity_word, shown_severity, summary_text
from tests.gui.helpers import click_modal, fake_core


def _page(make_window: Any, qtbot: Any, scenario: Any = "devbox_now") -> tuple[Any, Any]:
    win = make_window(scenario)
    win.go("system")
    page = win.page("system")
    qtbot.waitUntil(lambda: page.report is not None and not page.is_running(), timeout=5000)
    return win, page


def test_grouping_and_summary(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    view = page.findings
    titles = view.group_titles()
    # devbox_now: degraded (Xid, ffms2, mkvmerge) and ok; no blocking group; the only
    # info finding ("TensorRT: off") is replaced by the opt-in row before the opt-in.
    assert [t.split(" (")[0] for t in titles] == [
        severity_word(Severity.DEGRADED),
        severity_word(Severity.OK),
    ]
    assert view.is_group_expanded(Severity.DEGRADED)
    assert not view.is_group_expanded(Severity.OK)  # OK collapsed
    degraded = {f.id for f in view.rows_in_group(Severity.DEGRADED)}
    assert {"gpu_fault.xid", "render.ffms2", "render.mkvmerge"} <= degraded
    assert page.summary_badge.kind() == "degraded"
    assert page.summary_badge.text() == view.summary()
    assert "3 warnings" in view.summary()
    assert page.panel.state == "content"
    # first problem selected; its detail pane carries code, fix and commands
    det = view.detail
    assert det.finding is not None and det.finding.severity is Severity.DEGRADED
    assert det.code_label.text().startswith("BE-")


def test_detail_shows_commands_and_evidence(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    assert page.findings.select_finding("gpu_fault.xid")
    det = page.findings.detail
    assert det.code_label.text() == ErrorCode.RIFE_GPU_FAULT.code
    assert [f.text() for f in det.command_fields] == ["journalctl -k -g Xid"]
    assert "Xid 13" in det.evidence.toPlainText()
    assert det.evidence.isReadOnly()
    page.findings.select_finding("render.ffms2")
    assert [f.text() for f in page.findings.detail.command_fields] == ["sudo dnf install ffms2"]


def test_blocking_group_first(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot, "blocking_mpv")
    assert page.findings.group_titles()[0].startswith(severity_word(Severity.BLOCKING))
    assert page.summary_badge.kind() == "blocking"
    assert "1 problem" in page.findings.summary()


def test_trt_hidden_before_optin(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    assert not page.report.trt_included
    assert all(f.section is not Section.TRT for f in page.findings.findings())
    assert page.trt_row.isVisibleTo(page)
    assert page.trt_badge.text() == "Experimental NVIDIA TensorRT: off"

    seen = click_modal(qtbot, "trt.accept")
    page.trt_button.click()
    assert seen == ["trt_optin"]
    qtbot.waitUntil(lambda: core.fake_call_count("set_trt_experimental") == 1, timeout=3000)
    qtbot.waitUntil(lambda: page.report.trt_included and not page.is_running(), timeout=5000)
    doctor_calls = [c for c in core.fake_calls if c.name == "doctor"]
    assert doctor_calls[-1].kwargs == {"trt": True}
    assert not page.trt_row.isVisibleTo(page)
    assert any(f.section is Section.TRT for f in page.findings.findings())


def test_trt_optin_cancel_writes_nothing(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    click_modal(qtbot, "trt.cancel")
    page.trt_button.click()
    qtbot.wait(100)
    assert core.fake_call_count("set_trt_experimental") == 0
    assert core.fake_call_count("save_config") == 0
    assert page.trt_row.isVisibleTo(page)


def test_unreadable_journal_is_never_ok(make_window: Any, qtbot: Any) -> None:
    unreadable = sc.finding(
        "gpu_fault.journal",
        Section.GPU_FAULT,
        Severity.INFO,
        "Couldn't read the kernel log; GPU faults can't be checked",
        code=ErrorCode.JOURNAL_UNREADABLE,
    )
    # A provider bug reporting it as OK must still not land in the OK group.
    mislabelled = dataclasses.replace(unreadable, id="gpu_fault.journal2", severity=Severity.OK)
    rep = sc.report(sc.findings_ok() + (unreadable, mislabelled))
    scen = dataclasses.replace(sc.get("devbox_now"), report=rep, doctor_result=rep)
    _win, page = _page(make_window, qtbot, scen)
    ok_ids = {f.id for f in page.findings.rows_in_group(Severity.OK)}
    info_ids = {f.id for f in page.findings.rows_in_group(Severity.INFO)}
    assert "gpu_fault.journal" not in ok_ids and "gpu_fault.journal2" not in ok_ids
    assert {"gpu_fault.journal", "gpu_fault.journal2"} <= info_ids
    assert shown_severity(mislabelled) is Severity.INFO


def test_report_unavailable_state(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    assert not page.report_button.isEnabled()
    assert page.report_note.isVisibleTo(page)
    assert page.report_note.text() == "Bug reports can't be created in this build yet."
    assert "Copy all as text" in page.report_note_body.text()
    assert page.report_note_code.text() == ErrorCode.NOT_IMPLEMENTED.code
    assert "Bug reports" in page.report_button.accessibleDescription()
    page.create_report()  # no dialog, no call
    assert core.fake_call_count("report_bundle") == 0


def test_report_available_creates_bundle(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win, page = _page(make_window, qtbot, "all_ready")
    core = fake_core(win.bridge)
    assert page.report_button.isEnabled()
    assert not page.report_note.isVisibleTo(page)
    dest = tmp_path / "report.tar.gz"
    page.start_report(dest, redact=True)
    qtbot.waitUntil(lambda: page.report_result.isVisibleTo(page), timeout=3000)
    call = [c for c in core.fake_calls if c.name == "report_bundle"][-1]
    assert call.args == (dest,) and call.kwargs == {"redact": True}
    assert str(dest) in page.report_result.text()


def test_report_dialog_redacts_by_default(qtbot: Any) -> None:
    from buttereye.gui.pages.system import ReportDialog

    dlg = ReportDialog()
    qtbot.addWidget(dlg)
    assert not dlg.include_names.isChecked()
    assert "never included" in dlg.body.text()


def test_run_checks_again_and_progress(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    before = core.fake_call_count("doctor")
    win.refresh_current()  # F5 on System runs the checks
    assert page.is_running()
    assert page.op_row is not None
    qtbot.waitUntil(lambda: not page.is_running(), timeout=5000)
    assert core.fake_call_count("doctor") == before + 1
    assert page.op_row.is_finished()
    assert "Render tools" in page._phases  # section names as they finish


def test_first_run_without_report_shows_loading_phases(make_window: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("devbox_now"), report=None, op_step_s=0.03)
    win = make_window(scen)
    win.go("system")
    page = win.page("system")
    qtbot.waitUntil(lambda: page.panel.state == "loading", timeout=2000)
    qtbot.waitUntil(lambda: "mpv" in page.panel.loading_label.text(), timeout=3000)
    qtbot.waitUntil(lambda: page.report is not None, timeout=5000)
    assert page.panel.state == "content"


def test_cancel_checks(make_window: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("devbox_now"), report=None, op_step_s=0.2)
    win = make_window(scen)
    win.go("system")
    page = win.page("system")
    qtbot.waitUntil(page.is_running, timeout=2000)
    page.cancel()
    assert not page.is_running()
    assert page.panel.state == "empty"
    qtbot.wait(300)
    assert page.report is None


def test_hardware_and_plugins(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    assert "NVIDIA GeForce RTX 4090" in page.gpu_label.text()
    assert "615.71.09" in page.gpu_label.text()
    assert "software, not used" in page.vk_label.text()
    assert "used for interpolation" in page.vk_label.text()
    assert not page.hw_warning.isVisibleTo(page)
    qtbot.waitUntil(lambda: page.plugins_model.rowCount() == 3, timeout=3000)
    m = page.plugins_model
    assert m.item(0, 0).text() == "RIFE-ncnn-Vulkan"
    assert "bundled ncnn 0^20250503git305837fd" in m.item(0, 2).text()
    assert m.item(0, 4).text() == "Yes"
    assert m.item(2, 3).text() == "Not installed"


def test_decode_interpolation_warning() -> None:
    from buttereye.core.api import GpuInfo, HardwareInfo, VulkanDevice
    from buttereye.gui.pages.system import hardware_lines

    hw = HardwareInfo(
        gpus=(
            GpuInfo("0000:00:02.0", "Intel", "Intel UHD 770", None, None, None),
            GpuInfo("0000:01:00.0", "NVIDIA", "RTX 4060 Laptop", "615", 8 * 2**30, "8.9"),
        ),
        vulkan=(VulkanDevice(0, "u-nv", "RTX 4060 Laptop", "NVIDIA", "discrete", 8 * 2**30),),
        interpolation_device="u-nv",
        decode_device="0000:00:02.0",
        cpu_threads=16,
    )
    _gpus, _vk, warning = hardware_lines(hw)
    assert warning is not None and "Intel UHD 770" in warning and "RTX 4060" in warning


def test_copy_all_as_text(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    page.copy_button.click()
    text = QGuiApplication.clipboard().text()
    assert text.startswith(summary_text(page.findings.findings()))
    assert ErrorCode.FFMS2_MISSING.code in text
    assert "$ sudo dnf install ffms2" in text
    assert "NVIDIA GeForce RTX 4090" in text


def test_doctor_unavailable_panel(make_window: Any, qtbot: Any) -> None:
    caps = dict(sc.caps_devbox_now())
    caps[Feature.DOCTOR] = unavailable_state(
        Feature.DOCTOR, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED
    )
    scen = dataclasses.replace(sc.get("devbox_now"), capabilities=caps)
    win = make_window(scen)
    win.go("system")
    page = win.page("system")
    qtbot.wait(150)
    assert page.panel.state == "unavailable"
    assert page.panel.unavailable_texts()[2] == ErrorCode.NOT_IMPLEMENTED.code
    assert fake_core(win.bridge).fake_call_count("doctor") == 0


def test_doctor_error_shows_retry(make_window: Any, qtbot: Any) -> None:
    from buttereye.core.api import ButterEyeError, Msg

    err = ButterEyeError(ErrorCode.PROBE_FAILED, Msg("The in-mpv probe failed."))
    scen = dataclasses.replace(sc.get("devbox_now"), report=None, errors={"doctor": err})
    win = make_window(scen)
    win.go("system")
    page = win.page("system")
    qtbot.waitUntil(lambda: page.panel.state == "error", timeout=3000)
    banner = page.panel.error_banner
    assert banner is not None and banner.code == ErrorCode.PROBE_FAILED.code
    assert [b.text() for b in banner.buttons] == ["&Retry"]
