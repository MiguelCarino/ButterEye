# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The macOS calls ``osapi`` needs, where being POSIX is not enough.

Imported only on macOS. The rest of ``osapi``'s POSIX half works there as it
does on Linux; these are the five places where it does not:

- ``SO_PEERCRED`` does not exist. macOS splits it in two: ``LOCAL_PEERCRED``
  gives an ``xucred`` (uid and groups, no pid) and ``LOCAL_PEERPID`` gives the
  pid but stops answering once the peer has exited (``peer_cred``).
- there is no ``/proc``, so a process's state comes from libproc's
  ``proc_pidinfo`` (``pid_alive``).
- there is no ``$XDG_RUNTIME_DIR``, so the private folder for sockets is the
  per-user temporary directory the kernel names (``user_temp_dir``).
- there is no ``pidfd_open``; a kqueue holding one ``EVFILT_PROC`` filter is
  the same descriptor-you-can-watch (``exit_watch``).
- dyld searches nothing a Mac keeps VapourSynth in, so mpv's ``vapoursynth``
  filter cannot load VSScript by itself and has to be handed the file
  (``vsscript_library``, used by ``osapi.use_bundled_runtime``).

The three ``ctypes`` calls all go to ``libSystem``, which is already loaded in
every process: nothing here needs Homebrew, a compiler or a wheel.
``vsscript_library`` needs no library at all: it reads directories.
"""

from __future__ import annotations

import ctypes
import errno
import os
import select
import socket
import stat
import struct
import sys
from collections.abc import Iterator
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


# ======================================================================== VSScript
#: the leaf names mpv's ``vapoursynth`` filter ``dlopen``s. Both are strings in
#: ``<prefix>/bin/mpv``, next to "Failed to load VapourSynth VSScript library" and
#: ``getVSScriptAPI``, and ``libvsscript.dylib`` is what VapourSynth's meson build
#: installs beside the Python module -- Homebrew's bottle and a pip wheel both
#: carry that one. ``libvapoursynth-script.dylib`` is the name its older autotools
#: build left in ``<prefix>/lib``, which is also what the error message names.
VSSCRIPT_NAMES = ("libvsscript.dylib", "libvapoursynth-script.dylib")

#: where Homebrew's ``vapoursynth`` formula keeps it: inside the virtualenv the
#: formula builds under ``libexec``, so the path carries the formula's version
#: *and* the Python minor version. Both are globbed; neither is written down.
_KEG_GLOB = "Cellar/vapoursynth/*/libexec/lib/python3.*/site-packages/vapoursynth"


def vsscript_library() -> Path | None:
    """VSScript's library file on this Mac, or None when nothing holds one.

    mpv's ``vapoursynth`` filter does not link VSScript: it ``dlopen``s a bare
    leaf name, which sends dyld to ``/usr/lib``, the Swift toolchain directories
    and the dyld cache, and never inside Homebrew, where the only copy is.
    Measured on this Mac (docs/PLATFORMS.md, the phase-4 blocker), that copy is::

        <prefix>/Cellar/vapoursynth/<version>/libexec/lib/python3.<minor>/
            site-packages/vapoursynth/libvsscript.dylib

    inside the formula's own virtualenv -- a path with the formula's version and
    the Python minor version in it, under the other of the two names, on no dyld
    search path. So the file has to be found and handed over, which is what
    ``osapi.use_bundled_runtime`` does with this answer, and what the doctor and
    the Homebrew tap ask for with the same call.

    Found by reading directories. The shortest route would be the import system
    -- the ``vapoursynth`` package directory *is* where the library sits, and
    Homebrew's ``python@3.14`` reaches the formula's virtualenv through its
    ``homebrew-vapoursynth.pth`` -- but even ``importlib.util.find_spec`` goes
    through ``sys.meta_path``, where ``tests/boundary/import_guard.py`` refuses
    the name outright for the GUI process (F17, SCOPE §8.1) and records the
    violation. ``sys.path`` holds the same directories without the import
    machinery being involved at all, and ``doctor/brew.py`` reads Homebrew as
    files for a related reason. Three places, in order:

    - ``sys.path``, for a ``vapoursynth`` package directory with the library in
      it. This answers for a ``pip install vapoursynth`` into the environment
      ButterEye runs under, for a source build, and for Homebrew's own Python,
      whose ``.pth`` puts the keg's ``site-packages`` on the path -- and it never
      imports the package, only stats inside it;
    - the ``libexec`` virtualenv of every ``vapoursynth`` keg under Homebrew's
      prefix, newest version first (``_keg_key``), which is what answers inside a
      ``python -m venv`` made without ``--system-site-packages``: measured, that
      is the shape of this repository's own test environment, where the package
      is not on ``sys.path`` at all;
    - ``<prefix>/lib``, where a from-source ``make install`` leaves it and where
      a Homebrew tap can put a copy of its own -- the one layout that needs no
      environment variable, because it is on a path an ``-rpath``'d mpv searches.

    Nothing is cached and no version is remembered, so there is no stale answer
    to invalidate: a ``brew upgrade vapoursynth`` moves the keg and the next
    ``ButterEye.open()`` derives the new path. Returns None rather than raising
    when VapourSynth is not installed, or when a directory cannot be read, which
    is what makes the macOS branch of ``use_bundled_runtime`` a no-op there.
    """
    for folder in _vsscript_dirs():
        if not _trustworthy_dir(folder):
            continue
        for name in VSSCRIPT_NAMES:
            lib = folder / name
            try:
                if lib.is_file():
                    return lib
            except OSError:  # a path too long to stat, a keg being deleted
                continue
    return None


def _trustworthy_dir(folder: Path) -> bool:
    """Is ``folder`` one this user alone controls?

    What comes back from here is handed to mpv as ``VSSCRIPT_PATH`` and mpv
    ``dlopen``s it, so a directory anyone else can write to is a way to run code
    inside the player (SCOPE 4.3's threat model is a same-machine process, not a
    remote one). ``sys.path`` makes that reachable: under ``python -m`` its first
    entry is the current directory, so running ButterEye from a world-writable
    directory holding ``./vapoursynth/libvsscript.dylib`` would otherwise load
    that file -- measured, before this check, ``vsscript_library()`` answered
    with a planted copy under ``/private/tmp``.

    Both halves are needed. The owner test alone would accept a directory of our
    own that is group-writable, and the mode test alone would accept one owned by
    somebody else with tight permissions -- which they can loosen whenever they
    like. A directory that cannot be stat-ed is not trusted either.
    """
    try:
        st = folder.stat()  # resolves the link: what dlopen would actually open
    except OSError:
        return False
    return st.st_uid == os.getuid() and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _vsscript_dirs() -> Iterator[Path]:
    """The directories ``vsscript_library`` looks in, in order."""
    # The current directory is never asked, however it reaches sys.path. Under
    # ``python -m buttereye.cli`` -- which is how ButterEye runs from a checkout,
    # and on this machine there is no installed console script -- sys.path[0] IS
    # the working directory, so a ``./vapoursynth/libvsscript.dylib`` beside
    # whatever the user happened to cd into would be handed to mpv as
    # VSSCRIPT_PATH and dlopen-ed. Measured before this skip: run from a
    # directory holding that file, vsscript_library() answered with it.
    # An installed VapourSynth is never in the working directory, so nothing
    # legitimate is lost; _trustworthy_dir is the second line, for the rest.
    here = {Path.cwd(), Path.cwd().resolve()}
    for entry in sys.path:
        # "" is the current directory, and a .zip or an egg is not a directory a
        # dylib can be stat-ed inside; both are skipped by asking for the package
        # directory and letting the is_file() in the caller answer
        if not entry:
            continue
        folder = Path(entry)
        try:
            if folder in here or folder.resolve() in here:
                continue
        except OSError:  # unresolvable: not somewhere to load a library from
            continue
        yield folder / "vapoursynth"

    # Homebrew's prefix. Imported here and not at the top of the module because
    # ``buttereye.core.types`` imports ``osapi``, which imports this module: a
    # module-level import would close that circle. It is the one answer for where
    # Homebrew is (``HOMEBREW_PREFIX``, else the prefix with a ``brew`` in it).
    from buttereye.core.types import homebrew_prefix

    prefix = homebrew_prefix()
    try:
        yield from sorted(prefix.glob(_KEG_GLOB), key=_keg_key, reverse=True)
    except OSError:  # no Homebrew here, or no permission to look
        pass
    yield prefix / "lib"


def _keg_key(folder: Path) -> tuple[tuple[int, str], tuple[int, str]]:
    """``folder`` ordered by the formula's version, then Python's minor version.

    ``…/Cellar/vapoursynth/<version>/libexec/lib/python3.<minor>/site-packages/
    vapoursynth``: both numbers are in the path, an upgrade can leave the old keg
    in the Cellar beside the new one, and ``sorted`` on the strings would put
    ``9`` after ``81``, so each is read as a number.
    """
    parts = folder.parts
    version = parts[-6] if len(parts) >= 6 else ""
    minor = parts[-3].removeprefix("python3.") if len(parts) >= 3 else ""
    return _number_first(version), _number_first(minor)


def _number_first(name: str) -> tuple[int, str]:
    """``name`` as (its leading number, itself); -1 for a name with no number.

    VapourSynth's versions are plain integers (``81``) and Homebrew appends a
    revision to the keg directory when it rebuilds one (``81_1``), which the
    second half keeps as the tie-break.
    """
    digits = name[: len(name) - len(name.lstrip("0123456789"))]
    return int(digits) if digits else -1, name
