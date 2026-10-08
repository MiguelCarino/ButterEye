# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Play page: live sessions (docs/design/GUI.md §4.3; SCOPE F2-F4, F8, §4.3, §4.10, §4.11).

States (``StatePanel``): *loading* until capabilities and the first
``sessions()`` reply arrive; *unavailable* when this build has no live control
(``Feature.LIVE`` gated: zero rows, §5.2 text, BE code, ``play`` hint); *error*
when ``sessions()`` fails; otherwise *content*. Inside the content the page
shows either the empty view (``DropZone`` + ``play`` hint; ``view_state()`` is
``"empty"``) or the sessions view (tree + detail; ``"sessions"``) — the empty
state lives inside the content because it hosts a drop target, which
``StatePanel.show_empty`` cannot.

"Not ready" (a blocking doctor report, or no config yet) shows a banner with
[Open Setup] and disables opening/attaching, with the reason as the controls'
accessible description.

Health is never inferred here: health, filter state and rollbacks come from the
core (R12). Announcements are made for transitions only (headline changes,
health changes, notices, op results), never for counters.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections import deque
from fractions import Fraction
from pathlib import Path
from typing import ClassVar, Literal

from PySide6.QtCore import QCoreApplication, QEvent, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QGuiApplication,
    QIcon,
    QKeyEvent,
    QPalette,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    BackendId,
    ButterEyeError,
    BypassReason,
    CapabilitiesChanged,
    CapState,
    CommandHint,
    ConfigChanged,
    ConfigLoad,
    Counters,
    DetachPolicy,
    ErrorCode,
    Event,
    EventsDropped,
    Feature,
    FilterFailed,
    FilterState,
    HardwareInfo,
    Health,
    HealthChanged,
    NotAvailable,
    Notice,
    OperationCancelled,
    Origin,
    Profile,
    Progress,
    Reason,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SessionId,
    SessionSnapshot,
    SourceFacts,
    command_hint,
    render,
    unavailable_text,
)
from buttereye.gui.a11y import heading, labelled, message_box, selectable
from buttereye.gui.bridge import Ticket
from buttereye.gui.context import GuiContext
from buttereye.gui.pages.base import GATING_REASONS, Page, PageAction
from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.drop_zone import DropZone, FileChooser, default_chooser
from buttereye.gui.widgets.op_row import OpRow
from buttereye.gui.widgets.state_panel import StatePanel, split_headline
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge, badge_pixmap

EndReason = Literal["mpv_exited", "detached", "connection_lost"]

#: Ended rows disappear after this long (or on Delete), §4.3.
ENDED_LINGER_MS = 10_000
#: Counter deltas are shown over this window (§4.3 "10 s deltas").
DELTA_WINDOW_S = 10.0
#: The copyable diagnostic for GPU faults (NVIDIA Xid lines in the kernel log).
DIAGNOSTIC_COMMAND = "journalctl -k -b --grep=Xid"

_SID_ROLE = Qt.ItemDataRole.UserRole
_AUTO = "__auto__"  # profile combo data for "Automatic (rules)"


def _t(text: str) -> str:
    return QCoreApplication.translate("SessionsPage", text)


# ---------------------------------------------------------------------------
# Pure text helpers (GUI thread; tested directly)
# ---------------------------------------------------------------------------


def fmt_rate(x: Fraction | float) -> str:
    """23.976 / 119.88 / 60 — at most three decimals, no trailing zeros."""
    return f"{float(x):.3f}".rstrip("0").rstrip(".")


def fmt_multiplier(m: Fraction) -> str:
    return _t("{n}×").format(n=fmt_rate(m))


def backend_name(b: BackendId) -> str:
    names = {
        BackendId.RIFE_NCNN: _t("RIFE (Vulkan)"),
        BackendId.MVTOOLS: _t("MVTools (CPU)"),
        BackendId.RIFE_TRT: _t("RIFE · TensorRT (experimental)"),
    }
    return names[b]


_MODEL = re.compile(r"rife[-_]?v?(\d+(?:\.\d+)?)([-_]lite)?", re.IGNORECASE)


def model_short(model: str) -> str:
    """``rife-v4.26_ensembleFalse`` -> ``v4.26``; ``rife-v4.22_lite...`` -> ``v4.22 lite``."""
    m = _MODEL.search(model)
    if m is None:
        return model
    out = f"v{m.group(1)}"
    return out + (" " + _t("lite") if m.group(2) else "")


def bypass_text(reason: BypassReason) -> str:
    texts = {
        BypassReason.ALREADY_AT_RATE: _t("Already at your screen's rate — nothing to do"),
        BypassReason.INTERLACED: _t("Interlaced video — smoothing skipped"),
        BypassReason.HDR_SKIP: _t("HDR video — smoothing skipped (default)"),
        BypassReason.UNSUPPORTED_FORMAT: _t("This video format can't be smoothed"),
        BypassReason.NO_VIDEO: _t("No video to smooth"),
        BypassReason.NO_REALTIME: _t("Too slow for real time at this resolution — smoothing off"),
    }
    return texts[reason]


def ended_text(reason: EndReason | None) -> str:
    if reason == "mpv_exited":
        return _t("Ended: mpv exited")
    if reason == "detached":
        return _t("Detached")
    if reason == "connection_lost":
        return _t("Connection lost")
    return _t("Ended")


def drop_percent(rate: float | None) -> str | None:
    if rate is None:
        return None
    pct = rate * 100
    return f"{pct:.0f}" if pct >= 1 or pct == 0 else f"{pct:.1f}"


def health_text(health: Health, drop_rate: float | None = None) -> str:
    """Headline text for a session whose health is not OK."""
    if health is Health.DROPPING:
        pct = drop_percent(drop_rate)
        if pct is None:
            return _t("Dropping frames")
        return _t("Dropping frames ({pct}% over 10 s)").format(pct=pct)
    texts = {
        Health.OK: _t("Healthy"),
        Health.STALLED: _t("Video stalled — interpolation turned off"),
        Health.GPU_FAULT: _t("GPU fault — interpolation turned off"),
        Health.DEVICE_LOST: _t("GPU device lost — interpolation turned off"),
        Health.CONNECTION_LOST: _t("Lost the connection to mpv"),
    }
    return texts[health]


def health_word(health: Health) -> tuple[BadgeKind, str]:
    """Short word for the Health column."""
    words: dict[Health, tuple[BadgeKind, str]] = {
        Health.OK: ("ok", _t("OK")),
        Health.DROPPING: ("degraded", _t("Dropping frames")),
        Health.STALLED: ("blocking", _t("Stalled")),
        Health.GPU_FAULT: ("blocking", _t("GPU fault")),
        Health.DEVICE_LOST: ("blocking", _t("GPU lost")),
        Health.CONNECTION_LOST: ("blocking", _t("Disconnected")),
    }
    return words[health]


def filter_word(snap: SessionSnapshot) -> tuple[BadgeKind, str]:
    """Short word for the State column."""
    if snap.ended:
        return "off", _t("Ended")
    words: dict[FilterState, tuple[BadgeKind, str]] = {
        FilterState.OFF: ("off", _t("Off")),
        FilterState.PENDING: ("busy", _t("Starting")),
        FilterState.ACTIVE: ("ok", _t("Smooth")),
        FilterState.BYPASSED: ("info", _t("Skipped")),
        FilterState.ROLLED_BACK: ("blocking", _t("Rolled back")),
    }
    return words[snap.filter]


def rate_text(snap: SessionSnapshot) -> str | None:
    """ "23.976 → 119.88 fps (5×)", or None when the rates are not known."""
    if snap.source is None or snap.target_fps is None:
        return None
    if snap.multiplier is None:
        return _t("{src} → {dst} fps").format(
            src=fmt_rate(snap.source.fps), dst=fmt_rate(snap.target_fps)
        )
    return _t("{src} → {dst} fps ({mult})").format(
        src=fmt_rate(snap.source.fps),
        dst=fmt_rate(snap.target_fps),
        mult=fmt_multiplier(snap.multiplier),
    )


def engine_text(snap: SessionSnapshot) -> str | None:
    if snap.backend is None:
        return None
    name = backend_name(snap.backend)
    return f"{name} {model_short(snap.model)}" if snap.model else name


def failure_text(snap: SessionSnapshot, failure: ButterEyeError | None) -> str:
    """ROLLED_BACK headline: rendered cause + fix + code."""
    if failure is not None:
        cause = render(failure.cause)
        fix = render(failure.fix) if failure.fix is not None else ""
        code: ErrorCode | None = failure.code
    else:
        cause = (
            render(snap.notice)
            if snap.notice is not None
            else _t("Interpolation was turned off after a failure.")
        )
        fix = _t("Turn interpolation back on or pick another profile.")
        code = snap.health_code or ErrorCode.VF_ROLLED_BACK
    parts = [p for p in (cause, fix) if p]
    text = " ".join(parts)
    return f"{text} ({code.code})" if code is not None else text


def headline(
    snap: SessionSnapshot,
    *,
    ended_reason: EndReason | None = None,
    gpu: str | None = None,
    failure: ButterEyeError | None = None,
) -> tuple[BadgeKind, str]:
    """The detail headline: ``ended`` -> health != OK -> filter state (§4.3)."""
    if snap.ended:
        return "off", ended_text(ended_reason)
    if snap.health is not Health.OK:
        kind, _word = health_word(snap.health)
        return kind, health_text(snap.health, snap.drop_rate_10s)
    f = snap.filter
    if f is FilterState.PENDING:
        if snap.notice is not None:  # the core gave up waiting (start deadline)
            return "degraded", render(snap.notice)
        return "busy", _t("Starting… waiting for the video window")
    if f is FilterState.OFF:
        return "off", _t("Smoothing off")
    if f is FilterState.BYPASSED:
        if snap.bypass is None:
            return "info", _t("Smoothing skipped")
        return "info", bypass_text(snap.bypass)
    if f is FilterState.ROLLED_BACK:
        return "blocking", failure_text(snap, failure)
    # ACTIVE
    rates = rate_text(snap)
    head = _t("Smooth: {rates}").format(rates=rates) if rates else _t("Smooth")
    parts = [head]
    engine = engine_text(snap)
    if engine:
        parts.append(engine)
    if gpu and snap.backend is not BackendId.MVTOOLS:
        parts.append(gpu)
    return "ok", " · ".join(parts)


def default_health_code(health: Health) -> ErrorCode | None:
    codes: dict[Health, ErrorCode | None] = {
        Health.OK: None,
        Health.DROPPING: None,
        Health.STALLED: ErrorCode.FILTER_STALLED,
        Health.GPU_FAULT: ErrorCode.RIFE_GPU_FAULT,
        Health.DEVICE_LOST: ErrorCode.DEVICE_LOST,
        Health.CONNECTION_LOST: ErrorCode.IPC_LOST,
    }
    return codes[health]


@dataclasses.dataclass(frozen=True, slots=True)
class HealthBannerSpec:
    kind: BadgeKind
    title: str
    body: str
    code: str | None


#: An NVIDIA Xid kernel line (both the ``Xid 13,`` and ``Xid (PCI:…): 13,`` forms).
_XID_LINE = re.compile(r"NVRM: Xid\s*(?:\(PCI:[0-9A-Fa-f:.]+\))?\s*:?\s*(\d+)\s*,")


def xid_numbers(evidence: tuple[str, ...] | list[str]) -> tuple[int, ...]:
    """The distinct Xid numbers in ``evidence``, in first-seen order."""
    out: list[int] = []
    for line in evidence:
        m = _XID_LINE.search(line)
        if m is not None and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return tuple(out)


def gpu_fault_title(health: Health, evidence: tuple[str, ...] | list[str] = ()) -> str:
    """Banner title for GPU_FAULT / DEVICE_LOST, naming only the Xids actually seen."""
    xids = xid_numbers(evidence)
    if xids:
        return _t("The GPU reported a fault in RIFE-ncnn (Xid {xids}).").format(
            xids=", ".join(str(x) for x in xids)
        )
    if health is Health.DEVICE_LOST:
        return _t("The GPU stopped responding (Vulkan device lost).")
    return _t("The GPU reported a fault in RIFE-ncnn.")


def health_banner_spec(
    snap: SessionSnapshot, event: HealthChanged | None = None
) -> HealthBannerSpec | None:
    """The inline health banner for ``snap`` (§4.3 table), or None when healthy."""
    h = snap.health
    if h is Health.OK or snap.ended:
        return None
    code = snap.health_code or (event.code if event is not None else None)
    code = code or default_health_code(h)
    if h is Health.DROPPING:
        pct = drop_percent(snap.drop_rate_10s)
        title = (
            _t("Dropping frames ({pct}% over 10 s).").format(pct=pct)
            if pct is not None
            else _t("Dropping frames.")
        )
        body = _t("Your GPU can't keep up with this profile. A lighter profile may help.")
        kind: BadgeKind = "degraded"
    elif h is Health.STALLED:
        title = _t("Video stalled while audio kept playing; interpolation was turned off.")
        body = _t(
            "ButterEye never turns it back on by itself. MVTools runs on the CPU and "
            "avoids this stall."
        )
        kind = "blocking"
    elif h in (Health.GPU_FAULT, Health.DEVICE_LOST):
        title = gpu_fault_title(h, event.evidence if event is not None else ())
        body = _t("Interpolation was turned off. MVTools runs on the CPU and avoids the fault.")
        kind = "blocking"
    else:  # CONNECTION_LOST
        title = _t("Lost the connection to mpv.")
        body = _t(
            "mpv isn't answering. If it recovers, ButterEye picks it up again by itself; "
            "if it stays stuck, close that mpv window."
        )
        kind = "blocking"
    return HealthBannerSpec(kind, title, body, code.code if code is not None else None)


def counter_delta(
    history: deque[tuple[float, Counters]], now: float, window_s: float = DELTA_WINDOW_S
) -> Counters | None:
    """Change of every counter over the last ``window_s`` (oldest sample if shorter)."""
    if len(history) < 2:
        return None
    latest = history[-1][1]
    base = history[0][1]
    for t, c in history:
        if now - t >= window_s:
            base = c
        else:
            break
    return Counters(
        frame_drop=latest.frame_drop - base.frame_drop,
        decoder_drop=latest.decoder_drop - base.decoder_drop,
        vo_delayed=latest.vo_delayed - base.vo_delayed,
        mistimed=latest.mistimed - base.mistimed,
        display_sync=latest.display_sync,
    )


def source_text(src: SourceFacts) -> str:
    parts = [
        _t("{w}×{h}").format(w=src.width, h=src.height),
        _t("{fps} fps").format(fps=fmt_rate(src.fps)),
        src.hdr_class.value.upper(),
    ]
    if src.interlaced:
        parts.append(_t("interlaced"))
    if src.vfr:
        parts.append(_t("variable frame rate"))
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Per-session GUI state
# ---------------------------------------------------------------------------


@dataclasses.dataclass(slots=True)
class _Track:
    snap: SessionSnapshot
    ended_reason: EndReason | None = None
    health_event: HealthChanged | None = None
    failure: ButterEyeError | None = None
    dismissed: Health | None = None  # [Ignore]/[Keep off]/[Dismiss] for this health
    profile_choice: str | None = None  # None = Automatic (rules)
    history: deque[tuple[float, Counters]] = dataclasses.field(
        default_factory=lambda: deque(maxlen=64)
    )
    apply_text: str = ""
    busy: bool = False
    ended_announced: bool = False


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


class SessionsPage(Page):
    page_id: ClassVar[str] = "sessions"
    features: ClassVar[tuple[Feature, ...]] = (
        Feature.LIVE,
        Feature.ATTACH,
        Feature.DISCOVER,
        Feature.ORPHANS,
    )
    #: File chooser for Open and Play / the drop zone (tests replace it).
    chooser: ClassVar[FileChooser | None] = None

    #: Selected session's ``SourceFacts`` or None (U4 feeds GuiContext from it).
    facts_changed = Signal(object)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setWindowTitle(self.tr("Play"))
        self._tracks: dict[SessionId, _Track] = {}
        # sids whose row was dropped: a late snapshot never brings one back
        self._gone: set[SessionId] = set()
        self._early_health: dict[SessionId, HealthChanged] = {}
        self._items: dict[SessionId, list[QStandardItem]] = {}
        self._timers: dict[SessionId, QTimer] = {}
        self._loaded = False
        self._sessions_ticket: Ticket | None = None
        self._play_ticket: Ticket | None = None
        self._play_row: OpRow | None = None
        self._select_next: SessionId | None = None
        self._config: ConfigLoad | None = None
        self._config_known = False
        self._hardware: HardwareInfo | None = None
        self._hardware_asked = False
        self._last_facts: SourceFacts | None = None
        self._banner_key: object = None
        self._not_ready_reason: str | None = None
        self.health_banner: Banner | None = None
        self.message_banner: Banner | None = None
        self.not_ready_banner: Banner | None = None
        self._dialog: QDialog | None = None

        lay = QVBoxLayout(self)
        self.panel = StatePanel(self)
        lay.addWidget(self.panel)
        content = self.panel.content
        cl = QVBoxLayout(content)

        # --- not-ready banner + messages + op rows
        self._not_ready_lay = QVBoxLayout()
        cl.addLayout(self._not_ready_lay)
        self._message_lay = QVBoxLayout()
        cl.addLayout(self._message_lay)
        self._op_lay = QVBoxLayout()
        cl.addLayout(self._op_lay)

        # --- toolbar (always visible)
        bar = QHBoxLayout()
        self.open_button = QPushButton(self.tr("&Open and Play…"), content)
        self.open_button.setObjectName("playOpen")
        self.open_button.setAccessibleName(self.tr("Open and Play"))
        self.open_button.setAutoDefault(False)
        self.open_button.clicked.connect(self.open_and_play)
        self.attach_button = QPushButton(self.tr("Atta&ch to running mpv…"), content)
        self.attach_button.setObjectName("playAttach")
        self.attach_button.setAccessibleName(self.tr("Attach to running mpv"))
        self.attach_button.setAutoDefault(False)
        self.attach_button.clicked.connect(self.open_attach)
        self.include_button = QPushButton(self.tr("Profile for my normal &mpv…"), content)
        self.include_button.setObjectName("playInclude")
        self.include_button.setAccessibleName(self.tr("ButterEye's profile for my normal mpv"))
        self.include_button.setAccessibleDescription(
            self.tr(
                "Shows the line that makes ButterEye's mpv profile available in the mpv you "
                "start yourself. It doesn't smooth video there in this build."
            )
        )
        self.include_button.setToolTip(self.include_button.accessibleDescription())
        self.include_button.setAutoDefault(False)
        self.include_button.clicked.connect(self.open_include)
        bar.addWidget(self.open_button)
        bar.addWidget(self.attach_button)
        bar.addWidget(self.include_button)
        bar.addStretch(1)
        cl.addLayout(bar)

        # --- empty view
        self.empty_view = QWidget(content)
        ev = QVBoxLayout(self.empty_view)
        ev.setContentsMargins(0, 0, 0, 0)
        self.empty_label = QLabel(
            self.tr("No players yet. Open a video and ButterEye starts mpv with smoothing."),
            self.empty_view,
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setTextFormat(Qt.TextFormat.PlainText)
        self.drop_zone = DropZone(
            self.tr("Drop a video file here, or press Enter to choose one"),
            self.empty_view,
            chooser=self._choose,
        )
        self.drop_zone.setObjectName("playDropZone")
        self.drop_zone.fileChosen.connect(self.play_file)
        self.drop_zone.rejected.connect(self._on_drop_rejected)
        self.empty_hint = CliHint(self._play_hint(), self.empty_view)
        ev.addWidget(self.empty_label)
        ev.addWidget(self.drop_zone)
        ev.addWidget(self.empty_hint)
        cl.addWidget(self.empty_view)

        # --- sessions view
        self.sessions_view = QWidget(content)
        sv = QHBoxLayout(self.sessions_view)
        sv.setContentsMargins(0, 0, 0, 0)
        self.model = QStandardItemModel(0, 4, self)
        self.model.setHorizontalHeaderLabels(
            [self.tr("Title"), self.tr("Origin"), self.tr("State"), self.tr("Health")]
        )
        self.tree = QTreeView(self.sessions_view)
        self.tree.setObjectName("sessionsTree")
        self.tree.setModel(self.model)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setAccessibleName(self.tr("Players"))
        self.tree.setAccessibleDescription(
            self.tr("Space turns interpolation on or off. Delete removes an ended player.")
        )
        self.tree.installEventFilter(self)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        sel = self.tree.selectionModel()
        sel.currentRowChanged.connect(self._on_current_changed)
        sv.addWidget(self.tree, 1)

        self.detail = QWidget(self.sessions_view)
        dl = QVBoxLayout(self.detail)
        dl.setContentsMargins(0, 0, 0, 0)
        self.headline = StatusBadge("info", "", self.detail)
        self.headline.setObjectName("sessionHeadline")
        heading(self.headline.text_label)
        dl.addWidget(self.headline)
        self._health_lay = QVBoxLayout()
        dl.addLayout(self._health_lay)

        self.form = QFormLayout()
        self.fields: dict[str, QLabel] = {}
        self._field_rows: dict[str, int] = {}
        for key, name in (
            ("rate", self.tr("Rate")),
            ("engine", self.tr("Engine")),
            ("display", self.tr("Display")),
            ("source", self.tr("Source")),
            ("profile", self.tr("Profile in use")),
            ("gen", self.tr("Filter generation")),
            ("drops", self.tr("Dropped frames")),
            ("decoder", self.tr("Decoder drops")),
            ("delayed", self.tr("Delayed frames")),
            ("mistimed", self.tr("Mistimed frames")),
            ("sync", self.tr("Display sync")),
            ("restore", self.tr("Restored when detaching")),
            ("notice", self.tr("Notice")),
        ):
            value = QLabel(self.detail)
            value.setObjectName(f"field.{key}")
            value.setTextFormat(Qt.TextFormat.PlainText)
            value.setWordWrap(True)
            selectable(value, name=name)
            label = QLabel(name, self.detail)
            label.setBuddy(value)
            self._field_rows[key] = self.form.rowCount()
            self.form.addRow(label, value)
            self.fields[key] = value
        dl.addLayout(self.form)

        # controls
        self.interp = QCheckBox(self.tr("&Interpolation"), self.detail)
        self.interp.setObjectName("sessionInterpolation")
        self.interp.setAccessibleName(self.tr("Interpolation on/off"))
        self.interp.clicked.connect(self._on_interp_clicked)
        dl.addWidget(self.interp)

        prow = QHBoxLayout()
        self.profile_combo = QComboBox(self.detail)
        self.profile_combo.setObjectName("sessionProfile")
        plabel, _ = labelled(
            self.tr("P&rofile"),
            self.profile_combo,
            description=self.tr("Enter applies the chosen profile to this player."),
        )
        self.profile_combo.installEventFilter(self)
        self.apply_button = QPushButton(self.tr("Appl&y"), self.detail)
        self.apply_button.setObjectName("sessionApply")
        self.apply_button.setAccessibleName(self.tr("Apply profile"))
        self.apply_button.setAutoDefault(False)
        self.apply_button.clicked.connect(self.apply_selected_profile)
        prow.addWidget(plabel)
        prow.addWidget(self.profile_combo, 1)
        prow.addWidget(self.apply_button)
        dl.addLayout(prow)
        self.apply_status = QLabel(self.detail)
        self.apply_status.setObjectName("sessionApplyStatus")
        self.apply_status.setTextFormat(Qt.TextFormat.PlainText)
        self.apply_status.setWordWrap(True)
        dl.addWidget(self.apply_status)
        self._apply_banner_lay = QVBoxLayout()
        dl.addLayout(self._apply_banner_lay)
        self.apply_banner: Banner | None = None

        srow = QHBoxLayout()
        self.step_label = QLabel(self.detail)
        self.step_label.setTextFormat(Qt.TextFormat.PlainText)
        self.step_label.setWordWrap(True)
        self.step_button = QPushButton(self.tr("&Step down"), self.detail)
        self.step_button.setObjectName("sessionStepDown")
        self.step_button.setAccessibleName(self.tr("Step down"))
        self.step_button.setAutoDefault(False)
        self.step_button.clicked.connect(self.step_down_selected)
        self.step_label.setBuddy(self.step_button)
        srow.addWidget(self.step_label, 1)
        srow.addWidget(self.step_button)
        dl.addLayout(srow)

        drow = QHBoxLayout()
        self.detach_button = QPushButton(self.tr("&Detach"), self.detail)
        self.detach_button.setObjectName("sessionDetach")
        self.detach_button.setAccessibleName(self.tr("Detach"))
        self.detach_button.setAccessibleDescription(
            self.tr("ButterEye lets go of this player; smoothing stays as it is.")
        )
        self.detach_button.setAutoDefault(False)
        self.detach_button.clicked.connect(lambda: self.detach_selected(DetachPolicy.KEEP_FILTER))
        self.disable_detach_button = QPushButton(self.tr("Disable &and detach"), self.detail)
        self.disable_detach_button.setObjectName("sessionDisableDetach")
        self.disable_detach_button.setAccessibleName(self.tr("Disable and detach"))
        self.disable_detach_button.setAutoDefault(False)
        self.disable_detach_button.clicked.connect(
            lambda: self.detach_selected(DetachPolicy.DISABLE_FILTER)
        )
        drow.addWidget(self.detach_button)
        drow.addWidget(self.disable_detach_button)
        drow.addStretch(1)
        dl.addLayout(drow)
        self.controls_hint = CliHint(command_hint("detach"), self.detail)
        dl.addWidget(self.controls_hint)
        dl.addStretch(1)
        sv.addWidget(self.detail, 1)
        cl.addWidget(self.sessions_view)
        cl.addStretch(1)

        self._show_view()
        self._fill_profiles()
        self.panel.show_loading(self.tr("Connecting to ButterEye's engine…"))

    # ================================================================== §4.0 surface
    def title(self) -> str:
        return self.windowTitle()

    def refresh(self) -> None:
        caps = self.ctx.bridge.capabilities
        if caps is None:
            self.panel.show_loading(self.tr("Connecting to ButterEye's engine…"))
            return
        gated = self._gated(Feature.LIVE)
        if gated is not None:
            self._clear_all()
            self.panel.show_unavailable(Feature.LIVE, gated, self._play_hint())
            self.facts_changed.emit(None)
            return
        if not self._loaded:
            self.panel.show_loading(self.tr("Looking for players…"))
        if self._sessions_ticket is None or not self._sessions_ticket.pending:
            self._sessions_ticket = self.ctx.bridge.call(
                lambda core: core.sessions(),
                owner=self,
                ok=self._on_sessions,
                err=self._on_sessions_error,
            )
        if caps.ok(Feature.CONFIG):
            self.ctx.bridge.call(
                lambda core: core.load_config(),
                owner=self,
                ok=self._on_config,
                err=self._on_config_error,
            )
        else:
            self._config = None
            self._config_known = False
            self._fill_profiles()
        self._update_not_ready()

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, SessionAdded | SessionChanged):
            if self._loaded:
                self._apply_snapshot(ev.snapshot)
        elif isinstance(ev, SessionEnded):
            self._on_ended(ev.sid, ev.reason)
        elif isinstance(ev, HealthChanged):
            self._on_health(ev)
        elif isinstance(ev, Notice):
            self._on_notice(ev)
        elif isinstance(ev, CapabilitiesChanged):
            self.refresh()
        elif isinstance(ev, EventsDropped):
            self.refresh()
        elif isinstance(ev, ConfigChanged):
            caps = self.ctx.bridge.capabilities
            if caps is not None and caps.ok(Feature.CONFIG):
                self.ctx.bridge.call(
                    lambda core: core.load_config(),
                    owner=self,
                    ok=self._on_config,
                    err=self._on_config_error,
                )

    def handle_action(self, action: PageAction) -> bool:
        if action is PageAction.OPEN_AND_PLAY:
            self.open_and_play()
            return True
        if action is PageAction.ATTACH:
            self.open_attach()  # says why when attaching is unavailable
            return True
        if action in (
            PageAction.TOGGLE_INTERPOLATION,
            PageAction.DETACH,
            PageAction.DISABLE_AND_DETACH,
        ):
            if self._gated(Feature.LIVE) is not None:
                return False
            if self._selected_track() is None:
                self._say(self.tr("Select a player first."))
                return True
            if action is PageAction.TOGGLE_INTERPOLATION:
                self.toggle_selected()
            elif action is PageAction.DETACH:
                self.detach_selected(DetachPolicy.KEEP_FILTER)
            else:
                self.detach_selected(DetachPolicy.DISABLE_FILTER)
            return True
        return False

    def cli_hint(self) -> CommandHint | None:
        track = self._selected_track()
        if track is not None and track.snap.source is not None:
            profile = track.profile_choice
            if profile:
                return command_hint("play", file=track.snap.source.path, profile=profile)
            return command_hint("play", file=track.snap.source.path)
        return self._play_hint()

    def show_error(self, err: ButterEyeError) -> None:
        """Errors of user actions: an inline banner above the toolbar."""
        if isinstance(err, OperationCancelled):
            self.ctx.status(self.tr("Cancelled"))
            return
        self._show_message(Banner.from_error(err, self._dismiss_action(), self))
        self._say(self.tr("Error: {cause}").format(cause=render(err.cause)), assertive=True)

    # ================================================================== public actions
    def view_state(self) -> str:
        """ "empty" / "sessions" inside the content, else the panel state."""
        if self.panel.state != "content":
            return self.panel.state
        return "sessions" if self._tracks else "empty"

    def selected_sid(self) -> SessionId | None:
        track = self._selected_track()
        return track.snap.sid if track is not None else None

    def select(self, sid: SessionId) -> None:
        items = self._items.get(sid)
        if items is None:
            return
        self.tree.setCurrentIndex(items[0].index())

    def open_and_play(self) -> None:
        if self._not_ready_reason is not None:
            self._say(self._not_ready_reason)
            return
        self.drop_zone.choose()

    def play_file(self, path: Path) -> None:
        if self._not_ready_reason is not None:
            self._say(self._not_ready_reason)
            return
        if self._play_ticket is not None and self._play_ticket.pending:
            self._say(self.tr("A player is already starting."))
            return
        self._clear_message()
        name = path.name
        label = self.tr("Starting {name}…").format(name=name)
        self._finish_play_row()
        row = OpRow(label, self._cancel_play, self)
        row.setObjectName("playOp")
        self._play_row = row
        self._op_lay.addWidget(row)
        file = Path(path)
        self._play_ticket = self.ctx.bridge.run_op(
            lambda core: core.play(file),
            owner=self,
            ok=self._on_played,
            err=self._on_play_failed,
            progress=self._on_play_progress,
        )
        self._say(label)

    def open_attach(self, *, select_pid: int | None = None) -> None:
        if self._not_ready_reason is not None:
            self._say(self._not_ready_reason)
            return
        why = self.attach_unavailable_reason()
        if why is not None:
            self._say(why)
            return
        from buttereye.gui.dialogs.attach import AttachDialog

        dlg = AttachDialog(self.ctx.bridge, self, select_pid=select_pid)
        dlg.attached.connect(self._on_attached)
        self._dialog = dlg
        dlg.open()

    def open_include(self) -> None:
        from buttereye.gui.dialogs.include_line import IncludeLineDialog

        dlg = IncludeLineDialog(self.ctx.bridge, self)
        self._dialog = dlg
        dlg.open()

    def toggle_selected(self) -> None:
        track = self._selected_track()
        if track is None or not self.interp.isEnabled():
            return
        self._set_interpolation(track, not self._interp_on(track.snap))

    def apply_selected_profile(self) -> None:
        track = self._selected_track()
        if track is None or track.busy or track.snap.ended:
            return
        data = self.profile_combo.currentData()
        choice = None if data in (None, _AUTO) else str(data)
        track.profile_choice = choice
        self._apply_profile(track, choice)

    def step_down_selected(self) -> None:
        track = self._selected_track()
        if track is not None:
            self._step_down(track.snap.sid)

    def detach_selected(self, policy: DetachPolicy) -> None:
        track = self._selected_track()
        if track is None or track.snap.ended or track.busy:
            return
        sid = track.snap.sid
        track.busy = True
        self._update_controls()
        self.ctx.bridge.call(
            lambda core: core.detach(sid, policy),
            owner=self,
            ok=lambda _none: self._on_detached(sid, policy),
            err=lambda e: self._on_action_error(sid, e),
        )

    def remove_ended(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        if track is None or not track.snap.ended:
            return
        self._drop_row(sid)

    # ================================================================== event filter
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if watched is self.tree:
                if key == Qt.Key.Key_Space:
                    self.toggle_selected()
                    return True
                if key == Qt.Key.Key_Delete:
                    sid = self.selected_sid()
                    if sid is not None:
                        self.remove_ended(sid)
                    return True
            elif watched is self.profile_combo and key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if not self.profile_combo.view().isVisible():
                    self.apply_selected_profile()
                    return True
        return super().eventFilter(watched, event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.ApplicationPaletteChange):
            for sid in list(self._items):
                self._update_row(sid)
        super().changeEvent(event)

    # ================================================================== core replies
    def _on_sessions(self, snaps: tuple[SessionSnapshot, ...]) -> None:
        first = not self._loaded
        self._loaded = True
        seen = {s.sid for s in snaps}
        for sid in [s for s in self._tracks if s not in seen and not self._tracks[s].snap.ended]:
            self._drop_row(sid)
        for snap in snaps:
            self._apply_snapshot(snap, announce_changes=not first)
        self.panel.show_content()
        self._show_view()
        if self.tree.currentIndex().isValid() is False and self.model.rowCount():
            self.tree.setCurrentIndex(self.model.index(0, 0))
        self._update_detail()

    def _on_sessions_error(self, err: ButterEyeError) -> None:
        if isinstance(err, NotAvailable):
            self._clear_all()
            self.panel.show_unavailable(err.feature, err.state, self._play_hint())
            return
        self.panel.show_error(err, actions=[(self.tr("&Retry"), self.refresh)])
        self._say(self.tr("Error: {cause}").format(cause=render(err.cause)), assertive=True)

    def _on_config(self, load: ConfigLoad) -> None:
        self._config = load
        self._config_known = True
        self._fill_profiles()
        self._update_not_ready()
        self._update_detail()

    def _on_config_error(self, _err: ButterEyeError) -> None:
        self._config = None
        self._config_known = False
        self._fill_profiles()
        self._update_not_ready()

    def _on_hardware(self, hw: HardwareInfo) -> None:
        self._hardware = hw
        self._update_detail()
        for sid in list(self._items):
            self._update_row(sid)

    # ---- play
    def _choose(self, parent: QWidget) -> Path | None:
        chooser = type(self).chooser
        if chooser is not None:
            return chooser(parent)
        return default_chooser(
            self.tr("Open and Play"),
            self.tr("Video files (*.mkv *.mp4 *.webm *.mov *.avi *.m2ts *.ts);;All files (*)"),
        )(parent)

    def _on_drop_rejected(self, text: str) -> None:
        self._show_message(
            Banner(
                "degraded",
                text,
                code=ErrorCode.FILE_NOT_LOCAL.code
                if ErrorCode.FILE_NOT_LOCAL.code in text
                else None,
                actions=self._dismiss_action(),
                parent=self,
            )
        )
        self._say(text, assertive=True)

    def _on_play_progress(self, p: Progress) -> None:
        if self._play_row is not None:
            self._play_row.update(p)

    def _cancel_play(self) -> None:
        ticket = self._play_ticket
        if ticket is not None:
            ticket.cancel()
        self._play_ticket = None
        if self._play_row is not None:
            self._play_row.finish(False, self.tr("Cancelled"), cancelled=True)
        self._say(self.tr("Cancelled"))

    def _finish_play_row(self) -> None:
        if self._play_row is not None:
            self._play_row.hide()
            self._play_row.deleteLater()
            self._play_row = None

    def _on_played(self, sid: SessionId) -> None:
        self._play_ticket = None
        self._finish_play_row()
        self._select_next = sid
        if sid in self._items:
            self.select(sid)
            self._select_next = None
        self._say(self.tr("Player started."))

    def _on_play_failed(self, err: ButterEyeError) -> None:
        self._play_ticket = None
        if isinstance(err, OperationCancelled):
            if self._play_row is not None:
                self._play_row.finish(False, self.tr("Cancelled"), cancelled=True)
            self.ctx.status(self.tr("Cancelled"))
            return
        self._finish_play_row()
        self.show_error(err)

    def _on_attached(self, sid: object) -> None:
        if not isinstance(sid, str):
            return
        self._select_next = SessionId(sid)
        if self._select_next in self._items:
            self.select(self._select_next)
            self._select_next = None
        self._say(self.tr("Attached to the player."))
        self.refresh()

    # ---- live actions
    def _interp_on(self, snap: SessionSnapshot) -> bool:
        return snap.filter in (FilterState.ACTIVE, FilterState.PENDING)

    def _set_interpolation(self, track: _Track, enabled: bool) -> None:
        if track.busy or track.snap.ended:
            return
        sid = track.snap.sid
        track.busy = True
        self._update_controls()
        self.ctx.bridge.call(
            lambda core: core.set_interpolation(sid, enabled),
            owner=self,
            ok=lambda snap: self._on_action_done(
                sid, snap, self.tr("Interpolation on") if enabled else self.tr("Interpolation off")
            ),
            err=lambda e: self._on_action_error(sid, e),
        )

    def _apply_profile(self, track: _Track, profile_id: str | None) -> None:
        if track.busy or track.snap.ended:
            return
        sid = track.snap.sid
        track.busy = True
        track.failure = None
        track.apply_text = self.tr("Applying… (gen {gen})").format(gen=track.snap.gen)
        self._clear_apply_banner()
        self._update_detail()
        self.ctx.bridge.call(
            lambda core: core.apply_profile(sid, profile_id),
            owner=self,
            ok=lambda snap: self._on_applied(sid, snap),
            err=lambda e: self._on_action_error(sid, e),
        )

    def _step_down(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        if track is None or track.busy or track.snap.ended:
            return
        track.busy = True
        self._update_controls()
        self.ctx.bridge.call(
            lambda core: core.step_down(sid),
            owner=self,
            ok=lambda snap: self._on_action_done(sid, snap, self.tr("Stepped down")),
            err=lambda e: self._on_action_error(sid, e),
        )

    def _on_action_done(self, sid: SessionId, snap: SessionSnapshot, text: str) -> None:
        track = self._tracks.get(sid)
        if track is not None:
            track.busy = False
        self._apply_snapshot(snap)
        self.ctx.status(f"{snap.title}: {text}")
        self._update_controls()

    def _on_applied(self, sid: SessionId, snap: SessionSnapshot) -> None:
        track = self._tracks.get(sid)
        if track is not None:
            track.busy = False
            track.apply_text = self.tr("Applied (gen {gen})").format(gen=snap.gen)
        self._apply_snapshot(snap)
        self._say(self.tr("{title}: applied (gen {gen})").format(title=snap.title, gen=snap.gen))
        self._update_detail()

    def _on_action_error(self, sid: SessionId, err: ButterEyeError) -> None:
        track = self._tracks.get(sid)
        if track is not None:
            track.busy = False
            if isinstance(err, FilterFailed):
                track.failure = err
                track.apply_text = self.tr("Rolled back")
        if isinstance(err, OperationCancelled):
            self.ctx.status(self.tr("Cancelled"))
            self._update_controls()
            return
        if track is not None and sid == self.selected_sid():
            self._clear_apply_banner()
            self.apply_banner = Banner.from_error(err, parent=self.detail)
            self._apply_banner_lay.addWidget(self.apply_banner)
        else:
            self._show_message(Banner.from_error(err, self._dismiss_action(), self))
        self._say(self.tr("Error: {cause}").format(cause=render(err.cause)), assertive=True)
        self._update_detail()

    def _on_detached(self, sid: SessionId, policy: DetachPolicy) -> None:
        track = self._tracks.get(sid)
        if track is not None:
            track.busy = False
        if policy is DetachPolicy.DISABLE_FILTER:
            self.ctx.status(self.tr("Interpolation turned off and detached."))
        self._update_controls()

    # ---- health-banner actions
    def _use_mvtools(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        if track is None:
            return
        pid = self._mvtools_profile()
        if pid is None:
            self._say(self.tr("No MVTools profile is set up. Add one on the Profiles page."))
            return
        track.profile_choice = pid
        self._apply_profile(track, pid)

    def _retry_rife(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        if track is None:
            return
        box = message_box(self)
        box.setObjectName("retryRifeConfirm")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("Retry RIFE?"))
        box.setText(
            self.tr(
                "GPU faults may happen again with RIFE on this file, and the picture can "
                "freeze while audio keeps playing. Try RIFE again?"
            )
        )
        yes = box.addButton(self.tr("&Retry RIFE"), QMessageBox.ButtonRole.AcceptRole)
        yes.setObjectName("retryRifeConfirmYes")
        no = box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        no.setObjectName("retryRifeConfirmNo")
        box.setDefaultButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        track.dismissed = None
        self._set_interpolation(track, True)

    def _copy_diagnostic(self) -> None:
        QGuiApplication.clipboard().setText(DIAGNOSTIC_COMMAND)
        self._say(self.tr("Diagnostic command copied: {cmd}").format(cmd=DIAGNOSTIC_COMMAND))

    def _dismiss_health(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        if track is None:
            return
        track.dismissed = track.snap.health
        self._update_health_banner(force=True)

    def _reconnect(self, sid: SessionId) -> None:
        track = self._tracks.get(sid)
        self.open_attach(select_pid=track.snap.pid if track is not None else None)

    # ================================================================== events
    def _apply_snapshot(self, snap: SessionSnapshot, *, announce_changes: bool = True) -> None:
        track = self._tracks.get(snap.sid)
        now = time.monotonic()
        if track is None:
            if snap.sid in self._gone:
                return  # its row lingered and was dropped: a late snapshot never revives it
            track = _Track(snap, health_event=self._early_health.pop(snap.sid, None))
            self._tracks[snap.sid] = track
            track.history.append((now, snap.counters))
            self._add_row(snap.sid)
            if announce_changes:
                _k, text = self._headline_of(track)
                self._say(self.tr("{title}: {state}").format(title=snap.title, state=text))
            if not self.tree.currentIndex().isValid():
                self.select(snap.sid)
        else:
            old_key = self._transition_key(track.snap)
            if snap.health is Health.OK:
                track.dismissed = None
            if snap.filter is not FilterState.ROLLED_BACK and not track.busy:
                track.failure = None if snap.filter is FilterState.ACTIVE else track.failure
            track.snap = snap
            if not track.history or track.history[-1][1] != snap.counters:
                track.history.append((now, snap.counters))
            new_key = self._transition_key(snap)
            if announce_changes and new_key != old_key and new_key != "ended":
                kind, text = self._headline_of(track)
                self._say(
                    self.tr("{title}: {state}").format(title=snap.title, state=text),
                    assertive=kind == "blocking",
                )
            self._update_row(snap.sid)
        if snap.ended:
            self._schedule_removal(snap.sid)
        if self._select_next == snap.sid:
            self.select(snap.sid)
            self._select_next = None
        self._show_view()
        self._maybe_fetch_hardware(snap)
        if snap.sid == self.selected_sid():
            self._update_detail()

    def _on_ended(self, sid: SessionId, reason: EndReason) -> None:
        track = self._tracks.get(sid)
        if track is None:
            return
        track.ended_reason = reason
        track.busy = False
        track.snap = dataclasses.replace(track.snap, ended=True)
        self._update_row(sid)
        if not track.ended_announced:
            track.ended_announced = True
            self._say(
                self.tr("{title}: {state}").format(title=track.snap.title, state=ended_text(reason))
            )
        self._schedule_removal(sid)
        if sid == self.selected_sid():
            self._update_detail()

    def _on_health(self, ev: HealthChanged) -> None:
        track = self._tracks.get(ev.sid)
        if track is None:
            # Arrived before the first sessions() reply; kept for the details.
            self._early_health[ev.sid] = ev
            return
        track.health_event = ev
        if track.snap.health is not ev.health:
            track.snap = dataclasses.replace(track.snap, health=ev.health, health_code=ev.code)
            track.dismissed = None
            self._update_row(ev.sid)
            kind, text = self._headline_of(track)
            self._say(
                self.tr("{title}: {state}").format(title=track.snap.title, state=text),
                assertive=ev.health is not Health.OK,
            )
        if ev.sid == self.selected_sid():
            self._update_detail()

    def _on_notice(self, ev: Notice) -> None:
        text = render(ev.message)
        if ev.sid is not None and ev.sid in self._tracks:
            track = self._tracks[ev.sid]
            track.snap = dataclasses.replace(track.snap, notice=ev.message)
            if ev.sid == self.selected_sid():
                self._update_detail()
            self._say(self.tr("{title}: {notice}").format(title=track.snap.title, notice=text))
        elif ev.sid is None:
            self._say(text)

    # ================================================================== rows
    def _add_row(self, sid: SessionId) -> None:
        items = [QStandardItem() for _ in range(4)]
        for it in items:
            it.setEditable(False)
            it.setData(str(sid), _SID_ROLE)
        self.model.appendRow(items)
        self._items[sid] = items
        self._update_row(sid)

    def _update_row(self, sid: SessionId) -> None:
        items = self._items.get(sid)
        track = self._tracks.get(sid)
        if items is None or track is None:
            return
        snap = track.snap
        title, origin, state, health = items
        title.setText(snap.title)
        origin.setText(
            self.tr("Started by ButterEye")
            if snap.origin is Origin.LAUNCHED
            else self.tr("Attached")
        )
        skind, sword = filter_word(snap)
        if snap.ended:
            sword = ended_text(track.ended_reason)
        state.setText(sword)
        state.setIcon(self._icon(skind))
        hkind, hword = health_word(snap.health)
        if snap.ended:
            hkind, hword = "off", self.tr("—")
        health.setText(hword)
        health.setIcon(self._icon(hkind))
        kind, text = self._headline_of(track)
        title.setData(text, Qt.ItemDataRole.AccessibleDescriptionRole)
        pal = self.palette()
        group = QPalette.ColorGroup.Disabled if snap.ended else QPalette.ColorGroup.Active
        brush = QBrush(pal.color(group, QPalette.ColorRole.Text))
        for it in items:
            it.setForeground(brush)

    def _icon(self, kind: BadgeKind) -> QIcon:
        size = max(8, self.fontMetrics().height())
        return QIcon(badge_pixmap(kind, self.palette(), size, self.devicePixelRatioF()))

    def _schedule_removal(self, sid: SessionId) -> None:
        if sid in self._timers:
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(ENDED_LINGER_MS)
        timer.timeout.connect(lambda: self._drop_row(sid))
        self._timers[sid] = timer
        timer.start()

    def _drop_row(self, sid: SessionId) -> None:
        timer = self._timers.pop(sid, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        was_selected = sid == self.selected_sid()
        items = self._items.pop(sid, None)
        self._tracks.pop(sid, None)
        self._gone.add(sid)
        if items is not None:
            self.model.removeRow(items[0].row())
        if was_selected and self.model.rowCount():
            self.tree.setCurrentIndex(self.model.index(0, 0))
        self._show_view()
        self._update_detail()

    def _clear_all(self) -> None:
        for timer in self._timers.values():
            timer.stop()
            timer.deleteLater()
        self._timers.clear()
        self._tracks.clear()
        self._early_health.clear()
        self._items.clear()
        self.model.removeRows(0, self.model.rowCount())
        self._loaded = False
        self._show_view()

    # ================================================================== detail
    def _selected_track(self) -> _Track | None:
        idx = self.tree.currentIndex()
        if not idx.isValid():
            return None
        sid = idx.siblingAtColumn(0).data(_SID_ROLE)
        if sid is None:
            return None
        return self._tracks.get(SessionId(str(sid)))

    def _on_current_changed(self, _cur: QModelIndex, _prev: QModelIndex) -> None:
        self._clear_apply_banner()
        track = self._selected_track()
        self._select_profile_choice(track.profile_choice if track is not None else None)
        self._update_detail()

    def _gpu_name(self, snap: SessionSnapshot) -> str | None:
        hw = self._hardware
        if hw is None or snap.backend in (None, BackendId.MVTOOLS):
            return None
        for dev in hw.vulkan:
            if dev.uuid == hw.interpolation_device:
                return dev.name
        return None

    def _headline_of(self, track: _Track) -> tuple[BadgeKind, str]:
        return headline(
            track.snap,
            ended_reason=track.ended_reason,
            gpu=self._gpu_name(track.snap),
            failure=track.failure,
        )

    @staticmethod
    def _transition_key(snap: SessionSnapshot) -> object:
        # "ended" is announced from SessionEnded (it carries the reason).
        if snap.ended:
            return "ended"
        return (snap.health, snap.filter, snap.bypass)

    def _set_field(self, key: str, text: str | None) -> None:
        label = self.fields[key]
        shown = text is not None and text != ""
        label.setText(text or "")
        self.form.setRowVisible(self._field_rows[key], shown)

    def _profile_name(self, pid: str | None) -> str | None:
        if pid is None:
            return None
        for p in self._profiles():
            if p.id == pid:
                return p.name
        return pid

    def _update_detail(self) -> None:
        track = self._selected_track()
        self.detail.setVisible(track is not None)
        facts = track.snap.source if track is not None and not track.snap.ended else None
        if facts != self._last_facts:
            self._last_facts = facts
            self.facts_changed.emit(facts)
        if track is None:
            self._update_health_banner()
            return
        snap = track.snap
        kind, text = self._headline_of(track)
        self.headline.set_state(kind, text)
        self._set_field("rate", rate_text(snap))
        self._set_field("engine", engine_text(snap))
        self._set_field(
            "display",
            self.tr("{hz} Hz").format(hz=fmt_rate(snap.display_fps)) if snap.display_fps else None,
        )
        self._set_field("source", source_text(snap.source) if snap.source is not None else None)
        self._set_field("profile", self._profile_name(snap.profile_id))
        self._set_field("gen", str(snap.gen) if snap.gen else None)
        c = snap.counters
        delta = counter_delta(track.history, time.monotonic())

        def count(v: int, d: int | None) -> str:
            if d is None:
                return str(v)
            return self.tr("{n} (+{d} in 10 s)").format(n=v, d=d)

        self._set_field("drops", count(c.frame_drop, delta.frame_drop if delta else None))
        self._set_field("decoder", count(c.decoder_drop, delta.decoder_drop if delta else None))
        self._set_field("delayed", count(c.vo_delayed, delta.vo_delayed if delta else None))
        self._set_field("mistimed", count(c.mistimed, delta.mistimed if delta else None))
        self._set_field("sync", self.tr("Yes") if c.display_sync else self.tr("No"))
        self._set_field("restore", ", ".join(snap.restore_pending) or None)
        self._set_field("notice", render(snap.notice) if snap.notice is not None else None)
        self.apply_status.setText(track.apply_text)
        self.apply_status.setVisible(bool(track.apply_text))
        self._update_health_banner()
        self._update_controls()

    def _update_health_banner(self, *, force: bool = False) -> None:
        track = self._selected_track()
        spec = None
        if track is not None and track.dismissed is not track.snap.health:
            spec = health_banner_spec(track.snap, track.health_event)
        key = (
            (track.snap.sid, spec, track.snap.step_down_to, track.snap.origin, track.health_event)
            if track is not None and spec is not None
            else None
        )
        if key == self._banner_key and not force:
            return
        self._banner_key = key
        if self.health_banner is not None:
            self.health_banner.hide()
            self.health_banner.deleteLater()
            self.health_banner = None
        if track is None or spec is None:
            return
        sid = track.snap.sid
        h = track.snap.health
        actions: list[Action]
        if h is Health.DROPPING:
            actions = [
                (self.tr("Step down"), lambda: self._step_down(sid)),
                (self.tr("Ignore"), lambda: self._dismiss_health(sid)),
            ]
        elif h is Health.STALLED:
            actions = [
                (self.tr("Use MVTools for this file"), lambda: self._use_mvtools(sid)),
                (self.tr("Keep off"), lambda: self._dismiss_health(sid)),
            ]
        elif h in (Health.GPU_FAULT, Health.DEVICE_LOST):
            actions = [
                (self.tr("Switch to MVTools"), lambda: self._use_mvtools(sid)),
                (self.tr("Retry RIFE"), lambda: self._retry_rife(sid)),
                (self.tr("Copy diagnostic command"), self._copy_diagnostic),
            ]
        else:
            actions = [(self.tr("Dismiss"), lambda: self._dismiss_health(sid))]
            if self._gated(Feature.ATTACH) is None:  # attaching ships: offer it first
                actions.insert(0, (self.tr("Reconnect"), lambda: self._reconnect(sid)))
        detail = self._health_details(track)
        banner = Banner(
            spec.kind, spec.title, spec.body, spec.code, (), actions, self.detail, detail=detail
        )
        banner.setObjectName("sessionHealthBanner")
        self.health_banner = banner
        self._health_lay.addWidget(banner)

    def _health_details(self, track: _Track) -> str:
        lines: list[str] = []
        ev = track.health_event
        if ev is not None:
            lines.append(render(ev.reason))
            if ev.auto_action is not None:
                lines.append(self.tr("ButterEye: {action}").format(action=render(ev.auto_action)))
            lines.extend(ev.evidence)
        code = track.snap.health_code or default_health_code(track.snap.health)
        if not lines:
            lines.append(health_text(track.snap.health, track.snap.drop_rate_10s))
        if code is not None:
            lines.append(self.tr("Code: {code}").format(code=code.code))
        if track.snap.origin is Origin.ATTACHED:
            lines.append(
                self.tr(
                    "Only mpv's log is visible for attached players; some GPU faults may "
                    "not be detected."
                )
            )
        return "\n".join(lines)

    def _update_controls(self) -> None:
        track = self._selected_track()
        live_ok = self._gated(Feature.LIVE) is None
        usable = track is not None and not track.snap.ended and not track.busy and live_ok
        self.interp.setEnabled(usable)
        self.profile_combo.setEnabled(usable)
        self.apply_button.setEnabled(usable)
        self.detach_button.setEnabled(usable)
        self.disable_detach_button.setEnabled(usable)
        if track is not None:
            self.interp.setChecked(self._interp_on(track.snap))
        step_to = track.snap.step_down_to if track is not None and not track.snap.ended else None
        self.step_label.setVisible(step_to is not None)
        self.step_button.setVisible(step_to is not None)
        self.step_button.setEnabled(step_to is not None and usable)
        if track is not None and step_to is not None:
            name = self._profile_name(step_to) or step_to
            pct = drop_percent(track.snap.drop_rate_10s)
            if pct is not None and pct != "0":
                text = self.tr("Dropping {pct}% of frames — step down to {profile}?").format(
                    pct=pct, profile=name
                )
            else:
                text = self.tr("Step down to {profile}?").format(profile=name)
            self.step_label.setText(text)
            self.step_button.setAccessibleDescription(text)

    def _on_interp_clicked(self, checked: bool) -> None:
        track = self._selected_track()
        if track is None:
            return
        self._set_interpolation(track, checked)

    # ================================================================== profiles
    def _profiles(self) -> tuple[Profile, ...]:
        if self._config is None:
            return ()
        return self._config.config.profiles

    def _mvtools_profile(self) -> str | None:
        for p in self._profiles():
            if p.backend == BackendId.MVTOOLS:
                return p.id
        return None

    def _fill_profiles(self) -> None:
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem(self.tr("Automatic (rules)"), _AUTO)
        builtins = [p for p in self._profiles() if p.builtin]
        custom = [p for p in self._profiles() if not p.builtin]
        for p in builtins:
            self.profile_combo.addItem(p.name, p.id)
        for p in custom:
            self.profile_combo.addItem(self.tr("{name} (custom)").format(name=p.name), p.id)
        idx = self.profile_combo.findData(current)
        self.profile_combo.setCurrentIndex(max(0, idx))
        self.profile_combo.blockSignals(False)

    def _select_profile_choice(self, choice: str | None) -> None:
        idx = self.profile_combo.findData(choice if choice is not None else _AUTO)
        self.profile_combo.setCurrentIndex(max(0, idx))

    # ================================================================== readiness
    def _gated(self, feature: Feature) -> CapState | None:
        caps = self.ctx.bridge.capabilities
        if caps is None:
            return None
        st = caps.states.get(feature)
        if st is not None and not st.available and st.reason in GATING_REASONS:
            return st
        return None

    def attach_unavailable_reason(self) -> str | None:
        """Headline of the §5.2 text while ``Feature.ATTACH`` is gated, else None."""
        st = self._gated(Feature.ATTACH)
        if st is None:
            return None
        return split_headline(render(unavailable_text(Feature.ATTACH, st)))[0]

    def _sync_attach(self) -> None:
        """[Attach…] follows both readiness and ``Feature.ATTACH`` (§5.2)."""
        why = self._not_ready_reason or self.attach_unavailable_reason()
        self.attach_button.setEnabled(why is None)
        self.attach_button.setAccessibleDescription(why or "")
        self.attach_button.setToolTip(why or "")

    def _update_not_ready(self) -> None:
        caps = self.ctx.bridge.capabilities
        reason: str | None = None
        if caps is not None:
            st = caps.states.get(Feature.LIVE)
            if st is not None and not st.available and st.reason is Reason.BLOCKED_BY_DOCTOR:
                n = 1
                if st.message is not None:
                    raw = st.message.params.get("n", 1)
                    n = int(raw) if isinstance(raw, int | float) else 1
                reason = (
                    self.tr("Setup isn't finished — 1 problem needs fixing")
                    if n == 1
                    else self.tr("Setup isn't finished — {n} problems need fixing").format(n=n)
                )
        if reason is None and self._config_known and self._config is not None:
            if not self._config.exists:
                reason = self.tr("Setup isn't finished — run Setup to choose an engine")
        if reason == self._not_ready_reason:
            self._sync_attach()
            return
        self._not_ready_reason = reason
        if self.not_ready_banner is not None:
            self.not_ready_banner.hide()
            self.not_ready_banner.deleteLater()
            self.not_ready_banner = None
        enabled = reason is None
        for w in (self.open_button, self.drop_zone):
            w.setEnabled(enabled)
            w.setAccessibleDescription(reason or "")
        self._sync_attach()
        if reason is not None:
            banner = Banner(
                "blocking",
                reason,
                self.tr("Opening and attaching are turned off until setup is finished."),
                actions=[(self.tr("Open &Setup"), self._open_setup)],
                parent=self,
            )
            banner.setObjectName("sessionsNotReady")
            self.not_ready_banner = banner
            self._not_ready_lay.addWidget(banner)

    def _open_setup(self) -> None:
        opener = getattr(self.window(), "open_setup", None)
        if callable(opener):
            opener()
        else:
            self.ctx.go("system")

    # ================================================================== misc
    def _maybe_fetch_hardware(self, snap: SessionSnapshot) -> None:
        if self._hardware_asked or snap.backend in (None, BackendId.MVTOOLS):
            return
        caps = self.ctx.bridge.capabilities
        if caps is None or not caps.ok(Feature.HARDWARE):
            return
        self._hardware_asked = True
        self.ctx.bridge.call(
            lambda core: core.hardware(), owner=self, ok=self._on_hardware, err=lambda _e: None
        )

    def _show_view(self) -> None:
        has = bool(self._tracks)
        self.empty_view.setVisible(not has)
        self.sessions_view.setVisible(has)

    def _play_hint(self) -> CommandHint:
        return command_hint("play", file=self.tr("VIDEO"))

    def _dismiss_action(self) -> list[Action]:
        return [(self.tr("Dismiss"), self._clear_message)]

    def _show_message(self, banner: Banner) -> None:
        self._clear_message()
        banner.setObjectName("sessionsMessage")
        self.message_banner = banner
        self._message_lay.addWidget(banner)

    def _clear_message(self) -> None:
        if self.message_banner is not None:
            self.message_banner.hide()
            self.message_banner.deleteLater()
            self.message_banner = None

    def _clear_apply_banner(self) -> None:
        if self.apply_banner is not None:
            self.apply_banner.hide()
            self.apply_banner.deleteLater()
            self.apply_banner = None

    def _say(self, text: str, *, assertive: bool = False) -> None:
        self.ctx.announce(text, assertive=assertive)


__all__ = [
    "DIAGNOSTIC_COMMAND",
    "HealthBannerSpec",
    "SessionsPage",
    "backend_name",
    "bypass_text",
    "counter_delta",
    "ended_text",
    "failure_text",
    "filter_word",
    "fmt_multiplier",
    "fmt_rate",
    "headline",
    "health_banner_spec",
    "health_text",
    "health_word",
    "model_short",
    "gpu_fault_title",
    "xid_numbers",
    "rate_text",
]
