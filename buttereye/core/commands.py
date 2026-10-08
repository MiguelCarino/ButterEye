# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""CLI-equivalent command hints (docs/design/GUI.md §2.6).

Template syntax (one string per command, split on spaces into ``argv_template``):

- ``{name}`` - placeholder; the value becomes one argv element (spaces kept,
  ``CommandHint.text()`` quotes it). Placeholders may sit inside a token
  (``--{toggle}``).
- ``[--opt {name}]`` - optional group, emitted only when every placeholder in it
  is given. A group without placeholders (``[--full]``) is a switch named after
  its flag (``full``) and is emitted when that param is ``"1"``, ``"yes"`` or
  ``"true"``.
- ``--{name}...`` - repeated token: the param is a comma-separated list and the
  token is emitted once per item (``clean --{target}...``).

``command_hint`` raises ``KeyError`` for an unknown command id, an unknown
param, or a missing required placeholder.

The table is the CLI *spec* (R16): ``status`` says whether a form is in scope or
only proposed, not whether a ``buttereye`` executable exists. ``cli_available()``
answers that at runtime; while it is ``False`` every hint must be shown with a
"not installed in this build yet" note (``CLI_MISSING_NOTE``) so nobody pastes a
command that cannot run.
"""

from __future__ import annotations

import functools
import importlib.metadata
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from buttereye.core.types import CommandHint, Msg

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_TRUE = frozenset({"1", "yes", "true"})


@dataclass(frozen=True, slots=True)
class CommandSpec:
    id: str
    argv_template: tuple[str, ...]
    status: Literal["in_scope", "proposed"]

    def placeholders(self) -> frozenset[str]:
        names: set[str] = set()
        for token in self.argv_template:
            names.update(_PLACEHOLDER.findall(token))
            switch = _switch_name(token)
            if switch is not None:
                names.add(switch)
        return frozenset(names)


def _split_template(template: str) -> tuple[str, ...]:
    """Split on spaces, keeping ``[...]`` groups as one token."""
    tokens: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in template:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                raise ValueError(f"unbalanced ']' in {template!r}")
        if ch == " " and depth == 0:
            if buf:
                tokens.append("".join(buf))
                buf = []
            continue
        buf.append(ch)
    if depth:
        raise ValueError(f"unbalanced '[' in {template!r}")
    if buf:
        tokens.append("".join(buf))
    return tuple(tokens)


def _switch_name(token: str) -> str | None:
    if token.startswith("[") and token.endswith("]"):
        inner = token[1:-1].split(" ")
        if not any(_PLACEHOLDER.search(t) for t in inner) and inner and inner[0].startswith("--"):
            return inner[0].lstrip("-").replace("-", "_")
    return None


_TABLE: tuple[tuple[str, str, Literal["in_scope", "proposed"]], ...] = (
    ("setup", "buttereye setup", "in_scope"),
    ("doctor", "buttereye doctor", "in_scope"),
    ("doctor.report", "buttereye doctor --report", "in_scope"),
    ("play", "buttereye play {file} [--profile {profile}]", "in_scope"),
    ("attach", "buttereye attach [--socket {socket}]", "in_scope"),
    ("detach", "buttereye detach [--socket {socket}]", "in_scope"),
    ("detach.disable", "buttereye detach --disable [--socket {socket}]", "in_scope"),
    # toggle = "enable" | "disable"
    ("live.toggle", "buttereye attach --socket {socket} --{toggle}", "in_scope"),
    ("live.profile", "buttereye attach --socket {socket} --profile {profile}", "in_scope"),
    ("orphans.remove", "buttereye detach --orphans", "in_scope"),
    (
        "render",
        "buttereye render {src} -o {out} --profile {profile} --encoder {encoder}",
        "in_scope",
    ),
    ("bench", "buttereye bench [--file {file}] [--full]", "in_scope"),
    ("bench.apply", "buttereye bench --apply {label}", "in_scope"),
    ("models.list", "buttereye models list", "in_scope"),
    ("models.add", "buttereye models add {name}", "in_scope"),
    ("models.remove", "buttereye models remove {name}", "in_scope"),
    ("models.install", "buttereye models install --from {file} [--sha256 {sha}]", "in_scope"),
    ("plugins.list", "buttereye plugins list", "in_scope"),
    # target = comma-separated CleanTarget values, e.g. "engines,models"
    ("clean", "buttereye clean --{target}...", "in_scope"),
    ("clean.dry_run", "buttereye clean --dry-run", "in_scope"),
    ("profiles.list", "buttereye profiles list", "in_scope"),
    (
        "profiles.explain",
        "buttereye profiles explain [--fps {fps}] [--width {width}] [--height {height}]"
        " [--hdr {hdr}] [--display-hz {display_hz}] [--interlaced {interlaced}]"
        " [--path {path}]",
        "in_scope",
    ),
    ("trt.optin", "buttereye setup --trt-experimental", "in_scope"),
    ("licence", "buttereye licence", "in_scope"),
    ("version", "buttereye --version", "in_scope"),
)

COMMANDS: Mapping[str, CommandSpec] = MappingProxyType(
    {cid: CommandSpec(cid, _split_template(tpl), status) for cid, tpl, status in _TABLE}
)


def _fill(token: str, params: Mapping[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        return params[m.group(1)]

    return _PLACEHOLDER.sub(sub, token)


def command_hint(command_id: str, **params: str) -> CommandHint:
    """Render the CLI equivalent of a GUI action (display only; never executed)."""
    spec = COMMANDS[command_id]
    unknown = set(params) - spec.placeholders()
    if unknown:
        raise KeyError(f"{command_id}: unknown parameter(s) {', '.join(sorted(unknown))}")
    argv: list[str] = []
    for token in spec.argv_template:
        if token.startswith("[") and token.endswith("]"):
            inner = token[1:-1].split(" ")
            switch = _switch_name(token)
            if switch is not None:
                if params.get(switch, "").strip().lower() in _TRUE:
                    argv.extend(inner)
                continue
            names = {n for t in inner for n in _PLACEHOLDER.findall(t)}
            if all(params.get(n) not in (None, "") for n in names):
                argv.extend(_fill(t, params) for t in inner)
            continue
        if token.endswith("..."):
            base = token[: -len("...")]
            found = _PLACEHOLDER.findall(base)
            if len(found) != 1:
                raise ValueError(f"repeat token needs one placeholder: {token!r}")
            name = found[0]
            if name not in params:
                raise KeyError(f"{command_id}: missing parameter {name!r}")
            items = [v.strip() for v in params[name].split(",") if v.strip()]
            if not items:
                raise KeyError(f"{command_id}: empty parameter {name!r}")
            argv.extend(_fill(base, {**params, name: item}) for item in items)
            continue
        for name in _PLACEHOLDER.findall(token):
            if name not in params:
                raise KeyError(f"{command_id}: missing parameter {name!r}")
        argv.append(_fill(token, params))
    return CommandHint(tuple(argv), spec.status)


CLI_SCRIPT = "buttereye"

CLI_MISSING_NOTE = Msg(
    "Not available yet: the buttereye command-line tool isn't installed in this build."
)


@functools.cache
def cli_available() -> bool:
    """True when a ``buttereye`` command-line tool can actually be run: the
    installed distribution registers a ``buttereye`` console script, or one is
    on ``PATH``. Cached for the process (``cli_available.cache_clear()`` in tests).

    ``buttereye-gui`` does not count: hints are ``buttereye ...`` commands.
    """
    try:
        eps = importlib.metadata.entry_points(group="console_scripts", name=CLI_SCRIPT)
    except Exception:  # pragma: no cover - broken metadata must not break the GUI
        eps = importlib.metadata.EntryPoints(())
    for ep in eps:
        dist = ep.dist
        if dist is None or dist.metadata["Name"].lower() == "buttereye":
            return True
    return shutil.which(CLI_SCRIPT) is not None


__all__ = [
    "CommandSpec",
    "COMMANDS",
    "command_hint",
    "cli_available",
    "CLI_MISSING_NOTE",
    "CLI_SCRIPT",
]
