# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Attach dialog (docs/design/GUI.md §4.3; SCOPE §4.3).

Lists ``discover()``: "mpv — film.mkv (PID 4211)". Refused players stay visible
but disabled, with the refusal text. Rows with a leftover ButterEye filter offer
[Remove leftover ButterEye filter...] (confirmed first). Enter attaches, Delete
removes a leftover filter (confirmed), F5 scans again. The dialog scans only
when it opens and on F5 — never while hidden.

When this build cannot attach (``Feature.ATTACH`` gated) the dialog shows the
unavailable panel and nothing else: no scan, zero rows.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    CandidateId,
    CapState,
    Feature,
    InstanceCandidate,
    OperationCancelled,
    Reason,
    SessionId,
    command_hint,
    render,
)
from buttereye.gui.a11y import announce, labelled, message_box
from buttereye.gui.bridge import CoreBridge, Ticket
from buttereye.gui.dialogs.fit import FitDialog
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.state_panel import StatePanel

_GATING = frozenset({Reason.NOT_IMPLEMENTED, Reason.NOTHING_LISTED})
_CID_ROLE = Qt.ItemDataRole.UserRole


def gating_state(bridge: CoreBridge, feature: Feature) -> CapState | None:
    """The CapState when this build cannot provide ``feature`` at all, else None."""
    caps = bridge.capabilities
    if caps is None:
        return None
    st = caps.states.get(feature)
    if st is not None and not st.available and st.reason in _GATING:
        return st
    return None


class AttachDialog(FitDialog):
    """``AttachDialog(bridge, parent, select_pid=None)``; ``attached`` carries the SessionId."""

    attached = Signal(object)

    def __init__(
        self, bridge: CoreBridge, parent: QWidget | None = None, *, select_pid: int | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("attachDialog")
        self.setWindowTitle(self.tr("Attach to a running mpv"))
        self.bridge = bridge
        self._select_pid = select_pid
        self._candidates: dict[CandidateId, InstanceCandidate] = {}
        self._scan: Ticket | None = None
        self._busy = False
        self.attached_sid: SessionId | None = None

        lay = QVBoxLayout(self)
        self.panel = StatePanel(self)
        content = self.panel.content
        cl = QVBoxLayout(content)
        cl.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget(content)
        self.list.setObjectName("attachCandidates")
        list_label, _ = labelled(
            self.tr("&Running mpv players"),
            self.list,
            description=self.tr(
                "Enter attaches. Delete removes a leftover ButterEye filter. F5 scans again."
            ),
        )
        self.list.installEventFilter(self)
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.list.itemActivated.connect(lambda _item: self.attach_selected())
        cl.addWidget(list_label)
        cl.addWidget(self.list)
        self.detail = QLabel(content)
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        cl.addWidget(self.detail)
        row = QHBoxLayout()
        self.attach_button = QPushButton(self.tr("&Attach"), content)
        self.attach_button.setObjectName("attachGo")
        self.attach_button.setAccessibleName(self.tr("Attach"))
        self.attach_button.clicked.connect(self.attach_selected)
        self.remove_button = QPushButton(self.tr("&Remove leftover ButterEye filter…"), content)
        self.remove_button.setObjectName("attachRemoveOrphan")
        self.remove_button.setAccessibleName(self.tr("Remove leftover ButterEye filter"))
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self.remove_selected_orphan)
        self.rescan_button = QPushButton(self.tr("&Scan again"), content)
        self.rescan_button.setObjectName("attachRescan")
        self.rescan_button.setAccessibleName(self.tr("Scan again"))
        self.rescan_button.setAutoDefault(False)
        self.rescan_button.clicked.connect(self.rescan)
        row.addWidget(self.attach_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.rescan_button)
        row.addStretch(1)
        cl.addLayout(row)
        lay.addWidget(self.panel, 1)

        self._banner_lay = QVBoxLayout()
        lay.addLayout(self._banner_lay)
        self.banner: Banner | None = None

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = box.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setObjectName("attachClose")
            close.setAccessibleName(self.tr("Close"))
            close.setAutoDefault(False)
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        self._f5 = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        self._f5.activated.connect(self.rescan)

        self.rescan()

    # ------------------------------------------------------------------ API
    def candidates(self) -> tuple[InstanceCandidate, ...]:
        return tuple(self._candidates.values())

    def rescan(self) -> None:
        for feature in (Feature.ATTACH, Feature.DISCOVER):
            st = gating_state(self.bridge, feature)
            if st is not None:
                self._candidates = {}
                self.list.clear()
                self.panel.show_unavailable(feature, st, command_hint("attach"))
                return
        if self._scan is not None and self._scan.pending:
            return
        self.panel.show_loading(self.tr("Looking for running mpv players…"))
        self._scan = self.bridge.call(lambda core: core.discover(), owner=self, ok=self._on_found)

    def attach_selected(self) -> None:
        cand = self._selected()
        if cand is None or cand.refused is not None or self._busy:
            return
        if gating_state(self.bridge, Feature.ATTACH) is not None:
            return
        self._busy = True
        self._update_buttons()
        cid = cand.id
        self.bridge.call(lambda core: core.attach(cid), owner=self, ok=self._on_attached)

    def remove_selected_orphan(self) -> None:
        cand = self._selected()
        if cand is None or not cand.orphan_filter or self._busy:
            return
        box = message_box(self)
        box.setObjectName("orphanConfirm")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Remove leftover filter?"))
        box.setText(
            self.tr(
                "Remove the leftover ButterEye filter from {player}? It keeps playing "
                "without smoothing."
            ).format(player=self._row_text(cand))
        )
        yes = box.addButton(self.tr("&Remove filter"), QMessageBox.ButtonRole.AcceptRole)
        yes.setObjectName("orphanConfirmRemove")
        no = box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        no.setObjectName("orphanConfirmCancel")
        box.setDefaultButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._busy = True
        self._update_buttons()
        cid = cand.id
        self.bridge.call(
            lambda core: core.remove_orphan_filter(cid), owner=self, ok=self._on_orphan_removed
        )

    def show_error(self, err: ButterEyeError) -> None:
        self._busy = False
        self._clear_banner()
        if isinstance(err, OperationCancelled):
            return
        if self.panel.state == "loading":
            self.panel.show_error(err)
        else:
            self.banner = Banner.from_error(err, parent=self)
            self._banner_lay.addWidget(self.banner)
        self._update_buttons()
        announce(self, render(err.cause), assertive=True)

    # ------------------------------------------------------------- internals
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.list and event.type() == QEvent.Type.KeyPress:
            assert isinstance(event, QKeyEvent)
            if event.key() == Qt.Key.Key_Delete:
                self.remove_selected_orphan()
                return True
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.attach_selected()
                return True
        return super().eventFilter(watched, event)

    def _clear_banner(self) -> None:
        if self.banner is not None:
            self.banner.hide()
            self.banner.deleteLater()
            self.banner = None

    def _row_text(self, c: InstanceCandidate) -> str:
        name = c.title or c.socket.name
        if c.pid is not None:
            return self.tr("mpv — {title} (PID {pid})").format(title=name, pid=c.pid)
        return self.tr("mpv — {title}").format(title=name)

    def _item_text(self, c: InstanceCandidate) -> str:
        text = self._row_text(c)
        if c.refused is not None:
            why = render(c.refusal) if c.refusal is not None else self.tr("Refused")
            return f"{text} — {why}"
        notes: list[str] = []
        if c.managed_by_us:
            notes.append(self.tr("started by ButterEye"))
        if c.orphan_filter:
            notes.append(self.tr("leftover ButterEye filter"))
        return " — ".join([text, *notes])

    def _on_found(self, found: tuple[InstanceCandidate, ...]) -> None:
        self._candidates = {c.id: c for c in found}
        self.list.clear()
        if not found:
            self.panel.show_empty(
                self.tr(
                    "No running mpv players were found. ButterEye can connect to players "
                    "it started, or to an mpv started with --input-ipc-server."
                ),
                [(self.tr("&Scan again"), self.rescan)],
            )
            return
        select_row = -1
        first_ok = -1
        for c in found:
            item = QListWidgetItem(self._item_text(c))
            item.setData(_CID_ROLE, str(c.id))
            if c.refused is not None:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(item)
            row = self.list.count() - 1
            if c.refused is None and first_ok < 0:
                first_ok = row
            if self._select_pid is not None and c.pid == self._select_pid and c.refused is None:
                select_row = row
        self.panel.show_content()
        target = select_row if select_row >= 0 else first_ok
        if target >= 0:
            self.list.setCurrentRow(target)
        self._update_buttons()
        n = len(found)
        announce(
            self,
            self.tr("Found %n player(s).", None, n),
        )

    def _selected(self) -> InstanceCandidate | None:
        item = self.list.currentItem()
        if item is None:
            return None
        return self._candidates.get(CandidateId(str(item.data(_CID_ROLE))))

    def _update_buttons(self) -> None:
        cand = self._selected()
        can_attach = (
            cand is not None
            and cand.refused is None
            and not self._busy
            and gating_state(self.bridge, Feature.ATTACH) is None
        )
        self.attach_button.setEnabled(can_attach)
        self.attach_button.setDefault(can_attach)
        orphans_gated = gating_state(self.bridge, Feature.ORPHANS)
        has_orphan = cand is not None and cand.orphan_filter
        self.remove_button.setVisible(has_orphan)
        self.remove_button.setEnabled(has_orphan and not self._busy and orphans_gated is None)
        if orphans_gated is not None and orphans_gated.message is not None:
            self.remove_button.setAccessibleDescription(render(orphans_gated.message))
        if cand is None:
            self.detail.setText("")
        elif cand.refused is not None:
            self.detail.setText(
                render(cand.refusal) if cand.refusal is not None else self.tr("Refused")
            )
        else:
            self.detail.setText(self.tr("Socket: {path}").format(path=str(cand.socket)))

    def _on_attached(self, sid: SessionId) -> None:
        self._busy = False
        self.attached_sid = sid
        self.attached.emit(sid)
        self.accept()

    def _on_orphan_removed(self, _none: None) -> None:
        self._busy = False
        announce(self, self.tr("Leftover filter removed."))
        self.rescan()


__all__ = ["AttachDialog", "gating_state"]
