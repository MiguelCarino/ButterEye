# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Test-only pages for ``test_shell`` (custom registries)."""

from __future__ import annotations

from typing import ClassVar

from PySide6.QtWidgets import QLabel, QLineEdit, QVBoxLayout, QWidget

from buttereye.core.api import Event, Feature
from buttereye.gui.a11y import labelled
from buttereye.gui.context import GuiContext
from buttereye.gui.pages.base import Page, PageAction


class CountingPage(Page):
    page_id: ClassVar[str] = "counting"
    features: ClassVar[tuple[Feature, ...]] = ()

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle("Counting")
        self.refreshes = 0
        self.events: list[Event] = []
        self.handled: list[PageAction] = []
        lay = QVBoxLayout(self)
        label, self.edit = labelled("&Name", QLineEdit(self))
        lay.addWidget(label)
        lay.addWidget(self.edit)
        lay.addWidget(QLabel("plain text"))

    def refresh(self) -> None:
        self.refreshes += 1

    def on_event(self, ev: Event) -> None:
        self.events.append(ev)

    def handle_action(self, action: PageAction) -> bool:
        self.handled.append(action)
        return True


class DirtyPage(CountingPage):
    page_id: ClassVar[str] = "dirty"

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle("Dirty")
        self.dirty = True

    def has_unsaved_changes(self) -> bool:
        return self.dirty


class DecoratedDirtyPage(DirtyPage):
    """Like ProfilesPage: ``title()`` carries the unsaved-changes decoration."""

    page_id: ClassVar[str] = "decorated"

    def title(self) -> str:
        return "Dirty — unsaved changes *"


class BrokenPage(Page):
    page_id: ClassVar[str] = "broken"

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        raise RuntimeError("cannot build")


class Renamed(Page):
    """A page whose class name differs from the registry target."""

    page_id: ClassVar[str] = "renamed"
