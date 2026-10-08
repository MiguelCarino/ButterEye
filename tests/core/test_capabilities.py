# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""§2.5 capability resolution: missing provider -> NOT_IMPLEMENTED, NotAvailable
before any I/O, dynamic states from the doctor report and config, §5.2 wording."""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import io
import os
import socket
import subprocess
import sys
import types as pytypes
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import api, capabilities
from buttereye.core.capabilities import ProviderRef, compute, unavailable_text
from buttereye.core.errors import ErrorCode, NotAvailable
from buttereye.core.i18n import render
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import (
    BenchRequest,
    CandidateId,
    CapState,
    CleanTarget,
    DetachPolicy,
    Feature,
    Finding,
    JobId,
    Msg,
    Paths,
    Reason,
    RenderJobSpec,
    Section,
    SessionId,
    Severity,
)

ABSENT = "buttereye.core._absent_for_tests"


def _paths(root: Path) -> Paths:
    return sc.fake_paths(root)


def _all_static(available: bool = True) -> dict[Feature, CapState]:
    return {f: CapState(True) if available else capabilities.absent_state(f) for f in Feature}


@pytest.fixture
def absent_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every facade provider ref and the feature table at a missing module."""
    for name, value in vars(api).items():
        if isinstance(value, ProviderRef):
            monkeypatch.setattr(api, name, ProviderRef(ABSENT, value.attr))
    table = {
        f: tuple(ProviderRef(ABSENT, r.attr) for r in refs)
        for f, refs in capabilities.FEATURE_REQUIRES.items()
    }
    monkeypatch.setattr(capabilities, "FEATURE_REQUIRES", table)


def test_resolve_missing_and_present(monkeypatch: pytest.MonkeyPatch) -> None:
    assert capabilities.resolve(ProviderRef(ABSENT, "x")) is None
    assert capabilities.resolve(ProviderRef(ABSENT + ".deeper.still", "x")) is None
    assert capabilities.resolve(ProviderRef("buttereye.core.commands", "nope")) is None
    assert capabilities.resolve(ProviderRef("buttereye.core.commands", "COMMANDS")) is None
    assert capabilities.resolve(ProviderRef("buttereye.core.commands", "command_hint"))
    mod = pytypes.ModuleType("buttereye.core._fake_present")

    def go() -> int:
        return 1

    mod.go = go  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    assert capabilities.resolve(ProviderRef(mod.__name__, "go")) is go


def test_missing_provider_is_not_implemented(absent_providers: None) -> None:
    for f in Feature:
        st = capabilities.static_state(f)
        if f is Feature.HDR_PASSTHROUGH:
            assert st.available  # no provider; gated dynamically (SPIKE_PENDING)
            continue
        assert not st.available, f
        if f is Feature.MODEL_DOWNLOADS:
            assert st.reason is Reason.NOTHING_LISTED and st.code is None
        else:
            assert st.reason is Reason.NOT_IMPLEMENTED and st.code is ErrorCode.NOT_IMPLEMENTED
        assert st.message is not None


def test_today_live_bench_render_report_absent() -> None:
    """This tree has no M1-M3 providers: live/bench/render/report are not implemented."""
    caps = compute(None, None)
    for f in (Feature.ATTACH, Feature.ORPHANS, Feature.REPORT):
        assert caps.states[f].reason is Reason.NOT_IMPLEMENTED
    for f, refs in (
        (Feature.LIVE, capabilities.LIVE_REFS),
        (Feature.DISCOVER, capabilities.DISCOVER_REFS),
        (Feature.BENCH, capabilities.BENCH_REFS),
        (Feature.RENDER, capabilities.RENDER_REFS),
    ):
        if not capabilities.implemented(refs):
            assert caps.states[f].reason is Reason.NOT_IMPLEMENTED, f
    assert caps.states[Feature.HDR_PASSTHROUGH].reason is Reason.SPIKE_PENDING
    assert caps.states[Feature.TRT].reason is Reason.OPT_IN_REQUIRED


def _gated_calls(core: api.ButterEye, tmp: Path) -> list[tuple[str, Any]]:
    sid, cid, job = SessionId("s1"), CandidateId("c1"), JobId("j1")
    spec = RenderJobSpec(tmp / "a.mkv", tmp / "b.mkv", "fast", "libx265")
    return [
        ("doctor", lambda: core.doctor()),
        ("hardware", lambda: core.hardware()),
        ("plugins", lambda: core.plugins()),
        ("setup_plan", lambda: core.setup_plan(sc.report(()), trt_experimental=False)),
        ("report_bundle", lambda: core.report_bundle(tmp / "r.tar")),
        ("add_include", lambda: core.add_include_to_mpv_conf(consent=True)),
        ("discover", lambda: core.discover()),
        ("play", lambda: core.play(tmp / "a.mkv")),
        ("attach", lambda: core.attach(cid)),
        ("detach", lambda: core.detach(sid, DetachPolicy.DISABLE_FILTER)),
        ("set_interpolation", lambda: core.set_interpolation(sid, True)),
        ("apply_profile", lambda: core.apply_profile(sid, None)),
        ("step_down", lambda: core.step_down(sid)),
        ("remove_orphan_filter", lambda: core.remove_orphan_filter(cid)),
        ("sessions", lambda: core.sessions()),
        ("bench", lambda: core.bench(BenchRequest(1920, 1080, Fraction(24)))),
        ("bench_history", lambda: core.bench_history()),
        ("render_probe", lambda: core.render_probe(tmp / "a.mkv")),
        ("render_enqueue", lambda: core.render_enqueue(spec)),
        ("render_jobs", lambda: core.render_jobs()),
        ("render_move", lambda: core.render_move(job, 1)),
        ("render_cancel", lambda: core.render_cancel(job)),
        ("render_forget", lambda: core.render_forget(job)),
        ("stale_jobs", lambda: core.stale_jobs()),
        ("stale_job_stop", lambda: core.stale_job_stop(job)),
        ("storage", lambda: core.storage()),
        ("clean", lambda: core.clean(frozenset({CleanTarget.ENGINES}))),
        ("models", lambda: core.models()),
        ("model_downloads", lambda: core.model_downloads()),
        ("model_install", lambda: core.model_install("x")),
        ("model_install_file", lambda: core.model_install_file(tmp / "m.7z", None)),
        ("model_remove", lambda: core.model_remove("x")),
        ("third_party", lambda: core.third_party()),
        ("save_config", lambda: core.save_config(sc.default_config(), expected_revision="")),
    ]


async def test_not_available_before_any_io(
    absent_providers: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await api.ButterEye.open(_paths(tmp_path / "nope"))
    calls = _gated_calls(core, tmp_path)
    with monkeypatch.context() as m:
        trapped: list[str] = []

        def trap(*a: object, **k: object) -> None:
            trapped.append("io")
            raise AssertionError("I/O attempted")

        for target, name in (
            (builtins, "open"),
            (io, "open"),
            (os, "open"),
            (subprocess, "Popen"),
            (asyncio, "create_subprocess_exec"),
            (socket, "socket"),
        ):
            m.setattr(target, name, trap)
        for name, call in calls:
            with pytest.raises(NotAvailable) as exc:
                result = call()
                if asyncio.iscoroutine(result):
                    await result
            assert exc.value.state.reason in (Reason.NOT_IMPLEMENTED, Reason.NOTHING_LISTED), name
        assert trapped == []
    # nothing was created on disk either
    assert not (tmp_path / "nope").exists()
    await core.close(cancel_jobs=True)


def test_pure_helpers_without_provider(absent_providers: None) -> None:
    cfg = sc.default_config()
    for call in (
        lambda: api.validate_config(cfg),
        lambda: api.builtin_profiles(),
        lambda: api.explain_rules(cfg, sc.facts()),
    ):
        with pytest.raises(NotAvailable) as exc:
            call()
        assert exc.value.feature is Feature.CONFIG
    with pytest.raises(NotAvailable):
        api.legal_notices()


def _finding(
    code: ErrorCode | None,
    severity: Severity = Severity.DEGRADED,
    section: Section = Section.RENDER,
    fid: str = "x",
    commands: tuple[str, ...] = (),
) -> Finding:
    return sc.finding(fid, section, severity, "t", code=code, commands=commands)


def test_render_missing_ffms2_even_when_implemented() -> None:
    rep = sc.report((_finding(ErrorCode.FFMS2_MISSING, commands=("sudo dnf install ffms2",)),))
    caps = compute(None, rep, static=_all_static())
    st = caps.states[Feature.RENDER]
    assert (st.reason, st.code, st.commands) == (
        Reason.MISSING_DEPENDENCY,
        ErrorCode.FFMS2_MISSING,
        ("sudo dnf install ffms2",),
    )
    assert render(unavailable_text(Feature.RENDER, st)).startswith("Render needs ffms2.")
    assert caps.states[Feature.RENDER_HDR10] == st


def test_hdr10_encoder_finding() -> None:
    rep = sc.report((_finding(None, fid=capabilities.HDR10_ENCODER_FINDING),))
    caps = compute(None, rep, static=_all_static())
    assert caps.states[Feature.RENDER].available
    st = caps.states[Feature.RENDER_HDR10]
    assert st.reason is Reason.MISSING_DEPENDENCY
    assert render(unavailable_text(Feature.RENDER_HDR10, st)).startswith("HDR10 render needs")


def test_blocked_by_doctor() -> None:
    rep = sc.report(
        (
            _finding(ErrorCode.MPV_NO_VS_FILTER, Severity.BLOCKING, Section.MPV),
            _finding(ErrorCode.MPV_TOO_OLD, Severity.BLOCKING, Section.MPV),
        )
    )
    caps = compute(None, rep, static=_all_static())
    for f in capabilities.DOCTOR_GATED:
        assert caps.states[f].reason is Reason.BLOCKED_BY_DOCTOR, f
    assert caps.states[Feature.DOCTOR].available and caps.states[Feature.SETUP].available
    text = render(unavailable_text(Feature.LIVE, caps.states[Feature.LIVE]))
    assert text == "Setup isn't finished.\n2 problem(s) need fixing on the System page."
    # NOT_IMPLEMENTED wins over BLOCKED
    caps2 = compute(None, rep, static=_all_static(False))
    assert caps2.states[Feature.LIVE].reason is Reason.NOT_IMPLEMENTED


def test_trt_states() -> None:
    off = sc.default_config()
    on = dataclasses.replace(off, general=dataclasses.replace(off.general, trt_experimental=True))
    static = _all_static()
    assert compute(off, None, static=static).states[Feature.TRT].reason is Reason.OPT_IN_REQUIRED
    assert compute(None, None, static=static).states[Feature.TRT].reason is Reason.OPT_IN_REQUIRED
    rep = sc.report(
        (
            _finding(
                ErrorCode.TRT_UNSUPPORTED, section=Section.TRT, commands=("contrib/build-vstrt.sh",)
            ),
        )
    )
    st = compute(on, rep, static=static).states[Feature.TRT]
    assert (st.reason, st.code) == (Reason.MISSING_DEPENDENCY, ErrorCode.TRT_UNSUPPORTED)
    assert compute(on, sc.report(()), static=static).states[Feature.TRT].available
    ni = compute(on, sc.report(()), static=_all_static(False)).states[Feature.TRT]
    assert ni.reason is Reason.NOT_IMPLEMENTED


def test_hdr_passthrough_always_spike_pending() -> None:
    st = compute(None, None, static=_all_static()).states[Feature.HDR_PASSTHROUGH]
    assert st.reason is Reason.SPIKE_PENDING
    assert (
        render(unavailable_text(Feature.HDR_PASSTHROUGH, st)) == "Needs compatibility test M0(d)."
    )


@pytest.mark.parametrize(
    ("feature", "reason", "headline"),
    [
        (Feature.LIVE, Reason.NOT_IMPLEMENTED, "Live playback control isn't in this build yet."),
        (
            Feature.ATTACH,
            Reason.NOT_IMPLEMENTED,
            "Attaching to an mpv you started yourself isn't in this build yet.",
        ),
        (
            Feature.ORPHANS,
            Reason.NOT_IMPLEMENTED,
            "Removing a leftover ButterEye filter from another player isn't in this build yet.",
        ),
        (Feature.BENCH, Reason.NOT_IMPLEMENTED, "The benchmark isn't in this build yet."),
        (Feature.RENDER, Reason.NOT_IMPLEMENTED, "Offline render isn't in this build yet."),
        (Feature.REPORT, Reason.NOT_IMPLEMENTED, "Bug reports can't be created in this build yet."),
        (
            Feature.MODEL_DOWNLOADS,
            Reason.NOTHING_LISTED,
            "No downloadable models are listed in this release.",
        ),
        (Feature.TRT, Reason.OPT_IN_REQUIRED, "Experimental NVIDIA TensorRT: off."),
        (Feature.TRT, Reason.MISSING_DEPENDENCY, "TensorRT isn't set up."),
        (Feature.CONFIG, Reason.NOT_IMPLEMENTED, "Profiles can't be edited in this build yet."),
    ],
)
def test_section_5_2_headlines(feature: Feature, reason: Reason, headline: str) -> None:
    msg = unavailable_text(feature, CapState(False, reason))
    assert render(msg).split("\n")[0] == headline
    st = capabilities.unavailable_state(feature, reason, None)
    assert st.message == msg


def test_live_body_text() -> None:
    body = render(unavailable_text(Feature.LIVE, CapState(False, Reason.NOT_IMPLEMENTED)))
    assert body.split("\n", 1)[1] == (
        "This build can check your system and edit profiles. Playing and connecting to mpv "
        "arrive in a later build. Nothing is connected to mpv."
    )


@pytest.mark.parametrize("feature", [Feature.ATTACH, Feature.DISCOVER, Feature.ORPHANS])
def test_live_side_texts_do_not_deny_live_play(feature: Feature) -> None:
    """ATTACH / DISCOVER / ORPHANS can be unavailable while LIVE works: their text
    must not say that playing or connecting to mpv is missing (finding: the LIVE
    text was reused for them)."""
    text = render(unavailable_text(feature, CapState(False, Reason.NOT_IMPLEMENTED)))
    live = render(unavailable_text(Feature.LIVE, CapState(False, Reason.NOT_IMPLEMENTED)))
    assert text != live
    assert "Playing" not in text
    assert "Nothing is connected" not in text
    assert "Live playback control" not in text


def test_gated_texts_never_deny_live_play_when_live_is_available() -> None:
    """With the providers of this build, LIVE is available; no other gated
    feature's message may then claim that playing isn't built."""
    static = _all_static()
    for f in (Feature.ATTACH, Feature.DISCOVER, Feature.ORPHANS, Feature.REPORT, Feature.TRT):
        static[f] = capabilities.absent_state(f)
    caps = compute(None, None, static=static)
    assert caps.ok(Feature.LIVE)
    assert not caps.ok(Feature.ATTACH) and not caps.ok(Feature.ORPHANS)
    for feature, st in caps.states.items():
        if st.available:
            continue
        text = render(unavailable_text(feature, st))
        assert "Playing" not in text, (feature, text)
        assert "Nothing is connected" not in text, (feature, text)


def test_message_from_state_wins() -> None:
    st = CapState(False, Reason.MISSING_DEPENDENCY, None, Msg("custom"))
    assert unavailable_text(Feature.RENDER, st) == Msg("custom")


def test_api_import_pulls_no_subsystem(repo_root: Path) -> None:
    code = (
        f"import sys; sys.path.insert(0, {str(repo_root)!r}); import buttereye.core.api; "
        "print('\\n'.join(sorted(m for m in sys.modules if m.startswith(('buttereye', 'PySide6', "
        "'vapoursynth', 'shiboken')))))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True
    ).stdout.split()
    allowed = {
        f"buttereye.core.{m}"
        for m in ("api", "types", "errors", "events", "ops", "i18n", "commands", "capabilities")
    }
    allowed |= {"buttereye", "buttereye.core"}
    assert set(out) <= allowed, set(out) - allowed
