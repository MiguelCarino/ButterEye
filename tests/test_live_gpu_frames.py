# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Layer mode: the frames layer (``mvgpu/layer``) makes a cascade's last frames
as mpv presents them (``[general] gpu_frames``, experimental, ``mpvctl.frames``).

No mpv, no GPU, no layer: the decisions, the filter the session adds, the IPC
commands and the control block's bytes are real; the layer's half of the block
is written by the test the way ``mvgpu/layer/shm.cpp`` writes it."""

from __future__ import annotations

import dataclasses
import json
import mmap
import struct
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.events import Notice
from buttereye.core.mpvctl import decide, ipc
from buttereye.core.mpvctl import frames as gpuframes
from buttereye.core.mpvctl.health import sample_from
from buttereye.core.types import BenchResult, ContentKind, Profile
from tests.test_live_cascade import _ctx, _devbox, _display
from tests.test_live_decisions import FakeIpc, _load, _session
from tests.test_live_decisions import bench_history as _bench_history_fixture
from tests.test_live_decisions import no_xid as _no_xid_fixture
from tests.test_live_trt import TrtFake, _filters, _ready
from tests.test_live_trt import fake_trt as _fake_trt_fixture

bench_history = _bench_history_fixture
no_xid = _no_xid_fixture
fake_trt = _fake_trt_fixture

OSD = {"w": 3440, "h": 1440, "ml": 440, "mr": 440, "mt": 0, "mb": 0}


class PlayerIpc(FakeIpc):
    """A FakeIpc that answers ``get`` from a property table and applies
    ``set_property`` and ``change-list`` to it, like mpv."""

    def __init__(self, props: dict[str, Any]) -> None:
        super().__init__()
        self.values = props
        self.session_props: dict[str, Any] = {}  # mpv's vf events go here
        #: mpv too slow to answer: reads of these time out (nothing is read);
        #: sets of these time out after mpv has the command (it still applies)
        self.slow_get: set[str] = set()
        self.slow_set: set[str] = set()

    async def command(self, *args: object, timeout_s: float = 5.0) -> Any:
        ipc.check_command(args)  # the allowlist holds for every command sent
        self.commands.append(args)
        if args[0] == "set_property":
            self.values[str(args[1])] = args[2]
            if args[1] in self.slow_set:
                raise TimeoutError
        elif args[:2] == ("vf", "add"):
            arg = str(args[2])
            raw = arg[arg.index(":user-data=") + len(":user-data=") :]
            raw = raw[raw.index("%", 1) + 1 :] if raw.startswith("%") else raw
            entry = {"name": "vapoursynth", "label": "buttereye", "enabled": True,
                     "params": {"user-data": raw}}  # fmt: skip
            self.session_props["vf"] = [entry]
        elif args[:2] == ("vf", "remove"):
            self.session_props["vf"] = []
        elif args[0] == "change-list" and args[1] == "watch-later-options":
            names = list(self.values.get("watch-later-options", []))
            if args[2] == "remove":
                names = [n for n in names if n != args[3]]
            else:
                names.append(args[3])
            self.values["watch-later-options"] = names
        return None

    async def get(self, prop: str, *, timeout_s: float = 5.0) -> Any:
        assert prop in ipc.READABLE, prop
        if prop in self.slow_get:
            raise TimeoutError
        if prop == "display-fps":
            override = self.values.get("display-fps-override", 0)
            return override if override else self.values.get("display-fps")
        return self.values.get(prop)

    def sets(self, name: str) -> list[object]:
        return [c[2] for c in self.commands if c[:2] == ("set_property", name)]


def _write_stats(block: gpuframes.ControlBlock, **f: int) -> None:
    """The layer's half (offset 64), as shm.cpp's ``shm_write_stats`` writes it."""
    with block.path.open("r+b") as fh, mmap.mmap(fh.fileno(), gpuframes.SIZE) as m:
        seq = struct.unpack_from("<I", m, 64)[0]
        struct.pack_into(
            "<II11Q", m, 64, (seq | 1) + 1, f.get("k", 3), f.get("refresh_ns", 6_944_444),
            f.get("app", 0), f.get("frames", 0), f.get("app", 0) - f.get("frames", 0),
            2 * f.get("frames", 0), f.get("missed", 0), 0, 34_000_000, 0, f.get("mc", 0), 0,
        )  # fmt: skip


def _writing(block: gpuframes.ControlBlock) -> None:
    """The layer is inside ``shm_write_stats`` (its sequence number is odd)."""
    with block.path.open("r+b") as fh, mmap.mmap(fh.fileno(), gpuframes.SIZE) as m:
        seq = struct.unpack_from("<I", m, 64)[0]
        struct.pack_into("<I", m, 64, seq | 1)


def _control(block: gpuframes.ControlBlock) -> dict[str, int]:
    """ButterEye's half, read the way the layer reads it (shm.cpp ``shm_read``)."""
    raw = block.path.read_bytes()
    magic, version, size, seq = struct.unpack_from("<4I", raw, 0)
    k, mode, ev, x, y, w, h, src = struct.unpack_from("<iII5i", raw, 16)
    return {"magic": magic, "version": version, "size": size, "seq": seq, "k": k,
            "mode": mode, "event": ev, "x": x, "y": y, "w": w, "h": h, "source_h": src}  # fmt: skip


def _layer_session(
    tmp_path: Path, *, profile: Profile | None = None, frames: bool = True
) -> tuple[Any, PlayerIpc]:
    ctx = _ctx(tmp_path, profile or _display())
    s = _session(ctx, tmp_path)
    player = PlayerIpc(
        {
            "audio-delay": 0.05,  # the user's own
            "watch-later-options": ["start", "audio-delay", "vf"],
            "osd-dimensions": dict(OSD),
            "display-fps": 144.0,
        }
    )
    s.ipc = player  # type: ignore[assignment]
    player.session_props = s.props
    if frames:
        s.frames = gpuframes.ControlBlock(tmp_path / "mpv-s1.frames")
    s.opts.frames_check_s = 0.0  # judge at the first tick
    s.opts.display_debounce_s = 0.0
    _ready(ctx, s, 1920, 1080)
    _load(s, fps=24.0, hz=144.0)
    return s, player


# ------------------------------------------------------------------ the block
def test_control_block_layout_matches_the_layer(tmp_path: Path) -> None:
    b = gpuframes.ControlBlock(tmp_path / "blk")
    try:
        assert b.path.stat().st_size == gpuframes.SIZE
        assert b.path.stat().st_mode & 0o777 == 0o600
        c = _control(b)
        assert (c["magic"], c["version"], c["size"]) == (0x31464542, 1, 256)
        assert c["k"] == 1 and c["mode"] == 3 and c["seq"] % 2 == 0
        b.set(k=3, rect=(440, 0, 2560, 1440), source_h=1080, event=True)
        c = _control(b)
        assert (c["k"], c["x"], c["y"], c["w"], c["h"], c["source_h"]) == (
            3,
            440,
            0,
            2560,
            1440,
            1080,
        )
        assert c["event"] == 1 and c["seq"] % 2 == 0
        b.set(k=9)
        assert _control(b)["k"] == gpuframes.KMAX  # never more images than mpv got
        assert not b.stats().written
        _write_stats(b, app=100, frames=98, missed=2, mc=97)
        st = b.stats()
        assert (st.app_presents, st.frames_presents, st.missed_refreshes, st.mc_made) == (
            100,
            98,
            2,
            97,
        )
        assert st.refresh_hz == pytest.approx(144.0, abs=1e-3) and st.written
    finally:
        b.close()
    assert not b.path.exists() and b.stats() == gpuframes.LayerStats()


def test_layer_env_turns_the_layer_on_and_nv_present_off(tmp_path: Path) -> None:
    base = {
        "VK_INSTANCE_LAYERS": "VK_LAYER_NV_dlssnr:VK_LAYER_NV_present",
        "NVPRESENT_ENABLE_SMOOTH_MOTION": "1",
        "VK_LOADER_LAYERS_ENABLE": "VK_LAYER_KHRONOS_validation",
        "HOME": "/var/empty/u",
    }
    env = gpuframes.layer_env(base, shm=tmp_path / "blk", add_path=tmp_path / "layers")
    assert env["VK_INSTANCE_LAYERS"] == "VK_LAYER_NV_dlssnr"  # other layers stay
    assert env["VK_LOADER_LAYERS_DISABLE"] == "VK_LAYER_NV_present"
    assert env["VK_LOADER_LAYERS_ENABLE"] == "VK_LAYER_KHRONOS_validation,VK_LAYER_BUTTEREYE_frames"
    assert "NVPRESENT_ENABLE_SMOOTH_MOTION" not in env
    assert env["VK_ADD_LAYER_PATH"] == str(tmp_path / "layers")
    assert env["BUTTEREYE_FRAMES"] == "off" and env["BUTTEREYE_FRAMES_KMAX"] == "3"
    assert env["BUTTEREYE_FRAMES_MODE"] == "mc" and env["BUTTEREYE_FRAMES_SHM"] == str(
        tmp_path / "blk"
    )
    assert env["HOME"] == "/var/empty/u" and base["VK_INSTANCE_LAYERS"].endswith("NV_present")
    only_nv = gpuframes.layer_env({"VK_INSTANCE_LAYERS": "VK_LAYER_NV_present"}, shm=tmp_path)
    assert "VK_INSTANCE_LAYERS" not in only_nv and "VK_ADD_LAYER_PATH" not in only_nv


def test_rates_and_rectangle() -> None:
    from fractions import Fraction as F

    assert gpuframes.layer_k(F(48), F(144)) == 3
    assert gpuframes.layer_k(F(60), F(120)) == 2
    assert gpuframes.layer_k(F(48000, 1001), F(144000, 1001)) == 3
    assert gpuframes.layer_k(F(48), F(240)) is None  # k 5: more than the images mpv got
    assert gpuframes.layer_k(F(48), F(48)) is None
    assert gpuframes.override_fps(144.0, 3) == 48.0
    assert gpuframes.video_rect(OSD) == (440, 0, 2560, 1440)
    assert gpuframes.video_rect({"w": 0, "h": 0}) is None and gpuframes.video_rect(None) is None


def _manifest(folder: Path, *, lib: bool = True, name: str = osapi.FRAMES_LAYER) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    lib_path = "./libVkLayer_buttereye_frames.so"
    layer = {"name": name, "type": "INSTANCE", "library_path": lib_path}
    doc = {"file_format_version": "1.2.0", "layer": layer}
    (folder / osapi.FRAMES_MANIFEST).write_text(json.dumps(doc))
    if lib:
        (folder / "libVkLayer_buttereye_frames.so").write_bytes(b"\x7fELF")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the layer is Linux only")
def test_manifest_lookup_and_status(tmp_path: Path) -> None:
    empty = {"HOME": str(tmp_path / "home"), "XDG_DATA_DIRS": str(tmp_path / "none"),
             "XDG_CONFIG_DIRS": str(tmp_path / "none"), "WAYLAND_DISPLAY": "wayland-1"}  # fmt: skip
    build = tmp_path / "build" / "layer"
    _manifest(build)
    env = dict(empty, VK_ADD_LAYER_PATH=str(build))
    found = osapi.frames_layer(env)
    assert found is not None and found.manifest == build / osapi.FRAMES_MANIFEST
    assert found.add_path is None  # mpv inherits VK_ADD_LAYER_PATH itself
    assert osapi.frames_layer_status(env) is None
    assert osapi.frames_layer_status(dict(env, WAYLAND_DISPLAY="")) == "wayland"
    data_home = tmp_path / "home" / ".local" / "share" / "vulkan" / "explicit_layer.d"
    _manifest(data_home, lib=False)  # a manifest whose library is gone
    assert osapi.frames_layer(empty) is None and osapi.frames_layer_status(empty) == "layer"
    _manifest(data_home, name="VK_LAYER_other")
    assert osapi.frames_layer(empty) is None
    _manifest(data_home)
    assert osapi.frames_layer(empty) is not None


def test_ipc_allows_only_what_layer_mode_sets() -> None:
    ok: tuple[tuple[object, ...], ...] = (
        ("set_property", "display-fps-override", 48.0),
        ("set_property", "display-fps-override", 0),
        ("set_property", "audio-delay", 0.0632),
    )
    for args in ok:
        ipc.check_command(args)
    bad: tuple[tuple[object, ...], ...] = (
        ("set_property", "display-fps-override", True),
        ("set_property", "display-fps-override", 0.5),
        ("set_property", "display-fps-override", float("inf")),
        ("set_property", "audio-delay", "0.1"),
        ("set_property", "audio-delay", 90.0),
        # audio-delay stays in watch-later-options (frames.AudioLedger)
        ("change-list", "watch-later-options", "remove", "audio-delay"),
        ("change-list", "watch-later-options", "remove", "vf"),
    )
    for args in bad:
        with pytest.raises(ipc.CommandRefused):
            ipc.check_command(args)


# ------------------------------------------------------------------ sessions
async def test_layer_mode_on(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """1080p 24 fps on 144 Hz: RIFE to 48 in VapourSynth, the layer the rest at k=3."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["backend"] == "rife-trt" and f["target_fps"] == [48, 1] and "cascade_fps" not in f
    assert s.target == 144 and s.cascade == 48 and s.frames_k == 3 and s.real_hz == 144.0
    snap = s.snapshot()
    assert snap.gpu_frames and snap.target_fps == 144 and snap.display_fps == 144.0
    assert s.notice == gpuframes.layer_notice()
    c = _control(s.frames)
    assert (c["k"], c["mode"], c["source_h"]) == (3, 3, 1080)
    assert (c["x"], c["y"], c["w"], c["h"]) == (440, 0, 2560, 1440)
    assert player.sets("display-fps-override") == [48.0]
    assert player.values["audio-delay"] == pytest.approx(0.05 + 2 / 144)  # added, not replaced
    # the user's own offset keeps reaching resume files; the ledger knows the add
    assert player.values["watch-later-options"] == ["start", "audio-delay", "vf"]
    assert not any(c[0] == "change-list" for c in player.commands)
    ledger = gpuframes.AudioLedger(s.ctx.paths.state_dir / gpuframes.AUDIO_FILE)
    key = gpuframes.audio_key("/videos/film.mkv")
    assert ledger.match(key, 0.05 + 2 / 144) == pytest.approx(2 / 144)
    assert "film" not in ledger.path.read_text()  # keys are hashes
    # k is in the block before mpv hears of the override (mpv runs slow, never fast)
    order = [c[1] for c in player.commands if c[0] == "set_property"]
    assert order.index("display-fps-override") < order.index("audio-delay")
    s.filter = s.filter.__class__.ACTIVE
    assert "last frames made on the GPU" in s.status_text()
    s.frames.close()


async def test_layer_mode_off_plays_as_today(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path, frames=False)
    await s.apply(raise_on_fail=False)
    (f,) = _filters(s)
    assert f["target_fps"] == [144, 1] and f["cascade_fps"] == [48, 1]
    assert s.notice == decide.cascade_notice(None) and not s.snapshot().gpu_frames
    assert player.sets("display-fps-override") == [] and player.values["audio-delay"] == 0.05


@pytest.mark.parametrize("case", ["animation", "60hz", "240hz"])
async def test_layer_mode_only_for_a_cascade(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake, case: str
) -> None:
    bench_history.append(_devbox())
    profile = _display()
    if case == "animation":
        profile = dataclasses.replace(profile, content=ContentKind.ANIMATION)
    s, player = _layer_session(tmp_path, profile=profile)
    if case != "animation":
        hz = 60.0 if case == "60hz" else 240.0  # 24 → 48 on 240 would be k=5
        _load(s, fps=24.0, hz=hz)
        player.values["display-fps"] = hz
    await s.apply(raise_on_fail=False)
    assert s.frames_k == 0 and not s.snapshot().gpu_frames
    assert player.sets("display-fps-override") == []
    assert _control(s.frames)["k"] == 1
    s.frames.close()


async def test_override_is_never_read_back(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """mpv reports display-fps = the override in layer mode; it never reaches a
    decision, so no 144 → 48 → 16 loop and no re-apply."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    s.on_event({"event": "property-change", "name": "display-fps", "data": 48.0})
    assert s.props["display-fps"] == 144.0
    _write_stats(s.frames, app=150, frames=149, missed=0, mc=148)
    for _ in range(3):
        await s.step()
    assert len(_filters(s)) == 1 and s.frames_k == 3 and s.display_fps == 144.0
    assert player.sets("display-fps-override") == [48.0]
    s.frames.close()


async def test_pause_and_seek_bump_the_event_counter(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    s, _player = _layer_session(tmp_path)
    s.on_event({"event": "seek"})
    assert _control(s.frames)["event"] == 0  # not in layer mode yet
    await s.apply(raise_on_fail=False)
    first = _control(s.frames)["event"]
    s.on_event({"event": "property-change", "name": "pause", "data": True})
    s.on_event({"event": "property-change", "name": "pause", "data": False})
    s.on_event({"event": "seek"})
    s.on_event({"event": "property-change", "name": "seeking", "data": True})
    s.on_event({"event": "playback-restart"})
    assert _control(s.frames)["event"] == first + 5
    s.frames.close()


async def test_screen_move_leaves_and_decides_again(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """The window moves to a 60 Hz screen: mpv's own rate and the user's audio
    delay come back, then the decision runs again for 60 Hz (no cascade there)."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    _write_stats(s.frames, app=150, frames=149)
    player.values["display-fps"] = 60.0  # what mpv knows once the override is off
    s.on_event({"event": "property-change", "name": "display-names", "data": ["HDMI-A-1"]})
    await s.step()  # leaves layer mode
    assert s.frames_k == 0 and s.real_hz is None and not s.snapshot().gpu_frames
    assert player.sets("display-fps-override") == [48.0, 0]
    assert _control(s.frames)["k"] == 1
    assert player.values["audio-delay"] == pytest.approx(0.05)
    assert player.values["watch-later-options"] == ["start", "audio-delay", "vf"]
    assert s.props["display-fps"] == 60.0
    s.props["display-names"] = ["HDMI-A-1"]
    await s.step()  # decides again for 60 Hz
    filters = _filters(s)
    assert len(filters) >= 2 and "cascade_fps" not in filters[-1]
    assert filters[-1]["target_fps"] == [60, 1] and s.target == 60  # RIFE alone, as today
    assert s.frames_k == 0 and player.sets("display-fps-override") == [48.0, 0]
    s.frames.close()


async def test_refresh_change_seen_by_the_layer_leaves(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    _write_stats(s.frames, app=40, frames=39)  # 144 Hz measured
    await s.frames_watch(time.monotonic())
    assert s.frames_k == 3
    _write_stats(s.frames, app=20, frames=19, refresh_ns=16_666_667)  # a new 60 Hz swapchain
    await s.frames_watch(time.monotonic())
    assert s.frames_k == 0 and player.sets("display-fps-override")[-1] == 0
    assert s.frames_redecide_at is not None
    s.frames.close()


async def test_a_refresh_that_disagrees_from_the_start_is_not_a_move(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """Seen on the dev box: after a move back to the 144 Hz screen the new
    swapchain kept reporting 60 Hz; display-names had already said where it is."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    _write_stats(s.frames, app=10, frames=9, refresh_ns=16_666_667)
    await s.apply(raise_on_fail=False)
    for _ in range(3):
        _write_stats(s.frames, app=60, frames=59, refresh_ns=16_666_667)
        await s.frames_watch(time.monotonic())
    assert s.frames_k == 3 and player.sets("display-fps-override") == [48.0]
    assert player.values["audio-delay"] == pytest.approx(0.05 + 2 / 144)  # the decided rate
    s.frames.close()


@pytest.mark.parametrize("case", ["not-loaded", "passed-through"])
async def test_fallback_when_the_layer_does_not_take_over(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake, case: str
) -> None:
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    if case == "passed-through":
        _write_stats(s.frames, k=1, app=144, frames=0)
    await s.frames_watch(time.monotonic())
    assert s.frames_failed and s.frames_k == 0
    assert player.sets("display-fps-override") == [48.0, 0]
    assert player.values["audio-delay"] == pytest.approx(0.05)
    assert any(isinstance(e, Notice) and e.message == gpuframes.fallback_notice()
               for e in s.ctx.events)  # fmt: skip
    await s.step()  # decides again: today's cascade in VapourSynth
    f = _filters(s)[-1]
    assert f["target_fps"] == [144, 1] and f["cascade_fps"] == [48, 1]
    assert s.frames_k == 0 and s.notice == gpuframes.fallback_notice()
    assert _control(s.frames)["k"] == 1
    s.frames.close()


async def test_the_layer_taking_over_is_confirmed(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    s, _player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    _write_stats(s.frames, app=144, frames=143, mc=142)
    await s.frames_watch(time.monotonic())
    assert s.frames_confirmed and not s.frames_failed and s.frames_k == 3
    s.frames.close()


async def test_health_counts_the_layers_missed_refreshes(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """At the screen, layer mode counts what the layer saw missed, summed over
    mpv's swapchains (their counters start from 0)."""
    bench_history.append(_devbox())
    s, _player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    s.opts.frames_check_s = 1e9  # no take-over verdict here
    _write_stats(s.frames, app=500, frames=499, missed=4)
    await s.frames_watch(time.monotonic())
    _write_stats(s.frames, app=20, frames=20, missed=3)  # a new swapchain
    await s.frames_watch(time.monotonic())
    assert s.frames_missed == 7
    s.props["vo-delayed-frame-count"] = 900  # mpv's own count is not used
    seen: list[int] = []
    real_update = s.detector.update

    def spy(smp: Any) -> Any:
        seen.append(smp.vo_delayed)
        return real_update(smp)

    s.detector.update = spy
    s.active_gen = s.gen
    await s.step()
    assert seen and seen[-1] == 7
    assert sample_from(s.props, 0.0).vo_delayed == 900
    s.frames.close()


async def test_turning_smoothing_off_leaves_layer_mode(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    player.values["audio-delay"] = 0.2 + 2 / 144  # the user changed theirs meanwhile
    await s.set_off(s.filter.__class__.OFF)
    assert s.frames_k == 0 and player.sets("display-fps-override")[-1] == 0
    assert player.values["audio-delay"] == pytest.approx(0.2)  # theirs kept
    assert _control(s.frames)["k"] == 1
    s.frames.close()


# ------------------------------------------------------------------ play()
class _Stop(Exception):
    pass


class _Op:
    def report(self, *_a: Any, **_k: Any) -> None:
        pass


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the layer is Linux only")
@pytest.mark.parametrize("gpu_frames", [True, False, None])
async def test_play_starts_mpv_with_the_layer_only_when_asked(
    xdg_env: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gpu_frames: bool | None
) -> None:
    """mpv's options and environment are fixed at spawn: the layer, Vulkan on
    Wayland and NV_present off only with ``gpu_frames`` (None follows the
    setting, off by default)."""
    import os

    from buttereye.core.api import ButterEye
    from buttereye.core.mpvctl import launcher
    from buttereye.core.mpvctl import session as live
    from buttereye.core.testing import scenarios as sc
    from buttereye.core.types import DetachPolicy

    layers = tmp_path / "layers"
    _manifest(layers)
    monkeypatch.setenv("VK_ADD_LAYER_PATH", str(layers))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("VK_INSTANCE_LAYERS", "VK_LAYER_NV_present")
    seen: list[tuple[list[str], dict[str, str] | None]] = []

    async def spawn(argv: list[str], log: Path, *, env: Any = None) -> Any:
        seen.append((list(argv), dict(env) if env is not None else None))
        raise _Stop

    monkeypatch.setattr(launcher, "spawn_mpv", spawn)
    (tmp_path / "clip.mkv").write_bytes(b"")
    core = await ButterEye.open(sc.fake_paths(env=os.environ))
    try:
        op: Any = _Op()
        with pytest.raises(_Stop):
            await live.play(
                core._ctx, tmp_path / "clip.mkv", profile_id=None, op=op, gpu_frames=gpu_frames
            )
        runtime = core._ctx.paths.runtime_dir
        assert not list(runtime.glob("*.frames"))  # removed with the failed start
    finally:
        await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=1.0)
    ((argv, env),) = seen
    if gpu_frames:
        assert "--gpu-api=vulkan" in argv and "--gpu-context=waylandvk" in argv
        assert env is not None and env["VK_LOADER_LAYERS_ENABLE"] == osapi.FRAMES_LAYER
        assert env["VK_LOADER_LAYERS_DISABLE"] == "VK_LAYER_NV_present"
        assert "VK_INSTANCE_LAYERS" not in env
        assert env["BUTTEREYE_FRAMES_SHM"].endswith(".frames")
    else:
        assert "--gpu-api=vulkan" not in argv and env is None


def test_osd_line_says_gpu_frames() -> None:
    from buttereye.core.mpvctl import osd
    from buttereye.core.testing import scenarios as sc
    from buttereye.core.types import BackendId

    snap = dataclasses.replace(sc.session(0), backend=BackendId.RIFE_TRT, gpu_frames=True)
    assert any("GPU frames at present" in p for p in osd.parts(snap, ("engine",)))
    plain = dataclasses.replace(snap, gpu_frames=False)
    assert not any("GPU frames" in p for p in osd.parts(plain, ("engine",)))


# ------------------------------------------------------------------ review fixes
@pytest.mark.parametrize("own_override", [0, 50.0])
async def test_applying_again_with_the_same_k_engages_once(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: Any,
    fake_trt: TrtFake,
    own_override: float,
) -> None:
    """Alt+c, a TensorRT engine or a step-down applies again with the same k:
    ButterEye's own 48 is not taken for the user's override, and the audio
    delay is added once, so turning smoothing off puts both back."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    player.values["display-fps-override"] = own_override
    await s.apply(raise_on_fail=False)
    await s.apply(raise_on_fail=False)
    assert s.frames_k == 3 and s.frames_override_restore == own_override
    assert player.values["audio-delay"] == pytest.approx(0.05 + 2 / 144)
    await s.set_off(s.filter.__class__.OFF)
    assert s.frames_k == 0 and player.values["display-fps-override"] == own_override
    assert player.values["audio-delay"] == pytest.approx(0.05)
    s.frames.close()


async def test_leaving_keeps_the_audio_delay_until_mpv_answers(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """mpv does not answer the read while layer mode ends (a screen move, a
    slow player): the add stays known and comes off once mpv answers."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    player.slow_get.add("audio-delay")
    await s.frames_leave("the window moved to another screen")
    assert s.frames_k == 0 and s.frames_audio_add == pytest.approx(2 / 144)
    assert player.values["audio-delay"] == pytest.approx(0.05 + 2 / 144)
    await s.step()  # still silent: kept
    assert s.frames_audio_add == pytest.approx(2 / 144)
    player.slow_get.clear()
    await s.step()
    assert player.values["audio-delay"] == pytest.approx(0.05) and not s.frames_audio_add
    await s.step()
    assert player.values["audio-delay"] == pytest.approx(0.05)  # taken off once
    s.frames.close()


async def test_a_set_that_times_out_is_not_sent_twice(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """A set that times out was sent: mpv applies it late, so it is not retried."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    player.slow_set.add("audio-delay")
    await s.frames_leave("smoothing is off")
    await s.step()
    assert player.values["audio-delay"] == pytest.approx(0.05) and not s.frames_audio_add
    s.frames.close()


@pytest.mark.parametrize("changed", [False, True])
async def test_a_resume_file_written_in_layer_mode_keeps_the_users_offset(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: Any,
    fake_trt: TrtFake,
    changed: bool,
) -> None:
    """mpv writes a resume file inside its quit command (quit-watch-later,
    save-position-on-quit), with layer mode's add in audio-delay. audio-delay
    stays in watch-later-options, so the user's own offset is saved; the next
    run's layer mode knows the add is in (the ledger) and does not add it
    again, and smoothing off leaves the user's own offset."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    own = 0.05
    if changed:  # Ctrl+- in layer mode
        own = -0.05
        player.values["audio-delay"] = own + 2 / 144
        s.on_event({"event": "property-change", "name": "audio-delay", "data": own + 2 / 144})
    assert "audio-delay" in player.values["watch-later-options"]
    saved = float("%f" % player.values["audio-delay"])  # what mpv writes
    s.frames.close()
    # ButterEye and mpv run again and resume that file
    s2, player2 = _layer_session(tmp_path)
    player2.values["audio-delay"] = saved
    await s2.apply(raise_on_fail=False)
    assert s2.frames_k == 3
    assert player2.values["audio-delay"] == pytest.approx(own + 2 / 144, abs=1e-6)
    await s2.set_off(s2.filter.__class__.OFF)
    assert player2.values["audio-delay"] == pytest.approx(own, abs=1e-6)
    s2.frames.close()


@pytest.mark.parametrize("own", [None, -0.05])
async def test_the_next_files_own_audio_delay_is_left_alone(
    tmp_path: Path,
    bench_history: list[BenchResult],
    no_xid: Any,
    fake_trt: TrtFake,
    own: float | None,
) -> None:
    """mpv applies the next file's resume file (or per-file options) before
    ButterEye hears of the new file: that value has no add in it and stays. A
    value carried over from the layer's file gets the add taken off."""
    bench_history.append(_devbox())
    s, player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    s.on_event({"event": "start-file"})
    if own is not None:
        player.values["audio-delay"] = own
    await s.begin_new_file()
    assert s.frames_k == 0 and not s.frames_audio_add
    assert player.values["audio-delay"] == pytest.approx(0.05 if own is None else own)
    s.frames.close()


def test_stats_while_the_layer_writes_are_the_last_ones_read(tmp_path: Path) -> None:
    b = gpuframes.ControlBlock(tmp_path / "blk")
    try:
        _writing(b)
        assert b.stats() == gpuframes.LayerStats()  # nothing written yet
        _write_stats(b, app=500, frames=499, missed=4)
        first = b.stats()
        assert first.app_presents == 500
        _writing(b)
        assert b.stats() == first and b.stats().written
    finally:
        b.close()


async def test_a_busy_layer_is_no_new_swapchain_and_no_fallback(
    tmp_path: Path, bench_history: list[BenchResult], no_xid: Any, fake_trt: TrtFake
) -> None:
    """A read while the layer writes its counters is not a new swapchain (the
    missed refreshes would count twice) and not a layer that wrote nothing
    (layer mode would end for the session)."""
    bench_history.append(_devbox())
    s, _player = _layer_session(tmp_path)
    await s.apply(raise_on_fail=False)
    s.opts.frames_check_s = 1e9  # no take-over verdict yet
    _write_stats(s.frames, app=500, frames=499, missed=4)
    await s.frames_watch(time.monotonic())
    _writing(s.frames)
    await s.frames_watch(time.monotonic())
    _write_stats(s.frames, app=600, frames=599, missed=4)
    await s.frames_watch(time.monotonic())
    assert s.frames_missed == 4
    s.opts.frames_check_s = 0.0  # the verdict lands on a busy read
    _writing(s.frames)
    await s.frames_watch(time.monotonic())
    assert not s.frames_failed and s.frames_k == 3 and s.frames_confirmed
    s.frames.close()
