# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Lua helper's hold for "Start videos smooth", in a real mpv (no plugins).

mpv runs with ``--vo=null --ao=null`` on a lavfi test picture, started the way
``launcher.preload_args`` starts it. The test plays ButterEye's part over a raw
IPC client: the helper must let go on ``preload-release``, on its own after
the limit plus ``PRELOAD_GRACE_S``, and tell ButterEye when the user unpauses;
a later playlist entry is held again, unless the user had paused or ButterEye
said ``preload-off``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.mpvctl import launcher

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(osapi.IS_WINDOWS, reason="a Unix-socket test client"),
    pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv"),
]

PICTURE = "av://lavfi:testsrc=size=320x240:rate=24"


class Raw:
    """A minimal JSON-IPC client: replies by request id, events in a list."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.r, self.w = reader, writer
        self.events: list[tuple[float, dict[str, Any]]] = []
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.rid = 0
        self.task = asyncio.ensure_future(self._read())

    async def _read(self) -> None:
        while line := await self.r.readline():
            msg = json.loads(line)
            fut = self.pending.pop(msg.get("request_id", -1), None)
            if fut is not None:
                fut.set_result(msg)
            elif "event" in msg:
                self.events.append((time.monotonic(), msg))

    async def cmd(self, *args: object) -> Any:
        self.rid += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[self.rid] = fut
        self.w.write((json.dumps({"command": list(args), "request_id": self.rid}) + "\n").encode())
        await self.w.drain()
        return await asyncio.wait_for(fut, 5)

    def pause_changes(self) -> list[tuple[float, Any]]:
        return [
            (t, m.get("data"))
            for t, m in self.events
            if m.get("event") == "property-change" and m.get("name") == "pause"
        ]

    async def wait(self, pred: Any, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            await asyncio.sleep(0.02)
        return pred()


async def _start(
    tmp_path: Path, hold_s: float, *files: str, conf: tuple[str, ...] = ()
) -> AsyncIterator[Raw]:
    """mpv as ButterEye starts it; ``conf``: options as if from the user's mpv.conf."""
    sock = tmp_path / "m.sock"
    argv = [
        "mpv", "--no-config", "--vo=null", "--ao=null", "--idle=yes", *conf,
        f"--script={os.fspath(launcher.LUA_HELPER)}", f"--input-ipc-server={sock}",
        *launcher.preload_args(hold_s), "--", *files,
    ]  # fmt: skip
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )  # fmt: skip
    try:
        for _ in range(500):
            with contextlib.suppress(FileNotFoundError, ConnectionRefusedError):
                reader, writer = await asyncio.open_unix_connection(os.fspath(sock))
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("mpv did not open its socket")
        raw = Raw(reader, writer)
        await raw.cmd("observe_property", 1, "pause")
        await raw.cmd("observe_property", 2, "playback-time")
        yield raw
        writer.close()
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


@pytest.fixture
async def mpv1(tmp_path: Path) -> AsyncIterator[Raw]:
    async for raw in _start(tmp_path, 2.0, PICTURE):
        yield raw


@pytest.fixture
async def mpv2(tmp_path: Path) -> AsyncIterator[Raw]:
    async for raw in _start(tmp_path, 2.0, PICTURE, PICTURE):
        yield raw


def _unpaused(raw: Raw) -> bool:
    return any(v is False for _, v in raw.pause_changes())


async def _shown(raw: Raw) -> None:
    """The first picture is there, still paused."""

    def ready() -> bool:
        return any(m.get("event") == "playback-restart" for _, m in raw.events)

    assert await raw.wait(ready, 10)
    assert (await raw.cmd("get_property", "pause"))["data"] is True


async def test_release_starts_playback_at_once(mpv1: Raw) -> None:
    await _shown(mpv1)
    t0 = time.monotonic()
    await mpv1.cmd("script-message-to", "buttereye", "preload-release")
    assert await mpv1.wait(lambda: _unpaused(mpv1), 1.0)
    assert next(t for t, v in mpv1.pause_changes() if v is False) - t0 < 0.5
    # it plays on: the hold is over for this file
    await asyncio.sleep(0.5)
    assert (await mpv1.cmd("get_property", "pause"))["data"] is False


async def test_the_helper_lets_go_by_itself(mpv1: Raw) -> None:
    t0 = time.monotonic()
    await _shown(mpv1)
    limit = 2.0 + launcher.PRELOAD_GRACE_S
    assert not await mpv1.wait(lambda: _unpaused(mpv1), limit - 1.0)
    assert await mpv1.wait(lambda: _unpaused(mpv1), 3.0)
    took = next(t for t, v in mpv1.pause_changes() if v is False) - t0
    assert limit - 0.5 <= took <= limit + 1.5


async def test_the_user_unpausing_tells_butter_eye(mpv1: Raw) -> None:
    await _shown(mpv1)
    await mpv1.cmd("set_property", "pause", False)  # the user's space bar

    def cancelled() -> bool:
        return any(
            m.get("event") == "client-message"
            and m.get("args") == ["buttereye-request", "preload-cancel"]
            for _, m in mpv1.events
        )

    assert await mpv1.wait(cancelled, 2.0)
    # a late release from ButterEye changes nothing, and nothing pauses again
    await mpv1.cmd("script-message-to", "buttereye", "preload-release")
    await asyncio.sleep(0.3)
    assert [v for _, v in mpv1.pause_changes()][-1] is False


async def test_every_playlist_entry_is_held(mpv2: Raw) -> None:
    raw = mpv2
    await _shown(raw)
    await raw.cmd("script-message-to", "buttereye", "preload-release")
    assert await raw.wait(lambda: _unpaused(raw), 1.0)
    n = len(raw.pause_changes())
    await raw.cmd("playlist-next")
    assert await raw.wait(lambda: any(v is True for _, v in raw.pause_changes()[n:]), 5)
    # the second file's hold has its own limit
    assert await raw.wait(
        lambda: [v for _, v in raw.pause_changes()[n:]][-1] is False,
        2.0 + launcher.PRELOAD_GRACE_S + 2.0,
    )


def _cancelled(raw: Raw, *why: str) -> bool:
    want = ["buttereye-request", "preload-cancel", *why]
    return any(m.get("event") == "client-message" and m.get("args") == want for _, m in raw.events)


async def test_a_paused_entry_is_not_held_and_butter_eye_is_told(mpv2: Raw) -> None:
    raw = mpv2
    await _shown(raw)
    await raw.cmd("script-message-to", "buttereye", "preload-release")
    assert await raw.wait(lambda: _unpaused(raw), 1.0)
    await raw.cmd("set_property", "pause", True)  # the user pauses, then '>'
    await raw.cmd("playlist-next")
    assert await raw.wait(lambda: _cancelled(raw, "paused"), 5)
    n = len(raw.pause_changes())
    await raw.cmd("set_property", "pause", False)  # and plays: nothing holds it
    await asyncio.sleep(0.5)
    assert [v for _, v in raw.pause_changes()[n:]] == [False]


async def test_after_preload_off_later_entries_are_not_held(mpv2: Raw) -> None:
    raw = mpv2
    await _shown(raw)
    t0 = time.monotonic()
    await raw.cmd("script-message-to", "buttereye", "preload-off")  # ButterEye detached
    assert await raw.wait(lambda: _unpaused(raw), 1.0)
    assert next(t for t, v in raw.pause_changes() if v is False) - t0 < 0.5
    n = len(raw.pause_changes())
    await raw.cmd("playlist-next")
    assert await raw.wait(lambda: _cancelled(raw, "off"), 5)
    await asyncio.sleep(0.5)
    assert raw.pause_changes()[n:] == [], "the next file plays on"


@pytest.mark.parametrize(("conf", "want"), [((), 2.0), (("--demuxer-readahead-secs=30",), 30.0)])
async def test_read_ahead_is_raised_never_lowered(
    tmp_path: Path, conf: tuple[str, ...], want: float
) -> None:
    async for raw in _start(tmp_path, 2.0, PICTURE, conf=conf):
        await _shown(raw)  # mpv's own default is 1 s; the hold asks for 2
        assert (await raw.cmd("get_property", "demuxer-readahead-secs"))["data"] == want
