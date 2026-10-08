# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""About page (docs/design/GUI.md §4.9; SCOPE F18, §8.4).

About tab: ButterEye's own Appropriate Legal Notices — name, version,
copyright, no-warranty statement, licence name, the full AGPL text and the
generated-output permission from the shipped files (never only a URL), and the
source link. A shipped file that is missing is shown as a DEGRADED banner
(BE-1021), never hidden. These notices are never gated.

Licences tab: ``third_party()`` — every detected component with its SPDX
expression, how ButterEye uses it, whether ButterEye ships it, and its
licence texts. Gated by ``Feature.LICENCES``.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    CommandHint,
    ComponentLicence,
    ErrorCode,
    Feature,
    LegalNotices,
    OperationCancelled,
    command_hint,
    legal_notices,
    render,
)
from buttereye.gui.a11y import heading, selectable
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.text_viewer import TextViewer
from buttereye.gui.pages.base import GATING_REASONS, Page, PageAction
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.findings_view import flat_view, select_row
from buttereye.gui.widgets.state_panel import StatePanel

COLUMNS = 7
#: Legal notice text is never translated; used only if the licences provider is absent.
FALLBACK_COPYRIGHT = "Copyright (C) 2026 The ButterEye contributors"
FALLBACK_LICENCE = "GNU Affero General Public License v3.0 or later"
COMPONENT_ROLE = Qt.ItemDataRole.UserRole + 1


def _t(text: str) -> str:
    return QCoreApplication.translate("AboutPage", text)


def missing_text(path: Path) -> str:
    return _t("Licence file not found at {path} ({code}). This is a packaging bug.").format(
        path=path, code=ErrorCode.LICENCE_FILE_MISSING.code
    )


class AboutPage(Page):
    page_id: ClassVar[str] = "about"
    features: ClassVar[tuple[Feature, ...]] = (Feature.LICENCES,)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(_t("About"))
        self.notices: LegalNotices | None = None
        self.components: tuple[ComponentLicence, ...] = ()
        self._loaded = False
        self.last_viewer: TextViewer | None = None

        self.panel = StatePanel(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.panel)
        self.tabs = QTabWidget(self.panel.content)
        self.tabs.setAccessibleName(_t("About ButterEye"))
        self.tabs.tabBar().setAccessibleName(_t("About ButterEye"))
        cl = QVBoxLayout(self.panel.content)
        cl.addWidget(self.tabs)
        self._build_about_tab()
        self._build_licences_tab()
        self.panel.show_content()
        self._show_notices()

    # ------------------------------------------------------------------ build
    def _build_about_tab(self) -> None:
        tab = QWidget()
        self.about_tab = tab
        lay = QVBoxLayout(tab)
        self.name_label = heading(QLabel(tab))
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.name_label)
        self.copyright_label = QLabel(tab)
        self.copyright_label.setTextFormat(Qt.TextFormat.PlainText)
        self.copyright_label.setWordWrap(True)
        selectable(self.copyright_label)
        self.licence_label = QLabel(tab)
        self.licence_label.setTextFormat(Qt.TextFormat.PlainText)
        self.licence_label.setWordWrap(True)
        selectable(self.licence_label)
        self.warranty_label = QLabel(tab)
        self.warranty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warranty_label.setWordWrap(True)
        selectable(self.warranty_label)
        self.banner_box = QVBoxLayout()
        self.missing_banners: list[Banner] = []

        self.view_licence = QPushButton(_t("&View full licence"), tab)
        self.view_licence.setAccessibleName(_t("View full licence"))
        self.view_licence.setAutoDefault(False)
        self.view_licence.clicked.connect(self._view_agpl)
        self.view_permission = QPushButton(_t("View &output permission"), tab)
        self.view_permission.setAccessibleName(_t("View output permission"))
        self.view_permission.setAutoDefault(False)
        self.view_permission.clicked.connect(self._view_permission)
        row = QHBoxLayout()
        row.addWidget(self.view_licence)
        row.addWidget(self.view_permission)
        row.addStretch(1)

        self.source_caption = QLabel(_t("Source code"), tab)
        self.source_field = CopyField("", accessible_name=_t("Source code"), parent=tab)
        self.source_caption.setBuddy(self.source_field.line)
        self.source_note = QLabel(
            _t("Until a public repository exists, the source is the package's source RPM."),
            tab,
        )
        self.source_note.setWordWrap(True)
        self.source_note.setTextFormat(Qt.TextFormat.PlainText)
        self.open_source = QPushButton(_t("Open source &link"), tab)
        self.open_source.setAccessibleName(_t("Open source link"))
        self.open_source.setAutoDefault(False)
        self.open_source.clicked.connect(self._open_source)

        # One command hint per tab, matching cli_hint(): "version" here, "licence"
        # on the Licences tab (two stacked hints read as two separate commands).
        self.version_hint = CliHint(command_hint("version"), tab)

        lay.addWidget(self.name_label)
        lay.addWidget(self.copyright_label)
        lay.addWidget(self.licence_label)
        lay.addWidget(self.warranty_label)
        lay.addLayout(self.banner_box)
        lay.addLayout(row)
        lay.addWidget(self.source_caption)
        lay.addWidget(self.source_field)
        lay.addWidget(self.source_note)
        lay.addWidget(self.open_source)
        lay.addWidget(self.version_hint)
        lay.addStretch(1)
        self.tabs.addTab(tab, _t("&About"))

    def _build_licences_tab(self) -> None:
        tab = QWidget()
        self.licences_tab = tab
        lay = QVBoxLayout(tab)
        self.lic_panel = StatePanel(tab)
        lay.addWidget(self.lic_panel)
        content = self.lic_panel.content
        cl = QVBoxLayout(content)
        self.lic_intro = QLabel(
            _t(
                "Components ButterEye uses or ships, as detected on this system. "
                "This list is informational; each component's own licence applies."
            ),
            content,
        )
        self.lic_intro.setWordWrap(True)
        self.lic_intro.setTextFormat(Qt.TextFormat.PlainText)
        self.model = QStandardItemModel(0, COLUMNS, self)
        self.model.setHorizontalHeaderLabels(
            [
                _t("Name"),
                _t("Version"),
                _t("SPDX"),
                _t("How it's used"),
                _t("Shipped by ButterEye"),
                _t("Detected"),
                _t("Notes"),
            ]
        )
        self.table = flat_view(content, self.model, _t("Third-party components"))
        self.lic_intro.setBuddy(self.table)
        self.table.selectionModel().currentRowChanged.connect(lambda *_a: self._sync_view())
        self.table.doubleClicked.connect(lambda _i: self._view_selected())
        self.view_text = QPushButton(_t("View licence te&xt"), content)
        self.view_text.setAccessibleName(_t("View licence text"))
        self.view_text.setAutoDefault(False)
        self.view_text.clicked.connect(self._view_selected)
        self.lic_hint = CliHint(command_hint("licence"), content)
        cl.addWidget(self.lic_intro)
        cl.addWidget(self.table, 1)
        cl.addWidget(self.view_text)
        cl.addWidget(self.lic_hint)
        self.lic_panel.show_empty(_t("Press F5 to list the third-party components."))
        self.tabs.addTab(tab, _t("&Licences"))

    # ------------------------------------------------------------------ Page
    def title(self) -> str:
        return _t("About")

    def state_panel(self) -> StatePanel:
        return self.lic_panel

    def cli_hint(self) -> CommandHint | None:
        return command_hint("version" if self.tabs.currentIndex() == 0 else "licence")

    def handle_action(self, action: PageAction) -> bool:
        if action is PageAction.SHOW_LICENCES:
            self.tabs.setCurrentWidget(self.licences_tab)
            if not self._loaded:
                self.refresh()
            self.table.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        if action is PageAction.SHOW_ABOUT:
            self.tabs.setCurrentWidget(self.about_tab)
            return True
        return False

    def refresh(self) -> None:
        self._show_notices()
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(Feature.LICENCES) if caps is not None else None
        if st is not None and not st.available and st.reason in GATING_REASONS:
            self.lic_panel.show_unavailable(Feature.LICENCES, st, command_hint("licence"))
            return
        self.lic_panel.show_loading(_t("Looking for third-party components…"))
        self.ctx.bridge.call(
            lambda core: core.third_party(),
            owner=self,
            ok=self._on_components,
            err=self._on_components_error,
        )

    # ------------------------------------------------------------------ notices
    def _show_notices(self) -> None:
        try:
            n: LegalNotices | None = legal_notices()
        except ButterEyeError:
            n = None
        self.notices = n
        if n is None:
            # Never gated: the static notice still shows; the files can't be located.
            self.name_label.setText(_t("ButterEye"))
            self.copyright_label.setText(FALLBACK_COPYRIGHT)
            self.licence_label.setText(_t("Licence: {name}").format(name=FALLBACK_LICENCE))
            self.warranty_label.setText(_t("This program comes with ABSOLUTELY NO WARRANTY."))
            self.source_field.set_text("")
            self._set_missing(())
            self.view_licence.setEnabled(False)
            self.view_permission.setEnabled(False)
            self.open_source.setVisible(False)
            return
        self.name_label.setText(_t("{name} {version}").format(name=n.name, version=n.version))
        self.name_label.setAccessibleName(self.name_label.text())
        self.copyright_label.setText(n.copyright)
        self.copyright_label.setAccessibleName(n.copyright)
        self.licence_label.setText(_t("Licence: {name}").format(name=n.licence_name))
        self.licence_label.setAccessibleName(self.licence_label.text())
        self.warranty_label.setText(render(n.no_warranty))
        self.warranty_label.setAccessibleName(self.warranty_label.text())
        self.source_field.set_text(n.source_link)
        is_url = n.source_link.startswith(("https://", "http://"))
        self.open_source.setVisible(is_url)
        self.source_note.setVisible(not is_url)
        self._set_missing(n.missing)
        missing = set(n.missing)
        for btn, path in (
            (self.view_licence, n.agpl_text),
            (self.view_permission, n.output_permission_text),
        ):
            gone = path in missing
            btn.setEnabled(not gone)
            btn.setAccessibleDescription(missing_text(path) if gone else str(path))

    def _set_missing(self, paths: Sequence[Path]) -> None:
        for b in self.missing_banners:
            self.banner_box.removeWidget(b)
            b.hide()
            b.deleteLater()
        self.missing_banners = []
        for p in paths:
            b = Banner(
                "degraded",
                missing_text(p),
                code=ErrorCode.LICENCE_FILE_MISSING.code,
                parent=self.about_tab,
            )
            self.missing_banners.append(b)
            self.banner_box.addWidget(b)
            b.show()

    def _view_agpl(self) -> None:
        if self.notices is not None:
            self._viewer(_t("GNU Affero General Public License"), [self.notices.agpl_text])

    def _view_permission(self) -> None:
        if self.notices is not None:
            self._viewer(
                _t("Permission for generated output"), [self.notices.output_permission_text]
            )

    def _open_source(self) -> None:
        if self.notices is not None:
            QDesktopServices.openUrl(QUrl(self.notices.source_link))

    def _viewer(self, title: str, files: Sequence[Path]) -> None:
        dlg = TextViewer(title, files, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.last_viewer = dlg
        dlg.open()

    # ------------------------------------------------------------------ licences
    def _on_components(self, comps: tuple[ComponentLicence, ...]) -> None:
        self._loaded = True
        self.components = tuple(comps)
        self.model.removeRows(0, self.model.rowCount())
        yes, no = _t("Yes"), _t("No")
        for c in self.components:
            note = ""
            if c.finding is not None:
                note = render(c.finding.title)
                if c.finding.code is not None:
                    note += f" ({c.finding.code.code})"
            shipped = _t("Yes (COPR)") if c.conveyed else no
            cells = [
                c.name,
                c.version or _t("unknown"),
                c.spdx,
                render(c.relation),
                shipped,
                yes if c.detected else _t("Not found"),
                note,
            ]
            row = []
            for text in cells:
                item = QStandardItem(text)
                item.setData(c, COMPONENT_ROLE)
                row.append(item)
            row[0].setData(
                _t("{name}, {spdx}, shipped by ButterEye: {shipped}").format(
                    name=c.name, spdx=c.spdx, shipped=shipped
                ),
                Qt.ItemDataRole.AccessibleTextRole,
            )
            self.model.appendRow(row)
        if self.components:
            self.lic_panel.show_content()
            select_row(self.table, 0)
        else:
            self.lic_panel.show_empty(_t("No third-party components were detected."))
        self._sync_view()

    def _on_components_error(self, err: ButterEyeError) -> None:
        if isinstance(err, OperationCancelled):
            return
        self.lic_panel.show_error(err, actions=((_t("&Retry"), self.refresh),))

    def selected_component(self) -> ComponentLicence | None:
        idx = self.table.currentIndex()
        if not idx.isValid():
            return None
        c = idx.data(COMPONENT_ROLE)
        return c if isinstance(c, ComponentLicence) else None

    def _sync_view(self) -> None:
        c = self.selected_component()
        ok = c is not None and bool(c.text_files)
        self.view_text.setEnabled(ok)
        if c is not None and not c.text_files:
            self.view_text.setAccessibleDescription(
                _t("No licence text files were found for {name}.").format(name=c.name)
            )
        else:
            self.view_text.setAccessibleDescription("")

    def _view_selected(self) -> None:
        c = self.selected_component()
        if c is None or not c.text_files:
            return
        self._viewer(_t("Licence: {name}").format(name=c.name), list(c.text_files))


__all__ = ["AboutPage", "missing_text"]
