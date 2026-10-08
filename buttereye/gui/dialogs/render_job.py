# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Add-render-job dialog (docs/design/GUI.md §4.5; SCOPE §7).

Source (file dialog or drop) → ``render_probe()`` (loading state) → what will
happen: HDR class and outcome (HDR10 only with x265/SVT-AV1; HLG/DV refused,
BE-4003), VFR → CFR notice, the kept-stream inventory, the remux tool (with the
mkvtoolnix command when ffmpeg has to remux), profile, encoder (probed encoders
only), output path (``<stem>.buttereye.mkv`` beside the source, MKV only) and
free space. A refusal or BE-4004 disables [Add to queue] with the reason shown
beside it. Overwriting needs a confirmation: the core answers BE-4008 first.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from buttereye.core.api import (
    ButterEyeError,
    CapState,
    ConfigLoad,
    ErrorCode,
    Feature,
    Finding,
    HdrClass,
    JobId,
    NotAvailable,
    Profile,
    Reason,
    RenderJobSpec,
    RenderProbe,
    Severity,
    builtin_profiles,
    render,
    unavailable_text,
)
from buttereye.gui.a11y import announce, heading, labelled, message_box, plain
from buttereye.gui.context import GuiContext
from buttereye.gui.dialogs.fit import FitDialog
from buttereye.gui.widgets.banner import Banner
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.drop_zone import DropZone
from buttereye.gui.widgets.op_row import human_bytes
from buttereye.gui.widgets.state_panel import StatePanel, split_headline
from buttereye.gui.widgets.status_badge import BadgeKind, StatusBadge

_log = logging.getLogger(__name__)

MKVTOOLNIX_INSTALL = "sudo dnf install mkvtoolnix"
HDR10_ENCODERS = ("libx265", "libsvtav1")
#: SCOPE §7.5 defaults, best first, after the configured per-vendor encoders.
SDR_DEFAULTS = ("hevc_nvenc", "hevc_vaapi", "libsvtav1")
#: Software encoders: at the doubled frame rate they keep most CPU cores busy
#: (x265 ~9 cores, SVT-AV1 ~8 on the dev box; NVENC under 1).
CPU_ENCODERS = frozenset({"libx265", "libsvtav1", "libx264"})

Confirm = Callable[[QWidget, str, str], bool]

CTX = "RenderJobDialog"


def _t(text: str) -> str:
    return QCoreApplication.translate(CTX, text)


def default_output(src: Path) -> Path:
    """``<stem>.buttereye.mkv`` beside the source."""
    return src.with_name(f"{src.stem}.buttereye.mkv")


def pick_encoder(probe: RenderProbe, preferred: tuple[str, ...]) -> str | None:
    """Default encoder among the probed ones (SCOPE §7.5)."""
    if not probe.encoders:
        return None
    if probe.hdr_class in (HdrClass.HDR10, HdrClass.HDR10PLUS, HdrClass.DV):
        order: tuple[str, ...] = HDR10_ENCODERS
    else:
        order = preferred + SDR_DEFAULTS
    for e in order:
        if e in probe.encoders:
            return e
    return probe.encoders[0]


def confirm_box(parent: QWidget, title: str, text: str) -> bool:
    box = message_box(parent)
    box.setObjectName("confirmOverwrite")
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle(title)
    box.setText(text)
    yes = box.addButton(_t("&Replace"), QMessageBox.ButtonRole.DestructiveRole)
    yes.setObjectName("replace")
    no = box.addButton(_t("&Keep the file"), QMessageBox.ButtonRole.RejectRole)
    no.setObjectName("keep")
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box.exec()
    return box.clickedButton() is yes


class RenderJobDialog(FitDialog):
    """``RenderJobDialog(ctx, parent, source=None)``; emits ``enqueued(job_id)`` then accepts."""

    enqueued = Signal(str)

    def __init__(
        self, ctx: GuiContext, parent: QWidget | None = None, *, source: Path | None = None
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("renderJobDialog")
        self.setWindowTitle(self.tr("Add a render"))
        self.setModal(True)
        self.probe: RenderProbe | None = None
        self.source: Path | None = None
        self.job_id: JobId | None = None
        self.confirm: Confirm = lambda w, title, text: confirm_box(w, title, text)
        self.choose_output = self._default_choose_output
        self._profiles: tuple[Profile, ...] = ()
        self._preferred: tuple[str, ...] = ()
        self._block_reason = ""
        self._output_is_default = True

        lay = QVBoxLayout(self)
        self.drop = DropZone(
            self.tr("Drop the video to render here, or press Enter to choose it"), self
        )
        self.drop.setObjectName("renderSource")
        self.drop.fileChosen.connect(self.set_source)
        self.drop.rejected.connect(self._on_rejected)
        lay.addWidget(self.drop)
        self.source_label = QLabel(self)
        self.source_label.setTextFormat(Qt.TextFormat.PlainText)
        self.source_label.setWordWrap(True)
        self.source_label.setVisible(False)
        lay.addWidget(self.source_label)

        self.panel = StatePanel(self)
        lay.addWidget(self.panel, 1)
        self._build_details(self.panel.content)

        row = QHBoxLayout()
        self.reason_label = QLabel(self)
        self.reason_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reason_label.setWordWrap(True)
        self.add_button = QPushButton(self.tr("&Add to queue"), self)
        self.add_button.setObjectName("addToQueue")
        self.add_button.setAccessibleName(self.tr("Add to queue"))
        self.add_button.setDefault(True)
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.add)
        self.cancel_button = QPushButton(self.tr("Cancel"), self)
        self.cancel_button.setObjectName("cancelAdd")
        self.cancel_button.setAccessibleName(self.tr("Cancel adding a render"))
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.reason_label, 1)
        row.addWidget(self.add_button)
        row.addWidget(self.cancel_button)
        lay.addLayout(row)

        self.panel.show_empty(self.tr("Choose a video to see what will be rendered."))
        self._load_profiles()
        if source is not None:
            self.set_source(source)

    # ------------------------------------------------------------------ building
    def _build_details(self, content: QWidget) -> None:
        form = QFormLayout(content)
        title = heading(QLabel(self.tr("What will be rendered"), content))
        title.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow(title)

        self.hdr_badge = StatusBadge("info", "", content)
        form.addRow(self.hdr_badge)
        self.refusal_box = QVBoxLayout()
        form.addRow(self.refusal_box)
        self.refusal_banner: Banner | None = None

        self.vfr_label = QLabel(self.tr("Will be converted to constant frame rate."), content)
        self.vfr_label.setWordWrap(True)
        form.addRow(self.vfr_label)

        self.streams_label = QLabel(content)
        self.streams_label.setTextFormat(Qt.TextFormat.PlainText)
        self.streams_label.setWordWrap(True)
        form.addRow(self.streams_label)

        self.remux_label = QLabel(content)
        self.remux_label.setTextFormat(Qt.TextFormat.PlainText)
        self.remux_label.setWordWrap(True)
        form.addRow(self.remux_label)
        self.remux_command = CopyField(
            MKVTOOLNIX_INSTALL,
            accessible_name=self.tr("Command to install mkvtoolnix"),
            parent=content,
        )
        form.addRow(self.remux_command)

        self.warnings_label = QLabel(content)
        self.warnings_label.setTextFormat(Qt.TextFormat.PlainText)
        self.warnings_label.setWordWrap(True)
        form.addRow(self.warnings_label)

        self.profile_combo = QComboBox(content)
        p_label, _ = labelled(self.tr("&Profile"), self.profile_combo)
        form.addRow(p_label, self.profile_combo)
        self.encoder_combo = QComboBox(content)
        e_label, _ = labelled(
            self.tr("&Encoder"),
            self.encoder_combo,
            description=self.tr("Only encoders your ffmpeg provides are listed."),
        )
        form.addRow(e_label, self.encoder_combo)
        self.encoder_hint = QLabel(content)
        self.encoder_hint.setObjectName("encoderHint")
        self.encoder_hint.setTextFormat(Qt.TextFormat.PlainText)
        self.encoder_hint.setWordWrap(True)
        self.encoder_hint.setVisible(False)
        form.addRow(self.encoder_hint)

        out_row = QWidget(content)
        orl = QHBoxLayout(out_row)
        orl.setContentsMargins(0, 0, 0, 0)
        self.output_edit = QLineEdit(out_row)
        self.output_edit.textChanged.connect(self._update_add)
        self.output_edit.textEdited.connect(self._on_output_edited)
        o_label, _ = labelled(
            self.tr("&Output file"),
            self.output_edit,
            description=self.tr("Matroska (.mkv) only."),
        )
        self.output_choose = QPushButton(self.tr("C&hoose…"), out_row)
        self.output_choose.setAccessibleName(self.tr("Choose output file"))
        self.output_choose.setAutoDefault(False)
        self.output_choose.clicked.connect(self._on_choose_output)
        orl.addWidget(self.output_edit, 1)
        orl.addWidget(self.output_choose)
        form.addRow(o_label, out_row)

        self.space_label = QLabel(content)
        self.space_label.setTextFormat(Qt.TextFormat.PlainText)
        self.space_label.setWordWrap(True)
        form.addRow(self.space_label)

        self.encoder_combo.currentIndexChanged.connect(self._update_add)
        self.encoder_combo.currentIndexChanged.connect(lambda _i: self._update_encoder_hint())
        self.profile_combo.currentIndexChanged.connect(self._update_add)

    # ------------------------------------------------------------------ profiles
    def _load_profiles(self) -> None:
        caps = self.ctx.bridge.capabilities
        if (
            caps is not None
            and caps.states.get(Feature.CONFIG) is not None
            and caps.ok(Feature.CONFIG)
        ):
            self.ctx.bridge.call(
                lambda core: core.load_config(),
                owner=self,
                ok=self._on_config,
                err=self._on_config_error,
            )
            return
        self._fallback_profiles()

    def _on_config(self, load: ConfigLoad) -> None:
        self._preferred = tuple(load.config.render.encoder_by_vendor.values())
        self.set_profiles(load.config.profiles)

    def _on_config_error(self, err: ButterEyeError) -> None:
        _log.info("render dialog: config not loaded (%s)", err.code.code)
        self._fallback_profiles()

    def _fallback_profiles(self) -> None:
        try:
            self.set_profiles(builtin_profiles())
        except NotAvailable:
            self.set_profiles(())

    def set_profiles(self, profiles: tuple[Profile, ...]) -> None:
        self._profiles = profiles
        current = self.profile_combo.currentData()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p in profiles:
            text = p.name if not p.builtin else self.tr("{name} (built-in)").format(name=p.name)
            self.profile_combo.addItem(text, p.id)
        ids = [p.id for p in profiles]
        want = current if current in ids else ("balanced" if "balanced" in ids else None)
        if want is not None:
            self.profile_combo.setCurrentIndex(ids.index(want))
        self.profile_combo.blockSignals(False)
        if self.probe is not None:
            self._fill_encoders(self.probe)
        self._update_add()

    # ------------------------------------------------------------------ source / probe
    def set_source(self, path: Path) -> None:
        self.source = path
        self.probe = None
        self.source_label.setText(self.tr("Source: {path}").format(path=str(path)))
        self.source_label.setVisible(True)
        self.add_button.setEnabled(False)
        self.panel.show_loading(self.tr("Checking the video…"))
        self.ctx.bridge.call(
            lambda core: core.render_probe(path),
            owner=self,
            ok=lambda probe: self._on_probe(path, probe),
            err=self._on_probe_error,
        )

    def _on_rejected(self, text: str) -> None:
        self.reason_label.setText(text)
        announce(self.reason_label, text, assertive=True)

    def _on_probe_error(self, err: ButterEyeError) -> None:
        self.panel.show_error(err)
        self._set_block(render(err.cause))

    def _on_probe(self, path: Path, probe: RenderProbe) -> None:
        if path != self.source:
            return  # a newer source was chosen meanwhile
        self.probe = probe
        self._show_hdr(probe)
        self._show_refusal(probe.refusal)
        self.vfr_label.setVisible(probe.vfr)
        self.streams_label.setText(self.inventory_text(probe))
        if probe.remux_tool == "mkvmerge":
            self.remux_label.setText(self.tr("Remux: mkvmerge"))
            self.remux_command.setVisible(False)
        else:
            self.remux_label.setText(self.tr("Remux: mkvmerge not installed — using ffmpeg"))
            cmd = next(
                (c for f in probe.warnings if f.id == "render.mkvmerge" for c in f.commands),
                MKVTOOLNIX_INSTALL,
            )
            self.remux_command.set_text(cmd)
            self.remux_command.setVisible(True)
        notes = [
            f"{render(f.title)}: {render(f.cause)}" if f.cause.key else render(f.title)
            for f in probe.warnings
            if f.id not in ("render.mkvmerge", "render.vfr")
        ]
        self.warnings_label.setText("\n".join(notes))
        self.warnings_label.setVisible(bool(notes))
        self._fill_encoders(probe)
        if not self.output_edit.text().strip() or self._output_is_default:
            self.output_edit.setText(str(default_output(probe.source)))
            self._output_is_default = True
        self.space_label.setText(
            self.tr("Free space at the destination: {free}").format(
                free=human_bytes(probe.free_bytes)
            )
        )
        self.panel.show_content()
        self._update_add()
        announce(self, self.tr("Video checked."))

    def _on_output_edited(self, _text: str) -> None:
        self._output_is_default = False

    def inventory_text(self, probe: RenderProbe) -> str:
        kinds = [s.kind for s in probe.streams]
        parts: list[str] = []
        a, s, t = kinds.count("audio"), kinds.count("subtitle"), kinds.count("attachment")
        if a:
            parts.append(self.tr("%n audio track(s)", None, a))
        if s:
            parts.append(self.tr("%n subtitle(s)", None, s))
        if probe.chapters:
            parts.append(self.tr("chapters"))
        if t:
            parts.append(self.tr("%n attachment(s)", None, t))
        if not parts:
            return self.tr("Kept: video only (no other streams)")
        return self.tr("Kept: {streams}").format(streams=", ".join(parts))

    def _show_hdr(self, probe: RenderProbe) -> None:
        texts: dict[HdrClass, tuple[BadgeKind, str]] = {
            HdrClass.SDR: ("ok", self.tr("SDR video")),
            HdrClass.HDR10: (
                "ok",
                self.tr("HDR10 video — rendered as HDR10 with x265 or SVT-AV1"),
            ),
            HdrClass.HDR10PLUS: (
                "degraded",
                self.tr(
                    "HDR10+ video — rendered as plain HDR10; the HDR10+ dynamic metadata is dropped"
                ),
            ),
            HdrClass.HLG: ("blocking", self.tr("HLG video — can't be rendered in this version")),
            HdrClass.DV: ("degraded", self.tr("Dolby Vision video")),
        }
        kind, text = texts[probe.hdr_class]
        if probe.hdr_class is HdrClass.DV:
            if probe.refusal is not None:
                kind, text = "blocking", self.tr("Dolby Vision video — can't be rendered")
            else:
                text = self.tr(
                    "Dolby Vision video — rendered as plain HDR10 from its base layer; the "
                    "Dolby Vision metadata is dropped"
                )
        self.hdr_badge.set_state(kind, text)

    def _show_refusal(self, refusal: Finding | None) -> None:
        if self.refusal_banner is not None:
            self.refusal_banner.hide()
            self.refusal_banner.deleteLater()
            self.refusal_banner = None
        if refusal is None:
            return
        body = render(refusal.cause)
        if refusal.fix.key:
            body = f"{body} {render(refusal.fix)}".strip()
        kind: BadgeKind = "blocking" if refusal.severity is Severity.BLOCKING else "degraded"
        self.refusal_banner = Banner(
            kind,
            render(refusal.title),
            body,
            refusal.code.code if refusal.code is not None else None,
            refusal.commands,
            parent=self.panel.content,
        )
        self.refusal_box.addWidget(self.refusal_banner)

    def _fill_encoders(self, probe: RenderProbe) -> None:
        current = self.encoder_combo.currentText()
        self.encoder_combo.blockSignals(True)
        self.encoder_combo.clear()
        for i, e in enumerate(probe.encoders):
            self.encoder_combo.addItem(e, e)
            if e in CPU_ENCODERS:
                self.encoder_combo.setItemData(
                    i, self.tr("Runs on the CPU; uses it heavily"), Qt.ItemDataRole.ToolTipRole
                )
        want = current if current in probe.encoders else pick_encoder(probe, self._preferred)
        if want is not None:
            self.encoder_combo.setCurrentIndex(probe.encoders.index(want))
        self.encoder_combo.blockSignals(False)
        self._update_encoder_hint()

    def _update_encoder_hint(self) -> None:
        """A software encoder at the doubled rate keeps most CPU cores busy: say so."""
        enc = self.encoder_combo.currentData()
        if enc not in CPU_ENCODERS:
            self.encoder_hint.setVisible(False)
            self.encoder_hint.clear()
            return
        text = self.tr(
            "This encoder runs on the CPU and keeps most of its cores busy for the "
            "whole conversion; a GPU encoder, when listed, is much lighter."
        )
        probe = self.probe
        if probe is not None and probe.hdr_class in (
            HdrClass.HDR10, HdrClass.HDR10PLUS, HdrClass.DV
        ):  # fmt: skip
            text = self.tr(
                "HDR10 copies need a CPU encoder (x265 or SVT-AV1), which keeps most "
                "CPU cores busy for the whole conversion."
            )
        self.encoder_hint.setText(text)
        self.encoder_hint.setVisible(True)

    # ------------------------------------------------------------------ validation
    def hdr10_state(self) -> CapState | None:
        caps = self.ctx.bridge.capabilities
        if caps is None:
            return None
        st = caps.states.get(Feature.RENDER_HDR10)
        return st if st is not None and not st.available else None

    def block_reason(self) -> str:
        """Why [Add to queue] is disabled ("" = enabled)."""
        probe = self.probe
        if probe is None:
            return self.tr("Choose a video first.")
        if probe.refusal is not None:
            code = probe.refusal.code.code if probe.refusal.code is not None else ""
            text = render(probe.refusal.title)
            return f"{text} ({code})" if code else text
        if probe.hdr_class in (HdrClass.HDR10, HdrClass.HDR10PLUS, HdrClass.DV):
            st = self.hdr10_state()
            if st is not None and st.reason is not Reason.NOT_IMPLEMENTED:
                head, body = split_headline(render(unavailable_text(Feature.RENDER_HDR10, st)))
                return " ".join(t for t in (head, body) if t)
            if not any(e in probe.encoders for e in HDR10_ENCODERS):
                return self.tr("HDR10 render needs the x265 or SVT-AV1 encoder.")
        if not probe.encoders:
            return self.tr("Your ffmpeg lists no video encoder that can be used.")
        if self.encoder_combo.currentData() is None:
            return self.tr("Choose an encoder.")
        if self.profile_combo.currentData() is None:
            return self.tr("No profiles are available.")
        out = self.output_path()
        if out is None:
            return self.tr("Enter the output file.")
        if out.suffix.lower() != ".mkv":
            return self.tr("The output must be a Matroska (.mkv) file.")
        if self.source is not None and out == self.source:
            return self.tr("The output can't replace the source video.")
        return ""

    def output_path(self) -> Path | None:
        text = self.output_edit.text().strip()
        if not text:
            return None
        return Path(text).expanduser()

    def _set_block(self, text: str) -> None:
        self._block_reason = text
        self.reason_label.setText(text)
        self.add_button.setEnabled(not text)
        self.add_button.setAccessibleDescription(text)

    def _update_add(self, *_args: object) -> None:
        if self.probe is None:
            self.add_button.setEnabled(False)
            return
        self._set_block(self.block_reason())

    def _default_choose_output(self) -> Path | None:
        start = self.output_edit.text().strip()
        path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save the render as"), start, self.tr("Matroska video (*.mkv)")
        )
        if not path:
            return None
        p = Path(path)
        return p if p.suffix.lower() == ".mkv" else p.with_suffix(".mkv")

    def _on_choose_output(self) -> None:
        chosen = self.choose_output()
        if chosen is not None:
            self._output_is_default = False
            self.output_edit.setText(str(chosen))

    # ------------------------------------------------------------------ add
    def spec(self, *, overwrite: bool = False) -> RenderJobSpec | None:
        if self.block_reason() or self.source is None:
            return None
        out = self.output_path()
        assert out is not None
        return RenderJobSpec(
            source=self.source,
            output=out,
            profile_id=str(self.profile_combo.currentData()),
            encoder=str(self.encoder_combo.currentData()),
            overwrite=overwrite,
        )

    def add(self, *, overwrite: bool = False) -> None:
        spec = self.spec(overwrite=overwrite)
        if spec is None:
            self._update_add()
            return
        self.add_button.setEnabled(False)
        self.ctx.bridge.call(
            lambda core: core.render_enqueue(spec),
            owner=self,
            ok=self._on_enqueued,
            err=lambda e: self._on_enqueue_error(spec, e),
        )

    def _on_enqueued(self, jid: JobId) -> None:
        self.job_id = jid
        self.enqueued.emit(str(jid))
        self.accept()

    def _on_enqueue_error(self, spec: RenderJobSpec, err: ButterEyeError) -> None:
        if err.code is ErrorCode.OUTPUT_EXISTS and not spec.overwrite:
            if self.confirm(
                self,
                self.tr("Replace the file?"),
                self.tr("{path} already exists. Replace it with the new render?").format(
                    path=str(spec.output)
                ),
            ):
                self.add(overwrite=True)
                return
            self._set_block(self.tr("The output file exists. Choose another name."))
            return
        text = f"{render(err.cause)} ({err.code.code})"
        self._set_block(text)
        # Leave a retry possible once the user changed something.
        self.add_button.setEnabled(
            err.code not in (ErrorCode.NO_SPACE, ErrorCode.HDR_CLASS_REFUSED)
        )
        announce(self.reason_label, text, assertive=True)

    def accessible_reason(self) -> str:
        return plain(self.reason_label.text())


__all__ = ["RenderJobDialog", "confirm_box", "default_output", "pick_encoder"]
