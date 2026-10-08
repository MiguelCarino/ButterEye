# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: XDG path table and the runtime-dir safety check (BE-2010, never chmod)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.paths import ensure_runtime_dir, resolve


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


def _env(tmp_path: Path, **over: str) -> dict[str, str]:
    run = tmp_path / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    run.chmod(0o700)
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "XDG_RUNTIME_DIR": str(run),
    }
    env.update(over)
    return env


def test_xdg_table(tmp_path: Path) -> None:
    p = resolve(_env(tmp_path))
    assert p.config_file == tmp_path / "cfg/buttereye/config.toml"
    assert p.mpv_include == tmp_path / "cfg/buttereye/mpv/buttereye.conf"
    assert p.user_mpv_conf == tmp_path / "cfg/mpv/mpv.conf"
    assert p.data_dir == tmp_path / "data/buttereye"
    assert p.cache_dir == tmp_path / "cache/buttereye"
    assert p.state_dir == tmp_path / "state/buttereye"
    assert p.logs_dir == tmp_path / "state/buttereye/logs"
    assert p.jobs_dir == tmp_path / "state/buttereye/jobs"
    assert p.bench_file == tmp_path / "state/buttereye/bench.json"
    assert p.runtime_dir == tmp_path / "run/buttereye"
    assert p.rpm_plugin_dir == Path("/usr/lib64/buttereye/vapoursynth")
    # resolve() never creates anything
    assert not p.runtime_dir.exists()
    assert not (tmp_path / "cfg").exists()


def test_defaults_and_relative_values_ignored(tmp_path: Path) -> None:
    env = _env(tmp_path)
    for k in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        del env[k]
    env["XDG_STATE_HOME"] = "relative/state"  # invalid per the XDG spec
    p = resolve(env)
    home = tmp_path / "home"
    assert p.config_file == home / ".config/buttereye/config.toml"
    assert p.data_dir == home / ".local/share/buttereye"
    assert p.cache_dir == home / ".cache/buttereye"
    assert p.state_dir == home / ".local/state/buttereye"


def test_mpv_home(tmp_path: Path) -> None:
    p = resolve(_env(tmp_path, MPV_HOME=str(tmp_path / "mpvhome")))
    assert p.user_mpv_conf == tmp_path / "mpvhome/mpv.conf"


@pytest.mark.parametrize("value", ["", "relative/run"])
def test_runtime_unset_or_relative(tmp_path: Path, value: str) -> None:
    env = _env(tmp_path, XDG_RUNTIME_DIR=value)
    with pytest.raises(ButterEyeError) as ei:
        resolve(env)
    assert ei.value.code is ErrorCode.RUNTIME_DIR_UNSAFE


def test_unsafe_mode_never_chmodded(tmp_path: Path) -> None:
    env = _env(tmp_path)
    d = Path(env["XDG_RUNTIME_DIR"]) / "buttereye"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(ButterEyeError) as ei:
        resolve(env)
    assert ei.value.code is ErrorCode.RUNTIME_DIR_UNSAFE
    assert _mode(d) == 0o755  # untouched


def test_unsafe_base_runtime(tmp_path: Path) -> None:
    env = _env(tmp_path)
    Path(env["XDG_RUNTIME_DIR"]).chmod(0o755)
    with pytest.raises(ButterEyeError) as ei:
        resolve(env)
    assert ei.value.code is ErrorCode.RUNTIME_DIR_UNSAFE
    assert _mode(Path(env["XDG_RUNTIME_DIR"])) == 0o755


def test_symlink_and_file_refused(tmp_path: Path) -> None:
    env = _env(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = Path(env["XDG_RUNTIME_DIR"]) / "buttereye"
    link.symlink_to(target)
    with pytest.raises(ButterEyeError) as ei:
        resolve(env)
    assert ei.value.code is ErrorCode.RUNTIME_DIR_UNSAFE
    link.unlink()
    link.write_text("x")
    with pytest.raises(ButterEyeError):
        resolve(env)


def test_ensure_runtime_dir_creates_0700(tmp_path: Path) -> None:
    old = os.umask(0o022)
    try:
        p = resolve(_env(tmp_path))
        d = ensure_runtime_dir(p)
    finally:
        os.umask(old)
    assert d == p.runtime_dir
    assert _mode(d) == 0o700
    assert ensure_runtime_dir(p) == d  # idempotent


def test_resolve_from_environment(xdg_env: object) -> None:
    p = resolve()
    assert str(p.runtime_dir).startswith(os.environ["XDG_RUNTIME_DIR"])


def test_xdg_dotdot_values_ignored_and_values_normalised(tmp_path: Path) -> None:
    env = _env(tmp_path)
    env["XDG_CACHE_HOME"] = str(tmp_path / "home/.cache/../../../usr/share")
    env["XDG_DATA_HOME"] = str(tmp_path) + "//data/./x/"
    p = resolve(env)
    assert p.cache_dir == tmp_path / "home/.cache/buttereye"  # ignored -> default
    assert p.data_dir == tmp_path / "data/x/buttereye"
