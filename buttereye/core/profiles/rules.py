# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Config validation and the first-match rule trace (SCOPE §4.9, §5.3, §5.6; F15).

Both functions are pure (no I/O, thread-safe): the GUI calls them on its own
thread through ``api.validate_config`` / ``api.explain_rules``.

Rule semantics (first match wins; an empty ``match`` matches everything):

- ``fps_min`` / ``fps_max``: source fps >= / <= the bound (exact ``Fraction``s).
- ``width_max`` / ``height_max``: source width / height <= the bound.
- ``hdr_class``: equal to the source's HDR class.
- ``display_hz_min``: display refresh >= the bound.
- ``interlaced``: equal to the source's flag.
- ``path_glob``: case-sensitive. A glob without ``/`` matches the file name only
  (``*.mkv``); with ``/`` it matches the whole path (``~/Videos/Anime/**``, ``*``
  stays within one directory, ``**`` spans any number). A leading ``~`` is the
  home directory.

A rule whose profile id does not exist is skipped (shown in the trace). With no
match the profile is ``defaults.DEFAULT_PROFILE_ID`` (or the first profile when
the config has no profile of that id).
"""

from __future__ import annotations

import fnmatch
import math
import os.path
import re
from collections.abc import Mapping
from fractions import Fraction
from pathlib import PurePosixPath
from types import MappingProxyType

from buttereye.core.errors import ErrorCode
from buttereye.core.profiles import defaults
from buttereye.core.profiles.schema import format_fraction
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigIssue,
    HdrClass,
    Msg,
    Profile,
    Rule,
    RuleMatch,
    RuleStep,
    RuleTrace,
    SourceFacts,
    Target,
    TargetKind,
)


def _msg(key: str, params: Mapping[str, str | int | float] | None = None) -> Msg:
    """A ``Msg`` with immutable params (it crosses the GUI bridge)."""
    return Msg(key, MappingProxyType(dict(params or {})))


#: Valid profile ids: what a TOML bare key allows, starting with a letter or digit.
PROFILE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
#: A model is a directory *name* (never a path): no separators, no leading dot.
MODEL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
#: BCP 47-ish tag or gettext locale: "de", "pt-BR", "pt_BR", "sr-Latn".
LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*")
#: vs-mlrt RIFE scales (powers of two around 1).
VALID_SCALES = (0.25, 0.5, 1.0, 2.0, 4.0)
MAX_FPS = Fraction(1000)
MAX_FRAMES = 64
_VERSION_IN_NAME = re.compile(r"v(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


class _Issues:
    def __init__(self) -> None:
        self.items: list[ConfigIssue] = []

    def add(self, path: str, key: str, params: dict[str, str | int | float] | None = None) -> None:
        self.items.append(ConfigIssue(ErrorCode.CONFIG_VALUE, _msg(key, params or {}), field=path))


def _is_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_count(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def model_version(model: str) -> tuple[int, int] | None:
    """RIFE version in a model name (``rife-v4.26_ensembleFalse`` -> (4, 26))."""
    m = _VERSION_IN_NAME.search(model)
    return (int(m.group(1)), int(m.group(2))) if m else None


def validate(cfg: Config) -> tuple[ConfigIssue, ...]:
    """Field-level problems (``BE-2004``) in the order of the config. Pure.

    ``field`` is a dotted path into ``cfg`` (``profiles[3].sc_threshold``,
    ``rules[0].match.fps_max``, ``general.language``); line/column are ``None``
    (``config.load`` adds them for values that are written in the file).
    """
    out = _Issues()
    trt_on = cfg.general.trt_experimental is True
    _validate_general(cfg, out, trt_on)
    ids = _validate_profiles(cfg.profiles, out, trt_on)
    _validate_rules(cfg.rules, ids, out)
    _validate_render(cfg, out)
    return tuple(out.items)


def _validate_general(cfg: Config, out: _Issues, trt_on: bool) -> None:
    g = cfg.general
    if g.backend_override is not None:
        if not isinstance(g.backend_override, BackendId):
            out.add(
                "general.backend_override",
                "Unknown engine {value}.",
                {"value": str(g.backend_override)},
            )
        elif g.backend_override is BackendId.RIFE_TRT and not trt_on:
            out.add(
                "general.backend_override",
                "TensorRT is experimental and turned off; turn it on before choosing it.",
            )
    if g.gpu is not None and (not isinstance(g.gpu, str) or not g.gpu.strip()):
        out.add("general.gpu", "The GPU must be a Vulkan device UUID or index.")
    if g.language is not None and (
        not isinstance(g.language, str) or LANGUAGE_RE.fullmatch(g.language) is None
    ):
        out.add(
            "general.language",
            "{value} is not a language tag such as de or pt-BR.",
            {"value": str(g.language)},
        )
    if not isinstance(g.trt_experimental, bool):
        out.add("general.trt_experimental", "trt_experimental must be true or false.")


def _validate_profiles(profiles: tuple[Profile, ...], out: _Issues, trt_on: bool) -> set[str]:
    ids: set[str] = set()
    for i, p in enumerate(profiles):
        f = f"profiles[{i}]"
        if not isinstance(p.id, str) or PROFILE_ID_RE.fullmatch(p.id) is None:
            out.add(
                f"{f}.id", "Profile ids use letters, digits, - and _ (up to 64), such as my-anime."
            )
        elif p.id in ids:
            out.add(f"{f}.id", "Another profile already uses the id {id}.", {"id": p.id})
        else:
            ids.add(p.id)
        if not isinstance(p.name, str) or not p.name.strip():
            out.add(f"{f}.name", "The profile needs a name.")
        _validate_profile_engine(p, f, out, trt_on)
        _validate_target(p.target, f"{f}.target", out)
        if not _is_number(p.sc_threshold) or not 0.0 <= float(p.sc_threshold) <= 1.0:
            out.add(f"{f}.sc_threshold", "Scene-cut sensitivity must be between 0 and 1.")
        for name, value in (
            ("buffered_frames", p.buffered_frames),
            ("concurrent_frames", p.concurrent_frames),
        ):
            if value is not None and (not _is_count(value) or not 1 <= value <= MAX_FRAMES):
                out.add(
                    f"{f}.{name}",
                    "Must be a whole number from 1 to {max}, or automatic.",
                    {"max": MAX_FRAMES},
                )
        if p.hdr not in ("skip", "passthrough"):
            out.add(f"{f}.hdr", "HDR must be skip or passthrough.")
    return ids


def _validate_profile_engine(p: Profile, f: str, out: _Issues, trt_on: bool) -> None:
    backend = p.backend
    if backend != "auto" and not isinstance(backend, BackendId):
        out.add(f"{f}.backend", "Unknown engine {value}.", {"value": str(backend)})
        return
    if backend is BackendId.RIFE_TRT and not trt_on:
        out.add(
            f"{f}.backend",
            "TensorRT is experimental and turned off; turn it on before choosing it.",
        )
    if p.model is not None:
        if backend is BackendId.MVTOOLS:
            out.add(f"{f}.model", "MVTools uses no model; leave the model empty.")
        elif not isinstance(p.model, str) or MODEL_NAME_RE.fullmatch(p.model) is None:
            out.add(
                f"{f}.model",
                "A model is a model name such as rife-v4.26_ensembleFalse, not a path.",
            )
    if p.scale is not None:
        if backend in (BackendId.RIFE_NCNN, BackendId.MVTOOLS):
            out.add(
                f"{f}.scale",
                "Scale applies to TensorRT only. RIFE (Vulkan) uses UHD mode at 4K automatically.",
            )
        elif not _is_number(p.scale) or float(p.scale) not in VALID_SCALES:
            out.add(f"{f}.scale", "Scale must be one of 0.25, 0.5, 1, 2 or 4.")
        elif float(p.scale) != 1.0 and isinstance(p.model, str):
            version = model_version(p.model)
            if version is not None and version >= (4, 7):
                out.add(
                    f"{f}.scale",
                    "RIFE {version} and newer only run at scale 1.",
                    {"version": f"{version[0]}.{version[1]}"},
                )


def _validate_target(t: Target, f: str, out: _Issues) -> None:
    if not isinstance(t, Target) or not isinstance(t.kind, TargetKind):
        out.add(f, "The target must be display, 2x or a fixed rate.")
        return
    if t.kind is TargetKind.FPS:
        if not isinstance(t.fps, Fraction):
            out.add(f, "A fixed target needs a rate such as 120 or 120000/1001.")
        elif not 0 < t.fps <= MAX_FPS:
            out.add(
                f,
                "The target rate must be above 0 and at most {max} fps.",
                {"max": format_fraction(MAX_FPS)},
            )
    elif t.fps is not None:
        out.add(f, "Only a fixed-rate target takes a rate.")


def _validate_rules(rules: tuple[Rule, ...], ids: set[str], out: _Issues) -> None:
    for i, r in enumerate(rules):
        f = f"rules[{i}]"
        if not isinstance(r.profile, str) or not r.profile:
            out.add(f"{f}.profile", "The rule needs a profile.")
        elif r.profile not in ids:
            out.add(f"{f}.profile", "No profile has the id {id}.", {"id": r.profile})
        _validate_match(r.match, f"{f}.match", out)


def _validate_match(m: RuleMatch, f: str, out: _Issues) -> None:
    for name, value in (("fps_min", m.fps_min), ("fps_max", m.fps_max)):
        if value is not None and (not isinstance(value, Fraction) or not 0 < value <= MAX_FPS):
            out.add(
                f"{f}.{name}",
                "Frame rates must be above 0 and at most {max}.",
                {"max": format_fraction(MAX_FPS)},
            )
    if (
        isinstance(m.fps_min, Fraction)
        and isinstance(m.fps_max, Fraction)
        and m.fps_min > m.fps_max
    ):
        out.add(f"{f}.fps_min", "The lowest fps is above the highest fps; nothing can match.")
    for name, size in (("width_max", m.width_max), ("height_max", m.height_max)):
        if size is not None and (not _is_count(size) or size <= 0):
            out.add(f"{f}.{name}", "Must be a whole number of pixels above 0.")
    if m.hdr_class is not None and not isinstance(m.hdr_class, HdrClass):
        out.add(f"{f}.hdr_class", "Unknown HDR class {value}.", {"value": str(m.hdr_class)})
    if m.display_hz_min is not None and (
        not _is_number(m.display_hz_min) or float(m.display_hz_min) <= 0
    ):
        out.add(f"{f}.display_hz_min", "The display refresh must be a number above 0.")
    if m.interlaced is not None and not isinstance(m.interlaced, bool):
        out.add(f"{f}.interlaced", "Interlaced must be true or false.")
    if m.path_glob is not None and (not isinstance(m.path_glob, str) or not m.path_glob.strip()):
        out.add(f"{f}.path_glob", "The path pattern is empty.")


def _validate_render(cfg: Config, out: _Issues) -> None:
    for vendor, enc in cfg.render.encoder_by_vendor.items():
        if (
            not isinstance(vendor, str)
            or not vendor
            or not isinstance(enc, str)
            or not enc
            or any(c.isspace() for c in enc)
        ):
            out.add(
                f"render.encoder_by_vendor.{vendor}",
                "An encoder is an ffmpeg encoder name such as hevc_nvenc.",
            )
    if cfg.render.container != "mkv":
        out.add("render.container", "Only mkv output is supported.")
    if cfg.render.audio != "copy":
        out.add("render.audio", "Audio is always copied.")


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


def _fps(v: Fraction) -> str:
    if v.denominator == 1:
        return str(v.numerator)
    return f"{format_fraction(v)} ({float(v):.3f})"


def _hz(v: float) -> str:
    return f"{v:g}"


def path_matches(glob: str, path: str) -> bool:
    """``path_glob`` semantics (see module doc). Pure: ``~`` comes from ``$HOME``."""
    pattern = os.path.expanduser(glob) if glob.startswith("~") else glob
    if "/" not in pattern:
        return fnmatch.fnmatchcase(PurePosixPath(path).name, pattern)
    try:
        return PurePosixPath(path).full_match(pattern)
    except ValueError:
        return False


Check = tuple[bool, Msg]


def _checks(m: RuleMatch, s: SourceFacts) -> list[Check]:
    out: list[Check] = []

    def add(ok: bool, yes: str, no: str, params: Mapping[str, str | int | float]) -> None:
        out.append((ok, _msg(yes if ok else no, dict(params))))

    if m.fps_min is not None:
        p = {"value": _fps(s.fps), "limit": _fps(m.fps_min)}
        add(s.fps >= m.fps_min, "fps {value} ≥ fps_min {limit}", "fps {value} < fps_min {limit}", p)
    if m.fps_max is not None:
        p = {"value": _fps(s.fps), "limit": _fps(m.fps_max)}
        add(s.fps <= m.fps_max, "fps {value} ≤ fps_max {limit}", "fps {value} > fps_max {limit}", p)
    if m.width_max is not None:
        p2: dict[str, str | int | float] = {"value": s.width, "limit": m.width_max}
        add(
            s.width <= m.width_max,
            "width {value} ≤ width_max {limit}",
            "width {value} > width_max {limit}",
            p2,
        )
    if m.height_max is not None:
        p2 = {"value": s.height, "limit": m.height_max}
        add(
            s.height <= m.height_max,
            "height {value} ≤ height_max {limit}",
            "height {value} > height_max {limit}",
            p2,
        )
    if m.hdr_class is not None:
        p = {"value": str(s.hdr_class), "limit": str(m.hdr_class)}
        add(
            s.hdr_class == m.hdr_class,
            "HDR class {value} = hdr_class {limit}",
            "HDR class {value} ≠ hdr_class {limit}",
            p,
        )
    if m.display_hz_min is not None:
        p = {"value": _hz(s.display_hz), "limit": _hz(m.display_hz_min)}
        add(
            s.display_hz >= m.display_hz_min,
            "display {value} Hz ≥ display_hz_min {limit}",
            "display {value} Hz < display_hz_min {limit}",
            p,
        )
    if m.interlaced is not None:
        p = {"value": _yes_no(s.interlaced), "limit": _yes_no(m.interlaced)}
        add(
            s.interlaced == m.interlaced,
            "interlaced {value} = interlaced {limit}",
            "interlaced {value} ≠ interlaced {limit}",
            p,
        )
    if m.path_glob is not None:
        p = {"value": s.path, "limit": m.path_glob}
        add(
            path_matches(m.path_glob, s.path),
            "path matches path_glob {limit}",
            "path does not match path_glob {limit}",
            p,
        )
    return out


def _yes_no(v: bool) -> str:
    return "true" if v else "false"


def _fill(m: Msg) -> str:
    """Plain text of a condition, for the combined "all conditions met" line."""
    return m.key.format_map(dict(m.params))


def explain(cfg: Config, facts: SourceFacts) -> RuleTrace:
    """Evaluate ``cfg.rules`` in order; stop at the first match. Pure.

    ``steps`` holds every evaluated rule up to and including the match. A
    skipped step's ``why`` is the first condition that failed, e.g.
    ``Msg("fps {value} > fps_max {limit}", {"value": "60", "limit": "30"})``
    (the GUI adds "skipped:"); a matching step's ``why`` names the conditions met.
    """
    ids = {p.id for p in cfg.profiles}
    steps: list[RuleStep] = []
    for i, rule in enumerate(cfg.rules):
        if rule.profile not in ids:
            steps.append(
                RuleStep(i, False, _msg("profile {id} does not exist", {"id": rule.profile}))
            )
            continue
        checks = _checks(rule.match, facts)
        failed = next((msg for ok, msg in checks if not ok), None)
        if failed is not None:
            steps.append(RuleStep(i, False, failed))
            continue
        if not checks:
            why = _msg("no conditions: matches everything")
        elif len(checks) == 1:
            why = checks[0][1]
        else:
            why = _msg(
                "all conditions met: {conditions}",
                {"conditions": ", ".join(_fill(msg) for _, msg in checks)},
            )
        steps.append(RuleStep(i, True, why))
        return RuleTrace(matched_index=i, profile_id=rule.profile, steps=tuple(steps))
    return RuleTrace(matched_index=None, profile_id=default_profile_id(cfg), steps=tuple(steps))


def default_profile_id(cfg: Config) -> str:
    """The profile used when no rule matches."""
    ids = [p.id for p in cfg.profiles]
    if defaults.DEFAULT_PROFILE_ID in ids or not ids:
        return defaults.DEFAULT_PROFILE_ID
    return ids[0]
