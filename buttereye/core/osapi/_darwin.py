# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The one macOS call ``osapi`` needs: peer credentials for a Unix socket.

Imported only on macOS. The rest of ``osapi``'s POSIX half works there as it
does on Linux; what is missing is ``SO_PEERCRED``, which answers with the peer's
pid, uid and gid in a single call. macOS splits that in two: ``LOCAL_PEERCRED``
gives an ``xucred`` (uid and groups, no pid) and ``LOCAL_PEERPID`` gives the pid
but stops answering once the peer has exited.
"""

from __future__ import annotations

import errno
import os
import socket
import struct
import sys

assert sys.platform == "darwin"  # mypy: the rest is checked only for --platform darwin

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
