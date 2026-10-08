# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Profiles & Rules page (docs/design/GUI.md §4.4; SCOPE §4.9, §5.3, §5.6; F15).

The page edits a local copy of the loaded ``Config``. Every edit builds a new
frozen value with ``dataclasses.replace``; nothing reaches the core until Save
(Ctrl+S), which goes through the bridge with the revision the copy was loaded
at. ``validate_config`` and ``explain_rules`` are pure and run on the GUI thread
(validation debounced 150 ms after an edit).

States (§4.4):

- invalid TOML (``used_defaults``, BE-2001): banner with line/column, editors
  read-only, [Open folder], [Start fresh...] (Saving replaces the file; a .bak
  is kept);
- newer schema (``read_only``, BE-2002): banner, Save disabled;
- unknown keys: info "3 unknown settings will be kept.";
- Save conflict (BE-2003): [Reload theirs] / [Overwrite];
- page shown again with no local edits and a new revision: silent reload.
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Callable
from fractions import Fraction
from typing import TYPE_CHECKING, Literal, cast

from PySide6.QtCore import QCoreApplication, QModelIndex, Qt, QTimer, QUrl
from PySide6.QtGui import (
    QDesktopServices,
    QKeySequence,
    QShortcut,
    QShowEvent,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    BackendId,
    BackendStatus,
    ButterEye,
    ButterEyeError,
    CapabilitiesChanged,
    CommandHint,
    Config,
    ConfigChanged,
    ConfigConflict,
    ConfigIssue,
    ConfigLoad,
    DoctorReport,
    ErrorCode,
    Event,
    EventsDropped,
    Feature,
    HdrClass,
    ModelEntry,
    ModelKind,
    NotAvailable,
    Paths,
    Profile,
    Rule,
    RuleTrace,
    SessionAdded,
    SessionChanged,
    SessionEnded,
    SourceFacts,
    Target,
    TargetKind,
    builtin_profiles,
    command_hint,
    explain_rules,
    render,
    validate_config,
)
from buttereye.gui.a11y import announce, heading, labelled, message_box, plain
from buttereye.gui.dialogs.rule_edit import (
    RuleEditDialog,
    describe_match,
    hdr_label,
    profile_label,
)
from buttereye.gui.pages.base import Page
from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.fraction_edit import FractionEdit, format_rate
from buttereye.gui.widgets.state_panel import StatePanel
from buttereye.gui.widgets.status_badge import StatusBadge

if TYPE_CHECKING:
    from buttereye.gui.context import GuiContext

VALIDATE_DEBOUNCE_MS = 150
DEFAULT_BUFFERED_FRAMES = 4  # §4.4 table: every backend
DEFAULT_SC_THRESHOLD = 0.12
MAX_FRAMES = 64

_PROFILE_FIELD = re.compile(r"^profiles\[(\d+)\]\.(\w+)")
_RULE_FIELD = re.compile(r"^rules\[(\d+)\]")
_SLUG = re.compile(r"[^A-Za-z0-9_-]+")

#: Profile issue keys -> the form row that shows them.
_FIELD_ROW = {"id": "name"}


def _t(text: str) -> str:
    return QCoreApplication.translate("ProfilesPage", text)


def engine_label(backend: BackendId | str) -> str:
    """Display name of a profile engine ("auto" or a ``BackendId``)."""
    if backend == "auto":
        return _t("Automatic")
    names = {
        BackendId.RIFE_NCNN: _t("RIFE (Vulkan)"),
        BackendId.RIFE_TRT: _t("RIFE · TensorRT (experimental)"),
        BackendId.MVTOOLS: _t("MVTools (CPU)"),
    }
    try:
        return names[BackendId(backend)]
    except ValueError:
        return str(backend)


def concurrent_default(backend: BackendId | str) -> int | None:
    """§4.4 default ``concurrent-frames`` per backend; ``None`` = depends on the engine."""
    if backend == BackendId.RIFE_NCNN:
        return 8  # mpv concurrent-frames; plugin gpu_thread stays 4 (§4.4, M0(f))
    if backend == BackendId.RIFE_TRT:
        return 2  # num_streams
    if backend == BackendId.MVTOOLS:
        return min(os.cpu_count() or 1, 8)
    return None


def _unique_id(base: str, taken: set[str]) -> str:
    slug = _SLUG.sub("-", base.strip().lower()).strip("-_")[:56] or "profile"
    if not slug[0].isalnum():
        slug = "p" + slug
    candidate, n = slug, 2
    while candidate in taken:
        candidate = f"{slug}-{n}"
        n += 1
    return candidate


class ProfilesPage(Page):
    """Profiles tab, Rules tab and the rule tester (§4.4)."""

    page_id = "profiles"
    features = (Feature.CONFIG,)

    def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None:
        super().__init__(ctx, parent)
        self.setObjectName("profilesPage")
        self.setWindowTitle(self.tr("Profiles & Rules"))
        self._load: ConfigLoad | None = None
        self._base: Config | None = None  # as loaded / last saved
        self._cfg: Config | None = None  # local edits
        self._revision = ""
        self._fresh = False  # "Start fresh" chosen over an invalid file
        self._saving = False
        self._loaded = False
        self._was_dirty = False
        self._populating = False
        self._issues: tuple[ConfigIssue, ...] = ()
        self._models: tuple[ModelEntry, ...] | None = None  # None = list unavailable
        self._backends: dict[BackendId, BackendStatus] = {}
        self._paths: Paths | None = None
        self._external: str | None = None  # on-disk revision changed while editing
        self._save_error: ButterEyeError | None = None
        self._current = -1  # selected profile index

        self._validate_timer = QTimer(self)
        self._validate_timer.setSingleShot(True)
        self._validate_timer.setInterval(VALIDATE_DEBOUNCE_MS)
        self._validate_timer.timeout.connect(self.validate_now)

        self.panel = StatePanel(self)
        outer = QVBoxLayout(self)
        outer.addWidget(self.panel)
        content = self.panel.content
        lay = QVBoxLayout(content)

        # header
        head = QHBoxLayout()
        self.heading = heading(QLabel(self.tr("Profiles & Rules"), content))
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        head.addWidget(self.heading)
        self.unsaved_label = QLabel(self.tr("Unsaved changes"), content)
        self.unsaved_label.setObjectName("unsaved")
        self.unsaved_label.setTextFormat(Qt.TextFormat.PlainText)
        self.unsaved_label.hide()
        head.addWidget(self.unsaved_label)
        head.addStretch(1)
        self.save_button = self._button(self.tr("&Save"), "save", self.save, content)
        self.save_button.setAccessibleDescription(self.tr("Ctrl+S"))
        self.revert_button = self._button(self.tr("Re&vert"), "revert", self.revert, content)
        head.addWidget(self.save_button)
        head.addWidget(self.revert_button)
        lay.addLayout(head)
        self.save_shortcut = QShortcut(QKeySequence("Ctrl+S"), self)
        self.save_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.save_shortcut.activated.connect(self.save)

        self._banner_lay = QVBoxLayout()
        lay.addLayout(self._banner_lay)
        self.banners: dict[str, Banner] = {}
        self._general_lay = QVBoxLayout()
        lay.addLayout(self._general_lay)
        self.general_issues: list[StatusBadge] = []

        self.tabs = QTabWidget(content)
        self.tabs.setObjectName("tabs")
        self.tabs.setAccessibleName(self.tr("Profiles and rules"))
        self.profiles_tab = QWidget()
        self.rules_tab = QWidget()
        self.tabs.addTab(self.profiles_tab, self.tr("Pr&ofiles"))
        self.tabs.addTab(self.rules_tab, self.tr("R&ules"))
        lay.addWidget(self.tabs, 1)
        self._build_profiles_tab()
        self._build_rules_tab()

        self.hint = CliHint(command_hint("profiles.list"), content)
        lay.addWidget(self.hint)
        self._update_state()

    # ================================================================== building
    def _button(
        self, text: str, name: str, slot: Callable[[], None], parent: QWidget
    ) -> QPushButton:
        b = QPushButton(text, parent)
        b.setObjectName(name)
        b.setAccessibleName(plain(text))
        b.setAutoDefault(False)
        b.clicked.connect(lambda _c=False: slot())
        return b

    def _shortcut(self, keys: str, widget: QWidget, slot: Callable[[], None]) -> QShortcut:
        sc = QShortcut(QKeySequence(keys), widget)
        sc.setContext(Qt.ShortcutContext.WidgetShortcut)
        sc.activated.connect(slot)
        return sc

    def _issue_badge(self, parent: QWidget) -> StatusBadge:
        b = StatusBadge("blocking", "", parent)
        b.hide()
        return b

    def _build_profiles_tab(self) -> None:
        tab = self.profiles_tab
        row = QHBoxLayout(tab)

        left = QVBoxLayout()
        self.profile_list = QListWidget(tab)
        self.profile_list.setObjectName("profileList")
        lab, _ = labelled(self.tr("Profiles"), self.profile_list)
        lab.setParent(tab)
        self.profile_list.setAccessibleDescription(
            self.tr("Ins: new profile. F2: rename. Del: delete.")
        )
        self.profile_list.currentRowChanged.connect(self._on_profile_selected)
        left.addWidget(lab)
        left.addWidget(self.profile_list)
        # The list holds a handful of profiles: keep it narrow so the editor gets the
        # width (the Model combo shows the whole id at 800 px), scaled by the font.
        em = self.profile_list.fontMetrics().horizontalAdvance("M")
        self.profile_list.setMaximumWidth(max(240, em * 18))
        buttons = QGridLayout()
        self.new_button = self._button(self.tr("Ne&w"), "new", self.new_profile, tab)
        self.duplicate_button = self._button(
            self.tr("Duplicate"), "duplicate", self.duplicate_profile, tab
        )
        self.rename_button = self._button(self.tr("Rena&me"), "rename", self.rename_profile, tab)
        self.delete_button = self._button(self.tr("&Delete"), "delete", self.delete_profile, tab)
        self.reset_button = self._button(
            self.tr("&Reset to default"), "reset", self.reset_profile, tab
        )
        for i, b in enumerate(
            (
                self.new_button,
                self.duplicate_button,
                self.rename_button,
                self.delete_button,
                self.reset_button,
            )
        ):
            buttons.addWidget(b, i // 2, i % 2)
        left.addLayout(buttons)
        self.delete_reason = QLabel(tab)
        self.delete_reason.setObjectName("deleteReason")
        self.delete_reason.setWordWrap(True)
        self.delete_reason.setTextFormat(Qt.TextFormat.PlainText)
        self.delete_reason.hide()
        left.addWidget(self.delete_reason)
        left.addStretch(1)
        self._shortcut("Ins", self.profile_list, self.new_profile)
        self._shortcut("F2", self.profile_list, self.rename_profile)
        self._shortcut("Del", self.profile_list, self.delete_profile)
        row.addLayout(left, 0)

        right = QVBoxLayout()
        self.builtin_note = QLabel(
            self.tr("Built-in profiles can't be changed. Duplicate one to make your own."), tab
        )
        self.builtin_note.setObjectName("builtinNote")
        self.builtin_note.setWordWrap(True)
        self.builtin_note.setTextFormat(Qt.TextFormat.PlainText)
        right.addWidget(self.builtin_note)
        self.form_box = QWidget(tab)
        self.form = QFormLayout(self.form_box)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        right.addWidget(self.form_box)
        right.addStretch(1)
        row.addLayout(right, 1)

        self.issue_badges: dict[str, StatusBadge] = {}
        self.field_widgets: dict[str, QWidget] = {}
        fb = self.form_box

        # Name
        self.name_edit = QLineEdit(fb)
        self.name_edit.setObjectName("field.name")
        self._row(self.tr("&Name"), self.name_edit, "name")
        self.name_edit.textEdited.connect(lambda _t: self._on_form_edited())

        # Engine
        self.engine_combo = QComboBox(fb)
        self.engine_combo.setObjectName("field.backend")
        self._row(self.tr("&Engine"), self.engine_combo, "backend")
        self.engine_note = self._note(fb)
        self.form.addRow(self.engine_note)
        self.engine_combo.activated.connect(lambda _i: self._on_engine_changed())

        # Model
        self.model_combo = QComboBox(fb)
        self.model_combo.setObjectName("field.model")
        self.model_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.model_combo.currentTextChanged.connect(self.model_combo.setToolTip)
        self._row(self.tr("Mode&l"), self.model_combo, "model")
        self.model_note = self._note(fb)
        self.form.addRow(self.model_note)
        self.model_combo.activated.connect(lambda _i: self._on_form_edited())
        self.model_combo.editTextChanged.connect(self._on_model_text)

        # Scale
        self.scale_spin = QDoubleSpinBox(fb)
        self.scale_spin.setObjectName("field.scale")
        self.scale_spin.setRange(0.0, 4.0)
        self.scale_spin.setSingleStep(0.25)
        self.scale_spin.setDecimals(2)
        self.scale_spin.setSpecialValueText(self.tr("Automatic"))
        self._row(self.tr("Sc&ale"), self.scale_spin, "scale")
        self.scale_note = self._note(fb)
        self.form.addRow(self.scale_note)
        self.scale_spin.valueChanged.connect(lambda _v: self._on_form_edited())

        # Target
        self.target_box = QWidget(fb)
        tl = QVBoxLayout(self.target_box)
        tl.setContentsMargins(0, 0, 0, 0)
        self.target_display = QRadioButton(self.tr("Match m&y display"), self.target_box)
        self.target_display.setToolTip(
            self.tr("The lowest rate your display shows evenly that doubles the video's.")
        )
        self.target_display_max = QRadioButton(
            self.tr("Maximi&ze smoothness (highest display rate)"), self.target_box
        )
        self.target_display_max.setToolTip(
            self.tr("Smoothest motion; the GPU works up to twice as hard on fast displays.")
        )
        self.target_2x = QRadioButton(self.tr("Double (&2×)"), self.target_box)
        self.target_fixed = QRadioButton(self.tr("F&ixed rate"), self.target_box)
        self.target_group = QButtonGroup(self)
        radios = (self.target_display, self.target_display_max, self.target_2x, self.target_fixed)
        names = ("target.display", "target.display_max", "target.2x", "target.fixed")
        for i, r in enumerate(radios):
            r.setObjectName(names[i])
            r.setAccessibleName(plain(r.text()))
            self.target_group.addButton(r, i)
            tl.addWidget(r)
        self.target_fps = FractionEdit(None, self.target_box)
        self.target_fps.setObjectName("field.target_fps")
        fps_label, _ = labelled(self.tr("Fixed rate (fps)"), self.target_fps)
        fps_label.setParent(self.target_box)
        self.target_fps_label = fps_label
        fr = QHBoxLayout()
        fr.addWidget(fps_label)
        fr.addWidget(self.target_fps, 1)
        tl.addLayout(fr)
        self.target_box.setFocusProxy(self.target_display)
        self._row(self.tr("Target"), self.target_box, "target", focus=self.target_display)
        self.target_group.idClicked.connect(lambda _i: self._on_target_kind())
        self.target_fps.valueChanged.connect(lambda _v: self._on_form_edited())

        # Scene-cut sensitivity: slider + spin, both expose the value
        self.sc_box = QWidget(fb)
        sl = QHBoxLayout(self.sc_box)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sc_slider = QSlider(Qt.Orientation.Horizontal, self.sc_box)
        self.sc_slider.setObjectName("field.sc_slider")
        self.sc_slider.setRange(0, 100)
        self.sc_slider.setPageStep(5)
        self.sc_spin = QDoubleSpinBox(self.sc_box)
        self.sc_spin.setObjectName("field.sc_threshold")
        self.sc_spin.setRange(0.0, 1.0)
        self.sc_spin.setSingleStep(0.01)
        self.sc_spin.setDecimals(2)
        sl.addWidget(self.sc_slider, 1)
        sl.addWidget(self.sc_spin)
        self.sc_box.setFocusProxy(self.sc_slider)  # first in the Tab chain
        self._row(
            self.tr("S&cene-cut sensitivity"), self.sc_box, "sc_threshold", focus=self.sc_spin
        )
        self.sc_slider.setAccessibleName(self.tr("Scene-cut sensitivity, slider"))
        self.sc_spin.setAccessibleName(self.tr("Scene-cut sensitivity"))
        self.sc_slider.setAccessibleDescription(
            self.tr("Lower values detect more scene cuts. 0 to 100 hundredths.")
        )
        self.sc_slider.valueChanged.connect(self._on_sc_slider)
        self.sc_spin.valueChanged.connect(self._on_sc_spin)

        # Expert disclosure
        self.expert_button = QPushButton(self.tr("Show e&xpert settings"), fb)
        self.expert_button.setObjectName("expert")
        self.expert_button.setCheckable(True)
        self.expert_button.setAutoDefault(False)
        self.expert_button.setAccessibleName(self.tr("Show expert settings"))
        self.expert_button.toggled.connect(self._on_expert)
        self.form.addRow(self.expert_button)
        self.expert_box = QWidget(fb)
        ef = QFormLayout(self.expert_box)
        ef.setContentsMargins(0, 0, 0, 0)
        self.buffered_spin, self.buffered_auto = self._frames_row(
            ef, self.tr("&Buffered frames"), "buffered_frames"
        )
        self.concurrent_spin, self.concurrent_auto = self._frames_row(
            ef, self.tr("Concu&rrent frames"), "concurrent_frames"
        )
        self.form.addRow(self.expert_box)
        self.expert_box.hide()

        # HDR
        self.hdr_box = QWidget(fb)
        hl = QVBoxLayout(self.hdr_box)
        hl.setContentsMargins(0, 0, 0, 0)
        self.hdr_skip = QRadioButton(self.tr("S&kip (recommended)"), self.hdr_box)
        self.hdr_pass = QRadioButton(self.tr("Pass through (experimental)"), self.hdr_box)
        self.hdr_group = QButtonGroup(self)
        for i, r in enumerate((self.hdr_skip, self.hdr_pass)):
            r.setObjectName(("hdr.skip", "hdr.passthrough")[i])
            r.setAccessibleName(plain(r.text()))
            self.hdr_group.addButton(r, i)
            hl.addWidget(r)
        self.hdr_box.setFocusProxy(self.hdr_skip)
        self._row(self.tr("HDR video"), self.hdr_box, "hdr", focus=self.hdr_skip)
        self.hdr_note = self._note(fb)
        self.form.addRow(self.hdr_note)
        self.hdr_group.idClicked.connect(lambda _i: self._on_form_edited())

    def _note(self, parent: QWidget) -> QLabel:
        n = QLabel(parent)
        n.setWordWrap(True)
        n.setTextFormat(Qt.TextFormat.PlainText)
        n.hide()
        return n

    def _row(self, text: str, w: QWidget, key: str, *, focus: QWidget | None = None) -> None:
        label, _ = labelled(text, w)
        label.setParent(self.form_box)
        self.form.addRow(label, w)
        badge = self._issue_badge(self.form_box)
        badge.setObjectName(f"issue.{key}")
        self.form.addRow(badge)
        self.issue_badges[key] = badge
        self.field_widgets[key] = focus if focus is not None else w

    def _frames_row(self, form: QFormLayout, text: str, key: str) -> tuple[QSpinBox, QCheckBox]:
        box = QWidget(self.expert_box)
        bl = QHBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        spin = QSpinBox(box)
        spin.setObjectName(f"field.{key}")
        spin.setRange(1, MAX_FRAMES)
        auto = QCheckBox(self.tr("Automatic"), box)
        auto.setObjectName(f"auto.{key}")
        bl.addWidget(spin)
        bl.addWidget(auto, 1)
        label, _ = labelled(text, spin)
        label.setParent(self.expert_box)
        auto.setAccessibleName(self.tr("Automatic: {name}").format(name=plain(text)))
        form.addRow(label, box)
        badge = self._issue_badge(self.expert_box)
        badge.setObjectName(f"issue.{key}")
        form.addRow(badge)
        self.issue_badges[key] = badge
        self.field_widgets[key] = spin
        spin.valueChanged.connect(lambda _v: self._on_form_edited())
        auto.toggled.connect(lambda on, s=spin: self._on_auto(s, on))
        return spin, auto

    def _build_rules_tab(self) -> None:
        tab = self.rules_tab
        lay = QVBoxLayout(tab)
        note = QLabel(self.tr("First matching rule wins."), tab)
        note.setObjectName("rulesNote")
        note.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(note)

        self.rules_model = QStandardItemModel(0, 3, self)
        self.rules_model.setHorizontalHeaderLabels(
            [self.tr("#"), self.tr("When"), self.tr("Use profile")]
        )
        self.rules_view = QTableView(tab)
        self.rules_view.setObjectName("rulesView")
        self.rules_view.setModel(self.rules_model)
        self.rules_view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rules_view.setTabKeyNavigation(False)
        self.rules_view.setDragEnabled(False)
        self.rules_view.verticalHeader().hide()
        hh = self.rules_view.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        lab, _ = labelled(self.tr("&Rules"), self.rules_view)
        lab.setParent(tab)
        self.rules_view.setAccessibleDescription(
            self.tr("Ins: add. Enter: edit. Del: delete. Alt+Up and Alt+Down: move.")
        )
        self.rules_view.doubleClicked.connect(lambda _i: self.edit_rule())
        sel = self.rules_view.selectionModel()
        sel.currentRowChanged.connect(lambda _a, _b: self._update_rule_buttons())
        lay.addWidget(lab)
        lay.addWidget(self.rules_view, 1)

        buttons = QHBoxLayout()
        self.add_rule_button = self._button(self.tr("&Add"), "ruleAdd", self.add_rule, tab)
        self.edit_rule_button = self._button(self.tr("&Edit…"), "ruleEdit", self.edit_rule, tab)
        self.delete_rule_button = self._button(
            self.tr("&Delete"), "ruleDelete", self.delete_rule, tab
        )
        self.up_button = self._button(
            self.tr("&Move up"), "ruleUp", lambda: self.move_rule(-1), tab
        )
        self.down_button = self._button(
            self.tr("Move dow&n"), "ruleDown", lambda: self.move_rule(1), tab
        )
        self.up_button.setAccessibleDescription(self.tr("Alt+Up"))
        self.down_button.setAccessibleDescription(self.tr("Alt+Down"))
        for b in (
            self.add_rule_button,
            self.edit_rule_button,
            self.delete_rule_button,
            self.up_button,
            self.down_button,
        ):
            buttons.addWidget(b)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self._shortcut("Ins", self.rules_view, self.add_rule)
        self._shortcut("Return", self.rules_view, self.edit_rule)
        self._shortcut("Enter", self.rules_view, self.edit_rule)
        self._shortcut("Del", self.rules_view, self.delete_rule)
        self._shortcut("Alt+Up", self.rules_view, lambda: self.move_rule(-1))
        self._shortcut("Alt+Down", self.rules_view, lambda: self.move_rule(1))

        self._rule_issue_lay = QVBoxLayout()
        lay.addLayout(self._rule_issue_lay)
        self.rule_issues: list[StatusBadge] = []

        self._build_tester(lay)

    def _build_tester(self, lay: QVBoxLayout) -> None:
        box = QGroupBox(self.tr("Test the rules"), self.rules_tab)
        box.setObjectName("tester")
        lay.addWidget(box)
        form = QFormLayout(box)
        self.t_fps = FractionEdit(Fraction(24000, 1001), box)
        self.t_fps.setObjectName("test.fps")
        self.t_width = QSpinBox(box)
        self.t_width.setRange(1, 16384)
        self.t_width.setValue(1920)
        self.t_width.setSuffix(self.tr(" px"))
        self.t_height = QSpinBox(box)
        self.t_height.setRange(1, 8640)
        self.t_height.setValue(1080)
        self.t_height.setSuffix(self.tr(" px"))
        self.t_hdr = QComboBox(box)
        for h in HdrClass:
            self.t_hdr.addItem(hdr_label(h), h.value)
        self.t_hz = QDoubleSpinBox(box)
        self.t_hz.setRange(1.0, 1000.0)
        self.t_hz.setDecimals(3)
        self.t_hz.setValue(60.0)
        self.t_hz.setSuffix(self.tr(" Hz"))
        self.t_interlaced = QCheckBox(self.tr("Inter&laced"), box)
        self.t_interlaced.setAccessibleName(self.tr("Interlaced"))
        self.t_path = QLineEdit(box)
        self.t_path.setPlaceholderText(self.tr("/home/me/Videos/film.mkv"))
        for text, w, name in (
            (self.tr("Sour&ce fps"), self.t_fps, "test.fps"),
            (self.tr("&Width"), self.t_width, "test.width"),
            (self.tr("He&ight"), self.t_height, "test.height"),
            (self.tr("HDR class"), self.t_hdr, "test.hdr"),
            (self.tr("Displa&y refresh"), self.t_hz, "test.hz"),
            (self.tr("File path"), self.t_path, "test.path"),
        ):
            w.setObjectName(name)
            label, _ = labelled(text, w)
            label.setParent(box)
            form.addRow(label, w)
        form.addRow(self.t_interlaced)
        self.use_session_button = self._button(
            self.tr("Use current session"), "useSession", self.use_current_session, box
        )
        form.addRow(self.use_session_button)
        self.trace_box = QWidget(box)
        self.trace_box.setObjectName("trace")
        self._trace_lay = QVBoxLayout(self.trace_box)
        self._trace_lay.setContentsMargins(0, 0, 0, 0)
        self.trace_lines: list[QLabel] = []
        self._trace_count = 0
        form.addRow(self.trace_box)
        self.tester_hint = CliHint(None, box)
        form.addRow(self.tester_hint)
        self.t_fps.valueChanged.connect(lambda _v: self.run_tester())
        self.t_width.valueChanged.connect(lambda _v: self.run_tester())
        self.t_height.valueChanged.connect(lambda _v: self.run_tester())
        self.t_hdr.currentIndexChanged.connect(lambda _i: self.run_tester())
        self.t_hz.valueChanged.connect(lambda _v: self.run_tester())
        self.t_interlaced.toggled.connect(lambda _b: self.run_tester())
        self.t_path.textChanged.connect(lambda _s: self.run_tester())

    # ================================================================== Page API
    def title(self) -> str:
        base = self.tr("Profiles & Rules")
        if self.has_unsaved_changes():
            return self.tr("{title} — unsaved changes *").format(title=base)
        return base

    def has_unsaved_changes(self) -> bool:
        if self._cfg is None or self._base is None:
            return False
        return self._fresh or self._cfg != self._base

    def cli_hint(self) -> CommandHint | None:
        return command_hint("profiles.list")

    def state_panel(self) -> StatePanel | None:
        return self.panel

    def refresh(self) -> None:
        un = self.unavailable_feature()
        if un is not None:
            self.panel.show_unavailable(un[0], un[1], command_hint("profiles.list"))
            return
        if self._cfg is None:
            self.panel.show_loading(self.tr("Loading profiles…"))
        self._request_load()
        self._request_side_data()

    def on_event(self, ev: Event) -> None:
        if isinstance(ev, ConfigChanged):
            if self._loaded and not self._saving and ev.revision != self._revision:
                if self.has_unsaved_changes():
                    self._show_external(ev.revision)
                else:
                    self._request_load()
        elif isinstance(ev, CapabilitiesChanged):
            if self._cfg is not None:
                self._fill_engine(self._profile())
                self._update_hdr_note()
        elif isinstance(ev, SessionAdded | SessionChanged | SessionEnded | EventsDropped):
            self._update_session_button()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._update_session_button()
        # §4.4: shown again with no local edits and a new revision -> reload silently
        if self._loaded and not self._saving and not self.has_unsaved_changes():
            self._request_load()

    # ================================================================== loading
    def _request_load(self, *, force: bool = False) -> None:
        self.ctx.bridge.call(
            lambda core: core.load_config(),
            owner=self,
            ok=lambda load: self._on_loaded(load, force=force),
            err=self._on_load_error,
        )

    def _request_side_data(self) -> None:
        bridge = self.ctx.bridge
        caps = bridge.capabilities
        if caps is not None and caps.states.get(Feature.MODELS, None) is not None:
            if caps.ok(Feature.MODELS):
                bridge.call(
                    lambda core: core.models(),
                    owner=self,
                    ok=self._on_models,
                    err=lambda _e: self._on_models(None),
                )
            else:
                self._on_models(None)
        bridge.call(
            lambda core: core.last_report(),
            owner=self,
            ok=self._on_report,
            err=lambda _e: None,
        )
        if self._paths is None:

            async def get_paths(core: ButterEye) -> Paths:
                return core.paths()

            bridge.call(get_paths, owner=self, ok=self._on_paths, err=lambda _e: None)

    def _on_paths(self, paths: Paths) -> None:
        self._paths = paths

    def _on_report(self, report: DoctorReport | None) -> None:
        if report is None:
            return
        self.ctx.bridge.call(
            lambda core: core.backends(report),
            owner=self,
            ok=self._on_backends,
            err=lambda _e: None,
        )

    def _on_backends(self, statuses: tuple[BackendStatus, ...]) -> None:
        self._backends = {s.id: s for s in statuses}
        if self._cfg is not None:
            self._fill_engine(self._profile())

    def _on_models(self, models: tuple[ModelEntry, ...] | None) -> None:
        self._models = models
        if self._cfg is not None:
            self._fill_model(self._profile())

    def _on_load_error(self, err: ButterEyeError) -> None:
        if isinstance(err, NotAvailable):
            self.panel.show_unavailable(err.feature, err.state, command_hint("profiles.list"))
            return
        if self._cfg is not None:
            self._set_banner("error", Banner.from_error(err, ()))
            return
        self.show_error(err)

    def _on_loaded(self, load: ConfigLoad, *, force: bool = False) -> None:
        if not force and self._loaded and self.has_unsaved_changes():
            if load.revision != self._revision:
                self._show_external(load.revision)
            return
        if not force and self._loaded and load.revision == self._revision:
            return  # nothing new on disk, nothing edited
        self._apply_load(load)

    def _apply_load(self, load: ConfigLoad) -> None:
        self._load = load
        self._base = load.config
        self._cfg = load.config
        self._revision = load.revision
        self._fresh = False
        self._external = None
        self._save_error = None
        self._loaded = True
        self._drop_banner("external")
        self._drop_banner("error")
        self._drop_banner("save")
        keep = self._current
        self._rebuild_profile_list(select=keep if keep >= 0 else 0)
        self._rebuild_rules()
        self.panel.show_content()
        self.validate_now()
        self._update_state()
        self.run_tester()

    def _show_external(self, revision: str) -> None:
        self._external = revision
        self._set_banner(
            "external",
            Banner(
                "info",
                self.tr("config.toml changed outside ButterEye."),
                self.tr("Your unsaved changes are still here."),
                actions=(
                    (self.tr("Reload &theirs"), self.reload_theirs),
                    (self.tr("&Keep my changes"), lambda: self._drop_banner("external")),
                ),
            ),
        )

    def reload_theirs(self) -> None:
        self._drop_banner("external")
        self._request_load(force=True)

    # ================================================================== state
    def _editable(self) -> bool:
        load = self._load
        if load is None or self._cfg is None or self._saving:
            return False
        if load.read_only:
            return False
        return not load.used_defaults or self._fresh

    def _update_state(self) -> None:
        load = self._load
        editable = self._editable()
        dirty = self.has_unsaved_changes()
        self.save_button.setEnabled(editable and dirty)
        self.save_shortcut.setEnabled(editable and dirty)
        self.revert_button.setEnabled(dirty and not self._saving)
        self.unsaved_label.setVisible(dirty)
        if load is not None:
            self._state_banners(load)
        self._update_profile_controls()
        self._update_rule_buttons()
        if dirty != self._was_dirty:
            self._was_dirty = dirty
            self.title_changed.emit()

    def _state_banners(self, load: ConfigLoad) -> None:
        if load.used_defaults and not self._fresh:
            issue = next(
                (i for i in load.issues if i.code is ErrorCode.CONFIG_INVALID),
                load.issues[0] if load.issues else None,
            )
            cause = render(issue.message).rstrip(". ") if issue is not None else ""
            if issue is not None and issue.line is not None:
                title = self.tr(
                    "config.toml line {line}, column {column}: {cause}. Defaults are in use."
                ).format(line=issue.line, column=issue.column or 1, cause=cause)
            else:
                title = self.tr("config.toml can't be used: {cause}. Defaults are in use.").format(
                    cause=cause
                )
            actions: tuple[Action, ...] = (
                (self.tr("&Open folder"), self.open_folder),
                (self.tr("Start &fresh…"), self.start_fresh),
            )
            if "invalid" not in self.banners or self.banners["invalid"].title() != title:
                self._set_banner(
                    "invalid",
                    Banner(
                        "degraded",
                        title,
                        self.tr("The editors are read-only until you start fresh."),
                        ErrorCode.CONFIG_INVALID.code,
                        actions=actions,
                    ),
                )
        else:
            self._drop_banner("invalid")
        if load.read_only:
            if "read_only" not in self.banners:
                self._set_banner(
                    "read_only",
                    Banner(
                        "degraded",
                        self.tr("Created by a newer ButterEye — read-only."),
                        self.tr("Update ButterEye to change these settings here."),
                        ErrorCode.CONFIG_NEWER_SCHEMA.code,
                    ),
                )
        else:
            self._drop_banner("read_only")
        cfg = self._cfg if self._cfg is not None else load.config
        n = len(cfg.unknown) or sum(
            1 for i in load.issues if i.code is ErrorCode.CONFIG_UNKNOWN_KEY
        )
        if n and not (load.used_defaults and not self._fresh):
            text = self.tr("%n unknown setting(s) will be kept.", None, n)
            if "unknown" not in self.banners or self.banners["unknown"].title() != text:
                self._set_banner("unknown", Banner("info", text))
        else:
            self._drop_banner("unknown")

    def _set_banner(self, key: str, banner: Banner) -> None:
        self._drop_banner(key)
        banner.setObjectName(f"banner.{key}")
        banner.setParent(self.panel.content)
        self.banners[key] = banner
        self._banner_lay.addWidget(banner)
        banner.show()

    def _drop_banner(self, key: str) -> None:
        b = self.banners.pop(key, None)
        if b is not None:
            self._banner_lay.removeWidget(b)
            b.hide()
            b.deleteLater()

    def open_folder(self) -> None:
        if self._paths is None:
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._paths.config_file.parent)))

    def start_fresh(self) -> None:
        box = message_box(self)
        box.setObjectName("startFresh")
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle(self.tr("Start fresh"))
        box.setText(self.tr("Start fresh from the default profiles and rules?"))
        box.setInformativeText(self.tr("Saving replaces the file; a .bak is kept."))
        yes = box.addButton(self.tr("&Start fresh"), QMessageBox.ButtonRole.AcceptRole)
        yes.setObjectName("startFreshYes")
        no = box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        no.setObjectName("startFreshNo")
        box.setDefaultButton(no)
        box.setEscapeButton(no)
        box.exec()
        if box.clickedButton() is not yes:
            return
        self._fresh = True
        self._update_state()
        self._show_profile()
        announce(self, self.tr("Editing the defaults. Save to replace config.toml."))

    # ================================================================== save
    def save(self) -> None:
        if self._cfg is None or not self._editable() or not self.has_unsaved_changes():
            return
        self.validate_now()
        if self._issues:
            self.focus_first_issue()
            n = len(self._issues)
            msg = (
                self.tr("Can't save: 1 setting needs fixing.")
                if n == 1
                else self.tr("Can't save: {n} settings need fixing.").format(n=n)
            )
            self.ctx.announce(msg, assertive=True)
            return
        self._save(self._cfg, self._revision)

    def _save(self, cfg: Config, expected: str) -> None:
        self._saving = True
        self._drop_banner("save")
        self._update_state()
        self.ctx.status(self.tr("Saving…"))
        self.ctx.bridge.call(
            lambda core: core.save_config(cfg, expected_revision=expected),
            owner=self,
            ok=lambda rev: self._on_saved(rev, cfg),
            err=lambda e: self._on_save_error(e, cfg),
        )

    def _on_saved(self, revision: str, cfg: Config) -> None:
        self._saving = False
        self._revision = revision
        self._base = cfg
        self._fresh = False
        self._external = None
        self._drop_banner("external")
        if self._load is not None:
            self._load = dataclasses.replace(
                self._load,
                config=cfg,
                exists=True,
                revision=revision,
                used_defaults=False,
                read_only=False,
                issues=tuple(
                    i for i in self._load.issues if i.code is ErrorCode.CONFIG_UNKNOWN_KEY
                ),
            )
        self._update_state()
        self._show_profile()
        self.ctx.announce(self.tr("Profiles and rules saved."))

    def _on_save_error(self, err: ButterEyeError, cfg: Config) -> None:
        self._saving = False
        self._update_state()
        if isinstance(err, ConfigConflict):
            self._conflict(err, cfg)
            return
        self._save_error = err
        self._set_banner("save", Banner.from_error(err, ()))
        self.ctx.announce(
            self.tr("Not saved: {cause}").format(cause=render(err.cause)), assertive=True
        )

    def _conflict(self, err: ConfigConflict, cfg: Config) -> None:
        box = message_box(self)
        box.setObjectName("configConflict")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(self.tr("config.toml changed"))
        box.setText(self.tr("config.toml changed outside ButterEye."))
        box.setInformativeText(
            self.tr("Reload it and lose your changes, or overwrite it with your changes.")
        )
        reload_b = box.addButton(self.tr("&Reload theirs"), QMessageBox.ButtonRole.DestructiveRole)
        reload_b.setObjectName("reloadTheirs")
        over = box.addButton(self.tr("&Overwrite"), QMessageBox.ButtonRole.AcceptRole)
        over.setObjectName("overwrite")
        cancel = box.addButton(self.tr("Cancel"), QMessageBox.ButtonRole.RejectRole)
        cancel.setObjectName("cancelConflict")
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is reload_b:
            self._request_load(force=True)
        elif clicked is over:
            self._save(cfg, err.on_disk_revision)
        else:
            self._show_external(err.on_disk_revision)

    def revert(self) -> None:
        if self._load is None or self._base is None:
            return
        self._cfg = self._base
        self._fresh = False
        self._rebuild_profile_list(select=min(max(self._current, 0), len(self._base.profiles) - 1))
        self._rebuild_rules()
        self.validate_now()
        self._update_state()
        self.run_tester()
        self.ctx.announce(self.tr("Changes reverted."))

    # ================================================================== config edits
    def config(self) -> Config | None:
        """The local (edited) config."""
        return self._cfg

    def _set_cfg(self, cfg: Config, *, rules_changed: bool = False) -> None:
        self._cfg = cfg
        if rules_changed:
            self._rebuild_rules()
        self._refresh_profile_texts()
        self._validate_timer.start()
        self._update_state()
        self.run_tester()

    def _profile(self) -> Profile | None:
        cfg = self._cfg
        if cfg is None or not 0 <= self._current < len(cfg.profiles):
            return None
        return cfg.profiles[self._current]

    def _replace_profile(self, index: int, p: Profile) -> None:
        assert self._cfg is not None
        profiles = list(self._cfg.profiles)
        profiles[index] = p
        self._set_cfg(dataclasses.replace(self._cfg, profiles=tuple(profiles)))

    def _profile_ids(self) -> set[str]:
        return {p.id for p in self._cfg.profiles} if self._cfg is not None else set()

    def new_profile(self) -> None:
        if self._cfg is None or not self._editable():
            return
        name = self.tr("New profile")
        p = Profile(
            id=_unique_id("custom", self._profile_ids()),
            name=name,
            backend="auto",
            model=None,
            scale=None,
            target=Target(TargetKind.DISPLAY),
            sc_threshold=DEFAULT_SC_THRESHOLD,
            buffered_frames=None,
            concurrent_frames=None,
        )
        self._add_profile(p)
        self.ctx.announce(self.tr("Added profile {name}.").format(name=name))

    def duplicate_profile(self) -> None:
        src = self._profile()
        if src is None or not self._editable():
            return
        p = dataclasses.replace(
            src,
            id=_unique_id(f"{src.id}-copy", self._profile_ids()),
            name=self.tr("{name} (copy)").format(name=src.name),
            builtin=False,
        )
        self._add_profile(p)
        self.ctx.announce(self.tr("Duplicated as {name}.").format(name=p.name))

    def _add_profile(self, p: Profile) -> None:
        assert self._cfg is not None
        self._set_cfg(dataclasses.replace(self._cfg, profiles=self._cfg.profiles + (p,)))
        self._rebuild_profile_list(select=len(self._cfg.profiles) - 1)
        self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self.name_edit.selectAll()

    def rename_profile(self) -> None:
        p = self._profile()
        if p is None or p.builtin or not self._editable():
            return
        self.tabs.setCurrentWidget(self.profiles_tab)
        self.name_edit.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.name_edit.selectAll()

    def delete_blocker(self, p: Profile | None = None) -> str:
        """Why the profile can't be deleted ("" when it can)."""
        p = p if p is not None else self._profile()
        if p is None or self._cfg is None:
            return ""
        if p.builtin:
            return self.tr("Built-in profiles can't be deleted.")
        used = [i + 1 for i, r in enumerate(self._cfg.rules) if r.profile == p.id]
        if len(used) == 1:
            return self.tr("Used by rule {n}").format(n=used[0])
        if used:
            return self.tr("Used by rules {list}").format(list=", ".join(str(n) for n in used))
        return ""

    def delete_profile(self) -> None:
        p = self._profile()
        if p is None or self._cfg is None or not self._editable():
            return
        reason = self.delete_blocker(p)
        if reason:
            self.ctx.announce(
                self.tr("Can't delete {name}: {reason}").format(name=p.name, reason=reason),
                assertive=True,
            )
            return
        index = self._current
        profiles = self._cfg.profiles[:index] + self._cfg.profiles[index + 1 :]
        self._set_cfg(dataclasses.replace(self._cfg, profiles=profiles))
        self._rebuild_profile_list(select=min(index, len(profiles) - 1))
        self.ctx.announce(self.tr("Deleted profile {name}.").format(name=p.name))

    def _shipped(self, p: Profile) -> Profile | None:
        try:
            return next((b for b in builtin_profiles() if b.id == p.id), None)
        except ButterEyeError:
            return None

    def reset_profile(self) -> None:
        p = self._profile()
        if p is None or not p.builtin or not self._editable():
            return
        shipped = self._shipped(p)
        if shipped is None or shipped == p:
            return
        self._replace_profile(self._current, shipped)
        self._show_profile()
        self.ctx.announce(self.tr("{name} reset to the shipped settings.").format(name=p.name))

    # ================================================================== profile list / form
    def _profile_text(self, i: int, p: Profile) -> str:
        parts = [p.name or p.id]
        if p.builtin:
            parts.append(self.tr("Built-in"))
        if self._profile_has_issue(i):
            parts.append(self.tr("needs fixing"))
        return " — ".join(parts)

    def _profile_has_issue(self, i: int) -> bool:
        for issue in self._issues:
            m = _PROFILE_FIELD.match(issue.field or "")
            if m is not None and int(m.group(1)) == i:
                return True
        return False

    def _rebuild_profile_list(self, *, select: int) -> None:
        cfg = self._cfg
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        if cfg is not None:
            for i, p in enumerate(cfg.profiles):
                item = QListWidgetItem(self._profile_text(i, p))
                item.setData(Qt.ItemDataRole.UserRole, p.id)
                self.profile_list.addItem(item)
        self.profile_list.blockSignals(False)
        count = self.profile_list.count()
        self._current = -1
        if count:
            self.profile_list.setCurrentRow(min(max(select, 0), count - 1))
            self._on_profile_selected(self.profile_list.currentRow())
        else:
            self._show_profile()

    def _refresh_profile_texts(self) -> None:
        cfg = self._cfg
        if cfg is None or self.profile_list.count() != len(cfg.profiles):
            return
        for i, p in enumerate(cfg.profiles):
            item = self.profile_list.item(i)
            text = self._profile_text(i, p)
            if item is not None and item.text() != text:
                item.setText(text)

    def _on_profile_selected(self, row: int) -> None:
        self._current = row
        self._show_profile()

    def _show_profile(self) -> None:
        p = self._profile()
        self._populating = True
        try:
            if p is None:
                self.name_edit.clear()
            else:
                if self.name_edit.text() != p.name:
                    self.name_edit.setText(p.name)
                self._fill_engine(p)
                self._fill_model(p)
                self.scale_spin.setValue(p.scale if p.scale is not None else 0.0)
                kind = p.target.kind
                {
                    TargetKind.DISPLAY: self.target_display,
                    TargetKind.DISPLAY_MAX: self.target_display_max,
                    TargetKind.X2: self.target_2x,
                    TargetKind.FPS: self.target_fixed,
                }[kind].setChecked(True)
                if kind is TargetKind.FPS and p.target.fps is not None:
                    if self.target_fps.value() != p.target.fps:
                        try:
                            self.target_fps.setValue(p.target.fps)
                        except ValueError:
                            self.target_fps.setText(format_rate(p.target.fps))
                elif kind is not TargetKind.FPS:
                    self.target_fps.setValue(None)
                self.sc_spin.setValue(float(p.sc_threshold))
                self.sc_slider.setValue(round(float(p.sc_threshold) * 100))
                self._set_frames(self.buffered_spin, self.buffered_auto, p.buffered_frames)
                self._set_frames(self.concurrent_spin, self.concurrent_auto, p.concurrent_frames)
                (self.hdr_pass if p.hdr == "passthrough" else self.hdr_skip).setChecked(True)
        finally:
            self._populating = False
        self._update_frames_text(p)
        self._update_hdr_note()
        self._show_issues()
        self._update_profile_controls()

    def _set_frames(self, spin: QSpinBox, auto: QCheckBox, value: int | None) -> None:
        auto.setChecked(value is None)
        if value is not None:
            spin.setValue(max(spin.minimum(), min(spin.maximum(), value)))

    def _update_frames_text(self, p: Profile | None) -> None:
        self.buffered_auto.setText(self.tr("Automatic ({n})").format(n=DEFAULT_BUFFERED_FRAMES))
        n = concurrent_default(p.backend) if p is not None else None
        self.concurrent_auto.setText(
            self.tr("Automatic ({n})").format(n=n)
            if n is not None
            else self.tr("Automatic (depends on the engine)")
        )

    def _fill_engine(self, p: Profile | None) -> None:
        cfg = self._cfg
        combo = self.engine_combo
        was = self._populating
        self._populating = True
        try:
            combo.clear()
            trt_on = cfg is not None and cfg.general.trt_experimental
            choices: list[BackendId | str] = ["auto", BackendId.RIFE_NCNN]
            if trt_on or (p is not None and p.backend == BackendId.RIFE_TRT):
                choices.append(BackendId.RIFE_TRT)
            choices.append(BackendId.MVTOOLS)
            model = cast(QStandardItemModel, combo.model())
            notes: list[str] = []
            for b in choices:
                combo.addItem(engine_label(b), str(b))
                item = model.item(combo.count() - 1)
                status = self._backends.get(BackendId(b)) if b != "auto" else None
                off = status is not None and not status.available
                if b == BackendId.RIFE_TRT and not trt_on:
                    off = True
                    reason = self.tr("Experimental NVIDIA TensorRT is off.")
                else:
                    reason = render(status.reason) if off and status is not None else ""
                if off and item is not None:
                    item.setEnabled(False)
                    item.setData(reason, Qt.ItemDataRole.AccessibleDescriptionRole)
                    item.setToolTip(reason)
                    notes.append(
                        self.tr("{engine}: not available — {reason}").format(
                            engine=engine_label(b), reason=reason
                        )
                    )
            if p is not None:
                combo.setCurrentIndex(max(combo.findData(str(p.backend)), 0))
            note = "\n".join(notes)
            self.engine_note.setText(note)
            self.engine_note.setVisible(bool(note))
            combo.setAccessibleDescription(note)
        finally:
            self._populating = was
        self._update_scale_note(p)

    def _engine(self) -> BackendId | Literal["auto"]:
        data = self.engine_combo.currentData()
        if data == "auto" or data is None:
            return "auto"
        return BackendId(str(data))

    def _fill_model(self, p: Profile | None) -> None:
        combo = self.model_combo
        was = self._populating
        self._populating = True
        try:
            combo.clear()
            backend = p.backend if p is not None else "auto"
            free_text = self._models is None
            combo.setEditable(free_text)
            edit = combo.lineEdit()
            if free_text and edit is not None:
                edit.setPlaceholderText(self.tr("Automatic"))
                combo.setEditText(p.model or "" if p is not None else "")
                note = self.tr(
                    "The installed model list isn't available; type a model name, "
                    "or leave it empty for automatic."
                )
            else:
                combo.addItem(self.tr("Automatic"), None)
                names: set[str] = set()
                for m in self._models or ():
                    if not m.installed:
                        continue
                    if backend != "auto" and m.backend != backend:
                        continue
                    label = m.name
                    if m.kind is ModelKind.EXTRA:
                        label = self.tr("{name} (extra)").format(name=m.name)
                    elif m.kind is ModelKind.UNPINNED:
                        label = self.tr("{name} (unpinned)").format(name=m.name)
                    combo.addItem(label, m.name)
                    names.add(m.name)
                if p is not None and p.model is not None and p.model not in names:
                    combo.addItem(self.tr("{name} (not installed)").format(name=p.model), p.model)
                if p is not None:
                    combo.setCurrentIndex(max(combo.findData(p.model), 0))
                note = ""
                if backend == BackendId.MVTOOLS:
                    note = self.tr("MVTools uses no model.")
            self.model_note.setText(note)
            self.model_note.setVisible(bool(note))
            combo.setAccessibleDescription(note)
        finally:
            self._populating = was

    def _model(self) -> str | None:
        combo = self.model_combo
        if combo.isEditable():
            text = combo.currentText().strip()
            return text or None
        data = combo.currentData()
        return str(data) if data is not None else None

    def _update_scale_note(self, p: Profile | None) -> None:
        backend = self._engine() if p is not None else "auto"
        note = ""
        if backend == BackendId.RIFE_NCNN:
            note = self.tr(
                "At 4K, RIFE (Vulkan) works at a smaller size when the speed test "
                "says it can't keep up."
            )
        elif backend != BackendId.RIFE_TRT:
            note = self.tr("Scale applies to TensorRT only.")
        self.scale_note.setText(note)
        self.scale_note.setVisible(bool(note))
        self.scale_spin.setAccessibleDescription(note)

    def _update_hdr_note(self) -> None:
        caps = self.ctx.bridge.capabilities
        st = caps.states.get(Feature.HDR_PASSTHROUGH) if caps is not None else None
        available = st is not None and st.available
        note = "" if available else self.tr("Needs compatibility test M0(d).")
        self.hdr_note.setText(note)
        self.hdr_note.setVisible(bool(note))
        self.hdr_pass.setAccessibleDescription(note)
        self._hdr_available = available
        self._update_profile_controls()

    def _update_profile_controls(self) -> None:
        if not hasattr(self, "hdr_pass"):
            return
        p = self._profile()
        editable = self._editable()
        can_edit = editable and p is not None and not p.builtin
        self.builtin_note.setVisible(p is not None and p.builtin)
        for w in (
            self.name_edit,
            self.engine_combo,
            self.model_combo,
            self.target_display,
            self.target_display_max,
            self.target_2x,
            self.target_fixed,
            self.sc_slider,
            self.sc_spin,
            self.buffered_auto,
            self.concurrent_auto,
            self.hdr_skip,
        ):
            w.setEnabled(can_edit)
        backend = self._engine()
        self.model_combo.setEnabled(
            can_edit and (backend != BackendId.MVTOOLS or (p is not None and p.model is not None))
        )
        self.scale_spin.setEnabled(
            can_edit and (backend == BackendId.RIFE_TRT or (p is not None and p.scale is not None))
        )
        self._sync_target_fps(can_edit)
        self.buffered_spin.setEnabled(can_edit and not self.buffered_auto.isChecked())
        self.concurrent_spin.setEnabled(can_edit and not self.concurrent_auto.isChecked())
        hdr_ok = getattr(self, "_hdr_available", False)
        self.hdr_pass.setEnabled(can_edit and hdr_ok)
        self.new_button.setEnabled(editable)
        self.duplicate_button.setEnabled(editable and p is not None)
        self.rename_button.setEnabled(can_edit)
        blocker = self.delete_blocker(p) if p is not None and not p.builtin else ""
        self.delete_button.setEnabled(can_edit and not blocker)
        self.delete_button.setAccessibleDescription(blocker)
        self.delete_reason.setText(blocker)
        self.delete_reason.setVisible(bool(blocker))
        shipped = self._shipped(p) if p is not None and p.builtin else None
        self.reset_button.setEnabled(editable and shipped is not None and shipped != p)

    # ---- form -> Profile
    def _form_profile(self, p: Profile) -> Profile:
        kind = (
            TargetKind.FPS
            if self.target_fixed.isChecked()
            else TargetKind.X2
            if self.target_2x.isChecked()
            else TargetKind.DISPLAY_MAX
            if self.target_display_max.isChecked()
            else TargetKind.DISPLAY
        )
        target = Target(kind, self.target_fps.value() if kind is TargetKind.FPS else None)
        scale = self.scale_spin.value()
        return dataclasses.replace(
            p,
            name=self.name_edit.text(),
            backend=self._engine(),
            model=self._model(),
            scale=scale if scale > 0 else None,
            target=target,
            sc_threshold=round(self.sc_spin.value(), 4),
            buffered_frames=None if self.buffered_auto.isChecked() else self.buffered_spin.value(),
            concurrent_frames=None
            if self.concurrent_auto.isChecked()
            else self.concurrent_spin.value(),
            hdr="passthrough" if self.hdr_pass.isChecked() else "skip",
        )

    def _on_form_edited(self) -> None:
        if self._populating:
            return
        p = self._profile()
        if p is None or p.builtin or not self._editable():
            return
        new = self._form_profile(p)
        if new != p:
            self._replace_profile(self._current, new)
        self._update_profile_controls()

    def _on_engine_changed(self) -> None:
        if self._populating:
            return
        p = self._profile()
        if p is None:
            return
        backend = self._engine()
        self._populating = True
        try:
            if backend != BackendId.RIFE_TRT:
                self.scale_spin.setValue(0.0)
        finally:
            self._populating = False
        changed = dataclasses.replace(
            p,
            backend=backend,
            model=None if backend == BackendId.MVTOOLS else p.model,
            scale=p.scale if backend == BackendId.RIFE_TRT else None,
        )
        self._fill_model(changed)
        self._update_scale_note(changed)
        self._update_frames_text(changed)
        self._on_form_edited()

    def _on_model_text(self, _text: str) -> None:
        if self.model_combo.isEditable():
            self._on_form_edited()

    def _sync_target_fps(self, can_edit: bool) -> None:
        """The fixed-rate field exists only for "Fixed rate": hidden (not just disabled
        with a stale 24000/1001) while "Match my display" or "Double" is chosen."""
        fixed = self.target_fixed.isChecked()
        self.target_fps.setEnabled(can_edit and fixed)
        self.target_fps.setVisible(fixed)
        self.target_fps_label.setVisible(fixed)

    def _on_target_kind(self) -> None:
        self._sync_target_fps(self._editable())
        if self.target_fixed.isChecked() and not self._populating:
            self.target_fps.setFocus(Qt.FocusReason.OtherFocusReason)
        self._on_form_edited()

    def _on_sc_slider(self, v: int) -> None:
        if self._populating:
            return
        if round(self.sc_spin.value() * 100) != v:
            self.sc_spin.setValue(v / 100)

    def _on_sc_spin(self, v: float) -> None:
        if not self._populating and self.sc_slider.value() != round(v * 100):
            self._populating = True
            try:
                self.sc_slider.setValue(round(v * 100))
            finally:
                self._populating = False
        self._on_form_edited()

    def _on_auto(self, spin: QSpinBox, on: bool) -> None:
        spin.setEnabled(not on and self._editable())
        if not on and not self._populating:
            spin.setFocus(Qt.FocusReason.OtherFocusReason)
        self._on_form_edited()

    def _on_expert(self, on: bool) -> None:
        self.expert_box.setVisible(on)
        self.expert_button.setText(
            self.tr("Hide e&xpert settings") if on else self.tr("Show e&xpert settings")
        )
        self.expert_button.setAccessibleName(plain(self.expert_button.text()))

    # ================================================================== validation
    def validate_now(self) -> None:
        """Run ``validate_config`` on the local copy and show the issues inline."""
        self._validate_timer.stop()
        if self._cfg is None:
            self._issues = ()
            return
        try:
            self._issues = validate_config(self._cfg)
        except NotAvailable as err:
            self.panel.show_unavailable(err.feature, err.state, command_hint("profiles.list"))
            return
        except ButterEyeError as err:
            self._issues = ()
            self._set_banner("error", Banner.from_error(err, ()))
            return
        self._refresh_profile_texts()
        self._show_issues()
        self._show_rule_issues()

    def issues(self) -> tuple[ConfigIssue, ...]:
        return self._issues

    def _show_issues(self) -> None:
        by_key: dict[str, list[str]] = {}
        general: list[str] = []
        for issue in self._issues:
            f = issue.field or ""
            m = _PROFILE_FIELD.match(f)
            if m is not None:
                if int(m.group(1)) == self._current:
                    key = _FIELD_ROW.get(m.group(2), m.group(2))
                    by_key.setdefault(key, []).append(render(issue.message))
                continue
            if _RULE_FIELD.match(f):
                continue
            general.append(render(issue.message))
        for key, badge in self.issue_badges.items():
            texts = by_key.get(key)
            target = self.field_widgets[key]
            if texts:
                text = " ".join(texts)
                badge.set_state("blocking", text)
                badge.show()
                target.setAccessibleDescription(text)
                if key in ("buffered_frames", "concurrent_frames"):
                    self.expert_button.setChecked(True)
            else:
                badge.hide()
                if key not in ("backend", "model", "scale"):
                    target.setAccessibleDescription("")
        for old in self.general_issues:
            self._general_lay.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.general_issues = []
        for text in general:
            b = StatusBadge("blocking", text, self.panel.content)
            self._general_lay.addWidget(b)
            self.general_issues.append(b)

    def _show_rule_issues(self) -> None:
        for old in self.rule_issues:
            self._rule_issue_lay.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.rule_issues = []
        bad_rows: set[int] = set()
        for issue in self._issues:
            m = _RULE_FIELD.match(issue.field or "")
            if m is None:
                continue
            n = int(m.group(1))
            bad_rows.add(n)
            b = StatusBadge(
                "blocking",
                self.tr("Rule {n}: {problem}").format(n=n + 1, problem=render(issue.message)),
                self.rules_tab,
            )
            self._rule_issue_lay.addWidget(b)
            self.rule_issues.append(b)
        for row in range(self.rules_model.rowCount()):
            item = self.rules_model.item(row, 0)
            if item is not None:
                text = str(row + 1) + (" " + self.tr("(needs fixing)") if row in bad_rows else "")
                if item.text() != text:
                    item.setText(text)

    def focus_first_issue(self) -> None:
        """Move focus to the first invalid field (Save with problems, §4.4)."""
        if not self._issues:
            return
        f = self._issues[0].field or ""
        m = _PROFILE_FIELD.match(f)
        if m is not None:
            index = int(m.group(1))
            self.tabs.setCurrentWidget(self.profiles_tab)
            if index != self._current:
                self.profile_list.setCurrentRow(index)
            key = _FIELD_ROW.get(m.group(2), m.group(2))
            if key in ("buffered_frames", "concurrent_frames"):
                self.expert_button.setChecked(True)
            w = self.field_widgets.get(key, self.name_edit)
            if not w.isEnabled():
                w = self.profile_list
            w.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        r = _RULE_FIELD.match(f)
        if r is not None:
            self.tabs.setCurrentWidget(self.rules_tab)
            self.rules_view.selectRow(int(r.group(1)))
            self.rules_view.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.save_button.setFocus(Qt.FocusReason.OtherFocusReason)

    # ================================================================== rules
    def _rebuild_rules(self) -> None:
        cfg = self._cfg
        keep = self._rule_row()
        self.rules_model.removeRows(0, self.rules_model.rowCount())
        if cfg is None:
            return
        names = {p.id: p for p in cfg.profiles}
        for i, r in enumerate(cfg.rules):
            p = names.get(r.profile)
            use = (
                profile_label(p)
                if p is not None
                else self.tr("{id} (missing profile)").format(id=r.profile)
            )
            row = [
                QStandardItem(str(i + 1)),
                QStandardItem(describe_match(r.match)),
                QStandardItem(use),
            ]
            for it in row:
                it.setEditable(False)
            self.rules_model.appendRow(row)
        if keep is not None and self.rules_model.rowCount():
            self.rules_view.selectRow(min(keep, self.rules_model.rowCount() - 1))
        self._show_rule_issues()
        self._update_rule_buttons()

    def _rule_row(self) -> int | None:
        idx: QModelIndex = self.rules_view.currentIndex()
        return idx.row() if idx.isValid() else None

    def _update_rule_buttons(self) -> None:
        if not hasattr(self, "down_button"):
            return
        editable = self._editable()
        row = self._rule_row()
        n = self.rules_model.rowCount()
        has = row is not None and 0 <= row < n
        self.add_rule_button.setEnabled(editable)
        self.edit_rule_button.setEnabled(editable and has)
        self.delete_rule_button.setEnabled(editable and has)
        self.up_button.setEnabled(editable and has and row is not None and row > 0)
        self.down_button.setEnabled(editable and has and row is not None and row < n - 1)

    def _open_rule_dialog(self, rule: Rule | None) -> Rule | None:
        assert self._cfg is not None
        dlg = RuleEditDialog(rule, self._cfg.profiles, self)
        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        result = dlg.rule() if accepted and not dlg.problem_text() else None
        dlg.deleteLater()
        return result

    def add_rule(self) -> None:
        if self._cfg is None or not self._editable():
            return
        rule = self._open_rule_dialog(None)
        if rule is None:
            return
        row = self._rule_row()
        at = row + 1 if row is not None else len(self._cfg.rules)
        rules = self._cfg.rules[:at] + (rule,) + self._cfg.rules[at:]
        self._set_cfg(dataclasses.replace(self._cfg, rules=rules), rules_changed=True)
        self.rules_view.selectRow(at)
        self.rules_view.setFocus(Qt.FocusReason.OtherFocusReason)
        self.ctx.announce(self.tr("Rule {n} added.").format(n=at + 1))

    def edit_rule(self) -> None:
        row = self._rule_row()
        if self._cfg is None or row is None or not self._editable():
            return
        rule = self._open_rule_dialog(self._cfg.rules[row])
        if rule is None or rule == self._cfg.rules[row]:
            return
        rules = list(self._cfg.rules)
        rules[row] = rule
        self._set_cfg(dataclasses.replace(self._cfg, rules=tuple(rules)), rules_changed=True)
        self.rules_view.selectRow(row)
        self.rules_view.setFocus(Qt.FocusReason.OtherFocusReason)

    def delete_rule(self) -> None:
        row = self._rule_row()
        if self._cfg is None or row is None or not self._editable():
            return
        rules = self._cfg.rules[:row] + self._cfg.rules[row + 1 :]
        self._set_cfg(dataclasses.replace(self._cfg, rules=rules), rules_changed=True)
        if rules:
            self.rules_view.selectRow(min(row, len(rules) - 1))
        self.ctx.announce(self.tr("Rule {n} deleted.").format(n=row + 1))

    def move_rule(self, delta: int) -> None:
        row = self._rule_row()
        if self._cfg is None or row is None or not self._editable():
            return
        to = row + delta
        if not 0 <= to < len(self._cfg.rules):
            return
        rules = list(self._cfg.rules)
        rules[row], rules[to] = rules[to], rules[row]
        self._set_cfg(dataclasses.replace(self._cfg, rules=tuple(rules)), rules_changed=True)
        self.rules_view.selectRow(to)
        self.ctx.announce(self.tr("Rule moved to position {n}.").format(n=to + 1))

    # ================================================================== tester
    def _update_session_button(self) -> None:
        facts = self.ctx.current_session_facts()
        self.use_session_button.setEnabled(facts is not None)
        self.use_session_button.setAccessibleDescription(
            "" if facts is not None else self.tr("No live session is selected.")
        )

    def use_current_session(self) -> None:
        facts = self.ctx.current_session_facts()
        if facts is None:
            return
        self.set_test_facts(facts)

    def set_test_facts(self, facts: SourceFacts) -> None:
        widgets = (
            self.t_fps,
            self.t_width,
            self.t_height,
            self.t_hdr,
            self.t_hz,
            self.t_interlaced,
            self.t_path,
        )
        for w in widgets:
            w.blockSignals(True)
        try:
            try:
                self.t_fps.setValue(facts.fps)
            except ValueError:
                self.t_fps.setText(format_rate(facts.fps))
            self.t_width.setValue(facts.width)
            self.t_height.setValue(facts.height)
            self.t_hdr.setCurrentIndex(max(self.t_hdr.findData(facts.hdr_class.value), 0))
            self.t_hz.setValue(facts.display_hz)
            self.t_interlaced.setChecked(facts.interlaced)
            self.t_path.setText(facts.path)
        finally:
            for w in widgets:
                w.blockSignals(False)
        self.run_tester()

    def test_facts(self) -> SourceFacts | None:
        fps = self.t_fps.value()
        if fps is None:
            return None
        hdr = self.t_hdr.currentData()
        return SourceFacts(
            fps=fps,
            width=self.t_width.value(),
            height=self.t_height.value(),
            hdr_class=HdrClass(str(hdr)) if hdr is not None else HdrClass.SDR,
            display_hz=self.t_hz.value(),
            interlaced=self.t_interlaced.isChecked(),
            path=self.t_path.text(),
        )

    def trace_texts(self) -> list[str]:
        return [label.text() for label in self.trace_lines[: self._trace_count]]

    def run_tester(self) -> None:
        """Explain the local rules for the tester's facts (pure, GUI thread)."""
        if not hasattr(self, "trace_box"):
            return
        lines: list[str] = []
        facts = self.test_facts()
        cfg = self._cfg
        trace: RuleTrace | None = None
        if cfg is None:
            lines.append(self.tr("Profiles are not loaded yet."))
        elif facts is None:
            lines.append(self.tr("Enter a frame rate to test, such as 24000/1001."))
        else:
            try:
                trace = explain_rules(cfg, facts)
            except ButterEyeError as err:
                lines.append(render(err.cause))
        if trace is not None and cfg is not None:
            names = {p.id: p.name for p in cfg.profiles}
            name = names.get(trace.profile_id, trace.profile_id)
            if trace.matched_index is not None:
                lines.append(
                    self.tr("Rule {n} matched → {profile}").format(
                        n=trace.matched_index + 1, profile=name
                    )
                )
            else:
                lines.append(self.tr("No rule matched → {profile} (default)").format(profile=name))
            for step in trace.steps:
                if step.matched:
                    lines.append(
                        self.tr("Rule {n}: {why}").format(n=step.index + 1, why=render(step.why))
                    )
                else:
                    lines.append(
                        self.tr("Rule {n} skipped: {why}").format(
                            n=step.index + 1, why=render(step.why)
                        )
                    )
        while len(self.trace_lines) < len(lines):
            label = QLabel(self.trace_box)
            label.setWordWrap(True)
            label.setTextFormat(Qt.TextFormat.PlainText)
            if not self.trace_lines:
                heading(label, factor=1.0)  # the result line: bold
            self._trace_lay.addWidget(label)
            self.trace_lines.append(label)
        self._trace_count = len(lines)
        for i, label in enumerate(self.trace_lines):
            if i < len(lines):
                label.setText(lines[i])
                label.show()
            else:
                label.hide()
        self.trace_box.setAccessibleName(lines[0] if lines else "")
        self.trace_box.setAccessibleDescription("\n".join(lines[1:]))
        self._update_tester_hint(facts)

    def _update_tester_hint(self, facts: SourceFacts | None) -> None:
        if facts is None:
            self.tester_hint.set_hint(None)
            return
        params = {
            "fps": format_rate(facts.fps),
            "width": str(facts.width),
            "height": str(facts.height),
            "hdr": facts.hdr_class.value,
            "display_hz": f"{facts.display_hz:g}",
            "interlaced": "true" if facts.interlaced else "false",
        }
        if facts.path:
            params["path"] = facts.path
        try:
            self.tester_hint.set_hint(command_hint("profiles.explain", **params))
        except KeyError:
            self.tester_hint.set_hint(None)


__all__ = ["ProfilesPage", "concurrent_default", "engine_label"]
