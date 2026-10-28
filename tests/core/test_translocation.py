# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""ButterEye.app opened where it was downloaded: macOS runs a read-only copy
under ``AppTranslocation``, where ``xattr -dr com.apple.quarantine`` fails, so
the doctor says to move the app to Applications instead (review §5,
"Signing and translocation"). Pure: the app is a path and the quarantine
reading is faked."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from buttereye.core import osapi
from buttereye.core.doctor import checks_pkgs as pk

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a macOS bundle's paths")

TRANSLOCATED = Path(
    "/private/var/folders/x1/abc/T/AppTranslocation/0F1E-2D3C/d/ButterEye.app/Contents/Resources"
)
APPLICATIONS = Path("/Applications/ButterEye.app/Contents/Resources")


def test_a_translocated_copy_is_recognised_by_its_folder() -> None:
    assert pk.translocated(TRANSLOCATED.parent.parent)
    assert not pk.translocated(APPLICATIONS.parent.parent)
    # the word inside another folder name is not the system's folder
    assert not pk.translocated(Path("/Users/me/MyAppTranslocationNotes/ButterEye.app"))


def test_a_translocated_app_is_told_to_move_not_to_run_xattr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = TRANSLOCATED / "bin" / "mpv"
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: (held,))
    f = pk.app_quarantine_finding(TRANSLOCATED)
    assert f is not None
    assert f.severity.value == "blocking"
    assert f.commands == ()  # the xattr line dead-ends on the read-only copy
    assert "Applications" in f.fix.key
    assert f.evidence == (str(TRANSLOCATED.parent.parent), str(held))


def test_translocation_is_reported_even_with_nothing_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: ())
    f = pk.app_quarantine_finding(TRANSLOCATED)
    assert f is not None and f.severity.value == "degraded"
    assert f.commands == ()


def test_an_app_in_applications_keeps_the_xattr_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: (r / "bin" / "mpv",))
    f = pk.app_quarantine_finding(APPLICATIONS)
    assert f is not None
    assert f.commands == ("xattr -dr com.apple.quarantine /Applications/ButterEye.app",)
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: ())
    assert pk.app_quarantine_finding(APPLICATIONS) is None


def test_release_app_runs_nothing_on_a_translocated_copy() -> None:
    """xattr cannot change the read-only copy; the fix is to move the app."""
    ran: list[object] = []
    held = lambda p: True  # noqa: E731
    assert (
        osapi.release_app(TRANSLOCATED, has_quarantine=held, run=lambda *a, **k: ran.append(a))
        is False
    )
    assert ran == []
    assert osapi.APP_TRANSLOCATION_DIR == pk.TRANSLOCATION_DIR


def test_the_bundled_mkvmerge_is_resolved_by_full_path_and_checked_for_the_mark() -> None:
    assert "mkvmerge" in osapi.APP_TOOLS
    assert "Resources/bin/mkvmerge" in osapi.APP_PROGRAMS
