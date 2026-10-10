# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""FakeCore: the ``ButterEye`` surface, answered from a scripted ``Scenario``.

TEST AND DEVELOPMENT ONLY. It never spawns processes, never touches the network
and never reads or writes files. Public members match ``ButterEye`` exactly
(``tests/core/test_fakecore_contract.py``); the only extras are ``fake_*`` hooks
for tests.

    core = await FakeCore.for_scenario("live_active").open()
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import itertools
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal, TypeVar

from buttereye import __version__
from buttereye.core import api as _api
from buttereye.core.api import ButterEye
from buttereye.core.errors import (
    ButterEyeError,
    ConfigError,
    ErrorCode,
    InstanceRefused,
    NotAvailable,
)
from buttereye.core.events import (
    CapabilitiesChanged,
    ConfigChanged,
    Event,
    JobChanged,
    SessionAdded,
    SessionChanged,
    SessionEnded,
)
from buttereye.core.ops import OpContext, Operation
from buttereye.core.testing import scenarios as _sc
from buttereye.core.testing.scenarios import Scenario
from buttereye.core.types import (
    BackendId,
    BackendStatus,
    BenchRequest,
    BenchResult,
    CandidateId,
    Capabilities,
    CapState,
    CleanResult,
    CleanTarget,
    ComponentLicence,
    Config,
    ConfigLoad,
    DetachPolicy,
    DoctorReport,
    DownloadItem,
    Feature,
    FilterState,
    HardwareInfo,
    Health,
    InstanceCandidate,
    JobId,
    ModelEntry,
    ModelKind,
    Msg,
    OpKind,
    OpState,
    Origin,
    Paths,
    PluginStatus,
    Reason,
    RenderJobSpec,
    RenderJobState,
    RenderPhase,
    RenderProbe,
    Selection,
    SessionId,
    SessionSnapshot,
    SetupChoices,
    SetupPlan,
    SetupResult,
    ShutdownReport,
    StaleJob,
    StorageEntry,
)

T = TypeVar("T")

_URL = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]+:/")  # Path() collapses "//"
_GATING = frozenset({Reason.NOT_IMPLEMENTED, Reason.NOTHING_LISTED})


@dataclass(frozen=True, slots=True)
class FakeCall:
    name: str
    args: tuple[object, ...]
    kwargs: dict[str, object]


class FakeCore(ButterEye):
    """Scripted stand-in for ``ButterEye``; see module docstring."""

    scenario_name: ClassVar[str] = "devbox_now"
    scenario_override: ClassVar[Scenario | None] = None

    def __init__(self, scenario: Scenario, paths: Paths) -> None:
        super().__init__(paths, _token=_api._OPEN_TOKEN)
        self.fake_scenario = scenario
        self.fake_calls: list[FakeCall] = []
        self._states: dict[Feature, CapState] = dict(scenario.capabilities)
        self._load = scenario.config_load
        self._disk_revision = scenario.disk_revision
        self._conflict_seen = False
        self._report = scenario.report
        self._sessions: dict[SessionId, SessionSnapshot] = {s.sid: s for s in scenario.sessions}
        self._candidates: dict[CandidateId, InstanceCandidate] = {
            c.id: c for c in scenario.candidates
        }
        self._bench_history: list[BenchResult] = list(scenario.bench_history)
        self._jobs: dict[JobId, RenderJobState] = {j.id: j for j in scenario.render_jobs}
        self._stale: dict[JobId, StaleJob] = {j.id: j for j in scenario.stale_jobs}
        self._models: dict[str, ModelEntry] = {m.name: m for m in scenario.models}
        self._include_added = False
        self._pinged = False
        self._seq = itertools.count(100)
        self._revs = itertools.count(2)
        self._runner: asyncio.Task[None] | None = None
        self._job_wakeup = asyncio.Event()

    # ------------------------------------------------------------------ fake hooks
    @classmethod
    def for_scenario(cls, scenario: str | Scenario) -> type[FakeCore]:
        """A FakeCore subclass whose ``open()`` uses ``scenario``."""
        if isinstance(scenario, str):
            _sc.get(scenario)  # validate the name now
            return type(f"FakeCore_{scenario}", (cls,), {"scenario_name": scenario})
        return type(f"FakeCore_{scenario.name}", (cls,), {"scenario_override": scenario})

    def fake_emit(self, ev: Event) -> None:
        """Publish an event as if the core produced it."""
        self._bus.publish(ev)

    def fake_set_session(self, snap: SessionSnapshot) -> None:
        """Insert or replace a session and emit Session(Added|Changed)."""
        new = snap.sid not in self._sessions
        self._sessions[snap.sid] = snap
        self._bus.publish(SessionAdded(snap) if new else SessionChanged(snap))

    def fake_set_capability(self, feature: Feature, state: CapState) -> None:
        self._states[feature] = state
        self._bus.publish(CapabilitiesChanged(self._caps_fake()))

    def fake_block_loop(self, seconds: float) -> None:
        """Block the core loop synchronously (watchdog tests)."""
        time.sleep(seconds)

    def fake_call_count(self, name: str) -> int:
        return sum(1 for c in self.fake_calls if c.name == name)

    # ------------------------------------------------------------------ internals
    def _rec(self, name: str, *args: object, **kwargs: object) -> None:
        self.fake_calls.append(FakeCall(name, args, dict(kwargs)))

    def _gate(self, feature: Feature) -> None:
        self._check_open()
        st = self._states[feature]
        if not st.available and st.reason in _GATING:
            raise NotAvailable.for_state(feature, st)

    def _maybe_fail(self, name: str) -> None:
        err = self.fake_scenario.errors.get(name)
        if err is not None:
            raise err

    def _caps_fake(self) -> Capabilities:
        return Capabilities(MappingProxyType(dict(self._states)))

    def _new_revision(self, cfg: Config) -> str:
        return hashlib.sha256(f"{next(self._revs)}:{cfg!r}".encode()).hexdigest()

    async def _steps(
        self,
        op: OpContext,
        phases: tuple[str, ...],
        unit: Literal["frames", "bytes", "steps", "checks"] = "steps",
    ) -> None:
        sc = self.fake_scenario
        total = max(len(phases), 1) * sc.op_steps
        done = 0
        for phase in phases:
            for _ in range(sc.op_steps):
                await asyncio.sleep(sc.op_step_s)
                done += 1
                op.report(Msg(phase), done=done, total=total, unit=unit)

    def _fake_op(self, kind: OpKind, body: Callable[[OpContext], Awaitable[T]]) -> Operation[T]:
        return self._op(kind, body)

    def _snapshot(self, sid: SessionId) -> SessionSnapshot:
        try:
            return self._sessions[sid]
        except KeyError:
            raise ButterEyeError(ErrorCode.IPC_LOST, Msg("No such player session.")) from None

    def _update(self, sid: SessionId, **changes: Any) -> SessionSnapshot:
        snap = dataclasses.replace(self._snapshot(sid), **changes)
        self._sessions[sid] = snap
        self._bus.publish(SessionChanged(snap))
        return snap

    async def _run_script(self) -> None:
        for delay, ev in self.fake_scenario.script:
            await asyncio.sleep(delay)
            self._bus.publish(ev)

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    async def open(cls, paths: Paths | None = None) -> FakeCore:
        scenario = cls.scenario_override or _sc.get(cls.scenario_name)
        core = cls(scenario, paths or _sc.fake_paths())
        core._caps = core._caps_fake()
        if scenario.script:
            core._spawn(core._run_script(), name="fake-script")
        return core

    async def close(
        self,
        policy: DetachPolicy = DetachPolicy.KEEP_FILTER,
        *,
        cancel_jobs: bool,
        timeout_s: float = 5.0,
    ) -> ShutdownReport:
        self._rec("close", policy, cancel_jobs=cancel_jobs, timeout_s=timeout_s)
        if self._shutdown is not None:
            return self._shutdown
        self._closing = True
        detached: list[SessionId] = []
        for sid, snap in list(self._sessions.items()):
            if snap.ended:
                continue
            filt = FilterState.OFF if policy is DetachPolicy.DISABLE_FILTER else snap.filter
            self._sessions[sid] = dataclasses.replace(snap, ended=True, filter=filt)
            self._bus.publish(SessionEnded(sid, "detached"))
            detached.append(sid)
        cancelled: list[JobId] = []
        if cancel_jobs:
            for jid, job in list(self._jobs.items()):
                if job.state in (OpState.QUEUED, OpState.RUNNING):
                    self._set_job(jid, state=OpState.CANCELLED, phase=None)
                    cancelled.append(jid)
        for op in [o for o in self._ops.values() if not o.done]:
            op.cancel()
            await op.wait()
        for t in [t for t in self._tasks if not t.done()]:
            t.cancel()
        if self._runner is not None:
            self._runner.cancel()
        await asyncio.sleep(0)
        self._bus.close()
        self._shutdown = ShutdownReport(tuple(detached), (), tuple(cancelled))
        return self._shutdown

    def subscribe(self, maxlen: int = 4096) -> AsyncIterator[Event]:
        self._rec("subscribe", maxlen)
        return self._bus.subscribe(maxlen)

    async def capabilities(self) -> Capabilities:
        self._rec("capabilities")
        self._caps = self._caps_fake()
        return self._caps

    async def ping(self) -> float:
        if not self._pinged:
            self._pinged = True
            if self.fake_scenario.block_first_ping_s > 0:
                time.sleep(self.fake_scenario.block_first_ping_s)
        return time.monotonic()

    def paths(self) -> Paths:
        return self._paths

    # ------------------------------------------------------------------ config
    async def load_config(self) -> ConfigLoad:
        self._rec("load_config")
        self._gate(Feature.CONFIG)
        self._maybe_fail("load_config")
        if self._disk_revision is not None and self._conflict_seen:
            # "Reload theirs" after a conflict: the on-disk revision is loaded.
            self._load = dataclasses.replace(self._load, revision=self._disk_revision)
            self._disk_revision = None
        return self._load

    async def save_config(self, cfg: Config, *, expected_revision: str) -> str:
        self._rec("save_config", cfg, expected_revision=expected_revision)
        self._gate(Feature.CONFIG)
        if self._load.read_only:
            raise ConfigError(
                ErrorCode.CONFIG_NEWER_SCHEMA,
                Msg("config.toml was created by a newer ButterEye and is read-only."),
            )
        self._maybe_fail("save_config")
        on_disk = self._disk_revision if self._disk_revision is not None else self._load.revision
        if expected_revision != on_disk:
            self._conflict_seen = True
            raise _sc.conflict_error(on_disk)
        rev = self._new_revision(cfg)
        self._load = dataclasses.replace(
            self._load, config=cfg, revision=rev, exists=True, used_defaults=False
        )
        self._disk_revision = None
        self._bus.publish(ConfigChanged(rev))
        self._bus.publish(CapabilitiesChanged(self._caps_fake()))
        return rev

    # ------------------------------------------------------------------ doctor / setup
    def doctor(self, *, trt: bool | None = None) -> Operation[DoctorReport]:
        self._rec("doctor", trt=trt)
        self._gate(Feature.DOCTOR)
        use_trt = bool(self._load.config.general.trt_experimental) if trt is None else trt
        phases = (
            "mpv",
            "In-mpv probe",
            "Packages",
            "Vulkan",
            "GPU fault history",
            "Conflicting mpv settings",
            "Render tools",
        ) + (("TensorRT",) if use_trt else ())

        async def body(op: OpContext) -> DoctorReport:
            await self._steps(op, phases, "checks")
            self._maybe_fail("doctor")
            rep = dataclasses.replace(self.fake_scenario.doctor_result, trt_included=use_trt)
            self._report = rep
            self._bus.publish(CapabilitiesChanged(self._caps_fake()))
            return rep

        return self._fake_op(OpKind.DOCTOR, body)

    async def last_report(self) -> DoctorReport | None:
        self._rec("last_report")
        return self._report

    async def hardware(self) -> HardwareInfo:
        self._rec("hardware")
        self._gate(Feature.HARDWARE)
        self._maybe_fail("hardware")
        return self.fake_scenario.hardware

    async def plugins(self) -> tuple[PluginStatus, ...]:
        self._rec("plugins")
        self._gate(Feature.DOCTOR)
        self._maybe_fail("plugins")
        return self.fake_scenario.plugins

    async def backends(self, report: DoctorReport) -> tuple[BackendStatus, ...]:
        self._rec("backends", report)
        self._gate(Feature.SETUP)
        self._maybe_fail("backends")
        trt_on = self._load.config.general.trt_experimental
        return tuple(
            dataclasses.replace(b, reason=Msg("Experimental: needs TensorRT set up"))
            if trt_on and b.experimental and not b.available
            else b
            for b in self.fake_scenario.backends
        )

    async def select_backend(self, report: DoctorReport) -> Selection:
        self._rec("select_backend", report)
        self._gate(Feature.SETUP)
        self._maybe_fail("select_backend")
        return self.fake_scenario.selection

    async def set_trt_experimental(self, on: bool, *, expected_revision: str) -> str:
        self._rec("set_trt_experimental", on, expected_revision=expected_revision)
        self._gate(Feature.CONFIG)
        cfg = self._load.config
        new = dataclasses.replace(
            cfg, general=dataclasses.replace(cfg.general, trt_experimental=on)
        )
        rev = await self.save_config(new, expected_revision=expected_revision)
        if on and self.fake_scenario.trt_after_optin is not None:
            self._states[Feature.TRT] = self.fake_scenario.trt_after_optin
        elif not on:
            self._states[Feature.TRT] = self.fake_scenario.capabilities[Feature.TRT]
        self._bus.publish(CapabilitiesChanged(self._caps_fake()))
        return rev

    async def setup_plan(self, report: DoctorReport, *, trt_experimental: bool) -> SetupPlan:
        self._rec("setup_plan", report, trt_experimental=trt_experimental)
        self._gate(Feature.SETUP)
        self._maybe_fail("setup_plan")
        sc = self.fake_scenario
        return SetupPlan(
            report=report,
            proposed=sc.selection,
            backends=sc.backends,
            missing_packages=sc.missing_packages,
            dnf_lines=sc.dnf_lines,
            downloads=sc.trt_downloads if trt_experimental else (),
        )

    def setup_apply(self, plan: SetupPlan, choices: SetupChoices) -> Operation[SetupResult]:
        self._rec("setup_apply", plan, choices)
        self._gate(Feature.SETUP)
        sc = self.fake_scenario
        phases: tuple[str, ...] = ()
        if choices.confirmed_downloads:
            phases += ("Downloading models",)
        if choices.run_smoke_test:
            phases += ("Test run (10 seconds)",)
        phases += ("Writing settings",)

        async def body(op: OpContext) -> SetupResult:
            await self._steps(op, phases)
            self._maybe_fail("setup_apply")
            cfg = self._load.config
            cfg = dataclasses.replace(
                cfg,
                general=dataclasses.replace(
                    cfg.general,
                    backend_override=None,
                    trt_experimental=choices.trt_experimental,
                ),
            )
            rev = self._new_revision(cfg)
            self._load = dataclasses.replace(
                self._load, config=cfg, exists=True, revision=rev, used_defaults=False
            )
            self._bus.publish(ConfigChanged(rev))
            return SetupResult(
                config_revision=rev,
                smoke_ok=sc.smoke_finding is None,
                smoke_fps=sc.smoke_fps if choices.run_smoke_test else None,
                smoke_finding=sc.smoke_finding,
                include_line=await self.include_line(),
            )

        return self._fake_op(OpKind.SETUP, body)

    async def include_line(self) -> str:
        home = os.path.expanduser("~")
        p = self._paths.mpv_include
        try:
            shown = "~/" + p.relative_to(home).as_posix()
        except ValueError:
            shown = os.fspath(p)
        return "include=" + shown

    async def add_include_to_mpv_conf(self, *, consent: Literal[True]) -> str:
        self._rec("add_include_to_mpv_conf", consent=consent)
        self._gate(Feature.SETUP)
        if consent is not True:
            raise ValueError("consent must be True")
        self._maybe_fail("add_include_to_mpv_conf")
        if self._include_added:
            return ""
        self._include_added = True
        return (
            f"--- {self._paths.user_mpv_conf}\n+++ {self._paths.user_mpv_conf}\n"
            f"+{await self.include_line()}\n"
        )

    def report_bundle(self, dest: Path, *, redact: bool = True) -> Operation[Path]:
        self._rec("report_bundle", dest, redact=redact)
        self._gate(Feature.REPORT)

        async def body(op: OpContext) -> Path:
            await self._steps(op, ("Collecting logs", "Writing bundle"))
            self._maybe_fail("report_bundle")
            return dest

        return self._fake_op(OpKind.REPORT, body)

    # ------------------------------------------------------------------ live
    async def discover(self) -> tuple[InstanceCandidate, ...]:
        self._rec("discover")
        self._gate(Feature.DISCOVER)
        self._maybe_fail("discover")
        return tuple(self._candidates.values())

    def play(self, file: Path, *, profile_id: str | None = None) -> Operation[SessionId]:
        self._rec("play", file, profile_id=profile_id)
        self._gate(Feature.LIVE)
        if _URL.match(os.fspath(file)):
            raise ButterEyeError(
                ErrorCode.FILE_NOT_LOCAL,
                Msg("Only local files can be opened. Streaming isn't supported."),
            )

        async def body(op: OpContext) -> SessionId:
            await self._steps(op, ("Starting mpv", "Connecting"))
            self._maybe_fail("play")
            sid = SessionId(f"s{next(self._seq)}")
            snap = dataclasses.replace(
                _sc.session(
                    0, title=file.name, filter=FilterState.PENDING, source=_sc.facts(file.name)
                ),
                sid=sid,
                profile_id=profile_id or "quality",
            )
            self._sessions[sid] = snap
            self._bus.publish(SessionAdded(snap))
            self._spawn(self._activate_later(sid), name=f"fake-activate-{sid}")
            return sid

        return self._fake_op(OpKind.PLAY, body)

    async def _activate_later(self, sid: SessionId) -> None:
        await asyncio.sleep(self.fake_scenario.op_step_s * 2)
        active = _sc.session(0)
        if sid in self._sessions and not self._sessions[sid].ended:
            self._update(
                sid,
                filter=FilterState.ACTIVE,
                target_fps=active.target_fps,
                multiplier=active.multiplier,
                backend=active.backend,
                model=active.model,
                gen=1,
            )

    async def attach(self, cid: CandidateId, *, profile_id: str | None = None) -> SessionId:
        self._rec("attach", cid, profile_id=profile_id)
        self._gate(Feature.ATTACH)
        self._maybe_fail("attach")
        cand = self._candidates.get(cid)
        if cand is None:
            raise ButterEyeError(ErrorCode.IPC_LOST, Msg("That player is no longer running."))
        if cand.refused is not None:
            raise InstanceRefused(
                ErrorCode.SOCKET_UNSAFE, cand.refusal or Msg("Refused."), reason=cand.refused
            )
        sid = SessionId(f"s{next(self._seq)}")
        snap = dataclasses.replace(
            _sc.session(0, title=cand.title or cand.socket.name, origin=Origin.ATTACHED),
            sid=sid,
            pid=cand.pid,
            profile_id=profile_id or "quality",
        )
        self._sessions[sid] = snap
        self._bus.publish(SessionAdded(snap))
        return sid

    async def detach(self, sid: SessionId, policy: DetachPolicy = DetachPolicy.KEEP_FILTER) -> None:
        self._rec("detach", sid, policy)
        self._gate(Feature.LIVE)
        self._maybe_fail("detach")
        snap = self._snapshot(sid)
        filt = FilterState.OFF if policy is DetachPolicy.DISABLE_FILTER else snap.filter
        self._sessions[sid] = dataclasses.replace(snap, ended=True, filter=filt)
        self._bus.publish(SessionEnded(sid, "detached"))

    async def set_interpolation(
        self, sid: SessionId, enabled: bool, *, force: bool = False
    ) -> SessionSnapshot:
        if force:
            self._rec("set_interpolation", sid, enabled, force=True)
        else:
            self._rec("set_interpolation", sid, enabled)
        self._gate(Feature.LIVE)
        self._maybe_fail("set_interpolation")
        snap = self._snapshot(sid)
        if enabled:
            return self._update(sid, filter=FilterState.ACTIVE, bypass=None, gen=snap.gen + 1)
        return self._update(sid, filter=FilterState.OFF)

    async def apply_profile(self, sid: SessionId, profile_id: str | None) -> SessionSnapshot:
        self._rec("apply_profile", sid, profile_id)
        self._gate(Feature.LIVE)
        snap = self._snapshot(sid)
        err = self.fake_scenario.errors.get("apply_profile")
        if err is not None:
            self._update(sid, filter=FilterState.ROLLED_BACK, health_code=err.code)
            raise err
        return self._update(sid, profile_id=profile_id or "quality", gen=snap.gen + 1)

    async def step_down(self, sid: SessionId) -> SessionSnapshot:
        self._rec("step_down", sid)
        self._gate(Feature.LIVE)
        self._maybe_fail("step_down")
        snap = self._snapshot(sid)
        target = snap.step_down_to or "fast"
        return self._update(
            sid,
            profile_id=target,
            step_down_to=None,
            health=Health.OK,
            health_code=None,
            gen=snap.gen + 1,
            drop_rate_10s=0.0,
        )

    async def remove_orphan_filter(self, cid: CandidateId) -> None:
        self._rec("remove_orphan_filter", cid)
        self._gate(Feature.ORPHANS)
        self._maybe_fail("remove_orphan_filter")
        cand = self._candidates.get(cid)
        if cand is None:
            raise ButterEyeError(ErrorCode.IPC_LOST, Msg("That player is no longer running."))
        self._candidates[cid] = dataclasses.replace(cand, orphan_filter=False)

    async def sessions(self) -> tuple[SessionSnapshot, ...]:
        self._rec("sessions")
        self._gate(Feature.LIVE)
        self._maybe_fail("sessions")
        return tuple(self._sessions.values())

    async def session_log(self, sid: SessionId) -> str:
        self._rec("session_log", sid)
        self._maybe_fail("session_log")
        snap = self._sessions.get(sid)
        title = snap.title if snap is not None else str(sid)
        return f"ButterEye {__version__} — log for {title}\nSession: {sid}\n\n== ButterEye ==\n"

    # ------------------------------------------------------------------ bench
    def bench(self, req: BenchRequest) -> Operation[BenchResult]:
        self._rec("bench", req)
        self._gate(Feature.BENCH)
        base = self.fake_scenario.bench_result or _sc.bench_result_4090()

        async def body(op: OpContext) -> BenchResult:
            phases = tuple(
                f"{m.label} — run {r} of 3 — in mpv" for m in base.measurements for r in (1, 2, 3)
            )
            await self._steps(op, phases)
            self._maybe_fail("bench")
            result = dataclasses.replace(base, request=req, when=datetime.now(UTC))
            self._bench_history.append(result)
            return result

        return self._fake_op(OpKind.BENCH, body)

    async def bench_history(self) -> tuple[BenchResult, ...]:
        self._rec("bench_history")
        self._gate(Feature.BENCH)
        self._maybe_fail("bench_history")
        return tuple(self._bench_history)

    async def apply_bench(self, result: BenchResult, label: str, *, expected_revision: str) -> str:
        self._rec("apply_bench", result, label, expected_revision=expected_revision)
        self._gate(Feature.BENCH)
        self._maybe_fail("apply_bench")
        if label not in {m.label for m in result.measurements}:
            raise ButterEyeError(
                ErrorCode.BENCH_FAILED, Msg("No measurement named {label}.", {"label": label})
            )
        return await self.save_config(self._load.config, expected_revision=expected_revision)

    # ------------------------------------------------------------------ render
    def _set_job(self, jid: JobId, **changes: Any) -> RenderJobState:
        job = dataclasses.replace(self._jobs[jid], **changes)
        self._jobs[jid] = job
        self._bus.publish(JobChanged(job))
        return job

    def _job(self, jid: JobId) -> RenderJobState:
        try:
            return self._jobs[jid]
        except KeyError:
            raise ButterEyeError(ErrorCode.INTERNAL, Msg("No such render job.")) from None

    async def _run_jobs(self) -> None:
        sc = self.fake_scenario
        while True:
            queued = [j for j in self._jobs.values() if j.state is OpState.QUEUED]
            if not queued:
                self._job_wakeup.clear()
                await self._job_wakeup.wait()
                continue
            jid = queued[0].id
            total = sc.job_total_frames
            self._set_job(
                jid,
                state=OpState.RUNNING,
                phase=RenderPhase.PROBE,
                done_frames=0,
                total_frames=total,
            )
            steps = max(sc.op_steps, 1)
            for i in range(1, steps + 1):
                await asyncio.sleep(sc.op_step_s)
                if self._jobs[jid].state is not OpState.RUNNING:
                    break
                self._set_job(
                    jid,
                    phase=RenderPhase.RENDER,
                    done_frames=total * i // steps,
                    fps=61.3,
                    eta_s=sc.op_step_s * (steps - i),
                )
            else:
                self._set_job(jid, phase=RenderPhase.REMUX)
                self._set_job(jid, state=OpState.SUCCEEDED, phase=None, eta_s=0.0)

    async def render_probe(self, src: Path, *, profile_id: str | None = None) -> RenderProbe:
        self._rec("render_probe", src, profile_id=profile_id)
        self._gate(Feature.RENDER)
        self._maybe_fail("render_probe")
        probe = self.fake_scenario.render_probes.get(src.name)
        if probe is not None:
            return dataclasses.replace(probe, source=src)
        return _sc.render_probe(src)

    async def render_enqueue(self, spec: RenderJobSpec) -> JobId:
        self._rec("render_enqueue", spec)
        self._gate(Feature.RENDER)
        self._maybe_fail("render_enqueue")
        jid = JobId(f"j{next(self._seq)}")
        self._jobs[jid] = RenderJobState(
            jid, spec, OpState.QUEUED, None, None, None, None, None, None, None
        )
        self._bus.publish(JobChanged(self._jobs[jid]))
        if self._runner is None or self._runner.done():
            self._runner = self._spawn(self._run_jobs(), name="fake-render-runner")
        self._job_wakeup.set()
        return jid

    async def render_jobs(self) -> tuple[RenderJobState, ...]:
        self._rec("render_jobs")
        self._gate(Feature.RENDER)
        return tuple(self._jobs.values())

    async def render_move(self, job: JobId, delta: int) -> None:
        self._rec("render_move", job, delta)
        self._gate(Feature.RENDER)
        if self._job(job).state is not OpState.QUEUED:
            raise ButterEyeError(ErrorCode.INTERNAL, Msg("Only queued jobs can be moved."))
        order = list(self._jobs)
        i = order.index(job)
        j = max(0, min(len(order) - 1, i + delta))
        order.insert(j, order.pop(i))
        self._jobs = {k: self._jobs[k] for k in order}
        self._bus.publish(JobChanged(self._jobs[job]))

    async def render_cancel(self, job: JobId) -> None:
        self._rec("render_cancel", job)
        self._gate(Feature.RENDER)
        if self._job(job).state in (OpState.QUEUED, OpState.RUNNING):
            self._set_job(job, state=OpState.CANCELLED, phase=None, eta_s=None)

    async def job_log(self, job: JobId) -> str:
        self._rec("job_log", job)
        self._maybe_fail("job_log")
        state = self._jobs.get(job)
        title = state.spec.output.name if state is not None else str(job)
        return f"ButterEye {__version__} — log for {title}\nJob: {job}\n\n== ButterEye ==\n"

    async def render_forget(self, job: JobId) -> None:
        self._rec("render_forget", job)
        self._gate(Feature.RENDER)
        if self._job(job).state in (OpState.QUEUED, OpState.RUNNING, OpState.CANCELLING):
            raise ButterEyeError(ErrorCode.INTERNAL, Msg("Only finished jobs can be removed."))
        del self._jobs[job]

    async def stale_jobs(self) -> tuple[StaleJob, ...]:
        self._rec("stale_jobs")
        self._gate(Feature.RENDER)
        return tuple(self._stale.values())

    async def stale_job_stop(self, job: JobId) -> None:
        self._rec("stale_job_stop", job)
        self._gate(Feature.RENDER)
        self._stale.pop(job, None)

    # ------------------------------------------------------------------ storage / models
    async def storage(self) -> tuple[StorageEntry, ...]:
        self._rec("storage")
        self._gate(Feature.STORAGE)
        self._maybe_fail("storage")
        return self.fake_scenario.storage or _sc.storage_devbox(self._paths)

    def clean(self, targets: frozenset[CleanTarget]) -> Operation[CleanResult]:
        self._rec("clean", targets)
        self._gate(Feature.CLEAN)
        entries = self.fake_scenario.storage or _sc.storage_devbox(self._paths)

        async def body(op: OpContext) -> CleanResult:
            await self._steps(op, ("Removing files",))
            self._maybe_fail("clean")
            hit = [e for e in entries if e.clean_target in targets and not e.rpm_owned]
            return CleanResult(sum(e.bytes or 0 for e in hit), tuple(e.path for e in hit), ())

        return self._fake_op(OpKind.CLEAN, body)

    async def models(self) -> tuple[ModelEntry, ...]:
        self._rec("models")
        self._gate(Feature.MODELS)
        self._maybe_fail("models")
        return tuple(self._models.values())

    async def model_downloads(self) -> tuple[DownloadItem, ...]:
        self._rec("model_downloads")
        self._gate(Feature.MODEL_DOWNLOADS)
        self._maybe_fail("model_downloads")
        return self.fake_scenario.model_downloads

    def model_install(self, name: str) -> Operation[ModelEntry]:
        self._rec("model_install", name)
        self._gate(Feature.MODEL_DOWNLOADS)
        items = {d.name: d for d in self.fake_scenario.model_downloads}

        async def body(op: OpContext) -> ModelEntry:
            item = items.get(name)
            if item is None:
                raise ButterEyeError(
                    ErrorCode.DOWNLOAD_FAILED, Msg("{name} is not listed.", {"name": name})
                )
            await self._steps(op, ("Downloading", "Verifying"), "bytes")
            if name in self.fake_scenario.hash_mismatch:
                raise ButterEyeError(
                    ErrorCode.HASH_MISMATCH,
                    Msg(
                        "Hash mismatch (expected {expected}, got {got}); not installed.",
                        {"expected": item.sha256, "got": "f" * 64},
                    ),
                )
            entry = ModelEntry(
                name,
                ModelKind.DOWNLOADED,
                BackendId.RIFE_TRT,
                self._paths.data_dir / "models" / name,
                True,
                item.size_bytes,
                item.licence,
                item.sha256,
                True,
            )
            self._models[name] = entry
            return entry

        return self._fake_op(OpKind.MODEL_INSTALL, body)

    def model_install_file(self, file: Path, sha256: str | None) -> Operation[ModelEntry]:
        self._rec("model_install_file", file, sha256)
        self._gate(Feature.MODELS)

        async def body(op: OpContext) -> ModelEntry:
            await self._steps(op, ("Verifying", "Installing"))
            self._maybe_fail("model_install_file")
            name = file.name.split(".")[0]
            entry = ModelEntry(
                name,
                ModelKind.UNPINNED,
                BackendId.RIFE_NCNN,
                self._paths.data_dir / "models" / name,
                True,
                None,
                "unknown",
                sha256,
                True,
            )
            self._models[name] = entry
            return entry

        return self._fake_op(OpKind.MODEL_INSTALL, body)

    async def model_remove(self, name: str) -> None:
        self._rec("model_remove", name)
        self._gate(Feature.MODELS)
        self._maybe_fail("model_remove")
        entry = self._models.get(name)
        if entry is None or not entry.removable:
            raise ButterEyeError(ErrorCode.INTERNAL, Msg("This model can't be removed here."))
        del self._models[name]

    async def third_party(self) -> tuple[ComponentLicence, ...]:
        self._rec("third_party")
        self._gate(Feature.LICENCES)
        self._maybe_fail("third_party")
        return self.fake_scenario.third_party


async def open_fake(
    scenario: str | Scenario = "devbox_now", paths: Paths | None = None
) -> FakeCore:
    """Convenience: ``await open_fake("live_active")``."""
    return await FakeCore.for_scenario(scenario).open(paths)


__all__ = ["FakeCore", "FakeCall", "open_fake"]
