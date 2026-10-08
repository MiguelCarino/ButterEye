# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The simple window (docs/design/GUI.md §12, 2026-10-07 redesign).

One small window: drop or open a video and it plays smooth. One "Smooth
motion" switch, a "Target" choice and a "Smoothness" choice; everything else
(setup, the speed test, profiles and rules) happens silently, and system
information, the speed test, storage and licences live behind "Details".

The three choices are stored as one non-built-in profile, id ``simple``, with
a single catch-all rule pointing at it (built-in profiles are kept). The
on/off switch is a per-user convenience in ``QSettings``; it never touches
``config.toml``. ButterEye never writes the user's ``mpv.conf`` from here.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import shiboken6
from PySide6.QtCore import QCoreApplication, QSettings, QSize, Qt
from PySide6.QtGui import QAction, QCloseEvent, QFont, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    BackendId,
    BackendStatus,
    BenchRequest,
    BenchResult,
    ButterEyeError,
    BypassReason,
    Capabilities,
    CapabilitiesChanged,
    Config,
    ConfigChanged,
    ConfigConflict,
    ConfigLoad,
    DetachPolicy,
    DoctorReport,
    Event,
    EventsDropped,
    Feature,
    FilterState,
    Health,
    HealthChanged,
    JobChanged,
    JobId,
    NotAvailable,
    Notice,
    OperationCancelled,
    OpState,
    PluginStatus,
    Profile,
    Progress,
    Reason,
    RenderJobState,
    Rule,
    RuleMatch,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SessionId,
    SessionSnapshot,
    SetupChoices,
    SetupPlan,
    SetupResult,
    Severity,
    ShutdownReport,
    SourceFacts,
    Target,
    TargetKind,
    render,
    unavailable_text,
)
from buttereye.gui import a11y
from buttereye.gui.bridge import CoreBridge
from buttereye.gui.convert_dialog import (
    Confirm,
    ConvertDialog,
    JobRow,
    SaveChooser,
    open_folder,
)
from buttereye.gui.quit_dialog import quit_needs, run_close_flow
from buttereye.gui.widgets.drop_zone import DropZone, FileChooser, default_chooser
from buttereye.gui.widgets.primary_button import PrimaryButton
from buttereye.gui.widgets.state_panel import split_headline
from buttereye.gui.widgets.status_badge import ICON_DIR, BadgeKind, StatusBadge
from buttereye.gui.widgets.switch import Switch

_log = logging.getLogger(__name__)

APP_NAME = "ButterEye"
APP_ICON = ICON_DIR / "buttereye.svg"
DEFAULT_SIZE = QSize(460, 560)
MINIMUM_SIZE = QSize(360, 420)
SCREEN_FRACTION = 0.9

SIMPLE_ID = "simple"
SIMPLE_NAME = "ButterEye"
MODEL_BEST = "rife-v4.26_ensembleFalse"
MODEL_LIGHT = "rife-v4.22_lite_ensembleFalse"
#: The background speed test when there is no bench history: 1080p film rate.
FIRST_BENCH = BenchRequest(1920, 1080, Fraction(24000, 1001))

SMOOTH_KEY = "simple/smooth"
DISPLAY_HZ_KEY = "simple/display_hz"

#: Smoothness choice -> (backend, model).
SMOOTHNESS: dict[str, tuple[BackendId | str, str | None]] = {
    "auto": ("auto", None),
    "best": (BackendId.RIFE_NCNN, MODEL_BEST),
    "light": (BackendId.RIFE_NCNN, MODEL_LIGHT),
    "cpu": (BackendId.MVTOOLS, None),
}
GPU_CHOICES = frozenset({"best", "light"})
DEFAULT_SMOOTHNESS = "auto"
DEFAULT_TARGET = "x2"

_CODE_SUFFIX = re.compile(r"\s*\(BE-\d+\)\s*$")


def _t(text: str) -> str:
    return QCoreApplication.translate("SimpleWindow", text)


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------


def target_for(key: str) -> Target:
    if key == "fps60":
        return Target(TargetKind.FPS, Fraction(60))
    if key == "display":
        return Target(TargetKind.DISPLAY)
    return Target(TargetKind.X2)


def target_key(target: Target) -> str:
    if target.kind is TargetKind.DISPLAY:
        return "display"
    if target.kind is TargetKind.FPS and target.fps == Fraction(60):
        return "fps60"
    return "x2"


def smoothness_key(profile: Profile) -> str:
    if profile.backend == "auto":
        return "auto"
    if profile.backend is BackendId.MVTOOLS:
        return "cpu"
    if profile.model == MODEL_LIGHT:
        return "light"
    return "best"


def simple_profile(smoothness: str, target: str) -> Profile:
    backend, model = SMOOTHNESS.get(smoothness, SMOOTHNESS[DEFAULT_SMOOTHNESS])
    return Profile(
        id=SIMPLE_ID,
        name=SIMPLE_NAME,
        backend=backend,  # type: ignore[arg-type]  # BackendId | "auto"
        model=model,
        scale=None,
        target=target_for(target),
        sc_threshold=0.12,
        buffered_frames=None,
        concurrent_frames=None,
        hdr="skip",
        builtin=False,
    )


def simple_rules() -> tuple[Rule, ...]:
    return (Rule(RuleMatch(), SIMPLE_ID),)


def simple_config(cfg: Config, profile: Profile) -> Config:
    """``cfg`` with ``profile`` as the only rule target; other profiles kept."""
    others = tuple(p for p in cfg.profiles if p.id != SIMPLE_ID)
    return dataclasses.replace(cfg, profiles=others + (profile,), rules=simple_rules())


def find_simple(cfg: Config) -> Profile | None:
    return next((p for p in cfg.profiles if p.id == SIMPLE_ID and not p.builtin), None)


def is_simple(cfg: Config) -> bool:
    """True when ``cfg`` already routes everything to the simple profile."""
    return find_simple(cfg) is not None and tuple(cfg.rules) == simple_rules()


def fmt_rate(x: Fraction | float) -> str:
    """24 / 23.976 / 119.88: at most three decimals, no trailing zeros."""
    return f"{float(x):.3f}".rstrip("0").rstrip(".")


def short_rate(x: Fraction | float) -> str:
    """Rates as people say them: 23.976 -> 24, 59.94 -> 60, 119.88 -> 120."""
    v = float(x)
    r = round(v)
    # NTSC-style rates are k * 1000/1001: within 0.11 % of the whole number.
    return str(r) if r > 0 and abs(v - r) <= r * 0.0011 else fmt_rate(v)


def without_code(text: str) -> str:
    """Drop a trailing "(BE-1234)": codes belong in Details only."""
    return _CODE_SUFFIX.sub("", text)


def first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ""


def gpu_smoothing_available(
    backends: tuple[BackendStatus, ...] | None, plugins: tuple[PluginStatus, ...] | None
) -> bool | None:
    """Whether RIFE can run here; ``None`` while unknown."""
    if backends is not None:
        return any(b.id is BackendId.RIFE_NCNN and b.available for b in backends)
    if plugins is not None:
        return any(
            "rife" in p.name.lower() and p.active_copy != "none" and p.loads_in_mpv is not False
            for p in plugins
        )
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class RowView:
    """What one "Now playing" row shows."""

    kind: BadgeKind
    word: str
    rates: str
    engine: str
    notice: str
    paused: bool


def engine_words(snap: SessionSnapshot) -> str:
    if snap.filter is FilterState.ACTIVE and snap.backend is not None:
        return _t("CPU smoothing") if snap.backend is BackendId.MVTOOLS else _t("GPU smoothing")
    if snap.filter is FilterState.PENDING:
        return _t("Getting smoothing ready")
    if snap.filter is FilterState.OFF:
        return _t("Smoothing paused")
    return _t("Playing normally")


def bypass_words(snap: SessionSnapshot) -> str:
    texts = {
        BypassReason.ALREADY_AT_RATE: _t(
            "This video already matches your target, so it plays as is."
        ),
        BypassReason.INTERLACED: _t("Interlaced video plays without smoothing."),
        BypassReason.HDR_SKIP: _t("HDR video plays without smoothing."),
        BypassReason.UNSUPPORTED_FORMAT: _t("This video's format can't be smoothed."),
        BypassReason.NO_VIDEO: _t("There's no video to smooth."),
        BypassReason.NO_REALTIME: _t(
            "This computer can't smooth this video in real time, so it plays normally."
        ),
    }
    return texts[snap.bypass] if snap.bypass is not None else ""


def health_words(health: Health) -> str:
    texts = {
        Health.OK: "",
        Health.DROPPING: _t("Some frames are being dropped."),
        Health.STALLED: _t("The video stalled, so smoothing was turned off for it."),
        Health.GPU_FAULT: _t("The GPU had a problem, so smoothing was turned off for this video."),
        Health.DEVICE_LOST: _t("The GPU stopped responding, so smoothing was turned off."),
        Health.CONNECTION_LOST: _t("ButterEye lost touch with this player."),
    }
    return texts[health]


def row_view(snap: SessionSnapshot, note: str | None = None) -> RowView:
    """Plain-language row for ``snap``; ``note`` is the latest event text for it."""
    src = snap.source
    if src is not None and snap.filter is FilterState.ACTIVE and snap.target_fps is not None:
        rates = _t("{src} → {dst} fps").format(
            src=short_rate(src.fps), dst=short_rate(snap.target_fps)
        )
    elif src is not None:
        rates = _t("{src} fps").format(src=short_rate(src.fps))
    else:
        rates = ""
    notice = without_code(render(snap.notice)) if snap.notice is not None else ""
    if not notice and snap.filter is FilterState.BYPASSED:
        notice = bypass_words(snap)
    if not notice and snap.health is not Health.OK:
        notice = health_words(snap.health)
    if not notice and note:
        notice = without_code(note)
    if not notice and snap.filter is FilterState.ROLLED_BACK:
        notice = _t("Smoothing didn't work for this video, so it plays normally.")

    kind: BadgeKind
    if snap.health in (Health.STALLED, Health.GPU_FAULT, Health.DEVICE_LOST):
        kind, word = "degraded", _t("Playing normally")
    elif snap.health is Health.CONNECTION_LOST:
        kind, word = "blocking", _t("Lost touch")
    elif snap.filter is FilterState.ACTIVE:
        if snap.health is Health.DROPPING:
            kind, word = "degraded", _t("Smooth, some drops")
        else:
            kind, word = "ok", _t("Smooth")
    elif snap.filter is FilterState.PENDING:
        kind, word = "busy", _t("Starting")
    elif snap.filter is FilterState.OFF:
        kind, word = "off", _t("Paused")
    elif snap.filter is FilterState.BYPASSED:
        kind, word = "info", _t("Playing normally")
    else:  # ROLLED_BACK
        kind, word = "degraded", _t("Playing normally")
    return RowView(
        kind=kind,
        word=word,
        rates=rates,
        engine=engine_words(snap),
        notice=notice,
        paused=snap.filter in (FilterState.OFF, FilterState.ROLLED_BACK),
    )


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------


class SessionRow(QFrame):
    """One live player: title, rates, engine, plain notice, two actions."""

    def __init__(self, snap: SessionSnapshot, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"session.{snap.sid}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.sid = snap.sid
        self.snap = snap
        lay = QVBoxLayout(self)
        gap = self.fontMetrics().height() // 2
        lay.setContentsMargins(gap * 2, gap * 2, gap * 2, gap * 2)
        lay.setSpacing(gap // 2 + 2)

        top = QHBoxLayout()
        top.setSpacing(gap)
        self.title = QLabel(self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        bold = QFont(self.title.font())
        bold.setBold(True)
        self.title.setFont(bold)
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.badge = StatusBadge("busy", "", self)
        top.addWidget(self.title, 1)
        top.addWidget(self.badge, 0)
        lay.addLayout(top)

        self.detail = QLabel(self)
        self.detail.setObjectName("rowDetail")
        self.detail.setTextFormat(Qt.TextFormat.PlainText)
        self.detail.setWordWrap(True)
        lay.addWidget(self.detail)
        self.notice = QLabel(self)
        self.notice.setObjectName("rowNotice")
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setWordWrap(True)
        self.notice.hide()
        lay.addWidget(self.notice)

        buttons = QHBoxLayout()
        buttons.setSpacing(gap)
        self.pause_button = QPushButton(self)
        self.pause_button.setObjectName("pauseSmoothing")
        self.pause_button.setAutoDefault(False)
        self.let_go_button = QPushButton(_t("Let go"), self)
        self.let_go_button.setObjectName("letGo")
        self.let_go_button.setAutoDefault(False)
        self.let_go_button.setToolTip(_t("mpv keeps playing; ButterEye stops managing it."))
        self.force_button = QPushButton(_t("Smooth anyway"), self)
        self.force_button.setObjectName("smoothAnyway")
        self.force_button.setAutoDefault(False)
        self.force_button.setToolTip(
            _t("Smooth this video even though the speed test says it may stutter.")
        )
        self.force_button.hide()
        self.copy_button = QPushButton(_t("Save smooth copy…"), self)
        self.copy_button.setObjectName("saveCopy")
        self.copy_button.setAutoDefault(False)
        self.copy_button.setToolTip(_t("Convert this video into a smooth file you can keep."))
        self.copy_button.hide()
        buttons.addWidget(self.force_button)
        buttons.addWidget(self.pause_button)
        buttons.addWidget(self.let_go_button)
        buttons.addWidget(self.copy_button)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self.update_snapshot(snap)

    def update_snapshot(self, snap: SessionSnapshot, note: str | None = None) -> None:
        self.snap = snap
        view = row_view(snap, note)
        self.view = view
        self.title.setText(snap.title)
        self.title.setToolTip(snap.title)
        self.badge.set_state(view.kind, view.word)
        engine = view.engine if view.engine != view.word else ""  # say it once
        parts = [p for p in (view.rates, engine) if p]
        self.detail.setText(" · ".join(parts))
        self.notice.setText(view.notice)
        self.notice.setVisible(bool(view.notice))
        self.pause_button.setText(_t("Resume smoothing") if view.paused else _t("Pause smoothing"))
        self.pause_button.setAccessibleName(
            (
                _t("Resume smoothing for {title}")
                if view.paused
                else _t("Pause smoothing for {title}")
            ).format(title=snap.title)
        )
        self.pause_button.setEnabled(
            snap.health is not Health.CONNECTION_LOST
            and snap.filter not in (FilterState.PENDING, FilterState.BYPASSED)
        )
        self.force_button.setVisible(
            snap.filter is FilterState.BYPASSED
            and snap.bypass is BypassReason.NO_REALTIME
            and snap.health is not Health.CONNECTION_LOST
        )
        self.force_button.setAccessibleName(_t("Smooth {title} anyway").format(title=snap.title))
        self.copy_button.setAccessibleName(
            _t("Save a smooth copy of {title}").format(title=snap.title)
        )
        self.let_go_button.setAccessibleName(_t("Let go of {title}").format(title=snap.title))
        self.let_go_button.setAccessibleDescription(
            _t("mpv keeps playing; ButterEye stops managing it.")
        )
        self.setAccessibleName(
            " — ".join(p for p in (snap.title, view.word, self.detail.text(), view.notice) if p)
        )


class SimpleWindow(QMainWindow):
    """ButterEye's default window (§12)."""

    def __init__(
        self,
        bridge: CoreBridge,
        settings: QSettings,
        *,
        log_path: Path | None = None,
        auto_setup: bool = True,
        auto_bench: bool = True,
        chooser: FileChooser | None = None,
        save_chooser: SaveChooser | None = None,
        confirm_replace: Confirm | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("simpleWindow")
        self.setWindowTitle(APP_NAME)
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.bridge = bridge
        self.settings = settings
        self.log_path = log_path
        self.auto_setup = auto_setup
        self.auto_bench = auto_bench
        self.shutdown_timeout_s = 5.0
        self.last_shutdown: ShutdownReport | None = None
        self.config_load: ConfigLoad | None = None
        self.report: DoctorReport | None = None
        self.gpu_available: bool | None = None
        self.bench_started = False
        self.setup_started = False
        self._may_close = False
        self._in_close_flow = False
        self._sessions: dict[SessionId, SessionSnapshot] = {}
        self._rows: dict[SessionId, SessionRow] = {}
        self._notes: dict[SessionId, str] = {}
        self._needs_apply: set[SessionId] = set()
        self._saving = False
        self._save_again = False
        self._retried_conflict = False
        self._bench_running = False
        self._busy: str | None = None
        self._details: Any = None
        self._display_hz: float | None = _float(settings.value(DISPLAY_HZ_KEY))
        self._updating = False
        self._smooth_key = DEFAULT_SMOOTHNESS
        self._target_key = DEFAULT_TARGET
        self._jobs: dict[JobId, RenderJobState] = {}
        self._job_rows: dict[JobId, JobRow] = {}
        self._convert_visible = False
        self.save_chooser = save_chooser
        self.confirm_replace = confirm_replace
        self.open_folder: Callable[[Path], None] = open_folder
        self.convert_dialog: ConvertDialog | None = None
        self._chooser: FileChooser = chooser or default_chooser(
            self.tr("Choose a video to convert"),
            self.tr("Video files (*.mkv *.mp4 *.webm *.mov *.avi *.m2ts *.ts);;All files (*)"),
        )

        self._build(chooser)
        self._build_actions()
        self._restore_size()

        self._remove_listener = a11y.add_announcement_listener(self._mirror_announcement)
        self.destroyed.connect(lambda _o=None: self._remove_listener())

        bridge.ready.connect(self._on_ready)
        bridge.fatal.connect(self._on_fatal)
        bridge.event.connect(self._on_event)
        bridge.unresponsive.connect(self._on_unresponsive)
        self.set_status("busy", self.tr("Getting ready…"))
        if bridge.is_ready and bridge.capabilities is not None:
            self._on_ready(bridge.capabilities)
        elif bridge.startup_error is not None:
            self._on_fatal(bridge.startup_error)

    # ------------------------------------------------------------------ building
    def _build(self, chooser: FileChooser | None) -> None:
        fm = self.fontMetrics()
        unit = fm.height()

        central = QWidget(self)
        central.setObjectName("simpleCentral")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.setCentralWidget(central)

        scroll = QScrollArea(central)
        scroll.setObjectName("simpleScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        body = QWidget()
        body.setObjectName("simpleBody")
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(unit * 3 // 2, unit * 3 // 2, unit * 3 // 2, unit)
        lay.setSpacing(unit)

        # header
        head = QHBoxLayout()
        head.setSpacing(unit * 3 // 4)
        self.logo = QLabel(body)
        self.logo.setObjectName("logo")
        self.logo.setAccessibleName("")
        self._set_logo()
        head.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignVCenter)
        names = QVBoxLayout()
        names.setSpacing(0)
        self.app_title = a11y.heading(QLabel(APP_NAME, body), factor=1.8)
        self.app_title.setObjectName("appTitle")
        self.app_title.setTextFormat(Qt.TextFormat.PlainText)
        self.tagline = QLabel(self.tr("Smoother motion for the videos you play in mpv."), body)
        self.tagline.setObjectName("tagline")
        self.tagline.setTextFormat(Qt.TextFormat.PlainText)
        self.tagline.setWordWrap(True)
        names.addWidget(self.app_title)
        names.addWidget(self.tagline)
        head.addLayout(names, 1)
        lay.addLayout(head)

        # inline problem line (never modal)
        self.problem = QFrame(body)
        self.problem.setObjectName("problemLine")
        self.problem.setFrameShape(QFrame.Shape.StyledPanel)
        pl = QHBoxLayout(self.problem)
        pl.setContentsMargins(unit // 2, unit // 2, unit // 2, unit // 2)
        self.problem_badge = StatusBadge("blocking", "", self.problem)
        pl.addWidget(self.problem_badge, 1)
        self.problem_details = QPushButton(self.tr("Details"), self.problem)
        self.problem_details.setObjectName("problemDetails")
        self.problem_details.setFlat(True)
        self.problem_details.setAutoDefault(False)
        self.problem_details.setAccessibleName(self.tr("Show details about this problem"))
        self.problem_details.clicked.connect(lambda: self.open_details("system"))
        pl.addWidget(self.problem_details, 0, Qt.AlignmentFlag.AlignTop)
        self.problem.hide()
        lay.addWidget(self.problem)

        # drop zone + Open
        self.drop = DropZone(self.tr("Drop a video here"), body, chooser=chooser)
        self.drop.setObjectName("dropZone")
        self.drop.set_filled(True)
        self.drop.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the Open button is the keyboard path
        self.drop.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.drop.setMinimumHeight(unit * 7)
        big = QFont(self.drop.label.font())
        if big.pointSizeF() > 0:
            big.setPointSizeF(big.pointSizeF() * 1.25)
        self.drop.label.setFont(big)
        dl = self.drop.layout()
        assert isinstance(dl, QVBoxLayout)
        dl.setContentsMargins(unit, unit * 3 // 2, unit, unit * 3 // 2)
        dl.setSpacing(unit * 3 // 4)
        dl.insertStretch(0, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        self.or_label = QLabel(self.tr("or"), self.drop)
        self.or_label.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.or_label)
        self.open_button = PrimaryButton(self.tr("&Open video…"), self.drop)
        self.open_button.setObjectName("openVideo")
        self.open_button.setAccessibleName(self.tr("Open video"))
        self.open_button.setDefault(True)
        self.open_button.setMinimumHeight(round(unit * 2.2))
        self.open_button.setMinimumWidth(unit * 8)
        self.open_button.clicked.connect(self.drop.choose)
        row.addWidget(self.open_button)
        row.addStretch(1)
        dl.addLayout(row)
        crow = QHBoxLayout()
        crow.addStretch(1)
        self.convert_button = QPushButton(self.tr("&Convert a video…"), self.drop)
        self.convert_button.setObjectName("convertVideo")
        self.convert_button.setAccessibleName(self.tr("Convert a video"))
        self.convert_button.setAccessibleDescription(
            self.tr("Save a smooth copy of a video as a new file.")
        )
        self.convert_button.setAutoDefault(False)
        self.convert_button.clicked.connect(self.convert_video)
        self.convert_button.hide()
        crow.addWidget(self.convert_button)
        crow.addStretch(1)
        dl.addLayout(crow)
        dl.addStretch(1)
        self.drop.fileChosen.connect(self.play_file)
        self.drop.rejected.connect(lambda text: self.set_status("info", without_code(text)))
        lay.addWidget(self.drop, 1)

        # settings
        grid = QGridLayout()
        grid.setHorizontalSpacing(unit)
        grid.setVerticalSpacing(unit * 3 // 4)
        grid.setColumnStretch(1, 1)
        self.smooth_switch = Switch(self.tr("Smooth motion"), body)
        self.smooth_switch.setObjectName("smoothSwitch")
        self.smooth_switch.setChecked(_bool(self.settings.value(SMOOTH_KEY), True))
        self.smooth_switch.toggled.connect(self._on_smooth_toggled)
        label, _ = a11y.labelled(self.tr("&Smooth motion"), self.smooth_switch)
        self.smooth_switch.setAccessibleDescription(self.smooth_switch.state_text())
        grid.addWidget(label, 0, 0)
        grid.addWidget(self.smooth_switch, 0, 1, Qt.AlignmentFlag.AlignLeft)

        self.target_combo = QComboBox(body)
        self.target_combo.setObjectName("targetCombo")
        self._fill_targets()
        tlabel, _ = a11y.labelled(
            self.tr("&Target"),
            self.target_combo,
            description=self.tr("How many frames per second smoothing aims for."),
        )
        grid.addWidget(tlabel, 1, 0)
        grid.addWidget(self.target_combo, 1, 1)
        self.target_combo.currentIndexChanged.connect(lambda _i: self._on_choice_changed())

        self.smooth_combo = QComboBox(body)
        self.smooth_combo.setObjectName("smoothnessCombo")
        self._fill_smoothness()
        slabel, _ = a11y.labelled(
            self.tr("Smoot&hness"),
            self.smooth_combo,
            description=self.tr("Auto picks GPU smoothing when your GPU is fast enough."),
        )
        grid.addWidget(slabel, 2, 0)
        grid.addWidget(self.smooth_combo, 2, 1)
        self.smooth_combo.currentIndexChanged.connect(lambda _i: self._on_choice_changed())
        lay.addLayout(grid)

        # now playing
        self.playing = QWidget(body)
        self.playing.setObjectName("nowPlaying")
        pl2 = QVBoxLayout(self.playing)
        pl2.setContentsMargins(0, unit // 2, 0, 0)
        pl2.setSpacing(unit // 2)
        self.playing_heading = a11y.heading(QLabel(self.tr("Now playing"), self.playing))
        self.playing_heading.setTextFormat(Qt.TextFormat.PlainText)
        pl2.addWidget(self.playing_heading)
        self.rows_box = QVBoxLayout()
        self.rows_box.setSpacing(unit // 2)
        pl2.addLayout(self.rows_box)
        self.playing.hide()
        lay.addWidget(self.playing)

        # saving copies (offline render, §12.7)
        self.copies = QWidget(body)
        self.copies.setObjectName("savingCopies")
        cl = QVBoxLayout(self.copies)
        cl.setContentsMargins(0, unit // 2, 0, 0)
        cl.setSpacing(unit // 2)
        self.copies_heading = a11y.heading(QLabel(self.tr("Saving copies"), self.copies))
        self.copies_heading.setTextFormat(Qt.TextFormat.PlainText)
        cl.addWidget(self.copies_heading)
        self.jobs_box = QVBoxLayout()
        self.jobs_box.setSpacing(unit // 2)
        cl.addLayout(self.jobs_box)
        self.copies.hide()
        lay.addWidget(self.copies)
        lay.addStretch(0)

        # bottom status line
        line = QFrame(central)
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        outer.addWidget(line)
        bottom = QWidget(central)
        bottom.setObjectName("statusLine")
        bl = QHBoxLayout(bottom)
        bl.setContentsMargins(unit * 3 // 2, unit // 2, unit, unit // 2)
        self.status_badge = StatusBadge("busy", "", bottom)
        self.status_badge.setObjectName("statusText")
        bl.addWidget(self.status_badge, 1)
        self.details_button = QPushButton(self.tr("&Details ▸"), bottom)
        self.details_button.setObjectName("detailsButton")
        self.details_button.setAccessibleName(self.tr("Details"))
        self.details_button.setAccessibleDescription(
            self.tr("System information, speed test, storage and licences")
        )
        self.details_button.setFlat(True)
        self.details_button.setAutoDefault(False)
        self.details_button.clicked.connect(lambda: self.open_details())
        bl.addWidget(self.details_button, 0)
        outer.addWidget(bottom)

        self.setTabOrder(self.open_button, self.convert_button)
        self.setTabOrder(self.convert_button, self.smooth_switch)
        self.setTabOrder(self.smooth_switch, self.target_combo)
        self.setTabOrder(self.target_combo, self.smooth_combo)

    def _set_logo(self) -> None:
        size = round(self.fontMetrics().height() * 3.2)
        icon = QIcon(str(APP_ICON))
        self.logo.setPixmap(icon.pixmap(QSize(size, size), self.devicePixelRatioF()))
        self.logo.setMinimumSize(QSize(size, size))

    def _build_actions(self) -> None:
        def act(text: str, slot: Callable[[], object], key: str, name: str) -> QAction:
            a = QAction(text, self)
            a.setObjectName(name)
            a.setShortcut(QKeySequence(key))
            a.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
            a.triggered.connect(lambda _c=False: slot())
            self.addAction(a)
            return a

        self.act_open = act(self.tr("Open video…"), self.drop.choose, "Ctrl+O", "act.open")
        self.act_quit = act(self.tr("Quit"), self.close, "Ctrl+Q", "act.quit")
        self.act_details = act(self.tr("Details"), self.open_details, "F1", "act.details")
        self.act_convert = act(
            self.tr("Convert a video…"), self.convert_video, "Ctrl+Shift+O", "act.convert"
        )
        self.act_convert.setEnabled(False)

    def _fill_targets(self) -> None:
        combo = self.target_combo
        self._updating = True
        try:
            combo.clear()
            combo.addItem(self.tr("Double (2×)"), "x2")
            combo.addItem(self.tr("60 fps"), "fps60")
            if self._display_hz:
                text = self.tr("Your display ({hz} Hz)").format(hz=short_rate(self._display_hz))
            else:
                text = self.tr("Your display")
            combo.addItem(text, "display")
            combo.setCurrentIndex(max(combo.findData(self._target_key), 0))
        finally:
            self._updating = False

    def _fill_smoothness(self) -> None:
        """Smoothness items; GPU choices are hidden when RIFE can't run here
        (a GPU choice already in use stays, marked "not available here")."""
        combo = self.smooth_combo
        current = self._smooth_key
        self._updating = True
        try:
            combo.clear()
            combo.addItem(self.tr("Auto (recommended)"), "auto")
            for key, text in (
                ("best", self.tr("Best quality — GPU")),
                ("light", self.tr("Lighter — GPU")),
            ):
                if self.gpu_available is False:
                    if current != key:
                        continue
                    text = self.tr("{choice} (not available here)").format(choice=text)
                combo.addItem(text, key)
            combo.addItem(self.tr("CPU only"), "cpu")
            combo.setCurrentIndex(max(combo.findData(current), 0))
        finally:
            self._updating = False

    # ------------------------------------------------------------------ choices
    def smoothness(self) -> str:
        return self._smooth_key

    def target(self) -> str:
        return self._target_key

    def profile(self) -> Profile:
        return simple_profile(self.smoothness(), self.target())

    def _show_config_choices(self, cfg: Config) -> None:
        prof = find_simple(cfg)
        if prof is None:
            return
        self._target_key = target_key(prof.target)
        self._smooth_key = smoothness_key(prof)
        self._fill_targets()
        self._fill_smoothness()

    def _on_choice_changed(self) -> None:
        if self._updating:
            return
        self._target_key = str(self.target_combo.currentData() or DEFAULT_TARGET)
        self._smooth_key = str(self.smooth_combo.currentData() or DEFAULT_SMOOTHNESS)
        self.persist()

    def _on_smooth_toggled(self, on: bool) -> None:
        self.settings.setValue(SMOOTH_KEY, on)
        self.settings.sync()
        for snap in self.live_sessions:
            paused = snap.filter is FilterState.OFF
            if on and paused or not on and not paused:
                self._set_interpolation(snap.sid, on)
        self._announce(self.tr("Smooth motion on.") if on else self.tr("Smooth motion off."), False)

    @property
    def smooth_on(self) -> bool:
        return self.smooth_switch.isChecked()

    # ------------------------------------------------------------------ status
    def set_status(self, kind: BadgeKind, text: str) -> None:
        self.status_badge.set_state(kind, text)

    def status_text(self) -> str:
        return self.status_badge.text()

    def _announce(self, text: str, assertive: bool) -> None:
        a11y.announce(self.status_badge, text, assertive=assertive)

    def _mirror_announcement(self, w: QWidget, text: str, _assertive: bool) -> None:
        # Only our own announcements (the Details dialog has its own line).
        if shiboken6.isValid(self) and w is self.status_badge:
            self.set_status(self.status_badge.kind(), text)

    def _ready(self) -> None:
        if self._busy is None and not self._bench_running:
            self.set_status("ok", self.tr("Ready."))

    def show_problem(self, kind: BadgeKind, text: str, *, details: bool = True) -> None:
        self.problem_badge.set_state(kind, text)
        self.problem_details.setVisible(details)
        self.problem.show()
        a11y.announce(self.problem_badge, text, assertive=kind == "blocking")

    def hide_problem(self) -> None:
        self.problem.hide()

    def problem_text(self) -> str:
        return self.problem_badge.text() if self.problem.isVisible() else ""

    # ------------------------------------------------------------------ startup
    def _caps(self) -> Capabilities | None:
        return self.bridge.capabilities

    def _ok(self, feature: Feature) -> bool:
        caps = self._caps()
        return caps is not None and feature in caps.states and caps.ok(feature)

    def _on_ready(self, caps: Capabilities) -> None:
        self._apply_caps(caps)
        if self._ok(Feature.RENDER):
            self.bridge.call(
                lambda core: core.render_jobs(), owner=self, ok=self._seed_jobs, err=self._quiet
            )
        if self._ok(Feature.LIVE):
            self.bridge.call(
                lambda core: core.sessions(), owner=self, ok=self._seed, err=self._quiet
            )
        if self._ok(Feature.CONFIG):
            self.bridge.call(
                lambda core: core.load_config(),
                owner=self,
                ok=self._on_startup_config,
                err=self._startup_failed,
            )
        else:
            self._busy = None
            self._ready()

    def _apply_caps(self, caps: Capabilities) -> None:
        rst = caps.states.get(Feature.RENDER)
        # hidden only when this build can't convert at all; a missing ffms2 still
        # shows the buttons, and the dialog says what to install (§12.7)
        self._convert_visible = rst is not None and (
            rst.available or rst.reason is not Reason.NOT_IMPLEMENTED
        )
        self.convert_button.setVisible(self._convert_visible)
        self.act_convert.setEnabled(self._convert_visible)
        for row in self._rows.values():
            row.copy_button.setVisible(self._convert_visible and row.snap.source is not None)
        st = caps.states.get(Feature.LIVE)
        live_ok = st is not None and st.available
        self.drop.setEnabled(live_ok)
        self.open_button.setEnabled(live_ok)
        self.act_open.setEnabled(live_ok)
        if st is not None and not st.available:
            why = without_code(split_headline(render(unavailable_text(Feature.LIVE, st)))[0])
            self.drop.setToolTip(why)
            if self.report is None or not self.report.blocking:
                self.show_problem("blocking", why)  # the doctor's sentence is more specific
        else:
            self.drop.setToolTip("")

    def _seed(self, snaps: tuple[SessionSnapshot, ...]) -> None:
        for snap in snaps:
            self._upsert(snap)

    def _quiet(self, err: ButterEyeError) -> None:
        if not isinstance(err, NotAvailable | OperationCancelled):
            _log.warning("background call failed: %s", err)

    def _startup_failed(self, err: ButterEyeError) -> None:
        self._quiet(err)
        self._busy = None
        if not isinstance(err, NotAvailable):
            self.show_problem("degraded", without_code(first_line(render(err.cause))))
        self._ready()

    def _on_startup_config(self, load: ConfigLoad) -> None:
        self.config_load = load
        self._show_config_choices(load.config)
        if not load.exists and self.auto_setup and self._ok(Feature.SETUP):
            self._busy = "setup"
            self.set_status("busy", self.tr("Getting ready…"))
            self._run_doctor(self._setup_after_doctor)
            return
        self._adopt()
        if self._ok(Feature.DOCTOR):
            self._busy = "doctor"
            self.set_status("busy", self.tr("Checking your system…"))
            self._run_doctor(self._after_doctor)
        else:
            self._busy = None
            self._maybe_bench()
            self._ready()

    def _run_doctor(self, then: Callable[[DoctorReport], None]) -> None:
        self.bridge.run_op(
            lambda core: core.doctor(), owner=self, ok=then, err=self._startup_failed
        )

    def _note_report(self, report: DoctorReport) -> None:
        self.report = report
        blocking = [f for f in report.findings if f.severity is Severity.BLOCKING]
        if blocking:
            title = without_code(render(blocking[0].title))
            self.show_problem(
                "blocking",
                self.tr("ButterEye can't smooth video yet: {problem}.").format(
                    problem=title.rstrip(".")
                ),
            )
        elif self.problem_badge.kind() == "blocking" and self.problem.isVisible():
            if self._ok(Feature.LIVE):
                self.hide_problem()
        self._learn_gpu(report)

    def _learn_gpu(self, report: DoctorReport) -> None:
        if self._ok(Feature.SETUP):
            self.bridge.call(
                lambda core: core.backends(report),
                owner=self,
                ok=lambda b: self._set_gpu(gpu_smoothing_available(b, None)),
                err=lambda _e: self._learn_gpu_from_plugins(report),
            )
        else:
            self._learn_gpu_from_plugins(report)

    def _learn_gpu_from_plugins(self, report: DoctorReport) -> None:
        if report.probe is not None:
            self._set_gpu(gpu_smoothing_available(None, report.probe.plugins))
            return
        if self._ok(Feature.DOCTOR):
            self.bridge.call(
                lambda core: core.plugins(),
                owner=self,
                ok=lambda p: self._set_gpu(gpu_smoothing_available(None, p)),
                err=self._quiet,
            )

    def _set_gpu(self, available: bool | None) -> None:
        if available is None or available == self.gpu_available:
            return
        self.gpu_available = available
        self._fill_smoothness()

    def _after_doctor(self, report: DoctorReport) -> None:
        self._note_report(report)
        self._busy = None
        self._maybe_bench()
        self._ready()

    # silent first run -----------------------------------------------------
    def _setup_after_doctor(self, report: DoctorReport) -> None:
        self._note_report(report)
        if report.blocking:
            self._busy = None
            self._ready()
            return
        self.setup_started = True
        self.bridge.call(
            lambda core: core.setup_plan(report, trt_experimental=False),
            owner=self,
            ok=self._setup_apply,
            err=self._startup_failed,
        )

    def _setup_apply(self, plan: SetupPlan) -> None:
        available = [b.id for b in plan.backends if b.available and not b.experimental]
        backend = plan.proposed.backend
        if backend is None or backend not in available:
            backend = next(
                (b for b in (BackendId.RIFE_NCNN, BackendId.MVTOOLS) if b in available), None
            )
        self._set_gpu(gpu_smoothing_available(plan.backends, None))
        if backend is None:
            self._busy = None
            self.show_problem(
                "blocking", self.tr("No smoothing engine works on this computer yet.")
            )
            self._ready()
            return
        choices = SetupChoices(
            backend=backend, trt_experimental=False, confirmed_downloads=frozenset()
        )
        self.bridge.run_op(
            lambda core: core.setup_apply(plan, choices),
            owner=self,
            ok=self._setup_done,
            err=self._startup_failed,
            progress=lambda _p: self.set_status("busy", self.tr("Getting ready…")),
        )

    def _setup_done(self, result: SetupResult) -> None:
        if not result.smoke_ok:
            self.show_problem(
                "degraded",
                self.tr("ButterEye's test run had trouble; videos may play without smoothing."),
            )
        self.bridge.call(
            lambda core: core.load_config(),
            owner=self,
            ok=self._after_setup_config,
            err=self._startup_failed,
        )

    def _after_setup_config(self, load: ConfigLoad) -> None:
        self.config_load = load
        self._busy = None
        self._adopt()
        self._maybe_bench()
        self._ready()

    # background speed test -----------------------------------------------
    def _maybe_bench(self) -> None:
        if not self.auto_bench or self.bench_started or not self._ok(Feature.BENCH):
            return
        if self.report is not None and self.report.blocking:
            return
        self.bridge.call(
            lambda core: core.bench_history(), owner=self, ok=self._on_history, err=self._quiet
        )

    def _on_history(self, history: tuple[BenchResult, ...]) -> None:
        if history or self.bench_started:
            return
        self.bench_started = True
        self._bench_running = True
        self.set_status("busy", self._bench_text())
        self.bridge.run_op(
            lambda core: core.bench(FIRST_BENCH),
            owner=self,
            ok=self._bench_done,
            err=self._bench_failed,
            progress=self._bench_progress,
        )

    def _bench_text(self, pct: int | None = None) -> str:
        if self.gpu_available is False:
            base = self.tr("Measuring your computer (about a minute)…")
        else:
            base = self.tr("Measuring your GPU (about a minute)…")
        return f"{base} {pct}%" if pct else base

    def _bench_progress(self, p: Progress) -> None:
        pct = None
        if p.total and p.done is not None:
            pct = max(0, min(100, round(100 * p.done / p.total)))
        if self._busy is None:
            self.set_status("busy", self._bench_text(pct))

    def _bench_done(self, _result: BenchResult) -> None:
        self._bench_running = False
        self._ready()

    def _bench_failed(self, err: ButterEyeError) -> None:
        self._bench_running = False
        self._quiet(err)
        if isinstance(err, OperationCancelled):
            self._ready()
            return
        self.set_status("info", self.tr("Ready. The speed test didn't finish; see Details."))

    # ------------------------------------------------------------------ persisting
    def _adopt(self) -> None:
        """Route everything to the simple profile. A config that already does
        is the truth (the combos show it); otherwise the current choices are
        saved (first run, or a config made by the classic window)."""
        load = self.config_load
        if load is None:
            return
        if is_simple(load.config):
            self._show_config_choices(load.config)
            return
        self.persist()

    def _read_only_problem(self) -> None:
        self.show_problem(
            "degraded",
            self.tr("Your settings were saved by a newer ButterEye, so changes here aren't kept."),
        )

    def persist(self) -> None:
        """Save the current choices as the simple profile, then apply them live."""
        load = self.config_load
        if load is None or not self._ok(Feature.CONFIG):
            return
        if load.read_only:
            self._read_only_problem()
            return
        if self._saving:
            self._save_again = True
            return
        self._saving = True
        cfg = simple_config(load.config, self.profile())
        rev = load.revision
        self.bridge.call(
            lambda core: core.save_config(cfg, expected_revision=rev),
            owner=self,
            ok=lambda new_rev: self._saved(cfg, new_rev),
            err=self._save_failed,
        )

    def _saved(self, cfg: Config, revision: str) -> None:
        self._saving = False
        self._retried_conflict = False
        if self.config_load is not None:
            self.config_load = dataclasses.replace(
                self.config_load, config=cfg, revision=revision, exists=True, used_defaults=False
            )
        if self._save_again:
            self._save_again = False
            self.persist()
            return
        self.apply_to_live()

    def _save_failed(self, err: ButterEyeError) -> None:
        self._saving = False
        if isinstance(err, ConfigConflict) and not self._retried_conflict:
            self._retried_conflict = True
            self.bridge.call(
                lambda core: core.load_config(),
                owner=self,
                ok=self._reloaded_after_conflict,
                err=self._save_failed,
            )
            return
        self._retried_conflict = False
        self._quiet(err)
        self.set_status(
            "degraded",
            self.tr("Couldn't save your choice: {cause}").format(
                cause=without_code(first_line(render(err.cause)))
            ),
        )

    def _reloaded_after_conflict(self, load: ConfigLoad) -> None:
        self.config_load = load
        self.persist()  # the user's latest choice, on top of what is on disk now

    def apply_to_live(self) -> None:
        for snap in self.live_sessions:
            if snap.filter is FilterState.OFF:
                self._needs_apply.add(snap.sid)  # applied when smoothing resumes
                continue
            self._apply_profile(snap.sid)

    def _apply_profile(self, sid: SessionId) -> None:
        self.bridge.call(
            lambda core: core.apply_profile(sid, SIMPLE_ID),
            owner=self,
            ok=self._upsert,
            err=self._live_failed,
        )

    def smooth_anyway(self, sid: SessionId) -> None:
        """Smooth a "too demanding" video regardless of the speed test."""
        self.bridge.call(
            lambda core: core.set_interpolation(sid, True, force=True),
            owner=self,
            ok=self._upsert,
            err=self._live_failed,
        )

    def _set_interpolation(self, sid: SessionId, enabled: bool) -> None:
        def done(snap: SessionSnapshot) -> None:
            self._upsert(snap)
            if enabled and sid in self._needs_apply:
                self._needs_apply.discard(sid)
                self._apply_profile(sid)

        self.bridge.call(
            lambda core: core.set_interpolation(sid, enabled),
            owner=self,
            ok=done,
            err=self._live_failed,
        )

    def _live_failed(self, err: ButterEyeError) -> None:
        self._quiet(err)
        self.set_status("degraded", without_code(first_line(render(err.cause))))

    # ------------------------------------------------------------------ playing
    def play_file(self, path: Path) -> None:
        if not self._ok(Feature.LIVE):
            caps = self._caps()
            st = caps.states.get(Feature.LIVE) if caps is not None else None
            if st is not None:
                why = split_headline(render(unavailable_text(Feature.LIVE, st)))[0]
                self.set_status("info", without_code(why))
            return
        file = Path(path)
        profile_id = (
            SIMPLE_ID
            if self.config_load is not None and find_simple(self.config_load.config)
            else None
        )
        self.set_status("busy", self.tr("Starting {name}…").format(name=file.name))
        self.bridge.run_op(
            lambda core: core.play(file, profile_id=profile_id),
            owner=self,
            ok=self._played,
            err=self._play_failed,
        )

    def _played(self, sid: SessionId) -> None:
        if not self.smooth_on:
            self._set_interpolation(sid, False)
        self._ready()

    def _play_failed(self, err: ButterEyeError) -> None:
        if isinstance(err, OperationCancelled):
            self._ready()
            return
        self._quiet(err)
        self.set_status(
            "degraded",
            self.tr("Couldn't play that video: {cause}").format(
                cause=without_code(first_line(render(err.cause)))
            ),
        )

    def toggle_session(self, sid: SessionId) -> None:
        snap = self._sessions.get(sid)
        if snap is None:
            return
        self._set_interpolation(sid, row_view(snap).paused)

    def let_go(self, sid: SessionId) -> None:
        """Detach: mpv keeps playing, ButterEye stops managing it."""
        self.bridge.call(
            lambda core: core.detach(sid, DetachPolicy.KEEP_FILTER),
            owner=self,
            ok=lambda _r: None,
            err=self._live_failed,
        )

    # ------------------------------------------------------------------ copies
    def convert_video(self) -> None:
        """ "Convert a video…": choose a file, then the Save smooth copy dialog."""
        if not self._convert_visible:
            return
        path = self._chooser(self)
        if path is not None:
            self.convert_file(path)

    def save_copy(self, sid: SessionId) -> None:
        snap = self._sessions.get(sid)
        if snap is not None and snap.source is not None:
            self.convert_file(Path(snap.source.path))

    def convert_file(self, path: Path) -> ConvertDialog:
        dlg = ConvertDialog(
            self.bridge,
            Path(path),
            target_key=self.target(),
            save_chooser=self.save_chooser,
            confirm=self.confirm_replace,
            parent=self,
        )
        dlg.enqueued.connect(self._copy_enqueued)
        dlg.finished.connect(lambda _r: dlg.deleteLater())
        self.convert_dialog = dlg
        dlg.open()
        return dlg

    def _copy_enqueued(self, job: str) -> None:
        self.set_status("busy", self.tr("Saving a smooth copy…"))
        self.bridge.call(
            lambda core: core.render_jobs(), owner=self, ok=self._seed_jobs, err=self._quiet
        )

    def _seed_jobs(self, jobs: tuple[RenderJobState, ...]) -> None:
        for job in jobs:
            self._upsert_job(job)

    def _upsert_job(self, job: RenderJobState) -> None:
        self._jobs[job.id] = job
        row = self._job_rows.get(job.id)
        if row is None:
            row = JobRow(job, self.copies)
            jid = job.id
            row.cancel_button.clicked.connect(lambda: self.cancel_job(jid))
            row.remove_button.clicked.connect(lambda: self.forget_job(jid))
            row.show_button.clicked.connect(lambda: self.show_job(jid))
            self._job_rows[job.id] = row
            self.jobs_box.addWidget(row)
            self.copies.show()
        else:
            row.update_job(job)
        if job.state is OpState.SUCCEEDED:
            self.set_status("ok", self.tr("Saved {name}.").format(name=job.spec.output.name))
        elif job.state is OpState.FAILED:
            self.set_status(
                "degraded",
                self.tr("Couldn't save {name}.").format(name=job.spec.output.name),
            )

    @property
    def job_rows(self) -> dict[JobId, JobRow]:
        return self._job_rows

    def cancel_job(self, job: JobId) -> None:
        self.bridge.call(
            lambda core: core.render_cancel(job), owner=self, ok=lambda _r: None, err=self._quiet
        )

    def forget_job(self, job: JobId) -> None:
        def gone(_r: object) -> None:
            self._jobs.pop(job, None)
            row = self._job_rows.pop(job, None)
            if row is not None:
                self.jobs_box.removeWidget(row)
                row.hide()
                row.deleteLater()
            self.copies.setVisible(bool(self._job_rows))

        self.bridge.call(lambda core: core.render_forget(job), owner=self, ok=gone, err=self._quiet)

    def show_job(self, job: JobId) -> None:
        state = self._jobs.get(job)
        if state is not None:
            self.open_folder(state.spec.output)

    # ------------------------------------------------------------------ sessions
    @property
    def live_sessions(self) -> tuple[SessionSnapshot, ...]:
        return tuple(s for s in self._sessions.values() if not s.ended)

    @property
    def rows(self) -> dict[SessionId, SessionRow]:
        return dict(self._rows)

    def current_facts(self) -> SourceFacts | None:
        for snap in reversed(self.live_sessions):
            if snap.source is not None:
                return snap.source
        return None

    def _upsert(self, snap: SessionSnapshot) -> None:
        if snap.ended:
            self._remove(snap.sid)
            return
        self._sessions[snap.sid] = snap
        hz = snap.display_fps or (snap.source.display_hz if snap.source is not None else None)
        if hz and hz > 0 and (self._display_hz is None or abs(hz - self._display_hz) > 0.01):
            self._display_hz = float(hz)
            self.settings.setValue(DISPLAY_HZ_KEY, float(hz))
            self._fill_targets()
        row = self._rows.get(snap.sid)
        if row is None:
            row = SessionRow(snap, self.playing)
            sid = snap.sid
            row.pause_button.clicked.connect(lambda: self.toggle_session(sid))
            row.let_go_button.clicked.connect(lambda: self.let_go(sid))
            row.force_button.clicked.connect(lambda: self.smooth_anyway(sid))
            row.copy_button.clicked.connect(lambda: self.save_copy(sid))
            self._rows[snap.sid] = row
            self.rows_box.addWidget(row)
            self.playing.show()
            self.setTabOrder(self.smooth_combo, row.pause_button)
        row.update_snapshot(snap, self._notes.get(snap.sid))
        row.copy_button.setVisible(self._convert_visible and snap.source is not None)

    def _remove(self, sid: SessionId) -> None:
        self._sessions.pop(sid, None)
        self._notes.pop(sid, None)
        self._needs_apply.discard(sid)
        row = self._rows.pop(sid, None)
        if row is not None:
            focus = QApplication.focusWidget()
            had_focus = focus is not None and (focus is row or row.isAncestorOf(focus))
            self.rows_box.removeWidget(row)
            row.hide()
            row.deleteLater()
            if had_focus:
                self.open_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self.playing.setVisible(bool(self._rows))

    def _note(self, sid: SessionId, text: str) -> None:
        self._notes[sid] = text
        snap = self._sessions.get(sid)
        row = self._rows.get(sid)
        if snap is not None and row is not None:
            row.update_snapshot(snap, text)

    # ------------------------------------------------------------------ events
    def _on_event(self, ev: Event) -> None:
        if isinstance(ev, SessionAdded | SessionChanged):
            self._upsert(ev.snapshot)
        elif isinstance(ev, SessionEnded):
            self._remove(ev.sid)
        elif isinstance(ev, HealthChanged):
            self._note(ev.sid, render(ev.reason))
        elif isinstance(ev, Notice):
            text = without_code(render(ev.message))
            if ev.sid is not None:
                self._note(ev.sid, text)
            else:
                self.set_status("info", text)
        elif isinstance(ev, JobChanged):
            self._upsert_job(ev.job)
        elif isinstance(ev, CapabilitiesChanged):
            self._apply_caps(ev.caps)
        elif isinstance(ev, ConfigChanged):
            load = self.config_load
            if not self._saving and (load is None or ev.revision != load.revision):
                if self._busy != "setup":
                    self.bridge.call(
                        lambda core: core.load_config(),
                        owner=self,
                        ok=self._on_outside_config,
                        err=self._quiet,
                    )
        elif isinstance(ev, EventsDropped) and self._ok(Feature.LIVE):
            self.bridge.call(
                lambda core: core.sessions(), owner=self, ok=self._resync, err=self._quiet
            )
        if self._details is not None and shiboken6.isValid(self._details):
            self._details.on_event(ev)

    def _resync(self, snaps: tuple[SessionSnapshot, ...]) -> None:
        live = {s.sid for s in snaps if not s.ended}
        for sid in list(self._rows):
            if sid not in live:
                self._remove(sid)
        self._seed(snaps)

    def _on_outside_config(self, load: ConfigLoad) -> None:
        self.config_load = load
        self._show_config_choices(load.config)

    def _on_fatal(self, err: ButterEyeError) -> None:
        self._busy = None
        for w in (self.drop, self.open_button, self.smooth_switch):
            w.setEnabled(False)
        self.target_combo.setEnabled(False)
        self.smooth_combo.setEnabled(False)
        self.show_problem(
            "blocking",
            self.tr("ButterEye couldn't start: {cause}").format(
                cause=without_code(first_line(render(err.cause)))
            ),
        )
        self.set_status("blocking", self.tr("Not running."))

    def _on_unresponsive(self, stuck: bool) -> None:
        if stuck:
            self.show_problem("degraded", self.tr("ButterEye's engine is not responding."))
        elif self.problem.isVisible() and self.problem_badge.kind() == "degraded":
            self.hide_problem()
            self._announce(self.tr("ButterEye's engine is responding again."), False)

    # ------------------------------------------------------------------ details
    def open_details(self, tab: str | None = None) -> None:
        from buttereye.gui.details_dialog import DetailsDialog

        dlg = self._details
        if dlg is None or not shiboken6.isValid(dlg):
            dlg = DetailsDialog(self.bridge, self.settings, facts=self.current_facts, parent=self)
            self._details = dlg
        if tab is not None:
            dlg.go(tab)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    @property
    def details(self) -> Any:
        return self._details

    # ------------------------------------------------------------------ closing
    def closeEvent(self, event: QCloseEvent) -> None:
        if self._may_close:
            self._save_size()
            event.accept()
            return
        if self._in_close_flow:
            event.ignore()
            return
        self._in_close_flow = True
        try:
            allowed = self._close_flow()
        finally:
            self._in_close_flow = False
        if allowed:
            self._may_close = True
            self._save_size()
            if self._details is not None and shiboken6.isValid(self._details):
                self._details.close()
            event.accept()
        else:
            event.ignore()

    def _close_flow(self) -> bool:
        if not self.bridge.is_ready:
            self.last_shutdown = self.bridge.shutdown(
                DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=self.shutdown_timeout_s
            )
            return True
        self.bridge.drain_now()
        needs = quit_needs(self._sessions.values(), self._jobs.values())
        live = tuple(s.sid for s in needs.live)

        def shutdown(policy: DetachPolicy, cancel_jobs: bool) -> ShutdownReport | None:
            return self.bridge.shutdown(
                policy, cancel_jobs=cancel_jobs, timeout_s=self.shutdown_timeout_s, live=live
            )

        ok, report = run_close_flow(self, needs, shutdown)
        if ok:
            self.last_shutdown = report
        return ok

    def _restore_size(self) -> None:
        screen = self.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry().size() if screen is not None else DEFAULT_SIZE
        fit = QSize(int(avail.width() * SCREEN_FRACTION), int(avail.height() * SCREEN_FRACTION))
        self.setMinimumSize(MINIMUM_SIZE.boundedTo(fit))
        size = self.settings.value("simple/size")
        if isinstance(size, QSize) and size.isValid():
            target = size.boundedTo(avail)
        else:
            target = DEFAULT_SIZE.boundedTo(fit)
        self.resize(target.expandedTo(self.minimumSize()))

    def _save_size(self) -> None:
        self.settings.setValue("simple/size", self.size())
        self.settings.sync()


def _float(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except TypeError, ValueError:
        return None
    return v if v > 0 else None


def _bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


__all__ = [
    "APP_ICON",
    "FIRST_BENCH",
    "RowView",
    "SIMPLE_ID",
    "SessionRow",
    "SimpleWindow",
    "find_simple",
    "gpu_smoothing_available",
    "is_simple",
    "row_view",
    "short_rate",
    "simple_config",
    "simple_profile",
    "simple_rules",
    "smoothness_key",
    "target_for",
    "target_key",
    "without_code",
]
