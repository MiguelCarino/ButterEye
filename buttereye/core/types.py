# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Domain types that cross the core facade (docs/design/GUI.md §2.1, FROZEN).

Every type is a frozen, slotted dataclass or an enum. Editors derive new values
with ``dataclasses.replace``. No Qt type may appear here.

The one thing here that is not a type is where ``Paths``'s two packaged locations
default to (``homebrew_prefix``, ``macos_plugin_dir``, and on macOS the search
path ``macos_plugin_dirs`` the doctor asks). ``scriptgen`` has to give the
generated scripts the same answer, and this is the only module both it and the
field default can see: ``paths`` imports this one, not the other way round.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NewType

from buttereye.core import osapi

OpId = NewType("OpId", str)
SessionId = NewType("SessionId", str)
JobId = NewType("JobId", str)
CandidateId = NewType("CandidateId", str)


def _empty() -> Mapping[str, str | int | float]:
    return MappingProxyType({})


def _empty_str() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True, slots=True)
class Msg:  # translatable core text; key = English gettext msgid
    key: str
    params: Mapping[str, str | int | float] = field(default_factory=_empty)


# ---------- the packaged plugins and models (§7.1) ----------
#: Fedora: the RPMs patch VapourSynth's plugindir to this private directory, so
#: ButterEye's plugins are never autoloaded and a generated script names the copy
#: it loads. Windows overrides both fields from the install folder
#: (``paths._resolve_windows``); macOS answers below.
_FEDORA_PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")
_FEDORA_DATA_DIR = Path("/usr/share/buttereye")

#: Homebrew's prefix, Apple Silicon first. ``/usr/local`` exists on Macs that have
#: no Homebrew at all, which is why a prefix has to be recognised by the ``brew``
#: inside it rather than by being there.
_BREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))
#: Homebrew VapourSynth's autoload directory, relative to the prefix
#: (``_brew_autoload_dir`` says why the Python version is a wildcard)
_BREW_AUTOLOAD_GLOB = "lib/python3.*/site-packages/vapoursynth/plugins"


def homebrew_prefix() -> Path:
    """Homebrew's prefix on this Mac.

    ``HOMEBREW_PREFIX`` first: a brew shell exports it, and it is the only way a
    prefix nobody could guess can be known. It cannot be the whole answer, because
    an app launched from Finder inherits neither that variable nor a PATH that
    would find ``brew`` (measured: ``launchctl getenv PATH`` answers nothing), so
    the two canonical prefixes are then tried on disk.

    With no Homebrew anywhere the answer is ``/opt/homebrew``: the Apple Silicon
    prefix, and the only one measured (docs/PLATFORMS.md). Nothing is found under
    it, which is the truth about such a machine, where a Linux path would be a
    wrong answer wearing the shape of a right one.
    """
    value = os.environ.get("HOMEBREW_PREFIX", "")
    if value and os.path.isabs(value):
        return Path(value)
    for prefix in _BREW_PREFIXES:
        if (prefix / "bin" / "brew").exists():
            return prefix
    return _BREW_PREFIXES[0]


def _python_minor(plugins_dir: Path) -> int:
    """The minor version in ``…/python3.<minor>/site-packages/vapoursynth/plugins``."""
    minor = plugins_dir.parts[-4].removeprefix("python3.")
    return int(minor) if minor.isdigit() else -1


def _brew_autoload_dir(prefix: Path) -> Path | None:
    """Homebrew VapourSynth's autoload directory under ``prefix``, or None.

    The ``vapoursynth`` formula installs into a virtualenv of its own under
    ``libexec`` and then symlinks ``<prefix>/lib/python3.<minor>/site-packages/
    vapoursynth/plugins`` into it, under the comment "Automatically load plugins
    installed in separate formulae". That linked directory is what the core scans,
    and it is where each plugin formula (``vapoursynth-mvtools``, ``ffms2``) drops
    its ``.dylib``. The Python minor version is whichever ``python@3.x`` the
    formula depends on today, so it is globbed rather than written down; a
    directory left behind by an earlier one is possible, so the newest wins.
    """
    try:
        found = [d for d in prefix.glob(_BREW_AUTOLOAD_GLOB) if d.is_dir()]
    except OSError:
        return None
    return max(found, key=_python_minor, default=None)


def macos_plugin_dirs() -> tuple[Path, ...]:
    """Every directory a VapourSynth plugin ButterEye drives can be in on this
    Mac, in the order a load resolves them.

    macOS is the one platform where "where is this plugin" has more than one
    answer, and the two are not a fallback chain - a Mac with the tap installed
    has plugins in both at once. Homebrew VapourSynth's autoload directory holds
    ``vapoursynth-mvtools``'s ``mvtools.dylib`` and ``ffms2``'s
    ``libffms2.dylib``, because that is where those formulae install and a
    formula cannot install into another formula's keg. ButterEye's own
    ``<prefix>/lib/buttereye/vapoursynth`` holds what the ButterEye tap and
    packaging/macos/build-plugins.sh build: ``librife.dylib``,
    ``libmvgpu.dylib``. Fedora and Windows get away with one directory because
    the RPM patches VapourSynth's plugindir and the installer owns the folder.

    The autoload directory comes first because that copy is the one that wins. A
    plugin there is in the core before a generated script runs, so ``_load`` in
    template.vpy skips its ``LoadPlugin`` (a second copy of a namespace fails)
    and the clip goes through the autoloaded file whichever directory ButterEye
    would otherwise have named. Asking in load order is what stops the doctor
    reporting one copy while the script uses another.

    Answering one directory was measured to ship a false BLOCKING doctor row:
    installing any tap plugin formula creates ButterEye's own directory,
    ``macos_plugin_dir`` stopped falling back to the autoload one, and
    ``packages.mvtools`` went degraded with ``packages.plugins`` blocking on a
    Mac where Homebrew's autoloaded MVTools was interpolating
    (packaging/macos/homebrew/buttereye-vs-mvgpu.rb records both runs).
    """
    app = osapi.MACOS_APP
    if app is not None:
        # ButterEye.app: one directory, the app's own, as on Windows. Homebrew's
        # autoload directory is not asked even when this Mac has one - the
        # app's VapourSynth does not autoload from it, and a plugin found there
        # is not a plugin the app would run.
        return (app / "vapoursynth",)
    prefix = homebrew_prefix()
    own = prefix / "lib" / "buttereye" / "vapoursynth"
    autoload = _brew_autoload_dir(prefix)
    return (autoload, own) if autoload is not None else (own,)


def macos_plugin_dir() -> Path:
    """The one directory a generated script names on macOS (its ``PLUGIN_DIR``).

    Not the answer to "where is this plugin" - that is ``macos_plugin_dirs``,
    and on a Mac it is two directories. This is the narrower question a ``.vpy``
    asks, which has room for exactly one absolute path per plugin.

    ButterEye's own ``<brew prefix>/lib/buttereye/vapoursynth`` when it is there,
    the analogue of Fedora's ``%{_libdir}/buttereye/vapoursynth`` and of the
    Windows installer's ``<install>\\vapoursynth``: ButterEye keeps a private,
    non-autoloaded directory on every platform, so a script names the copy it
    loads instead of inheriting whatever the system autoloads (§7.1, and the
    reason the RPM patches plugindir). The Homebrew tap of phase 4 fills it.

    Until something does, Homebrew VapourSynth's autoload directory, which on a
    Mac with the bottles really holds a ``mvtools.dylib`` that interpolates. It
    is a fallback and not the answer: a plugin there is already autoloaded, so
    ``_load`` in the generated scripts skips the ``LoadPlugin``, and the version
    is Homebrew's choice rather than one ButterEye packaged and measured.

    With neither, ButterEye's own directory, so that every caller names the place
    a tap would install into rather than a directory nobody owns.

    ButterEye.app answers before any of that: its own ``Contents/Resources/
    vapoursynth``, the one directory it loads plugins from (``osapi.MACOS_APP``).

    Naming ButterEye's own directory while Homebrew's MVTools sits in the
    autoload one costs a script nothing - ``_load`` skips a namespace
    VapourSynth has already autoloaded - which is why this stayed one directory
    and only the doctor needed the search path beside it.
    """
    if osapi.MACOS_APP is not None:
        return osapi.MACOS_APP / "vapoursynth"
    prefix = homebrew_prefix()
    own = prefix / "lib" / "buttereye" / "vapoursynth"
    if own.is_dir():
        return own
    return _brew_autoload_dir(prefix) or own


def _packaged_plugin_dir() -> Path:
    return macos_plugin_dir() if osapi.IS_MACOS else _FEDORA_PLUGIN_DIR


def _packaged_data_dir() -> Path:
    """Where the RIFE models live. On macOS ``<brew prefix>/share/buttereye``, the
    analogue of Fedora's ``%{_datadir}/buttereye``: no fallback, because Homebrew
    has no models of its own to find, so nothing is there until a tap ships them.
    ButterEye.app carries its own, in ``Contents/Resources/share``."""
    if osapi.IS_MACOS:
        if osapi.MACOS_APP is not None:
            return osapi.MACOS_APP / "share"
        return homebrew_prefix() / "share" / "buttereye"
    return _FEDORA_DATA_DIR


# ---------- paths (§4.6) ----------
@dataclass(frozen=True, slots=True)
class Paths:
    config_file: Path  # $XDG_CONFIG_HOME/buttereye/config.toml
    mpv_include: Path  # $XDG_CONFIG_HOME/buttereye/mpv/buttereye.conf
    user_mpv_conf: Path  # ~/.config/mpv/mpv.conf (read; written only via consented include)
    data_dir: Path  # $XDG_DATA_HOME/buttereye
    cache_dir: Path  # $XDG_CACHE_HOME/buttereye
    state_dir: Path  # $XDG_STATE_HOME/buttereye
    logs_dir: Path
    jobs_dir: Path
    bench_file: Path
    runtime_dir: Path  # $XDG_RUNTIME_DIR/buttereye (0700, verified, never chmod-ed)
    # the name is Fedora-shaped; the field is "wherever this platform's packaged
    # plugins and models are" (Windows: the install folder, macOS: Homebrew's prefix)
    rpm_plugin_dir: Path = field(default_factory=lambda: _packaged_plugin_dir())
    rpm_data_dir: Path = field(default_factory=lambda: _packaged_data_dir())


# ---------- enums ----------
class Severity(StrEnum):
    BLOCKING = "blocking"
    DEGRADED = "degraded"
    OK = "ok"
    INFO = "info"


class Section(StrEnum):
    MPV = "mpv"
    PROBE = "probe"
    PACKAGES = "packages"
    VULKAN = "vulkan"
    GPU_FAULT = "gpu_fault"
    CONFLICTS = "conflicts"
    RENDER = "render"
    CONFIG = "config"
    TRT = "trt"


class BackendId(StrEnum):
    RIFE_NCNN = "rife-ncnn"
    MVTOOLS = "mvtools"
    RIFE_TRT = "rife-trt"
    MVGPU = "mvgpu"  # GPU block motion vectors (Vulkan), mvgpu/


class HdrClass(StrEnum):
    SDR = "sdr"
    HDR10 = "hdr10"
    HDR10PLUS = "hdr10plus"
    HLG = "hlg"
    DV = "dv"


class TargetKind(StrEnum):
    DISPLAY = "display"  # lowest refresh/k that doubles the source (§5.6)
    DISPLAY_MAX = "display-max"  # highest refresh/k the GPU sustains (max smoothness)
    X2 = "2x"
    FPS = "fps"


class Origin(StrEnum):
    LAUNCHED = "launched"
    ATTACHED = "attached"


class FilterState(StrEnum):
    OFF = "off"
    PENDING = "pending"  # waiting for display-fps>0 and video-params (§4.2.2)
    ACTIVE = "active"  # vf + gen verified AND frames flowing
    BYPASSED = "bypassed"
    ROLLED_BACK = "rolled_back"


class BypassReason(StrEnum):  # §4.11 rows
    ALREADY_AT_RATE = "already_at_rate"
    INTERLACED = "interlaced"
    HDR_SKIP = "hdr_skip"
    UNSUPPORTED_FORMAT = "unsupported_format"
    NO_VIDEO = "no_video"
    NO_REALTIME = "no_realtime"


class Health(StrEnum):
    OK = "ok"
    DROPPING = "dropping"
    STALLED = "stalled"
    GPU_FAULT = "gpu_fault"
    DEVICE_LOST = "device_lost"
    CONNECTION_LOST = "connection_lost"


class RefusalReason(StrEnum):
    NOT_SOCKET = "not_socket"
    WRONG_UID = "wrong_uid"
    BAD_PARENT = "bad_parent"
    PEERCRED = "peercred"
    MPV_TOO_OLD = "mpv_too_old"
    NO_VS_FILTER = "no_vs_filter"
    SANDBOXED = "sandboxed"


class DetachPolicy(StrEnum):
    KEEP_FILTER = "keep"
    DISABLE_FILTER = "disable"


class OpKind(StrEnum):
    DOCTOR = "doctor"
    SETUP = "setup"
    PLAY = "play"
    BENCH = "bench"
    RENDER = "render"
    MODEL_INSTALL = "model_install"
    CLEAN = "clean"
    REPORT = "report"
    ENGINE_BUILD = "engine_build"


class OpState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Feature(StrEnum):
    CONFIG = "config"
    DOCTOR = "doctor"
    HARDWARE = "hardware"
    SETUP = "setup"
    REPORT = "report"
    DISCOVER = "discover"
    LIVE = "live"
    ATTACH = "attach"
    ORPHANS = "orphans"
    BENCH = "bench"
    RENDER = "render"
    RENDER_HDR10 = "render_hdr10"
    STORAGE = "storage"
    CLEAN = "clean"
    MODELS = "models"
    MODEL_DOWNLOADS = "model_downloads"
    LICENCES = "licences"
    TRT = "trt"
    HDR_PASSTHROUGH = "hdr_passthrough"


class Reason(StrEnum):
    NOT_IMPLEMENTED = "not_implemented"  # this build lacks the code path
    MISSING_DEPENDENCY = "missing_dependency"  # environment; carries ErrorCode + dnf line
    BLOCKED_BY_DOCTOR = "blocked"
    OPT_IN_REQUIRED = "opt_in"
    SPIKE_PENDING = "spike_pending"
    NOTHING_LISTED = "nothing_listed"  # e.g. manifest has no downloadable models


class CleanTarget(StrEnum):
    ENGINES = "engines"
    MODELS = "models"
    PLUGINS = "plugins"
    DOWNLOADS = "downloads"
    JOBS = "jobs"


class StorageKind(StrEnum):
    CONFIG = "config"
    ENGINES = "engines"
    MODELS = "models"
    PLUGINS = "plugins"
    DOWNLOADS = "downloads"
    JOBS = "jobs"
    LOGS = "logs"
    BENCH = "bench"
    RUNTIME = "runtime"
    RPM = "rpm"


class ModelKind(StrEnum):
    PACKAGED = "packaged"
    DOWNLOADED = "downloaded"
    EXTRA = "extra"
    UNPINNED = "unpinned"


class RenderPhase(StrEnum):
    PROBE = "probe"
    RENDER = "render"
    REMUX = "remux"
    CLEANUP = "cleanup"


# ---------- config (§4.9, F15) ----------
@dataclass(frozen=True, slots=True)
class Target:
    kind: TargetKind
    fps: Fraction | None = None  # required iff kind is FPS


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    backend: BackendId | Literal["auto"]
    model: str | None
    scale: float | None  # vs-mlrt only; ncnn has uhd, read by v1-v3 models only (§5.3)
    target: Target
    sc_threshold: float
    buffered_frames: int | None  # None = §4.4 default
    concurrent_frames: int | None
    hdr: Literal["skip", "passthrough"] = "skip"
    builtin: bool = False


@dataclass(frozen=True, slots=True)
class RuleMatch:
    fps_min: Fraction | None = None
    fps_max: Fraction | None = None
    width_max: int | None = None
    height_max: int | None = None
    hdr_class: HdrClass | None = None
    display_hz_min: float | None = None
    interlaced: bool | None = None
    path_glob: str | None = None


@dataclass(frozen=True, slots=True)
class Rule:
    match: RuleMatch
    profile: str  # profile id


#: what the mpv OSD can show (GeneralSettings.osd_items), in display order
OSD_ITEMS: tuple[str, ...] = ("rates", "engine", "size", "health")


@dataclass(frozen=True, slots=True)
class GeneralSettings:
    backend_override: BackendId | None = None
    gpu: str | None = None  # Vulkan device UUID
    language: str | None = None
    #: §5.2: None (unset) = use TensorRT when it is set up (building vstrt with
    #: contrib/build-vstrt.sh is the opt-in, owner decision 2026-10-10); True = the
    #: Setup opt-in (also plans its downloads); False = turned off
    trt_experimental: bool | None = None
    #: display upscaling (§15.2, spike M0(o)): mpv's own scaler, or the bundled
    #: FSRCNNX shader on top of it ("sharper")
    upscaling: Literal["standard", "sharper"] = "standard"
    #: mpv's own debanding (libplacebo, ~0 GPU cost) switched on by ButterEye (§15.2)
    deband: bool = False
    #: never smooth live at a smaller size to keep up; smooth at the video's own
    #: size even if frames may drop (the user's choice over the speed test)
    full_size: bool = False
    #: mpv on-screen info when smoothing starts or changes (GUI.md §12.1.5):
    #: seconds shown, 0 = off, -1 = always; and which details
    osd_seconds: float = 1.0
    osd_items: tuple[str, ...] = ("rates", "engine")
    #: per-file logs to copy (Copy log, closed rows, More logs); off = opted out:
    #: no log buttons, and each mpv log is deleted when the video closes
    keep_logs: bool = True


@dataclass(frozen=True, slots=True)
class RenderDefaults:
    encoder_by_vendor: Mapping[str, str] = field(default_factory=_empty_str)
    container: Literal["mkv"] = "mkv"
    audio: Literal["copy"] = "copy"


@dataclass(frozen=True, slots=True)
class Config:
    schema_version: int
    general: GeneralSettings
    profiles: tuple[Profile, ...]  # built-ins included, builtin=True
    rules: tuple[Rule, ...]
    render: RenderDefaults
    unknown: Mapping[str, object]  # preserved verbatim on save (§4.9)


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    code: ErrorCode
    message: Msg
    line: int | None = None
    column: int | None = None
    field: str | None = None  # dotted path, e.g. "profiles[3].sc_threshold"


@dataclass(frozen=True, slots=True)
class ConfigLoad:
    config: Config
    exists: bool  # False -> first run (§4.7)
    revision: str  # sha256 of file bytes; "" if no file
    read_only: bool  # newer schema_version
    used_defaults: bool  # invalid TOML -> defaults in use
    issues: tuple[ConfigIssue, ...]


@dataclass(frozen=True, slots=True)
class SourceFacts:
    fps: Fraction
    width: int
    height: int
    hdr_class: HdrClass
    display_hz: float
    interlaced: bool
    path: str
    vfr: bool = False


@dataclass(frozen=True, slots=True)
class RuleStep:
    index: int
    matched: bool
    why: Msg  # "fps 60 > fps_max 30"


@dataclass(frozen=True, slots=True)
class RuleTrace:
    matched_index: int | None  # None -> no rule matched, default profile
    profile_id: str
    steps: tuple[RuleStep, ...]  # evaluated rules up to and including the match


# ---------- doctor / hardware (F1, F9, F21) ----------
@dataclass(frozen=True, slots=True)
class Finding:
    id: str  # stable check id, e.g. "mpv.vf_vapoursynth"
    section: Section
    severity: Severity
    code: ErrorCode | None  # required for BLOCKING/DEGRADED
    title: Msg
    cause: Msg
    fix: Msg
    commands: tuple[str, ...] = ()  # exact copyable lines; never executed
    evidence: tuple[str, ...] = ()  # raw lines (Xid, stderr tail); untranslated
    experimental: bool = False


@dataclass(frozen=True, slots=True)
class GpuInfo:
    pci: str
    vendor: str
    name: str
    driver: str | None
    vram_bytes: int | None
    compute_cap: str | None  # NVIDIA only, from nvidia-smi subprocess


@dataclass(frozen=True, slots=True)
class VulkanDevice:
    index: int
    uuid: str
    name: str
    vendor: str
    device_type: Literal["discrete", "integrated", "virtual", "cpu", "other"]
    heap_bytes: int | None


@dataclass(frozen=True, slots=True)
class HardwareInfo:
    gpus: tuple[GpuInfo, ...]
    vulkan: tuple[VulkanDevice, ...]
    interpolation_device: str | None  # Vulkan UUID; never a cpu device
    decode_device: str | None  # pci; warn if != interpolation (§4.5)
    cpu_threads: int


@dataclass(frozen=True, slots=True)
class PluginStatus:
    name: str
    package: str | None
    version: str | None
    active_copy: Literal["buttereye", "distro", "user-vstrt", "none"]
    loads_in_mpv: bool | None  # None = probe not run
    variant: str | None  # e.g. "bundled ncnn 0^20250503git305837fd"
    licence_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class ProbeResult:
    vs_core: str
    python: str
    plugins: tuple[PluginStatus, ...]
    vsmlrt_importable: bool


@dataclass(frozen=True, slots=True)
class DoctorReport:
    findings: tuple[Finding, ...]
    hardware: HardwareInfo
    probe: ProbeResult | None
    duration_s: float
    trt_included: bool

    @property
    def blocking(self) -> bool:
        return any(f.severity is Severity.BLOCKING for f in self.findings)


@dataclass(frozen=True, slots=True)
class BackendStatus:
    id: BackendId
    available: bool
    experimental: bool
    reason: Msg  # why (un)available, plain language
    code: ErrorCode | None = None


@dataclass(frozen=True, slots=True)
class Selection:  # §5.3, F13
    backend: BackendId | None
    model: str | None
    profile_id: str
    reason: Msg
    from_benchmark: bool
    ranked: tuple[BackendId, ...]


# ---------- setup (F0, §4.7) ----------
@dataclass(frozen=True, slots=True)
class DownloadItem:
    name: str
    url: str
    size_bytes: int
    licence: str
    sha256: str
    pinned: bool


@dataclass(frozen=True, slots=True)
class SetupPlan:
    report: DoctorReport
    proposed: Selection
    backends: tuple[BackendStatus, ...]
    missing_packages: tuple[str, ...]
    dnf_lines: tuple[str, ...]
    downloads: tuple[DownloadItem, ...]  # empty unless TRT opted in


@dataclass(frozen=True, slots=True)
class SetupChoices:
    backend: BackendId
    trt_experimental: bool
    confirmed_downloads: frozenset[str]  # DownloadItem.name
    run_smoke_test: bool = True


@dataclass(frozen=True, slots=True)
class SetupResult:
    config_revision: str
    smoke_ok: bool
    smoke_fps: float | None
    smoke_finding: Finding | None
    include_line: str  # "include=~/.config/buttereye/mpv/buttereye.conf"


# ---------- live (F2-F4, F8, §4.3, §4.10, §4.11) ----------
@dataclass(frozen=True, slots=True)
class Counters:
    frame_drop: int
    decoder_drop: int
    vo_delayed: int
    mistimed: int
    display_sync: bool


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    sid: SessionId
    origin: Origin
    pid: int | None
    title: str  # basename, redaction-aware
    source: SourceFacts | None
    display_fps: float | None
    target_fps: Fraction | None
    multiplier: Fraction | None
    backend: BackendId | None
    model: str | None
    profile_id: str | None
    filter: FilterState
    bypass: BypassReason | None
    health: Health
    health_code: ErrorCode | None
    gen: int
    counters: Counters
    drop_rate_10s: float | None
    step_down_to: str | None  # profile id offered (§4.11), None = no offer
    notice: Msg | None  # e.g. "TRT engine building; using RIFE-ncnn"
    restore_pending: tuple[str, ...]  # attach settings to restore (§4.3)
    ended: bool
    #: what the running filter does (GUI rows say it): the size RIFE/MVTools work
    #: at (None = the video's own), RIFE TensorRT's flow resolution (M0(q)) and
    #: MVTools' block mode (M0(p))
    smooth_size: tuple[int, int] | None = None
    flow_scale: float = 1.0
    mv_block: bool = False
    #: how mpv ended (once ``ended``): its exit code, negative = killed by that
    #: signal (a crash); None while it runs or when ButterEye only let it go
    exit_code: int | None = None


@dataclass(frozen=True, slots=True)
class InstanceCandidate:
    id: CandidateId
    socket: Path
    pid: int | None
    title: str | None
    refused: RefusalReason | None
    refusal: Msg | None
    orphan_filter: bool
    managed_by_us: bool


@dataclass(frozen=True, slots=True)
class ShutdownReport:
    detached: tuple[SessionId, ...]
    failed: tuple[tuple[SessionId, ErrorCode], ...]
    jobs_cancelled: tuple[JobId, ...]


# ---------- capabilities ----------
@dataclass(frozen=True, slots=True)
class CapState:
    available: bool
    reason: Reason | None = None
    code: ErrorCode | None = None
    message: Msg | None = None  # user-facing why-not (§5 wording)
    commands: tuple[str, ...] = ()  # e.g. ("sudo dnf install ffms2",)


@dataclass(frozen=True, slots=True)
class Capabilities:
    states: Mapping[Feature, CapState]

    def ok(self, f: Feature) -> bool:
        return self.states[f].available


# ---------- bench (F14) ----------
@dataclass(frozen=True, slots=True)
class BenchRequest:
    width: int
    height: int
    source_fps: Fraction
    target_fps: Fraction | None = None
    full: bool = False
    user_file: Path | None = None


@dataclass(frozen=True, slots=True)
class BenchMeasurement:
    label: str
    backend: BackendId
    model: str | None
    vspipe_fps: float
    mpv_fps: float  # selection uses mpv_fps (§5.3)
    cov: float
    repeatable: bool  # cov < 0.05 over 3 runs after warm-up
    startup_s: float
    reload_s: float
    vram_bytes: int | None
    realtime: bool
    gpu_faults: int | None  # None = journal unreadable


@dataclass(frozen=True, slots=True)
class BenchResult:
    request: BenchRequest
    measurements: tuple[BenchMeasurement, ...]
    recommended: str | None
    when: datetime
    gpu_uuid: str | None


# ---------- render (§7, F16) ----------
@dataclass(frozen=True, slots=True)
class StreamInfo:
    index: int
    kind: Literal["video", "audio", "subtitle", "attachment", "data"]
    codec: str
    title: str | None


@dataclass(frozen=True, slots=True)
class RenderSize:
    """One size a render can be made at (§7.8)."""

    width: int
    height: int
    est_fps: float | None  # estimated output fps at 2× (decide.load_rate); None = no benchmark


@dataclass(frozen=True, slots=True)
class RenderProbe:
    source: Path
    hdr_class: HdrClass
    vfr: bool
    duration_s: float
    streams: tuple[StreamInfo, ...]
    chapters: int
    encoders: tuple[str, ...]  # probed `ffmpeg -encoders`, HDR10-filtered (§7.4/7.5)
    remux_tool: Literal["mkvmerge", "ffmpeg"]
    free_bytes: int
    warnings: tuple[Finding, ...]  # VFR->CFR (§7.3), mkvmerge missing
    refusal: Finding | None  # HLG/DV (§7.4), codec not decodable (§7.7 #7)
    # source facts and the sizes it can be made at (§7.8); defaults keep old data valid
    width: int = 0
    height: int = 0
    fps: Fraction | None = None
    sizes: tuple[RenderSize, ...] = ()  # source size first, then smaller ones


@dataclass(frozen=True, slots=True)
class RenderJobSpec:
    source: Path
    output: Path
    profile_id: str
    encoder: str
    overwrite: bool = False
    target: Target | None = None  # None = the profile's; DISPLAY renders as 2x (§7.8)
    size: tuple[int, int] | None = None  # None = the source size (§7.8)


@dataclass(frozen=True, slots=True)
class RenderJobState:
    id: JobId
    spec: RenderJobSpec
    state: OpState
    phase: RenderPhase | None
    done_frames: int | None
    total_frames: int | None
    fps: float | None
    eta_s: float | None
    est_bytes: int | None
    error: ButterEyeError | None


@dataclass(frozen=True, slots=True)
class StaleJob:
    id: JobId
    pgid: int | None
    alive: bool
    partial_files: tuple[Path, ...]


# ---------- storage / models / licences (§4.6, F10, F18) ----------
@dataclass(frozen=True, slots=True)
class StorageEntry:
    kind: StorageKind
    path: Path
    bytes: int | None  # None = not measurable
    deletable: bool
    rpm_owned: bool
    note: Msg
    clean_target: CleanTarget | None


@dataclass(frozen=True, slots=True)
class CleanResult:
    freed_bytes: int
    removed: tuple[Path, ...]
    skipped: tuple[tuple[Path, Msg], ...]


@dataclass(frozen=True, slots=True)
class ModelEntry:
    name: str
    kind: ModelKind
    backend: BackendId
    path: Path | None
    installed: bool
    size_bytes: int | None
    licence: str
    sha256: str | None
    removable: bool  # False for PACKAGED


@dataclass(frozen=True, slots=True)
class ComponentLicence:
    name: str
    version: str | None
    spdx: str
    relation: Msg  # "separate program", "loaded by mpv/vspipe", "in-process"
    conveyed: bool  # "yes (COPR)" rows of §8.2
    detected: bool
    text_files: tuple[Path, ...]
    finding: Finding | None  # missing %license -> DEGRADED


@dataclass(frozen=True, slots=True)
class LegalNotices:
    name: str
    version: str
    copyright: str  # "Copyright (C) 2026 The ButterEye contributors"
    licence_name: str  # "GNU Affero General Public License v3.0 or later"
    no_warranty: Msg
    agpl_text: Path  # shipped COPYING
    output_permission_text: Path  # LICENSES/AdditionRef-ButterEye-generated-output.txt
    source_link: str  # COPR SRPM URL until a repository exists
    missing: tuple[Path, ...]  # files not found -> shown, never hidden


@dataclass(frozen=True, slots=True)
class CommandHint:
    argv: tuple[str, ...]  # ("buttereye", "render", "IN", "-o", "OUT")
    status: Literal["in_scope", "proposed"]

    def text(self) -> str:  # display only
        return shlex.join(self.argv)


# Runtime names for annotation resolution (typing.get_type_hints). Imported last
# because errors.py itself refers to names defined above.
from buttereye.core.errors import ButterEyeError, ErrorCode  # noqa: E402

__all__ = [
    "OSD_ITEMS",
    "OpId",
    "SessionId",
    "JobId",
    "CandidateId",
    "Msg",
    "Paths",
    "homebrew_prefix",
    "macos_plugin_dir",
    "macos_plugin_dirs",
    "Severity",
    "Section",
    "BackendId",
    "HdrClass",
    "TargetKind",
    "Origin",
    "FilterState",
    "BypassReason",
    "Health",
    "RefusalReason",
    "DetachPolicy",
    "OpKind",
    "OpState",
    "Feature",
    "Reason",
    "CleanTarget",
    "StorageKind",
    "ModelKind",
    "RenderPhase",
    "Target",
    "Profile",
    "RuleMatch",
    "Rule",
    "GeneralSettings",
    "RenderDefaults",
    "Config",
    "ConfigIssue",
    "ConfigLoad",
    "SourceFacts",
    "RuleStep",
    "RuleTrace",
    "Finding",
    "GpuInfo",
    "VulkanDevice",
    "HardwareInfo",
    "PluginStatus",
    "ProbeResult",
    "DoctorReport",
    "BackendStatus",
    "Selection",
    "DownloadItem",
    "SetupPlan",
    "SetupChoices",
    "SetupResult",
    "Counters",
    "SessionSnapshot",
    "InstanceCandidate",
    "ShutdownReport",
    "CapState",
    "Capabilities",
    "BenchRequest",
    "BenchMeasurement",
    "BenchResult",
    "StreamInfo",
    "RenderProbe",
    "RenderSize",
    "RenderJobSpec",
    "RenderJobState",
    "StaleJob",
    "StorageEntry",
    "CleanResult",
    "ModelEntry",
    "ComponentLicence",
    "LegalNotices",
    "CommandHint",
]
