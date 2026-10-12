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

On macOS the folders are the ones a Mac application uses: settings, data and
state in a folder each (``config``, ``data``, ``state``) under
``~/Library/Application Support/ButterEye``, caches under
``~/Library/Caches/ButterEye``, logs under ``~/Library/Logs/ButterEye`` (where
Console.app looks), and the runtime folder under the per-user temporary
directory the system makes at login, because macOS sets no ``XDG_RUNTIME_DIR``.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from buttereye.core import osapi
from buttereye.core.doctor.text import M
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.types import Paths

if sys.platform == "darwin":
    from buttereye.core.osapi import _darwin

APP = "buttereye"
#: the folder name under %APPDATA% / %LOCALAPPDATA% (Windows convention: the product name)
WINDOWS_APP = "ButterEye"
#: the folder name under ~/Library (macOS convention: the product name, as on Windows)
MACOS_APP = "ButterEye"


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
    0700 directory owned by this user. macOS has no ``XDG_RUNTIME_DIR`` to be
    unset: without it ``_resolve_macos`` uses the system's per-user temporary
    directory, which is private already.
    """
    e: Mapping[str, str] = os.environ if env is None else env
    if osapi.IS_WINDOWS:
        return _resolve_windows(e)
    if osapi.IS_MACOS:
        return _resolve_macos(e)
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


def _macos_runtime_base(e: Mapping[str, str]) -> Path:
    """The folder the macOS runtime folder goes in: ``XDG_RUNTIME_DIR`` when
    something has set it, else this user's temporary directory.

    macOS never sets the variable itself, so the second answer is the normal one;
    the first is kept because a test and the macOS CI job have to be able to put
    the sockets somewhere of their own choosing. What the system supplies is
    ``confstr(_CS_DARWIN_USER_TEMP_DIR)``, a 0700 directory under ``/var/folders``
    owned by this uid: ``check_private_dir`` applies to it unchanged. It is also
    49 bytes long, which is the reason to prefer it -- macOS binds at most 103
    bytes of ``sun_path`` where Linux takes 107, a session socket under it
    measures 80, and a ``$TMPDIR``-rooted pytest directory is already 136.

    Its one cost, which is not hidden: ``dirhelper`` runs daily and deletes files
    there that are more than three days old, so a session left attached for longer
    could in principle lose its socket.

    Raises ``ButterEyeError(RUNTIME_DIR_UNSAFE)`` when ``confstr`` answers with
    nothing or with something that is not an absolute path, which is the place
    Linux raises it for an unset ``XDG_RUNTIME_DIR``: BE-2010 stays the only way
    this can fail, rather than an ``OSError`` no caller above here handles.
    """
    run_s = e.get("XDG_RUNTIME_DIR", "")
    if run_s and os.path.isabs(run_s):
        return Path(run_s)
    if sys.platform == "darwin":
        try:
            return _darwin.user_temp_dir()
        except OSError as exc:
            raise ButterEyeError(
                ErrorCode.RUNTIME_DIR_UNSAFE,
                M(
                    "macOS did not name a private folder for ButterEye's sockets: {why}.",
                    {"why": exc.strerror or str(exc)},
                ),  # fmt: skip
                M("Log out and back in, so the system creates it again."),
            ) from None
    raise AssertionError("the macOS runtime folder was asked for on another platform")


def _resolve_macos(e: Mapping[str, str]) -> Paths:
    """macOS: one folder per role under ``~/Library``, as on Windows, and a runtime
    folder the system supplies instead of ``$XDG_RUNTIME_DIR``
    (``_macos_runtime_base``). The XDG variables are not read for ButterEye's own
    folders here: a Mac user who has them exported for other tools did not ask
    ButterEye to move out of ``~/Library``.
    """
    home_s = e.get("HOME", "")
    home = Path(home_s) if home_s and os.path.isabs(home_s) else Path(os.path.expanduser("~"))
    library = home / "Library"
    base = library / "Application Support" / MACOS_APP
    # mpv on macOS reads ~/.config/mpv like everywhere else, and honours MPV_HOME
    # and XDG_CONFIG_HOME there too (mpv(1), "FILES ON MACOS": only the cache and
    # watch_later move to ~/Library). So this one folder stays XDG, not Library.
    mpv_home = e.get("MPV_HOME", "")
    mpv_dir = (
        Path(mpv_home)
        if mpv_home and os.path.isabs(mpv_home)
        else _xdg(e, "XDG_CONFIG_HOME", home, ".config") / "mpv"
    )

    run = _macos_runtime_base(e)
    check_private_dir(run, must_exist=True)
    runtime_dir = run / APP
    check_private_dir(runtime_dir, must_exist=False)

    state_dir = base / "state"
    # config in a folder of its own, not at the root of base: storage.locations()
    # builds its CONFIG row from config_file.parent, and base is the parent of the
    # data, state and jobs rows too, so a config.toml sitting directly in base
    # would make that row report every byte of them -- a user with 10 GiB of
    # models would read "Settings, profiles and rules: 10 GiB" and a doubled
    # total. Linux and Windows keep config under a separate root entirely, so the
    # extra level is also what makes the three layouts the same shape.
    config_dir = base / "config"
    return Paths(
        config_file=config_dir / "config.toml",
        mpv_include=config_dir / "mpv" / "buttereye.conf",
        user_mpv_conf=mpv_dir / "mpv.conf",
        data_dir=base / "data",
        cache_dir=library / "Caches" / MACOS_APP,
        state_dir=state_dir,
        # not state_dir/logs as on Linux: ~/Library/Logs is the one place
        # Console.app reads, and a log is the thing a user is asked to hand over
        logs_dir=library / "Logs" / MACOS_APP,
        jobs_dir=state_dir / "jobs",
        bench_file=state_dir / "bench.json",
        runtime_dir=runtime_dir,
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
    "MACOS_APP",
    "WINDOWS_APP",
    "install_dir",
    "resolve",
    "ensure_runtime_dir",
    "ensure_logs_dir",
    "check_private_dir",
]
