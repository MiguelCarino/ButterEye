#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Extract the GUI string catalogue into Qt TS XML (docs/design/GUI.md R3, §10 item 4).

``lupdate``/``lrelease`` are unusable on the dev box (dangling symlinks, no
``qt6-linguist``), so this stdlib-only tool walks ``buttereye/gui/**/*.py`` with
``ast`` and collects:

- ``<obj>.tr("text"[, "disambiguation"[, n]])`` -> context = enclosing class name
  (PySide6 uses the runtime class name; base classes use ``translate`` instead);
- ``QCoreApplication.translate("Context", "text"[, "disambiguation"[, n]])``
  (also ``QApplication``/``QGuiApplication`` and a bare ``translate``);
- ``QT_TRANSLATE_NOOP("Context", "text")`` and ``QT_TR_NOOP("text")``;
- calls to *helpers*: any function or method whose body is a single
  ``return QCoreApplication.translate("Context", <its first parameter>[, ...])``,
  called with a literal (``_t("text")``, ``a11y.tr("text")``,
  ``Banner.tr_static("text")``);
- a context may be a module-level string constant (``CTX = "RenderPage"`` then
  ``translate(CTX, text)``);
- a module-level tuple/list of string literals (``COLUMNS = ("Source", ...)``)
  iterated by ``for`` or a comprehension whose loop variable is passed to a
  helper (``for h in COLUMNS: _t(h)``) contributes every element.

``unresolved()`` lists translate/helper calls whose context or source text this
tool cannot resolve to a literal: those strings would be missing from the
catalogue. ``tests/gui/test_ts_fresh.py`` fails on any of them unless the line
carries ``# i18n: dynamic`` (the text is catalogued elsewhere, e.g. through
``QT_TRANSLATE_NOOP``, or is not GUI text at all).

Locations carry the file name but no line numbers, so the committed catalogue
changes only when strings change. Output is deterministic (sorted).

    python tools/extract_ts.py            # rewrite buttereye/data/i18n/buttereye_en.ts
    python tools/extract_ts.py --check    # exit 1 if the committed file is stale
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO / "buttereye" / "gui"
DEFAULT_OUT = REPO / "buttereye" / "data" / "i18n" / "buttereye_en.ts"
LANGUAGE = "en"

_APP_CLASSES = frozenset({"QCoreApplication", "QApplication", "QGuiApplication"})


@dataclass(frozen=True, order=True)
class Message:
    context: str
    source: str
    comment: str = ""
    plural: bool = False


@dataclass
class Catalogue:
    #: (context, source, comment) -> (plural, {relative filenames})
    entries: dict[tuple[str, str, str], tuple[bool, set[str]]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, msg: Message, filename: str) -> None:
        key = (msg.context, msg.source, msg.comment)
        plural, files = self.entries.get(key, (False, set()))
        files.add(filename)
        self.entries[key] = (plural or msg.plural, files)

    def contexts(self) -> list[str]:
        return sorted({k[0] for k in self.entries})


def _str(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_name(path: Path) -> str:
    rel = path.resolve().relative_to(REPO).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _translate_call(node: ast.expr) -> ast.Call | None:
    """``QCoreApplication.translate(...)`` / ``translate(...)``, else None."""
    if not isinstance(node, ast.Call):
        return None
    f = node.func
    if isinstance(f, ast.Attribute) and f.attr == "translate":
        if isinstance(f.value, ast.Name) and f.value.id in _APP_CLASSES:
            return node
    if isinstance(f, ast.Name) and f.id == "translate":
        return node
    return None


def module_constants(tree: ast.Module) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Module-level ``NAME = "literal"`` and ``NAME = ("a", "b", ...)`` assignments."""
    strings: dict[str, str] = {}
    sequences: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        targets: list[ast.expr]
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        text = _str(value)
        if text is not None:
            for n in names:
                strings[n] = text
        elif isinstance(value, ast.Tuple | ast.List) and value.elts:
            items = [_str(e) for e in value.elts]
            if all(i is not None for i in items):
                for n in names:
                    sequences[n] = tuple(i for i in items if i is not None)
    return strings, sequences


def _helper_context(fn: ast.FunctionDef, consts: dict[str, str] | None = None) -> str | None:
    """Context if ``fn`` is ``return QCoreApplication.translate(<ctx>, <first param>)``.

    ``<ctx>`` is a literal or a module-level string constant from ``consts``.
    """
    params = [a.arg for a in fn.args.posonlyargs + fn.args.args]
    if params and params[0] in ("self", "cls"):
        params = params[1:]
    if not params:
        return None
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and _str(s.value) is not None)]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
        return None
    call = _translate_call(body[0].value)
    if call is None or len(call.args) < 2:
        return None
    ctx = _resolve(call.args[0], consts or {})
    arg = call.args[1]
    if ctx is None or not isinstance(arg, ast.Name) or arg.id != params[0]:
        return None
    return ctx


def _resolve(node: ast.expr, consts: dict[str, str]) -> str | None:
    text = _str(node)
    if text is None and isinstance(node, ast.Name):
        text = consts.get(node.id)
    return text


def collect_helpers(tree: ast.Module) -> dict[str, str]:
    """name -> context for every translate helper defined in ``tree`` (any depth)."""
    consts, _seqs = module_constants(tree)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            ctx = _helper_context(node, consts)
            if ctx is not None:
                out[node.name] = ctx
    return out


def _loop_sources(tree: ast.Module, seqs: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Loop variable -> module-level string sequence it iterates (``for h in COLUMNS``,
    ``for i, h in enumerate(COLUMNS)``, comprehensions)."""
    out: dict[str, str] = {}

    def bind(target: ast.expr, it: ast.expr) -> None:
        if isinstance(it, ast.Name) and it.id in seqs and isinstance(target, ast.Name):
            out[target.id] = it.id
        elif (
            isinstance(it, ast.Call)
            and isinstance(it.func, ast.Name)
            and it.func.id == "enumerate"
            and it.args
            and isinstance(it.args[0], ast.Name)
            and it.args[0].id in seqs
            and isinstance(target, ast.Tuple)
            and len(target.elts) == 2
            and isinstance(target.elts[1], ast.Name)
        ):
            out[target.elts[1].id] = it.args[0].id

    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.AsyncFor):
            bind(node.target, node.iter)
        elif isinstance(node, ast.comprehension):
            bind(node.target, node.iter)
    return out


class _Visitor(ast.NodeVisitor):
    def __init__(
        self,
        filename: str,
        local_helpers: dict[str, str],
        imported_helpers: dict[str, str],
        module_helpers: dict[str, dict[str, str]],
        cat: Catalogue,
        consts: dict[str, str] | None = None,
        seqs: dict[str, tuple[str, ...]] | None = None,
        loops: dict[str, str] | None = None,
        lines: list[str] | None = None,
    ) -> None:
        self.filename = filename
        self.consts = consts or {}
        self.seqs = seqs or {}
        self.loops = loops or {}
        self.lines = lines or []
        self.unresolved: list[str] = []
        self.helper_stack: list[bool] = []
        self.local = local_helpers
        self.imported = imported_helpers  # bare name -> context (from-imports)
        self.modules = module_helpers  # alias -> {helper -> context}
        self.cat = cat
        self.classes: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.generic_visit(node)
        self.classes.pop()

    def _flag(self, call: ast.Call, why: str) -> None:
        line = self.lines[call.lineno - 1] if 0 < call.lineno <= len(self.lines) else ""
        if "i18n: dynamic" not in line:
            self.unresolved.append(f"{self.filename}:{call.lineno}: {why}")

    def _emit(self, ctx: str | None, call: ast.Call, src_index: int) -> None:
        if ctx is None:
            self._flag(call, "context is not a literal or module constant")
            return
        if len(call.args) <= src_index:
            return
        arg = call.args[src_index]
        source = _str(arg)
        if source is None:
            if isinstance(arg, ast.Name) and arg.id in self.loops:
                for item in self.seqs[self.loops[arg.id]]:
                    self.cat.add(Message(ctx, item), self.filename)
                return
            self._flag(call, "source text is not a literal")
            return
        comment = ""
        if len(call.args) > src_index + 1:
            comment = _str(call.args[src_index + 1]) or ""
        plural = len(call.args) > src_index + 2
        for kw in call.keywords:
            if kw.arg == "disambiguation":
                comment = _str(kw.value) or comment
            elif kw.arg == "n":
                plural = True
        self.cat.add(Message(ctx, source, comment, plural), self.filename)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        translate = _translate_call(node)
        if translate is not None:
            if not self._is_helper_body(node):
                ctx = _resolve(node.args[0], self.consts) if node.args else None
                self._emit(ctx, node, 1)
        elif isinstance(f, ast.Name) and f.id == "QT_TRANSLATE_NOOP":
            ctx = _resolve(node.args[0], self.consts) if node.args else None
            self._emit(ctx, node, 1)
        elif isinstance(f, ast.Name) and f.id == "QT_TR_NOOP":
            if self.classes:
                self._emit(self.classes[-1], node, 0)
        elif (
            isinstance(f, ast.Attribute)
            and f.attr == "tr"
            and not (isinstance(f.value, ast.Name) and f.value.id in self.modules)
        ):
            if self.classes:
                if not self._is_helper_body(node):
                    self._emit(self.classes[-1], node, 0)
            elif _str(node.args[0] if node.args else None) is not None:
                self.cat.warnings.append(f"{self.filename}: tr() outside a class")
        elif isinstance(f, ast.Name) and f.id in self.local:
            self._emit(self.local[f.id], node, 0)
        elif isinstance(f, ast.Name) and f.id in self.imported:
            self._emit(self.imported[f.id], node, 0)
        elif isinstance(f, ast.Attribute):
            owner = f.value
            if isinstance(owner, ast.Name) and owner.id in self.modules:
                ctx = self.modules[owner.id].get(f.attr)
                if ctx is not None:
                    self._emit(ctx, node, 0)
            elif f.attr in self.local:  # cls.tr_static("...") / Banner.tr_static("...")
                self._emit(self.local[f.attr], node, 0)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        is_helper = _helper_context(node, self.consts) is not None or _tr_forwarder(node)
        self.helper_stack.append(is_helper)
        self.generic_visit(node)
        self.helper_stack.pop()

    def _is_helper_body(self, _call: ast.Call) -> bool:
        """The forwarding call inside a translate helper itself (its callers count)."""
        return bool(self.helper_stack) and self.helper_stack[-1]


def _tr_forwarder(fn: ast.FunctionDef) -> bool:
    """``def x(self, text): return self.tr(text)``-style forwarders."""
    body = [s for s in fn.body if not (isinstance(s, ast.Expr) and _str(s.value) is not None)]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    v = body[0].value
    return (
        isinstance(v, ast.Call)
        and isinstance(v.func, ast.Attribute)
        and v.func.attr == "tr"
        and bool(v.args)
        and isinstance(v.args[0], ast.Name)
    )


def _sources(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def extract(root: Path = SOURCE_ROOT) -> Catalogue:
    return _extract(root)[0]


def unresolved(root: Path = SOURCE_ROOT) -> list[str]:
    """Translate/helper calls whose context or text is not a resolvable literal."""
    return _extract(root)[1]


def _extract(root: Path) -> tuple[Catalogue, list[str]]:
    cat = Catalogue()
    problems: list[str] = []
    files = _sources(root)
    trees: dict[Path, ast.Module] = {}
    helpers_by_module: dict[str, dict[str, str]] = {}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        trees[path] = tree
        helpers_by_module[_module_name(path)] = collect_helpers(tree)
    for path in files:
        tree = trees[path]
        imported: dict[str, str] = {}
        modules: dict[str, dict[str, str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    as_name = alias.asname or alias.name
                    sub = f"{node.module}.{alias.name}"
                    if sub in helpers_by_module:  # from buttereye.gui import a11y
                        modules[as_name] = helpers_by_module[sub]
                    ctx = helpers_by_module.get(node.module, {}).get(alias.name)
                    if ctx is not None:  # from buttereye.gui.a11y import tr
                        imported[as_name] = ctx
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in helpers_by_module:
                        modules[alias.asname or alias.name] = helpers_by_module[alias.name]
        filename = path.resolve().relative_to(REPO).as_posix()
        consts, seqs = module_constants(tree)
        visitor = _Visitor(
            filename,
            helpers_by_module[_module_name(path)],
            imported,
            modules,
            cat,
            consts,
            seqs,
            _loop_sources(tree, seqs),
            path.read_text(encoding="utf-8").splitlines(),
        )
        visitor.visit(tree)
        problems += visitor.unresolved
    return cat, problems


def to_ts(cat: Catalogue, language: str = LANGUAGE) -> str:
    out: list[str] = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE TS>",
        f'<TS version="2.1" language="{escape(language)}">',
    ]
    by_ctx: dict[str, list[tuple[str, str, bool, set[str]]]] = {}
    for (ctx, source, comment), (plural, files) in cat.entries.items():
        by_ctx.setdefault(ctx, []).append((source, comment, plural, files))
    for ctx in sorted(by_ctx):
        out.append("<context>")
        out.append(f"    <name>{escape(ctx)}</name>")
        for source, comment, plural, files in sorted(by_ctx[ctx], key=lambda m: (m[0], m[1])):
            out.append('    <message numerus="yes">' if plural else "    <message>")
            for fn in sorted(files):
                out.append(f'        <location filename="{escape(fn, {chr(34): "&quot;"})}"/>')
            out.append(f"        <source>{escape(source)}</source>")
            if comment:
                out.append(f"        <comment>{escape(comment)}</comment>")
            if plural:
                out.append('        <translation type="unfinished">')
                out.append("            <numerusform></numerusform>")
                out.append("        </translation>")
            else:
                out.append('        <translation type="unfinished"></translation>')
            out.append("    </message>")
        out.append("</context>")
    out.append("</TS>")
    return "\n".join(out) + "\n"


def contexts_in_ts(text: str) -> list[str]:
    import xml.etree.ElementTree as ET  # noqa: N817 - stdlib idiom

    root = ET.fromstring(text)
    return [c.findtext("name") or "" for c in root.iter("context")]


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--root", type=Path, default=SOURCE_ROOT)
    parser.add_argument("--check", action="store_true", help="fail if the output is stale")
    ns = parser.parse_args(list(argv) if argv is not None else None)
    cat = extract(ns.root)
    for w in cat.warnings:
        print(f"warning: {w}", file=sys.stderr)
    text = to_ts(cat)
    if ns.check:
        current = ns.output.read_text(encoding="utf-8") if ns.output.exists() else ""
        if current != text:
            print(f"{ns.output} is stale; run tools/extract_ts.py", file=sys.stderr)
            return 1
        return 0
    ns.output.parent.mkdir(parents=True, exist_ok=True)
    ns.output.write_text(text, encoding="utf-8")
    print(f"wrote {ns.output} ({len(cat.entries)} messages, {len(cat.contexts())} contexts)")
    return 0


def iter_messages(cat: Catalogue) -> Iterator[Message]:
    for (ctx, source, comment), (plural, _files) in sorted(cat.entries.items()):
        yield Message(ctx, source, comment, plural)


if __name__ == "__main__":
    sys.exit(main())
