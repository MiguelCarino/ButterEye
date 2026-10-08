# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The page registry (docs/design/GUI.md §1, §4.1).

Pages are referenced by lazy import strings, so a page written by another unit
plugs in by existing, and a missing module shows ``MissingPage`` instead. The
order is the sidebar order and the Ctrl+1...Ctrl+7 order.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication

from buttereye.core.api import Feature
from buttereye.gui.pages.base import Page

_log = logging.getLogger(__name__)

TITLE_CONTEXT = "PageRegistry"


@dataclass(frozen=True, slots=True)
class PageSpec:
    page_id: str
    target: str  # "package.module:ClassName"
    title: str  # English source text; translated in context TITLE_CONTEXT
    features: tuple[Feature, ...]

    @property
    def module(self) -> str:
        return self.target.partition(":")[0]

    @property
    def attr(self) -> str:
        return self.target.partition(":")[2]

    def translated_title(self) -> str:
        return QCoreApplication.translate(TITLE_CONTEXT, self.title)  # i18n: dynamic (NOOP below)


PAGE_REGISTRY: tuple[PageSpec, ...] = (
    PageSpec(
        "sessions",
        "buttereye.gui.pages.sessions:SessionsPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "Play")),
        (Feature.LIVE, Feature.ATTACH, Feature.DISCOVER, Feature.ORPHANS),
    ),
    PageSpec(
        "profiles",
        "buttereye.gui.pages.profiles:ProfilesPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "Profiles & Rules")),
        (Feature.CONFIG,),
    ),
    PageSpec(
        "render",
        "buttereye.gui.pages.render:RenderPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "Render")),
        (Feature.RENDER, Feature.RENDER_HDR10),
    ),
    PageSpec(
        "bench",
        "buttereye.gui.pages.bench:BenchPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "Benchmark")),
        (Feature.BENCH,),
    ),
    PageSpec(
        "system",
        "buttereye.gui.pages.system:SystemPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "System")),
        (Feature.DOCTOR, Feature.HARDWARE, Feature.REPORT),
    ),
    PageSpec(
        "storage",
        "buttereye.gui.pages.storage:StoragePage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "Storage")),
        (Feature.STORAGE, Feature.CLEAN, Feature.MODELS, Feature.MODEL_DOWNLOADS),
    ),
    PageSpec(
        "about",
        "buttereye.gui.pages.about:AboutPage",
        str(QT_TRANSLATE_NOOP("PageRegistry", "About")),
        (Feature.LICENCES,),
    ),
)


class PageMissing(LookupError):
    """The page's module (or a Page class in it) is not part of this build."""


def spec_for(page_id: str) -> PageSpec:
    for spec in PAGE_REGISTRY:
        if spec.page_id == page_id:
            return spec
    raise KeyError(page_id)


def module_present(spec: PageSpec) -> bool:
    try:
        return importlib.util.find_spec(spec.module) is not None
    except ImportError, ValueError:
        return False


def load_page_class(spec: PageSpec) -> type[Page]:
    """Import the page class. ``PageMissing`` when the module is absent; any other
    import failure propagates (a real bug, shown as an error panel)."""
    if not module_present(spec):
        raise PageMissing(spec.module)
    mod = importlib.import_module(spec.module)
    cls = getattr(mod, spec.attr, None)
    if isinstance(cls, type) and issubclass(cls, Page):
        return cls
    # Tolerate a different class name: the one Page subclass with this page_id.
    found = [
        obj
        for _, obj in inspect.getmembers(mod, inspect.isclass)
        if issubclass(obj, Page) and obj.__module__ == mod.__name__ and obj.page_id == spec.page_id
    ]
    if len(found) == 1:
        _log.warning("page %s: using %s instead of %s", spec.page_id, found[0].__name__, spec.attr)
        return found[0]
    raise PageMissing(f"{spec.target} (no Page subclass with page_id {spec.page_id!r})")


__all__ = [
    "PAGE_REGISTRY",
    "PageMissing",
    "PageSpec",
    "TITLE_CONTEXT",
    "load_page_class",
    "module_present",
    "spec_for",
]
