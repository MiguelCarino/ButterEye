# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""XDG path resolution and the runtime-directory safety check (SCOPE §4.6, §4.3).

``resolve()`` reads only the environment and ``lstat``s the runtime directory; it
never creates, writes or ``chmod``s anything. ``ensure_runtime_dir()`` creates
``$XDG_RUNTIME_DIR/buttereye`` (0700) on first use and verifies it again.

On Windows (docs/PLATFORMS.md) the same folders live under the user's profile:
settings in ``%APPDATA%\\ButterEye``, everything else in
``%LOCALAPPDATA%\\ButterEye`` (``data``, ``cache``, ``state``, and ``run`` for the
runtime folder, which Windows has no XDG equivalent of).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from buttereye.core import osapi
from buttereye.core.doctor.text import M
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.types import Paths

APP = "buttereye"
#: the folder name under %APPDATA% / %LOCALAPPDATA% (Windows convention: the product name)
WINDOWS_APP = "ButterEye"


def _xdg(env: Mapping[str, str], var: str, home: Path, default: str) -> Path:
    """An XDG base dir. Relative values are invalid per the spec and ignored, and
    so are values with a ``..`` component (it can't be normalised without
    following symlinks, and storage cleaning must know where it points)."""
    value = env.get(var, "")
    if value and os.path.isabs(value) and ".." not in Path(value).parts:
        return Path(os.path.normpath(value))
    return home / default


def _unsafe(path: Path, why: str) -> ButterEyeError:
    if osapi.IS_WINDOWS:
        return ButterEyeError(
            ErrorCode.RUNTIME_DIR_UNSAFE,
            M(
                "The runtime folder {path} is not safe to use: {why}.",
                {"path": str(path), "why": why},
            ),  # fmt: skip
            M(
                "Make sure it is a normal folder owned by your account, not a link "
                "(for example: delete it and start ButterEye again). ButterEye never "
                "changes it itself."
            ),
        )
    return ButterEyeError(
        ErrorCode.RUNTIME_DIR_UNSAFE,
        M("The runtime folder {path} is not safe to use: {why}.", {"path": str(path), "why": why}),
        M(
            "Make sure it is a folder owned by you with permissions 0700 "
            "(for example: log out and back in). ButterEye never changes it itself."
        ),
        commands=(f"ls -ld {path}",),
    )


def check_private_dir(path: Path, *, must_exist: bool) -> None:
    """Raise ``BE-2010`` unless ``path`` is a real directory owned by this uid with
    no group/other permissions. Never changes the directory."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if must_exist:
            raise _unsafe(path, "it does not exist") from None
        return
    except OSError as exc:
        raise _unsafe(path, f"it cannot be inspected ({exc.strerror})") from None
    if osapi.is_link(st):
        raise _unsafe(path, "it is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(path, "it is not a folder")
    if not osapi.owned_by_me(path, st):
        raise _unsafe(path, "it belongs to another user")
    if osapi.shared_mode_bits(st):
        raise _unsafe(path, f"its permissions are {stat.S_IMODE(st.st_mode):04o}, not 0700")


def resolve(env: Mapping[str, str] | None = None) -> Paths:
    """Resolve every ButterEye path from ``env`` (default: ``os.environ``).

    Raises ``ButterEyeError(RUNTIME_DIR_UNSAFE)`` when ``XDG_RUNTIME_DIR`` is unset,
    relative or unsafe, or when an existing ``$XDG_RUNTIME_DIR/buttereye`` is not a
    0700 directory owned by this user.
    """
    e: Mapping[str, str] = os.environ if env is None else env
    if osapi.IS_WINDOWS:
        return _resolve_windows(e)
    home_s = e.get("HOME", "")
    home = Path(home_s) if home_s and os.path.isabs(home_s) else Path(os.path.expanduser("~"))
    config = _xdg(e, "XDG_CONFIG_HOME", home, ".config")
    data = _xdg(e, "XDG_DATA_HOME", home, ".local/share")
    cache = _xdg(e, "XDG_CACHE_HOME", home, ".cache")
    state = _xdg(e, "XDG_STATE_HOME", home, ".local/state")

    mpv_home = e.get("MPV_HOME", "")
    mpv_dir = Path(mpv_home) if mpv_home and os.path.isabs(mpv_home) else config / "mpv"

    run_s = e.get("XDG_RUNTIME_DIR", "")
    if not run_s or not os.path.isabs(run_s):
        raise ButterEyeError(
            ErrorCode.RUNTIME_DIR_UNSAFE,
            M("XDG_RUNTIME_DIR is not set, so ButterEye has no private folder for its sockets."),
            M("Start ButterEye from a normal desktop or login session."),
        )
    run = Path(run_s)
    check_private_dir(run, must_exist=True)
    runtime_dir = run / APP
    check_private_dir(runtime_dir, must_exist=False)

    state_dir = state / APP
    return Paths(
        config_file=config / APP / "config.toml",
        mpv_include=config / APP / "mpv" / "buttereye.conf",
        user_mpv_conf=mpv_dir / "mpv.conf",
        data_dir=data / APP,
        cache_dir=cache / APP,
        state_dir=state_dir,
        logs_dir=state_dir / "logs",
        jobs_dir=state_dir / "jobs",
        bench_file=state_dir / "bench.json",
        runtime_dir=runtime_dir,
    )


def _resolve_windows(e: Mapping[str, str]) -> Paths:
    """Windows: %APPDATA% (roams) for settings, %LOCALAPPDATA% for the rest. The
    runtime folder needs no session variable: it is a folder of this user's
    profile, created on first use and checked like the Linux one."""
    home_s = e.get("USERPROFILE", "")
    home = Path(home_s) if home_s and os.path.isabs(home_s) else Path(os.path.expanduser("~"))
    roaming = _xdg(e, "APPDATA", home, "AppData/Roaming")
    local = _xdg(e, "LOCALAPPDATA", home, "AppData/Local")
    config = roaming / WINDOWS_APP
    base = local / WINDOWS_APP
    mpv_home = e.get("MPV_HOME", "")
    mpv_dir = Path(mpv_home) if mpv_home and os.path.isabs(mpv_home) else roaming / "mpv"
    runtime_dir = base / "run"
    check_private_dir(runtime_dir, must_exist=False)
    state_dir = base / "state"
    install = install_dir()
    return Paths(
        config_file=config / "config.toml",
        mpv_include=config / "mpv" / "buttereye.conf",
        user_mpv_conf=mpv_dir / "mpv.conf",
        data_dir=base / "data",
        cache_dir=base / "cache",
        state_dir=state_dir,
        logs_dir=state_dir / "logs",
        jobs_dir=state_dir / "jobs",
        bench_file=state_dir / "bench.json",
        runtime_dir=runtime_dir,
        rpm_plugin_dir=install / "vapoursynth",
        rpm_data_dir=install / "share",
    )


def install_dir() -> Path:
    """Windows: the folder ButterEye is installed in (its plugins and models sit
    next to it, where Linux has /usr/lib64 and /usr/share). ``BUTTEREYE_HOME``
    overrides; otherwise the folder above the ``buttereye`` package."""
    home = os.environ.get("BUTTEREYE_HOME", "")
    if home and os.path.isabs(home):
        return Path(home)
    return Path(__file__).resolve().parents[2]


def ensure_runtime_dir(paths: Paths) -> Path:
    """Create ``paths.runtime_dir`` (0700) if missing, then verify it (BE-2010)."""
    try:
        paths.runtime_dir.mkdir(mode=0o700, parents=osapi.IS_WINDOWS)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _unsafe(paths.runtime_dir, f"it cannot be created ({exc.strerror})") from None
    check_private_dir(paths.runtime_dir, must_exist=True)
    return paths.runtime_dir


def ensure_logs_dir(paths: Paths) -> Path:
    """Create the logs folder (the only thing doctor/setup may write before the end)."""
    paths.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    return paths.logs_dir


__all__ = [
    "APP",
    "WINDOWS_APP",
    "install_dir",
    "resolve",
    "ensure_runtime_dir",
    "ensure_logs_dir",
    "check_private_dir",
]
