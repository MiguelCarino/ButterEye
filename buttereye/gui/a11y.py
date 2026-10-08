# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Accessibility helpers shared by every page and widget (docs/design/GUI.md §4.0, U5).

- ``labelled()`` builds the visible label for an input, sets its buddy (so the
  ``&`` mnemonic focuses the input) and the input's accessible name.
- ``announce()`` posts a ``QAccessibleAnnouncementEvent`` (Qt 6.8+) so a screen
  reader speaks a status change. Use it for transitions only, never for counters.
- ``require_name()`` refuses a widget without an accessible name.
- ``message_box()`` builds every ``QMessageBox``: plain text only, so a media
  title or file name with markup can't reword a (destructive) prompt.

Listeners registered with ``add_announcement_listener`` see every announcement
(the shell mirrors them into the status bar; tests record them).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import cast

from PySide6.QtCore import QCoreApplication, QObject, Qt, QTranslator
from PySide6.QtGui import QAccessible, QAccessibleAnnouncementEvent, QFont
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget

AnnouncementListener = Callable[[QWidget, str, bool], None]

_listeners: list[AnnouncementListener] = []

#: Dynamic property holding a heading's size factor; ``theme.rescale_fonts``
#: re-applies it when the application font changes.
HEADING_PROPERTY = "buttereyeHeadingFactor"

_MNEMONIC = re.compile(r"&(.)")


def plain(text: str) -> str:
    """``text`` without ``&`` mnemonics (``&&`` stays a literal ``&``) or a trailing colon."""
    out = _MNEMONIC.sub(lambda m: m.group(1), text)
    return out.strip().removesuffix(":").strip()


def labelled(
    text: str, w: QWidget, *, description: str | None = None, wrap: bool = False
) -> tuple[QLabel, QWidget]:
    """Return ``(label, w)`` with the label's buddy set to ``w``.

    ``text`` is the visible, translated label and may carry an ``&`` mnemonic.
    ``w`` gets ``text`` (without mnemonic/colon) as its accessible name, and
    ``description`` as its accessible description when given.

    Form labels do not wrap by default: in a ``QGridLayout`` row the height comes
    from the field, so a wrapped label is clipped (at 1.5x font "Target rate
    (fps)" lost its top and bottom). Unwrapped, the layout reserves the label's
    full width. Pass ``wrap=True`` only for long explanatory labels, in layouts
    that honour height-for-width.
    """
    label = QLabel(text)
    label.setBuddy(w)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(wrap)
    name = plain(text)
    if name:
        w.setAccessibleName(name)
    if description is not None:
        w.setAccessibleDescription(description)
    return label, w


def message_box(
    parent: QWidget | None,
    *,
    icon: QMessageBox.Icon = QMessageBox.Icon.NoIcon,
    title: str = "",
    text: str = "",
    informative: str = "",
) -> QMessageBox:
    """A ``QMessageBox`` that shows ``text``/``informative`` as plain text.

    ``QMessageBox`` defaults to ``Qt::AutoText``: a title such as
    ``<b>a</b><!--`` would render as markup and could hide or reword the rest
    of the question. The informative text follows the box's format, so it is
    plain too. Use this for every message box (a test enforces it).
    """
    box = QMessageBox(parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setIcon(icon)
    if title:
        box.setWindowTitle(title)
    if text:
        box.setText(text)
    if informative:
        box.setInformativeText(informative)
    return box


def require_name(w: QWidget) -> QWidget:
    """Return ``w``; raise ``ValueError`` if it has no accessible name."""
    if not w.accessibleName().strip():
        raise ValueError(f"{type(w).__name__} {w.objectName()!r} has no accessible name")
    return w


def announce(w: QWidget, text: str, *, assertive: bool = False) -> None:
    """Ask assistive technology to speak ``text`` on behalf of ``w``.

    Polite announcements wait for the screen reader to finish; assertive ones
    interrupt (use for failures and health faults only).
    """
    if not text:
        return
    ev = QAccessibleAnnouncementEvent(w, text)
    ev.setPoliteness(
        QAccessible.AnnouncementPoliteness.Assertive
        if assertive
        else QAccessible.AnnouncementPoliteness.Polite
    )
    QAccessible.updateAccessibility(ev)
    for listener in tuple(_listeners):
        listener(w, text, assertive)


def add_announcement_listener(listener: AnnouncementListener) -> Callable[[], None]:
    """Register ``listener``; returns a function that removes it again."""
    _listeners.append(listener)

    def remove() -> None:
        if listener in _listeners:
            _listeners.remove(listener)

    return remove


def selectable(label: QLabel, *, name: str | None = None) -> QLabel:
    """Make ``label`` selectable by mouse and keyboard (codes, paths, commands).

    A keyboard-selectable label takes Tab focus, so it also gets an accessible
    name (``name`` or its own text).
    """
    label.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard
    )
    label.setAccessibleName(name if name is not None else label.text())
    return label


def heading(label: QLabel, *, factor: float = 1.25) -> QLabel:
    """Scale ``label``'s font to ``factor`` × the current point size, bold (§4.0).

    The factor is kept on the label so the heading follows later application
    font changes (``theme.rescale_fonts``).
    """
    label.setProperty(HEADING_PROPERTY, float(factor))
    font = QFont(label.font())
    size = font.pointSizeF()
    if size > 0:
        font.setPointSizeF(size * factor)
    font.setBold(True)
    label.setFont(font)
    return label


class SourcePlurals(QTranslator):
    """English plural forms for numerus source strings (``"%n player(s)"``).

    No ``.qm`` can be built yet (no ``lrelease``, GUI.md §6), and without one Qt
    shows the source with only ``%n`` filled in ("1 player(s)"). Installed
    before any real catalogue, so a loaded translation always wins; it answers
    only numerus lookups whose source carries "(s)".
    """

    def translate(
        self, context: str, source: str, disambiguation: str | None = None, n: int = -1
    ) -> str | None:
        if n < 0 or "(s)" not in source:
            return None  # a null QString: "no translation here", Qt keeps looking
        return source.replace("(s)", "" if n == 1 else "s").replace("%n", str(n))

    def isEmpty(self) -> bool:  # noqa: N802 - Qt API
        return False


def install_source_plurals(app: QCoreApplication) -> QTranslator:
    """Install ``SourcePlurals`` once (call before loading real catalogues)."""
    existing = app.findChild(SourcePlurals)
    if existing is not None:
        return existing
    plurals = SourcePlurals(cast(QObject, app))
    app.installTranslator(plurals)
    return plurals


def tr(text: str) -> str:
    """Module-level translation in the ``a11y`` context."""
    return QCoreApplication.translate("a11y", text)


__all__ = [
    "AnnouncementListener",
    "HEADING_PROPERTY",
    "SourcePlurals",
    "install_source_plurals",
    "add_announcement_listener",
    "announce",
    "heading",
    "labelled",
    "message_box",
    "plain",
    "require_name",
    "selectable",
    "tr",
]
