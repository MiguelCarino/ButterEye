# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Provider probing -> ``Capabilities`` (docs/design/GUI.md §2.5, §5.2, §11).

Static part: is the provider ``module.attr`` importable? (``importlib`` only;
a missing provider is detected without running any subsystem code.)
Dynamic part: the last ``DoctorReport`` (``MISSING_DEPENDENCY``,
``BLOCKED_BY_DOCTOR``) and the config (``OPT_IN_REQUIRED`` for TRT).

Precedence per feature: NOT_IMPLEMENTED / NOTHING_LISTED (static) >
OPT_IN_REQUIRED > MISSING_DEPENDENCY > BLOCKED_BY_DOCTOR > SPIKE_PENDING.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from buttereye.core.errors import ErrorCode
from buttereye.core.types import (
    Capabilities,
    CapState,
    Config,
    DoctorReport,
    Feature,
    Finding,
    Msg,
    Reason,
    Section,
    Severity,
)

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ProviderRef:
    module: str
    attr: str

    @property
    def dotted(self) -> str:
        return f"{self.module}.{self.attr}"


def _refs(module: str, *attrs: str) -> tuple[ProviderRef, ...]:
    return tuple(ProviderRef(module, a) for a in attrs)


_P = "buttereye.core."
CONFIG_REFS = (
    _refs(_P + "profiles.config", "load", "save")
    + _refs(_P + "profiles.rules", "validate", "explain")
    + _refs(_P + "profiles.defaults", "builtin_profiles", "default_config")
)
_CFG_LOAD = _refs(_P + "profiles.config", "load")
SELECT_REFS = _refs(_P + "backends.select", "backends", "select")
LIVE_REFS = _refs(
    _P + "mpvctl.session",
    "play",
    "sessions",
    "set_interpolation",
    "detach",
    "apply_profile",
    "step_down",
    "close",
)
DISCOVER_REFS = _refs(_P + "mpvctl.session", "discover")
ATTACH_REFS = _refs(_P + "mpvctl.attach", "attach")  # reserved, unassigned (§11)
ORPHANS_REFS = _refs(_P + "mpvctl.orphans", "remove_orphan_filter")  # reserved, unassigned
INCLUDE_REFS = _refs(_P + "mpvctl.include", "add_include")
BENCH_REFS = _refs(_P + "bench.runner", "bench", "history", "apply")
RENDER_REFS = _refs(
    _P + "render.jobs",
    "probe",
    "enqueue",
    "jobs",
    "move",
    "cancel",
    "forget",
    "stale_jobs",
    "stale_stop",
    "shutdown",
)
REPORT_REFS = _refs(_P + "doctor.report", "bundle")
DOWNLOAD_REFS = _refs(_P + "plugins.download", "listed", "install")
MODEL_FILE_REFS = _refs(_P + "plugins.install", "install_file", "remove")
TRT_REFS = _refs(_P + "backends.trt", "build_engine")  # reserved for M2

#: Static requirements: a feature is implemented iff every ref resolves.
FEATURE_REQUIRES: dict[Feature, tuple[ProviderRef, ...]] = {
    Feature.CONFIG: CONFIG_REFS,
    Feature.DOCTOR: _refs(_P + "doctor.run", "doctor", "plugins") + _CFG_LOAD,
    Feature.HARDWARE: _refs(_P + "hw.detect", "hardware"),
    Feature.SETUP: _refs(_P + "setup", "plan", "apply")
    + _refs(_P + "doctor.run", "doctor")
    + SELECT_REFS
    + _CFG_LOAD,
    Feature.REPORT: REPORT_REFS,
    Feature.DISCOVER: DISCOVER_REFS,
    Feature.LIVE: LIVE_REFS + _CFG_LOAD,
    Feature.ATTACH: ATTACH_REFS,
    Feature.ORPHANS: ORPHANS_REFS,
    Feature.BENCH: BENCH_REFS + _CFG_LOAD,
    Feature.RENDER: RENDER_REFS,
    Feature.RENDER_HDR10: RENDER_REFS,
    Feature.STORAGE: _refs(_P + "storage", "entries"),
    Feature.CLEAN: _refs(_P + "storage", "clean"),
    Feature.MODELS: _refs(_P + "storage", "models"),
    Feature.MODEL_DOWNLOADS: DOWNLOAD_REFS,
    Feature.LICENCES: _refs(_P + "licences", "third_party", "legal_notices"),
    Feature.TRT: TRT_REFS,
    Feature.HDR_PASSTHROUGH: (),
}

#: Features whose real use needs a non-blocking doctor report.
DOCTOR_GATED: frozenset[Feature] = frozenset(
    {Feature.LIVE, Feature.ATTACH, Feature.BENCH, Feature.RENDER, Feature.RENDER_HDR10}
)

#: Finding id that U3 emits (Section.RENDER, DEGRADED) when ffmpeg lists neither
#: libx265 nor libsvtav1 (§7.5). Documented in GUI.md §11.
HDR10_ENCODER_FINDING = "render.hdr10_encoder"

# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(ref: ProviderRef) -> Callable[..., Any] | None:
    """Return the provider callable, or ``None`` if the module/attr is absent.

    A module that is not installed is detected with ``find_spec`` (no subsystem
    code runs). A module that exists but fails to import is logged and treated
    as absent.
    """
    mod = sys.modules.get(ref.module)
    if mod is None:
        try:
            spec = importlib.util.find_spec(ref.module)
        except ImportError, ValueError:
            spec = None
        if spec is None:
            return None
        try:
            mod = importlib.import_module(ref.module)
        except ImportError as exc:
            _log.warning("provider module %s failed to import: %s", ref.module, exc)
            return None
        except Exception:
            _log.exception("provider module %s raised on import", ref.module)
            return None
    fn = getattr(mod, ref.attr, None)
    if fn is None or not callable(fn):
        return None
    return fn  # type: ignore[no-any-return]


def implemented(refs: Iterable[ProviderRef]) -> bool:
    return all(resolve(r) is not None for r in refs)


def static_state(feature: Feature) -> CapState:
    """Availability from the provider table alone."""
    if implemented(FEATURE_REQUIRES[feature]):
        return CapState(True)
    return absent_state(feature)


def absent_state(feature: Feature) -> CapState:
    """The CapState for a feature whose provider is missing in this build."""
    if feature is Feature.MODEL_DOWNLOADS:
        # This release ships no download manifest: nothing is listed (§5.2).
        return unavailable_state(feature, Reason.NOTHING_LISTED, None)
    return unavailable_state(feature, Reason.NOT_IMPLEMENTED, ErrorCode.NOT_IMPLEMENTED)


def unavailable_state(
    feature: Feature,
    reason: Reason,
    code: ErrorCode | None,
    *,
    commands: tuple[str, ...] = (),
    params: Mapping[str, str | int | float] | None = None,
) -> CapState:
    """An unavailable CapState carrying the §5.2 message (``params`` fill it)."""
    st = CapState(False, reason, code, None, commands)
    msg = unavailable_text(feature, st)
    if params:
        msg = Msg(msg.key, MappingProxyType(dict(params)))
    return CapState(False, reason, code, msg, commands)


# ---------------------------------------------------------------------------
# Dynamic part
# ---------------------------------------------------------------------------


def _problem(f: Finding) -> bool:
    return f.severity in (Severity.BLOCKING, Severity.DEGRADED)


def _commands(findings: Iterable[Finding]) -> tuple[str, ...]:
    out: list[str] = []
    for f in findings:
        for c in f.commands:
            if c not in out:
                out.append(c)
    return tuple(out)


def compute(
    cfg: Config | None,
    report: DoctorReport | None,
    *,
    static: Mapping[Feature, CapState] | None = None,
) -> Capabilities:
    """Full capability map for this build, config and last doctor report."""
    states: dict[Feature, CapState] = {}
    for feature in Feature:
        st = static[feature] if static is not None else static_state(feature)
        states[feature] = st

    # TRT: opt-in first (§5.2), then the TRT findings, then the implementation.
    trt_static = states[Feature.TRT]
    if cfg is None or not cfg.general.trt_experimental:
        states[Feature.TRT] = unavailable_state(Feature.TRT, Reason.OPT_IN_REQUIRED, None)
    else:
        trt_findings = (
            [f for f in report.findings if f.section is Section.TRT and _problem(f)]
            if report is not None
            else []
        )
        if trt_findings:
            states[Feature.TRT] = unavailable_state(
                Feature.TRT,
                Reason.MISSING_DEPENDENCY,
                ErrorCode.TRT_UNSUPPORTED,
                commands=_commands(trt_findings),
            )
        else:
            states[Feature.TRT] = trt_static

    states[Feature.HDR_PASSTHROUGH] = unavailable_state(
        Feature.HDR_PASSTHROUGH, Reason.SPIKE_PENDING, None
    )

    if report is not None:
        # RENDER: ffms2 missing -> MISSING_DEPENDENCY even once implemented.
        if states[Feature.RENDER].available:
            ffms2 = [
                f for f in report.findings if f.code is ErrorCode.FFMS2_MISSING and _problem(f)
            ]
            if ffms2:
                states[Feature.RENDER] = unavailable_state(
                    Feature.RENDER,
                    Reason.MISSING_DEPENDENCY,
                    ErrorCode.FFMS2_MISSING,
                    commands=_commands(ffms2) or ("sudo dnf install ffms2",),
                )
        if not states[Feature.RENDER].available:
            states[Feature.RENDER_HDR10] = states[Feature.RENDER]
        else:
            hdr = [f for f in report.findings if f.id == HDR10_ENCODER_FINDING and _problem(f)]
            if hdr:
                states[Feature.RENDER_HDR10] = unavailable_state(
                    Feature.RENDER_HDR10,
                    Reason.MISSING_DEPENDENCY,
                    hdr[0].code,
                    commands=_commands(hdr),
                )

        if report.blocking:
            n = sum(1 for f in report.findings if f.severity is Severity.BLOCKING)
            for feature in DOCTOR_GATED:
                if states[feature].available:
                    states[feature] = unavailable_state(
                        feature, Reason.BLOCKED_BY_DOCTOR, None, params={"n": n}
                    )
    return Capabilities(MappingProxyType(states))


# ---------------------------------------------------------------------------
# §5.2 wording. The msgid's first line is the headline; the rest is the body.
# ---------------------------------------------------------------------------

_LIVE_NI = (
    "Live playback control isn't in this build yet.\n"
    "This build can check your system and edit profiles. Playing and connecting to mpv "
    "arrive in a later build. Nothing is connected to mpv."
)

# ATTACH / DISCOVER / ORPHANS can be unavailable while LIVE works (ButterEye
# starts mpv itself), so their texts must not claim that playing or connecting
# to mpv is missing, nor that nothing is connected.
_TEXTS: dict[tuple[Feature, Reason], str] = {
    (Feature.LIVE, Reason.NOT_IMPLEMENTED): _LIVE_NI,
    (Feature.ATTACH, Reason.NOT_IMPLEMENTED): (
        "Attaching to an mpv you started yourself isn't in this build yet.\n"
        "Use Open to let ButterEye start mpv for you."
    ),
    (Feature.DISCOVER, Reason.NOT_IMPLEMENTED): (
        "Finding mpv players you started yourself isn't in this build yet.\n"
        "Players that ButterEye started are listed on the Play page."
    ),
    (Feature.ORPHANS, Reason.NOT_IMPLEMENTED): (
        "Removing a leftover ButterEye filter from another player isn't in this build yet.\n"
        "Close that mpv and start it again to clear the filter."
    ),
    (Feature.BENCH, Reason.NOT_IMPLEMENTED): (
        "The benchmark isn't in this build yet.\n"
        "No measurements are shown until ButterEye has measured your GPU itself."
    ),
    (Feature.RENDER, Reason.NOT_IMPLEMENTED): "Offline render isn't in this build yet.",
    (Feature.RENDER_HDR10, Reason.NOT_IMPLEMENTED): "Offline render isn't in this build yet.",
    (Feature.RENDER, Reason.MISSING_DEPENDENCY): (
        "Render needs ffms2.\nffms2 reads your video for VapourSynth. Install it, then press F5."
    ),
    (Feature.RENDER_HDR10, Reason.MISSING_DEPENDENCY): (
        "HDR10 render needs the x265 or SVT-AV1 encoder.\n"
        "Your ffmpeg doesn't list libx265 or libsvtav1."
    ),
    (Feature.REPORT, Reason.NOT_IMPLEMENTED): (
        "Bug reports can't be created in this build yet.\n"
        "Copy the check results with Copy all as text instead."
    ),
    (Feature.MODEL_DOWNLOADS, Reason.NOTHING_LISTED): (
        "No downloadable models are listed in this release.\n"
        "Packaged models are installed with dnf. You can install a model archive from a file."
    ),
    (Feature.TRT, Reason.OPT_IN_REQUIRED): (
        "Experimental NVIDIA TensorRT: off.\n"
        "It uses pre-release software you install yourself and may break on updates."
    ),
    (Feature.TRT, Reason.MISSING_DEPENDENCY): (
        "TensorRT isn't set up.\n"
        "trtexec and vstrt were not found. See the TensorRT items on the System page."
    ),
    (Feature.TRT, Reason.NOT_IMPLEMENTED): (
        "Experimental NVIDIA TensorRT isn't in this build yet."
    ),
    (Feature.HDR_PASSTHROUGH, Reason.SPIKE_PENDING): "Needs compatibility test M0(d).",
    (Feature.CONFIG, Reason.NOT_IMPLEMENTED): "Profiles can't be edited in this build yet.",
    (Feature.DOCTOR, Reason.NOT_IMPLEMENTED): "System checks aren't in this build yet.",
    (Feature.HARDWARE, Reason.NOT_IMPLEMENTED): "Hardware detection isn't in this build yet.",
    (Feature.SETUP, Reason.NOT_IMPLEMENTED): "Setup isn't in this build yet.",
    (Feature.STORAGE, Reason.NOT_IMPLEMENTED): "The storage overview isn't in this build yet.",
    (Feature.CLEAN, Reason.NOT_IMPLEMENTED): "Cleaning up isn't in this build yet.",
    (Feature.MODELS, Reason.NOT_IMPLEMENTED): "The model list isn't in this build yet.",
    (Feature.MODEL_DOWNLOADS, Reason.NOT_IMPLEMENTED): (
        "Model downloads aren't in this build yet."
    ),
    (Feature.LICENCES, Reason.NOT_IMPLEMENTED): (
        "The third-party licence list isn't in this build yet."
    ),
}

_BLOCKED = "Setup isn't finished.\n{n} problem(s) need fixing on the System page."
_GENERIC: dict[Reason, str] = {
    Reason.NOT_IMPLEMENTED: "This part of ButterEye isn't in this build yet.",
    Reason.MISSING_DEPENDENCY: (
        "A program this needs is missing.\nInstall it with the command below, then press F5."
    ),
    Reason.BLOCKED_BY_DOCTOR: _BLOCKED,
    Reason.OPT_IN_REQUIRED: "This is an experimental option and is off.",
    Reason.SPIKE_PENDING: "This option is waiting for a compatibility test.",
    Reason.NOTHING_LISTED: "Nothing is listed for this in this release.",
}


def unavailable_text(feature: Feature, state: CapState) -> Msg:
    """§5.2 wording for an unavailable feature. First line = headline, rest = body.

    ``CapState.message`` produced by this module is the same Msg (with params,
    e.g. ``{n}`` for BLOCKED_BY_DOCTOR), so show one of them, not both.
    """
    if state.message is not None:
        return state.message
    if state.available or state.reason is None:
        return Msg("Available.")
    if state.reason is Reason.BLOCKED_BY_DOCTOR:
        return Msg(_BLOCKED)
    return Msg(_TEXTS.get((feature, state.reason), _GENERIC[state.reason]))


__all__ = [
    "ProviderRef",
    "FEATURE_REQUIRES",
    "DOCTOR_GATED",
    "HDR10_ENCODER_FINDING",
    "CONFIG_REFS",
    "SELECT_REFS",
    "LIVE_REFS",
    "DISCOVER_REFS",
    "ATTACH_REFS",
    "ORPHANS_REFS",
    "INCLUDE_REFS",
    "BENCH_REFS",
    "RENDER_REFS",
    "REPORT_REFS",
    "DOWNLOAD_REFS",
    "MODEL_FILE_REFS",
    "TRT_REFS",
    "resolve",
    "implemented",
    "static_state",
    "absent_state",
    "unavailable_state",
    "compute",
    "unavailable_text",
]
