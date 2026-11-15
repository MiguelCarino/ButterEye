# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The frames layer in a session: the last in-between frames made on the GPU
when mpv presents (experimental, ``[general] gpu_frames``; docs/spikes/m0z.md).

``VK_LAYER_BUTTEREYE_frames`` (``mvgpu/layer``) sits under mpv's Vulkan output.
For every image mpv presents it presents k - 1 frames of its own first, made
with mvgpu's engine from mpv's last two images, so mpv draws R frames a second
and the screen gets k·R. In "layer mode" VapourSynth runs only the first stage
of a cascade (RIFE to twice the source rate) and the layer makes the rest at
k = n (``decide.cascade_target``: 24 → 48 → 144 on 144 Hz is k = 3).

This module holds what does not need a session:

- ``layer_env``: mpv's environment with the layer turned on and the driver's own
  frame-generation layer (``VK_LAYER_NV_present``) turned off;
- ``ControlBlock``: the 256-byte shared file the layer reads on every present
  (k, mode, video rectangle, an event counter) and writes its counters into.
  The layout is ``mvgpu/layer/shm.cpp``'s; both halves are seqlocks;
- ``AudioLedger``: the audio delay layer mode left in mpv per file, for the
  resume files mpv writes while it is on.

The session (``mpvctl.session``) decides when layer mode applies, sets
``display-fps-override`` = real refresh / k over IPC, keeps the real refresh,
adds the layer's delay to ``audio-delay``, and falls back to today's cascade
when the layer does not take over.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import mmap
import os
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from buttereye.core import osapi
from buttereye.core.types import Msg

#: the driver's own frame-generation layer; never under ButterEye's (two layers
#: making frames would each see the other's)
NV_PRESENT = "VK_LAYER_NV_present"
#: the most frames the layer adds per mpv frame, plus one (``BUTTEREYE_FRAMES_KMAX``):
#: the swapchain gets this many more images, so it is fixed when mpv starts
KMAX = 3
#: the mpv options layer mode needs: libplacebo's Vulkan swapchain on Wayland
MPV_ARGS: tuple[str, ...] = ("--gpu-api=vulkan", "--gpu-context=waylandvk")

#: control block layout (``mvgpu/layer/shm.cpp``)
MAGIC = 0x31464542  # "BEF1"
VERSION = 1
SIZE = 256
MODE_KEEP, MODE_COPIES, MODE_BLEND, MODE_MC = 0, 1, 2, 3
_HEADER = struct.Struct("<4I")  # magic, version, size, control seq (offset 0)
_CONTROL = struct.Struct("<iII5i")  # k, mode, event seq, rect x y w h, source h (offset 16)
_STATS = struct.Struct("<II11Q")  # stats seq, k in use, refresh ns, counters (offset 64)
_CONTROL_SEQ = 12
_STATS_AT = 64


@dataclass(frozen=True, slots=True)
class LayerStats:
    """What the layer wrote into the control block (all zero until it has).

    The counters are per swapchain: they start again from 0 when mpv makes a
    new one (a window resize, fullscreen, another screen)."""

    seq: int = 0
    k_active: int = 0
    refresh_ns: int = 0
    app_presents: int = 0
    frames_presents: int = 0  # app presents that took the k path
    passed: int = 0  # app presents passed through unchanged
    generated: int = 0  # presents the layer added
    missed_refreshes: int = 0  # missed refreshes seen in present timing
    timed_intervals: int = 0
    latency_ns: int = 0  # app present to first pixel of its image, last value
    blended: int = 0
    mc_made: int = 0  # app presents whose added frames were motion-compensated
    mc_cuts: int = 0  # of those, pairs the GPU cut test called a scene cut

    @property
    def written(self) -> bool:
        return self.seq != 0 or self.app_presents != 0

    @property
    def refresh_hz(self) -> float | None:
        return 1e9 / self.refresh_ns if self.refresh_ns > 0 else None


class ControlBlock:
    """The control block of one mpv, created (mode 0600) before mpv starts,
    so the layer maps it at its first present; removed with ``close``.

    Python writes the mapping with plain stores; the seqlock (odd while writing)
    keeps the layer from reading a half-written state, and the layer's own
    half is read the same way (``stats``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | osapi.O_CLOEXEC | osapi.O_NOFOLLOW,
                     0o600)  # fmt: skip
        try:
            os.ftruncate(fd, SIZE)
            self._map: mmap.mmap | None = mmap.mmap(fd, SIZE)
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        os.close(fd)
        self.k = 1
        self.mode = MODE_MC
        self.event_seq = 0
        self.rect: tuple[int, int, int, int] = (0, 0, 0, 0)
        self.source_h = 0
        self._stats = LayerStats()  # the last consistent copy (``stats``)
        _HEADER.pack_into(self._map, 0, MAGIC, VERSION, SIZE, 0)
        self._write()

    @property
    def closed(self) -> bool:
        return self._map is None

    def _write(self) -> None:
        m = self._map
        if m is None:
            return
        seq = struct.unpack_from("<I", m, _CONTROL_SEQ)[0]
        if seq & 1:
            seq += 1  # never left odd
        struct.pack_into("<I", m, _CONTROL_SEQ, (seq + 1) & 0xFFFFFFFF)
        _CONTROL.pack_into(
            m, 16, self.k, self.mode, self.event_seq & 0xFFFFFFFF, *self.rect, self.source_h
        )
        struct.pack_into("<I", m, _CONTROL_SEQ, (seq + 2) & 0xFFFFFFFF)

    def set(
        self,
        *,
        k: int | None = None,
        mode: int | None = None,
        rect: tuple[int, int, int, int] | None = None,
        source_h: int | None = None,
        event: bool = False,
    ) -> None:
        """Change what the layer does from its next present. ``k`` 1 = off;
        ``event``: the next present passes through and the layer's history
        starts again (pause, seek, playback-restart, entering or leaving)."""
        if k is not None:
            self.k = max(1, min(KMAX, int(k)))
        if mode is not None:
            self.mode = mode
        if rect is not None:
            self.rect = rect
        if source_h is not None:
            self.source_h = max(0, int(source_h))
        if event:
            self.event_seq += 1
        self._write()

    def bump_event(self) -> None:
        self.set(event=True)

    def stats(self) -> LayerStats:
        """The layer's half: a consistent copy, the zero state until the layer
        has written, and the last consistent copy while it is writing (a few
        tries). A busy writer is never read as zeros: the session would take
        them for a new swapchain or a layer that is not loaded."""
        m = self._map
        if m is None:
            return LayerStats()
        for _ in range(8):
            values = _STATS.unpack_from(m, _STATS_AT)
            if values[0] & 1:
                continue
            if struct.unpack_from("<I", m, _STATS_AT)[0] != values[0]:
                continue
            self._stats = LayerStats(*values)
            return self._stats
        return self._stats

    def close(self) -> None:
        """Unmap and remove the file (the layer keeps its own mapping, and its
        last settings, until mpv exits)."""
        m, self._map = self._map, None
        if m is not None:
            with contextlib.suppress(ValueError, OSError):
                m.close()
        with contextlib.suppress(OSError):
            self.path.unlink()


def _names(value: str | None, sep: str) -> list[str]:
    return [n.strip() for n in (value or "").split(sep) if n.strip()]


def layer_env(
    base: Mapping[str, str], *, shm: Path, add_path: Path | None = None
) -> dict[str, str]:
    """mpv's environment in layer mode, from ``base`` (ButterEye's own).

    - ``VK_LOADER_LAYERS_ENABLE`` gains the layer; ``VK_ADD_LAYER_PATH`` gains
      ``add_path`` (the AppImage's manifest folder) when given;
    - ``VK_LAYER_NV_present`` leaves ``VK_INSTANCE_LAYERS`` and goes into
      ``VK_LOADER_LAYERS_DISABLE`` (the off switch that holds even over
      ``VK_INSTANCE_LAYERS``), and its smooth-motion switch is removed; every
      other layer is left as it was;
    - the layer's own settings: at most ``KMAX``, off until the control block
      says otherwise, motion-compensated frames, and the block's path."""
    env = dict(base)
    layers = [n for n in _names(env.get("VK_INSTANCE_LAYERS"), ":") if n != NV_PRESENT]
    if layers:
        env["VK_INSTANCE_LAYERS"] = ":".join(layers)
    else:
        env.pop("VK_INSTANCE_LAYERS", None)
    enable = _names(env.get("VK_LOADER_LAYERS_ENABLE"), ",")
    if osapi.FRAMES_LAYER not in enable:
        enable.append(osapi.FRAMES_LAYER)
    env["VK_LOADER_LAYERS_ENABLE"] = ",".join(enable)
    disable = [
        n for n in _names(env.get("VK_LOADER_LAYERS_DISABLE"), ",") if n != osapi.FRAMES_LAYER
    ]
    if NV_PRESENT not in disable:
        disable.append(NV_PRESENT)
    env["VK_LOADER_LAYERS_DISABLE"] = ",".join(disable)
    env.pop("NVPRESENT_ENABLE_SMOOTH_MOTION", None)
    if add_path is not None:
        paths = _names(env.get("VK_ADD_LAYER_PATH"), ":")
        if os.fspath(add_path) not in paths:
            paths.insert(0, os.fspath(add_path))
        env["VK_ADD_LAYER_PATH"] = ":".join(paths)
    env["BUTTEREYE_FRAMES_KMAX"] = str(KMAX)
    env["BUTTEREYE_FRAMES"] = "off"
    env["BUTTEREYE_FRAMES_MODE"] = "mc"
    env["BUTTEREYE_FRAMES_SHM"] = os.fspath(shm)
    return env


def layer_k(mid: Fraction, target: Fraction) -> int | None:
    """k for a cascade ``mid`` → ``target`` (``decide.cascade_target``), or None
    when the layer can't make it (not a whole step of 2 to ``KMAX``)."""
    if mid <= 0:
        return None
    n = target / mid
    if n.denominator != 1 or not 2 <= n <= KMAX:
        return None
    return int(n)


def override_fps(real_hz: float, k: int) -> float:
    """``display-fps-override`` in layer mode: the rate mpv presents at."""
    return real_hz / k


#: the audio ledger (``AudioLedger``), in ButterEye's state folder
AUDIO_FILE = "frames-audio.json"
AUDIO_ENTRIES = 256
#: mpv writes ``audio-delay`` into a resume file with 6 decimals
AUDIO_MATCH_S = 1e-5


def audio_key(path: str) -> str:
    """The ledger's key for a file mpv plays: a hash, so the ledger is no list
    of what was watched."""
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class AudioLedger:
    """What layer mode left in mpv's ``audio-delay``, per file.

    Layer mode adds its delay to the user's own ``audio-delay`` and leaves
    ``audio-delay`` in ``watch-later-options``, so the user's own offset still
    reaches a resume file. When mpv writes one while layer mode is on
    (``quit-watch-later``, ``save-position-on-quit``: mpv writes it inside its
    quit command, before any hook or IPC message could take the add off), the
    value has the add in it. The ledger keeps that value and the add, so the
    next layer mode on that file sees the add is there already (``match``) and
    does not add it twice, and leaving takes it off: the user's own offset
    again. Played without ButterEye, such a file is (k - 1) refreshes late
    until layer mode next plays it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: dict[str, tuple[float, float]] = {}
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            data = None
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and isinstance(v, list) and len(v) == 2:
                    if _num(v[0]) and _num(v[1]):
                        self.entries[k] = (float(v[0]), float(v[1]))

    def match(self, key: str, value: float) -> float:
        """The add inside ``value`` when it is the value layer mode last left
        for this file, else 0."""
        entry = self.entries.get(key)
        if entry is None or abs(entry[0] - value) > AUDIO_MATCH_S:
            return 0.0
        return entry[1]

    def note(self, key: str, value: float, add: float) -> None:
        """mpv's ``audio-delay`` for this file is ``value``, ``add`` of it layer
        mode's (stored at once: mpv may write a resume file at any moment). A
        failed write keeps it for this run only."""
        old = self.entries.pop(key, None)
        self.entries[key] = (value, add)
        while len(self.entries) > AUDIO_ENTRIES:
            del self.entries[next(iter(self.entries))]
        if old is not None and max(abs(old[0] - value), abs(old[1] - add)) <= AUDIO_MATCH_S:
            return
        from buttereye.core.scriptgen.generator import write_atomic

        doc = {k: [v, a] for k, (v, a) in self.entries.items()}
        with contextlib.suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_atomic(self.path, json.dumps(doc) + "\n")


def video_rect(osd: object) -> tuple[int, int, int, int] | None:
    """The video's rectangle in the window from mpv's ``osd-dimensions``
    (margins are the black bars), or None when it says nothing usable."""
    if not isinstance(osd, dict):
        return None
    try:
        w, h = int(osd["w"]), int(osd["h"])
        ml, mr = int(osd.get("ml", 0)), int(osd.get("mr", 0))
        mt, mb = int(osd.get("mt", 0)), int(osd.get("mb", 0))
    except KeyError, TypeError, ValueError:
        return None
    rw, rh = w - ml - mr, h - mt - mb
    if w <= 0 or h <= 0 or rw <= 0 or rh <= 0:
        return None
    return (max(0, ml), max(0, mt), rw, rh)


def layer_notice() -> Msg:
    """The session's notice while the layer makes the last frames."""
    return Msg(
        "RIFE makes every second frame and the GPU makes the rest as mpv shows them (experimental)."
    )


def fallback_notice() -> Msg:
    """The layer did not take over: the cascade runs in VapourSynth as before."""
    return Msg("The GPU frames layer did not start, so smoothing runs as before.")


def unavailable_notice(why: str) -> Msg:
    """``[general] gpu_frames`` is on but can't work here (``osapi.frames_layer_status``)."""
    if why == "platform":
        return Msg(
            "Making the last frames on the GPU works on Linux only; smoothing runs as before."
        )
    if why == "wayland":
        return Msg(
            "Making the last frames on the GPU needs a Wayland session; smoothing runs as before."
        )
    return Msg(
        "Making the last frames on the GPU needs ButterEye's frames layer, which was not "
        "found; smoothing runs as before."
    )


__all__ = [
    "fallback_notice",
    "layer_notice",
    "unavailable_notice",
    "AUDIO_ENTRIES",
    "AUDIO_FILE",
    "AUDIO_MATCH_S",
    "KMAX",
    "MPV_ARGS",
    "NV_PRESENT",
    "AudioLedger",
    "ControlBlock",
    "LayerStats",
    "audio_key",
    "layer_env",
    "layer_k",
    "override_fps",
    "video_rect",
]
