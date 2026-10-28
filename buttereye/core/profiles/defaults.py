# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shipped profiles and the default config (SCOPE §4.9, §5.3, §5.5).

The built-in profiles are ``quality``, ``balanced``, ``fast``, ``cpu`` and
``battery``. The
RIFE model names are the directory names of the curated
``buttereye-rife-ncnn-models`` package (``/usr/share/buttereye/rife-ncnn-models``).
Buffer sizes are left ``None`` so the generated script uses the §4.4 defaults.
Nothing here claims a measured speed: real-time capability comes from the
benchmark (F14) only.

``battery`` is for a laptop away from the wall or running hot: GPU motion
vectors at 2×, the lightest GPU engine. No shipped rule picks it, because the
default rules apply to every platform and a new rule must not change what an
existing install plays; ``BATTERY_RULE`` is the rule to offer (put first, it
wins whenever the laptop is on battery, on any platform).
"""

from __future__ import annotations

from fractions import Fraction
from types import MappingProxyType

from buttereye.core.types import (
    BackendId,
    Config,
    GeneralSettings,
    Profile,
    RenderDefaults,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)

SCHEMA_VERSION = 1

#: Profile used when no rule matches (``RuleTrace.matched_index is None``).
DEFAULT_PROFILE_ID = "balanced"

#: Scene-change threshold of the from-scratch PlaneStats detector (§5.5).
DEFAULT_SC_THRESHOLD = 0.12

_BUILTINS: tuple[Profile, ...] = (
    Profile(
        id="quality",
        name="Quality",
        backend=BackendId.RIFE_NCNN,
        model="rife-v4.26_ensembleFalse",
        scale=None,
        target=Target(TargetKind.DISPLAY),
        sc_threshold=DEFAULT_SC_THRESHOLD,
        buffered_frames=None,
        concurrent_frames=None,
        builtin=True,
    ),
    Profile(
        id="balanced",
        name="Balanced",
        backend=BackendId.RIFE_NCNN,
        model="rife-v4.22_lite_ensembleFalse",
        scale=None,
        target=Target(TargetKind.DISPLAY),
        sc_threshold=DEFAULT_SC_THRESHOLD,
        buffered_frames=None,
        concurrent_frames=None,
        builtin=True,
    ),
    Profile(
        id="fast",
        name="Fast",
        backend=BackendId.RIFE_NCNN,
        model="rife-v4.22_lite_ensembleFalse",
        scale=None,
        target=Target(TargetKind.X2),
        sc_threshold=DEFAULT_SC_THRESHOLD,
        buffered_frames=None,
        concurrent_frames=None,
        builtin=True,
    ),
    Profile(
        id="cpu",
        name="CPU (MVTools)",
        backend=BackendId.MVTOOLS,
        model=None,
        scale=None,
        target=Target(TargetKind.X2),
        sc_threshold=DEFAULT_SC_THRESHOLD,
        buffered_frames=None,
        concurrent_frames=None,
        builtin=True,
    ),
    Profile(
        id="battery",
        name="Battery",
        backend=BackendId.MVGPU,
        model=None,
        scale=None,
        target=Target(TargetKind.X2),
        sc_threshold=DEFAULT_SC_THRESHOLD,
        buffered_frames=None,
        concurrent_frames=None,
        builtin=True,
    ),
)

#: id of the shipped low-power profile
BATTERY_PROFILE_ID = "battery"

BUILTIN_IDS: frozenset[str] = frozenset(p.id for p in _BUILTINS)

#: Shipped rules (first match wins). Low-rate sources up to 1080p get the
#: heaviest model; high-rate sources (already >= 48 fps) only get doubled.
#: Everything else falls through to ``DEFAULT_PROFILE_ID``.
_DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(RuleMatch(fps_max=Fraction(30), height_max=1080), "quality"),
    Rule(RuleMatch(fps_min=Fraction(48)), "fast"),
)

#: the rule that sends a laptop on battery to ``battery`` (not shipped; see above)
BATTERY_RULE = Rule(RuleMatch(on_battery=True), BATTERY_PROFILE_ID)


def builtin_profiles() -> tuple[Profile, ...]:
    """The five shipped profiles, ``builtin=True``. Pure."""
    return _BUILTINS


def builtin_profile(profile_id: str) -> Profile | None:
    """The shipped version of a built-in profile (for "Reset to default")."""
    return next((p for p in _BUILTINS if p.id == profile_id), None)


def default_rules() -> tuple[Rule, ...]:
    return _DEFAULT_RULES


def default_config() -> Config:
    """The config used before ``config.toml`` exists, or when it is invalid. Pure."""
    return Config(
        schema_version=SCHEMA_VERSION,
        general=GeneralSettings(),
        profiles=_BUILTINS,
        rules=_DEFAULT_RULES,
        render=RenderDefaults(),
        unknown=MappingProxyType({}),
    )
