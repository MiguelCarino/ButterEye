# ButterEye v1 GUI design (M4)

Status: authoritative for M4 implementation. Date: 2026-10-07.
Sources: `docs/SCOPE.md` (cited as §/F), `docs/spikes/m0f.md`, checks run on the dev box today.
Synthesized from three candidate designs (user-first, architecture-first, risk-first). §0 records the scoring and every conflict ruling. Sections 2–4 are **frozen contracts**: implementers do not change a signature without a recorded amendment in §10.

---

## 0. Scoring and rulings

### 0.1 Scores (1–10)

| Criterion | user-first | architecture-first | risk-first |
|---|---|---|---|
| SCOPE fidelity (F15/F17/F18/M4, §4.10, §4.7) | 8 | 6 | 8 |
| Buildable now without faking data | 5 | 9 | 6 |
| Testability | 8 | 9 | 9 |
| Accessibility | 9 | 7 | 8 |
| Risk handling | 7 | 7 | 10 |
| Simplicity | 8 | 5 | 6 |
| **Total** | **45** | **43** | **47** |

**Winner: risk-first.** It is the base for the contract, bridge and test plan. Grafted in:

- From **architecture-first**: capability gating with `NOT_IMPLEMENTED` that refuses before any I/O (the only honest way to ship a GUI over an unbuilt core); `Fraction` rates; config revision as a content hash; a single command-hint table for CLI parity; the `StatePanel` four-state rule.
- From **user-first**: the plain-language state texts; drop-zone and running-player affordances; rule tester pre-filled from the live session; focus-frame fallback; chart/table pairing; `QAccessibleAnnouncementEvent` for state changes; consent-gated `include=` writer.

Rationale. user-first has the best UX but assumes a working engine and uses mutable `Profile`/`Config` across threads. architecture-first is the most buildable but contradicts SCOPE twice (see 0.2) and doubles the string tables. risk-first is the only design that treats the m0f failure mode (audio over a frozen picture, Xid 13) and mpv lifetime as first-class, but it claims live play is "real now" when no core exists.

### 0.2 Conflict rulings (verified)

| # | Conflict | Ruling | Evidence |
|---|---|---|---|
| R1 | QtAsyncio usable? | **No.** Thread bridge is the only design (§3). | `PySide6/QtAsyncio/events.py` 6.11.2: `subprocess_exec`, `subprocess_shell`, `create_connection`, `create_unix_connection`, `add_reader/writer`, `connect_read_pipe`, all `sock_*`, `add_signal_handler` raise `NotImplementedError`. `asyncio.create_subprocess_exec` in a non-main thread on 3.14.7 works (tested: rc 0). |
| R2 | Desktop | **Hyprland** (`XDG_CURRENT_DESKTOP=Hyprland`), not COSMIC as the task text says. No Qt platform theme by default; Fusion. | env on dev box |
| R3 | lupdate | **Unusable.** `PySide6/lupdate` and `lrelease` are dangling symlinks into `/usr/lib64/qt6/bin/` (no `qt6-linguist`). Catalogue extracted by a stdlib AST tool (`tools/extract_ts.py`). architecture-first was wrong. | `ls /usr/lib64/qt6/bin` |
| R4 | Writing `include=` into `~/.config/mpv/mpv.conf` | **Offered, explicit consent, one line, diff shown** (§4.6 "may offer … with explicit consent", §4.7.5). architecture-first ("never writes") was wrong. F2's byte-identical check applies to `play`, not to the consented action. | §4.6, §4.7 |
| R5 | Where core text is translated | **Core gettext** (§2 goal 9 names `doctor` messages under gettext). Core returns `Msg(key, params)`; GUI renders it with `api.render(msg)`. The GUI's own strings use `tr()`. No GUI-side MsgId table. | §2 goal 9 |
| R6 | Mutable vs frozen domain types | **All frozen, slotted.** Editors use `dataclasses.replace`. | thread safety |
| R7 | Session API shape | **Flat facade keyed by `SessionId`** (no `LiveSession` objects whose methods would be called off-loop). | bridge invariant |
| R8 | Setup UI | **`QWizard`, `ClassicStyle`**, modal over the main window. Standard Back/Next/Cancel semantics Orca already knows; less code than a custom stepper. | simplicity |
| R9 | Models/Plugins page | **No separate page.** Models (F10) live on Storage; plugin status (active copy) on System. 7 pages. | simplicity, F17 still met |
| R10 | Allowed PySide6 modules | **{`PySide6`, `QtCore`, `QtGui`, `QtWidgets`}** in the GUI process. `QtTest` only inside tests. `QtNetwork` and `QtDBus` banned (F20; not needed). | F17, F20 |
| R11 | Which events reach the GUI | One ordered event stream + per-op coalesced progress (risk-first), GUI-side drain timer 100 ms; live stats ≤ 4 Hz. | flood control |
| R12 | Stall/GPU-fault detection | **Core-side** (`mpvctl/health.py`, `doctor/gpufault.py`), automatic rollback on STALLED, never auto re-enable. GUI never infers health. Thresholds are M1 core work. | m0f, F1 Xid line |
| R13 | GUI may spawn processes? | **No.** `gui/` imports no `subprocess`/`socket`/`asyncio` subprocess API. Opening folders uses `QDesktopServices`. | single owner of processes |
| R14 | Single GUI instance | `fcntl.flock` on `$XDG_RUNTIME_DIR/buttereye/gui.lock`; second launch prints and shows "ButterEye is already open." then exits. | two-controller risk |
| R15 | SCOPE ncnn drift | **Already resolved.** SCOPE §8.2, F1 (Xid), F10, §4.4 (`gpu_thread=4`) and Appendix B "bundled ncnn" reflect the bundled build. Licences pane lists ncnn + glslang as conveyed. | SCOPE §8.2, App. B |
| R16 | CLI parity gaps | GUI ships the actions. CLI forms marked `proposed` in the command table (§2.6) and listed in §10; **owner must amend §4.1 before M4 exit**. | §4.1 "every GUI action has a CLI equivalent" |
| R17 | Step-down shortcut | No global key; page mnemonic `Alt+S` only (Ctrl+Shift+D is "Disable and detach"). | key-map clash |

---

## 1. File layout

One owner unit per file (§7 gives the units). `core/` never imports Qt. `gui/` imports only `buttereye.core.api` (which re-exports types, errors, events).

```
buttereye/
  __init__.py                         U1  (__version__)
  core/
    __init__.py                       U1  (empty)
    api.py                            U1  facade ButterEye + pure helpers + re-exports
    types.py                          U1  all dataclasses/enums (§2.1)
    errors.py                         U1  ErrorCode, ButterEyeError tree (§2.2)
    events.py                         U1  Event union (§2.3)
    ops.py                            U1  Operation[T], ProgressSink (§2.4)
    i18n.py                           U1  gettext render(Msg), set_language
    commands.py                       U1  COMMANDS table, command_hint (§2.6)
    capabilities.py                   U1  provider probing → Capabilities (§2.5)
    testing/__init__.py               U1
    testing/fakecore.py               U1  FakeCore: same surface, scripted scenarios
    testing/scenarios.py              U1  named scenarios (fresh box, devbox-now, live-active, stalled, …)
    paths.py                          U3  resolve() → Paths, runtime dir 0700 check
    hw/__init__.py, hw/detect.py      U3  sysfs, vulkaninfo -j, nvidia-smi subprocess, display hint
    doctor/__init__.py                U3
    doctor/run.py                     U3  doctor() orchestrator, section budget 10 s
    doctor/checks_mpv.py              U3  version, --vf=help, host binary, conflicting user settings, mpvSockets
    doctor/checks_pkgs.py             U3  rpm -q, bundled(ncnn) provide, %license presence, ffms2/mkvmerge/trtexec/7z
    doctor/checks_vulkan.py           U3
    doctor/probe.py, doctor/probe.vpy U3  in-mpv probe (F1)
    doctor/gpufault.py                U3  journalctl -k Xid scan; unreadable ≠ OK
    doctor/smoke.py, doctor/smoke.vpy U3  10 s vspipe smoke test
    backends/__init__.py, backends/select.py   U3  BackendStatus + §5.3 selection (pure over report)
    setup.py                          U3  setup_plan / setup_apply (config written last via U2 provider)
    storage.py                        U3  sizes, clean (never RPM-owned), packaged/extra model listing
    licences.py                       U3  LegalNotices, ComponentLicence detection (%license paths)
    profiles/__init__.py              U2
    profiles/defaults.py              U2  quality/balanced/fast/cpu, default_config()
    profiles/schema.py                U2  TOML ↔ Config, unknown-key capture, line/col errors
    profiles/config.py                U2  load/save (atomic, revision, .bak migrate, read-only newer)
    profiles/rules.py                 U2  validate(), explain() first-match trace
  gui/
    __init__.py                       U4
    __main__.py                       U4  `buttereye-gui`, `python -m buttereye.gui`
    app.py                            U4  QApplication, desktop file name, flock, translator, theme hooks
    bridge.py                         U4  CoreThread + CoreBridge (§3)
    context.py                        U4  GuiContext passed to every page
    main_window.py                    U4  menu bar, sidebar, QStackedWidget, banners, status bar, shortcuts
    theme.py                          U4  palette-only, colorSchemeChanged, focus-frame proxy style
    quit_dialog.py                    U4  §4.10 close flow
    pages/__init__.py                 U4  PAGE_REGISTRY (lazy import strings)
    pages/base.py                     U4  Page base class (§4.0)
    a11y.py                           U5  announce(), labelled(), require_name()
    widgets/__init__.py               U5
    widgets/status_badge.py           U5  icon shape + word; never colour only
    widgets/banner.py                 U5  Banner, Banner.from_error(ButterEyeError)
    widgets/state_panel.py            U5  empty/loading/error/unavailable/content stack
    widgets/op_row.py                 U5  label, QProgressBar, rate, ETA, Cancel
    widgets/copy_field.py             U5  read-only monospace line + Copy
    widgets/cli_hint.py               U5  "Equivalent command" CopyField from command_hint
    widgets/fraction_edit.py          U5  n/d rate with validator
    widgets/bench_plot.py             U5  QPainter bars (no QtCharts), Qt.NoFocus
    widgets/drop_zone.py              U5  focusable file drop target (local files only)
    widgets/findings_view.py          U6  QTreeView + model + detail pane (setup + system)
    pages/system.py                   U6  doctor, hardware, plugins, report
    pages/storage.py                  U6  storage table, clean, models
    pages/about.py                    U6  F18
    dialogs/__init__.py               U6
    dialogs/setup_wizard.py           U6  F0, §4.7
    dialogs/trt_optin.py              U6  §5.2 text
    dialogs/consent.py                U6  download consent (URL, size, licence, SHA-256)
    dialogs/text_viewer.py            U6  read-only licence/text viewer
    pages/sessions.py                 U7  live page
    dialogs/attach.py                 U7
    dialogs/include_line.py           U7  copy / consented write of include=
    pages/profiles.py                 U8  profiles + rules tabs + rule tester
    dialogs/rule_edit.py              U8
    pages/render.py                   U9  queue
    dialogs/render_job.py             U9
    pages/bench.py                    U9
  data/i18n/buttereye_en.ts           U4  generated, committed (.ts ships in sdist, §8.4)
tools/extract_ts.py                   U4  stdlib-ast tr()/translate() → Qt TS XML
docs/errors.md                        U1  generated from ErrorCode, then hand-edited fix texts
docs/spikes/m0h.md                    U1  M0(h) no-go record
spikes/m0h-qtasyncio/probe.py         U1  reproducer
tests/  (owners in §6)
```

**Dependency order.** U1 → {U2, U3, U4, U5} → {U6, U7, U8, U9}. Because §2–§4 are frozen here, all nine units may start at once; U2–U9 code against this document and run against `FakeCore` until U1 lands, and against real providers as U2/U3 land.

**Internal provider contract (core-to-core).** `api.py` (U1) never imports a subsystem at module load. Each facade method resolves its provider lazily; if the module or attribute is missing, the feature is `NOT_IMPLEMENTED` and the method raises `NotAvailable` before any I/O. Providers (exact signatures, owned by U2/U3):

```python
# U2
buttereye.core.profiles.config.load(paths: Paths) -> ConfigLoad
buttereye.core.profiles.config.save(paths: Paths, cfg: Config, *, expected_revision: str) -> str  # new revision
buttereye.core.profiles.rules.validate(cfg: Config) -> tuple[ConfigIssue, ...]
buttereye.core.profiles.rules.explain(cfg: Config, facts: SourceFacts) -> RuleTrace
buttereye.core.profiles.defaults.builtin_profiles() -> tuple[Profile, ...]
buttereye.core.profiles.defaults.default_config() -> Config
# U3
buttereye.core.paths.resolve(env: Mapping[str, str] | None = None) -> Paths
async buttereye.core.hw.detect.hardware(paths: Paths) -> HardwareInfo
async buttereye.core.doctor.run.doctor(paths: Paths, cfg: Config, *, trt: bool, progress: ProgressSink) -> DoctorReport
buttereye.core.backends.select.backends(report: DoctorReport, cfg: Config) -> tuple[BackendStatus, ...]
buttereye.core.backends.select.select(report: DoctorReport, cfg: Config, bench: tuple[BenchResult, ...]) -> Selection
async buttereye.core.setup.plan(paths: Paths, report: DoctorReport, *, trt_experimental: bool) -> SetupPlan
async buttereye.core.setup.apply(paths: Paths, plan: SetupPlan, choices: SetupChoices, progress: ProgressSink) -> SetupResult
async buttereye.core.storage.entries(paths: Paths) -> tuple[StorageEntry, ...]
async buttereye.core.storage.clean(paths: Paths, targets: frozenset[CleanTarget], progress: ProgressSink) -> CleanResult
async buttereye.core.storage.models(paths: Paths) -> tuple[ModelEntry, ...]
async buttereye.core.doctor.run.plugins(paths: Paths) -> tuple[PluginStatus, ...]
buttereye.core.licences.legal_notices() -> LegalNotices
async buttereye.core.licences.third_party(paths: Paths) -> tuple[ComponentLicence, ...]
# later milestones (absent today → NOT_IMPLEMENTED): mpvctl.session.*, render.jobs.*, bench.runner.*,
# plugins.download.*, doctor.report.bundle
```

`ProgressSink = Callable[[Progress], None]` (called on the core loop; `Operation` forwards it).

---

## 2. Core facade contract (FROZEN)

All in `buttereye/core/`, `from __future__ import annotations`, Python 3.14, `mypy --strict` clean. Everything that crosses the bridge is a frozen slotted dataclass, an enum, `str/int/float/bool/None`, `Fraction`, `Path`, `tuple`, `frozenset` or `MappingProxyType`.

### 2.1 Types (`types.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping, NewType

OpId = NewType("OpId", str)
SessionId = NewType("SessionId", str)
JobId = NewType("JobId", str)
CandidateId = NewType("CandidateId", str)


def _empty() -> Mapping[str, str | int | float]:
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
    DISPLAY = "display"  # lowest refresh/k that doubles the source (SCOPE §5.6)
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
    scale: float | None  # vs-mlrt only; ncnn at 4K: a smaller size via the benchmark caps (§5.3)
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
    trt_experimental: bool | None = None  # §5.2: None = use TensorRT when set up
    upscaling: Literal["standard", "sharper"] = "standard"  # SCOPE §15.2
    deband: bool = False  # SCOPE §15.2
    full_size: bool = False  # never smaller to keep up (GUI.md §12)


@dataclass(frozen=True, slots=True)
class RenderDefaults:
    encoder_by_vendor: Mapping[str, str] = field(default_factory=_empty)  # type: ignore[assignment]
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
    code: "ErrorCode"
    message: Msg
    line: int | None = None
    column: int | None = None
    field: str | None = None  # dotted path, e.g. "profiles[3].sc_threshold"


@dataclass(frozen=True, slots=True)
class ConfigLoad:
    config: Config
    exists: bool  # False → first run (§4.7)
    revision: str  # sha256 of file bytes; "" if no file
    read_only: bool  # newer schema_version
    used_defaults: bool  # invalid TOML → defaults in use
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
    matched_index: int | None  # None → no rule matched, default profile
    profile_id: str
    steps: tuple[RuleStep, ...]  # evaluated rules up to and including the match


# ---------- doctor / hardware (F1, F9, F21) ----------
@dataclass(frozen=True, slots=True)
class Finding:
    id: str  # stable check id, e.g. "mpv.vf_vapoursynth"
    section: Section
    severity: Severity
    code: "ErrorCode | None"  # required for BLOCKING/DEGRADED
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
    decode_device: str | None  # pci; warn if ≠ interpolation (§4.5)
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
    code: "ErrorCode | None" = None


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


# ---------- live (F2–F4, F8, §4.3, §4.10, §4.11) ----------
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
    health_code: "ErrorCode | None"
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
    failed: tuple[tuple[SessionId, "ErrorCode"], ...]
    jobs_cancelled: tuple[JobId, ...]


# ---------- capabilities ----------
@dataclass(frozen=True, slots=True)
class CapState:
    available: bool
    reason: Reason | None = None
    code: "ErrorCode | None" = None
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
    est_fps: float | None  # estimated output frames per second; None = no benchmark


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
    warnings: tuple[Finding, ...]  # VFR→CFR (§7.3), mkvmerge missing
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
    error: "ButterEyeError | None"


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
    finding: Finding | None  # missing %license → DEGRADED


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
    missing: tuple[Path, ...]  # files not found → shown, never hidden


@dataclass(frozen=True, slots=True)
class CommandHint:
    argv: tuple[str, ...]  # ("buttereye", "render", "IN", "-o", "OUT")
    status: Literal["in_scope", "proposed"]

    def text(self) -> str:
        return " ".join(self.argv)  # display only; shlex.join in impl
```

### 2.2 Errors (`errors.py`)

`docs/errors.md` (F21) is generated from this enum and is authoritative for fix texts. Tests assert codes, never text. Exit codes per §4.1.

```python
class ErrorCode(Enum):
    # value = (code, exit_code)
    MPV_TOO_OLD = ("BE-1001", 3)
    MPV_NO_VS_FILTER = ("BE-1002", 3)
    MPV_SANDBOXED = ("BE-1003", 3)
    MPV_NOT_FOUND = ("BE-1004", 4)
    PROBE_FAILED = ("BE-1005", 3)
    VULKAN_LOADER_MISSING = ("BE-1010", 4)
    VULKAN_CPU_ONLY = ("BE-1011", 4)  # degraded: MVTools only (§5.3.2)
    PKG_MISSING = ("BE-1020", 4)
    LICENCE_FILE_MISSING = ("BE-1021", 1)  # %license absent (F1/F18)
    VARIANT_SYSTEM_NCNN = ("BE-1022", 1)  # degraded: Fedora-ncnn build active (m0f)
    RIFE_GPU_FAULT = ("BE-1030", 1)  # Xid in journalctl -k (F1)
    JOURNAL_UNREADABLE = ("BE-1031", 1)  # info: fault history unknown
    USER_CONF_CONFLICT = ("BE-1040", 1)  # hwdec/interpolation/save-position (F1)
    MPVSOCKETS_IN_USE = ("BE-1041", 1)
    TRT_UNSUPPORTED = ("BE-1050", 4)  # TRT section items (a)–(g), experimental
    CONFIG_INVALID = ("BE-2001", 2)
    CONFIG_NEWER_SCHEMA = ("BE-2002", 2)
    CONFIG_CONFLICT = ("BE-2003", 2)  # revision changed since load
    CONFIG_VALUE = ("BE-2004", 2)  # field-level validation
    CONFIG_UNKNOWN_KEY = ("BE-2005", 2)  # warning only
    RUNTIME_DIR_UNSAFE = ("BE-2010", 1)  # §4.3 0700/owner
    GUI_ALREADY_RUNNING = ("BE-2020", 1)
    SOCKET_UNSAFE = ("BE-3001", 1)
    INSTANCE_UNSUPPORTED = ("BE-3002", 3)
    VF_ROLLED_BACK = ("BE-3003", 1)  # F4
    FILTER_STALLED = ("BE-3004", 1)
    IPC_LOST = ("BE-3005", 1)
    DEVICE_LOST = ("BE-3006", 1)
    FILE_NOT_LOCAL = ("BE-3007", 2)  # URLs refused (§2 non-goals)
    FFMS2_MISSING = ("BE-4001", 4)
    CODEC_NOT_DECODABLE = ("BE-4002", 4)
    HDR_CLASS_REFUSED = ("BE-4003", 2)
    NO_SPACE = ("BE-4004", 1)
    VSPIPE_FRAME_ERROR = ("BE-4005", 1)
    ENCODER_FAILED = ("BE-4006", 1)
    REMUX_FAILED = ("BE-4007", 1)
    OUTPUT_EXISTS = ("BE-4008", 2)
    BENCH_FAILED = ("BE-5001", 1)
    HASH_MISMATCH = ("BE-6001", 1)
    HOST_NOT_ALLOWED = ("BE-6002", 1)
    DOWNLOAD_FAILED = ("BE-6003", 1)
    SEVENZIP_MISSING = ("BE-6004", 4)
    NOT_IMPLEMENTED = ("BE-9001", 1)
    INTERNAL = ("BE-9999", 1)

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def exit_code(self) -> int:
        return self.value[1]


class ButterEyeError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,  # raw stderr/log tail, untranslated
        commands: tuple[str, ...] = (),
    ) -> None: ...

    code: ErrorCode
    cause: Msg
    fix: Msg | None
    detail: str | None
    commands: tuple[str, ...]


class ConfigError(ButterEyeError):
    line: int | None
    column: int | None  # extra kw-only ctor args


class ConfigConflict(ConfigError):
    on_disk_revision: str


class DependencyMissing(ButterEyeError): ...  # commands = dnf lines; never runs dnf


class BlockingIssue(ButterEyeError):
    findings: tuple[Finding, ...]


class InstanceRefused(ButterEyeError):
    reason: RefusalReason


class FilterFailed(ButterEyeError):
    rolled_back: bool


class RenderRefused(ButterEyeError): ...


class InsufficientSpace(ButterEyeError):
    need_bytes: int
    free_bytes: int


class DownloadFailed(ButterEyeError):
    url: str


class NotAvailable(ButterEyeError):
    feature: Feature
    state: CapState  # code = NOT_IMPLEMENTED or the dep code


class OperationCancelled(ButterEyeError): ...  # raised with the op's own code context; CLI exit 130
```

`OperationCancelled.exit_code` is 130 regardless of `code` (CLI maps it). Any non-`ButterEyeError` escaping the core is wrapped as `ButterEyeError(INTERNAL, …, detail=traceback)` by the bridge.

### 2.3 Events (`events.py`)

```python
@dataclass(frozen=True, slots=True)
class Progress:
    op_id: OpId
    phase: Msg
    done: int | None
    total: int | None  # total None → indeterminate
    unit: Literal["frames", "bytes", "steps", "checks"]
    rate: float | None
    eta_s: float | None  # render ETA from the slower of vspipe/ffmpeg (§7.6)
    detail: Msg | None = None


@dataclass(frozen=True, slots=True)
class OpStarted:
    op_id: OpId
    kind: OpKind


@dataclass(frozen=True, slots=True)
class OpFinished:
    op_id: OpId
    state: OpState
    error: ButterEyeError | None


@dataclass(frozen=True, slots=True)
class SessionAdded:
    snapshot: SessionSnapshot


@dataclass(frozen=True, slots=True)
class SessionChanged:
    snapshot: SessionSnapshot  # coalesced ≤ 4 Hz per session for counter-only changes


@dataclass(frozen=True, slots=True)
class SessionEnded:
    sid: SessionId
    reason: Literal["mpv_exited", "detached", "connection_lost"]


@dataclass(frozen=True, slots=True)
class HealthChanged:
    sid: SessionId
    health: Health
    code: ErrorCode | None
    reason: Msg
    evidence: tuple[str, ...]
    auto_action: Msg | None  # "interpolation turned off"


@dataclass(frozen=True, slots=True)
class JobChanged:
    job: RenderJobState


@dataclass(frozen=True, slots=True)
class ConfigChanged:
    revision: str  # emitted after any save through this core


@dataclass(frozen=True, slots=True)
class CapabilitiesChanged:
    caps: Capabilities


@dataclass(frozen=True, slots=True)
class OrphansFound:
    candidates: tuple[InstanceCandidate, ...]


@dataclass(frozen=True, slots=True)
class Notice:
    message: Msg
    code: ErrorCode | None = None
    sid: SessionId | None = None


@dataclass(frozen=True, slots=True)
class EventsDropped:
    count: int  # subscriber must resync via snapshots


Event = (
    OpStarted
    | OpFinished
    | SessionAdded
    | SessionChanged
    | SessionEnded
    | HealthChanged
    | JobChanged
    | ConfigChanged
    | CapabilitiesChanged
    | OrphansFound
    | Notice
    | EventsDropped
)
```

Ordering: transitions (`Op*`, `Session*` except counter-only `SessionChanged`, `HealthChanged`, `JobChanged`, `Config*`, `Capabilities*`, `Notice`) are never dropped. `Progress` is not an `Event`; it goes to the op's `ProgressSink`.

### 2.4 Operations (`ops.py`)

```python
ProgressSink = Callable[[Progress], None]


class Operation(Generic[T]):
    id: OpId
    kind: OpKind

    @property
    def state(self) -> OpState: ...
    def add_progress_sink(self, sink: ProgressSink) -> None: ...  # loop thread only
    async def result(self) -> T: ...  # raises ButterEyeError / OperationCancelled
    def cancel(self) -> None: ...  # idempotent; loop thread only (bridge marshals)
```

Cancellation contract: `cancel()` → `CANCELLING` → SIGTERM to the subprocess **process group** (`start_new_session=True`, §7.6) → SIGKILL after 5 s → delete partial files → `CANCELLED`. R72 vspipe ignores SIGINT, so SIGTERM always. `preexec_fn` banned (multithreaded process; AST test). Setup cancel leaves only logs (F0).

### 2.5 Facade (`api.py`)

All `async def` and `Operation`-returning methods run **on the core loop only**; the GUI reaches them through `CoreBridge`. Module-level functions marked *pure* are thread-safe, do no I/O and may be called from the GUI thread.

```python
class ButterEye:
    @classmethod
    async def open(cls, paths: Paths | None = None) -> "ButterEye": ...   # resolves paths, loads config; no writes
    async def close(self, policy: DetachPolicy = DetachPolicy.KEEP_FILTER, *,
                    cancel_jobs: bool, timeout_s: float = 5.0) -> ShutdownReport: ...
    def subscribe(self, maxlen: int = 4096) -> AsyncIterator[Event]: ...   # one per bridge
    async def capabilities(self) -> Capabilities: ...
    async def ping(self) -> float: ...                                      # loop liveness; returns monotonic()
    def paths(self) -> Paths: ...

    # config / profiles / rules (§4.9, F15)                                       Feature.CONFIG
    async def load_config(self) -> ConfigLoad: ...
    async def save_config(self, cfg: Config, *, expected_revision: str) -> str: ...   # ConfigConflict; read_only → ConfigError(BE-2002)

    # doctor / setup / hardware (F0, F1, F9, F13)
    def doctor(self, *, trt: bool | None = None) -> Operation[DoctorReport]: ...       # None = config opt-in
    async def last_report(self) -> DoctorReport | None: ...
    async def hardware(self) -> HardwareInfo: ...
    async def plugins(self) -> tuple[PluginStatus, ...]: ...
    async def backends(self, report: DoctorReport) -> tuple[BackendStatus, ...]: ...
    async def select_backend(self, report: DoctorReport) -> Selection: ...
    async def set_trt_experimental(self, on: bool, *, expected_revision: str) -> str: ...  # writes [general]
    async def setup_plan(self, report: DoctorReport, *, trt_experimental: bool) -> SetupPlan: ...  # in memory
    def setup_apply(self, plan: SetupPlan, choices: SetupChoices) -> Operation[SetupResult]: ...   # config written last
    async def include_line(self) -> str: ...
    async def add_include_to_mpv_conf(self, *, consent: Literal[True]) -> str: ...   # returns the diff applied; idempotent
    def report_bundle(self, dest: Path, *, redact: bool = True) -> Operation[Path]: ...   # Feature.REPORT

    # live (F2–F4, F8, §4.3, §4.10, §4.11)                                       Feature.LIVE/ATTACH/DISCOVER/ORPHANS
    async def discover(self) -> tuple[InstanceCandidate, ...]: ...          # refused ones included, with reason
    def play(self, file: Path, *, profile_id: str | None = None) -> Operation[SessionId]: ...  # --input-ipc-server
    async def attach(self, cid: CandidateId, *, profile_id: str | None = None) -> SessionId: ...
    async def detach(self, sid: SessionId, policy: DetachPolicy = DetachPolicy.KEEP_FILTER) -> None: ...
    async def set_interpolation(self, sid: SessionId, enabled: bool, *, force: bool = False) -> SessionSnapshot: ...
    async def apply_profile(self, sid: SessionId, profile_id: str | None) -> SessionSnapshot: ...  # None = rules; gen = max(seen)+1, verified
    async def step_down(self, sid: SessionId) -> SessionSnapshot: ...       # §5.3 step 5
    async def remove_orphan_filter(self, cid: CandidateId) -> None: ...
    async def sessions(self) -> tuple[SessionSnapshot, ...]: ...
    async def session_log(self, sid: SessionId) -> str: ...              # per-file log to copy (§12.1.1)

    # bench (F14)                                                                 Feature.BENCH
    def bench(self, req: BenchRequest) -> Operation[BenchResult]: ...
    async def bench_history(self) -> tuple[BenchResult, ...]: ...
    async def apply_bench(self, result: BenchResult, label: str, *, expected_revision: str) -> str: ...

    # render (§7, F16)                                                            Feature.RENDER
    async def render_probe(self, src: Path, *, profile_id: str | None = None) -> RenderProbe: ...
    async def render_enqueue(self, spec: RenderJobSpec) -> JobId: ...       # sequential queue, concurrency 1
    async def render_jobs(self) -> tuple[RenderJobState, ...]: ...
    async def render_move(self, job: JobId, delta: int) -> None: ...        # queued jobs only
    async def render_cancel(self, job: JobId) -> None: ...
    async def job_log(self, job: JobId) -> str: ...                         # per-file log to copy (§12.1.1)
    async def render_forget(self, job: JobId) -> None: ...                  # finished/failed/cancelled only
    async def stale_jobs(self) -> tuple[StaleJob, ...]: ...
    async def stale_job_stop(self, job: JobId) -> None: ...                 # SIGTERM pgid, delete partials

    # storage / models / licences (§4.6, F10, F18)
    async def storage(self) -> tuple[StorageEntry, ...]: ...                # Feature.STORAGE
    def clean(self, targets: frozenset[CleanTarget]) -> Operation[CleanResult]: ...   # Feature.CLEAN; never RPM-owned
    async def models(self) -> tuple[ModelEntry, ...]: ...                   # Feature.MODELS
    async def model_downloads(self) -> tuple[DownloadItem, ...]: ...        # Feature.MODEL_DOWNLOADS
    def model_install(self, name: str) -> Operation[ModelEntry]: ...
    def model_install_file(self, file: Path, sha256: str | None) -> Operation[ModelEntry]: ...
    async def model_remove(self, name: str) -> None: ...
    async def third_party(self) -> tuple[ComponentLicence, ...]: ...        # Feature.LICENCES

# ---- module-level pure helpers (GUI thread allowed) ----
def render(msg: Msg) -> str: ...                                   # core gettext, current language
def set_language(tag: str | None) -> None: ...                     # called once at start, before any render()
def validate_config(cfg: Config) -> tuple[ConfigIssue, ...]: ...
def explain_rules(cfg: Config, facts: SourceFacts) -> RuleTrace: ...
def builtin_profiles() -> tuple[Profile, ...]: ...
def legal_notices() -> LegalNotices: ...
def command_hint(command_id: str, **params: str) -> CommandHint: ...
def unavailable_text(feature: Feature, state: CapState) -> Msg: ...  # §5 wording

__all__ re-exports every name in types, errors, events, ops.
```

Pure helpers that need an unbuilt provider (`validate_config`, `explain_rules`, `builtin_profiles`) raise `NotAvailable(Feature.CONFIG)`; the GUI then shows the unavailable state for the profiles page.

**Capability resolution** (`capabilities.py`): static part from `importlib.util.find_spec` + `hasattr` on the provider table; dynamic part from the last `DoctorReport` (`MISSING_DEPENDENCY`, `BLOCKED_BY_DOCTOR`) and config (`OPT_IN_REQUIRED` for TRT). `HDR_PASSTHROUGH` is `SPIKE_PENDING` until M0(d) passes (F7). `RENDER` is `MISSING_DEPENDENCY` with `FFMS2_MISSING` when ffms2 is absent, even once implemented. Emits `CapabilitiesChanged` after each doctor run and config save.

### 2.6 Command hints (`commands.py`)

`COMMANDS: Mapping[str, CommandSpec]` with `CommandSpec(id, argv_template: tuple[str, ...], status)`. Placeholders `{name}` filled from `command_hint(**params)`; unknown placeholders raise `KeyError` (tested). Initial table:

| id | argv | status |
|---|---|---|
| `setup` | `buttereye setup` | in_scope |
| `doctor` | `buttereye doctor` | in_scope |
| `doctor.report` | `buttereye doctor --report` | in_scope |
| `play` | `buttereye play {file} [--profile {profile}]` | in_scope |
| `attach` | `buttereye attach [--socket {socket}]` | in_scope |
| `detach` | `buttereye detach [--socket {socket}]` | in_scope |
| `detach.disable` | `buttereye detach --disable [--socket {socket}]` | in_scope |
| `live.toggle` | `buttereye attach --socket {socket} --enable\|--disable` | **proposed** |
| `live.profile` | `buttereye attach --socket {socket} --profile {profile}` | **proposed** |
| `orphans.remove` | `buttereye detach --orphans` | **proposed** |
| `render` | `buttereye render {src} -o {out} --profile {profile} --encoder {encoder}` | in_scope |
| `bench` | `buttereye bench [--file {file}] [--full]` | in_scope |
| `bench.apply` | `buttereye bench --apply {label}` | **proposed** |
| `models.list` / `.add` / `.remove` / `.install` | `buttereye models list` / `add {name}` / `remove {name}` / `install --from {file} [--sha256 {sha}]` | in_scope |
| `plugins.list` | `buttereye plugins list` | in_scope |
| `clean` | `buttereye clean --{target}…` | in_scope |
| `clean.dry_run` | `buttereye clean --dry-run` | **proposed** |
| `profiles.list` / `profiles.explain` | `buttereye profiles list` / `profiles explain --fps … --height …` | **proposed** |
| `trt.optin` | `buttereye setup --trt-experimental` | **proposed** |
| `licence` / `version` | `buttereye licence` / `buttereye --version` | in_scope |

Bracketed parts are omitted when the param is absent.

---

## 3. Qt/async bridge (`gui/bridge.py`)

**Decision.** M0(h) is **no-go** on PySide6 6.11.2 (R1). A dedicated non-daemon `threading.Thread` runs a stock `asyncio.new_event_loop()`; the facade is opened inside it. Signals carry results back. `docs/spikes/m0h.md` records this with the reproducer; re-test on each PySide6 bump.

**Fallback / future swap.** `CoreBridge` is the only class that knows the loop lives in a thread. If a later PySide6 implements subprocess + Unix sockets, a `QtAsyncioBridge` with the same public surface replaces it in this one file; no page changes. If the thread bridge itself proves unworkable (it should not), the next fallback is running the CLI as a subprocess with `--json`, which the facade contract already mirrors.

```python
class Ticket:  # plain Python, GUI thread
    def cancel(self) -> None: ...  # drops the reply; for ops also cancels the Operation


class CoreBridge(QObject):
    event = Signal(object)  # Event, in order, GUI thread
    ready = Signal(object)  # Capabilities after open()
    fatal = Signal(object)  # ButterEyeError at startup (e.g. BE-2010)
    unresponsive = Signal(bool)  # watchdog

    def __init__(self, paths: Paths | None = None, parent: QObject | None = None) -> None: ...
    def start(self) -> None: ...
    def call(
        self,
        fn: Callable[[ButterEye], Awaitable[T]],
        *,
        owner: QObject,
        ok: Callable[[T], None],
        err: Callable[[ButterEyeError], None] | None = None,
    ) -> Ticket: ...
    def run_op(
        self,
        fn: Callable[[ButterEye], Operation[T]],
        *,
        owner: QObject,
        ok: Callable[[T], None],
        err: Callable[[ButterEyeError], None] | None = None,
        progress: Callable[[Progress], None] | None = None,
    ) -> Ticket: ...
    @property
    def capabilities(self) -> Capabilities | None: ...  # latest cached
    def shutdown(
        self, policy: DetachPolicy, *, cancel_jobs: bool, timeout_s: float = 5.0
    ) -> ShutdownReport | None: ...  # None on timeout
```

Rules:

1. `call`/`run_op` submit with `asyncio.run_coroutine_threadsafe`. Replies go to a private `_Reply(QObject)` created in the GUI thread, via `Signal(object, object)`; cross-thread emission is queued, so `ok`/`err` always run on the GUI thread.
2. **Owner lifetime.** Each request is bound to `owner.destroyed`; if the owner is gone the reply is dropped (no call into a deleted C++ object).
3. `err` defaults to the owner page's `show_error`. `OperationCancelled` goes to `err` too; pages show it as neutral "Cancelled", never as an error banner.
4. Non-`ButterEyeError` → `ButterEyeError(INTERNAL)` with traceback in `detail`, logged. `loop.set_exception_handler` logs; the core thread never dies silently.
5. **Pump.** One coroutine `async for ev in core.subscribe()` appends to a lock-protected deque. Progress sinks write into a dict keyed by `op_id` (latest wins). A GUI `QTimer(100 ms)` drains both. On `EventsDropped`, pages re-fetch snapshots.
6. **Watchdog.** `ping()` every 2 s; no reply in 5 s → `unresponsive(True)` → banner "ButterEye's engine is not responding. Log: <path>". Clears on next reply. Tests run the loop with `slow_callback_duration=0.1`; a slow-callback warning fails the test.
7. **Payload rule.** Only §2 types cross. Lambdas passed to `call` capture plain values, never QObjects.
8. **Shutdown.** `shutdown` posts `core.close(...)`, waits in a local `QEventLoop` with `QTimer.singleShot(timeout)`, then stops the loop and joins (1 s). Never `.result()` on the GUI thread.
9. **mpv lifetime is decided at spawn** (core, M1; GUI relies on it): `start_new_session=True`, `stdin=DEVNULL`, stdout/stderr to `logs/mpv-<id>.log` (never pipes), `--input-ipc-server` in the 0700 runtime dir. A SIGKILLed GUI leaves mpv playing; the next start's `discover()` returns it with `managed_by_us=True` and emits `OrphansFound`.

**Close flow** (`quit_dialog.py`, §4.10). On `closeEvent`/Ctrl+Q:
- No sessions, no running jobs → close immediately (detach is a no-op).
- Running render → "A render is running. Quitting cancels it and deletes the partial file." [Cancel render and quit] [Keep window open] (default: Keep). No tray, no background rendering (F17, §7.6).
- Live sessions → "Smooth playback keeps running in mpv after ButterEye closes." checkbox "Turn interpolation off in those players" (unchecked) [Close] [Keep window open]. Checked → `DISABLE_FILTER`.
- Then a modal progress "Detaching from 2 players…"; on timeout "Could not detach from: <titles>. They keep their current settings." [OK].

---

## 4. Shell and pages

### 4.0 Shared rules

- **Page base** (`pages/base.py`):
  ```python
  class Page(QWidget):
      page_id: ClassVar[str]
      features: ClassVar[tuple[Feature, ...]]  # gating features

      def __init__(self, ctx: GuiContext, parent: QWidget | None = None) -> None: ...
      def title(self) -> str: ...  # tr()
      def refresh(self) -> None: ...  # F5; also called on first show
      def on_event(self, ev: Event) -> None: ...  # routed by main window
      def show_error(
          self, err: ButterEyeError
      ) -> None: ...  # default: Banner.from_error in StatePanel
      def has_unsaved_changes(self) -> bool:
          return False
  ```
  `GuiContext` (`context.py`): `bridge: CoreBridge`, `announce(text: str, *, assertive: bool = False)`, `status(text: str)`, `go(page_id: str)`, `settings: QSettings`, `current_session_facts() -> SourceFacts | None`.
- **Every page body is a `StatePanel`** with `empty / loading / error / unavailable / content`. The controller chooses the state explicitly; "no rows" never implies a state. **Unavailable shows zero data rows**, the reason text from `unavailable_text()`, the BE code as selectable text, any `commands` in `CopyField`s, and the `CliHint`.
- **Every page inside a `QScrollArea`** (font scale scrolls, never clips).
- **Strings.** GUI strings via `self.tr()`. Core `Msg` via `api.render()`. Behaviour keys on `ErrorCode`/enums, never on text.
- **Accessible names.** Every input built through `a11y.labelled(label_text, widget)` (sets buddy + `setAccessibleName`). Icon-only buttons are banned. Status changes that matter are announced with `QAccessibleAnnouncementEvent` (available in 6.11): session state transitions, op finished/failed, health changes. Counters are never announced.
- **Theme.** (Superseded in part 2026-10-10 by the Carino branding, §12.3: the brand's colours are literals in `theme.CARINO` only, the palette is dark whatever the desktop scheme, and the rest of this rule still holds.) No colour literals outside `theme.CARINO`, no `setStyleSheet` with colours, no `setPixelSize`, no `setFixed*` with constants. Headings `pointSizeF() * 1.25`. Custom painting from `palette()`, repaint on `PaletteChange` and `QStyleHints.colorSchemeChanged`. Hyprland has no platform theme → Fusion; dark mode follows the portal colour scheme. `theme.py` installs a `QProxyStyle` that draws a 2 px `palette.highlight` focus frame where the style draws none.
- **Status badges.** Distinct glyph per state (check, triangle, octagon, info circle, hourglass, pause) from `QIcon.fromTheme` with bundled SVG fallback, plus the word. Never colour alone.
- **Wayland.** `setDesktopFileName("io.github.buttereye.ButterEye")`. No window positioning (size saved only), no global shortcuts (F19 deferred), no tray, no `QSystemTrayIcon` import. A session's rate comes from IPC `display-fps`, never `QScreen`. Portal paths under `/run/user/*/doc/` are accepted and flagged in details.
- **Files.** `QFileDialog` (portal). Drops accept `QUrl.isLocalFile()` only; others → "Only local files can be opened. Streaming isn't supported." (BE-3007).

### 4.1 Shell (`main_window.py`)

- Sidebar `QListWidget` (accessible name "Sections"), `QStackedWidget`, global banner area (orphans, unresponsive core, startup fatal), `QStatusBar` that mirrors the latest announcement as text.
- Pages, in order (Ctrl+1…Ctrl+7): **1 Play** (sessions), **2 Profiles & Rules**, **3 Render**, **4 Benchmark**, **5 System**, **6 Storage**, **7 About**.
- Menus: **File** (Open and Play… Ctrl+O, Attach to Running mpv… Ctrl+Shift+A, Run Setup…, Quit Ctrl+Q); **Playback** (Interpolation On/Off Ctrl+T, Detach Ctrl+D, Disable and Detach Ctrl+Shift+D — act on the selected session); **Tools** (Run Checks F5 on System, Create Bug Report…, Copy Equivalent Command Ctrl+Shift+C); **Help** (Licences, About F1).
- Global keys: Ctrl+1…7, Ctrl+O, Ctrl+Shift+A, Ctrl+T, Ctrl+D, Ctrl+Shift+D, F5 (refresh current page), Ctrl+S (save, Profiles only), Ctrl+Shift+C, F6 (cycle focus sidebar → page → status bar), F1, Ctrl+Q. All labels carry `&` mnemonics.
- Startup: `bridge.start()`; on `ready` → `load_config()`; `exists=False` → open setup wizard. `fatal` → full-window error panel with code, cause, fix, log path, [Quit].
- Help line on Play: "Keyboard shortcuts for playback work inside the mpv window (ButterEye helper), not system-wide."
- Leaving a page with `has_unsaved_changes()` → "Discard unsaved changes to profiles?" [Discard] [Stay].

### 4.2 Setup wizard (`dialogs/setup_wizard.py`) — F0, §4.7. CLI `buttereye setup`

`QWizard`, `ClassicStyle`, modal. Opened when `ConfigLoad.exists` is False, or from File → Run Setup. Holds a `SetupPlan` in memory; nothing is written before Apply's final step.

| Page | Widgets / states | Core calls |
|---|---|---|
| 1 Welcome | Three sentences: what ButterEye does; it never edits your mpv.conf; no network unless you start a download. Copyright + licence name + [View licence] (F18). | `legal_notices()` |
| 2 Checks | `OpRow` "Checking your system (up to 10 seconds)" + Cancel; then `FindingsView` grouped Blocking / Degraded / OK (OK collapsed), summary "2 problems, 1 warning, 18 checks passed". Each finding: badge, title, cause, fix, `CopyField` per command, [Check again]. Blocking → Next disabled, label beside it: "Fix the blocking items, then press Check again." Error → banner with code + [Retry]. | `doctor()` |
| 3 Engine | Radio list from `backends()`; unavailable rows disabled, reason as visible text (e.g. "CPU-only: MVTools interpolation, RIFE unavailable — llvmpipe only"). Proposed row pre-selected, reason shown ("RIFE (Vulkan) on NVIDIA GeForce RTX 4090"). Unchecked "Show experimental options" → checkbox "Use experimental NVIDIA TensorRT" → `trt_optin` dialog (§5.2 text: pre-release v16.x, TRT 11 RPMs installed by you, may break on updates; [I understand, turn it on] [Cancel]). Opt-in re-runs checks with `trt=True`. | `backends()`, `setup_plan()` |
| 4 Packages | Only if `missing_packages`. `CopyField` per `dnf_lines`; text "Run these in a terminal, then press Check again. ButterEye never runs dnf." [Check again]. | `doctor()`, `setup_plan()` |
| 5 Downloads | Only if `downloads` non-empty (TRT opt-in). Table Name / Size / Licence / Source URL / SHA-256, one checkbox per row, none pre-checked (§5.4). If MODEL_DOWNLOADS unavailable: "No downloadable models are listed in this release." and Next continues without TRT models. | `setup_plan()` |
| 6 Apply | `OpRow` per phase: downloads (bytes), smoke test (10 s vspipe + in-mpv probe), writing settings. Cancel → `Ticket.cancel()`; text "Cancelled. Nothing was saved." Failure → finding view; [Back] [Retry]. | `setup_apply()` |
| 7 Finish | Result line ("Test run passed: 74 fps" or the finding). Unchecked offers: "Measure my GPU now (about a minute)" → Benchmark page; "Make my normal mpv use ButterEye's settings" → `include_line` dialog (§4.3). [Finish]. | `include_line()` |

Accessible names: "Check result: Vulkan loader, OK", "Copy dnf command", "Engine choice". Keys: Alt+N / Alt+B / Esc (Qt), Ctrl+C on a focused `CopyField`.

### 4.3 Play page (`pages/sessions.py`, `dialogs/attach.py`, `dialogs/include_line.py`) — F2–F4, F8, §4.3, §4.10, §4.11

**Features:** LIVE, ATTACH, DISCOVER, ORPHANS.

- **Empty (no sessions):** `DropZone` (accessible name "Drop a video file here, or press Enter to choose one"; Enter/Space opens the file dialog), [Open and Play…], [Attach to running mpv…], `CliHint` `play`.
- **Not ready:** last report blocking or config missing → banner "Setup isn't finished — 1 problem needs fixing" [Open Setup]; open/attach disabled with that reason as accessible description.
- **Unavailable (today):** see §5 wording; no session rows, no discovered players.
- **Content:** left `QTreeView` of sessions (Title, Origin, State badge, Health badge); right detail `QFormLayout`:
  - headline badge derived in this order: `ended` → health ≠ OK → filter state;
  - "23.976 → 119.88 fps (5×)", backend + model, display Hz, source facts, `gen`;
  - counters (drops, decoder drops, delayed, mistimed, display sync) with 10 s deltas — read-only labels with buddies;
  - `restore_pending` list; `notice` text.
- **Controls** (selected session): `QCheckBox` "&Interpolation" (text "Interpolation on/off"; Space toggles when the tree has focus); "&Profile" combo ("Automatic (rules)", built-ins, custom) + [Apply] (Enter) → "Applying… (gen 7)" → "Applied (gen 8)" or the rollback banner (F4); [&Step down] visible only when `step_down_to` is set ("Dropping 3% of frames — step down to Fast?"); [&Detach]; [Disable and detach].
- **Headline texts:**

  | State | Text |
  |---|---|
  | PENDING | "Starting… waiting for the video window" |
  | ACTIVE | "Smooth: 23.976 → 119.88 fps (5×) · RIFE (Vulkan) v4.26 · RTX 4090" |
  | BYPASSED | ALREADY_AT_RATE "Already at your screen's rate — nothing to do"; INTERLACED "Interlaced video — smoothing skipped"; HDR_SKIP "HDR video — smoothing skipped (default)"; UNSUPPORTED_FORMAT "This video format can't be smoothed"; NO_VIDEO "No video to smooth"; NO_REALTIME "Too slow for real time at this resolution — smoothing off" |
  | OFF | "Smoothing off" |
  | ROLLED_BACK | rendered cause + fix + code |
  | ended | "Ended: mpv exited" / "Detached" / "Connection lost" — row greyed with text, removed after 10 s or Delete |

- **Health banners** (inline per session, code + cause + fix):

  | Health | Banner | Buttons |
  |---|---|---|
  | DROPPING | "Dropping frames (3% over 10 s)." | [Step down] [Ignore] |
  | STALLED | "Video stalled while audio kept playing; interpolation was turned off." (BE-3004) | [Use MVTools for this file] [Keep off] [Details] |
  | DEVICE_LOST / GPU_FAULT | "The GPU reported a fault in RIFE-ncnn (Xid 13)." (BE-3006/BE-1030); the title lists only the Xid numbers seen in the evidence ("(Xid 13, 109)"), and reads "The GPU stopped responding (Vulkan device lost)." with no Xid | [Switch to MVTools] [Retry RIFE] (warns faults may repeat) [Copy diagnostic command] |
  | CONNECTION_LOST | "Lost the connection to mpv." (BE-3005). On a session that has not ended this means mpv stopped answering (§11.8); body: "mpv isn't answering. If it recovers, ButterEye picks it up again by itself; if it stays stuck, close that mpv window." | [Reconnect] only while ATTACH is available; [Dismiss] |

  Attached instances: Details states "Only mpv's log is visible for attached players; some GPU faults may not be detected."
- **Attach dialog:** list from `discover()`: "mpv — film.mkv (PID 4211)". Refused rows visible, disabled, with the refusal text ("Refused: socket owned by another user"). Orphan rows: [Remove leftover ButterEye filter] (confirm). Enter attaches, Delete removes an orphan (confirm), F5 re-scans. No polling while hidden.
- **Include line dialog:** the exact line in a `CopyField`; [Copy line]; [Add it for me…] → confirmation showing the one-line diff to `~/.config/mpv/mpv.conf` → `add_include_to_mpv_conf(consent=True)`.
- **CLI:** `play`, `attach`, `detach`, `detach.disable`, proposed `live.toggle`, `live.profile`, `orphans.remove`.

### 4.4 Profiles & Rules (`pages/profiles.py`, `dialogs/rule_edit.py`) — F15, §4.9, §5.3, §5.6

**Feature:** CONFIG. Edits a local `Config` copy (frozen; `replace`). Title shows "Profiles & Rules — unsaved changes" in text plus `*`. [Save] (Ctrl+S) / [Revert].

- **Profiles tab.** List: built-ins tagged "Built-in" (text), read-only, [Duplicate] [Reset to default]; custom: [New] (Ins) [Duplicate] [Rename] (F2) [Delete] (Del; blocked with reason "Used by rule 2" if referenced). Form:

  | Field | Control |
  |---|---|
  | Name | `QLineEdit` |
  | Engine | combo Automatic / RIFE (Vulkan) / RIFE · TensorRT (experimental) / MVTools (CPU); unavailable entries disabled, reason in accessible description and a visible note line; TRT listed only after opt-in |
  | Model | combo from `models()` for that backend ("extra", "unpinned" labelled); if MODELS unavailable, free text with note |
  | Scale | spin, TRT only; for RIFE (Vulkan) a note "At 4K, RIFE (Vulkan) works at a smaller size when the speed test says it can't keep up." (§5.3; v4.x models ignore `uhd`) |
  | Target | radios Match my display / Double (2×) / Fixed rate + `FractionEdit` (n/d) |
  | Scene-cut sensitivity | slider + spin (both expose the value) |
  | Buffered / concurrent frames | spins with "Automatic" checkbox (§4.4 default shown), inside "Expert" disclosure |
  | HDR | Skip (recommended) / Pass through (experimental) — disabled with "Needs compatibility test M0(d)" while HDR_PASSTHROUGH unavailable |

  `validate_config()` on every edit (debounced 150 ms); issues inline under the field (badge + text); Save moves focus to the first invalid field.
- **Rules tab.** Note "First matching rule wins." `QTableView` # / When / Use profile; When is a sentence ("fps ≤ 30 and height ≤ 1080"). [Add] (Ins) [Edit] (Enter) [Delete] (Del) [Move up] (Alt+Up) [Move down] (Alt+Down). Drag optional; keyboard mandatory. Rule edit dialog: one row per §4.9 key, each with its own "Use" checkbox (unset never looks like 0).
- **Test the rules** panel: fps (`FractionEdit`), width, height, HDR class, display Hz, interlaced, path; [Use current session] (enabled when `current_session_facts()` is not None). Output from `explain_rules()`: "Rule 2 matched → Fast", each prior step "skipped: fps 60 > fps_max 30".
- **States.** `used_defaults` (BE-2001): banner "config.toml line 12, column 5: <cause>. Defaults are in use." editors read-only, [Open folder], [Start fresh…] (confirm "Saving replaces the file; a .bak is kept"). `read_only` (BE-2002): banner "Created by a newer ButterEye — read-only." Save disabled. Unknown keys: info "3 unknown settings will be kept." Save `ConfigConflict`: dialog "config.toml changed outside ButterEye." [Reload theirs] [Overwrite]. On page show with no local edits and a new revision → reload silently.
- **CLI:** edit `config.toml`; `doctor` validates; proposed `profiles.list`, `profiles.explain`.

### 4.5 Render (`pages/render.py`, `dialogs/render_job.py`) — §7, F16

**Features:** RENDER, RENDER_HDR10.

- **Queue table:** Source, Output, Profile, Status (badge + word), Progress (`QProgressBar` format "42% · 61.3 fps", indeterminate when total unknown, never backwards), Phase, ETA, Encoder. Keys: Ctrl+N add, Del cancel (confirm) or forget (finished), Alt+Up/Alt+Down reorder queued, Ctrl+L open job log. One job at a time. No Pause, no "stop and keep" — not shown at all (§7.6 deferred).
- **Notice** when a session is active: "Playback is running: renders use lower priority." (§4.11)
- **Add job dialog:** source (dialog or drop) → `render_probe()` (loading state). Shows HDR class and outcome (HDR10 allowed only with x265/SVT-AV1; HLG/DV refused BE-4003 with §7.4 text), VFR notice "Will be converted to constant frame rate" (§7.3), stream inventory "Kept: 2 audio, 3 subtitles, chapters, 1 attachment", remux tool ("mkvmerge" or "mkvmerge not installed — using ffmpeg" + `CopyField` `sudo dnf install mkvtoolnix`), profile combo, encoder combo from `encoders` only, output path (default `<stem>.buttereye.mkv` beside the source, MKV only), overwrite confirmation, free space vs estimate. `refusal` or BE-4004 → [Add to queue] disabled with the reason visible beside it.
- **Finished:** [Show in folder] (`QDesktopServices`); cancelled: "Cancelled — partial files removed."
- **Stale jobs** (on show): banner "A render from a previous session is still running (PID 1234)." [Stop it and delete partial files] [Leave it].
- **CLI:** `render` per job; queue order has no CLI counterpart (GUI orchestration; CLI → GUI coverage holds).

### 4.6 Benchmark (`pages/bench.py`, `widgets/bench_plot.py`) — F14, §5.3

**Feature:** BENCH.

- **Form:** source "Current session (1920×1080 @ 23.976)" or custom W×H @ fps; "Full matrix (720p–2160p, slow)"; "Also test my own file…"; [Run] / [Cancel]. Before running: the top 2 candidates listed.
- **Empty:** "No measurements yet. ButterEye measures inside mpv with generated test clips — none of your videos are used."
- **Running:** one `OpRow` per configuration/pass ("RIFE (Vulkan) v4.26 — run 2 of 3 — in mpv").
- **Results:** `QTableView` (Config, vspipe fps, in-mpv fps, Real-time Yes/No, Repeatable Yes/No (CoV), startup s, reload s, VRAM, GPU faults "0" / "1 — unstable" / "unknown (log not readable)") + `BenchPlot` (horizontal bars, in-mpv solid, vspipe hatched, target line labelled, numbers on bars, palette colours + hatch patterns, `Qt.NoFocus`, accessible description "Best: v4.26, 74 fps in mpv, real time"). A faulting config is never "recommended". The table is the accessible source of truth.
- **Actions:** [Use recommended] → `apply_bench()`; History combo from `bench_history()`.
- **CLI:** `bench`, proposed `bench.apply`.

### 4.7 System (`pages/system.py`, `widgets/findings_view.py`) — F1, F9, F21

**Features:** DOCTOR, HARDWARE, REPORT.

- Summary line; `FindingsView` (shared with the wizard) grouped Blocking/Degraded/OK/Info, Section column; detail pane with cause, fix, commands, evidence; [Copy all as text].
- **Sections:** mpv, In-mpv probe, Packages (incl. ncnn variant "bundled ncnn 0^20250503git305837fd"; Fedora-ncnn variant → DEGRADED BE-1022), Vulkan, GPU fault history (Xid; unreadable → INFO BE-1031 "Couldn't read the kernel log; GPU faults can't be checked", never OK), Conflicting mpv settings, Render tools (ffms2, mkvmerge, 7z), TensorRT (only after opt-in, tagged "Experimental"; before: one INFO row "Experimental NVIDIA TensorRT: off" [Turn on…]).
- **Hardware box:** GPUs, Vulkan devices (llvmpipe "software, not used"), decode vs interpolation device warning (§4.5).
- **Plugins table** (`plugins()`): name, package, version, active copy, loads in mpv, licence files.
- **Actions:** [Run checks again] (F5), [Create bug report…] (save dialog, "Include file names and home paths" unchecked; engines never included), [Run setup again].
- **Loading** shows section names as they finish (from `Progress.phase`).
- **CLI:** `doctor`, `doctor.report`, `plugins.list`.

### 4.8 Storage (`pages/storage.py`, `dialogs/consent.py`) — §4.6, F10

**Features:** STORAGE, CLEAN, MODELS, MODEL_DOWNLOADS.

- **Table:** Location, Purpose (`note`), Path (selectable), Size (human, "0 bytes" when empty — true), Deletable (Yes / "No — installed by dnf"). RPM rows show `CopyField` "sudo dnf remove <package>" for orientation. Row checkbox for `clean_target` rows; [Clean selected…] → confirmation listing exact paths and sizes → `clean()` progress → "Freed 1.2 GB." [Open folder].
- **Models:** table Name / Kind / Backend / Size / Licence; packaged rows not removable; [Remove] (confirm); [Download…] → `consent` dialog (URL, size, licence, SHA-256; nothing pre-checked) → `model_install` byte progress + Cancel; hash mismatch BE-6001 "Hash mismatch (expected X, got Y); not installed." no override; [Install from file…] asks for a SHA-256 if not in the manifest, result labelled "unpinned".
- **CLI:** `clean`, `models.*`, proposed `clean.dry_run`.

### 4.9 About (`pages/about.py`, `dialogs/text_viewer.py`) — F18, §8.4

**Feature:** LICENCES (ButterEye's own notices never gated).

- **About tab:** name, version, copyright, no-warranty statement, licence name, [View full licence] (shipped `COPYING` in a read-only `QTextBrowser`, never only a URL), [View output permission] (§8.4 text), Source code link (COPR SRPM until a repository exists). `LegalNotices.missing` → DEGRADED banner "Licence file not found at <path> (BE-1021). This is a packaging bug." — never hidden.
- **Licences tab:** `third_party()` table Name / Version / SPDX / How it's used / Shipped by ButterEye (Yes (COPR) / No) / Detected; [View licence text] opens each `text_files`. Must include rows for RIFE-ncnn-Vulkan, **ncnn and glslang (bundled, conveyed)** with the §8.2 SPDX expressions, MVTools, RIFE ncnn models (full MIT texts), vsmlrt.py (if installed), PySide6/Qt (in-process), mpv, VapourSynth, FFmpeg, ffms2, mkvtoolnix (separate programs, detected). Downloaded ONNX rows carry the vs-mlrt GPL-3.0 pointer (§5.4).
- All text read-only, selectable, keyboard-scrollable.
- **CLI:** `licence`, `version`.

---

## 5. Real now vs unavailable

**Rule.** No placeholder numbers, sample sessions or demo results in the shipped GUI. Fakes live only in `buttereye/core/testing/` and tests. A feature is either real (data from this machine) or shows its `CapState`.

### 5.1 Implement for real now (U2, U3)

| Area | Source on the dev box |
|---|---|
| Config load/save/validate/migrate, unknown-key preservation, rule explain (F15) | pure Python |
| Paths + runtime-dir 0700 check | XDG env |
| Hardware (F9) | sysfs, `vulkaninfo -j` (CPU devices filtered), `nvidia-smi` subprocess |
| Doctor: mpv version, `--vf=help`, host binary, conflicting user settings, mpvSockets | mpv 0.41.0 |
| Doctor: in-mpv probe | VS R72, `/usr/lib64/buttereye/vapoursynth/{librife.so,mvtools.so}` |
| Doctor: packages, `bundled(ncnn)` provide, `%license` presence | `rpm -q` (spike RPMs 9.33-0.2.spike, 29.2-0.1.spike, models 9.33-0.1.spike) |
| Doctor: ffms2 / mkvmerge / trtexec / 7z presence | real "missing" findings for ffms2, mkvmerge, trtexec |
| Doctor: Xid scan | `journalctl -k` readable; real m0f Xid 13 lines present |
| Backend status + selection (§5.3, without bench) | over the report |
| Setup plan + apply: smoke test (vspipe on `smoke.vpy` + in-mpv probe), config write last | as above |
| Storage sizes, clean of user dirs (all empty today), packaged model listing | filesystem, RPM data |
| Legal notices, third-party licence detection | shipped files, `/usr/share/licenses/buttereye-*` |

### 5.2 Unavailable states — exact wording

`unavailable_text()` returns these (English msgids). Format in the panel: **headline**, then body, then code `BE-xxxx` (selectable), then commands, then `CliHint`.

| Feature | Reason today | Headline | Body |
|---|---|---|---|
| LIVE | NOT_IMPLEMENTED (M1) | "Live playback control isn't in this build yet." | "This build can check your system and edit profiles. Playing and connecting to mpv arrive in a later build. Nothing is connected to mpv." BE-9001 |
| ATTACH | NOT_IMPLEMENTED | "Attaching to an mpv you started yourself isn't in this build yet." | "Use Open to let ButterEye start mpv for you." BE-9001 |
| DISCOVER | NOT_IMPLEMENTED | "Finding mpv players you started yourself isn't in this build yet." | "Players that ButterEye started are listed on the Play page." BE-9001 |
| ORPHANS | NOT_IMPLEMENTED | "Removing a leftover ButterEye filter from another player isn't in this build yet." | "Close that mpv and start it again to clear the filter." BE-9001 |
| BENCH | NOT_IMPLEMENTED (M2) | "The benchmark isn't in this build yet." | "No measurements are shown until ButterEye has measured your GPU itself." BE-9001 |
| RENDER | NOT_IMPLEMENTED (M3); once built, MISSING_DEPENDENCY here | "Offline render isn't in this build yet." / "Render needs ffms2." | — / "ffms2 reads your video for VapourSynth. Install it, then press F5." BE-4001, `sudo dnf install ffms2` |
| RENDER (degraded note) | mkvmerge missing | — | "mkvmerge isn't installed, so ffmpeg will remux. Install mkvtoolnix for the preferred path." `sudo dnf install mkvtoolnix` |
| RENDER_HDR10 | needs x265/SVT-AV1 encoder | "HDR10 render needs the x265 or SVT-AV1 encoder." | "Your ffmpeg doesn't list libx265 or libsvtav1." |
| REPORT | NOT_IMPLEMENTED | "Bug reports can't be created in this build yet." | "Copy the check results with Copy all as text instead." BE-9001 |
| MODEL_DOWNLOADS | NOTHING_LISTED | "No downloadable models are listed in this release." | "Packaged models are installed with dnf. You can install a model archive from a file." |
| TRT | OPT_IN_REQUIRED | "Experimental NVIDIA TensorRT: off." | "It uses pre-release software you install yourself and may break on updates." [Turn on…] |
| TRT (after opt-in) | MISSING_DEPENDENCY | "TensorRT isn't set up." | "trtexec and vstrt were not found. See the TensorRT items on the System page." BE-1050 |
| HDR_PASSTHROUGH | SPIKE_PENDING | — (disabled option) | "Needs compatibility test M0(d)." |
| any | BLOCKED_BY_DOCTOR | "Setup isn't finished." | "{n} problem(s) need fixing on the System page." [Open System] |
| CONFIG | NOT_IMPLEMENTED (only before U2 lands) | "Profiles can't be edited in this build yet." | BE-9001 |

Live verdicts: no "real-time capable" text appears without a bench result; seek-latency numbers are shown as measured values only, no pass/fail (M0(b) open). RIFE-ncnn in mpv with the bundled build (9.33-0.2.spike) was played on the dev box on 2026-10-07 (README Status): ACTIVE, 1280×720 24 fps at 60 fps with v4.25-lite (2.5×), no new Xid faults; at 1080p it reached only ~46 fps with `concurrent-frames=4`, below real time for 2× of 23.976 fps; the default is now `concurrent-frames=8` with `gpu_thread` 4 (~56–58 fps, SCOPE §4.4, 2026-10-07). The m0f spike record still needs this section (m0f "Still open" predates it).

---

## 6. Test plan

Setup: `python3 -m venv --system-site-packages .venv && .venv/bin/pip install pytest pytest-qt ruff mypy`. GUI tests: `QT_QPA_PLATFORM=offscreen QT_ACCESSIBILITY=1`. Markers: `integration` (real mpv/vspipe, `--vo=null --ao=null`), `gpu`, `devbox`, `manual`. All files under `tests/`.

| File | Owner | Asserts |
|---|---|---|
| `tests/core/test_types_frozen.py` | U1 | every exported type is a frozen slotted dataclass/enum; `typing.get_type_hints` resolves no PySide6 type |
| `tests/core/test_errors.py` | U1 | codes unique, exit codes ∈ {1,2,3,4}; `docs/errors.md` lists every code |
| `tests/core/test_commands.py` | U1 | every id renders; unknown placeholder raises; proposed ids listed |
| `tests/core/test_capabilities.py` | U1 | missing provider → NOT_IMPLEMENTED; `NotAvailable` raised before any I/O (monkeypatched `subprocess`/`open` never called) |
| `tests/core/test_fakecore_contract.py` | U1 | FakeCore has every public `ButterEye` member with identical signatures (`inspect.signature`); same contract suite runs against real providers where present |
| `tests/core/test_ops_cancel.py` | U1 | `sh -c 'sleep 100 \| sleep 100'` in its own session; cancel kills the group ≤ 5 s, partials deleted, `CANCELLED` |
| `tests/core/test_config_roundtrip.py` | U2 | load/save preserves content and unknown keys; v0 migration + `.bak`; newer schema → read-only; invalid TOML → line/col, defaults |
| `tests/core/test_config_conflict.py` | U2 | stale `expected_revision` → `ConfigConflict`; atomic write |
| `tests/core/test_rules.py` | U2 | first-match, each key, `RuleTrace.steps` reasons, Fraction fps (24000/1001) |
| `tests/core/test_validate.py` | U2 | field paths, ncnn+scale rejected, fps target needs fraction |
| `tests/core/test_paths.py` | U3 | XDG table; unsafe runtime dir → BE-2010, never chmod |
| `tests/core/test_hw_fixtures.py` | U3 | vulkaninfo fixtures: llvmpipe never selected, two-GPU deterministic |
| `tests/core/test_doctor_fixtures.py` | U3 | each check from recorded command output → expected finding codes; total budget < 10 s with fakes |
| `tests/core/test_xid_parser.py` | U3 | m0f lines, pid match, unreadable → BE-1031 never OK |
| `tests/core/test_select.py` | U3 | §5.3 fixtures; no TRT without opt-in; CPU-only → MVTools/none with reason |
| `tests/core/test_setup_cancel.py` | U3 | cancel at each phase; diff XDG dirs: only logs exist (F0); re-run idempotent |
| `tests/core/test_storage_clean.py` | U3 | RPM-owned never removed; sizes; symlink not followed |
| `tests/core/test_licences.py` | U3 | ncnn + glslang rows conveyed; missing file → BE-1021 |
| `tests/integration/test_doctor_devbox.py` | U3 | `devbox`: real doctor, probe loads both plugins, real "ffms2 missing" |
| `tests/boundary/test_gui_import_ban.py` | U4 | subprocess `python -I`: build MainWindow on FakeCore, every page/dialog, 500 ms loop; `sys.modules` has no `vapoursynth`, `vsmlrt`, `tensorrt`, `pynvml`, no `PySide6.*` outside {QtCore, QtGui, QtWidgets}; `/proc/self/maps` no `libnvinfer`, `libcuda`, `libnvidia-ml`, `libvapoursynth` (driver GL/EGL allowed, Q18) |
| `tests/boundary/test_import_guard.py` | U4 | meta_path finder names the importing frame on a banned import |
| `tests/boundary/test_static_bans.py` | U4 | AST: no Qt in `core/`; `gui/` imports only `buttereye.core.api`; no `subprocess`/`socket`/`asyncio.create_subprocess*` in `gui/`; no `preexec_fn`; network modules only in `core/plugins/download.py` (F20); no `QSystemTrayIcon`; no colour literals in `setStyleSheet`, no `setPixelSize`, no constant `setFixed*` |
| `tests/boundary/test_core_no_qt.py` | U4 | subprocess `import buttereye.core.api`; no `PySide6*` in `sys.modules` |
| `tests/gui/test_bridge.py` | U4 | slots on GUI thread; owner destroyed → no callback; cancel reaches op; non-BE exception → INTERNAL; 10k progress/s → ≤ 12 updates per op, last = final, monotonic; shutdown timeout → None; watchdog banner on 6 s block then clears |
| `tests/gui/test_shell.py` | U4 | Ctrl+1…7, F5, F6, F1, Ctrl+Q; single-instance flock; quit dialog variants and policies passed |
| `tests/gui/test_ts_fresh.py` | U4 | `tools/extract_ts.py` output equals committed `.ts`; ≥ 1 context per page |
| `tests/gui/test_pseudo_locale.py` | U4 | pseudo translator; every visible label/button/header starts with `[!!`; layouts fit in scroll area |
| `tests/gui/test_a11y_sweep.py` | U4 | per page and dialog: every TabFocus widget has a non-empty accessible Name; buddies set; Tab cycle visits every enabled control once with no trap |
| `tests/gui/test_theme_scale.py` | U4 | dark/light scheme switch repaints; 2× font: nothing below `minimumSizeHint` |
| `tests/gui/test_widgets.py` | U5 | StatusBadge text + icon per state; greyscale grab still distinct; CopyField copies; FractionEdit validation; DropZone rejects non-local; BenchPlot label contrast ≥ 4.5:1 in dark palette; StatePanel unavailable shows code + hint |
| `tests/gui/test_setup_wizard.py` | U6 | scenarios: blocking disables Next with text; TRT opt-in dialog; downloads unchecked; cancel at each page → FakeCore records no `setup_apply` call before page 6 and no completed apply after a page-6 cancel |
| `tests/gui/test_system_page.py` | U6 | grouping, TRT hidden pre-opt-in, unreadable journal never OK, report unavailable state |
| `tests/gui/test_storage_page.py` | U6 | RPM rows not cleanable; clean confirm lists paths; hash mismatch text has BE-6001 |
| `tests/gui/test_about_page.py` | U6 | all F18 items visible; missing file banner; ncnn/glslang rows |
| `tests/gui/test_sessions_page.py` | U7 | every FilterState × BypassReason × Health text; announcement on transitions only; unavailable shows zero rows; refused candidates visible+disabled; include-line write only after confirm |
| `tests/gui/test_profiles_page.py` | U8 | built-ins read-only; delete blocked when referenced; inline validation; Alt+Up/Down reorder; rule tester trace; used_defaults/read_only/unknown/conflict states |
| `tests/gui/test_render_page.py` | U9 | refusal disables Add with reason; encoder combo only probed; progress never backwards; stale-job banner; no Pause control exists |
| `tests/gui/test_bench_page.py` | U9 | table and plot show the same values; faulting config never recommended; empty/unavailable texts |
| `tests/integration/test_gui_devbox.py` | U4 | `devbox`: real core, System page shows real findings, Storage real sizes, About real files |
| later (M1/M3 owners) | — | `test_gui_close_keeps_mpv` (normal/SIGTERM/SIGKILL), `test_attach_restore`, `test_mpv_output_not_piped`, `test_stall_rollback` (`stall.vpy`), `test_render_cancel_group` real pipeline |
| manual (M4 checklist) | — | Orca walk-through on GNOME 48+ (F17 acceptance); Hyprland pass; X11 pass; 200% font scale |

**FakeCore scenarios** (`core/testing/scenarios.py`): `fresh_box` (no config), `devbox_now` (real-shaped report: ffms2/mkvmerge/trtexec missing, live/bench/render NOT_IMPLEMENTED), `all_ready`, `cpu_only`, `blocking_mpv`, `live_active`, `live_bypassed_each`, `stalled`, `gpu_fault`, `connection_lost`, `config_invalid`, `config_newer`, `config_conflict`, `render_refusals`, `bench_results`, `slow_core` (6 s block). Scenario data must use the real dataclasses; FakeCore is never imported outside tests (static ban).

---

## 7. Work breakdown

Nine units. Each owns its files exclusively (§1), its tests (§6) and nothing else. Contract changes go through §10, not ad-hoc edits. Every unit passes `ruff check`, `mypy --strict` on its files, and its tests.

**U1 — Core contract and FakeCore**
- Owns: `buttereye/__init__.py`, `core/__init__.py`, `core/{api,types,errors,events,ops,i18n,commands,capabilities}.py`, `core/testing/*`, `docs/errors.md`, `docs/spikes/m0h.md`, `spikes/m0h-qtasyncio/probe.py`, tests `test_types_frozen`, `test_errors`, `test_commands`, `test_capabilities`, `test_fakecore_contract`, `test_ops_cancel`.
- Depends on: nothing.
- Acceptance: §2 transcribed exactly; every facade method gated through the provider table, raising `NotAvailable` before I/O when absent; `render()` falls back to the msgid with `str.format_map(params)` when no catalogue; FakeCore implements every member with identical signatures and all named scenarios; m0h.md records the no-go (method list, 6.11.2, reproducer output) and states the thread fallback; `import buttereye.core.api` imports no subsystem module.

**U2 — Config, profiles, rules**
- Owns: `core/profiles/*`, tests `test_config_roundtrip`, `test_config_conflict`, `test_rules`, `test_validate`.
- Depends on: U1 types (codes against §2 now).
- Acceptance: providers match §1 signatures; stdlib `tomllib` read, writer emits valid TOML for every Config (own minimal writer; no new dependency); atomic save (tmp 0600 + rename) with sha256 revision; four built-ins per §4.9; F15 criteria pass.

**U3 — Environment core (paths, hw, doctor, selection, setup, storage, licences)**
- Owns: `core/paths.py`, `core/hw/*`, `core/doctor/*`, `core/backends/*`, `core/setup.py`, `core/storage.py`, `core/licences.py`, tests `test_paths`, `test_hw_fixtures`, `test_doctor_fixtures`, `test_xid_parser`, `test_select`, `test_setup_cancel`, `test_storage_clean`, `test_licences`, `integration/test_doctor_devbox`.
- Depends on: U1; U2 only through `profiles.config.save`/`default_config` signatures (setup's last step).
- Acceptance: providers match §1; all subprocesses `asyncio.create_subprocess_exec` with timeouts, no `preexec_fn`, no network; doctor < 10 s on the dev box; findings for this box: ffms2 missing (BE-4001-class finding in Render tools), mkvmerge degraded, trtexec absent only inside TRT section, Xid lines reported as BE-1030, bundled ncnn variant identified; nothing written outside logs until setup's final step.

**U4 — GUI foundation (bridge, app, shell, i18n tooling, boundary tests)**
- Owns: `gui/{__init__,__main__,app,bridge,context,main_window,theme,quit_dialog}.py`, `gui/pages/{__init__,base}.py`, `data/i18n/buttereye_en.ts`, `tools/extract_ts.py`, tests `boundary/*`, `test_bridge`, `test_shell`, `test_ts_fresh`, `test_pseudo_locale`, `test_a11y_sweep`, `test_theme_scale`, `integration/test_gui_devbox`.
- Depends on: U1 (FakeCore); pages via `PAGE_REGISTRY` lazy strings — a missing page module shows a built-in "Page not in this build" `StatePanel` so U4 is testable alone.
- Acceptance: §3 rules 1–9 implemented and tested; §4.1 shell, key map, close flow; flock single instance; `--fake <scenario>` dev flag allowed only when `BUTTEREYE_DEV=1` (never in shipped default path, test asserts); a11y sweep and import ban iterate the registry so new pages are covered automatically.

**U5 — Widgets and a11y helpers**
- Owns: `gui/a11y.py`, `gui/widgets/{__init__,status_badge,banner,state_panel,op_row,copy_field,cli_hint,fraction_edit,bench_plot,drop_zone}.py`, bundled SVG icons under `gui/icons/`, test `test_widgets`.
- Depends on: U1.
- Widget API (frozen for U6–U9):
  ```python
  def labelled(text: str, w: QWidget, *, description: str | None = None) -> tuple[QLabel, QWidget]
  def announce(w: QWidget, text: str, *, assertive: bool = False) -> None
  class StatusBadge(QWidget): set_state(kind: Literal["ok","degraded","blocking","info","busy","off"], text: str)
  class Banner(QFrame): __init__(kind, title, body="", code=None, commands=(), actions=()); @classmethod from_error(err: ButterEyeError, actions=()) -> Banner
  class StatePanel(QStackedWidget): show_empty(text, actions=()); show_loading(text, cancel: Callable|None=None); show_error(err); show_unavailable(feature: Feature, state: CapState, hint: CommandHint|None); show_content(); content: QWidget
  class OpRow(QWidget): __init__(label: str, cancel: Callable[[], None] | None); update(p: Progress); finish(ok: bool, text: str)
  class CopyField(QWidget): __init__(text: str, *, accessible_name: str)
  class CliHint(QWidget): set_hint(h: CommandHint | None)
  class FractionEdit(QLineEdit): value() -> Fraction | None; setValue(Fraction | None); valueChanged = Signal(object)
  class BenchPlot(QWidget): set_data(rows: Sequence[BenchMeasurement], target_fps: float | None)
  class DropZone(QFrame): fileChosen = Signal(Path); rejected = Signal(str)
  ```
- Acceptance: every widget has a name by construction; no colour literals; glyph+word per state; tests in §6 pass.

**U6 — Setup, System, Storage, About**
- Owns: `gui/widgets/findings_view.py`, `gui/pages/{system,storage,about}.py`, `gui/dialogs/{__init__,setup_wizard,trt_optin,consent,text_viewer}.py`, tests `test_setup_wizard`, `test_system_page`, `test_storage_page`, `test_about_page`.
- Depends on: U1, U4 (`Page`, `GuiContext`), U5.
- Acceptance: §4.2, §4.7, §4.8, §4.9 exactly; wizard never writes before Apply; works end-to-end on the dev box against real U2/U3 (`devbox` run: wizard reaches Finish with real smoke test or a real finding).

**U7 — Play (sessions)**
- Owns: `gui/pages/sessions.py`, `gui/dialogs/{attach,include_line}.py`, test `test_sessions_page`.
- Depends on: U1, U4, U5.
- Acceptance: §4.3 tables implemented for every enum value against FakeCore; on the real core today shows the LIVE unavailable panel with zero rows; include-line write only via confirm dialog; `current_session_facts()` supplied to `GuiContext` via a page signal U4 subscribes to (`facts_changed = Signal(object)` on `SessionsPage`).

**U8 — Profiles & Rules**
- Owns: `gui/pages/profiles.py`, `gui/dialogs/rule_edit.py`, test `test_profiles_page`.
- Depends on: U1, U4, U5; real behaviour from U2.
- Acceptance: §4.4 exactly; all edits via `dataclasses.replace`; `validate_config`/`explain_rules` called on GUI thread only (pure); save via bridge with `expected_revision`; F15 GUI side complete against real U2 on the dev box.

**U9 — Render and Benchmark**
- Owns: `gui/pages/{render,bench}.py`, `gui/dialogs/render_job.py`, tests `test_render_page`, `test_bench_page`.
- Depends on: U1, U4, U5.
- Acceptance: §4.5, §4.6 exactly against FakeCore; on the real core today both show their unavailable panels (BE-9001; Render switches to BE-4001 + `sudo dnf install ffms2` once RENDER is implemented and ffms2 is still missing); no Pause/stop-and-keep controls.

**Integration order.** U1 → U2 + U3 (real data) and U4 + U5 (shell) in parallel → U6–U9 in parallel. M4 exit additionally needs M1/M3 core providers for live/render/bench and the manual Orca pass.

---

## 8. Risks

1. **Thread bridge correctness** (QtAsyncio no-go). Single `CoreBridge`, frozen payloads, owner-bound replies, watchdog, asyncio debug in tests.
2. **Audio over a frozen picture / Xid 13** (m0f). Core health detector with auto-rollback; GUI shows ACTIVE only after `vf`+`gen` verified and frames flowing; never re-enables automatically; Xid scan after sessions and bench runs. Thresholds unvalidated until M1. Attached instances have weaker detection (IPC log only) — stated in the UI.
3. **mpv bundled-ncnn playback below real time at 1080p.** Played at 720p (ACTIVE, 60 fps, no new Xid) on 2026-10-07; 1080p reached ~46 fps with `concurrent-frames=4`; the default is now 8 with `gpu_thread` 4 (~56–58 fps, SCOPE §4.4). First experience may still be BYPASSED/STALLED on slower GPUs. Honest states, one-click MVTools fallback, bench offered at setup.
4. **Essentials-only unenforceable by packaging on Fedora** (QtCharts imports fine). Runtime `sys.modules` + `/proc/self/maps` check in a clean subprocess, meta_path guard in all GUI tests.
5. **CLI parity** (§4.1 rule). The §2.6 commands marked proposed were adopted into SCOPE §4.1 on 2026-10-07; the CLI itself is M1 work.
6. **No lupdate.** Stdlib extractor covers catalogue extraction; `.qm` compilation waits for `qt6-linguist` (`sudo dnf install qt6-linguist`) when translations arrive.
7. **Accessibility on the dev desktop.** Hyprland + Fusion is not the acceptance target; GNOME 48+ Orca pass is mandatory for F17; focus-frame proxy covers styles without focus rects.
8. **mpv lifetime and orphans.** Spawn-time rules (new session, file logs, socket server), SIGKILL tests (M1), orphan banner at start.
9. **Orphaned renders after a GUI crash** (no pdeathsig). Job manifests store pgid; stale-job banner.
10. **Two controllers on one mpv** (GUI + CLI). `gen = max(seen)+1`; restore set stored in the runtime dir keyed by socket; single GUI instance.
11. **Config edited by CLI and GUI.** `expected_revision` + BE-2003 dialog.
12. **Dev box ≠ task description** (Hyprland, not COSMIC). Tests do not assume a desktop; portal colour scheme verified in the M5 GNOME/KDE/X11 passes.

---

## 9. Out of scope for M4 GUI

Tray, global shortcuts, embedded player, render pause/stop-and-keep/resume, preserve-VFR, MP4, HLG/DV render, HDR live passthrough unless M0(d) passes, any network UI.

---

## 10. SCOPE amendments (applied 2026-10-07)

The owner approved all four; they are in SCOPE.md (§2 goal 9, §4.1, §12 M0(h)/M4, F17, Appendix B), together with RIFE-ncnn `concurrent-frames=8` (§4.4).


1. §4.1: QtAsyncio no-go (M0(h)) → thread bridge is the GUI design; record in §12 M0(h).
2. §4.1 CLI: add the `proposed` commands of §2.6 (`attach --enable/--disable/--profile`, `detach --orphans`, `bench --apply`, `clean --dry-run`, `profiles list|explain`, `setup --trt-experimental`).
3. §6 F17: allowed in-process PySide6 modules = {QtCore, QtGui, QtWidgets}; `QtNetwork`/`QtDBus` banned.
4. §2 goal 9: catalogue extraction via `tools/extract_ts.py` until `lupdate` is available.

---

## 11. Providers for U10/U11 (added at implementation)

Added by U1 on 2026-10-07. **Additive only:** nothing in §2 changed. U10 (live play) and U11 (bench) code against exactly this section; the stand-in modules in `tests/core/test_provider_context.py` are a runnable example of every signature below.

### 11.1 Resolution rules

- Every facade method names one provider `module.attr` (the `*_REF`-style constants at the top of `buttereye/core/api.py`). The method resolves it with `capabilities.resolve()` on each call: a module already in `sys.modules` is used; otherwise `importlib.util.find_spec` decides presence (no subsystem code runs for a missing module), then `import_module`. A module that exists but raises on import is logged and treated as absent. A missing module or attribute raises `NotAvailable(feature, state)` before any I/O.
- `Capabilities` (static part) = "every ref of the feature resolves" per `capabilities.FEATURE_REQUIRES`:

| Feature | Required providers (`buttereye.core.` omitted) |
|---|---|
| `CONFIG` | `profiles.config.load`, `profiles.config.save`, `profiles.rules.validate`, `profiles.rules.explain`, `profiles.defaults.builtin_profiles`, `profiles.defaults.default_config` |
| `DOCTOR` | `doctor.run.doctor`, `doctor.run.plugins`, `profiles.config.load` |
| `HARDWARE` | `hw.detect.hardware` |
| `SETUP` | `setup.plan`, `setup.apply`, `doctor.run.doctor`, `backends.select.backends`, `backends.select.select`, `profiles.config.load` |
| `REPORT` | `doctor.report.bundle` (reserved, unassigned) |
| `DISCOVER` | `mpvctl.session.discover` (U10) |
| `LIVE` | `mpvctl.session.play`, `mpvctl.session.sessions`, `mpvctl.session.set_interpolation`, `mpvctl.session.detach`, `mpvctl.session.apply_profile`, `mpvctl.session.step_down`, `mpvctl.session.close` (U10), `profiles.config.load` |
| `ATTACH` | `mpvctl.attach.attach` (reserved, unassigned — U10 must **not** create `mpvctl/attach.py`) |
| `ORPHANS` | `mpvctl.orphans.remove_orphan_filter` (reserved, unassigned — U10 must **not** create `mpvctl/orphans.py`) |
| `BENCH` | `bench.runner.bench`, `bench.runner.history`, `bench.runner.apply` (U11), `profiles.config.load` |
| `RENDER`, `RENDER_HDR10` | `render.jobs.probe`, `render.jobs.enqueue`, `render.jobs.jobs`, `render.jobs.move`, `render.jobs.cancel`, `render.jobs.forget`, `render.jobs.stale_jobs`, `render.jobs.stale_stop`, `render.jobs.shutdown` (reserved, M3) |
| `STORAGE` / `CLEAN` / `MODELS` | `storage.entries` / `storage.clean` / `storage.models` |
| `MODEL_DOWNLOADS` | `plugins.download.listed`, `plugins.download.install` (reserved; absent → `NOTHING_LISTED`, code `None`, per §5.2) |
| `LICENCES` | `licences.third_party`, `licences.legal_notices` |
| `TRT` | `backends.trt.build_engine` (reserved, M2) |
| `HDR_PASSTHROUGH` | — (always `SPIKE_PENDING` until M0(d)) |

  Methods that need a provider outside their feature's table (gated with that feature): `add_include_to_mpv_conf` → `mpvctl.include.add_include` (`SETUP`); `model_install_file` / `model_remove` → `plugins.install.install_file` / `plugins.install.remove` (`MODELS`); `open(None)` → `paths.resolve`.
- Dynamic part (`capabilities.compute(cfg, report)`), precedence per feature: static `NOT_IMPLEMENTED`/`NOTHING_LISTED` > `OPT_IN_REQUIRED` > `MISSING_DEPENDENCY` > `BLOCKED_BY_DOCTOR` > `SPIKE_PENDING`. TRT: not opted in → `OPT_IN_REQUIRED`; opted in and a BLOCKING/DEGRADED `Section.TRT` finding → `MISSING_DEPENDENCY` `BE-1050` with the findings' commands; otherwise the static state. RENDER: a BLOCKING/DEGRADED finding with code `FFMS2_MISSING` → `MISSING_DEPENDENCY` `BE-4001` (`sudo dnf install ffms2`); `RENDER_HDR10` copies an unavailable RENDER state, else is `MISSING_DEPENDENCY` when U3 emits a BLOCKING/DEGRADED finding with id **`render.hdr10_encoder`** (ffmpeg lists neither libx265 nor libsvtav1). A blocking report → `BLOCKED_BY_DOCTOR` (Msg param `n` = number of blocking findings) for `LIVE`, `ATTACH`, `BENCH`, `RENDER`, `RENDER_HDR10`.
- **Facade gating is static only.** Dynamic states are advisory for the GUI; e.g. `play()` on a blocked box is not refused by the facade — the provider fails with its own error.

### 11.2 `ProviderContext` (`buttereye.core.api`)

One per `ButterEye` instance, handed to every U10/U11 (and reserved) provider as the first argument. Loop thread only.

```python
class ProviderContext:
    @property
    def paths(self) -> Paths: ...
    @property
    def closing(self) -> bool: ...  # True once ButterEye.close() started
    def config(self) -> Config:
        ...  # current in-memory config; defaults if no file;
        # NotAvailable(Feature.CONFIG) without U2

    def config_load(self) -> ConfigLoad | None: ...
    def config_revision(self) -> str: ...  # "" before a load / when no file
    def last_report(self) -> DoctorReport | None: ...
    def capabilities(self) -> Capabilities: ...  # cached; no probing
    def emit(self, ev: Event) -> None: ...  # transitions: never dropped, in order
    def emit_coalesced(self, key: str, ev: Event, *, min_interval_s: float = 0.25) -> None:
        ...
        # counter-only SessionChanged; key = the SessionId; latest wins; <= 1 per interval per key;
        # droppable under backpressure (subscriber then gets EventsDropped and resyncs)

    def forget_coalesced(self, key: str) -> None: ...  # call when a session ends
    def spawn(self, coro: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
        ...
        # background task owned by the core; cancelled (and awaited <= 1 s) by close();
        # exceptions are logged; raises ButterEyeError(INTERNAL) once closing

    def state(self, key: str, factory: Callable[[], S]) -> S:
        ...
        # per-core-instance singleton for provider state (use "mpvctl.session", "bench.runner");
        # never keep state in module globals (tests open several cores per process)

    async def save_config(self, cfg: Config, *, expected_revision: str) -> str:
        ...
        # through the facade: ConfigConflict on a stale revision; emits ConfigChanged + CapabilitiesChanged

    def refresh_capabilities(self) -> Capabilities: ...  # recompute + emit CapabilitiesChanged
```

### 11.3 `OpContext` and process helpers (`buttereye.core.ops`)

Operation-returning facade methods (`play`, `bench`, and the reserved `report_bundle`, `model_install`, `model_install_file`) pass the running op's `OpContext` as `op=`:

```python
class OpContext:
    op_id: OpId; kind: OpKind                           # read-only properties
    def progress(self, p: Progress) -> None: ...        # a ProgressSink; foreign/empty op_id is re-stamped
    def report(self, phase: Msg, *, done: int | None = None, total: int | None = None,
               unit: Literal["frames", "bytes", "steps", "checks"] = "steps",
               rate: float | None = None, eta_s: float | None = None, detail: Msg | None = None) -> None: ...
    def add_partial(self, path: Path) -> None: ...      # deleted if the op fails or is cancelled
    def keep(self, path: Path) -> None: ...             # unregister a finished file
    async def spawn(self, argv: Sequence[str], *, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL,
                    env: Mapping[str, str] | None = None, cwd: Path | None = None) -> asyncio.subprocess.Process: ...
        # own session/process group; SIGTERM -> 5 s -> SIGKILL when the op ends in ANY way.
        # Never use it for mpv in play(): mpv must outlive the op and the GUI (§3 rule 9).

async def spawn_group(argv, *, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, env=None, cwd=None) -> asyncio.subprocess.Process
async def terminate_group(proc, *, grace_s: float = 5.0) -> None     # SIGTERM group, SIGKILL after grace_s
async with process_group(argv, ..., grace_s=5.0) as proc: ...        # terminates the group on exit/cancel
```

Cancellation reaches a provider as `asyncio.CancelledError` at its current `await`; the `Operation` then terminates the `op.spawn` groups, deletes partials and finishes `CANCELLED`. Providers clean up anything else (e.g. the mpv they launched) in `except CancelledError: ...; raise`. Providers that take a plain `ProgressSink` (U2/U3, §1) may build `Progress(OpId(""), ...)`; the op stamps its id. A non-`ButterEyeError` escaping a provider becomes `ButterEyeError(INTERNAL, detail=traceback)`.

### 11.4 U10 — `buttereye/core/mpvctl/session.py` (exact signatures)

```python
async def play(ctx: ProviderContext, file: Path, *, profile_id: str | None, op: OpContext) -> SessionId
async def sessions(ctx: ProviderContext) -> tuple[SessionSnapshot, ...]
async def set_interpolation(ctx: ProviderContext, sid: SessionId, enabled: bool, *, force: bool = False) -> SessionSnapshot
async def detach(ctx: ProviderContext, sid: SessionId, policy: DetachPolicy) -> None
async def apply_profile(ctx: ProviderContext, sid: SessionId, profile_id: str | None) -> SessionSnapshot
async def step_down(ctx: ProviderContext, sid: SessionId) -> SessionSnapshot
async def discover(ctx: ProviderContext) -> tuple[InstanceCandidate, ...]
async def close(ctx: ProviderContext, policy: DetachPolicy, *, timeout_s: float) -> ShutdownReport
# optional, same unit (consented include writer, §4.6/R4); absent -> add_include_to_mpv_conf is NOT_IMPLEMENTED:
# buttereye/core/mpvctl/include.py
async def add_include(ctx: ProviderContext) -> str   # returns the one-line diff applied; "" if already present
```

Rules:
- `play` completes (returns the `SessionId`) once mpv is launched with `--input-ipc-server` in `ctx.paths.runtime_dir`, the IPC connection is up and `SessionAdded` (filter `PENDING`) was emitted. Filter activation, bypass, rollback and health arrive later as events. The facade already refused URLs (`BE-3007`) before calling `play`. Spawn mpv per §3 rule 9 (`start_new_session=True`, `stdin=DEVNULL`, output to `ctx.paths.logs_dir / f"mpv-{sid}.log"`), not with `op.spawn`; kill it yourself if `play` is cancelled before returning.
- Keep the session registry in `ctx.state("mpvctl.session", factory)`. Long-lived per-session tasks (IPC reader, stats observer, health detector) go through `ctx.spawn(..., name=f"mpvctl-{sid}")`.
- Events: `SessionAdded`, transition `SessionChanged` (filter/health/gen/profile/notice changes) and `SessionEnded`/`HealthChanged`/`Notice` via `ctx.emit`; counter-only `SessionChanged` via `ctx.emit_coalesced(sid, ..., min_interval_s=0.25)` (≤ 4 Hz, R11); `ctx.forget_coalesced(sid)` after `SessionEnded`.
- `set_interpolation`/`apply_profile`/`step_down` return the snapshot after the change is verified (`gen = max(seen)+1`, §2.5); a rejected change raises `FilterFailed(VF_ROLLED_BACK, rolled_back=True)` after rollback. Unknown `sid` → `ButterEyeError(IPC_LOST)`. `set_interpolation(sid, True, force=True)` sets a per-session "smooth anyway" flag (`_Session.forced`, kept until the session ends): a decision that would be `NO_REALTIME` instead runs the first candidate engine with no speed-test cap (still within the 5× multiplier and never when the video is already at the target), with `snapshot.notice` = "Smoothing anyway, past what the speed test says this computer can keep up with, so it may stutter." The health detector and the RIFE→MVTools switch still guard it. Before anything is forced, a `NO_REALTIME` decision tries **smaller sizes** (`decide.smaller_sizes`: 1440p, 1080p, 720p below the source, same shape, even sides): each size's live caps come from `sustainable_fps` at that size less the cost of decoding and shrinking the source (`decide.shrink_cap`: ~4 ms per output frame for 4K, scaled by pixels; measured on the dev box, RIFE v4.26 4K→1080p 47.5 fps and 4K→720p 86 fps against a 61 fps 1080p benchmark). `decide.pick_size` takes the measured GPU engine at the largest size where it keeps up (in auto, only if it at least doubles the rate), else any engine at the largest size `pick_engine` finds, else (forced) the first engine uncapped at the smallest size ("Smoothing anyway at {height}p, …"). The filter's `user_data` gets `"size": [w, h]` and the script shrinks with Spline36 before interpolating; mpv scales the output to the window (ButterEye's mpv profile sets `auto-window-resize=no` so the window keeps its size). `snapshot.notice` = "Smoothing at {height}p so this computer can keep up; the picture is scaled back up to your window." **Logging:** every engine decision ("<title>: smoothing 24 → 48 fps with RIFE (Vulkan) <model> at 1280 × 720 (video 3840 × 2160)"), every unsmoothed decision with its reason, and every `HealthChanged`/`Notice` the session emits (with its code, auto action and up to 10 evidence lines) are written at INFO to `gui.log`.
- `discover` returns only ButterEye-launched instances for now (sockets in `ctx.paths.runtime_dir`), with `managed_by_us=True`; emit `OrphansFound` when it finds launched instances this core does not hold. Attach and orphan-filter removal stay `NOT_IMPLEMENTED` (§11.1).
- `close` runs when `ctx.closing` is already True: detach every live session with `policy` within `timeout_s`, return `ShutdownReport(detached, failed, jobs_cancelled=())`. The facade wraps it in `wait_for(timeout_s + 1)`; on timeout it reports every non-ended session as failed with `IPC_LOST`. After `close`, the facade cancels everything spawned via `ctx.spawn`. mpv keeps playing (§4.10).
- Read rules/profiles via `ctx.config()` and the U2 functions (`buttereye.core.profiles.rules.explain`) directly; core-to-core imports are allowed (only `api.py` must not import subsystems at module load).

### 11.5 U11 — `buttereye/core/bench/runner.py` (exact signatures)

```python
async def bench(ctx: ProviderContext, req: BenchRequest, *, op: OpContext) -> BenchResult
async def history(ctx: ProviderContext) -> tuple[BenchResult, ...]
async def apply(ctx: ProviderContext, result: BenchResult, label: str, *, expected_revision: str) -> str
```

Rules: `bench` runs every vspipe/mpv measurement through `op.spawn` (or `ops.process_group`), registers generated test clips with `op.add_partial`, reports one phase per configuration/pass (`Msg("{config} — run {n} of {total} — in mpv", …)`), appends the result to `ctx.paths.bench_file` atomically (0600 temp + rename) and returns it. `history` reads `bench_file` (missing file → `()`); it is also used by `ButterEye.select_backend` when present. `apply` builds the new `Config` from `ctx.config()` and saves it with `ctx.save_config(new, expected_revision=…)` (never writes `config.toml` itself); an unknown `label` → `ButterEyeError(BENCH_FAILED)`. A configuration with `gpu_faults` > 0 is never `recommended` (§4.6).

### 11.6 Reserved signatures (no owner yet; absent → the feature is unavailable)

```python
# buttereye/core/mpvctl/attach.py                       Feature.ATTACH
async def attach(ctx: ProviderContext, cid: CandidateId, *, profile_id: str | None) -> SessionId
# buttereye/core/mpvctl/orphans.py                      Feature.ORPHANS
async def remove_orphan_filter(ctx: ProviderContext, cid: CandidateId) -> None
# buttereye/core/render/jobs.py                          Feature.RENDER (M3)
async def probe(ctx, src: Path, *, profile_id: str | None = None) -> RenderProbe
async def enqueue(ctx, spec: RenderJobSpec) -> JobId
async def jobs(ctx) -> tuple[RenderJobState, ...]
async def move(ctx, job: JobId, delta: int) -> None
async def cancel(ctx, job: JobId) -> None
async def forget(ctx, job: JobId) -> None
async def stale_jobs(ctx) -> tuple[StaleJob, ...]
async def stale_stop(ctx, job: JobId) -> None
async def shutdown(ctx, *, cancel: bool, timeout_s: float) -> tuple[JobId, ...]   # cancelled job ids
# buttereye/core/doctor/report.py                        Feature.REPORT
async def bundle(ctx, dest: Path, *, redact: bool, op: OpContext) -> Path
# buttereye/core/plugins/download.py                     Feature.MODEL_DOWNLOADS
async def listed(ctx) -> tuple[DownloadItem, ...]
async def install(ctx, name: str, *, op: OpContext) -> ModelEntry
# buttereye/core/plugins/install.py                      Feature.MODELS
async def install_file(ctx, file: Path, sha256: str | None, *, op: OpContext) -> ModelEntry
async def remove(ctx, name: str) -> None
# buttereye/core/backends/trt.py                         Feature.TRT (M2; marker only, no facade method)
def build_engine(...) -> ...
```

### 11.7 Clarifications made at implementation (no §2 signature changed)

1. `unavailable_text(feature, state)` returns **one** `Msg`: the first line of the rendered text is the headline, the rest the body (§5.2 wording). `CapState.message` produced by the core is that same `Msg` (with params, e.g. `{n}`), so a panel shows one of them, not both, and splits on the first `\n` to style the headline.
2. `OperationCancelled` raised by the generic `Operation` wrapper carries `ErrorCode.INTERNAL` as a neutral placeholder; branch on the type, never the code. Its `exit_code` property is 130.
3. Additive helpers: `ErrorCode.from_code(str)`, `ButterEyeError.exit_code`, `NotAvailable.for_state(feature, state)`, `Operation.wait()/done/error/last_progress`, `OpContext`, `ops.spawn_group/terminate_group/process_group`, `events.EventBus`, `EVENT_TYPES`, `capabilities.unavailable_state/compute/resolve`. `CommandHint.text()` uses `shlex.join`.
4. Command templates (§2.6): `[--opt {x}]` optional group; `[--full]` switch emitted when param `full` is `1|yes|true`; `--{target}...` repeated per comma-separated item (`command_hint("clean", target="engines,models")`); `live.toggle` takes `toggle="enable"|"disable"`; `profiles.explain` takes `fps,width,height,hdr,display_hz,interlaced,path`. Unknown params and missing required placeholders raise `KeyError`.
5. `include_line()` is computed by the facade from `Paths.mpv_include` (home contracted to `~`); no provider.
6. `FakeCore` subclasses `ButterEye` (so bridges typed `ButterEye` accept it): `await FakeCore.for_scenario("live_active").open(paths)` or `await open_fake(name)`. Test hooks: `fake_emit`, `fake_set_session`, `fake_set_capability`, `fake_block_loop`, `fake_call_count`, `fake_calls`, `fake_scenario`. Every call is recorded in `fake_calls` before gating. Extra scenario `apply_fails` (F4 rollback on `apply_profile`). In `config_conflict`, the first `load_config()` returns `rev-1`; a save with it raises `ConfigConflict(on_disk_revision="rev-disk")`, after which `load_config()` returns `rev-disk` ("Reload theirs") and a save with `rev-disk` succeeds ("Overwrite").

### 11.8 Review-fix notes, 2026-10-07 (no §2 signature changed)

Live play (`mpvctl`):

1. `sessions()` lists live players only; `end()` removes a session from the registry (mpv is started with `os.posix_spawnp(setsid=True)` and survives the core, so nothing needs the ended entry). The Play page remembers dropped sids and never re-adds a row for them.
2. `CONNECTION_LOST` **without** `SessionEnded` means "mpv is not responding" (IPC silent ≥ 3 s plus an unanswered probe). It returns to OK by itself ("mpv is responding again."). While it lasts, `set_interpolation`/`apply_profile` raise IPC_LOST and `detach(DISABLE_FILTER)` leaves the filter in place.
3. New `LiveOptions` fields: `start_deadline_s` (15 s: PENDING with no video → `notice` "mpv hasn't opened the video yet…", shown as the Play headline), `probe_after_s`, `unresponsive_after_s`, `probe_timeout_s`, `log_check_s`, `display_debounce_s`.
4. When the user's mpvSockets script moves mpv's socket, `launcher.connect` adopts `/tmp/mpvSockets/<pid>` after the §4.3 checks and only if the peer pid is the mpv it started (SCOPE §4.3). `launcher.MPVSOCKETS_DIR`.
5. Logs: `--term-status-msg=`; `launcher.prune_logs` (newest 5, ≤ 14 days) runs on each `play()`, not at core open (open does no writes); running logs are capped at 5 MB.
6. Live and the bench write `doctor.gpufault.write_session_marker(paths, pids=…)` before RIFE-ncnn starts.

Doctor: new INFO row `gpu_fault.xid_older` (faults before the RIFE package's install time or the last session marker; never blamed, no engine advice). New `gpufault.write_session_marker`, `read_session_marker`, `doctor_xid_findings`; `RpmInfo.installtime`.

Core helpers: `ops.run_to_completion`, `ops.owned_groups`, `ops.kill_owned_groups`, `Operation.kill_children`; `commands.cli_available()` and `CLI_MISSING_NOTE` (every `CliHint` shows the note while no `buttereye` CLI is installed); `EventBus.publish` delivers a pending coalesced update for the same session before a transition.

Config: for a built-in profile an optional field set to Automatic is written as an explicit marker (`model = ""`, `scale = 0.0`, `buffered_frames = 0`, `concurrent_frames = 0`); the loader reads these as Automatic for every profile. `save` refuses (BE-INTERNAL) a config that would read back differently.

Bench: a model that faulted the GPU in ≥ 2 of the last 10 stored benchmarks on the same GPU is not recommended (`repeat_faulters`, `recommend(avoid=)`). The GPU override is resolved like doctor (`hw.detect.choose_device`) and stored as the lowercase UUID.

GUI shell: `bridge.shutdown(live=)`, `shutdown_timed_out`, `close_wait_s`, `CLOSE_MARGIN_S`, `a11y.message_box` (plain text; required for every message box), `a11y.SourcePlurals`, `labelled(wrap=False)`, `theme.fixed_font`/`apply_fixed_font`/`accessible_palette`.


### 11.9 Reliable live decisions (2026-10-07)

Field report (RTX 4090, 3440×1440 @ 180 Hz, Hyprland): live playback kept stalling (BE-3004). RIFE-ncnn tops out at ~60–70 fps in mpv at 1080p whatever the model (bench `mpv_fps`: v4.26 61.2, v4.22-lite 62.6, v4.18 59.4; MVTools 102.3); a 1920×1080 file tagged 24.0031 fps missed the benchmark rule (`fps_max` exactly 24000/1001) and fell to a display-rate rule that asked RIFE for 4–5× of 180 Hz; the cap ignored benchmarks at other heights; Xid 109 faults happen now and then with RIFE on the display GPU. The core now decides as below. **No §2 signature changed**; everything is additive. The simple window stores one non-builtin profile `simple` (backend `auto` | `rife-ncnn` + model | `mvtools`; target `2x` | `fps:60` | `display`) with one catch-all rule and calls `apply_profile` on live sessions; these rules make that profile just work.

1. **Live cap** (`session.LIVE_HEADROOM = 1.25`). Cap = benchmark `mpv_fps` / 1.25. The bench times mpv untimed with `--vo=null`; real playback also presents every frame to the display and shares the GPU with the compositor, so it sustains less. 1080p RIFE 61.2 → 49 live: enough for 24 → 48, not 30 → 60. The bench's own "real time" verdict (`REALTIME_HEADROOM = 1.15`) is unchanged.
2. **Any benchmark size counts.** A measurement at another size is scaled by the pixel-count ratio (bench w×h / video w×h); `"<label> @ <h>p"` entries of a full benchmark use their own size. The measurement closest in pixel count wins, then the newest, then the fastest. Still skipped: faulted measurements, failed in-mpv passes (vspipe speed stored as `mpv_fps`), and (RIFE only) results from another GPU.
3. **Engine after the target** (`decide.pick_engine`). Candidates in order RIFE-ncnn (the profile's model, else the best installed default), then MVTools; each gets its cap and `choose_target()`.
   - *Auto*: RIFE-ncnn is eligible only with benchmark data for it; without, Auto uses MVTools with no notice (an unmeasured RIFE-ncnn is used only when it is the only engine installed). Among the engines that reach a target, the first whose target at least doubles the source (≥ 1.98×) or equals the best target wins, so a display target never trades 24 → 60 on MVTools for 24 → 30/45 on RIFE.
   - *Pinned* RIFE-ncnn (profile or Setup choice) that can't reach its target falls back to MVTools for this file with the notice "The GPU engine can't keep up with {target} fps at this size, so ButterEye is using CPU smoothing for this video." (`{target}` is the uncapped target, e.g. `60`). Pinned MVTools never moves to RIFE.
   - Nothing reaches a target → BYPASSED `NO_REALTIME`, `snapshot.notice` = "This video is too demanding to smooth in real time on this computer, so it plays without smoothing." (the OSD keeps `BYPASS_TEXT`).
   - A bypass clears `backend`, `model`, `target_fps` and `multiplier` in the snapshot (no engine runs).
4. **Display target** follows §5.6 with the scaled cap, costed in interpolated frames (`decide.load_rate`: 2 × the frames per second that do not land on a source frame, compared with the 2× benchmark rate): `"display"` takes the lowest refresh/k ≥ 1.98× that fits, `"display-max"` the highest; when none fits, both take 2× (if below the display rate), else the highest refresh/k that fits. 24 fps at 180 Hz: `"display"` → 60 uncapped, 48 (2×) with RIFE's ~49 or MVTools' ~82 (24 → 60 costs 96); `"display-max"` → 90 uncapped.
5. **Benchmark rule** (`bench.runner.RULE_FPS_TOLERANCE = 101/100`): `fps_max` = measured rate × 1.01 as an exact Fraction (24000/1001 → 24240/1001 ≈ 24.22), so 24.0031 and 24 match; 25 does not.
6. **Stall or GPU fault on RIFE-ncnn → MVTools once.** A stall (STALLED or DEVICE_LOST), a lost device logged while the filter runs, or a new Xid found while RIFE-ncnn runs switches *this session* to MVTools (an in-session engine override, `_Session.backend_override`): `HealthChanged(<STALLED|DEVICE_LOST|GPU_FAULT>, …, auto_action="ButterEye switched this video to CPU smoothing.")`, a new verified gen with MVTools, then `HealthChanged(OK, "Frames are flowing again with CPU smoothing.")`. While the override holds, `snapshot.notice` = "The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing." and `snapshot.backend` is MVTools. The override is never lifted in that session (not by `set_interpolation`, `apply_profile`, another file or a display change); a new `play()` starts fresh. If MVTools then stalls, interpolation turns off as before ("Video stalled while audio kept playing; interpolation was turned off.") and is not retried; if MVTools can't reach the target, the video plays unsmoothed (`NO_REALTIME`). Without MVTools installed, the RIFE stall turns interpolation off as before. An Xid found later while MVTools runs leaves it running and emits `Notice(RIFE_GPU_FAULT)` "The GPU reported a fault while RIFE-ncnn was running earlier; {engine} is running now."

## 12. Simple window (2026-10-07 redesign)

Owner feedback: the multi-page window "looks like a clone of existing proprietary tools; it should be its own thing, aiming to be simpler." The owner chose (1) one small window: drop or open a video and it plays smooth, one **Smooth motion** switch and one **Smoothness** choice, everything else silent, a **Details** area for system information and licences; (2) the user picks the **Target**: "Double (2×)", "60 fps" or "Your display (N Hz)".

```
┌ ButterEye ───────────────────────────────────────────────────────────┐
│ (icon) BUTTEREYE   (gold wordmark)                                    │
│        Smoother motion for the videos you play in mpv.                │
│ [problem line, only when needed]                                      │
│ ╭───────────────────────────────╮ ╭─────────────────────────────────╮ │
│ │  Drop a video to play smooth  │ │   Drop a video to save a copy   │ │
│ │        [OPEN VIDEO…]          │ │       [CONVERT A VIDEO…]        │ │
│ ╰───────────────────────────────╯ ╰─────────────────────────────────╯ │
│ Smooth motion (●  ) On  Target (Double (2×)▾)  Smoothness (Auto    ▾) │
│ Picture  (Standard   ▾) Resolution (Lower if needed▾)                 │
│ NOW PLAYING                                                           │
│ ╭───────────────────────────────────────────────────────────────────╮ │
│ │ movie.mkv                                               ✓ Smooth  │ │
│ │ 24 → 48 fps · GPU smoothing                                       │ │
│ │ [PAUSE] [LET GO] [SAVE COPY…] [SMOOTH HDR]            [COPY LOG]  │ │
│ ╰───────────────────────────────────────────────────────────────────╯ │
│ SAVING COPIES                                                         │
│ ╭───────────────────────────────────────────────────────────────────╮ │
│ │ movie.smooth.mkv                                     Saving 45 %  │ │
│ │ ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │ │
│ │ [CANCEL]                                              [COPY LOG]  │ │
│ ╰───────────────────────────────────────────────────────────────────╯ │
├───────────────────────────────────────────────────────────────────────┤
│ ✓ Ready.                                                   DETAILS ▸  │
└───────────────────────────────────────────────────────────────────────┘
```

### 12.1 Modules

- `gui/simple_window.py` — `SimpleWindow` (default 760 wide; the height always equals the content's preferred height, growing and shrinking as rows come and go, up to 90 % of the screen, so there is never empty space; only the width is resizable and saved in `QSettings` `simple/size`; never narrower than the layout needs; never scrolls; settings two per row, each choice as wide as its longest text), `SessionRow`, and pure helpers (`simple_profile`, `simple_config`, `row_view`, `short_rate`, `without_code`, `gpu_smoothing_available`).
- `gui/details_dialog.py` — `DetailsDialog`: a modeless `QDialog` with tabs System, Speed test, Storage, About, built from the existing pages with their own `GuiContext` (`go(page_id)` switches tabs; `status`/`announce` go to the dialog's status line). Pages refresh on first show of their tab and receive every core event. The Speed test tab hides "Use recommended" (rules come from the simple profile). Esc and Close close it.
- `gui/widgets/switch.py` — `Switch(QCheckBox)`: painted track and knob plus the word On/Off (state by position and word, never colour alone); accessible name is the setting, the state is the check box state; 2 px Highlight focus ring.
- `gui/widgets/primary_button.py` — `PrimaryButton`: Button = Highlight, ButtonText = HighlightedText; re-derived on `QGuiApplication.paletteChanged` (a widget with its own palette gets no palette events).
- `gui/icons/buttereye.svg` — the app icon (a butter droplet with an eye; AGPL), window icon and header logo.
- `app.py` — opens `SimpleWindow`. `--classic` (with `BUTTEREYE_DEV=1`, like `--fake`) opens the old `MainWindow`, which stays importable and tested.

### 12.1.0 HDR on a row (2026-10-10, F7)

The row of an HDR10 video (`source.hdr_class` HDR10) shows **Smooth HDR** while it plays unsmoothed (`bypass` HDR_SKIP; accessible name "Smooth HDR videos (experimental)") and **Play HDR as is** while it is smoothed. Either saves the simple profile's `hdr` ("passthrough" / "skip") and re-applies to every playing video; the status line says "HDR videos will be smoothed (experimental)." or "HDR videos will play as they are." A smoothed HDR10 video's notice is "HDR smoothing is experimental." unless a more urgent note applies.

### 12.1.1 Copy log (2026-10-10)

Every Now playing and Saving copies row has **Copy log** (accessible name "Copy the log for {file}"). It puts on the clipboard what ButterEye logged about that file (the core keeps the latest 400 lines per session and per job in memory, `core/filelog.py`, records tagged by `filelog.for_item`) followed by the last 200 lines of that file's own tool log (`mpv-<sid>.log`, or vspipe and ffmpeg's `render-<job>.log`), under a header with the version, file, engine and state. Core API: `session_log(sid)`, `job_log(job)`. The status line says "Log for {file} copied." A row disappears when its mpv closes, and its log button with it.

### 12.1.2 Resolution (2026-10-10)

**Resolution**: "Lower if needed" (default) or "Always full size", stored as `[general] full_size`. With full size on, a video the speed test says can't be smoothed at its own size is smoothed there anyway (uncapped, like Smooth anyway) instead of at a smaller size or in MVTools' block mode; the row says "Smoothing at full size as you chose; this computer may not keep up, so some frames may drop."

### 12.1.4 The speed test runs once per hardware (2026-10-10)

The automatic speed test (`FIRST_BENCH`) runs when no stored result was measured on a GPU that is in the computer now (`current_results`: the result's Vulkan UUID against the doctor report's devices; CPU-only results on a machine without a GPU). The TensorRT measurement runs once when no such result has a TensorRT measurement. Results live in `bench.json` and survive restarts and app updates; a new graphics card has a new UUID and is measured. Engines are cached separately per GPU, driver, TensorRT/vstrt version, model, size and flow scale (§4.6), so they are built once too.

### 12.1.3 What a row says (2026-10-10)

The detail line names what is smoothing the video and how: "24 → 48 fps · RIFE v4.26 on the GPU with TensorRT, motion estimated at half resolution, full size 3840 × 1616, HDR10" (engine and model, MVTools' fast block mode, half-resolution flow, the size it works at, "smoothed at W × H, scaled up" when smaller, HDR10), from the snapshot's `backend`, `model`, `smooth_size`, `flow_scale`, `mv_block` and `source`. When the video isn't smoothed the badge says "Not smoothed" and the reason line starts "Not smoothed:" with the cause and what to change (Target, Smoothness, Always full size, Smooth HDR, Resume, Copy log). A button that can't act for the row (Pause on an unsmoothed video) is hidden, not greyed out.

### 12.3 Carino Systems branding (2026-10-10)

Owner decision: ButterEye follows the Carino Systems visual language (branding.carino.systems) though it is not a Carino product; no Carino navbar, links or marks. `theme.ThemeController(brand=True)` (the default): the Carino palette (dark only, whatever the desktop scheme; `theme.CARINO` holds the tokens), still tuned by `accessible_palette()` so control outlines and drop-zone edges keep >= 3:1 and focus is the 2 px gold frame; Fusion with `FocusFrameStyle` painting rounded buttons (IBM Plex Sans semibold in sentence case, at least 2.1 text lines tall with 0.9 lines of extra side padding, `theme.BUTTON_MIN_LINES`, over WCAG 2.5.8's 24 px; the brand's small uppercase mono buttons were too hard to read in the app; the primary action filled gold) and Carino cards for rows (`CARD_PROPERTY`, decorative #262626 hairline); IBM Plex Sans for text; section headings as gold Plex Mono kickers; the app name as a Red Hat Display Black wordmark in the gold gradient (`widgets/wordmark.py`, still a QLabel reading "ButterEye"). No style sheets. Fonts come from Fedora (`ibm-plex-sans-fonts`, `ibm-plex-mono-fonts`, `redhat-display-fonts`, required by the RPM) and fall back to the system's. `set_brand(False)` restores the scheme-following palette with the butter accent (tests).

### 12.2 Choices → config

The three choices are one non-built-in profile, id `simple`, name "ButterEye": `sc_threshold` 0.12, `buffered_frames`/`concurrent_frames` None, `hdr` "skip", `scale` None. The rules become a single catch-all `Rule(RuleMatch(), "simple")`; built-in and other profiles are kept, `general`, `render` and unknown keys are untouched.

| Smoothness | backend | model |
|---|---|---|
| Auto (recommended) | `"auto"` | None |
| Best quality — GPU | `rife-ncnn` | `rife-v4.26_ensembleFalse` |
| Lighter — GPU | `rife-ncnn` | `rife-v4.22_lite_ensembleFalse` |
| CPU only | `mvtools` | None |

| Target | `Target` |
|---|---|
| Double (2×) (default) | `Target(X2)` |
| 60 fps | `Target(FPS, 60)` |
| Match your display (N Hz) | `Target(DISPLAY)`; N is the last live session's `display_fps` (remembered in `QSettings` `simple/display_hz`); "(N Hz)" is left out while unknown; tooltip: refresh/k, the lowest that at least doubles the video (60 on 180 Hz, 48 on 144 Hz, SCOPE §5.6) |
| Match your display, smoothest | `Target(DISPLAY_MAX)`; listed only while the "simple" profile already has `target = "display-max"` (config.toml), so other choice changes keep it instead of saving `"display"` |

- On every change: `save_config(cfg, expected_revision=<last revision>)`, then `apply_profile(sid, "simple")` for each live session that isn't paused (a paused one gets it when it resumes). A `ConfigConflict` reloads the config once and saves the same choice on top of it; a read-only (newer schema) config shows a problem line and nothing is written.
- At start, a config that already routes everything to `simple` is the truth (the combos show it). Any other config — first run, or one written by the classic window — is **adopted**: the current choices are saved as above. Hand-written rules are replaced at that point (pre-alpha; noted here on purpose).
- A `ConfigChanged` from elsewhere reloads the config and updates the combos.
- **Smooth motion** is a per-user convenience in `QSettings` (`simple/smooth`), never in `config.toml`. Off: `set_interpolation(sid, False)` for every live session, and every new video gets `set_interpolation(sid, False)` right after `play(file, profile_id="simple")`. On: `set_interpolation(sid, True)` for paused ones.
- GPU choices are hidden when `backends(report)` says RIFE-ncnn isn't available (fallback: the doctor probe's / `plugins()` RIFE plugin). A GPU choice already in use stays, labelled "(not available here)".

### 12.3 Silent start

1. `load_config()`. If no config exists and setup is available: status "Getting ready…", `doctor()`; blocking findings → problem line, stop. Otherwise `setup_plan(report, trt_experimental=False)` and `setup_apply(plan, SetupChoices(backend, False, frozenset(), run_smoke_test=True))` with the proposed backend (else the first available of RIFE-ncnn, MVTools). A failed smoke test → a warning problem line, not a stop. Then reload and adopt. `add_include_to_mpv_conf` is never called; `mpv.conf` is never written.
2. Config exists: adopt, then `doctor()` in the background ("Checking your system…") for the problem line and the GPU choices.
3. When `bench_history()` is empty (and nothing blocks): `bench(BenchRequest(1920, 1080, 24000/1001))` in the background, status "Measuring your GPU (about a minute)… N%" ("your computer" when RIFE can't run). `apply_bench` is never called. Playing works meanwhile (the core uses MVTools until it has RIFE data).
4. Status line: glyph + word, "Ready." when nothing runs. Errors from background work go to the log and, in one plain sentence, to the status or problem line.

### 12.4 Rows and wording

One row per live session (ended sessions disappear; `EventsDropped` resyncs from `sessions()`). Title (bold), status badge (glyph + word: Smooth / Smooth, some drops / Starting / Paused / Playing normally / Lost touch), "24 → 48 fps · GPU smoothing" (rates rounded the way people say them: 23.976 → 24, 119.88 → 120; engine "GPU smoothing", "CPU smoothing", "Smoothing paused", "Getting smoothing ready" or "Playing normally"), and one plain notice line: the session's `notice` (e.g. the core's "The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing."), else the bypass reason, else the health, else the latest `HealthChanged.reason`/`Notice` for that sid. A trailing "(BE-nnnn)" is stripped: **no error codes in the main window**; they live in Details (System). Actions: "Smooth anyway" (only on a "too demanding" row: `BYPASSED` + `NO_REALTIME`; `set_interpolation(sid, True, force=True)`; accessible name "Smooth {title} anyway"), "Pause smoothing"/"Resume smoothing" (`set_interpolation`) and "Let go" (`detach(KEEP_FILTER)`; tooltip and accessible description: "mpv keeps playing; ButterEye stops managing it.").

The problem line (inline, never modal) shows one sentence and a "Details" button that opens Details on System: live playback unavailable, a blocking doctor finding ("ButterEye can't smooth video yet: <finding title>."), no usable engine, a failed smoke test, a read-only config, an unresponsive engine, a startup failure.

### 12.5 Identity and accessibility

- (Before the Carino branding, §12.3, and still used with `set_brand(False)`.) One brand colour: butter gold `theme.BUTTER_HSL` = (45, 204, 140) ≈ #E8B931, applied by `theme.butter_palette()` to Highlight and Accent with the palette's darkest text/background colour as HighlightedText (dark text on gold), then tuned by `accessible_palette()` like every role: in light schemes it settles on a deeper amber (≥ 3:1 against the window for the focus frame, ≥ 4.5:1 under the dark text); in dark schemes it stays bright gold. `ThemeController(app, accent=True)` is the default. It is defined with `QColor.fromHsl` from the named triple — the single sanctioned colour constant in `gui/`.
- Two rounded drop zones side by side, filled with Base (`DropZone.set_filled`): **play** (left, `dropZone`, plays the file smooth in mpv) and **convert** (right, `convertZone`, opens the Save a smooth copy dialog for the file). Larger heading (1.8×), generous spacing in font-height units, glyph + word statuses everywhere.
- **No scrolling (owner decision 2026-10-09).** The window has no scroll area: the drop zones shrink (to 6 font heights) to make room for Now playing and Saving copies rows, and the window's minimum size follows its content.
- Keyboard: Tab order Open video → Convert a video → Smooth motion → Target → Smoothness → row buttons → Details. Ctrl+O opens, F1 opens Details, Ctrl+Q quits (the usual quit dialog when players are live). The drop zones are not Tab stops (their buttons are the keyboard path). Every control has an accessible name; combos and the switch have buddy labels with mnemonics.

### 12.6 Tests and screenshots

`tests/gui/test_simple_window.py` (FakeCore): exact saved profile and rule, revisions, conflict reload, apply_profile for live sessions, the switch and new videos, rows and plain notices from events (no codes), GPU-choice hiding, the silent first run (doctor → setup_plan → setup_apply → save → bench, no `apply_bench`, no include), Details with four tabs and Esc, the `--classic` gate, an a11y sweep (names, buddies, Tab cycle) and a dark-palette render. `tests/boundary/test_gui_import_ban.py` also builds the simple window and every Details tab. `tools/screenshots.py` writes `simple-first-run`, `simple-empty`, `simple-playing`, `simple-playing-dark` and `simple-details-{system,speed,storage,about}.png` over the real core (`--simple-only` skips the classic shots).


### 12.7 Save a smooth copy (offline render, SCOPE §7.8)

Entry points: the convert drop zone (drop a file, or its "Convert a video…" button, also Ctrl+Shift+O, which opens the file chooser) and "Save smooth copy…" on every Now playing row (the row's file). The convert zone and the row button are hidden while `Feature.RENDER` is unavailable for a reason the user can't fix here (`NOT_IMPLEMENTED`); with `MISSING_DEPENDENCY` (ffms2 missing) they stay visible and the dialog opens on the one-line reason and its command (`sudo dnf install ffms2`).

**Dialog** (`gui/convert_dialog.py`, `ConvertDialog`, modal to the window): heading "Save a smooth copy of <file>". On open it calls `render_probe(src, profile_id="simple")` (busy line "Reading the video…").
- **Refusal** (`probe.refusal`, e.g. HLG/Dolby Vision, an undecodable codec, or HDR10 in the SDR-first build): its title in one sentence, without a code; Save stays disabled.
- **Size** combo: "Original (3840 × 2160)" then each smaller `RenderSize` as "1080p (1920 × 1080)"; each entry ends in the estimated time, "about 1 h 40 min" (duration × target fps ÷ `est_fps`; omitted when `est_fps` is None). Default: the original size when its estimate is ≤ 2 × the video's duration, else the largest size that is, else the smallest.
- **Smoothness target**: Double (2×) / 60 fps, defaulting to the window's Target (Your display → Double). Sent as `RenderJobSpec.target`.
- **Format** combo from `probe.encoders` with plain names: "HEVC (NVIDIA GPU)" for `hevc_nvenc`, "AV1 (NVIDIA GPU)" `av1_nvenc`, "HEVC (CPU, x265)" `libx265`, "AV1 (CPU, SVT-AV1)" `libsvtav1`, "H.264 (CPU, x264)" `libx264`; others by their ffmpeg name. Default per §7.5.
- **Save to**: `<stem>.smooth.mkv` beside the source; "Change…" opens a save dialog (MKV only). An existing file asks "Replace it?" and sets `overwrite`.
- Notes (plain lines, no codes): the VFR warning, "Audio, subtitles and chapters are kept.", and without mkvmerge "Using ffmpeg to copy the other streams (install mkvtoolnix for the preferred way)."
- [Save] → `render_enqueue(spec)`; [Cancel] closes. Esc cancels.

**Progress rows** under Now playing ("Saving copies"): file name, a progress bar ("12 % · about 1 h 20 min left"), the phase in words (Waiting / Converting / Finishing / Done / Failed / Cancelled), [Cancel] while queued or running (`render_cancel`), [Show file] when done (opens the folder via `QDesktopServices`), [Remove] for finished rows (`render_forget`). Rows follow `JobChanged`; on start the window seeds them from `render_jobs()`. Progress never moves backwards. Errors show the rendered cause without its code. Quitting with a running job uses the existing quit dialog wording (§3).

Tests (`tests/gui/test_convert.py`, FakeCore): entry points hidden/visible by capability, probe call with `profile_id="simple"`, size labels and the default-size rule, target mapping, encoder names, output name and overwrite, the enqueued spec, refusal disables Save, progress rows (monotonic, cancel, remove), a11y names.
