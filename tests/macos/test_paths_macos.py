# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Where ButterEye puts its files on macOS (``buttereye.core.paths``, SCOPE §4.6).

Skipped everywhere but macOS. ``resolve()`` takes the environment as an argument,
so every test here hands it one it wrote itself: nothing patches ``os.environ``,
and the answers do not depend on how the test run was started.

The two things phase 1 settles. macOS sets no ``$XDG_RUNTIME_DIR``, which is
where the 0700 directory holding mpv's sockets comes from on Linux, so
``resolve()`` raised BE-2010 RUNTIME_DIR_UNSAFE on every call - including the one
``tests/integration/test_live_trt_real.py`` makes while being imported, which is
why CI had to skip that file's collection. And the user directories are not XDG's
on a Mac; they are under ``~/Library``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

if sys.platform == "darwin":
    from buttereye.core import paths
    from buttereye.core.mpvctl import session
    from buttereye.core.osapi import _darwin as d


#: the longest path ``bind`` accepts on macOS: ``sun_path`` is 104 bytes and one
#: of them is the terminator (Linux allows 108)
SUN_PATH_MAX = 103


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A home directory that exists but holds nothing: ``resolve()`` must build
    every path from it without reading or creating anything."""
    path = tmp_path / "home"
    path.mkdir()
    return path


# ---------------------------------------------------------------- the runtime directory
def test_the_user_temp_dir_is_private_and_is_not_slash_tmp() -> None:
    """``confstr(_CS_DARWIN_USER_TEMP_DIR)`` is the runtime directory's parent, and
    the only reason it can be is that it already satisfies the checks SCOPE §4.3
    demands of the directory sockets live in: 0700, ours, not a symlink.

    ``/tmp`` is the obvious alternative and it fails all of that - it is 1777 and
    shared with every account on the machine - so the test says which one this
    is, not merely that it is a directory.
    """
    temp = d.user_temp_dir()
    assert temp.is_absolute() and temp.is_dir()
    st = os.lstat(temp)
    assert not stat.S_ISLNK(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.getuid()
    assert temp != Path("/tmp")
    paths.check_private_dir(temp, must_exist=True)  # raises BE-2010 if it is not


def test_resolve_without_xdg_runtime_dir_falls_back_to_the_user_temp_dir(home: Path) -> None:
    """What every ``resolve()`` on a Mac does, since no session sets the variable.

    Before phase 1 this raised BE-2010 with "XDG_RUNTIME_DIR is not set", which
    is true on macOS and will stay true, so the message was not something a user
    could act on. ``$TMPDIR`` is not the answer either: ``env -i`` loses it, and
    the confstr call always answers.
    """
    resolved = paths.resolve({"HOME": str(home)})
    assert resolved.runtime_dir == d.user_temp_dir() / paths.APP
    # ensure_runtime_dir creates it without parents=True, so the parent has to
    # be there already - it is, which is what keeps that call unchanged
    assert resolved.runtime_dir.parent.is_dir()


def test_the_fallback_runtime_dir_leaves_room_for_an_mpv_socket(home: Path) -> None:
    """The constraint that actually bites, asserted in bytes.

    ``bind`` takes at most 103 bytes of path and says nothing useful when it is
    given more: the OSError surfaces in mpv's own log, not in ButterEye. The
    per-user temp directory measures 49 bytes here and the socket name is 21, so
    there is room twice over - which is the whole reason it was chosen over the
    ``$TMPDIR``-shaped paths pytest hands out, which are around 136.

    The name comes from ``session._new_sid()`` rather than a literal so that a
    longer session id would fail here instead of in mpv.
    """
    runtime_dir = paths.resolve({"HOME": str(home)}).runtime_dir
    socket_path = runtime_dir / f"mpv-{session._new_sid()}.sock"
    assert len(os.fsencode(socket_path)) <= SUN_PATH_MAX


def test_resolve_still_honours_xdg_runtime_dir_when_it_is_set(short_dir: Path) -> None:
    """Set, the variable still wins. Nothing on macOS sets it, but the test suite
    does (``tests/conftest.py``'s ``xdg_env``), and so could someone who wants
    the sockets somewhere else; the fallback is a fallback, not an override."""
    resolved = paths.resolve({"HOME": str(short_dir), "XDG_RUNTIME_DIR": str(short_dir)})
    assert resolved.runtime_dir == short_dir / paths.APP
    assert resolved.runtime_dir != d.user_temp_dir() / paths.APP
    assert paths.ensure_runtime_dir(resolved) == resolved.runtime_dir
    assert stat.S_IMODE(os.lstat(resolved.runtime_dir).st_mode) == 0o700


def test_an_unsafe_xdg_runtime_dir_is_still_refused(home: Path) -> None:
    """The fallback must not become a way around the check. ``/tmp`` is 1777, so
    a variable pointing at it is the shared directory SCOPE §4.3 refuses, and
    ``resolve()`` has to raise rather than quietly use the private one instead."""
    with pytest.raises(Exception) as err:
        paths.resolve({"HOME": str(home), "XDG_RUNTIME_DIR": "/tmp"})
    assert "BE-2010" in str(err.value)


# ---------------------------------------------------------------- the user directories
def test_the_user_directories_are_under_library(home: Path) -> None:
    """Apple's locations, not XDG's: a Mac user looking for an application's
    settings or caches looks in ``~/Library``, and nothing else on the system
    writes to ``~/.config`` or ``~/.cache`` for them to find it next to.

    The folder is named ``ButterEye`` here, as it is on Windows and unlike the
    lower-case ``buttereye`` of the XDG paths, because the name is shown to the
    user in Finder.

    Each role gets a folder of its own under that one base, ``config`` included.
    Nesting the settings is not cosmetic: ``storage.locations()`` builds its
    CONFIG row from ``config_file.parent``, so a ``config.toml`` sitting directly
    in the base would make that row -- and the storage page's total -- count
    every byte of the data and state folders beside it. The last assertion is
    that property, and it is what keeps the extra level from being tidied away.
    """
    resolved = paths.resolve({"HOME": str(home)})
    support = home / "Library" / "Application Support" / paths.MACOS_APP
    assert resolved.config_file == support / "config" / "config.toml"
    assert resolved.mpv_include == support / "config" / "mpv" / "buttereye.conf"
    assert resolved.cache_dir == home / "Library" / "Caches" / paths.MACOS_APP
    assert resolved.data_dir == support / "data"
    assert resolved.state_dir == support / "state"
    assert resolved.jobs_dir == support / "state" / "jobs"
    assert resolved.bench_file == support / "state" / "bench.json"
    config_dir = resolved.config_file.parent
    for enclosed in (resolved.data_dir, resolved.state_dir, resolved.jobs_dir, resolved.bench_file):
        assert not enclosed.is_relative_to(config_dir)


def test_the_logs_go_where_console_app_reads_them(home: Path) -> None:
    """``~/Library/Logs/ButterEye``, not the state folder's ``logs`` as on Linux.
    A log is the one thing a user is asked to hand over, and this is where macOS
    has them look for it; what matters for the test is that it is under
    ``~/Library`` and named for the application rather than back in
    ``~/.local/state`` or loose in the home directory."""
    resolved = paths.resolve({"HOME": str(home)})
    library = home / "Library"
    assert resolved.logs_dir == library / "Logs" / paths.MACOS_APP
    assert resolved.logs_dir.is_relative_to(library)
    assert paths.MACOS_APP in resolved.logs_dir.relative_to(library).parts


def test_the_users_own_mpv_config_stays_where_mpv_looks_for_it(home: Path) -> None:
    """``~/.config/mpv/mpv.conf``, which is not a ~/Library path and must not
    become one: it is the user's file, read by mpv, and mpv 0.41.0's manual says
    so for macOS in as many words ("FILES ON MACOS": only the watch-later and
    cache directories differ, "everything else is the same as FILES", where the
    configuration directory is ``~/.config/mpv`` unless ``$XDG_CONFIG_HOME`` or
    ``$MPV_HOME`` moves it). ButterEye only reads this file, to warn about
    settings that fight interpolation; it is never written."""
    resolved = paths.resolve({"HOME": str(home)})
    assert resolved.user_mpv_conf == home / ".config" / "mpv" / "mpv.conf"
    moved = paths.resolve({"HOME": str(home), "MPV_HOME": str(home / "mpv-elsewhere")})
    assert moved.user_mpv_conf == home / "mpv-elsewhere" / "mpv.conf"
