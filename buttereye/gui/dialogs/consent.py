# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Download consent (SCOPE §4.8, §5.4; GUI.md §4.2 page 5, §4.8).

Every downloadable item is listed with its upstream URL, size, licence and the
pinned SHA-256 before anything is fetched. Nothing is pre-checked; the
download button stays disabled until the user ticks at least one row.
``DownloadTable`` is shared with the setup wizard's Downloads page.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import DownloadItem
from buttereye.gui.dialogs.fit import FitDialog, add_buttons
from buttereye.gui.widgets.op_row import human_bytes

NAME_ROLE = Qt.ItemDataRole.UserRole + 1


def _t(text: str) -> str:
    return QCoreApplication.translate("ConsentDialog", text)


class DownloadTable(QTableWidget):
    """Name / Size / Licence / Source URL / SHA-256, one checkbox per row, none checked."""

    selection_changed = Signal()

    def __init__(self, items: Sequence[DownloadItem] = (), parent: QWidget | None = None) -> None:
        super().__init__(0, 5, parent)
        self.setHorizontalHeaderLabels(
            [_t("Name"), _t("Size"), _t("Licence"), _t("Source URL"), _t("SHA-256")]
        )
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setTabKeyNavigation(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.setAccessibleName(_t("Downloads to confirm"))
        self.setAccessibleDescription(
            _t("Space ticks or unticks the selected download. Nothing is ticked at first.")
        )
        self.itemChanged.connect(lambda _item: self.selection_changed.emit())
        self.set_items(items)

    def set_items(self, items: Sequence[DownloadItem]) -> None:
        self.blockSignals(True)
        try:
            self.setRowCount(0)
            for d in items:
                r = self.rowCount()
                self.insertRow(r)
                name = QTableWidgetItem(
                    d.name if d.pinned else _t("{name} (unpinned)").format(name=d.name)
                )
                name.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                name.setCheckState(Qt.CheckState.Unchecked)
                name.setData(NAME_ROLE, d.name)
                name.setData(
                    Qt.ItemDataRole.AccessibleTextRole,
                    _t("Download {name}, {size}, licence {licence}").format(
                        name=d.name, size=human_bytes(d.size_bytes), licence=d.licence
                    ),
                )
                cells = [
                    name,
                    QTableWidgetItem(human_bytes(d.size_bytes)),
                    QTableWidgetItem(d.licence),
                    QTableWidgetItem(d.url),
                    QTableWidgetItem(d.sha256),
                ]
                for c, cell in enumerate(cells):
                    if c:
                        cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    self.setItem(r, c, cell)
        finally:
            self.blockSignals(False)
        if self.rowCount():
            self.setCurrentCell(0, 0)
        self.selection_changed.emit()

    def checked_names(self) -> frozenset[str]:
        out: set[str] = set()
        for r in range(self.rowCount()):
            item = self.item(r, 0)
            if item is not None and item.checkState() is Qt.CheckState.Checked:
                out.add(str(item.data(NAME_ROLE)))
        return frozenset(out)

    def set_checked(self, name: str, on: bool) -> None:
        for r in range(self.rowCount()):
            item = self.item(r, 0)
            if item is not None and item.data(NAME_ROLE) == name:
                item.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)


class ConsentDialog(FitDialog):
    """``ConsentDialog(items)``; after ``exec()``, ``selected()`` is the confirmed names."""

    def __init__(self, items: Sequence[DownloadItem], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("consent")
        self.setWindowTitle(_t("Confirm downloads"))
        self.intro = QLabel(
            _t(
                "ButterEye downloads only what you tick below, from the upstream address "
                "shown. Each file is checked against its SHA-256 before it is installed; "
                "a file that does not match is deleted."
            ),
            self,
        )
        self.intro.setWordWrap(True)
        self.intro.setTextFormat(Qt.TextFormat.PlainText)
        self.table = DownloadTable(items, self)
        self.intro.setBuddy(self.table)
        self.buttons = QDialogButtonBox(self)
        self.download_button = QPushButton(_t("&Download selected"), self)
        self.download_button.setObjectName("consent.download")
        self.download_button.setAccessibleName(_t("Download selected"))
        self.cancel_button = QPushButton(_t("Cancel"), self)
        self.cancel_button.setObjectName("consent.cancel")
        self.cancel_button.setAccessibleName(_t("Cancel"))
        add_buttons(self.buttons, self.download_button, self.cancel_button)  # Cancel is default
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.table.selection_changed.connect(self._sync)
        lay = QVBoxLayout(self)
        lay.addWidget(self.intro)
        lay.addWidget(self.table, 1)
        lay.addWidget(self.buttons)
        self._sync()
        metrics = self.fontMetrics()
        self.resize(metrics.horizontalAdvance("M") * 90, metrics.height() * 18)

    def selected(self) -> frozenset[str]:
        return self.table.checked_names()

    def _sync(self) -> None:
        self.download_button.setEnabled(bool(self.table.checked_names()))


__all__ = ["ConsentDialog", "DownloadTable"]
