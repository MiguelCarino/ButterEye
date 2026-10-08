# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""First-run setup wizard (docs/design/GUI.md §4.2; SCOPE F0, §4.7). CLI: ``buttereye setup``.

``QWizard`` in ``ClassicStyle``, modal. The plan lives in memory; **nothing is
written before the Apply page** (``setup_apply`` writes ``config.toml`` last).
Opting in to TensorRT here only changes the in-memory plan: the opt-in is
saved by Apply together with everything else. Cancelling before Apply leaves
no files; cancelling during Apply cancels the operation ("Cancelled. Nothing
was saved.").

Pages: 1 Welcome · 2 Checks · 3 Engine · 4 Packages (only when packages are
missing) · 5 Downloads (only when the plan lists downloads) · 6 Apply ·
7 Finish.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from buttereye.core.api import (
    BackendId,
    BackendStatus,
    BlockingIssue,
    ButterEyeError,
    CapState,
    DoctorReport,
    Feature,
    LegalNotices,
    OperationCancelled,
    Progress,
    SetupChoices,
    SetupPlan,
    SetupResult,
    command_hint,
    legal_notices,
    render,
    unavailable_text,
)
from buttereye.gui.a11y import announce
from buttereye.gui.bridge import Ticket
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.consent import DownloadTable
from buttereye.gui.dialogs.fit import FitDialog
from buttereye.gui.dialogs.text_viewer import TextViewer
from buttereye.gui.dialogs.trt_optin import TrtOptInDialog
from buttereye.gui.pages.base import GATING_REASONS
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.findings_view import FindingsView
from buttereye.gui.widgets.op_row import OpRow
from buttereye.gui.widgets.state_panel import StatePanel
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge

_log = logging.getLogger(__name__)

INCLUDE_DIALOG = "buttereye.gui.dialogs.include_line"
#: Legal notice text is never translated; used only if the licences provider is absent.
FALLBACK_COPYRIGHT = "Copyright (C) 2026 The ButterEye contributors"
FALLBACK_LICENCE = "GNU Affero General Public License v3.0 or later"

PAGE_WELCOME, PAGE_CHECKS, PAGE_ENGINE, PAGE_PACKAGES, PAGE_DOWNLOADS, PAGE_APPLY, PAGE_FINISH = (
    range(7)
)


def _t(text: str) -> str:
    return QCoreApplication.translate("SetupWizard", text)


def backend_label(b: BackendId) -> str:
    names = {
        BackendId.RIFE_NCNN: _t("RIFE (Vulkan)"),
        BackendId.MVTOOLS: _t("MVTools (CPU)"),
        BackendId.RIFE_TRT: _t("RIFE · TensorRT (experimental)"),
    }
    return names[b]


def _label(text: str, parent: QWidget) -> QLabel:
    lab = QLabel(text, parent)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    lab.setWordWrap(True)
    return lab


def _clear(layout: QVBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget() if item is not None else None
        if w is not None:
            w.hide()
            w.deleteLater()


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


class WelcomePage(QWizardPage):
    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Welcome to ButterEye"))
        lay = QVBoxLayout(self)
        self.intro = _label(
            _t(
                "ButterEye makes video in mpv look smoother by adding in-between frames.\n"
                "It never edits your mpv.conf; it only offers a one-line include at the "
                "end, and only adds it if you ask.\n"
                "It uses no network unless you start a download yourself."
            ),
            self,
        )
        self.next_label = _label(
            _t("Setup checks your system first. Nothing is saved until the last step."), self
        )
        self.copyright = _label("", self)
        self.licence = _label("", self)
        self.view_licence = QPushButton(_t("&View licence"), self)
        self.view_licence.setObjectName("setup.view_licence")
        self.view_licence.setAccessibleName(_t("View licence"))
        self.view_licence.setAutoDefault(False)
        self.view_licence.clicked.connect(self._view)
        self.hint = CliHint(command_hint("setup"), self)
        lay.addWidget(self.intro)
        lay.addWidget(self.next_label)
        lay.addSpacing(self.fontMetrics().height())
        lay.addWidget(self.copyright)
        lay.addWidget(self.licence)
        row = QHBoxLayout()
        row.addWidget(self.view_licence)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        lay.addWidget(self.hint)
        self.notices: LegalNotices | None = None
        try:
            self.notices = legal_notices()
        except ButterEyeError:
            self.notices = None
        if self.notices is not None:
            self.copyright.setText(self.notices.copyright)
            self.licence.setText(_t("Licence: {name}").format(name=self.notices.licence_name))
            self.view_licence.setEnabled(self.notices.agpl_text not in self.notices.missing)
        else:
            self.copyright.setText(FALLBACK_COPYRIGHT)
            self.licence.setText(_t("Licence: {name}").format(name=FALLBACK_LICENCE))
            self.view_licence.setEnabled(False)

    def _view(self) -> None:
        if self.notices is None:
            return
        dlg = TextViewer(_t("GNU Affero General Public License"), [self.notices.agpl_text], self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.open()


class ChecksPage(QWizardPage):
    """Page 2: doctor with progress + Cancel, then the grouped findings."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Checking your system"))
        self.busy = False
        self.op_row: OpRow | None = None
        lay = QVBoxLayout(self)
        self.panel = StatePanel(self)
        lay.addWidget(self.panel)
        c = self.panel.content
        cl = QVBoxLayout(c)
        self.op_box = QVBoxLayout()
        self.summary = StatusBadge("info", "", c)
        self.blocking_label = _label(_t("Fix the blocking items, then press Check again."), c)
        self.blocking_label.setObjectName("setup.blocking_note")
        self.findings = FindingsView(c, accessible_name=_t("Check results"))
        self.again = QPushButton(_t("C&heck again"), c)
        self.again.setObjectName("setup.check_again")
        self.again.setAccessibleName(_t("Check again"))
        self.again.setAutoDefault(False)
        self.again.clicked.connect(self.start)
        row = QHBoxLayout()
        row.addWidget(self.again)
        row.addWidget(self.blocking_label, 1)
        cl.addLayout(self.op_box)
        cl.addWidget(self.summary)
        cl.addWidget(self.findings, 1)
        cl.addLayout(row)
        self.summary.setVisible(False)
        self.findings.setVisible(False)
        self.blocking_label.setVisible(False)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        if self.wiz.report is None and not self.busy:
            self.start()

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        plan = self.wiz.plan
        return not self.busy and plan is not None and not plan.report.blocking

    def start(self) -> None:
        gate = self.wiz.gate(Feature.SETUP) or self.wiz.gate(Feature.DOCTOR)
        if gate is not None:
            feature = Feature.SETUP if self.wiz.gate(Feature.SETUP) else Feature.DOCTOR
            self.panel.show_unavailable(feature, gate, command_hint("setup"))
            self.completeChanged.emit()
            return
        if self.busy:
            return
        self.busy = True
        self.panel.show_content()
        self.again.setEnabled(False)
        self.findings.setVisible(False)
        self.summary.setVisible(False)
        self.blocking_label.setVisible(False)
        _clear(self.op_box)
        self.op_row = OpRow(_t("Checking your system (up to 10 seconds)"), self.cancel, self)
        self.op_box.addWidget(self.op_row)
        self.completeChanged.emit()
        self.wiz.run_checks(self, progress=self.op_row.update, ok=self._on_plan, err=self._on_error)

    def cancel(self) -> None:
        self.wiz.cancel_checks()
        self.busy = False
        if self.op_row is not None:
            self.op_row.finish(False, _t("Cancelled"), cancelled=True)
        self.again.setEnabled(True)
        self.completeChanged.emit()

    def _on_plan(self, plan: SetupPlan) -> None:
        self.busy = False
        self.again.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(True, _t("Checks finished"))
        self.show_report(plan.report)
        self.completeChanged.emit()
        announce(self, _t("Checks finished: {summary}").format(summary=self.findings.summary()))

    def show_report(self, report: DoctorReport) -> None:
        self.findings.set_findings(report.findings)
        self.findings.setVisible(True)
        kind: BadgeKind = "blocking" if report.blocking else "ok"
        self.summary.set_state(kind, self.findings.summary())
        self.summary.setVisible(True)
        self.blocking_label.setVisible(report.blocking)

    def _on_error(self, err: ButterEyeError) -> None:
        self.busy = False
        self.again.setEnabled(True)
        if isinstance(err, OperationCancelled):
            self.cancel()
            return
        self.panel.show_error(err, actions=((_t("&Retry"), self.start),))
        self.completeChanged.emit()
        announce(self, _t("Checks failed: {cause}").format(cause=render(err.cause)), assertive=True)


class EnginePage(QWizardPage):
    """Page 3: backend radios; experimental TensorRT behind two unchecked boxes."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Choose the interpolation engine"))
        self.busy = False
        self.radios: dict[BackendId, QRadioButton] = {}
        self.reasons: dict[BackendId, QLabel] = {}
        lay = QVBoxLayout(self)
        self.box = QGroupBox(_t("Engine choice"), self)
        self.box.setAccessibleName(_t("Engine choice"))
        self.box_lay = QVBoxLayout(self.box)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.buttonToggled.connect(lambda *_a: self.completeChanged.emit())
        self.recommended = _label("", self)
        self.show_experimental = QCheckBox(_t("Show &experimental options"), self)
        self.show_experimental.setObjectName("setup.show_experimental")
        self.show_experimental.setChecked(False)
        self.use_trt = QCheckBox(_t("Use experimental NVIDIA &TensorRT"), self)
        self.use_trt.setObjectName("setup.use_trt")
        self.use_trt.setChecked(False)
        self.use_trt.setVisible(False)
        self.show_experimental.toggled.connect(self._toggle_experimental)
        self.use_trt.clicked.connect(self._trt_clicked)
        self.op_box = QVBoxLayout()
        self.op_row: OpRow | None = None
        self.banner_box = QVBoxLayout()
        lay.addWidget(self.recommended)
        lay.addWidget(self.box)
        lay.addWidget(self.show_experimental)
        lay.addWidget(self.use_trt)
        lay.addLayout(self.op_box)
        lay.addLayout(self.banner_box)
        lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        self.populate()

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        if self.busy or self.wiz.plan is None or self.wiz.plan.report.blocking:
            return False
        b = self.selected()
        return b is not None and self._status(b) is not None

    def _status(self, b: BackendId) -> BackendStatus | None:
        plan = self.wiz.plan
        if plan is None:
            return None
        return next((s for s in plan.backends if s.id is b and s.available), None)

    def selected(self) -> BackendId | None:
        for b, r in self.radios.items():
            if r.isChecked():
                return b
        return None

    def populate(self) -> None:
        plan = self.wiz.plan
        previous = self.selected()
        for r in list(self.radios.values()):
            self.group.removeButton(r)
        _clear(self.box_lay)
        self.radios.clear()
        self.reasons.clear()
        if plan is None:
            return
        proposed = plan.proposed.backend
        self.recommended.setText(
            _t("Recommended: {reason}").format(reason=render(plan.proposed.reason))
            if proposed is not None
            else _t("No engine can run on this system yet: {reason}").format(
                reason=render(plan.proposed.reason)
            )
        )
        for st in plan.backends:
            if st.id is BackendId.RIFE_TRT and not self.wiz.trt:
                continue  # listed only after the opt-in (§5.2)
            r = QRadioButton(backend_label(st.id), self.box)
            r.setObjectName(f"setup.engine.{st.id.value}")
            reason = render(st.reason)
            if st.code is not None:
                reason += f" ({st.code.code})"
            if not st.available:
                reason = _t("Unavailable: {reason}").format(reason=reason)
            lab = _label(reason, self.box)
            lab.setIndent(self.fontMetrics().horizontalAdvance("MM"))
            r.setAccessibleDescription(reason)
            r.setEnabled(st.available)
            self.group.addButton(r)
            self.radios[st.id] = r
            self.reasons[st.id] = lab
            self.box_lay.addWidget(r)
            self.box_lay.addWidget(lab)
        pick = previous if previous in self.radios and self._status(previous) else proposed
        if pick is not None and pick in self.radios and self.radios[pick].isEnabled():
            self.radios[pick].setChecked(True)
        self.completeChanged.emit()

    def _toggle_experimental(self, on: bool) -> None:
        self.use_trt.setVisible(on)

    def _trt_clicked(self, checked: bool) -> None:
        if checked:
            dlg = TrtOptInDialog(self)
            self.wiz.last_optin = dlg
            if dlg.exec() != QDialog.DialogCode.Accepted:
                self.use_trt.setChecked(False)
                return
        self.wiz.trt = checked
        self._recheck()

    def _recheck(self) -> None:
        self.busy = True
        self.use_trt.setEnabled(False)
        _clear(self.op_box)
        _clear(self.banner_box)
        label = (
            _t("Checking TensorRT (up to 10 seconds)")
            if self.wiz.trt
            else _t("Updating the engine list")
        )
        self.op_row = OpRow(label, self._cancel, self)
        self.op_box.addWidget(self.op_row)
        self.completeChanged.emit()
        self.wiz.run_checks(self, progress=self.op_row.update, ok=self._on_plan, err=self._on_error)

    def _cancel(self) -> None:
        self.wiz.cancel_checks()
        self.busy = False
        self.wiz.trt = False
        self.use_trt.setChecked(False)
        self.use_trt.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(False, _t("Cancelled"), cancelled=True)
        self.populate()

    def _on_plan(self, _plan: SetupPlan) -> None:
        self.busy = False
        self.use_trt.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(True, _t("Checks finished"))
        self.populate()
        if self.wiz.plan is not None and self.wiz.plan.report.blocking:
            self.banner_box.addWidget(
                Banner(
                    "blocking",
                    _t("The checks found a blocking problem."),
                    _t("Go back to the Checks page to see it."),
                    parent=self,
                )
            )

    def _on_error(self, err: ButterEyeError) -> None:
        self.busy = False
        self.use_trt.setEnabled(True)
        if isinstance(err, OperationCancelled):
            self._cancel()
            return
        if self.op_row is not None:
            self.op_row.finish(False, render(err.cause))
        self.banner_box.addWidget(Banner.from_error(err, parent=self))
        self.completeChanged.emit()


class PackagesPage(QWizardPage):
    """Page 4: the exact dnf lines (never run) + [Check again]."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Packages to install"))
        self.busy = False
        lay = QVBoxLayout(self)
        self.body = _label(
            _t("Run these in a terminal, then press Check again. ButterEye never runs dnf."),
            self,
        )
        self.missing = _label("", self)
        self.cmd_box = QVBoxLayout()
        self.fields: list[CopyField] = []
        self.again = QPushButton(_t("C&heck again"), self)
        self.again.setObjectName("setup.packages_again")
        self.again.setAccessibleName(_t("Check again"))
        self.again.setAutoDefault(False)
        self.again.clicked.connect(self._recheck)
        self.op_box = QVBoxLayout()
        self.op_row: OpRow | None = None
        self.banner_box = QVBoxLayout()
        self.optional = _label(
            _t("These packages are optional unless a check above is blocking; you can go on."),
            self,
        )
        lay.addWidget(self.body)
        lay.addWidget(self.missing)
        lay.addLayout(self.cmd_box)
        row = QHBoxLayout()
        row.addWidget(self.again)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addLayout(self.op_box)
        lay.addLayout(self.banner_box)
        lay.addWidget(self.optional)
        lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        self.populate()

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        plan = self.wiz.plan
        return not self.busy and plan is not None and not plan.report.blocking

    def populate(self) -> None:
        _clear(self.cmd_box)
        self.fields = []
        plan = self.wiz.plan
        if plan is None:
            return
        self.missing.setText(
            _t("Missing: {packages}").format(packages=", ".join(plan.missing_packages))
            if plan.missing_packages
            else _t("Nothing is missing any more.")
        )
        n = len(plan.dnf_lines)
        for i, line in enumerate(plan.dnf_lines):
            name = (
                _t("Copy dnf command")
                if n == 1
                else _t("Copy dnf command {i} of {n}").format(i=i + 1, n=n)
            )
            f = CopyField(line, accessible_name=name, parent=self)
            self.fields.append(f)
            self.cmd_box.addWidget(f)
        self.optional.setVisible(not plan.report.blocking)
        self.completeChanged.emit()

    def _recheck(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.again.setEnabled(False)
        _clear(self.op_box)
        _clear(self.banner_box)
        self.op_row = OpRow(_t("Checking your system (up to 10 seconds)"), self._cancel, self)
        self.op_box.addWidget(self.op_row)
        self.completeChanged.emit()
        self.wiz.run_checks(self, progress=self.op_row.update, ok=self._on_plan, err=self._on_error)

    def _cancel(self) -> None:
        self.wiz.cancel_checks()
        self.busy = False
        self.again.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(False, _t("Cancelled"), cancelled=True)
        self.completeChanged.emit()

    def _on_plan(self, _plan: SetupPlan) -> None:
        self.busy = False
        self.again.setEnabled(True)
        if self.op_row is not None:
            self.op_row.finish(True, _t("Checks finished"))
        self.populate()

    def _on_error(self, err: ButterEyeError) -> None:
        self.busy = False
        self.again.setEnabled(True)
        if isinstance(err, OperationCancelled):
            self._cancel()
            return
        if self.op_row is not None:
            self.op_row.finish(False, render(err.cause))
        self.banner_box.addWidget(Banner.from_error(err, parent=self))
        self.completeChanged.emit()


class DownloadsPage(QWizardPage):
    """Page 5: consent table, nothing pre-checked (§5.4)."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Downloads for the experimental TensorRT path"))
        lay = QVBoxLayout(self)
        self.body = _label(
            _t(
                "Tick only what you want downloaded. Each file comes from the upstream "
                "address shown and is checked against its SHA-256 before it is installed."
            ),
            self,
        )
        self.table = DownloadTable((), self)
        self.body.setBuddy(self.table)
        self.none_label = _label(_t("This release lists no models to download."), self)
        self.none_body = _label(
            _t("Setup continues without TensorRT models; RIFE (Vulkan) and MVTools still work."),
            self,
        )
        lay.addWidget(self.body)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.none_label)
        lay.addWidget(self.none_body)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        plan = self.wiz.plan
        listed = self.wiz.gate_any(Feature.MODEL_DOWNLOADS) is None
        items = plan.downloads if plan is not None and listed else ()
        self.table.set_items(items)
        self.table.setVisible(bool(items))
        self.body.setVisible(bool(items))
        self.none_label.setVisible(not items)
        self.none_body.setVisible(not items)

    def confirmed(self) -> frozenset[str]:
        return self.table.checked_names() if self.table.isVisible() else frozenset()


class ApplyPage(QWizardPage):
    """Page 6: downloads, smoke test, writing settings — one row per phase."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("Setting up"))
        self.setCommitPage(True)
        self.busy = False
        self.rows: list[OpRow] = []
        self._phase: str | None = None
        lay = QVBoxLayout(self)
        self.intro = _label(
            _t("ButterEye runs a 10-second test and then saves your settings."), self
        )
        self.op_box = QVBoxLayout()
        self.status = StatusBadge("busy", "", self)
        self.status.setVisible(False)
        self.findings = FindingsView(self, accessible_name=_t("Problems found"))
        self.findings.setVisible(False)
        self.banner_box = QVBoxLayout()
        self.retry = QPushButton(_t("&Retry"), self)
        self.retry.setObjectName("setup.retry")
        self.retry.setAccessibleName(_t("Retry"))
        self.retry.setAutoDefault(False)
        self.retry.clicked.connect(self.start)
        self.retry.setVisible(False)
        lay.addWidget(self.intro)
        lay.addLayout(self.op_box)
        lay.addWidget(self.status)
        lay.addLayout(self.banner_box)
        lay.addWidget(self.findings, 1)
        row = QHBoxLayout()
        row.addWidget(self.retry)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        self.start()

    def cleanupPage(self) -> None:  # noqa: N802 - Qt API
        if self.busy:
            self.cancel()

    def isComplete(self) -> bool:  # noqa: N802 - Qt API
        return not self.busy and self.wiz.setup_result is not None

    def start(self) -> None:
        if self.busy:
            return
        choices = self.wiz.choices()
        plan = self.wiz.plan
        if choices is None or plan is None:
            return
        self.busy = True
        self.wiz.setup_result = None
        self._phase = None
        _clear(self.op_box)
        _clear(self.banner_box)
        self.rows = []
        self.findings.setVisible(False)
        self.retry.setVisible(False)
        self.status.set_state("busy", _t("Working…"))
        self.status.setVisible(True)
        self.completeChanged.emit()
        self.wiz.start_apply(plan, choices, progress=self._on_progress, ok=self._ok, err=self._err)

    def _row(self, phase: str) -> OpRow:
        if self.rows and not self.rows[-1].is_finished():
            self.rows[-1].finish(True, _t("Done"))
        row = OpRow(phase, self.cancel, self)
        self.rows.append(row)
        self.op_box.addWidget(row)
        return row

    def _on_progress(self, p: Progress) -> None:
        phase = render(p.phase)
        if phase != self._phase or not self.rows:
            self._phase = phase
            self._row(phase)
        self.rows[-1].update(p)

    def cancel(self) -> None:
        if not self.busy:
            return
        self.wiz.cancel_apply()
        self.busy = False
        text = _t("Cancelled. Nothing was saved.")
        if self.rows and not self.rows[-1].is_finished():
            self.rows[-1].finish(False, _t("Cancelled"), cancelled=True)
        self.status.set_state("off", text)
        self.retry.setVisible(True)
        self.completeChanged.emit()
        announce(self, text)

    def _ok(self, _result: SetupResult) -> None:
        self.busy = False
        if self.rows and not self.rows[-1].is_finished():
            self.rows[-1].finish(True, _t("Done"))
        self.status.set_state("ok", _t("Settings saved."))
        self.completeChanged.emit()
        announce(self, _t("Setup finished. Settings saved."))

    def _err(self, err: ButterEyeError) -> None:
        self.busy = False
        if isinstance(err, OperationCancelled):
            self.busy = True
            self.cancel()
            return
        if self.rows and not self.rows[-1].is_finished():
            self.rows[-1].finish(False, render(err.cause))
        self.status.set_state("blocking", _t("Setup did not finish. Nothing was saved."))
        if isinstance(err, BlockingIssue) and err.findings:
            self.findings.set_findings(err.findings)
            self.findings.setVisible(True)
        self.banner_box.addWidget(Banner.from_error(err, parent=self))
        self.retry.setVisible(True)
        self.completeChanged.emit()
        announce(self, _t("Setup failed: {cause}").format(cause=render(err.cause)), assertive=True)


class FinishPage(QWizardPage):
    """Page 7: test result + the two unchecked offers (benchmark, include line)."""

    def __init__(self, wizard: SetupWizard) -> None:
        super().__init__()
        self.wiz = wizard
        self.setTitle(_t("ButterEye is set up"))
        lay = QVBoxLayout(self)
        self.result_badge = StatusBadge("ok", "", self)
        self.findings = FindingsView(self, accessible_name=_t("Test run result"))
        self.findings.setVisible(False)
        self.bench = QCheckBox(_t("&Measure my GPU now (about a minute)"), self)
        self.bench.setObjectName("setup.offer_bench")
        self.bench.setChecked(False)
        self.include = QCheckBox(
            _t("Make ButterEye's mpv profile available in my &normal mpv"), self
        )
        self.include.setObjectName("setup.offer_include")
        self.include.setChecked(False)
        self.include_note = _label("", self)
        lay.addWidget(self.result_badge)
        lay.addWidget(self.findings, 1)
        lay.addWidget(self.bench)
        lay.addWidget(self.include)
        lay.addWidget(self.include_note)
        lay.addStretch(1)

    def initializePage(self) -> None:  # noqa: N802 - Qt API
        r = self.wiz.setup_result
        if r is None:
            return
        if r.smoke_ok:
            text = (
                _t("Test run passed: {fps} fps").format(fps=f"{r.smoke_fps:.0f}")
                if r.smoke_fps is not None
                else _t("Test run passed.")
            )
            self.result_badge.set_state("ok", text)
            self.findings.setVisible(False)
        else:
            title = render(r.smoke_finding.title) if r.smoke_finding else _t("Test run failed.")
            self.result_badge.set_state("degraded", _t("Test run: {title}").format(title=title))
            if r.smoke_finding is not None:
                self.findings.set_findings([r.smoke_finding])
                self.findings.setVisible(True)
        self.include_note.setText(
            _t(
                "This adds one line to your mpv.conf, after you confirm: {line}\n"
                "It makes the profile available (mpv --profile=buttereye) but does not smooth "
                "video in an mpv you start yourself; that needs attaching, which isn't in this "
                "build yet. Use Open and Play on the Play page for smooth motion."
            ).format(line=r.include_line)
        )
        bench_gate = self.wiz.gate_any(Feature.BENCH)
        self.bench.setEnabled(bench_gate is None)
        if bench_gate is not None:
            self.bench.setAccessibleDescription(
                render(unavailable_text(Feature.BENCH, bench_gate)).partition("\n")[0]
            )


# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------


class IncludeLineFallback(FitDialog):
    """Copy-only include line, used when the Play unit's dialog is not in this build."""

    def __init__(self, line: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("include_fallback")
        self.setWindowTitle(_t("ButterEye's profile in mpv"))
        body = _label(
            _t(
                "Add this line to ~/.config/mpv/mpv.conf to make ButterEye's mpv profile "
                "available (mpv --profile=buttereye). It does not smooth video on its own; "
                "smooth motion in an mpv you start yourself needs attaching, which isn't in "
                "this build yet. This build of ButterEye can't add the line for you, and "
                "never changes that file without asking."
            ),
            self,
        )
        self.field = CopyField(line, accessible_name=_t("Include line"), parent=self)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        close.setAccessibleName(_t("Close"))
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(body)
        lay.addWidget(self.field)
        lay.addWidget(buttons)


def open_include_dialog(ctx: GuiContext, line: str, parent: QWidget | None) -> QDialog:
    """The Play unit's ``IncludeLineDialog(bridge, parent)`` (copy, or a write only after
    its own confirmation) when present in this build, else a copy-only dialog."""
    dlg: QDialog | None = None
    try:
        if importlib.util.find_spec(INCLUDE_DIALOG) is not None:
            mod = importlib.import_module(INCLUDE_DIALOG)
            factory: Any = getattr(mod, "IncludeLineDialog", None)
            if factory is not None:
                made = factory(ctx.bridge, parent)
                dlg = made if isinstance(made, QDialog) else None
    except Exception:
        _log.exception("include-line dialog failed to open; using the copy-only fallback")
        dlg = None
    if dlg is None:
        dlg = IncludeLineFallback(line, parent)
    dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
    dlg.open()
    return dlg


class SetupWizard(QWizard):
    """``SetupWizard(ctx, parent)``; the shell opens it window-modal."""

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("setup_wizard")
        self.setWindowTitle(_t("ButterEye setup"))
        self.setWizardStyle(QWizard.WizardStyle.ClassicStyle)
        self.setModal(True)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.report: DoctorReport | None = None
        self.plan: SetupPlan | None = None
        self.setup_result: SetupResult | None = None
        self.trt = False
        self.last_optin: TrtOptInDialog | None = None
        self.include_dialog: QDialog | None = None
        self._checks: Ticket | None = None
        self._checks_gen = 0
        self._apply: Ticket | None = None

        self.welcome = WelcomePage(self)
        self.checks = ChecksPage(self)
        self.engine = EnginePage(self)
        self.packages = PackagesPage(self)
        self.downloads = DownloadsPage(self)
        self.apply_page = ApplyPage(self)
        self.finish_page = FinishPage(self)
        for pid, page in (
            (PAGE_WELCOME, self.welcome),
            (PAGE_CHECKS, self.checks),
            (PAGE_ENGINE, self.engine),
            (PAGE_PACKAGES, self.packages),
            (PAGE_DOWNLOADS, self.downloads),
            (PAGE_APPLY, self.apply_page),
            (PAGE_FINISH, self.finish_page),
        ):
            self.setPage(pid, page)
        self.setStartId(PAGE_WELCOME)
        for which, name in (
            (QWizard.WizardButton.NextButton, _t("Next")),
            (QWizard.WizardButton.BackButton, _t("Back")),
            (QWizard.WizardButton.CancelButton, _t("Cancel")),
            (QWizard.WizardButton.FinishButton, _t("Finish")),
            (QWizard.WizardButton.CommitButton, _t("Next")),
        ):
            btn = self.button(which)
            btn.setAccessibleName(name)
        metrics = self.fontMetrics()
        self.resize(metrics.horizontalAdvance("M") * 70, metrics.height() * 34)

    # ------------------------------------------------------------------ flow
    def nextId(self) -> int:  # noqa: N802 - Qt API
        cur = self.currentId()
        plan = self.plan
        if cur == PAGE_WELCOME:
            return PAGE_CHECKS
        if cur == PAGE_CHECKS:
            return PAGE_ENGINE
        if cur in (PAGE_ENGINE, PAGE_PACKAGES):
            if cur == PAGE_ENGINE and plan is not None and plan.missing_packages:
                return PAGE_PACKAGES
            if plan is not None and plan.downloads and self.trt:
                return PAGE_DOWNLOADS
            return PAGE_APPLY
        if cur == PAGE_DOWNLOADS:
            return PAGE_APPLY
        if cur == PAGE_APPLY:
            return PAGE_FINISH
        return -1

    def gate_any(self, f: Feature) -> CapState | None:
        """Unavailable for any reason (advisory states included)."""
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(f) if caps is not None else None
        return st if st is not None and not st.available else None

    def gate(self, f: Feature) -> CapState | None:
        """Unavailable because this build lacks it (the wizard cannot proceed)."""
        st = self.gate_any(f)
        return st if st is not None and st.reason in GATING_REASONS else None

    def choices(self) -> SetupChoices | None:
        b = self.engine.selected()
        if b is None:
            return None
        return SetupChoices(
            backend=b,
            trt_experimental=self.trt,
            confirmed_downloads=self.downloads.confirmed() if self.trt else frozenset(),
            run_smoke_test=True,
        )

    # ------------------------------------------------------------------ checks
    def run_checks(
        self,
        owner: QWidget,
        *,
        progress: Callable[[Progress], None],
        ok: Callable[[SetupPlan], None],
        err: Callable[[ButterEyeError], None],
    ) -> None:
        """doctor(trt) then setup_plan(report, trt); ``ok`` gets the new plan."""
        self.cancel_checks()
        self._checks_gen += 1
        gen = self._checks_gen
        trt = self.trt

        def planned(plan: SetupPlan) -> None:
            if gen != self._checks_gen:
                return
            self._checks = None
            self.plan = plan
            self.report = plan.report
            ok(plan)

        def reported(report: DoctorReport) -> None:
            if gen != self._checks_gen:
                return
            self.report = report
            self._checks = self.ctx.bridge.call(
                lambda core: core.setup_plan(report, trt_experimental=trt),
                owner=owner,
                ok=planned,
                err=failed,
            )

        def failed(e: ButterEyeError) -> None:
            if gen != self._checks_gen:
                return
            self._checks = None
            err(e)

        self._checks = self.ctx.bridge.run_op(
            lambda core: core.doctor(trt=trt),
            owner=owner,
            ok=reported,
            err=failed,
            progress=progress,
        )

    def cancel_checks(self) -> None:
        self._checks_gen += 1
        if self._checks is not None:
            self._checks.cancel()
            self._checks = None

    # ------------------------------------------------------------------ apply
    def start_apply(
        self,
        plan: SetupPlan,
        choices: SetupChoices,
        *,
        progress: Callable[[Progress], None],
        ok: Callable[[SetupResult], None],
        err: Callable[[ButterEyeError], None],
    ) -> None:
        def done(result: SetupResult) -> None:
            self._apply = None
            self.setup_result = result
            ok(result)

        def failed(e: ButterEyeError) -> None:
            self._apply = None
            err(e)

        self._apply = self.ctx.bridge.run_op(
            lambda core: core.setup_apply(plan, choices),
            owner=self.apply_page,
            ok=done,
            err=failed,
            progress=progress,
        )

    def cancel_apply(self) -> None:
        if self._apply is not None:
            self._apply.cancel()
            self._apply = None

    def is_applying(self) -> bool:
        return self._apply is not None

    # ------------------------------------------------------------------ close
    def reject(self) -> None:
        self.cancel_checks()
        if self.is_applying():
            self.apply_page.cancel()
        super().reject()

    def accept(self) -> None:
        result = self.setup_result
        bench = self.finish_page.bench.isChecked() and self.finish_page.bench.isEnabled()
        include = self.finish_page.include.isChecked()
        parent = self.parentWidget()
        super().accept()
        if result is None:
            return
        if bench:
            self.ctx.go("bench")
        if include:
            self.include_dialog = open_include_dialog(self.ctx, result.include_line, parent)


__all__ = [
    "ApplyPage",
    "ChecksPage",
    "DownloadsPage",
    "EnginePage",
    "FinishPage",
    "IncludeLineFallback",
    "PackagesPage",
    "SetupWizard",
    "WelcomePage",
    "backend_label",
    "open_include_dialog",
]
