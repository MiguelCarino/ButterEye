# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Start videos smooth ([general] preload) without a real mpv: the launch
options, and when the session tells the Lua helper to let go of the held first
picture. The helper owns the pause; ButterEye only ever sends ``preload-release``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
import types as pytypes
from pathlib import Path
from typing import Any

import pytest

from buttereye.core.mpvctl import launcher
from buttereye.core.mpvctl import session as live
from buttereye.core.mpvctl.ipc import MpvError, check_command
from buttereye.core.types import (
    BackendId,
    BypassReason,
    DetachPolicy,
    FilterState,
    GeneralSettings,
    Health,
    SessionId,
    Target,
    TargetKind,
)
from tests.test_live_decisions import NTSC, FakeCtx, FakeIpc, _config, _heavy, _paths, _simple

HOLD_S = 3.0
TARGET = float(NTSC * 2)  # MVTools 2x without a benchmark (_simple)


# ---------------------------------------------------------------- launch options
def test_launch_options_only_with_preload(tmp_path: Path) -> None:
    kw: dict[str, Any] = {"include": tmp_path / "i", "socket": tmp_path / "s", "file": Path("f")}
    plain = launcher.build_argv("mpv", **kw)
    assert not any(a == "--pause" or "preload" in a or "readahead" in a for a in plain)
    held = launcher.build_argv("mpv", **kw, preload_s=10.0, extra=("--vo=null",))
    opts = held[: held.index("--")]
    assert "--pause" in opts and "--script-opts-append=buttereye-readahead=5" in opts
    # read-ahead is the helper's to raise: an option here would lower a longer one
    # from the user's mpv.conf for the whole session
    assert not any(a.startswith("--demuxer-readahead-secs") for a in held)
    assert "--script-opts-append=buttereye-preload=10" in opts
    assert opts.index("--script-opts-append=buttereye-preload=10") < opts.index("--vo=null")
    assert [a for a in held if a not in plain] == list(launcher.preload_args(10.0)) + ["--vo=null"]
    # a short hold reads ahead no further than it holds; seconds keep their fraction
    assert launcher.preload_args(2.5) == (
        "--pause",
        "--script-opts-append=buttereye-preload=2.5",
        "--script-opts-append=buttereye-readahead=2.5",
    )
    assert "--cache=yes" not in launcher.preload_args(30.0)


def test_release_is_an_allowed_message_and_pause_stays_unwritable() -> None:
    check_command(("script-message-to", "buttereye", "preload-release"))
    check_command(("get_property", "estimated-vf-fps"))
    check_command(("get_property", "demuxer-cache-duration"))
    with pytest.raises(ValueError):
        check_command(("set_property", "pause", False))


def test_the_profile_never_saves_the_hold_in_watch_later() -> None:
    lines = launcher.include_text().splitlines()
    assert "watch-later-options-remove=pause" in lines
    assert "watch-later-options-remove=vf" in lines


def test_lua_helper_parses() -> None:
    lua = shutil.which("luac") or shutil.which("luac5.4") or shutil.which("luajit")
    if lua is None:
        pytest.skip("no Lua compiler to check the helper with")
    args = ["-p"] if "luac" in Path(lua).name else ["-b"]
    out = [os.devnull] if args == ["-b"] else []
    subprocess.run([lua, *args, os.fspath(launcher.LUA_HELPER), *out], check=True, timeout=30)
    text = launcher.LUA_HELPER.read_text(encoding="utf-8")
    assert 'register_script_message("preload-release"' in text
    assert 'register_script_message("preload-off"' in text
    assert '"buttereye-request", "preload-cancel"' in text
    assert re.search(r"PRELOAD_GRACE = %g\b" % launcher.PRELOAD_GRACE_S, text)


# ---------------------------------------------------------------- the session
class HeldMpv(FakeIpc):
    """An mpv started paused: ``vf add`` shows the filter, and its first frames
    arrive (a playback-restart, then ``estimated-vf-fps``) after ``ready_s``."""

    def __init__(self) -> None:
        super().__init__()
        self.values: dict[str, Any] = {
            "current-tracks/video": {"id": 1},
            "demuxer-cache-duration": 5.0,
        }
        self.session: live._Session | None = None
        self.ready_s: float | None = 0.05
        self.fps = TARGET
        #: mpv's own restart (the refresh seek of a paused add) with a low rate
        self.early_restart = False
        self.fail_add = False
        self.sent: list[tuple[float, tuple[object, ...]]] = []

    async def command(self, *args: object, timeout_s: float = 5.0) -> Any:
        self.commands.append(args)
        self.sent.append((time.monotonic(), args))
        if args[:2] == ("vf", "add"):
            if self.fail_add:
                raise MpvError("invalid parameter", args)
            m = re.search(r'\\?"gen\\?":\s*(\d+)', str(args[2]))
            assert m is not None
            self.values["vf"] = [{
                "name": "vapoursynth", "label": "buttereye", "enabled": True,
                "params": {"user-data": json.dumps({"gen": int(m.group(1))})},
            }]  # fmt: skip
            loop = asyncio.get_running_loop()
            if self.early_restart:
                self.values["estimated-vf-fps"] = 12.0
                loop.call_soon(self._restart)
            if self.ready_s is not None:
                loop.call_later(self.ready_s, self._ready)
        return None

    def _restart(self) -> None:
        assert self.session is not None
        self.session.on_event({"event": "playback-restart"})

    def _ready(self) -> None:
        self.values["estimated-vf-fps"] = self.fps
        if not self.early_restart:
            self._restart()

    async def get(self, prop: str, *, timeout_s: float = 5.0) -> Any:
        return self.values.get(prop)

    def released(self) -> list[float]:
        return [
            t for t, c in self.sent if c == ("script-message-to", "buttereye", "preload-release")
        ]


def _held(tmp_path: Path, *, hold_s: float = HOLD_S) -> tuple[FakeCtx, live._Session, HeldMpv]:
    ctx = FakeCtx(_paths(tmp_path), _config(_simple()))
    ipc = HeldMpv()
    s = live._Session(
        ctx,  # type: ignore[arg-type]
        live.LiveOptions(verify_timeout_s=0.5),
        sid=SessionId("s1"),
        file=tmp_path / "film.mkv",
        proc=pytypes.SimpleNamespace(pid=os.getpid(), returncode=None),  # type: ignore[arg-type]
        ipc=ipc,  # type: ignore[arg-type]
        socket=tmp_path / "mpv-s1.sock",
        log_path=tmp_path / "mpv-s1.log",
        script_path=tmp_path / "mpv-s1.vpy",
        profile_request="simple",
    )
    ipc.session = s
    s.preload_s = hold_s
    s.start_preload()
    s.props.update({
        "path": "/videos/film.mkv",
        "container-fps": float(NTSC),
        "display-fps": 60.0,
        "pause": True,
        "current-tracks/video": {"id": 1},
        "video-params": {"w": 1920, "h": 1080, "pixelformat": "yuv420p", "colormatrix": "bt.709"},
    })  # fmt: skip
    s.file_loaded_at = time.monotonic()
    return ctx, s, ipc


async def test_released_once_the_filter_is_ready(tmp_path: Path, caplog: Any) -> None:
    _, s, ipc = _held(tmp_path)
    assert s.status_text() == "ButterEye: getting smooth playback ready…"
    t0 = time.monotonic()
    with caplog.at_level("INFO", logger="buttereye.core.mpvctl.session"):
        await s.apply(raise_on_fail=False)
    assert s.active_gen is not None and not s.preloading
    assert len(ipc.released()) == 1 and ipc.released()[0] - t0 < 1.0
    assert "playback starts (smoothing is ready)" in caplog.text
    # the status went out before the filter was added, while held
    status = [i for i, (_, c) in enumerate(ipc.sent) if c[:3] == ("script-message-to",
              "buttereye", "status")]  # fmt: skip
    add = next(i for i, (_, c) in enumerate(ipc.sent) if c[:2] == ("vf", "add"))
    assert status and status[0] < add
    assert "ready" in str(ipc.sent[status[0]][1][3])
    # ButterEye never wrote pause itself
    assert not any(c[:2] == ("set_property", "pause") for _, c in ipc.sent)


async def test_mpvs_own_restart_at_a_low_rate_is_not_enough(tmp_path: Path) -> None:
    """The restart of mpv's refresh seek came after the add reply, before the
    filter's frames: releasing on it alone froze playback for 5 s."""
    _, s, ipc = _held(tmp_path)
    ipc.early_restart, ipc.ready_s = True, 0.4
    t0 = time.monotonic()
    await s.apply(raise_on_fail=False)
    assert s.restart_seq == 1
    assert len(ipc.released()) == 1 and ipc.released()[0] - t0 >= 0.4


async def test_a_restart_before_the_add_reply_does_not_count(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path, hold_s=1.0)
    s.on_event({"event": "playback-restart"})  # the first picture, before the add
    ipc.ready_s = None  # the filter never gets there
    ipc.values["estimated-vf-fps"] = TARGET
    t0 = time.monotonic()
    await s.apply(raise_on_fail=False)
    took = ipc.released()[0] - t0
    assert 0.8 <= took < 2.0, "held to the limit, never past it"
    assert s.active_gen is not None and not s.preloading


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"current-tracks/video": {"id": 1, "albumart": True}}, BypassReason.NO_VIDEO),
        (
            {"video-params": {"w": 1920, "h": 1080, "pixelformat": "yuv420p10",
                              "colormatrix": "bt.2020-ncl", "gamma": "pq"}},
            BypassReason.HDR_SKIP,
        ),
        ({"container-fps": 60.0}, BypassReason.ALREADY_AT_RATE),
    ],
)  # fmt: skip
async def test_a_bypass_releases_at_once(
    tmp_path: Path, change: dict[str, Any], reason: BypassReason
) -> None:
    profile = _simple(target=Target(TargetKind.DISPLAY))
    _, s, ipc = _held(tmp_path)
    s.ctx.cfg = _config(profile)  # type: ignore[attr-defined]
    s.props.update(change)
    await s.apply(raise_on_fail=False)
    assert s.filter is FilterState.BYPASSED and s.bypass is reason
    assert len(ipc.released()) == 1 and not s.preloading
    assert not any(c[:2] == ("vf", "add") for c in ipc.commands)


async def test_a_failed_add_rolls_back_and_releases(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    ipc.fail_add = True
    await s.apply(raise_on_fail=False)
    assert s.filter is FilterState.ROLLED_BACK
    assert len(ipc.released()) == 1 and not s.preloading


async def test_turning_smoothing_off_releases(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    await s.set_off(FilterState.OFF)
    assert len(ipc.released()) == 1 and not s.preloading
    await s.set_off(FilterState.OFF)  # once only
    assert len(ipc.released()) == 1


async def test_the_user_unpausing_cancels_the_hold(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    s.on_event({"event": "client-message", "args": ["buttereye-request", "preload-cancel"]})
    assert not s.preloading
    ipc.ready_s = None
    await s.apply(raise_on_fail=False)
    assert ipc.released() == [], "the helper already let go"


async def test_a_file_the_helper_did_not_hold_is_not_waited_for(tmp_path: Path) -> None:
    """The user had paused (the helper does not hold the next entry), or the
    helper stopped holding: it says so, and drops count again at once."""
    for why in ("paused", "off"):
        _, s, ipc = _held(tmp_path)
        s.on_event({"event": "client-message",
                    "args": ["buttereye-request", "preload-cancel", why]})  # fmt: skip
        assert not s.preloading and ipc.released() == []


async def test_a_later_file_with_smoothing_off_is_released_at_once(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    await s.set_off(FilterState.OFF)  # Alt+b, or a rollback
    s.want = False
    assert len(ipc.released()) == 1
    s.on_event({"event": "start-file"})  # the next playlist entry: the helper holds it
    s.on_event({"event": "file-loaded"})
    assert s.preloading
    await s.step()
    assert len(ipc.released()) == 2 and not s.preloading
    for _ in range(3):
        await s.step()
    assert len(ipc.released()) == 2


async def test_detach_stops_the_helper_holding_later_files(tmp_path: Path) -> None:
    ctx, s, ipc = _held(tmp_path)
    live.registry(ctx).sessions[s.sid] = s  # type: ignore[arg-type]
    await live.detach(ctx, s.sid, DetachPolicy.KEEP_FILTER)  # type: ignore[arg-type]
    sent = [c for _, c in ipc.sent if c[:2] == ("script-message-to", "buttereye")]
    release = ("script-message-to", "buttereye", "preload-release")
    off = ("script-message-to", "buttereye", "preload-off")
    assert release in sent and off in sent and sent.index(release) < sent.index(off)


async def test_no_video_track_releases_at_file_loaded(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    ipc.values["current-tracks/video"] = None
    await s.check_preload_video()
    assert len(ipc.released()) == 1 and not s.preloading


async def test_the_limit_releases_from_the_controller(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    s.props.pop("video-params")  # mpv has not decoded a picture yet: apply waits
    await s.step()
    assert s.preloading and ipc.released() == []
    assert s.preload_until is not None
    s.preload_until = time.monotonic() - 0.01
    await s.step()
    assert len(ipc.released()) == 1 and not s.preloading


async def test_no_engine_change_and_no_start_notice_while_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, s, ipc = _held(tmp_path)
    s.preload_until = time.monotonic() + 60
    s.active_gen = 1  # as if a filter were in and the hold still on
    s.props["vf"] = [{"name": "vapoursynth", "label": "buttereye", "enabled": True}]
    s.auto_chosen, s.backend, s.source = True, BackendId.MVTOOLS, None
    fell_back: list[float] = []

    async def live_fallback(rate: float, *, learn: bool = True) -> None:
        fell_back.append(rate)

    monkeypatch.setattr(s, "live_fallback", live_fallback)
    monkeypatch.setattr(s.detector, "update", lambda _sample: _heavy(0.6))
    s.heavy_since = time.monotonic() - live.HEAVY_HOLD_S - 1
    s.file_started_mono = time.monotonic() - 3600  # past the start deadline
    await s.step()
    assert fell_back == [] and s.health is Health.OK and s.start_notice is None
    # at the release the drop window starts again
    restarts: list[float] = []
    monkeypatch.setattr(s.detector, "note_restart", lambda t: restarts.append(t))
    await s.release_preload("test")
    assert restarts and s.heavy_since is None and s.clean_tick is None


async def test_seeks_and_unheld_sessions_never_hold(tmp_path: Path) -> None:
    _, s, ipc = _held(tmp_path)
    await s.release_preload("test")
    s.on_event({"event": "seek"})
    s.on_event({"event": "playback-restart"})
    assert not s.preloading
    # another file in the same mpv is held again (the helper re-pauses it) ...
    s.on_event({"event": "start-file"})
    assert s.preloading and s.preload_until is not None
    # ... but never in an mpv started without the setting, or attached to
    s.preload_s = None
    s.preloading = False
    s.on_event({"event": "start-file"})
    assert not s.preloading


def test_defaults() -> None:
    g = GeneralSettings()
    assert g.preload is False and g.preload_max_s == 10.0
