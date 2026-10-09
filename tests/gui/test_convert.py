# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Save a smooth copy in the simple window (docs/design/GUI.md §12.7) over FakeCore."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QWidget

from buttereye.core.api import (
    ButterEyeError,
    ErrorCode,
    JobId,
    Msg,
    OpState,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
    RenderSize,
    SessionId,
    Target,
    TargetKind,
)
from buttereye.gui import convert_dialog as cd
from tests.gui import test_simple_window as _tsw
from tests.gui.test_simple_window import Make, calls

make_simple = _tsw.make_simple  # the window fixture, shared

# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def test_durations_and_names() -> None:
    assert cd.fmt_duration(20) == "under a minute"
    assert cd.fmt_duration(25 * 60) == "about 25 min"
    assert cd.fmt_duration(100 * 60) == "about 1 h 40 min"
    assert cd.fmt_duration(120 * 60) == "about 2 h"
    assert cd.encoder_name("hevc_nvenc") == "HEVC (NVIDIA GPU)"
    assert cd.encoder_name("libsvtav1") == "AV1 (CPU, SVT-AV1)"
    assert cd.encoder_name("prores_ks") == "prores_ks"


def test_targets_for_a_copy() -> None:
    assert cd.copy_target("display") == Target(TargetKind.X2)  # a file has no display
    assert cd.copy_target("x2") == Target(TargetKind.X2)
    assert cd.copy_target("fps60") == Target(TargetKind.FPS, Fraction(60))
    src = Fraction(24000, 1001)
    assert cd.target_rate(Target(TargetKind.X2), src) == src * 2
    assert cd.target_rate(Target(TargetKind.FPS, Fraction(60)), src) == 60


KLAUS = (
    RenderSize(3840, 2160, 12.2),
    RenderSize(2560, 1440, 22.0),
    RenderSize(1920, 1080, 39.4),
    RenderSize(1280, 720, 70.0),
)


def test_size_labels_and_the_default_size() -> None:
    rate = Fraction(48000, 1001)
    film = 97 * 60
    first = cd.size_label(KLAUS[0], original=True, duration_s=film, rate=rate)
    assert first.startswith("Original (3840 × 2160) — about 6 h")
    third = cd.size_label(KLAUS[2], original=False, duration_s=film, rate=rate)
    assert third == "1080p (1920 × 1080) — about 1 h 58 min"
    # 4K would take > 2× the film's length, 1440p just over, 1080p fits
    assert cd.default_size_index(KLAUS, film, rate) == 2
    short = (RenderSize(1920, 1080, 45.0), RenderSize(1280, 720, 90.0))
    assert cd.default_size_index(short, 600, rate) == 0
    slow = (RenderSize(3840, 2160, 1.0), RenderSize(1280, 720, 2.0))
    assert cd.default_size_index(slow, 600, rate) == 1  # nothing fits: the smallest
    unknown = (RenderSize(1920, 1080, None), RenderSize(1280, 720, None))
    assert cd.default_size_index(unknown, 600, rate) == 0
    no_est = cd.size_label(unknown[1], original=False, duration_s=600, rate=rate)
    assert no_est == "720p (1280 × 720)"


def test_default_output_never_overwrites(tmp_path: Path) -> None:
    src = tmp_path / "Klaus.mkv"
    assert cd.default_output(src) == tmp_path / "Klaus.smooth.mkv"
    (tmp_path / "Klaus.smooth.mkv").write_bytes(b"")
    assert cd.default_output(src) == tmp_path / "Klaus.smooth (2).mkv"


def _job(state: OpState, **kw: Any) -> RenderJobState:
    spec = RenderJobSpec(Path("/v/a.mkv"), Path("/v/a.smooth.mkv"), "simple", "hevc_nvenc")
    base: dict[str, Any] = {
        "phase": None, "done_frames": None, "total_frames": None, "fps": None,
        "eta_s": None, "est_bytes": None, "error": None,
    }  # fmt: skip
    base.update(kw)
    return RenderJobState(JobId("j1"), spec, state, **base)


def test_job_words_without_codes() -> None:
    assert cd.job_progress_text(_job(OpState.QUEUED)) == "Waiting"
    running = _job(
        OpState.RUNNING, phase=RenderPhase.RENDER, done_frames=120, total_frames=1000,
        eta_s=80 * 60,
    )  # fmt: skip
    assert cd.job_progress_text(running) == "Converting · 12 % · about 1 h 20 min left"
    remux = _job(OpState.RUNNING, phase=RenderPhase.REMUX, done_frames=1000, total_frames=1000)
    assert cd.job_progress_text(remux) == "Finishing · 100 %"
    err = ButterEyeError(ErrorCode.ENCODER_FAILED, Msg("The encoder stopped (BE-4006)"))
    assert cd.job_progress_text(_job(OpState.FAILED, error=err)) == ("Failed: The encoder stopped")
    assert cd.job_percent(_job(OpState.SUCCEEDED)) == 100


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------


def test_convert_entry_points_follow_the_capability(make_simple: Make, qtbot: Any) -> None:
    devbox = make_simple("devbox_now", ready=False)
    qtbot.waitUntil(lambda: devbox.bridge.is_ready, timeout=5000)
    assert not devbox.convert_drop.isVisibleTo(devbox)
    assert not devbox.convert_button.isVisibleTo(devbox)
    assert not devbox.act_convert.isEnabled()
    win = make_simple("live_active")
    assert win.convert_drop.isVisibleTo(win)
    assert win.convert_button.isVisibleTo(win) and win.act_convert.isEnabled()
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows)
    row = win.rows[sid]
    assert row.copy_button.isVisibleTo(row)
    assert row.copy_button.accessibleName() == "Save a smooth copy of film.mkv"


def _chooser(path: Path) -> Any:
    return lambda _parent: path


def test_two_drop_zones_play_and_convert(make_simple: Make, qtbot: Any, tmp_path: Path) -> None:
    from PySide6.QtCore import QUrl
    from PySide6.QtWidgets import QScrollArea

    src = tmp_path / "Klaus.mkv"
    src.write_bytes(b"x")
    win = make_simple("all_ready")
    assert not win.findChildren(QScrollArea)  # the window never scrolls (GUI.md §12.5)
    assert win.drop.objectName() == "dropZone"
    assert win.convert_drop.objectName() == "convertZone"
    played: list[Path] = []
    win.play_file = played.append  # type: ignore[method-assign]
    win.drop.fileChosen.disconnect()
    win.drop.fileChosen.connect(win.play_file)
    assert win.drop.offer_urls([QUrl.fromLocalFile(str(src))])
    assert played == [src] and win.convert_dialog is None
    assert win.convert_drop.offer_urls([QUrl.fromLocalFile(str(src))])
    dlg = win.convert_dialog
    assert dlg is not None
    qtbot.waitUntil(lambda: dlg.save_button.isEnabled(), timeout=5000)
    assert calls(win, "render_probe")[-1].args[0] == src
    dlg.reject()


def test_dialog_probes_and_enqueues_the_choices(
    make_simple: Make, qtbot: Any, tmp_path: Path
) -> None:
    src = tmp_path / "Klaus.mkv"
    src.write_bytes(b"x")
    win = make_simple("all_ready", chooser=_chooser(src))
    win.convert_button.click()
    dlg = win.convert_dialog
    assert dlg is not None
    qtbot.waitUntil(lambda: dlg.save_button.isEnabled(), timeout=5000)
    assert calls(win, "render_probe")[-1].kwargs == {"profile_id": "simple"}
    assert dlg.size_combo.count() == 4
    assert dlg.size_combo.itemText(0).startswith("Original (3840 × 2160)")
    assert dlg.format_combo.itemText(0) == "HEVC (NVIDIA GPU)"
    assert dlg.output == tmp_path / "Klaus.smooth.mkv"
    assert "Kept as they are: audio, subtitles, chapters." in dlg.notes.text()
    assert "install mkvtoolnix for the preferred way" in dlg.notes.text()
    assert "remux" not in dlg.notes.text()
    dlg.size_combo.setCurrentIndex(dlg.size_combo.findData("1280x720"))
    dlg.target_combo.setCurrentIndex(dlg.target_combo.findData("fps60"))
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData("libsvtav1"))
    dlg.save_button.click()
    qtbot.waitUntil(lambda: bool(calls(win, "render_enqueue")), timeout=5000)
    spec = calls(win, "render_enqueue")[-1].args[0]
    assert spec == RenderJobSpec(
        source=src,
        output=tmp_path / "Klaus.smooth.mkv",
        profile_id="simple",
        encoder="libsvtav1",
        overwrite=False,
        target=Target(TargetKind.FPS, Fraction(60)),
        size=(1280, 720),
    )
    # the job shows up as a row and runs to Done
    qtbot.waitUntil(lambda: bool(win.job_rows), timeout=5000)
    row = next(iter(win.job_rows.values()))
    qtbot.waitUntil(lambda: row.job.state is OpState.SUCCEEDED, timeout=10000)
    assert row.status.text() == "Done" and row.bar.value() == 100
    assert row.show_button.isVisibleTo(row) and not row.cancel_button.isVisibleTo(row)
    opened: list[Path] = []
    win.open_folder = opened.append
    row.show_button.click()
    assert opened == [tmp_path / "Klaus.smooth.mkv"]
    row.remove_button.click()
    qtbot.waitUntil(lambda: not win.job_rows, timeout=5000)
    assert not win.copies.isVisible()


def test_original_size_sends_no_size(make_simple: Make, qtbot: Any, tmp_path: Path) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    win = make_simple("all_ready")
    dlg = win.convert_file(src)
    qtbot.waitUntil(lambda: dlg.save_button.isEnabled(), timeout=5000)
    dlg.size_combo.setCurrentIndex(0)
    assert dlg.spec().size is None and dlg.spec().target == Target(TargetKind.X2)


def test_existing_output_asks_before_replacing(
    make_simple: Make, qtbot: Any, tmp_path: Path
) -> None:
    src = tmp_path / "a.mkv"
    src.write_bytes(b"x")
    other = tmp_path / "chosen.mkv"
    other.write_bytes(b"old")
    asked: list[str] = []

    def no(_parent: QWidget, name: str) -> bool:
        asked.append(name)
        return False

    win = make_simple("all_ready", save_chooser=lambda _p, _s: other, confirm_replace=no)
    dlg = win.convert_file(src)
    qtbot.waitUntil(lambda: dlg.save_button.isEnabled(), timeout=5000)
    dlg.change_button.click()
    assert dlg.output == other and dlg.output_label.text() == str(other)
    dlg.save_button.click()
    assert asked == ["chosen.mkv"] and not calls(win, "render_enqueue")
    dlg._confirm = lambda _p, _n: True
    dlg.save_button.click()
    qtbot.waitUntil(lambda: bool(calls(win, "render_enqueue")), timeout=5000)
    assert calls(win, "render_enqueue")[-1].args[0].overwrite is True


def test_refusal_disables_save_and_jobs_are_seeded(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("render_refusals")
    # jobs from the core's queue appear at start (render_jobs)
    qtbot.waitUntil(lambda: bool(win.job_rows), timeout=5000)
    assert win.copies.isVisible()
    for row in win.job_rows.values():
        assert "BE-" not in row.status.text()
    dlg = win.convert_file(Path("/videos/hlg.mkv"))
    qtbot.waitUntil(lambda: dlg.probe is not None, timeout=5000)
    assert dlg.probe is not None and dlg.probe.refusal is not None
    assert not dlg.save_button.isEnabled() and not dlg.form.isEnabled()
    assert dlg.message.text() and "BE-" not in dlg.message.text()
    dlg.reject()


def test_save_copy_from_a_row_uses_its_file(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("live_active")
    sid = SessionId("s1")
    qtbot.waitUntil(lambda: sid in win.rows)
    win.rows[sid].copy_button.click()
    dlg = win.convert_dialog
    assert dlg is not None
    snap = win.live_sessions[0]
    assert snap.source is not None and dlg.source == Path(snap.source.path)
    dlg.reject()


def test_cancel_a_running_copy(make_simple: Make, qtbot: Any) -> None:
    win = make_simple("all_ready")
    spec = RenderJobSpec(Path("/videos/a.mkv"), Path("/videos/a.smooth.mkv"), "simple", "libx264")
    win.bridge.call(lambda core: core.render_enqueue(spec), owner=win, ok=lambda _j: None)
    qtbot.waitUntil(lambda: bool(win.job_rows), timeout=5000)
    row = next(iter(win.job_rows.values()))
    assert row.cancel_button.accessibleName() == "Cancel saving a.smooth.mkv"
    row.cancel_button.click()
    qtbot.waitUntil(lambda: bool(calls(win, "render_cancel")), timeout=5000)
    qtbot.waitUntil(lambda: row.job.state in (OpState.CANCELLED, OpState.SUCCEEDED), timeout=10000)
