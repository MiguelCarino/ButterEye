# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.2: error codes are unique, exit codes valid, docs/errors.md is complete."""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import errors
from buttereye.core.errors import (
    BlockingIssue,
    ButterEyeError,
    ConfigConflict,
    ConfigError,
    DependencyMissing,
    DownloadFailed,
    ErrorCode,
    FilterFailed,
    InstanceRefused,
    InsufficientSpace,
    NotAvailable,
    OperationCancelled,
    RenderRefused,
)
from buttereye.core.types import CapState, Feature, Msg, Reason, RefusalReason


def test_codes_unique_and_well_formed() -> None:
    codes = [e.code for e in ErrorCode]
    assert len(codes) == len(set(codes))
    for e in ErrorCode:
        assert re.fullmatch(r"BE-\d{4}", e.code), e
        assert e.exit_code in {1, 2, 3, 4}, e


def test_codes_match_design(design_doc: Any) -> None:
    tree = ast.parse(design_doc.python("### 2.2"))
    enum_node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ErrorCode")
    doc = {
        s.targets[0].id: ast.literal_eval(s.value)
        for s in enum_node.body
        if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name)
    }
    assert {e.name: e.value for e in ErrorCode} == doc
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef) and n.name != "ErrorCode"]
    for name in classes:
        assert issubclass(getattr(errors, name), ButterEyeError) or name == "ButterEyeError"


def test_errors_md_lists_every_code(repo_root: Path) -> None:
    text = (repo_root / "docs" / "errors.md").read_text(encoding="utf-8")
    listed = set(re.findall(r"^\| (BE-\d{4}) \| `([A-Z0-9_]+)` \| (\d+) \|", text, re.M))
    expected = {(e.code, e.name, str(e.exit_code)) for e in ErrorCode}
    assert listed == expected


def test_lookup_by_code() -> None:
    assert ErrorCode.from_code("BE-4001") is ErrorCode.FFMS2_MISSING
    with pytest.raises(KeyError):
        ErrorCode.from_code("BE-0000")


def test_base_error_fields() -> None:
    e = ButterEyeError(
        ErrorCode.IPC_LOST, Msg("lost"), Msg("reconnect"), detail="tail", commands=("a",)
    )
    assert (e.code, e.cause.key, e.fix and e.fix.key, e.detail, e.commands) == (
        ErrorCode.IPC_LOST,
        "lost",
        "reconnect",
        "tail",
        ("a",),
    )
    assert e.exit_code == 1
    assert "BE-3005" in str(e)


def test_subclass_extra_fields() -> None:
    m = Msg("x")
    assert ConfigError(ErrorCode.CONFIG_INVALID, m, line=3, column=4).line == 3
    cc = ConfigConflict(ErrorCode.CONFIG_CONFLICT, m, on_disk_revision="abc")
    assert isinstance(cc, ConfigError) and cc.on_disk_revision == "abc"
    assert DependencyMissing(ErrorCode.FFMS2_MISSING, m, commands=("sudo dnf install ffms2",))
    assert BlockingIssue(ErrorCode.MPV_TOO_OLD, m, findings=()).findings == ()
    assert InstanceRefused(ErrorCode.SOCKET_UNSAFE, m, reason=RefusalReason.WRONG_UID).reason
    assert FilterFailed(ErrorCode.VF_ROLLED_BACK, m, rolled_back=True).rolled_back
    assert isinstance(RenderRefused(ErrorCode.HDR_CLASS_REFUSED, m), ButterEyeError)
    sp = InsufficientSpace(ErrorCode.NO_SPACE, m, need_bytes=10, free_bytes=1)
    assert (sp.need_bytes, sp.free_bytes) == (10, 1)
    assert DownloadFailed(ErrorCode.DOWNLOAD_FAILED, m, url="https://x").url == "https://x"


def test_cancelled_exit_code_is_130() -> None:
    assert OperationCancelled(ErrorCode.INTERNAL, Msg("Cancelled.")).exit_code == 130
    assert OperationCancelled(ErrorCode.ENCODER_FAILED, Msg("Cancelled.")).exit_code == 130


def test_not_available_for_state() -> None:
    st = CapState(
        False,
        Reason.MISSING_DEPENDENCY,
        ErrorCode.FFMS2_MISSING,
        Msg("Render needs"),
        ("sudo dnf install ffms2",),
    )
    e = NotAvailable.for_state(Feature.RENDER, st)
    assert e.code is ErrorCode.FFMS2_MISSING
    assert e.feature is Feature.RENDER and e.state is st
    assert e.commands == ("sudo dnf install ffms2",)
    e2 = NotAvailable.for_state(Feature.LIVE, CapState(False, Reason.NOT_IMPLEMENTED))
    assert e2.code is ErrorCode.NOT_IMPLEMENTED
