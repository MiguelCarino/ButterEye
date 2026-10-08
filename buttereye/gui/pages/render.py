# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Render queue page (docs/design/GUI.md §4.5; SCOPE §7, F16).

A sequential queue (one job at a time, run by the core). Columns: Source,
Output, Profile, Status (glyph + word), Progress ("42% · 61.3 fps", busy while
the total is unknown, never backwards), Phase, ETA, Encoder. Keys: Ctrl+N add,
Del cancel (confirm) or remove a finished job, Alt+Up/Alt+Down reorder queued
jobs, Ctrl+L open the job's folder (logs). There is deliberately no Pause and no
"stop and keep" (SCOPE §7.6 deferred).

When this build has no render engine, or ffms2 is missing, the page shows only
the unavailable panel (BE-9001, or BE-4001 + ``sudo dnf install ffms2``).
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QSize,
    Qt,
    QUrl,
)
from PySide6.QtGui import (
    QDesktopServices,
    QHideEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QShortcut,
    QShowEvent,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionProgressBar,
    QStyleOptionViewItem,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEye,
    ButterEyeError,
    CapabilitiesChanged,
    CapState,
    CommandHint,
    ConfigChanged,
    ConfigLoad,
    Event,
    EventsDropped,
    Feature,
    JobChanged,
    JobId,
    OpState,
    Paths,
    Reason,
    RenderJobState,
    RenderPhase,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SessionId,
    SessionSnapshot,
    StaleJob,
    command_hint,
    render,
)
from buttereye.gui.a11y import heading, message_box
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.render_job import RenderJobDialog
from buttereye.gui.pages.base import GATING_REASONS, Page
from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.op_row import format_duration
from buttereye.gui.widgets.state_panel import StatePanel
from buttereye.gui.widgets.status_badge import BadgeKind, badge_pixmap

_log = logging.getLogger(__name__)

CTX = "RenderPage"

#: RENDER states that replace the page with the unavailable panel (§7 U9).
PAGE_GATES = GATING_REASONS | {Reason.MISSING_DEPENDENCY, Reason.BLOCKED_BY_DOCTOR}

COLUMNS = ("Source", "Output", "Profile", "Status", "Progress", "Phase", "ETA", "Encoder")
COL_SOURCE, COL_OUTPUT, COL_PROFILE, COL_STATUS, COL_PROGRESS, COL_PHASE, COL_ETA, COL_ENCODER = (
    range(len(COLUMNS))
)

#: Progress cell: permille 0..1000, or -1 for indeterminate (busy), None = no bar.
PROGRESS_ROLE = Qt.ItemDataRole.UserRole + 1
JOB_ROLE = Qt.ItemDataRole.UserRole + 2
SCALE = 1000

FINISHED = frozenset({OpState.SUCCEEDED, OpState.FAILED, OpState.CANCELLED})
ACTIVE = frozenset({OpState.QUEUED, OpState.RUNNING, OpState.CANCELLING})

Confirm = Callable[[QWidget, str, str, str, str], bool]


def _t(text: str) -> str:
    return QCoreApplication.translate(CTX, text)


def status_of(job: RenderJobState) -> tuple[BadgeKind, str]:
    if job.state is OpState.QUEUED:
        return "info", _t("Queued")
    if job.state is OpState.RUNNING:
        return "busy", _t("Rendering")
    if job.state is OpState.CANCELLING:
        return "busy", _t("Cancelling…")
    if job.state is OpState.SUCCEEDED:
        return "ok", _t("Finished")
    if job.state is OpState.CANCELLED:
        return "off", _t("Cancelled — partial files removed.")
    code = job.error.code.code if job.error is not None else ""
    return "blocking", _t("Failed ({code})").format(code=code) if code else _t("Failed")


def phase_text(phase: RenderPhase | None) -> str:
    if phase is None:
        return ""
    return {
        RenderPhase.PROBE: _t("Checking the source"),
        RenderPhase.RENDER: _t("Rendering"),
        RenderPhase.REMUX: _t("Remuxing"),
        RenderPhase.CLEANUP: _t("Cleaning up"),
    }[phase]


def confirm_box(parent: QWidget, title: str, text: str, yes: str, no: str) -> bool:
    box = message_box(parent)
    box.setObjectName("renderConfirm")
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes_b = box.addButton(yes, QMessageBox.ButtonRole.DestructiveRole)
    yes_b.setObjectName("confirm")
    no_b = box.addButton(no, QMessageBox.ButtonRole.RejectRole)
    no_b.setObjectName("keep")
    box.setDefaultButton(no_b)
    box.setEscapeButton(no_b)
    box.exec()
    return box.clickedButton() is yes_b


class ProgressDelegate(QStyledItemDelegate):
    """Paints the Progress column as a progress bar; the cell text stays the model's
    display text, so screen readers read "42% · 61.3 fps"."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        value = index.data(PROGRESS_ROLE)
        if not isinstance(value, int):
            super().paint(painter, option, index)
            return
        bar = QStyleOptionProgressBar()
        bar.rect = option.rect.adjusted(2, 2, -2, -2)
        bar.palette = option.palette
        bar.state = option.state
        bar.fontMetrics = option.fontMetrics
        bar.minimum = 0
        bar.maximum = 0 if value < 0 else SCALE
        bar.progress = max(0, value)
        bar.text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        bar.textVisible = True
        bar.textAlignment = Qt.AlignmentFlag.AlignCenter
        style = option.widget.style() if option.widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ProgressBar, bar, painter, option.widget)


class RenderPage(Page):
    page_id: ClassVar[str] = "render"
    features: ClassVar[tuple[Feature, ...]] = (Feature.RENDER, Feature.RENDER_HDR10)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(self.tr("Render"))
        self._jobs: dict[JobId, RenderJobState] = {}
        self._permille: dict[JobId, int] = {}
        self._sessions: set[SessionId] = set()
        self._stale: dict[JobId, Banner] = {}
        self._profile_names: dict[str, str] = {}
        self._paths: Paths | None = None
        self._loaded = False
        self._gated = False
        self.dialog: RenderJobDialog | None = None
        self.confirm: Confirm = confirm_box
        self.open_url: Callable[[QUrl], object] = QDesktopServices.openUrl

        self.panel = StatePanel(self)
        outer = QVBoxLayout(self)
        outer.addWidget(self.panel)
        root = QVBoxLayout(self.panel.content)

        title = heading(QLabel(self.tr("Render"), self.panel.content))
        title.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(title)
        intro = QLabel(
            self.tr(
                "Render a video with smooth motion to a new Matroska file. One render runs at "
                "a time; all audio, subtitles, chapters and attachments are kept."
            ),
            self.panel.content,
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        root.addWidget(intro)

        self.playback_notice = QLabel(
            self.tr("Playback is running: renders use lower priority."), self.panel.content
        )
        self.playback_notice.setWordWrap(True)
        self.playback_notice.setVisible(False)
        root.addWidget(self.playback_notice)

        self.stale_box = QVBoxLayout()
        root.addLayout(self.stale_box)

        bar = QHBoxLayout()
        self.add_button = self._button(self.tr("&Add…"), _t("Add a render"), lambda: self._add())
        self.remove_button = self._button(
            self.tr("&Cancel render"), _t("Cancel render"), self.cancel_or_forget
        )
        self.up_button = self._button(self.tr("Move &up"), _t("Move up"), lambda: self.move_job(-1))
        self.down_button = self._button(
            self.tr("Move &down"), _t("Move down"), lambda: self.move_job(1)
        )
        self.log_button = self._button(self.tr("Open &log"), _t("Open job log"), self.open_log)
        self.folder_button = self._button(
            self.tr("Show in f&older"), _t("Show in folder"), self.show_in_folder
        )
        for b in (
            self.add_button,
            self.remove_button,
            self.up_button,
            self.down_button,
            self.log_button,
            self.folder_button,
        ):
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        self.queue_panel = StatePanel(self.panel.content)
        root.addWidget(self.queue_panel, 1)
        ql = QVBoxLayout(self.queue_panel.content)
        ql.setContentsMargins(0, 0, 0, 0)
        self.model = QStandardItemModel(0, len(COLUMNS), self)
        for i, h in enumerate(COLUMNS):
            self.model.setHeaderData(i, Qt.Orientation.Horizontal, _t(h))
        self.table = QTableView(self.queue_panel.content)
        self.table.setModel(self.model)
        self.table.setAccessibleName(self.tr("Render queue"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setTabKeyNavigation(False)
        # Row numbers (queue position); never collapses to zero width when empty.
        vh = self.table.verticalHeader()
        vh.setMinimumWidth(vh.fontMetrics().horizontalAdvance("000"))
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self.table))
        self.table.selectionModel().selectionChanged.connect(self._update_buttons)
        ql.addWidget(self.table)

        self.detail_box = QVBoxLayout()
        root.addLayout(self.detail_box)
        self.detail_banner: Banner | None = None

        self.hint = CliHint(None, self.panel.content)
        root.addWidget(self.hint)
        root.addStretch(1)

        def key(
            seq: QKeySequence | str,
            fn: Callable[[], None],
            context: Qt.ShortcutContext = Qt.ShortcutContext.WidgetWithChildrenShortcut,
        ) -> QShortcut:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(context)
            sc.activated.connect(fn)
            return sc

        # Ctrl+N / Ctrl+L work wherever focus is in the window while this page is
        # current (e.g. still in the sidebar after Ctrl+4), as the empty-queue text
        # promises; they are switched off while the page is hidden (showEvent /
        # hideEvent). Delete and Alt+Up/Down stay page-scoped so Delete pressed in
        # the sidebar can never cancel a job.
        window = Qt.ShortcutContext.WindowShortcut
        self.shortcuts = {
            "add": key("Ctrl+N", self._add, window),
            "delete": key(QKeySequence(Qt.Key.Key_Delete), self.cancel_or_forget),
            "up": key("Alt+Up", lambda: self.move_job(-1)),
            "down": key("Alt+Down", lambda: self.move_job(1)),
            "log": key("Ctrl+L", self.open_log, window),
        }
        self._window_keys = (self.shortcuts["add"], self.shortcuts["log"])
        for sc in self._window_keys:
            sc.setEnabled(False)

        self.queue_panel.show_empty(self.empty_text())
        self.panel.show_loading(self.tr("Loading the render queue…"))
        self._update_buttons()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        for sc in self._window_keys:
            sc.setEnabled(True)
        super().showEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:  # noqa: N802 - Qt API
        for sc in self._window_keys:
            sc.setEnabled(False)
        super().hideEvent(event)

    def _button(self, text: str, name: str, fn: Callable[[], None]) -> QPushButton:
        b = QPushButton(text, self.panel.content)
        b.setAccessibleName(name)
        b.setAutoDefault(False)
        b.clicked.connect(lambda _checked=False: fn())
        return b

    def empty_text(self) -> str:
        return self.tr("Nothing in the queue. Press Add (Ctrl+N) to render a video.")

    # ------------------------------------------------------------------ Page surface
    def state_panel(self) -> StatePanel:
        return self.panel

    def refresh(self) -> None:
        gate = self._gate()
        if gate is not None:
            self._show_gate(*gate)
            return
        self._gated = False
        if not self._loaded:
            self.panel.show_loading(self.tr("Loading the render queue…"))
        bridge = self.ctx.bridge
        bridge.call(
            lambda core: core.render_jobs(), owner=self, ok=self._on_jobs, err=self._on_jobs_error
        )
        bridge.call(lambda core: core.stale_jobs(), owner=self, ok=self._on_stale, err=self._quiet)
        bridge.call(lambda core: core.sessions(), owner=self, ok=self._on_sessions, err=self._quiet)
        bridge.call(_get_paths, owner=self, ok=self._on_paths, err=self._quiet)
        caps = bridge.capabilities
        if (
            caps is not None
            and caps.states.get(Feature.CONFIG) is not None
            and caps.ok(Feature.CONFIG)
        ):
            bridge.call(
                lambda core: core.load_config(), owner=self, ok=self._on_config, err=self._quiet
            )

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, CapabilitiesChanged):
            was = self._gated
            gate = self._gate()
            if gate is not None:
                self._show_gate(*gate)
            elif was:
                self.refresh()
        elif isinstance(ev, JobChanged):
            self._job_changed(ev.job)
        elif isinstance(ev, SessionAdded | SessionChanged):
            snap = ev.snapshot
            if snap.ended:
                self._sessions.discard(snap.sid)
            else:
                self._sessions.add(snap.sid)
            self._update_notice()
        elif isinstance(ev, SessionEnded):
            self._sessions.discard(ev.sid)
            self._update_notice()
        elif isinstance(ev, EventsDropped):
            if self._loaded and not self._gated:
                self.refresh()
        elif isinstance(ev, ConfigChanged):
            if self._loaded and not self._gated:
                self.ctx.bridge.call(
                    lambda core: core.load_config(),
                    owner=self,
                    ok=self._on_config,
                    err=self._quiet,
                )

    def cli_hint(self) -> CommandHint | None:
        return self.hint.hint()

    # ------------------------------------------------------------------ gating
    def _gate(self) -> tuple[Feature, CapState] | None:
        caps = self.ctx.bridge.capabilities
        if caps is None:
            return None
        st = caps.states.get(Feature.RENDER)
        if st is not None and not st.available and st.reason in PAGE_GATES:
            return Feature.RENDER, st
        return None

    def _show_gate(self, feature: Feature, st: CapState) -> None:
        self._gated = True
        actions: tuple[Action, ...] = ()
        if st.reason is Reason.BLOCKED_BY_DOCTOR:
            actions = ((self.tr("Open &System"), lambda: self.ctx.go("system")),)
        hint = command_hint(
            "render", src="VIDEO", out="OUTPUT.mkv", profile="balanced", encoder="ENCODER"
        )
        self.panel.show_unavailable(feature, st, hint, actions=actions)

    def _quiet(self, err: ButterEyeError) -> None:
        _log.info("render page: optional call failed: %s", err.code.code)

    # ------------------------------------------------------------------ data
    def jobs(self) -> tuple[RenderJobState, ...]:
        return tuple(self._jobs.values())

    def _on_jobs(self, jobs: tuple[RenderJobState, ...]) -> None:
        self._loaded = True
        self._jobs = {j.id: j for j in jobs}
        for j in jobs:
            self._track_progress(j)
        self._rebuild()
        self.panel.show_content()

    def _on_jobs_error(self, err: ButterEyeError) -> None:
        self._loaded = True
        self.panel.show_error(err, actions=((self.tr("&Retry"), self.refresh),))

    def _on_paths(self, paths: Paths) -> None:
        self._paths = paths
        self._update_buttons()

    def _on_config(self, load: ConfigLoad) -> None:
        self._profile_names = {p.id: p.name for p in load.config.profiles}
        self._rebuild()

    def _on_sessions(self, snaps: tuple[SessionSnapshot, ...]) -> None:
        self._sessions = {s.sid for s in snaps if not s.ended}
        self._update_notice()

    def _update_notice(self) -> None:
        self.playback_notice.setVisible(bool(self._sessions))

    def _job_changed(self, job: RenderJobState) -> None:
        old = self._jobs.get(job.id)
        self._jobs[job.id] = job
        self._track_progress(job)
        if old is None or old.state is not job.state:
            self._announce_transition(job)
        if self._loaded and not self._gated:
            self._rebuild()

    def _track_progress(self, job: RenderJobState) -> None:
        """Progress permille, never backwards per job; SUCCEEDED is 100 %."""
        prev = self._permille.get(job.id, -1)
        if job.state is OpState.SUCCEEDED:
            value = SCALE
        elif job.done_frames is not None and job.total_frames:
            value = min(SCALE, max(0, job.done_frames * SCALE // job.total_frames))
        else:
            value = -1
        self._permille[job.id] = max(prev, value)

    def progress_of(self, jid: JobId) -> int:
        """Permille shown for ``jid`` (-1 = not started / unknown)."""
        return self._permille.get(jid, -1)

    def _announce_transition(self, job: RenderJobState) -> None:
        name = job.spec.source.name
        if job.state is OpState.RUNNING:
            self.ctx.announce(self.tr("Rendering {name}.").format(name=name))
        elif job.state is OpState.SUCCEEDED:
            self.ctx.announce(self.tr("Render finished: {name}.").format(name=name))
        elif job.state is OpState.FAILED:
            cause = render(job.error.cause) if job.error is not None else ""
            self.ctx.announce(
                self.tr("Render failed: {name}. {cause}").format(name=name, cause=cause),
                assertive=True,
            )
        elif job.state is OpState.CANCELLED:
            self.ctx.announce(
                self.tr("Render cancelled: {name}. Partial files removed.").format(name=name)
            )

    def progress_text(self, job: RenderJobState) -> str:
        value = self._permille.get(job.id, -1)
        if value < 0:
            if job.state is OpState.RUNNING:
                return self.tr("Working…")
            return ""
        pct = value // 10
        if job.fps is not None and job.state is OpState.RUNNING:
            return self.tr("{pct}% · {fps} fps").format(pct=pct, fps=f"{job.fps:.1f}")
        return self.tr("{pct}%").format(pct=pct)

    def _rebuild(self) -> None:
        selected = self.selected_job()
        sel_id = selected.id if selected is not None else None
        self.model.removeRows(0, self.model.rowCount())
        size = max(8, self.fontMetrics().height())
        dpr = self.devicePixelRatioF()
        for job in self._jobs.values():
            kind, word = status_of(job)
            eta = (
                format_duration(job.eta_s)
                if job.eta_s is not None and job.state is OpState.RUNNING
                else ""
            )
            cells = [
                job.spec.source.name,
                job.spec.output.name,
                self._profile_names.get(job.spec.profile_id, job.spec.profile_id),
                word,
                self.progress_text(job),
                phase_text(job.phase) if job.state is OpState.RUNNING else "",
                eta,
                job.spec.encoder,
            ]
            items = []
            for col, text in enumerate(cells):
                item = QStandardItem(text)
                item.setEditable(False)
                item.setData(job.id, JOB_ROLE)
                if col == COL_SOURCE:
                    item.setToolTip(str(job.spec.source))
                elif col == COL_OUTPUT:
                    item.setToolTip(str(job.spec.output))
                elif col == COL_STATUS:
                    item.setIcon(QIcon(badge_pixmap(kind, self.palette(), size, dpr)))
                elif col == COL_PROGRESS:
                    permille = self._permille.get(job.id, -1)
                    if job.state is OpState.QUEUED and permille < 0:
                        item.setData(None, PROGRESS_ROLE)
                    elif job.state in FINISHED and permille < 0:
                        item.setData(None, PROGRESS_ROLE)
                    else:
                        item.setData(permille, PROGRESS_ROLE)
                items.append(item)
            self.model.appendRow(items)
        self.table.setIconSize(QSize(size, size))
        if self._jobs:
            self.queue_panel.show_content()
        else:
            self.queue_panel.show_empty(self.empty_text())
        if sel_id is not None:
            self.select_job(sel_id)
        self._update_buttons()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.PaletteChange and self._loaded:
            self._rebuild()
        super().changeEvent(event)

    # ------------------------------------------------------------------ selection
    def selected_job(self) -> RenderJobState | None:
        sm = self.table.selectionModel()
        if sm is None:
            return None
        rows = sm.selectedRows()
        if not rows:
            return None
        jid = rows[0].data(JOB_ROLE)
        return self._jobs.get(JobId(str(jid))) if jid is not None else None

    def select_job(self, jid: JobId) -> bool:
        for row in range(self.model.rowCount()):
            if self.model.item(row, 0).data(JOB_ROLE) == jid:
                self.table.selectRow(row)
                return True
        return False

    def _update_buttons(self, *_args: object) -> None:
        job = self.selected_job()
        finished = job is not None and job.state in FINISHED
        self.remove_button.setText(
            self.tr("&Remove from list") if finished else self.tr("&Cancel render")
        )
        self.remove_button.setAccessibleName(
            self.tr("Remove from list") if finished else self.tr("Cancel render")
        )
        self.remove_button.setEnabled(job is not None and job.state is not OpState.CANCELLING)
        queued = [j.id for j in self._jobs.values() if j.state is OpState.QUEUED]
        is_queued = job is not None and job.state is OpState.QUEUED
        self.up_button.setEnabled(is_queued and job is not None and queued.index(job.id) > 0)
        self.down_button.setEnabled(
            is_queued and job is not None and queued.index(job.id) < len(queued) - 1
        )
        self.log_button.setEnabled(job is not None and self._paths is not None)
        self.folder_button.setEnabled(job is not None and job.state is OpState.SUCCEEDED)
        self._update_detail(job)
        if job is not None:
            try:
                self.hint.set_hint(
                    command_hint(
                        "render",
                        src=str(job.spec.source),
                        out=str(job.spec.output),
                        profile=job.spec.profile_id,
                        encoder=job.spec.encoder,
                    )
                )
            except KeyError:
                self.hint.set_hint(None)
        else:
            self.hint.set_hint(None)

    def _update_detail(self, job: RenderJobState | None) -> None:
        if self.detail_banner is not None:
            self.detail_box.removeWidget(self.detail_banner)
            self.detail_banner.hide()
            self.detail_banner.deleteLater()
            self.detail_banner = None
        if job is None:
            return
        if job.state is OpState.FAILED and job.error is not None:
            self.detail_banner = Banner.from_error(job.error, parent=self.panel.content)
        elif job.state is OpState.CANCELLED:
            self.detail_banner = Banner(
                "off",
                self.tr("Cancelled — partial files removed."),
                parent=self.panel.content,
            )
        elif job.state is OpState.SUCCEEDED:
            self.detail_banner = Banner(
                "ok",
                self.tr("Finished: {path}").format(path=str(job.spec.output)),
                actions=((self.tr("Show in folder"), self.show_in_folder),),
                parent=self.panel.content,
            )
        if self.detail_banner is not None:
            self.detail_box.addWidget(self.detail_banner)

    # ------------------------------------------------------------------ actions
    def _add(self) -> None:
        self.add_job()

    def add_job(self, source: Path | None = None) -> RenderJobDialog | None:
        if self._gated:
            return None
        dlg = RenderJobDialog(self.ctx, self, source=source)
        dlg.enqueued.connect(self._on_enqueued)
        dlg.finished.connect(lambda _r: self._dialog_closed(dlg))
        self.dialog = dlg
        dlg.open()
        return dlg

    def _dialog_closed(self, dlg: RenderJobDialog) -> None:
        if self.dialog is dlg:
            self.dialog = None
        dlg.deleteLater()

    def _on_enqueued(self, jid: str) -> None:
        self.ctx.announce(self.tr("Added to the render queue."))
        # JobChanged usually arrived already; fetch the order anyway.
        self.ctx.bridge.call(
            lambda core: core.render_jobs(),
            owner=self,
            ok=lambda jobs: self._after_enqueue(JobId(jid), jobs),
            err=self.show_error,
        )

    def _after_enqueue(self, jid: JobId, jobs: tuple[RenderJobState, ...]) -> None:
        self._on_jobs(jobs)
        self.select_job(jid)

    def cancel_or_forget(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        name = job.spec.source.name
        if job.state in FINISHED:
            jid = job.id
            self.ctx.bridge.call(
                lambda core: core.render_forget(jid),
                owner=self,
                ok=lambda _none: self._forgotten(jid),
            )
            return
        if job.state is OpState.CANCELLING:
            return
        if not self.confirm(
            self,
            self.tr("Cancel the render?"),
            self.tr("Cancel the render of {name}? The partial file is deleted.").format(name=name),
            self.tr("&Cancel render"),
            self.tr("&Keep rendering") if job.state is OpState.RUNNING else self.tr("&Keep it"),
        ):
            return
        jid = job.id
        self.ctx.bridge.call(lambda core: core.render_cancel(jid), owner=self, ok=_ignore)

    def _forgotten(self, jid: JobId) -> None:
        self._jobs.pop(jid, None)
        self._permille.pop(jid, None)
        self._rebuild()
        self.ctx.status(self.tr("Removed from the list."))

    def move_job(self, delta: int) -> None:
        job = self.selected_job()
        if job is None or job.state is not OpState.QUEUED:
            return
        queued = [j.id for j in self._jobs.values() if j.state is OpState.QUEUED]
        i = queued.index(job.id)
        if not 0 <= i + delta < len(queued):
            return
        # The core's delta counts queue positions; skip non-queued rows in between.
        all_ids = list(self._jobs)
        steps = all_ids.index(queued[i + delta]) - all_ids.index(job.id)
        jid = job.id
        self.ctx.bridge.call(
            lambda core: core.render_move(jid, steps),
            owner=self,
            ok=lambda _none: self._moved(jid),
        )

    def _moved(self, jid: JobId) -> None:
        self.ctx.bridge.call(
            lambda core: core.render_jobs(),
            owner=self,
            ok=lambda jobs: self._after_enqueue(jid, jobs),
        )

    def job_dir(self, job: RenderJobState) -> Path | None:
        return self._paths.jobs_dir / job.id if self._paths is not None else None

    def open_log(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        d = self.job_dir(job)
        if d is None or not d.is_dir():
            self.ctx.status(self.tr("No log for this render yet."))
            return
        self.open_url(QUrl.fromLocalFile(str(d)))

    def show_in_folder(self) -> None:
        job = self.selected_job()
        if job is None or job.state is not OpState.SUCCEEDED:
            return
        self.open_url(QUrl.fromLocalFile(str(job.spec.output.parent)))

    # ------------------------------------------------------------------ stale jobs
    def stale_banners(self) -> dict[JobId, Banner]:
        return dict(self._stale)

    def _on_stale(self, stale: tuple[StaleJob, ...]) -> None:
        current = {s.id for s in stale}
        for jid in list(self._stale):
            if jid not in current:
                self._drop_stale(jid)
        for s in stale:
            if s.id in self._stale or (not s.alive and not s.partial_files):
                continue
            jid = s.id
            if s.alive:
                pid = str(s.pgid) if s.pgid is not None else self.tr("unknown")
                title = self.tr(
                    "A render from a previous session is still running (PID {pid})."
                ).format(pid=pid)
                actions: tuple[Action, ...] = (
                    (
                        self.tr("&Stop it and delete partial files"),
                        functools.partial(self.stop_stale, jid),
                    ),
                    (self.tr("Leave &it"), functools.partial(self._drop_stale, jid)),
                )
            else:
                title = self.tr("A render from a previous session left partial files.")
                actions = (
                    (self.tr("&Delete partial files"), functools.partial(self.stop_stale, jid)),
                    (self.tr("Leave &them"), functools.partial(self._drop_stale, jid)),
                )
            body = "\n".join(str(p) for p in s.partial_files)
            banner = Banner("degraded", title, body, actions=actions, parent=self.panel.content)
            self._stale[jid] = banner
            self.stale_box.addWidget(banner)
            self.ctx.announce(title)

    def stop_stale(self, jid: JobId) -> None:
        self.ctx.bridge.call(
            lambda core: core.stale_job_stop(jid),
            owner=self,
            ok=lambda _none: self._stale_stopped(jid),
        )

    def _stale_stopped(self, jid: JobId) -> None:
        self._drop_stale(jid)
        self.ctx.announce(self.tr("Stopped the old render and deleted its partial files."))

    def _drop_stale(self, jid: JobId) -> None:
        banner = self._stale.pop(jid, None)
        if banner is not None:
            self.stale_box.removeWidget(banner)
            banner.hide()
            banner.deleteLater()


async def _get_paths(core: ButterEye) -> Paths:
    return core.paths()


def _ignore(_value: object) -> None:
    return None


__all__ = ["COLUMNS", "PROGRESS_ROLE", "ProgressDelegate", "RenderPage", "status_of"]
