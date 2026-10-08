# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Details dialog behind the simple window (docs/design/GUI.md §12).

Tabs reuse the existing pages: System, Speed test (the Benchmark page),
Storage and About. Error codes, findings and commands live here, never in the
simple window. The Speed test tab hides "Use recommended": in the simple
design the profile comes from the window's choices, not from bench rules.
Esc closes the dialog.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, QSettings, QSize, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import Event, SourceFacts, internal_error
from buttereye.gui import a11y
from buttereye.gui.bridge import CoreBridge
from buttereye.gui.context import GuiContext
from buttereye.gui.pages import PageMissing, load_page_class, spec_for
from buttereye.gui.pages.base import MissingPage, Page

_log = logging.getLogger(__name__)

CONTEXT = "DetailsDialog"

#: (page id, tab title) in tab order.
TABS: tuple[tuple[str, str], ...] = (
    ("system", str(QT_TRANSLATE_NOOP("DetailsDialog", "System"))),
    ("bench", str(QT_TRANSLATE_NOOP("DetailsDialog", "Speed test"))),
    ("storage", str(QT_TRANSLATE_NOOP("DetailsDialog", "Storage"))),
    ("about", str(QT_TRANSLATE_NOOP("DetailsDialog", "About"))),
)

DEFAULT_SIZE = QSize(860, 640)


def _title(text: str) -> str:
    return QCoreApplication.translate(CONTEXT, text)  # i18n: dynamic (NOOP above)


class DetailsDialog(QDialog):
    """System information, the speed test, storage and licences."""

    def __init__(
        self,
        bridge: CoreBridge,
        settings: QSettings,
        *,
        facts: Callable[[], SourceFacts | None] = lambda: None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("detailsDialog")
        self.setWindowTitle(self.tr("ButterEye details"))
        self.bridge = bridge
        self.ctx = GuiContext(
            bridge,
            settings,
            announce=self._announce,
            status=self._set_status,
            go=self.go,
            facts=facts,
        )
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        self.tabs.setObjectName("detailsTabs")
        self.tabs.setAccessibleName(self.tr("Details"))
        self.tabs.setDocumentMode(True)
        lay.addWidget(self.tabs, 1)
        self.pages: dict[str, Page] = {}
        self._shown: set[str] = set()
        for page_id, source in TABS:
            title = _title(source)  # i18n: dynamic (QT_TRANSLATE_NOOP in TABS)
            page = self._make_page(page_id, title)
            self.pages[page_id] = page
            scroll = QScrollArea(self.tabs)
            scroll.setObjectName(f"details.{page_id}")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            scroll.setWidget(page)
            scroll.setAccessibleName(title)
            self.tabs.addTab(scroll, title)
        self._simplify_bench()

        self.status_label = QLabel(self)
        self.status_label.setObjectName("detailsStatus")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        self.status_label.setAccessibleName(self.tr("Status"))
        lay.addWidget(self.status_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.setObjectName("detailsButtons")
        close = buttons.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setObjectName("detailsClose")
            close.setAccessibleName(self.tr("Close"))
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self.tabs.currentChanged.connect(self._on_tab)
        self._remove_listener = a11y.add_announcement_listener(self._mirror)
        self.destroyed.connect(lambda _o=None: self._remove_listener())
        self.resize(DEFAULT_SIZE)
        self._on_tab(self.tabs.currentIndex())

    def _make_page(self, page_id: str, title: str) -> Page:
        spec = spec_for(page_id)
        try:
            cls = load_page_class(spec)
        except PageMissing:
            return MissingPage(self.ctx, page_id, title)
        except Exception as exc:
            _log.exception("page %s failed to import", page_id)
            return MissingPage(self.ctx, page_id, title, internal_error(exc))
        try:
            page = cls(self.ctx)
        except Exception as exc:
            _log.exception("page %s failed to build", page_id)
            return MissingPage(self.ctx, page_id, title, internal_error(exc))
        page.setWindowTitle(title)
        return page

    def _simplify_bench(self) -> None:
        bench = self.pages.get("bench")
        for name in ("apply_button", "apply_hint"):
            w = getattr(bench, name, None)
            if isinstance(w, QWidget):
                w.setVisible(False)

    # ------------------------------------------------------------------ tabs
    def page_ids(self) -> tuple[str, ...]:
        return tuple(pid for pid, _ in TABS)

    def current_page_id(self) -> str:
        i = self.tabs.currentIndex()
        return TABS[i][0] if 0 <= i < len(TABS) else ""

    def go(self, page_id: str) -> None:
        for i, (pid, _) in enumerate(TABS):
            if pid == page_id:
                self.tabs.setCurrentIndex(i)
                return
        _log.info("details: no tab %r", page_id)

    def _on_tab(self, index: int) -> None:
        if not 0 <= index < len(TABS):
            return
        pid = TABS[index][0]
        if pid in self._shown:
            return
        self._shown.add(pid)
        try:
            self.pages[pid].refresh()
        except Exception:
            _log.exception("refresh of %s failed", pid)

    def on_event(self, ev: Event) -> None:
        for page in self.pages.values():
            try:
                page.on_event(ev)
            except Exception:
                _log.exception("%s.on_event failed", type(page).__name__)

    # ------------------------------------------------------------------ status
    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _announce(self, text: str, assertive: bool) -> None:
        a11y.announce(self.status_label, text, assertive=assertive)

    def _mirror(self, w: QWidget, text: str, _assertive: bool) -> None:
        if shiboken6.isValid(self) and shiboken6.isValid(w) and (w is self or self.isAncestorOf(w)):
            self._set_status(text)


__all__ = ["DetailsDialog", "TABS"]
