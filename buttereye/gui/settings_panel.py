# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Details → Settings (GUI.md §12.1.5): the small line ButterEye shows in mpv,
the picture after smoothing, the power preset, and whether per-file logs are
kept to copy. Each change is saved at once, like the main window's choices
(``[general] osd_seconds``, ``osd_items``, ``keep_logs``, ``upscaling``,
``picture``, ``lut``, ``deband*``, ``compare``, ``even_pacing``,
``cadence_first``); a slider or a number box saves
once it has been still for ``SLIDER_SAVE_MS``, not at every step of a drag.

Only what was changed here is written over the file as loaded, so a choice the
main window saved meanwhile survives a retry after ``ConfigConflict``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from buttereye.core import api as core_api
from buttereye.core.api import (
    DEBAND_GRAIN_RANGE,
    LUT_SUFFIX,
    OSD_ITEMS,
    PICTURE_KEYS,
    PICTURE_RANGE,
    ButterEye,
    ButterEyeError,
    Config,
    ConfigConflict,
    ConfigLoad,
    PictureAdjust,
    Rule,
    RuleMatch,
    osapi,
)
from buttereye.gui import a11y, theme
from buttereye.gui.bridge import CoreBridge

_log = logging.getLogger(__name__)

#: "Show for" choices: seconds (0 = off, -1 = always)
DURATIONS: tuple[float, ...] = (0.0, 1.0, 3.0, 5.0, 10.0, -1.0)
#: a slider or number box saves once it has been still this long
SLIDER_SAVE_MS = 400

#: the shipped profile the power preset switches to (``defaults.BATTERY_PROFILE_ID``)
BATTERY_PROFILE = "battery"
#: the rules each power preset puts first in ``[[rules]]``. "battery" is
#: ``defaults.BATTERY_RULE``; "cool" adds the same profile while macOS reports
#: the Mac as running hot (serious or worse, ``THERMAL_STATES[2]``), which on a
#: fanless Air is when it starts slowing down. Off macOS the power state is
#: unknown and reads as mains power at nominal temperature, so neither matches.
POWER_RULES: dict[str, tuple[Rule, ...]] = {
    "off": (),
    "battery": (Rule(RuleMatch(on_battery=True), BATTERY_PROFILE),),
    "cool": (
        Rule(RuleMatch(on_battery=True), BATTERY_PROFILE),
        Rule(RuleMatch(thermal_min=2), BATTERY_PROFILE),
    ),
}
_PRESET_RULES = frozenset(r for rules in POWER_RULES.values() for r in rules)


def power_choice(rules: tuple[Rule, ...]) -> str:
    """The preset ``rules`` start with: the longest one whose rules are all
    there (a user who moved one down still sees it as on)."""
    have = set(rules)
    for key in ("cool", "battery"):
        if all(r in have for r in POWER_RULES[key]):
            return key
    return "off"


def with_power_rules(rules: tuple[Rule, ...], choice: str) -> tuple[Rule, ...]:
    """``rules`` with the preset's rules first and no other preset rule left."""
    return POWER_RULES[choice] + tuple(r for r in rules if r not in _PRESET_RULES)


async def import_lut(core: ButterEye, source: Path) -> tuple[str | None, str]:
    """Copy ``source`` into ButterEye's LUT folder (``profiles.luts.import_lut``)
    off the loop: (the name to store, "") or (None, why not). A ``LutError`` is
    a sentence for the user, not an internal error."""
    fn = getattr(core_api, "import_lut", None)
    if fn is None:
        return None, "This build of ButterEye can't import LUTs yet."
    try:
        return await asyncio.to_thread(fn, core.paths(), source), ""
    except (ValueError, OSError) as exc:
        return None, str(exc)


class SettingsPanel(QWidget):
    def __init__(self, bridge: CoreBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self.bridge = bridge
        self.load: ConfigLoad | None = None
        self._updating = False
        self._retried = False
        #: the [general] fields changed here since the last load, by name
        self._want: dict[str, object] = {}
        #: the power preset chosen here (None: not touched since the last load)
        self._want_power: str | None = None
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SLIDER_SAVE_MS)
        self._save_timer.timeout.connect(self._save)
        lay = QVBoxLayout(self)

        osd_box = QGroupBox(self.tr("On-screen info in mpv"), self)
        grid = QGridLayout(osd_box)
        self.duration = QComboBox(osd_box)
        self.duration.setObjectName("osdDuration")
        label, _ = a11y.labelled(
            self.tr("Show &for"),
            self.duration,
            description=self.tr(
                "A small line at the top left of mpv when smoothing starts or changes."
            ),
        )
        grid.addWidget(label, 0, 0)
        grid.addWidget(self.duration, 0, 1, Qt.AlignmentFlag.AlignLeft)
        self.items: dict[str, QCheckBox] = {}
        texts = {
            "rates": self.tr("Frame rates (24 → 60 fps)"),
            "engine": self.tr("CPU or GPU, and the method"),
            "size": self.tr("The size it smooths at"),
            "health": self.tr("Dropped frames"),
        }
        for i, key in enumerate(OSD_ITEMS):
            box = QCheckBox(texts[key], osd_box)
            box.setObjectName(f"osd.{key}")
            box.toggled.connect(lambda _on: self._osd_changed())
            self.items[key] = box
            grid.addWidget(box, 1 + i, 1)
        grid.setColumnStretch(1, 1)
        lay.addWidget(osd_box)

        self._build_picture(lay)
        self._build_pacing(lay)
        self._build_power(lay)

        log_box = QGroupBox(self.tr("Logs"), self)
        ll = QVBoxLayout(log_box)
        self.keep_logs = QCheckBox(self.tr("Keep logs I can copy"), log_box)
        self.keep_logs.setObjectName("keepLogs")
        note = QLabel(
            self.tr(
                "What happened with each video and copy, for Copy log and More logs. "
                "Off: no log buttons, and each mpv log is deleted when the video closes."
            ),
            log_box,
        )
        note.setWordWrap(True)
        theme.secondary(note)
        self.keep_logs.setAccessibleDescription(note.text())
        self.keep_logs.toggled.connect(lambda on: self._set(keep_logs=on))
        ll.addWidget(self.keep_logs)
        ll.addWidget(note)
        lay.addWidget(log_box)
        lay.addStretch(1)
        self.duration.currentIndexChanged.connect(lambda _i: self._osd_changed())
        self.refresh()

    # ------------------------------------------------------------------ build
    def _note(self, text: str, parent: QWidget) -> QLabel:
        note = QLabel(text, parent)
        note.setWordWrap(True)
        note.setTextFormat(Qt.TextFormat.PlainText)
        theme.secondary(note)
        return note

    def _build_picture(self, lay: QVBoxLayout) -> None:
        box = QGroupBox(self.tr("Picture"), self)
        self.picture_box = box
        grid = QGridLayout(box)
        row = 0

        self.upscaler = QComboBox(box)
        self.upscaler.setObjectName("upscaler")
        for text, key in (
            (self.tr("Standard (mpv's own)"), "standard"),
            (self.tr("Sharper (FSRCNNX)"), "sharper"),
            (self.tr("Sharp edges, lighter (RAVU)"), "ravu"),
        ):
            self.upscaler.addItem(text, key)
        label, _ = a11y.labelled(
            self.tr("Upscaler"),
            self.upscaler,
            description=self.tr(
                "Used when the video is smaller than the window. FSRCNNX is the "
                "sharpest and costs the most GPU time; RAVU keeps edges crisp for "
                "much less."
            ),
        )
        grid.addWidget(label, row, 0)
        grid.addWidget(self.upscaler, row, 1, 1, 2, Qt.AlignmentFlag.AlignLeft)
        self.upscaler.activated.connect(
            lambda _i: self._set(upscaling=str(self.upscaler.currentData()))
        )
        row += 1

        # colour controls: mpv's own, -100..100, after smoothing
        lo, hi = PICTURE_RANGE
        names = {
            "brightness": self.tr("Brightness"),
            "contrast": self.tr("Contrast"),
            "saturation": self.tr("Saturation"),
            "gamma": self.tr("Gamma"),
            "hue": self.tr("Hue"),
        }
        self.picture_sliders: dict[str, QSlider] = {}
        self.picture_spins: dict[str, QSpinBox] = {}
        for key in PICTURE_KEYS:
            slider = QSlider(Qt.Orientation.Horizontal, box)
            slider.setObjectName(f"picture.{key}")
            slider.setRange(lo, hi)
            slider.setPageStep(10)
            spin = QSpinBox(box)
            spin.setObjectName(f"picture.{key}.value")
            spin.setRange(lo, hi)
            label, _ = a11y.labelled(names[key], spin)
            slider.setAccessibleName(self.tr("{name}, slider").format(name=names[key]))
            slider.setAccessibleDescription(
                self.tr("{lo} to {hi}; 0 leaves the video as it is.").format(lo=lo, hi=hi)
            )
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            spin.valueChanged.connect(lambda _v: self._picture_changed())
            grid.addWidget(label, row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(spin, row, 2)
            self.picture_sliders[key] = slider
            self.picture_spins[key] = spin
            row += 1
        self.picture_reset = QPushButton(self.tr("Reset colours"), box)
        self.picture_reset.setObjectName("picture.reset")
        self.picture_reset.setAccessibleName(self.tr("Reset colours"))
        self.picture_reset.setAutoDefault(False)
        self.picture_reset.clicked.connect(self.reset_picture)
        grid.addWidget(self.picture_reset, row, 1, 1, 2, Qt.AlignmentFlag.AlignLeft)
        row += 1

        # 3D LUT: a file copied into ButterEye's own folder, stored by name
        self.lut_name = QLabel(box)
        self.lut_name.setObjectName("lut.name")
        self.lut_name.setTextFormat(Qt.TextFormat.PlainText)
        a11y.selectable(self.lut_name, name=self.tr("Colour LUT"))
        lut_label = QLabel(self.tr("Colour LUT"), box)
        self.lut_import = QPushButton(self.tr("Import…"), box)
        self.lut_import.setObjectName("lut.import")
        self.lut_import.setAccessibleName(self.tr("Import a colour LUT"))
        self.lut_import.setAccessibleDescription(
            self.tr("A .cube file; ButterEye keeps its own copy.")
        )
        self.lut_import.setAutoDefault(False)
        self.lut_import.clicked.connect(self.choose_lut)
        self.lut_clear = QPushButton(self.tr("No LUT"), box)
        self.lut_clear.setObjectName("lut.clear")
        self.lut_clear.setAccessibleName(self.tr("Use no colour LUT"))
        self.lut_clear.setAutoDefault(False)
        self.lut_clear.clicked.connect(lambda: self._set_lut(None))
        lut_row = QHBoxLayout()
        lut_row.addWidget(self.lut_name, 1)
        lut_row.addWidget(self.lut_import)
        lut_row.addWidget(self.lut_clear)
        grid.addWidget(lut_label, row, 0)
        grid.addLayout(lut_row, row, 1, 1, 2)
        row += 1
        self.lut_error = self._note("", box)
        self.lut_error.setObjectName("lut.error")
        self.lut_error.hide()
        grid.addWidget(self.lut_error, row, 1, 1, 2)
        row += 1

        # debanding: off / light / strong, and its grain
        self.deband = QComboBox(box)
        self.deband.setObjectName("deband")
        for text, key in (
            (self.tr("Off"), "off"),
            (self.tr("Light"), "light"),
            (self.tr("Strong"), "strong"),
        ):
            self.deband.addItem(text, key)
        label, _ = a11y.labelled(
            self.tr("Less banding"),
            self.deband,
            description=self.tr(
                "Smooths the steps in skies and dark gradients. Strong also smooths "
                "fine detail a little."
            ),
        )
        grid.addWidget(label, row, 0)
        grid.addWidget(self.deband, row, 1, 1, 2, Qt.AlignmentFlag.AlignLeft)
        self.deband.activated.connect(lambda _i: self._deband_changed())
        row += 1
        g_lo, g_hi = DEBAND_GRAIN_RANGE
        self.grain = QSpinBox(box)
        self.grain.setObjectName("deband.grain")
        # one below the range is "Automatic": mpv's own grain (32)
        self.grain.setRange(g_lo - 1, g_hi)
        self.grain.setSpecialValueText(self.tr("Automatic"))
        label, _ = a11y.labelled(
            self.tr("Grain"),
            self.grain,
            description=self.tr("Noise added over debanded areas to hide what is left."),
        )
        grid.addWidget(label, row, 0)
        grid.addWidget(self.grain, row, 1, 1, 2, Qt.AlignmentFlag.AlignLeft)
        self.grain.valueChanged.connect(lambda _v: self._grain_changed())
        row += 1

        self.compare = QCheckBox(self.tr("Compare: original on the left half"), box)
        self.compare.setObjectName("compare")
        self.compare.setAccessibleName(a11y.plain(self.compare.text()))
        self.compare.setAccessibleDescription(
            self.tr(
                "Shows the video unsmoothed on the left and smoothed on the right. "
                "Alt+C in mpv switches it for the video playing."
            )
        )
        self.compare.toggled.connect(lambda on: self._set(compare="split" if on else "off"))
        grid.addWidget(self.compare, row, 0, 1, 3)
        grid.setColumnStretch(1, 1)
        lay.addWidget(box)

    def _build_pacing(self, lay: QVBoxLayout) -> None:
        # 2x of a 24 fps film is 48 fps, which a 60 Hz panel shows as 1-1-1-2
        # refreshes per frame; both are opt-in (mpvctl.decide.uneven_pacing)
        box = QGroupBox(self.tr("Even motion"), self)
        bl = QVBoxLayout(box)
        self.even_pacing = QCheckBox(self.tr("Blend frames the display can't show evenly"), box)
        self.even_pacing.setObjectName("evenPacing")
        note = self._note(
            self.tr(
                "When the smoothed rate doesn't divide into the display's, mpv blends "
                "the two frames around each in-between refresh, so the slight regular "
                "judder goes. Blending softens motion a little."
            ),
            box,
        )
        self.even_pacing.setAccessibleName(a11y.plain(self.even_pacing.text()))
        self.even_pacing.setAccessibleDescription(note.text())
        self.even_pacing.toggled.connect(lambda on: self._set(even_pacing=on))
        bl.addWidget(self.even_pacing)
        bl.addWidget(note)
        self.cadence_first = QCheckBox(self.tr("Match my display: even motion before size"), box)
        self.cadence_first.setObjectName("cadenceFirst")
        note = self._note(
            self.tr(
                "Match my display smooths at a smaller size when that keeps every frame "
                "evenly paced, instead of doubling at the full size."
            ),
            box,
        )
        self.cadence_first.setAccessibleName(a11y.plain(self.cadence_first.text()))
        self.cadence_first.setAccessibleDescription(note.text())
        self.cadence_first.toggled.connect(lambda on: self._set(cadence_first=on))
        bl.addWidget(self.cadence_first)
        bl.addWidget(note)
        lay.addWidget(box)

    def _build_power(self, lay: QVBoxLayout) -> None:
        box = QGroupBox(self.tr("Battery and heat"), self)
        self.power_box = box
        bl = QVBoxLayout(box)
        self.power = QComboBox(box)
        self.power.setObjectName("power")
        for text, key in (
            (self.tr("Always use my choice"), "off"),
            (self.tr("Use the Battery profile on battery"), "battery"),
            (self.tr("Battery profile on battery or when the Mac runs hot"), "cool"),
        ):
            self.power.addItem(text, key)
        note = self._note(
            self.tr(
                "The Battery profile smooths with GPU motion vectors at 2×, which uses "
                "far less power than RIFE. It applies to videos opened after the change."
            ),
            box,
        )
        label, _ = a11y.labelled(self.tr("Power"), self.power, description=note.text())
        row = QHBoxLayout()
        row.addWidget(label)
        row.addWidget(self.power, 1, Qt.AlignmentFlag.AlignLeft)
        bl.addLayout(row)
        bl.addWidget(note)
        self.power.activated.connect(lambda _i: self._power_changed())
        # the power state is only read on macOS; elsewhere these rules never match
        box.setVisible(osapi.IS_MACOS)
        lay.addWidget(box)

    # ------------------------------------------------------------------ load
    def refresh(self) -> None:
        if self._save_timer.isActive():
            # a slider still waiting to save: save it rather than reload over it
            self._save_timer.stop()
            self._save()
            return
        self.bridge.call(lambda core: core.load_config(), owner=self, ok=self._show, err=self._err)

    def _show(self, load: ConfigLoad) -> None:
        self.load = load
        self._want = {}
        self._want_power = None
        g = load.config.general
        self._updating = True
        try:
            self.duration.clear()
            values = list(DURATIONS)
            if g.osd_seconds not in values:  # a value set by hand in the file
                values.insert(-1, g.osd_seconds)
            for v in values:
                self.duration.addItem(self.duration_text(v), v)
            self.duration.setCurrentIndex(values.index(g.osd_seconds))
            for key, box in self.items.items():
                box.setChecked(key in g.osd_items)
                box.setEnabled(g.osd_seconds != 0)
            self.keep_logs.setChecked(g.keep_logs)
            self.upscaler.setCurrentIndex(max(0, self.upscaler.findData(g.upscaling)))
            for key in PICTURE_KEYS:
                self.picture_spins[key].setValue(int(getattr(g.picture, key)))
            self._show_lut(g.lut)
            self.deband.setCurrentIndex(
                self.deband.findData(g.deband_strength if g.deband else "off")
            )
            self.grain.setValue(self.grain.minimum() if g.deband_grain is None else g.deband_grain)
            self.grain.setEnabled(g.deband)
            self.compare.setChecked(g.compare == "split")
            self.even_pacing.setChecked(g.even_pacing)
            self.cadence_first.setChecked(g.cadence_first)
            self.power.setCurrentIndex(self.power.findData(power_choice(load.config.rules)))
            # a rule naming a profile that is not there would not validate
            has_battery = any(p.id == BATTERY_PROFILE for p in load.config.profiles)
            self.power.setEnabled(has_battery)
        finally:
            self._updating = False

    def _show_lut(self, name: str | None) -> None:
        self.lut_name.setText(name if name else self.tr("None"))
        self.lut_clear.setEnabled(name is not None)

    # ------------------------------------------------------------------ edits
    def _set(self, **fields: object) -> None:
        """Record [general] changes and save them now."""
        if self._updating or self.load is None:
            return
        self._want.update(fields)
        self._save_timer.stop()
        self._save()

    def _osd_changed(self) -> None:
        if self._updating or self.load is None:
            return
        seconds = float(self.duration.currentData())
        for box in self.items.values():
            box.setEnabled(seconds != 0)
        self._set(
            osd_seconds=seconds,
            osd_items=tuple(k for k in OSD_ITEMS if self.items[k].isChecked()),
        )

    def picture(self) -> PictureAdjust:
        return PictureAdjust(**{k: self.picture_spins[k].value() for k in PICTURE_KEYS})

    def _picture_changed(self) -> None:
        if self._updating or self.load is None:
            return
        self._want["picture"] = self.picture()
        self._save_timer.start()  # once the slider has been still for a moment

    def reset_picture(self) -> None:
        self._updating = True
        try:
            for spin in self.picture_spins.values():
                spin.setValue(0)
        finally:
            self._updating = False
        self._set(picture=PictureAdjust())

    def _deband_changed(self) -> None:
        key = str(self.deband.currentData())
        self.grain.setEnabled(key != "off")
        if key == "off":
            self._set(deband=False)
        else:
            self._set(deband=True, deband_strength=key)

    def _grain_changed(self) -> None:
        if self._updating or self.load is None:
            return
        v = self.grain.value()
        self._want["deband_grain"] = None if v < self.grain.minimum() + 1 else v
        self._save_timer.start()

    def _power_changed(self) -> None:
        if self._updating or self.load is None:
            return
        self._want_power = str(self.power.currentData())
        self._save_timer.stop()
        self._save()

    def choose_lut(self) -> None:
        name, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Import a colour LUT"),
            "",
            self.tr("3D LUT (*{suffix})").format(suffix=LUT_SUFFIX),
        )
        if name:
            self.import_lut(Path(name))

    def import_lut(self, source: Path) -> None:
        self.lut_error.hide()
        self.lut_import.setEnabled(False)
        self.bridge.call(
            lambda core: import_lut(core, source),
            owner=self,
            ok=self._lut_imported,
            err=self._lut_failed,
        )

    def _lut_imported(self, result: tuple[str | None, str]) -> None:
        self.lut_import.setEnabled(True)
        name, why = result
        if name is None:
            self.lut_error.setText(self.tr("Not imported: {why}").format(why=why))
            self.lut_error.show()
            a11y.announce(self.lut_error, self.lut_error.text())
            return
        self._set_lut(name)

    def _lut_failed(self, err: ButterEyeError) -> None:
        self.lut_import.setEnabled(True)
        self._err(err)

    def _set_lut(self, name: str | None) -> None:
        self._show_lut(name)
        self._set(lut=name)

    # ------------------------------------------------------------------ save
    def _save(self) -> None:
        if self.load is None:
            return
        cfg = self.load.config
        general = dataclasses.replace(cfg.general, **self._want)  # type: ignore[arg-type]
        rules = (
            cfg.rules if self._want_power is None else with_power_rules(cfg.rules, self._want_power)
        )
        cfg = dataclasses.replace(cfg, general=general, rules=rules)
        rev = self.load.revision
        self.bridge.call(
            lambda core: core.save_config(cfg, expected_revision=rev),
            owner=self,
            ok=lambda new: self._saved(cfg, new),
            err=self._save_failed,
        )

    def _saved(self, cfg: Config, revision: str) -> None:
        self._retried = False
        if self.load is not None:
            self.load = dataclasses.replace(self.load, config=cfg, revision=revision)

    def _save_failed(self, err: ButterEyeError) -> None:
        if isinstance(err, ConfigConflict) and not self._retried:
            # someone else saved meanwhile: take theirs, then put these choices on top
            self._retried = True
            self.bridge.call(
                lambda core: core.load_config(), owner=self, ok=self._reloaded, err=self._err
            )
            return
        self._err(err)

    def _reloaded(self, load: ConfigLoad) -> None:
        self.load = load
        self._save()

    def duration_text(self, seconds: float) -> str:
        if seconds == 0:
            return self.tr("Off")
        if seconds < 0:
            return self.tr("Always")
        if seconds == 1:
            return self.tr("1 second")
        return self.tr("{n} seconds").format(n=f"{seconds:g}")

    def _err(self, err: ButterEyeError) -> None:
        _log.warning("settings: %s", err)


__all__ = [
    "DURATIONS",
    "POWER_RULES",
    "SLIDER_SAVE_MS",
    "SettingsPanel",
    "import_lut",
    "power_choice",
    "with_power_rules",
]
