# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Page base class (docs/design/GUI.md §4.0) and the shell's built-in panels.

Every page is a ``Page`` subclass with a class-level ``page_id`` and gating
``features``. The main window wraps each page in a ``QScrollArea``, calls
``refresh()`` on first show and on F5, and routes every core event to
``on_event()``.

Strings shared by every page use ``QCoreApplication.translate("Page", ...)``:
PySide6's ``self.tr()`` uses the *runtime* class name as the context, so a base
class string would land in each subclass's context and never be found.

Optional hooks (additive to §4.0, all with safe defaults):

- ``handle_action(action)`` lets menu entries and global keys (Ctrl+O, Ctrl+T,
  Ctrl+D, ...) act on a page; return ``True`` when handled. Pages must not bind
  the global keys themselves (ambiguous shortcuts fire nothing).
- ``cli_hint()`` returns the page's "Equivalent command" for Ctrl+Shift+C.
- ``title_changed`` is emitted when ``title()`` changes (e.g. unsaved marker).
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from buttereye.core.api import (
    ButterEyeError,
    CapState,
    CommandHint,
    ErrorCode,
    Event,
    Feature,
    OperationCancelled,
    Reason,
    render,
)
from buttereye.gui.a11y import heading, selectable
from buttereye.gui.widgets.state_panel import StatePanel

if TYPE_CHECKING:
    from buttereye.gui.context import GuiContext

_log = logging.getLogger(__name__)

#: Reasons that mean "this build cannot do it at all" (the page shows only the
#: unavailable panel). Other unavailable reasons are advisory (§11.1).
GATING_REASONS = frozenset({Reason.NOT_IMPLEMENTED, Reason.NOTHING_LISTED})


def _t(text: str) -> str:
    return QCoreApplication.translate("Page", text)


class PageAction(StrEnum):
    """Shell commands a page may implement through ``Page.handle_action``."""

    OPEN_AND_PLAY = "play.open"  # File > Open and Play... (Ctrl+O)          sessions
    ATTACH = "attach"  # File > Attach to Running mpv... (Ctrl+Shift+A)       sessions
    TOGGLE_INTERPOLATION = "live.toggle"  # Playback > Interpolation On/Off (Ctrl+T)
    DETACH = "detach"  # Playback > Detach (Ctrl+D)                           sessions
    DISABLE_AND_DETACH = "detach.disable"  # Playback > Disable and Detach (Ctrl+Shift+D)
    RUN_CHECKS = "doctor"  # Tools > Run Checks                               system
    CREATE_REPORT = "doctor.report"  # Tools > Create Bug Report...            system
    SHOW_LICENCES = "licence"  # Help > Licences                              about
    SHOW_ABOUT = "version"  # Help > About (F1)                               about


class Page(QWidget):
    """Base class of every sidebar page (§4.0)."""

    page_id: ClassVar[str] = ""
    features: ClassVar[tuple[Feature, ...]] = ()

    title_changed = Signal()

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx

    # ---- §4.0 surface ----
    def title(self) -> str:
        """Translated page title (shown in the window title)."""
        return self.windowTitle() or type(self).__name__

    def refresh(self) -> None:
        """Reload data (F5); also called on first show."""

    def on_event(self, ev: Event) -> None:
        """Core events, in order, routed by the main window."""

    def show_error(self, err: ButterEyeError) -> None:
        """Default error sink for ``CoreBridge`` replies (§3 rule 3).

        ``OperationCancelled`` is neutral: a status line, never an error banner.
        """
        if isinstance(err, OperationCancelled):
            self.ctx.status(_t("Cancelled"))
            return
        panel = self.state_panel()
        if panel is not None:
            panel.show_error(err)
        else:
            self.ctx.status(f"{err.code.code}: {render(err.cause)}")
        self.ctx.announce(_t("Error: {cause}").format(cause=render(err.cause)), assertive=True)

    def has_unsaved_changes(self) -> bool:
        return False

    # ---- optional hooks ----
    def handle_action(self, action: PageAction) -> bool:
        """Run a shell command on this page; ``False`` = not handled here."""
        return False

    def cli_hint(self) -> CommandHint | None:
        """The page's equivalent CLI command (Ctrl+Shift+C), if any."""
        return None

    # ---- helpers ----
    def state_panel(self) -> StatePanel | None:
        """The page body's ``StatePanel`` (first one found among the children)."""
        return self.findChild(StatePanel)

    def unavailable_feature(self) -> tuple[Feature, CapState] | None:
        """The first gating feature this build cannot provide, if any."""
        caps = self.ctx.bridge.capabilities
        if caps is None:
            return None
        for f in self.features:
            st = caps.states.get(f)
            if st is not None and not st.available and st.reason in GATING_REASONS:
                return f, st
        return None


class MissingPage(Page):
    """Shown when a page module is not part of this build (U4 is testable alone)."""

    def __init__(
        self,
        ctx: GuiContext,
        page_id: str,
        title: str,
        error: ButterEyeError | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(ctx, parent)
        self._page_id = page_id
        self.setWindowTitle(title)
        self.panel = StatePanel(self)
        lay = QVBoxLayout(self)
        lay.addWidget(self.panel)
        if error is not None:
            # The module exists but failed to import: a bug, shown with its code.
            self.panel.show_error(error)
            return
        content = self.panel.content
        cl = QVBoxLayout(content)
        self.headline = heading(QLabel(_t("Page not in this build"), content))
        self.headline.setTextFormat(Qt.TextFormat.PlainText)
        self.body = QLabel(
            _t("This part of ButterEye isn't included in this build yet. Nothing is shown here."),
            content,
        )
        self.body.setTextFormat(Qt.TextFormat.PlainText)
        self.body.setWordWrap(True)
        self.code = selectable(
            QLabel(ErrorCode.NOT_IMPLEMENTED.code, content),
            name=_t("Error code {code}").format(code=ErrorCode.NOT_IMPLEMENTED.code),
        )
        cl.addWidget(self.headline)
        cl.addWidget(self.body)
        cl.addWidget(self.code)
        cl.addStretch(1)
        self.panel.show_content()

    @property
    def missing_page_id(self) -> str:
        return self._page_id

    def title(self) -> str:
        return self.windowTitle()


__all__ = ["GATING_REASONS", "MissingPage", "Page", "PageAction"]
