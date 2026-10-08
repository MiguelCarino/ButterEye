# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Domain types that cross the core facade (docs/design/GUI.md §2.1, FROZEN).

Every type is a frozen, slotted dataclass or an enum. Editors derive new values
with ``dataclasses.replace``. No Qt type may appear here.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, NewType

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
    rpm_plugin_dir: Path = Path("/usr/lib64/buttereye/vapoursynth")
    rpm_data_dir: Path = Path("/usr/share/buttereye")


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


@dataclass(frozen=True, slots=True)
class GeneralSettings:
    backend_override: BackendId | None = None
    gpu: str | None = None  # Vulkan device UUID
    language: str | None = None
    trt_experimental: bool = False  # §5.2 opt-in


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
    "OpId",
    "SessionId",
    "JobId",
    "CandidateId",
    "Msg",
    "Paths",
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
