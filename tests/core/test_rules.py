# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U2: first-match rules and ``RuleTrace`` reasons (SCOPE §4.9, F15; GUI.md §6)."""

from __future__ import annotations

import dataclasses
from fractions import Fraction

import pytest

from buttereye.core import api
from buttereye.core.i18n import render
from buttereye.core.profiles import defaults, rules, schema
from buttereye.core.types import (
    Config,
    HdrClass,
    PowerSnapshot,
    Rule,
    RuleMatch,
    SourceFacts,
)

NTSC_FILM = Fraction(24000, 1001)


def facts(**changes: object) -> SourceFacts:
    base = SourceFacts(
        fps=NTSC_FILM,
        width=1920,
        height=1080,
        hdr_class=HdrClass.SDR,
        display_hz=120.0,
        interlaced=False,
        path="/home/u/Videos/Anime/s01/e01.mkv",
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def cfg_with(*rule_list: Rule) -> Config:
    return dataclasses.replace(defaults.default_config(), rules=rule_list)


def why(trace_step_msg: object) -> str:
    from buttereye.core.types import Msg

    assert isinstance(trace_step_msg, Msg)
    return render(trace_step_msg)


# ---------------------------------------------------------------- first match


def test_first_match_wins_and_stops() -> None:
    cfg = cfg_with(
        Rule(RuleMatch(fps_max=Fraction(30)), "quality"),
        Rule(RuleMatch(fps_max=Fraction(30)), "fast"),
    )
    trace = rules.explain(cfg, facts())
    assert trace.matched_index == 0 and trace.profile_id == "quality"
    assert len(trace.steps) == 1 and trace.steps[0].matched


def test_skipped_steps_carry_the_failing_condition() -> None:
    cfg = cfg_with(
        Rule(RuleMatch(fps_max=Fraction(30)), "quality"),
        Rule(RuleMatch(height_max=720), "balanced"),
        Rule(RuleMatch(fps_min=Fraction(50)), "fast"),
        Rule(RuleMatch(), "cpu"),
        Rule(RuleMatch(), "quality"),  # never evaluated
    )
    trace = rules.explain(cfg, facts(fps=Fraction(60)))
    assert trace.matched_index == 2 and trace.profile_id == "fast"
    assert [s.index for s in trace.steps] == [0, 1, 2]
    assert [s.matched for s in trace.steps] == [False, False, True]
    assert why(trace.steps[0].why) == "fps 60 > fps_max 30"
    assert why(trace.steps[1].why) == "height 1080 > height_max 720"
    assert why(trace.steps[2].why) == "fps 60 ≥ fps_min 50"


def test_first_failing_condition_is_reported() -> None:
    cfg = cfg_with(Rule(RuleMatch(width_max=1280, height_max=720), "quality"))
    trace = rules.explain(cfg, facts())
    assert trace.matched_index is None
    assert why(trace.steps[0].why) == "width 1920 > width_max 1280"


def test_all_conditions_listed_on_match() -> None:
    cfg = cfg_with(Rule(RuleMatch(fps_max=Fraction(30), height_max=1080), "quality"))
    trace = rules.explain(cfg, facts())
    assert why(trace.steps[0].why) == (
        "all conditions met: fps 24000/1001 (23.976) ≤ fps_max 30, height 1080 ≤ height_max 1080"
    )


def test_empty_match_matches_everything() -> None:
    trace = rules.explain(cfg_with(Rule(RuleMatch(), "cpu")), facts())
    assert trace.matched_index == 0 and trace.profile_id == "cpu"
    assert why(trace.steps[0].why) == "no conditions: matches everything"


def test_no_match_falls_back_to_default_profile() -> None:
    trace = rules.explain(cfg_with(Rule(RuleMatch(height_max=480), "quality")), facts())
    assert trace.matched_index is None
    assert trace.profile_id == defaults.DEFAULT_PROFILE_ID == "balanced"
    assert len(trace.steps) == 1


def test_no_rules_at_all() -> None:
    trace = rules.explain(cfg_with(), facts())
    assert trace == trace.__class__(None, "balanced", ())


def test_default_falls_back_to_first_profile_without_balanced() -> None:
    cfg = dataclasses.replace(
        cfg_with(), profiles=tuple(p for p in defaults.builtin_profiles() if p.id != "balanced")
    )
    assert rules.explain(cfg, facts()).profile_id == "quality"


def test_rule_with_missing_profile_is_skipped() -> None:
    cfg = cfg_with(Rule(RuleMatch(), "gone"), Rule(RuleMatch(), "fast"))
    trace = rules.explain(cfg, facts())
    assert trace.matched_index == 1 and trace.profile_id == "fast"
    assert not trace.steps[0].matched
    assert why(trace.steps[0].why) == "profile gone does not exist"


# ---------------------------------------------------------------- each key


@pytest.mark.parametrize(
    ("match", "source", "ok", "text"),
    [
        (RuleMatch(fps_min=Fraction(25)), facts(fps=Fraction(25)), True, "fps 25 ≥ fps_min 25"),
        (RuleMatch(fps_min=Fraction(25)), facts(), False, "fps 24000/1001 (23.976) < fps_min 25"),
        (RuleMatch(fps_max=Fraction(30)), facts(fps=Fraction(30)), True, "fps 30 ≤ fps_max 30"),
        (
            RuleMatch(fps_max=Fraction(30)),
            facts(fps=Fraction(60000, 1001)),
            False,
            "fps 60000/1001 (59.940) > fps_max 30",
        ),
        (RuleMatch(width_max=1920), facts(), True, "width 1920 ≤ width_max 1920"),
        (RuleMatch(width_max=1919), facts(), False, "width 1920 > width_max 1919"),
        (RuleMatch(height_max=1080), facts(), True, "height 1080 ≤ height_max 1080"),
        (RuleMatch(height_max=1080), facts(height=2160), False, "height 2160 > height_max 1080"),
        (RuleMatch(hdr_class=HdrClass.SDR), facts(), True, "HDR class sdr = hdr_class sdr"),
        (
            RuleMatch(hdr_class=HdrClass.SDR),
            facts(hdr_class=HdrClass.HDR10),
            False,
            "HDR class hdr10 ≠ hdr_class sdr",
        ),
        (RuleMatch(display_hz_min=119.5), facts(), True, "display 120 Hz ≥ display_hz_min 119.5"),
        (
            RuleMatch(display_hz_min=144.0),
            facts(display_hz=59.94),
            False,
            "display 59.94 Hz < display_hz_min 144",
        ),
        (
            RuleMatch(interlaced=True),
            facts(interlaced=True),
            True,
            "interlaced true = interlaced true",
        ),
        (
            RuleMatch(interlaced=False),
            facts(interlaced=True),
            False,
            "interlaced true ≠ interlaced false",
        ),
        (RuleMatch(path_glob="*.mkv"), facts(), True, "path matches path_glob *.mkv"),
        (RuleMatch(path_glob="*.mp4"), facts(), False, "path does not match path_glob *.mp4"),
    ],
)
def test_each_key(match: RuleMatch, source: SourceFacts, ok: bool, text: str) -> None:
    trace = rules.explain(cfg_with(Rule(match, "fast")), source)
    assert (trace.matched_index == 0) is ok
    assert trace.steps[0].matched is ok
    assert why(trace.steps[0].why) == text


def test_ntsc_fractions_are_exact() -> None:
    exact = cfg_with(Rule(RuleMatch(fps_min=NTSC_FILM, fps_max=NTSC_FILM), "fast"))
    assert rules.explain(exact, facts()).matched_index == 0
    # 23.976 as a plain fraction is 2997/125, just below 24000/1001: that is what a
    # rule bound used to be, and it matched no NTSC film
    decimal = cfg_with(Rule(RuleMatch(fps_max=schema.parse_fraction(23.976)), "fast"))
    trace = rules.explain(decimal, facts())
    assert trace.matched_index is None
    assert why(trace.steps[0].why) == "fps 24000/1001 (23.976) > fps_max 2997/125 (23.976)"
    # B16: a rule bound reads it as the NTSC rate it stands for
    snapped = cfg_with(Rule(RuleMatch(fps_max=schema.parse_rule_fps(23.976)), "fast"))
    assert rules.explain(snapped, facts()).matched_index == 0
    # and through TOML text
    loaded = schema.parse_text(
        'schema_version = 1\n[[rules]]\nprofile = "fast"\n'
        'match = { fps_min = "24000/1001", fps_max = "24000/1001" }\n'
    )
    assert rules.explain(loaded.config, facts()).matched_index == 0


@pytest.mark.parametrize(
    ("glob", "path", "ok"),
    [
        ("*.mkv", "/a/b/c.mkv", True),
        ("*.MKV", "/a/b/c.mkv", False),  # case-sensitive
        ("e0?.mkv", "/x/e01.mkv", True),
        ("/home/u/Videos/Anime/**", "/home/u/Videos/Anime/s01/e01.mkv", True),
        ("/home/u/Videos/Anime/*", "/home/u/Videos/Anime/s01/e01.mkv", False),
        ("/home/u/Videos/Anime/*/*.mkv", "/home/u/Videos/Anime/s01/e01.mkv", True),
        ("/home/u/Videos/**/*.mkv", "/home/u/Videos/Anime/s01/e01.mkv", True),
        ("~/Videos/Anime/**", "/home/u/Videos/Anime/s01/e01.mkv", True),
        ("~/Videos/Films/**", "/home/u/Videos/Anime/s01/e01.mkv", False),
    ],
)
def test_path_glob(monkeypatch: pytest.MonkeyPatch, glob: str, path: str, ok: bool) -> None:
    monkeypatch.setenv("HOME", "/home/u")
    assert rules.path_matches(glob, path) is ok
    trace = rules.explain(cfg_with(Rule(RuleMatch(path_glob=glob), "fast")), facts(path=path))
    assert (trace.matched_index == 0) is ok


def test_default_rules_on_common_sources() -> None:
    cfg = defaults.default_config()
    assert rules.explain(cfg, facts()).profile_id == "quality"  # 1080p23.976
    assert rules.explain(cfg, facts(height=2160, width=3840)).profile_id == "balanced"
    assert rules.explain(cfg, facts(fps=Fraction(60))).profile_id == "fast"
    every_referenced = {r.profile for r in cfg.rules} | {defaults.DEFAULT_PROFILE_ID}
    assert every_referenced <= {p.id for p in cfg.profiles}


def test_facade_helper_matches_provider() -> None:
    cfg = defaults.default_config()
    assert api.explain_rules(cfg, facts()) == rules.explain(cfg, facts())


def test_trace_is_immutable() -> None:
    trace = rules.explain(defaults.default_config(), facts(fps=Fraction(60)))
    with pytest.raises(TypeError):
        trace.steps[0].why.params["value"] = "x"  # type: ignore[index]


# ---------------------------------------------------------------- NTSC decimals (B16)


@pytest.mark.parametrize(
    ("written", "read"),
    [
        (23.976, Fraction(24000, 1001)),
        ("23.976", Fraction(24000, 1001)),
        (29.97, Fraction(30000, 1001)),
        (59.94, Fraction(60000, 1001)),
        ("119.88", Fraction(120000, 1001)),
        (47.952, Fraction(48000, 1001)),
        (23.975, Fraction(24000, 1001)),  # 1.0e-3 away: still meant
        # left exactly as written
        ("2997/125", Fraction(2997, 125)),  # an explicit n/d is never moved
        (24, Fraction(24)),
        (24.0, Fraction(24)),
        ("30", Fraction(30)),
        (23.97, Fraction(2397, 100)),  # 6e-3 away: a different rate
        (25.0, Fraction(25)),
        (0.5, Fraction(1, 2)),
    ],
)
def test_rule_fps_snaps_decimal_ntsc_rates(written: object, read: Fraction) -> None:
    assert schema.parse_rule_fps(written) == read


def test_ntsc_decimal_bounds_through_toml() -> None:
    text = (
        'schema_version = 1\n[[rules]]\nprofile = "fast"\n'
        'match = { fps_min = 23.976, fps_max = "29.97" }\n'
    )
    loaded = schema.parse_text(text)
    assert loaded.issues == []
    match = loaded.config.rules[0].match
    assert (match.fps_min, match.fps_max) == (NTSC_FILM, Fraction(30000, 1001))
    assert rules.explain(loaded.config, facts()).matched_index == 0
    # and written back as the exact fraction
    again = schema.parse_text(schema.dumps_config(loaded.config))
    assert again.config.rules == loaded.config.rules
    assert 'fps_min = "24000/1001"' in schema.dumps_config(loaded.config)


# ---------------------------------------------------------------- power


HOT_BATTERY = PowerSnapshot(on_battery=True, low_power=False, thermal=2)


@pytest.mark.parametrize(
    ("match", "power", "ok", "text"),
    [
        (
            RuleMatch(on_battery=True),
            HOT_BATTERY,
            True,
            "on battery true = on_battery true",
        ),
        (
            RuleMatch(on_battery=True),
            PowerSnapshot(on_battery=False),
            False,
            "on battery false ≠ on_battery true",
        ),
        (
            RuleMatch(thermal_min=2),
            HOT_BATTERY,
            True,
            "temperature serious ≥ thermal_min serious",
        ),
        (
            RuleMatch(thermal_min=2),
            PowerSnapshot(thermal=1),
            False,
            "temperature fair < thermal_min serious",
        ),
        (
            RuleMatch(thermal_max=1),
            HOT_BATTERY,
            False,
            "temperature serious > thermal_max fair",
        ),
        (
            RuleMatch(thermal_max=1),
            PowerSnapshot(thermal=0),
            True,
            "temperature nominal ≤ thermal_max fair",
        ),
    ],
)
def test_power_conditions(match: RuleMatch, power: PowerSnapshot, ok: bool, text: str) -> None:
    trace = rules.explain(cfg_with(Rule(match, "fast")), facts(), power)
    assert (trace.matched_index == 0) is ok
    assert why(trace.steps[0].why) == text


@pytest.mark.parametrize("power", [None, PowerSnapshot()])
def test_unknown_power_is_mains_at_nominal(power: PowerSnapshot | None) -> None:
    """What Linux and Windows always answer: a Mac rule keeps working there."""
    cfg = cfg_with(
        Rule(RuleMatch(on_battery=True), "cpu"),
        Rule(RuleMatch(thermal_min=1), "cpu"),
        Rule(RuleMatch(on_battery=False, thermal_max=0), "fast"),
    )
    trace = rules.explain(cfg, facts(), power)
    assert trace.matched_index == 2 and trace.profile_id == "fast"
    assert why(trace.steps[0].why) == "on battery false ≠ on_battery true"
    assert why(trace.steps[1].why) == "temperature nominal < thermal_min fair"


def test_battery_rule_sends_a_mac_on_battery_to_the_battery_profile() -> None:
    cfg = cfg_with(defaults.BATTERY_RULE, *defaults.default_rules())
    on_battery = rules.explain(cfg, facts(), PowerSnapshot(on_battery=True))
    assert on_battery.profile_id == defaults.BATTERY_PROFILE_ID
    on_mains = rules.explain(cfg, facts(), PowerSnapshot(on_battery=False))
    assert on_mains.profile_id == "quality"


def test_power_bounds_through_toml() -> None:
    text = (
        'schema_version = 1\n[[rules]]\nprofile = "battery"\n'
        'match = { on_battery = true, thermal_min = "fair", thermal_max = 3 }\n'
    )
    loaded = schema.parse_text(text)
    assert loaded.issues == []
    assert loaded.config.rules[0].match == RuleMatch(on_battery=True, thermal_min=1, thermal_max=3)
    out = schema.dumps_config(loaded.config)
    assert 'match = { on_battery = true, thermal_min = "fair", thermal_max = "critical" }' in out
    assert schema.parse_text(out).config.rules == loaded.config.rules


@pytest.mark.parametrize("bad", ['"warm"', "4", "-1", "true", "1.5"])
def test_bad_thermal_bound_is_reported(bad: str) -> None:
    loaded = schema.parse_text(
        f'schema_version = 1\n[[rules]]\nprofile = "fast"\nmatch = {{ thermal_max = {bad} }}\n'
    )
    assert [i.field for i in loaded.issues] == ["rules[0].match.thermal_max"]
    assert loaded.config.rules[0].match.thermal_max is None
