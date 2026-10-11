# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The operating-system seam: every call that differs between Linux and Windows.

The rest of the core asks this module instead of reaching for POSIX directly, so
the Linux behaviour stays exactly what SCOPE §4.3/§4.6/§2.4 describe and Windows
gets the nearest equivalent (docs/PLATFORMS.md):

- process group: Linux runs the child in a new session (pgid = leader pid) and
  sends SIGTERM, then SIGKILL. Windows uses the leader pid and its process tree;
  ButterEye sits in a kill-on-close job so its helpers end with it, and "soft"
  means WM_CLOSE when the tree has windows, else terminate at once.
- mpv lifetime: ``posix_spawnp(setsid)`` and a pidfd watch on Linux; on Windows a
  ``Popen`` that breaks away from the job, polled.
- mpv control: Unix socket + ``SO_PEERCRED`` on Linux; on Windows a named pipe,
  checked by its server pid and that process's user SID, with a marker file in
  the runtime folder standing for the socket (discovery, cleanup).
- private folder: 0700, own uid, not a symlink on Linux; on Windows owned by this
  user's SID and not a reparse point (it lives under %LOCALAPPDATA%, per-user by ACL).
- locks: ``flock`` on Linux; ``msvcrt.locking`` on byte 0 of a lock file on Windows.

mypy checks both halves: ``mypy`` and ``mypy --platform win32``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"

#: open() flags that exist only on POSIX; 0 where they don't (Windows handles are
#: not inherited unless asked for, and there are no symlinked-path races to stop
#: with O_NOFOLLOW in a per-user folder)
O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_BINARY: int = getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import msvcrt

    from buttereye.core.osapi import _win32 as w
else:
    import fcntl


# ======================================================================== files
def is_link(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (junction, mount point)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return IS_WINDOWS and bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def owned_by_me(path: Path, st: os.stat_result) -> bool:
    """``path`` (already ``lstat``-ed as ``st``) belongs to this user."""
    if sys.platform == "win32":
        me = w.my_sid()
        return me is not None and w.file_owner_sid(path) == me
    return st.st_uid == os.getuid()


def shared_mode_bits(st: os.stat_result) -> int:
    """Group/other permission bits (0: private). Windows has no mode bits to
    read; privacy there comes from the per-user profile folder's ACL."""
    if IS_WINDOWS:
        return 0
    return stat.S_IMODE(st.st_mode) & 0o077


def set_mode(fd: int, mode: int) -> None:
    """``fchmod`` where modes mean something (not Windows)."""
    if sys.platform != "win32":
        os.fchmod(fd, mode)


def fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable (POSIX); Windows can't open folders."""
    if sys.platform == "win32":
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def replace(src: Path | str, dst: Path | str) -> None:
    """``os.replace``; on Windows retried briefly, because a reader holding the
    target open (an editor, the virus scanner) makes the rename fail for a moment."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def try_lock(fd: int) -> bool:
    """Exclusive lock on ``fd`` without waiting; False if someone else holds it."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def lock(fd: int) -> None:
    """Exclusive lock on ``fd``, waiting as long as it takes."""
    if sys.platform == "win32":
        while not try_lock(fd):
            time.sleep(0.02)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


def unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_dir(directory: Path) -> Iterator[None]:
    """Serialise writers of ``directory``: ``flock`` on the folder itself on POSIX,
    on ``<directory>/.lock`` on Windows (folders can't be opened there)."""
    if IS_WINDOWS:
        fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
    else:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | O_CLOEXEC)
    try:
        lock(fd)
        yield
    finally:
        os.close(fd)  # closing releases the lock


# ======================================================================== libraries
def plugin_file(linux_name: str) -> str:
    """A VapourSynth plugin's file name on this system. Plugins are named for Linux
    (``librife.so``); the Windows builds use the same stem with ``.dll``."""
    if IS_WINDOWS and linux_name.endswith(".so"):
        return linux_name[:-3] + ".dll"
    return linux_name


#: vs-mlrt's TensorRT plugin: ``vstrt.dll`` is the name its Windows releases use
VSTRT_LIB = "vstrt.dll" if IS_WINDOWS else "libvstrt.so"


# ======================================================================== processes
def executable(name: str) -> str | None:
    """``shutil.which`` that prefers the windowed ``.exe`` on Windows: mpv ships
    ``mpv.com`` (a console wrapper) next to ``mpv.exe``, and PATHEXT finds the
    ``.com`` first."""
    if IS_WINDOWS and not os.path.splitext(name)[1]:
        found = shutil.which(name + ".exe")
        if found is not None:
            return found
    return shutil.which(name)


def group_spawn_kwargs(*, low_priority: bool = False) -> dict[str, Any]:
    """``subprocess``/``asyncio`` keyword arguments that make the child lead its
    own process group (§2.4). On Windows: its own console group, no console
    window, and inside ButterEye's kill-on-close job; ``low_priority`` asks for
    below-normal priority from the start (POSIX renices after the spawn)."""
    if sys.platform == "win32":
        w.ensure_job()
        flags = w.CREATE_NEW_PROCESS_GROUP | w.CREATE_NO_WINDOW
        if low_priority:
            flags |= w.BELOW_NORMAL_PRIORITY_CLASS
        return {"creationflags": flags}
    return {"start_new_session": True}


def lower_group_priority(pgid: int, nice: int) -> None:
    """Renice a whole POSIX process group; Windows set it at spawn already."""
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PGRP, pgid, nice)


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return w.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError, IndexError:
        return True
    return state not in ("Z", "X")


def group_alive(pgid: int | None) -> bool:
    """Is any process of the group led by ``pgid`` still there? On Windows the
    leader stands for its tree (a leader that exited leaves its children to the
    kill-on-close job)."""
    if not pgid:
        return False
    if sys.platform == "win32":
        return w.pid_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, *, kill: bool) -> None:
    """End the group led by ``pgid``: SIGTERM (``kill=False``) or SIGKILL.

    Windows has no SIGTERM. The polite form closes the tree's windows (mpv then
    quits as if the user closed it); a tree without windows (vspipe, ffmpeg,
    probes) is terminated at once, since there is nothing to ask. Never raises
    for a group that is already gone."""
    if sys.platform == "win32":
        pids = w.tree(pgid)
        if not kill and w.post_close(set(pids)):
            return
        for pid in pids:
            w.terminate(pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL if kill else signal.SIGTERM)


# ======================================================================== mpv
class DetachedChild:
    """A process started to outlive ButterEye (mpv). ``poll`` reaps it once it
    has exited: the exit code, negative signal number when killed (POSIX)."""

    def __init__(self, pid: int, popen: subprocess.Popen[bytes] | None = None) -> None:
        self.pid = pid
        self._popen = popen

    def open_pidfd(self) -> int | None:
        """A new pidfd the event loop can watch (Linux), else None: poll instead."""
        if sys.platform == "win32" or not hasattr(os, "pidfd_open"):
            return None
        try:
            return os.pidfd_open(self.pid)
        except OSError:
            return None

    def poll(self) -> int | None:
        if self._popen is not None:
            return self._popen.poll()
        if sys.platform == "win32":
            return None  # unreachable: Windows children always come with a Popen
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # reaped elsewhere (or not our child): judge by existence
            return None if pid_alive(self.pid) else -1
        if pid == 0:
            return None
        return os.waitstatus_to_exitcode(status)


def spawn_detached(
    argv: Sequence[str], log_path: Path, *, env: Mapping[str, str] | None = None
) -> DetachedChild:
    """Start ``argv`` so that it outlives ButterEye: stdin from the null device,
    stdout+stderr appended to ``log_path``, no other descriptor or handle
    inherited. POSIX: ``posix_spawnp`` with ``setsid`` (no asyncio transport or
    ``Popen`` finaliser can kill it). Windows: ``Popen`` that breaks away from
    ButterEye's job; if a parent job forbids that, mpv starts inside it and a
    warning says it will close with ButterEye. Raises ``FileNotFoundError``."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_CLOEXEC | O_BINARY, 0o600)
    try:
        if sys.platform == "win32":
            base = w.CREATE_NEW_PROCESS_GROUP | w.CREATE_NO_WINDOW
            for flags in (base | w.CREATE_BREAKAWAY_FROM_JOB, base):
                try:
                    p = subprocess.Popen(
                        list(argv), stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                        env=dict(env) if env is not None else None, close_fds=True,
                        creationflags=flags,
                    )  # fmt: skip
                except PermissionError:
                    if flags & w.CREATE_BREAKAWAY_FROM_JOB:
                        import logging

                        logging.getLogger(__name__).warning(
                            "Windows refused to let mpv leave ButterEye's job; "
                            "mpv will close when ButterEye does"
                        )
                        continue
                    raise
                return DetachedChild(p.pid, p)
            raise AssertionError("unreachable")
        pid = os.posix_spawnp(
            argv[0],
            list(argv),
            dict(env) if env is not None else dict(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                (os.POSIX_SPAWN_DUP2, fd, 1),
                (os.POSIX_SPAWN_DUP2, fd, 2),
            ],
            setsid=True,
        )
        return DetachedChild(pid)
    finally:
        os.close(fd)


# ======================================================================== mpv control channel
def ipc_address(endpoint: Path) -> str:
    """What mpv's ``--input-ipc-server`` gets for ``endpoint``: the socket path on
    POSIX; on Windows a named pipe whose name is derived from the endpoint path
    (which already names this user's profile folder, so users never collide)."""
    if not IS_WINDOWS:
        return os.fspath(endpoint)
    digest = hashlib.sha256(os.fspath(endpoint).lower().encode("utf-8")).hexdigest()[:24]
    return rf"\\.\pipe\buttereye-{digest}"


def is_endpoint(st: os.stat_result) -> bool:
    """The runtime-folder entry for a session: a socket (POSIX) or the marker
    file standing for the pipe (Windows)."""
    return stat.S_ISREG(st.st_mode) if IS_WINDOWS else stat.S_ISSOCK(st.st_mode)


def prepare_endpoint(endpoint: Path) -> None:
    """Windows: create the marker file for the pipe (mpv creates the pipe itself),
    so discovery and cleanup find the session the way they find a socket."""
    if IS_WINDOWS:
        fd = os.open(endpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
        os.close(fd)


async def open_ipc(
    endpoint: Path, *, limit: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to mpv's control channel. A pipe or socket nobody serves raises
    ``FileNotFoundError`` / ``ConnectionRefusedError``."""
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=limit, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        create = getattr(loop, "create_pipe_connection", None)
        if create is None:  # a selector loop: named pipes need the proactor
            raise ConnectionError("named pipes need the Windows proactor event loop")
        transport, _ = await create(lambda: protocol, ipc_address(endpoint))
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
    return await asyncio.open_unix_connection(os.fspath(endpoint), limit=limit)


def ipc_peer(writer: asyncio.StreamWriter) -> tuple[int | None, bool] | None:
    """``(pid, same_user)`` of the process serving the connection, or None when it
    can't be told (then the caller refuses). POSIX: ``SO_PEERCRED``. Windows:
    the pipe server's pid and the user its process runs as."""
    if sys.platform == "win32":
        pipe = writer.get_extra_info("pipe")
        handle = getattr(pipe, "handle", None)
        if handle is None:
            return None
        pid = w.pipe_server_pid(int(handle))
        if pid is None:
            return None
        me = w.my_sid()
        return pid, me is not None and w.process_sid(pid) == me
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    return (pid if pid > 0 else None), uid == os.getuid()


__all__ = [
    "IS_WINDOWS",
    "VSTRT_LIB",
    "O_BINARY",
    "O_CLOEXEC",
    "O_NOFOLLOW",
    "DetachedChild",
    "executable",
    "fsync_dir",
    "group_alive",
    "group_spawn_kwargs",
    "ipc_address",
    "ipc_peer",
    "is_endpoint",
    "is_link",
    "lock",
    "locked_dir",
    "lower_group_priority",
    "open_ipc",
    "owned_by_me",
    "pid_alive",
    "plugin_file",
    "prepare_endpoint",
    "replace",
    "set_mode",
    "shared_mode_bits",
    "signal_group",
    "spawn_detached",
    "try_lock",
    "unlock",
]
