# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Details → Settings (GUI.md §12.1.5): the small line ButterEye shows in mpv, and
whether per-file logs are kept to copy. Each change is saved at once, like the
main window's choices (``[general] osd_seconds``, ``osd_items``, ``keep_logs``).
"""

from __future__ import annotations

import dataclasses
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import OSD_ITEMS, ButterEyeError, Config, ConfigConflict, ConfigLoad
from buttereye.gui import a11y, theme
from buttereye.gui.bridge import CoreBridge

_log = logging.getLogger(__name__)

#: "Show for" choices: seconds (0 = off, -1 = always)
DURATIONS: tuple[float, ...] = (0.0, 1.0, 3.0, 5.0, 10.0, -1.0)


class SettingsPanel(QWidget):
    def __init__(self, bridge: CoreBridge, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        self.bridge = bridge
        self.load: ConfigLoad | None = None
        self._updating = False
        self._retried = False
        self._want: tuple[float, tuple[str, ...], bool] = (1.0, ("rates", "engine"), True)
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
            box.toggled.connect(lambda _on: self._changed())
            self.items[key] = box
            grid.addWidget(box, 1 + i, 1)
        grid.setColumnStretch(1, 1)
        lay.addWidget(osd_box)

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
        self.keep_logs.toggled.connect(lambda _on: self._changed())
        ll.addWidget(self.keep_logs)
        ll.addWidget(note)
        lay.addWidget(log_box)
        lay.addStretch(1)
        self.duration.currentIndexChanged.connect(lambda _i: self._changed())
        self.refresh()

    # ------------------------------------------------------------------ load
    def refresh(self) -> None:
        self.bridge.call(lambda core: core.load_config(), owner=self, ok=self._show, err=self._err)

    def _show(self, load: ConfigLoad) -> None:
        self.load = load
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
        finally:
            self._updating = False

    # ------------------------------------------------------------------ save
    def _changed(self) -> None:
        if self._updating or self.load is None:
            return
        seconds = float(self.duration.currentData())
        for box in self.items.values():
            box.setEnabled(seconds != 0)
        self._want = (
            seconds,
            tuple(k for k in OSD_ITEMS if self.items[k].isChecked()),
            self.keep_logs.isChecked(),
        )
        self._save()

    def _save(self) -> None:
        if self.load is None:
            return
        seconds, items, keep = self._want
        cfg = self.load.config
        cfg = dataclasses.replace(
            cfg,
            general=dataclasses.replace(
                cfg.general, osd_seconds=seconds, osd_items=items, keep_logs=keep
            ),
        )
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


__all__ = ["DURATIONS", "SettingsPanel"]
