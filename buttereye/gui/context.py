# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``GuiContext``: what the shell hands to every page (docs/design/GUI.md §4.0)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QSettings

from buttereye.core.api import SourceFacts

if TYPE_CHECKING:
    from buttereye.gui.bridge import CoreBridge


class GuiContext:
    """Plain object, GUI thread only.

    ``announce`` speaks a status change through the screen reader and mirrors it
    in the status bar; ``status`` only sets the status-bar text; ``go`` switches
    to a page by id; ``current_session_facts`` is the selected live session's
    source facts (from ``SessionsPage.facts_changed``), or ``None``.
    """

    __slots__ = ("bridge", "settings", "_announce", "_status", "_go", "_facts")

    def __init__(
        self,
        bridge: CoreBridge,
        settings: QSettings,
        *,
        announce: Callable[[str, bool], None],
        status: Callable[[str], None],
        go: Callable[[str], None],
        facts: Callable[[], SourceFacts | None],
    ) -> None:
        self.bridge = bridge
        self.settings = settings
        self._announce = announce
        self._status = status
        self._go = go
        self._facts = facts

    def announce(self, text: str, *, assertive: bool = False) -> None:
        self._announce(text, assertive)

    def status(self, text: str) -> None:
        self._status(text)

    def go(self, page_id: str) -> None:
        self._go(page_id)

    def current_session_facts(self) -> SourceFacts | None:
        return self._facts()


__all__ = ["GuiContext"]
