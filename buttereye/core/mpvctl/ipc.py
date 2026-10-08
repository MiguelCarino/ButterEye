# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv JSON-IPC client and the command allowlist (SCOPE §4.3 "IPC security").

Every command ButterEye can send to mpv passes ``check_command`` in this module:
``run``, ``subprocess``, ``load-script``, ``loadfile`` and anything else not
listed are refused before a byte is written. Only ``vf`` entries labelled
``@buttereye`` can be added, toggled or removed. Sockets are checked with
``lstat`` (socket, own uid, parent owned by us, no symlinked parent) and, after
connecting, with ``SO_PEERCRED``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import socket
import stat
import struct
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

from buttereye.core.types import RefusalReason

_log = logging.getLogger(__name__)

#: mpv label of the only filter entry ButterEye ever touches.
LABEL = "buttereye"
VF_LABEL = "@" + LABEL
#: Prefix every ``vf add`` argument must start with.
VF_ADD_PREFIX = VF_LABEL + ":vapoursynth="
#: Name of the Lua helper as mpv sees it (file name without extension).
HELPER_NAME = "buttereye"

#: Properties ButterEye observes (property-change events).
OBSERVABLE: frozenset[str] = frozenset(
    {
        "container-fps",
        "display-fps",
        "frame-drop-count",
        "decoder-frame-drop-count",
        "vo-delayed-frame-count",
        "mistimed-frame-count",
        "display-sync-active",
        "vf",
        "video-params",
        "current-tracks/video",
        "playback-time",
        "audio-pts",
        "pause",
        "core-idle",
        "paused-for-cache",
        "seeking",
        "path",
        "video-frame-info",
    }
)
#: Properties ButterEye may read once.
READABLE: frozenset[str] = OBSERVABLE | frozenset(
    {
        "pid",
        "mpv-version",
        "media-title",
        "filename",
        "hwdec-current",
        "hwdec",
        "hr-seek",
        "hr-seek-framedrop",
        "interpolation",
        "video-sync",
        "watch-later-options",
    }
)
#: Properties ButterEye may set (the attach settings of §4.3; restored on detach).
WRITABLE: frozenset[str] = frozenset({"hwdec", "hr-seek-framedrop", "interpolation"})
LOG_LEVELS: frozenset[str] = frozenset({"no", "fatal", "error", "warn", "info"})


class CommandRefused(ValueError):
    """A command is not on the allowlist (a programming error, never user input)."""


class MpvError(Exception):
    """mpv answered a command with an error string (e.g. "property unavailable")."""

    def __init__(self, error: str, command: Sequence[object]) -> None:
        super().__init__(f"mpv: {error} ({command[0] if command else '?'})")
        self.error = error
        self.command = tuple(command)


class IpcClosed(ConnectionError):
    """The IPC connection ended (mpv quit or the socket broke)."""


def _strs(args: Sequence[object]) -> bool:
    return all(isinstance(a, str) for a in args)


def check_command(args: Sequence[object]) -> None:
    """Raise ``CommandRefused`` unless ``args`` is an allowed mpv command."""
    if not args or not isinstance(args[0], str):
        raise CommandRefused("empty command")
    name, rest = args[0], tuple(args[1:])
    if name == "observe_property":
        if len(rest) == 2 and type(rest[0]) is int and rest[1] in OBSERVABLE:
            return
    elif name == "unobserve_property":
        if len(rest) == 1 and type(rest[0]) is int:
            return
    elif name == "get_property":
        if len(rest) == 1 and rest[0] in READABLE:
            return
    elif name == "set_property":
        if len(rest) == 2 and rest[0] in WRITABLE and isinstance(rest[1], (str, bool)):
            return
    elif name == "request_log_messages":
        if len(rest) == 1 and rest[0] in LOG_LEVELS:
            return
    elif name == "vf":
        if len(rest) == 2 and _strs(rest):
            op, arg = rest
            if op == "add" and isinstance(arg, str) and valid_vf_add(arg):
                return
            if op in ("remove", "toggle") and arg == VF_LABEL:
                return
    elif name == "script-message-to":
        if len(rest) >= 2 and _strs(rest) and rest[0] == HELPER_NAME:
            return
    elif name == "show-text":
        if 1 <= len(rest) <= 2 and isinstance(rest[0], str):
            if len(rest) == 1 or type(rest[1]) is int:
                return
    elif name in ("client_name", "get_version"):
        if not rest:
            return
    raise CommandRefused(f"command not allowed: {name!r}")


_VF_KEYS = frozenset({"file", "buffered-frames", "concurrent-frames", "user-data"})
_PLAIN = re.compile(r"[A-Za-z0-9_./+-]+")


def valid_vf_add(arg: str) -> bool:
    """True if ``arg`` is exactly ``@buttereye:vapoursynth=k=v[:k=v...]`` with known
    keys and every value either ``%len%``-quoted or plain (no separators), so no
    second filter or option can be smuggled in."""
    if not arg.startswith(VF_ADD_PREFIX):
        return False
    rest = arg[len(VF_ADD_PREFIX) :]
    data = rest.encode("utf-8")
    pos, seen = 0, set()
    while True:
        eq = data.find(b"=", pos)
        if eq < 0:
            return False
        key = data[pos:eq].decode("utf-8", errors="replace")
        if key not in _VF_KEYS or key in seen:
            return False
        seen.add(key)
        pos = eq + 1
        if data[pos : pos + 1] == b"%":
            end = data.find(b"%", pos + 1)
            if end < 0 or not data[pos + 1 : end].isdigit():
                return False
            n = int(data[pos + 1 : end])
            pos = end + 1 + n
            if pos > len(data):
                return False
        else:
            m = _PLAIN.match(data[pos:].decode("utf-8", errors="replace"))
            if m is None:
                return False
            pos += len(m.group(0).encode("utf-8"))
        if pos == len(data):
            return "file" in seen
        if data[pos : pos + 1] != b":":
            return False
        pos += 1


def quote(value: str) -> str:
    """mpv length-prefixed quoting ``%<bytes>%value`` for option values (§4.2 step 5)."""
    return f"%{len(value.encode('utf-8'))}%{value}"


# ---------------------------------------------------------------------------
# Socket checks
# ---------------------------------------------------------------------------


def check_socket_path(path: Path) -> RefusalReason | None:
    """``lstat`` checks before connecting (§4.3). Raises ``FileNotFoundError``."""
    st = os.lstat(path)
    if not stat.S_ISSOCK(st.st_mode):
        return RefusalReason.NOT_SOCKET
    if st.st_uid != os.getuid():
        return RefusalReason.WRONG_UID
    try:
        pst = os.lstat(path.parent)
    except OSError:
        return RefusalReason.BAD_PARENT
    if not stat.S_ISDIR(pst.st_mode) or pst.st_uid != os.getuid():
        return RefusalReason.BAD_PARENT
    return None


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """``(pid, uid, gid)`` of the process at the other end of a Unix socket."""
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, gid = struct.unpack("3i", raw)
    return pid, uid, gid


class SocketRefused(ConnectionError):
    def __init__(self, reason: RefusalReason, path: Path) -> None:
        super().__init__(f"refused {path}: {reason.value}")
        self.reason = reason
        self.path = path


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

EventHandler = Callable[[Mapping[str, Any]], None]
Spawner = Callable[[Coroutine[Any, Any, None], str], "asyncio.Task[None]"]

_LINE_LIMIT = 8 * 1024 * 1024


class MpvIpc:
    """One JSON-IPC connection. Loop thread only.

    ``start()`` launches the reader task; replies resolve ``command()`` futures by
    ``request_id``; every event (property changes, log messages, client messages,
    ...) goes to the handlers added with ``add_handler``.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        path: Path,
        *,
        peer_pid: int | None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.path = path
        self.peer_pid = peer_pid
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._handlers: list[EventHandler] = []
        self._observed: dict[str, int] = {}
        self.closed = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: ``time.monotonic()`` of the last line received from mpv (reply or event);
        #: a playing mpv sends property changes many times a second.
        self.last_rx_mono = time.monotonic()

    @classmethod
    async def connect(cls, path: Path, *, timeout_s: float = 2.0) -> MpvIpc:
        """Check the socket, connect, and check the peer's uid.

        Raises ``SocketRefused``, ``FileNotFoundError`` or ``ConnectionError``/``OSError``.
        """
        reason = check_socket_path(path)
        if reason is not None:
            raise SocketRefused(reason, path)
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(os.fspath(path), limit=_LINE_LIMIT), timeout_s
        )
        sock = writer.get_extra_info("socket")
        peer_pid: int | None = None
        try:
            if sock is None:
                raise SocketRefused(RefusalReason.PEERCRED, path)
            pid, uid, _gid = peer_credentials(sock)
            if uid != os.getuid():
                raise SocketRefused(RefusalReason.PEERCRED, path)
            peer_pid = pid if pid > 0 else None
        except BaseException:
            writer.close()
            raise
        return cls(reader, writer, path, peer_pid=peer_pid)

    # -- lifecycle --
    def start(self, spawn: Spawner | None = None) -> None:
        if self._task is not None:
            return
        coro = self._read_loop()
        name = f"mpv-ipc-{self.path.name}"
        if spawn is None:
            self._task = asyncio.get_running_loop().create_task(coro, name=name)
        else:
            self._task = spawn(coro, name)

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def close(self) -> None:
        """Close the connection only; mpv keeps running."""
        if not self._writer.is_closing():
            self._writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer.wait_closed(), 1.0)
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(asyncio.shield(self._task), 1.0)
        self._mark_closed()

    def _mark_closed(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(IpcClosed("mpv IPC connection closed"))

    # -- commands --
    async def command(self, *args: object, timeout_s: float = 5.0) -> Any:
        """Send an allowlisted command and return mpv's ``data`` (``MpvError`` on error)."""
        check_command(args)
        if self.closed.is_set():
            raise IpcClosed("mpv IPC connection closed")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        line = json.dumps({"command": list(args), "request_id": rid}, ensure_ascii=False)
        try:
            self._writer.write(line.encode("utf-8") + b"\n")
            await self._writer.drain()
        except (ConnectionError, OSError) as exc:
            self._pending.pop(rid, None)
            self._mark_closed()
            raise IpcClosed(str(exc)) from exc
        try:
            reply = await asyncio.wait_for(fut, timeout_s)
        finally:
            self._pending.pop(rid, None)
        error = reply.get("error", "success")
        if error != "success":
            raise MpvError(str(error), args)
        return reply.get("data")

    async def get(self, prop: str, *, timeout_s: float = 5.0) -> Any:
        """Read a property; ``None`` when mpv says it is unavailable."""
        try:
            return await self.command("get_property", prop, timeout_s=timeout_s)
        except MpvError as exc:
            if exc.error in ("property unavailable", "property not found"):
                return None
            raise

    async def observe(self, prop: str) -> int:
        if prop in self._observed:
            return self._observed[prop]
        oid = len(self._observed) + 1
        self._observed[prop] = oid
        await self.command("observe_property", oid, prop)
        return oid

    # -- reader --
    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._reader.readline()
                except asyncio.LimitOverrunError, ValueError:
                    _log.warning("oversized mpv IPC line skipped on %s", self.path)
                    continue
                except ConnectionError, OSError:
                    break
                if not line:
                    break
                self.last_rx_mono = time.monotonic()
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    _log.warning("unparsable mpv IPC line on %s", self.path)
                    continue
                if not isinstance(msg, dict):
                    continue
                if "event" not in msg and "request_id" in msg:
                    rid = msg.get("request_id")
                    fut = self._pending.get(rid) if isinstance(rid, int) else None
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                    continue
                for handler in tuple(self._handlers):
                    try:
                        handler(msg)
                    except Exception:
                        _log.exception("mpv IPC event handler failed")
        finally:
            self._mark_closed()


__all__ = [
    "LABEL",
    "VF_LABEL",
    "VF_ADD_PREFIX",
    "HELPER_NAME",
    "OBSERVABLE",
    "READABLE",
    "WRITABLE",
    "CommandRefused",
    "MpvError",
    "IpcClosed",
    "SocketRefused",
    "MpvIpc",
    "check_command",
    "check_socket_path",
    "peer_credentials",
    "quote",
    "valid_vf_add",
]
