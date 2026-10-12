# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The operating-system seam: every call that differs between Linux, Windows and macOS.

The rest of the core asks this module instead of reaching for POSIX directly, so
the Linux behaviour stays exactly what SCOPE §4.3/§4.6/§2.4 describe and Windows
gets the nearest equivalent (docs/PLATFORMS.md). macOS is POSIX, so it takes the
Linux path everywhere the rows below do not say otherwise:

- process group: Linux runs the child in a new session (pgid = leader pid) and
  sends SIGTERM, then SIGKILL. Windows uses the leader pid and its process tree;
  ButterEye sits in a kill-on-close job so its helpers end with it, and "soft"
  means WM_CLOSE when the tree has windows, else terminate at once.
- mpv lifetime: ``posix_spawnp(setsid)`` and a pidfd watch on Linux; on Windows a
  ``Popen`` that breaks away from the job, polled. macOS spawns as Linux does and
  has no pidfd, but a kqueue watching ``EVFILT_PROC``/``NOTE_EXIT`` is the same
  watchable descriptor (``_darwin.exit_watch``), so it does not poll either.
- is a process still there: on POSIX it takes two questions, because a process that
  has exited and has not been reaped still answers ``os.kill(pid, 0)``. Linux reads
  the state out of ``/proc/<pid>/stat``; macOS has no ``/proc`` and asks libproc
  (``_darwin.pid_alive``). Windows reads the exit code of a process handle.
- mpv control: Unix socket + ``SO_PEERCRED`` on Linux. macOS keeps the socket but
  has no ``SO_PEERCRED``: the peer's uid comes from ``LOCAL_PEERCRED`` and its pid
  from ``LOCAL_PEERPID`` (``_darwin.peer_cred``), and a peer that has already gone
  reads as ``ConnectionResetError`` rather than an answer from the kernel's cache.
  On Windows a named pipe, checked by its server pid and that process's user SID,
  with a marker file in the runtime folder standing for the socket (discovery,
  cleanup).
- private folder: 0700, own uid, not a symlink on Linux; on Windows owned by this
  user's SID and not a reparse point (it lives under %LOCALAPPDATA%, per-user by ACL).
  macOS checks it as Linux does, but has no ``$XDG_RUNTIME_DIR`` to put it in: the
  kernel's per-user temporary directory stands in (``_darwin.user_temp_dir``).
- locks: ``flock`` on Linux and macOS; ``msvcrt.locking`` on byte 0 of a lock file
  on Windows.
- durable renames: ``fsync`` on the folder, and on macOS ``F_FULLFSYNC`` as well,
  where ``fsync`` alone stops at the drive's own cache; Windows cannot open a folder
  and leaves the ordering to NTFS (``fsync_dir``).
- plugin files: ``mvtools.so`` on Linux, the same stem with ``.dll`` on Windows and
  ``.dylib`` on macOS (``plugin_file``). TensorRT's plugin has no macOS build and
  never will, so ``HAS_VSTRT`` is False there.
- VapourSynth for child processes: Linux has the distribution's; on Windows the
  installer bundles Python with VapourSynth, and ``use_bundled_runtime`` points
  every child (mpv, vspipe) at it.

mypy checks all three: ``mypy --platform linux``, ``mypy --platform win32``
and ``mypy --platform darwin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import ntpath
import os
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

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

    if sys.platform == "darwin":
        from buttereye.core.osapi import _darwin as d


# ======================================================================== files
def is_link(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (junction, mount point)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return IS_WINDOWS and bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def owned_by_me(path: Path, st: os.stat_result) -> bool:
    """``path`` (already ``lstat``-ed as ``st``) belongs to this user. On Windows
    its owner is this user, or the owner this process gives what it creates: for
    an administrator on Windows Server, or elevated, that is the Administrators
    group, so a folder ButterEye just made would otherwise not count as its own."""
    if sys.platform == "win32":
        mine = {w.my_sid(), w.my_owner_sid()} - {None}
        return bool(mine) and w.file_owner_sid(path) in mine
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
    """Make a rename in ``directory`` durable (POSIX); Windows can't open folders.

    On macOS ``fsync`` only hands the data to the drive, which may still be
    holding it in a volatile cache; ``F_FULLFSYNC`` is the call that waits for
    the platter or the flash. Without it a profile saved seconds before the power
    went could come back as the old one -- never as a damaged one, since the
    rename itself stays atomic."""
    if sys.platform == "win32":
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
        if sys.platform == "darwin":
            with contextlib.suppress(OSError):
                fcntl.fcntl(dfd, fcntl.F_FULLFSYNC, 0)
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
    (``librife.so``); the Windows builds use the same stem with ``.dll``, the macOS
    ones with ``.dylib``. The extension is VapourSynth's own, not a convention: R81
    fixes it per platform in ``src/core/vscore.cpp`` and uses it both for a manifest
    load and to filter the autoload scan, so a plugin under any other name is unseen.
    Only the extension changes -- Homebrew's are ``mvtools.dylib`` and
    ``libffms2.dylib``, the same stems Linux has."""
    if IS_WINDOWS and linux_name.endswith(".so"):
        return linux_name[:-3] + ".dll"
    if IS_MACOS and linux_name.endswith(".so"):
        return linux_name[:-3] + ".dylib"
    return linux_name


#: vs-mlrt's TensorRT plugin: ``vstrt.dll`` is the name its Windows releases use
VSTRT_LIB = "vstrt.dll" if IS_WINDOWS else "libvstrt.so"

#: Can a TensorRT backend exist on this system at all? Not on macOS: TensorRT needs
#: CUDA, no Apple Silicon Mac has an NVIDIA GPU, and that is permanent rather than
#: pending (docs/PLATFORMS.md). Nothing is ever found under ``VSTRT_LIB`` there, so
#: callers must ask this before they look. It is a flag and not ``VSTRT_LIB = None``
#: because every caller joins that name onto a path, and making it optional moves
#: the type error into files this seam has no business editing.
HAS_VSTRT = not IS_MACOS


# ======================================================================== bundled runtime
def runtime_dirs() -> tuple[str, str | None]:
    """(the folder of this Python, the folder of its ``vapoursynth`` package or
    None). Looks the package up without importing it: the GUI process must not
    load VapourSynth (SCOPE §8.1)."""
    try:
        spec = importlib.util.find_spec("vapoursynth")
    except ImportError, ValueError:
        spec = None
    vs_dir = os.path.dirname(spec.origin) if spec is not None and spec.origin else None
    return os.path.dirname(sys.executable), vs_dir


def point_at_runtime(
    env: MutableMapping[str, str], python_dir: str, vs_dir: str | None, *, sep: str = os.pathsep
) -> None:
    """Make ``env`` lead children to this Python's VapourSynth (Windows):

    - ``VSSCRIPT_PATH``: Windows mpv builds load VSScript at run time from this
      variable (else the registry, else the DLL search), so without it mpv finds
      no VapourSynth or someone else's;
    - ``PATH`` starts with the Python folder (``vsscript.dll`` loads ``python3.dll``,
      which forwards to ``python3XX.dll`` next to it) and the package folder
      (``vspipe.exe``), so ``vspipe`` resolves to the bundled one everywhere.

    Idempotent: folders already on PATH move to the front instead of repeating."""
    first = [python_dir] + ([vs_dir] if vs_dir else [])
    if vs_dir is not None:
        vsscript = os.path.join(vs_dir, "vsscript.dll")
        if os.path.isfile(vsscript):
            env["VSSCRIPT_PATH"] = vsscript

    def key(p: str) -> str:
        return os.path.normcase(p.rstrip("\\/"))

    lead = {key(p) for p in first}
    rest = [p for p in env.get("PATH", "").split(sep) if p and key(p) not in lead]
    env["PATH"] = sep.join(first + rest)


#: Windows: with this set, ``CreateProcess`` and ``shutil.which`` stop looking for a
#: bare program name in the current folder first (a ``vspipe.exe`` or ``mpv.exe``
#: sitting in Downloads would otherwise win over the bundled one or PATH's)
NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"


def use_bundled_runtime() -> None:
    """Windows: point every child process at the VapourSynth of the Python that
    runs ButterEye (the installer's bundled runtime), through ``os.environ``
    (``point_at_runtime``), and take the current folder out of the program search
    (``NO_CWD_SEARCH``, inherited by the children). Nothing anywhere else: Linux
    uses the system's."""
    if not IS_WINDOWS:
        return
    os.environ[NO_CWD_SEARCH] = "1"
    python_dir, vs_dir = runtime_dirs()
    point_at_runtime(os.environ, python_dir, vs_dir)
    hold_install_mutex()


#: the installer's record, in the folder it installed into (next to ``python``)
INSTALL_RECORD = "buttereye-install.json"


def install_root() -> str | None:
    """Windows: the folder the installer put this Python in (the parent of the
    ``python`` folder, with the install record), else None."""
    python_dir, _ = runtime_dirs()
    root = os.path.dirname(python_dir)
    return root if os.path.isfile(os.path.join(root, INSTALL_RECORD)) else None


def install_mutex_name(folder: str) -> str:
    """The named mutex an installed ButterEye holds while it runs from ``folder``
    (its long path). The installer and uninstaller build the same name from
    ``$INSTDIR`` (packaging/windows/buttereye.nsi, ``MutexName``) and won't
    change files a running ButterEye uses: the path in lower case with ``/`` for
    ``\\``, since a mutex name can't hold a backslash; ``Local\\``: this session."""
    return "Local\\ButterEye-" + ntpath.normpath(folder).lower().replace("\\", "/")


def hold_install_mutex() -> None:
    """Windows: hold ``install_mutex_name`` of this installation for the life of
    the process (the GUI's and the command line's core both open through
    ``use_bundled_runtime``). Nothing outside an installed copy, or on Linux."""
    if sys.platform == "win32":
        root = install_root()
        if root is not None:
            w.hold_mutex(install_mutex_name(w.long_path(root)))


def bundled_vspipe() -> str | None:
    """Windows: ``vspipe.exe`` of the running Python's VapourSynth package (the
    installer's), by its full path, else None. Never searched for by name. Linux:
    None (the distribution's vspipe, from PATH)."""
    if not IS_WINDOWS:
        return None
    _, vs_dir = runtime_dirs()
    if vs_dir is None:
        return None
    exe = os.path.join(vs_dir, "vspipe.exe")
    return exe if os.path.isabs(exe) and os.path.isfile(exe) else None


def vspipe_command() -> str:
    """What to start as vspipe: the bundled one by its full path on Windows,
    else plain ``vspipe`` (Linux, unchanged)."""
    return bundled_vspipe() or "vspipe"


def vspipe() -> str | None:
    """The vspipe to run, as a full path, or None: the bundled one on Windows,
    else the one on PATH (``executable``)."""
    return bundled_vspipe() or executable("vspipe")


# ======================================================================== processes
def search_windows_path(
    name: str, path: str, pathext: str, *, isfile: Callable[[str], bool] = os.path.isfile
) -> str | None:
    """Where Windows would find program ``name`` on ``path`` (``;``-separated),
    as a full path, or None. Unlike ``shutil.which`` (and ``CreateProcess``) it
    never looks in the current folder, and it skips PATH entries that are not
    absolute (``.``, ``bin``), which would mean the current folder too. A ``name``
    with a folder in it counts only when that folder is absolute. ``pathext``:
    the PATHEXT list, tried when ``name`` has none of those extensions."""
    exts = [e for e in (pathext or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    upper = name.upper()
    names = [name] if any(upper.endswith(e.upper()) for e in exts) else [name + e for e in exts]
    folder = ntpath.dirname(name)
    if folder:
        if not ntpath.isabs(name):
            return None
        return next((n for n in names if isfile(n)), None)
    for d in path.split(";"):
        d = d.strip().strip('"')
        if not d or not ntpath.isabs(d):
            continue
        for n in names:
            candidate = ntpath.join(d, n)
            if isfile(candidate):
                return candidate
    return None


def executable(name: str) -> str | None:
    """``shutil.which`` on Linux. On Windows a full path or None, never from the
    current folder (``search_windows_path``), and preferring the windowed
    ``.exe``: mpv ships ``mpv.com`` (a console wrapper) next to ``mpv.exe``, and
    PATHEXT finds the ``.com`` first."""
    if not IS_WINDOWS:
        return shutil.which(name)
    path, pathext = os.environ.get("PATH", ""), os.environ.get("PATHEXT", "")
    if not os.path.splitext(name)[1]:
        found = search_windows_path(name + ".exe", path, pathext)
        if found is not None:
            return found
    return search_windows_path(name, path, pathext)


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
    if sys.platform == "darwin":
        return d.pid_alive(pid)
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
        """A new descriptor the event loop can watch, else None: poll instead. A
        pidfd on Linux, a kqueue on macOS (``_darwin.exit_watch``); both go
        readable when the process exits and stay that way."""
        if sys.platform == "darwin":
            return d.exit_watch(self.pid)
        if sys.platform == "win32" or not hasattr(os, "pidfd_open"):
            return None
        try:
            fd: int = os.pidfd_open(self.pid)  # annotated: Any where the call is absent (macOS)
        except OSError:
            return None
        return fd

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
    can't be told (then the caller refuses). Linux: ``SO_PEERCRED``. macOS:
    ``LOCAL_PEERCRED`` and ``LOCAL_PEERPID``, which raise ``ConnectionResetError``
    once the peer has gone. Windows: the pipe server's pid and the user its
    process runs as."""
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
    if sys.platform == "darwin":
        return d.peer_cred(sock)
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    return (pid if pid > 0 else None), uid == os.getuid()


__all__ = [
    "INSTALL_RECORD",
    "IS_MACOS",
    "IS_WINDOWS",
    "NO_CWD_SEARCH",
    "VSTRT_LIB",
    "HAS_VSTRT",
    "O_BINARY",
    "O_CLOEXEC",
    "O_NOFOLLOW",
    "DetachedChild",
    "bundled_vspipe",
    "executable",
    "fsync_dir",
    "group_alive",
    "group_spawn_kwargs",
    "hold_install_mutex",
    "install_mutex_name",
    "install_root",
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
    "point_at_runtime",
    "prepare_endpoint",
    "replace",
    "runtime_dirs",
    "search_windows_path",
    "set_mode",
    "shared_mode_bits",
    "signal_group",
    "spawn_detached",
    "try_lock",
    "unlock",
    "use_bundled_runtime",
    "vspipe",
    "vspipe_command",
]
