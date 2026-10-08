# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv JSON-IPC client: command allowlist, socket checks, replies, events (SCOPE §4.3)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import ipc as m
from buttereye.core.types import RefusalReason

DATA = "@buttereye:vapoursynth=file=%8%/a/b.vpy:buffered-frames=4:concurrent-frames=4"


@pytest.mark.parametrize(
    "cmd",
    [
        ("observe_property", 1, "vf"),
        ("observe_property", 2, "estimated-vf-fps"),
        ("get_property", "pid"),
        ("get_property", "video-params"),
        ("set_property", "interpolation", False),
        ("set_property", "hwdec", "auto-copy"),
        ("request_log_messages", "warn"),
        ("vf", "add", DATA),
        ("vf", "add", DATA + ':user-data=%7%{"a":1}'),
        ("vf", "remove", "@buttereye"),
        ("vf", "toggle", "@buttereye"),
        ("script-message-to", "buttereye", "status", "hello"),
        ("show-text", "hi"),
        ("show-text", "hi", 2000),
        ("client_name",),
        ("get_version",),
    ],
)
def test_allowed(cmd: tuple[object, ...]) -> None:
    m.check_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        (),
        ("run", "sh", "-c", "id"),
        ("subprocess", ["sh"]),
        ("load-script", "/tmp/evil.lua"),
        ("loadfile", "/etc/passwd"),
        ("quit",),
        ("keypress", "q"),
        ("observe_property", 1, "user-data"),
        ("observe_property", "1", "vf"),
        ("get_property", "input-ipc-server"),
        ("set_property", "vf", ""),
        ("set_property", "input-ipc-server", "/tmp/x"),
        ("request_log_messages", "trace"),
        ("vf", "add", "@other:vapoursynth=file=%1%x"),
        ("vf", "add", "vapoursynth=file=%1%x"),
        ("vf", "add", "@buttereye:lavfi=[movie=x]"),
        ("vf", "add", DATA + ",@evil:lavfi=[movie=/etc/passwd]"),
        ("vf", "add", "@buttereye:vapoursynth=file=/a/b.vpy,lavfi=x"),
        ("vf", "add", "@buttereye:vapoursynth=file=%99%short"),
        ("vf", "add", "@buttereye:vapoursynth=user-data=%1%x"),
        ("vf", "add", DATA + ":file=%1%y"),
        ("vf", "add", DATA + ":evil=1"),
        ("vf", "set", ""),
        ("vf", "clr", ""),
        ("vf", "remove", "@other"),
        ("vf", "toggle", "@buttereye,@other"),
        ("script-message-to", "other-script", "x"),
        ("script-message", "buttereye-request", "toggle"),
        ("show-text", "hi", "2000"),
        ("client_name", "x"),
    ],
)
def test_refused(cmd: tuple[object, ...]) -> None:
    with pytest.raises(m.CommandRefused):
        m.check_command(cmd)


def test_quote_is_byte_length() -> None:
    assert m.quote("abc") == "%3%abc"
    assert m.quote("ü") == "%2%ü"
    assert m.quote("") == "%0%"


# ---------------------------------------------------------------------------
# A scripted stand-in for mpv's IPC server (test only)
# ---------------------------------------------------------------------------


class FakeMpv:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.writers: list[asyncio.StreamWriter] = []
        self.props: dict[str, Any] = {"pid": 4242, "vf": []}
        self.reply_delay = 0.0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writers.append(writer)
        while line := await reader.readline():
            msg = json.loads(line)
            self.received.append(msg)
            cmd, rid = msg["command"], msg.get("request_id", 0)
            if self.reply_delay:
                await asyncio.sleep(self.reply_delay)
            if cmd[0] == "get_property":
                if cmd[1] in self.props:
                    out = {"request_id": rid, "error": "success", "data": self.props[cmd[1]]}
                else:
                    out = {"request_id": rid, "error": "property unavailable"}
            elif cmd[0] == "vf" and cmd[1] == "add":
                out = {"request_id": rid, "error": "success", "data": None}
            else:
                out = {"request_id": rid, "error": "success", "data": None}
            writer.write((json.dumps(out) + "\n").encode())
            await writer.drain()

    async def send(self, event: dict[str, Any]) -> None:
        for w in self.writers:
            w.write((json.dumps(event) + "\n").encode())
            await w.drain()


@pytest.fixture
async def server(xdg_env: Any) -> AsyncIterator[tuple[FakeMpv, Path]]:
    d = xdg_env.runtime / "buttereye"
    d.mkdir(mode=0o700)
    path = d / "mpv-test.sock"
    fake = FakeMpv()
    srv = await asyncio.start_unix_server(fake.handle, path=os.fspath(path))
    try:
        yield fake, path
    finally:
        for w in fake.writers:
            w.close()
        srv.close()
        with contextlib.suppress(Exception):
            await srv.wait_closed()


async def test_command_reply_and_get(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    client = await m.MpvIpc.connect(path)
    assert client.peer_pid == os.getpid()  # SO_PEERCRED of our in-process server
    client.start()
    try:
        assert await client.get("pid") == 4242
        assert await client.get("video-params") is None  # unavailable → None
        await client.observe("vf")
        await client.observe("vf")  # idempotent
        assert [r["command"] for r in fake.received] == [
            ["get_property", "pid"],
            ["get_property", "video-params"],
            ["observe_property", 1, "vf"],
        ]
        ids = [r["request_id"] for r in fake.received]
        assert len(set(ids)) == len(ids)
    finally:
        await client.close()


async def test_refused_command_is_never_sent(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    client = await m.MpvIpc.connect(path)
    client.start()
    try:
        with pytest.raises(m.CommandRefused):
            await client.command("run", "sh")
        with pytest.raises(m.CommandRefused):
            await client.command("loadfile", "x")
        await client.command("client_name")
        assert [r["command"][0] for r in fake.received] == ["client_name"]
    finally:
        await client.close()


async def test_events_reach_handlers(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    client = await m.MpvIpc.connect(path)
    got: list[Any] = []
    client.add_handler(lambda ev: got.append(dict(ev)))

    def broken(ev: Any) -> None:
        raise RuntimeError("handler bug")

    client.add_handler(broken)  # a broken handler does not stop the others
    client.start()
    try:
        await client.command("client_name")
        await fake.send({"event": "property-change", "id": 1, "name": "vf", "data": []})
        await fake.send({"event": "log-message", "prefix": "vf", "level": "error", "text": "x"})
        for _ in range(50):
            if len(got) == 2:
                break
            await asyncio.sleep(0.01)
        assert [e["event"] for e in got] == ["property-change", "log-message"]
    finally:
        await client.close()


async def test_error_reply_raises_mpv_error(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    fake.props.pop("pid")
    client = await m.MpvIpc.connect(path)
    client.start()
    try:
        with pytest.raises(m.MpvError) as info:
            await client.command("get_property", "pid")
        assert info.value.error == "property unavailable"
    finally:
        await client.close()


async def test_eof_fails_pending_and_sets_closed(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    fake.reply_delay = 5.0
    client = await m.MpvIpc.connect(path)
    client.start()
    pending = asyncio.ensure_future(client.command("client_name", timeout_s=10))
    await asyncio.sleep(0.1)
    for w in fake.writers:
        w.close()
    with pytest.raises(m.IpcClosed):
        await pending
    await asyncio.wait_for(client.closed.wait(), 2)
    with pytest.raises(m.IpcClosed):
        await client.command("client_name")
    await client.close()


async def test_timeout(server: tuple[FakeMpv, Path]) -> None:
    fake, path = server
    fake.reply_delay = 1.0
    client = await m.MpvIpc.connect(path)
    client.start()
    try:
        with pytest.raises(TimeoutError):
            await client.command("client_name", timeout_s=0.1)
    finally:
        await client.close()


async def test_socket_checks(xdg_env: Any, tmp_path: Path) -> None:
    d = xdg_env.runtime
    regular = d / "not-a-socket"
    regular.write_text("x")
    assert m.check_socket_path(regular) is RefusalReason.NOT_SOCKET
    with pytest.raises(m.SocketRefused) as info:
        await m.MpvIpc.connect(regular)
    assert info.value.reason is RefusalReason.NOT_SOCKET
    with pytest.raises(FileNotFoundError):
        m.check_socket_path(d / "missing.sock")
    # a real socket in our own dir passes
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(os.fspath(d / "ok.sock"))
        assert m.check_socket_path(d / "ok.sock") is None
    finally:
        s.close()


async def test_socket_in_foreign_parent_refused(
    xdg_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = xdg_env.runtime
    s = socket.socket(socket.AF_UNIX)
    try:
        s.bind(os.fspath(d / "x.sock"))
        real_lstat: Callable[..., os.stat_result] = os.lstat

        def fake_lstat(p: Any, *a: Any, **kw: Any) -> os.stat_result:
            st = real_lstat(p, *a, **kw)
            if Path(p) == d:  # pretend another user owns the folder
                vals = list(st)
                vals[4] = os.getuid() + 1
                return os.stat_result(vals)
            return st

        monkeypatch.setattr(os, "lstat", fake_lstat)
        assert m.check_socket_path(d / "x.sock") is RefusalReason.BAD_PARENT
    finally:
        s.close()
