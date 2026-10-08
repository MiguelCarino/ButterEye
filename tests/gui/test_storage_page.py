# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Storage page (docs/design/GUI.md §4.8, §6 ``test_storage_page``; U6).

RPM-owned rows are never cleanable; the clean confirmation lists the exact
paths and sizes; a hash mismatch is reported with BE-6001 and installs nothing.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from buttereye.core.api import (
    CleanTarget,
    ErrorCode,
    ModelKind,
    StorageKind,
)
from buttereye.core.testing import scenarios as sc
from buttereye.gui.dialogs.consent import ConsentDialog
from buttereye.gui.pages.storage import CleanConfirmDialog, StoragePage, cleanable
from tests.gui.helpers import click_modal, fake_core


def _page(make_window: Any, qtbot: Any, scenario: Any = "devbox_now") -> tuple[Any, StoragePage]:
    win = make_window(scenario)
    win.go("storage")
    page = win.page("storage")
    assert isinstance(page, StoragePage)
    qtbot.waitUntil(
        lambda: page.panel.state == "content" and page.models_panel.state == "content",
        timeout=5000,
    )
    return win, page


def _on_modal(fn: Callable[[Any], None]) -> list[str]:
    """Run ``fn(dialog)`` on the next modal dialog (then it must close it)."""
    seen: list[str] = []

    def attempt(tries: int = 0) -> None:
        modal = QApplication.activeModalWidget()
        if modal is None:
            if tries < 100:
                QTimer.singleShot(20, lambda: attempt(tries + 1))
            return
        seen.append(modal.objectName())
        fn(modal)

    QTimer.singleShot(0, attempt)
    return seen


def test_rpm_rows_are_not_cleanable(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    m = page.loc_model
    rpm_rows = [r for r in range(m.rowCount()) if "dnf" in m.item(r, 4).text()]
    assert len(rpm_rows) == 2
    for r in rpm_rows:
        assert not page.is_checkable(r)
        assert m.item(r, 4).text() == "No — installed by dnf"
    assert not page.set_checked(StorageKind.RPM, True)
    for e in page.entries:
        assert cleanable(e) == (e.clean_target is not None and not e.rpm_owned)
    # orientation command, never run
    page.loc_table.setCurrentIndex(m.index(rpm_rows[0], 0))
    assert page.rpm_field.isVisibleTo(page)
    assert page.rpm_field.text() == "sudo dnf remove buttereye-vs-rife-ncnn buttereye-vs-mvtools"
    page.loc_table.setCurrentIndex(m.index(0, 0))
    assert not page.rpm_field.isVisibleTo(page)


def test_sizes_are_true(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    m = page.loc_model
    sizes = {m.item(r, 0).text(): m.item(r, 3).text() for r in range(m.rowCount())}
    assert sizes["TensorRT engines"] == "0 bytes"
    assert sizes["Logs"] == "4.1 kB"
    assert sizes["Live files"] == "Not measurable"


def test_clean_button_needs_a_tick(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    assert not page.clean_button.isEnabled()
    assert page.clean_note.isVisibleTo(page)
    assert page.set_checked(StorageKind.ENGINES, True)
    assert page.clean_button.isEnabled()
    assert page.checked_targets() == frozenset({CleanTarget.ENGINES})
    hint = page.cli_hint()
    assert hint is not None and hint.text() == "buttereye clean --engines"


def test_clean_confirm_lists_paths_then_cancel(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    page.set_checked(StorageKind.ENGINES, True)
    page.set_checked(StorageKind.MODELS, True)
    listing: list[str] = []

    def read_and_cancel(dlg: Any) -> None:
        listing.append(dlg.listing.toPlainText())
        dlg.findChild(type(dlg.cancel), "clean.cancel").click()

    seen = _on_modal(read_and_cancel)
    page.clean_button.click()
    assert seen == ["clean_confirm"]
    chosen = page.checked_entries()
    for e in chosen:
        assert f"{e.path} — 0 bytes" in listing[0]
    assert len(listing[0].splitlines()) == 2
    assert core.fake_call_count("clean") == 0


def test_clean_confirm_then_clean(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    core = fake_core(win.bridge)
    page.set_checked(StorageKind.ENGINES, True)
    page.set_checked(StorageKind.JOBS, True)
    click_modal(qtbot, "clean.confirm")
    page.clean_button.click()
    qtbot.waitUntil(lambda: page.clean_result.isVisibleTo(page), timeout=3000)
    call = [c for c in core.fake_calls if c.name == "clean"][-1]
    assert call.args == (frozenset({CleanTarget.ENGINES, CleanTarget.JOBS}),)
    assert page.clean_result.text().startswith("Freed 0 bytes.")
    assert page.open_folder.isVisibleTo(page)
    assert page.last_clean is not None
    assert all("/usr/" not in str(p) for p in page.last_clean.removed)


def test_clean_confirm_dialog_shows_sizes(qtbot: Any) -> None:
    entries = [e for e in sc.storage_devbox(sc.fake_paths(Path("/x"))) if e.rpm_owned is False]
    logs = [dataclasses.replace(e, bytes=1_200_000_000) for e in entries if e.clean_target][:1]
    dlg = CleanConfirmDialog(logs)
    qtbot.addWidget(dlg)
    assert "1.2 GB" in dlg.listing.toPlainText()
    assert "1.2 GB" in dlg.intro.text()
    assert dlg.cancel.isDefault()


def test_packaged_models_not_removable(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot)
    assert page.models_model.rowCount() == 3  # v4.26, v4.22-lite, v4.18 (v4.25-lite dropped)
    assert page.select_model("rife-v4.26_ensembleFalse")
    m = page.selected_model()
    assert m is not None and m.kind is ModelKind.PACKAGED
    assert not page.remove_button.isEnabled()
    assert "can't be removed" in page.remove_note.text()
    page.remove_selected()
    assert fake_core(win.bridge).fake_call_count("model_remove") == 0
    assert page.models_model.item(0, 1).text() == "Packaged"


def test_downloads_unavailable_on_devbox(make_window: Any, qtbot: Any) -> None:
    _win, page = _page(make_window, qtbot)
    assert not page.download_button.isEnabled()
    assert page.dl_badge.isVisibleTo(page)
    assert page.dl_badge.text() == "No downloadable models are listed in this release."
    assert "install a model archive from a file" in page.dl_body.text()


def test_consent_dialog_nothing_prechecked(qtbot: Any) -> None:
    items = sc.get("all_ready").model_downloads
    dlg = ConsentDialog(items)
    qtbot.addWidget(dlg)
    assert dlg.selected() == frozenset()
    assert not dlg.download_button.isEnabled()
    assert dlg.cancel_button.isDefault()
    t: Any = dlg.table
    headers = [t.horizontalHeaderItem(c).text() for c in range(t.columnCount())]
    assert headers == ["Name", "Size", "Licence", "Source URL", "SHA-256"]
    assert t.item(0, 3).text().startswith("https://github.com/")
    assert t.item(0, 4).text() == "0" * 64
    t.item(0, 0).setCheckState(Qt.CheckState.Checked)
    assert dlg.selected() == frozenset({"rife_v4.26.onnx"})
    assert dlg.download_button.isEnabled()


def _consent_and_download(dlg: Any) -> None:
    dlg.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
    dlg.download_button.click()


def test_hash_mismatch_reports_be6001(make_window: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(
        sc.get("all_ready"), hash_mismatch=frozenset({"rife_v4.26.onnx"}), op_step_s=0.005
    )
    win, page = _page(make_window, qtbot, scen)
    core = fake_core(win.bridge)
    assert page.download_button.isEnabled()
    seen = _on_modal(_consent_and_download)
    page.download_button.click()
    qtbot.waitUntil(lambda: seen == ["consent"], timeout=3000)
    qtbot.waitUntil(lambda: page.model_banner is not None, timeout=5000)
    banner = page.model_banner
    assert banner is not None
    assert banner.code == ErrorCode.HASH_MISMATCH.code == "BE-6001"
    assert banner.code_label.text() == "BE-6001"
    assert banner.title().startswith("Hash mismatch (expected ")
    assert banner.title().endswith("not installed.")
    assert not any("install anyway" in b.text().lower() for b in banner.buttons)
    assert "BE-6001" in page.install_rows[-1].result_badge.text()
    assert core.fake_call_count("model_install") == 1
    assert "rife_v4.26.onnx" not in {m.name for m in page.models}


def test_download_installs_after_consent(make_window: Any, qtbot: Any) -> None:
    scen = dataclasses.replace(sc.get("all_ready"), op_step_s=0.005)
    win, page = _page(make_window, qtbot, scen)
    seen = _on_modal(_consent_and_download)
    page.download_button.click()
    qtbot.waitUntil(lambda: seen == ["consent"], timeout=3000)
    qtbot.waitUntil(lambda: "rife_v4.26.onnx" in {m.name for m in page.models}, timeout=5000)
    row = page.install_rows[-1]
    assert row.is_finished() and row.value_permille() == 1000
    call = [c for c in fake_core(win.bridge).fake_calls if c.name == "model_install"][-1]
    assert call.args == ("rife_v4.26.onnx",)


def test_consent_cancel_downloads_nothing(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot, "all_ready")
    seen = click_modal(qtbot, "consent.cancel")
    page.download_button.click()
    qtbot.waitUntil(lambda: seen == ["consent"], timeout=3000)
    qtbot.wait(100)
    assert fake_core(win.bridge).fake_call_count("model_install") == 0


def test_install_from_file_unpinned(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win, page = _page(make_window, qtbot, "all_ready")
    archive = tmp_path / "rife-extra.tar.gz"
    archive.write_bytes(b"x")
    page.start_install_file(archive, "a" * 64, ask=False)
    qtbot.waitUntil(lambda: "rife-extra" in {m.name for m in page.models}, timeout=5000)
    call = [c for c in fake_core(win.bridge).fake_calls if c.name == "model_install_file"][-1]
    assert call.args == (archive, "a" * 64)
    assert page.select_model("rife-extra")
    r = page.models_table.currentIndex().row()
    assert page.models_model.item(r, 1).text() == "Unpinned"
    assert "unpinned" in page.models_model.item(r, 4).text()
    assert page.remove_button.isEnabled()
    assert "(unpinned)" in page.install_rows[-1].result_badge.text()


def test_sha256_dialog_validation(qtbot: Any) -> None:
    from buttereye.gui.pages.storage import Sha256Dialog

    dlg = Sha256Dialog(Path("/tmp/model.7z"))
    qtbot.addWidget(dlg)
    assert dlg.ok.isEnabled() and dlg.sha256() is None  # empty = use the manifest
    dlg.edit.setText("abc")
    assert not dlg.ok.isEnabled()
    dlg.edit.setText("A" * 64)
    assert dlg.ok.isEnabled() and dlg.sha256() == "a" * 64
    assert dlg.label.buddy() is dlg.edit


def test_remove_extra_model_confirms(make_window: Any, qtbot: Any, tmp_path: Path) -> None:
    win, page = _page(make_window, qtbot, "all_ready")
    archive = tmp_path / "extra.7z"
    page.start_install_file(archive, None, ask=False)
    qtbot.waitUntil(lambda: page.select_model("extra"), timeout=5000)
    core = fake_core(win.bridge)
    click_modal(qtbot, "remove.cancel")
    page.remove_button.click()
    qtbot.wait(50)
    assert core.fake_call_count("model_remove") == 0
    click_modal(qtbot, "remove.confirm")
    page.remove_button.click()
    qtbot.waitUntil(lambda: core.fake_call_count("model_remove") == 1, timeout=3000)
    qtbot.waitUntil(lambda: "extra" not in {m.name for m in page.models}, timeout=3000)


def test_storage_unavailable(make_window: Any, qtbot: Any) -> None:
    from buttereye.core.api import Feature, Reason
    from buttereye.core.capabilities import unavailable_state

    caps = dict(sc.caps_devbox_now())
    caps[Feature.STORAGE] = unavailable_state(
        Feature.STORAGE, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED
    )
    win = make_window(dataclasses.replace(sc.get("devbox_now"), capabilities=caps))
    win.go("storage")
    page = win.page("storage")
    qtbot.wait(150)
    assert page.panel.state == "unavailable"
    assert page.loc_model.rowCount() == 0
    assert fake_core(win.bridge).fake_call_count("storage") == 0


# ---------------------------------------------------------------------------
# install-from-file / remove need plugins.install (not in this build)
# ---------------------------------------------------------------------------


def _files_reason() -> str:
    from buttereye.gui.pages.storage import file_install_unavailable_text

    return file_install_unavailable_text()


def test_install_from_file_disabled_when_core_lacks_it(make_window: Any, qtbot: Any) -> None:
    win, page = _page(make_window, qtbot, "all_ready")
    assert page.file_button.isEnabled()  # FakeCore without the probe: unknown -> allowed
    core = fake_core(win.bridge)
    core.model_files_available = lambda: False  # what the real core reports today
    page.refresh()
    qtbot.waitUntil(lambda: page.files_ok is False, timeout=3000)
    assert not page.file_button.isEnabled()
    assert page.file_button.accessibleDescription() == _files_reason()
    assert page.file_button.toolTip() == _files_reason()
    assert not page.remove_button.isEnabled()
    assert page.remove_note.isVisibleTo(page) and page.remove_note.text() == _files_reason()
    page.install_from_file()  # no file chooser, nothing called
    assert core.fake_call_count("model_install_file") == 0


def test_install_from_file_learns_not_available(
    make_window: Any, qtbot: Any, tmp_path: Path
) -> None:
    from buttereye.core.api import Feature, NotAvailable, Reason
    from buttereye.core.capabilities import unavailable_state

    st = unavailable_state(Feature.MODELS, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)
    scen = dataclasses.replace(
        sc.get("all_ready"),
        errors={"model_install_file": NotAvailable.for_state(Feature.MODELS, st)},
    )
    _win, page = _page(make_window, qtbot, scen)
    page.start_install_file(tmp_path / "x.7z", None, ask=False)
    qtbot.waitUntil(lambda: page.files_ok is False, timeout=3000)
    assert not page.file_button.isEnabled()
    assert page.model_banner is not None and page.model_banner.title() == _files_reason()
    assert page.file_button.accessibleDescription() == _files_reason()
