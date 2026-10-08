# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.6: every command id renders; unknown placeholders raise; proposed ids listed."""

from __future__ import annotations

import importlib.metadata
import shutil
import tomllib
from pathlib import Path

import pytest

from buttereye.core import commands
from buttereye.core.commands import COMMANDS, command_hint

# SCOPE §4.1 adopted every formerly proposed command on 2026-10-07 (GUI.md §10).
PROPOSED: set[str] = set()

ALL_IDS = {
    "setup",
    "doctor",
    "doctor.report",
    "play",
    "attach",
    "detach",
    "detach.disable",
    "live.toggle",
    "live.profile",
    "orphans.remove",
    "render",
    "bench",
    "bench.apply",
    "models.list",
    "models.add",
    "models.remove",
    "models.install",
    "plugins.list",
    "clean",
    "clean.dry_run",
    "profiles.list",
    "profiles.explain",
    "trt.optin",
    "licence",
    "version",
}

SAMPLE = {
    "file": "/v/a.mkv",
    "profile": "fast",
    "socket": "/run/user/1000/buttereye/mpv.sock",
    "toggle": "enable",
    "src": "/v/in.mkv",
    "out": "/v/out.mkv",
    "encoder": "libx265",
    "label": "rife-v4.26",
    "name": "rife-v4.26",
    "sha": "ab" * 32,
    "target": "engines,models",
    "fps": "24000/1001",
    "width": "1920",
    "height": "1080",
    "hdr": "sdr",
    "display_hz": "120",
    "interlaced": "no",
    "path": "/v/a.mkv",
    "full": "yes",
}


def test_table_ids_and_status() -> None:
    assert set(COMMANDS) == ALL_IDS
    assert {cid for cid, spec in COMMANDS.items() if spec.status == "proposed"} == PROPOSED


@pytest.mark.parametrize("cid", sorted(ALL_IDS))
def test_every_id_renders(cid: str) -> None:
    spec = COMMANDS[cid]
    params = {k: SAMPLE[k] for k in spec.placeholders()}
    hint = command_hint(cid, **params)
    assert hint.argv[0] == "buttereye"
    assert hint.status == spec.status
    assert "{" not in hint.text() and "[" not in hint.text()


def test_optional_groups() -> None:
    assert command_hint("play", file="/v/a.mkv").argv == ("buttereye", "play", "/v/a.mkv")
    assert command_hint("play", file="/v/a.mkv", profile="fast").argv[-2:] == ("--profile", "fast")
    assert command_hint("attach").argv == ("buttereye", "attach")
    assert command_hint("bench").argv == ("buttereye", "bench")
    assert command_hint("bench", full="yes").argv == ("buttereye", "bench", "--full")
    assert command_hint("bench", full="no").argv == ("buttereye", "bench")
    assert command_hint("detach.disable").argv == ("buttereye", "detach", "--disable")


def test_repeat_and_embedded() -> None:
    assert command_hint("clean", target="engines,models").argv == (
        "buttereye",
        "clean",
        "--engines",
        "--models",
    )
    assert command_hint("live.toggle", socket="/s", toggle="disable").argv[-1] == "--disable"


def test_unknown_placeholder_raises() -> None:
    with pytest.raises(KeyError):
        command_hint("doctor", file="x")
    with pytest.raises(KeyError):
        command_hint("play", fiel="x")


def test_missing_required_raises() -> None:
    with pytest.raises(KeyError):
        command_hint("play")
    with pytest.raises(KeyError):
        command_hint("render", src="a", out="b", profile="c")
    with pytest.raises(KeyError):
        command_hint("clean")


def test_unknown_command_raises() -> None:
    with pytest.raises(KeyError):
        command_hint("no.such.command")


def test_text_quotes_spaces() -> None:
    h = command_hint("play", file="/home/me/My Film.mkv")
    assert h.argv[2] == "/home/me/My Film.mkv"
    assert h.text() == "buttereye play '/home/me/My Film.mkv'"


def test_cli_available_tracks_console_script(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hints are a spec, not proof a CLI exists: cli_available() is the gate."""
    monkeypatch.setattr(
        importlib.metadata, "entry_points", lambda **kw: importlib.metadata.EntryPoints(())
    )
    monkeypatch.setattr(shutil, "which", lambda name: None)
    commands.cli_available.cache_clear()
    try:
        assert commands.cli_available() is False
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/" + name)
        commands.cli_available.cache_clear()
        assert commands.cli_available() is True
    finally:
        commands.cli_available.cache_clear()


def test_this_build_has_no_cli_script(repo_root: Path) -> None:
    """pyproject installs only buttereye-gui today, so the gate must say False
    unless a buttereye console script is declared (then flip this test)."""
    scripts = tomllib.loads((repo_root / "pyproject.toml").read_text())["project"]["scripts"]
    if commands.CLI_SCRIPT in scripts:  # pragma: no cover - once the CLI ships
        pytest.skip("buttereye CLI is declared")
    commands.cli_available.cache_clear()
    if shutil.which(commands.CLI_SCRIPT) is None:
        assert commands.cli_available() is False
