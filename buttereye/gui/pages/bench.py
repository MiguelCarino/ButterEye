# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark page (docs/design/GUI.md §4.6; SCOPE F14, §5.3).

Runs the core's benchmark through the bridge (one ``Operation[BenchResult]``),
shows one ``OpRow`` per configuration while it runs, then the results as a
table (the accessible source of truth) next to a ``BenchPlot``. GPU faults are
flagged in words and with BE-1030; a faulting configuration is never offered as
the recommendation, even if a result claims it. [Use recommended] saves it via
``apply_bench()`` with the config revision loaded just before.

Nothing here invents numbers: every value shown comes from a ``BenchResult``
the core measured or stored in ``bench.json``.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QCoreApplication, Qt, QTimer
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    ButterEyeError,
    CapabilitiesChanged,
    CapState,
    CommandHint,
    ConfigLoad,
    DoctorReport,
    ErrorCode,
    Event,
    Feature,
    Msg,
    Notice,
    OperationCancelled,
    Progress,
    Reason,
    Selection,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SourceFacts,
    command_hint,
    render,
)
from buttereye.gui.a11y import announce, heading, labelled
from buttereye.gui.bridge import Ticket
from buttereye.gui.context import GuiContext
from buttereye.gui.pages.base import GATING_REASONS, Page
from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.bench_plot import BenchPlot
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.fraction_edit import FractionEdit
from buttereye.gui.widgets.op_row import OpRow, human_bytes
from buttereye.gui.widgets.state_panel import StatePanel

_log = logging.getLogger(__name__)

CTX = "BenchPage"

#: Unavailable reasons that replace the whole page with the unavailable panel.
PAGE_GATES = GATING_REASONS | {Reason.BLOCKED_BY_DOCTOR}

COLUMNS = (
    "Configuration",
    "vspipe fps",
    "In-mpv fps",
    "Real time",
    "Repeatable (CoV)",
    "Startup s",
    "Reload s",
    "VRAM",
    "GPU faults",
)
COL_CONFIG, COL_VSPIPE, COL_MPV, COL_RT, COL_REP, COL_START, COL_RELOAD, COL_VRAM, COL_FAULTS = (
    range(len(COLUMNS))
)

#: The data role holding the raw value of a numeric cell (tests compare it with the plot).
VALUE_ROLE = Qt.ItemDataRole.UserRole + 1


def _t(text: str) -> str:
    return QCoreApplication.translate(CTX, text)


def fmt_rate(v: Fraction | float) -> str:
    """``23.976``, ``60``, ``47.952``: up to three decimals."""
    return f"{float(v):.3f}".rstrip("0").rstrip(".")


def fmt_fps(v: float) -> str:
    return f"{v:.1f}"


def target_of(req: BenchRequest) -> Fraction:
    """The output rate the benchmark measured against (default: double the source)."""
    return req.target_fps if req.target_fps is not None else req.source_fps * 2


def has_faults(m: BenchMeasurement) -> bool:
    return m.gpu_faults is not None and m.gpu_faults > 0


def measured(m: BenchMeasurement) -> bool:
    """False for a configuration whose vspipe pass failed (the core reports 0 fps)."""
    return m.vspipe_fps > 0


def recommended_measurement(result: BenchResult) -> BenchMeasurement | None:
    """The recommended measurement, never one with GPU faults or that was not measured."""
    if result.recommended is None:
        return None
    m = next((x for x in result.measurements if x.label == result.recommended), None)
    if m is None:
        _log.warning("bench result recommends unknown label %r", result.recommended)
        return None
    if has_faults(m) or not measured(m):
        _log.warning("bench result recommends %r despite faults/failure; ignored", m.label)
        return None
    return m


def faults_text(m: BenchMeasurement) -> str:
    if m.gpu_faults is None:
        return _t("unknown (log not readable)")
    if m.gpu_faults == 0:
        return "0"
    return _t("{n} — unstable").format(n=m.gpu_faults)


def backend_name(b: BackendId) -> str:
    names = {
        BackendId.RIFE_NCNN: _t("RIFE (Vulkan)"),
        BackendId.MVTOOLS: _t("MVTools (CPU)"),
        BackendId.RIFE_TRT: _t("RIFE · TensorRT (experimental)"),
    }
    return names[b]


def request_text(req: BenchRequest) -> str:
    text = _t("{w}×{h} @ {src} → {dst} fps").format(
        w=req.width, h=req.height, src=fmt_rate(req.source_fps), dst=fmt_rate(target_of(req))
    )
    if req.full:
        text += " · " + _t("full matrix")
    return text


def when_text(when: datetime) -> str:
    local = when.astimezone() if when.tzinfo is not None else when
    return local.strftime("%Y-%m-%d %H:%M")


def row_values(m: BenchMeasurement, recommended: bool) -> list[tuple[str, object]]:
    """(display text, raw value) per column, in ``COLUMNS`` order."""
    config = m.label
    if recommended:
        config = _t("{label} — recommended").format(label=m.label)
    elif has_faults(m):
        config = _t("{label} — GPU fault").format(label=m.label)
    ok = measured(m)
    not_measured = _t("Not measured")
    if not ok:
        rep = _t("No")
    elif math.isfinite(m.cov):
        rep = _t("{yes_no} ({cov}%)").format(
            yes_no=_t("Yes") if m.repeatable else _t("No"), cov=f"{m.cov * 100:.1f}"
        )
    else:
        rep = _t("No")
    return [
        (config, m.label),
        (fmt_fps(m.vspipe_fps) if ok else not_measured, m.vspipe_fps),
        (fmt_fps(m.mpv_fps) if ok else not_measured, m.mpv_fps),
        (_t("Yes") if m.realtime else _t("No"), m.realtime),
        (rep, m.repeatable),
        (f"{m.startup_s:.2f}" if ok else "—", m.startup_s),
        (f"{m.reload_s:.2f}" if ok else "—", m.reload_s),
        (human_bytes(m.vram_bytes) if m.vram_bytes is not None else _t("unknown"), m.vram_bytes),
        (faults_text(m), m.gpu_faults),
    ]


class BenchPage(Page):
    page_id: ClassVar[str] = "bench"
    features: ClassVar[tuple[Feature, ...]] = (Feature.BENCH,)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(self.tr("Benchmark"))
        self._history: list[BenchResult] = []
        self._shown: BenchResult | None = None
        self._ticket: Ticket | None = None
        self._rows: dict[str, OpRow] = {}
        self._run_started = 0.0
        self._current_config: str | None = None
        self._user_file: Path | None = None
        self._loaded = False
        self._gated = False
        self.choose_file = self._default_choose_file

        self.panel = StatePanel(self)
        outer = QVBoxLayout(self)
        outer.addWidget(self.panel)
        root = QVBoxLayout(self.panel.content)

        title = heading(QLabel(self.tr("Benchmark"), self.panel.content))
        title.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(title)
        intro = QLabel(
            self.tr(
                "ButterEye measures inside mpv with generated test clips — none of your "
                "videos are used."
            ),
            self.panel.content,
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(intro)

        self.advisory_box = QVBoxLayout()
        root.addLayout(self.advisory_box)
        self.advisory: Banner | None = None

        root.addWidget(self._build_form())

        # running
        self.running_box = QGroupBox(self.tr("Measuring"), self.panel.content)
        self.running_lay = QVBoxLayout(self.running_box)
        self.overall: OpRow | None = None
        self.running_box.setVisible(False)
        root.addWidget(self.running_box)

        # core notices during a run (e.g. "in-mpv speed could not be measured")
        self.notes = QLabel(self.panel.content)
        self.notes.setWordWrap(True)
        self.notes.setTextFormat(Qt.TextFormat.PlainText)
        self.notes.setVisible(False)
        root.addWidget(self.notes)
        self._notes: list[str] = []

        self.error_box = QVBoxLayout()
        root.addLayout(self.error_box)
        self.run_error: Banner | None = None

        self.results_box = self._build_results()
        root.addWidget(self.results_box)
        root.addStretch(1)
        self.panel.show_loading(self.tr("Loading earlier measurements…"))

    # ------------------------------------------------------------------ building
    def _build_form(self) -> QGroupBox:
        box = QGroupBox(self.tr("What to measure"), self.panel.content)
        grid = QGridLayout(box)
        self.source_group = QButtonGroup(box)
        self.session_radio = QRadioButton(self.tr("Current session"), box)
        self.session_radio.setAccessibleName(self.tr("Current session"))
        self.custom_radio = QRadioButton(self.tr("&Custom size"), box)
        self.custom_radio.setAccessibleName(self.tr("Custom size"))
        self.source_group.addButton(self.session_radio)
        self.source_group.addButton(self.custom_radio)
        self.custom_radio.setChecked(True)
        grid.addWidget(self.session_radio, 0, 0, 1, 4)
        grid.addWidget(self.custom_radio, 1, 0, 1, 4)

        self.width_spin = QSpinBox(box)
        self.width_spin.setRange(16, 7680)
        self.width_spin.setSingleStep(2)
        self.width_spin.setValue(1920)
        self.height_spin = QSpinBox(box)
        self.height_spin.setRange(16, 4320)
        self.height_spin.setSingleStep(2)
        self.height_spin.setValue(1080)
        self.rate_edit = FractionEdit(Fraction(24000, 1001), box)
        self.target_edit = FractionEdit(None, box)
        self.target_edit.setPlaceholderText(self.tr("2× source rate"))
        w_label, _ = labelled(self.tr("&Width"), self.width_spin)
        h_label, _ = labelled(self.tr("H&eight"), self.height_spin)
        r_label, _ = labelled(self.tr("Source r&ate (fps)"), self.rate_edit)
        target_help = self.tr("Leave empty to measure double the source rate.")
        t_label, _ = labelled(
            self.tr("Ta&rget rate (fps)"), self.target_edit, description=target_help
        )
        # Short form labels stay on one line; the label columns size to their text
        # and the field columns take the spare width (no "Target rate" / "(fps)" wrap).
        for lab in (w_label, h_label, r_label, t_label):
            lab.setWordWrap(False)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        self.target_help = QLabel(target_help, box)
        self.target_help.setObjectName("bench.target_help")
        self.target_help.setTextFormat(Qt.TextFormat.PlainText)
        self.target_help.setWordWrap(True)
        grid.addWidget(w_label, 2, 0)
        grid.addWidget(self.width_spin, 2, 1)
        grid.addWidget(h_label, 2, 2)
        grid.addWidget(self.height_spin, 2, 3)
        grid.addWidget(r_label, 3, 0)
        grid.addWidget(self.rate_edit, 3, 1)
        grid.addWidget(t_label, 3, 2)
        grid.addWidget(self.target_edit, 3, 3)
        grid.addWidget(self.target_help, 4, 2, 1, 2)

        self.full_check = QCheckBox(self.tr("Full matri&x (720p–2160p, slow)"), box)
        self.full_check.setAccessibleName(self.tr("Full matrix (720p–2160p, slow)"))
        grid.addWidget(self.full_check, 5, 0, 1, 4)
        self.file_check = QCheckBox(self.tr("Also test my &own file…"), box)
        self.file_check.setAccessibleName(self.tr("Also test my own file"))
        self.file_check.toggled.connect(self._on_file_toggled)
        self.file_label = QLabel(box)
        self.file_label.setTextFormat(Qt.TextFormat.PlainText)
        self.file_label.setWordWrap(True)
        self.file_label.setVisible(False)
        grid.addWidget(self.file_check, 6, 0, 1, 4)
        grid.addWidget(self.file_label, 7, 0, 1, 4)

        self.candidates_label = QLabel(box)
        self.candidates_label.setTextFormat(Qt.TextFormat.PlainText)
        self.candidates_label.setWordWrap(True)
        grid.addWidget(self.candidates_label, 8, 0, 1, 4)

        self.form_error = QLabel(box)
        self.form_error.setTextFormat(Qt.TextFormat.PlainText)
        self.form_error.setWordWrap(True)
        self.form_error.setVisible(False)
        grid.addWidget(self.form_error, 9, 0, 1, 4)

        row = QHBoxLayout()
        self.run_button = QPushButton(self.tr("R&un"), box)
        self.run_button.setAccessibleName(self.tr("Run benchmark"))
        self.run_button.setAutoDefault(False)
        self.run_button.clicked.connect(self.run)
        self.cancel_button = QPushButton(self.tr("Ca&ncel"), box)
        self.cancel_button.setAccessibleName(self.tr("Cancel benchmark"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        row.addWidget(self.run_button)
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        grid.addLayout(row, 10, 0, 1, 4)

        self.gpu_warning = QLabel(
            self.tr(
                "Benchmarking pushes the GPU hard; on some setups a model can briefly "
                "stall the GPU. Save your work first."
            ),
            box,
        )
        self.gpu_warning.setObjectName("benchGpuWarning")
        self.gpu_warning.setTextFormat(Qt.TextFormat.PlainText)
        self.gpu_warning.setWordWrap(True)
        self.run_button.setAccessibleDescription(self.gpu_warning.text())
        grid.addWidget(self.gpu_warning, 11, 0, 1, 4)

        self.hint = CliHint(None, box)
        grid.addWidget(self.hint, 12, 0, 1, 4)

        for w in (self.width_spin, self.height_spin):
            w.valueChanged.connect(self._custom_edited)
        for e in (self.rate_edit, self.target_edit):
            e.textEdited.connect(self._custom_edited)
        self.full_check.toggled.connect(self._update_hint)
        self.source_group.buttonToggled.connect(self._update_source_enabled)
        self._update_session_radio()
        self._update_hint()
        return box

    def _build_results(self) -> QGroupBox:
        box = QGroupBox(self.tr("Results"), self.panel.content)
        lay = QVBoxLayout(box)
        self.results_panel = StatePanel(box)
        lay.addWidget(self.results_panel)
        rl = QVBoxLayout(self.results_panel.content)
        rl.setContentsMargins(0, 0, 0, 0)

        self.history_combo = QComboBox(self.results_panel.content)
        hist_label, _ = labelled(self.tr("H&istory"), self.history_combo)
        self.history_combo.currentIndexChanged.connect(self._on_history_index)
        hrow = QHBoxLayout()
        hrow.addWidget(hist_label)
        hrow.addWidget(self.history_combo, 1)
        rl.addLayout(hrow)

        self.result_title = QLabel(self.results_panel.content)
        self.result_title.setTextFormat(Qt.TextFormat.PlainText)
        self.result_title.setWordWrap(True)
        rl.addWidget(self.result_title)

        self.fault_box = QVBoxLayout()
        rl.addLayout(self.fault_box)
        self.fault_banners: list[Banner] = []

        self.model = QStandardItemModel(0, len(COLUMNS), self)
        for i, h in enumerate(COLUMNS):
            self.model.setHeaderData(i, Qt.Orientation.Horizontal, _t(h))
        self.table = QTableView(self.results_panel.content)
        self.table.setModel(self.model)
        self.table.setAccessibleName(self.tr("Benchmark results"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        # Row numbers (queue position); never collapses to zero width when empty.
        vh = self.table.verticalHeader()
        vh.setMinimumWidth(vh.fontMetrics().horizontalAdvance("000"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        rl.addWidget(self.table)

        self.plot = BenchPlot(self.results_panel.content)
        rl.addWidget(self.plot)

        self.recommend_label = QLabel(self.results_panel.content)
        self.recommend_label.setTextFormat(Qt.TextFormat.PlainText)
        self.recommend_label.setWordWrap(True)
        rl.addWidget(self.recommend_label)

        arow = QHBoxLayout()
        self.apply_button = QPushButton(self.tr("Use reco&mmended"), self.results_panel.content)
        self.apply_button.setAccessibleName(self.tr("Use recommended"))
        self.apply_button.setAutoDefault(False)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_recommended)
        arow.addWidget(self.apply_button)
        arow.addStretch(1)
        rl.addLayout(arow)
        self.apply_status = QLabel(self.results_panel.content)
        self.apply_status.setTextFormat(Qt.TextFormat.PlainText)
        self.apply_status.setWordWrap(True)
        self.apply_status.setVisible(False)
        rl.addWidget(self.apply_status)
        self.apply_hint = CliHint(None, self.results_panel.content)
        rl.addWidget(self.apply_hint)

        self.results_panel.show_empty(self.empty_text())
        return box

    # ------------------------------------------------------------------ texts
    def empty_text(self) -> str:
        return self.tr(
            "No measurements yet. ButterEye measures inside mpv with generated test clips — "
            "none of your videos are used."
        )

    # ------------------------------------------------------------------ Page surface
    def state_panel(self) -> StatePanel:
        return self.panel

    def refresh(self) -> None:
        gate = self._gate()
        if gate is not None:
            self._show_gate(*gate)
            return
        self._gated = False
        self._update_advisory()
        self._update_session_radio()
        if not self._loaded:
            self.panel.show_loading(self.tr("Loading earlier measurements…"))
        self.ctx.bridge.call(
            lambda core: core.bench_history(),
            owner=self,
            ok=self._on_history,
            err=self._on_history_error,
        )
        self._load_candidates()

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, CapabilitiesChanged):
            was = self._gated
            gate = self._gate()
            if gate is not None:
                self._show_gate(*gate)
            elif was:
                self.refresh()
            else:
                self._update_advisory()
        elif isinstance(ev, Notice):
            if self._ticket is not None and ev.code is ErrorCode.BENCH_FAILED:
                self._notes.append(render(ev.message))
                self.notes.setText("\n".join(self._notes))
                self.notes.setVisible(True)
        elif isinstance(ev, SessionAdded | SessionChanged | SessionEnded):
            self._update_session_radio()

    def cli_hint(self) -> CommandHint | None:
        return self.hint.hint()

    # ------------------------------------------------------------------ gating
    def _gate(self) -> tuple[Feature, CapState] | None:
        caps = self.ctx.bridge.capabilities
        if caps is None:
            return None
        st = caps.states.get(Feature.BENCH)
        if st is not None and not st.available and st.reason in PAGE_GATES:
            return Feature.BENCH, st
        return None

    def _show_gate(self, feature: Feature, st: CapState) -> None:
        self._gated = True
        actions: tuple[Action, ...] = ()
        if st.reason is Reason.BLOCKED_BY_DOCTOR:
            actions = ((self.tr("Open &System"), lambda: self.ctx.go("system")),)
        self.panel.show_unavailable(feature, st, command_hint("bench"), actions=actions)

    def _update_advisory(self) -> None:
        """Non-gating unavailable states (e.g. a missing dependency) as a banner."""
        if self.advisory is not None:
            self.advisory.hide()
            self.advisory.deleteLater()
            self.advisory = None
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(Feature.BENCH) if caps is not None else None
        if st is None or st.available:
            return
        msg = st.message
        text = render(msg) if msg is not None else ""
        head, _, body = text.partition("\n")
        self.advisory = Banner(
            "degraded",
            head or self.tr("The benchmark may not work on this system."),
            body.strip(),
            st.code.code if st.code is not None else None,
            st.commands,
            parent=self.panel.content,
        )
        self.advisory_box.addWidget(self.advisory)

    # ------------------------------------------------------------------ form
    def _update_session_radio(self) -> None:
        facts = self.ctx.current_session_facts()
        if facts is None:
            self.session_radio.setText(self.tr("Current session (none playing)"))
            self.session_radio.setEnabled(False)
            if self.session_radio.isChecked():
                self.custom_radio.setChecked(True)
        else:
            self.session_radio.setText(
                self.tr("Current &session ({w}×{h} @ {fps})").format(
                    w=facts.width, h=facts.height, fps=fmt_rate(facts.fps)
                )
            )
            self.session_radio.setEnabled(True)
        self.session_radio.setAccessibleName(self.session_radio.text().replace("&", ""))
        self._update_source_enabled()

    def _update_source_enabled(self, *_args: object) -> None:
        custom = not self.session_radio.isChecked()
        for w in (self.width_spin, self.height_spin, self.rate_edit):
            w.setEnabled(custom)
        self._update_hint()

    def _custom_edited(self, *_args: object) -> None:
        self.form_error.setVisible(False)

    def _default_choose_file(self) -> Path | None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Choose a video to test"),
            "",
            self.tr("Video files (*.mkv *.mp4 *.webm *.mov *.avi *.m2ts *.ts);;All files (*)"),
        )
        return Path(path) if path else None

    def _on_file_toggled(self, on: bool) -> None:
        if on and self._user_file is None:
            chosen = self.choose_file()
            if chosen is None:
                self.file_check.blockSignals(True)
                self.file_check.setChecked(False)
                self.file_check.blockSignals(False)
                return
            self._user_file = chosen
        if not on:
            self._user_file = None
        self.file_label.setText(
            self.tr("Also measured on: {path}").format(path=str(self._user_file))
            if self._user_file is not None
            else ""
        )
        self.file_label.setVisible(self._user_file is not None)
        self._update_hint()

    def _update_hint(self, *_args: object) -> None:
        params: dict[str, str] = {}
        if self.full_check.isChecked():
            params["full"] = "1"
        if self._user_file is not None:
            params["file"] = str(self._user_file)
        try:
            self.hint.set_hint(command_hint("bench", **params))
        except KeyError:
            self.hint.set_hint(None)

    def request(self) -> BenchRequest | None:
        """The request the form describes, or ``None`` (with the reason shown)."""
        facts: SourceFacts | None = None
        if self.session_radio.isChecked():
            facts = self.ctx.current_session_facts()
        if facts is not None:
            width, height, rate = facts.width, facts.height, facts.fps
        else:
            width, height = self.width_spin.value(), self.height_spin.value()
            rate_v = self.rate_edit.value()
            if rate_v is None:
                self._form_problem(self.tr("Enter the source frame rate, e.g. 24000/1001."))
                return None
            rate = rate_v
        target = self.target_edit.value()
        if self.target_edit.text().strip() and target is None:
            self._form_problem(self.tr("The target rate isn't a valid frame rate."))
            return None
        if target is not None and target <= rate:
            self._form_problem(self.tr("The target rate must be higher than the source rate."))
            return None
        # Interpolation runs on 4:2:0 video: sizes must be even.
        width -= width % 2
        height -= height % 2
        self.form_error.setVisible(False)
        return BenchRequest(
            width=width,
            height=height,
            source_fps=rate,
            target_fps=target,
            full=self.full_check.isChecked(),
            user_file=self._user_file,
        )

    def _form_problem(self, text: str) -> None:
        self.form_error.setText(text)
        self.form_error.setVisible(True)
        announce(self.form_error, text, assertive=True)

    def _load_candidates(self) -> None:
        caps = self.ctx.bridge.capabilities
        if caps is None or not caps.ok(Feature.DOCTOR):
            self.candidates_label.setText("")
            self.candidates_label.setVisible(False)
            return
        self.ctx.bridge.call(
            lambda core: core.last_report(),
            owner=self,
            ok=self._on_last_report,
            err=self._quiet,
        )

    def _on_last_report(self, report: DoctorReport | None) -> None:
        if report is None:
            self.candidates_label.setText(
                self.tr("Run the checks on the System page to see the top candidates.")
            )
            self.candidates_label.setVisible(True)
            return
        self.ctx.bridge.call(
            lambda core: core.select_backend(report),
            owner=self,
            ok=self._on_selection,
            err=self._quiet,
        )

    def _on_selection(self, sel: Selection) -> None:
        top = sel.ranked[:2]
        if not top:
            self.candidates_label.setText(
                self.tr("No interpolation engine is available: {reason}").format(
                    reason=render(sel.reason)
                )
            )
        else:
            names = []
            for b in top:
                name = backend_name(b)
                if b is sel.backend and sel.model:
                    name = f"{name} {sel.model}"
                names.append(name)
            self.candidates_label.setText(
                self.tr("Top candidates: {names}. Every installed model is measured.").format(
                    names=", ".join(names)
                )
            )
        self.candidates_label.setVisible(True)

    def _quiet(self, err: ButterEyeError) -> None:
        _log.info("bench page: optional call failed: %s", err.code.code)
        self.candidates_label.setVisible(False)

    # ------------------------------------------------------------------ history / results
    def _on_history(self, results: tuple[BenchResult, ...]) -> None:
        self._loaded = True
        keep = self._shown
        self._history = list(results)
        if keep is not None and keep not in self._history:
            self._history.append(keep)
        self._rebuild_history(select=keep if keep is not None else None)
        self.panel.show_content()

    def _on_history_error(self, err: ButterEyeError) -> None:
        self._loaded = True
        self.panel.show_error(err, actions=((self.tr("&Retry"), self.refresh),))

    def _rebuild_history(self, select: BenchResult | None) -> None:
        self.history_combo.blockSignals(True)
        self.history_combo.clear()
        # newest first
        for i in reversed(range(len(self._history))):
            r = self._history[i]
            self.history_combo.addItem(f"{when_text(r.when)} — {request_text(r.request)}", i)
        self.history_combo.blockSignals(False)
        if not self._history:
            self._shown = None
            self.results_panel.show_empty(self.empty_text())
            self.apply_button.setEnabled(False)
            return
        target = select if select is not None else self._history[-1]
        idx = self._history.index(target)
        combo_index = len(self._history) - 1 - idx
        self.history_combo.setCurrentIndex(combo_index)
        self.show_result(self._history[idx])

    def _on_history_index(self, combo_index: int) -> None:
        if combo_index < 0:
            return
        i = self.history_combo.itemData(combo_index)
        if isinstance(i, int) and 0 <= i < len(self._history):
            self.show_result(self._history[i])

    def shown_result(self) -> BenchResult | None:
        return self._shown

    def show_result(self, result: BenchResult) -> None:
        self._shown = result
        rec = recommended_measurement(result)
        self.result_title.setText(
            self.tr("Measured {when}: {what}").format(
                when=when_text(result.when), what=request_text(result.request)
            )
        )
        self.model.removeRows(0, self.model.rowCount())
        for m in result.measurements:
            items: list[QStandardItem] = []
            for text, raw in row_values(m, rec is not None and m.label == rec.label):
                item = QStandardItem(text)
                item.setEditable(False)
                item.setData(raw, VALUE_ROLE)
                items.append(item)
            self.model.appendRow(items)
        target = float(target_of(result.request))
        self.plot.set_data(result.measurements, target)
        self._update_faults(result)
        self.apply_status.setVisible(False)
        if rec is not None:
            rt = self.tr("real time") if rec.realtime else self.tr("not real time")
            text = self.tr("Recommended: {label} — {fps} fps in mpv, {rt}.").format(
                label=rec.label, fps=fmt_fps(rec.mpv_fps), rt=rt
            )
            self.plot.setAccessibleDescription(
                self.tr("Best: {label}, {fps} fps in mpv, {rt}").format(
                    label=rec.label, fps=f"{rec.mpv_fps:.0f}", rt=rt
                )
            )
            self.apply_hint.set_hint(command_hint("bench.apply", label=rec.label))
        else:
            text = self.tr(
                "Nothing is recommended: no configuration held real time in mpv without GPU faults."
            )
            self.plot.setAccessibleDescription(text)
            self.apply_hint.set_hint(None)
        self.recommend_label.setText(text)
        self.apply_button.setEnabled(rec is not None and self._ticket is None)
        self.results_panel.show_content()

    def _update_faults(self, result: BenchResult) -> None:
        for b in self.fault_banners:
            b.hide()
            b.deleteLater()
        self.fault_banners = []
        faulty = [m.label for m in result.measurements if has_faults(m)]
        unknown = [m.label for m in result.measurements if m.gpu_faults is None]
        if faulty:
            self.fault_banners.append(
                Banner(
                    "blocking",
                    self.tr("The GPU reported a fault while measuring: {labels}.").format(
                        labels=", ".join(faulty)
                    ),
                    self.tr(
                        "These configurations are never recommended; the fault may repeat "
                        "during playback. Press F5 on the System page to see the kernel log "
                        "lines."
                    ),
                    ErrorCode.RIFE_GPU_FAULT.code,
                    actions=((self.tr("Open S&ystem"), lambda: self.ctx.go("system")),),
                    parent=self.results_panel.content,
                )
            )
        if unknown:
            self.fault_banners.append(
                Banner(
                    "info",
                    self.tr("GPU faults couldn't be checked for: {labels}.").format(
                        labels=", ".join(unknown)
                    ),
                    self.tr("The kernel log isn't readable, so faults may have gone unnoticed."),
                    ErrorCode.JOURNAL_UNREADABLE.code,
                    parent=self.results_panel.content,
                )
            )
        for b in self.fault_banners:
            self.fault_box.addWidget(b)

    # ------------------------------------------------------------------ running
    def is_running(self) -> bool:
        return self._ticket is not None

    def run(self) -> None:
        if self._ticket is not None:
            return
        req = self.request()
        if req is None:
            return
        self._clear_run_state()
        self._run_started = time.monotonic()
        self.running_box.setVisible(True)
        self.overall = OpRow(self.tr("Benchmark: {what}").format(what=request_text(req)), None)
        self.running_lay.addWidget(self.overall)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.apply_button.setEnabled(False)
        self.ctx.announce(self.tr("Benchmark started."))
        self._ticket = self.ctx.bridge.run_op(
            lambda core: core.bench(req),
            owner=self,
            ok=self._on_result,
            err=self._on_run_error,
            progress=self._on_progress,
        )

    def cancel(self) -> None:
        if self._ticket is None:
            return
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(self.tr("Cancelling…"))
        self._ticket.cancel()
        # Ticket.cancel() drops the reply: finish here.
        self._finish_rows(ok=False, cancelled=True)
        self._end_run()
        self.ctx.announce(self.tr("Benchmark cancelled. Nothing was saved."))
        self.ctx.status(self.tr("Benchmark cancelled. Nothing was saved."))

    def op_rows(self) -> dict[str, OpRow]:
        return dict(self._rows)

    def _clear_run_state(self) -> None:
        while self.running_lay.count():
            item = self.running_lay.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.hide()
                w.deleteLater()
        self._rows = {}
        self._current_config = None
        self.overall = None
        self._notes = []
        self.notes.setVisible(False)
        if self.run_error is not None:
            self.run_error.hide()
            self.run_error.deleteLater()
            self.run_error = None

    @staticmethod
    def config_of(p: Progress) -> str | None:
        """The configuration a progress phase is about (``{config}`` param or prefix)."""
        cfg = p.phase.params.get("config")
        if isinstance(cfg, str) and cfg:
            return cfg
        head, sep, _ = p.phase.key.partition(" — ")
        return head if sep and "{" not in head else None

    def _on_progress(self, p: Progress) -> None:
        if self.overall is not None:
            self.overall.update(p)
        cfg = self.config_of(p)
        if cfg is None:
            return
        if cfg != self._current_config:
            if self._current_config is not None:
                prev = self._rows.get(self._current_config)
                if prev is not None and not prev.is_finished():
                    prev.finish(True, self.tr("Measured"))
            self._current_config = cfg
            row = self._rows.get(cfg)
            if row is None:
                row = OpRow(cfg, None)
                self._rows[cfg] = row
                self.running_lay.addWidget(row)
        row = self._rows[cfg]
        if not row.is_finished():
            phase = self.phase_without_config(p.phase, cfg)
            row.update(Progress(p.op_id, phase, None, None, p.unit, p.rate, None, p.detail))

    @staticmethod
    def phase_without_config(phase: Msg, cfg: str) -> Msg:
        """``"{config} — run 2 of 3 — in mpv"`` -> ``"run 2 of 3 — in mpv"``: the row's
        own label already names the configuration, so it is not repeated under it."""
        for prefix in ("{config} — ", f"{cfg} — "):
            if phase.key.startswith(prefix) and len(phase.key) > len(prefix):
                return Msg(phase.key[len(prefix) :], phase.params)
        return phase

    def _finish_rows(self, *, ok: bool, cancelled: bool = False) -> None:
        text = (
            self.tr("Cancelled")
            if cancelled
            else (self.tr("Measured") if ok else self.tr("Not finished"))
        )
        for row in self._rows.values():
            if not row.is_finished():
                row.finish(ok, text, cancelled=cancelled)
        if self.overall is not None and not self.overall.is_finished():
            self.overall.finish(
                ok,
                self.tr("Cancelled") if cancelled else self.tr("Done") if ok else self.tr("Failed"),
                cancelled=cancelled,
            )

    def _end_run(self) -> None:
        self._ticket = None
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText(self.tr("Ca&ncel"))
        self.apply_button.setEnabled(
            self._shown is not None and recommended_measurement(self._shown) is not None
        )

    def _collapse_running(self) -> None:
        """After a successful run, fold the finished per-configuration rows into the
        overall row's one-line summary so the results are not pushed below the fold."""
        n = len(self._rows)
        for row in self._rows.values():
            row.setVisible(False)
        if self.overall is not None:
            secs = max(0, round(time.monotonic() - self._run_started))
            took = f"{secs // 60}:{secs % 60:02d}"
            summary = (
                self.tr("Measured 1 configuration in {time}").format(time=took)
                if n == 1
                else self.tr("Measured {n} configurations in {time}").format(n=n, time=took)
            )
            self.overall.finish(True, summary)
        QTimer.singleShot(0, self._show_results)

    def _show_results(self) -> None:
        """Scroll the results into view and move focus to the results table."""
        w = self.parentWidget()
        while w is not None and not isinstance(w, QScrollArea):
            w = w.parentWidget()
        if isinstance(w, QScrollArea):
            w.ensureWidgetVisible(self.results_box, 0, 0)
        if self.table.isVisibleTo(self):
            self.table.setFocus(Qt.FocusReason.OtherFocusReason)

    def _on_result(self, result: BenchResult) -> None:
        if self.overall is not None and not self.overall.is_finished():
            for row in self._rows.values():
                if not row.is_finished():
                    row.finish(True, self.tr("Measured"))
            self._collapse_running()
        self._finish_rows(ok=True)
        self._ticket = None
        if result not in self._history:
            self._history.append(result)
        self._rebuild_history(select=result)
        self._end_run()
        rec = recommended_measurement(result)
        if rec is not None:
            msg = self.tr("Benchmark finished. Recommended: {label}.").format(label=rec.label)
        else:
            msg = self.tr("Benchmark finished. Nothing is recommended.")
        self.ctx.announce(msg)

    def _on_run_error(self, err: ButterEyeError) -> None:
        cancelled = isinstance(err, OperationCancelled)
        self._finish_rows(ok=False, cancelled=cancelled)
        self._end_run()
        if cancelled:
            self.ctx.announce(self.tr("Benchmark cancelled. Nothing was saved."))
            return
        if self.run_error is not None:
            self.run_error.hide()
            self.run_error.deleteLater()
        self.run_error = Banner.from_error(err, parent=self.panel.content)
        self.error_box.addWidget(self.run_error)
        self.ctx.announce(
            self.tr("Benchmark failed: {cause}").format(cause=render(err.cause)), assertive=True
        )

    # ------------------------------------------------------------------ apply
    def apply_recommended(self) -> None:
        result = self._shown
        rec = recommended_measurement(result) if result is not None else None
        if result is None or rec is None:
            return
        self.apply_button.setEnabled(False)
        label = rec.label

        def loaded(load: ConfigLoad) -> None:
            self.ctx.bridge.call(
                lambda core: core.apply_bench(result, label, expected_revision=load.revision),
                owner=self,
                ok=lambda _rev: self._applied(result, label),
                err=self._apply_failed,
            )

        self.ctx.bridge.call(
            lambda core: core.load_config(), owner=self, ok=loaded, err=self._apply_failed
        )

    def _applied(self, result: BenchResult, label: str) -> None:
        req = result.request
        text = self.tr("Saved: {label} is now used for videos up to {w}×{h} @ {fps}.").format(
            label=label, w=req.width, h=req.height, fps=fmt_rate(req.source_fps)
        )
        self.apply_status.setText(text)
        self.apply_status.setVisible(True)
        self.apply_button.setEnabled(True)
        self.ctx.announce(text)

    def _apply_failed(self, err: ButterEyeError) -> None:
        text = f"{render(err.cause)} ({err.code.code})"
        if err.fix is not None:
            text = f"{text} {render(err.fix)}"
        self.apply_status.setText(self.tr("Not saved: {why}").format(why=text))
        self.apply_status.setVisible(True)
        self.apply_button.setEnabled(True)
        self.ctx.announce(self.tr("Not saved: {why}").format(why=render(err.cause)), assertive=True)


__all__ = ["BenchPage", "COLUMNS", "VALUE_ROLE", "recommended_measurement", "row_values"]
