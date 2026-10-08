# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""About page (docs/design/GUI.md §4.9, §6 ``test_about_page``; U6; SCOPE F18).

Every F18 item is visible; a missing shipped file shows the BE-1021 banner;
the Licences tab lists ncnn and glslang as bundled and conveyed.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import Qt

from buttereye.core.api import (
    ComponentLicence,
    ErrorCode,
    Feature,
    LegalNotices,
    Msg,
    Reason,
    Section,
    Severity,
)
from buttereye.core.capabilities import unavailable_state
from buttereye.core.testing import scenarios as sc
from buttereye.gui.dialogs.text_viewer import TextViewer, read_text
from buttereye.gui.pages import about as about_mod
from buttereye.gui.pages.about import AboutPage
from buttereye.gui.pages.base import PageAction
from buttereye.gui.widgets.cli_hint import CliHint
from tests.gui.helpers import fake_core


def _notices(tmp_path: Path, *, missing: bool = False) -> LegalNotices:
    agpl = tmp_path / "COPYING"
    perm = tmp_path / "AdditionRef-ButterEye-generated-output.txt"
    if not missing:
        agpl.write_text("GNU AFFERO GENERAL PUBLIC LICENSE\nVersion 3, 19 November 2007\n")
        perm.write_text("Additional permission under GNU AGPL version 3 section 7\n")
    return dataclasses.replace(
        sc.legal_notices_fake(),
        agpl_text=agpl,
        output_permission_text=perm,
        missing=(agpl, perm) if missing else (),
    )


@pytest.fixture
def notices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LegalNotices:
    n = _notices(tmp_path)
    monkeypatch.setattr(about_mod, "legal_notices", lambda: n)
    return n


def _page(make_window: Any, qtbot: Any, scenario: Any = "devbox_now") -> tuple[Any, AboutPage]:
    win = make_window(scenario)
    win.go("about")
    page = win.page("about")
    assert isinstance(page, AboutPage)
    return win, page


def test_all_f18_items_visible(make_window: Any, qtbot: Any, notices: LegalNotices) -> None:
    _win, page = _page(make_window, qtbot)
    assert page.tabs.currentWidget() is page.about_tab
    assert page.name_label.text() == f"ButterEye {notices.version}"
    assert page.copyright_label.text() == "Copyright (C) 2026 The ButterEye contributors"
    assert "GNU Affero General Public License v3.0 or later" in page.licence_label.text()
    assert "ABSOLUTELY NO WARRANTY" in page.warranty_label.text()
    for w in (
        page.name_label,
        page.copyright_label,
        page.licence_label,
        page.warranty_label,
        page.view_licence,
        page.view_permission,
        page.source_field,
    ):
        assert w.isVisibleTo(page), w
    assert page.view_licence.isEnabled() and page.view_permission.isEnabled()
    assert page.source_field.text() == notices.source_link
    assert page.open_source.isVisibleTo(page)  # an https link
    assert not page.missing_banners
    assert page.version_hint.text() == "buttereye --version"
    assert page.lic_hint.text() == "buttereye licence"
    # one command hint per tab, never two stacked on the About tab
    about_tab = page.tabs.widget(0)
    assert about_tab is not None
    assert len([h for h in about_tab.findChildren(CliHint) if h.isVisibleTo(about_tab)]) == 1


def test_view_full_licence_shows_shipped_text(
    make_window: Any, qtbot: Any, notices: LegalNotices
) -> None:
    _win, page = _page(make_window, qtbot)
    page.view_licence.click()
    dlg = page.last_viewer
    assert isinstance(dlg, TextViewer)
    qtbot.waitExposed(dlg)
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in dlg.current_text()
    assert dlg.text.isReadOnly()
    assert dlg.text.tabChangesFocus()  # keyboard-scrollable, no Tab trap
    dlg.close()
    page.view_permission.click()
    assert page.last_viewer is not None
    assert "section 7" in page.last_viewer.current_text()
    page.last_viewer.close()


def test_missing_file_banner(
    make_window: Any, qtbot: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    n = _notices(tmp_path, missing=True)
    monkeypatch.setattr(about_mod, "legal_notices", lambda: n)
    _win, page = _page(make_window, qtbot)
    assert len(page.missing_banners) == 2
    b = page.missing_banners[0]
    assert b.isVisibleTo(page)
    assert b.kind == "degraded"
    assert b.code == ErrorCode.LICENCE_FILE_MISSING.code == "BE-1021"
    assert b.title() == (
        f"Licence file not found at {n.agpl_text} (BE-1021). This is a packaging bug."
    )
    assert not page.view_licence.isEnabled()
    assert "BE-1021" in page.view_licence.accessibleDescription()
    # the rest of the notices are still shown
    assert page.copyright_label.text() == n.copyright


def test_real_notices_never_hidden(make_window: Any, qtbot: Any) -> None:
    """With the real provider: whatever is missing on this checkout is shown, not hidden."""
    from buttereye.core.api import legal_notices

    n = legal_notices()
    _win, page = _page(make_window, qtbot)
    assert len(page.missing_banners) == len(n.missing)
    assert page.copyright_label.text() == n.copyright
    assert page.source_field.text() == n.source_link


def test_licences_tab_lists_ncnn_and_glslang(
    make_window: Any, qtbot: Any, notices: LegalNotices
) -> None:
    win, page = _page(make_window, qtbot)
    assert page.handle_action(PageAction.SHOW_LICENCES)
    assert page.tabs.currentWidget() is page.licences_tab
    qtbot.waitUntil(lambda: page.lic_panel.state == "content", timeout=3000)
    m = page.model
    rows = {
        m.item(r, 0).text(): [m.item(r, c).text() for c in range(7)] for r in range(m.rowCount())
    }
    for name in ("ncnn (bundled)", "glslang (bundled)"):
        assert name in rows
        assert rows[name][4] == "Yes (COPR)"
    assert rows["ncnn (bundled)"][2] == "BSD-3-Clause AND BSD-2-Clause AND Zlib"
    assert "GPL-3.0-or-later WITH Bison-exception-2.2" in rows["glslang (bundled)"][2]
    assert rows["PySide6 / Qt"][3] == "in-process"
    assert rows["PySide6 / Qt"][4] == "No"
    assert rows["mpv"][3] == "separate program"
    assert rows["ffms2"][5] == "Not found"
    assert rows["ffms2"][1] == "unknown"
    headers = [m.headerData(c, Qt.Orientation.Horizontal) for c in range(7)]
    assert headers[:6] == [
        "Name",
        "Version",
        "SPDX",
        "How it's used",
        "Shipped by ButterEye",
        "Detected",
    ]
    assert fake_core(win.bridge).fake_call_count("third_party") >= 1
    assert page.handle_action(PageAction.SHOW_ABOUT)
    assert page.tabs.currentWidget() is page.about_tab


def test_view_licence_text_of_component(
    make_window: Any, qtbot: Any, tmp_path: Path, notices: LegalNotices
) -> None:
    lic = tmp_path / "LICENSE"
    lic.write_text("MIT License\n\nCopyright (c) 2021 nihui\n")
    other = tmp_path / "LICENSE.ncnn.txt"
    other.write_text("BSD 3-Clause License\n")
    comps = (
        ComponentLicence(
            "RIFE ncnn models", "r9_mod_v33", "MIT", Msg("data"), True, True, (lic, other), None
        ),
        ComponentLicence("ffms2", None, "MIT", Msg("loaded by vspipe"), False, False, (), None),
    )
    scen = dataclasses.replace(sc.get("devbox_now"), third_party=comps)
    _win, page = _page(make_window, qtbot, scen)
    page.handle_action(PageAction.SHOW_LICENCES)
    qtbot.waitUntil(lambda: page.lic_panel.state == "content", timeout=3000)
    page.table.setCurrentIndex(page.model.index(0, 0))
    assert page.view_text.isEnabled()
    page.view_text.click()
    dlg = page.last_viewer
    assert dlg is not None
    assert "MIT License" in dlg.current_text()
    assert dlg.chooser.isVisibleTo(dlg) and dlg.chooser.count() == 2
    dlg.chooser.setCurrentIndex(1)
    assert "BSD 3-Clause" in dlg.current_text()
    dlg.close()
    page.table.setCurrentIndex(page.model.index(1, 0))
    assert not page.view_text.isEnabled()
    assert "ffms2" in page.view_text.accessibleDescription()


def test_licence_finding_shown_in_notes(
    make_window: Any, qtbot: Any, notices: LegalNotices
) -> None:
    f = sc.finding(
        "licences.rife",
        Section.PACKAGES,
        Severity.DEGRADED,
        "Licence file missing",
        code=ErrorCode.LICENCE_FILE_MISSING,
    )
    comps = (
        ComponentLicence(
            "RIFE-ncnn-Vulkan", "9.33", "MIT", Msg("loaded by mpv/vspipe"), True, True, (), f
        ),
    )
    _win, page = _page(
        make_window, qtbot, dataclasses.replace(sc.get("devbox_now"), third_party=comps)
    )
    page.handle_action(PageAction.SHOW_LICENCES)
    qtbot.waitUntil(lambda: page.lic_panel.state == "content", timeout=3000)
    assert page.model.item(0, 6).text() == "Licence file missing (BE-1021)"


def test_licences_unavailable_keeps_notices(
    make_window: Any, qtbot: Any, notices: LegalNotices
) -> None:
    caps = dict(sc.caps_devbox_now())
    caps[Feature.LICENCES] = unavailable_state(
        Feature.LICENCES, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED
    )
    win, page = _page(
        make_window, qtbot, dataclasses.replace(sc.get("devbox_now"), capabilities=caps)
    )
    qtbot.wait(100)
    assert page.lic_panel.state == "unavailable"
    assert page.model.rowCount() == 0
    assert page.lic_panel.unavailable_texts()[2] == "BE-9001"
    # ButterEye's own notices are never gated
    assert page.copyright_label.text() == notices.copyright
    assert fake_core(win.bridge).fake_call_count("third_party") == 0


def test_text_viewer_missing_file(qtbot: Any, tmp_path: Path) -> None:
    gone = tmp_path / "nope.txt"
    text, found = read_text(gone)
    assert not found and "BE-1021" in text
    dlg = TextViewer("Licence", [gone])
    qtbot.addWidget(dlg)
    assert "BE-1021" in dlg.current_text()
    assert dlg.found[gone] is False
    assert not dlg.chooser.isVisibleTo(dlg)
