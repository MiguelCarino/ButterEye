# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Save a smooth copy: the dialog and the progress rows (docs/design/GUI.md §12.7,
SCOPE §7.8).

One small dialog per video: size (with an estimated time for each), target,
format and where to save. Rows under "Saving copies" follow ``JobChanged``.
No error codes in the window; they stay in the log and Details.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    JobId,
    OpState,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
    RenderProbe,
    RenderSize,
    Target,
    TargetKind,
    render,
)
from buttereye.gui import a11y
from buttereye.gui.bridge import CoreBridge

SIMPLE_ID = "simple"
COPY_SUFFIX = ".smooth.mkv"
#: The original size is the default while its estimate is at most this many times
#: the video's length; otherwise the largest size that is (else the smallest).
DEFAULT_SIZE_FACTOR = 2.0

_CODE_SUFFIX = re.compile(r"\s*\(BE-\d+\)\s*$")

SaveChooser = Callable[[QWidget, Path], Path | None]
Confirm = Callable[[QWidget, str], bool]


def _t(text: str) -> str:
    return QCoreApplication.translate("ConvertDialog", text)


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------


def encoder_name(encoder: str) -> str:
    names = {
        "hevc_nvenc": _t("HEVC (NVIDIA GPU)"),
        "av1_nvenc": _t("AV1 (NVIDIA GPU)"),
        "libx265": _t("HEVC (CPU, x265)"),
        "libsvtav1": _t("AV1 (CPU, SVT-AV1)"),
        "libx264": _t("H.264 (CPU, x264)"),
    }
    return names.get(encoder, encoder)


def fmt_duration(seconds: float) -> str:
    """ "about 1 h 40 min", "about 25 min", "under a minute"."""
    if seconds < 60:
        return _t("under a minute")
    minutes = round(seconds / 60)
    if minutes < 60:
        return _t("about {m} min").format(m=minutes)
    h, m = divmod(minutes, 60)
    if m == 0:
        return _t("about {h} h").format(h=h)
    return _t("about {h} h {m} min").format(h=h, m=m)


def copy_target(key: str) -> Target:
    """The window's Target choice for a copy: 60 fps stays, anything else is 2×."""
    if key == "fps60":
        return Target(TargetKind.FPS, Fraction(60))
    return Target(TargetKind.X2)


def target_rate(target: Target, src_fps: Fraction | None) -> Fraction | None:
    if target.kind is TargetKind.FPS and target.fps is not None:
        return target.fps
    return src_fps * 2 if src_fps is not None else None


def estimate_s(size: RenderSize, duration_s: float, rate: Fraction | None) -> float | None:
    if not size.est_fps or rate is None or duration_s <= 0:
        return None
    return duration_s * float(rate) / size.est_fps


def size_label(
    size: RenderSize, *, original: bool, duration_s: float, rate: Fraction | None
) -> str:
    if original:
        text = _t("Original ({w} × {h})").format(w=size.width, h=size.height)
    else:
        text = _t("{h}p ({w} × {hh})").format(h=size.height, w=size.width, hh=size.height)
    est = estimate_s(size, duration_s, rate)
    if est is not None:
        text += " — " + fmt_duration(est)
    return text


def default_size_index(
    sizes: Sequence[RenderSize], duration_s: float, rate: Fraction | None
) -> int:
    if not sizes:
        return 0
    for i, s in enumerate(sizes):
        est = estimate_s(s, duration_s, rate)
        if est is None:
            return 0 if i == 0 else i
        if est <= DEFAULT_SIZE_FACTOR * duration_s:
            return i
    return len(sizes) - 1


def default_output(src: Path) -> Path:
    """``<stem>.smooth.mkv`` beside the source; ``(2)``, ``(3)``… when taken."""
    base = src.with_name(src.stem + COPY_SUFFIX)
    n = 2
    out = base
    while out.exists():
        out = src.with_name(f"{src.stem}.smooth ({n}).mkv")
        n += 1
    return out


def note_text(finding_id: str, text: str) -> str:
    """Plain words for the probe's notes (§12.7)."""
    if finding_id == "render.mkvmerge":
        return _t(
            "Using ffmpeg to copy the other streams (install mkvtoolnix for the preferred way)."
        )
    return text


def size_key(size: tuple[int, int]) -> str:
    """Combo data for a size (Qt does not keep tuples as item data)."""
    return f"{size[0]}x{size[1]}"


def parse_size_key(data: object) -> tuple[int, int] | None:
    if not isinstance(data, str) or "x" not in data:
        return None
    w, _, h = data.partition("x")
    return (int(w), int(h)) if w.isdigit() and h.isdigit() else None


def without_code(text: str) -> str:
    return _CODE_SUFFIX.sub("", text).strip()


def job_words(job: RenderJobState) -> str:
    """The phase in words for a progress row."""
    if job.state is OpState.QUEUED:
        return _t("Waiting")
    if job.state is OpState.CANCELLING:
        return _t("Cancelling…")
    if job.state is OpState.CANCELLED:
        return _t("Cancelled")
    if job.state is OpState.SUCCEEDED:
        return _t("Done")
    if job.state is OpState.FAILED:
        cause = without_code(render(job.error.cause)) if job.error is not None else ""
        return _t("Failed: {cause}").format(cause=cause) if cause else _t("Failed")
    phase = {
        RenderPhase.PROBE: _t("Getting ready"),
        RenderPhase.RENDER: _t("Converting"),
        RenderPhase.REMUX: _t("Finishing"),
        RenderPhase.CLEANUP: _t("Finishing"),
    }
    return phase.get(job.phase, _t("Converting")) if job.phase is not None else _t("Converting")


def job_percent(job: RenderJobState) -> int | None:
    if job.state is OpState.SUCCEEDED:
        return 100
    if job.done_frames is None or not job.total_frames:
        return None
    return max(0, min(100, job.done_frames * 100 // job.total_frames))


def job_progress_text(job: RenderJobState) -> str:
    pct = job_percent(job)
    words = job_words(job)
    if job.state is not OpState.RUNNING or pct is None:
        return words
    text = f"{words} · {pct} %"
    if job.eta_s is not None and job.phase is RenderPhase.RENDER:
        text += " · " + _t("{left} left").format(left=fmt_duration(job.eta_s))
    return text


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------


def _default_save_chooser(parent: QWidget, suggested: Path) -> Path | None:
    name, _ = QFileDialog.getSaveFileName(
        parent, _t("Save smooth copy as"), os.fspath(suggested), _t("Matroska video (*.mkv)")
    )
    if not name:
        return None
    path = Path(name)
    return path if path.suffix.lower() == ".mkv" else path.with_suffix(".mkv")


def _default_confirm(parent: QWidget, name: str) -> bool:
    box = a11y.message_box(
        parent,
        icon=QMessageBox.Icon.Question,
        title=_t("Replace file?"),
        text=_t("{name} already exists. Replace it?").format(name=name),
    )
    replace = box.addButton(_t("&Replace"), QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Cancel)
    box.exec()
    return box.clickedButton() is replace


class ConvertDialog(QDialog):
    """ "Save a smooth copy of <file>" (§12.7)."""

    enqueued = Signal(str)  # JobId

    def __init__(
        self,
        bridge: CoreBridge,
        source: Path,
        *,
        target_key: str = "x2",
        save_chooser: SaveChooser | None = None,
        confirm: Confirm | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("convertDialog")
        self.setWindowTitle(_t("Save smooth copy"))
        self.setModal(True)
        self.bridge = bridge
        self.source = Path(source)
        self.probe: RenderProbe | None = None
        self.output = default_output(self.source)
        self.overwrite = False
        self._save_chooser = save_chooser or _default_save_chooser
        self._confirm = confirm or _default_confirm
        unit = self.fontMetrics().height()

        lay = QVBoxLayout(self)
        lay.setSpacing(unit * 3 // 4)
        self.heading = a11y.heading(
            QLabel(_t("Save a smooth copy of {name}").format(name=self.source.name), self)
        )
        self.heading.setTextFormat(Qt.TextFormat.PlainText)
        self.heading.setWordWrap(True)
        lay.addWidget(self.heading)

        self.message = QLabel(_t("Reading the video…"), self)
        self.message.setObjectName("convertMessage")
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        lay.addWidget(self.message)

        grid = QGridLayout()
        grid.setHorizontalSpacing(unit)
        grid.setColumnStretch(1, 1)
        self.size_combo = QComboBox(self)
        self.size_combo.setObjectName("convertSize")
        lbl, _ = a11y.labelled(
            _t("&Size"),
            self.size_combo,
            description=_t("Smaller sizes convert faster; the times are estimates."),
        )
        grid.addWidget(lbl, 0, 0)
        grid.addWidget(self.size_combo, 0, 1)

        self.target_combo = QComboBox(self)
        self.target_combo.setObjectName("convertTarget")
        self.target_combo.addItem(_t("Double (2×)"), "x2")
        self.target_combo.addItem(_t("60 fps"), "fps60")
        self.target_combo.setCurrentIndex(1 if target_key == "fps60" else 0)
        lbl, _ = a11y.labelled(_t("&Target"), self.target_combo)
        grid.addWidget(lbl, 1, 0)
        grid.addWidget(self.target_combo, 1, 1)
        self.target_combo.currentIndexChanged.connect(lambda _i: self._fill_sizes())

        self.format_combo = QComboBox(self)
        self.format_combo.setObjectName("convertFormat")
        lbl, _ = a11y.labelled(_t("&Format"), self.format_combo)
        grid.addWidget(lbl, 2, 0)
        grid.addWidget(self.format_combo, 2, 1)

        out_row = QHBoxLayout()
        self.output_label = QLabel(self)
        self.output_label.setObjectName("convertOutput")
        self.output_label.setTextFormat(Qt.TextFormat.PlainText)
        self.output_label.setWordWrap(True)
        self.output_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.change_button = QPushButton(_t("C&hange…"), self)
        self.change_button.setObjectName("convertChange")
        self.change_button.setAutoDefault(False)
        self.change_button.setAccessibleName(_t("Change where to save"))
        self.change_button.clicked.connect(self.choose_output)
        out_row.addWidget(self.output_label, 1)
        out_row.addWidget(self.change_button)
        save_to = QLabel(_t("Save to"), self)
        save_to.setTextFormat(Qt.TextFormat.PlainText)
        self.output_label.setAccessibleName(_t("Save to"))
        grid.addWidget(save_to, 3, 0)
        grid.addLayout(out_row, 3, 1)
        self.form = QWidget(self)
        self.form.setLayout(grid)
        self.form.setEnabled(False)
        lay.addWidget(self.form)

        self.notes = QLabel(self)
        self.notes.setObjectName("convertNotes")
        self.notes.setTextFormat(Qt.TextFormat.PlainText)
        self.notes.setWordWrap(True)
        lay.addWidget(self.notes)

        self.buttons = QDialogButtonBox(self)
        self.save_button = self.buttons.addButton(
            _t("&Save copy"), QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.save_button.setObjectName("convertSave")
        self.save_button.setEnabled(False)
        self.cancel_button = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.save)
        self.buttons.rejected.connect(self.reject)
        lay.addWidget(self.buttons)
        self._show_output()
        self.resize(unit * 30, self.sizeHint().height())

        bridge.call(
            lambda core: core.render_probe(self.source, profile_id=SIMPLE_ID),
            owner=self,
            ok=self._probed,
            err=self._probe_failed,
        )

    # ---- probe
    def _probed(self, probe: RenderProbe) -> None:
        self.probe = probe
        if probe.refusal is not None:
            r = probe.refusal
            text = without_code(render(r.title))
            fix = render(r.fix)
            self.message.setText(f"{text}. {fix}" if fix else text)
            if r.commands:
                self.message.setText(self.message.text() + "\n" + "\n".join(r.commands))
            a11y.announce(self.message, self.message.text())
            return
        self.message.setText(
            _t("{w} × {h} · {fps} fps · {length}").format(
                w=probe.width,
                h=probe.height,
                fps=_rate_text(probe.fps),
                length=_length_text(probe.duration_s),
            )
        )
        self._fill_sizes()
        for enc in probe.encoders:
            self.format_combo.addItem(encoder_name(enc), enc)
        notes = [note_text(w.id, without_code(render(w.cause))) for w in probe.warnings]
        kept = _kept_text(probe)
        if kept:
            notes.insert(0, kept)
        self.notes.setText("\n".join(n for n in notes if n))
        self.form.setEnabled(True)
        self.save_button.setEnabled(bool(probe.encoders))
        self.size_combo.setFocus(Qt.FocusReason.OtherFocusReason)

    def _probe_failed(self, err: ButterEyeError) -> None:
        self.message.setText(without_code(render(err.cause)))
        a11y.announce(self.message, self.message.text(), assertive=True)

    def _fill_sizes(self) -> None:
        probe = self.probe
        if probe is None:
            return
        keep = self.size_combo.currentData()
        self.size_combo.blockSignals(True)
        self.size_combo.clear()
        rate = target_rate(self.target(), probe.fps)
        for i, s in enumerate(probe.sizes):
            label = size_label(s, original=i == 0, duration_s=probe.duration_s, rate=rate)
            self.size_combo.addItem(label, size_key((s.width, s.height)))
        idx = self.size_combo.findData(keep) if keep is not None else -1
        if idx < 0:
            idx = default_size_index(probe.sizes, probe.duration_s, rate)
        self.size_combo.setCurrentIndex(idx)
        self.size_combo.blockSignals(False)

    # ---- choices
    def target(self) -> Target:
        return copy_target(str(self.target_combo.currentData() or "x2"))

    def chosen_size(self) -> tuple[int, int] | None:
        size = parse_size_key(self.size_combo.currentData())
        if self.probe is None or size is None:
            return None
        return None if size == (self.probe.width, self.probe.height) else size

    def encoder(self) -> str:
        return str(self.format_combo.currentData() or "")

    def spec(self) -> RenderJobSpec:
        return RenderJobSpec(
            source=self.source,
            output=self.output,
            profile_id=SIMPLE_ID,
            encoder=self.encoder(),
            overwrite=self.overwrite,
            target=self.target(),
            size=self.chosen_size(),
        )

    def _show_output(self) -> None:
        self.output_label.setText(os.fspath(self.output))

    def choose_output(self) -> None:
        path = self._save_chooser(self, self.output)
        if path is None:
            return
        self.output = path
        self.overwrite = False
        self._show_output()

    # ---- save
    def save(self) -> None:
        if not self.save_button.isEnabled():
            return
        if self.output.exists() and not self.overwrite:
            if not self._confirm(self, self.output.name):
                return
            self.overwrite = True
        spec = self.spec()
        self.save_button.setEnabled(False)
        self.bridge.call(
            lambda core: core.render_enqueue(spec),
            owner=self,
            ok=self._enqueued,
            err=self._enqueue_failed,
        )

    def _enqueued(self, job: JobId) -> None:
        self.enqueued.emit(str(job))
        self.accept()

    def _enqueue_failed(self, err: ButterEyeError) -> None:
        self.message.setText(without_code(render(err.cause)))
        a11y.announce(self.message, self.message.text(), assertive=True)
        self.save_button.setEnabled(True)


def _rate_text(r: Fraction | None) -> str:
    if r is None:
        return "?"
    return f"{float(r):.3f}".rstrip("0").rstrip(".")


def _length_text(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return _t("{m} min").format(m=max(1, minutes))
    h, m = divmod(minutes, 60)
    return _t("{h} h {m} min").format(h=h, m=m)


def _kept_text(probe: RenderProbe) -> str:
    kinds = {s.kind for s in probe.streams}
    parts = []
    if "audio" in kinds:
        parts.append(_t("audio"))
    if "subtitle" in kinds:
        parts.append(_t("subtitles"))
    if probe.chapters:
        parts.append(_t("chapters"))
    if not parts:
        return ""
    return _t("Kept as they are: {things}.").format(things=", ".join(parts))


# ---------------------------------------------------------------------------
# Progress row
# ---------------------------------------------------------------------------


class JobRow(QFrame):
    """One conversion: file name, progress, phase in words, Cancel / Show file / Remove."""

    def __init__(self, job: RenderJobState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName(f"job.{job.id}")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        unit = self.fontMetrics().height()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(unit * 3 // 4, unit // 2, unit * 3 // 4, unit // 2)
        lay.setSpacing(unit // 3)
        self.title = QLabel(job.spec.output.name, self)
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        f = self.title.font()
        f.setBold(True)
        self.title.setFont(f)
        lay.addWidget(self.title)
        self.bar = QProgressBar(self)
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 100)
        lay.addWidget(self.bar)
        self.status = QLabel(self)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        buttons = QHBoxLayout()
        self.cancel_button = QPushButton(_t("Cancel"), self)
        self.cancel_button.setAutoDefault(False)
        self.show_button = QPushButton(_t("Show file"), self)
        self.show_button.setAutoDefault(False)
        self.remove_button = QPushButton(_t("Remove"), self)
        self.remove_button.setAutoDefault(False)
        for b in (self.cancel_button, self.show_button, self.remove_button):
            buttons.addWidget(b)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        self.job = job
        self._pct = 0
        self.update_job(job)

    def update_job(self, job: RenderJobState) -> None:
        self.job = job
        pct = job_percent(job)
        if pct is None and job.state in (OpState.QUEUED, OpState.RUNNING):
            self.bar.setRange(0, 0 if job.state is OpState.RUNNING else 100)
        else:
            self.bar.setRange(0, 100)
            # never backwards while the same job runs (§4.5)
            self._pct = max(self._pct, pct or 0) if job.state is OpState.RUNNING else pct or 0
            self.bar.setValue(self._pct)
        self.status.setText(job_progress_text(job))
        active = job.state in (OpState.QUEUED, OpState.RUNNING)
        finished = job.state in (OpState.SUCCEEDED, OpState.FAILED, OpState.CANCELLED)
        self.cancel_button.setVisible(active)
        self.show_button.setVisible(job.state is OpState.SUCCEEDED)
        self.remove_button.setVisible(finished)
        name = job.spec.output.name
        self.cancel_button.setAccessibleName(_t("Cancel saving {name}").format(name=name))
        self.show_button.setAccessibleName(_t("Show {name} in its folder").format(name=name))
        self.remove_button.setAccessibleName(_t("Remove {name} from the list").format(name=name))
        self.setAccessibleName(f"{name} — {self.status.text()}")


def open_folder(path: Path) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(path.parent)))


__all__ = [
    "ConvertDialog",
    "JobRow",
    "encoder_name",
    "fmt_duration",
    "copy_target",
    "target_rate",
    "estimate_s",
    "size_label",
    "default_size_index",
    "default_output",
    "job_words",
    "job_percent",
    "job_progress_text",
    "open_folder",
]
