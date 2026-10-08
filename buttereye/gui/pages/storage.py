# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Storage page (docs/design/GUI.md §4.8; SCOPE §4.6, §4.8, F10).

Locations table: what each directory holds, its path, its true size ("0 bytes"
when empty) and whether ButterEye may delete it. RPM-owned rows are never
cleanable; they show the dnf command for orientation only (never run). Rows
with a clean target carry a checkbox; [Clean selected…] confirms the exact
paths and sizes first.

Models table: packaged models are not removable. [Download…] asks for consent
(URL, size, licence, SHA-256; nothing pre-checked) and shows byte progress
with Cancel; a hash mismatch (BE-6001) is reported and nothing is installed.
[Install from file…] asks for a SHA-256 when the file is not in the manifest;
such models are labelled "unpinned".
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QCoreApplication, QRegularExpression, Qt, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QRegularExpressionValidator,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    BackendId,
    ButterEyeError,
    CapState,
    CleanResult,
    CleanTarget,
    CommandHint,
    DownloadItem,
    ErrorCode,
    Feature,
    ModelEntry,
    ModelKind,
    NotAvailable,
    OperationCancelled,
    Progress,
    StorageEntry,
    StorageKind,
    command_hint,
    render,
    unavailable_text,
)
from buttereye.gui import theme
from buttereye.gui.a11y import labelled, message_box, selectable
from buttereye.gui.bridge import Ticket
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.consent import ConsentDialog
from buttereye.gui.dialogs.fit import FitDialog, add_buttons
from buttereye.gui.pages.base import GATING_REASONS, Page
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.findings_view import flat_view, select_row
from buttereye.gui.widgets.op_row import OpRow, human_bytes
from buttereye.gui.widgets.state_panel import StatePanel, split_headline
from buttereye.gui.widgets.status_badge import StatusBadge

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
MODEL_ROLE = Qt.ItemDataRole.UserRole + 2

#: RPM packages owning the fixed RPM paths (SCOPE §9), for the orientation line.
RPM_PACKAGES: dict[Path, tuple[str, ...]] = {
    Path("/usr/lib64/buttereye/vapoursynth"): ("buttereye-vs-rife-ncnn", "buttereye-vs-mvtools"),
    Path("/usr/share/buttereye"): ("buttereye-rife-ncnn-models",),
}

_SHA256 = QRegularExpression("^([0-9a-fA-F]{64})?$")


def _t(text: str) -> str:
    return QCoreApplication.translate("StoragePage", text)


def kind_name(kind: StorageKind) -> str:
    names = {
        StorageKind.CONFIG: _t("Settings"),
        StorageKind.ENGINES: _t("TensorRT engines"),
        StorageKind.MODELS: _t("Models"),
        StorageKind.PLUGINS: _t("Plugins"),
        StorageKind.DOWNLOADS: _t("Downloads"),
        StorageKind.JOBS: _t("Render jobs"),
        StorageKind.LOGS: _t("Logs"),
        StorageKind.BENCH: _t("Benchmark results"),
        StorageKind.RUNTIME: _t("Live files"),
        StorageKind.RPM: _t("Installed by dnf"),
    }
    return names[kind]


def model_kind_name(kind: ModelKind) -> str:
    names = {
        ModelKind.PACKAGED: _t("Packaged"),
        ModelKind.DOWNLOADED: _t("Downloaded"),
        ModelKind.EXTRA: _t("Extra"),
        ModelKind.UNPINNED: _t("Unpinned"),
    }
    return names[kind]


def backend_name(b: BackendId) -> str:
    names = {
        BackendId.RIFE_NCNN: _t("RIFE (Vulkan)"),
        BackendId.MVTOOLS: _t("MVTools (CPU)"),
        BackendId.RIFE_TRT: _t("RIFE · TensorRT (experimental)"),
    }
    return names[b]


def size_text(n: int | None) -> str:
    return _t("Not measurable") if n is None else human_bytes(n)


def cleanable(e: StorageEntry) -> bool:
    return e.clean_target is not None and e.deletable and not e.rpm_owned


def dnf_remove_line(e: StorageEntry) -> str:
    pkgs = RPM_PACKAGES.get(e.path)
    if pkgs:
        return "sudo dnf remove " + " ".join(pkgs)
    return f"rpm -qf {e.path}"


def clean_listing(entries: Sequence[StorageEntry]) -> str:
    return "\n".join(f"{e.path} — {size_text(e.bytes)}" for e in entries)


class CleanConfirmDialog(FitDialog):
    """Lists the exact paths and sizes before anything is deleted."""

    def __init__(self, entries: Sequence[StorageEntry], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("clean_confirm")
        self.setWindowTitle(_t("Clean selected folders"))
        total = sum(e.bytes or 0 for e in entries)
        self.intro = QLabel(
            _t(
                "The contents of these folders will be deleted ({size} in total). "
                "Files installed by dnf are never touched."
            ).format(size=human_bytes(total)),
            self,
        )
        self.intro.setWordWrap(True)
        self.intro.setTextFormat(Qt.TextFormat.PlainText)
        self.listing = QPlainTextEdit(clean_listing(entries), self)
        self.listing.setReadOnly(True)
        self.listing.setTabChangesFocus(True)
        theme.apply_fixed_font(self.listing)
        self.listing.setAccessibleName(_t("Folders to clean"))
        self.intro.setBuddy(self.listing)
        buttons = QDialogButtonBox(self)
        self.confirm = QPushButton(_t("&Delete contents"), self)
        self.confirm.setObjectName("clean.confirm")
        self.confirm.setAccessibleName(_t("Delete contents"))
        self.cancel = QPushButton(_t("Cancel"), self)
        self.cancel.setObjectName("clean.cancel")
        self.cancel.setAccessibleName(_t("Cancel"))
        add_buttons(buttons, self.confirm, self.cancel)  # Cancel is default
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(self.intro)
        lay.addWidget(self.listing, 1)
        lay.addWidget(buttons)


class Sha256Dialog(FitDialog):
    """Optional SHA-256 for a model archive installed from a file."""

    def __init__(self, file: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sha256")
        self.setWindowTitle(_t("Install model from file"))
        self.body = QLabel(
            _t(
                "{name}: if this file is listed in this release, leave the SHA-256 empty and "
                "it is checked against the pinned value. Otherwise enter the SHA-256 you got "
                "from its source; the model is then shown as “unpinned”."
            ).format(name=file.name),
            self,
        )
        self.body.setWordWrap(True)
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.edit = QLineEdit(self)
        self.edit.setObjectName("sha256.edit")
        self.edit.setValidator(QRegularExpressionValidator(_SHA256, self.edit))
        theme.apply_fixed_font(self.edit)
        self.label, _ = labelled(_t("&SHA-256 (64 hex digits, optional)"), self.edit)
        self.label.setParent(self)
        buttons = QDialogButtonBox(self)
        self.ok = QPushButton(_t("&Install"), self)
        self.ok.setObjectName("sha256.ok")
        self.ok.setAccessibleName(_t("Install"))
        cancel = QPushButton(_t("Cancel"), self)
        cancel.setObjectName("sha256.cancel")
        cancel.setAccessibleName(_t("Cancel"))
        buttons.addButton(self.ok, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(cancel, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.edit.textChanged.connect(self._sync)
        lay = QVBoxLayout(self)
        lay.addWidget(self.body)
        lay.addWidget(self.label)
        lay.addWidget(self.edit)
        lay.addWidget(buttons)
        self._sync()

    def sha256(self) -> str | None:
        text = self.edit.text().strip().lower()
        return text or None

    def _sync(self) -> None:
        self.ok.setEnabled(self.edit.hasAcceptableInput())


#: Shown while installing/removing a model archive is not in this build.
def file_install_unavailable_text() -> str:
    return _t(
        "Installing or removing a model from a file isn't in this build yet. "
        "Packaged models are installed and removed with dnf."
    )


async def model_files_supported(core: object) -> bool | None:
    """Whether the core can install/remove a model archive (``plugins.install``).

    Asks the facade's ``model_files_available()`` when the core has it; returns
    None ("unknown") on a core without it, and the page then learns it from the
    first ``NotAvailable`` instead.
    """
    probe = getattr(core, "model_files_available", None)
    if not callable(probe):
        return None
    return bool(probe())


class StoragePage(Page):
    page_id: ClassVar[str] = "storage"
    features: ClassVar[tuple[Feature, ...]] = (
        Feature.STORAGE,
        Feature.CLEAN,
        Feature.MODELS,
        Feature.MODEL_DOWNLOADS,
    )

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(_t("Storage"))
        self.entries: tuple[StorageEntry, ...] = ()
        self.models: tuple[ModelEntry, ...] = ()
        self._clean_ticket: Ticket | None = None
        self._install_ticket: Ticket | None = None
        self._queue: list[str] = []
        self._installing_file = False
        #: plugins.install present? None = unknown (assume yes until NotAvailable).
        self.files_ok: bool | None = None
        self.last_clean: CleanResult | None = None
        self._open_target: Path | None = None
        self.last_confirm: CleanConfirmDialog | None = None
        self.last_consent: ConsentDialog | None = None

        self.panel = StatePanel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.panel)
        content = self.panel.content
        cl = QVBoxLayout(content)

        # ---- locations
        self.loc_box = QGroupBox(_t("Where ButterEye keeps files"), content)
        ll = QVBoxLayout(self.loc_box)
        self.loc_model = QStandardItemModel(0, 5, self)
        self.loc_model.setHorizontalHeaderLabels(
            [_t("Location"), _t("Purpose"), _t("Path"), _t("Size"), _t("Deletable")]
        )
        self.loc_model.itemChanged.connect(lambda _i: self._sync_clean())
        self.loc_table = flat_view(self.loc_box, self.loc_model, _t("Storage locations"))
        self.loc_table.setAccessibleDescription(
            _t("Space ticks a folder to clean. Folders installed by dnf can't be ticked.")
        )
        self.loc_table.selectionModel().currentRowChanged.connect(lambda *_a: self._sync_rpm())
        self.rpm_caption = QLabel(_t("For orientation only (ButterEye never runs it):"), self)
        self.rpm_caption.setWordWrap(True)
        self.rpm_field = CopyField("", accessible_name=_t("Package command"), parent=self.loc_box)
        self.rpm_caption.setBuddy(self.rpm_field.line)
        self.clean_button = QPushButton(_t("C&lean selected…"), self.loc_box)
        self.clean_button.setObjectName("storage.clean")
        self.clean_button.setAccessibleName(_t("Clean selected"))
        self.clean_button.setAutoDefault(False)
        self.clean_button.clicked.connect(self.clean_selected)
        self.clean_note = QLabel(self.loc_box)
        self.clean_note.setWordWrap(True)
        self.clean_note.setTextFormat(Qt.TextFormat.PlainText)
        self.clean_box = QVBoxLayout()
        self.clean_row: OpRow | None = None
        self.clean_result = QLabel(self.loc_box)
        self.clean_result.setWordWrap(True)
        self.clean_result.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.clean_result, name=_t("Clean result"))
        self.clean_result.setVisible(False)
        self.open_folder = QPushButton(_t("&Open folder"), self.loc_box)
        self.open_folder.setAccessibleName(_t("Open folder"))
        self.open_folder.setAutoDefault(False)
        self.open_folder.clicked.connect(self._open_folder)
        self.open_folder.setVisible(False)
        crow = QHBoxLayout()
        crow.addWidget(self.clean_button)
        crow.addWidget(self.open_folder)
        crow.addStretch(1)
        self.clean_hint = CliHint(None, self.loc_box)
        ll.addWidget(self.loc_table, 1)
        ll.addWidget(self.rpm_caption)
        ll.addWidget(self.rpm_field)
        ll.addLayout(crow)
        ll.addWidget(self.clean_note)
        ll.addLayout(self.clean_box)
        ll.addWidget(self.clean_result)
        ll.addWidget(self.clean_hint)

        # ---- models
        self.models_box = QGroupBox(_t("Models"), content)
        ml = QVBoxLayout(self.models_box)
        self.models_panel = StatePanel(self.models_box)
        ml.addWidget(self.models_panel)
        mc = QVBoxLayout(self.models_panel.content)
        self.models_model = QStandardItemModel(0, 5, self)
        self.models_model.setHorizontalHeaderLabels(
            [_t("Name"), _t("Kind"), _t("Backend"), _t("Size"), _t("Licence")]
        )
        self.models_table = flat_view(self.models_panel.content, self.models_model, _t("Models"))
        self.models_table.selectionModel().currentRowChanged.connect(
            lambda *_a: self._sync_models()
        )
        self.remove_button = QPushButton(_t("&Remove"), self.models_panel.content)
        self.remove_button.setObjectName("models.remove")
        self.remove_button.setAccessibleName(_t("Remove model"))
        self.remove_button.setAutoDefault(False)
        self.remove_button.clicked.connect(self.remove_selected)
        self.download_button = QPushButton(_t("&Download…"), self.models_panel.content)
        self.download_button.setObjectName("models.download")
        self.download_button.setAccessibleName(_t("Download models"))
        self.download_button.setAutoDefault(False)
        self.download_button.clicked.connect(self.download)
        self.file_button = QPushButton(_t("&Install from file…"), self.models_panel.content)
        self.file_button.setObjectName("models.install_file")
        self.file_button.setAccessibleName(_t("Install model from file"))
        self.file_button.setAutoDefault(False)
        self.file_button.clicked.connect(self.install_from_file)
        mrow = QHBoxLayout()
        for b in (self.remove_button, self.download_button, self.file_button):
            mrow.addWidget(b)
        mrow.addStretch(1)
        self.remove_note = QLabel(self.models_panel.content)
        self.remove_note.setWordWrap(True)
        self.remove_note.setTextFormat(Qt.TextFormat.PlainText)
        self.dl_badge = StatusBadge("info", "", self.models_panel.content)
        self.dl_body = QLabel(self.models_panel.content)
        self.dl_body.setWordWrap(True)
        self.dl_body.setTextFormat(Qt.TextFormat.PlainText)
        self.op_box = QVBoxLayout()
        self.install_rows: list[OpRow] = []
        self.model_banner_box = QVBoxLayout()
        self.model_banner: Banner | None = None
        self.models_hint = CliHint(command_hint("models.list"), self.models_panel.content)
        mc.addWidget(self.models_table, 1)
        mc.addLayout(mrow)
        mc.addWidget(self.remove_note)
        mc.addWidget(self.dl_badge)
        mc.addWidget(self.dl_body)
        mc.addLayout(self.op_box)
        mc.addLayout(self.model_banner_box)
        mc.addWidget(self.models_hint)

        cl.addWidget(self.loc_box, 1)
        cl.addWidget(self.models_box, 1)
        self.panel.show_empty(_t("Press F5 to measure ButterEye's folders."))
        self.models_panel.show_empty(_t("Press F5 to list the models."))

    # ------------------------------------------------------------------ Page
    def title(self) -> str:
        return _t("Storage")

    def state_panel(self) -> StatePanel:
        return self.panel

    def cli_hint(self) -> CommandHint | None:
        targets = self.checked_targets()
        if targets:
            return command_hint("clean", target=",".join(sorted(t.value for t in targets)))
        return command_hint("models.list")

    def _state(self, f: Feature) -> CapState | None:
        caps = self.ctx.bridge.capabilities
        return caps.states.get(f) if caps is not None else None

    def _gated(self, f: Feature) -> CapState | None:
        st = self._state(f)
        if st is not None and not st.available and st.reason in GATING_REASONS:
            return st
        return None

    def refresh(self) -> None:
        gate = self._gated(Feature.STORAGE)
        if gate is not None:
            self.panel.show_unavailable(Feature.STORAGE, gate, command_hint("clean.dry_run"))
            return
        if not self.entries:
            self.panel.show_loading(_t("Measuring ButterEye's folders…"))
        self.ctx.bridge.call(
            lambda core: core.storage(), owner=self, ok=self._on_entries, err=self._on_error
        )
        self._refresh_models()

    def _on_error(self, err: ButterEyeError) -> None:
        if isinstance(err, OperationCancelled):
            return
        self.panel.show_error(err, actions=((_t("&Retry"), self.refresh),))

    # ------------------------------------------------------------------ locations
    def _on_entries(self, entries: tuple[StorageEntry, ...]) -> None:
        checked = {e.path for e in self.checked_entries()}
        self.entries = tuple(entries)
        self.loc_model.blockSignals(True)
        try:
            self.loc_model.removeRows(0, self.loc_model.rowCount())
            clean_ok = self._gated(Feature.CLEAN) is None and self._state_ok(Feature.CLEAN)
            for e in self.entries:
                loc = QStandardItem(kind_name(e.kind))
                loc.setData(e, ENTRY_ROLE)
                if cleanable(e) and clean_ok:
                    loc.setCheckable(True)
                    loc.setCheckState(
                        Qt.CheckState.Checked if e.path in checked else Qt.CheckState.Unchecked
                    )
                if e.rpm_owned:
                    deletable = _t("No — installed by dnf")
                elif e.deletable:
                    deletable = _t("Yes")
                else:
                    deletable = _t("No")
                cells = [
                    loc,
                    QStandardItem(render(e.note)),
                    QStandardItem(str(e.path)),
                    QStandardItem(size_text(e.bytes)),
                    QStandardItem(deletable),
                ]
                for c in cells[1:]:
                    c.setData(e, ENTRY_ROLE)
                loc.setData(
                    _t("{location}, {size}, deletable: {deletable}").format(
                        location=kind_name(e.kind), size=size_text(e.bytes), deletable=deletable
                    ),
                    Qt.ItemDataRole.AccessibleTextRole,
                )
                self.loc_model.appendRow(cells)
        finally:
            self.loc_model.blockSignals(False)
        self.loc_table.reset()
        if self.entries:
            self.panel.show_content()
            if not self.loc_table.currentIndex().isValid():
                select_row(self.loc_table, 0)
        else:
            self.panel.show_empty(_t("ButterEye reported no storage locations."))
        self._sync_rpm()
        self._sync_clean()

    def _state_ok(self, f: Feature) -> bool:
        st = self._state(f)
        return st is None or st.available

    def checked_entries(self) -> list[StorageEntry]:
        out: list[StorageEntry] = []
        for r in range(self.loc_model.rowCount()):
            item = self.loc_model.item(r, 0)
            if item is None or not item.isCheckable():
                continue
            e = item.data(ENTRY_ROLE)
            if item.checkState() is Qt.CheckState.Checked and isinstance(e, StorageEntry):
                out.append(e)
        return out

    def checked_targets(self) -> frozenset[CleanTarget]:
        return frozenset(e.clean_target for e in self.checked_entries() if e.clean_target)

    def set_checked(self, kind: StorageKind, on: bool) -> bool:
        for r in range(self.loc_model.rowCount()):
            item = self.loc_model.item(r, 0)
            e = item.data(ENTRY_ROLE) if item is not None else None
            if item is not None and isinstance(e, StorageEntry) and e.kind is kind:
                if not item.isCheckable():
                    return False
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
                return True
        return False

    def is_checkable(self, row: int) -> bool:
        item = self.loc_model.item(row, 0)
        return item is not None and item.isCheckable()

    def _current_entry(self) -> StorageEntry | None:
        idx = self.loc_table.currentIndex()
        e = idx.data(ENTRY_ROLE) if idx.isValid() else None
        return e if isinstance(e, StorageEntry) else None

    def _sync_rpm(self) -> None:
        e = self._current_entry()
        show = e is not None and e.rpm_owned
        self.rpm_caption.setVisible(show)
        self.rpm_field.setVisible(show)
        if e is not None and show:
            self.rpm_field.set_text(dnf_remove_line(e))

    def _sync_clean(self) -> None:
        st = self._state(Feature.CLEAN)
        if st is not None and not st.available:
            head, body = split_headline(render(unavailable_text(Feature.CLEAN, st)))
            self.clean_button.setEnabled(False)
            self.clean_note.setText(" ".join(t for t in (head, body) if t))
            self.clean_note.setVisible(True)
            self.clean_hint.set_hint(command_hint("clean.dry_run"))
            return
        targets = self.checked_targets()
        busy = self._clean_ticket is not None
        self.clean_button.setEnabled(bool(targets) and not busy)
        self.clean_note.setText(_t("Tick the folders to clean first."))
        self.clean_note.setVisible(not targets and not busy)
        self.clean_hint.set_hint(
            command_hint("clean", target=",".join(sorted(t.value for t in targets)))
            if targets
            else command_hint("clean.dry_run")
        )

    def clean_selected(self) -> None:
        chosen = self.checked_entries()
        if not chosen or self._clean_ticket is not None:
            return
        dlg = CleanConfirmDialog(chosen, self)
        self.last_confirm = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        targets = frozenset(e.clean_target for e in chosen if e.clean_target is not None)
        self._open_target = chosen[0].path
        if self.clean_row is not None:
            self.clean_box.removeWidget(self.clean_row)
            self.clean_row.deleteLater()
        self.clean_row = OpRow(_t("Cleaning"), self._cancel_clean, self.loc_box)
        self.clean_box.addWidget(self.clean_row)
        self.clean_result.setVisible(False)
        self.open_folder.setVisible(False)
        self._clean_ticket = self.ctx.bridge.run_op(
            lambda core: core.clean(targets),
            owner=self,
            ok=self._on_cleaned,
            err=self._on_clean_error,
            progress=self._clean_progress,
        )
        self._sync_clean()

    def _clean_progress(self, p: Progress) -> None:
        if self.clean_row is not None:
            self.clean_row.update(p)

    def _cancel_clean(self) -> None:
        if self._clean_ticket is not None:
            self._clean_ticket.cancel()
            self._clean_ticket = None
        if self.clean_row is not None:
            self.clean_row.finish(False, _t("Cancelled"), cancelled=True)
        self._sync_clean()
        self.refresh()

    def _on_cleaned(self, res: CleanResult) -> None:
        self._clean_ticket = None
        self.last_clean = res
        text = _t("Freed {size}.").format(size=human_bytes(res.freed_bytes))
        if res.skipped:
            text += "\n" + _t("Skipped:") + "\n"
            text += "\n".join(f"{p} — {render(why)}" for p, why in res.skipped)
        if self.clean_row is not None:
            self.clean_row.finish(True, text.splitlines()[0])
        self.clean_result.setText(text)
        self.clean_result.setAccessibleName(text)
        self.clean_result.setVisible(True)
        self.open_folder.setVisible(self._open_target is not None)
        self.ctx.announce(text.splitlines()[0])
        self.refresh()

    def _on_clean_error(self, err: ButterEyeError) -> None:
        self._clean_ticket = None
        if isinstance(err, OperationCancelled):
            self._cancel_clean()
            return
        if self.clean_row is not None:
            self.clean_row.finish(False, f"{render(err.cause)} ({err.code.code})")
        self.ctx.announce(
            _t("Cleaning failed: {cause}").format(cause=render(err.cause)), assertive=True
        )
        self._sync_clean()

    def _open_folder(self) -> None:
        if self._open_target is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._open_target)))

    # ------------------------------------------------------------------ models
    def _refresh_models(self) -> None:
        gate = self._gated(Feature.MODELS)
        if gate is not None:
            self.models_panel.show_unavailable(Feature.MODELS, gate, command_hint("models.list"))
            return
        if not self.models:
            self.models_panel.show_loading(_t("Listing models…"))
        self.ctx.bridge.call(
            lambda core: core.models(), owner=self, ok=self._on_models, err=self._on_models_error
        )
        self.ctx.bridge.call(
            model_files_supported, owner=self, ok=self._on_files_ok, err=lambda _e: None
        )

    def _on_files_ok(self, ok: bool | None) -> None:
        if ok is not None:
            self.files_ok = ok
        self._sync_models()

    def _on_models_error(self, err: ButterEyeError) -> None:
        if isinstance(err, OperationCancelled):
            return
        self.models_panel.show_error(err, actions=((_t("&Retry"), self._refresh_models),))

    def _on_models(self, models: tuple[ModelEntry, ...]) -> None:
        self.models = tuple(models)
        self.models_model.removeRows(0, self.models_model.rowCount())
        for m in self.models:
            licence = m.licence
            if m.kind is ModelKind.UNPINNED:
                licence = _t("{licence} (unpinned: no pinned SHA-256)").format(licence=licence)
            cells = [
                QStandardItem(m.name),
                QStandardItem(model_kind_name(m.kind)),
                QStandardItem(backend_name(m.backend)),
                QStandardItem(size_text(m.size_bytes)),
                QStandardItem(licence),
            ]
            for c in cells:
                c.setData(m, MODEL_ROLE)
            self.models_model.appendRow(cells)
        self.models_panel.show_content()
        if self.models and not self.models_table.currentIndex().isValid():
            select_row(self.models_table, 0)
        self._sync_models()

    def selected_model(self) -> ModelEntry | None:
        idx = self.models_table.currentIndex()
        m = idx.data(MODEL_ROLE) if idx.isValid() else None
        return m if isinstance(m, ModelEntry) else None

    def select_model(self, name: str) -> bool:
        for r in range(self.models_model.rowCount()):
            item = self.models_model.item(r, 0)
            if item is not None and item.text() == name:
                select_row(self.models_table, r)
                return True
        return False

    def _sync_models(self) -> None:
        m = self.selected_model()
        files_ok = self.files_ok is not False
        removable = m is not None and m.removable
        self.remove_button.setEnabled(removable and files_ok)
        if not files_ok:
            note = file_install_unavailable_text()
            self.remove_note.setText(note)
            self.remove_button.setAccessibleDescription(note)
        elif m is not None and not m.removable:
            note = _t("{name} was installed by dnf and can't be removed here.").format(name=m.name)
            self.remove_note.setText(note)
            self.remove_button.setAccessibleDescription(note)
        else:
            self.remove_note.setText("")
            self.remove_button.setAccessibleDescription("")
        self.remove_button.setToolTip(self.remove_button.accessibleDescription())
        self.remove_note.setVisible(bool(self.remove_note.text()))
        st = self._state(Feature.MODEL_DOWNLOADS)
        dl_ok = st is None or st.available
        busy = self._install_ticket is not None
        self.download_button.setEnabled(dl_ok and not busy)
        self.file_button.setEnabled(files_ok and not busy)
        files_why = "" if files_ok else file_install_unavailable_text()
        self.file_button.setAccessibleDescription(files_why)
        self.file_button.setToolTip(files_why)
        self.dl_badge.setVisible(not dl_ok)
        self.dl_body.setVisible(not dl_ok)
        if st is not None and not dl_ok:
            head, body = split_headline(render(unavailable_text(Feature.MODEL_DOWNLOADS, st)))
            self.dl_badge.set_state("info", head)
            self.dl_body.setText(body)
            self.download_button.setAccessibleDescription(head)
        else:
            self.download_button.setAccessibleDescription("")

    def remove_selected(self) -> None:
        m = self.selected_model()
        if m is None or not m.removable:
            return
        box = message_box(self)
        box.setObjectName("remove_confirm")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(_t("Remove model"))
        box.setText(_t("Remove the model {name}?").format(name=m.name))
        if m.path is not None:
            box.setInformativeText(_t("This deletes {path}.").format(path=m.path))
        yes = box.addButton(_t("&Remove"), QMessageBox.ButtonRole.DestructiveRole)
        yes.setObjectName("remove.confirm")
        no = box.addButton(_t("Cancel"), QMessageBox.ButtonRole.RejectRole)
        no.setObjectName("remove.cancel")
        box.setDefaultButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        name = m.name
        self.ctx.bridge.call(
            lambda core: core.model_remove(name),
            owner=self,
            ok=lambda _r: self._on_removed(name),
            err=self._on_remove_error,
        )

    def _on_removed(self, name: str) -> None:
        self.ctx.announce(_t("Model {name} removed.").format(name=name))
        self._refresh_models()
        self.refresh()

    def _set_model_banner(self, banner: Banner | None) -> None:
        if self.model_banner is not None:
            self.model_banner_box.removeWidget(self.model_banner)
            self.model_banner.hide()
            self.model_banner.deleteLater()
        self.model_banner = banner
        if banner is not None:
            banner.setParent(self.models_panel.content)
            self.model_banner_box.addWidget(banner)
            banner.show()

    def _show_model_error(self, err: ButterEyeError) -> None:
        if isinstance(err, OperationCancelled):
            return
        self._set_model_banner(Banner.from_error(err))
        self.ctx.announce(f"{err.code.code}: {render(err.cause)}", assertive=True)

    def download(self) -> None:
        self.ctx.bridge.call(
            lambda core: core.model_downloads(),
            owner=self,
            ok=self._ask_consent,
            err=self._show_model_error,
        )

    def _ask_consent(self, items: tuple[DownloadItem, ...]) -> None:
        if not items:
            self._set_model_banner(Banner("info", _t("This release lists no models to download.")))
            return
        dlg = ConsentDialog(items, self)
        self.last_consent = dlg
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = [d.name for d in items if d.name in dlg.selected()]
        self._queue.extend(chosen)
        self._next_install()

    def _next_install(self) -> None:
        if self._install_ticket is not None or not self._queue:
            self._sync_models()
            return
        name = self._queue.pop(0)
        row = OpRow(
            _t("Downloading {name}").format(name=name),
            self._cancel_install,
            self.models_panel.content,
        )
        self.install_rows.append(row)
        self.op_box.addWidget(row)
        self._set_model_banner(None)
        self._installing_file = False
        self._install_ticket = self.ctx.bridge.run_op(
            lambda core: core.model_install(name),
            owner=self,
            ok=self._on_installed,
            err=self._on_install_error,
            progress=row.update,
        )
        self._sync_models()

    def _learn_unavailable(self, err: ButterEyeError) -> bool:
        """A NotAvailable from install-file/remove means plugins.install is absent."""
        if not isinstance(err, NotAvailable):
            return False
        self.files_ok = False
        self._set_model_banner(Banner("info", file_install_unavailable_text()))
        self.ctx.announce(file_install_unavailable_text())
        self._sync_models()
        return True

    def _on_remove_error(self, err: ButterEyeError) -> None:
        if not self._learn_unavailable(err):
            self._show_model_error(err)

    def install_from_file(self) -> None:
        if self.files_ok is False:
            self.ctx.announce(file_install_unavailable_text())
            return
        name, _filter = QFileDialog.getOpenFileName(
            self,
            _t("Install model from file"),
            "",
            _t("Model archives (*.7z *.tar.gz *.tgz);;All files (*)"),
        )
        if not name:
            return
        self.start_install_file(Path(name))

    def start_install_file(
        self, file: Path, sha256: str | None = None, *, ask: bool = True
    ) -> None:
        if ask:
            dlg = Sha256Dialog(file, self)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            sha256 = dlg.sha256()
        if self._install_ticket is not None:
            return
        row = OpRow(
            _t("Installing {name}").format(name=file.name),
            self._cancel_install,
            self.models_panel.content,
        )
        self.install_rows.append(row)
        self.op_box.addWidget(row)
        self._set_model_banner(None)
        sha = sha256
        self._installing_file = True
        self._install_ticket = self.ctx.bridge.run_op(
            lambda core: core.model_install_file(file, sha),
            owner=self,
            ok=self._on_installed,
            err=self._on_install_error,
            progress=row.update,
        )
        self._sync_models()

    def _cancel_install(self) -> None:
        if self._install_ticket is not None:
            self._install_ticket.cancel()
            self._install_ticket = None
        if self.install_rows and not self.install_rows[-1].is_finished():
            self.install_rows[-1].finish(
                False, _t("Cancelled — nothing was installed."), cancelled=True
            )
        self._queue.clear()
        self._sync_models()

    def _on_installed(self, entry: ModelEntry) -> None:
        self._install_ticket = None
        text = _t("Installed {name}.").format(name=entry.name)
        if entry.kind is ModelKind.UNPINNED:
            text = _t("Installed {name} (unpinned).").format(name=entry.name)
        if self.install_rows:
            self.install_rows[-1].finish(True, text)
        self.ctx.announce(text)
        self._refresh_models()
        self._next_install()

    def _on_install_error(self, err: ButterEyeError) -> None:
        self._install_ticket = None
        if isinstance(err, OperationCancelled):
            self._cancel_install()
            return
        if self.install_rows:
            self.install_rows[-1].finish(False, f"{render(err.cause)} ({err.code.code})")
        if self._installing_file and self._learn_unavailable(err):
            self._queue.clear()
            return
        if err.code is ErrorCode.HASH_MISMATCH:
            # §4.8: no override in v1 — the banner offers no "install anyway".
            self._queue.clear()
        self._show_model_error(err)
        self._next_install()


__all__ = [
    "CleanConfirmDialog",
    "RPM_PACKAGES",
    "Sha256Dialog",
    "StoragePage",
    "cleanable",
    "dnf_remove_line",
    "kind_name",
]
