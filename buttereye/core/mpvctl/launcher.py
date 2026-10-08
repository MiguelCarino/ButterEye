# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Start the user's mpv for a GUI session (SCOPE §4.3 "Launch from the GUI", §4.10).

mpv is started in its own session/process group with stdin from /dev/null and
its output in ``logs/mpv-<id>.log`` (never pipes), with ``--input-ipc-server`` in
the 0700 runtime directory, so it outlives the GUI (GUI.md §3 rule 9). ButterEye
never writes to ``~/.config/mpv``: it passes ``--include`` of its own generated
profile file under ``$XDG_CONFIG_HOME/buttereye/mpv/`` (§4.6).

mpv's lifetime is decided at spawn (§4.10): it is started with ``posix_spawn``,
not as an asyncio subprocess, so no transport or ``Popen`` finaliser can ever
kill it. ``MpvProcess`` watches it with a pidfd and reaps it when it exits; only
an explicit ``terminate`` (a cancelled or failed ``play``) or the user stops it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import stat
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import buttereye
from buttereye.core import capabilities
from buttereye.core.capabilities import ProviderRef
from buttereye.core.errors import ButterEyeError, ErrorCode
from buttereye.core.mpvctl.ipc import MpvIpc, SocketRefused
from buttereye.core.scriptgen.generator import write_atomic
from buttereye.core.types import Msg

DATA_DIR = Path(buttereye.__file__).resolve().parent / "data" / "mpv"
LUA_HELPER = DATA_DIR / "buttereye.lua"
CONF_TEMPLATE = DATA_DIR / "buttereye.conf"
PROFILE = "buttereye"
#: Per-session mpv logs kept in ``logs_dir`` (SCOPE §4.6: 5 files of at most 5 MB).
LOG_KEEP = 5
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_MAX_AGE_S = 14 * 24 * 3600.0
_log = logging.getLogger(__name__)
_CHECK_PRIVATE = ProviderRef("buttereye.core.paths", "check_private_dir")


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` with mode 0700, or verify an existing one (never chmod, §4.3).

    Uses U3's ``paths.check_private_dir`` when present (same BE-2010 texts)."""
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ButterEyeError(
            ErrorCode.RUNTIME_DIR_UNSAFE,
            Msg("The runtime directory {path} can't be created.", {"path": os.fspath(path)}),
            Msg("Start ButterEye from a normal desktop or login session."),
            detail=str(exc),
        ) from exc
    check = capabilities.resolve(_CHECK_PRIVATE)
    if check is not None:
        check(path, must_exist=True)
        return path
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ButterEyeError(
            ErrorCode.RUNTIME_DIR_UNSAFE,
            Msg(
                "{path} is not a private directory owned by you (mode 0700).",
                {"path": os.fspath(path)},
            ),
            Msg("Remove it and start ButterEye again; ButterEye never changes its permissions."),
        )
    return path


def write_include(path: Path) -> bool:
    """Write ButterEye's own ``[buttereye]`` profile file; True if it changed."""
    text = CONF_TEMPLATE.read_text(encoding="utf-8")
    with contextlib.suppress(OSError):
        if path.read_text(encoding="utf-8") == text:
            return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(path, text, mode=0o644)
    return True


def build_argv(
    mpv: str,
    *,
    include: Path,
    socket: Path,
    file: Path,
    extra: Sequence[str] = (),
) -> list[str]:
    """The mpv command line. No ``--vf``: the filter is added over IPC (§4.2 step 2).

    ``--term-status-msg=`` empties the terminal status line, which mpv would
    otherwise append to the log several times a second (~24 MB/h); warnings,
    errors and ncnn's stderr still reach the log (``device_lost_lines``), and
    the user's own ``msg-level`` is left alone."""
    return [
        mpv,
        f"--include={os.fspath(include)}",
        f"--profile={PROFILE}",
        f"--script={os.fspath(LUA_HELPER)}",
        f"--input-ipc-server={os.fspath(socket)}",
        "--hwdec=auto-copy",
        "--video-sync=display-resample",
        "--term-status-msg=",
        *extra,
        "--",
        os.fspath(file),
    ]


class MpvProcess:
    """The mpv ButterEye started: pid, exit status and an async ``wait``.

    Exit is watched with a pidfd on the running loop and the child is reaped as
    soon as it exits (no zombies while ButterEye runs). Dropping this object,
    closing the loop or exiting ButterEye never signals mpv."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self._exited = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pidfd: int | None = None
        try:
            self._pidfd = os.pidfd_open(pid)
        except OSError:
            self._pidfd = None
        if self._pidfd is not None:
            self._loop = asyncio.get_running_loop()
            try:
                self._loop.add_reader(self._pidfd, self._poll)
            except NotImplementedError, RuntimeError, OSError:
                self._close_pidfd()
        self._poll()

    def _close_pidfd(self) -> None:
        fd, self._pidfd = self._pidfd, None
        if fd is None:
            return
        if self._loop is not None and not self._loop.is_closed():
            with contextlib.suppress(Exception):
                self._loop.remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _poll(self) -> None:
        """Reap mpv if it has exited (never blocks)."""
        if self.returncode is not None:
            return
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # reaped elsewhere (or not our child): judge by existence
            if _pid_alive(self.pid):
                return
            self.returncode = -1
        else:
            if pid == 0:
                return
            self.returncode = os.waitstatus_to_exitcode(status)
        self._close_pidfd()
        self._exited.set()

    async def wait(self) -> int:
        """Wait for mpv to exit; the exit code (negative signal number if killed)."""
        while self.returncode is None:
            if self._pidfd is not None:
                await self._exited.wait()
            else:  # no pidfd (old kernel): poll
                await asyncio.sleep(0.1)
                self._poll()
        return self.returncode

    def stop_watching(self) -> None:
        """Stop the pidfd watch (mpv is not signalled; it is reaped by init later)."""
        self._close_pidfd()


def _pid_alive(pid: int) -> bool:
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


async def spawn_mpv(
    argv: Sequence[str], log_path: Path, *, env: Mapping[str, str] | None = None
) -> MpvProcess:
    """Start mpv detached from ButterEye's lifetime (own session, file output).

    ``posix_spawnp`` with ``setsid``: stdin /dev/null, stdout+stderr appended to
    ``log_path``; every other descriptor of ButterEye is close-on-exec (PEP 446)."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600)
    try:
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
    except FileNotFoundError as exc:
        raise ButterEyeError(
            ErrorCode.MPV_NOT_FOUND,
            Msg("mpv was not found."),
            Msg("Install the distribution's mpv package."),
            commands=("sudo dnf install mpv",),
            detail=str(exc),
        ) from exc
    finally:
        os.close(fd)
    return MpvProcess(pid)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def terminate(proc: MpvProcess, *, grace_s: float = 2.0) -> None:
    """SIGTERM mpv's process group, SIGKILL after ``grace_s``; used only when
    ``play`` fails or is cancelled before it returns (GUI.md §11.4)."""
    pgid = proc.pid
    if proc.returncode is None or _group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.shield(proc.wait()), grace_s)
    if proc.returncode is None or _group_alive(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), 2.0)


def prune_logs(
    logs_dir: Path,
    runtime_dir: Path,
    *,
    keep: int = LOG_KEEP,
    max_age_s: float = LOG_MAX_AGE_S,
    now: float | None = None,
) -> tuple[Path, ...]:
    """Delete old ``mpv-<sid>.log`` files: beyond the newest ``keep`` or older than
    ``max_age_s``. A log whose ``mpv-<sid>.sock`` still exists in ``runtime_dir``
    (a player that may still run) is never deleted. Returns the deleted paths."""
    now = time.time() if now is None else now
    try:
        logs = [p for p in logs_dir.glob("mpv-*.log") if p.is_file() and not p.is_symlink()]
    except OSError:
        return ()
    dated: list[tuple[float, Path]] = []
    for p in logs:
        with contextlib.suppress(OSError):
            dated.append((p.stat().st_mtime, p))
    dated.sort(reverse=True)
    removed: list[Path] = []
    for i, (mtime, p) in enumerate(dated):
        if i < keep and now - mtime <= max_age_s:
            continue
        sid = p.name[len("mpv-") : -len(".log")]
        if (runtime_dir / f"mpv-{sid}.sock").exists():
            continue
        with contextlib.suppress(OSError):
            p.unlink()
            removed.append(p)
    return tuple(removed)


def cap_log(path: Path, *, max_bytes: int = LOG_MAX_BYTES) -> bool:
    """Truncate a running session's log that grew past ``max_bytes`` (mpv writes
    with O_APPEND, so it continues at the new end). True if truncated."""
    try:
        if path.stat().st_size <= max_bytes:
            return False
        with path.open("r+b") as fh:
            fh.truncate(0)
        with path.open("ab") as fh:
            fh.write(b"[buttereye] log truncated: it grew past the size limit\n")
    except OSError:
        return False
    return True


def remove_runtime_files(runtime_dir: Path, sid: str) -> None:
    """Delete ``mpv-<sid>.sock`` and ``mpv-<sid>.vpy`` once that mpv is gone."""
    for suffix in (".sock", ".vpy"):
        p = runtime_dir / f"mpv-{sid}{suffix}"
        with contextlib.suppress(OSError):
            st = os.lstat(p)
            if st.st_uid == os.getuid() and (stat.S_ISSOCK(st.st_mode) or stat.S_ISREG(st.st_mode)):
                p.unlink()


def log_tail(path: Path, *, lines: int = 20, max_bytes: int = 64 * 1024) -> str:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - max_bytes))
            data = fh.read()
    except OSError:
        return ""
    return "\n".join(data.decode("utf-8", errors="replace").splitlines()[-lines:])


#: Where the mpvSockets user script moves every mpv's IPC socket (SCOPE §4.3).
MPVSOCKETS_DIR = Path("/tmp/mpvSockets")


async def _adopt_mpvsockets(proc: MpvProcess, sockets_dir: Path) -> MpvIpc | None:
    """The mpvSockets script replaces ``input-ipc-server`` when it loads, so the
    socket ButterEye asked for stops answering. Adopt ``<sockets_dir>/<pid>``
    instead (SCOPE §4.3), only after the same path and ``SO_PEERCRED`` checks
    and only when the peer is the very mpv we started; anything else is ignored
    (keep waiting on our own socket)."""
    path = sockets_dir / str(proc.pid)
    try:
        ipc = await MpvIpc.connect(path, timeout_s=0.5)
    except (SocketRefused, OSError, TimeoutError) as exc:
        if not isinstance(exc, FileNotFoundError):
            _log.debug("not adopting %s: %s", path, exc)
        return None
    if ipc.peer_pid != proc.pid:
        await ipc.close()
        _log.warning("not adopting %s: peer pid %s, expected %s", path, ipc.peer_pid, proc.pid)
        return None
    _log.info("mpvSockets moved mpv's socket; using %s", path)
    return ipc


async def connect(
    socket: Path,
    proc: MpvProcess,
    log_path: Path,
    *,
    timeout_s: float,
    mpvsockets_dir: Path | None = MPVSOCKETS_DIR,
) -> MpvIpc:
    """Wait for mpv's IPC socket and connect; the peer must be the mpv we started.

    When the user's mpvSockets script has moved the socket, ``<mpvsockets_dir>/<pid>``
    is adopted instead (``None`` turns that off)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        if proc.returncode is not None:
            raise ButterEyeError(
                ErrorCode.INTERNAL,
                Msg(
                    "mpv exited before ButterEye could connect (exit code {code}).",
                    {"code": proc.returncode},
                ),
                Msg("See the mpv log for the reason."),
                detail=log_tail(log_path),
            )
        try:
            ipc = await MpvIpc.connect(socket, timeout_s=max(0.1, deadline - loop.time()))
        except SocketRefused as exc:
            raise ButterEyeError(
                ErrorCode.SOCKET_UNSAFE,
                Msg(
                    "The mpv control socket failed a safety check ({reason}).",
                    {"reason": exc.reason.value},
                ),  # fmt: skip
                detail=os.fspath(socket),
            ) from exc
        except FileNotFoundError, ConnectionRefusedError, TimeoutError:
            if mpvsockets_dir is not None:
                adopted = await _adopt_mpvsockets(proc, mpvsockets_dir)
                if adopted is not None:
                    return adopted
            if loop.time() >= deadline:
                raise ButterEyeError(
                    ErrorCode.IPC_LOST,
                    Msg("mpv did not open its control socket in time."),
                    Msg("See the mpv log for the reason."),
                    detail=log_tail(log_path),
                ) from None
            await asyncio.sleep(0.05)
            continue
        if ipc.peer_pid is not None and ipc.peer_pid != proc.pid:
            await ipc.close()
            raise ButterEyeError(
                ErrorCode.SOCKET_UNSAFE,
                Msg("Another process answered on ButterEye's mpv socket."),
                detail=f"peer pid {ipc.peer_pid}, expected {proc.pid}",
            )
        return ipc


__all__ = [
    "DATA_DIR",
    "LUA_HELPER",
    "CONF_TEMPLATE",
    "PROFILE",
    "ensure_private_dir",
    "write_include",
    "LOG_KEEP",
    "LOG_MAX_BYTES",
    "MpvProcess",
    "build_argv",
    "spawn_mpv",
    "terminate",
    "prune_logs",
    "cap_log",
    "remove_runtime_files",
    "log_tail",
    "MPVSOCKETS_DIR",
    "connect",
]
