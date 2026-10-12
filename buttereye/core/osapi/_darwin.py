# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The macOS calls ``osapi`` needs, where being POSIX is not enough.

Imported only on macOS. The rest of ``osapi``'s POSIX half works there as it
does on Linux; these are the four places where it does not:

- ``SO_PEERCRED`` does not exist. macOS splits it in two: ``LOCAL_PEERCRED``
  gives an ``xucred`` (uid and groups, no pid) and ``LOCAL_PEERPID`` gives the
  pid but stops answering once the peer has exited (``peer_cred``).
- there is no ``/proc``, so a process's state comes from libproc's
  ``proc_pidinfo`` (``pid_alive``).
- there is no ``$XDG_RUNTIME_DIR``, so the private folder for sockets is the
  per-user temporary directory the kernel names (``user_temp_dir``).
- there is no ``pidfd_open``; a kqueue holding one ``EVFILT_PROC`` filter is
  the same descriptor-you-can-watch (``exit_watch``).

The three ``ctypes`` calls all go to ``libSystem``, which is already loaded in
every process: nothing here needs Homebrew, a compiler or a wheel.
"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import socket
import struct
import sys
from pathlib import Path

assert sys.platform == "darwin"  # mypy: the rest is checked only for --platform darwin

#: ``libSystem``, which is what ``CDLL(None)`` resolves against inside this
#: process; libproc's and ``confstr``'s symbols both live there, and
#: ``/usr/lib/libproc.dylib`` only re-exports them. ``use_errno`` makes ctypes
#: keep its own copy of errno, so nothing Python runs between the call and the
#: read can overwrite it.
_libc = ctypes.CDLL(None, use_errno=True)

#: ``SOL_LOCAL`` and ``LOCAL_PEERPID`` from ``<sys/un.h>``: Python exports
#: ``socket.LOCAL_PEERCRED`` but neither of these, so they are spelled out here.
SOL_LOCAL = 0
LOCAL_PEERPID = 0x002

#: ``cr_version`` of the ``xucred`` layout read below. The kernel fills in
#: anything else only when it means something else, so a different version makes
#: ``cr_uid`` untrustworthy (``<sys/ucred.h>``).
XUCRED_VERSION = 0

#: ``sizeof(struct xucred)``: version, uid, group count, then NGROUPS gids.
_XUCRED_SIZE = 76
#: its first three fields; the gids are not read
_XUCRED_HEAD = "=IIh"


def peer_cred(sock: socket.socket) -> tuple[int | None, bool]:
    """``(pid, same_user)`` for a connected Unix-socket peer.

    Answers ``(None, False)`` -- the refusal, which ``ipc.py`` turns into
    SOCKET_UNSAFE -- whenever the kernel's answer cannot be trusted: a short
    ``xucred``, an unexpected ``cr_version``, or any ``OSError`` that is not
    ENOTCONN. Raises ``ConnectionResetError`` for ENOTCONN, which on macOS means
    there is no connected peer: it has gone, or there never was one (an
    unconnected or a listening socket answers the same way). Raises nothing
    else, and ``struct.error`` is impossible by construction.
    """
    try:
        raw = sock.getsockopt(SOL_LOCAL, socket.LOCAL_PEERCRED, _XUCRED_SIZE)
        # macOS truncates a getsockopt answer rather than failing: a socket that
        # carries no xucred answers with fewer bytes (0, on a TCP socket), so a
        # short buffer is the "cannot tell" case and not a struct.error
        if len(raw) < struct.calcsize(_XUCRED_HEAD):
            return None, False
        version, uid, _ngroups = struct.unpack_from(_XUCRED_HEAD, raw)
        if version != XUCRED_VERSION:
            return None, False
        # the int form of getsockopt: one word, which the kernel cannot truncate
        pid = sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID)
    except OSError as exc:
        # ENOTCONN is the one errno worth retrying on. It says only "no connected
        # peer": usually mpv exited between the connect and this check, where
        # Linux's SO_PEERCRED would still answer from its cache -- but a socket
        # that was never connected, or a listening one, reads the same.
        if exc.errno == errno.ENOTCONN:
            raise ConnectionResetError(errno.ENOTCONN, os.strerror(errno.ENOTCONN)) from exc
        # every other errno (EBADF on a closed socket, EINVAL on an unconnected
        # one) is an answer we cannot read: refuse the peer instead of raising
        # an OSError no caller between here and the user handles
        return None, False
    return (pid if pid > 0 else None), uid == os.getuid()


#: ``proc_pidinfo``'s PROC_PIDTBSDINFO flavour and the ``struct proc_bsdinfo``
#: it fills in (``<sys/proc_info.h>``). The size and the offset of ``pbi_status``
#: -- the kernel's ``p_stat`` -- come from compiling ``offsetof`` against the
#: MacOSX27.0 SDK header rather than from counting fields by eye.
PROC_PIDTBSDINFO = 3
_PROC_BSDINFO_SIZE = 136
_PBI_STATUS_OFFSET = 4
_PBI_STATUS = "=I"

#: ``p_stat`` for a process that has exited and is waiting for its parent to
#: reap it (``<sys/proc.h>``). This is the state Linux's ``/proc/<pid>/stat``
#: spells ``Z``, and the only one of the five that is not a live process.
SZOMB = 5

_proc_pidinfo = _libc.proc_pidinfo
_proc_pidinfo.restype = ctypes.c_int
_proc_pidinfo.argtypes = (
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_uint64,
    ctypes.c_void_p,
    ctypes.c_int,
)


def pid_alive(pid: int) -> bool:
    """Is ``pid`` a process that has not yet exited?

    What ``osapi.pid_alive`` reads out of ``/proc/<pid>/stat`` on Linux. The
    answer that matters is the zombie: a detached mpv that has exited but whose
    parent has not reaped it still answers ``os.kill(pid, 0)``, and reporting it
    alive would keep ButterEye waiting for a process that is already gone.
    ``proc_pidinfo`` fails with ESRCH for exactly that state as well as for a pid
    nobody holds, so both read as dead, as they do on Linux.

    EPERM -- another user's process -- means alive, which is what the
    ``os.kill`` check in front of this already answers. So does any other errno:
    a call that stops working must never be read as "mpv has gone".
    """
    buf = ctypes.create_string_buffer(_PROC_BSDINFO_SIZE)
    ctypes.set_errno(0)
    # the return is the number of bytes filled in (136), or 0 with errno set
    size = int(_proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, _PROC_BSDINFO_SIZE))
    if size >= _PBI_STATUS_OFFSET + struct.calcsize(_PBI_STATUS):
        status = int(struct.unpack_from(_PBI_STATUS, buf, _PBI_STATUS_OFFSET)[0])
        return status != SZOMB
    return ctypes.get_errno() != errno.ESRCH


#: ``_CS_DARWIN_USER_TEMP_DIR`` from ``<unistd.h>``. ``os.confstr`` knows only
#: the POSIX names, so this one is spelled out and called directly.
_CS_DARWIN_USER_TEMP_DIR = 65537

_confstr = _libc.confstr
_confstr.restype = ctypes.c_size_t
_confstr.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t)


def user_temp_dir() -> Path:
    """This user's private temporary directory, as an absolute path.

    macOS sets no ``$XDG_RUNTIME_DIR``, and this is what it has instead: a
    per-user 0700 directory under ``/var/folders`` whose name the kernel derives
    from the uid and ``launchd`` creates at login. ``$TMPDIR`` normally holds the
    same path, but it is an ordinary variable that ``env -i`` or a service
    manager can drop, where ``confstr`` always answers.

    Raises ``OSError`` when it does not, or answers something that is not an
    absolute path, so that the caller fails with BE-2010 (RUNTIME_DIR_UNSAFE)
    instead of putting ButterEye's socket somewhere other users can reach. The
    kernel's answer ends in a slash; ``Path`` drops it.

    It is not a session directory the way Linux's is. ``dirhelper``
    (``com.apple.bsd.dirhelper``) sweeps it daily at 03:35 and deletes what has
    not been touched for three days, so an mpv ButterEye has been attached to for
    longer than that can in principle lose its socket from under it. Short paths
    are the reason this is used rather than ``$TMPDIR``-derived ones anyway: the
    result is 49 bytes, against the 103 a macOS ``AF_UNIX`` path may be.
    """
    ctypes.set_errno(0)
    size = int(_confstr(_CS_DARWIN_USER_TEMP_DIR, None, 0))
    if size == 0:
        # confstr reports failure by returning 0, not by the errno it leaves
        # behind: the size probe sets ENOTSUP even when it succeeds
        raise OSError(ctypes.get_errno(), "confstr(_CS_DARWIN_USER_TEMP_DIR) failed")
    buf = ctypes.create_string_buffer(size)
    ctypes.set_errno(0)
    if int(_confstr(_CS_DARWIN_USER_TEMP_DIR, buf, size)) == 0:
        raise OSError(ctypes.get_errno(), "confstr(_CS_DARWIN_USER_TEMP_DIR) failed")
    path = os.fsdecode(buf.value)
    if not os.path.isabs(path):
        raise OSError(errno.EINVAL, f"the per-user temporary directory is not absolute: {path!r}")
    return Path(path)


def exit_watch(pid: int) -> int | None:
    """A descriptor that becomes readable when ``pid`` exits, or None.

    macOS's answer to Linux's pidfd, so ``MpvProcess`` watches mpv here instead
    of waking ten times a second: a kqueue carrying one ``EVFILT_PROC`` filter
    for ``NOTE_EXIT``. Like a pidfd it needs no relationship to the process --
    measured against an orphan reparented to launchd, which is the shape of an
    mpv ButterEye attached to rather than started -- and it is level-triggered,
    because the caller never dequeues the event: the descriptor stays readable
    until it is closed. asyncio's own kqueue selector can watch it.

    None when the filter cannot be registered. For a pid that has already
    exited, including a zombie, that is ESRCH; the caller then polls, and its
    first poll already sees the exit.
    """
    try:
        kq = select.kqueue()
    except OSError:
        return None
    try:
        kq.control(
            [
                select.kevent(
                    pid,
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                    fflags=select.KQ_NOTE_EXIT,
                )
            ],
            0,
            0,
        )
        # a plain descriptor, not the kqueue object's own: the caller closes this
        # with os.close, and a surviving select.kqueue would close that same
        # number a second time when it was collected
        return os.dup(kq.fileno())
    except OSError:
        return None
    finally:
        kq.close()
