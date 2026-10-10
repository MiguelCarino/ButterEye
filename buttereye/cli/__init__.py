# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The ``buttereye`` command (SCOPE §4.1): a thin layer over the core API.

Every command accepts ``--json`` (one versioned JSON document on stdout),
``-v``/``-q`` and honours ``NO_COLOR``. Exit codes (SCOPE §4.1): 0 ok,
1 runtime failure, 2 usage or config error, 3 blocking ``doctor`` issue,
4 dependency missing, 130 cancelled. Commands the core doesn't implement yet
(attach, detach, report bundles, model downloads) say so with BE-9001.

No Qt is imported here (SCOPE §4.1): the CLI runs on a plain asyncio loop.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import enum
import json
import os
import signal
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any, TextIO

import buttereye
from buttereye.core import licences
from buttereye.core.api import ButterEye
from buttereye.core.capabilities import unavailable_text
from buttereye.core.errors import ButterEyeError, ErrorCode, OperationCancelled
from buttereye.core.events import HealthChanged, JobChanged, Notice, SessionChanged, SessionEnded
from buttereye.core.i18n import render
from buttereye.core.profiles import rules
from buttereye.core.types import (
    BenchRequest,
    CleanTarget,
    DetachPolicy,
    Feature,
    HdrClass,
    Msg,
    OpState,
    Paths,
    RenderJobSpec,
    SetupChoices,
    Severity,
    SourceFacts,
    Target,
    TargetKind,
)

JSON_VERSION = 1
EXIT_OK, EXIT_FAILURE, EXIT_USAGE, EXIT_BLOCKING, EXIT_DEPENDENCY = 0, 1, 2, 3, 4
EXIT_CANCELLED = 130

Opener = Callable[[Paths | None], Awaitable[ButterEye]]

_GLYPH = {"ok": "✓", "info": "i", "degraded": "!", "blocking": "✗"}


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


def plain(value: Any) -> Any:
    """JSON-safe form of core values (dataclasses, enums, paths, fractions, messages)."""
    if isinstance(value, Msg):
        return render(value)
    if isinstance(value, ErrorCode):
        return value.code
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (Path, Fraction)):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(plain(k)): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


class Out:
    """Human lines on stdout/stderr, or one JSON document at the end (``--json``)."""

    def __init__(self, ns: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> None:
        self.json = bool(getattr(ns, "json", False))
        self.quiet = bool(getattr(ns, "quiet", False))
        self.verbose = bool(getattr(ns, "verbose", False))
        self.stdout, self.stderr = stdout, stderr
        self.color = (
            not self.json and "NO_COLOR" not in os.environ and getattr(stdout, "isatty", bool)()
        )
        self._last_progress = 0.0

    def line(self, text: str = "") -> None:
        if not self.json and not self.quiet:
            print(text, file=self.stdout)

    def detail(self, text: str) -> None:
        if self.verbose and not self.json:
            print(text, file=self.stdout)

    def progress(self, text: str, *, every_s: float = 1.0) -> None:
        if self.json or self.quiet:
            return
        now = time.monotonic()
        if now - self._last_progress >= every_s:
            self._last_progress = now
            print(text, file=self.stderr)

    def badge(self, kind: str, text: str) -> str:
        glyph = _GLYPH.get(kind, "·")
        if not self.color:
            return f"{glyph} {text}"
        colour = {"ok": "32", "info": "36", "degraded": "33", "blocking": "31"}.get(kind, "0")
        return f"\033[{colour}m{glyph}\033[0m {text}"

    def document(self, command: str, data: Any, *, ok: bool, error: Any = None) -> None:
        if self.json:
            doc = {"v": JSON_VERSION, "command": command, "ok": ok, "data": plain(data)}
            if error is not None:
                doc["error"] = error
            json.dump(doc, self.stdout, ensure_ascii=False, indent=1)
            self.stdout.write("\n")

    def error(self, command: str, err: ButterEyeError) -> int:
        payload = {
            "code": err.code.code,
            "message": render(err.cause),
            "fix": render(err.fix) if err.fix is not None else None,
            "commands": list(err.commands),
        }
        if self.json:
            self.document(command, None, ok=False, error=payload)
        else:
            print(f"{err.code.code}: {payload['message']}", file=self.stderr)
            if payload["fix"]:
                print(f"  {payload['fix']}", file=self.stderr)
            for cmd in err.commands:
                print(f"  $ {cmd}", file=self.stderr)
            if err.detail and self.verbose:
                print(err.detail, file=self.stderr)
        return err.exit_code


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def need(core: ButterEye, feature: Feature) -> None:
    """Raise BE-9001 / the capability's own code when ``feature`` isn't available."""
    caps = await core.capabilities()
    st = caps.states.get(feature)
    if st is None or st.available:
        return
    raise ButterEyeError(
        st.code or ErrorCode.NOT_IMPLEMENTED,
        unavailable_text(feature, st),
        commands=st.commands,
    )


def parse_rate(text: str) -> Fraction:
    try:
        rate = Fraction(text)
    except ValueError, ZeroDivisionError:
        raise argparse.ArgumentTypeError(f"not a frame rate: {text!r}") from None
    if rate <= 0:
        raise argparse.ArgumentTypeError("the frame rate must be positive")
    return rate


def parse_size(text: str) -> tuple[int, int]:
    try:
        w, h = (int(x) for x in text.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a size (WxH): {text!r}") from None
    return w, h


def parse_target(text: str) -> Target:
    if text in ("2x", "x2"):
        return Target(TargetKind.X2)
    return Target(TargetKind.FPS, parse_rate(text))


def _rate(r: Fraction | None) -> str:
    return "—" if r is None else f"{float(r):.3f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def cmd_doctor(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    report = await core.doctor(trt=True if ns.trt else None).result()
    section = None
    for f in report.findings:
        if f.severity is Severity.OK and not out.verbose:
            continue
        if f.section is not section:
            section = f.section
            out.line(f"\n{section.value}")
        code = f" ({f.code.code})" if f.code is not None else ""
        out.line("  " + out.badge(f.severity.value, render(f.title) + code))
        if f.severity is not Severity.OK:
            for text in (render(f.cause), render(f.fix)):
                if text:
                    out.line(f"      {text}")
            for cmd in f.commands:
                out.line(f"      $ {cmd}")
    counts = {s: sum(1 for f in report.findings if f.severity is s) for s in Severity}
    out.line(
        f"\n{counts[Severity.OK]} ok, {counts[Severity.INFO]} info, "
        f"{counts[Severity.DEGRADED]} degraded, {counts[Severity.BLOCKING]} blocking "
        f"({report.duration_s:.1f} s)"
    )
    out.document("doctor", report, ok=not report.blocking)
    return EXIT_BLOCKING if report.blocking else EXIT_OK


async def cmd_setup(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    report = await core.doctor(trt=True if ns.trt_experimental else None).result()
    if report.blocking:
        out.line("Setup can't continue: run `buttereye doctor` to see what's blocking.")
        out.document("setup", report, ok=False)
        return EXIT_BLOCKING
    plan = await core.setup_plan(report, trt_experimental=bool(ns.trt_experimental))
    backend = plan.proposed.backend
    if backend is None:
        raise ButterEyeError(
            ErrorCode.PKG_MISSING,
            Msg("No smoothing engine works on this computer yet."),
            commands=plan.dnf_lines,
        )
    choices = SetupChoices(
        backend=backend,
        trt_experimental=bool(ns.trt_experimental),
        confirmed_downloads=frozenset(),
    )
    result = await core.setup_apply(plan, choices).result()
    out.line(f"Set up with {backend.value}.")
    out.document("setup", result, ok=True)
    return EXIT_OK


async def cmd_play(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    await need(core, Feature.LIVE)
    file = Path(ns.file)
    events = core.subscribe()
    sid = await core.play(file, profile_id=ns.profile).result()
    out.line(f"Playing {file.name} in mpv (session {sid}). Ctrl+C leaves mpv playing.")
    last = ""
    try:
        async for ev in events:
            if isinstance(ev, SessionEnded) and ev.sid == sid:
                out.line(f"mpv closed ({ev.reason}).")
                break
            if isinstance(ev, SessionChanged) and ev.snapshot.sid == sid:
                s = ev.snapshot
                if s.backend is not None and s.target_fps is not None:
                    text = (
                        f"{_rate(s.source.fps if s.source else None)} → "
                        f"{_rate(s.target_fps)} fps with {s.backend.value}"
                        f" ({s.filter.value}, {s.health.value})"
                    )
                else:
                    text = f"{s.filter.value}" + (f": {render(s.notice)}" if s.notice else "")
                if text != last:
                    last = text
                    out.line(text)
            elif isinstance(ev, HealthChanged) and ev.sid == sid:
                out.line(f"{ev.health.value}: {render(ev.reason)}")
            elif isinstance(ev, Notice) and ev.sid in (sid, None):
                out.line(render(ev.message))
    except asyncio.CancelledError:
        out.line("Leaving mpv playing; ButterEye stops managing it.")
        raise
    out.document("play", {"session": sid}, ok=True)
    return EXIT_OK


async def cmd_render(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    await need(core, Feature.RENDER)
    src = Path(ns.source)
    load = await core.load_config()
    ids = [p.id for p in load.config.profiles]
    profile_id = ns.profile or ("simple" if "simple" in ids else (ids[0] if ids else "simple"))
    probe = await core.render_probe(src, profile_id=profile_id)
    if probe.refusal is not None:
        raise ButterEyeError(
            probe.refusal.code or ErrorCode.CODEC_NOT_DECODABLE,
            probe.refusal.title,
            probe.refusal.fix,
            commands=probe.refusal.commands,
        )
    if not probe.encoders:
        raise ButterEyeError(ErrorCode.ENCODER_FAILED, Msg("No usable video encoder was found."))
    encoder = ns.encoder or probe.encoders[0]
    output = Path(ns.output) if ns.output else src.with_name(f"{src.stem}.smooth.mkv")
    spec = RenderJobSpec(
        source=src,
        output=output,
        profile_id=profile_id,
        encoder=encoder,
        overwrite=bool(ns.overwrite),
        target=ns.target,
        size=ns.size,
    )
    events = core.subscribe()
    job = await core.render_enqueue(spec)
    out.line(f"Saving a smooth copy: {output} ({encoder}). Ctrl+C cancels.")
    try:
        async for ev in events:
            if not (isinstance(ev, JobChanged) and ev.job.id == job):
                continue
            j = ev.job
            if j.state is OpState.RUNNING and j.total_frames:
                pct = 100 * (j.done_frames or 0) / j.total_frames
                eta = f", about {int(j.eta_s // 60)} min left" if j.eta_s else ""
                out.progress(f"{j.phase.value if j.phase else 'render'} {pct:.0f} %{eta}")
            if j.state in (OpState.SUCCEEDED, OpState.FAILED, OpState.CANCELLED):
                out.document("render", j, ok=j.state is OpState.SUCCEEDED)
                if j.state is OpState.SUCCEEDED:
                    out.line(f"Saved {j.spec.output}.")
                    return EXIT_OK
                if j.state is OpState.CANCELLED:
                    return EXIT_CANCELLED
                if j.error is not None:
                    raise j.error
                return EXIT_FAILURE
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await core.render_cancel(job)
        raise
    return EXIT_FAILURE


async def cmd_bench(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    await need(core, Feature.BENCH)
    if ns.history:
        hist = await core.bench_history()
        for r in hist:
            out.line(f"{r.when:%Y-%m-%d %H:%M}  {r.request.width}×{r.request.height}  "
                     f"recommended: {r.recommended or '—'}")  # fmt: skip
        out.document("bench", hist, ok=True)
        return EXIT_OK
    if ns.apply:
        hist = await core.bench_history()
        if not hist:
            raise ButterEyeError(ErrorCode.BENCH_FAILED, Msg("There is no benchmark result yet."))
        load = await core.load_config()
        rev = await core.apply_bench(hist[-1], ns.apply, expected_revision=load.revision)
        out.line(f"Saved {ns.apply} as the benchmark profile.")
        out.document("bench", {"applied": ns.apply, "revision": rev}, ok=True)
        return EXIT_OK
    req = BenchRequest(ns.width, ns.height, ns.fps, full=bool(ns.full))
    op = core.bench(req)
    op.add_progress_sink(lambda p: out.progress(render(p.phase)))
    result = await op.result()
    out.line(f"{'configuration':32s} {'vspipe':>8s} {'in mpv':>8s}  real time  faults")
    for m in result.measurements:
        rt = "yes" if m.realtime else "no"
        out.line(f"{m.label:32s} {m.vspipe_fps:8.1f} {m.mpv_fps:8.1f}  {rt:9s}  "
                 f"{m.gpu_faults if m.gpu_faults is not None else '?'}")  # fmt: skip
    out.line(f"recommended: {result.recommended or '—'}")
    out.document("bench", result, ok=True)
    return EXIT_OK


async def cmd_profiles(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    load = await core.load_config()
    cfg = load.config
    if ns.action == "explain":
        facts = SourceFacts(ns.fps, ns.width, ns.height, HdrClass.SDR, ns.display_hz or 0.0,
                            False, str(ns.path or ""), False)  # fmt: skip
        trace = rules.explain(cfg, facts)
        for step in trace.steps:
            mark = "matches" if step.matched else "doesn't match"
            out.line(f"rule {step.index + 1} {mark}: {render(step.why)}")
        out.line(f"→ profile {trace.profile_id}")
        out.document("profiles", trace, ok=True)
        return EXIT_OK
    for p in cfg.profiles:
        backend = p.backend if isinstance(p.backend, str) else p.backend.value
        out.line(f"{p.id:16s} {p.name:28s} {backend:10s} {p.model or '':30s} "
                 f"{p.target.kind.value}{' builtin' if p.builtin else ''}")  # fmt: skip
    out.document("profiles", {"profiles": cfg.profiles, "rules": cfg.rules}, ok=True)
    return EXIT_OK


async def cmd_plugins(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    rows = await core.plugins()
    for p in rows:
        out.line(f"{p.name:22s} {p.active_copy:12s} {p.version or ''}")
    out.document("plugins", rows, ok=True)
    return EXIT_OK


async def cmd_models(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    if ns.action != "list":
        await need(core, Feature.MODEL_DOWNLOADS)
    rows = await core.models()
    for m in rows:
        state = "installed" if m.installed else "not installed"
        out.line(f"{m.name:36s} {m.backend.value:10s} {m.kind.value:10s} {state}")
    out.document("models", rows, ok=True)
    return EXIT_OK


async def cmd_clean(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    targets = frozenset(t for t in CleanTarget if getattr(ns, t.value, False))
    if ns.dry_run or not targets:
        rows = await core.storage()
        for e in rows:
            out.line(f"{e.kind.value:10s} {e.bytes or 0:>14,d} bytes  {e.path}")
        if not targets and not ns.dry_run:
            out.line(
                "Nothing removed; choose --engines, --models, --plugins, --downloads or --jobs."
            )
        out.document("clean", rows, ok=True)
        return EXIT_OK
    await need(core, Feature.CLEAN)
    result = await core.clean(targets).result()
    out.line(f"Removed {', '.join(t.value for t in sorted(targets))}.")
    out.document("clean", result, ok=True)
    return EXIT_OK


async def cmd_licence(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    notes = licences.legal_notices()
    out.line(version_text())
    rows = await core.third_party()
    out.line("\nThird-party components:")
    for c in rows:
        conveyed = "shipped" if c.conveyed else "used"
        out.line(f"  {c.name:34s} {c.spdx}  ({conveyed}, {render(c.relation)})")
    out.document("licence", {"notices": notes, "components": rows}, ok=True)
    return EXIT_OK


async def cmd_unavailable(core: ButterEye, ns: argparse.Namespace, out: Out) -> int:
    feature = {"attach": Feature.ATTACH, "detach": Feature.ATTACH}[ns.command]
    if ns.command == "detach" and getattr(ns, "orphans", False):
        feature = Feature.ORPHANS
    await need(core, feature)
    raise ButterEyeError(  # available but not wired into the CLI yet
        ErrorCode.NOT_IMPLEMENTED, Msg("This command isn't in the command-line tool yet.")
    )


COMMANDS: dict[str, Callable[[ButterEye, argparse.Namespace, Out], Awaitable[int]]] = {
    "doctor": cmd_doctor,
    "setup": cmd_setup,
    "play": cmd_play,
    "render": cmd_render,
    "bench": cmd_bench,
    "profiles": cmd_profiles,
    "plugins": cmd_plugins,
    "models": cmd_models,
    "clean": cmd_clean,
    "licence": cmd_licence,
    "attach": cmd_unavailable,
    "detach": cmd_unavailable,
}


# ---------------------------------------------------------------------------
# parser and entry point
# ---------------------------------------------------------------------------


def version_text() -> str:
    n = licences.legal_notices()
    return f"ButterEye {buttereye.__version__}\n{n.copyright}\nLicence: {n.licence_name}"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="one JSON document on stdout")
    common.add_argument("-v", "--verbose", action="store_true", help="more detail")
    common.add_argument("-q", "--quiet", action="store_true", help="only errors")
    p = argparse.ArgumentParser(
        prog="buttereye", description="Smooth motion for mpv: the ButterEye command."
    )
    p.add_argument("--version", action="store_true", help="print the version and exit")
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    d = sub.add_parser("doctor", parents=[common], help="check this computer")
    d.add_argument("--trt", action="store_true", help="include the TensorRT checks")
    d.add_argument("--report", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("setup", parents=[common], help="check, choose an engine and save")
    s.add_argument("--trt-experimental", action="store_true",
                   help="turn on experimental NVIDIA TensorRT")  # fmt: skip
    pl = sub.add_parser("play", parents=[common], help="play a video smoothly in mpv")
    pl.add_argument("file")
    pl.add_argument("--profile")
    r = sub.add_parser("render", parents=[common], help="save a smooth copy of a video")
    r.add_argument("source")
    r.add_argument("-o", "--output")
    r.add_argument("--profile")
    r.add_argument("--encoder")
    r.add_argument("--size", type=parse_size, help="WxH, smaller than the source")
    r.add_argument("--target", type=parse_target, help='"2x" or a frame rate such as 60')
    r.add_argument("--overwrite", action="store_true")
    b = sub.add_parser("bench", parents=[common], help="measure the engines")
    b.add_argument("--width", type=int, default=1920)
    b.add_argument("--height", type=int, default=1080)
    b.add_argument("--fps", type=parse_rate, default=Fraction(24000, 1001))
    b.add_argument("--full", action="store_true")
    b.add_argument("--file", help=argparse.SUPPRESS)
    b.add_argument("--apply", metavar="LABEL", help="save a measured configuration as a profile")
    b.add_argument("--history", action="store_true", help="list earlier results")
    pr = sub.add_parser("profiles", parents=[common], help="list profiles or explain a match")
    pr.add_argument("action", choices=("list", "explain"), nargs="?", default="list")
    pr.add_argument("--fps", type=parse_rate, default=Fraction(24000, 1001))
    pr.add_argument("--width", type=int, default=1920)
    pr.add_argument("--height", type=int, default=1080)
    pr.add_argument("--display-hz", type=float)
    pr.add_argument("--path")
    sub.add_parser("plugins", parents=[common], help="installed plugins").add_argument(
        "action", choices=("list",), nargs="?", default="list"
    )
    m = sub.add_parser("models", parents=[common], help="installed models")
    m.add_argument("action", choices=("list", "add", "remove", "install"), nargs="?",
                   default="list")  # fmt: skip
    m.add_argument("name", nargs="?")
    c = sub.add_parser("clean", parents=[common], help="free disk space")
    for t in CleanTarget:
        c.add_argument(f"--{t.value}", action="store_true")
    c.add_argument("--dry-run", action="store_true", help="only list what is stored")
    sub.add_parser("licence", parents=[common], help="licences and legal notices")
    a = sub.add_parser("attach", parents=[common], help="manage an mpv you started")
    a.add_argument("--socket")
    de = sub.add_parser("detach", parents=[common], help="stop managing an mpv")
    de.add_argument("--socket")
    de.add_argument("--orphans", action="store_true")
    de.add_argument("--disable", action="store_true")
    return p


async def run(ns: argparse.Namespace, out: Out, opener: Opener) -> int:
    core = await opener(None)
    policy_cancel = ns.command == "render"
    try:
        if getattr(ns, "report", False):
            await need(core, Feature.REPORT)
        return await COMMANDS[ns.command](core, ns, out)
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=policy_cancel, timeout_s=5.0)


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Opener | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = build_parser()
    try:
        ns = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # argparse: --help (0) or a usage error (2)
        return int(exc.code or 0)
    out = Out(ns, stdout or sys.stdout, stderr or sys.stderr)
    if ns.version:
        print(version_text(), file=out.stdout)
        return EXIT_OK
    if ns.command is None:
        parser.print_help(out.stdout)
        return EXIT_USAGE
    open_core: Opener = opener if opener is not None else ButterEye.open

    async def guarded() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        if task is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(signal.SIGINT, task.cancel)
                loop.add_signal_handler(signal.SIGTERM, task.cancel)
        return await run(ns, out, open_core)

    try:
        return asyncio.run(guarded())
    except asyncio.CancelledError, KeyboardInterrupt, OperationCancelled:
        if out.json:
            out.document(ns.command, None, ok=False, error={"code": "cancelled"})
        return EXIT_CANCELLED
    except ButterEyeError as err:
        return out.error(ns.command, err)


__all__ = ["EXIT_CANCELLED", "JSON_VERSION", "build_parser", "main", "plain", "version_text"]
