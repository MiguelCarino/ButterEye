# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""XDG path resolution and the runtime-directory safety check (SCOPE §4.6, §4.3).

``resolve()`` reads only the environment and ``lstat``s the runtime directory; it
never creates, writes or ``chmod``s anything. ``ensure_runtime_dir()`` creates
``$XDG_RUNTIME_DIR/buttereye`` (0700) on first use and verifies it again.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from buttereye.core.doctor.text import M
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.types import Paths

APP = "buttereye"


def _xdg(env: Mapping[str, str], var: str, home: Path, default: str) -> Path:
    """An XDG base dir. Relative values are invalid per the spec and ignored, and
    so are values with a ``..`` component (it can't be normalised without
    following symlinks, and storage cleaning must know where it points)."""
    value = env.get(var, "")
    if value and os.path.isabs(value) and ".." not in Path(value).parts:
        return Path(os.path.normpath(value))
    return home / default


def _unsafe(path: Path, why: str) -> ButterEyeError:
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
    if stat.S_ISLNK(st.st_mode):
        raise _unsafe(path, "it is a symbolic link")
    if not stat.S_ISDIR(st.st_mode):
        raise _unsafe(path, "it is not a folder")
    if st.st_uid != os.getuid():
        raise _unsafe(path, "it belongs to another user")
    if st.st_mode & 0o077:
        raise _unsafe(path, f"its permissions are {stat.S_IMODE(st.st_mode):04o}, not 0700")


def resolve(env: Mapping[str, str] | None = None) -> Paths:
    """Resolve every ButterEye path from ``env`` (default: ``os.environ``).

    Raises ``ButterEyeError(RUNTIME_DIR_UNSAFE)`` when ``XDG_RUNTIME_DIR`` is unset,
    relative or unsafe, or when an existing ``$XDG_RUNTIME_DIR/buttereye`` is not a
    0700 directory owned by this user.
    """
    e: Mapping[str, str] = os.environ if env is None else env
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


def ensure_runtime_dir(paths: Paths) -> Path:
    """Create ``paths.runtime_dir`` (0700) if missing, then verify it (BE-2010)."""
    try:
        paths.runtime_dir.mkdir(mode=0o700)
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


__all__ = ["APP", "resolve", "ensure_runtime_dir", "ensure_logs_dir", "check_private_dir"]
