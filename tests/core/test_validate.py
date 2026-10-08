# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U2: ``validate()`` field paths and rules (SCOPE §4.9, §5.3; GUI.md §6)."""

from __future__ import annotations

import dataclasses
from fractions import Fraction
from types import MappingProxyType

import pytest

from buttereye.core import api
from buttereye.core.errors import ErrorCode
from buttereye.core.profiles import defaults, rules
from buttereye.core.types import (
    BackendId,
    Config,
    GeneralSettings,
    HdrClass,
    Profile,
    RenderDefaults,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)

CUSTOM_INDEX = len(defaults.builtin_profiles())  # first custom profile's index


def profile(**changes: object) -> Profile:
    base = Profile(
        id="mine",
        name="Mine",
        backend=BackendId.RIFE_NCNN,
        model="rife-v4.26_ensembleFalse",
        scale=None,
        target=Target(TargetKind.DISPLAY),
        sc_threshold=0.12,
        buffered_frames=None,
        concurrent_frames=None,
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def with_profile(p: Profile, **cfg_changes: object) -> Config:
    cfg = dataclasses.replace(defaults.default_config(), profiles=(*defaults.builtin_profiles(), p))
    return dataclasses.replace(cfg, **cfg_changes)  # type: ignore[arg-type]


def fields(cfg: Config) -> list[str | None]:
    issues = rules.validate(cfg)
    assert all(i.code is ErrorCode.CONFIG_VALUE for i in issues)
    assert all(i.line is None and i.column is None for i in issues)
    return [i.field for i in issues]


def f(name: str) -> str:
    return f"profiles[{CUSTOM_INDEX}].{name}"


def test_defaults_are_valid() -> None:
    assert rules.validate(defaults.default_config()) == ()
    assert api.validate_config(defaults.default_config()) == ()


def test_builtins_are_the_four_shipped_profiles() -> None:
    ps = defaults.builtin_profiles()
    assert [p.id for p in ps] == ["quality", "balanced", "fast", "cpu"]
    assert all(p.builtin for p in ps)
    assert ps[3].backend is BackendId.MVTOOLS and ps[3].model is None
    assert defaults.default_config().profiles == ps


def test_ncnn_with_scale_rejected() -> None:
    assert fields(with_profile(profile(scale=1.0))) == [f("scale")]


def test_mvtools_with_scale_or_model_rejected() -> None:
    p = profile(backend=BackendId.MVTOOLS, model="rife-v4.26_ensembleFalse", scale=0.5)
    assert fields(with_profile(p)) == [f("model"), f("scale")]


def test_trt_needs_opt_in() -> None:
    p = profile(backend=BackendId.RIFE_TRT, model="rife_v4.6", scale=0.5)
    assert fields(with_profile(p)) == [f("backend")]
    on = with_profile(p, general=GeneralSettings(trt_experimental=True))
    assert fields(on) == []


@pytest.mark.parametrize(
    ("model", "scale", "ok"),
    [
        ("rife_v4.6", 0.5, True),
        ("rife_v4.6", 1.0, True),
        ("rife_v4.26", 1.0, True),
        ("rife_v4.26", 0.5, False),  # scale != 1 rejected for >= 4.7 (§5.3)
        ("rife_v4.7", 2.0, False),
        ("rife_v4.6", 0.3, False),  # not a vs-mlrt scale
    ],
)
def test_trt_scale(model: str, scale: float, ok: bool) -> None:
    p = profile(backend=BackendId.RIFE_TRT, model=model, scale=scale)
    cfg = with_profile(p, general=GeneralSettings(trt_experimental=True))
    assert fields(cfg) == ([] if ok else [f("scale")])


def test_auto_backend_accepts_scale() -> None:
    assert fields(with_profile(profile(backend="auto", scale=0.5, model="rife_v4.6"))) == []


def test_fps_target_needs_fraction() -> None:
    assert fields(with_profile(profile(target=Target(TargetKind.FPS)))) == [f("target")]
    assert fields(with_profile(profile(target=Target(TargetKind.FPS, Fraction(0))))) == [
        f("target")
    ]
    assert fields(with_profile(profile(target=Target(TargetKind.FPS, Fraction(5000))))) == [
        f("target")
    ]
    ok = profile(target=Target(TargetKind.FPS, Fraction(120000, 1001)))
    assert fields(with_profile(ok)) == []
    assert fields(with_profile(profile(target=Target(TargetKind.X2, Fraction(60))))) == [
        f("target")
    ]


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"id": ""}, "id"),
        ({"id": "has space"}, "id"),
        ({"id": "-dash-first"}, "id"),
        ({"id": "x" * 65}, "id"),
        ({"name": "  "}, "name"),
        ({"model": "../../etc"}, "model"),
        ({"model": "/usr/share/buttereye/rife-ncnn-models/x"}, "model"),
        ({"sc_threshold": 1.5}, "sc_threshold"),
        ({"sc_threshold": -0.1}, "sc_threshold"),
        ({"sc_threshold": float("nan")}, "sc_threshold"),
        ({"buffered_frames": 0}, "buffered_frames"),
        ({"concurrent_frames": 65}, "concurrent_frames"),
        ({"buffered_frames": True}, "buffered_frames"),
        ({"hdr": "on"}, "hdr"),
        ({"backend": "warp"}, "backend"),
    ],
)
def test_profile_fields(changes: dict[str, object], field: str) -> None:
    assert fields(with_profile(profile(**changes))) == [f(field)]


def test_duplicate_profile_id() -> None:
    dup = profile(id="fast")
    assert fields(with_profile(dup)) == [f("id")]


def test_rules_reference_existing_profiles() -> None:
    cfg = dataclasses.replace(
        defaults.default_config(),
        rules=(
            Rule(RuleMatch(), "quality"),
            Rule(RuleMatch(), "nope"),
            Rule(RuleMatch(), ""),
        ),
    )
    assert fields(cfg) == ["rules[1].profile", "rules[2].profile"]


@pytest.mark.parametrize(
    ("match", "field"),
    [
        (RuleMatch(fps_min=Fraction(60), fps_max=Fraction(30)), "fps_min"),
        (RuleMatch(fps_max=Fraction(0)), "fps_max"),
        (RuleMatch(fps_min=Fraction(-1)), "fps_min"),
        (RuleMatch(width_max=0), "width_max"),
        (RuleMatch(height_max=-1080), "height_max"),
        (RuleMatch(display_hz_min=0.0), "display_hz_min"),
        (RuleMatch(path_glob=" "), "path_glob"),
        (RuleMatch(hdr_class="hdr11"), "hdr_class"),  # type: ignore[arg-type]
        (RuleMatch(interlaced=1), "interlaced"),  # type: ignore[arg-type]
    ],
)
def test_match_fields(match: RuleMatch, field: str) -> None:
    cfg = dataclasses.replace(defaults.default_config(), rules=(Rule(match, "fast"),))
    assert fields(cfg) == [f"rules[0].match.{field}"]


def test_valid_match_with_every_key() -> None:
    m = RuleMatch(
        fps_min=Fraction(24000, 1001),
        fps_max=Fraction(30),
        width_max=1920,
        height_max=1080,
        hdr_class=HdrClass.HDR10,
        display_hz_min=119.88,
        interlaced=False,
        path_glob="~/Videos/**",
    )
    assert fields(dataclasses.replace(defaults.default_config(), rules=(Rule(m, "fast"),))) == []


def test_general_fields() -> None:
    cfg = dataclasses.replace(
        defaults.default_config(),
        general=GeneralSettings(
            backend_override=BackendId.RIFE_TRT,
            gpu=" ",
            language="not a tag",
        ),
    )
    assert fields(cfg) == ["general.backend_override", "general.gpu", "general.language"]
    ok = GeneralSettings(language="pt-BR", gpu="0")
    assert fields(dataclasses.replace(defaults.default_config(), general=ok)) == []


def test_render_fields() -> None:
    cfg = dataclasses.replace(
        defaults.default_config(),
        render=RenderDefaults(MappingProxyType({"nvidia": "hevc nvenc", "amd": "hevc_vaapi"})),
    )
    assert fields(cfg) == ["render.encoder_by_vendor.nvidia"]


def test_issues_are_in_config_order() -> None:
    cfg = with_profile(
        profile(scale=1.0, sc_threshold=3.0),
        general=GeneralSettings(language="??"),
        rules=(Rule(RuleMatch(width_max=0), "zzz"),),
    )
    assert fields(cfg) == [
        "general.language",
        f("scale"),
        f("sc_threshold"),
        "rules[0].profile",
        "rules[0].match.width_max",
    ]


def test_messages_are_immutable_and_rendered() -> None:
    (issue,) = rules.validate(with_profile(profile(id="fast")))
    with pytest.raises(TypeError):
        issue.message.params["id"] = "x"  # type: ignore[index]
    assert api.render(issue.message) == "Another profile already uses the id fast."
