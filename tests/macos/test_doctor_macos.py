# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The doctor on a real Mac (docs/PLATFORMS.md, macOS phase 2).

Skipped everywhere but macOS, and collect-safe on every platform: nothing is
imported at module level that only a Mac has. What runs here is the whole
``doctor`` against this machine's real Homebrew, real ``vulkaninfo`` and real
mpv - the peer of the Wine/Windows-Server run in ``tests/windows``, and the half
of phase 2 that fixtures cannot prove: that the Fedora advice is gone *and* that
what replaced it is true of the machine it is running on.

Measured on the machine these were written for (macOS 27.0.1, Apple M2, 2026-10-12):
mpv 0.41.0 and VapourSynth R81 from Homebrew, ``mvtools.dylib`` and
``libffms2.dylib`` in VapourSynth's autoload directory, ``libvulkan.1.dylib``
and MoltenVK installed, one MoltenVK ICD manifest.

macOS only, and only when ``BUTTEREYE_TEST_BREW`` is set, which is how someone
says those bottles are installed here (``tools/macos-tests.sh`` sets it; the
peer gate is ``BUTTEREYE_TEST_PLUGINS`` in ``tests/windows``). The variable is
needed because a bottle is not optional to every test in this file: most rows
skip for the missing piece, but Homebrew's package database, the in-mpv probe
and the backend list are claims about a Mac that has finished setup, and on one
that has not they *fail* - measured against a Homebrew prefix with formulae in
it but none of ButterEye's: three failures. GitHub's ``macos-latest`` is exactly
that machine, because ``.github/workflows/macos.yml`` installs no bottle, so the
gate is the whole file rather than a third of it. What runs on that runner
instead is the fixture half, ``tests/core/test_macos_doctor_pure.py``, which
needs no Homebrew and carries the same Fedora-word sweep.

No ``short_dir`` here: the doctor binds no Unix socket (its probe passes mpv a
file in the runtime directory, ``probe.run_probe``), so the 103-byte ``sun_path``
limit that fixture exists for does not apply. The runtime directory still comes
from ``xdg_env``, which puts it under ``/tmp`` on macOS for that same reason, so
nothing here writes into the directory a real run uses.
"""

from __future__ import annotations

import dataclasses
import glob
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

BREW = os.environ.get("BUTTEREYE_TEST_BREW", "")

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
    pytest.mark.skipif(not BREW, reason="BUTTEREYE_TEST_BREW not set"),
]

if sys.platform == "darwin":
    from buttereye.core import osapi
    from buttereye.core.backends import select, trt
    from buttereye.core.doctor import brew, checks_mpv, checks_vulkan, probe
    from buttereye.core.doctor import checks_pkgs as pk
    from buttereye.core.doctor.proc import CmdResult
    from buttereye.core.doctor.run import BUDGET_S, doctor
    from buttereye.core.paths import resolve
    from buttereye.core.profiles.defaults import default_config
    from buttereye.core.types import (
        BackendId,
        DoctorReport,
        Finding,
        Paths,
        Section,
        Severity,
    )

#: Words that are a Fedora (or Windows) answer and can only be wrong here. ``rpm``
#: carries its separator because it is a substring of innocent words.
LINUX_WORDS = (
    "dnf",
    "sudo ",
    "rpm ",
    "rpm -",
    "copr",
    "usermod",
    "systemd-journal",
    "journalctl",
    "/usr/lib64",
    "mesa-vulkan",
    "winget",
    ".so.1",
)
#: The only two shapes a command in a macOS finding may have: a Homebrew line, or
#: the ``VSSCRIPT_PATH`` export, which phase 4 leaves only for the mpv ButterEye
#: cannot hand the library to (``probe._vsscript_finding``).
COMMAND_PREFIXES = ("brew ", "export VSSCRIPT_PATH=")


def _texts(f: Finding) -> list[str]:
    out = [f.title.key, f.cause.key, f.fix.key, *f.commands, *f.evidence]
    for msg in (f.title, f.cause, f.fix):
        out += [str(v) for v in msg.params.values()]
    return out


def _linux_words(findings: Sequence[Finding]) -> list[tuple[str, str]]:
    return [
        (f.id, text)
        for f in findings
        for text in _texts(f)
        for word in LINUX_WORDS
        if word in text.lower()
    ]


@pytest.fixture
def paths(xdg_env: Any) -> Paths:
    """This machine's real paths, with the runtime directory the fixture made.

    ``rpm_plugin_dir`` is not redirected: on macOS it resolves to Homebrew's
    VapourSynth autoload directory (``types.macos_plugin_dir``), which is where
    the ``mvtools.dylib`` that really interpolates here lives, and finding it is
    the thing being tested.
    """
    return resolve()


@pytest.fixture
def runtime_env() -> Iterator[None]:
    """``os.environ`` as a real run has it, put back afterwards.

    ``ButterEye.open()`` calls ``osapi.use_bundled_runtime`` before anything
    starts a player (``core.api``), and the doctor's in-mpv probe *is* a player:
    without this, a doctor called straight from a test runs an mpv that was never
    handed the VSScript library, and the report says the probe failed on a machine
    where live smoothing works. The call writes into ``os.environ`` itself -- that
    is how the mpv it starts inherits it -- so ``monkeypatch`` cannot undo it and
    the snapshot and restore are done here. Leaving it set would hand this file's
    environment to the rest of the session, which is the fault that made the gate
    green by test ordering (tests/core/test_windows_runtime_pure.py).
    """
    before = dict(os.environ)
    osapi.use_bundled_runtime()
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(before)


@pytest.fixture
async def report(paths: Paths, runtime_env: None) -> DoctorReport:
    return await doctor(paths, default_config(), trt=None, progress=lambda _p: None)


def _by_id(report: DoctorReport) -> dict[str, Finding]:
    return {f.id: f for f in report.findings}


def _on_disk(pattern: str) -> list[Path]:
    """Files matching ``pattern`` under Homebrew's prefix, asked of the disk.

    This is what the guards below skip on, and it deliberately reads no constant
    and calls no check that the test then asserts about: a test whose skip
    condition and whose assertion go through the same production code can only
    pass or skip, so the Fedora path it exists to catch would make it skip
    quietly instead of failing. Asking the disk by the names Homebrew really
    uses leaves the production answer as the only thing under test.
    """
    return sorted(brew.prefix().glob(pattern))


def _molten_icds() -> list[Path]:
    """MoltenVK's driver manifest where Homebrew's ``molten-vk`` puts it
    (``<prefix>/etc/vulkan/icd.d/MoltenVK_icd.json`` here), found on the disk
    rather than through ``checks_vulkan.ICD_DIRS``."""
    return _on_disk("etc/vulkan/icd.d/MoltenVK_icd.json") + _on_disk(
        "share/vulkan/icd.d/MoltenVK_icd.json"
    )


# ---------------------------------------------------------------- the whole run


async def test_the_doctor_runs_and_speaks_macos(report: DoctorReport) -> None:
    """It finishes well inside F1's budget and every section answers."""
    assert len(report.findings) >= 10
    assert 0 < report.duration_s < BUDGET_S
    sections = {f.section for f in report.findings}
    assert {
        Section.MPV,
        Section.PROBE,
        Section.PACKAGES,
        Section.VULKAN,
        Section.GPU_FAULT,
        Section.RENDER,
        Section.CONFIG,
        Section.TRT,
    } <= sections
    # a finding that is OK may not carry advice, and one that is not must say
    # something: an empty fix everywhere would pass the sweep below
    assert all(f.fix.key or f.commands for f in report.findings if f.severity is Severity.BLOCKING)


async def test_no_finding_mentions_a_tool_this_machine_has_not_got(
    report: DoctorReport,
) -> None:
    """The phase in one assertion: no dnf, rpm, sudo, copr, journalctl or
    /usr/lib64 anywhere in what a Mac user is shown - title, cause, fix, command
    or evidence."""
    assert _linux_words(report.findings) == []
    for f in report.findings:
        for c in f.commands:
            assert c.startswith(COMMAND_PREFIXES), (f.id, c)


async def test_the_fault_history_row_is_the_macos_one(report: DoctorReport) -> None:
    row = _by_id(report)["gpu_fault.xid"]
    assert row.severity is Severity.INFO and row.commands == ()
    assert "macOS" in row.title.key and "yet" in row.title.key
    assert "GPU Restart Count" in row.cause.key  # the counters that do exist


# ---------------------------------------------------------------- Homebrew


async def test_brew_installed_finds_mpv_and_reports_its_version(paths: Paths) -> None:
    """``brew.installed`` against the real Cellar, checked against the binary
    rather than against itself: the version in the receipt has to be the version
    ``mpv --version`` prints, and the keg has to be the one under this prefix."""
    keg = brew.installed(["mpv"]).get("mpv")
    if keg is None:
        pytest.skip("Homebrew's mpv is not installed on this machine")
    assert keg.path.parent.parent == brew.prefix() / brew.CELLAR
    assert keg.path.is_dir() and (keg.path / brew.RECEIPT).is_file()
    assert keg.installed_at and keg.installed_at > 1_600_000_000
    out = subprocess.run(
        [str(brew.prefix() / "bin" / "mpv"), "--no-config", "--version"],
        capture_output=True,
        text=True,
        timeout=20,
    ).stdout
    version = checks_mpv.parse_version(out)
    assert version is not None
    # the receipt's version is the plain one; the keg directory adds Homebrew's
    # revision suffix (0.41.0_10), which is not what mpv prints
    assert keg.version.split("_")[0] == ".".join(str(n) for n in version)
    assert keg.version == keg.version.split("_")[0]
    assert "ffmpeg" in dict(keg.deps)  # the runtime dependencies rpm would list


async def test_the_package_database_is_homebrews_cellar(paths: Paths) -> None:
    db = pk.brew_db()
    if not db.available:
        pytest.skip("Homebrew is not installed on this machine")
    mpv = db.get("mpv")
    assert mpv is not None and mpv.name == "mpv"
    assert mpv.license and mpv.license != "NOASSERTION"  # from the keg's SBOM
    assert all(p.is_file() for p in mpv.licence_files)
    assert db.get(pk.PKG_RIFE) is None  # Homebrew has no RIFE-ncnn formula to find


async def test_reading_the_cellar_starts_no_program(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCOPE forbids the network, and ``brew info`` can go there (1.09 s warm).
    So the install database is files, on the machine where it is a real one."""

    def refuse(*a: object, **k: object) -> Any:
        raise AssertionError(f"the doctor started a program: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    kegs = brew.installed()
    assert len(kegs) > 1, "nothing in the Cellar: this machine has no Homebrew"
    assert all(k.version for k in kegs.values())


# ---------------------------------------------------------------- Vulkan, MoltenVK


async def test_the_vulkan_loader_and_a_gpu_driver_are_both_found(
    report: DoctorReport,
) -> None:
    """Both are installed here, so the doctor may not report either missing -
    which is exactly what it did before phase 2 (``libvulkan.so.1``, and ICD
    directories under /usr/share).

    Guarded on the two files being on the disk under the names Homebrew gives
    them, not on ``LOADER_PATHS`` finding them: with the Fedora values back in
    ``checks_vulkan`` this fails, where asking the constant whether a loader
    exists made it skip.
    """
    loaders = _on_disk("lib/libvulkan.1.dylib")
    molten = _molten_icds()
    if not loaders or not molten:
        pytest.skip(f"no libvulkan.1.dylib or no MoltenVK manifest under {brew.prefix()}")
    loader = loaders[0]
    assert loader.name.endswith(".dylib")
    # the production answer about those two files: the paths the check searches
    # have to resolve to the loader that is really here, and the manifest that is
    # really here has to be one the check counts as a GPU driver
    assert next((p for p in checks_vulkan.LOADER_PATHS if p.exists()), None) == loader
    assert set(molten) <= set(checks_vulkan.gpu_icds())
    rows = _by_id(report)
    assert "vulkan.icd" not in rows, rows.get("vulkan.icd")
    assert rows["vulkan.loader"].severity is Severity.OK, rows["vulkan.loader"]
    assert "vulkan.devices" not in rows, rows.get("vulkan.devices")
    gpus = [d for d in report.hardware.vulkan if d.device_type != "cpu"]
    assert gpus and report.hardware.interpolation_device == gpus[0].uuid


async def test_moltenvk_counts_as_a_driver_and_the_mock_icd_does_not() -> None:
    """MoltenVK is a translation driver, like dzn on Linux, and on macOS it is
    the only route to the GPU. The mock ICD vulkan-tools ships is the one that
    must never count: its own brew caveat invites the user to point
    ``VK_ICD_FILENAMES`` at it, and it answers a software device.

    The guard is the manifest file itself, so a ``gpu_icds`` that stopped
    counting MoltenVK - or looked for manifests where Fedora keeps them - fails
    here instead of leaving the test with nothing to say.
    """
    molten = _molten_icds()
    if not molten:
        pytest.skip(f"molten-vk is not installed under {brew.prefix()}")
    icds = checks_vulkan.gpu_icds()
    assert set(molten) <= set(icds)
    assert any("molten" in p.name.lower() for p in icds)
    mock_dir = brew.prefix() / "opt/vulkan-tools/lib/mock_icd"
    mocks = sorted(mock_dir.glob("*.json"))
    if not mocks:
        pytest.skip("vulkan-tools is not installed, so it shipped no mock ICD")
    assert checks_vulkan.gpu_icds([mock_dir]) == ()
    assert not any(p in mocks for p in icds)


# ---------------------------------------------------------------- the plugins


async def test_mvtools_is_found_under_the_name_vapoursynth_loads(
    report: DoctorReport, paths: Paths
) -> None:
    """"No interpolation plugin is installed" was simply false on this machine:
    Homebrew's ``mvtools.dylib`` is in VapourSynth's autoload directory and a
    generated ButterEye script really doubled a clip's frames through it.

    Guarded on the dylib being somewhere under Homebrew's autoload directory,
    by its own name, rather than on ``paths.rpm_plugin_dir`` - which is the
    directory this test is about, and which would send the test to a skip if it
    went back to ``/usr/lib64/vapoursynth``.
    """
    on_disk = _on_disk("lib/python3.*/site-packages/vapoursynth/plugins/mvtools.dylib")
    if not on_disk:
        pytest.skip(f"vapoursynth-mvtools is not installed under {brew.prefix()}")
    assert pk.MVTOOLS_SO.endswith(".dylib")  # what VSCore::libraryExtension gives
    # The production answer about that file, asked the way the doctor asks it.
    # Deliberately NOT "paths.rpm_plugin_dir / MVTOOLS_SO": that pinned the
    # single-directory invariant phase 4 had to remove, so it was green while the
    # false BLOCKING row was present and went red the moment a tap plugin formula
    # created ButterEye's own directory - the wrong way round for a regression test.
    found = pk.installed_plugin(paths, pk.MVTOOLS_SO)
    assert found in on_disk, (found, on_disk)
    rows = _by_id(report)
    assert rows["packages.mvtools"].severity is Severity.OK, rows["packages.mvtools"]
    assert "packages.plugins" not in rows, rows.get("packages.plugins")
    # the formula a user would install, not the Fedora package name
    assert pk.FORMULA_MVTOOLS in " ".join(_texts(rows["packages.mvtools"]))


async def test_ffms2_is_found(report: DoctorReport) -> None:
    """``ffms2 is not installed (BE-4001)`` with ``sudo dnf install ffms2`` was
    the other false row: ffms2 is a Homebrew bottle and it is installed here.

    Guarded on the keg in the Cellar, which is a different question from the one
    being asked: ``ffms2_present`` is the function under test, and gating on it
    said nothing at all about the search paths. Measured - with the Fedora globs
    back, the Cellar answer still carried the check and the row, so this test
    passed with ``/usr/lib64/libffms2.so*`` as its answer. Hence the line below:
    the paths themselves have to hold the library on the machine that is running.
    """
    if brew.keg("ffms2") is None:
        pytest.skip(f"Homebrew's ffms2 is not installed under {brew.prefix()}")
    globs = pk.macos_ffms2_globs()
    assert any(Path(g).name.startswith("libffms2") for g in globs)
    # the paths the check searches really hold the library on this machine, and
    # the check says so (its first answer is the Cellar, the globs its second)
    assert [p for g in globs for p in glob.glob(g)]
    assert pk.ffms2_present(pk.brew_db(), globs)
    row = _by_id(report)["render.ffms2"]
    assert row.severity is Severity.OK, row


async def test_the_in_mpv_probe_passes_because_buttereye_hands_mpv_vsscript(
    report: DoctorReport,
) -> None:
    """Phase 4's result, which is what the port was for: a VapourSynth script runs
    *inside* mpv on this Mac, so the doctor reports the core mpv embeds instead of
    the blocking "mpv can't load VapourSynth's VSScript library" (BE-1005) that
    every earlier phase measured here -- and long before that, ``sudo dnf install
    python3-vapoursynth``.

    mpv's filter dlopens VSScript by two bare leaf names and Homebrew's only copy
    is inside VapourSynth's ``libexec`` virtualenv, on no dyld search path;
    ``osapi.use_bundled_runtime`` (the ``runtime_env`` fixture, the call the CLI
    makes) hands the file over, and nothing but that makes this pass.

    Guarded on the library being on the disk under the name and in the places
    Homebrew's formula really puts it, asked of the disk by those names rather than
    of ``osapi.vsscript_library`` -- the answer under test. So a Mac without
    VapourSynth skips, while a seam that stopped finding the file that *is* here
    fails instead of skipping quietly.
    """
    on_disk = _on_disk("lib/libvsscript.dylib") + _on_disk(
        "Cellar/vapoursynth/*/libexec/lib/python3.*/site-packages/vapoursynth/libvsscript.dylib"
    )
    if not on_disk:
        pytest.skip(f"Homebrew's VapourSynth is not installed under {brew.prefix()}")
    rows = _by_id(report)
    # the whole failure family is gone: no probe.run row of any severity, which is
    # also what a probe that was skipped for a missing mpv would leave behind
    assert "probe.run" not in rows, rows.get("probe.run")
    assert rows["probe.vs_version"].severity is Severity.OK, rows["probe.vs_version"]
    # and the report carries what only an answer from inside mpv can carry
    assert report.probe is not None
    assert report.probe.vs_core.startswith("VapourSynth R"), report.probe.vs_core
    assert report.probe.python.startswith("3."), report.probe.python
    if _on_disk("lib/python3.*/site-packages/vapoursynth/plugins/mvtools.dylib"):
        assert rows["probe.plugins"].severity is Severity.OK, rows["probe.plugins"]
        loaded = [p.name for p in report.probe.plugins if p.loads_in_mpv]
        assert "MVTools" in loaded, report.probe.plugins

    # and the reason it passed: the one file on this Mac that mpv can be given,
    # named by the seam the runtime uses, in the player's environment
    lib = osapi.vsscript_library()
    assert lib in on_disk, lib
    assert os.environ.get("VSSCRIPT_PATH") == str(lib)


async def test_the_probe_still_names_the_vsscript_problem_when_the_fix_cannot_reach_mpv(
    runtime_env: None,
) -> None:
    """The row the test above no longer reaches, kept honest.

    It is still what a user sees: a Mac with no VapourSynth has no file to hand
    over, and an mpv the system protects never receives a ``DYLD_*`` variable
    (``checks_mpv.dyld_restricted``), as does a player someone starts outside
    ButterEye's environment. What the doctor says then may never be a missing
    package: it is this file, and the variable mpv reads on every platform.

    Driven from a synthesised ``ProbeOutcome``, the way
    tests/core/test_macos_doctor_pure.py drives it, because a real run on this
    machine now passes. What this machine contributes is the part a fixture cannot
    plant: the mpv the doctor would really start, and the real library path that
    this process would really have handed it.
    """
    mpv = osapi.executable("mpv")
    if mpv is None:
        pytest.skip("no mpv on PATH")
    lib = osapi.vsscript_library()
    if lib is None:
        pytest.skip(f"Homebrew's VapourSynth is not installed under {brew.prefix()}")
    # mpv's own line and all it leaves behind: it carries on without the filter and
    # exits 0, so there is no exit status to read (measured, Homebrew's mpv 0.41.0)
    failed = (
        "[vapoursynth] Failed to load VapourSynth VSScript library: "
        f"dlopen({probe.MPV_VSSCRIPT_NAME}, 0x0002): tried: ...\n"
    )
    assert probe.vsscript_load_failed(failed)
    outcome = probe.ProbeOutcome(None, CmdResult((mpv,), 0, "", failed), "no report")

    rows = probe.probe_findings(outcome, (), vspipe_core=None)
    assert [f.id for f in rows] == ["probe.run"]
    row = rows[0]
    assert row.severity is Severity.BLOCKING
    text = " ".join(_texts(row))
    assert "VSScript" in row.title.key
    assert "VSSCRIPT_PATH" in text
    assert str(lib) in text  # the file ButterEye would hand this very mpv
    assert _linux_words([row]) == []
    # the evidence that tells "the fix never reached mpv" from "mpv refused the
    # file": what this process would have passed it, which is set here
    assert f"VSSCRIPT_PATH={lib}" in row.evidence, row.evidence
    for c in row.commands:
        assert c.startswith("export VSSCRIPT_PATH="), c
        named = Path(c.split("=", 1)[1])
        assert named.is_file(), named  # the file it names is really there


# ---------------------------------------------------------------- TensorRT


async def test_tensorrt_is_permanently_unavailable_and_never_selected(
    report: DoctorReport, paths: Paths
) -> None:
    """No CUDA, and no Apple Silicon Mac has an NVIDIA GPU, so the row says
    "can't", not "not yet", and nothing offers it however the config asks."""
    assert not osapi.HAS_VSTRT
    assert trt._vstrt_dirs(paths) == []
    row = _by_id(report)["trt.off"]
    assert row.severity is Severity.INFO and row.commands == ()
    assert "can't run on macOS" in row.title.key and "yet" not in row.title.key

    cfg = default_config()
    opted_in = dataclasses.replace(
        cfg, general=dataclasses.replace(cfg.general, trt_experimental=True)
    )
    # Also asked of a report with the blocking rows taken out, because while
    # anything blocks, *every* backend is unavailable and "TensorRT is not
    # offered" would be true of a machine that simply has not finished setup.
    # Without them MVTools becomes available on this Mac and TensorRT may not.
    unblocked = dataclasses.replace(
        report, findings=tuple(f for f in report.findings if f.severity is not Severity.BLOCKING)
    )
    for rep in (report, unblocked):
        for config in (cfg, opted_in):
            statuses = {s.id: s for s in select.backends(rep, config)}
            assert not statuses[BackendId.RIFE_TRT].available, statuses[BackendId.RIFE_TRT]
            chosen = select.select(rep, config, ())
            assert chosen.backend is not BackendId.RIFE_TRT
            assert BackendId.RIFE_TRT not in chosen.ranked
    assert {s.id for s in select.backends(unblocked, cfg) if s.available} == {BackendId.MVTOOLS}
