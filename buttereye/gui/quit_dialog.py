# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Close flow (docs/design/GUI.md §3 "Close flow", SCOPE §4.10).

- No live sessions and no running render: close at once.
- A running render: "A render is running. Quitting cancels it and deletes the
  partial file." [Cancel render and quit] [Keep window open] (default Keep).
- Live sessions: "Smooth playback keeps running in mpv after ButterEye closes."
  with the unchecked option "Turn interpolation off in those players"
  (checked -> ``DetachPolicy.DISABLE_FILTER``) [Close] [Keep window open].
- Then a modal "Detaching from N players..." while the core closes; players that
  could not be detached are listed afterwards.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    DetachPolicy,
    OpState,
    RenderJobState,
    SessionId,
    SessionSnapshot,
    ShutdownReport,
)
from buttereye.gui import a11y

Shutdown = Callable[[DetachPolicy, bool], ShutdownReport | None]

_ACTIVE_JOB = frozenset({OpState.RUNNING, OpState.CANCELLING})


def _t(text: str) -> str:
    return QCoreApplication.translate("QuitDialog", text)


@dataclass(frozen=True, slots=True)
class QuitNeeds:
    """What stands between the user and closing the window."""

    live: tuple[SessionSnapshot, ...]
    running_jobs: tuple[RenderJobState, ...]
    queued_jobs: tuple[RenderJobState, ...]

    @property
    def trivial(self) -> bool:
        return not self.live and not self.running_jobs


def quit_needs(sessions: Iterable[SessionSnapshot], jobs: Iterable[RenderJobState]) -> QuitNeeds:
    jobs = tuple(jobs)
    return QuitNeeds(
        live=tuple(s for s in sessions if not s.ended),
        running_jobs=tuple(j for j in jobs if j.state in _ACTIVE_JOB),
        queued_jobs=tuple(j for j in jobs if j.state is OpState.QUEUED),
    )


@dataclass(frozen=True, slots=True)
class QuitDecision:
    proceed: bool
    policy: DetachPolicy = DetachPolicy.KEEP_FILTER
    cancel_jobs: bool = False


def ask_render_running(parent: QWidget | None) -> bool:
    """True = "Cancel render and quit"."""
    box = a11y.message_box(
        parent,
        icon=QMessageBox.Icon.Warning,
        title=_t("Quit ButterEye"),
        text=_t("A render is running. Quitting cancels it and deletes the partial file."),
    )
    box.setObjectName("quit.renderRunning")
    cancel_render = box.addButton(_t("&Cancel render and quit"), QMessageBox.ButtonRole.AcceptRole)
    cancel_render.setObjectName("quit.cancelRender")
    keep = box.addButton(_t("&Keep window open"), QMessageBox.ButtonRole.RejectRole)
    keep.setObjectName("quit.keepOpen")
    box.setDefaultButton(keep)
    box.setEscapeButton(keep)
    box.exec()
    return box.clickedButton() is cancel_render


class LiveSessionsDialog(QDialog):
    """The live-sessions question; ``policy()`` after ``exec()`` returns Accepted."""

    def __init__(self, sessions: tuple[SessionSnapshot, ...], parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName("quit.liveSessions")
        self.setWindowTitle(_t("Quit ButterEye"))
        self.setModal(True)
        lay = QVBoxLayout(self)
        self.message = QLabel(_t("Smooth playback keeps running in mpv after ButterEye closes."))
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.message)
        titles = ", ".join(s.title for s in sessions)
        self.players = QLabel(_t("Players: {titles}").format(titles=titles))
        self.players.setWordWrap(True)
        self.players.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.players)
        self.disable = QCheckBox(_t("&Turn interpolation off in those players"))
        self.disable.setObjectName("quit.disableFilter")
        self.disable.setChecked(False)
        lay.addWidget(self.disable)
        buttons = QDialogButtonBox()
        self.close_button = QPushButton(_t("&Close"))
        self.close_button.setObjectName("quit.close")
        self.keep_button = QPushButton(_t("&Keep window open"))
        self.keep_button.setObjectName("quit.keepOpen")
        buttons.addButton(self.close_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(self.keep_button, QDialogButtonBox.ButtonRole.RejectRole)
        self.close_button.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def policy(self) -> DetachPolicy:
        return DetachPolicy.DISABLE_FILTER if self.disable.isChecked() else DetachPolicy.KEEP_FILTER


def ask_live_sessions(
    sessions: tuple[SessionSnapshot, ...], parent: QWidget | None
) -> DetachPolicy | None:
    """The detach policy, or ``None`` for "Keep window open"."""
    dlg = LiveSessionsDialog(sessions, parent)
    accepted = dlg.exec() == QDialog.DialogCode.Accepted
    policy = dlg.policy()
    dlg.deleteLater()
    return policy if accepted else None


def decide(needs: QuitNeeds, parent: QWidget | None) -> QuitDecision:
    """Ask the questions ``needs`` calls for (render first, then live sessions)."""
    cancel_jobs = False
    if needs.running_jobs:
        if not ask_render_running(parent):
            return QuitDecision(False)
        cancel_jobs = True
    policy = DetachPolicy.KEEP_FILTER
    if needs.live:
        chosen = ask_live_sessions(needs.live, parent)
        if chosen is None:
            return QuitDecision(False)
        policy = chosen
    # Queued (not started) jobs die with the window too: no background rendering.
    return QuitDecision(True, policy, cancel_jobs or bool(needs.queued_jobs))


class DetachProgress(QDialog):
    """Modal "Detaching from N players..." shown while the core closes."""

    def __init__(self, count: int, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setObjectName("quit.detaching")
        self.setWindowTitle(_t("Quit ButterEye"))
        self.setModal(True)
        lay = QVBoxLayout(self)
        text = QCoreApplication.translate("QuitDialog", "Detaching from %n player(s)…", None, count)
        self.label = QLabel(text)
        self.label.setTextFormat(Qt.TextFormat.PlainText)
        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.setAccessibleName(text)
        self.label.setBuddy(self.bar)
        lay.addWidget(self.label)
        lay.addWidget(self.bar)


def failed_titles(needs: QuitNeeds, report: ShutdownReport | None) -> tuple[str, ...]:
    """Titles of the players that kept their settings (all of them on timeout)."""
    by_sid: dict[SessionId, str] = {s.sid: s.title for s in needs.live}
    if report is None:
        return tuple(by_sid.values())
    return tuple(by_sid.get(sid, str(sid)) for sid, _code in report.failed)


def show_detach_failed(titles: tuple[str, ...], parent: QWidget | None) -> None:
    # Titles come from media metadata: plain text only (a11y.message_box).
    box = a11y.message_box(
        parent,
        icon=QMessageBox.Icon.Warning,
        title=_t("Quit ButterEye"),
        text=_t("Could not detach from: {titles}. They keep their current settings.").format(
            titles=", ".join(titles)
        ),
    )
    box.setObjectName("quit.detachFailed")
    ok = box.addButton(QMessageBox.StandardButton.Ok)
    ok.setObjectName("quit.ok")
    box.exec()


def run_close_flow(
    parent: QWidget | None,
    needs: QuitNeeds,
    shutdown: Shutdown,
) -> tuple[bool, ShutdownReport | None]:
    """Ask, shut the core down, report failures. Returns (may_close, report)."""
    decision = decide(needs, parent)
    if not decision.proceed:
        return False, None
    progress: DetachProgress | None = None
    if needs.live:
        progress = DetachProgress(len(needs.live), parent)
        progress.show()
    try:
        report = shutdown(decision.policy, decision.cancel_jobs)
    finally:
        if progress is not None:
            progress.hide()
            progress.deleteLater()
    if needs.live:
        titles = failed_titles(needs, report)
        if titles:
            show_detach_failed(titles, parent)
    return True, report


__all__ = [
    "DetachProgress",
    "LiveSessionsDialog",
    "QuitDecision",
    "QuitNeeds",
    "Shutdown",
    "ask_live_sessions",
    "ask_render_running",
    "decide",
    "failed_titles",
    "quit_needs",
    "run_close_flow",
    "show_detach_failed",
]
