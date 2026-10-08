# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The five page states: empty / loading / error / unavailable / content (§4.0).

The controller picks the state explicitly; "no rows" never implies one.
Unavailable shows zero data rows: the §5.2 headline and body from
``unavailable_text()``, the BE code as selectable text, each command in a
``CopyField`` and the ``CliHint``. Only the current page takes space.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    CapState,
    CommandHint,
    ErrorCode,
    Feature,
    Reason,
    render,
    unavailable_text,
)
from buttereye.gui.a11y import heading, selectable
from buttereye.gui.widgets.banner import Action, Banner, command_name, make_button
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge

PanelState = Literal["empty", "loading", "error", "unavailable", "content"]


def _clear(layout: QVBoxLayout | QHBoxLayout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item is None:
            break
        w = item.widget()
        if w is not None:
            w.hide()
            w.deleteLater()
        sub = item.layout()
        if isinstance(sub, (QVBoxLayout, QHBoxLayout)):
            _clear(sub)


def split_headline(text: str) -> tuple[str, str]:
    """First line = headline, the rest = body (§11.7 item 1)."""
    head, _, body = text.partition("\n")
    return head.strip(), body.strip()


class StatePanel(QStackedWidget):
    def __init__(self, parent: QWidget | None = None, content: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state: PanelState = "content"

        # empty
        self._empty = QWidget(self)
        self._empty_lay = QVBoxLayout(self._empty)
        self.empty_label = QLabel(self._empty)
        self.empty_label.setWordWrap(True)
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self._empty_lay.addWidget(self.empty_label)
        self._empty_actions = QHBoxLayout()
        self._empty_lay.addLayout(self._empty_actions)
        self._empty_lay.addStretch(1)

        # loading
        self._loading = QWidget(self)
        lay = QVBoxLayout(self._loading)
        self.loading_label = QLabel(self._loading)
        self.loading_label.setWordWrap(True)
        self.loading_label.setTextFormat(Qt.TextFormat.PlainText)
        self.loading_bar = QProgressBar(self._loading)
        self.loading_bar.setRange(0, 0)
        self.loading_label.setBuddy(self.loading_bar)
        self.loading_cancel = QPushButton(self.tr("Cancel"), self._loading)
        self.loading_cancel.setAccessibleName(self.tr("Cancel"))
        self.loading_cancel.setAutoDefault(False)
        self.loading_cancel.clicked.connect(self._on_cancel)
        self._cancel_cb: Callable[[], None] | None = None
        row = QHBoxLayout()
        row.addWidget(self.loading_bar, 1)
        row.addWidget(self.loading_cancel)
        lay.addWidget(self.loading_label)
        lay.addLayout(row)
        lay.addStretch(1)

        # error
        self._error = QWidget(self)
        self._error_lay = QVBoxLayout(self._error)
        self.error_banner: Banner | None = None

        # unavailable
        self._unavail = QWidget(self)
        ul = QVBoxLayout(self._unavail)
        self.unavailable_badge = StatusBadge("info", "", self._unavail)
        heading(self.unavailable_badge.text_label)
        self.unavailable_body = QLabel(self._unavail)
        self.unavailable_body.setWordWrap(True)
        self.unavailable_body.setTextFormat(Qt.TextFormat.PlainText)
        self.unavailable_code = QLabel(self._unavail)
        selectable(self.unavailable_code)
        self._unavail_cmds = QVBoxLayout()
        self.unavailable_hint = CliHint(None, self._unavail)
        self._unavail_actions = QHBoxLayout()
        ul.addWidget(self.unavailable_badge)
        ul.addWidget(self.unavailable_body)
        ul.addWidget(self.unavailable_code)
        ul.addLayout(self._unavail_cmds)
        ul.addLayout(self._unavail_actions)
        ul.addWidget(self.unavailable_hint)
        ul.addStretch(1)
        self.unavailable_commands: list[CopyField] = []

        # content
        self.content: QWidget = content if content is not None else QWidget()
        self.content.setParent(self)
        self._content_policy = self.content.sizePolicy()

        self._pages: dict[PanelState, QWidget] = {
            "empty": self._empty,
            "loading": self._loading,
            "error": self._error,
            "unavailable": self._unavail,
            "content": self.content,
        }
        for page in self._pages.values():
            self.addWidget(page)
        self.currentChanged.connect(self._fit_current)
        self.show_content()

    # ------------------------------------------------------------------ API
    @property
    def state(self) -> PanelState:
        return self._state

    def show_empty(self, text: str, actions: Sequence[Action] = ()) -> None:
        self.empty_label.setText(text)
        self._empty.setAccessibleName(text)
        _clear(self._empty_actions)
        for a in actions:
            self._empty_actions.addWidget(make_button(a, self._empty))
        self._empty_actions.addStretch(1)
        self._set("empty")

    def show_loading(self, text: str, cancel: Callable[[], None] | None = None) -> None:
        self.loading_label.setText(text)
        self.loading_bar.setAccessibleName(text)
        self.loading_cancel.setAccessibleName(self.tr("Cancel: {what}").format(what=text))
        self.loading_cancel.setEnabled(True)
        self.loading_cancel.setVisible(cancel is not None)
        self._loading.setAccessibleName(text)
        self._cancel_cb = cancel
        self._set("loading")

    def show_error(self, err: ButterEyeError, *, actions: Sequence[Action] = ()) -> None:
        if self.error_banner is not None:
            self._error_lay.removeWidget(self.error_banner)
            self.error_banner.hide()
            self.error_banner.deleteLater()
        _clear(self._error_lay)
        self.error_banner = Banner.from_error(err, actions, self._error)
        self._error_lay.addWidget(self.error_banner)
        self._error_lay.addStretch(1)
        self._error.setAccessibleName(self.error_banner.accessibleName())
        self._set("error")

    def show_unavailable(
        self,
        feature: Feature,
        state: CapState,
        hint: CommandHint | None,
        *,
        actions: Sequence[Action] = (),
    ) -> None:
        headline, body = split_headline(render(unavailable_text(feature, state)))
        kind: BadgeKind = "degraded" if state.reason is Reason.MISSING_DEPENDENCY else "info"
        if state.reason is Reason.BLOCKED_BY_DOCTOR:
            kind = "blocking"
        self.unavailable_badge.set_state(kind, headline)
        self.unavailable_body.setText(body)
        self.unavailable_body.setVisible(bool(body))
        code = state.code
        if code is None and state.reason is Reason.NOT_IMPLEMENTED:
            code = ErrorCode.NOT_IMPLEMENTED
        code_text = code.code if code is not None else ""
        self.unavailable_code.setText(code_text)
        self.unavailable_code.setAccessibleName(self.tr("Error code {code}").format(code=code_text))
        self.unavailable_code.setVisible(bool(code_text))
        _clear(self._unavail_cmds)
        self.unavailable_commands = []
        n = len(state.commands)
        for i, c in enumerate(state.commands):
            field = CopyField(c, accessible_name=command_name(i, n), parent=self._unavail)
            self.unavailable_commands.append(field)
            self._unavail_cmds.addWidget(field)
        _clear(self._unavail_actions)
        for a in actions:
            self._unavail_actions.addWidget(make_button(a, self._unavail))
        if actions:
            self._unavail_actions.addStretch(1)
        self.unavailable_hint.set_hint(hint)
        self._unavail.setAccessibleName(headline)
        self._unavail.setAccessibleDescription(" ".join(t for t in (body, code_text) if t))
        self._set("unavailable")

    def show_content(self) -> None:
        self._set("content")

    def unavailable_texts(self) -> tuple[str, str, str]:
        """(headline, body, code) currently shown on the unavailable page."""
        return (
            self.unavailable_badge.text(),
            self.unavailable_body.text(),
            self.unavailable_code.text(),
        )

    # ------------------------------------------------------------- internals
    def _set(self, state: PanelState) -> None:
        self._state = state
        page = self._pages[state]
        name = page.accessibleName() if page is not self.content else ""
        self.setAccessibleName(name or self.content.accessibleName() or self.tr("Content"))
        if self.currentWidget() is page:
            self._fit_current()
        else:
            self.setCurrentWidget(page)

    def _fit_current(self, _index: int = -1) -> None:
        current = self.currentWidget()
        for page in self._pages.values():
            if page is not current:
                page.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
            elif page is self.content:
                page.setSizePolicy(self._content_policy)
            else:
                page.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.updateGeometry()

    def _on_cancel(self) -> None:
        cb = self._cancel_cb
        if cb is None:
            return
        self.loading_cancel.setEnabled(False)
        cb()


__all__ = ["PanelState", "StatePanel", "split_headline"]
