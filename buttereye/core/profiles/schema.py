# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``config.toml`` text <-> ``Config`` (SCOPE §4.9, F15).

Reading uses the stdlib ``tomllib``. Writing uses the small TOML writer below (no
third-party dependency); every ``Config`` it emits parses back to an equal
``Config``. Comments are not preserved (F15).

Schema v1 layout (keys are the ``types.py`` field names)::

    schema_version = 1
    rules = []                      # only when there are no rules (absent = shipped rules)

    [general]                       # backend_override, gpu, language, trt_experimental,
                                    # upscaling, deband, full_size
    [render]                        # container = "mkv", audio = "copy"
    [render.encoder_by_vendor]      # vendor = "encoder"
    [[profiles]]                    # id, name, backend, model, scale, target,
                                    # sc_threshold, buffered_frames, concurrent_frames, hdr
    [[rules]]                       # profile = "<id>", match = { fps_max = 30, ... }

- ``target`` is ``"display"``, ``"display-max"``, ``"2x"`` or ``"fps:<n>/<d>"`` (``"fps:60"`` and
  ``"fps:59.94"`` are read too). Rule fps bounds are an integer, a decimal, or a
  ``"<n>/<d>"`` string (24000/1001 stays exact).
- Built-in profiles always exist. A ``[[profiles]]`` entry whose ``id`` is a
  built-in id overrides that built-in in place; on save a built-in is written only
  when it differs from the shipped one (or carries unknown keys), so "Reset to
  default" removes the entry and shipped updates keep reaching the user.
- ``None`` values are omitted (TOML has no null), as are ``trt_experimental = false``
  ``upscaling = "standard"``, ``deband = false`` and ``full_size = false``.
  Exception: a built-in override whose optional engine field is ``None`` while
  the shipped value is not writes an explicit "automatic" marker, because an
  absent key on a built-in means "the shipped value": ``model = ""``,
  ``scale = 0.0``, ``buffered_frames = 0``, ``concurrent_frames = 0``. The reader
  maps these markers to ``None`` for every profile (none of them is a valid
  value otherwise).
- Unknown keys are reported (``BE-2005``, warning) and kept verbatim in
  ``Config.unknown`` under a flat path: ``key`` (top level), ``general.key``,
  ``render.key``, ``profiles[<id>].key``, ``rules[<i>].key``,
  ``rules[<i>].match.key``. Path segments that are not bare TOML keys are JSON
  quoted. Profile unknowns follow the profile id; rule unknowns follow the rule
  position (a reordered rule list keeps them at the old position; past the end
  they are dropped).

Schema v0 is the pre-release layout of the M0 spikes: no ``schema_version`` key
(or ``schema_version = 0``), ``[general] backend`` instead of
``backend_override`` (``"auto"`` meant none) and an integer profile ``target``
multiplier (``2`` = ``"2x"``). It is migrated in memory; ``config.save`` writes a
``.bak`` of the old file before replacing it.
"""

from __future__ import annotations

import json
import math
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import date, datetime, time
from fractions import Fraction
from types import MappingProxyType
from typing import Literal

from buttereye.core.errors import ErrorCode
from buttereye.core.profiles import defaults
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigIssue,
    GeneralSettings,
    HdrClass,
    Msg,
    Profile,
    RenderDefaults,
    Rule,
    RuleMatch,
    Target,
    TargetKind,
)


def _msg(key: str, params: Mapping[str, str | int | float] | None = None) -> Msg:
    """A ``Msg`` with immutable params (it crosses the GUI bridge)."""
    return Msg(key, MappingProxyType(dict(params or {})))


SCHEMA_VERSION = defaults.SCHEMA_VERSION

ROOT_KEYS = ("schema_version", "general", "render", "profiles", "rules")
GENERAL_KEYS = tuple(f.name for f in fields(GeneralSettings))
RENDER_KEYS = tuple(f.name for f in fields(RenderDefaults))
PROFILE_KEYS = tuple(f.name for f in fields(Profile) if f.name != "builtin")
RULE_KEYS = ("profile", "match")
MATCH_KEYS = tuple(f.name for f in fields(RuleMatch))

Section = tuple[str | int, ...]  # locator address: (), ("general",), ("profiles", 2), ...

_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_FRACTION = re.compile(r"\s*(\d+)\s*/\s*(\d+)\s*")


# ---------------------------------------------------------------------------
# Small helpers shared with rules.py
# ---------------------------------------------------------------------------


def format_fraction(value: Fraction) -> str:
    """``30`` for whole numbers, else ``24000/1001``."""
    if value.denominator == 1:
        return str(value.numerator)
    return f"{value.numerator}/{value.denominator}"


def parse_fraction(value: object) -> Fraction | None:
    """An int, a finite float/decimal string or an ``n/d`` string; ``None`` if not."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Fraction(repr(value))
    if isinstance(value, str):
        m = _FRACTION.fullmatch(value)
        if m:
            den = int(m.group(2))
            return None if den == 0 else Fraction(int(m.group(1)), den)
        try:
            frac = Fraction(value.strip())
        except ValueError, ZeroDivisionError:
            return None
        return frac
    return None


def target_text(target: Target) -> str:
    if target.kind is TargetKind.FPS:
        fps = target.fps if target.fps is not None else Fraction(0)
        return "fps:" + format_fraction(fps)
    return target.kind.value


def parse_target(value: object) -> Target | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text == TargetKind.DISPLAY.value:
        return Target(TargetKind.DISPLAY)
    if text == TargetKind.DISPLAY_MAX.value:
        return Target(TargetKind.DISPLAY_MAX)
    if text == TargetKind.X2.value:
        return Target(TargetKind.X2)
    if text.startswith("fps:"):
        fps = parse_fraction(text[4:])
        if fps is None:
            return None
        return Target(TargetKind.FPS, fps)
    return None


def key_segment(key: str) -> str:
    """A TOML key segment: bare when possible, else a quoted basic string."""
    return key if _BARE_KEY.fullmatch(key) else _quote(key)


def unknown_path(section: Section, key: str, profile_id: str | None = None) -> str:
    """Flat ``Config.unknown`` path for an unknown key (see module doc)."""
    seg = key_segment(key)
    if not section:
        return seg
    head = section[0]
    if head == "profiles":
        return f"profiles[{key_segment(profile_id or '')}].{seg}"
    if head == "rules":
        tail = ".match" if len(section) > 2 else ""
        return f"rules[{section[1]}]{tail}.{seg}"
    return f"{head}.{seg}"


_UNKNOWN_PATH = re.compile(
    r"""(?:
        (?P<table>general|render)\.
      | profiles\[(?P<pid>[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*")\]\.
      | rules\[(?P<rule>\d+)\](?P<match>\.match)?\.
    )?
    (?P<key>[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*")""",
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class UnknownPath:
    where: Literal["root", "general", "render", "profile", "rule", "match"]
    key: str
    profile_id: str | None = None
    rule_index: int | None = None


def split_unknown_path(path: str) -> UnknownPath:
    """Inverse of ``unknown_path``. A path outside the grammar is a literal root key."""
    m = _UNKNOWN_PATH.fullmatch(path)
    if m is None:
        return UnknownPath("root", path)
    try:
        key = _unquote(m.group("key"))
        if m.group("table"):
            return UnknownPath("general" if m.group("table") == "general" else "render", key)
        if m.group("pid") is not None:
            return UnknownPath("profile", key, profile_id=_unquote(m.group("pid")))
        if m.group("rule") is not None:
            where: Literal["rule", "match"] = "match" if m.group("match") else "rule"
            return UnknownPath(where, key, rule_index=int(m.group("rule")))
    except ValueError:
        return UnknownPath("root", path)
    return UnknownPath("root", key)


def _unquote(seg: str) -> str:
    if seg.startswith('"'):
        value = json.loads(seg)
        if not isinstance(value, str):
            raise ValueError(seg)
        return value
    return seg


def freeze(value: object) -> object:
    """Immutable copy of a parsed TOML value (dict -> MappingProxyType, list -> tuple)."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(k): freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(v) for v in value)
    return value


# ---------------------------------------------------------------------------
# TOML writer (TOML 1.0)
# ---------------------------------------------------------------------------


class Inline(dict[str, object]):
    """A table the writer emits as an inline table (``match = { ... }``)."""


class TomlWriteError(ValueError):
    def __init__(self, path: str, value: object) -> None:
        super().__init__(f"cannot write {type(value).__name__} at {path or '<root>'}")
        self.path = path
        self.value = value


def _quote(text: str) -> str:
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\u{code:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _is_table(value: object) -> bool:
    return isinstance(value, Mapping) and not isinstance(value, Inline)


def _is_table_array(value: object) -> bool:
    return isinstance(value, (list, tuple)) and len(value) > 0 and all(_is_table(v) for v in value)


def _value(value: object, path: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "nan"
        if math.isinf(value):
            return "inf" if value > 0 else "-inf"
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (datetime, date, time)):  # datetime is a date subclass
        return value.isoformat()
    if isinstance(value, Mapping):
        items = ", ".join(
            f"{key_segment(str(k))} = {_value(v, f'{path}.{k}')}" for k, v in value.items()
        )
        return "{ " + items + " }" if items else "{}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v, f"{path}[{i}]") for i, v in enumerate(value)) + "]"
    raise TomlWriteError(path, value)


def _emit(lines: list[str], prefix: tuple[str, ...], table: Mapping[str, object]) -> None:
    sub: list[tuple[str, object]] = []
    for k, v in table.items():
        key = str(k)
        if _is_table(v) or _is_table_array(v):
            sub.append((key, v))
        else:
            lines.append(f"{key_segment(key)} = {_value(v, '.'.join((*prefix, key)))}")
    for key, v in sub:
        path = (*prefix, key)
        header = ".".join(key_segment(p) for p in path)
        if isinstance(v, Mapping):
            lines.append("")
            lines.append(f"[{header}]")
            _emit(lines, path, v)
        else:
            assert isinstance(v, (list, tuple))
            for element in v:
                assert isinstance(element, Mapping)
                lines.append("")
                lines.append(f"[[{header}]]")
                _emit(lines, path, element)


def dumps(doc: Mapping[str, object], *, header: Sequence[str] = ()) -> str:
    """Serialise a document of TOML-representable values. Raises ``TomlWriteError``."""
    lines = [f"# {h}" if h else "#" for h in header]
    if lines:
        lines.append("")
    _emit(lines, (), doc)
    while lines and lines[0] == "":
        lines.pop(0)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Locator: best-effort line/column of keys in the original text
# ---------------------------------------------------------------------------

_AOT_RE = re.compile(r"\s*\[\[\s*(?P<name>[^\]]+?)\s*\]\]")
_TABLE_RE = re.compile(r"\s*\[\s*(?P<name>[^\]]+?)\s*\]")
_KEY_PART = r"""(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')"""
_KEY_RE = re.compile(rf"\s*(?P<key>{_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*=\s*(?P<rest>.*)")
_INLINE_KEY_RE = re.compile(rf"[{{,]\s*(?P<key>{_KEY_PART})\s*=")


def _split_key(text: str) -> list[str]:
    parts = re.findall(_KEY_PART, text)
    out: list[str] = []
    for p in parts:
        if p.startswith('"'):
            try:
                out.append(_unquote(p))
            except ValueError:
                out.append(p)
        elif p.startswith("'"):
            out.append(p[1:-1])
        else:
            out.append(p)
    return out


class Locator:
    """Maps (section, key) to the 1-based (line, column) where the key is written."""

    def __init__(self, text: str) -> None:
        self._at: dict[tuple[Section, str], tuple[int, int]] = {}
        counts: dict[str, int] = {}
        section: Section = ()
        multiline: str | None = None
        for lineno, line in enumerate(text.splitlines(), 1):
            if multiline is not None:
                if multiline in line:
                    multiline = None
                continue
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            m = _AOT_RE.match(line)
            if m:
                parts = _split_key(m.group("name"))
                name = parts[0] if parts else ""
                if len(parts) == 1:
                    counts[name] = counts.get(name, -1) + 1
                section = self._section(parts, counts)
                self._at.setdefault((section, ""), (lineno, m.start("name") + 1))
                continue
            m = _TABLE_RE.match(line)
            if m and not _KEY_RE.match(line):
                section = self._section(_split_key(m.group("name")), counts)
                self._at.setdefault((section, ""), (lineno, m.start("name") + 1))
                continue
            m = _KEY_RE.match(line)
            if not m:
                continue
            parts = _split_key(m.group("key"))
            if not parts:
                continue
            where: Section = (*section, *parts[:-1])
            self._at.setdefault((where, parts[-1]), (lineno, m.start("key") + 1))
            rest = m.group("rest")
            for quote in ('"""', "'''"):
                if rest.startswith(quote) and rest.count(quote) == 1:
                    multiline = quote
            if rest.startswith("{"):
                inner: Section = (*where, parts[-1])
                offset = m.start("rest")
                for im in _INLINE_KEY_RE.finditer(rest):
                    for ik in _split_key(im.group("key"))[-1:]:
                        self._at.setdefault((inner, ik), (lineno, offset + im.start("key") + 1))

    @staticmethod
    def _section(parts: list[str], counts: Mapping[str, int]) -> Section:
        if parts and parts[0] in counts:
            return (parts[0], counts[parts[0]], *parts[1:])
        return tuple(parts)

    def find(self, section: Section, key: str | None = None) -> tuple[int, int] | None:
        """``key=None`` finds the ``[table]``/``[[table]]`` header of ``section``.

        A key written as its own table (``[extra]`` / ``[[extra]]``) is found at
        its header.
        """
        found = self._at.get((section, key or ""))
        if found is None and key:
            found = self._at.get(((*section, key), "")) or self._at.get(((*section, key, 0), ""))
        return found


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Parsed:
    """Result of reading ``config.toml`` text (internal to the profiles package)."""

    config: Config
    issues: list[ConfigIssue]
    read_only: bool = False
    used_defaults: bool = False
    file_version: int | None = None  # None: not readable
    migrated: bool = False
    locator: Locator | None = None
    #: Config.profiles index -> [[profiles]] position in the file
    profile_file_index: dict[int, int] = field(default_factory=dict)

    @property
    def needs_backup(self) -> bool:
        """Saving over this file must keep a ``.bak`` (migration or unreadable)."""
        return self.migrated or self.used_defaults

    def locate_field(self, path: str | None) -> tuple[int, int] | None:
        """Line/column for a ``ConfigIssue.field`` path, when it is written in the file."""
        if self.locator is None or not path:
            return None
        m = re.fullmatch(r"profiles\[(\d+)\]\.(\w+)", path)
        if m:
            file_idx = self.profile_file_index.get(int(m.group(1)))
            if file_idx is None:
                return None
            return self.locator.find(("profiles", file_idx), m.group(2))
        m = re.fullmatch(r"rules\[(\d+)\]\.match\.(\w+)", path)
        if m:
            return self.locator.find(("rules", int(m.group(1)), "match"), m.group(2))
        m = re.fullmatch(r"rules\[(\d+)\](?:\.(\w+))?", path)
        if m:
            sec: Section = ("rules", int(m.group(1)))
            return self.locator.find(sec, m.group(2)) if m.group(2) else self.locator.find(sec)
        parts = path.split(".")
        if len(parts) >= 2:
            return self.locator.find(tuple(parts[:-1]), parts[-1])
        return self.locator.find((), path)


def _defaults_result(issue: ConfigIssue) -> Parsed:
    return Parsed(defaults.default_config(), [issue], used_defaults=True)


def parse_bytes(data: bytes) -> Parsed:
    """Bytes of ``config.toml`` -> ``Parsed``. Never raises for bad input."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        line = data.count(b"\n", 0, exc.start) + 1
        column = exc.start - (data.rfind(b"\n", 0, exc.start) + 1) + 1
        return _defaults_result(
            ConfigIssue(
                ErrorCode.CONFIG_INVALID,
                _msg("config.toml is not valid UTF-8 text."),
                line=line,
                column=column,
            )
        )
    return parse_text(text)


def parse_text(text: str) -> Parsed:
    """TOML text -> ``Parsed``. Never raises for bad input."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        cause = exc.msg if isinstance(getattr(exc, "msg", None), str) else str(exc)
        return _defaults_result(
            ConfigIssue(
                ErrorCode.CONFIG_INVALID,
                _msg("{cause}", {"cause": cause}),
                line=getattr(exc, "lineno", None),
                column=getattr(exc, "colno", None),
            )
        )
    locator = Locator(text)
    raw_version = doc.get("schema_version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 0:
        line_col = locator.find((), "schema_version")
        return Parsed(
            defaults.default_config(),
            [
                ConfigIssue(
                    ErrorCode.CONFIG_INVALID,
                    _msg("schema_version must be a whole number, such as 1."),
                    line=line_col[0] if line_col else None,
                    column=line_col[1] if line_col else None,
                    field="schema_version",
                )
            ],
            used_defaults=True,
            locator=locator,
        )
    reader = _Reader(locator)
    migrated = False
    if raw_version == 0:
        doc = _migrate_v0(doc)
        migrated = True
    if raw_version > SCHEMA_VERSION:
        line_col = locator.find((), "schema_version")
        reader.issues.append(
            ConfigIssue(
                ErrorCode.CONFIG_NEWER_SCHEMA,
                _msg(
                    "config.toml uses schema version {found}; this ButterEye understands "
                    "version {supported}. It is shown read-only.",
                    {"found": raw_version, "supported": SCHEMA_VERSION},
                ),
                line=line_col[0] if line_col else None,
                column=line_col[1] if line_col else None,
                field="schema_version",
            )
        )
    cfg = reader.config(doc, raw_version)
    return Parsed(
        cfg,
        reader.issues,
        read_only=raw_version > SCHEMA_VERSION,
        file_version=raw_version,
        migrated=migrated,
        locator=locator,
        profile_file_index=reader.profile_file_index,
    )


def _migrate_v0(doc: dict[str, object]) -> dict[str, object]:
    out = dict(doc)
    out.pop("schema_version", None)
    general = out.get("general")
    if isinstance(general, dict) and "backend" in general and "backend_override" not in general:
        g = dict(general)
        backend = g.pop("backend")
        if backend != "auto":
            g["backend_override"] = backend
        out["general"] = g
    profiles = out.get("profiles")
    if isinstance(profiles, list):
        new_profiles: list[object] = []
        for p in profiles:
            target = p.get("target") if isinstance(p, dict) else None
            if isinstance(p, dict) and isinstance(target, int) and target == 2:
                p = {**p, "target": TargetKind.X2.value}
            new_profiles.append(p)
        out["profiles"] = new_profiles
    return out


class _Reader:
    def __init__(self, locator: Locator) -> None:
        self.locator = locator
        self.issues: list[ConfigIssue] = []
        self.unknown: dict[str, object] = {}
        self.profile_file_index: dict[int, int] = {}

    # -- issue helpers --
    def _issue(
        self, code: ErrorCode, msg: Msg, section: Section, key: str | None, path: str | None
    ) -> None:
        at = self.locator.find(section, key)
        self.issues.append(
            ConfigIssue(
                code, msg, line=at[0] if at else None, column=at[1] if at else None, field=path
            )
        )

    def bad(self, section: Section, key: str, path: str, expected: str, value: object) -> None:
        self._issue(
            ErrorCode.CONFIG_VALUE,
            _msg(
                "{field} must be {expected} (found {found}); the default is used.",
                {"field": path, "expected": expected, "found": _describe(value)},
            ),
            section,
            key,
            path,
        )

    def keep_unknown(
        self,
        section: Section,
        table: Mapping[str, object],
        known: Iterable[str],
        profile_id: str | None = None,
    ) -> None:
        known_set = set(known)
        for key, value in table.items():
            if key in known_set:
                continue
            path = unknown_path(section, key, profile_id)
            self.unknown[path] = freeze(value)
            self._issue(
                ErrorCode.CONFIG_UNKNOWN_KEY,
                _msg("Unknown setting {key}; it is kept as written.", {"key": path}),
                section,
                key,
                path,
            )

    def table(self, value: object, section: Section, path: str) -> Mapping[str, object] | None:
        """``value`` as a table; ``section`` is the table's own address."""
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        self.bad(section[:-1], str(section[-1]), path, "a table", value)
        return None

    # -- typed getters (missing -> default, silently; wrong type -> issue + default) --
    def get[T](
        self,
        table: Mapping[str, object],
        key: str,
        section: Section,
        path: str,
        conv: Callable[[object], T | None],
        expected: str,
        default: T,
    ) -> T:
        if key not in table:
            return default
        raw = table[key]
        value = conv(raw)
        if value is None:
            self.bad(section, key, path, expected, raw)
            return default
        return value

    # -- sections --
    def config(self, doc: Mapping[str, object], version: int) -> Config:
        self.keep_unknown((), doc, ROOT_KEYS)
        general = self.general(self.table(doc.get("general"), ("general",), "general"))
        render = self.render(self.table(doc.get("render"), ("render",), "render"))
        profiles = self.profiles(doc.get("profiles"))
        rules = self.rules(doc["rules"]) if "rules" in doc else defaults.default_rules()
        return Config(
            schema_version=max(version, SCHEMA_VERSION),
            general=general,
            profiles=profiles,
            rules=rules,
            render=render,
            unknown=MappingProxyType(dict(self.unknown)),
        )

    def general(self, t: Mapping[str, object] | None) -> GeneralSettings:
        if t is None:
            return GeneralSettings()
        sec: Section = ("general",)
        self.keep_unknown(sec, t, GENERAL_KEYS)
        override: BackendId | None = None
        if "backend_override" in t and t["backend_override"] != "auto":
            override = self.get(
                t,
                "backend_override",
                sec,
                "general.backend_override",
                _backend_id,
                _BACKEND_CHOICES,
                None,
            )
        return GeneralSettings(
            backend_override=override,
            gpu=self.get(t, "gpu", sec, "general.gpu", _str, "text", None),
            language=self.get(t, "language", sec, "general.language", _str, "text", None),
            trt_experimental=self.get(
                t,
                "trt_experimental",
                sec,
                "general.trt_experimental",
                _bool,
                "true or false",
                None,
            ),
            upscaling=self.get(
                t,
                "upscaling",
                sec,
                "general.upscaling",
                _upscaling,
                '"standard" or "sharper"',
                "standard",
            ),
            deband=self.get(t, "deband", sec, "general.deband", _bool, "true or false", False),
            full_size=self.get(
                t, "full_size", sec, "general.full_size", _bool, "true or false", False
            ),
        )

    def render(self, t: Mapping[str, object] | None) -> RenderDefaults:
        if t is None:
            return RenderDefaults()
        sec: Section = ("render",)
        self.keep_unknown(sec, t, RENDER_KEYS)
        encoders: dict[str, str] = {}
        enc = self.table(
            t.get("encoder_by_vendor"), (*sec, "encoder_by_vendor"), "render.encoder_by_vendor"
        )
        if enc is not None:
            for vendor, value in enc.items():
                if isinstance(value, str):
                    encoders[vendor] = value
                else:
                    self.bad(
                        (*sec, "encoder_by_vendor"),
                        vendor,
                        f"render.encoder_by_vendor.{vendor}",
                        "text",
                        value,
                    )
        # v1 knows one container and one audio mode; anything else is reported.
        self.get(t, "container", sec, "render.container", _literal("mkv"), '"mkv"', "mkv")
        self.get(t, "audio", sec, "render.audio", _literal("copy"), '"copy"', "copy")
        return RenderDefaults(encoder_by_vendor=MappingProxyType(encoders))

    def profiles(self, raw: object) -> tuple[Profile, ...]:
        builtins = list(defaults.builtin_profiles())
        if raw is None:
            return tuple(builtins)
        if not isinstance(raw, list):
            self.bad((), "profiles", "profiles", "a list of [[profiles]] tables", raw)
            return tuple(builtins)
        builtin_pos = {p.id: i for i, p in enumerate(builtins)}
        result: list[Profile] = list(builtins)
        seen: set[str] = set()
        for file_idx, entry in enumerate(raw):
            sec: Section = ("profiles", file_idx)
            if not isinstance(entry, dict):
                self.bad((), "profiles", f"profiles#{file_idx + 1}", "a table", entry)
                continue
            pid = entry.get("id")
            if not isinstance(pid, str) or not pid:
                self._issue(
                    ErrorCode.CONFIG_VALUE,
                    _msg("Profile number {n} has no id; it is ignored.", {"n": file_idx + 1}),
                    sec,
                    "id" if "id" in entry else None,
                    f"profiles#{file_idx + 1}",
                )
                continue
            if pid in seen:
                self._issue(
                    ErrorCode.CONFIG_VALUE,
                    _msg("Profile id {id} is used twice; the second one is ignored.", {"id": pid}),
                    sec,
                    "id",
                    f"profiles#{file_idx + 1}",
                )
                continue
            seen.add(pid)
            cfg_idx = builtin_pos.get(pid, len(result))
            self.profile_file_index[cfg_idx] = file_idx
            profile = self.profile(entry, sec, pid, cfg_idx)
            if pid in builtin_pos:
                result[cfg_idx] = profile
            else:
                result.append(profile)
        return tuple(result)

    def profile(self, t: Mapping[str, object], sec: Section, pid: str, idx: int) -> Profile:
        self.keep_unknown(sec, t, PROFILE_KEYS, profile_id=pid)
        base = defaults.builtin_profile(pid)
        p = f"profiles[{idx}]"

        def g[T](key: str, conv: Callable[[object], T | None], expected: str, default: T) -> T:
            return self.get(t, key, sec, f"{p}.{key}", conv, expected, default)

        def opt[T](
            key: str, conv: Callable[[object], T | None], expected: str, default: T | None
        ) -> T | None:
            if key in t and _is_unset_marker(key, t[key]):
                return None  # explicit "automatic" (see the module doc)
            return g(key, conv, expected, default)

        if base is not None:  # a built-in override: unset keys keep the shipped value
            return Profile(
                id=pid,
                name=g("name", _str, "text", base.name),
                backend=g("backend", _backend, _BACKEND_OR_AUTO, base.backend),
                model=opt("model", _str, "text", base.model),
                scale=opt("scale", _float, "a number", base.scale),
                target=g("target", parse_target, _TARGET_CHOICES, base.target),
                sc_threshold=g("sc_threshold", _float, "a number", base.sc_threshold),
                buffered_frames=opt(
                    "buffered_frames", _int, "a whole number", base.buffered_frames
                ),
                concurrent_frames=opt(
                    "concurrent_frames", _int, "a whole number", base.concurrent_frames
                ),
                hdr=g("hdr", _hdr_mode, '"skip" or "passthrough"', base.hdr),
                builtin=True,
            )
        return Profile(
            id=pid,
            name=g("name", _str, "text", pid),
            backend=g("backend", _backend, _BACKEND_OR_AUTO, "auto"),
            model=opt("model", _str, "text", None),
            scale=opt("scale", _float, "a number", None),
            target=g("target", parse_target, _TARGET_CHOICES, Target(TargetKind.DISPLAY)),
            sc_threshold=g("sc_threshold", _float, "a number", defaults.DEFAULT_SC_THRESHOLD),
            buffered_frames=opt("buffered_frames", _int, "a whole number", None),
            concurrent_frames=opt("concurrent_frames", _int, "a whole number", None),
            hdr=g("hdr", _hdr_mode, '"skip" or "passthrough"', "skip"),
            builtin=False,
        )

    def rules(self, raw: object) -> tuple[Rule, ...]:
        if not isinstance(raw, list):
            self.bad((), "rules", "rules", "a list of [[rules]] tables", raw)
            return defaults.default_rules()
        out: list[Rule] = []
        for i, entry in enumerate(raw):
            sec: Section = ("rules", i)
            if not isinstance(entry, dict):
                self.bad((), "rules", f"rules[{i}]", "a table", entry)
                entry = {}
            self.keep_unknown(sec, entry, RULE_KEYS)
            profile = entry.get("profile")
            if "profile" not in entry:
                self._issue(
                    ErrorCode.CONFIG_VALUE,
                    _msg("Rule {n} names no profile.", {"n": i + 1}),
                    sec,
                    None,
                    f"rules[{i}].profile",
                )
                profile = ""
            elif not isinstance(profile, str):
                self.bad(sec, "profile", f"rules[{i}].profile", "a profile id", profile)
                profile = ""
            match = RuleMatch()
            mt = self.table(entry.get("match"), (*sec, "match"), f"rules[{i}].match")
            if mt is not None:
                match = self.match(mt, (*sec, "match"), f"rules[{i}].match")
            out.append(Rule(match=match, profile=profile))
        return tuple(out)

    def match(self, t: Mapping[str, object], sec: Section, p: str) -> RuleMatch:
        self.keep_unknown(sec, t, MATCH_KEYS)

        def g[T](key: str, conv: Callable[[object], T | None], expected: str) -> T | None:
            return self.get(t, key, sec, f"{p}.{key}", conv, expected, None)

        return RuleMatch(
            fps_min=g("fps_min", parse_fraction, _FPS_EXPECTED),
            fps_max=g("fps_max", parse_fraction, _FPS_EXPECTED),
            width_max=g("width_max", _int, "a whole number"),
            height_max=g("height_max", _int, "a whole number"),
            hdr_class=g("hdr_class", _hdr_class, _HDR_CHOICES),
            display_hz_min=g("display_hz_min", _float, "a number"),
            interlaced=g("interlaced", _bool, "true or false"),
            path_glob=g("path_glob", _str, "text"),
        )


_BACKEND_CHOICES = ", ".join(f'"{b.value}"' for b in BackendId)
_BACKEND_OR_AUTO = '"auto", ' + _BACKEND_CHOICES
_TARGET_CHOICES = '"display", "display-max", "2x" or "fps:<n>/<d>"'
_HDR_CHOICES = ", ".join(f'"{h.value}"' for h in HdrClass)
_FPS_EXPECTED = 'a frame rate such as 30, 59.94 or "24000/1001"'


def _describe(value: object) -> str:
    if value is None:
        return "nothing"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, Mapping):
        return "a table"
    if isinstance(value, (list, tuple)):
        return "a list"
    return type(value).__name__


#: Explicit "automatic" markers for optional profile fields (see the module doc).
UNSET_MARKERS: Mapping[str, str | int | float] = MappingProxyType(
    {"model": "", "scale": 0.0, "buffered_frames": 0, "concurrent_frames": 0}
)


def _is_unset_marker(key: str, v: object) -> bool:
    if key not in UNSET_MARKERS or isinstance(v, bool):
        return False
    if key == "model":
        return v == ""
    return isinstance(v, (int, float)) and v == 0


def _str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _bool(v: object) -> bool | None:
    return v if isinstance(v, bool) else None


def _upscaling(v: object) -> Literal["standard", "sharper"] | None:
    if v == "standard":
        return "standard"
    if v == "sharper":
        return "sharper"
    return None


def _int(v: object) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _float(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


def _backend_id(v: object) -> BackendId | None:
    try:
        return BackendId(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _backend(v: object) -> BackendId | Literal["auto"] | None:
    if v == "auto":
        return "auto"
    return _backend_id(v)


def _hdr_mode(v: object) -> Literal["skip", "passthrough"] | None:
    if v == "skip":
        return "skip"
    if v == "passthrough":
        return "passthrough"
    return None


def _hdr_class(v: object) -> HdrClass | None:
    try:
        return HdrClass(v) if isinstance(v, str) else None
    except ValueError:
        return None


def _literal(expected: str) -> Callable[[object], str | None]:
    def conv(v: object) -> str | None:
        return expected if v == expected else None

    return conv


# ---------------------------------------------------------------------------
# Config -> document
# ---------------------------------------------------------------------------


def _profile_doc(p: Profile) -> dict[str, object]:
    # The reader keys built-in overrides on the id (not ``p.builtin``), so do we.
    shipped = defaults.builtin_profile(p.id)

    def optional(key: str, value: object, shipped_value: object) -> None:
        if value is not None:
            d[key] = value
        elif shipped_value is not None:
            # An absent key on a built-in reads back as the shipped value.
            d[key] = UNSET_MARKERS[key]

    d: dict[str, object] = {"id": p.id, "name": p.name, "backend": str(p.backend)}
    optional("model", p.model, shipped.model if shipped else None)
    optional(
        "scale", None if p.scale is None else float(p.scale), shipped.scale if shipped else None
    )
    d["target"] = target_text(p.target)
    d["sc_threshold"] = float(p.sc_threshold)
    optional("buffered_frames", p.buffered_frames, shipped.buffered_frames if shipped else None)
    optional(
        "concurrent_frames", p.concurrent_frames, shipped.concurrent_frames if shipped else None
    )
    d["hdr"] = p.hdr
    return d


def _match_doc(m: RuleMatch) -> Inline:
    d = Inline()
    if m.fps_min is not None:
        d["fps_min"] = _fraction_value(m.fps_min)
    if m.fps_max is not None:
        d["fps_max"] = _fraction_value(m.fps_max)
    if m.width_max is not None:
        d["width_max"] = m.width_max
    if m.height_max is not None:
        d["height_max"] = m.height_max
    if m.hdr_class is not None:
        d["hdr_class"] = str(m.hdr_class)
    if m.display_hz_min is not None:
        d["display_hz_min"] = float(m.display_hz_min)
    if m.interlaced is not None:
        d["interlaced"] = m.interlaced
    if m.path_glob is not None:
        d["path_glob"] = m.path_glob
    return d


def _fraction_value(f: Fraction) -> int | str:
    return f.numerator if f.denominator == 1 else format_fraction(f)


def to_document(cfg: Config) -> dict[str, object]:
    """``Config`` -> plain document for ``dumps`` (unknown keys merged back in)."""
    root_unknown: dict[str, object] = {}
    general_unknown: dict[str, object] = {}
    render_unknown: dict[str, object] = {}
    profile_unknown: dict[str, dict[str, object]] = {}
    rule_unknown: dict[int, dict[str, object]] = {}
    match_unknown: dict[int, dict[str, object]] = {}
    for path, value in cfg.unknown.items():
        u = split_unknown_path(path)
        if u.where == "root":
            root_unknown[u.key] = value
        elif u.where == "general":
            general_unknown[u.key] = value
        elif u.where == "render":
            render_unknown[u.key] = value
        elif u.where == "profile" and u.profile_id is not None:
            profile_unknown.setdefault(u.profile_id, {})[u.key] = value
        elif u.where == "rule" and u.rule_index is not None:
            rule_unknown.setdefault(u.rule_index, {})[u.key] = value
        elif u.where == "match" and u.rule_index is not None:
            match_unknown.setdefault(u.rule_index, {})[u.key] = value

    doc: dict[str, object] = {"schema_version": SCHEMA_VERSION}

    g = cfg.general
    general: dict[str, object] = {}
    if g.backend_override is not None:
        general["backend_override"] = str(g.backend_override)
    if g.gpu is not None:
        general["gpu"] = g.gpu
    if g.language is not None:
        general["language"] = g.language
    if g.trt_experimental is not None:  # unset = use TensorRT when it is set up
        general["trt_experimental"] = g.trt_experimental
    if g.upscaling != "standard":
        general["upscaling"] = g.upscaling
    if g.deband:
        general["deband"] = True
    if g.full_size:
        general["full_size"] = True
    _merge(general, general_unknown)
    if general:
        doc["general"] = general

    render: dict[str, object] = {"container": cfg.render.container, "audio": cfg.render.audio}
    if cfg.render.encoder_by_vendor:
        render["encoder_by_vendor"] = dict(cfg.render.encoder_by_vendor)
    _merge(render, render_unknown)
    doc["render"] = render

    profiles: list[dict[str, object]] = []
    for p in cfg.profiles:
        extra = profile_unknown.get(p.id, {})
        shipped = defaults.builtin_profile(p.id)
        if shipped is not None and p == shipped and not extra:
            continue  # unchanged built-in: not written
        d = _profile_doc(p)
        _merge(d, extra)
        profiles.append(d)
    if profiles:
        doc["profiles"] = profiles

    rules: list[dict[str, object]] = []
    for i, r in enumerate(cfg.rules):
        rd: dict[str, object] = {"profile": r.profile}
        match = _match_doc(r.match)
        _merge(match, match_unknown.get(i, {}))
        rd["match"] = match
        _merge(rd, rule_unknown.get(i, {}))
        rules.append(rd)
    doc["rules"] = rules  # [] is written: an absent key means "shipped rules"

    _merge(doc, root_unknown)
    return doc


def _merge(target: dict[str, object], extra: Mapping[str, object]) -> None:
    for k, v in extra.items():
        target.setdefault(k, v)  # a known key always wins over a stale unknown


FILE_HEADER = (
    "ButterEye settings, profiles and rules.",
    "Written by ButterEye; comments in this file are not kept when it saves.",
)


def dumps_config(cfg: Config) -> str:
    """``Config`` -> TOML text. Raises ``TomlWriteError`` for unrepresentable unknowns."""
    return dumps(to_document(cfg), header=FILE_HEADER)
