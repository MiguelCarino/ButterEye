# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live-play provider (GUI.md §11.4; SCOPE §4.2, §4.3, §4.10, §4.11, F2-F4, F7, F8).

M1 subset: ``play`` launches the user's mpv with ButterEye's include, profile and
Lua helper and an IPC socket in the 0700 runtime directory; mpv outlives the GUI.
The filter is added over IPC once ``video-params`` and ``display-fps`` are known
(§4.2 step 2), as ``vf add @buttereye:vapoursynth=…`` with a new ``gen`` per change,
verified in the ``vf`` property. A failed init, a stalled picture or a lost GPU
device removes the filter (rollback, F4) and is reported; ButterEye never turns
it back on by itself, except that a stalled or faulting RIFE-ncnn filter is
replaced by MVTools once per session (GUI.md §11.9). The engine is chosen after
the target, against each engine's benchmark cap with live headroom (§11.9).
``detach`` closes the IPC connection only.

Another file in the same mpv (playlist-next, ``loadfile``, drag and drop) removes
the filter and decides again from the new file's facts; a display-rate change
re-decides an active filter and a display-dependent bypass; an mpv that stops
answering is reported as CONNECTION_LOST ("mpv is not responding") without ending
the session. Ended sessions leave the registry: ``sessions()`` lists live players.

Not in this subset: attach to foreign instances (``mpvctl.attach``), orphan
filter removal (``mpvctl.orphans``), VFR handling (sources are treated as CFR),
the consented ``include=`` writer (``mpvctl.include``).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import math
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from buttereye.core import capabilities, filelog
from buttereye.core.backends import trt
from buttereye.core.capabilities import ProviderRef
from buttereye.core.errors import ButterEyeError, ErrorCode, FilterFailed, NotAvailable
from buttereye.core.events import (
    Event,
    HealthChanged,
    Notice,
    OrphansFound,
    SessionAdded,
    SessionChanged,
    SessionEnded,
)
from buttereye.core.i18n import render
from buttereye.core.mpvctl import decide, launcher, shaders
from buttereye.core.mpvctl.health import (
    HealthDetector,
    Thresholds,
    Verdict,
    classify_log,
    device_lost_lines,
    gpu_fault_scan,
    mark_rife_session,
    sample_from,
)
from buttereye.core.mpvctl.ipc import (
    HELPER_NAME,
    LABEL,
    OBSERVABLE,
    VF_LABEL,
    IpcClosed,
    MpvError,
    MpvIpc,
    SocketRefused,
    check_socket_path,
)
from buttereye.core.scriptgen.generator import (
    RIFE_GPU_THREAD_DEFAULT,
    FilterParams,
    ScriptConstants,
    default_frames,
    entry_gen,
    filter_entry,
    vf_argument,
    write_script,
)
from buttereye.core.types import (
    BackendId,
    BypassReason,
    CandidateId,
    Config,
    Counters,
    DetachPolicy,
    FilterState,
    HdrClass,
    Health,
    InstanceCandidate,
    Msg,
    Origin,
    Profile,
    RefusalReason,
    SessionId,
    SessionSnapshot,
    ShutdownReport,
    SourceFacts,
    Target,
    TargetKind,
    VulkanDevice,
)

if TYPE_CHECKING:
    from buttereye.core.api import ProviderContext
    from buttereye.core.ops import OpContext

_log = logging.getLogger(__name__)

REGISTRY_KEY = "mpvctl.session"
MODEL_SUBDIR = "rife-ncnn-models"
USER_MODEL_SUBDIR = "models"  # data_dir/models (storage.models lists it, §4.6)

_RULES_EXPLAIN = ProviderRef("buttereye.core.profiles.rules", "explain")
_BUILTIN_PROFILE = ProviderRef("buttereye.core.profiles.defaults", "builtin_profile")
#: Shipped profiles whose engine follows Setup's choice; "cpu" stays MVTools (the
#: last step of the step-down chain, §5.3 step 5).
SETUP_FOLLOWING_BUILTINS = frozenset({"quality", "balanced", "fast"})
_BENCH_HISTORY = ProviderRef("buttereye.core.bench.runner", "history")
_CHOOSE_DEVICE = ProviderRef("buttereye.core.hw.detect", "choose_device")
#: the full benchmark's sizes (``bench.runner.FULL_SIZES``; not imported, the bench
#: is an optional provider)
_FULL_SIZES: tuple[tuple[int, int], ...] = ((1280, 720), (1920, 1080), (2560, 1440), (3840, 2160))
#: In-mpv rate must exceed the target by this factor to count as real time (F2);
#: the same value as ``bench.runner.REALTIME_HEADROOM`` (the bench's own verdict).
REALTIME_HEADROOM = 1.15
#: Live playback cap = benchmark ``mpv_fps`` / this (§11.9). The bench measures mpv
#: untimed with ``--vo=null``; real playback also renders every output frame to the
#: display and shares the GPU with the desktop compositor, so it sustains less.
#: 1080p RIFE-ncnn at 61 fps in the bench → ~49 live: enough for 24 → 48, not for
#: 30 → 60 (which then uses MVTools or plays unsmoothed). The cap is a 2× rate;
#: other multipliers are costed by their interpolated frames (``decide.load_rate``),
#: so 23.976 → 60 needs ~120 (almost every frame inferred), not 60.
LIVE_HEADROOM = 1.25
#: The lite model must beat the full one by more than this in this machine's
#: benchmark to be offered as a step down (§5.3 step 5).
LITE_GAIN = 1.05
#: Targets that follow the display rate (re-decided when it changes, §4.11).
DISPLAY_KINDS = frozenset({TargetKind.DISPLAY, TargetKind.DISPLAY_MAX})
#: Property changes that wake the controller at once. The others (playback-time,
#: audio-pts, video-frame-info and the drop counters change every frame, several
#: times per output frame in all) are only stored and read on the 0.25 s tick.
WAKE_PROPS = frozenset(
    {
        "vf",
        "video-params",
        "path",
        "display-fps",
        "pause",
        "seeking",
        "core-idle",
        "paused-for-cache",
        "current-tracks/video",
        "container-fps",
    }
)
#: Properties that describe the loaded file; dropped when mpv starts another file.
FILE_PROPS = (
    "path",
    "container-fps",
    "video-params",
    "current-tracks/video",
    "video-frame-info",
    "playback-time",
    "audio-pts",
)

#: Used only when this build has no config provider (U2): automatic settings,
#: not data. Every value is the documented default (§4.4, §4.9, §5.6).
FALLBACK_PROFILE = Profile(
    id="automatic",
    name="Automatic",
    backend="auto",
    model=None,
    scale=None,
    target=Target(TargetKind.DISPLAY),
    sc_threshold=0.12,
    buffered_frames=None,
    concurrent_frames=None,
    hdr="skip",
    builtin=True,
)

BYPASS_TEXT: Mapping[BypassReason, str] = {
    BypassReason.ALREADY_AT_RATE: "Already at your screen's rate — nothing to do",
    BypassReason.INTERLACED: "Interlaced video — smoothing skipped",
    BypassReason.HDR_SKIP: "HDR video — smoothing skipped (default)",
    BypassReason.UNSUPPORTED_FORMAT: "This video format can't be smoothed",
    BypassReason.NO_VIDEO: "No video to smooth",
    BypassReason.NO_REALTIME: "Too slow for real time at this resolution — smoothing off",
}

_REFUSAL_TEXT: Mapping[RefusalReason, str] = {
    RefusalReason.NOT_SOCKET: "Refused: not a socket",
    RefusalReason.WRONG_UID: "Refused: socket owned by another user",
    RefusalReason.BAD_PARENT: "Refused: the socket's folder is not owned by you",
    RefusalReason.PEERCRED: "Refused: the process behind the socket belongs to another user",
}


@dataclass
class LiveOptions:
    """Launch and detection settings. Defaults are the shipped behaviour; tests
    change them through ``registry(ctx).options`` (e.g. ``--vo=null``)."""

    mpv: str = "mpv"
    extra_args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None
    thresholds: Thresholds = field(default_factory=Thresholds)
    connect_timeout_s: float = 10.0
    verify_timeout_s: float = 4.0
    tick_s: float = 0.25
    display_debounce_s: float = 2.0  # §4.2 step 7
    no_video_after_s: float = 3.0
    start_deadline_s: float = 15.0  # no video yet after this → "mpv hasn't opened the video"
    probe_after_s: float = 1.5  # IPC silence before ButterEye checks that mpv still answers
    unresponsive_after_s: float = 3.0  # silence + an unanswered probe → "not responding"
    probe_timeout_s: float = 1.0
    log_check_s: float = 30.0  # how often a running session's log size is checked


TRT_BUILDS = "mpvctl.trt_builds"  # EngineKey -> "building" | "ready" | exception
TRT_IDENTITY = "mpvctl.trt_identity"  # Vulkan UUID -> (nvidia UUID, driver) | None
#: Automatic engine order: TensorRT when set up and not turned off
LIVE_RANKING = (BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVTOOLS)


class Registry:
    """Per-core session registry (``ctx.state("mpvctl.session", Registry)``)."""

    def __init__(self) -> None:
        self.options = LiveOptions()
        self.sessions: dict[SessionId, _Session] = {}


def registry(ctx: ProviderContext) -> Registry:
    return ctx.state(REGISTRY_KEY, Registry)


def _new_sid() -> SessionId:
    return SessionId(uuid.uuid4().hex[:12])


def _lost(sid: SessionId) -> ButterEyeError:
    return ButterEyeError(
        ErrorCode.IPC_LOST,
        Msg("ButterEye is not connected to that player."),
        Msg("Open the video again from ButterEye."),
        detail=str(sid),
    )


def _fmt_rate(r: Fraction) -> str:
    return f"{float(r):.3f}".rstrip("0").rstrip(".")


def _follows_setup_choice(profile: Profile) -> bool:
    """A shipped Quality/Balanced/Fast profile still on its shipped engine."""
    if not profile.builtin or profile.id not in SETUP_FOLLOWING_BUILTINS:
        return False
    shipped_of = capabilities.resolve(_BUILTIN_PROFILE)
    shipped = shipped_of(profile.id) if shipped_of is not None else None
    if shipped is None:
        return profile.backend is BackendId.RIFE_NCNN
    return bool(shipped.backend == profile.backend)


def _when_key(result: Any) -> float:
    """A benchmark result's time for sorting (-inf = undated)."""
    when = getattr(result, "when", None)
    return when.timestamp() if isinstance(when, datetime) else -math.inf


def _measured_in_mpv(m: Any) -> bool:
    """False for a benchmark entry whose in-mpv pass failed: the runner then stores
    the vspipe speed as ``mpv_fps`` (and the vspipe reload time as ``startup_s``)
    with ``realtime`` False."""
    if getattr(m, "realtime", False):
        return True
    return not (m.mpv_fps == m.vspipe_fps and m.startup_s == m.reload_s)


def _measured_size(label: str, req: Any) -> tuple[int, int]:
    """The frame size a measurement ran at: the request's, or ``FULL_SIZES`` for a
    ``"<label> @ <h>p"`` entry of a full benchmark (16:9 when the height is unknown)."""
    _, sep, tail = label.rpartition(" @ ")
    if sep and tail.endswith("p") and tail[:-1].isdigit():
        h = int(tail[:-1])
        for fw, fh in _FULL_SIZES:
            if fh == h:
                return fw, fh
        return (h * 16 // 9) // 2 * 2, h
    return int(getattr(req, "width", 0) or 0), int(getattr(req, "height", 0) or 0)


def _choose_device(devices: Sequence[VulkanDevice], override: str | None) -> str | None:
    choose = capabilities.resolve(_CHOOSE_DEVICE)
    if choose is not None:
        try:
            chosen = choose(devices, override)
        except Exception:
            _log.exception("choose_device failed")
        else:
            return chosen if isinstance(chosen, str) else None
    usable = [d for d in devices if d.device_type != "cpu"]
    if override:
        o = override.strip().lower()
        for d in usable:
            if d.uuid.lower() == o or str(d.index) == o:
                return d.uuid
    return usable[0].uuid if usable else None


def _resolve_device(
    devices: Sequence[VulkanDevice], wanted: str | None, *, choose: bool = False
) -> VulkanDevice | None:
    """The device named by ``wanted`` (UUID or index, any case, spaces ignored).

    ``choose``: pick the device doctor would pick (``hw.detect.choose_device``),
    which also applies when ``wanted`` is unset or names no usable device."""
    if choose:
        uuid_ = _choose_device(devices, wanted)
        return next((d for d in devices if d.uuid == uuid_), None) if uuid_ else None
    if not wanted:
        return None
    o = wanted.strip().lower()
    for d in devices:
        if d.uuid.lower() == o or str(d.index) == o:
            return d
    return None


# ---------------------------------------------------------------------------
# One session
# ---------------------------------------------------------------------------


class _Session:
    def __init__(
        self,
        ctx: ProviderContext,
        options: LiveOptions,
        *,
        sid: SessionId,
        file: Path,
        proc: launcher.MpvProcess,
        ipc: MpvIpc,
        socket: Path,
        log_path: Path,
        script_path: Path,
        profile_request: str | None,
    ) -> None:
        self.ctx = ctx
        self.opts = options
        self.sid = sid
        self.file = file
        self.title = file.name
        self.log = filelog.for_item(_log, filelog.session_key(sid))
        self.flow_scale = 1.0  # TensorRT RIFE flow resolution chosen by apply (M0(q))
        # what the running filter does, for the snapshot (reset when it is off)
        self.run_size: tuple[int, int] | None = None
        self.run_flow = 1.0
        self.run_block = False
        self.proc = proc
        self.pid: int | None = proc.pid
        self.ipc = ipc
        self.socket = socket
        self.log_path = log_path
        self.script_path = script_path
        self.profile_request = profile_request
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        # Xid scan cursor: only faults newer than the last reported one (F1)
        self.fault_since_wall: float = self.started_wall
        self.fault_since_mono: float = self.started_mono
        self.fault_lock = asyncio.Lock()
        self.props: dict[str, Any] = {}
        self.file_loaded_at: float | None = None
        # another file in the same mpv (playlist-next, loadfile, drag and drop)
        self.new_file = False
        self.refetch_props = False
        self.start_notice: Msg | None = None
        # liveness: consecutive unanswered probes, "not responding" state
        self.probe_timeouts = 0
        self.unresponsive = False
        self.health_before_unresponsive: tuple[Health, ErrorCode | None] | None = None
        self.log_checked_at = self.started_mono
        self.file_started_mono = self.started_mono
        self.wake = asyncio.Event()
        self.lock = asyncio.Lock()
        self.detector = HealthDetector(options.thresholds)
        self.task: asyncio.Task[None] | None = None
        self.busy = False
        self.remove_pending = False
        self.want = True
        self.toggle_requested = False
        #: TensorRT engine this session fell back from while it is being built
        self.trt_waiting: trt.EngineKey | None = None
        #: the bundled shader this session added to mpv (§15.2), or None
        self.shader: Path | None = None
        #: mpv's deband value before ButterEye turned it on (§15.2), or None
        self.deband_restore: bool | None = None
        self.trt_built = False
        self.failure: list[str] = []
        self.device_lost = False
        self.rife_used = False
        # §11.9: after RIFE-ncnn stalled or faulted, this session runs MVTools only
        # (set once, never cleared: ButterEye never goes back to RIFE by itself)
        self.backend_override: BackendId | None = None
        self.override_notice: Msg | None = None
        # the user asked to smooth anyway: NO_REALTIME runs uncapped (whole session)
        self.forced = False
        self.active_gen: int | None = None
        self.applied_display: float | None = None
        self.display_changed_at: float | None = None
        # display rate a display-dependent bypass (ALREADY_AT_RATE/NO_REALTIME) was decided at
        self.bypass_display: float | None = None
        self.config_notice_sent = False
        self.last_status: str | None = None
        # snapshot fields
        self.source: SourceFacts | None = None
        self.display_fps: float | None = None
        self.target: Fraction | None = None
        self.multiplier: Fraction | None = None
        self.backend: BackendId | None = None
        self.model: str | None = None
        self.profile_id: str | None = None
        self.filter = FilterState.PENDING
        self.bypass: BypassReason | None = None
        self.health = Health.OK
        self.health_code: ErrorCode | None = None
        self.gen = 0
        self.drop_rate: float | None = None
        self.step_down_to: str | None = None
        self.notice: Msg | None = None
        self.ended = False
        self._last_key: tuple[object, ...] | None = None
        self._last_counters: tuple[object, ...] | None = None

    # ---- snapshot / events ----
    def emit(self, ev: Event) -> None:
        """Emit ``ev``; health changes and notices also go to the log (gui.log), so
        what happened to a video can be read back after the fact."""
        if isinstance(ev, HealthChanged):
            self.log.info(
                "%s: %s%s: %s%s",
                self.title,
                ev.health.value,
                f" ({ev.code.value[0]})" if ev.code is not None else "",
                render(ev.reason),
                f" → {render(ev.auto_action)}" if ev.auto_action is not None else "",
            )
            for line in ev.evidence[:10]:
                self.log.info("%s:   %s", self.title, line)
        elif isinstance(ev, Notice):
            self.log.info(
                "%s: %s%s",
                self.title,
                render(ev.message),
                f" ({ev.code.value[0]})" if ev.code is not None else "",
            )
        self.ctx.emit(ev)

    def counters(self) -> Counters:
        def n(key: str) -> int:
            v = self.props.get(key)
            return v if isinstance(v, int) and not isinstance(v, bool) else 0

        return Counters(
            frame_drop=n("frame-drop-count"),
            decoder_drop=n("decoder-frame-drop-count"),
            vo_delayed=n("vo-delayed-frame-count"),
            mistimed=n("mistimed-frame-count"),
            display_sync=self.props.get("display-sync-active") is True,
        )

    def snapshot(self) -> SessionSnapshot:
        return SessionSnapshot(
            sid=self.sid,
            origin=Origin.LAUNCHED,
            pid=self.pid,
            title=self.title,
            source=self.source,
            display_fps=self.display_fps,
            target_fps=self.target,
            multiplier=self.multiplier,
            backend=self.backend,
            model=self.model,
            profile_id=self.profile_id,
            filter=self.filter,
            bypass=self.bypass,
            health=self.health,
            health_code=self.health_code,
            gen=self.gen,
            counters=self.counters(),
            drop_rate_10s=self.drop_rate,
            step_down_to=self.step_down_to,
            notice=self.notice,
            restore_pending=(),
            ended=self.ended,
            smooth_size=self.run_size,
            flow_scale=self.run_flow,
            mv_block=self.run_block,
        )

    def _key(self, s: SessionSnapshot) -> tuple[object, ...]:
        return (
            s.source, s.display_fps, s.target_fps, s.multiplier, s.backend, s.model,
            s.profile_id, s.filter, s.bypass, s.health, s.health_code, s.gen,
            s.step_down_to, s.notice, s.ended, s.smooth_size, s.flow_scale, s.mv_block,
        )  # fmt: skip

    def publish(self) -> SessionSnapshot:
        """Emit a transition ``SessionChanged`` or a coalesced counter update."""
        snap = self.snapshot()
        key = self._key(snap)
        counters = (snap.counters, snap.drop_rate_10s)
        if key != self._last_key:
            self._last_key = key
            self._last_counters = counters
            # a pending counter-only update must not arrive after this transition
            self.ctx.forget_coalesced(self.sid)
            self.emit(SessionChanged(snap))
        elif counters != self._last_counters:
            self._last_counters = counters
            self.ctx.emit_coalesced(self.sid, SessionChanged(snap), min_interval_s=0.25)
        return snap

    def mark_published(self, snap: SessionSnapshot) -> None:
        self._last_key = self._key(snap)
        self._last_counters = (snap.counters, snap.drop_rate_10s)

    # ---- IPC events (sync, from the reader task) ----
    def on_event(self, msg: Mapping[str, Any]) -> None:
        ev = msg.get("event")
        if ev == "property-change":
            name = msg.get("name")
            if isinstance(name, str):
                self.props[name] = msg.get("data")
            if name not in WAKE_PROPS:
                return  # per-frame counters: the 0.25 s tick reads them
        elif ev == "log-message":
            kind = classify_log(msg, only_our_vapoursynth=self._only_our_vapoursynth())
            ours = self.active_gen is not None or self.busy
            if kind == "device_lost" or (kind == "filter_failed" and ours):
                text = str(msg.get("text", "")).strip()
                if kind == "device_lost":
                    self.device_lost = True
                if text and len(self.failure) < 20:
                    self.failure.append(text)
        elif ev == "start-file":
            # mpv opens another file in the same player: everything decided for the
            # previous file (source facts, matrix, src_fps, bypass) is stale (§4.11).
            for key in FILE_PROPS:
                self.props.pop(key, None)
            self.file_loaded_at = None
            self.file_started_mono = time.monotonic()
            self.new_file = True
            self.refetch_props = True
        elif ev == "file-loaded":
            self.file_loaded_at = time.monotonic()
            self.refetch_props = True
        elif ev in ("seek", "playback-restart"):
            self.detector.note_restart(time.monotonic())
        elif ev == "client-message":
            args = msg.get("args")
            if isinstance(args, list) and args[:2] == ["buttereye-request", "toggle"]:
                self.toggle_requested = True
        self.wake.set()

    def _only_our_vapoursynth(self) -> bool:
        vf = self.props.get("vf")
        if not isinstance(vf, list):
            return True
        return all(
            not (isinstance(e, dict) and e.get("name") == "vapoursynth" and e.get("label") != LABEL)
            for e in vf
        )

    # ---- helper / OSD ----
    def status_text(self) -> str:
        """The helper's OSD status line (F8), translated like every core text."""
        if self.ended:
            return render(Msg("ButterEye: disconnected"))
        if self.filter is FilterState.ACTIVE and self.source and self.target and self.multiplier:
            engine = decide.BACKEND_NAMES.get(self.backend, "") if self.backend else ""
            return render(
                Msg(
                    "ButterEye: {source} → {target} fps ({multiplier}×) · {engine}{model}",
                    {
                        "source": _fmt_rate(self.source.fps),
                        "target": _fmt_rate(self.target),
                        "multiplier": _fmt_rate(self.multiplier),
                        "engine": engine,
                        "model": f" {self.model}" if self.model else "",
                    },
                )
            )
        if self.filter is FilterState.BYPASSED and self.bypass is not None:
            return render(
                Msg("ButterEye: {reason}", {"reason": render(Msg(BYPASS_TEXT[self.bypass]))})
            )
        if self.filter is FilterState.ROLLED_BACK:
            why = render(self.notice) if self.notice else ""
            return render(Msg("ButterEye: smoothing turned off. {why}", {"why": why})).strip()
        if self.filter is FilterState.OFF:
            return render(Msg("ButterEye: smoothing off"))
        return render(Msg("ButterEye: starting…"))

    async def send_status(self, *, quiet: bool = False) -> None:
        text = self.status_text()
        if text == self.last_status or self.ipc.closed.is_set():
            return
        self.last_status = text
        args: list[object] = ["script-message-to", HELPER_NAME, "status", text]
        if quiet:
            args.append("quiet")
        with contextlib.suppress(IpcClosed, MpvError, TimeoutError):
            await self.ipc.command(*args, timeout_s=1.0)

    # ---- config / profile ----
    def config(self) -> tuple[Config | None, Msg | None]:
        try:
            return self.ctx.config(), None
        except NotAvailable:
            return None, Msg("Profiles aren't available in this build; using automatic settings.")
        except ButterEyeError as exc:
            return None, exc.cause

    def pick_profile(self, cfg: Config | None, facts: SourceFacts) -> tuple[Profile, Msg | None]:
        if cfg is None or not cfg.profiles:
            return FALLBACK_PROFILE, None
        by_id = {p.id: p for p in cfg.profiles}
        if self.profile_request is not None:
            if self.profile_request in by_id:
                return by_id[self.profile_request], None
            note = Msg(
                "Profile {profile} no longer exists; using the rules.",
                {"profile": self.profile_request},
            )
        else:
            note = None
        explain = capabilities.resolve(_RULES_EXPLAIN)
        if explain is not None:
            try:
                trace = explain(cfg, facts)
                chosen = by_id.get(getattr(trace, "profile_id", ""))
                if chosen is not None:
                    return chosen, note
            except Exception:
                self.log.exception("rule evaluation failed")
        return cfg.profiles[0], note

    async def sustainable_fps(
        self, backend: BackendId, model: str | None, facts: SourceFacts, cfg: Config | None
    ) -> float | None:
        """The live rate cap for ``backend``/``model`` at this video's size (§11.9).

        Only measurements taken inside mpv count (a failed in-mpv pass stores the
        vspipe speed, which is far above what mpv sustains), only on the GPU used
        now, never one that faulted. A measurement at another size is scaled by the
        pixel-count ratio (bench w×h / video w×h: interpolation cost grows with the
        pixels); the measurement closest in pixel count wins, the newest among
        equally close ones, then the fastest. The result is ``mpv_fps /
        LIVE_HEADROOM`` as a 2× output rate (``decide.load_rate`` units: a bench
        run at another multiplier is converted by its interpolated frames), so
        ``decide.choose_target`` costs each target by the frames it infers. None =
        no benchmark for this engine."""
        history = capabilities.resolve(_BENCH_HISTORY)
        if history is None:
            return None
        try:
            results = await history(self.ctx)
        except Exception:
            return None
        video_px = facts.width * facts.height
        if video_px <= 0:
            return None
        current = self.gpu_device(cfg)
        devices = self.vulkan_devices()
        # (-distance in log pixel count, measured at (s, -inf = undated), scaled fps)
        found: list[tuple[float, float, float]] = []
        for result in results:
            req = getattr(result, "request", None)
            if req is None:
                continue
            if backend in decide.GPU_BACKENDS and current is not None:
                measured_on = _resolve_device(devices, getattr(result, "gpu_uuid", None))
                if measured_on is not None and measured_on.uuid != current.uuid:
                    continue
            when = getattr(result, "when", None)
            stamp = when.timestamp() if isinstance(when, datetime) else -math.inf
            for m in getattr(result, "measurements", ()):
                if m.backend is not backend or (model is not None and m.model != model):
                    continue
                if m.gpu_faults or not _measured_in_mpv(m) or not m.mpv_fps > 0:
                    continue
                w, h = _measured_size(m.label, req)
                if w <= 0 or h <= 0:
                    continue
                ratio = (w * h) / video_px
                fps = decide.as_2x_rate(
                    float(m.mpv_fps),
                    getattr(req, "source_fps", None),
                    getattr(req, "target_fps", None),
                )
                found.append((round(-abs(math.log(ratio)), 6), stamp, fps * ratio))
        if not found:
            return None
        return max(found)[2] / LIVE_HEADROOM

    async def step_down_choice(self) -> str | None:
        """The profile to offer when frames drop (§5.3 step 5)."""
        cfg, _ = self.config()
        profiles = cfg.profiles if cfg is not None else ()
        return decide.step_down_target(
            self.profile_id,
            profiles,
            multiplier=self.multiplier,
            skip_lite=self.profile_id == "quality" and await self.lite_no_faster(profiles),
        )

    async def lite_no_faster(self, profiles: Sequence[Profile]) -> bool:
        """This machine's newest benchmark shows "balanced"'s (lite) model at most
        ``LITE_GAIN`` faster in mpv than "quality"'s, at the same size: stepping
        to it would not shed load (RTX 4090: v4.26 61.2 fps, v4.22-lite 62.6).
        False without such a pair, so untested GPUs keep the full chain."""
        by_id = {p.id: p for p in profiles}
        heavy = by_id.get("quality")
        lite = by_id.get("balanced")
        if heavy is None or lite is None or not heavy.model or not lite.model:
            return False
        history = capabilities.resolve(_BENCH_HISTORY)
        if history is None:
            return False
        try:
            results = await history(self.ctx)
        except Exception:
            return False
        for result in sorted(results, key=_when_key, reverse=True):
            rates: dict[tuple[str | None, tuple[int, int]], float] = {}
            for m in getattr(result, "measurements", ()):
                if m.backend is not BackendId.RIFE_NCNN or m.gpu_faults:
                    continue
                if not _measured_in_mpv(m) or not m.mpv_fps > 0:
                    continue
                rates[(m.model, _measured_size(m.label, result.request))] = float(m.mpv_fps)
            for (model, size), fps in rates.items():
                lite_fps = rates.get((lite.model, size))
                if model == heavy.model and lite_fps is not None:
                    return lite_fps <= fps * LITE_GAIN
        return False

    def vulkan_devices(self) -> tuple[VulkanDevice, ...]:
        report = self.ctx.last_report()
        return tuple(report.hardware.vulkan) if report is not None else ()

    def gpu_device(self, cfg: Config | None) -> VulkanDevice | None:
        """The interpolation device, chosen like doctor/setup do (config ``gpu`` is
        a Vulkan UUID or index, any case, spaces ignored); None without a report."""
        report = self.ctx.last_report()
        if report is None:
            return None
        override = cfg.general.gpu if cfg is not None else None
        return _resolve_device(report.hardware.vulkan, override, choose=True)

    def gpu_index(self, cfg: Config | None) -> int | None:
        dev = self.gpu_device(cfg)
        return dev.index if dev is not None else None

    # ---- filter operations (call with self.lock held) ----
    def next_gen(self) -> int:
        seen = entry_gen(filter_entry(self.props.get("vf"))) or 0
        self.gen = max(self.gen, seen) + 1
        return self.gen

    async def remove_filter(self, *, timeout_s: float = 5.0) -> None:
        """``vf remove @buttereye``. A stuck filter can block mpv; then the removal
        is retried on every tick until mpv answers (``remove_pending``). A player
        known to be unresponsive is not asked again until it answers."""
        if (
            filter_entry(self.props.get("vf")) is not None
            or self.active_gen is not None
            or self.remove_pending
        ):
            if self.unresponsive:
                self.remove_pending = True
            else:
                try:
                    await self.ipc.command("vf", "remove", VF_LABEL, timeout_s=timeout_s)
                    self.remove_pending = False
                except TimeoutError:
                    self.remove_pending = True
                    self.log.warning("session %s: mpv did not answer vf remove; retrying", self.sid)
                except IpcClosed, MpvError:
                    self.remove_pending = False
        self.active_gen = None
        self.detector.filter_stopped()

    async def set_off(self, state: FilterState, notice: Msg | None = None) -> None:
        await self.remove_filter()
        self.filter = state
        if state is not FilterState.BYPASSED:
            self.bypass = None
        self.bypass_display = None
        self.display_changed_at = None
        self.notice = notice
        self.step_down_to = None
        self.drop_rate = None
        self.run_size, self.run_flow, self.run_block = None, 1.0, False

    async def bypass_with(self, reason: BypassReason, *, display_hz: float | None = None) -> None:
        """Bypass; ``display_hz`` when the verdict depends on the display rate, so a
        later change of the display rate re-decides it (§4.11 "recompute"). No
        engine runs, so the snapshot shows none."""
        await self.set_off(FilterState.BYPASSED)
        self.bypass = reason
        self.bypass_display = display_hz
        self.backend = self.model = None
        self.target = self.multiplier = None

    def wanted_backend(
        self, profile: Profile, cfg: Config | None
    ) -> tuple[BackendId | None, Msg | None]:
        """The engine a profile asks for, with the Setup choice applied (§5.3).

        ``general.backend_override`` (Setup's engine page) applies to profiles set
        to Automatic and to the shipped Quality/Balanced/Fast profiles while they
        keep their shipped engine; CPU (MVTools) and profiles the user pinned to an
        engine keep theirs, with a notice when that differs from the Setup choice."""
        override = cfg.general.backend_override if cfg is not None else None
        if profile.backend == "auto":
            return override, None
        pinned = BackendId(profile.backend)
        if override is None or override is pinned:
            return pinned, None
        if _follows_setup_choice(profile):
            return override, None
        return pinned, Msg(
            "Profile {profile} uses {engine}; the engine chosen in Setup ({choice}) "
            "applies only to profiles set to Automatic.",
            {
                "profile": profile.name,
                "engine": decide.BACKEND_NAMES.get(pinned, pinned.value),
                "choice": decide.BACKEND_NAMES.get(override, override.value),
            },
        )

    # ---- TensorRT (experimental, SCOPE §5.2) ----
    def trt_setup(
        self, cfg: Config | None, profile: Profile
    ) -> tuple[trt.TrtInstall | None, str | None]:
        """The TensorRT install and model to use, or (None, None) when the user
        turned it off or something is missing (doctor's TensorRT section says what).
        Unset counts as on: setting TensorRT up is the opt-in (§5.2)."""
        if cfg is None or cfg.general.trt_experimental is False:
            return None, None
        inst = trt.find_install(self.ctx.paths)
        if inst is None:
            return None, None
        avail = trt.available_models(inst)
        for name in (profile.model, trt.DEFAULT_MODEL, *sorted(avail)):
            if name in avail:
                return inst, name
        return inst, None

    async def trt_key(
        self,
        inst: trt.TrtInstall,
        model: str,
        width: int,
        height: int,
        cfg: Config | None,
        *,
        flow_scale: float = 1.0,
    ) -> trt.EngineKey | None:
        dev = self.gpu_device(cfg)
        cache: dict[str | None, tuple[str, str] | None] = self.ctx.state(TRT_IDENTITY, dict)
        vk = dev.uuid if dev is not None else None
        if vk not in cache:
            cache[vk] = await trt.nvidia_identity(vk)
        ident = cache[vk]
        if ident is None:
            return None
        return trt.EngineKey(
            ident[0],
            ident[1],
            inst.vstrt_version,
            inst.trt_version,
            model,
            width,
            height,
            flow_scale=flow_scale,
        )

    def engine_build_failed(self, key: trt.EngineKey | None) -> Msg | None:
        """A notice when the engine for ``key`` failed to build in this run."""
        if key is None:
            return Msg("TensorRT needs an NVIDIA GPU that nvidia-smi can see.")
        builds: dict[trt.EngineKey, object] = self.ctx.state(TRT_BUILDS, dict)
        state = builds.get(key)
        if isinstance(state, BaseException):
            return Msg(
                "The TensorRT engine for {w} × {h} couldn't be built; see the log in {logs}.",
                {"w": key.width, "h": key.height, "logs": str(self.ctx.paths.logs_dir)},
            )
        return None

    def start_engine_build(self, inst: trt.TrtInstall, key: trt.EngineKey) -> None:
        """Build the engine for ``key`` in the background (once per core); every
        session waiting for it re-decides when it is ready."""
        builds: dict[trt.EngineKey, object] = self.ctx.state(TRT_BUILDS, dict)
        if key in builds:
            return
        builds[key] = "building"
        ctx, starter = self.ctx, self
        script = self.script_path.with_name("trt-build.vpy")

        async def run() -> None:
            try:
                write_script(script, ScriptConstants(ctx.paths.rpm_plugin_dir))
                await trt.build_engine(ctx.paths, inst, key, script=script)
                builds[key] = "ready"
            except asyncio.CancelledError:
                builds.pop(key, None)
                raise
            except Exception as exc:  # recorded; the session keeps its fallback
                self.log.warning("TensorRT engine %s failed: %s", key.folder_name(), exc)
                builds[key] = exc
            for other in {*registry(ctx).sessions.values(), starter}:
                other.trt_built = True
                other.wake.set()

        ctx.spawn(run(), name=f"trt-build-{key.folder_name()}")

    async def sync_shader(self, cfg: Config | None) -> None:
        """Add or remove the bundled upscaling shader to match ``general.upscaling``
        (§15.2). Only ButterEye's own shader is touched; the user's stay."""
        want = shaders.shader_for(cfg.general.upscaling if cfg is not None else "standard")
        if want == self.shader:
            return
        try:
            if self.shader is not None:
                await self.ipc.command("change-list", "glsl-shaders", "remove", str(self.shader))
                self.shader = None
            if want is not None:
                # never twice: mpv may already list it (added at launch, or by a
                # sync that raced this one)
                listed = await self.ipc.get("glsl-shaders")
                if not (isinstance(listed, list) and str(want) in map(str, listed)):
                    await self.ipc.command("change-list", "glsl-shaders", "append", str(want))
                self.shader = want
        except (IpcClosed, MpvError, TimeoutError) as exc:
            self.log.warning("%s: could not change the upscaling shader: %s", self.title, exc)

    async def sync_deband(self, cfg: Config | None) -> None:
        """Turn mpv's own debanding on for ``general.deband`` and back to its
        previous value when it is turned off (§15.2). A deband=yes from the
        user's mpv.conf is never turned off."""
        want = bool(cfg.general.deband) if cfg is not None else False
        try:
            if want and self.deband_restore is None:
                current = await self.ipc.get("deband")
                if current is not True:
                    await self.ipc.command("set_property", "deband", True)
                    self.deband_restore = bool(current)
            elif not want and self.deband_restore is not None:
                await self.ipc.command("set_property", "deband", self.deband_restore)
                self.deband_restore = None
        except (IpcClosed, MpvError, TimeoutError) as exc:
            self.log.warning("%s: could not change debanding: %s", self.title, exc)

    async def apply(self, *, raise_on_fail: bool) -> None:
        """Decide (§4.11, §5.6) and add/replace/remove the filter; verify a new gen."""
        cfg_now = self.config()[0]
        await self.sync_shader(cfg_now)
        await self.sync_deband(cfg_now)
        props = self.props
        vp = props.get("video-params")
        loaded = self.file_loaded_at is not None
        if decide.no_video(props) or (
            loaded
            and not isinstance(vp, dict)
            and time.monotonic() - (self.file_loaded_at or 0.0) >= self.opts.no_video_after_s
            and props.get("current-tracks/video") is None
        ):
            await self.bypass_with(BypassReason.NO_VIDEO)
            return
        facts = decide.source_facts(props, path=str(props.get("path") or self.file))
        if facts is None or not isinstance(vp, dict) or not decide.params_ready(vp):
            return  # still PENDING (mpv can publish w/h before the pixel format)
        self.source = facts
        self.display_fps = facts.display_hz or None
        cfg, cfg_note = self.config()
        if cfg_note is not None and not self.config_notice_sent:
            self.config_notice_sent = True
            self.emit(Notice(cfg_note, None, self.sid))
        profile, profile_note = self.pick_profile(cfg, facts)
        self.profile_id = profile.id
        reason = decide.bypass_reason(facts, vp, profile)
        if reason is not None:
            if reason is not self.bypass:  # once per verdict, not on every re-apply
                self.log.info(
                    "%s: %s × %s at %s fps (%s) plays unsmoothed: %s",
                    self.title,
                    facts.width,
                    facts.height,
                    decide.fmt_rate(facts.fps),
                    facts.hdr_class.value,
                    reason.value,
                )
            await self.bypass_with(reason)
            return
        if profile.target.kind in DISPLAY_KINDS and not facts.display_hz:
            return  # PENDING: "waiting for the video window" (§4.2 step 2)

        paths = self.ctx.paths
        model_dir = paths.rpm_data_dir / MODEL_SUBDIR
        user_dirs = (paths.data_dir / USER_MODEL_SUBDIR,)
        trt_inst, trt_model = self.trt_setup(cfg, profile)
        installed = decide.installed_backends(
            paths.rpm_plugin_dir, model_dir, user_dirs, trt=trt_model is not None
        )
        wanted: BackendId | None
        pinned_note: Msg | None
        if self.backend_override is not None:
            wanted, pinned_note = self.backend_override, None
        else:
            wanted, pinned_note = self.wanted_backend(profile, cfg)
        notes = [n for n in (profile_note, pinned_note) if n is not None]
        # §11.9: candidate engines in preference order; the target decides among them
        order: list[BackendId]
        if wanted is None:
            order = [b for b in LIVE_RANKING if b in installed]
        else:
            choice = decide.resolve_backend(wanted, installed)
            if choice.notice is not None:
                notes.append(choice.notice)
            order = [choice.backend] if choice.backend is not None else []
            # fallbacks, used only if the chosen engine can't reach the target
            if choice.backend is BackendId.RIFE_TRT and BackendId.RIFE_NCNN in installed:
                order.append(BackendId.RIFE_NCNN)
            if choice.backend in decide.GPU_BACKENDS and BackendId.MVTOOLS in installed:
                order.append(BackendId.MVTOOLS)
        model: str | None = None
        model_path: Path | None = None
        model_note: Msg | None = None
        if BackendId.RIFE_NCNN in order:
            mc = decide.find_model(profile.model, model_dir, user_dirs)
            model, model_path, model_note = mc.name, mc.path, mc.notice
            if model is None:
                order.remove(BackendId.RIFE_NCNN)
                if model_note is not None and (wanted is not None or not order):
                    notes.append(model_note)
        if not order:
            msg = notes[-1] if notes else Msg("No interpolation engine is installed.")
            await self.set_off(FilterState.OFF, msg)
            self.emit(Notice(msg, ErrorCode.PKG_MISSING, self.sid))
            return

        def model_for(b: BackendId) -> str | None:
            if b is BackendId.RIFE_NCNN:
                return model
            return trt_model if b is BackendId.RIFE_TRT else None

        async def engine_options(
            f: SourceFacts, cands: Sequence[BackendId]
        ) -> list[decide.EngineOption]:
            return [
                decide.EngineOption(b, await self.sustainable_fps(b, model_for(b), f, cfg))
                for b in cands
            ]

        def choose(opts: Sequence[decide.EngineOption], *, forced: bool) -> decide.EnginePick:
            return decide.pick_engine(
                opts,
                facts.fps,
                facts.display_hz or None,
                profile.target.kind,
                profile.target.fps,
                auto=wanted is None,
                forced=forced,
            )

        full_size = cfg is not None and cfg.general.full_size

        async def decide_with(
            cands: Sequence[BackendId],
        ) -> tuple[decide.EnginePick, tuple[int, int] | None, bool, list[Msg], bool]:
            """(pick, smaller size or None, forced past the speed test, notes,
            MVTools in block mode)"""
            extra: list[Msg] = []
            options = await engine_options(facts, cands)
            pick = choose(options, forced=False)
            size: tuple[int, int] | None = None
            forced_used = False
            if (
                pick.target.bypass is BypassReason.NO_REALTIME
                and BackendId.RIFE_TRT in cands
                and facts.width * facts.height >= decide.HALF_FLOW_MIN_PIXELS
            ):
                # TensorRT with half-resolution flow (M0(q)) before a smaller picture
                half = [
                    dataclasses.replace(
                        o,
                        sustainable_fps=o.sustainable_fps * decide.HALF_FLOW_GAIN
                        if o.sustainable_fps is not None
                        else None,
                    )
                    for o in options
                    if o.backend is BackendId.RIFE_TRT
                ]
                hp = choose(half, forced=False)
                if hp.backend is BackendId.RIFE_TRT and hp.target.bypass is None:
                    self.flow_scale = 0.5
                    return hp, None, False, [decide.half_flow_notice()], False
            if pick.target.bypass is BypassReason.NO_REALTIME and full_size:
                # the user chose the video's own size over keeping up: no smaller
                # size and no block mode, smooth uncapped (frames may drop)
                full = choose(options, forced=True)
                if full.backend is not None and full.target.bypass is None:
                    full = dataclasses.replace(full, notice=decide.full_size_notice())
                    return full, None, True, [], False
            if pick.target.bypass is BypassReason.NO_REALTIME:
                # too demanding at its own size: smooth at a smaller size (§11.9)
                sized = [
                    (
                        (w, h),
                        [
                            dataclasses.replace(
                                o,
                                sustainable_fps=decide.shrink_cap(
                                    o.sustainable_fps, facts.width, facts.height
                                ),
                            )
                            for o in await engine_options(
                                dataclasses.replace(facts, width=w, height=h), cands
                            )
                        ],
                    )
                    for w, h in decide.smaller_sizes(facts.width, facts.height)
                ]
                found = decide.pick_size(
                    sized,
                    facts.fps,
                    facts.display_hz or None,
                    profile.target.kind,
                    profile.target.fps,
                    auto=wanted is None,
                    forced=self.forced,
                )
                if found is not None:
                    pick, size = found
                    forced_used = pick.notice == decide.forced_smaller_notice(size[1])
                    if not forced_used:
                        extra.append(decide.smaller_size_notice(size[1]))
                elif self.forced and not sized:
                    forced_pick = choose(options, forced=True)
                    if forced_pick.backend is not None:
                        pick, forced_used = forced_pick, True
                # a GPU engine at a smaller size wins; otherwise MVTools' block mode
                # at the video's own size beats MVTools at a smaller one (M0(p))
                gpu_smaller = found is not None and found[0].backend in decide.GPU_BACKENDS
                block = decide.blockfps_option(options) if not gpu_smaller else None
                if block is not None:
                    bp = choose([block], forced=False)
                    if bp.backend is BackendId.MVTOOLS and bp.target.bypass is None:
                        if bp.target.target is not None:
                            return bp, None, False, [decide.blockfps_notice()], True
            return pick, size, forced_used, extra, False

        self.flow_scale = 1.0
        pick, size, forced_used, extra, mv_block = await decide_with(order)
        trt_key: trt.EngineKey | None = None
        self.trt_waiting = None
        if pick.backend is BackendId.RIFE_TRT and trt_inst is not None and trt_model is not None:
            w, h = size or (facts.width, facts.height)
            trt_key = await self.trt_key(trt_inst, trt_model, w, h, cfg, flow_scale=self.flow_scale)
            ready = trt_key is not None and trt.is_ready(trt.engine_dir(paths, trt_key))
            if not ready:
                failed = self.engine_build_failed(trt_key)
                if trt_key is not None and failed is None:
                    self.start_engine_build(trt_inst, trt_key)
                    self.trt_waiting = trt_key
                rest = [b for b in order if b is not BackendId.RIFE_TRT]
                if rest:
                    pick, size, forced_used, extra, mv_block = await decide_with(rest)
                if failed is not None:
                    notes.append(failed)
                else:
                    notes.append(
                        decide.engine_building_notice(w, h, pick.backend if rest else None)
                    )
                trt_key = None
                if not rest:
                    msg = notes[-1]
                    await self.set_off(FilterState.OFF, msg)
                    self.notice = msg
                    return
        notes.extend(extra)
        if facts.hdr_class is HdrClass.HDR10:
            # first, so a fallback or a smaller size (more urgent) takes the notice
            notes.insert(0, Msg("HDR smoothing is experimental."))
        tc = pick.target
        if tc.bypass is not None:
            self.log.info(
                "%s: %s × %s at %s fps plays unsmoothed: %s",
                self.title,
                facts.width,
                facts.height,
                decide.fmt_rate(facts.fps),
                tc.bypass.value,
            )
            # ALREADY_AT_RATE / NO_REALTIME for a display target depend on the
            # display rate: remember it so a new rate re-decides (§4.11).
            depends = profile.target.kind in DISPLAY_KINDS and tc.bypass in (
                BypassReason.ALREADY_AT_RATE,
                BypassReason.NO_REALTIME,
            )
            await self.bypass_with(tc.bypass, display_hz=facts.display_hz if depends else None)
            if pick.notice is not None:
                self.notice = pick.notice
            return
        backend = pick.backend
        if tc.target is None or backend is None:
            return  # waiting for the display rate
        if backend is BackendId.RIFE_NCNN:
            if model_note is not None and model_note not in notes:
                notes.append(model_note)
        elif backend is BackendId.RIFE_TRT:
            model, model_path = trt_model, None
        else:
            model, model_path = None, None
        if pick.notice is not None:
            notes.append(pick.notice)
        if self.backend_override is not None and self.override_notice is not None:
            notes.append(self.override_notice)
        buffered, concurrent = default_frames(backend)
        if profile.buffered_frames:
            buffered = profile.buffered_frames
        if profile.concurrent_frames:
            concurrent = profile.concurrent_frames
        h = facts.height
        gen = self.next_gen()
        self.run_size = size
        self.run_flow = self.flow_scale if backend is BackendId.RIFE_TRT else 1.0
        self.run_block = bool(mv_block and backend is BackendId.MVTOOLS)
        params = FilterParams(
            gen=gen,
            backend=backend,
            src_fps=facts.fps,
            target_fps=tc.target,
            buffered_frames=buffered,
            concurrent_frames=concurrent,
            matrix=decide.matrix_name(vp.get("colormatrix"), h),
            range=decide.range_name(vp.get("colorlevels")),
            model_path=model_path if backend is BackendId.RIFE_NCNN else None,
            gpu_id=self.gpu_index(cfg) if backend is BackendId.RIFE_NCNN else None,
            gpu_thread=RIFE_GPU_THREAD_DEFAULT if backend is BackendId.RIFE_NCNN else None,
            uhd=decide.uhd_mode(*(size or (facts.width, facts.height))),
            sc_threshold=profile.sc_threshold if backend in decide.GPU_BACKENDS else None,
            title=self.title,
            size=size,
            mv_mode="block" if mv_block and backend is BackendId.MVTOOLS else None,
            trt=(
                trt.script_settings(
                    trt_inst,
                    trt_key,
                    trt.engine_dir(paths, trt_key),
                    streams=trt.streams_for(*(size or (facts.width, facts.height))),
                    build=False,
                )
                if backend is BackendId.RIFE_TRT and trt_inst is not None and trt_key is not None
                else None
            ),
        )
        self.log.info(
            "%s: smoothing %s → %s fps with %s%s at %s × %s (video %s × %s)%s",
            self.title,
            decide.fmt_rate(facts.fps),
            decide.fmt_rate(tc.target),
            decide.BACKEND_NAMES.get(backend, backend.value),
            f" {model}" if model else "",
            *(size or (facts.width, facts.height)),
            facts.width,
            facts.height,
            ", forced past the speed test" if forced_used else "",
        )
        if facts.hdr_class is HdrClass.HDR10:
            self.log.info("%s: HDR10 smoothed with the experimental HDR pass-through", self.title)
        write_script(self.script_path, ScriptConstants(paths.rpm_plugin_dir))
        self.failure.clear()
        self.device_lost = False
        self.busy = True
        if backend is BackendId.RIFE_NCNN:
            mark_rife_session(self.ctx.paths, (self.proc.pid,))  # BE-1030 window starts
        try:
            try:
                await self.ipc.command("vf", "add", vf_argument(self.script_path, params))
            except MpvError as exc:
                self.failure.append(f"vf add: {exc.error}")
            ok = not self.failure and await self.verify(gen, float(tc.target))
        finally:
            self.busy = False
        if backend is BackendId.RIFE_NCNN:
            self.rife_used = True
        if not ok:
            evidence = tuple(self.failure[:10])
            await self.rollback_failed(evidence)
            if raise_on_fail:
                raise FilterFailed(
                    ErrorCode.VF_ROLLED_BACK,
                    Msg("The smoothing filter failed to start and was removed."),
                    Msg("Try another profile, or check the System page."),
                    detail="\n".join(evidence) or None,
                    rolled_back=True,
                )
            return
        self.active_gen = gen
        self.backend, self.model = backend, model
        self.target, self.multiplier = tc.target, tc.target / facts.fps
        self.applied_display = facts.display_hz
        self.display_changed_at = None
        self.bypass_display = None
        self.filter = FilterState.ACTIVE if self.detector.flowing else FilterState.PENDING
        self.bypass = None
        self.notice = notes[0] if len(notes) == 1 else (notes[-1] if notes else None)
        self.health, self.health_code = Health.OK, None

    async def verify(self, gen: int, target_fps: float) -> bool:
        """The new gen is in ``vf``, no failure was logged, and (unless paused)
        frames flow within the verify window."""
        try:
            vf = await self.ipc.get("vf")
        except IpcClosed, MpvError, TimeoutError:
            return False
        if vf is not None:
            self.props["vf"] = vf
        if entry_gen(filter_entry(vf)) != gen:
            self.failure.append("the vf property does not show the new filter generation")
            return False
        loop = asyncio.get_running_loop()
        self.detector.filter_started(time.monotonic(), target_fps)
        deadline = loop.time() + self.opts.verify_timeout_s
        paused_since: float | None = None
        while loop.time() < deadline:
            if self.failure:
                return False
            if self.ipc.closed.is_set():
                return False
            verdict = self.detector.update(sample_from(self.props, time.monotonic()))
            if verdict.flowing:
                return True
            if self.props.get("pause") is True:
                paused_since = paused_since or loop.time()
                if loop.time() - paused_since >= 1.0:
                    return True
            await asyncio.sleep(0.05)
        return not self.failure

    async def rollback_failed(self, evidence: tuple[str, ...]) -> None:
        """Init/eval failure or device loss: remove @buttereye, report (F4)."""
        await self.remove_filter()
        self.want = False
        self.filter = FilterState.ROLLED_BACK
        self.bypass = None
        if self.device_lost:
            self.health, self.health_code = Health.DEVICE_LOST, ErrorCode.DEVICE_LOST
            reason = Msg("The GPU device was lost; interpolation was turned off.")
            self.notice = reason
            self.emit(
                HealthChanged(
                    self.sid,
                    Health.DEVICE_LOST,
                    ErrorCode.DEVICE_LOST,
                    reason,
                    evidence,
                    Msg("Interpolation was turned off."),
                )  # fmt: skip
            )
        else:
            self.health_code = ErrorCode.VF_ROLLED_BACK
            self.notice = Msg("The smoothing filter failed and was removed; playback continues.")
            self.emit(Notice(self.notice, ErrorCode.VF_ROLLED_BACK, self.sid))
        self.after_failure()

    async def stalled(self, verdict: Verdict) -> None:
        """STALLED: roll back automatically (R12). A RIFE-ncnn filter is replaced by
        MVTools once (§11.9); otherwise interpolation stays off (never re-enabled)."""
        evidence = verdict.evidence + device_lost_lines(self.log_path)
        if self.device_lost or len(evidence) > len(verdict.evidence):
            health, code = Health.DEVICE_LOST, ErrorCode.DEVICE_LOST
        else:
            health, code = Health.STALLED, ErrorCode.FILTER_STALLED
        reason = verdict.reason or Msg("Video stalled while audio kept playing.")
        if await self.switch_to_cpu(health, code, reason, evidence):
            return
        await self.remove_filter()
        self.want = False
        self.filter = FilterState.ROLLED_BACK
        self.health, self.health_code = health, code
        self.notice = Msg("Video stalled while audio kept playing; interpolation was turned off.")
        self.emit(
            HealthChanged(
                self.sid,
                self.health,
                self.health_code,
                reason,
                evidence,
                Msg("Interpolation was turned off."),
            )  # fmt: skip
        )
        self.after_failure()

    def can_switch_to_cpu(self) -> bool:
        """A running RIFE-ncnn filter that may still be replaced by MVTools (once)."""
        if self.backend is not BackendId.RIFE_NCNN or self.backend_override is not None:
            return False
        paths = self.ctx.paths
        installed = decide.installed_backends(
            paths.rpm_plugin_dir,
            paths.rpm_data_dir / MODEL_SUBDIR,
            (paths.data_dir / USER_MODEL_SUBDIR,),
        )
        return BackendId.MVTOOLS in installed

    async def switch_to_cpu(
        self,
        health: Health,
        code: ErrorCode,
        reason: Msg,
        evidence: tuple[str, ...],
        *,
        scan_faults: bool = True,
    ) -> bool:
        """§11.9: RIFE-ncnn stalled, lost its device or faulted the GPU → this
        session switches to MVTools once (an in-session engine override that is
        never lifted). False when that is not possible (then the caller turns
        interpolation off). Health returns to OK when MVTools frames flow; if
        MVTools can't reach a target the video plays unsmoothed (NO_REALTIME)."""
        if not self.can_switch_to_cpu():
            return False
        await self.remove_filter()
        self.backend_override = BackendId.MVTOOLS
        self.override_notice = decide.switched_to_cpu_notice()
        self.health, self.health_code = health, code
        self.emit(
            HealthChanged(
                self.sid,
                health,
                code,
                reason,
                evidence,
                Msg("ButterEye switched this video to CPU smoothing."),
            )  # fmt: skip
        )
        if scan_faults:
            self.after_failure()  # the Xid check for the RIFE run (F1)
        self.backend = self.model = None
        self.failure.clear()
        self.device_lost = False
        self.want = True
        self.filter = FilterState.PENDING
        self.notice = self.override_notice
        await self.apply(raise_on_fail=False)
        if self.active_gen is not None:
            self.emit(
                HealthChanged(
                    self.sid,
                    Health.OK,
                    None,
                    Msg("Frames are flowing again with CPU smoothing."),
                    (),
                    None,
                )  # fmt: skip
            )
        return True

    def after_failure(self) -> None:
        if self.rife_used:
            with contextlib.suppress(ButterEyeError):
                self.ctx.spawn(self.check_gpu_faults(), name=f"mpvctl-{self.sid}-xid")

    async def check_gpu_faults(self) -> None:
        """Xid lines newer than the last reported one (F1); unknown is reported, never OK.

        A per-session cursor starts at the session start and moves past every
        reported fault, so a later failure or the session end never reports the
        same kernel lines again."""
        async with self.fault_lock:
            scan_wall, scan_mono = time.time(), time.monotonic()
            res = await gpu_fault_scan(
                since_monotonic=self.fault_since_mono, since_wall=self.fault_since_wall
            )
            if res is None:
                self.emit(
                    Notice(
                        Msg(
                            "GPU faults can't be checked in this build; whether RIFE faulted "
                            "is unknown."
                        ),  # fmt: skip
                        None,
                        self.sid,
                    )
                )
                return
            if not res.lines:
                return
            self.fault_since_wall = (
                res.newest_wall + 1e-6 if res.newest_wall is not None else scan_wall
            )
            self.fault_since_mono = (
                res.newest_monotonic + 1e-6 if res.newest_monotonic is not None else scan_mono
            )
            lines = res.lines
        reason = Msg("The GPU reported a fault in RIFE-ncnn.")
        async with self.lock:
            if self.active_gen is not None and self.backend is not BackendId.RIFE_NCNN:
                # the fault came from an earlier RIFE run (e.g. before the switch
                # to CPU smoothing); the engine running now is not to blame
                self.emit(
                    Notice(
                        Msg(
                            "The GPU reported a fault while RIFE-ncnn was running earlier; "
                            "{engine} is running now.",
                            {
                                "engine": decide.BACKEND_NAMES.get(self.backend, "")
                                if self.backend
                                else ""
                            },
                        ),  # fmt: skip
                        ErrorCode.RIFE_GPU_FAULT,
                        self.sid,
                    )
                )
                return
            if self.active_gen is not None and await self.switch_to_cpu(
                Health.GPU_FAULT,
                ErrorCode.RIFE_GPU_FAULT,
                reason,
                tuple(lines[:10]),
                scan_faults=False,
            ):
                if not self.ended:
                    self.publish()
                    await self.send_status()
                return
            self.health, self.health_code = Health.GPU_FAULT, ErrorCode.RIFE_GPU_FAULT
            auto = None
            if self.active_gen is not None:
                await self.remove_filter()
                self.want = False
                self.filter = FilterState.ROLLED_BACK
                auto = Msg("Interpolation was turned off.")
            self.emit(
                HealthChanged(
                    self.sid,
                    Health.GPU_FAULT,
                    ErrorCode.RIFE_GPU_FAULT,
                    reason,
                    tuple(lines[:10]),
                    auto,
                )  # fmt: skip
            )
            if not self.ended:
                self.publish()
                await self.send_status()

    # ---- controller ----
    async def run(self) -> None:
        cancelled = False
        try:
            while not self.ended:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), self.opts.tick_s)
                self.wake.clear()
                if self.ipc.closed.is_set():
                    break
                await self.probe()  # outside the lock: detach never queues behind it
                async with self.lock:
                    if self.ended:
                        break
                    try:
                        await self.step()
                    except IpcClosed:
                        break
                    except TimeoutError:
                        self.probe_timeouts += 1
                        self.log.warning("session %s: mpv is not answering", self.sid)
                    except ButterEyeError as exc:
                        self.log.warning("session %s: %s", self.sid, exc)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled and not self.ended:
                await self.ended_by_mpv()

    # ---- liveness (a SIGSTOPped or hung mpv) ----
    async def probe(self) -> None:
        """Ask mpv for its pid after ``probe_after_s`` of IPC silence; a playing mpv
        is never silent that long (property changes), a paused one is."""
        silent = time.monotonic() - self.ipc.last_rx_mono
        if silent < self.opts.probe_after_s:
            self.probe_timeouts = 0
            return
        try:
            await self.ipc.get("pid", timeout_s=self.opts.probe_timeout_s)
        except TimeoutError:
            self.probe_timeouts += 1
        except IpcClosed:
            return
        except MpvError:
            self.probe_timeouts = 0
        else:
            self.probe_timeouts = 0

    def check_liveness(self, now: float) -> None:
        silent = now - self.ipc.last_rx_mono
        if not self.unresponsive:
            if self.probe_timeouts >= 1 and silent >= self.opts.unresponsive_after_s:
                self.unresponsive = True
                if self.health in (Health.OK, Health.DROPPING):
                    self.health_before_unresponsive = (self.health, self.health_code)
                    self.health, self.health_code = Health.CONNECTION_LOST, ErrorCode.IPC_LOST
                    self.step_down_to = None
                    self.emit(
                        HealthChanged(
                            self.sid, Health.CONNECTION_LOST, ErrorCode.IPC_LOST,
                            Msg("mpv is not responding."),
                            (f"no reply from mpv for {silent:.1f} s",), None,
                        )
                    )  # fmt: skip
                self.log.warning("session %s: mpv is not responding (%.1f s)", self.sid, silent)
        elif silent < self.opts.probe_after_s and self.probe_timeouts == 0:
            self.unresponsive = False
            self.detector.note_restart(now)  # the frozen gap is not a stall
            before, self.health_before_unresponsive = self.health_before_unresponsive, None
            if before is not None and self.health is Health.CONNECTION_LOST:
                self.health, self.health_code = Health.OK, None
                self.emit(
                    HealthChanged(self.sid, Health.OK, None, Msg("mpv is responding again."),
                                  (), None)
                )  # fmt: skip

    # ---- another file in the same mpv ----
    def file_changed(self) -> bool:
        """Safety net: the loaded file no longer matches the facts the filter was
        built for (path, rate, size, transfer)."""
        src = self.source
        vp = self.props.get("video-params")
        if src is None or not isinstance(vp, dict):
            return False
        path = self.props.get("path")
        if isinstance(path, str) and path != src.path:
            return True
        fps = decide.fps_fraction(self.props.get("container-fps"))
        if fps is not None and fps != src.fps:
            return True
        w, h = vp.get("w"), vp.get("h")
        if isinstance(w, int) and isinstance(h, int) and (w, h) != (src.width, src.height):
            return True
        return decide.hdr_class(vp) is not src.hdr_class

    async def begin_new_file(self) -> None:
        """Forget everything decided for the previous file and decide again (§4.11)."""
        self.new_file = False
        self.display_changed_at = None
        if (
            self.active_gen is not None
            or filter_entry(self.props.get("vf")) is not None
            or self.remove_pending
        ):
            await self.remove_filter()  # never run the old filter on the new file
        self.detector.filter_stopped()
        self.source = None
        self.target = self.multiplier = None
        self.backend, self.model = None, None
        self.profile_id = None
        self.applied_display = None
        self.bypass_display = None
        self.step_down_to = None
        self.drop_rate = None
        self.start_notice = None
        self.file_started_mono = time.monotonic()
        if self.health is Health.DROPPING:
            self.health, self.health_code = Health.OK, None
        if self.want:
            self.filter, self.bypass, self.notice = FilterState.PENDING, None, None
        elif self.filter is FilterState.BYPASSED:
            self.filter, self.bypass = FilterState.PENDING, None

    def _needs_fetch(self, key: str) -> bool:
        have = self.props.get(key)
        if key == "video-params" and isinstance(have, dict):
            return not decide.params_ready(have)
        return key not in self.props

    async def refetch_file_props(self) -> None:
        """Read the new file's properties once it is loaded: mpv may not notify a
        value that equals the previous file's. Values an event delivered since
        ``start-file`` are newer and kept."""
        if self.file_loaded_at is None:
            return
        for key in FILE_PROPS:
            if not self._needs_fetch(key):
                continue
            with contextlib.suppress(MpvError):
                value = await self.ipc.get(key, timeout_s=1.0)
                # an event may have delivered a newer value meanwhile
                if value is not None and self._needs_fetch(key):
                    self.props[key] = value
        vp = self.props.get("video-params")
        if isinstance(vp, dict) and decide.params_ready(vp):
            self.refetch_props = False
        elif time.monotonic() - self.file_loaded_at > self.opts.no_video_after_s + 2.0:
            self.refetch_props = False  # no video track: apply() decides NO_VIDEO
        path = self.props.get("path")
        if isinstance(path, str) and path:
            self.title = Path(path).name

    def check_start_deadline(self, now: float) -> None:
        """No video after ``start_deadline_s``: say so instead of "Starting…"."""
        waiting = self.filter is FilterState.PENDING and self.active_gen is None
        nothing = self.file_loaded_at is None or not isinstance(
            self.props.get("playback-time"), (int, float)
        )
        if self.start_notice is not None:
            if not waiting or not nothing:
                if self.notice is self.start_notice:
                    self.notice = None
                self.start_notice = None
            return
        if waiting and nothing and now - self.file_started_mono >= self.opts.start_deadline_s:
            self.start_notice = Msg(
                "mpv hasn't opened the video yet. It may be stuck on a script, the file "
                "or the video output."
            )
            self.notice = self.start_notice
            self.emit(Notice(self.start_notice, None, self.sid))

    async def step(self) -> None:
        now = time.monotonic()
        self.check_liveness(now)
        if self.unresponsive:
            # stale properties say nothing; only a stall of our own filter is judged
            if self.active_gen is not None:
                verdict = self.detector.update(sample_from(self.props, now))
                if verdict.health is Health.STALLED:
                    await self.stalled(verdict)
            self.publish()
            return

        if now - self.log_checked_at >= self.opts.log_check_s:
            self.log_checked_at = now
            launcher.cap_log(self.log_path)

        if self.toggle_requested:
            self.toggle_requested = False
            with contextlib.suppress(IpcClosed, MpvError, TimeoutError):
                await self.ipc.command("script-message-to", HELPER_NAME, "ack", "toggle")
            if self.active_gen is not None:
                self.want = False
                await self.set_off(FilterState.OFF)
            else:
                self.reset_health()
                self.want = True
                self.filter = FilterState.PENDING
                await self.apply(raise_on_fail=False)

        if self.trt_built and not self.busy:
            self.trt_built = False
            waiting = self.trt_waiting
            if (
                waiting is not None
                and self.want
                and trt.is_ready(trt.engine_dir(self.ctx.paths, waiting))
            ):
                self.log.info("%s: TensorRT engine ready; switching to it", self.title)
                await self.apply(raise_on_fail=False)  # hot reload (§4.11)
        if not self.busy and (
            self.new_file or (self.active_gen is not None and self.file_changed())
        ):
            await self.begin_new_file()
        if self.refetch_props:
            await self.refetch_file_props()

        if self.remove_pending and self.active_gen is None:
            await self.remove_filter(timeout_s=1.0)

        if self.active_gen is not None and self.failure:
            evidence = tuple(self.failure[:10])
            if not (
                self.device_lost
                and await self.switch_to_cpu(
                    Health.DEVICE_LOST,
                    ErrorCode.DEVICE_LOST,
                    Msg("The GPU device was lost."),
                    evidence,
                )
            ):
                await self.rollback_failed(evidence)

        # someone else removed or disabled @buttereye (e.g. the helper without us)
        if self.active_gen is not None and not self.busy:
            entry = filter_entry(self.props.get("vf"))
            if entry is None or entry.get("enabled") is False:
                self.active_gen = None
                self.detector.filter_stopped()
                self.want = False
                self.filter = FilterState.OFF
                self.notice = Msg("Interpolation was turned off in mpv.")

        if (
            self.filter is FilterState.BYPASSED
            and self.bypass is BypassReason.NO_VIDEO
            and isinstance(self.props.get("video-params"), dict)
            and not decide.no_video(self.props)
        ):
            self.filter, self.bypass = FilterState.PENDING, None  # the video track arrived late
        if self.want and self.active_gen is None and self.filter is FilterState.PENDING:
            await self.apply(raise_on_fail=False)
        if self.source is None and self.filter is not FilterState.PENDING:
            # smoothing is off for this file; still show its facts
            self.source = decide.source_facts(
                self.props, path=str(self.props.get("path") or self.file)
            )

        disp = self.props.get("display-fps")
        if isinstance(disp, (int, float)) and disp > 0:
            if self.display_fps is None or abs(disp - self.display_fps) > 0.01:
                self.display_fps = float(disp)
            if self.active_gen is not None and self.applied_display is not None:
                decided_at: float | None = self.applied_display
            elif (
                self.want
                and self.filter is FilterState.BYPASSED
                and self.bypass_display is not None
            ):
                decided_at = self.bypass_display  # ALREADY_AT_RATE / NO_REALTIME
            else:
                decided_at = None
            if decided_at is not None and abs(disp - decided_at) > 0.5:
                if self.display_changed_at is None:
                    self.display_changed_at = now
                elif now - self.display_changed_at >= self.opts.display_debounce_s:
                    self.display_changed_at = None
                    if self.active_gen is None:
                        self.filter, self.bypass, self.bypass_display = (
                            FilterState.PENDING, None, None,
                        )  # fmt: skip
                    await self.apply(raise_on_fail=False)  # recompute + hot reload (§4.11)
            else:
                self.display_changed_at = None

        if self.active_gen is not None:
            verdict = self.detector.update(sample_from(self.props, now))
            self.drop_rate = verdict.drop_rate
            if verdict.flowing and self.filter is FilterState.PENDING:
                self.filter = FilterState.ACTIVE
            if verdict.health is Health.STALLED:
                await self.stalled(verdict)
            elif verdict.health is Health.DROPPING:
                if self.health is not Health.DROPPING:
                    self.health, self.health_code = Health.DROPPING, None
                    self.step_down_to = await self.step_down_choice()
                    self.emit(
                        HealthChanged(self.sid, Health.DROPPING, None,
                                      verdict.reason or Msg("Dropping frames."), (), None)
                    )  # fmt: skip
            elif self.health is Health.DROPPING:
                self.health, self.step_down_to = Health.OK, None
                self.emit(
                    HealthChanged(self.sid, Health.OK, None, Msg("Frames are on time again."),
                                  (), None)
                )  # fmt: skip
        self.check_start_deadline(now)
        self.publish()
        await self.send_status()

    def reset_health(self) -> None:
        """The user turned interpolation on again: clear the previous failure."""
        if self.health is not Health.OK or self.health_code is not None:
            self.health, self.health_code = Health.OK, None
        self.notice = None
        self.failure.clear()
        self.device_lost = False

    async def ended_by_mpv(self) -> None:
        returncode = self.proc.returncode
        if returncode is None:
            with contextlib.suppress(TimeoutError):
                returncode = await asyncio.wait_for(self.proc.wait(), 1.0)
        if self.rife_used:
            await self.check_gpu_faults()
        if returncode is not None:
            # mpv is gone: its socket and script are leftovers (discover would re-probe them)
            launcher.remove_runtime_files(self.socket.parent, self.sid)
        if returncode is None:
            self.health, self.health_code = Health.CONNECTION_LOST, ErrorCode.IPC_LOST
            self.emit(
                HealthChanged(self.sid, Health.CONNECTION_LOST, ErrorCode.IPC_LOST,
                              Msg("Lost the connection to mpv."), (), None)
            )  # fmt: skip
            await self.end("connection_lost")
        else:
            await self.end("mpv_exited")

    async def end(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        self.active_gen = None
        self.detector.filter_stopped()
        with contextlib.suppress(Exception):
            await self.ipc.close()
        self.ctx.forget_coalesced(self.sid)
        snap = self.snapshot()
        self.mark_published(snap)
        self.emit(SessionChanged(snap))
        if reason == "detached":
            self.emit(SessionEnded(self.sid, "detached"))
        elif reason == "connection_lost":
            self.emit(SessionEnded(self.sid, "connection_lost"))
        else:
            self.emit(SessionEnded(self.sid, "mpv_exited"))
        self.ctx.forget_coalesced(self.sid)
        # Ended sessions leave the registry (sessions() lists live players only).
        # Safe for a detached mpv: nothing ButterEye holds can kill it (launcher).
        reg = registry(self.ctx)
        if reg.sessions.get(self.sid) is self:
            del reg.sessions[self.sid]


# ---------------------------------------------------------------------------
# Providers (exact §11.4 signatures)
# ---------------------------------------------------------------------------


def _get(ctx: ProviderContext, sid: SessionId) -> _Session:
    s = registry(ctx).sessions.get(sid)
    if s is None or s.ended:
        raise _lost(sid)
    return s


def _answering(s: _Session) -> _Session:
    """Refuse a filter change while mpv is not responding (it would only time out)."""
    if s.unresponsive:
        raise ButterEyeError(
            ErrorCode.IPC_LOST,
            Msg("mpv is not responding."),
            Msg("Wait until the player responds again, or close it."),
            detail=str(s.sid),
        )
    return s


def _profile_known(ctx: ProviderContext, profile_id: str | None) -> None:
    if profile_id is None:
        return
    try:
        cfg = ctx.config()
    except NotAvailable:
        return
    if all(p.id != profile_id for p in cfg.profiles):
        raise ButterEyeError(
            ErrorCode.CONFIG_VALUE,
            Msg("There is no profile called {profile}.", {"profile": profile_id}),
            Msg("Pick a profile from the list on the Profiles page."),
        )


async def play(
    ctx: ProviderContext, file: Path, *, profile_id: str | None, op: OpContext
) -> SessionId:
    """Launch mpv on ``file``; returns once IPC is up and ``SessionAdded`` was emitted."""
    reg = registry(ctx)
    opts = reg.options
    _profile_known(ctx, profile_id)
    file = Path(file)
    if not file.exists():
        raise ButterEyeError(
            ErrorCode.FILE_NOT_LOCAL,
            Msg("{file} was not found.", {"file": file.name}),
            Msg("Choose a local video file."),
        )
    op.report(Msg("Starting mpv"))
    paths = ctx.paths
    runtime = launcher.ensure_private_dir(paths.runtime_dir)
    launcher.write_include(paths.mpv_include)
    sid = _new_sid()
    socket = runtime / f"mpv-{sid}.sock"
    script = runtime / f"mpv-{sid}.vpy"
    log_path = paths.logs_dir / f"mpv-{sid}.log"
    try:
        upscaling = ctx.config().general.upscaling
    except ButterEyeError:
        upscaling = "standard"
    shader = shaders.shader_for(upscaling)
    argv = launcher.build_argv(
        opts.mpv,
        include=paths.mpv_include,
        socket=socket,
        file=file,
        extra=opts.extra_args,
        shaders=(shader,) if shader is not None else (),
    )
    launcher.prune_logs(paths.logs_dir, runtime)
    proc = await launcher.spawn_mpv(argv, log_path, env=opts.env)
    try:
        op.report(Msg("Connecting to mpv"))
        ipc = await launcher.connect(socket, proc, log_path, timeout_s=opts.connect_timeout_s)
        session = _Session(
            ctx, opts, sid=sid, file=file, proc=proc, ipc=ipc, socket=socket,
            log_path=log_path, script_path=script, profile_request=profile_id,
        )  # fmt: skip
        session.shader = shader  # added at launch
        ipc.add_handler(session.on_event)
        ipc.start(lambda coro, name: ctx.spawn(coro, name=f"mpvctl-{sid}-ipc"))
        await ipc.command("client_name")
        await ipc.command("request_log_messages", "warn")
        for prop in sorted(OBSERVABLE):
            await ipc.observe(prop)
    except BaseException:
        with contextlib.suppress(Exception):
            await asyncio.shield(launcher.terminate(proc, grace_s=2.0))
        launcher.remove_runtime_files(runtime, sid)
        raise
    reg.sessions[sid] = session
    snap = session.snapshot()
    session.mark_published(snap)
    ctx.emit(SessionAdded(snap))
    session.task = ctx.spawn(session.run(), name=f"mpvctl-{sid}")
    return sid


async def sessions(ctx: ProviderContext) -> tuple[SessionSnapshot, ...]:
    """Live players only; an ended session was announced by ``SessionEnded``."""
    return tuple(s.snapshot() for s in registry(ctx).sessions.values() if not s.ended)


async def set_interpolation(
    ctx: ProviderContext, sid: SessionId, enabled: bool, *, force: bool = False
) -> SessionSnapshot:
    """``force``: smooth even what the speed test says can't keep up (NO_REALTIME
    runs uncapped) for the rest of this session."""
    s = _answering(_get(ctx, sid))
    async with s.lock:
        if enabled:
            if force and not s.forced:
                s.forced = True
                s.log.info("%s: smooth anyway (past the speed test) was asked for", s.title)
            s.reset_health()
            s.want = True
            s.filter = FilterState.PENDING
            await s.apply(raise_on_fail=True)
        else:
            s.want = False
            await s.set_off(FilterState.OFF)
        snap = s.publish()
        await s.send_status()
        return snap


async def detach(ctx: ProviderContext, sid: SessionId, policy: DetachPolicy) -> None:
    """Close IPC only (mpv keeps playing); DISABLE_FILTER removes @buttereye first.

    Bounded even for a hung mpv: the controller is cancelled when it holds the
    session lock for more than a second, an mpv known to be unresponsive is not
    asked anything, and the session always ends."""
    s = _get(ctx, sid)
    task = s.task
    locked = False
    try:
        try:
            await asyncio.wait_for(s.lock.acquire(), 1.0)
            locked = True
        except TimeoutError:
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(task), 1.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(s.lock.acquire(), 1.0)
                locked = True
        if not s.unresponsive and not s.ipc.closed.is_set():
            if policy is DetachPolicy.DISABLE_FILTER:
                await s.remove_filter(timeout_s=3.0)
            with contextlib.suppress(IpcClosed, MpvError, TimeoutError):
                on = filter_entry(s.props.get("vf")) is not None
                text = render(
                    Msg("ButterEye disconnected; smoothing stays on (Alt+b toggles it).")
                    if on and policy is DetachPolicy.KEEP_FILTER
                    else Msg("ButterEye disconnected.")
                )
                await s.ipc.command("script-message-to", HELPER_NAME, "status", text, timeout_s=1.0)
    finally:
        try:
            await s.end("detached")
        finally:
            if locked:
                s.lock.release()
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), 1.0)


async def apply_profile(
    ctx: ProviderContext, sid: SessionId, profile_id: str | None
) -> SessionSnapshot:
    """Switch profile (None = rules) with a verified new gen (F4)."""
    _profile_known(ctx, profile_id)
    s = _answering(_get(ctx, sid))
    async with s.lock:
        s.profile_request = profile_id
        s.reset_health()
        s.want = True
        if s.active_gen is None:
            s.filter = FilterState.PENDING
        await s.apply(raise_on_fail=True)
        snap = s.publish()
        await s.send_status()
        return snap


async def step_down(ctx: ProviderContext, sid: SessionId) -> SessionSnapshot:
    """§5.3 step 5: the offered lighter profile."""
    s = _get(ctx, sid)
    target = s.step_down_to
    if target is None:
        target = await s.step_down_choice()
    if target is None:
        raise ButterEyeError(
            ErrorCode.CONFIG_VALUE,
            Msg("There is no lighter profile to step down to."),
            Msg("Turn interpolation off, or pick a profile on the Profiles page."),
        )
    return await apply_profile(ctx, sid, target)


def _refusal(reason: RefusalReason) -> Msg:
    return Msg(_REFUSAL_TEXT.get(reason, "Refused: {reason}"), {"reason": reason.value})


async def discover(ctx: ProviderContext) -> tuple[InstanceCandidate, ...]:
    """ButterEye-launched instances in the runtime dir that this core does not hold."""
    reg = registry(ctx)
    runtime = ctx.paths.runtime_dir
    if not runtime.is_dir():
        return ()
    held = {s.socket for s in reg.sessions.values() if not s.ended}
    found: list[InstanceCandidate] = []
    for path in sorted(runtime.glob("mpv-*.sock")):
        if path in held:
            continue
        cid = CandidateId(path.name)
        try:
            reason = check_socket_path(path)
        except FileNotFoundError:
            continue
        if reason is not None:
            found.append(InstanceCandidate(cid, path, None, None, reason, _refusal(reason),
                                           False, True))  # fmt: skip
            continue
        try:
            ipc = await MpvIpc.connect(path, timeout_s=0.5)
        except SocketRefused as exc:
            found.append(InstanceCandidate(cid, path, None, None, exc.reason,
                                           _refusal(exc.reason), False, True))  # fmt: skip
            continue
        except ConnectionRefusedError, FileNotFoundError:
            _remove_stale(path)  # nobody listens: its mpv is gone (crashed or killed)
            continue
        except OSError, TimeoutError:
            continue
        try:
            ipc.start()
            pid = await ipc.get("pid", timeout_s=1.0)
            title = await ipc.get("media-title", timeout_s=1.0)
            vf = await ipc.get("vf", timeout_s=1.0)
        except IpcClosed, MpvError, TimeoutError:
            continue
        finally:
            await ipc.close()
        found.append(
            InstanceCandidate(
                id=cid,
                socket=path,
                pid=pid if isinstance(pid, int) else ipc.peer_pid,
                title=Path(title).name if isinstance(title, str) else None,
                refused=None,
                refusal=None,
                orphan_filter=filter_entry(vf) is not None,
                managed_by_us=True,
            )
        )
    result = tuple(found)
    reachable = tuple(c for c in result if c.refused is None)
    if reachable:
        ctx.emit(OrphansFound(reachable))
    return result


def _remove_stale(sock: Path) -> None:
    """Delete a dead ``mpv-<sid>.sock`` in our 0700 runtime dir and its ``.vpy``."""
    name = sock.name
    if not (name.startswith("mpv-") and name.endswith(".sock")):
        return
    sid = name[len("mpv-") : -len(".sock")]
    launcher.remove_runtime_files(sock.parent, sid)


async def close(ctx: ProviderContext, policy: DetachPolicy, *, timeout_s: float) -> ShutdownReport:
    """Detach every live session (mpv keeps playing, §4.10)."""
    live = [s for s in registry(ctx).sessions.values() if not s.ended]

    async def one(s: _Session) -> tuple[SessionId, ErrorCode | None]:
        try:
            await asyncio.wait_for(detach(ctx, s.sid, policy), timeout_s)
        except ButterEyeError as exc:
            return s.sid, exc.code
        except Exception:
            return s.sid, ErrorCode.IPC_LOST
        return s.sid, None

    results = await asyncio.gather(*(one(s) for s in live))
    detached = tuple(sid for sid, code in results if code is None)
    failed = tuple((sid, code) for sid, code in results if code is not None)
    return ShutdownReport(detached, failed, ())


__all__ = [
    "REGISTRY_KEY",
    "FALLBACK_PROFILE",
    "BYPASS_TEXT",
    "LiveOptions",
    "Registry",
    "registry",
    "play",
    "sessions",
    "set_interpolation",
    "detach",
    "apply_profile",
    "step_down",
    "discover",
    "close",
]
