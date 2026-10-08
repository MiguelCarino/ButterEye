# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Rule editor dialog (docs/design/GUI.md §4.4; SCOPE §4.9, F15).

One row per ``[[rules]] match`` key, each with its own "Use" checkbox, so an
unset condition never looks like 0: an unused row shows "Not used" and its
input is disabled. The dialog only builds a ``Rule`` value; the Profiles page
puts it into its local ``Config`` copy.

Also home of the plain-language texts shared with the page: ``describe_match``
("fps ≤ 30 and height ≤ 1080"), ``hdr_label`` and ``fps_text``.
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QDoubleSpinBox,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import HdrClass, Profile, Rule, RuleMatch
from buttereye.gui.a11y import labelled, plain
from buttereye.gui.dialogs.fit import FitDialog, add_buttons
from buttereye.gui.widgets.fraction_edit import FractionEdit, format_rate

CONTEXT = "RuleEditDialog"


def _t(text: str) -> str:
    return QCoreApplication.translate("RuleEditDialog", text)


def hdr_label(h: HdrClass) -> str:
    """Display name of an HDR class."""
    names = {
        HdrClass.SDR: _t("SDR"),
        HdrClass.HDR10: _t("HDR10"),
        HdrClass.HDR10PLUS: _t("HDR10+"),
        HdrClass.HLG: _t("HLG"),
        HdrClass.DV: _t("Dolby Vision"),
    }
    return names[h]


def fps_text(v: Fraction) -> str:
    """``30`` or ``24000/1001 (23.976)``."""
    if v.denominator == 1:
        return str(v.numerator)
    return f"{format_rate(v)} ({float(v):.3f})"


def describe_match(m: RuleMatch) -> str:
    """The rule's condition as one sentence; "Always" when it has none."""
    parts: list[str] = []
    if m.fps_min is not None:
        parts.append(_t("fps ≥ {value}").format(value=fps_text(m.fps_min)))
    if m.fps_max is not None:
        parts.append(_t("fps ≤ {value}").format(value=fps_text(m.fps_max)))
    if m.width_max is not None:
        parts.append(_t("width ≤ {value}").format(value=m.width_max))
    if m.height_max is not None:
        parts.append(_t("height ≤ {value}").format(value=m.height_max))
    if m.hdr_class is not None:
        parts.append(_t("HDR is {value}").format(value=hdr_label(m.hdr_class)))
    if m.display_hz_min is not None:
        parts.append(_t("display ≥ {value} Hz").format(value=f"{m.display_hz_min:g}"))
    if m.interlaced is not None:
        parts.append(_t("interlaced") if m.interlaced else _t("not interlaced"))
    if m.path_glob is not None:
        parts.append(_t("path matches {value}").format(value=m.path_glob))
    if not parts:
        return _t("Always")
    return _t(" and ").join(parts)


def profile_label(p: Profile) -> str:
    return f"{p.name} ({p.id})"


#: (key, label, default when first ticked)
_SPIN_DEFAULTS = {"width_max": 1920, "height_max": 1080}
_HZ_DEFAULT = 120.0


class RuleEditDialog(FitDialog):
    """``RuleEditDialog(rule, profiles)``; after ``exec()`` read ``rule()``.

    ``rule`` ``None`` = a new rule. ``profiles`` are the profiles the rule may use.
    """

    def __init__(
        self,
        rule: Rule | None,
        profiles: Sequence[Profile],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("ruleEdit")
        self.setWindowTitle(self.tr("Add rule") if rule is None else self.tr("Edit rule"))
        self.setModal(True)
        m = rule.match if rule is not None else RuleMatch()
        self.use: dict[str, QCheckBox] = {}
        self.inputs: dict[str, QWidget] = {}

        lay = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "The rule matches when every condition you tick is true. "
                "Conditions you don't tick are not checked."
            ),
            self,
        )
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(intro)

        grid = QGridLayout()
        lay.addLayout(grid)

        self.fps_min = FractionEdit(None, self)
        self.fps_max = FractionEdit(None, self)
        self.width_max = QSpinBox(self)
        self.width_max.setRange(0, 16384)
        self.width_max.setSuffix(self.tr(" px"))
        self.height_max = QSpinBox(self)
        self.height_max.setRange(0, 8640)
        self.height_max.setSuffix(self.tr(" px"))
        self.hdr_class = QComboBox(self)
        for h in HdrClass:
            self.hdr_class.addItem(hdr_label(h), h.value)
        self.display_hz_min = QDoubleSpinBox(self)
        self.display_hz_min.setRange(0.0, 1000.0)
        self.display_hz_min.setDecimals(3)
        self.display_hz_min.setSuffix(self.tr(" Hz"))
        self.interlaced = QComboBox(self)
        self.interlaced.addItem(self.tr("Interlaced"), True)
        self.interlaced.addItem(self.tr("Not interlaced (progressive)"), False)
        self.path_glob = QLineEdit(self)
        self.path_glob.setPlaceholderText(self.tr("*.mkv or ~/Videos/Anime/**"))

        rows: tuple[tuple[str, str, QWidget], ...] = (
            ("fps_min", self.tr("Source fps at &least"), self.fps_min),
            ("fps_max", self.tr("Source fps at &most"), self.fps_max),
            ("width_max", self.tr("&Width at most"), self.width_max),
            ("height_max", self.tr("&Height at most"), self.height_max),
            ("hdr_class", self.tr("HD&R class is"), self.hdr_class),
            ("display_hz_min", self.tr("&Display refresh at least"), self.display_hz_min),
            ("interlaced", self.tr("&Interlacing is"), self.interlaced),
            ("path_glob", self.tr("File &path matches"), self.path_glob),
        )
        for row, (key, text, w) in enumerate(rows):
            name = plain(text)
            box = QCheckBox(self.tr("Use"), self)
            box.setObjectName(f"use.{key}")
            box.setAccessibleName(self.tr("Use condition: {name}").format(name=name))
            label, _ = labelled(text, w)
            label.setParent(self)
            w.setObjectName(f"input.{key}")
            self.use[key] = box
            self.inputs[key] = w
            grid.addWidget(box, row, 0)
            grid.addWidget(label, row, 1)
            grid.addWidget(w, row, 2)
            box.toggled.connect(lambda on, k=key: self._on_use(k, on))
        grid.setColumnStretch(2, 1)

        self.profile = QComboBox(self)
        self.profile.setObjectName("input.profile")
        for p in profiles:
            self.profile.addItem(profile_label(p), p.id)
        if rule is not None and self.profile.findData(rule.profile) < 0:
            self.profile.addItem(
                self.tr("{id} (missing profile)").format(id=rule.profile), rule.profile
            )
        plabel, _ = labelled(self.tr("Use &profile"), self.profile)
        plabel.setParent(self)
        grid.addWidget(plabel, len(rows), 1)
        grid.addWidget(self.profile, len(rows), 2)

        self.problem = QLabel(self)
        self.problem.setObjectName("problem")
        self.problem.setWordWrap(True)
        self.problem.setTextFormat(Qt.TextFormat.PlainText)
        lay.addWidget(self.problem)

        self.buttons = QDialogButtonBox(self)
        self.ok_button = QPushButton(self.tr("OK"), self)
        self.ok_button.setObjectName("ok")
        self.ok_button.setAccessibleName(self.tr("OK"))
        cancel = QPushButton(self.tr("Cancel"), self)
        cancel.setObjectName("cancel")
        cancel.setAccessibleName(self.tr("Cancel"))
        add_buttons(self.buttons, self.ok_button, cancel, default=self.ok_button)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)

        # initial values: set the input first, then tick the box
        self._set_initial(m, rule)
        for w in (self.fps_min, self.fps_max):
            w.valueChanged.connect(lambda _v: self._check())
        self.path_glob.textChanged.connect(lambda _t: self._check())
        self.profile.currentIndexChanged.connect(lambda _i: self._check())
        self._check()

    # ------------------------------------------------------------------ values
    def _set_initial(self, m: RuleMatch, rule: Rule | None) -> None:
        values: dict[str, object] = {
            "fps_min": m.fps_min,
            "fps_max": m.fps_max,
            "width_max": m.width_max,
            "height_max": m.height_max,
            "hdr_class": m.hdr_class,
            "display_hz_min": m.display_hz_min,
            "interlaced": m.interlaced,
            "path_glob": m.path_glob,
        }
        for key, value in values.items():
            used = value is not None
            if used:
                self._put(key, value)
            self.use[key].setChecked(used)
            self._on_use(key, used)
        if rule is not None:
            i = self.profile.findData(rule.profile)
            self.profile.setCurrentIndex(max(i, 0))

    def _put(self, key: str, value: object) -> None:
        w = self.inputs[key]
        if isinstance(w, FractionEdit):
            try:
                w.setValue(value if isinstance(value, Fraction) else None)
            except ValueError:  # out of range in the file: show it, the validator flags it
                w.setText(str(value))
        elif isinstance(w, QSpinBox) and isinstance(value, int):
            w.setSpecialValueText("")
            w.setValue(value)
        elif isinstance(w, QDoubleSpinBox) and isinstance(value, (int, float)):
            w.setSpecialValueText("")
            w.setValue(float(value))
        elif isinstance(w, QComboBox):
            data = value.value if isinstance(value, HdrClass) else value
            w.setCurrentIndex(max(w.findData(data), 0))
        elif isinstance(w, QLineEdit) and isinstance(value, str):
            w.setText(value)

    def _on_use(self, key: str, on: bool) -> None:
        w = self.inputs[key]
        w.setEnabled(on)
        not_used = self.tr("Not used")
        if isinstance(w, QAbstractSpinBox):
            assert isinstance(w, (QSpinBox, QDoubleSpinBox))
            if on:
                w.setSpecialValueText("")
                if w.value() <= w.minimum():
                    if isinstance(w, QSpinBox):
                        w.setValue(_SPIN_DEFAULTS.get(key, 1))
                    else:
                        w.setValue(_HZ_DEFAULT)
            else:
                w.setSpecialValueText(not_used)
                if isinstance(w, QSpinBox):
                    w.setValue(w.minimum())
                else:
                    w.setValue(w.minimum())
        elif isinstance(w, QComboBox):
            if on:
                if w.currentIndex() < 0:
                    w.setCurrentIndex(0)
            else:
                w.setPlaceholderText(not_used)
                w.setCurrentIndex(-1)
        elif isinstance(w, QLineEdit):
            w.setPlaceholderText(
                (self.tr("*.mkv or ~/Videos/Anime/**") if key == "path_glob" else "24000/1001")
                if on
                else not_used
            )
            if not on:
                w.setText("")
        if on and self.isVisible():
            w.setFocus(Qt.FocusReason.OtherFocusReason)
        self._check()

    def _check(self) -> None:
        if not hasattr(self, "ok_button"):
            return
        problem = self.problem_text()
        self.problem.setText(problem)
        self.problem.setVisible(bool(problem))
        self.ok_button.setEnabled(not problem)
        self.ok_button.setAccessibleDescription(problem)

    def problem_text(self) -> str:
        """Why OK is disabled ("" when the rule can be used)."""
        for key, w in (("fps_min", self.fps_min), ("fps_max", self.fps_max)):
            if self.use[key].isChecked() and w.value() is None:
                return self.tr("Enter a frame rate such as 30 or 24000/1001.")
        lo, hi = self.fps_min.value(), self.fps_max.value()
        if (
            self.use["fps_min"].isChecked()
            and self.use["fps_max"].isChecked()
            and lo is not None
            and hi is not None
            and lo > hi
        ):
            return self.tr("The lowest fps is above the highest fps; nothing can match.")
        if self.use["path_glob"].isChecked() and not self.path_glob.text().strip():
            return self.tr("Enter a path pattern, or untick it.")
        if self.profile.currentIndex() < 0:
            return self.tr("Choose the profile this rule uses.")
        return ""

    def match(self) -> RuleMatch:
        def used(key: str) -> bool:
            return self.use[key].isChecked()

        hdr = self.hdr_class.currentData()
        inter = self.interlaced.currentData()
        return RuleMatch(
            fps_min=self.fps_min.value() if used("fps_min") else None,
            fps_max=self.fps_max.value() if used("fps_max") else None,
            width_max=self.width_max.value() if used("width_max") else None,
            height_max=self.height_max.value() if used("height_max") else None,
            hdr_class=HdrClass(hdr) if used("hdr_class") and isinstance(hdr, str) else None,
            display_hz_min=self.display_hz_min.value() if used("display_hz_min") else None,
            interlaced=bool(inter) if used("interlaced") and inter is not None else None,
            path_glob=self.path_glob.text().strip() if used("path_glob") else None,
        )

    def rule(self) -> Rule:
        """The edited rule (valid only when ``problem_text()`` is empty)."""
        pid = self.profile.currentData()
        return Rule(self.match(), str(pid) if pid is not None else "")


__all__ = ["CONTEXT", "RuleEditDialog", "describe_match", "fps_text", "hdr_label", "profile_label"]
