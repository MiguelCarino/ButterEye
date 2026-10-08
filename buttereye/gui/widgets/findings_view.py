# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Doctor findings: grouped tree + detail pane (docs/design/GUI.md §4.2 page 2, §4.7).

Shared by the setup wizard and the System page. Findings are grouped
Blocking / Degraded / OK / Info (OK collapsed). Each row shows the glyph and
the word of its severity (never colour alone) and the section; the detail pane
shows cause, fix, the BE code, every command in a ``CopyField`` (never run) and
the raw evidence lines.

Rule from §4.7: an unreadable kernel log (BE-1031) is never shown as OK, even
if a provider reported it so.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QCoreApplication, QEvent, QModelIndex, Qt, Signal
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import ErrorCode, Finding, Section, Severity, render
from buttereye.gui import theme
from buttereye.gui.a11y import selectable
from buttereye.gui.widgets.banner import command_name
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge, badge_pixmap

#: Group order on screen.
GROUP_ORDER: tuple[Severity, ...] = (
    Severity.BLOCKING,
    Severity.DEGRADED,
    Severity.OK,
    Severity.INFO,
)

BADGE_FOR: dict[Severity, BadgeKind] = {
    Severity.BLOCKING: "blocking",
    Severity.DEGRADED: "degraded",
    Severity.OK: "ok",
    Severity.INFO: "info",
}

FINDING_ROLE = Qt.ItemDataRole.UserRole + 1
BADGE_ROLE = Qt.ItemDataRole.UserRole + 2  # BadgeKind of a row icon, to recolour it


def _t(text: str) -> str:
    return QCoreApplication.translate("FindingsView", text)


def severity_word(sev: Severity) -> str:
    words = {
        Severity.BLOCKING: _t("Blocking"),
        Severity.DEGRADED: _t("Degraded"),
        Severity.OK: _t("OK"),
        Severity.INFO: _t("Info"),
    }
    return words[sev]


def section_name(section: Section) -> str:
    names = {
        Section.MPV: _t("mpv"),
        Section.PROBE: _t("In-mpv probe"),
        Section.PACKAGES: _t("Packages"),
        Section.VULKAN: _t("Vulkan"),
        Section.GPU_FAULT: _t("GPU fault history"),
        Section.CONFLICTS: _t("Conflicting mpv settings"),
        Section.RENDER: _t("Render tools"),
        Section.CONFIG: _t("Settings"),
        Section.TRT: _t("TensorRT (experimental)"),
    }
    return names[section]


def shown_severity(f: Finding) -> Severity:
    """The group a finding is shown in. Unreadable journal is never OK (§4.7)."""
    if f.code is ErrorCode.JOURNAL_UNREADABLE and f.severity is Severity.OK:
        return Severity.INFO
    return f.severity


def counts(findings: Sequence[Finding]) -> dict[Severity, int]:
    out = dict.fromkeys(GROUP_ORDER, 0)
    for f in findings:
        out[shown_severity(f)] += 1
    return out


def summary_text(findings: Sequence[Finding]) -> str:
    """ "2 problems, 1 warning, 18 checks passed" (plus notes, when any)."""
    c = counts(findings)
    b, d, ok, info = (c[s] for s in GROUP_ORDER)
    parts = [
        QCoreApplication.translate("FindingsView", "%n problem(s)", None, b),
        QCoreApplication.translate("FindingsView", "%n warning(s)", None, d),
        QCoreApplication.translate("FindingsView", "%n check(s) passed", None, ok),
    ]
    if info:
        parts.append(QCoreApplication.translate("FindingsView", "%n note(s)", None, info))
    return ", ".join(parts)


def finding_text(f: Finding) -> str:
    """Plain-text form of one finding (Copy all as text)."""
    sev = shown_severity(f)
    head = f"[{severity_word(sev)}] {section_name(f.section)}: {render(f.title)}"
    if f.code is not None:
        head += f" ({f.code.code})"
    if f.experimental:
        head += " " + _t("[Experimental]")
    lines = [head]
    cause = render(f.cause)
    fix = render(f.fix)
    if cause:
        lines.append("  " + _t("Cause: {text}").format(text=cause))
    if fix:
        lines.append("  " + _t("Fix: {text}").format(text=fix))
    lines += [f"  $ {c}" for c in f.commands]
    lines += [f"  > {e}" for e in f.evidence]
    return "\n".join(lines)


def findings_text(findings: Sequence[Finding]) -> str:
    ordered = sorted(findings, key=lambda f: GROUP_ORDER.index(shown_severity(f)))
    return "\n".join([summary_text(findings), ""] + [finding_text(f) for f in ordered])


def flat_view(parent: QWidget, model: QStandardItemModel, name: str) -> QTreeView:
    """A flat, row-selecting table (a tree without branches; Tab leaves it)."""
    view = QTreeView(parent)
    view.setModel(model)
    view.setRootIsDecorated(False)
    view.setItemsExpandable(False)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setTabKeyNavigation(False)
    view.setWordWrap(True)
    view.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.header().setStretchLastSection(True)
    view.setAccessibleName(name)
    return view


def select_row(view: QTreeView, row: int) -> None:
    model = view.model()
    if model is not None and 0 <= row < model.rowCount():
        view.setCurrentIndex(model.index(row, 0))


class FindingDetail(QWidget):
    """Cause, fix, code, commands and evidence of the selected finding."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.finding: Finding | None = None
        self.badge = StatusBadge("info", "", self)
        font = self.badge.text_label.font()
        font.setBold(True)
        self.badge.text_label.setFont(font)
        self.meta_label = QLabel(self)
        self.meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self.meta_label.setWordWrap(True)
        self.cause_label = QLabel(self)
        self.cause_label.setTextFormat(Qt.TextFormat.PlainText)
        self.cause_label.setWordWrap(True)
        self.fix_label = QLabel(self)
        self.fix_label.setTextFormat(Qt.TextFormat.PlainText)
        self.fix_label.setWordWrap(True)
        self.code_label = QLabel(self)
        self.code_label.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.code_label, name=_t("Error code"))
        self._cmds = QVBoxLayout()
        self._cmds.setContentsMargins(0, 0, 0, 0)
        self.command_fields: list[CopyField] = []
        self.evidence_caption = QLabel(_t("Evidence (raw lines)"), self)
        self.evidence = QPlainTextEdit(self)
        self.evidence.setReadOnly(True)
        self.evidence.setTabChangesFocus(True)
        self.evidence.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        theme.apply_fixed_font(self.evidence)
        self.evidence.setAccessibleName(_t("Evidence (raw lines)"))
        self.evidence_caption.setBuddy(self.evidence)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.badge)
        lay.addWidget(self.meta_label)
        lay.addWidget(self.cause_label)
        lay.addWidget(self.fix_label)
        lay.addWidget(self.code_label)
        lay.addLayout(self._cmds)
        lay.addWidget(self.evidence_caption)
        lay.addWidget(self.evidence)
        self.setAccessibleName(_t("Check details"))
        self.show_finding(None)

    def show_finding(self, f: Finding | None) -> None:
        self.finding = f
        while self._cmds.count():
            item = self._cmds.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.hide()
                w.deleteLater()
        self.command_fields = []
        if f is None:
            self.badge.set_state("info", _t("Select a check to see its details."))
            for w in (
                self.meta_label,
                self.cause_label,
                self.fix_label,
                self.code_label,
                self.evidence_caption,
                self.evidence,
            ):
                w.setVisible(False)
            return
        sev = shown_severity(f)
        self.badge.set_state(BADGE_FOR[sev], render(f.title))
        meta = _t("{severity} · {section}").format(
            severity=severity_word(sev), section=section_name(f.section)
        )
        if f.experimental:
            meta += " · " + _t("Experimental")
        self.meta_label.setText(meta)
        self.meta_label.setVisible(True)
        cause, fix = render(f.cause), render(f.fix)
        self.cause_label.setText(_t("Cause: {text}").format(text=cause))
        self.cause_label.setVisible(bool(cause))
        self.fix_label.setText(_t("Fix: {text}").format(text=fix))
        self.fix_label.setVisible(bool(fix))
        code = f.code.code if f.code is not None else ""
        self.code_label.setText(code)
        self.code_label.setAccessibleName(_t("Error code {code}").format(code=code))
        self.code_label.setVisible(bool(code))
        n = len(f.commands)
        for i, c in enumerate(f.commands):
            field = CopyField(c, accessible_name=command_name(i, n), parent=self)
            self.command_fields.append(field)
            self._cmds.addWidget(field)
        self.evidence.setPlainText("\n".join(f.evidence))
        self.evidence_caption.setVisible(bool(f.evidence))
        self.evidence.setVisible(bool(f.evidence))


class FindingsView(QWidget):
    """``set_findings(findings)``; ``summary()``; ``as_text()``; ``detail`` pane."""

    finding_selected = Signal(object)  # Finding | None

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        groups: Sequence[Severity] = GROUP_ORDER,
        accessible_name: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._groups = tuple(groups)
        self._findings: tuple[Finding, ...] = ()
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels([_t("Check"), _t("Section"), _t("Result")])
        self.tree = QTreeView(self)
        self.tree.setModel(self.model)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setTabKeyNavigation(False)
        self.tree.setUniformRowHeights(False)
        self.tree.setWordWrap(True)
        self.tree.setAccessibleName(accessible_name or _t("Check results"))
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        sel = self.tree.selectionModel()
        sel.currentRowChanged.connect(self._on_current)
        self.detail = FindingDetail(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.detail)
        self.setFocusProxy(self.tree)

    # ------------------------------------------------------------------ API
    def set_findings(self, findings: Sequence[Finding]) -> None:
        self._findings = tuple(f for f in findings if shown_severity(f) in self._groups)
        self.model.removeRows(0, self.model.rowCount())
        first: QModelIndex | None = None
        for sev in self._groups:
            members = [f for f in self._findings if shown_severity(f) is sev]
            if not members:
                continue
            group = QStandardItem(
                _t("{group} ({n})").format(group=severity_word(sev), n=len(members))
            )
            self._set_icon(group, BADGE_FOR[sev])
            group.setData(None, FINDING_ROLE)
            group.setData(
                _t("{group}: {n} checks").format(group=severity_word(sev), n=len(members)),
                Qt.ItemDataRole.AccessibleTextRole,
            )
            group.setSelectable(False)
            filler1, filler2 = QStandardItem(""), QStandardItem("")
            for it in (filler1, filler2):
                it.setSelectable(False)
            self.model.appendRow([group, filler1, filler2])
            for f in members:
                title = render(f.title)
                if f.experimental:
                    title = _t("{title} (experimental)").format(title=title)
                name = QStandardItem(title)
                self._set_icon(name, BADGE_FOR[sev])
                name.setData(f, FINDING_ROLE)
                name.setData(
                    _t("Check result: {title}, {result}").format(
                        title=title, result=severity_word(sev)
                    ),
                    Qt.ItemDataRole.AccessibleTextRole,
                )
                section = QStandardItem(section_name(f.section))
                section.setData(f, FINDING_ROLE)
                result = QStandardItem(severity_word(sev))
                result.setData(f, FINDING_ROLE)
                group.appendRow([name, section, result])
            gindex = group.index()
            self.tree.setFirstColumnSpanned(gindex.row(), QModelIndex(), True)
            self.tree.setExpanded(gindex, sev is not Severity.OK)
            if first is None and sev is not Severity.OK:
                first = group.child(0, 0).index()
        if first is None and self.model.rowCount():
            top = self.model.item(0, 0)
            if top is not None and top.rowCount():
                first = top.child(0, 0).index()
        if first is not None:
            self.tree.setCurrentIndex(first)
        else:
            self.detail.show_finding(None)
        self.tree.setAccessibleDescription(self.summary())

    def findings(self) -> tuple[Finding, ...]:
        return self._findings

    def counts(self) -> dict[Severity, int]:
        return counts(self._findings)

    def summary(self) -> str:
        return summary_text(self._findings)

    def as_text(self) -> str:
        return findings_text(self._findings)

    def group_titles(self) -> list[str]:
        return [self.model.item(r, 0).text() for r in range(self.model.rowCount())]

    def rows_in_group(self, sev: Severity) -> list[Finding]:
        return [f for f in self._findings if shown_severity(f) is sev]

    def is_group_expanded(self, sev: Severity) -> bool:
        for r in range(self.model.rowCount()):
            item = self.model.item(r, 0)
            if item is not None and item.text().startswith(severity_word(sev) + " ("):
                return self.tree.isExpanded(item.index())
        return False

    def select_finding(self, finding_id: str) -> bool:
        for r in range(self.model.rowCount()):
            group = self.model.item(r, 0)
            if group is None:
                continue
            for c in range(group.rowCount()):
                child = group.child(c, 0)
                f = child.data(FINDING_ROLE) if child is not None else None
                if isinstance(f, Finding) and f.id == finding_id and child is not None:
                    self.tree.setExpanded(group.index(), True)
                    self.tree.setCurrentIndex(child.index())
                    return True
        return False

    # ------------------------------------------------------------- internals
    def _icon(self, kind: BadgeKind) -> QIcon:
        size = max(8, self.fontMetrics().height())
        return QIcon(badge_pixmap(kind, self.palette(), size, self.devicePixelRatioF()))

    def _set_icon(self, item: QStandardItem, kind: BadgeKind) -> None:
        item.setData(kind, BADGE_ROLE)
        item.setIcon(self._icon(kind))

    def _recolour_icons(self) -> None:
        """Glyphs are tinted from the palette; redraw them when it changes (dark mode)."""
        for r in range(self.model.rowCount()):
            group = self.model.item(r, 0)
            if group is None:
                continue
            for item in (group, *(group.child(c, 0) for c in range(group.rowCount()))):
                kind = item.data(BADGE_ROLE) if item is not None else None
                if item is not None and kind is not None:
                    item.setIcon(self._icon(kind))

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.FontChange,
        ):
            self._recolour_icons()
        super().changeEvent(event)

    def _on_current(self, current: QModelIndex, _previous: QModelIndex) -> None:
        f = current.data(FINDING_ROLE) if current.isValid() else None
        finding = f if isinstance(f, Finding) else None
        self.detail.show_finding(finding)
        self.finding_selected.emit(finding)


__all__ = [
    "BADGE_FOR",
    "flat_view",
    "select_row",
    "FindingDetail",
    "FindingsView",
    "GROUP_ORDER",
    "counts",
    "finding_text",
    "findings_text",
    "section_name",
    "severity_word",
    "shown_severity",
    "summary_text",
]
