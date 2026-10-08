# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The core facade (docs/design/GUI.md §2.5, FROZEN) and the provider context (§11).

``import buttereye.core.api`` imports no subsystem module. Every facade method
resolves its provider lazily through ``capabilities.resolve``; a missing module
or attribute raises ``NotAvailable`` before any I/O.

All ``async def`` and ``Operation``-returning methods run **on the core loop
only**. Module-level helpers marked *pure* are thread-safe and do no I/O beyond
importing their provider.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from buttereye.core import capabilities as _caps
from buttereye.core import errors as _errors_mod
from buttereye.core import events as _events_mod
from buttereye.core import ops as _ops_mod
from buttereye.core import types as _types_mod
from buttereye.core.capabilities import ProviderRef, unavailable_text
from buttereye.core.commands import CLI_MISSING_NOTE, cli_available, command_hint
from buttereye.core.errors import *  # noqa: F403
from buttereye.core.errors import (
    ButterEyeError,
    ConfigError,
    ErrorCode,
    NotAvailable,
)
from buttereye.core.events import *  # noqa: F403
from buttereye.core.events import (
    CapabilitiesChanged,
    ConfigChanged,
    Event,
    EventBus,
)
from buttereye.core.i18n import render, set_language
from buttereye.core.ops import *  # noqa: F403
from buttereye.core.ops import OpContext, Operation
from buttereye.core.types import *  # noqa: F403
from buttereye.core.types import (
    BackendStatus,
    BenchRequest,
    BenchResult,
    CandidateId,
    Capabilities,
    CleanResult,
    CleanTarget,
    ComponentLicence,
    Config,
    ConfigIssue,
    ConfigLoad,
    DetachPolicy,
    DoctorReport,
    DownloadItem,
    Feature,
    HardwareInfo,
    InstanceCandidate,
    JobId,
    LegalNotices,
    ModelEntry,
    Msg,
    OpId,
    OpKind,
    Paths,
    PluginStatus,
    Profile,
    RenderJobSpec,
    RenderJobState,
    RenderProbe,
    RuleTrace,
    Selection,
    SessionId,
    SessionSnapshot,
    SetupChoices,
    SetupPlan,
    SetupResult,
    ShutdownReport,
    SourceFacts,
    StaleJob,
    StorageEntry,
)

_log = logging.getLogger(__name__)

T = TypeVar("T")
S = TypeVar("S")

# ---------------------------------------------------------------------------
# Provider references (one per facade method; see GUI.md §1 and §11)
# ---------------------------------------------------------------------------

_P = "buttereye.core."


def _ref(module: str, attr: str) -> ProviderRef:
    return ProviderRef(_P + module, attr)


PATHS_RESOLVE = _ref("paths", "resolve")
CFG_LOAD = _ref("profiles.config", "load")
CFG_SAVE = _ref("profiles.config", "save")
RULES_VALIDATE = _ref("profiles.rules", "validate")
RULES_EXPLAIN = _ref("profiles.rules", "explain")
DEF_BUILTINS = _ref("profiles.defaults", "builtin_profiles")
DEF_CONFIG = _ref("profiles.defaults", "default_config")
HW = _ref("hw.detect", "hardware")
DOCTOR = _ref("doctor.run", "doctor")
PLUGINS = _ref("doctor.run", "plugins")
BACKENDS = _ref("backends.select", "backends")
SELECT = _ref("backends.select", "select")
SETUP_PLAN = _ref("setup", "plan")
SETUP_APPLY = _ref("setup", "apply")
STORAGE_ENTRIES = _ref("storage", "entries")
STORAGE_CLEAN = _ref("storage", "clean")
STORAGE_MODELS = _ref("storage", "models")
LIC_NOTICES = _ref("licences", "legal_notices")
LIC_THIRD = _ref("licences", "third_party")
REPORT_BUNDLE = _ref("doctor.report", "bundle")
INCLUDE_ADD = _ref("mpvctl.include", "add_include")
LIVE_PLAY = _ref("mpvctl.session", "play")
LIVE_SESSIONS = _ref("mpvctl.session", "sessions")
LIVE_SET_INTERP = _ref("mpvctl.session", "set_interpolation")
LIVE_DETACH = _ref("mpvctl.session", "detach")
LIVE_APPLY = _ref("mpvctl.session", "apply_profile")
LIVE_STEP_DOWN = _ref("mpvctl.session", "step_down")
LIVE_DISCOVER = _ref("mpvctl.session", "discover")
LIVE_CLOSE = _ref("mpvctl.session", "close")
ATTACH = _ref("mpvctl.attach", "attach")
ORPHAN_REMOVE = _ref("mpvctl.orphans", "remove_orphan_filter")
BENCH_RUN = _ref("bench.runner", "bench")
BENCH_HISTORY = _ref("bench.runner", "history")
BENCH_APPLY = _ref("bench.runner", "apply")
RENDER_PROBE = _ref("render.jobs", "probe")
RENDER_ENQUEUE = _ref("render.jobs", "enqueue")
RENDER_JOBS = _ref("render.jobs", "jobs")
RENDER_MOVE = _ref("render.jobs", "move")
RENDER_CANCEL = _ref("render.jobs", "cancel")
RENDER_FORGET = _ref("render.jobs", "forget")
RENDER_STALE = _ref("render.jobs", "stale_jobs")
RENDER_STALE_STOP = _ref("render.jobs", "stale_stop")
RENDER_SHUTDOWN = _ref("render.jobs", "shutdown")
DL_LISTED = _ref("plugins.download", "listed")
DL_INSTALL = _ref("plugins.download", "install")
MODEL_INSTALL_FILE = _ref("plugins.install", "install_file")
MODEL_REMOVE = _ref("plugins.install", "remove")

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:/")  # Path() collapses "//"
_OPEN_TOKEN = object()


def _provider(feature: Feature, ref: ProviderRef) -> Callable[..., Any]:
    fn = _caps.resolve(ref)
    if fn is None:
        raise NotAvailable.for_state(feature, _caps.absent_state(feature))
    return fn


def _tilde(path: Path) -> str:
    home = os.path.expanduser("~")
    try:
        rel = path.relative_to(home)
    except ValueError:
        return os.fspath(path)
    return "~/" + rel.as_posix()


def _closed_error() -> ButterEyeError:
    return ButterEyeError(ErrorCode.INTERNAL, Msg("ButterEye's engine has shut down."))


# ---------------------------------------------------------------------------
# Provider context (GUI.md §11)
# ---------------------------------------------------------------------------


class ProviderContext:
    """What a later-milestone provider (live, bench, render, ...) may use.

    One per ``ButterEye`` instance; loop thread only. Providers keep their own
    per-instance state with ``state(key, factory)``, never in module globals.
    """

    __slots__ = ("_core",)

    def __init__(self, core: ButterEye) -> None:
        self._core = core

    @property
    def paths(self) -> Paths:
        return self._core._paths

    @property
    def closing(self) -> bool:
        """True once ``ButterEye.close()`` has started."""
        return self._core._closing

    def config(self) -> Config:
        """The current in-memory config (defaults when no file exists).

        Raises ``NotAvailable(Feature.CONFIG)`` when the config provider is absent.
        """
        return self._core._current_config()

    def config_load(self) -> ConfigLoad | None:
        """The last ``ConfigLoad`` (None before the first successful load)."""
        return self._core._config_load

    def config_revision(self) -> str:
        load = self._core._config_load
        return load.revision if load is not None else ""

    def last_report(self) -> DoctorReport | None:
        return self._core._report

    def capabilities(self) -> Capabilities:
        """Latest computed capabilities (cached; no provider probing)."""
        return self._core._caps_now()

    def emit(self, ev: Event) -> None:
        """Publish a transition event. Never dropped, delivered in order."""
        self._core._bus.publish(ev)

    def emit_coalesced(self, key: str, ev: Event, *, min_interval_s: float = 0.25) -> None:
        """Publish a counter-only update; at most one per ``min_interval_s`` per
        ``key`` (use the session id), latest wins, droppable under backpressure."""
        self._core._bus.publish_coalesced(key, ev, min_interval_s=min_interval_s)

    def forget_coalesced(self, key: str) -> None:
        """Discard pending coalesced state for ``key`` (call when a session ends)."""
        self._core._bus.forget(key)

    def spawn(self, coro: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
        """Run a background task owned by the core; it is cancelled by ``close()``.

        Exceptions are logged (a background task never dies silently).
        """
        return self._core._spawn(coro, name=name)

    def state(self, key: str, factory: Callable[[], S]) -> S:
        """Per-core-instance singleton for provider state (created on first use)."""
        store = self._core._provider_state
        if key not in store:
            store[key] = factory()
        return cast(S, store[key])

    async def save_config(self, cfg: Config, *, expected_revision: str) -> str:
        """Save through the facade (emits ``ConfigChanged`` + ``CapabilitiesChanged``)."""
        return await self._core.save_config(cfg, expected_revision=expected_revision)

    def refresh_capabilities(self) -> Capabilities:
        """Recompute capabilities now and emit ``CapabilitiesChanged``."""
        return self._core._refresh_caps()


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


class ButterEye:
    """The single entry point to the core. Create it with ``await ButterEye.open()``."""

    def __init__(self, paths: Paths, *, _token: object = None) -> None:
        if _token is not _OPEN_TOKEN:
            raise TypeError("use 'await ButterEye.open()'")
        self._paths = paths
        self._bus = EventBus()
        self._ctx = ProviderContext(self)
        self._config_load: ConfigLoad | None = None
        self._report: DoctorReport | None = None
        self._caps: Capabilities | None = None
        self._ops: dict[OpId, Operation[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._provider_state: dict[str, object] = {}
        self._closing = False
        self._shutdown: ShutdownReport | None = None

    # ---- lifecycle ----
    @classmethod
    async def open(cls, paths: Paths | None = None) -> ButterEye:
        """Resolve paths, load the config; no writes."""
        if paths is None:
            resolve_paths = _provider(Feature.CONFIG, PATHS_RESOLVE)
            paths = cast(Paths, resolve_paths())
        core = cls(paths, _token=_OPEN_TOKEN)
        if _caps.implemented((CFG_LOAD,)):
            try:
                core._load_config()
            except ButterEyeError as exc:
                _log.warning("config load failed at open: %s", exc)
        core._caps = core._compute_caps()
        return core

    async def close(
        self,
        policy: DetachPolicy = DetachPolicy.KEEP_FILTER,
        *,
        cancel_jobs: bool,
        timeout_s: float = 5.0,
    ) -> ShutdownReport:
        if self._shutdown is not None:
            return self._shutdown
        self._closing = True
        detached: tuple[SessionId, ...] = ()
        failed: tuple[tuple[SessionId, ErrorCode], ...] = ()
        jobs_cancelled: tuple[JobId, ...] = ()

        live_close = _caps.resolve(LIVE_CLOSE)
        if live_close is not None:
            try:
                rep = cast(
                    ShutdownReport,
                    await asyncio.wait_for(
                        live_close(self._ctx, policy, timeout_s=timeout_s), timeout_s + 1.0
                    ),
                )
                detached, failed = rep.detached, rep.failed
            except TimeoutError:
                _log.warning("live sessions did not detach within %.1f s", timeout_s)
                failed = await self._sessions_as_failed()
            except Exception:
                _log.exception("live close failed")
                failed = await self._sessions_as_failed()

        render_shutdown = _caps.resolve(RENDER_SHUTDOWN)
        if render_shutdown is not None:
            try:
                jobs_cancelled = tuple(
                    await asyncio.wait_for(
                        render_shutdown(self._ctx, cancel=cancel_jobs, timeout_s=timeout_s),
                        timeout_s + 1.0,
                    )
                )
            except Exception:
                _log.exception("render shutdown failed")

        ops = [op for op in self._ops.values() if not op.done]
        for op in ops:
            op.cancel()
        if ops:
            waits = [asyncio.ensure_future(op.wait()) for op in ops]
            _done, late = await asyncio.wait(waits, timeout=timeout_s)
            if late:
                # Budget spent (a child ignoring SIGTERM needs the full 5 s grace):
                # SIGKILL what is left so no op child outlives the core, then give
                # the ops a moment to reap and finish their cleanup.
                for op in ops:
                    if not op.done:
                        op.kill_children()
                await asyncio.wait(late, timeout=1.0)

        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1.0)
        # Last resort: no op / process_group child of this core outlives close().
        killed = _ops_mod.kill_owned_groups(asyncio.get_running_loop())
        if killed:
            _log.warning("killed %d leftover process group(s) at close", len(killed))

        self._bus.close()
        self._shutdown = ShutdownReport(detached, failed, jobs_cancelled)
        return self._shutdown

    async def _sessions_as_failed(self) -> tuple[tuple[SessionId, ErrorCode], ...]:
        fn = _caps.resolve(LIVE_SESSIONS)
        if fn is None:
            return ()
        try:
            snaps = cast(tuple[SessionSnapshot, ...], await asyncio.wait_for(fn(self._ctx), 1.0))
        except Exception:
            return ()
        return tuple((s.sid, ErrorCode.IPC_LOST) for s in snaps if not s.ended)

    def subscribe(self, maxlen: int = 4096) -> AsyncIterator[Event]:
        """One per bridge. Ends when the core closes."""
        return self._bus.subscribe(maxlen)

    async def capabilities(self) -> Capabilities:
        self._caps = self._compute_caps()
        return self._caps

    async def ping(self) -> float:
        """Loop liveness; returns ``time.monotonic()``."""
        return time.monotonic()

    def paths(self) -> Paths:
        return self._paths

    # ---- internals ----
    def _check_open(self) -> None:
        if self._closing:
            raise _closed_error()

    def _need(self, feature: Feature, ref: ProviderRef) -> Callable[..., Any]:
        self._check_open()
        return _provider(feature, ref)

    def _compute_caps(self) -> Capabilities:
        cfg = self._config_load.config if self._config_load is not None else None
        return _caps.compute(cfg, self._report)

    def _caps_now(self) -> Capabilities:
        if self._caps is None:
            self._caps = self._compute_caps()
        return self._caps

    def _refresh_caps(self) -> Capabilities:
        self._caps = self._compute_caps()
        self._bus.publish(CapabilitiesChanged(self._caps))
        return self._caps

    def _load_config(self) -> ConfigLoad:
        load = _provider(Feature.CONFIG, CFG_LOAD)
        result = cast(ConfigLoad, load(self._paths))
        self._config_load = result
        return result

    def _current_config(self) -> Config:
        if self._config_load is not None:
            return self._config_load.config
        if _caps.resolve(CFG_LOAD) is not None:
            return self._load_config().config
        default = _provider(Feature.CONFIG, DEF_CONFIG)
        return cast(Config, default())

    def _spawn(self, coro: Coroutine[Any, Any, T], *, name: str) -> asyncio.Task[T]:
        if self._closing:
            coro.close()
            raise _closed_error()
        task = asyncio.get_running_loop().create_task(coro, name=f"buttereye-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error("background task %s failed", task.get_name(), exc_info=task.exception())

    def _op(self, kind: OpKind, body: Callable[[OpContext], Awaitable[T]]) -> Operation[T]:
        op: Operation[T] = Operation(kind, body, emit=self._bus.publish)
        self._ops[op.id] = op
        op_id = op.id

        async def _forget() -> None:
            await op.wait()
            self._ops.pop(op_id, None)

        task = asyncio.get_running_loop().create_task(_forget())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return op

    # ---- config / profiles / rules (§4.9, F15)                     Feature.CONFIG ----
    async def load_config(self) -> ConfigLoad:
        self._check_open()
        result = self._load_config()
        self._caps = self._compute_caps()
        return result

    async def save_config(self, cfg: Config, *, expected_revision: str) -> str:
        save = self._need(Feature.CONFIG, CFG_SAVE)
        if self._config_load is not None and self._config_load.read_only:
            raise ConfigError(
                ErrorCode.CONFIG_NEWER_SCHEMA,
                Msg("config.toml was created by a newer ButterEye and is read-only."),
                Msg("Update ButterEye, or edit the file by hand."),
            )
        revision = cast(str, save(self._paths, cfg, expected_revision=expected_revision))
        try:
            self._load_config()
        except ButterEyeError as exc:  # pragma: no cover - file vanished after save
            _log.warning("reload after save failed: %s", exc)
        self._bus.publish(ConfigChanged(revision))
        self._refresh_caps()
        return revision

    # ---- doctor / setup / hardware (F0, F1, F9, F13) ----
    def doctor(self, *, trt: bool | None = None) -> Operation[DoctorReport]:
        """``trt=None`` follows the config opt-in."""
        run = self._need(Feature.DOCTOR, DOCTOR)
        cfg = self._current_config()
        use_trt = cfg.general.trt_experimental if trt is None else trt

        async def body(op: OpContext) -> DoctorReport:
            report = cast(
                DoctorReport, await run(self._paths, cfg, trt=use_trt, progress=op.progress)
            )
            self._report = report
            self._refresh_caps()
            return report

        return self._op(OpKind.DOCTOR, body)

    async def last_report(self) -> DoctorReport | None:
        return self._report

    async def hardware(self) -> HardwareInfo:
        fn = self._need(Feature.HARDWARE, HW)
        return cast(HardwareInfo, await fn(self._paths))

    async def plugins(self) -> tuple[PluginStatus, ...]:
        fn = self._need(Feature.DOCTOR, PLUGINS)
        return tuple(await fn(self._paths))

    async def backends(self, report: DoctorReport) -> tuple[BackendStatus, ...]:
        fn = self._need(Feature.SETUP, BACKENDS)
        return tuple(fn(report, self._current_config()))

    async def select_backend(self, report: DoctorReport) -> Selection:
        fn = self._need(Feature.SETUP, SELECT)
        bench: tuple[BenchResult, ...] = ()
        history = _caps.resolve(BENCH_HISTORY)
        if history is not None:
            try:
                bench = tuple(await history(self._ctx))
            except ButterEyeError as exc:
                _log.warning("bench history unavailable for selection: %s", exc)
        return cast(Selection, fn(report, self._current_config(), bench))

    async def set_trt_experimental(self, on: bool, *, expected_revision: str) -> str:
        self._need(Feature.CONFIG, CFG_SAVE)
        cfg = self._current_config()
        new = dataclasses.replace(
            cfg, general=dataclasses.replace(cfg.general, trt_experimental=on)
        )
        return await self.save_config(new, expected_revision=expected_revision)

    async def setup_plan(self, report: DoctorReport, *, trt_experimental: bool) -> SetupPlan:
        fn = self._need(Feature.SETUP, SETUP_PLAN)
        return cast(SetupPlan, await fn(self._paths, report, trt_experimental=trt_experimental))

    def setup_apply(self, plan: SetupPlan, choices: SetupChoices) -> Operation[SetupResult]:
        """Config is written last (by the provider)."""
        fn = self._need(Feature.SETUP, SETUP_APPLY)

        async def body(op: OpContext) -> SetupResult:
            result = cast(SetupResult, await fn(self._paths, plan, choices, op.progress))
            if _caps.resolve(CFG_LOAD) is not None:
                self._load_config()
            self._bus.publish(ConfigChanged(result.config_revision))
            self._refresh_caps()
            return result

        return self._op(OpKind.SETUP, body)

    async def include_line(self) -> str:
        return "include=" + _tilde(self._paths.mpv_include)

    async def add_include_to_mpv_conf(self, *, consent: Literal[True]) -> str:
        """Returns the diff applied; idempotent. Needs explicit consent (§4.6)."""
        fn = self._need(Feature.SETUP, INCLUDE_ADD)
        if consent is not True:
            raise ValueError("consent must be True")
        return cast(str, await fn(self._ctx))

    def report_bundle(self, dest: Path, *, redact: bool = True) -> Operation[Path]:
        fn = self._need(Feature.REPORT, REPORT_BUNDLE)

        async def body(op: OpContext) -> Path:
            return cast(Path, await fn(self._ctx, dest, redact=redact, op=op))

        return self._op(OpKind.REPORT, body)

    # ---- live (F2-F4, F8, §4.3, §4.10, §4.11) ----
    async def discover(self) -> tuple[InstanceCandidate, ...]:
        """Refused candidates are included, with their reason."""
        fn = self._need(Feature.DISCOVER, LIVE_DISCOVER)
        return tuple(await fn(self._ctx))

    def play(self, file: Path, *, profile_id: str | None = None) -> Operation[SessionId]:
        fn = self._need(Feature.LIVE, LIVE_PLAY)
        if _URL.match(os.fspath(file)):
            raise ButterEyeError(
                ErrorCode.FILE_NOT_LOCAL,
                Msg("Only local files can be opened. Streaming isn't supported."),
            )

        async def body(op: OpContext) -> SessionId:
            return cast(SessionId, await fn(self._ctx, file, profile_id=profile_id, op=op))

        return self._op(OpKind.PLAY, body)

    async def attach(self, cid: CandidateId, *, profile_id: str | None = None) -> SessionId:
        fn = self._need(Feature.ATTACH, ATTACH)
        return cast(SessionId, await fn(self._ctx, cid, profile_id=profile_id))

    async def detach(self, sid: SessionId, policy: DetachPolicy = DetachPolicy.KEEP_FILTER) -> None:
        fn = self._need(Feature.LIVE, LIVE_DETACH)
        await fn(self._ctx, sid, policy)

    async def set_interpolation(
        self, sid: SessionId, enabled: bool, *, force: bool = False
    ) -> SessionSnapshot:
        """``force``: smooth even when the speed test says it can't keep up."""
        fn = self._need(Feature.LIVE, LIVE_SET_INTERP)
        if force:
            return cast(SessionSnapshot, await fn(self._ctx, sid, enabled, force=True))
        return cast(SessionSnapshot, await fn(self._ctx, sid, enabled))

    async def apply_profile(self, sid: SessionId, profile_id: str | None) -> SessionSnapshot:
        """``None`` = rules; gen = max(seen)+1, verified."""
        fn = self._need(Feature.LIVE, LIVE_APPLY)
        return cast(SessionSnapshot, await fn(self._ctx, sid, profile_id))

    async def step_down(self, sid: SessionId) -> SessionSnapshot:
        fn = self._need(Feature.LIVE, LIVE_STEP_DOWN)
        return cast(SessionSnapshot, await fn(self._ctx, sid))

    async def remove_orphan_filter(self, cid: CandidateId) -> None:
        fn = self._need(Feature.ORPHANS, ORPHAN_REMOVE)
        await fn(self._ctx, cid)

    async def sessions(self) -> tuple[SessionSnapshot, ...]:
        fn = self._need(Feature.LIVE, LIVE_SESSIONS)
        return tuple(await fn(self._ctx))

    # ---- bench (F14)                                                 Feature.BENCH ----
    def bench(self, req: BenchRequest) -> Operation[BenchResult]:
        fn = self._need(Feature.BENCH, BENCH_RUN)

        async def body(op: OpContext) -> BenchResult:
            return cast(BenchResult, await fn(self._ctx, req, op=op))

        return self._op(OpKind.BENCH, body)

    async def bench_history(self) -> tuple[BenchResult, ...]:
        fn = self._need(Feature.BENCH, BENCH_HISTORY)
        return tuple(await fn(self._ctx))

    async def apply_bench(self, result: BenchResult, label: str, *, expected_revision: str) -> str:
        fn = self._need(Feature.BENCH, BENCH_APPLY)
        return cast(str, await fn(self._ctx, result, label, expected_revision=expected_revision))

    # ---- render (§7, F16)                                           Feature.RENDER ----
    async def render_probe(self, src: Path, *, profile_id: str | None = None) -> RenderProbe:
        """``profile_id`` picks the engine the size estimates are for (§7.8)."""
        fn = self._need(Feature.RENDER, RENDER_PROBE)
        return cast(RenderProbe, await fn(self._ctx, src, profile_id=profile_id))

    async def render_enqueue(self, spec: RenderJobSpec) -> JobId:
        """Sequential queue, concurrency 1."""
        fn = self._need(Feature.RENDER, RENDER_ENQUEUE)
        return cast(JobId, await fn(self._ctx, spec))

    async def render_jobs(self) -> tuple[RenderJobState, ...]:
        fn = self._need(Feature.RENDER, RENDER_JOBS)
        return tuple(await fn(self._ctx))

    async def render_move(self, job: JobId, delta: int) -> None:
        """Queued jobs only."""
        fn = self._need(Feature.RENDER, RENDER_MOVE)
        await fn(self._ctx, job, delta)

    async def render_cancel(self, job: JobId) -> None:
        fn = self._need(Feature.RENDER, RENDER_CANCEL)
        await fn(self._ctx, job)

    async def render_forget(self, job: JobId) -> None:
        """Finished/failed/cancelled jobs only."""
        fn = self._need(Feature.RENDER, RENDER_FORGET)
        await fn(self._ctx, job)

    async def stale_jobs(self) -> tuple[StaleJob, ...]:
        fn = self._need(Feature.RENDER, RENDER_STALE)
        return tuple(await fn(self._ctx))

    async def stale_job_stop(self, job: JobId) -> None:
        """SIGTERM the job's process group, delete partials."""
        fn = self._need(Feature.RENDER, RENDER_STALE_STOP)
        await fn(self._ctx, job)

    # ---- storage / models / licences (§4.6, F10, F18) ----
    async def storage(self) -> tuple[StorageEntry, ...]:
        fn = self._need(Feature.STORAGE, STORAGE_ENTRIES)
        return tuple(await fn(self._paths))

    def clean(self, targets: frozenset[CleanTarget]) -> Operation[CleanResult]:
        """Never touches RPM-owned files."""
        fn = self._need(Feature.CLEAN, STORAGE_CLEAN)

        async def body(op: OpContext) -> CleanResult:
            return cast(CleanResult, await fn(self._paths, frozenset(targets), op.progress))

        return self._op(OpKind.CLEAN, body)

    async def models(self) -> tuple[ModelEntry, ...]:
        fn = self._need(Feature.MODELS, STORAGE_MODELS)
        return tuple(await fn(self._paths))

    async def model_downloads(self) -> tuple[DownloadItem, ...]:
        fn = self._need(Feature.MODEL_DOWNLOADS, DL_LISTED)
        return tuple(await fn(self._ctx))

    def model_install(self, name: str) -> Operation[ModelEntry]:
        fn = self._need(Feature.MODEL_DOWNLOADS, DL_INSTALL)

        async def body(op: OpContext) -> ModelEntry:
            return cast(ModelEntry, await fn(self._ctx, name, op=op))

        return self._op(OpKind.MODEL_INSTALL, body)

    def model_install_file(self, file: Path, sha256: str | None) -> Operation[ModelEntry]:
        fn = self._need(Feature.MODELS, MODEL_INSTALL_FILE)

        async def body(op: OpContext) -> ModelEntry:
            return cast(ModelEntry, await fn(self._ctx, file, sha256, op=op))

        return self._op(OpKind.MODEL_INSTALL, body)

    async def model_remove(self, name: str) -> None:
        fn = self._need(Feature.MODELS, MODEL_REMOVE)
        await fn(self._ctx, name)

    async def third_party(self) -> tuple[ComponentLicence, ...]:
        fn = self._need(Feature.LICENCES, LIC_THIRD)
        return tuple(await fn(self._paths))


# ---------------------------------------------------------------------------
# Module-level pure helpers (GUI thread allowed)
# ---------------------------------------------------------------------------


def validate_config(cfg: Config) -> tuple[ConfigIssue, ...]:
    """Pure. Raises ``NotAvailable(Feature.CONFIG)`` without the provider."""
    return tuple(_provider(Feature.CONFIG, RULES_VALIDATE)(cfg))


def explain_rules(cfg: Config, facts: SourceFacts) -> RuleTrace:
    """Pure. First-match trace (§4.9)."""
    return cast(RuleTrace, _provider(Feature.CONFIG, RULES_EXPLAIN)(cfg, facts))


def builtin_profiles() -> tuple[Profile, ...]:
    """Pure. The four shipped profiles."""
    return tuple(_provider(Feature.CONFIG, DEF_BUILTINS)())


def legal_notices() -> LegalNotices:
    """Pure. ButterEye's own notices (F18)."""
    return cast(LegalNotices, _provider(Feature.LICENCES, LIC_NOTICES)())


__all__ = [
    "ButterEye",
    "ProviderContext",
    "render",
    "set_language",
    "validate_config",
    "explain_rules",
    "builtin_profiles",
    "legal_notices",
    "command_hint",
    "cli_available",
    "CLI_MISSING_NOTE",
    "unavailable_text",
]
__all__ += _types_mod.__all__
__all__ += _errors_mod.__all__
__all__ += _events_mod.__all__
__all__ += _ops_mod.__all__
