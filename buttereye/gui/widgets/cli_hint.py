# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
""" "Equivalent command" line for CLI parity (SCOPE §4.1, GUI.md §2.6).

Shows a ``CommandHint`` in a ``CopyField``. A visible note (also in the field's
accessible description) says when the command can't be run:

- no ``buttereye`` command-line tool in this build (``cli_available()`` is
  False): every hint says so, whatever its spec status; the note disappears by
  itself once the CLI is installed;
- otherwise, commands still marked proposed carry the proposed note (none are since
  SCOPE §4.1 adopted them on 2026-10-07).

Copy stays available either way (the text is what the command will look like).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from buttereye.core.api import CLI_MISSING_NOTE, CommandHint, cli_available, render
from buttereye.gui.widgets.copy_field import CopyField


class CliHint(QWidget):
    def __init__(self, hint: CommandHint | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hint: CommandHint | None = None
        self.caption = QLabel(self.tr("Equivalent command"), self)
        self.field = CopyField("", accessible_name=self.tr("Equivalent command"), parent=self)
        self.caption.setBuddy(self.field.line)
        self.note = QLabel(self)
        self.note.setTextFormat(Qt.TextFormat.PlainText)
        self.note.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.caption)
        lay.addWidget(self.field)
        lay.addWidget(self.note)
        self.setAccessibleName(self.tr("Equivalent command"))
        self.set_hint(hint)

    def set_hint(self, h: CommandHint | None) -> None:
        self._hint = h
        if h is None:
            self.field.set_text("")
            for w in (self.caption, self.field, self.note):
                w.setVisible(False)
            return
        self.field.set_text(h.text())
        note = self.note_text(h)
        self.note.setText(note or "")
        self.caption.setVisible(True)
        self.field.setVisible(True)
        self.note.setVisible(bool(note))
        self.field.line.setAccessibleDescription(h.text() + ("\n" + note if note else ""))

    def note_text(self, h: CommandHint) -> str | None:
        """Why ``h`` can't be run as shown, or ``None`` when it can."""
        if not cli_available():
            return render(CLI_MISSING_NOTE)
        if h.status == "proposed":
            return self.tr("Proposed: this command isn't in the command-line tool yet.")
        return None

    def hint(self) -> CommandHint | None:
        return self._hint

    def text(self) -> str | None:
        return None if self._hint is None else self._hint.text()

    def copy(self) -> bool:
        """Copy the command (Tools → Copy Equivalent Command); False when there is none."""
        if self._hint is None:
            return False
        self.field.copy()
        return True


__all__ = ["CliHint"]
