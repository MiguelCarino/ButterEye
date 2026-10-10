# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Sharper upscaling with the bundled FSRCNNX shader (SCOPE §15.2, spike M0(o)):
config, the IPC allowlist, the launch flag, the live add/remove and the
licences pane. No mpv needed."""

from __future__ import annotations

import dataclasses
import hashlib
from pathlib import Path

import pytest

from buttereye.core import licences
from buttereye.core.mpvctl import ipc, launcher, shaders
from buttereye.core.profiles import schema
from buttereye.core.testing import scenarios as sc
from tests.test_live_decisions import FakeCtx, _config, _paths, _session, _simple

SHADER = shaders.UPSCALERS["sharper"]


def test_bundled_shader_is_the_recorded_file() -> None:
    readme = (shaders.SHADER_DIR / "README.md").read_text()
    digest = hashlib.sha256(SHADER.read_bytes()).hexdigest()
    assert digest in readme
    head = SHADER.read_text()[:600]
    assert "GNU Lesser General Public" in head and "version 3.0" in head
    assert (shaders.SHADER_DIR / "LGPL-3.0.txt").is_file()
    assert (shaders.SHADER_DIR / "GPL-3.0.txt").is_file()


def test_setting_maps_to_the_shader() -> None:
    assert shaders.shader_for("sharper") == SHADER
    assert shaders.shader_for("standard") is None
    assert shaders.shader_for("anything") is None


def test_config_round_trip_and_default_omitted() -> None:
    base = sc.default_config()
    assert base.general.upscaling == "standard"
    assert "upscaling" not in schema.dumps_config(base)
    sharp = dataclasses.replace(
        base, general=dataclasses.replace(base.general, upscaling="sharper")
    )
    text = schema.dumps_config(sharp)
    assert 'upscaling = "sharper"' in text
    assert schema.parse_text(text).config.general.upscaling == "sharper"


def test_bad_value_is_reported_and_falls_back() -> None:
    load = schema.parse_text('schema_version = 1\n[general]\nupscaling = "ultra"\n')
    assert load.config.general.upscaling == "standard"
    assert any(i.field == "general.upscaling" for i in load.issues)


def test_ipc_allows_only_the_bundled_shader() -> None:
    ipc.check_command(["change-list", "glsl-shaders", "append", str(SHADER)])
    ipc.check_command(["change-list", "glsl-shaders", "remove", str(SHADER)])
    for bad in (
        ["change-list", "glsl-shaders", "append", "/tmp/evil.glsl"],
        ["change-list", "glsl-shaders", "set", str(SHADER)],
        ["change-list", "glsl-shaders", "clr", ""],
        ["change-list", "script-opts", "append", str(SHADER)],
        ["change-list", "glsl-shaders", "append"],
    ):
        with pytest.raises(ipc.CommandRefused):
            ipc.check_command(bad)
    ipc.check_command(["get_property", "glsl-shaders"])


def test_launch_appends_so_user_shaders_stay(tmp_path: Path) -> None:
    argv = launcher.build_argv(
        "mpv", include=tmp_path / "i.conf", socket=tmp_path / "s", file=tmp_path / "f.mkv",
        shaders=(SHADER,),
    )  # fmt: skip
    assert f"--glsl-shaders-append={SHADER}" in argv
    assert not any(a.startswith("--glsl-shaders=") for a in argv)
    plain = launcher.build_argv(
        "mpv", include=tmp_path / "i.conf", socket=tmp_path / "s", file=tmp_path / "f.mkv"
    )
    assert not any("glsl-shaders" in a for a in plain)


def _upscaling(cfg: object, value: str) -> object:
    return dataclasses.replace(
        cfg,  # type: ignore[type-var]
        general=dataclasses.replace(cfg.general, upscaling=value),  # type: ignore[attr-defined]
    )


async def test_session_adds_and_removes_only_its_shader(tmp_path: Path) -> None:
    cfg = _config(_simple())
    ctx = FakeCtx(_paths(tmp_path), cfg)
    s = _session(ctx, tmp_path)
    await s.sync_shader(_upscaling(cfg, "sharper"))  # type: ignore[arg-type]
    await s.sync_shader(_upscaling(cfg, "sharper"))  # type: ignore[arg-type]  # no change
    await s.sync_shader(_upscaling(cfg, "standard"))  # type: ignore[arg-type]
    lists = [c for c in s.ipc.commands if c[0] == "change-list"]  # type: ignore[attr-defined]
    assert lists == [
        ("change-list", "glsl-shaders", "append", str(SHADER)),
        ("change-list", "glsl-shaders", "remove", str(SHADER)),
    ]
    assert s.shader is None


def test_licences_list_the_bundled_shader() -> None:
    rows = {r.name: r for r in licences.build_third_party({}, None)}
    row = rows["FSRCNNX x2 8-0-4-1 shader"]
    assert row.spdx == "LGPL-3.0-or-later" and row.conveyed and row.detected
    assert {p.name for p in row.text_files} == {"LGPL-3.0.txt", "GPL-3.0.txt"}
