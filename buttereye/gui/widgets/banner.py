# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Inline banner: badge + title, body, selectable BE code, copyable commands, actions.

``Banner.from_error(err)`` renders a ``ButterEyeError``: title = cause, body = fix,
code = ``BE-xxxx``, commands = ``err.commands``, raw ``detail`` behind a
[Show details] toggle. ``OperationCancelled`` becomes a neutral "Cancelled"
banner without a code (§3 rule 3); ``NotAvailable`` is informational.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeAlias

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QAction, QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import ButterEyeError, NotAvailable, OperationCancelled, render
from buttereye.gui import theme
from buttereye.gui.a11y import plain, selectable
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.status_badge import BADGE_KINDS, BadgeKind, StatusBadge

Action: TypeAlias = tuple[str, Callable[[], None]] | QAction
"""A button: ``(label, callback)`` or a ``QAction`` (text, enabled state, trigger)."""


def make_button(action: Action, parent: QWidget) -> QPushButton:
    """A ``QPushButton`` for ``action``; a QAction keeps its enabled state in sync."""
    if isinstance(action, QAction):
        btn = QPushButton(action.text(), parent)
        btn.setEnabled(action.isEnabled())
        btn.setAccessibleName(plain(action.text()))
        if action.toolTip():
            btn.setAccessibleDescription(action.toolTip())
        btn.clicked.connect(action.trigger)
        action.changed.connect(lambda: _sync(btn, action))
        return btn
    text, callback = action
    btn = QPushButton(text, parent)
    btn.setAccessibleName(plain(text))
    btn.setAutoDefault(False)
    btn.clicked.connect(lambda _checked=False: callback())
    return btn


def _sync(btn: QPushButton, action: QAction) -> None:
    try:
        btn.setEnabled(action.isEnabled())
        btn.setText(action.text())
    except RuntimeError:  # button already deleted
        pass


def command_name(i: int, n: int) -> str:
    """Accessible name for the i-th (0-based) of n copyable commands.

    One fixed context ("Banner") so the catalogue has these strings (a passed-in
    ``tr`` translated them in whatever class called, which the .ts never saw).
    """
    if n == 1:
        return QCoreApplication.translate("Banner", "Command")
    return QCoreApplication.translate("Banner", "Command {i} of {n}").format(i=i + 1, n=n)


class Banner(QFrame):
    def __init__(
        self,
        kind: BadgeKind,
        title: str,
        body: str = "",
        code: str | None = None,
        commands: Sequence[str] = (),
        actions: Sequence[Action] = (),
        parent: QWidget | None = None,
        *,
        detail: str | None = None,
    ) -> None:
        super().__init__(parent)
        if kind not in BADGE_KINDS:
            raise ValueError(f"unknown banner kind {kind!r}")
        self.kind: BadgeKind = kind
        self.code = code
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFrameShadow(QFrame.Shadow.Raised)
        self.setAutoFillBackground(True)
        self.setBackgroundRole(QPalette.ColorRole.AlternateBase)

        self.badge = StatusBadge(kind, title, self)
        font = self.badge.text_label.font()
        font.setBold(True)
        self.badge.text_label.setFont(font)
        self.title_label = self.badge.text_label

        self.body_label = QLabel(body, self)
        self.body_label.setTextFormat(Qt.TextFormat.PlainText)
        self.body_label.setWordWrap(True)
        self.body_label.setVisible(bool(body))

        self.code_label = QLabel(code or "", self)
        self.code_label.setTextFormat(Qt.TextFormat.PlainText)
        selectable(self.code_label, name=self.tr("Error code {code}").format(code=code or ""))
        self.code_label.setVisible(bool(code))

        lay = QVBoxLayout(self)
        lay.addWidget(self.badge)
        lay.addWidget(self.body_label)
        lay.addWidget(self.code_label)

        cmds = tuple(commands)
        self.command_fields: list[CopyField] = []
        for i, c in enumerate(cmds):
            field = CopyField(c, accessible_name=command_name(i, len(cmds)), parent=self)
            self.command_fields.append(field)
            lay.addWidget(field)

        self.detail_view: QPlainTextEdit | None = None
        self.detail_button: QPushButton | None = None
        if detail:
            self.detail_view = QPlainTextEdit(detail, self)
            self.detail_view.setReadOnly(True)
            theme.apply_fixed_font(self.detail_view)
            self.detail_view.setAccessibleName(self.tr("Technical details"))
            self.detail_view.setVisible(False)
            self.detail_button = QPushButton(self.tr("Show &details"), self)
            self.detail_button.setAccessibleName(self.tr("Show details"))
            self.detail_button.setCheckable(True)
            self.detail_button.setAutoDefault(False)
            self.detail_button.toggled.connect(self._toggle_detail)
            lay.addWidget(self.detail_view)

        self.buttons: list[QPushButton] = []
        if actions or self.detail_button is not None:
            row = QHBoxLayout()
            for a in actions:
                btn = make_button(a, self)
                self.buttons.append(btn)
                row.addWidget(btn)
            if self.detail_button is not None:
                row.addWidget(self.detail_button)
            row.addStretch(1)
            lay.addLayout(row)

        word = self.badge.kind_word(kind)
        self.setAccessibleName(f"{word}: {title}")
        desc = " ".join(part for part in (body, code or "") if part)
        self.setAccessibleDescription(desc)

    def title(self) -> str:
        return self.title_label.text()

    def body(self) -> str:
        return self.body_label.text()

    def _toggle_detail(self, on: bool) -> None:
        if self.detail_view is None or self.detail_button is None:
            return
        self.detail_view.setVisible(on)
        self.detail_button.setText(self.tr("Hide &details") if on else self.tr("Show &details"))
        self.detail_button.setAccessibleName(plain(self.detail_button.text()))

    @classmethod
    def from_error(
        cls, err: ButterEyeError, actions: Sequence[Action] = (), parent: QWidget | None = None
    ) -> Banner:
        if isinstance(err, OperationCancelled):
            return cls("off", cls.tr_static("Cancelled"), actions=actions, parent=parent)
        kind: BadgeKind = "info" if isinstance(err, NotAvailable) else "blocking"
        return cls(
            kind,
            render(err.cause),
            render(err.fix) if err.fix is not None else "",
            err.code.code,
            err.commands,
            actions,
            parent,
            detail=err.detail,
        )

    @staticmethod
    def tr_static(text: str) -> str:
        return QCoreApplication.translate("Banner", text)


__all__ = ["Action", "Banner", "command_name", "make_button"]
