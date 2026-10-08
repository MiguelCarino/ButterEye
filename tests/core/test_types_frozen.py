# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.1/§2.3: every exported type is a frozen slotted dataclass or an enum, its
hints resolve without Qt, and fields/members match the frozen design text."""

from __future__ import annotations

import ast
import dataclasses
import enum
import typing
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core import events, types
from buttereye.core.types import Msg


def _exported(mod: object) -> list[tuple[str, type]]:
    out = []
    for name in mod.__all__:  # type: ignore[attr-defined]
        obj = getattr(mod, name)
        if isinstance(obj, type):
            out.append((name, obj))
    return out


TYPE_CLASSES = _exported(types) + [
    (n, c) for n, c in _exported(events) if dataclasses.is_dataclass(c)
]


def _walk_hint(hint: object) -> typing.Iterator[object]:
    yield hint
    for arg in typing.get_args(hint):
        yield from _walk_hint(arg)


@pytest.mark.parametrize(("name", "cls"), TYPE_CLASSES, ids=[n for n, _ in TYPE_CLASSES])
def test_frozen_slotted_or_enum(name: str, cls: type) -> None:
    if issubclass(cls, enum.Enum):
        return
    if name == "EventBus":
        return
    assert dataclasses.is_dataclass(cls), name
    params = cls.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen, f"{name} is not frozen"
    assert "__slots__" in cls.__dict__, f"{name} is not slotted"


@pytest.mark.parametrize(("name", "cls"), TYPE_CLASSES, ids=[n for n, _ in TYPE_CLASSES])
def test_hints_resolve_without_qt(name: str, cls: type) -> None:
    if not dataclasses.is_dataclass(cls):
        return
    hints = typing.get_type_hints(cls)
    for hint in hints.values():
        for part in _walk_hint(hint):
            module = getattr(part, "__module__", "") or ""
            assert not module.startswith(("PySide6", "shiboken")), (name, part)


def test_instances_are_immutable() -> None:
    m = Msg("hello {n}", {"n": 1})
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.key = "x"  # type: ignore[misc]
    assert dict(Msg("x").params) == {}


def test_bridge_payload_kinds() -> None:
    """Every field type is a §2 type, a builtin scalar, Fraction, Path, datetime,
    tuple, frozenset or Mapping (MappingProxyType at runtime)."""
    allowed_modules = {
        "builtins",
        "fractions",
        "pathlib",
        "datetime",
        "types",
        "typing",
        "collections.abc",
        "buttereye.core.types",
        "buttereye.core.errors",
        "buttereye.core.events",
    }
    for name, cls in TYPE_CLASSES:
        if not dataclasses.is_dataclass(cls):
            continue
        for hint in typing.get_type_hints(cls).values():
            for part in _walk_hint(hint):
                if part in (type(None), Fraction, Path):
                    continue
                origin = typing.get_origin(part)
                module = getattr(origin or part, "__module__", None)
                if module is None:  # Literal values, NewType strings
                    continue
                assert module in allowed_modules, (name, part, module)


# ---------------------------------------------------------------------------
# Transcription check against docs/design/GUI.md §2.1 and §2.3
# ---------------------------------------------------------------------------


def _doc_classes(code: str) -> dict[str, ast.ClassDef]:
    return {n.name: n for n in ast.parse(code).body if isinstance(n, ast.ClassDef)}


def _doc_fields(node: ast.ClassDef) -> list[str]:
    return [
        s.target.id
        for s in node.body
        if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
    ]


def _doc_enum_members(node: ast.ClassDef) -> dict[str, object]:
    members: dict[str, object] = {}
    for s in node.body:
        if isinstance(s, ast.Assign) and isinstance(s.targets[0], ast.Name):
            members[s.targets[0].id] = ast.literal_eval(s.value)
    return members


def _check_against_doc(code: str, module: object) -> int:
    checked = 0
    for name, node in _doc_classes(code).items():
        cls = getattr(module, name)
        bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
        if bases & {"StrEnum", "Enum"}:
            assert {m.name: m.value for m in cls} == _doc_enum_members(node), name
        else:
            actual = [f.name for f in dataclasses.fields(cls)]
            assert actual == _doc_fields(node), name
            for s in node.body:
                if isinstance(s, ast.FunctionDef):
                    assert hasattr(cls, s.name), (name, s.name)
        checked += 1
    return checked


def test_types_match_design_2_1(design_doc: typing.Any) -> None:
    code = design_doc.python("### 2.1")
    assert _check_against_doc(code, types) >= 60
    tree = ast.parse(code)
    newtypes = [
        s.targets[0].id
        for s in tree.body
        if isinstance(s, ast.Assign)
        and isinstance(s.targets[0], ast.Name)
        and isinstance(s.value, ast.Call)
        and getattr(s.value.func, "id", "") == "NewType"
    ]
    assert newtypes == ["OpId", "SessionId", "JobId", "CandidateId"]
    for n in newtypes:
        assert n in types.__all__


def test_events_match_design_2_3(design_doc: typing.Any) -> None:
    code = design_doc.python("### 2.3")
    assert _check_against_doc(code, events) == 13
    union = {a.__name__ for a in typing.get_args(events.Event)}
    assert union == {c for c in _doc_classes(code) if c != "Progress"}
    assert set(events.EVENT_TYPES) == set(typing.get_args(events.Event))


def test_paths_defaults() -> None:
    p = types.Paths(*(Path(f"/x/{i}") for i in range(10)))
    assert p.rpm_plugin_dir == Path("/usr/lib64/buttereye/vapoursynth")
    assert p.rpm_data_dir == Path("/usr/share/buttereye")


def test_command_hint_text_quotes() -> None:
    h = types.CommandHint(("buttereye", "play", "/v/My Film.mkv"), "in_scope")
    assert h.text() == "buttereye play '/v/My Film.mkv'"


def test_doctor_report_blocking() -> None:
    hw = types.HardwareInfo((), (), None, None, 1)
    f = types.Finding(
        "x", types.Section.MPV, types.Severity.BLOCKING, None, Msg("t"), Msg("c"), Msg("f")
    )
    assert types.DoctorReport((f,), hw, None, 0.1, False).blocking
    ok = dataclasses.replace(f, severity=types.Severity.OK)
    assert not types.DoctorReport((ok,), hw, None, 0.1, False).blocking
