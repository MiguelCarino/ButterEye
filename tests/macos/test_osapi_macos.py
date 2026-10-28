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

Phase 0's share is the three places macOS raised ``AttributeError`` where Linux
answered. Phase 1's is the rest of the seam: ``pid_alive`` through libproc rather
than ``/proc``, the ``.dylib`` extension, and TensorRT refused by the generated
script, which is where macOS's "never" is actually acted on. The paths half of
phase 1 is in ``test_paths_macos.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")

if sys.platform == "darwin":
    from buttereye.core import osapi
    from buttereye.core.errors import ButterEyeError, ErrorCode
    from buttereye.core.hw import detect
    from buttereye.core.mpvctl import ipc, launcher
    from buttereye.core.osapi import _darwin as d
    from buttereye.core.scriptgen import generator
    from buttereye.core.testing import scenarios as sc


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


# ---------------------------------------------------------------- process state
def _ps_state(pid: int) -> str:
    """What ``ps`` says ``pid``'s state is (``Z`` for a zombie), "" when it is gone.

    An oracle outside the code under test, and the only one available: the two
    ways ButterEye could ask - ``os.kill(pid, 0)`` and ``os.waitpid`` - would
    respectively lie about a zombie and destroy the thing being measured.
    """
    out = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return out.stdout.strip()


def test_an_exited_but_unwaited_child_is_not_alive() -> None:
    """The defect phase 1 fixes, and the reason it reads a process without /proc.

    ``pid_alive`` is what tells ops.py and launcher.py that the mpv or vspipe
    they started has finished. The POSIX body's own ``os.kill(pid, 0)`` cannot
    answer it: a zombie - a child that has exited and not been waited for -
    still accepts signal 0, which is asserted below so the test says what it is
    measuring. Linux reads ``/proc/<pid>/stat`` for the Z, and on macOS that
    open raised FileNotFoundError into an ``except OSError: return True``, so
    every zombie read as alive and an operation that had ended looked live.

    The child is spawned rather than forked: pytest runs with the memory
    watchdog's thread alive, and forking a multi-threaded process is deprecated
    in 3.14. Nothing reaps it in the meantime - ``Popen`` waits only when asked,
    and 3.14 has no asyncio child watcher that could take an arbitrary child -
    so the zombie stands until the ``wait()`` below.
    """
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(500):  # dying takes a moment; wait for the state, not a clock
            if _ps_state(child.pid).startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"the child never became a zombie (ps says {_ps_state(child.pid)!r})")
        os.kill(child.pid, 0)  # no ProcessLookupError: this is what misleads the POSIX body
        assert osapi.pid_alive(child.pid) is False
        assert d.pid_alive(child.pid) is False
    finally:
        child.wait()
    # and once it has been waited for the pid is simply gone, which libproc
    # reports with the same ESRCH as the zombie did
    assert osapi.pid_alive(child.pid) is False


def test_a_running_process_is_alive() -> None:
    """The other half: libproc must not answer False for a live process either.
    This one is certainly running, since it is asking."""
    assert osapi.pid_alive(os.getpid()) is True
    assert d.pid_alive(os.getpid()) is True


def test_a_process_of_another_user_is_still_alive() -> None:
    """``proc_pidinfo`` returns EPERM for a process we do not own, where Linux's
    ``/proc`` is world-readable. pid 1 (launchd) is root's, and refusing to
    answer must not become "it has exited": that would make ButterEye think
    someone else's mpv had finished and stop driving a session that is running.

    Both doors are asked because they reach different code. ``osapi.pid_alive``
    answers True from its own ``os.kill(pid, 0)`` PermissionError arm and never
    gets as far as libproc, so on its own it says nothing about what libproc
    does with EPERM. ``d.pid_alive`` is the libproc call with nothing in front
    of it: measured, ``proc_pidinfo(1, PROC_PIDTBSDINFO, ...)`` returns 0 and
    leaves errno EPERM, which falls to the "any errno but ESRCH is alive" line.
    Reading that refusal as "gone" is the mutation this catches."""
    assert osapi.pid_alive(1) is True
    assert d.pid_alive(1) is True


# ---------------------------------------------------------------- libraries
def test_plugin_file_asks_for_a_dylib() -> None:
    """The plugins are named for Linux in the code; macOS wants the same stems
    with ``.dylib``, which is what Homebrew installs (``mvtools.dylib``,
    ``libffms2.dylib``) and what VapourSynth R81 hardcodes per OS in
    ``src/core/vscore.cpp``. Only the extension moves: no prefix changes."""
    assert osapi.plugin_file("mvtools.so") == "mvtools.dylib"
    assert osapi.plugin_file("librife.so") == "librife.dylib"
    assert osapi.plugin_file("libmvgpu.so") == "libmvgpu.dylib"
    assert osapi.plugin_file("libffms2.so") == "libffms2.dylib"


def test_plugin_file_leaves_anything_else_exactly_as_it_is() -> None:
    """The rule is a suffix swap and nothing more. A versioned soname does not
    end in ``.so`` and is not a plugin file name, and neither is the Python
    module beside the plugins; both have to come back untouched."""
    assert osapi.plugin_file("mvtools.dylib") == "mvtools.dylib"
    assert osapi.plugin_file("vsmlrt.py") == "vsmlrt.py"
    assert osapi.plugin_file("libsomething.so.1") == "libsomething.so.1"


def test_the_vstrt_plugin_name_is_not_swapped_to_a_dylib() -> None:
    """``VSTRT_LIB`` is the one plugin name that does not go through
    ``plugin_file``, and that is deliberate: ``trt._vstrt_dirs`` and
    ``probe.user_vstrt`` glob for it, and a ``.dylib`` there would be a name no
    build has ever produced for macOS, which reads as "look again" rather than
    "this cannot exist". So the constant keeps the Linux name while
    ``plugin_file`` would have said ``libvstrt.dylib``."""
    assert osapi.VSTRT_LIB == "libvstrt.so"
    assert osapi.plugin_file(osapi.VSTRT_LIB) == "libvstrt.dylib"  # not what callers use


@pytest.mark.integration  # needs vspipe, which evaluates the script
def test_the_generated_script_refuses_tensorrt_outright(tmp_path: Path) -> None:
    """TensorRT needs CUDA, no Apple Silicon Mac has an NVIDIA GPU, and that is
    permanent rather than pending. ``template.vpy``'s ``_rife_trt`` is the one
    place in the tree that acts on it, so it is the one place worth testing.

    Real ``vspipe`` evaluating a real generated script, asked for the
    ``rife-trt`` backend over a blank source, so nothing but VapourSynth itself
    has to be installed. The refusal has to come from the macOS arm and not from
    the engine check three lines below it: an unbuilt engine says "not built
    yet", which sends the user off to build one, and no build on this machine
    can ever produce it. Deleting that arm is the mutation this catches - the
    message then becomes the engine one.

    The seam's own ``HAS_VSTRT`` is not asserted here, because a test of its
    value would be a test of a line of source. What reads it is
    ``trt._vstrt_dirs``, the one place that looks for vstrt at all -- doctor's
    probe asks through it too -- and
    ``test_a_planted_vstrt_is_not_found_on_macos`` covers that."""
    script = tmp_path / "gen.vpy"
    generator.write_script(script)
    user_data = {
        "v": 1,
        "gen": 1,
        "target_fps": [60, 1],
        "source": {"kind": "blank", "width": 640, "height": 360, "fps": [24, 1], "length": 48},
        "backend": "rife-trt",
        # an engine folder with no ready.json in it, which is what the check
        # below the macOS arm complains about: without that arm this run would
        # reach it and blame the missing engine
        "trt": {"engine_dir": os.fspath(tmp_path / "engines" / "v4_26-640x360-fp16-h-0")},
    }
    res = subprocess.run(
        ["vspipe", "-a", "user_data=" + json.dumps(user_data), "--info", os.fspath(script), "-"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert res.returncode != 0, res.stdout
    assert "TensorRT is not available on macOS" in res.stderr
    assert "not built yet" not in res.stderr, "it got as far as the engine check"


# ---------------------------------------------------------------- watching an exit
def test_the_exit_watch_sees_a_process_that_is_not_our_child() -> None:
    """macOS has no ``pidfd_open``; a kqueue ``EVFILT_PROC``/``NOTE_EXIT`` filter
    is the same watchable descriptor, and this is the case that decides whether it
    can replace the 10 Hz poll at all.

    ButterEye watches mpv processes it did not start - ``buttereye attach``, and
    any mpv that outlived the GUI that launched it - so a watch that only worked
    for a child would be no use. The process here is a grandchild whose parent
    exited immediately, so it has been reparented to launchd: ours by uid, not by
    descent. It is killed rather than left to finish, which also leaves no zombie
    behind, since launchd reaps what it has adopted.
    """
    orphan = (
        "import os, subprocess, sys\n"
        # the null device, not the inherited pipe: a grandchild holding the write
        # end open would keep the run() below waiting for the whole sleep
        "child = subprocess.Popen(['sleep', '30'], stdin=subprocess.DEVNULL,\n"
        "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "sys.stdout.write(f'{child.pid}\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"  # exit without waiting: the sleep is handed to launchd
    )
    first = subprocess.run(
        [sys.executable, "-c", orphan], capture_output=True, text=True, check=True, timeout=60
    )
    pid = int(first.stdout)
    assert _ps_state(pid), "the orphan was gone before the watch could be made"
    ppid = subprocess.run(
        ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert ppid == "1", f"it is still ours by descent (ppid {ppid}), so this proves nothing"
    fd = osapi.DetachedChild(pid).open_pidfd()
    assert fd is not None, "no kqueue for an orphan: the poll fallback would be the answer"
    try:
        assert select.select([fd], [], [], 0)[0] == [], "readable while the process is running"
        os.kill(pid, signal.SIGKILL)
        assert select.select([fd], [], [], 10.0)[0] == [fd], "the exit never woke the watch"
        # level-triggered, because MpvProcess never dequeues the event: the
        # descriptor has to stay readable or the loop would hang on the next wait
        assert select.select([fd], [], [], 0)[0] == [fd]
    finally:
        os.close(fd)


def test_there_is_no_exit_watch_for_a_process_that_has_already_gone() -> None:
    """ESRCH when the filter cannot be registered, and None rather than a
    descriptor that would never fire. The caller then polls, and its first poll
    already sees the exit, so nothing waits forever."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert d.exit_watch(child.pid) is None


def test_a_planted_vstrt_is_not_found_on_macos(tmp_path: Path) -> None:
    """``trt._vstrt_dirs`` refuses on macOS even with a plugin sitting there.

    It is the one place anything looks for vstrt -- ``doctor.probe.user_vstrt``
    asks through it -- so this is where ``osapi.HAS_VSTRT`` earns its keep.

    Both names are planted on purpose. ``VSTRT_LIB`` keeps the Linux spelling on
    macOS (it is the only plugin name that does not go through ``plugin_file``),
    so that is what the glob would match and what makes the guard load-bearing:
    without it this directory is found and a CUDA-only backend gets offered on a
    machine that can never run it. The ``.dylib`` is what a real macOS build
    would be called, and must not be found either. Planting only the ``.dylib``
    would pass whether the guard is there or not -- the glob would simply miss
    it -- which is a test of the accident rather than of the fix.
    """
    from buttereye.core.backends import trt

    paths = sc.fake_paths(root=tmp_path)
    built = paths.data_dir / trt.VSTRT_SUBDIR / "v15.0"
    built.mkdir(parents=True)
    for name in (osapi.VSTRT_LIB, osapi.plugin_file(osapi.VSTRT_LIB)):
        (built / name).write_bytes(b"\xcf\xfa\xed\xfe")  # Mach-O magic, so it looks real
    assert sorted(p.name for p in built.iterdir()) == ["libvstrt.dylib", "libvstrt.so"]
    assert trt._vstrt_dirs(paths) == []


# ---------------------------------------------------------------- power and heat
def test_power_answers_come_from_the_real_api() -> None:
    """What this Mac says right now: the types and ranges, not the values (a CI
    runner has no battery; a laptop may be on either)."""
    thermal = d.thermal_state()
    assert thermal in (0, 1, 2, 3)
    assert isinstance(d.low_power_mode(), bool)
    assert d.providing_power_source() in ("AC Power", "Battery Power", "UPS Power")
    assert isinstance(d.on_battery(), bool)
    snap = osapi.power_snapshot()
    assert snap.thermal in (0, 1, 2, 3) and isinstance(snap.on_battery, bool)


@pytest.mark.parametrize(
    ("source", "battery"),
    [("AC Power", False), ("Battery Power", True), ("UPS Power", True), ("Off Line", False)],
)
def test_power_source_names(source: str, battery: bool) -> None:
    assert d.on_battery(source) is battery


def test_power_answers_none_when_the_runtime_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "_objc_cache", [None])
    monkeypatch.setattr(d, "_ps_cache", [None])
    assert d.thermal_state() is None
    assert d.low_power_mode() is None
    assert d.providing_power_source() is None and d.on_battery() is None
    assert d.begin_activity("x") is None
    d.end_activity(1)  # nothing to end, and no raise


def test_out_of_range_thermal_state_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    rt = d._objc()
    assert rt is not None
    real = rt.send
    monkeypatch.setattr(
        rt, "send", lambda restype, *a: 7 if a[1] == "thermalState" else real(restype, *a)
    )
    assert d.thermal_state() is None


def test_activity_holds_an_assertion_until_ended() -> None:
    """``pmset -g assertions`` lists what holds the machine awake, with its reason."""
    reason = f"ButterEye test {os.getpid()}"

    def listed() -> bool:
        out = subprocess.run(
            ["/usr/bin/pmset", "-g", "assertions"], capture_output=True, text=True, check=True
        )
        return reason in out.stdout

    with osapi.activity(reason) as token:
        assert isinstance(token, int)
        assert listed()
    assert not listed()


def test_caffeinate_ends_with_the_pid_it_watches() -> None:
    sleeper = subprocess.Popen(["/bin/sleep", "30"])
    try:
        awake = osapi.keep_awake(sleeper.pid)
        assert awake is not None
        assert awake.args == ["/usr/bin/caffeinate", "-i", "-w", str(sleeper.pid)]
        time.sleep(0.2)
        assert awake.poll() is None
    finally:
        sleeper.kill()
        sleeper.wait()
    assert awake.wait(timeout=5) == 0
