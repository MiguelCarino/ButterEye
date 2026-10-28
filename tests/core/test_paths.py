# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: XDG path table and the runtime-dir safety check (BE-2010, never chmod)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from buttereye.core import osapi
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


@pytest.fixture
def xdg_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ask ``resolve()`` for the XDG table whatever platform the tests run on.

    ``resolve()`` picks its branch from ``osapi.IS_MACOS`` when it is called, so
    clearing that is enough to get the Linux one, and all that branch then does is
    plain POSIX (read the environment, ``lstat``, st_uid, mode bits): a Mac
    answers with the same table from the same environment. So the tests below go
    on checking the XDG contract (SCOPE §4.6) on macOS instead of being skipped
    there - resolve() on a Mac otherwise answers with the ~/Library paths that
    tests/macos/test_paths_macos.py asserts, and these assertions would have
    nothing left to say.

    Nothing changes on Linux, where the flag is already False, or on Windows,
    where ``IS_WINDOWS`` is read first and is deliberately left alone.
    """
    monkeypatch.setattr(osapi, "IS_MACOS", False)


def test_xdg_table(tmp_path: Path, xdg_layout: None) -> None:
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


def test_defaults_and_relative_values_ignored(tmp_path: Path, xdg_layout: None) -> None:
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
def test_runtime_unset_or_relative(tmp_path: Path, value: str, xdg_layout: None) -> None:
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


def test_xdg_dotdot_values_ignored_and_values_normalised(tmp_path: Path, xdg_layout: None) -> None:
    env = _env(tmp_path)
    env["XDG_CACHE_HOME"] = str(tmp_path / "home/.cache/../../../usr/share")
    env["XDG_DATA_HOME"] = str(tmp_path) + "//data/./x/"
    p = resolve(env)
    assert p.cache_dir == tmp_path / "home/.cache/buttereye"  # ignored -> default
    assert p.data_dir == tmp_path / "data/x/buttereye"


# ---------------------------------------------------------------- the GUI's own files (B13)
def test_gui_files_stay_in_the_xdg_state_folder_off_macos(tmp_path: Path) -> None:
    from buttereye.core.paths import gui_dirs

    d = gui_dirs(_env(tmp_path), macos=False, windows=False)
    assert d.settings_file == tmp_path / "state" / "buttereye" / "gui.ini"
    assert d.log_file == tmp_path / "state" / "buttereye" / "logs" / "gui.log"
    assert d.legacy_settings_file is None and d.legacy_log_file is None
    # the log is Paths.logs_dir's, so Storage's logs row counts it
    assert d.log_file.parent == tmp_path / "state" / "buttereye" / "logs"


def test_gui_files_live_in_library_on_macos_and_name_where_0_0_5_put_them(
    tmp_path: Path,
) -> None:
    from buttereye.core.paths import gui_dirs

    env = _env(tmp_path)
    d = gui_dirs(env, macos=True)
    lib = tmp_path / "home" / "Library"
    assert d.log_file == lib / "Logs" / "ButterEye" / "gui.log"
    # beside config.toml, the folder Storage reports as "Settings"
    assert d.settings_file == lib / "Application Support" / "ButterEye" / "config" / "gui.ini"
    assert d.legacy_settings_file == tmp_path / "state" / "buttereye" / "gui.ini"
    assert d.legacy_log_file == tmp_path / "state" / "buttereye" / "logs" / "gui.log"
    # an exported XDG variable does not move them out of ~/Library
    other = gui_dirs({**env, "XDG_STATE_HOME": str(tmp_path / "elsewhere")}, macos=True)
    assert other.log_file == d.log_file and other.settings_file == d.settings_file


def test_gui_files_are_pure(tmp_path: Path) -> None:
    """No folder is created or checked: the GUI asks before the core is open."""
    from buttereye.core.paths import gui_dirs

    env = _env(tmp_path, XDG_RUNTIME_DIR="relative/not-checked")
    gui_dirs(env, macos=True)
    gui_dirs(env, macos=False, windows=False)
    gui_dirs(_win_env(tmp_path), macos=False, windows=True)
    assert not (tmp_path / "home").exists() and not (tmp_path / "state").exists()
    assert not (tmp_path / "Roaming").exists() and not (tmp_path / "Local").exists()


def _win_env(tmp_path: Path, **over: str) -> dict[str, str]:
    """A Windows environment whose folders are absolute on the host running the
    test, so ``_resolve_windows`` can be asked from Linux as well."""
    env = {
        "HOME": str(tmp_path / "home"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "USERPROFILE": str(tmp_path / "profile"),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }
    env.update(over)
    return env


def test_gui_files_live_beside_the_cores_on_windows_and_name_where_0_0_5_put_them(
    tmp_path: Path,
) -> None:
    from buttereye.core.paths import _resolve_windows, gui_dirs
    from buttereye.core.storage import locations
    from buttereye.core.types import StorageKind

    env = _win_env(tmp_path)
    d = gui_dirs(env, macos=False, windows=True)
    assert d.settings_file == tmp_path / "Roaming" / "ButterEye" / "gui.ini"
    assert d.log_file == tmp_path / "Local" / "ButterEye" / "state" / "logs" / "gui.log"
    # 0.0.5 used the Linux layout on Windows too
    assert d.legacy_settings_file == tmp_path / "state" / "buttereye" / "gui.ini"
    assert d.legacy_log_file == tmp_path / "state" / "buttereye" / "logs" / "gui.log"
    # the same folders the core resolves, so Storage counts both
    paths = _resolve_windows(env)
    assert d.log_file.parent == paths.logs_dir
    assert d.settings_file.parent == paths.config_file.parent
    rows = {loc.kind: loc.path for loc in locations(paths)}
    assert d.log_file.parent == rows[StorageKind.LOGS]
    assert d.settings_file.parent == rows[StorageKind.CONFIG]


@pytest.mark.parametrize(
    ("unset", "where"),
    [("APPDATA", "settings_file"), ("LOCALAPPDATA", "log_file")],
)
def test_windows_gui_files_fall_back_to_the_profile_like_the_core(
    tmp_path: Path, unset: str, where: str
) -> None:
    """Without the variable both answers fall back to the same folder under
    ``%USERPROFILE%``: the GUI never ends up somewhere the core does not look."""
    from buttereye.core.paths import _resolve_windows, gui_dirs

    env = _win_env(tmp_path)
    del env[unset]
    d = gui_dirs(env, macos=False, windows=True)
    paths = _resolve_windows(env)
    assert str(getattr(d, where)).startswith(str(tmp_path / "profile"))
    assert d.log_file.parent == paths.logs_dir
    assert d.settings_file.parent == paths.config_file.parent


def test_macos_wins_when_both_platform_flags_are_set(tmp_path: Path) -> None:
    from buttereye.core.paths import gui_dirs

    d = gui_dirs(_win_env(tmp_path), macos=True, windows=True)
    assert d.log_file == tmp_path / "home" / "Library" / "Logs" / "ButterEye" / "gui.log"


@pytest.mark.skipif(not osapi.IS_MACOS, reason="the macOS answer is the platform default")
def test_on_this_mac_the_gui_logs_with_the_core(tmp_path: Path) -> None:
    from buttereye.core.paths import gui_dirs
    from buttereye.core.storage import locations
    from buttereye.core.types import StorageKind

    env = _env(tmp_path)
    paths = resolve(env)
    assert gui_dirs(env).log_file.parent == paths.logs_dir
    assert gui_dirs(env).settings_file.parent == paths.config_file.parent
    # so the Storage page counts both, in its Logs and Settings rows
    rows = {loc.kind: loc.path for loc in locations(paths)}
    assert gui_dirs(env).log_file.parent == rows[StorageKind.LOGS]
    assert gui_dirs(env).settings_file.parent == rows[StorageKind.CONFIG]
