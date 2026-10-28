# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live-play provider (GUI.md §11.4; SCOPE §4.2, §4.3, §4.10, §4.11, F2-F4, F7, F8).

M1 subset: ``play`` launches the user's mpv with ButterEye's include, profile and
Lua helper and an IPC socket in the 0700 runtime directory; mpv outlives the GUI.
The filter is added over IPC once ``video-params`` and ``display-fps`` are known
(§4.2 step 2), as ``vf add @buttereye:vapoursynth=…`` with a new ``gen`` per change,
verified in the ``vf`` property. A failed init, a stalled picture or a lost GPU
device removes the filter (rollback, F4) and is reported; ButterEye never turns
it back on by itself, except that a stalled or faulting GPU engine (RIFE-ncnn or
mvgpu) is replaced by MVTools once per session (GUI.md §11.9). The engine is chosen after
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

from buttereye.core import capabilities, filelog, osapi
from buttereye.core import paths as _paths
from buttereye.core.backends import trt
from buttereye.core.bench.verdict import LIVE_HEADROOM, REALTIME_HEADROOM, measured_in_mpv
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
from buttereye.core.mpvctl import decide, launcher, livelimits, osd, shaders
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
    READABLE,
    TSCALE_CHOICES,
    VF_LABEL,
    CommandRefused,
    IpcClosed,
    MpvError,
    MpvIpc,
    SocketRefused,
    check_socket_path,
)
from buttereye.core.scriptgen.generator import (
    BUFFERED_FRAMES_DEFAULT,
    FilterParams,
    ScriptConstants,
    default_frames,
    entry_gen,
    filter_entry,
    rife_gpu_thread,
    vf_argument,
    write_script,
)
from buttereye.core.types import (
    DEBAND_PRESETS,
    PICTURE_KEYS,
    BackendId,
    BypassReason,
    CandidateId,
    Config,
    ContentKind,
    Counters,
    DetachPolicy,
    FilterState,
    HdrClass,
    Health,
    InstanceCandidate,
    Msg,
    Origin,
    Paths,
    PowerSnapshot,
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
#: a measurement scaled *up* to a bigger picture: cost grows faster than the pixel
#: count (RIFE TensorRT on an RTX 4090: 1080p -> 3840x1616 is 2.99x the pixels and
#: 3.54x the time; 1080p -> 2160p 4x and 4.3x), so fps / ratio ** 1.15; scaling down
#: stays linear, which already errs on the slow side
UPSCALE_EXPONENT = 1.15
#: live correction (§11.9): an engine Automatic chose that drops at least this share
#: of frames for HEAVY_HOLD_S is recorded as slower here (livelimits) and replaced
HEAVY_DROP = 0.15
HEAVY_HOLD_S = 10.0
#: ... but at least this share means almost nothing reached the screen, which
#: is not an engine's speed (a GPU engine that kept up a moment before went to
#: 95-101 % on the dev box: a hidden window, another program on the GPU): every
#: replacement would drop as much, so nothing is replaced and nothing learned.
#: Drops while ButterEye builds a TensorRT engine on the same GPU (or built one
#: within HEAVY_HOLD_S) are the build's: the engine is replaced for this video
#: only (``Session.video_limits``)
IMPLAUSIBLE_DROP = 0.9
#: an engine Automatic chose that drops at least this share for STEADY_HOLD_S
#: is replaced for this video only: a smaller share is often other programs'
#: load (a 4K film on MVTools dropped 1-10 % for half a minute while other
#: programs kept ten of 24 cores busy), which comes and goes
STEADY_DROP = 0.04
STEADY_HOLD_S = 30.0
#: the live cap of an engine and size replaced for this video: reaches no target
#: (0 would read as "no cap")
EXCLUDED_FPS = 1e-3
LIVE_LIMITS = "mpvctl.live_limits"  # livelimits records, loaded once per core
#: The lite model must beat the full one by more than this in this machine's
#: benchmark to be offered as a step down (§5.3 step 5).
LITE_GAIN = 1.05
#: Targets that follow the display rate (re-decided when it changes, §4.11).
DISPLAY_KINDS = frozenset({TargetKind.DISPLAY, TargetKind.DISPLAY_MAX})
#: Property changes that wake the controller at once. The others (the drop
#: counters, display-sync-active) are only stored and read on the 0.25 s tick.
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
#: P9: per-frame properties read on the controller's own 0.25 s tick instead of
#: observed. Observed, they arrived as about 100-150 events a second (each one
#: changes with every frame, several times per output frame in all), every one a
#: wakeup of ButterEye's process for a value it reads four times a second; now
#: about 12 replies a second, which keep ``last_rx_mono`` fresh like the events
#: did. The drop counters and display-sync-active are sent only when they change
#: and stay observed.
TICK_PROPS = ("playback-time", "audio-pts", "video-frame-info")
OBSERVED: frozenset[str] = OBSERVABLE - frozenset(TICK_PROPS)
#: B4: playback this long without drops (not paused, the Mac not hot) by an
#: engine Automatic chose gives back one step of a lowered estimate at that size
#: (``livelimits.raised``)
CLEAN_RECOVER_S = 600.0
#: The speed test ran cool and on mains power. While the system reports a serious
#: or critical thermal state (macOS) or a power saver mode (``PowerSnapshot.constrained``)
#: the chip runs slower than that: live caps are divided by this as well, on top of
#: ``LIVE_HEADROOM``, so Automatic leaves room instead of learning drops
PRESSURE_HEADROOM = 1.2
#: how often the power state (and the window size, for "sharper") is read again
POWER_CHECK_S = 5.0

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

    mpv: str = "mpv.exe" if osapi.IS_WINDOWS else "mpv"
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
TRT_BUILT_AT = "mpvctl.trt_built_at"  # {"t": monotonic time the last build ended}
TRT_IDENTITY = "mpvctl.trt_identity"  # Vulkan UUID -> (nvidia UUID, driver) | None
#: Automatic engine order: TensorRT when set up and not turned off, RIFE-ncnn,
#: mvgpu (GPU motion vectors: far faster than RIFE, a little less exact on fast
#: objects), MVTools. The target decides among them (``decide.pick_engine``).
LIVE_RANKING = (BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVGPU, BackendId.MVTOOLS)
#: The same for a 2D animation profile: MVTools before mvgpu, which loses large
#: flat shapes and has to find twice the step on drawings held on twos.
#: TensorRT has no hold fill: RIFE-ncnn goes before it when RIFE-ncnn keeps up
#: with hold fill's extra work (``Session.apply``)
ANIMATION_RANKING = (BackendId.RIFE_TRT, BackendId.RIFE_NCNN, BackendId.MVTOOLS, BackendId.MVGPU)
#: Hold fill re-times drawings held up to this many frames (``FilterParams.hold_max``)
HOLD_MAX = 3


def hold_fill_buffer(hold_max: int) -> int:
    """mpv buffered-frames for hold fill. mpv refuses a request further back than
    its buffer from the newest frame asked for ("Frame N requested, but only have
    frames starting from M"). With nothing cached one output frame reads source
    frames -9..+8 around its own with MVTools (-7..+7 with mvgpu; -4..+4 without
    hold fill, which runs on mpv's default of 4) at ``hold_max`` 3, but
    VapourSynth's cache keeps what was read: 4 survived seeking at 16 frames in
    parallel on the dev box. This leaves room above that measurement."""
    return 2 * hold_max + 4


#: GPU engines that MVTools replaces once after a stall, a lost device or a GPU
#: fault (§11.9), and whose runs are followed by the Xid scan (F1). TensorRT has
#: its own fallback while its engine builds and is not switched.
_SWITCHABLE = frozenset({BackendId.RIFE_NCNN, BackendId.MVGPU})


def _installed_backends(paths: Paths, *, trt: bool = False) -> frozenset[BackendId]:
    """What is installed, asked over every plugin directory this platform has.

    One function for both of this module's questions - the engines a generation
    may choose from, and whether a stalled GPU engine can still fall back to
    MVTools - so the two can never be answered from different directories. The
    directories come from ``paths.plugin_dirs``: on macOS there are two at once,
    and asking only ButterEye's own was measured to hide Homebrew's MVTools on a
    Mac with the tap installed.
    """
    return decide.installed_backends(
        _paths.plugin_dirs(paths),
        paths.rpm_data_dir / MODEL_SUBDIR,
        (paths.data_dir / USER_MODEL_SUBDIR,),
        trt=trt,
    )


class Registry:
    """Per-core session registry (``ctx.state("mpvctl.session", Registry)``)."""

    def __init__(self) -> None:
        self.options = LiveOptions()
        self.sessions: dict[SessionId, _Session] = {}
        #: the last few ended sessions' final snapshots, newest last, so their
        #: log can still be copied after mpv closed (or crashed)
        self.recent: dict[SessionId, SessionSnapshot] = {}
        #: sockets of players ``play`` is starting (not in ``sessions`` yet), so
        #: ``discover`` does not offer ButterEye's own launching mpv as an orphan
        self.launching: set[Path] = set()


def registry(ctx: ProviderContext) -> Registry:
    return ctx.state(REGISTRY_KEY, Registry)


#: ended sessions whose final snapshot is kept for their log
RECENT_KEPT = 8


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


#: what mpv uses when it can't report its own value of a property ButterEye
#: changed (``_whole``): the colour controls' neutral 0, mpv's deband defaults
_MPV_DEFAULTS: Mapping[str, int] = {
    **dict.fromkeys(PICTURE_KEYS, 0),
    **DEBAND_PRESETS["light"],
    "deband-grain": 32,
}


def _whole(name: str, value: object) -> int:
    """mpv's value of an ``ipc.INT_RANGES`` property as the whole number the
    allowlist lets ButterEye send back (mpv 0.41 keeps the colour controls as
    floats); mpv's default when it gave none."""
    from buttereye.core.mpvctl.ipc import INT_RANGES

    lo, hi = INT_RANGES.get(name, (-(2**31), 2**31 - 1))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return max(lo, min(hi, round(value)))
    return _MPV_DEFAULTS.get(name, max(lo, min(hi, 0)))


def deband_values(cfg: Config) -> dict[str, int]:
    """The deband-* values for ``general.deband_strength``/``deband_grain``
    with debanding on. "light" is mpv's own default and sets nothing, so
    deband-* lines in the user's mpv.conf stay; a grain is set only when one
    was chosen (None: mpv's own, 32)."""
    general = cfg.general
    out: dict[str, int] = {}
    if general.deband_strength != "light":
        out.update(DEBAND_PRESETS.get(general.deband_strength, {}))
    if general.deband_grain is not None:
        out["deband-grain"] = int(general.deband_grain)
    return out


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
        self.exit_code: int | None = None  # mpv's, once it has exited
        # the on-screen line (osd.py): what it last showed, and its hide timer
        self.osd_shown: tuple[object, ...] = ()
        self.osd_task: asyncio.Task[None] | None = None
        # what the running filter does, for the snapshot (reset when it is off)
        self.run_size: tuple[int, int] | None = None
        self.run_flow = 1.0
        self.auto_chosen = False  # the running engine was Automatic's choice
        self.heavy_since: float | None = None  # heavy dropping since (monotonic)
        self.steady_since: float | None = None  # STEADY_DROP or more since (monotonic)
        #: engines and sizes replaced on this file without learning anything
        #: (``livelimits.LimitKey.text``); cleared at the next file
        self.video_limits: set[str] = set()
        self.run_block = False
        #: the last hold fill verdict logged, so a re-apply does not repeat it
        self.fill_logged: tuple[bool, str] | None = None
        self.run_fill = False  # the running filter re-times held drawings
        #: hold fill dropped frames on this file: off until the next file
        self.fill_dropped = False
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
        #: detach with DetachPolicy.DISABLE_FILTER got mpv's answer to vf remove
        self.filter_removed = False
        self.want = True
        self.toggle_requested = False
        #: TensorRT engine this session fell back from while it is being built
        self.trt_waiting: trt.EngineKey | None = None
        #: the bundled shader this session added to mpv (§15.2), or None
        self.shader: Path | None = None
        #: mpv's deband value before ButterEye turned it on (§15.2), or None
        self.deband_restore: bool | None = None
        #: mpv's own values of what ButterEye changed, to put back when the setting
        #: returns to neutral and on detach: the deband-* values of a strength or
        #: grain, and the colour controls (PICTURE_KEYS)
        self.deband_params_restore: dict[str, int] = {}
        self.picture_restore: dict[str, int] = {}
        self.sent_values: dict[str, int] = {}  # what ButterEye last set of those
        #: the LUT path ButterEye set in mpv (at launch or later), or None
        self.lut: str | None = None
        #: mpv's interpolation/tscale before even pacing changed them, or None
        self.pacing_restore: dict[str, object] | None = None
        #: the A/B view for this session (Alt+c); None follows general.compare
        self.compare: bool | None = None
        self.compare_requested = False
        #: the power state at the last decision or POWER_CHECK_S tick
        self.power = PowerSnapshot()
        self.power_checked_at = self.started_mono
        #: B4: seconds of clean playback by the running engine, and the last tick
        self.clean_s = 0.0
        self.clean_tick: float | None = None
        #: "GPU faults can't be checked" was said in this session (once)
        self.fault_unknown_sent = False
        #: the user's own mpv.conf shaders and scaler were looked at (once)
        self.foreign_checked = False
        #: the last uneven-pacing verdict logged (target, display rate)
        self.pacing_logged: tuple[object, ...] | None = None
        self.step_errors = 0  # unexpected controller errors logged in full
        self.trt_built = False
        self.failure: list[str] = []
        self.device_lost = False
        #: the GPU engine (RIFE-ncnn or mvgpu) that ran last: its failures and the
        #: session end are followed by a GPU-fault (Xid) scan (F1)
        self.gpu_used: BackendId | None = None
        # §11.9: after a GPU engine stalled or faulted, this session runs MVTools only
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
            exit_code=self.exit_code,
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
            self.update_osd(snap)
        elif counters != self._last_counters:
            self._last_counters = counters
            self.ctx.emit_coalesced(self.sid, SessionChanged(snap), min_interval_s=0.25)
        return snap

    def keep_logs(self) -> bool:
        try:
            return self.ctx.config().general.keep_logs
        except ButterEyeError:
            return True

    def update_osd(self, snap: SessionSnapshot) -> None:
        """Show the small mpv line when what smoothing does changed, for the
        configured time (GUI.md §12.1.5); "always" keeps it up and updates it."""
        try:
            general = self.ctx.config().general
        except ButterEyeError:
            return
        seconds, items = general.osd_seconds, general.osd_items
        sig = osd.signature(snap, items)
        if seconds == 0 or snap.ended or not sig or sig == self.osd_shown:
            return
        self.osd_shown = sig
        if self.osd_task is not None:
            self.osd_task.cancel()
        text = osd.ass(osd.parts(snap, items))

        async def show() -> None:
            with contextlib.suppress(IpcClosed, MpvError, TimeoutError, CommandRefused):
                await self.ipc.command(
                    "osd-overlay", osd.OVERLAY_ID, "ass-events", text, 0, osd.RES_Y
                )
                if seconds > 0:
                    await asyncio.sleep(seconds)
                    await self.ipc.command("osd-overlay", osd.OVERLAY_ID, "none", "", 0, 0)
                    self.osd_shown = ()  # the same state may be shown again later

        self.osd_task = self.ctx.spawn(show(), name=f"osd-{self.sid}")

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
                # B19: the helper flips the filter itself when no ack arrives in
                # 0.6 s, and the controller can hold the lock for the 4 s verify
                # window: the ack goes out now, without the lock, and the toggle
                # waits for the controller as before
                ack = self.ack("toggle")
                try:
                    self.ctx.spawn(ack, name=f"mpvctl-{self.sid}-ack")
                except ButterEyeError, RuntimeError:  # the core is shutting down
                    ack.close()
                self.toggle_requested = True
            elif isinstance(args, list) and args[:2] == ["buttereye-request", "compare"]:
                self.compare_requested = True
        self.wake.set()

    async def ack(self, what: str) -> None:
        """Tell the Lua helper ButterEye handles ``what`` (no lock taken)."""
        with contextlib.suppress(IpcClosed, MpvError, TimeoutError):
            await self.ipc.command("script-message-to", HELPER_NAME, "ack", what, timeout_s=1.0)

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
            text = render(
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
            display = decide.display_rate(self.display_fps)
            if display is not None and decide.uneven_pacing(self.target, self.display_fps):
                # B3: 2x of 23.976 on 60 Hz is shown 1-1-1-2
                text += " · " + render(Msg("uneven pacing on {hz} Hz", {"hz": _fmt_rate(display)}))
            return text
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
                # power and heat conditions (RuleMatch.on_battery, thermal_*)
                trace = explain(cfg, facts, self.power)
                chosen = by_id.get(getattr(trace, "profile_id", ""))
                if chosen is not None:
                    return chosen, note
            except Exception:
                self.log.exception("rule evaluation failed")
        return cfg.profiles[0], note

    async def sustainable_fps(
        self,
        backend: BackendId,
        model: str | None,
        facts: SourceFacts,
        cfg: Config | None,
        *,
        flow_scale: float = 1.0,
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
        no benchmark for this engine.

        Scaling up to a bigger picture divides by ``ratio ** UPSCALE_EXPONENT``;
        ``flow_scale`` 0.5 (TensorRT half-resolution flow) gains
        ``decide.HALF_FLOW_GAIN``; what live playback learned about this engine at
        this size on this GPU (``livelimits``) multiplies the result. An engine
        and size replaced on this video (``video_limits``) get ``EXCLUDED_FPS``,
        with or without a benchmark."""
        current = self.gpu_device(cfg)
        size = (facts.width, facts.height)
        if self.limit_key(backend, model, size, flow_scale, current).text() in self.video_limits:
            return EXCLUDED_FPS
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
                if m.gpu_faults or not measured_in_mpv(m) or not m.mpv_fps > 0:
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
                scaled = fps * ratio**UPSCALE_EXPONENT if ratio < 1 else fps * ratio
                found.append((round(-abs(math.log(ratio)), 6), stamp, scaled))
        if not found:
            return None
        rate = max(found)[2] / LIVE_HEADROOM
        if flow_scale != 1.0:
            rate *= decide.HALF_FLOW_GAIN
        if self.power.constrained:
            rate /= PRESSURE_HEADROOM
        key = self.limit_key(backend, model, (facts.width, facts.height), flow_scale, current)
        return rate * livelimits.factor(self.live_limits(), key)

    def read_power(self) -> PowerSnapshot:
        """``osapi.power_snapshot`` (None where the system does not say); never raises."""
        try:
            return osapi.power_snapshot()
        except Exception:
            self.log.debug("power state unreadable", exc_info=True)
            return PowerSnapshot()

    def live_limits(self) -> dict[str, dict[str, float]]:
        path = self.ctx.paths.state_dir / livelimits.FILE
        return self.ctx.state(LIVE_LIMITS, lambda: livelimits.load(path))

    @staticmethod
    def limit_key(
        backend: BackendId,
        model: str | None,
        size: tuple[int, int],
        flow_scale: float,
        device: Any,
    ) -> livelimits.LimitKey:
        uuid = getattr(device, "uuid", None)
        return livelimits.LimitKey(
            uuid if backend in decide.GPU_BACKENDS else None,
            backend.value,
            model,
            size[0],
            size[1],
            flow_scale,
        )

    def trt_building(self) -> bool:
        """A TensorRT engine is being built in this core (on the GPU), or one
        ended within ``HEAVY_HOLD_S`` (the drops judged now began during it)."""
        builds: dict[trt.EngineKey, object] = self.ctx.state(TRT_BUILDS, dict)
        if any(state == "building" for state in builds.values()):
            return True
        built_at: dict[str, float] = self.ctx.state(TRT_BUILT_AT, dict)
        ended = built_at.get("t")
        return ended is not None and time.monotonic() - ended < HEAVY_HOLD_S

    async def live_fallback(self, drop_rate: float, *, learn: bool = True) -> None:
        """Automatic's engine keeps dropping heavily: record that it is slower here
        than estimated (``livelimits``) and decide again, which moves to the next
        engine or size that fits, or plays unsmoothed (§11.9).

        Not learned, only replaced for this video (``video_limits``), when
        ``learn`` is False (steady moderate drops), when a GPU engine dropped
        while a TensorRT engine was being built, or while the system reports
        a serious thermal state or a power saver mode (B4: on the fanless Air two hot
        episodes ruled mvgpu out at 1080p for a month). What is learned carries
        the power state (``livelimits.record``). Near-total drops
        (``IMPLAUSIBLE_DROP``) never get here (``step``)."""
        self.heavy_since = None
        self.steady_since = None
        src = self.source
        if self.backend is None or src is None:
            return
        if decide.over_display(self.target, self.display_fps):
            # the display can't show this rate (it changed after the decision):
            # mpv drops the surplus by design, which says nothing about the
            # engine's speed, so nothing is learned - only decided again
            await self.apply(raise_on_fail=False)
            return
        if self.run_fill:
            # hold fill's extra work, not the engine: turn it off for this file
            # and learn nothing, so the engine's estimate stays right for every
            # other video (live-action ones never re-time)
            self.fill_dropped = True
            self.log.info(
                "%s: dropped %.0f%% of frames while re-timing held drawings; "
                "playing on without re-timing",
                self.title, drop_rate * 100,
            )  # fmt: skip
            if self.health is Health.DROPPING:
                self.health, self.step_down_to = Health.OK, None
            await self.apply(raise_on_fail=False)
            return
        cfg, _ = self.config()
        size = self.run_size or (src.width, src.height)
        key = self.limit_key(self.backend, self.model, size, self.run_flow, self.gpu_device(cfg))
        name = decide.BACKEND_NAMES.get(self.backend, self.backend.value)
        engine = f"{name} {self.model}" if self.model else name
        why: str | None = None
        self.power = power = self.read_power()
        if not learn:
            why = f"for {STEADY_HOLD_S:.0f} s"
        elif self.backend in decide.GPU_BACKENDS and self.trt_building():
            why = "while a TensorRT engine was being built on the GPU"
        elif power.constrained:
            # B4: a hot machine, or one with the power saver on, runs everything
            # slower than the speed test did; learned, it ruled engines out for a month
            why = "while the system reported it running hot or in power-saving mode"
        if why is not None:
            self.video_limits.add(key.text())
            self.log.info(
                "%s: %s dropped %.0f%% of frames at %s × %s %s; choosing again for "
                "this video (nothing learned)",
                self.title, engine, drop_rate * 100, size[0], size[1], why,
            )  # fmt: skip
        else:
            limits = self.live_limits()
            new = livelimits.lowered(livelimits.factor(limits, key), drop_rate)
            livelimits.record(
                self.ctx.paths.state_dir / livelimits.FILE, limits, key, new, power=power
            )
            self.log.info(
                "%s: %s dropped %.0f%% of frames at %s × %s; it is slower here than the "
                "speed test showed (estimate × %.2f from now on), choosing again",
                self.title, engine, drop_rate * 100, size[0], size[1], new,
            )  # fmt: skip
        msg = Msg(
            "{engine} couldn't keep up at {w} × {h}, so ButterEye switched to what can.",
            {"engine": name, "w": size[0], "h": size[1]},
        )
        self.emit(Notice(msg, None, self.sid))
        if self.health is Health.DROPPING:
            self.health, self.step_down_to = Health.OK, None
        await self.apply(raise_on_fail=False)

    async def step_down_choice(self) -> str | None:
        """The profile to offer when frames drop (§5.3 step 5)."""
        cfg, _ = self.config()
        profiles = cfg.profiles if cfg is not None else ()
        return decide.step_down_target(
            self.profile_id,
            profiles,
            multiplier=self.multiplier,
            skip_lite=self.profile_id == "quality" and await self.lite_no_faster(profiles),
            running=self.backend,
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
                if not measured_in_mpv(m) or not m.mpv_fps > 0:
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
        # even pacing belongs to a running target: turned off, rolled back or
        # bypassed, mpv's own blending settings come back (an apply that adds a
        # filter again decides anew)
        if self.pacing_restore is not None and not self.unresponsive and not self.remove_pending:
            await self.sync_pacing(None)

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
        self.run_fill = False

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
        engine keep theirs, with a notice when that differs from the Setup choice.

        Where RIFE-ncnn needs a benchmark before it runs live (a Mac, or an
        interpolation GPU that is not discrete: ``decide.rife_live_needs_proof``),
        a shipped profile still on its shipped RIFE-ncnn, with no Setup choice,
        asks for Automatic instead."""
        override = cfg.general.backend_override if cfg is not None else None
        if profile.backend == "auto":
            return override, None
        pinned = BackendId(profile.backend)
        if (
            override is None
            and pinned is BackendId.RIFE_NCNN
            and _follows_setup_choice(profile)
            and decide.rife_live_needs_proof(self.gpu_device(cfg))
        ):
            # a shipped profile's RIFE is a discrete-GPU default, not the user's
            # choice, and on a Mac or an integrated GPU RIFE was measured offline only. Automatic
            # then trusts RIFE only with a benchmark that caps it
            # (decide.pick_engine); a profile the user set to RIFE, or RIFE chosen
            # in Setup, still pins it.
            return None, None
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

    async def hold_fill_choice(
        self,
        profile: Profile,
        backend: BackendId,
        facts: SourceFacts,
        size: tuple[int, int] | None,
        target: Fraction,
        cfg: Config | None,
        *,
        model: str | None,
        forced: bool,
        block: bool,
    ) -> tuple[bool, str]:
        """Whether held drawings are re-timed (``FilterParams.hold_fill``), and why
        (for the log).

        A live-action profile never has it. Otherwise it is on only when this
        engine's live cap at the size it runs absorbs the worst case it adds
        (``decide.hold_fill_fits``: 1.67× the work at 2× on threes), and never
        when the engine already can't keep up - smoothing forced past the speed
        test, MVTools in block mode - or it dropped frames on this file
        (``live_fallback``): held drawings then judder as before, which beats
        dropping frames, and the engine's estimate is not lowered for its sake.
        Without a benchmark a 2D animation profile has it (the user asked) and
        Automatic does not. TensorRT has no hold fill."""
        if backend is BackendId.RIFE_TRT:
            return False, "TensorRT does not support it"
        if profile.content is ContentKind.LIVE:
            return False, "the profile is set to live action"
        if self.fill_dropped:
            return False, "frames dropped with it on this video"
        if forced:
            return False, "smoothing runs past the speed test"
        if block:
            return False, "MVTools is in block mode to keep up"
        w, h = size or (facts.width, facts.height)
        cap = await self.sustainable_fps(
            backend, model, dataclasses.replace(facts, width=w, height=h), cfg
        )
        if size is not None:
            cap = decide.shrink_cap(cap, facts.width, facts.height)
        if cap is None:
            if profile.content is ContentKind.ANIMATION:
                return True, "the profile is set to 2D animation (no speed test to check)"
            return False, "no speed test measured this engine, so it may not keep up"
        need = float(decide.load_rate(facts.fps, target)) * decide.hold_fill_factor(
            facts.fps, target
        )
        if not decide.hold_fill_fits(facts.fps, target, cap):
            return False, f"it needs about {need:.0f} fps of capacity and {cap:.0f} is measured"
        return True, f"capacity {cap:.0f} fps covers the worst case of {need:.0f}"

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
                # no plugin name to resolve: the TensorRT branch of the template
                # loads vstrt from the install's own directory, never PLUGIN_DIR
                write_script(script, ScriptConstants(_paths.plugin_dir_for(ctx.paths)))
                await trt.build_engine(ctx.paths, inst, key, script=script)
                builds[key] = "ready"
            except asyncio.CancelledError:
                builds.pop(key, None)
                raise
            except Exception as exc:  # recorded; the session keeps its fallback
                self.log.warning("TensorRT engine %s failed: %s", key.folder_name(), exc)
                builds[key] = exc
            built_at: dict[str, float] = ctx.state(TRT_BUILT_AT, dict)
            built_at["t"] = time.monotonic()
            for other in {*registry(ctx).sessions.values(), starter}:
                other.trt_built = True
                other.wake.set()

        ctx.spawn(run(), name=f"trt-build-{key.folder_name()}")

    async def sync_shader(self, cfg: Config | None) -> None:
        """Add or remove the bundled upscaling shader to match ``general.upscaling``
        (§15.2); "sharper" only while ``shaders.sharper_allowed`` says it is worth
        its GPU time (P5). Only ButterEye's own shader is touched; the user's stay."""
        setting = cfg.general.upscaling if cfg is not None else "standard"
        want = shaders.shader_for(setting)
        if want is not None and setting == "sharper" and not await self.sharper_ok():
            want = None
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

    async def sharper_ok(self) -> bool:
        """``shaders.sharper_allowed`` for the running picture: the upscale ratio
        from mpv's ``osd-dimensions`` when ButterEye may read it, the smoothing
        size and the power state."""
        src = self.source
        size = self.run_size or ((src.width, src.height) if src is not None else None)
        ratio: float | None = None
        if size is not None and "osd-dimensions" in READABLE:
            with contextlib.suppress(IpcClosed, MpvError, TimeoutError):
                osd_dims = await self.ipc.get("osd-dimensions", timeout_s=1.0)
                ratio = shaders.video_scale(osd_dims, *size)
        ok = shaders.sharper_allowed(
            ratio, reduced_size=self.run_size is not None, power=self.power
        )
        if not ok and self.shader is not None:
            self.log.info(
                "%s: Sharper upscaling off for now (%s)",
                self.title,
                "smoothing at a smaller size"
                if self.run_size is not None
                else "on battery, hot or in Low Power Mode"
                if self.power.on_battery or self.power.constrained
                else f"the picture is enlarged only {ratio:.2f}x",
            )
        return ok

    async def sync_values(
        self, wanted: Mapping[str, int], restore: dict[str, int], what: str
    ) -> None:
        """Set integer player properties to ``wanted``; ``restore`` keeps mpv's own
        value of each one ButterEye changed, and every name that leaves ``wanted``
        gets it back. mpv keeps some of them as floats; ButterEye sends whole
        numbers only (``ipc.INT_RANGES``), so the old value is rounded and clamped,
        and one mpv can't report goes back to mpv's default."""
        try:
            for name in [n for n in restore if n not in wanted]:
                await self.ipc.command("set_property", name, restore[name])
                del restore[name]
                self.sent_values.pop(name, None)
            for name, value in wanted.items():
                if name not in restore:
                    restore[name] = _whole(name, await self.ipc.get(name))
                elif self.sent_values.get(name) == value:
                    continue
                await self.ipc.command("set_property", name, int(value))
                self.sent_values[name] = int(value)
        except (IpcClosed, MpvError, TimeoutError) as exc:
            self.log.warning("%s: could not change %s: %s", self.title, what, exc)

    async def sync_deband(self, cfg: Config | None) -> None:
        """Turn mpv's own debanding on for ``general.deband`` and back to its
        previous value when it is turned off (§15.2). A deband=yes from the
        user's mpv.conf is never turned off. With it on, ``deband_strength`` and
        ``deband_grain`` set mpv's deband-* values (``deband_values``)."""
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
        values = deband_values(cfg) if want and cfg is not None else {}
        await self.sync_values(values, self.deband_params_restore, "debanding")

    async def sync_picture(self, cfg: Config | None) -> None:
        """mpv's colour controls (brightness, contrast, saturation, gamma, hue)
        to ``general.picture``. mpv applies them when it renders, after
        smoothing, so motion estimation and the source frames passed through
        never see them. A control at 0 is left to mpv and the user's mpv.conf;
        one ButterEye changed gets mpv's own value back at 0 and on detach."""
        pic = cfg.general.picture if cfg is not None else None
        wanted = {
            k: int(getattr(pic, k)) for k in PICTURE_KEYS if pic is not None and getattr(pic, k)
        }
        await self.sync_values(wanted, self.picture_restore, "the picture settings")

    async def sync_lut(self, cfg: Config | None) -> None:
        """mpv's ``lut`` to the imported 3D LUT ``general.lut`` names
        (``profiles.luts.resolve_lut``: only a file in ButterEye's own private
        luts folder; the IPC allowlist checks the same). Set at launch
        (``--lut``) and changed while playing; mpv reloads a LUT whose path
        changes. A player ButterEye launches takes the LUT chosen in ButterEye:
        the command line wins over a ``lut=`` in the user's mpv.conf, and on
        detach the player is left with none (mpv cannot say what the file had
        set). A player ButterEye attaches to keeps a LUT of its own: it is never
        replaced or cleared, since the allowlist could not put it back."""
        from buttereye.core.profiles import luts

        name = cfg.general.lut if cfg is not None else None
        path = luts.resolve_lut(self.ctx.paths, name) if name else None
        want = str(path) if path is not None else None
        if want == self.lut:
            return
        try:
            if self.lut is None:
                current = await self.ipc.get("lut")
                if isinstance(current, str) and current and current != want:
                    self.log.info("%s: mpv already uses its own LUT; leaving it", self.title)
                    return
            await self.ipc.command("set_property", "lut", want or "")
            self.lut = want
        except (IpcClosed, MpvError, TimeoutError, CommandRefused) as exc:
            self.log.warning("%s: could not change the LUT: %s", self.title, exc)

    async def sync_pacing(self, cfg: Config | None) -> None:
        """B3, opt-in even pacing (``general.even_pacing``): for a running target
        the display can't show evenly (``decide.uneven_pacing``), mpv's own frame
        blending with ``tscale=oversample``. Oversample blends only the two
        frames around a refresh that falls between them, so most refreshes still
        show one whole frame and the 1-1-1-2 judder goes. ButterEye's profile
        turns blending off on purpose (it must not mix with interpolated frames
        by default), so this is only ever the user's choice; mpv's own values
        come back when it no longer applies and on detach."""
        on = (
            cfg is not None
            and cfg.general.even_pacing
            and self.active_gen is not None
            and decide.uneven_pacing(self.target, self.display_fps)
        )
        try:
            if on and self.pacing_restore is None:
                interp = await self.ipc.get("interpolation")
                tscale = await self.ipc.get("tscale")
                restore: dict[str, object] = {
                    "interpolation": interp if isinstance(interp, (bool, str)) else False
                }
                if isinstance(tscale, str) and tscale in TSCALE_CHOICES:
                    restore["tscale"] = tscale
                await self.ipc.command("set_property", "tscale", "oversample")
                await self.ipc.command("set_property", "interpolation", True)
                self.pacing_restore = restore
                self.log.info("%s: even pacing on (mpv interpolation, oversample)", self.title)
            elif not on and self.pacing_restore is not None:
                restore, self.pacing_restore = self.pacing_restore, None
                for name, value in restore.items():
                    await self.ipc.command("set_property", name, value)
        except (IpcClosed, MpvError, TimeoutError) as exc:
            self.log.warning("%s: could not change frame pacing: %s", self.title, exc)

    async def restore_player(self) -> None:
        """Detach: put back what ButterEye changed in the player besides the
        filter (picture, debanding, LUT, pacing). The upscaling shader stays,
        as before (removing it would sharpen nothing the user chose)."""
        await self.sync_picture(None)
        await self.sync_deband(None)
        if self.lut is not None:
            await self.sync_lut(None)
        await self.sync_pacing(None)

    def compare_on(self, cfg: Config | None) -> bool:
        """The A/B split view: this session's Alt+c choice, else the setting."""
        if self.compare is not None:
            return self.compare
        return cfg is not None and cfg.general.compare == "split"

    async def check_user_render(self, backend: BackendId) -> None:
        """Once per session, when a GPU engine runs: shaders from the user's own
        mpv.conf, or an ``ewa_*`` scaler, run on every frame mpv shows, the
        interpolated ones included, on the GPU the engine uses, and the speed
        test (``--vo=null``) never saw them. Said once, so drops can be traced."""
        if self.foreign_checked or backend not in decide.GPU_BACKENDS:
            return
        self.foreign_checked = True
        try:
            listed = await self.ipc.get("glsl-shaders", timeout_s=1.0)
            scale = await self.ipc.get("scale", timeout_s=1.0)
        except IpcClosed, MpvError, TimeoutError:
            return
        own = shaders.ALLOWED
        foreign = [x for x in listed if str(x) not in own] if isinstance(listed, list) else []
        ewa = isinstance(scale, str) and scale.startswith("ewa_")
        engine = decide.BACKEND_NAMES.get(backend, backend.value)
        msgs: list[Msg] = []
        if foreign:
            msgs.append(
                Msg(
                    "Your mpv.conf adds {count} shader(s); mpv runs them on every smoothed "
                    "frame, on the GPU {engine} uses. If frames drop, try without them.",
                    {"count": len(foreign), "engine": engine},
                )
            )
        if ewa:
            msgs.append(
                Msg(
                    "Your mpv.conf uses the {scale} scaler, which is costly on every "
                    "smoothed frame, on the GPU {engine} uses. If frames drop, try a "
                    "lighter one.",
                    {"scale": scale, "engine": engine},
                )
            )
        for m in msgs:
            self.emit(Notice(m, None, self.sid))

    async def apply(self, *, raise_on_fail: bool) -> None:
        """Decide (§4.11, §5.6) and add/replace/remove the filter; verify a new gen."""
        cfg_now = self.config()[0]
        self.power = self.read_power()
        await self.sync_shader(cfg_now)
        await self.sync_deband(cfg_now)
        await self.sync_picture(cfg_now)
        await self.sync_lut(cfg_now)
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
        installed = _installed_backends(paths, trt=trt_model is not None)
        wanted: BackendId | None
        pinned_note: Msg | None
        if self.backend_override is not None:
            wanted, pinned_note = self.backend_override, None
        else:
            wanted, pinned_note = self.wanted_backend(profile, cfg)
        # an unmeasured RIFE-ncnn needs a benchmark before Automatic runs it live
        # here (a Mac, a GPU that is not discrete); the engine picks below ask this
        rife_proof = decide.rife_live_needs_proof(self.gpu_device(cfg))
        notes = [n for n in (profile_note, pinned_note) if n is not None]
        # §11.9: candidate engines in preference order; the target decides among them
        order: list[BackendId]
        animation = profile.content is ContentKind.ANIMATION
        if wanted is None:
            ranking = ANIMATION_RANKING if animation else LIVE_RANKING
            order = [b for b in ranking if b in installed]
        else:
            choice = decide.resolve_backend(wanted, installed)
            if choice.notice is not None:
                notes.append(choice.notice)
            order = [choice.backend] if choice.backend is not None else []
            # fallbacks, used only if the chosen engine can't reach the target
            if choice.backend is BackendId.RIFE_TRT and BackendId.RIFE_NCNN in installed:
                order.append(BackendId.RIFE_NCNN)
            fallbacks: list[BackendId] = []
            if choice.backend in decide.RIFE_BACKENDS and BackendId.MVGPU in installed:
                fallbacks.append(BackendId.MVGPU)
            if choice.backend in decide.GPU_BACKENDS and BackendId.MVTOOLS in installed:
                fallbacks.append(BackendId.MVTOOLS)
            if animation:
                fallbacks.sort(key=ANIMATION_RANKING.index)  # MVTools before mvgpu
            order += fallbacks
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
                animation=animation,
                rife_proof=rife_proof,
            )

        full_size = cfg is not None and cfg.general.full_size
        #: B3: the user puts even pacing before resolution ("Match your display")
        cadence_first = cfg is not None and cfg.general.cadence_first

        async def sized_options(
            cands: Sequence[BackendId], below: int | None = None
        ) -> list[tuple[tuple[int, int], list[decide.EngineOption]]]:
            """The engine options at each smaller size (below ``below`` lines)."""
            return [
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
                if below is None or h < below
            ]

        async def decide_with(
            cands: Sequence[BackendId],
        ) -> tuple[decide.EnginePick, tuple[int, int] | None, bool, list[Msg], bool]:
            """``decide_size`` with B3's cadence-first step: a display target that
            would pace unevenly takes a smaller size where a GPU engine reaches an
            even one, when the user asked for that."""
            found = await decide_size(cands)
            pick, size = found[0], found[1]
            t = pick.target.target
            if (
                cadence_first
                and not full_size
                and not found[2]
                and profile.target.kind in DISPLAY_KINDS
                and t is not None
                and decide.uneven_pacing(t, facts.display_hz)
            ):
                even = decide.pick_even_size(
                    await sized_options(cands, size[1] if size else None),
                    facts.fps,
                    facts.display_hz or None,
                    profile.target.kind,
                    profile.target.fps,
                    auto=wanted is None,
                    animation=animation,
                )
                if even is not None:
                    ep, es = even
                    return (
                        ep,
                        es,
                        False,
                        [decide.even_pacing_notice(es[1], facts.display_hz)],
                        False,
                    )
            return found

        async def decide_size(
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
                    decide.EngineOption(
                        BackendId.RIFE_TRT,
                        await self.sustainable_fps(
                            BackendId.RIFE_TRT, trt_model, facts, cfg, flow_scale=0.5
                        ),
                    )
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
            if (
                pick.backend is BackendId.MVTOOLS
                and pick.target.bypass is None
                and pick.target.target is not None
                and wanted is None
                and not animation
                and not full_size
                and BackendId.MVGPU in cands
                and decide.prefer_gpu_smaller()
            ):
                # P3: on a fanless Mac, mvgpu at a smaller size before CPU MVTools
                # at the video's own, when it reaches as high a target; measured
                # GPU engines only (an unmeasured one would run uncapped)
                gpu = [b for b in cands if b in decide.GPU_BACKENDS]
                measured = [
                    (sz, [o for o in opts if o.sustainable_fps is not None])
                    for sz, opts in await sized_options(gpu)
                ]
                smaller = decide.pick_size(
                    [(sz, opts) for sz, opts in measured if opts],
                    facts.fps,
                    facts.display_hz or None,
                    profile.target.kind,
                    profile.target.fps,
                    auto=True,
                    rife_proof=rife_proof,
                )
                if smaller is not None:
                    gp, gsize = smaller
                    gt = gp.target.target
                    if (
                        gp.backend in decide.GPU_BACKENDS
                        and gt is not None
                        and gt >= pick.target.target
                    ):
                        return gp, gsize, False, [decide.smaller_size_notice(gsize[1])], False
            if pick.target.bypass is BypassReason.NO_REALTIME:
                # too demanding at its own size: smooth at a smaller size (§11.9)
                sized = await sized_options(cands)
                found = decide.pick_size(
                    sized,
                    facts.fps,
                    facts.display_hz or None,
                    profile.target.kind,
                    profile.target.fps,
                    auto=wanted is None,
                    forced=self.forced,
                    animation=animation,
                    rife_proof=rife_proof,
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

        if (
            animation
            and wanted is None
            and not self.fill_dropped
            and BackendId.RIFE_TRT in order
            and BackendId.RIFE_NCNN in order
        ):
            # TensorRT has no hold fill: RIFE-ncnn first when it doubles the
            # rate at full size with room for hold fill's extra work
            (ncnn,) = await engine_options(facts, [BackendId.RIFE_NCNN])
            rife = choose([ncnn], forced=False)
            t = rife.target.target
            if (
                rife.backend is BackendId.RIFE_NCNN
                and rife.target.bypass is None
                and t is not None
                and t >= facts.fps * 2 * decide.AUTO_DOUBLING_TOLERANCE
                and decide.hold_fill_fits(facts.fps, t, ncnn.sustainable_fps)
            ):
                order.remove(BackendId.RIFE_NCNN)
                order.insert(order.index(BackendId.RIFE_TRT), BackendId.RIFE_NCNN)
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
            # ALREADY_AT_RATE / NO_REALTIME depend on the display rate: for a
            # display target always, for 2x and a fixed rate once it is known
            # (they never go above it, decide.over_display): remember it so a
            # new rate re-decides (§4.11).
            depends = (
                profile.target.kind in DISPLAY_KINDS or bool(facts.display_hz)
            ) and tc.bypass in (
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
        hold_fill, fill_why = await self.hold_fill_choice(
            profile,
            backend,
            facts,
            size,
            tc.target,
            cfg,
            model=model_for(backend),
            forced=forced_used,
            block=bool(mv_block and backend is BackendId.MVTOOLS),
        )
        # the script's frame analysis (cuts, still pairs) reads up to 4 frames
        # back: a profile's smaller buffer would make mpv refuse those requests
        buffered = max(buffered, BUFFERED_FRAMES_DEFAULT)
        if hold_fill:
            buffered = max(buffered, hold_fill_buffer(HOLD_MAX))
        if (hold_fill, fill_why) != self.fill_logged:
            self.fill_logged = (hold_fill, fill_why)
            self.log.info(
                "%s: re-timing held drawings %s: %s",
                self.title,
                "on" if hold_fill else "off",
                fill_why,
            )
        h = facts.height
        gen = self.next_gen()
        self.run_size = size
        self.run_flow = self.flow_scale if backend is BackendId.RIFE_TRT else 1.0
        # live correction only second-guesses ButterEye's own choice: not an engine
        # the user picked, nor "smooth at full size" (frames may drop by choice)
        self.auto_chosen = wanted is None and not forced_used
        self.heavy_since = None
        self.steady_since = None
        self.clean_s, self.clean_tick = 0.0, None
        self.run_block = bool(mv_block and backend is BackendId.MVTOOLS)
        self.run_fill = hold_fill
        params = FilterParams(
            gen=gen,
            backend=backend,
            src_fps=facts.fps,
            target_fps=tc.target,
            buffered_frames=buffered,
            concurrent_frames=concurrent,
            matrix=decide.matrix_name(vp.get("colormatrix"), h),
            range=decide.range_name(vp.get("colorlevels")),
            chroma_loc=decide.chroma_loc_name(vp.get("chroma-location")),
            model_path=model_path if backend is BackendId.RIFE_NCNN else None,
            gpu_id=(
                self.gpu_index(cfg) if backend in (BackendId.RIFE_NCNN, BackendId.MVGPU) else None
            ),
            gpu_thread=rife_gpu_thread() if backend is BackendId.RIFE_NCNN else None,
            uhd=decide.uhd_mode(*(size or (facts.width, facts.height))),
            # every engine: the template also blocks hold fill and MVTools'
            # vectors across cuts with it
            sc_threshold=profile.sc_threshold,
            hold_fill=hold_fill,
            hold_max=HOLD_MAX,
            # identical frames are copied, not interpolated: speed only
            still_skip=True,
            title=self.title,
            size=size,
            mv_mode="block" if mv_block and backend is BackendId.MVTOOLS else None,
            compare="split" if self.compare_on(cfg) else None,
            aa=profile.aa and backend in decide.GPU_BACKENDS,
            hdr=facts.hdr_class is HdrClass.HDR10,
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
        # one absolute path per plugin, not the search path: the directory that
        # really holds this engine's plugin, which is the directory the doctor
        # reports it in (paths.plugin_dir_for)
        self.failure.clear()
        self.device_lost = False
        try:
            write_script(
                self.script_path,
                ScriptConstants(_paths.plugin_dir_for(paths, decide.BACKEND_PLUGIN.get(backend))),
            )
        except OSError as exc:
            # B14: a full disk, or the runtime folder removed under a running
            # player (macOS cleans /var/folders): the new filter can't start, and
            # the running one goes the normal rollback way instead of the
            # controller dying with mpv still playing an unmanaged filter
            self.failure.append(f"could not write the script {self.script_path.name}: {exc}")
        self.busy = True
        if backend is BackendId.RIFE_NCNN and not self.failure:
            mark_rife_session(self.ctx.paths, (self.proc.pid,))  # BE-1030 window starts
        try:
            if not self.failure:
                try:
                    await self.ipc.command("vf", "add", vf_argument(self.script_path, params))
                except MpvError as exc:
                    self.failure.append(f"vf add: {exc.error}")
            ok = not self.failure and await self.verify(gen, float(tc.target))
        finally:
            self.busy = False
        if backend in _SWITCHABLE:
            self.gpu_used = backend
        if not ok and self.ipc.closed.is_set():
            # B22: mpv quit inside the verify window; nothing failed
            return
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
        # B3: the OSD line and the helper's status say it (osd.parts, status_text);
        # the GUI reads the same from the snapshot (decide.uneven_pacing)
        pacing = (tc.target, decide.display_rate(facts.display_hz))
        if decide.uneven_pacing(tc.target, facts.display_hz):
            if pacing != self.pacing_logged:
                self.log.info(
                    "%s: %s fps on a %s Hz display paces unevenly (%.4f refreshes per frame)",
                    self.title,
                    decide.fmt_rate(tc.target),
                    decide.fmt_rate(pacing[1] or Fraction(0)),
                    float(decide.cadence(tc.target, facts.display_hz) or 0),
                )
        self.pacing_logged = pacing
        await self.sync_shader(cfg)  # the smoothing size is known now (P5)
        await self.sync_pacing(cfg)
        await self.check_user_render(backend)

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
            await self.poll_tick_props(("playback-time", "audio-pts"))
            verdict = self.detector.update(sample_from(self.props, time.monotonic()))
            if verdict.flowing:
                return True
            if self.props.get("pause") is True:
                paused_since = paused_since or loop.time()
                if loop.time() - paused_since >= 1.0:
                    return True
            await asyncio.sleep(0.05)
        return not self.failure

    async def poll_tick_props(self, names: Sequence[str] = TICK_PROPS) -> None:
        """Read the per-frame properties ButterEye no longer observes (P9), all at
        once. A value mpv does not have (no audio track: no audio-pts) leaves the
        last one in place; a property mpv does not answer in time is skipped."""
        if self.ipc.closed.is_set():
            return

        async def one(name: str) -> None:
            with contextlib.suppress(MpvError, TimeoutError):
                value = await self.ipc.get(name, timeout_s=0.5)
                if value is not None:
                    self.props[name] = value

        await asyncio.gather(*(one(n) for n in names))

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
        """STALLED: roll back automatically (R12). A RIFE-ncnn or mvgpu filter is
        replaced by MVTools once (§11.9); otherwise interpolation stays off (never
        re-enabled)."""
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
        """A running GPU-engine filter (RIFE-ncnn or mvgpu) that may still be
        replaced by MVTools (once)."""
        if self.backend not in _SWITCHABLE or self.backend_override is not None:
            return False
        return BackendId.MVTOOLS in _installed_backends(self.ctx.paths)

    async def switch_to_cpu(
        self,
        health: Health,
        code: ErrorCode,
        reason: Msg,
        evidence: tuple[str, ...],
        *,
        scan_faults: bool = True,
    ) -> bool:
        """§11.9: a GPU engine (RIFE-ncnn or mvgpu) stalled, lost its device or
        faulted the GPU → this
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
            self.after_failure()  # the Xid check for the GPU engine's run (F1)
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
        if self.gpu_used is not None:
            with contextlib.suppress(ButterEyeError):
                self.ctx.spawn(self.check_gpu_faults(), name=f"mpvctl-{self.sid}-xid")

    def gpu_name(self) -> str:
        """The GPU engine that ran last, as the user sees it (RIFE before any run)."""
        return decide.BACKEND_NAMES[self.gpu_used or BackendId.RIFE_NCNN]

    async def check_gpu_faults(self, *, final: bool = False) -> None:
        """Xid lines newer than the last reported one (F1); unknown is reported, never OK.

        A per-session cursor starts at the session start and moves past every
        reported fault, so a later failure or the session end never reports the
        same kernel lines again. "Can't be checked" is said once per session and
        only after a failure, not at the session end (``final``): on macOS and
        Windows the scan never can, and 0.0.5 said so at every stall, switch and
        end of an mvgpu or RIFE video."""
        async with self.fault_lock:
            scan_wall, scan_mono = time.time(), time.monotonic()
            res = await gpu_fault_scan(
                since_monotonic=self.fault_since_mono, since_wall=self.fault_since_wall
            )
            if res is None:
                if final or self.fault_unknown_sent:
                    return
                self.fault_unknown_sent = True
                self.emit(
                    Notice(
                        Msg(
                            "GPU faults can't be checked in this build; whether {engine} "
                            "faulted is unknown.",
                            {"engine": self.gpu_name()},
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
        reason = Msg("The GPU reported a fault in {engine}.", {"engine": self.gpu_name()})
        async with self.lock:
            if self.active_gen is not None and self.backend not in _SWITCHABLE:
                # the fault came from an earlier GPU-engine run (e.g. before the
                # switch to CPU smoothing); the engine running now is not to blame
                self.emit(
                    Notice(
                        Msg(
                            "The GPU reported a fault while {earlier} was running earlier; "
                            "{engine} is running now.",
                            {
                                "earlier": self.gpu_name(),
                                "engine": decide.BACKEND_NAMES.get(self.backend, "")
                                if self.backend
                                else "",
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
                    except Exception:
                        # B14: a bug or an unexpected OS error must not end the
                        # controller: mpv keeps playing, and the GUI would say
                        # "Lost the connection" about a player that answers
                        self.step_errors += 1
                        if self.step_errors <= 3:
                            self.log.exception("session %s: the controller step failed", self.sid)
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
        self.fill_logged = None
        self.fill_dropped = False
        self.target = self.multiplier = None
        self.backend, self.model = None, None
        self.profile_id = None
        self.applied_display = None
        self.bypass_display = None
        self.step_down_to = None
        self.drop_rate = None
        self.heavy_since = None
        self.steady_since = None
        self.video_limits = set()
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
        await self.poll_tick_props()

        if now - self.power_checked_at >= POWER_CHECK_S:
            self.power_checked_at = now
            power = self.read_power()
            if power != self.power:
                self.log.info(
                    "%s: power state now battery=%s low power=%s thermal=%s",
                    self.title, power.on_battery, power.low_power, power.thermal,
                )  # fmt: skip
            self.power = power
            await self.sync_shader(self.config()[0])  # P5: battery, heat, window size

        if self.compare_requested:
            self.compare_requested = False
            self.compare = not self.compare_on(self.config()[0])
            self.log.info("%s: A/B split view %s", self.title, "on" if self.compare else "off")
            if self.active_gen is not None and self.want and not self.busy:
                await self.apply(raise_on_fail=False)  # a new gen with the split

        if self.toggle_requested:
            self.toggle_requested = False  # acknowledged by on_event (B19)
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
            rate = verdict.drop_rate if verdict.health is Health.DROPPING else None
            if rate is not None and rate >= IMPLAUSIBLE_DROP:
                rate = None  # nothing reaches the screen: not the engine (see above)
            heavy = rate is not None and rate >= HEAVY_DROP
            steady = rate is not None and rate >= STEADY_DROP and self.auto_chosen
            if not heavy:
                self.heavy_since = None
            elif self.heavy_since is None:
                self.heavy_since = now
            if not steady:
                self.steady_since = None
            elif self.steady_since is None:
                self.steady_since = now
            self.note_clean(now, rate)
            replace = (self.auto_chosen or self.run_fill) and not self.busy and rate is not None
            if replace and (
                (self.heavy_since is not None and now - self.heavy_since >= HEAVY_HOLD_S)
                or (self.steady_since is not None and now - self.steady_since >= STEADY_HOLD_S)
            ):
                assert rate is not None
                learn = self.heavy_since is not None and now - self.heavy_since >= HEAVY_HOLD_S
                await self.live_fallback(rate, learn=learn)
                self.publish()
                await self.send_status()
                return
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

    def note_clean(self, now: float, rate: float | None) -> None:
        """B4: count clean playback by Automatic's engine; after
        ``CLEAN_RECOVER_S`` of it, a lowered estimate at this size gets one
        step back (``livelimits.raised``). Drops, heat and Low Power Mode start
        the count again; a pause only stops it."""
        last, self.clean_tick = self.clean_tick, now
        if not self.auto_chosen or self.backend is None or self.source is None:
            self.clean_s = 0.0
            return
        if rate is not None or self.health is not Health.OK or self.power.constrained:
            self.clean_s = 0.0
            return
        if last is None or self.props.get("pause") is True or not self.detector.flowing:
            return
        self.clean_s += min(max(0.0, now - last), 1.0)  # a stalled tick is not 10 s clean
        if self.clean_s < CLEAN_RECOVER_S:
            return
        self.clean_s = 0.0
        cfg, _ = self.config()
        size = self.run_size or (self.source.width, self.source.height)
        key = self.limit_key(self.backend, self.model, size, self.run_flow, self.gpu_device(cfg))
        limits = self.live_limits()
        old = livelimits.factor(limits, key)
        if old >= 1.0:
            return
        new = livelimits.raised(old)
        livelimits.record(
            self.ctx.paths.state_dir / livelimits.FILE, limits, key, new, power=self.power
        )
        self.log.info(
            "%s: %s ran %.0f min without drops at %s × %s; its estimate there goes "
            "from × %.2f to × %.2f",
            self.title, decide.BACKEND_NAMES.get(self.backend, self.backend.value),
            CLEAN_RECOVER_S / 60, size[0], size[1], old, new,
        )  # fmt: skip

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
        if self.gpu_used is not None:
            await self.check_gpu_faults(final=True)
        if returncode is not None:
            # mpv is gone: its socket and script are leftovers (discover would re-probe them)
            launcher.remove_runtime_files(self.socket.parent, self.sid)
            self.exit_code = returncode
            words = filelog.exit_words(returncode)
            if returncode == 0:
                self.log.info("%s: %s", self.title, words)
            else:
                self.log.warning("%s: %s", self.title, words)
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
        reg.recent[self.sid] = snap
        while len(reg.recent) > RECENT_KEPT:
            del reg.recent[next(iter(reg.recent))]
        if not self.keep_logs():  # opted out of logs: mpv's goes with the video
            with contextlib.suppress(OSError):
                self.log_path.unlink()


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
    # absolute before anything keeps it: mpv, the rules and the session's file
    # all outlive the caller's working folder, which is not mpv's or the core's
    file = Path(file).expanduser().absolute()
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
        general = ctx.config().general
    except ButterEyeError:
        general = None
    upscaling = general.upscaling if general is not None else "standard"
    shader = shaders.shader_for(upscaling)
    power = osapi.power_snapshot()
    if upscaling == "sharper" and not shaders.sharper_allowed(
        None, reduced_size=False, power=power
    ):
        shader = None  # P5: on battery or hot; the session adds it when that passes
    lut = None
    if general is not None and general.lut:
        from buttereye.core.profiles import luts

        lut = luts.resolve_lut(paths, general.lut)
    argv = launcher.build_argv(
        opts.mpv,
        include=paths.mpv_include,
        socket=socket,
        file=file,
        extra=opts.extra_args,
        shaders=(shader,) if shader is not None else (),
        lut=lut,
    )
    launcher.prune_logs(paths.logs_dir, runtime)
    launcher.prepare_socket(socket)
    reg.launching.add(socket)  # B23: not an orphan for discover() while it starts
    try:
        proc = await launcher.spawn_mpv(argv, log_path, env=opts.env)
        try:
            op.report(Msg("Connecting to mpv"))
            ipc = await launcher.connect(socket, proc, log_path, timeout_s=opts.connect_timeout_s)
            session = _Session(
                ctx, opts, sid=sid, file=file, proc=proc, ipc=ipc, socket=socket,
                log_path=log_path, script_path=script, profile_request=profile_id,
            )  # fmt: skip
            session.shader = shader  # added at launch
            session.lut = str(lut) if lut is not None else None
            session.power = power
            ipc.add_handler(session.on_event)
            ipc.start(lambda coro, name: ctx.spawn(coro, name=f"mpvctl-{sid}-ipc"))
            await ipc.command("client_name")
            await ipc.command("request_log_messages", "warn")
            # the per-frame ones are read on the tick (TICK_PROPS, P9)
            for prop in sorted(OBSERVED):
                await ipc.observe(prop)
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(launcher.terminate(proc, grace_s=2.0))
            launcher.remove_runtime_files(runtime, sid)
            raise
        reg.sessions[sid] = session
    finally:
        reg.launching.discard(socket)
    snap = session.snapshot()
    session.mark_published(snap)
    ctx.emit(SessionAdded(snap))
    session.task = ctx.spawn(session.run(), name=f"mpvctl-{sid}")
    return sid


async def sessions(ctx: ProviderContext) -> tuple[SessionSnapshot, ...]:
    """Live players only; an ended session was announced by ``SessionEnded``."""
    return tuple(s.snapshot() for s in registry(ctx).sessions.values() if not s.ended)


async def recent_sessions(ctx: ProviderContext) -> tuple[SessionSnapshot, ...]:
    """Recently ended sessions, newest first."""
    return tuple(reversed(registry(ctx).recent.values()))


async def recent_session(ctx: ProviderContext, sid: SessionId) -> SessionSnapshot | None:
    """A live session's snapshot, or the final one of a recently ended session."""
    reg = registry(ctx)
    live = reg.sessions.get(sid)
    if live is not None:
        return live.snapshot()
    return reg.recent.get(sid)


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
                s.filter_removed = not s.remove_pending
            with contextlib.suppress(Exception):
                await s.restore_player()  # picture, debanding, LUT, pacing
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
    held = {s.socket for s in reg.sessions.values() if not s.ended} | reg.launching
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

    def done(s: _Session) -> ErrorCode | None:
        # "turn smoothing off" is done only when the filter is gone: a detach
        # cut short before or during vf remove (or a player that did not answer
        # it) leaves @buttereye running in mpv
        off = s.filter_removed or (
            s.active_gen is None
            and not s.remove_pending
            and filter_entry(s.props.get("vf")) is None
        )
        if policy is DetachPolicy.DISABLE_FILTER and not off:
            return ErrorCode.IPC_LOST
        return None

    async def one(s: _Session) -> tuple[SessionId, ErrorCode | None]:
        try:
            await asyncio.wait_for(detach(ctx, s.sid, policy), timeout_s)
        except ButterEyeError as exc:
            return s.sid, exc.code
        except TimeoutError:
            # B24: detach ends the session in its own ``finally``, even when the
            # timeout cancels it; a session that did end was detached, only late
            return s.sid, done(s) if s.ended else ErrorCode.IPC_LOST
        except Exception:
            return s.sid, ErrorCode.IPC_LOST
        return s.sid, done(s)

    results = await asyncio.gather(*(one(s) for s in live))
    detached = tuple(sid for sid, code in results if code is None)
    failed = tuple((sid, code) for sid, code in results if code is not None)
    return ShutdownReport(detached, failed, ())


__all__ = [
    # one definition with the benchmark (bench.verdict imports only the types,
    # so the benchmark itself stays an optional provider)
    "LIVE_HEADROOM",
    "REALTIME_HEADROOM",
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
