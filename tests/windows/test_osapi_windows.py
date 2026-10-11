# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Windows half of ``buttereye.core.osapi``, against the real Windows API.

Skipped everywhere but Windows. On the Linux dev box they run under Wine with a
Windows Python (``tools/wine-tests.sh``); on a Windows machine, plainly with
pytest. Nothing here is mocked: real job objects, process trees, named pipes,
file locks and, when ``BUTTEREYE_TEST_MPV`` and ``BUTTEREYE_TEST_VIDEO`` name a
Windows ``mpv.exe`` and a short video, a real mpv driven over its pipe.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")

if sys.platform == "win32":
    from buttereye.core import osapi, paths
    from buttereye.core.errors import ButterEyeError, ErrorCode
    from buttereye.core.mpvctl import ipc, launcher
    from buttereye.core.osapi import _win32 as w

SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]
#: a parent that starts a grandchild sleeper and sleeps itself
PARENT = [
    sys.executable,
    "-c",
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)",
]


def _wait_until(cond: object, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():  # type: ignore[operator]
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------- folders
def test_windows_paths_live_under_the_profile(tmp_path: Path) -> None:
    env = {
        "USERPROFILE": str(tmp_path),
        "APPDATA": str(tmp_path / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Local"),
    }
    p = paths.resolve(env)
    assert p.config_file == tmp_path / "Roaming" / "ButterEye" / "config.toml"
    assert p.runtime_dir == tmp_path / "Local" / "ButterEye" / "run"
    assert p.logs_dir == tmp_path / "Local" / "ButterEye" / "state" / "logs"
    assert p.user_mpv_conf == tmp_path / "Roaming" / "mpv" / "mpv.conf"
    # created on first use, then accepted by the same check
    assert paths.ensure_runtime_dir(p) == p.runtime_dir
    paths.check_private_dir(p.runtime_dir, must_exist=True)


def test_a_file_we_make_is_ours(tmp_path: Path) -> None:
    f = tmp_path / "mine.txt"
    f.write_text("x")
    assert w.my_sid() is not None and w.my_sid().startswith("S-1-")
    assert w.my_owner_sid() is not None  # whom our new files belong to (may be a group)
    assert osapi.owned_by_me(f, os.lstat(f))


def test_a_file_is_not_a_runtime_folder(tmp_path: Path) -> None:
    f = tmp_path / "not-a-folder"
    f.write_text("x")
    with pytest.raises(ButterEyeError) as ei:
        paths.check_private_dir(f, must_exist=True)
    assert ei.value.code is ErrorCode.RUNTIME_DIR_UNSAFE
    assert ei.value.cause.params["why"] == "it is not a folder"


# ---------------------------------------------------------------- locks
def test_lock_is_exclusive_between_handles(tmp_path: Path) -> None:
    f = tmp_path / "lock"
    a = os.open(f, os.O_RDWR | os.O_CREAT | osapi.O_BINARY)
    b = os.open(f, os.O_RDWR | osapi.O_BINARY)
    try:
        assert osapi.try_lock(a)
        assert not osapi.try_lock(b)
        osapi.unlock(a)
        assert osapi.try_lock(b)
        osapi.unlock(b)
    finally:
        os.close(a)
        os.close(b)


def test_locked_dir_serialises_and_releases(tmp_path: Path) -> None:
    with osapi.locked_dir(tmp_path):
        fd = os.open(tmp_path / ".lock", os.O_RDWR | osapi.O_BINARY)
        try:
            assert not osapi.try_lock(fd)
        finally:
            os.close(fd)
    with osapi.locked_dir(tmp_path):
        pass  # released on exit, so this does not wait


# ---------------------------------------------------------------- processes
def test_executable_prefers_the_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("tool.com", "tool.exe"):
        (tmp_path / name).write_bytes(b"MZ")
    monkeypatch.setenv("PATH", str(tmp_path))
    found = osapi.executable("tool")
    assert found is not None and found.lower().endswith("tool.exe")


def test_executable_never_comes_from_the_current_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    planted = tmp_path / "Downloads"
    planted.mkdir()
    (planted / "tool.exe").write_bytes(b"MZ")
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", ".")  # a relative entry is the current folder too
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    assert osapi.executable("tool") is None
    real = tmp_path / "bin"
    real.mkdir()
    (real / "tool.exe").write_bytes(b"MZ")
    monkeypatch.setenv("PATH", f".;{real}")
    found = osapi.executable("tool")
    assert found is not None and os.path.isabs(found)
    assert os.path.normcase(found) == os.path.normcase(str(real / "tool.exe"))


def test_tree_finds_the_grandchild_and_signal_group_ends_it() -> None:
    p = subprocess.Popen(PARENT, **osapi.group_spawn_kwargs())
    try:
        assert _wait_until(lambda: len(w.tree(p.pid)) >= 2), w.tree(p.pid)
        pids = w.tree(p.pid)
        assert pids[0] == p.pid
        # no windows in this tree: the polite form terminates at once
        osapi.signal_group(p.pid, kill=False)
        assert _wait_until(lambda: not any(osapi.pid_alive(x) for x in pids))
        assert not osapi.group_alive(p.pid)
    finally:
        p.kill()
        p.wait()


def test_pid_alive_tracks_exit() -> None:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not osapi.pid_alive(p.pid)
    q = subprocess.Popen(SLEEPER)
    try:
        assert osapi.pid_alive(q.pid)
    finally:
        q.kill()
        q.wait()


def test_helpers_die_with_buttereye_but_mpv_does_not(tmp_path: Path) -> None:
    """The job: a process that joined it (ensure_job) takes its group children
    with it when it exits, while a spawn_detached child (mpv) breaks away."""
    script = tmp_path / "jobcheck.py"
    script.write_text(
        textwrap.dedent(f"""
        import subprocess, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from pathlib import Path
        from buttereye.core import osapi
        helper = subprocess.Popen({SLEEPER!r}, **osapi.group_spawn_kwargs())
        mpv = osapi.spawn_detached({SLEEPER!r}, Path({str(tmp_path / "mpv.log")!r}))
        print(helper.pid, mpv.pid, flush=True)
    """)
    )
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         timeout=60, check=True)  # fmt: skip
    helper, mpv = (int(x) for x in out.stdout.split())
    try:
        assert _wait_until(lambda: not osapi.pid_alive(helper)), "helper outlived ButterEye"
        assert osapi.pid_alive(mpv), "mpv must outlive ButterEye"
    finally:
        for pid in (helper, mpv):
            w.terminate(pid)


async def test_ops_process_group_terminates_on_timeout() -> None:
    from buttereye.core.doctor.proc import run

    t0 = time.monotonic()
    res = await run(PARENT, timeout_s=1.0, grace_s=0.5)
    assert res.timed_out and res.pid is not None
    assert time.monotonic() - t0 < 10
    assert _wait_until(lambda: not osapi.group_alive(res.pid))


# ---------------------------------------------------------------- named pipes
async def test_pipe_endpoint_and_peer_check(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    endpoint = runtime / "mpv-test.sock"
    osapi.prepare_endpoint(endpoint)
    assert ipc.check_socket_path(endpoint) is None

    loop = asyncio.get_running_loop()
    got: list[bytes] = []

    class Echo(asyncio.Protocol):
        def connection_made(self, transport: asyncio.BaseTransport) -> None:
            self.t = transport

        def data_received(self, data: bytes) -> None:
            got.append(data)
            self.t.write(b'{"request_id": 1, "error": "success", "data": 42}\n')  # type: ignore[attr-defined]

    servers = await loop.start_serving_pipe(Echo, osapi.ipc_address(endpoint))  # type: ignore[attr-defined]
    try:
        conn = await ipc.MpvIpc.connect(endpoint)
        assert conn.peer_pid == os.getpid()  # we serve the pipe ourselves here
        conn.start()
        assert await conn.command("get_property", "pid") == 42
        assert b"get_property" in b"".join(got)
        await conn.close()
    finally:
        for s in servers:
            s.close()


async def test_nobody_serving_reads_as_not_found(tmp_path: Path) -> None:
    endpoint = tmp_path / "mpv-gone.sock"
    osapi.prepare_endpoint(endpoint)
    with pytest.raises((FileNotFoundError, ConnectionRefusedError)):
        await ipc.MpvIpc.connect(endpoint, timeout_s=1.0)


def test_a_directory_is_not_an_endpoint(tmp_path: Path) -> None:
    d = tmp_path / "mpv-x.sock"
    d.mkdir()
    assert ipc.check_socket_path(d) is not None


# ---------------------------------------------------------------- real mpv
MPV = os.environ.get("BUTTEREYE_TEST_MPV", "")
VIDEO = os.environ.get("BUTTEREYE_TEST_VIDEO", "")


@pytest.mark.skipif(not (MPV and VIDEO), reason="BUTTEREYE_TEST_MPV / _VIDEO not set")
async def test_real_mpv_over_its_pipe(tmp_path: Path) -> None:
    runtime = tmp_path / "run"
    runtime.mkdir()
    socket = runtime / "mpv-e2e.sock"
    include = tmp_path / "buttereye.conf"
    launcher.write_include(include)
    argv = launcher.build_argv(MPV, include=include, socket=socket, file=Path(VIDEO),
                               extra=("--vo=null", "--ao=null", "--loop-file=inf"))  # fmt: skip
    log = tmp_path / "mpv.log"
    launcher.prepare_socket(socket)
    proc = await launcher.spawn_mpv(argv, log)
    try:
        conn = await launcher.connect(socket, proc, log, timeout_s=30.0)
        conn.start()
        assert conn.peer_pid == proc.pid
        assert await conn.get("pid") == proc.pid
        version = await conn.get("mpv-version")
        assert isinstance(version, str) and version.startswith("mpv")
        await conn.close()
    finally:
        await launcher.terminate(proc, grace_s=2.0)
    assert proc.returncode is not None
    assert not osapi.pid_alive(proc.pid)


@pytest.mark.skipif(
    not (MPV and os.environ.get("BUTTEREYE_TEST_WINDOWED")),
    reason="opens a real mpv window: set BUTTEREYE_TEST_WINDOWED=1",
)
async def test_real_mpv_window_closes_politely(tmp_path: Path) -> None:
    """``terminate``'s first step closes mpv's window (WM_CLOSE): mpv quits on
    its own with exit code 0, well before the grace period would force it."""
    argv = [MPV, "--force-window=immediate", "--idle=yes", "--ao=null"]
    proc = await launcher.spawn_mpv(argv, tmp_path / "mpv.log")
    await asyncio.sleep(5)  # let the window appear
    assert proc.returncode is None
    t0 = time.monotonic()
    await launcher.terminate(proc, grace_s=10.0)
    assert proc.returncode == 0
    assert time.monotonic() - t0 < 5


# ---------------------------------------------------------------- files the core writes
def _win_paths(tmp_path: Path) -> paths.Paths:
    return paths.resolve(
        {
            "USERPROFILE": str(tmp_path),
            "APPDATA": str(tmp_path / "Roaming"),
            "LOCALAPPDATA": str(tmp_path / "Local"),
        }
    )


def test_settings_save_over_an_existing_file_and_read_back(tmp_path: Path) -> None:
    from buttereye.core.profiles import config

    p = _win_paths(tmp_path)
    first = config.load(p)
    assert not first.exists
    rev = config.save(p, first.config, expected_revision=first.revision)
    again = config.load(p)
    assert again.exists and again.revision == rev and again.config == first.config
    # the second save renames onto an existing file, under the folder lock
    assert config.save(p, again.config, expected_revision=rev) == rev


def test_script_written_atomically_twice(tmp_path: Path) -> None:
    from buttereye.core.scriptgen.generator import write_atomic

    target = tmp_path / "mpv-1.vpy"
    write_atomic(target, "a = 1\n")
    write_atomic(target, "a = 2\n")
    assert target.read_text() == "a = 2\n"
    assert [x.name for x in tmp_path.iterdir()] == ["mpv-1.vpy"]  # no temp left behind


async def test_storage_clean_removes_a_junction_not_its_target(tmp_path: Path) -> None:
    import _winapi
    import stat

    from buttereye.core import storage
    from buttereye.core.types import CleanTarget

    p = _win_paths(tmp_path)
    engines = storage.target_dir(p, CleanTarget.ENGINES)
    engines.mkdir(parents=True)
    (engines / "a.engine").write_bytes(b"x" * 10)
    (engines / "sub").mkdir()
    (engines / "sub" / "b.engine").write_bytes(b"y" * 5)
    ro = engines / "read-only.engine"
    ro.write_bytes(b"z")
    os.chmod(ro, stat.S_IREAD)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    try:
        _winapi.CreateJunction(str(outside), str(engines / "link"))
    except OSError:
        pytest.skip("this system can't make junctions")
    res = await storage.clean(p, frozenset({CleanTarget.ENGINES}), lambda _p: None)
    assert (outside / "keep.txt").read_text() == "keep", "followed a junction"
    assert list(engines.iterdir()) == []
    assert res.freed_bytes == 16
    assert res.skipped == ()


# ---------------------------------------------------------------- the doctor (phase 2)
LINUX_WORDS = ("dnf", "sudo", "rpm ", "usermod", "copr")


async def test_doctor_runs_and_speaks_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole doctor on Windows: it finishes, reads the install record in place
    of rpm, and no advice mentions a Linux tool."""
    import json

    from buttereye.core.doctor import checks_pkgs as pk
    from buttereye.core.doctor.run import doctor
    from buttereye.core.profiles.defaults import default_config

    install = tmp_path / "ButterEye"
    plugins = install / "vapoursynth"
    plugins.mkdir(parents=True)
    for name in ("librife.dll", "mvtools.dll", "libmvgpu.dll"):
        (plugins / name).write_bytes(b"MZ")
    models = install / "share" / pk.MODELS_SUBDIR / "rife-v4.22-lite"
    models.mkdir(parents=True)
    (install / "licenses").mkdir()
    (install / "licenses" / "LICENSE").write_text("MIT")
    components = [
        {"name": n, "version": "1-1", "license": "MIT", "licence_files": ["licenses/LICENSE"]}
        for n in (pk.PKG_RIFE, pk.PKG_MVTOOLS, pk.PKG_MODELS, pk.PKG_MVGPU)
    ]
    components[0]["provides"] = [["bundled(ncnn)", "20250916"]]
    (install / pk.INSTALL_MANIFEST).write_text(json.dumps({"version": 1, "components": components}))
    monkeypatch.setenv("BUTTEREYE_HOME", str(install))
    if MPV:
        monkeypatch.setenv("PATH", str(Path(MPV).parent) + os.pathsep + os.environ["PATH"])

    p = _win_paths(tmp_path)
    assert p.rpm_plugin_dir == plugins
    report = await doctor(p, default_config(), trt=None, progress=lambda _p: None)
    by_id = {f.id: f for f in report.findings}

    for f in report.findings:
        for c in f.commands:
            assert not any(w in c.lower() for w in LINUX_WORDS), (f.id, c)
    assert by_id["packages.rife"].severity.value == "ok", by_id["packages.rife"]
    assert by_id["packages.mvtools"].severity.value == "ok"
    assert by_id["packages.mvgpu"].severity.value == "ok"
    assert by_id["packages.licence_files"].severity.value == "ok"
    assert by_id["gpu_fault.xid"].title.key == "GPU faults aren't checked on Windows"
    assert by_id["trt.off"].title.key.startswith("Experimental NVIDIA TensorRT isn't available")
    assert "conflicts.mpvsockets" not in by_id  # a Unix-socket script: nothing to find here
    if MPV:
        assert by_id["mpv.version"].severity.value == "ok", by_id["mpv.version"]
    else:
        assert any(c.startswith("winget install") for c in by_id["mpv.binary"].commands)
