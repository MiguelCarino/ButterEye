# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U2: config.toml load/save round trip, unknown keys, v0 migration, newer schema,
invalid TOML (docs/design/GUI.md §6, SCOPE §4.9, F15)."""

from __future__ import annotations

import dataclasses
import os
import stat
import tomllib
from datetime import UTC, date, datetime, time
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

import pytest

from buttereye.core.errors import ConfigError, ErrorCode
from buttereye.core.profiles import config, defaults, schema
from buttereye.core.testing.scenarios import fake_paths
from buttereye.core.types import (
    BackendId,
    Config,
    GeneralSettings,
    HdrClass,
    Paths,
    Profile,
    RenderDefaults,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return fake_paths(tmp_path)


def _write(paths: Paths, text: str | bytes) -> str:
    paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode() if isinstance(text, str) else text
    paths.config_file.write_bytes(data)
    return config.revision_of(data)


def _custom(pid: str = "anime", **changes: object) -> Profile:
    base = Profile(
        id=pid,
        name="Anime night",
        backend=BackendId.RIFE_NCNN,
        model="rife-v4.26_ensembleFalse",
        scale=None,
        target=Target(TargetKind.FPS, Fraction(120000, 1001)),
        sc_threshold=0.2,
        buffered_frames=5,
        concurrent_frames=3,
        hdr="skip",
    )
    return dataclasses.replace(base, **changes)  # type: ignore[arg-type]


def _rich_config() -> Config:
    """Every field set somewhere, unknown keys at every level."""
    builtins = list(defaults.builtin_profiles())
    builtins[2] = dataclasses.replace(builtins[2], sc_threshold=0.3, buffered_frames=6)
    profiles = (
        *builtins,
        _custom(),
        _custom(
            "trt-4k",
            name='TRT "4K" \\ night',
            backend=BackendId.RIFE_TRT,
            model="rife_v4.6",
            scale=0.5,
            target=Target(TargetKind.X2),
            hdr="passthrough",
            buffered_frames=None,
            concurrent_frames=None,
        ),
        _custom("auto-one", backend="auto", model=None, target=Target(TargetKind.DISPLAY)),
    )
    rules = (
        Rule(
            RuleMatch(
                fps_min=Fraction(24000, 1001),
                fps_max=Fraction(30),
                width_max=1920,
                height_max=1080,
                hdr_class=HdrClass.SDR,
                display_hz_min=119.5,
                interlaced=False,
                path_glob="~/Videos/Anime/**",
            ),
            "anime",
        ),
        Rule(RuleMatch(interlaced=True), "cpu"),
        Rule(RuleMatch(), "balanced"),
    )
    unknown = MappingProxyType(
        {
            "future_flag": True,
            "future_table": MappingProxyType({"a": 1, "nested": MappingProxyType({"b": "c"})}),
            "future_list": (1, 2, 3),
            "future_aot": (MappingProxyType({"x": 1}), MappingProxyType({"x": 2})),
            "when": datetime(2026, 10, 7, 12, 30, tzinfo=UTC),
            "day": date(2026, 10, 7),
            "clock": time(7, 30, 15, 250000),
            '"odd key"': "quoted",
            "general.future_c": "x",
            "render.future_r": 2.5,
            "profiles[anime].future_p": MappingProxyType({"k": "v"}),
            "profiles[fast].note": "kept with the override",
            "rules[0].future_rule": "y",
            "rules[0].match.future_m": 7,
        }
    )
    return Config(
        schema_version=1,
        general=GeneralSettings(
            backend_override=BackendId.RIFE_TRT,
            gpu="5a0b2c3d-0000-1111-2222-333344445555",
            language="pt-BR",
            trt_experimental=True,
        ),
        profiles=profiles,
        rules=rules,
        render=RenderDefaults(MappingProxyType({"nvidia": "hevc_nvenc", "amd": "hevc_vaapi"})),
        unknown=unknown,
    )


# ---------------------------------------------------------------- first run


def test_missing_file_is_first_run(paths: Paths) -> None:
    load = config.load(paths)
    assert load.exists is False
    assert load.revision == ""
    assert load.read_only is False and load.used_defaults is False
    assert load.issues == ()
    assert load.config == defaults.default_config()
    assert not paths.config_file.parent.exists()  # load never writes


def test_first_save_creates_0600_file(paths: Paths) -> None:
    rev = config.save(paths, defaults.default_config(), expected_revision="")
    data = paths.config_file.read_bytes()
    assert rev == config.revision_of(data)
    assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600
    load = config.load(paths)
    assert load.exists and load.revision == rev and load.issues == ()
    assert load.config == defaults.default_config()
    assert tomllib.loads(data.decode())["schema_version"] == 1
    # unchanged built-ins are not written
    assert "[[profiles]]" not in data.decode()
    leftovers = [p.name for p in paths.config_file.parent.iterdir()]
    assert leftovers == ["config.toml"]


# ---------------------------------------------------------------- round trip


def test_rich_config_round_trips(paths: Paths) -> None:
    cfg = _rich_config()
    rev = config.save(paths, cfg, expected_revision="")
    load = config.load(paths)
    assert load.config == cfg
    assert load.revision == rev
    assert all(i.code is ErrorCode.CONFIG_UNKNOWN_KEY for i in load.issues), load.issues
    assert {i.field for i in load.issues} == set(cfg.unknown)
    first = paths.config_file.read_bytes()
    # saving the loaded config again is byte-identical and keeps the revision
    rev2 = config.save(paths, load.config, expected_revision=rev)
    assert rev2 == rev and paths.config_file.read_bytes() == first


def test_writer_output_is_toml_that_tomllib_reads(paths: Paths) -> None:
    text = schema.dumps_config(_rich_config())
    doc = tomllib.loads(text)
    assert doc["schema_version"] == 1
    assert doc["general"]["backend_override"] == "rife-trt"
    assert doc["rules"][0]["match"]["fps_min"] == "24000/1001"
    assert doc["rules"][0]["match"]["fps_max"] == 30
    assert doc["rules"][0]["future_rule"] == "y"
    assert doc["future_aot"] == [{"x": 1}, {"x": 2}]
    assert doc["odd key"] == "quoted"
    names = [p["id"] for p in doc["profiles"]]
    assert names == ["fast", "anime", "trt-4k", "auto-one"]  # only the changed built-in


@pytest.mark.parametrize(
    "value",
    [
        'quote " and \\ backslash',
        "new\nline\ttab\r",
        "\x00\x1f\x7f control",
        "日本語 ✓",
        '"""',
        "'''",
        "",
        "%:,",
        "a b",
    ],
)
def test_strings_survive(paths: Paths, value: str) -> None:
    cfg = dataclasses.replace(
        defaults.default_config(),
        profiles=(*defaults.builtin_profiles(), _custom(name=value or "x")),
        rules=(Rule(RuleMatch(path_glob=value or "*"), "anime"),),
        unknown=MappingProxyType({"s": value}),
    )
    out = schema.parse_text(schema.dumps_config(cfg))
    assert not out.used_defaults
    assert out.config == cfg


def test_float_edge_values_written(paths: Paths) -> None:
    for v in (0.0, 1e-7, 1e16, 123456789.123, 0.1 + 0.2, float("inf"), float("-inf")):
        cfg = dataclasses.replace(defaults.default_config(), unknown=MappingProxyType({"f": v}))
        assert schema.parse_text(schema.dumps_config(cfg)).config.unknown["f"] == v
    nan = dataclasses.replace(
        defaults.default_config(), unknown=MappingProxyType({"f": float("nan")})
    )
    got = schema.parse_text(schema.dumps_config(nan)).config.unknown["f"]
    assert isinstance(got, float) and got != got


def test_unwritable_unknown_value_is_refused(paths: Paths) -> None:
    cfg = dataclasses.replace(
        defaults.default_config(), unknown=MappingProxyType({"obj": object()})
    )
    with pytest.raises(ConfigError) as exc:
        config.save(paths, cfg, expected_revision="")
    assert exc.value.code is ErrorCode.CONFIG_VALUE
    assert not paths.config_file.exists()


HAND_WRITTEN = """\
# my settings
schema_version = 1
mystery = "kept"

[general]
gpu = "abc"
colour = "blue"

[[profiles]]
id = "mine"
name = "Mine"
backend = "mvtools"
target = "fps:60"
sc_threshold = 0.1
sparkle = 3

[[rules]]
profile = "mine"
match = { fps_max = 23.976, wobble = true }

[[rules]]
profile = "quality"
[rules.match]
height_max = 1080
shimmer = "x"

[extra]
a = [1, 2]
"""


def test_hand_written_unknown_keys_kept_and_located(paths: Paths) -> None:
    rev = _write(paths, HAND_WRITTEN)
    load = config.load(paths)
    assert not load.used_defaults and not load.read_only
    by_field = {i.field: i for i in load.issues}
    assert all(i.code is ErrorCode.CONFIG_UNKNOWN_KEY for i in load.issues)
    assert set(by_field) == {
        "mystery",
        "general.colour",
        "profiles[mine].sparkle",
        "rules[0].match.wobble",
        "rules[1].match.shimmer",
        "extra",
    }
    assert (by_field["mystery"].line, by_field["mystery"].column) == (3, 1)
    assert (by_field["general.colour"].line, by_field["general.colour"].column) == (7, 1)
    assert by_field["profiles[mine].sparkle"].line == 15
    wobble = by_field["rules[0].match.wobble"]
    assert wobble.line == 19 and wobble.column == HAND_WRITTEN.splitlines()[18].index("wobble") + 1
    assert by_field["rules[1].match.shimmer"].line == 25
    assert by_field["extra"].line == 27
    cfg = load.config
    assert cfg.rules[0].match.fps_max == Fraction("23.976")  # decimal kept exact
    mine = next(p for p in cfg.profiles if p.id == "mine")
    assert mine.target == Target(TargetKind.FPS, Fraction(60)) and mine.backend is BackendId.MVTOOLS
    assert cfg.unknown["extra"] == MappingProxyType({"a": (1, 2)})
    # save without edits: content and unknown keys survive (comments do not, F15)
    config.save(paths, cfg, expected_revision=rev)
    text = paths.config_file.read_text()
    assert "# my settings" not in text
    doc = tomllib.loads(text)
    original = tomllib.loads(HAND_WRITTEN)
    assert doc["mystery"] == original["mystery"]
    assert doc["general"] == original["general"]
    assert doc["profiles"][0]["sparkle"] == 3
    assert doc["rules"][0]["match"]["wobble"] is True
    assert doc["rules"][1]["match"]["shimmer"] == "x"
    assert doc["extra"] == {"a": [1, 2]}
    assert config.load(paths).config == cfg


def test_builtin_override_and_reset(paths: Paths) -> None:
    base = defaults.default_config()
    changed = list(base.profiles)
    changed[0] = dataclasses.replace(changed[0], name="My quality", sc_threshold=0.25)
    rev = config.save(
        paths, dataclasses.replace(base, profiles=tuple(changed)), expected_revision=""
    )
    doc = tomllib.loads(paths.config_file.read_text())
    assert [p["id"] for p in doc["profiles"]] == ["quality"]
    load = config.load(paths)
    assert load.config.profiles[0].builtin is True
    assert load.config.profiles[0].name == "My quality"
    assert [p.id for p in load.config.profiles] == ["quality", "balanced", "fast", "cpu"]
    # "Reset to default" = the shipped profile again -> entry disappears
    reset = dataclasses.replace(load.config, profiles=defaults.builtin_profiles())
    config.save(paths, reset, expected_revision=rev)
    assert "profiles" not in tomllib.loads(paths.config_file.read_text())


def test_partial_builtin_override_keeps_shipped_values(paths: Paths) -> None:
    _write(paths, 'schema_version = 1\n[[profiles]]\nid = "fast"\nsc_threshold = 0.4\n')
    fast = config.load(paths).config.profiles[2]
    shipped = defaults.builtin_profile("fast")
    assert shipped is not None
    assert fast == dataclasses.replace(shipped, sc_threshold=0.4)


def test_rules_absent_vs_empty(paths: Paths) -> None:
    _write(paths, "schema_version = 1\n")
    assert config.load(paths).config.rules == defaults.default_rules()
    rev = config.revision_of(paths.config_file.read_bytes())
    no_rules = dataclasses.replace(defaults.default_config(), rules=())
    config.save(paths, no_rules, expected_revision=rev)
    assert "rules = []" in paths.config_file.read_text()
    assert config.load(paths).config.rules == ()


def test_load_does_not_write(paths: Paths) -> None:
    _write(paths, HAND_WRITTEN)
    before = sorted((p.name, p.stat().st_mtime_ns) for p in paths.config_file.parent.iterdir())
    config.load(paths)
    after = sorted((p.name, p.stat().st_mtime_ns) for p in paths.config_file.parent.iterdir())
    assert before == after


def test_symlinked_config_is_followed(paths: Paths, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "buttereye.toml"
    real.parent.mkdir()
    real.write_text("schema_version = 1\n")
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.symlink_to(real)
    rev = config.load(paths).revision
    config.save(
        paths, dataclasses.replace(defaults.default_config(), rules=()), expected_revision=rev
    )
    assert paths.config_file.is_symlink()
    assert "rules = []" in real.read_text()


# ---------------------------------------------------------------- v0 migration

V0_FIXTURE = """\
# pre-release (M0 spike) layout: no schema_version
[general]
backend = "mvtools"
language = "de"

[[profiles]]
id = "old"
name = "Old 2x"
backend = "rife-ncnn"
model = "rife-v4.18_ensembleFalse"
target = 2
sc_threshold = 0.15

[[rules]]
profile = "old"
match = { height_max = 720 }
"""

V0_AUTO_FIXTURE = """\
schema_version = 0
[general]
backend = "auto"
"""


def test_v0_migrates_in_memory_without_writing(paths: Paths) -> None:
    rev = _write(paths, V0_FIXTURE)
    load = config.load(paths)
    assert load.exists and not load.used_defaults and not load.read_only
    assert load.issues == ()
    cfg = load.config
    assert cfg.schema_version == 1
    assert cfg.general.backend_override is BackendId.MVTOOLS
    assert cfg.general.language == "de"
    old = next(p for p in cfg.profiles if p.id == "old")
    assert old.target == Target(TargetKind.X2) and old.model == "rife-v4.18_ensembleFalse"
    assert cfg.rules == (Rule(RuleMatch(height_max=720), "old"),)
    assert not config.backup_path(paths).exists()
    assert config.revision_of(paths.config_file.read_bytes()) == rev


def test_v0_save_writes_bak_first(paths: Paths) -> None:
    rev = _write(paths, V0_FIXTURE)
    cfg = config.load(paths).config
    new_rev = config.save(paths, cfg, expected_revision=rev)
    bak = config.backup_path(paths)
    assert bak.read_bytes() == V0_FIXTURE.encode()
    assert stat.S_IMODE(bak.stat().st_mode) == 0o600
    doc = tomllib.loads(paths.config_file.read_text())
    assert doc["schema_version"] == 1 and doc["general"]["backend_override"] == "mvtools"
    reload = config.load(paths)
    assert reload.config == cfg and reload.revision == new_rev
    # a later v1 save leaves the .bak alone
    bak_mtime = bak.stat().st_mtime_ns
    config.save(paths, dataclasses.replace(cfg, rules=()), expected_revision=new_rev)
    assert bak.stat().st_mtime_ns == bak_mtime and bak.read_bytes() == V0_FIXTURE.encode()


def test_v0_auto_backend_means_no_override(paths: Paths) -> None:
    _write(paths, V0_AUTO_FIXTURE)
    load = config.load(paths)
    assert load.issues == () and load.config.general.backend_override is None


# ---------------------------------------------------------------- newer schema


def test_newer_schema_is_read_only(paths: Paths) -> None:
    text = 'schema_version = 2\nnew_thing = 1\n[general]\nlanguage = "fr"\n'
    rev = _write(paths, text)
    load = config.load(paths)
    assert load.read_only is True and load.used_defaults is False
    assert load.config.schema_version == 2
    assert load.config.general.language == "fr"
    codes = [i.code for i in load.issues]
    assert ErrorCode.CONFIG_NEWER_SCHEMA in codes
    newer = next(i for i in load.issues if i.code is ErrorCode.CONFIG_NEWER_SCHEMA)
    assert (newer.line, newer.column) == (1, 1)
    with pytest.raises(ConfigError) as exc:
        config.save(paths, load.config, expected_revision=rev)
    assert exc.value.code is ErrorCode.CONFIG_NEWER_SCHEMA
    assert paths.config_file.read_text() == text


# ---------------------------------------------------------------- invalid files


def test_invalid_toml_reports_line_and_column(paths: Paths) -> None:
    text = 'schema_version = 1\n\n[general]\nlanguage = "de"\ngpu = \n'
    rev = _write(paths, text)
    load = config.load(paths)
    assert load.used_defaults is True and load.exists is True
    assert load.config == defaults.default_config()
    assert load.revision == rev
    (issue,) = load.issues
    assert issue.code is ErrorCode.CONFIG_INVALID and issue.code.exit_code == 2
    assert (issue.line, issue.column) == (5, 7)
    assert issue.message.key  # the tomllib reason, e.g. "Invalid value"


def test_start_fresh_over_invalid_file_keeps_bak(paths: Paths) -> None:
    text = "this is = = not toml\n"
    rev = _write(paths, text)
    load = config.load(paths)
    config.save(paths, load.config, expected_revision=load.revision)
    assert config.backup_path(paths).read_text() == text
    assert config.load(paths).used_defaults is False
    assert rev != config.load(paths).revision


def test_invalid_utf8_reports_position(paths: Paths) -> None:
    _write(paths, b'schema_version = 1\nname = "caf\xe9"\n')
    load = config.load(paths)
    (issue,) = load.issues
    assert load.used_defaults and issue.code is ErrorCode.CONFIG_INVALID
    assert (issue.line, issue.column) == (2, 12)


@pytest.mark.parametrize("value", ['"one"', "true", "-1", "1.5"])
def test_bad_schema_version_uses_defaults(paths: Paths, value: str) -> None:
    _write(paths, f"schema_version = {value}\n")
    load = config.load(paths)
    assert load.used_defaults
    (issue,) = load.issues
    assert issue.field == "schema_version" and issue.line == 1


def test_unreadable_file_uses_defaults(paths: Paths) -> None:
    paths.config_file.mkdir(parents=True)  # a directory where the file should be
    load = config.load(paths)
    assert load.exists and load.used_defaults and load.revision == ""
    assert load.issues[0].code is ErrorCode.CONFIG_INVALID


WRONG_TYPES = """\
schema_version = 1
[general]
trt_experimental = "yes"
backend_override = "warp-drive"
[[profiles]]
id = "p"
sc_threshold = "high"
target = "3x"
buffered_frames = 2.5
hdr = "maybe"
[[profiles]]
name = "no id"
[[rules]]
profile = 7
match = { fps_max = "fast", hdr_class = "hdr11", interlaced = 1 }
"""


def test_wrong_types_report_field_and_position(paths: Paths) -> None:
    _write(paths, WRONG_TYPES)
    load = config.load(paths)
    assert not load.used_defaults
    got = {(i.field, i.code, i.line) for i in load.issues}
    expected_value = {
        ("general.trt_experimental", 3),
        ("general.backend_override", 4),
        ("profiles[4].sc_threshold", 7),
        ("profiles[4].target", 8),
        ("profiles[4].buffered_frames", 9),
        ("profiles[4].hdr", 10),
        ("profiles#2", 11),
        ("rules[0].profile", 14),
        ("rules[0].match.fps_max", 15),
        ("rules[0].match.hdr_class", 15),
        ("rules[0].match.interlaced", 15),
    }
    for f, line in expected_value:
        assert (f, ErrorCode.CONFIG_VALUE, line) in got, (f, got)
    p = next(p for p in load.config.profiles if p.id == "p")
    assert p.sc_threshold == defaults.DEFAULT_SC_THRESHOLD
    assert p.target == Target(TargetKind.DISPLAY) and p.hdr == "skip"
    assert load.config.general.trt_experimental is None  # a wrong type falls back to unset
    # the rule with a bad profile is kept (so positions stay aligned) and flagged by validate
    assert load.config.rules[0].profile == ""
    assert any(i.field == "rules[0].profile" for i in load.issues)


def test_semantic_issues_get_file_positions(paths: Paths) -> None:
    _write(
        paths, 'schema_version = 1\n[[profiles]]\nid = "x"\nbackend = "rife-ncnn"\nscale = 0.5\n'
    )
    load = config.load(paths)
    (issue,) = load.issues
    assert issue.code is ErrorCode.CONFIG_VALUE and issue.field == "profiles[4].scale"
    assert (issue.line, issue.column) == (5, 1)


def test_tmp_files_never_left(paths: Paths) -> None:
    rev = ""
    for i in range(3):
        rev = config.save(
            paths,
            dataclasses.replace(defaults.default_config(), general=GeneralSettings(gpu=str(i))),
            expected_revision=rev,
        )
    assert sorted(os.listdir(paths.config_file.parent)) == ["config.toml"]


# -- built-in fields cleared to "automatic" (review finding: model/None lost) --

_OPTIONAL_FIELDS = ("model", "scale", "buffered_frames", "concurrent_frames")


def _with_profile(cfg: Config, p: Profile) -> Config:
    return dataclasses.replace(cfg, profiles=tuple(p if q.id == p.id else q for q in cfg.profiles))


def _filled_builtins() -> tuple[Profile, ...]:
    """Shipped built-ins with every optional field set, so clearing each one matters."""
    return tuple(
        dataclasses.replace(
            p,
            backend=BackendId.RIFE_TRT,
            model="rife-v4.6_ensembleFalse",
            scale=0.5,
            buffered_frames=6,
            concurrent_frames=4,
        )
        for p in defaults.builtin_profiles()
    )


@pytest.mark.parametrize("pid", sorted(defaults.BUILTIN_IDS))
@pytest.mark.parametrize("field_name", _OPTIONAL_FIELDS)
@pytest.mark.parametrize("filled", [False, True], ids=["shipped", "filled"])
def test_builtin_optional_field_cleared_round_trips(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, pid: str, field_name: str, filled: bool
) -> None:
    if filled:  # a future shipped default for every optional field
        monkeypatch.setattr(defaults, "_BUILTINS", _filled_builtins())
    cfg = dataclasses.replace(
        defaults.default_config(), general=GeneralSettings(trt_experimental=True)
    )
    shipped = defaults.builtin_profile(pid)
    assert shipped is not None
    cleared = dataclasses.replace(shipped, **{field_name: None})  # type: ignore[arg-type]
    want = _with_profile(cfg, cleared)
    rev = config.save(paths, want, expected_revision="")
    loaded = config.load(paths)
    assert loaded.issues == ()
    assert loaded.config == want
    # Saving what was loaded is a no-op, not an error.
    assert config.save(paths, loaded.config, expected_revision=loaded.revision) == rev


def test_builtin_switched_to_mvtools_saves_and_reloads(paths: Paths) -> None:
    cfg = defaults.default_config()
    quality = defaults.builtin_profile("quality")
    assert quality is not None and quality.model is not None
    want = _with_profile(cfg, dataclasses.replace(quality, backend=BackendId.MVTOOLS, model=None))
    config.save(paths, want, expected_revision="")
    doc = tomllib.loads(paths.config_file.read_text())
    assert doc["profiles"][0]["model"] == ""  # explicit "automatic"
    loaded = config.load(paths)
    assert loaded.issues == ()
    assert loaded.config.profiles[0].backend is BackendId.MVTOOLS
    assert loaded.config.profiles[0].model is None
    # Back to the shipped profile: the override disappears from the file.
    config.save(paths, cfg, expected_revision=loaded.revision)
    assert "profiles" not in tomllib.loads(paths.config_file.read_text())


def test_unset_markers_read_as_automatic(paths: Paths) -> None:
    _write(
        paths,
        "schema_version = 1\n"
        '[[profiles]]\nid = "balanced"\nmodel = ""\n'
        '[[profiles]]\nid = "anime"\nmodel = ""\nscale = 0\n'
        "buffered_frames = 0\nconcurrent_frames = 0\n",
    )
    loaded = config.load(paths)
    assert loaded.issues == ()
    by_id = {p.id: p for p in loaded.config.profiles}
    assert by_id["balanced"].model is None
    anime = by_id["anime"]
    assert (anime.model, anime.scale, anime.buffered_frames, anime.concurrent_frames) == (
        None,
        None,
        None,
        None,
    )


def test_custom_profile_writes_no_unset_markers(paths: Paths) -> None:
    cfg = defaults.default_config()
    custom = _custom(model=None, buffered_frames=None, concurrent_frames=None)
    config.save(
        paths, dataclasses.replace(cfg, profiles=(*cfg.profiles, custom)), expected_revision=""
    )
    entry = tomllib.loads(paths.config_file.read_text())["profiles"][0]
    assert not {"model", "scale", "buffered_frames", "concurrent_frames"} & entry.keys()


def test_save_refuses_a_file_that_reads_back_differently(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from buttereye.core.errors import ButterEyeError

    real = schema.parse_bytes

    def lossy(data: bytes) -> schema.Parsed:
        parsed = real(data)
        return dataclasses.replace(
            parsed, config=dataclasses.replace(parsed.config, profiles=parsed.config.profiles[1:])
        )

    monkeypatch.setattr(schema, "parse_bytes", lossy)
    cfg = defaults.default_config()
    with pytest.raises(ButterEyeError) as err:
        config.save(paths, cfg, expected_revision="")
    assert err.value.code is ErrorCode.INTERNAL
    assert not paths.config_file.exists()


def test_save_keeps_dropping_unknowns_of_deleted_rules(paths: Paths) -> None:
    cfg = dataclasses.replace(
        defaults.default_config(),
        rules=defaults.default_rules()[:1],
        unknown=MappingProxyType({"rules[1].note": "x", "profiles[gone].k": 1}),
    )
    config.save(paths, cfg, expected_revision="")  # the guard does not misfire
    assert config.load(paths).config.unknown == {}
