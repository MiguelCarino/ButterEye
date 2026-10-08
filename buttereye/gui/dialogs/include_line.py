# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Include-line dialog (docs/design/GUI.md §4.3, R4; SCOPE §4.6, §4.7.5).

Shows the exact ``include=`` line that makes ButterEye's ``[buttereye]`` mpv
profile available in a normal ``mpv`` (``--profile=buttereye``), with [Copy line].
The line defines the profile only: it does not interpolate. Smooth motion in an
mpv the user started needs attaching (``Feature.ATTACH``), which is not in this
build, and the text says so instead of promising smoothing.
[Add it for me...] opens a confirmation that shows
the one-line change to ``~/.config/mpv/mpv.conf``; only an explicit [Add the
line] there calls ``add_include_to_mpv_conf(consent=True)``. Nothing is written
on open, on copy or on cancel.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEye,
    ButterEyeError,
    NotAvailable,
    OperationCancelled,
    Paths,
    render,
)
from buttereye.gui import theme
from buttereye.gui.a11y import announce, heading, labelled
from buttereye.gui.bridge import CoreBridge
from buttereye.gui.dialogs.fit import FitDialog
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.copy_field import CopyField


async def _paths(core: ButterEye) -> Paths:
    return core.paths()


def _home_short(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


class IncludeConfirmDialog(FitDialog):
    """The consent step: shows the file and the one line that will be added."""

    def __init__(self, line: str, target: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("includeConfirm")
        self.setWindowTitle(self.tr("Add the line to mpv.conf?"))
        self.setModal(True)
        lay = QVBoxLayout(self)
        intro = QLabel(
            self.tr("ButterEye will add one line to the end of {path}:").format(path=target), self
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(intro)
        self.diff = QPlainTextEdit(f"+{line}", self)
        self.diff.setReadOnly(True)
        self.diff.setTabChangesFocus(True)
        theme.apply_fixed_font(self.diff)
        self.diff.setMaximumBlockCount(4)
        diff_label, _ = labelled(self.tr("Change to {path}").format(path=target), self.diff)
        lay.addWidget(diff_label)
        lay.addWidget(self.diff)
        note = QLabel(
            self.tr(
                "Nothing else in the file changes. Remove the line later to undo it. "
                "mpv reads it the next time it starts."
            ),
            self,
        )
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(note)
        box = QDialogButtonBox(self)
        self.add_button = QPushButton(self.tr("&Add the line"), self)
        self.add_button.setObjectName("includeConfirmAdd")
        self.add_button.setAccessibleName(self.tr("Add the line"))
        self.cancel_button = QPushButton(self.tr("Cancel"), self)
        self.cancel_button.setObjectName("includeConfirmCancel")
        self.cancel_button.setAccessibleName(self.tr("Cancel"))
        box.addButton(self.add_button, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self.cancel_button.setDefault(True)  # consent is never the default


class IncludeLineDialog(FitDialog):
    """``IncludeLineDialog(bridge, parent)``; ``added`` carries the diff applied."""

    added = Signal(str)

    def __init__(self, bridge: CoreBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("includeLineDialog")
        self.bridge = bridge
        self.setWindowTitle(self.tr("ButterEye's profile in your normal mpv"))
        self._line: str | None = None
        self._target: str = "~/.config/mpv/mpv.conf"
        self._busy = False

        lay = QVBoxLayout(self)
        title = heading(QLabel(self.tr("ButterEye's profile in your normal mpv"), self))
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        lay.addWidget(title)
        self.explain = QLabel(
            self.tr(
                "This line makes ButterEye's mpv profile available in the mpv you start "
                "yourself: start mpv with --profile=buttereye to use it. It does not smooth "
                "video on its own; smooth motion in an mpv you start yourself needs attaching, "
                "which isn't in this build yet. Use Open and Play for smooth motion.\n"
                "ButterEye never changes mpv.conf unless you ask it to."
            ),
            self,
        )
        self.explain.setWordWrap(True)
        self.explain.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.explain)

        self.field = CopyField("", accessible_name=self.tr("Include line"), parent=self)
        field_label = QLabel(self.tr("Include line"), self)
        field_label.setBuddy(self.field.line)
        lay.addWidget(field_label)
        lay.addWidget(self.field)

        row = QHBoxLayout()
        self.copy_button = QPushButton(self.tr("Copy &line"), self)
        self.copy_button.setObjectName("includeCopy")
        self.copy_button.setAccessibleName(self.tr("Copy line"))
        self.copy_button.setAutoDefault(False)
        self.copy_button.clicked.connect(self.copy_line)
        self.add_button = QPushButton(self.tr("Add it for &me…"), self)
        self.add_button.setObjectName("includeAdd")
        self.add_button.setAccessibleName(self.tr("Add it for me"))
        self.add_button.setAutoDefault(False)
        self.add_button.clicked.connect(self.ask_and_add)
        row.addWidget(self.copy_button)
        row.addWidget(self.add_button)
        row.addStretch(1)
        lay.addLayout(row)

        self.result_label = QLabel(self)
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.hide()
        lay.addWidget(self.result_label)
        self._banner_lay = QVBoxLayout()
        lay.addLayout(self._banner_lay)
        self.banner: Banner | None = None

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        close = box.button(QDialogButtonBox.StandardButton.Close)
        if close is not None:
            close.setObjectName("includeClose")
            close.setAccessibleName(self.tr("Close"))
        box.rejected.connect(self.reject)
        lay.addWidget(box)

        self._set_enabled(False)
        bridge.call(lambda core: core.include_line(), owner=self, ok=self._on_line)
        bridge.call(_paths, owner=self, ok=self._on_paths, err=lambda _e: None)

    # ------------------------------------------------------------------ API
    def line(self) -> str | None:
        return self._line

    def copy_line(self) -> None:
        if self._line is None:
            return
        self.field.copy()
        announce(self, self.tr("Include line copied."))

    def ask_and_add(self) -> None:
        """Ask for consent; write only after [Add the line]."""
        if self._line is None or self._busy:
            return
        dlg = IncludeConfirmDialog(self._line, self._target, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            announce(self, self.tr("Nothing was changed."))
            return
        self._busy = True
        self.add_button.setEnabled(False)
        self.bridge.call(
            lambda core: core.add_include_to_mpv_conf(consent=True),
            owner=self,
            ok=self._on_added,
            err=self.show_error,
        )

    def show_error(self, err: ButterEyeError) -> None:
        self._busy = False
        self.add_button.setEnabled(self._line is not None)
        self._clear_banner()
        if isinstance(err, OperationCancelled):
            return
        if isinstance(err, NotAvailable):
            self.banner = Banner(
                "info",
                self.tr("Adding the line isn't in this build yet."),
                self.tr("Copy the line and add it to {path} yourself.").format(path=self._target),
                err.code.code,
                parent=self,
            )
        else:
            self.banner = Banner.from_error(err, parent=self)
        self._banner_lay.addWidget(self.banner)
        announce(self, render(err.cause), assertive=True)

    # ------------------------------------------------------------- internals
    def _set_enabled(self, on: bool) -> None:
        self.copy_button.setEnabled(on)
        self.add_button.setEnabled(on)

    def _clear_banner(self) -> None:
        if self.banner is not None:
            self.banner.hide()
            self.banner.deleteLater()
            self.banner = None

    def _on_line(self, line: str) -> None:
        self._line = line
        self.field.set_text(line)
        self._set_enabled(True)

    def _on_paths(self, paths: Paths) -> None:
        self._target = _home_short(paths.user_mpv_conf)

    def _on_added(self, diff: str) -> None:
        self._busy = False
        self._clear_banner()
        if diff:
            text = self.tr("Added to {path}. mpv uses it the next time it starts.").format(
                path=self._target
            )
        else:
            text = self.tr("The line is already in {path}. Nothing was changed.").format(
                path=self._target
            )
        self.result_label.setText(text)
        self.result_label.show()
        self.add_button.setEnabled(False)
        announce(self, text)
        self.added.emit(diff)


__all__ = ["IncludeConfirmDialog", "IncludeLineDialog"]
