# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""First-run setup (SCOPE §4.7, F0).

``plan`` is computed in memory from a doctor report. ``apply`` runs the smoke
test (vspipe on ``smoke.vpy`` + the in-mpv probe) and writes ``config.toml``
**last**, through ``profiles.config.save``. If the user cancels at any step,
nothing has been written except the setup log under
``$XDG_STATE_HOME/buttereye/logs`` (plus the probe's short-lived file in the
0700 runtime directory, which is removed at once). Re-running is idempotent.

Providers (GUI.md §1)::

    async plan(paths, report, *, trt_experimental) -> SetupPlan
    async apply(paths, plan, choices, progress) -> SetupResult
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from buttereye.core.backends.select import backends, chosen_device, select
from buttereye.core.capabilities import ProviderRef, absent_state, resolve
from buttereye.core.doctor.probe import run_probe
from buttereye.core.doctor.smoke import smoke_test
from buttereye.core.doctor.text import M
from buttereye.core.errors import (
    BlockingIssue,
    ButterEyeError,
    ConfigError,
    DependencyMissing,
    ErrorCode,
    NotAvailable,
)
from buttereye.core.events import Progress
from buttereye.core.ops import ProgressSink
from buttereye.core.paths import ensure_logs_dir
from buttereye.core.types import (
    BackendId,
    Config,
    ConfigLoad,
    DoctorReport,
    Feature,
    Finding,
    Msg,
    OpId,
    Paths,
    Section,
    SetupChoices,
    SetupPlan,
    SetupResult,
    Severity,
)

_CFG_LOAD = ProviderRef("buttereye.core.profiles.config", "load")
_CFG_SAVE = ProviderRef("buttereye.core.profiles.config", "save")
_DEFAULTS = ProviderRef("buttereye.core.profiles.defaults", "default_config")
_DNF_INSTALL = re.compile(r"^sudo dnf install (.+)$")
STEPS = 4


def _need(ref: ProviderRef) -> Callable[..., Any]:
    fn = resolve(ref)
    if fn is None:
        raise NotAvailable.for_state(Feature.CONFIG, absent_state(Feature.CONFIG))
    return fn


def _load(paths: Paths) -> ConfigLoad:
    load = _need(_CFG_LOAD)
    result = load(paths)
    assert isinstance(result, ConfigLoad)
    return result


def base_config(paths: Paths) -> tuple[Config, str, ConfigLoad]:
    """The config to build on: the existing file when it is valid, else defaults."""
    loaded = _load(paths)
    if loaded.exists and not loaded.used_defaults:
        return loaded.config, loaded.revision, loaded
    default = _need(_DEFAULTS)
    cfg = default()
    assert isinstance(cfg, Config)
    return cfg, loaded.revision, loaded


def _with_trt(cfg: Config, on: bool) -> Config:
    return dataclasses.replace(cfg, general=dataclasses.replace(cfg.general, trt_experimental=on))


def _problem(f: Finding) -> bool:
    return f.severity in (Severity.BLOCKING, Severity.DEGRADED)


def dnf_lines(report: DoctorReport, *, trt: bool) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(missing package names, exact command lines) from the report's findings."""
    lines: list[str] = []
    pkgs: list[str] = []
    for f in report.findings:
        if not _problem(f) or (f.section is Section.TRT and not trt):
            continue
        for c in f.commands:
            if not c.startswith("sudo dnf"):
                continue
            if c not in lines:
                lines.append(c)
            m = _DNF_INSTALL.match(c)
            if m:
                for p in m.group(1).split():
                    if p not in pkgs:
                        pkgs.append(p)
    # "copr enable" must come before the installs it makes possible.
    lines.sort(key=lambda c: 0 if " copr enable " in c else 1)
    return tuple(pkgs), tuple(lines)


async def plan(paths: Paths, report: DoctorReport, *, trt_experimental: bool) -> SetupPlan:
    """In memory only; reads config.toml but never writes."""
    cfg, _, _ = base_config(paths)
    cfg = _with_trt(cfg, trt_experimental)
    statuses = backends(report, cfg)
    proposed = select(report, cfg, ())
    missing, lines = dnf_lines(report, trt=trt_experimental)
    return SetupPlan(
        report=report,
        proposed=proposed,
        backends=statuses,
        missing_packages=missing,
        dnf_lines=lines,
        downloads=(),  # this release lists no downloadable models (GUI.md §5.2)
    )


def include_line(paths: Paths) -> str:
    home = os.path.expanduser("~")
    try:
        rel = paths.mpv_include.relative_to(home)
        shown = "~/" + rel.as_posix()
    except ValueError:
        shown = os.fspath(paths.mpv_include)
    return "include=" + shown


class _Log:
    """The setup log (the only file setup writes before its last step)."""

    def __init__(self, paths: Paths) -> None:
        ensure_logs_dir(paths)
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = paths.logs_dir / f"setup-{stamp}-{os.getpid()}.log"
        self._fh: TextIO = self.path.open("a", encoding="utf-8")

    def write(self, *lines: str) -> None:
        for ln in lines:
            self._fh.write(ln.rstrip("\n") + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()


def _step(progress: ProgressSink, n: int, phase: Msg) -> None:
    progress(Progress(OpId(""), phase, n, STEPS, "steps", None, None))


async def apply(
    paths: Paths, plan: SetupPlan, choices: SetupChoices, progress: ProgressSink
) -> SetupResult:
    report = plan.report
    if report.blocking:
        blocking = tuple(f for f in report.findings if f.severity is Severity.BLOCKING)
        raise BlockingIssue(
            blocking[0].code or ErrorCode.INTERNAL,
            M(
                "Setup isn't finished.\n{n} problem(s) need fixing on the System page.",
                {"n": len(blocking)},
            ),
            findings=blocking,
        )
    status = next((s for s in plan.backends if s.id is choices.backend), None)
    if status is None or not status.available:
        raise DependencyMissing(
            status.code if status is not None and status.code else ErrorCode.PKG_MISSING,
            status.reason if status is not None else M("Unknown backend."),
            M("Pick an available backend, or install what is missing and run setup again."),
            commands=plan.dnf_lines,
        )
    offered = {d.name for d in plan.downloads}
    if not choices.confirmed_downloads <= offered:
        raise ButterEyeError(
            ErrorCode.INTERNAL,
            M("A confirmed download is not part of the setup plan."),
            detail=", ".join(sorted(choices.confirmed_downloads - offered)),
        )
    if choices.confirmed_downloads:  # pragma: no cover - plan.downloads is empty today
        raise NotAvailable.for_state(Feature.MODEL_DOWNLOADS, absent_state(Feature.MODEL_DOWNLOADS))

    log = _Log(paths)
    try:
        log.write(
            f"ButterEye setup: backend={choices.backend.value} "
            f"trt_experimental={choices.trt_experimental}"
        )
        _step(progress, 1, M("Checking your choices"))

        smoke_ok = True
        smoke_fps: float | None = None
        smoke_finding: Finding | None = None
        if choices.run_smoke_test:
            model = plan.proposed.model if choices.backend is plan.proposed.backend else None
            if choices.backend is BackendId.RIFE_NCNN and model is None:
                cfg_pick, _, _ = base_config(paths)
                cfg_pick = dataclasses.replace(
                    cfg_pick,
                    general=dataclasses.replace(
                        cfg_pick.general, backend_override=BackendId.RIFE_NCNN
                    ),
                )
                model = select(report, cfg_pick, ()).model
            dev = chosen_device(report)
            _step(progress, 2, M("Smoke test: interpolating a generated clip with vspipe"))
            outcome = await smoke_test(
                paths, choices.backend, model=model, gpu_index=dev.index if dev else 0
            )
            log.write(*outcome.log)
            smoke_ok, smoke_fps, smoke_finding = outcome.ok, outcome.fps, outcome.finding

            _step(progress, 3, M("Smoke test: loading the plugins inside mpv"))
            mpv = shutil.which("mpv")
            if mpv is None:
                smoke_ok = False
                smoke_finding = smoke_finding or Finding(
                    "setup.probe",
                    Section.PROBE,
                    Severity.BLOCKING,
                    ErrorCode.MPV_NOT_FOUND,
                    M("mpv is not installed"),
                    M("The in-mpv probe needs mpv."),
                    M("Install the distro mpv package."),
                    ("sudo dnf install mpv",),
                )
            else:
                probe = await run_probe(paths, mpv, trt=choices.backend is BackendId.RIFE_TRT)
                ns = {
                    BackendId.RIFE_NCNN: "rife",
                    BackendId.MVTOOLS: "mv",
                    BackendId.RIFE_TRT: "trt",
                }[choices.backend]
                rec = None
                if probe.raw is not None:
                    rec = next(
                        (
                            r
                            for r in probe.raw.get("plugins", [])
                            if isinstance(r, dict) and r.get("ns") == ns
                        ),
                        None,
                    )
                if rec is None or not rec.get("loaded"):
                    smoke_ok = False
                    why = probe.error or (str(rec.get("error")) if rec else "not loaded")
                    log.write(f"in-mpv probe: {ns} failed: {why}")
                    smoke_finding = smoke_finding or Finding(
                        "setup.probe",
                        Section.PROBE,
                        Severity.DEGRADED,
                        ErrorCode.PROBE_FAILED,
                        M("The plugin did not load inside mpv"),
                        M("mpv could not load the {ns} plugin: {why}.", {"ns": ns, "why": why}),
                        M("Run the checks again and see the In-mpv probe section."),
                        (),
                        probe.cmd.tail() if probe.cmd else (),
                    )
                else:
                    log.write(f"in-mpv probe: {ns} loaded from {rec.get('loaded_from')}")
        else:
            log.write("smoke test skipped by the user")

        # ---- last step: write config.toml (no await after this point) ----
        _step(progress, 4, M("Saving your settings"))
        base, revision, loaded = base_config(paths)
        if loaded.read_only:
            raise ConfigError(
                ErrorCode.CONFIG_NEWER_SCHEMA,
                M("config.toml was created by a newer ButterEye and is read-only."),
                M("Update ButterEye, or edit the file by hand."),
            )
        override = None if choices.backend is plan.proposed.backend else choices.backend
        new = dataclasses.replace(
            base,
            general=dataclasses.replace(
                base.general,
                trt_experimental=choices.trt_experimental,
                backend_override=override,
            ),
        )
        save = _need(_CFG_SAVE)
        new_rev = save(paths, new, expected_revision=revision)
        assert isinstance(new_rev, str)
        log.write(
            f"config written: {paths.config_file} (revision {new_rev[:12]})",
            f"smoke_ok={smoke_ok} fps={smoke_fps}",
        )
        return SetupResult(
            config_revision=new_rev,
            smoke_ok=smoke_ok,
            smoke_fps=smoke_fps,
            smoke_finding=smoke_finding,
            include_line=include_line(paths),
        )
    except BaseException as exc:
        log.write(f"setup stopped: {type(exc).__name__}: {exc}")
        raise
    finally:
        log.close()


def log_files(paths: Paths) -> tuple[Path, ...]:
    try:
        return tuple(sorted(paths.logs_dir.glob("setup-*.log")))
    except OSError:
        return ()


__all__ = ["plan", "apply", "base_config", "dnf_lines", "include_line", "log_files", "STEPS"]
