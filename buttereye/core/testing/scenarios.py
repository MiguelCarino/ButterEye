# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Named FakeCore scenarios (docs/design/GUI.md §6). TEST DATA ONLY.

Every value is built from the real §2 dataclasses. Nothing here may be imported
by shipped code paths (static ban, U4).
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

from buttereye import __version__
from buttereye.core.capabilities import unavailable_state
from buttereye.core.errors import (
    ButterEyeError,
    ConfigConflict,
    ErrorCode,
    FilterFailed,
)
from buttereye.core.events import Event, HealthChanged, SessionChanged
from buttereye.core.types import (
    BackendId,
    BackendStatus,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    BypassReason,
    CandidateId,
    CapState,
    CleanTarget,
    ComponentLicence,
    Config,
    ConfigIssue,
    ConfigLoad,
    Counters,
    DoctorReport,
    DownloadItem,
    Feature,
    FilterState,
    Finding,
    GeneralSettings,
    GpuInfo,
    HardwareInfo,
    HdrClass,
    Health,
    InstanceCandidate,
    JobId,
    LegalNotices,
    ModelEntry,
    ModelKind,
    Msg,
    OpState,
    Origin,
    Paths,
    PluginStatus,
    ProbeResult,
    Profile,
    Reason,
    RefusalReason,
    RenderDefaults,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
    RenderProbe,
    RenderSize,
    Rule,
    RuleMatch,
    Section,
    Selection,
    SessionId,
    SessionSnapshot,
    Severity,
    SourceFacts,
    StaleJob,
    StorageEntry,
    StorageKind,
    StreamInfo,
    Target,
    TargetKind,
    VulkanDevice,
)


@dataclass(frozen=True, slots=True)
class Scenario:
    """Everything FakeCore answers. Derive variants with ``dataclasses.replace``."""

    name: str
    description: str
    capabilities: Mapping[Feature, CapState]
    config_load: ConfigLoad
    report: DoctorReport | None  # last_report() right after open
    doctor_result: DoctorReport  # what doctor() returns
    hardware: HardwareInfo
    plugins: tuple[PluginStatus, ...]
    backends: tuple[BackendStatus, ...]
    selection: Selection
    missing_packages: tuple[str, ...] = ()
    dnf_lines: tuple[str, ...] = ()
    trt_downloads: tuple[DownloadItem, ...] = ()
    smoke_fps: float | None = 74.0
    smoke_finding: Finding | None = None
    sessions: tuple[SessionSnapshot, ...] = ()
    candidates: tuple[InstanceCandidate, ...] = ()
    bench_result: BenchResult | None = None
    bench_history: tuple[BenchResult, ...] = ()
    render_probes: Mapping[str, RenderProbe] = field(default_factory=lambda: MappingProxyType({}))
    render_jobs: tuple[RenderJobState, ...] = ()
    stale_jobs: tuple[StaleJob, ...] = ()
    storage: tuple[StorageEntry, ...] = ()
    models: tuple[ModelEntry, ...] = ()
    model_downloads: tuple[DownloadItem, ...] = ()
    hash_mismatch: frozenset[str] = frozenset()
    third_party: tuple[ComponentLicence, ...] = ()
    legal_notices: LegalNotices | None = None
    #: method name -> error raised by that call (after gating)
    errors: Mapping[str, ButterEyeError] = field(default_factory=lambda: MappingProxyType({}))
    #: config_conflict: the revision "on disk" that differs from the loaded one
    disk_revision: str | None = None
    #: (delay_s, event) emitted after open()
    script: tuple[tuple[float, Event], ...] = ()
    #: slow_core: the first ping() blocks the loop this long
    block_first_ping_s: float = 0.0
    #: TRT state after the user opts in
    trt_after_optin: CapState | None = None
    op_steps: int = 4
    op_step_s: float = 0.01
    job_total_frames: int = 240


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

UUID_4090 = "GPU-3f1c9a52-7d2e-4b8a-9c61-0e5d2b7a4f10"
UUID_LLVMPIPE = "llvmpipe-00000000-0000-0000-0000-000000000000"
PCI_4090 = "0000:01:00.0"
XID_LINES = (
    "NVRM: Xid 13, Graphics Exception: SKEDCHECK36_DEPENDENCE_COUNTER_UNDERFLOW failed",
    "NVRM: Xid 13, pid=48211, name=vo, Graphics Exception: channel 0x00000049, "
    "Class 0000c9c0 (compute)",
)


def fake_paths(root: Path | None = None, env: Mapping[str, str] | None = None) -> Paths:
    """Display-only Paths. FakeCore never reads or writes them."""
    if root is not None:
        cfg, data, cache, state, run = (
            root / d for d in ("config", "data", "cache", "state", "run")
        )
        home = root / "home"
    else:
        e = os.environ if env is None else env
        home = Path(e.get("HOME", "/home/user"))
        cfg = Path(e.get("XDG_CONFIG_HOME") or home / ".config")
        data = Path(e.get("XDG_DATA_HOME") or home / ".local/share")
        cache = Path(e.get("XDG_CACHE_HOME") or home / ".cache")
        state = Path(e.get("XDG_STATE_HOME") or home / ".local/state")
        run = Path(e.get("XDG_RUNTIME_DIR") or "/run/user/1000")
    state_dir = state / "buttereye"
    return Paths(
        config_file=cfg / "buttereye" / "config.toml",
        mpv_include=cfg / "buttereye" / "mpv" / "buttereye.conf",
        user_mpv_conf=home / ".config" / "mpv" / "mpv.conf",
        data_dir=data / "buttereye",
        cache_dir=cache / "buttereye",
        state_dir=state_dir,
        logs_dir=state_dir / "logs",
        jobs_dir=state_dir / "jobs",
        bench_file=state_dir / "bench.json",
        runtime_dir=run / "buttereye",
    )


def builtin_profiles() -> tuple[Profile, ...]:
    def p(pid: str, name: str, backend: BackendId, model: str | None, kind: TargetKind) -> Profile:
        return Profile(
            id=pid,
            name=name,
            backend=backend,
            model=model,
            scale=None,
            target=Target(kind),
            sc_threshold=0.12,
            buffered_frames=None,
            concurrent_frames=None,
            builtin=True,
        )

    return (
        p(
            "quality",
            "Quality",
            BackendId.RIFE_NCNN,
            "rife-v4.26_ensembleFalse",
            TargetKind.DISPLAY,
        ),
        p(
            "balanced",
            "Balanced",
            BackendId.RIFE_NCNN,
            "rife-v4.22_lite_ensembleFalse",
            TargetKind.DISPLAY,
        ),
        p("fast", "Fast", BackendId.RIFE_NCNN, "rife-v4.22_lite_ensembleFalse", TargetKind.X2),
        p("cpu", "CPU (MVTools)", BackendId.MVTOOLS, None, TargetKind.X2),
    )


def default_config(**general: object) -> Config:
    return Config(
        schema_version=1,
        general=dataclasses.replace(GeneralSettings(), **general),  # type: ignore[arg-type]
        profiles=builtin_profiles(),
        rules=(
            Rule(RuleMatch(fps_max=Fraction(30), height_max=1080), "quality"),
            Rule(RuleMatch(fps_max=Fraction(30)), "balanced"),
            Rule(RuleMatch(hdr_class=HdrClass.SDR, height_max=2160), "fast"),
        ),
        render=RenderDefaults(MappingProxyType({"nvidia": "hevc_nvenc", "cpu": "libx265"})),
        unknown=MappingProxyType({}),
    )


def config_load(
    cfg: Config | None = None,
    *,
    exists: bool = True,
    revision: str = "rev-1",
    read_only: bool = False,
    used_defaults: bool = False,
    issues: tuple[ConfigIssue, ...] = (),
) -> ConfigLoad:
    return ConfigLoad(
        config=cfg or default_config(),
        exists=exists,
        revision=revision if exists else "",
        read_only=read_only,
        used_defaults=used_defaults,
        issues=issues,
    )


def finding(
    fid: str,
    section: Section,
    severity: Severity,
    title: str,
    *,
    code: ErrorCode | None = None,
    cause: str = "",
    fix: str = "",
    commands: tuple[str, ...] = (),
    evidence: tuple[str, ...] = (),
    experimental: bool = False,
) -> Finding:
    return Finding(
        id=fid,
        section=section,
        severity=severity,
        code=code,
        title=Msg(title),
        cause=Msg(cause),
        fix=Msg(fix),
        commands=commands,
        evidence=evidence,
        experimental=experimental,
    )


def hardware_4090() -> HardwareInfo:
    return HardwareInfo(
        gpus=(
            GpuInfo(PCI_4090, "NVIDIA", "NVIDIA GeForce RTX 4090", "615.71.09", 24 * 2**30, "8.9"),
        ),
        vulkan=(
            VulkanDevice(0, UUID_4090, "NVIDIA GeForce RTX 4090", "NVIDIA", "discrete", 24 * 2**30),
            VulkanDevice(1, UUID_LLVMPIPE, "llvmpipe (LLVM 21.1.0, 256 bits)", "Mesa", "cpu", None),
        ),
        interpolation_device=UUID_4090,
        decode_device=PCI_4090,
        cpu_threads=32,
    )


def hardware_cpu_only() -> HardwareInfo:
    return HardwareInfo(
        gpus=(),
        vulkan=(
            VulkanDevice(0, UUID_LLVMPIPE, "llvmpipe (LLVM 21.1.0, 256 bits)", "Mesa", "cpu", None),
        ),
        interpolation_device=None,
        decode_device=None,
        cpu_threads=8,
    )


_LIC = Path("/usr/share/licenses")


def plugins_devbox() -> tuple[PluginStatus, ...]:
    return (
        PluginStatus(
            "RIFE-ncnn-Vulkan",
            "buttereye-vs-rife-ncnn",
            "9.33-0.2.spike",
            "buttereye",
            True,
            "bundled ncnn 0^20250503git305837fd",
            (
                _LIC / "buttereye-vs-rife-ncnn/LICENSE",
                _LIC / "buttereye-vs-rife-ncnn/ncnn-LICENSE.txt",
                _LIC / "buttereye-vs-rife-ncnn/glslang-LICENSE.txt",
            ),
        ),
        PluginStatus(
            "MVTools",
            "buttereye-vs-mvtools",
            "29.2-0.1.spike",
            "buttereye",
            True,
            None,
            (_LIC / "buttereye-vs-mvtools/COPYING",),
        ),
        PluginStatus("vstrt", None, None, "none", None, None, ()),
    )


def probe_devbox() -> ProbeResult:
    return ProbeResult("VapourSynth R72", "3.14.7", plugins_devbox()[:2], vsmlrt_importable=False)


def findings_ok() -> tuple[Finding, ...]:
    return (
        finding("mpv.version", Section.MPV, Severity.OK, "mpv 0.41.0"),
        finding("mpv.vf_vapoursynth", Section.MPV, Severity.OK, "mpv has the VapourSynth filter"),
        finding("mpv.host_binary", Section.MPV, Severity.OK, "mpv is a host binary"),
        finding("probe.plugins", Section.PROBE, Severity.OK, "Both plugins load inside mpv"),
        finding(
            "packages.rife",
            Section.PACKAGES,
            Severity.OK,
            "RIFE plugin: bundled ncnn 0^20250503git305837fd",
        ),
        finding("packages.licence_files", Section.PACKAGES, Severity.OK, "Licence files present"),
        finding("vulkan.loader", Section.VULKAN, Severity.OK, "Vulkan: NVIDIA GeForce RTX 4090"),
        finding(
            "conflicts.user_conf", Section.CONFLICTS, Severity.OK, "No conflicting mpv settings"
        ),
        finding("trt.off", Section.TRT, Severity.INFO, "Experimental NVIDIA TensorRT: off"),
    )


def finding_ffms2_missing() -> Finding:
    return finding(
        "render.ffms2",
        Section.RENDER,
        Severity.DEGRADED,
        "ffms2 is not installed",
        code=ErrorCode.FFMS2_MISSING,
        cause="Offline render reads video through ffms2.",
        fix="Install ffms2.",
        commands=("sudo dnf install ffms2",),
    )


def finding_mkvmerge_missing() -> Finding:
    return finding(
        "render.mkvmerge",
        Section.RENDER,
        Severity.DEGRADED,
        "mkvmerge is not installed",
        code=ErrorCode.PKG_MISSING,
        cause="ffmpeg will remux instead.",
        fix="Install mkvtoolnix for the preferred path.",
        commands=("sudo dnf install mkvtoolnix",),
    )


def finding_xid() -> Finding:
    return finding(
        "gpu_fault.xid",
        Section.GPU_FAULT,
        Severity.DEGRADED,
        "GPU fault in RIFE-ncnn",
        code=ErrorCode.RIFE_GPU_FAULT,
        cause="The kernel log has NVIDIA Xid 13 errors since the last RIFE-ncnn session.",
        fix="Use MVTools for affected files, or retry RIFE; faults may repeat.",
        commands=("journalctl -k -g Xid",),
        evidence=XID_LINES,
    )


def report(findings: tuple[Finding, ...], hw: HardwareInfo | None = None) -> DoctorReport:
    return DoctorReport(findings, hw or hardware_4090(), probe_devbox(), 3.2, trt_included=False)


def backends_devbox() -> tuple[BackendStatus, ...]:
    return (
        BackendStatus(
            BackendId.RIFE_NCNN, True, False, Msg("RIFE (Vulkan) on NVIDIA GeForce RTX 4090")
        ),
        BackendStatus(BackendId.MVTOOLS, True, False, Msg("MVTools on the CPU")),
        BackendStatus(
            BackendId.RIFE_TRT, False, True, Msg("Experimental NVIDIA TensorRT: off"), None
        ),
    )


def selection_rife() -> Selection:
    return Selection(
        BackendId.RIFE_NCNN,
        "rife-v4.26_ensembleFalse",
        "quality",
        Msg("RIFE (Vulkan) on NVIDIA GeForce RTX 4090"),
        False,
        (BackendId.RIFE_NCNN, BackendId.MVTOOLS),
    )


def _ni(f: Feature) -> CapState:
    return unavailable_state(f, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)


def caps_devbox_now() -> Mapping[Feature, CapState]:
    """Mirrors the real core today: live/bench/render/report not in this build."""
    states = {f: CapState(True) for f in Feature}
    for f in (
        Feature.LIVE,
        Feature.ATTACH,
        Feature.DISCOVER,
        Feature.ORPHANS,
        Feature.BENCH,
        Feature.RENDER,
        Feature.RENDER_HDR10,
        Feature.REPORT,
    ):
        states[f] = _ni(f)
    states[Feature.MODEL_DOWNLOADS] = unavailable_state(
        Feature.MODEL_DOWNLOADS, Reason.NOTHING_LISTED, None
    )
    states[Feature.TRT] = unavailable_state(Feature.TRT, Reason.OPT_IN_REQUIRED, None)
    states[Feature.HDR_PASSTHROUGH] = unavailable_state(
        Feature.HDR_PASSTHROUGH, Reason.SPIKE_PENDING, None
    )
    return MappingProxyType(states)


def caps_all_ready() -> Mapping[Feature, CapState]:
    states = {f: CapState(True) for f in Feature}
    states[Feature.TRT] = unavailable_state(Feature.TRT, Reason.OPT_IN_REQUIRED, None)
    states[Feature.HDR_PASSTHROUGH] = unavailable_state(
        Feature.HDR_PASSTHROUGH, Reason.SPIKE_PENDING, None
    )
    return MappingProxyType(states)


def _with(caps: Mapping[Feature, CapState], **changes: CapState) -> Mapping[Feature, CapState]:
    d = dict(caps)
    for k, v in changes.items():
        d[Feature(k)] = v
    return MappingProxyType(d)


def trt_missing_state() -> CapState:
    return unavailable_state(
        Feature.TRT,
        Reason.MISSING_DEPENDENCY,
        ErrorCode.TRT_UNSUPPORTED,
        commands=("contrib/build-vstrt.sh",),
    )


def storage_devbox(paths: Paths) -> tuple[StorageEntry, ...]:
    return (
        StorageEntry(
            StorageKind.CONFIG,
            paths.config_file.parent,
            0,
            False,
            False,
            Msg("Settings, profiles and rules"),
            None,
        ),
        StorageEntry(
            StorageKind.ENGINES,
            paths.cache_dir / "engines",
            0,
            True,
            False,
            Msg("TensorRT engines (experimental)"),
            CleanTarget.ENGINES,
        ),
        StorageEntry(
            StorageKind.MODELS,
            paths.data_dir / "models",
            0,
            True,
            False,
            Msg("Downloaded and extra models"),
            CleanTarget.MODELS,
        ),
        StorageEntry(
            StorageKind.DOWNLOADS,
            paths.cache_dir / "downloads",
            0,
            True,
            False,
            Msg("Downloads in progress"),
            CleanTarget.DOWNLOADS,
        ),
        StorageEntry(
            StorageKind.JOBS,
            paths.jobs_dir,
            0,
            True,
            False,
            Msg("Offline render jobs"),
            CleanTarget.JOBS,
        ),
        StorageEntry(
            StorageKind.LOGS,
            paths.logs_dir,
            4096,
            False,
            False,
            Msg("ButterEye and mpv logs (old ones pruned)"),
            None,
        ),
        StorageEntry(
            StorageKind.RUNTIME,
            paths.runtime_dir,
            None,
            False,
            False,
            Msg("Live scripts and sockets"),
            None,
        ),
        StorageEntry(
            StorageKind.RPM,
            paths.rpm_plugin_dir,
            15_000_000,
            False,
            True,
            Msg("Plugins installed by dnf (buttereye-vs-rife-ncnn)"),
            None,
        ),
        StorageEntry(
            StorageKind.RPM,
            paths.rpm_data_dir,
            420_000_000,
            False,
            True,
            Msg("Models installed by dnf (buttereye-rife-ncnn-models)"),
            None,
        ),
    )


def models_devbox() -> tuple[ModelEntry, ...]:
    root = Path("/usr/share/buttereye/rife-ncnn-models")
    names = (
        "rife-v4.26_ensembleFalse",
        "rife-v4.22_lite_ensembleFalse",
        "rife-v4.18_ensembleFalse",
    )
    return tuple(
        ModelEntry(
            n,
            ModelKind.PACKAGED,
            BackendId.RIFE_NCNN,
            root / n,
            True,
            100_000_000,
            "MIT",
            None,
            False,
        )
        for n in names
    )


def third_party_devbox() -> tuple[ComponentLicence, ...]:
    def c(
        name: str,
        ver: str | None,
        spdx: str,
        rel: str,
        conveyed: bool,
        detected: bool,
        files: tuple[Path, ...] = (),
    ) -> ComponentLicence:
        return ComponentLicence(name, ver, spdx, Msg(rel), conveyed, detected, files, None)

    rife = _LIC / "buttereye-vs-rife-ncnn"
    return (
        c(
            "RIFE-ncnn-Vulkan",
            "r9_mod_v33",
            "MIT AND WTFPL AND LGPL-2.1-or-later AND BSD-3-Clause",
            "loaded by mpv/vspipe",
            True,
            True,
            (rife / "LICENSE",),
        ),
        c(
            "ncnn (bundled)",
            "0^20250503git305837fd",
            "BSD-3-Clause AND BSD-2-Clause AND Zlib",
            "loaded by mpv/vspipe",
            True,
            True,
            (rife / "ncnn-LICENSE.txt",),
        ),
        c(
            "glslang (bundled)",
            "0^gita9ac7d5f",
            "BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND GPL-3.0-or-later WITH "
            "Bison-exception-2.2",
            "loaded by mpv/vspipe",
            True,
            True,
            (rife / "glslang-LICENSE.txt",),
        ),
        c(
            "MVTools",
            "29.2",
            "GPL-2.0-or-later",
            "loaded by mpv/vspipe",
            True,
            True,
            (_LIC / "buttereye-vs-mvtools/COPYING",),
        ),
        c(
            "RIFE ncnn models",
            "r9_mod_v33",
            "MIT",
            "data",
            True,
            True,
            (_LIC / "buttereye-rife-ncnn-models/LICENSE",),
        ),
        c(
            "PySide6 / Qt",
            "6.11.2",
            "LGPL-3.0-only OR GPL-3.0-only WITH Qt-GPL-exception-1.0",
            "in-process",
            False,
            True,
        ),
        c("mpv", "0.41.0", "GPL-2.0-or-later", "separate program", False, True),
        c("VapourSynth", "R72", "LGPL-2.1-or-later", "separate program", False, True),
        c("FFmpeg", "8.1.3", "GPL-3.0-or-later", "separate program", False, True),
        c("ffms2", None, "MIT", "loaded by vspipe", False, False),
        c(
            "mkvtoolnix",
            None,
            "GPL-2.0-or-later AND LGPL-2.1-or-later",
            "separate program",
            False,
            False,
        ),
    )


def legal_notices_fake() -> LegalNotices:
    share = Path("/usr/share/licenses/buttereye")
    return LegalNotices(
        name="ButterEye",
        version=__version__,
        copyright="Copyright (C) 2026 The ButterEye contributors",
        licence_name="GNU Affero General Public License v3.0 or later",
        no_warranty=Msg(
            "This program comes with ABSOLUTELY NO WARRANTY. It is free software, and "
            "you are welcome to redistribute it under the terms of the GNU AGPL."
        ),
        agpl_text=share / "COPYING",
        output_permission_text=share / "AdditionRef-ButterEye-generated-output.txt",
        source_link="https://copr.fedorainfracloud.org/coprs/buttereye/buttereye/",
        missing=(),
    )


# ---------- live ----------
def counters(drops: int = 0) -> Counters:
    return Counters(frame_drop=drops, decoder_drop=0, vo_delayed=0, mistimed=0, display_sync=True)


def facts(
    name: str = "film.mkv",
    fps: Fraction = Fraction(24000, 1001),
    height: int = 1080,
    hdr: HdrClass = HdrClass.SDR,
    interlaced: bool = False,
) -> SourceFacts:
    return SourceFacts(fps, height * 16 // 9, height, hdr, 120.0, interlaced, f"/videos/{name}")


def session(
    n: int,
    *,
    title: str = "film.mkv",
    filter: FilterState = FilterState.ACTIVE,
    bypass: BypassReason | None = None,
    health: Health = Health.OK,
    health_code: ErrorCode | None = None,
    origin: Origin = Origin.LAUNCHED,
    source: SourceFacts | None = None,
    ended: bool = False,
    step_down_to: str | None = None,
    drop_rate: float | None = 0.0,
    notice: Msg | None = None,
) -> SessionSnapshot:
    src = source if source is not None else facts(title)
    active = filter is FilterState.ACTIVE
    return SessionSnapshot(
        sid=SessionId(f"s{n}"),
        origin=origin,
        pid=4200 + n,
        title=title,
        source=src,
        display_fps=120.0,
        target_fps=Fraction(120000, 1001) if active else None,
        multiplier=Fraction(5) if active else None,
        backend=BackendId.RIFE_NCNN if active else None,
        model="rife-v4.26_ensembleFalse" if active else None,
        profile_id="quality",
        filter=filter,
        bypass=bypass,
        health=health,
        health_code=health_code,
        gen=7 if active else 0,
        counters=counters(),
        drop_rate_10s=drop_rate,
        step_down_to=step_down_to,
        notice=notice,
        restore_pending=("hwdec", "interpolation") if origin is Origin.ATTACHED else (),
        ended=ended,
    )


def candidates_mixed(runtime: Path) -> tuple[InstanceCandidate, ...]:
    return (
        InstanceCandidate(
            CandidateId("c1"), runtime / "mpv-4211.sock", 4211, "film.mkv", None, None, False, False
        ),
        InstanceCandidate(
            CandidateId("c2"),
            Path("/tmp/mpvsocket"),
            5120,
            None,
            RefusalReason.WRONG_UID,
            Msg("Refused: socket owned by another user"),
            False,
            False,
        ),
        InstanceCandidate(
            CandidateId("c3"), runtime / "mpv-3999.sock", 3999, "old.mkv", None, None, True, True
        ),
    )


# ---------- bench ----------
def bench_result_4090(req: BenchRequest | None = None) -> BenchResult:
    r = req or BenchRequest(1920, 1080, Fraction(24000, 1001))

    def m(
        label: str,
        backend: BackendId,
        model: str | None,
        vs: float,
        mpv: float,
        cov: float,
        faults: int | None,
        realtime: bool,
    ) -> BenchMeasurement:
        return BenchMeasurement(
            label,
            backend,
            model,
            vs,
            mpv,
            cov,
            cov < 0.05,
            1.8,
            0.6,
            3 * 2**30 if backend is BackendId.RIFE_NCNN else None,
            realtime,
            faults,
        )

    return BenchResult(
        request=r,
        measurements=(
            m(
                "rife-v4.26",
                BackendId.RIFE_NCNN,
                "rife-v4.26_ensembleFalse",
                75.2,
                74.1,
                0.02,
                0,
                True,
            ),
            m(
                "rife-v4.22-lite",
                BackendId.RIFE_NCNN,
                "rife-v4.22_lite_ensembleFalse",
                89.0,
                86.4,
                0.03,
                0,
                True,
            ),
            m(
                "rife-v4.18",
                BackendId.RIFE_NCNN,
                "rife-v4.18_ensembleFalse",
                77.0,
                70.3,
                0.09,
                1,
                True,
            ),
            m("mvtools", BackendId.MVTOOLS, None, 108.0, 96.5, 0.01, None, True),
        ),
        recommended="rife-v4.26",
        when=datetime(2026, 10, 7, 12, 0, tzinfo=UTC),
        gpu_uuid=UUID_4090,
    )


# ---------- render ----------
def render_probe(
    src: Path,
    *,
    hdr: HdrClass = HdrClass.SDR,
    vfr: bool = False,
    mkvmerge: bool = False,
    free_bytes: int = 500 * 2**30,
    refusal: Finding | None = None,
) -> RenderProbe:
    warnings: list[Finding] = []
    if vfr:
        warnings.append(
            finding(
                "render.vfr",
                Section.RENDER,
                Severity.INFO,
                "Will be converted to constant frame rate",
            )
        )
    if not mkvmerge:
        warnings.append(finding_mkvmerge_missing())
    encoders = (
        ("libx265", "libsvtav1")
        if hdr is HdrClass.HDR10
        else ("hevc_nvenc", "av1_nvenc", "libx264", "libx265", "libsvtav1")
    )
    return RenderProbe(
        source=src,
        hdr_class=hdr,
        vfr=vfr,
        duration_s=1420.5,
        streams=(
            StreamInfo(0, "video", "h264", None),
            StreamInfo(1, "audio", "aac", "English"),
            StreamInfo(2, "audio", "opus", "Commentary"),
            StreamInfo(3, "subtitle", "ass", "English"),
            StreamInfo(4, "subtitle", "subrip", "Spanish"),
            StreamInfo(5, "subtitle", "subrip", "German"),
            StreamInfo(6, "attachment", "ttf", "font.ttf"),
        ),
        chapters=12,
        encoders=encoders,
        remux_tool="mkvmerge" if mkvmerge else "ffmpeg",
        free_bytes=free_bytes,
        warnings=tuple(warnings),
        refusal=refusal,
        width=3840,
        height=2160,
        fps=Fraction(24000, 1001),
        sizes=(
            RenderSize(3840, 2160, 10.4),
            RenderSize(2560, 1440, 24.0),
            RenderSize(1920, 1080, 47.5),
            RenderSize(1280, 720, 86.0),
        ),
    )


def render_probes_refusals() -> Mapping[str, RenderProbe]:
    v = Path("/videos")
    refuse = finding  # alias for readability
    return MappingProxyType(
        {
            "hlg.mkv": render_probe(
                v / "hlg.mkv",
                hdr=HdrClass.HLG,
                refusal=refuse(
                    "render.hdr_class",
                    Section.RENDER,
                    Severity.BLOCKING,
                    "HLG video can't be rendered in this version",
                    code=ErrorCode.HDR_CLASS_REFUSED,
                ),
            ),
            "dv.mkv": render_probe(
                v / "dv.mkv",
                hdr=HdrClass.DV,
                refusal=refuse(
                    "render.hdr_class",
                    Section.RENDER,
                    Severity.BLOCKING,
                    "Dolby Vision video can't be rendered in this version",
                    code=ErrorCode.HDR_CLASS_REFUSED,
                ),
            ),
            "undecodable.mkv": render_probe(
                v / "undecodable.mkv",
                refusal=refuse(
                    "render.codec",
                    Section.RENDER,
                    Severity.BLOCKING,
                    "ffms2 can't decode this video",
                    code=ErrorCode.CODEC_NOT_DECODABLE,
                ),
            ),
            "nospace.mkv": render_probe(
                v / "nospace.mkv",
                free_bytes=2**20,
                refusal=refuse(
                    "render.space",
                    Section.RENDER,
                    Severity.BLOCKING,
                    "Not enough free space",
                    code=ErrorCode.NO_SPACE,
                ),
            ),
            "vfr.mkv": render_probe(v / "vfr.mkv", vfr=True),
            "hdr10.mkv": render_probe(v / "hdr10.mkv", hdr=HdrClass.HDR10, mkvmerge=True),
            "film.mkv": render_probe(v / "film.mkv", mkvmerge=True),
        }
    )


def render_jobs_mixed() -> tuple[RenderJobState, ...]:
    def j(
        n: int,
        state: OpState,
        phase: RenderPhase | None,
        done: int | None,
        error: ButterEyeError | None = None,
    ) -> RenderJobState:
        spec = RenderJobSpec(
            Path(f"/videos/ep{n}.mkv"),
            Path(f"/videos/ep{n}.buttereye.mkv"),
            "quality",
            "hevc_nvenc",
        )
        return RenderJobState(
            JobId(f"j{n}"),
            spec,
            state,
            phase,
            done,
            68_000,
            61.3 if done else None,
            900.0 if state is OpState.RUNNING else None,
            2_400_000_000,
            error,
        )

    return (
        j(1, OpState.SUCCEEDED, None, 68_000),
        j(
            2,
            OpState.FAILED,
            RenderPhase.RENDER,
            1200,
            ButterEyeError(ErrorCode.ENCODER_FAILED, Msg("The encoder stopped with an error.")),
        ),
        j(3, OpState.QUEUED, None, None),
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

_PATHS = fake_paths(Path("/nonexistent/buttereye-fake"))


def _devbox_now() -> Scenario:
    findings = findings_ok() + (finding_xid(), finding_ffms2_missing(), finding_mkvmerge_missing())
    rep = report(findings)
    return Scenario(
        name="devbox_now",
        description="Real-shaped dev box today: ffms2/mkvmerge/trtexec missing; live, bench, "
        "render and bug reports not in this build.",
        capabilities=caps_devbox_now(),
        config_load=config_load(),
        report=rep,
        doctor_result=rep,
        hardware=hardware_4090(),
        plugins=plugins_devbox(),
        backends=backends_devbox(),
        selection=selection_rife(),
        storage=storage_devbox(_PATHS),
        models=models_devbox(),
        third_party=third_party_devbox(),
        legal_notices=legal_notices_fake(),
        trt_after_optin=trt_missing_state(),
    )


def _fresh_box() -> Scenario:
    base = _devbox_now()
    return dataclasses.replace(
        base,
        name="fresh_box",
        description="No config.toml yet: first run opens setup.",
        config_load=config_load(exists=False),
        report=None,
    )


def _all_ready() -> Scenario:
    rep = report(findings_ok())
    dl = (
        DownloadItem(
            "rife_v4.26.onnx",
            "https://github.com/AmusementClub/vs-mlrt/releases/download/"
            "external-models/rife_v4.26.7z",
            41_000_000,
            "MIT (weights); conversion: see vs-mlrt GPL-3.0",
            "0" * 64,
            True,
        ),
    )
    return dataclasses.replace(
        _devbox_now(),
        name="all_ready",
        description="Every feature implemented and its dependencies present.",
        capabilities=caps_all_ready(),
        report=rep,
        doctor_result=rep,
        bench_history=(bench_result_4090(),),
        bench_result=bench_result_4090(),
        render_probes=MappingProxyType(
            {"film.mkv": render_probe(Path("/videos/film.mkv"), mkvmerge=True)}
        ),
        model_downloads=dl,
        trt_downloads=dl,
        candidates=candidates_mixed(_PATHS.runtime_dir),
    )


def _cpu_only() -> Scenario:
    findings = (
        finding("mpv.version", Section.MPV, Severity.OK, "mpv 0.41.0"),
        finding(
            "vulkan.cpu_only",
            Section.VULKAN,
            Severity.DEGRADED,
            "CPU-only: MVTools interpolation, RIFE unavailable",
            code=ErrorCode.VULKAN_CPU_ONLY,
            cause="Only llvmpipe (software) is available.",
            fix="Install the Vulkan driver for your GPU.",
            commands=("sudo dnf install mesa-vulkan-drivers",),
        ),
    )
    rep = report(findings, hardware_cpu_only())
    return dataclasses.replace(
        _all_ready(),
        name="cpu_only",
        description="No usable GPU: MVTools only.",
        report=rep,
        doctor_result=rep,
        hardware=hardware_cpu_only(),
        backends=(
            BackendStatus(
                BackendId.RIFE_NCNN,
                False,
                False,
                Msg("CPU-only: MVTools interpolation, RIFE unavailable — llvmpipe only"),
                ErrorCode.VULKAN_CPU_ONLY,
            ),
            BackendStatus(BackendId.MVTOOLS, True, False, Msg("MVTools on the CPU")),
            BackendStatus(
                BackendId.RIFE_TRT, False, True, Msg("Experimental NVIDIA TensorRT: off")
            ),
        ),
        selection=Selection(
            BackendId.MVTOOLS,
            None,
            "cpu",
            Msg("CPU-only: MVTools interpolation, RIFE unavailable"),
            False,
            (BackendId.MVTOOLS,),
        ),
        smoke_fps=96.0,
    )


def _blocking_mpv() -> Scenario:
    blocking = finding(
        "mpv.vf_vapoursynth",
        Section.MPV,
        Severity.BLOCKING,
        "Your mpv was built without VapourSynth",
        code=ErrorCode.MPV_NO_VS_FILTER,
        cause="mpv --vf=help does not list the vapoursynth filter.",
        fix="Install the distro mpv package.",
        commands=("sudo dnf install mpv",),
    )
    rep = report((blocking,) + findings_ok()[2:])
    caps = dict(caps_all_ready())
    for f in (Feature.LIVE, Feature.ATTACH, Feature.BENCH, Feature.RENDER, Feature.RENDER_HDR10):
        caps[f] = unavailable_state(f, Reason.BLOCKED_BY_DOCTOR, None, params={"n": 1})
    return dataclasses.replace(
        _all_ready(),
        name="blocking_mpv",
        description="mpv lacks the VapourSynth filter.",
        capabilities=MappingProxyType(caps),
        report=rep,
        doctor_result=rep,
        missing_packages=("mpv",),
        dnf_lines=("sudo dnf install mpv",),
    )


def _live_active() -> Scenario:
    return dataclasses.replace(
        _all_ready(),
        name="live_active",
        description="One RIFE session, 23.976 -> 119.88 fps (5x).",
        sessions=(session(1),),
    )


def _live_bypassed_each() -> Scenario:
    titles = {
        BypassReason.ALREADY_AT_RATE: ("sixty.mkv", facts("sixty.mkv", Fraction(60))),
        BypassReason.INTERLACED: ("tv.ts", facts("tv.ts", interlaced=True)),
        BypassReason.HDR_SKIP: ("hdr.mkv", facts("hdr.mkv", hdr=HdrClass.HDR10, height=2160)),
        BypassReason.UNSUPPORTED_FORMAT: ("rgb.mkv", facts("rgb.mkv")),
        BypassReason.NO_VIDEO: ("song.flac", None),
        BypassReason.NO_REALTIME: ("big.mkv", facts("big.mkv", height=2160)),
    }
    sessions = [
        session(i + 1, title=t, filter=FilterState.BYPASSED, bypass=r, source=src)
        for i, (r, (t, src)) in enumerate(titles.items())
    ]
    n = len(sessions)
    sessions += [
        session(n + 1, title="paused.mkv", filter=FilterState.OFF),
        session(n + 2, title="starting.mkv", filter=FilterState.PENDING),
        session(n + 3, title="film.mkv", filter=FilterState.ACTIVE),
        session(
            n + 4,
            title="broken.mkv",
            filter=FilterState.ROLLED_BACK,
            health_code=ErrorCode.VF_ROLLED_BACK,
        ),
        session(n + 5, title="attached.mkv", origin=Origin.ATTACHED),
        session(n + 6, title="gone.mkv", ended=True),
        session(
            n + 7, title="dropping.mkv", health=Health.DROPPING, step_down_to="fast", drop_rate=0.03
        ),
    ]
    return dataclasses.replace(
        _all_ready(),
        name="live_bypassed_each",
        description="One session per BypassReason plus every FilterState.",
        sessions=tuple(sessions),
    )


def _health(
    name: str,
    health: Health,
    code: ErrorCode,
    reason: str,
    auto: str | None,
    evidence: tuple[str, ...] = (),
) -> Scenario:
    filt = (
        FilterState.ROLLED_BACK
        if health in (Health.STALLED, Health.GPU_FAULT, Health.DEVICE_LOST)
        else FilterState.ACTIVE
    )
    snap = session(1, filter=filt, health=health, health_code=code)
    ev = HealthChanged(snap.sid, health, code, Msg(reason), evidence, Msg(auto) if auto else None)
    return dataclasses.replace(
        _all_ready(),
        name=name,
        description=reason,
        sessions=(snap,),
        script=((0.0, ev), (0.0, SessionChanged(snap))),
    )


def _stalled() -> Scenario:
    return _health(
        "stalled",
        Health.STALLED,
        ErrorCode.FILTER_STALLED,
        "Video stalled while audio kept playing; interpolation was turned off.",
        "interpolation turned off",
    )


def _gpu_fault() -> Scenario:
    return _health(
        "gpu_fault",
        Health.GPU_FAULT,
        ErrorCode.RIFE_GPU_FAULT,
        "The GPU reported a fault in RIFE-ncnn (Xid 13).",
        "interpolation turned off",
        XID_LINES,
    )


def _connection_lost() -> Scenario:
    return _health(
        "connection_lost",
        Health.CONNECTION_LOST,
        ErrorCode.IPC_LOST,
        "Lost the connection to mpv.",
        None,
    )


def _config_invalid() -> Scenario:
    issue = ConfigIssue(ErrorCode.CONFIG_INVALID, Msg("Expected '=' after a key"), 12, 5, None)
    return dataclasses.replace(
        _devbox_now(),
        name="config_invalid",
        description="config.toml has a TOML syntax error.",
        config_load=config_load(used_defaults=True, issues=(issue,)),
    )


def _config_newer() -> Scenario:
    issue = ConfigIssue(
        ErrorCode.CONFIG_NEWER_SCHEMA, Msg("Created by a newer ButterEye — read-only.")
    )
    return dataclasses.replace(
        _devbox_now(),
        name="config_newer",
        description="schema_version is newer: read-only.",
        config_load=config_load(
            dataclasses.replace(default_config(), schema_version=2), read_only=True, issues=(issue,)
        ),
    )


def _config_conflict() -> Scenario:
    unknown = MappingProxyType({"future_a": 1, "future_b": "x", "general.future_c": True})
    issues = tuple(
        ConfigIssue(ErrorCode.CONFIG_UNKNOWN_KEY, Msg("Unknown setting {key}", {"key": k}), field=k)
        for k in unknown
    )
    cfg = dataclasses.replace(default_config(), unknown=unknown)
    return dataclasses.replace(
        _devbox_now(),
        name="config_conflict",
        description="config.toml changed on disk since load (3 unknown keys kept).",
        config_load=config_load(cfg, issues=issues),
        disk_revision="rev-disk",
    )


def _render_refusals() -> Scenario:
    stale = (StaleJob(JobId("j0"), 1234, True, (Path("/videos/old.buttereye.mkv.part"),)),)
    return dataclasses.replace(
        _all_ready(),
        name="render_refusals",
        description="Render probes with each refusal (HLG, DV, undecodable, no space) and a "
        "stale job from a previous session.",
        render_probes=render_probes_refusals(),
        render_jobs=render_jobs_mixed(),
        stale_jobs=stale,
    )


def _bench_results() -> Scenario:
    return dataclasses.replace(
        _all_ready(),
        name="bench_results",
        description="Bench history with a faulting and an unreadable-journal row.",
        bench_history=(bench_result_4090(),),
    )


def _slow_core() -> Scenario:
    return dataclasses.replace(
        _devbox_now(),
        name="slow_core",
        description="The first ping blocks the loop for 6 s.",
        block_first_ping_s=6.0,
    )


def _apply_fails() -> Scenario:
    err = FilterFailed(
        ErrorCode.VF_ROLLED_BACK,
        Msg("mpv rejected the new filter; rolled back."),
        Msg("Try another profile."),
        rolled_back=True,
    )
    return dataclasses.replace(
        _live_active(),
        name="apply_fails",
        description="apply_profile rolls back (F4).",
        errors=MappingProxyType({"apply_profile": err}),
    )


SCENARIOS: Mapping[str, Callable[[], Scenario]] = MappingProxyType(
    {
        "fresh_box": _fresh_box,
        "devbox_now": _devbox_now,
        "all_ready": _all_ready,
        "cpu_only": _cpu_only,
        "blocking_mpv": _blocking_mpv,
        "live_active": _live_active,
        "live_bypassed_each": _live_bypassed_each,
        "stalled": _stalled,
        "gpu_fault": _gpu_fault,
        "connection_lost": _connection_lost,
        "config_invalid": _config_invalid,
        "config_newer": _config_newer,
        "config_conflict": _config_conflict,
        "render_refusals": _render_refusals,
        "bench_results": _bench_results,
        "slow_core": _slow_core,
        "apply_fails": _apply_fails,
    }
)

#: The scenario names the design (§6) requires.
REQUIRED = (
    "fresh_box",
    "devbox_now",
    "all_ready",
    "cpu_only",
    "blocking_mpv",
    "live_active",
    "live_bypassed_each",
    "stalled",
    "gpu_fault",
    "connection_lost",
    "config_invalid",
    "config_newer",
    "config_conflict",
    "render_refusals",
    "bench_results",
    "slow_core",
)


def get(name: str) -> Scenario:
    """Build a fresh scenario by name (KeyError lists the valid names)."""
    try:
        return SCENARIOS[name]()
    except KeyError:
        raise KeyError(f"unknown scenario {name!r}; known: {', '.join(SCENARIOS)}") from None


def conflict_error(on_disk: str) -> ConfigConflict:
    return ConfigConflict(
        ErrorCode.CONFIG_CONFLICT,
        Msg("config.toml changed outside ButterEye."),
        Msg("Reload it, or overwrite it with your changes."),
        on_disk_revision=on_disk,
    )


__all__ = ["Scenario", "SCENARIOS", "REQUIRED", "get", "fake_paths", "conflict_error"]
