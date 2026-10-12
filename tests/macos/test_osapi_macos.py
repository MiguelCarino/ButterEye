# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The macOS half of ``buttereye.core.osapi``, against the real macOS API.

Skipped everywhere but macOS. Unlike ``tests/windows`` these run natively on the
dev machine, so there is no Wine wrapper and no ``--noconftest`` escape: plainly
pytest. Nothing here is mocked - a real Unix socket with a real peer, and the
``xucred`` and pid the kernel answers with.

The peer check is tested across a process boundary (a spawned child serves the
socket), because a check asked about this very process cannot tell a working
answer from a hardcoded one: both are ``os.getpid()``.

This covers phase 0 only, which is the three places macOS raised
``AttributeError`` where Linux answered. ``pid_alive`` still reads a reaped but
unwaited child as alive here (it wants libproc: phase 1) and ``VSTRT_LIB`` still
carries the Linux name (phase 2), so neither is tested for what it will do later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

if sys.platform == "darwin":
    from buttereye.core import osapi
    from buttereye.core.errors import ButterEyeError, ErrorCode
    from buttereye.core.hw import detect
    from buttereye.core.mpvctl import ipc, launcher
    from buttereye.core.osapi import _darwin as d


@pytest.fixture
def short_dir() -> Iterator[Path]:
    """A directory a Unix socket can be bound in.

    ``sun_path`` is 104 bytes on macOS, so 103 is the longest path ``bind``
    accepts. ``tmp_path`` is 119 bytes before a file name is added here, under
    ``/var/folders/<hash>/T/pytest-of-<user>/...``, and binding in it fails with
    ``OSError: AF_UNIX path too long`` - which reads like a bug in the test
    rather than a limit of the platform. ``/tmp`` keeps the whole path near 20.

    ``mkdtemp`` makes it 0700 and owned by us, which is what a runtime directory
    holding an mpv socket has to be for ``ipc.check_socket_path`` to pass.
    """
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="be-t-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextlib.asynccontextmanager
async def _unix_pair(
    endpoint: Path,
) -> AsyncIterator[tuple[asyncio.StreamWriter, asyncio.StreamWriter]]:
    """A connected pair over ``endpoint``, served by this process: our end first,
    then the serving end - the one standing in for mpv."""
    accepted: asyncio.Queue[asyncio.StreamWriter] = asyncio.Queue()

    async def serve(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await accepted.put(writer)

    server = await asyncio.start_unix_server(serve, os.fspath(endpoint))
    try:
        _reader, ours = await asyncio.open_unix_connection(os.fspath(endpoint))
        theirs = await asyncio.wait_for(accepted.get(), timeout=5.0)
        try:
            yield ours, theirs
        finally:
            ours.close()
            theirs.close()
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- the seam
def test_the_seam_knows_which_platform_this_is() -> None:
    assert osapi.IS_MACOS
    assert not osapi.IS_WINDOWS


def test_python_still_hides_the_constants_darwin_spells_out() -> None:
    """``_darwin`` writes SOL_LOCAL and LOCAL_PEERPID as literals because CPython
    exports neither, only LOCAL_PEERCRED. A CPython that exports them would make
    this fail, and the literals could then go."""
    assert hasattr(socket, "LOCAL_PEERCRED")
    assert not hasattr(socket, "SOL_LOCAL")
    assert not hasattr(socket, "LOCAL_PEERPID")
    assert (d.SOL_LOCAL, d.LOCAL_PEERPID, d.XUCRED_VERSION) == (0, 0x002, 0)


# ---------------------------------------------------------------- peer credentials
async def test_the_peer_of_a_socket_we_serve_is_this_process(short_dir: Path) -> None:
    """What ipc.py asks for after every connect. We serve the socket ourselves,
    so the answer is this pid and this user. Asked through the seam as well as
    the module, since the seam's POSIX branch is what the caller reaches.

    Same-process, so it says nothing about where the pid came from: the two
    tests below are the ones that pin that."""
    async with _unix_pair(short_dir / "mpv-peer.sock") as (ours, _theirs):
        assert osapi.ipc_peer(ours) == (os.getpid(), True)
        assert d.peer_cred(ours.get_extra_info("socket")) == (os.getpid(), True)


#: A server for the cross-process peer test: binds the socket it is given,
#: says "ready" on stdout, then holds one accepted connection until its client
#: goes away. Its pid is what LOCAL_PEERPID has to answer with.
_CHILD_SERVER = """
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.settimeout(20.0)
s.bind(sys.argv[1])
s.listen(1)
sys.stdout.write("ready\\n")
sys.stdout.flush()
conn, _ = s.accept()
sys.stdout.write("accepted\\n")
sys.stdout.flush()
conn.recv(1)
"""


def test_the_peer_of_a_socket_a_child_serves_is_the_child(short_dir: Path) -> None:
    """The pid really comes from the kernel, for the process on the other end.

    This is the only test here that a peer check answering ``os.getpid()``, or
    not asking at all, cannot pass: the serving end belongs to another process,
    whose pid is not ours. A spawned child rather than ``fork``, because pytest
    runs this with the memory watchdog's thread alive and forking a
    multi-threaded process is deprecated in 3.14 (and can deadlock the child);
    plain blocking sockets rather than asyncio, since the child is a bare
    interpreter.

    The pid LOCAL_PEERPID answers with belongs to whoever holds the *accepted*
    socket, so the check waits for the child's "accepted" line - connect alone
    only reaches the listen backlog.
    """
    endpoint = short_dir / "mpv-child.sock"
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SERVER, os.fspath(endpoint)],
        stdout=subprocess.PIPE,
        text=True,
    )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n", "the child never bound the socket"
        client.settimeout(10.0)
        client.connect(os.fspath(endpoint))
        assert child.stdout.readline() == "accepted\n", "the child never accepted"
        assert child.pid != os.getpid()
        assert d.peer_cred(client) == (child.pid, True)
    finally:
        client.close()  # the child's recv then returns empty and it exits
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=10.0)
        if child.returncode is None:
            child.kill()
            child.wait()
        if child.stdout is not None:
            child.stdout.close()


async def test_a_peer_owned_by_another_user_is_not_the_same_user(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``same_user`` really compares the peer's uid against ours.

    Nothing in a test can be served by root, so this moves the other half of the
    comparison instead: with ``os.getuid`` answering 0, the peer's real uid stops
    matching and the answer has to be false. ipc.py refuses a falsy ``same_user``
    (``RefusalReason.PEERCRED``), which is the whole point of the check - a peer
    check that never reads the ``xucred`` would still say true here."""
    monkeypatch.setattr(os, "getuid", lambda: 0)
    async with _unix_pair(short_dir / "mpv-uid.sock") as (ours, _theirs):
        assert osapi.ipc_peer(ours) == (os.getpid(), False)
        assert d.peer_cred(ours.get_extra_info("socket")) == (os.getpid(), False)


async def test_a_peer_that_has_gone_reads_as_a_reset(short_dir: Path) -> None:
    """LOCAL_PEERPID stops answering (ENOTCONN) the moment the serving end goes,
    where Linux's SO_PEERCRED keeps answering from its cache. The caller has to
    see a connection error it already retries on, not a bare OSError."""
    async with _unix_pair(short_dir / "mpv-gone.sock") as (ours, theirs):
        theirs.close()
        await theirs.wait_closed()
        with pytest.raises(ConnectionResetError):
            osapi.ipc_peer(ours)


def test_a_socket_the_options_do_not_fit_fails_closed() -> None:
    """An answer that cannot be trusted is the refusal, never an exception.

    A TCP socket is the cheapest socket these two options do not describe:
    measured here, LOCAL_PEERCRED answers with zero bytes and LOCAL_PEERPID with
    ENOPROTOOPT. Neither says anything about a peer, so ``peer_cred`` returns
    ``(None, False)`` - the same shape ``_win32.pipe_server_pid`` returns when it
    cannot tell, and one ipc.py already refuses on. A ``struct.error`` out of the
    short ``xucred``, or the bare OSError, would reach no caller's except."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        assert d.peer_cred(sock) == (None, False)


# ---------------------------------------------------------------- the connect path
async def test_connect_checks_the_peer_of_a_real_socket(short_dir: Path) -> None:
    """The macOS peer of ``tests/windows``' ``test_pipe_endpoint_and_peer_check``:
    the whole path, from the lstat checks through the peer check to one command
    and its reply, over a real socket in a real 0700 runtime directory.

    This is what phase 0 fixed. Before it, ``osapi.ipc_peer`` looked up
    ``socket.SO_PEERCRED``, which does not exist on macOS, so every mpv
    connection here died with an AttributeError inside ``MpvIpc.connect``."""
    endpoint = short_dir / "mpv-connect.sock"
    osapi.prepare_endpoint(endpoint)  # POSIX: nothing to do; the bind below makes it
    got: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One mpv reply, answering whatever request_id the client sent."""
        line = await reader.readline()
        got.append(line)
        rid = json.loads(line)["request_id"]
        reply = {"request_id": rid, "error": "success", "data": 42}
        writer.write(json.dumps(reply).encode("utf-8") + b"\n")
        await writer.drain()

    server = await asyncio.start_unix_server(serve, os.fspath(endpoint))
    try:
        assert ipc.check_socket_path(endpoint) is None
        conn = await ipc.MpvIpc.connect(endpoint)
        assert conn.peer_pid == os.getpid()  # we serve the socket ourselves here
        conn.start()
        assert await conn.command("get_property", "pid") == 42
        assert b"get_property" in b"".join(got)
        await conn.close()
    finally:
        server.close()
        await server.wait_closed()


async def test_connect_to_a_peer_that_has_already_gone_is_retried(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``launcher.connect`` retries on the reset instead of dying on it.

    mpv's socket can answer and mpv be gone by the time the peer check asks, and
    on macOS that is a ConnectionResetError rather than the empty read Linux
    gives - which is why the retry tuple grew that arm. The window is
    microseconds wide in the real thing, so the serving end is closed inside a
    wrapped ``osapi.open_ipc``: the kernel's ENOTCONN is still the real one,
    only its timing is made certain."""
    endpoint = short_dir / "mpv-reset.sock"
    accepted: asyncio.Queue[asyncio.StreamWriter] = asyncio.Queue()

    async def serve(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await accepted.put(writer)

    server = await asyncio.start_unix_server(serve, os.fspath(endpoint))
    real_open_ipc = osapi.open_ipc

    async def open_then_lose_the_peer(
        path: Path, *, limit: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        pair = await real_open_ipc(path, limit=limit)
        serving = await asyncio.wait_for(accepted.get(), timeout=5.0)
        serving.close()
        await serving.wait_closed()
        return pair

    monkeypatch.setattr(osapi, "open_ipc", open_then_lose_the_peer)
    try:
        with pytest.raises(ConnectionResetError):
            await ipc.MpvIpc.connect(endpoint)
        # a pid that is certainly alive, so connect keeps waiting rather than
        # reporting that mpv exited; nothing is spawned or signalled
        proc = launcher.MpvProcess(os.getpid())
        with pytest.raises(ButterEyeError) as err:
            await launcher.connect(
                endpoint, proc, short_dir / "mpv.log", timeout_s=0.3, mpvsockets_dir=None
            )
        assert err.value.code is ErrorCode.IPC_LOST  # retried to the deadline, no traceback
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------- detection
def test_cpu_threads_counts_this_machines_threads() -> None:
    """``os.sched_getaffinity`` does not exist here; before phase 0 the lookup
    raised AttributeError out of probe_hardware and took the doctor with it.

    The count is the one the docstring promises, not merely some positive int:
    8 on this machine, where ``sysctl -n hw.logicalcpu`` says 8 as well."""
    assert detect.cpu_threads() == os.process_cpu_count()


# ---------------------------------------------------------------- not yet ported
def test_vstrt_lib_is_still_the_linux_name() -> None:
    """TensorRT needs CUDA, so it cannot exist on macOS and nothing will ever be
    found under this name. Phase 0 leaves the constant alone: making it None and
    short-circuiting the trt probe and its doctor check is phase 2's job."""
    assert osapi.VSTRT_LIB == "libvstrt.so"
