# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""System page (docs/design/GUI.md §4.7; SCOPE F1, F9, F21).

Doctor results grouped Blocking / Degraded / OK / Info with a detail pane,
the hardware box, the plugins table and the actions [Run checks again] (F5),
[Create bug report…], [Run setup again] and [Copy all as text].

TensorRT findings are shown only when the report included them (after the
opt-in); before that one Info row "Experimental NVIDIA TensorRT: off" offers
[Turn on…]. While the checks run, the section names appear as they finish.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QGuiApplication, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEye,
    ButterEyeError,
    CapState,
    CommandHint,
    ConfigChanged,
    DoctorReport,
    ErrorCode,
    Event,
    Feature,
    Finding,
    HardwareInfo,
    OperationCancelled,
    PluginStatus,
    Progress,
    Reason,
    Section,
    Severity,
    command_hint,
    render,
    unavailable_text,
)
from buttereye.gui.a11y import heading, selectable
from buttereye.gui.bridge import Ticket
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.fit import FitDialog
from buttereye.gui.dialogs.trt_optin import TrtOptInDialog
from buttereye.gui.pages.base import GATING_REASONS, Page, PageAction
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.findings_view import FindingsView, findings_text, flat_view
from buttereye.gui.widgets.op_row import OpRow, human_bytes
from buttereye.gui.widgets.state_panel import StatePanel, split_headline
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge


def _t(text: str) -> str:
    return QCoreApplication.translate("SystemPage", text)


def visible_findings(report: DoctorReport) -> tuple[Finding, ...]:
    """Findings shown in the tree: TRT items only when the report included TRT."""
    if report.trt_included:
        return report.findings
    return tuple(f for f in report.findings if f.section is not Section.TRT)


def hardware_lines(hw: HardwareInfo) -> tuple[list[str], list[str], str | None]:
    """(GPU lines, Vulkan device lines, decode-vs-interpolation warning)."""
    gpus: list[str] = []
    for g in hw.gpus:
        parts = [g.name]
        if g.driver:
            parts.append(_t("driver {v}").format(v=g.driver))
        if g.vram_bytes:
            parts.append(_t("{size} video memory").format(size=human_bytes(g.vram_bytes)))
        if g.compute_cap:
            parts.append(_t("compute capability {cc}").format(cc=g.compute_cap))
        parts.append(_t("PCI {pci}").format(pci=g.pci))
        gpus.append(" · ".join(parts))
    vk: list[str] = []
    for d in hw.vulkan:
        if d.device_type == "cpu":
            note = _t("software, not used")
        elif d.uuid == hw.interpolation_device:
            note = _t("used for interpolation")
        else:
            note = {
                "discrete": _t("discrete GPU"),
                "integrated": _t("integrated GPU"),
                "virtual": _t("virtual GPU"),
            }.get(d.device_type, _t("other device"))
        vk.append(_t("{name} ({note})").format(name=d.name, note=note))
    warning: str | None = None
    interp = next((d for d in hw.vulkan if d.uuid == hw.interpolation_device), None)
    decode = next((g for g in hw.gpus if g.pci == hw.decode_device), None)
    if interp is not None and decode is not None and decode.name != interp.name:
        warning = _t(
            "Video is decoded on {decode} but interpolated on {interp}. Copying frames "
            "between GPUs costs time; this can cause dropped frames."
        ).format(decode=decode.name, interp=interp.name)
    return gpus, vk, warning


class ReportDialog(FitDialog):
    """Bug report options: redaction stays on unless the user turns it off."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("report_options")
        self.setWindowTitle(_t("Create bug report"))
        self.body = QLabel(
            _t(
                "The report contains the check results, versions and ButterEye's logs. "
                "TensorRT engines are never included. Nothing is sent anywhere: you choose "
                "where to save the file."
            ),
            self,
        )
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.include_names = QCheckBox(_t("&Include file names and home paths"), self)
        self.include_names.setObjectName("report.include_names")
        self.include_names.setChecked(False)
        buttons = QDialogButtonBox(self)
        self.save_button = QPushButton(_t("&Save…"), self)
        self.save_button.setObjectName("report.save")
        self.save_button.setAccessibleName(_t("Save"))
        cancel = QPushButton(_t("Cancel"), self)
        cancel.setObjectName("report.cancel")
        cancel.setAccessibleName(_t("Cancel"))
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.body)
        lay.addWidget(self.include_names)
        lay.addWidget(buttons)


class SystemPage(Page):
    page_id: ClassVar[str] = "system"
    features: ClassVar[tuple[Feature, ...]] = (Feature.DOCTOR, Feature.HARDWARE, Feature.REPORT)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(_t("System"))
        self.report: DoctorReport | None = None
        self.plugins: tuple[PluginStatus, ...] = ()
        self._ticket: Ticket | None = None
        self._report_ticket: Ticket | None = None
        self._phases: list[str] = []
        self._first = True
        self.op_row: OpRow | None = None

        self.panel = StatePanel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.panel)
        content = self.panel.content
        cl = QVBoxLayout(content)

        self.summary_badge = StatusBadge("info", "", content)
        heading(self.summary_badge.text_label, factor=1.1)
        self.duration_label = QLabel(content)
        self.duration_label.setTextFormat(Qt.TextFormat.PlainText)
        self.op_box = QVBoxLayout()

        self.run_button = QPushButton(_t("&Run checks again"), content)
        self.run_button.setAccessibleName(_t("Run checks again"))
        self.run_button.setAccessibleDescription(_t("F5"))
        self.run_button.setAutoDefault(False)
        self.run_button.clicked.connect(self.run_checks)
        self.report_button = QPushButton(_t("Create &bug report…"), content)
        self.report_button.setAccessibleName(_t("Create bug report"))
        self.report_button.setAutoDefault(False)
        self.report_button.clicked.connect(self.create_report)
        self.setup_button = QPushButton(_t("Run &setup again"), content)
        self.setup_button.setAccessibleName(_t("Run setup again"))
        self.setup_button.setAutoDefault(False)
        self.setup_button.clicked.connect(self.open_setup)
        self.copy_button = QPushButton(_t("&Copy all as text"), content)
        self.copy_button.setAccessibleName(_t("Copy all as text"))
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.copy_all)
        actions = QHBoxLayout()
        for b in (self.run_button, self.report_button, self.setup_button, self.copy_button):
            actions.addWidget(b)
        actions.addStretch(1)

        # REPORT unavailable: visible reason beside the disabled button (§5.2)
        self.report_note = StatusBadge("info", "", content)
        self.report_note_body = QLabel(content)
        self.report_note_body.setWordWrap(True)
        self.report_note_body.setTextFormat(Qt.TextFormat.PlainText)
        self.report_note_code = QLabel(content)
        selectable(self.report_note_code)
        self.report_result = QLabel(content)
        self.report_result.setWordWrap(True)
        self.report_result.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.report_result, name=_t("Bug report result"))
        self.report_result.setVisible(False)

        # TensorRT row before the opt-in
        self.trt_row = QWidget(content)
        trl = QHBoxLayout(self.trt_row)
        trl.setContentsMargins(0, 0, 0, 0)
        self.trt_badge = StatusBadge("info", _t("Experimental NVIDIA TensorRT: off"), self.trt_row)
        self.trt_button = QPushButton(_t("Turn &on…"), self.trt_row)
        self.trt_button.setObjectName("system.trt_on")
        self.trt_button.setAccessibleName(_t("Turn on experimental NVIDIA TensorRT"))
        self.trt_button.setAutoDefault(False)
        self.trt_button.clicked.connect(self.turn_on_trt)
        trl.addWidget(self.trt_badge, 1)
        trl.addWidget(self.trt_button)
        self.trt_row.setVisible(False)

        self.banner_box = QVBoxLayout()
        self.banner: Banner | None = None

        self.findings = FindingsView(content, accessible_name=_t("Check results"))

        self.hw_box = QGroupBox(_t("Hardware"), content)
        hl = QVBoxLayout(self.hw_box)
        self.gpu_caption = QLabel(_t("GPUs"), self.hw_box)
        self.gpu_label = QLabel(self.hw_box)
        self.vk_caption = QLabel(_t("Vulkan devices"), self.hw_box)
        self.vk_label = QLabel(self.hw_box)
        self.cpu_label = QLabel(self.hw_box)
        for lab in (self.gpu_label, self.vk_label, self.cpu_label):
            lab.setWordWrap(True)
            lab.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.gpu_label, name=_t("GPUs"))
        selectable(self.vk_label, name=_t("Vulkan devices"))
        self.hw_warning = StatusBadge("degraded", "", self.hw_box)
        self.hw_warning.setVisible(False)
        for w in (
            self.gpu_caption,
            self.gpu_label,
            self.vk_caption,
            self.vk_label,
            self.cpu_label,
            self.hw_warning,
        ):
            hl.addWidget(w)

        self.plugins_box = QGroupBox(_t("Plugins"), content)
        pl = QVBoxLayout(self.plugins_box)
        self.plugins_model = QStandardItemModel(0, 6, self)
        self.plugins_model.setHorizontalHeaderLabels(
            [
                _t("Name"),
                _t("Package"),
                _t("Version"),
                _t("Active copy"),
                _t("Loads in mpv"),
                _t("Licence files"),
            ]
        )
        self.plugins_table = flat_view(self.plugins_box, self.plugins_model, _t("Plugins"))
        pl.addWidget(self.plugins_table)

        self.hint = CliHint(command_hint("doctor"), content)
        self.report_hint = CliHint(command_hint("doctor.report"), content)
        self.plugins_hint = CliHint(command_hint("plugins.list"), content)

        cl.addWidget(self.summary_badge)
        cl.addWidget(self.duration_label)
        cl.addLayout(self.op_box)
        cl.addLayout(actions)
        cl.addWidget(self.report_note)
        cl.addWidget(self.report_note_body)
        cl.addWidget(self.report_note_code)
        cl.addWidget(self.report_result)
        cl.addLayout(self.banner_box)
        cl.addWidget(self.trt_row)
        cl.addWidget(self.findings, 1)
        cl.addWidget(self.hw_box)
        cl.addWidget(self.plugins_box)
        cl.addWidget(self.hint)
        cl.addWidget(self.report_hint)
        cl.addWidget(self.plugins_hint)
        self.panel.show_empty(_t("Press F5 to check your system."))

    # ------------------------------------------------------------------ Page
    def title(self) -> str:
        return _t("System")

    def state_panel(self) -> StatePanel:
        return self.panel

    def cli_hint(self) -> CommandHint | None:
        return command_hint("doctor")

    def handle_action(self, action: PageAction) -> bool:
        if action is PageAction.RUN_CHECKS:
            self.run_checks()
            return True
        if action is PageAction.CREATE_REPORT:
            self.create_report()
            return True
        return False

    def refresh(self) -> None:
        """First show: the last report if there is one; afterwards (F5) run the checks."""
        gate = self._gate(Feature.DOCTOR)
        if gate is not None:
            self.panel.show_unavailable(Feature.DOCTOR, gate, command_hint("doctor"))
            return
        self._sync_report_action()
        if self._first:
            self._first = False
            self.ctx.bridge.call(
                lambda core: core.last_report(), owner=self, ok=self._on_last_report
            )
            return
        self.run_checks()

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, ConfigChanged) and self.report is not None:
            self._sync_trt_row()

    # ------------------------------------------------------------------ checks
    def _gate(self, f: Feature) -> CapState | None:
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(f) if caps is not None else None
        if st is not None and not st.available and st.reason in GATING_REASONS:
            return st
        return None

    def _on_last_report(self, report: DoctorReport | None) -> None:
        if report is None:
            self.run_checks()
        else:
            self._show_report(report)

    def is_running(self) -> bool:
        return self._ticket is not None

    def run_checks(self, *, trt: bool | None = None) -> None:
        if self._gate(Feature.DOCTOR) is not None:
            self.refresh()
            return
        if self._ticket is not None:
            return
        self._phases = []
        self.run_button.setEnabled(False)
        if self.report is None:
            self.panel.show_loading(_t("Checking your system (up to 10 seconds)…"), self.cancel)
        else:
            self._clear_op_row()
            self.op_row = OpRow(_t("Checking your system (up to 10 seconds)"), self.cancel)
            self.op_box.addWidget(self.op_row)
        self.ctx.announce(_t("Checking your system…"))

        self._ticket = self.ctx.bridge.run_op(
            lambda core: core.doctor(trt=trt),
            owner=self,
            ok=self._on_report,
            err=self._on_error,
            progress=self._on_progress,
        )

    def cancel(self) -> None:
        if self._ticket is None:
            return
        self._ticket.cancel()
        self._ticket = None
        self.run_button.setEnabled(True)
        self.ctx.announce(_t("Checks cancelled."))
        if self.report is None:
            self.panel.show_empty(_t("Checks cancelled. Press F5 to check your system."))
        elif self.op_row is not None:
            self.op_row.finish(False, _t("Cancelled"), cancelled=True)

    def _on_progress(self, p: Progress) -> None:
        phase = render(p.phase)
        if phase and (not self._phases or self._phases[-1] != phase):
            self._phases.append(phase)
        if self.op_row is not None:
            self.op_row.update(p)
        if self.report is None and self.panel.state == "loading":
            done = "\n".join(self._phases)
            self.panel.loading_label.setText(
                _t("Checking your system (up to 10 seconds)…") + ("\n" + done if done else "")
            )

    def _on_report(self, report: DoctorReport) -> None:
        self._ticket = None
        self.run_button.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(True, _t("Checks finished"))
        self._show_report(report)
        self.ctx.announce(_t("Checks finished: {summary}").format(summary=self.findings.summary()))

    def _on_error(self, err: ButterEyeError) -> None:
        self._ticket = None
        self.run_button.setEnabled(True)
        if isinstance(err, OperationCancelled):
            self.cancel()
            return
        if self.report is None:
            self.panel.show_error(err, actions=((_t("&Retry"), self.run_checks),))
        else:
            if self.op_row is not None:
                self.op_row.finish(False, render(err.cause))
            self._set_banner(Banner.from_error(err, ((_t("&Retry"), self.run_checks),)))
        self.ctx.announce(
            _t("Checks failed: {cause}").format(cause=render(err.cause)), assertive=True
        )

    def _show_report(self, report: DoctorReport) -> None:
        self.report = report
        self._set_banner(None)
        self.findings.set_findings(visible_findings(report))
        kind: BadgeKind = "ok"
        if report.blocking:
            kind = "blocking"
        elif self.findings.counts()[Severity.DEGRADED]:
            kind = "degraded"
        self.summary_badge.set_state(kind, self.findings.summary())
        self.duration_label.setText(
            _t("Checked in {s} seconds.").format(s=f"{report.duration_s:.1f}")
        )
        self._show_hardware(report.hardware)
        self._sync_trt_row()
        self._sync_report_action()
        self.panel.show_content()
        self.ctx.bridge.call(
            lambda core: core.plugins(), owner=self, ok=self._on_plugins, err=self._quiet
        )

    def _quiet(self, err: ButterEyeError) -> None:
        self.ctx.status(f"{err.code.code}: {render(err.cause)}")

    def _set_banner(self, banner: Banner | None) -> None:
        if self.banner is not None:
            self.banner_box.removeWidget(self.banner)
            self.banner.hide()
            self.banner.deleteLater()
        self.banner = banner
        if banner is not None:
            banner.setParent(self.panel.content)
            self.banner_box.addWidget(banner)
            banner.show()

    def _clear_op_row(self) -> None:
        if self.op_row is not None:
            self.op_box.removeWidget(self.op_row)
            self.op_row.hide()
            self.op_row.deleteLater()
            self.op_row = None

    # ------------------------------------------------------------------ hardware / plugins
    def _show_hardware(self, hw: HardwareInfo) -> None:
        gate = self._gate(Feature.HARDWARE)
        if gate is not None:
            head, body = split_headline(render(unavailable_text(Feature.HARDWARE, gate)))
            self.gpu_label.setText(head)
            self.vk_label.setText(body)
            self.cpu_label.setText("")
            self.hw_warning.setVisible(False)
            return
        gpus, vk, warning = hardware_lines(hw)
        self.gpu_label.setText("\n".join(gpus) or _t("No GPU found."))
        self.gpu_label.setAccessibleName(_t("GPUs: {list}").format(list=self.gpu_label.text()))
        self.vk_label.setText("\n".join(vk) or _t("No Vulkan device found."))
        self.vk_label.setAccessibleName(
            _t("Vulkan devices: {list}").format(list=self.vk_label.text())
        )
        self.cpu_label.setText(_t("CPU threads: {n}").format(n=hw.cpu_threads))
        self.hw_warning.setVisible(warning is not None)
        if warning is not None:
            self.hw_warning.set_state("degraded", warning)

    def _on_plugins(self, plugins: tuple[PluginStatus, ...]) -> None:
        self.plugins = tuple(plugins)
        self.plugins_model.removeRows(0, self.plugins_model.rowCount())
        copies = {
            "buttereye": _t("ButterEye package"),
            "distro": _t("Fedora package"),
            "user-vstrt": _t("Built by you"),
            "none": _t("Not installed"),
        }
        for p in self.plugins:
            version = p.version or _t("—")
            if p.variant:
                version = _t("{version} ({variant})").format(version=version, variant=p.variant)
            loads = (
                _t("Not checked")
                if p.loads_in_mpv is None
                else (_t("Yes") if p.loads_in_mpv else _t("No"))
            )
            files = ", ".join(f.name for f in p.licence_files) or _t("none found")
            row = [
                QStandardItem(p.name),
                QStandardItem(p.package or _t("—")),
                QStandardItem(version),
                QStandardItem(copies.get(p.active_copy, p.active_copy)),
                QStandardItem(loads),
                QStandardItem(files),
            ]
            row[5].setToolTip("\n".join(str(f) for f in p.licence_files))
            self.plugins_model.appendRow(row)

    # ------------------------------------------------------------------ TensorRT
    def _sync_trt_row(self) -> None:
        r = self.report
        self.trt_row.setVisible(r is not None and not r.trt_included)

    def turn_on_trt(self) -> None:
        dlg = TrtOptInDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        async def opt_in(core: ButterEye) -> str:
            load = await core.load_config()
            return await core.set_trt_experimental(True, expected_revision=load.revision)

        self.trt_button.setEnabled(False)
        self.ctx.bridge.call(opt_in, owner=self, ok=self._on_trt_on, err=self._on_trt_error)

    def _on_trt_on(self, _revision: str) -> None:
        self.trt_button.setEnabled(True)
        self.ctx.announce(_t("Experimental NVIDIA TensorRT turned on. Checking again…"))
        self.run_checks(trt=True)

    def _on_trt_error(self, err: ButterEyeError) -> None:
        self.trt_button.setEnabled(True)
        self._set_banner(Banner.from_error(err))

    # ------------------------------------------------------------------ actions
    def _sync_report_action(self) -> None:
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(Feature.REPORT) if caps is not None else None
        unavailable = st is not None and not st.available
        self.report_button.setEnabled(not unavailable)
        for w in (self.report_note, self.report_note_body, self.report_note_code):
            w.setVisible(unavailable)
        self.report_hint.setVisible(not unavailable)
        if st is None or not unavailable:
            self.report_button.setAccessibleDescription("")
            return
        head, body = split_headline(render(unavailable_text(Feature.REPORT, st)))
        code = st.code or (
            ErrorCode.NOT_IMPLEMENTED if st.reason is Reason.NOT_IMPLEMENTED else None
        )
        self.report_note.set_state("info", head)
        self.report_note_body.setText(body)
        self.report_note_body.setVisible(bool(body))
        self.report_note_code.setText(code.code if code is not None else "")
        self.report_note_code.setAccessibleName(
            _t("Error code {code}").format(code=code.code if code is not None else "")
        )
        self.report_note_code.setVisible(code is not None)
        self.report_button.setAccessibleDescription(" ".join(t for t in (head, body) if t))

    def copy_all(self) -> None:
        text = self.all_text()
        QGuiApplication.clipboard().setText(text)
        self.ctx.announce(_t("Check results copied."))

    def all_text(self) -> str:
        r = self.report
        if r is None:
            return ""
        parts = [findings_text(visible_findings(r)), "", _t("Hardware")]
        gpus, vk, warning = hardware_lines(r.hardware)
        parts += [f"  {g}" for g in gpus] + [f"  {v}" for v in vk]
        if warning:
            parts.append(f"  {warning}")
        if self.plugins:
            parts += ["", _t("Plugins")]
            for p in self.plugins:
                parts.append(
                    f"  {p.name}: {p.package or '-'} {p.version or '-'} "
                    f"[{p.active_copy}] loads_in_mpv={p.loads_in_mpv}"
                )
        return "\n".join(parts)

    def open_setup(self) -> None:
        win = self.window()
        opener = getattr(win, "open_setup", None)
        if callable(opener):
            opener()
            return
        from buttereye.gui.dialogs.setup_wizard import SetupWizard

        dlg = SetupWizard(self.ctx, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.open()

    def create_report(self) -> None:
        if not self.report_button.isEnabled() or self._report_ticket is not None:
            self._sync_report_action()
            return
        opts = ReportDialog(self)
        if opts.exec() != QDialog.DialogCode.Accepted:
            return
        redact = not opts.include_names.isChecked()
        name, _filter = QFileDialog.getSaveFileName(
            self,
            _t("Save bug report"),
            "buttereye-report.tar.gz",
            _t("Bug report (*.tar.gz)"),
        )
        if not name:
            return
        self.start_report(Path(name), redact=redact)

    def start_report(self, dest: Path, *, redact: bool) -> None:
        self._clear_op_row()
        self.op_row = OpRow(_t("Creating bug report"), self._cancel_report)
        self.op_box.addWidget(self.op_row)
        self._report_ticket = self.ctx.bridge.run_op(
            lambda core: core.report_bundle(dest, redact=redact),
            owner=self,
            ok=self._on_report_done,
            err=self._on_report_error,
            progress=self._report_progress,
        )

    def _report_progress(self, p: Progress) -> None:
        if self.op_row is not None:
            self.op_row.update(p)

    def _cancel_report(self) -> None:
        if self._report_ticket is not None:
            self._report_ticket.cancel()
            self._report_ticket = None
        if self.op_row is not None:
            self.op_row.finish(False, _t("Cancelled"), cancelled=True)

    def _on_report_done(self, path: Path) -> None:
        self._report_ticket = None
        text = _t("Bug report saved: {path}").format(path=path)
        if self.op_row is not None:
            self.op_row.finish(True, text)
        self.report_result.setText(text)
        self.report_result.setVisible(True)
        self.ctx.announce(text)

    def _on_report_error(self, err: ButterEyeError) -> None:
        self._report_ticket = None
        if isinstance(err, OperationCancelled):
            self._cancel_report()
            return
        if self.op_row is not None:
            self.op_row.finish(False, render(err.cause))
        self._set_banner(Banner.from_error(err))
        self.ctx.announce(
            _t("Bug report failed: {cause}").format(cause=render(err.cause)), assertive=True
        )


__all__ = ["ReportDialog", "SystemPage", "hardware_lines", "visible_findings"]
