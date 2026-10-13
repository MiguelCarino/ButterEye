# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The platform-independent parts of the macOS doctor (docs/PLATFORMS.md,
phase 2), tested on Linux: Homebrew's Cellar in place of ``rpm -q``, the
per-platform install lines, the macOS arms of the two fix helpers, the rule that
keeps MoltenVK a real driver and the mock ICD out, and the rows for GPU faults,
TensorRT and mpv's missing VSScript. The real-Mac half runs in
``tests/macos/test_doctor_macos.py``.

The peer of ``test_windows_doctor_pure.py``, and pure in the same sense: no
Homebrew, no Mac, no subprocess. Three things make that work.

- ``as_macos`` sets ``osapi.IS_MACOS`` and ``commands.sys.platform``, which is
  what every function that branches per platform reads at call time.
- ``HOMEBREW_PREFIX`` points at a planted Cellar, because that is the first
  thing ``types.homebrew_prefix`` honours, so the install record is read from
  ``tmp_path`` on any platform.
- the checks that keep their Linux answer in a module constant
  (``checks_vulkan.LOADER_PATHS``, ``ICD_DIRS``) take the directories as
  arguments, so a test passes its own, and the loader *name* in the finding is
  taken from those same arguments rather than from an import-time constant.

So this file runs in Fedora CI, where the Fedora wording it guards against is
the wording that is actually installed. Verified off-darwin by forcing
``sys.platform`` to ``linux`` in a pytest plugin loaded before the first
``buttereye`` import; the one test that skips there
(``test_the_macos_loader_and_icd_paths_are_under_a_derived_prefix``) is about the
import-time constants themselves and says so.

The Fedora-word sweep has two halves, because a fixture that enumerates builders
by hand only ever guards the builders somebody remembered to add:
``_bare_mac_findings`` walks the finding builders, and
``test_no_macos_string_in_the_source_names_a_linux_tool`` is a static ban over
every macOS arm in ``buttereye/`` - constants named for macOS, ``macos=``
arguments, the bodies of ``if osapi.IS_MACOS``, and any string that names a Mac.
The static half is the one that catches the next string nobody listed.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from buttereye.core import commands, osapi
from buttereye.core import paths as bepaths
from buttereye.core.backends import select
from buttereye.core.bench import runner
from buttereye.core.doctor import brew, checks_mpv, checks_trt, checks_vulkan, gpufault, probe
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.doctor.proc import CmdResult
from buttereye.core.doctor.text import M
from buttereye.core.hw.detect import HwProbe
from buttereye.core.mpvctl import decide
from buttereye.core.mpvctl import session as live
from buttereye.core.paths import resolve
from buttereye.core.profiles.defaults import default_config
from buttereye.core.setup import dnf_lines
from buttereye.core.types import (
    BackendId,
    BenchMeasurement,
    BenchRequest,
    BenchResult,
    DoctorReport,
    Finding,
    HardwareInfo,
    Paths,
    PluginStatus,
    ProbeResult,
    Section,
    Severity,
    VulkanDevice,
)

GiB = 2**30
#: What a Mac's Vulkan device really looks like: one integrated GPU whose
#: device-local heap is the machine's RAM (MoltenVK reports close to
#: ``hw.memsize``), with no separate video memory to measure.
APPLE_M2 = VulkanDevice(0, "0000106b-1b00", "Apple M2", "Apple", "integrated", 16 * GiB)
RTX = VulkanDevice(0, "uuid-nv", "NVIDIA GeForce RTX 4090", "NVIDIA", "discrete", 24 * GiB)


#: Read once, before any fixture flips ``sys.platform``.
_REALLY_DARWIN = sys.platform == "darwin"


class _NoDarwin:
    """``osapi._darwin`` off a Mac: every name is a test failure that says which
    macOS call a pure test reached. Pure tests answer those calls themselves
    (``HOMEBREW_PREFIX``, an explicit ``XDG_RUNTIME_DIR``, a monkeypatched seam)."""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)  # introspection, not a macOS call
        pytest.fail(f"a pure test reached the macOS-only call _darwin.{name}()")


@pytest.fixture
def as_macos(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The platform answers a Mac gives, on whatever machine this runs on.

    ``HAS_VSTRT`` comes with the platform: it is False on macOS and nowhere else
    (phase 1), and a test that flipped the platform without it would be asking
    about a Mac that has CUDA.
    """
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "HAS_VSTRT", False)
    if not _REALLY_DARWIN:
        # ``commands.sys`` is *the* ``sys``: flipping its platform flips every
        # ``sys.platform == "darwin"`` guard in the product, and the bodies of those
        # guards call ``osapi.d`` and ``paths._darwin``, which are only imported on a
        # real Mac (``test_darwin_modules_are_used_only_where_they_are_imported``).
        # Off a Mac a stub stands in, so a test that reaches a real macOS system call
        # fails saying which one, instead of with a NameError.
        monkeypatch.setattr(osapi, "d", _NoDarwin(), raising=False)
        monkeypatch.setattr(bepaths, "_darwin", _NoDarwin(), raising=False)
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    yield


@pytest.fixture
def as_linux(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The Fedora answers, on whatever machine this runs on - the other side of
    every guard here, because "no Fedora advice on a Mac" is also satisfied by a
    doctor that stopped giving advice at all."""
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "HAS_VSTRT", True)
    monkeypatch.setattr(commands.sys, "platform", "linux")
    yield


# ---------------------------------------------------------------- Homebrew's Cellar


def _keg(
    cellar: Path,
    name: str,
    version: str,
    *,
    keg: str | None = None,
    when: int | None = 1_760_000_000,
    deps: Sequence[tuple[str, str]] = (),
    licence: str | None = None,
    licence_files: Sequence[str] = ("LICENSE", "COPYING"),
    receipt: bool = True,
) -> Path:
    """One installed formula, written the way Homebrew writes it: the keg
    directory carries the version with its revision suffix (``0.41.0_10``) and
    the receipt carries the plain one."""
    keg_dir = cellar / name / (keg or version)
    keg_dir.mkdir(parents=True)
    if receipt:
        (keg_dir / brew.RECEIPT).write_text(
            json.dumps(
                {
                    "source": {"versions": {"stable": version}},
                    "time": when,
                    "arch": "arm64",
                    "poured_from_bottle": True,
                    "runtime_dependencies": [
                        {"full_name": d, "version": v, "pkg_version": v} for d, v in deps
                    ],
                }
            ),
            encoding="utf-8",
        )
    if licence is not None:
        (keg_dir / brew.SBOM).write_text(
            json.dumps(
                {
                    "packages": [
                        {"SPDXID": "SPDXRef-Archive-other-src", "licenseConcluded": "NOASSERTION"},
                        {"SPDXID": f"SPDXRef-Archive-{name}-src", "licenseConcluded": licence},
                    ]
                }
            ),
            encoding="utf-8",
        )
    for f in licence_files:
        (keg_dir / f).write_text("licence text", encoding="utf-8")
    return keg_dir


@pytest.fixture
def cellar(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Cellar that ``brew.prefix()`` answers with."""
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    out = tmp_path / brew.CELLAR
    out.mkdir()
    assert brew.prefix() == tmp_path
    return out


def test_the_cellar_reads_like_rpm(cellar: Path) -> None:
    _keg(
        cellar,
        "mpv",
        "0.41.0",
        keg="0.41.0_10",
        when=1_760_111_222,
        deps=[("ffmpeg", "8.1.3"), ("vapoursynth", "81")],
        licence="GPL-2.0-or-later AND LGPL-2.1-or-later",
    )
    kegs = brew.installed()
    assert set(kegs) == {"mpv"}
    mpv = kegs["mpv"]
    assert mpv.name == "mpv"
    assert mpv.version == "0.41.0"  # the receipt's, not the keg directory's 0.41.0_10
    assert mpv.path.name == "0.41.0_10"
    assert mpv.installed_at == 1_760_111_222  # rpm's %{INSTALLTIME}
    assert mpv.deps == (("ffmpeg", "8.1.3"), ("vapoursynth", "81"))
    assert mpv.licence() == "GPL-2.0-or-later AND LGPL-2.1-or-later"
    assert [p.name for p in mpv.licence_files()] == ["COPYING", "LICENSE"]
    assert brew.keg("mpv") == mpv


def test_only_a_finished_keg_counts_as_installed(cellar: Path) -> None:
    _keg(cellar, "mpv", "0.41.0")
    _keg(cellar, "ffms2", "5.0", receipt=False)  # an install that never finished
    assert brew.keg("ffms2") is None
    assert brew.keg("molten-vk") is None  # never installed
    assert set(brew.installed(["mpv", "ffms2", "molten-vk"])) == {"mpv"}
    assert set(brew.installed()) == {"mpv"}


def test_the_newest_install_is_the_live_one(cellar: Path) -> None:
    """An upgrade can leave the old keg behind; the version in use is the one
    installed last, not the one sorted last."""
    _keg(cellar, "mpv", "0.40.0", when=1_000)
    _keg(cellar, "mpv", "0.41.0", keg="0.41.0_10", when=2_000)
    keg = brew.keg("mpv")
    assert keg is not None and keg.version == "0.41.0"


def test_no_homebrew_at_all_reads_as_not_checkable(
    tmp_path: Path, xdg_env: Any, as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mac without Homebrew has no mpv either: one row saying that, rather than
    nine rows saying every package is missing."""
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "nowhere"))
    db = pk.brew_db()
    assert not db.available and db.packages == {} and db.error
    assert "homebrew" in (db.error or "").lower()
    row = next(f for f in pk.package_findings(db, _paths(tmp_path)) if f.id == "packages.rpm")
    assert "Homebrew" in row.title.key
    assert "rpm" not in row.cause.key and "dnf" not in row.fix.key


@pytest.mark.parametrize("body", [None, "{not json", "[]", '{"source": 3}'])
def test_a_damaged_receipt_reads_as_not_installed(cellar: Path, body: str | None) -> None:
    keg_dir = cellar / "mpv" / "0.41.0"
    keg_dir.mkdir(parents=True)
    if body is not None:
        (keg_dir / brew.RECEIPT).write_text(body, encoding="utf-8")
    keg = brew.keg("mpv")
    if body is None or body in ("{not json", "[]"):
        assert keg is None
    else:  # JSON without a version: the keg directory's name is the fallback
        assert keg is not None and keg.version == "0.41.0" and keg.installed_at is None


def test_the_cellar_is_read_without_running_brew(
    cellar: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SCOPE forbids the network and gives the doctor 10 s: ``brew info`` can go
    to the network and took 1.09 s warm, and ``brew --prefix`` exits 0 for a
    formula that is not installed. So this answer may not run a program at all."""
    _keg(cellar, "mpv", "0.41.0", licence="GPL-2.0-or-later")

    def refuse(*a: object, **k: object) -> Any:
        raise AssertionError(f"the doctor started a program: {a!r}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(os, "posix_spawn", refuse)
    monkeypatch.setattr(os, "system", refuse)
    kegs = brew.installed()
    assert kegs["mpv"].licence() == "GPL-2.0-or-later"
    assert kegs["mpv"].licence_files()
    assert pk.brew_db(("mpv",)).packages["mpv"].evr == "0.41.0"


def test_the_package_db_names_the_formula_a_user_would_install(
    cellar: Path, as_macos: None
) -> None:
    """``buttereye-vs-mvtools`` is the key the checks ask about and Homebrew's
    ``vapoursynth-mvtools`` is the answer, so the finding can name what to install."""
    _keg(cellar, "vapoursynth-mvtools", "29", when=1_760_000_321, licence="GPL-2.0-or-later")
    db = pk.brew_db()
    assert db.available
    info = db.get(pk.PKG_MVTOOLS)
    assert info is not None
    assert info.name == pk.FORMULA_MVTOOLS and info.evr == "29"
    assert info.license == "GPL-2.0-or-later"
    assert info.installtime == 1_760_000_321  # what gpufault splits the log at
    assert db.get(pk.PKG_RIFE) is None  # Homebrew has no RIFE formula to find


# ---------------------------------------------------------------- install lines


def test_install_commands_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands.sys, "platform", "linux")
    assert commands.install_command("mkvtoolnix") == "sudo dnf install mkvtoolnix"
    monkeypatch.setattr(commands.sys, "platform", "win32")
    assert commands.install_command("mkvtoolnix").startswith("winget install ")
    monkeypatch.setattr(commands.sys, "platform", "darwin")
    assert commands.install_command("mkvtoolnix") == "brew install mkvtoolnix"
    assert commands.install_command("mpv", verb="upgrade") == "brew upgrade mpv"
    assert set(commands.BREW) == set(commands.DNF) == set(commands.WINGET)
    # a formula name, not a package name with a distro's spelling in it
    assert all(v and " " not in v and "." not in v for v in commands.BREW.values())


def test_the_formula_names_are_one_table(cellar: Path) -> None:
    """Every formula the package checks look for is one ``commands.BREW`` spells
    or a name read off a real Cellar - never built from an rpm package name."""
    for pkg, formula in pk.BREW_FORMULAE.items():
        assert formula and not formula.startswith("buttereye"), pkg
        assert formula == formula.lower()
    assert pk.BREW_FORMULAE["mpv"] == commands.BREW["mpv"]
    assert pk.BREW_FORMULAE["vapoursynth-tools"] == commands.BREW["vapoursynth"]
    # what ButterEye builds itself has no formula, so nothing may claim one
    for pkg in (pk.PKG_RIFE, pk.PKG_MVGPU, pk.PKG_MODELS):
        assert pkg not in pk.BREW_FORMULAE


def test_platform_fix_has_a_macos_arm(as_macos: None) -> None:
    fedora = M("Install it from the ButterEye COPR if you want it.")
    assert pk.platform_fix(fedora, ("sudo dnf install x",)) == {
        "fix": pk.NO_MACOS_PACKAGE,
        "commands": (),
    }
    told = pk.platform_fix(
        fedora,
        ("sudo dnf install x",),
        macos=pk.MACOS_MVTOOLS_FIX,
        macos_commands=("brew install vapoursynth-mvtools",),
    )
    assert told == {
        "fix": pk.MACOS_MVTOOLS_FIX,
        "commands": ("brew install vapoursynth-mvtools",),
    }


def test_tool_fix_takes_its_line_from_the_one_table(as_macos: None) -> None:
    told = pk.tool_fix(
        M("Install the distro mpv package."),
        "sudo dnf install mpv",
        "mpv",
        macos=M("Install Homebrew's mpv."),
    )
    assert told == {"fix": M("Install Homebrew's mpv."), "commands": ("brew install mpv",)}
    # no macOS advice of its own: the Fedora words stay, and only the line changes
    assert pk.tool_fix(M("Update mpv."), "sudo dnf upgrade mpv", "mpv", verb="upgrade") == {
        "fix": M("Update mpv."),
        "commands": ("brew upgrade mpv",),
    }


def test_the_fedora_tail_of_both_helpers_is_untouched(as_linux: None) -> None:
    """Without the macOS platform the two helpers answer exactly what they
    answered before phase 2 - the macOS arm is ahead of that tail, not in it."""
    assert pk.platform_fix(M("f"), ("sudo dnf install x",)) == {
        "fix": M("f"),
        "commands": ("sudo dnf install x",),
    }
    assert pk.tool_fix(M("f"), "sudo dnf install mkvtoolnix", "mkvtoolnix") == {
        "fix": M("f"),
        "commands": ("sudo dnf install mkvtoolnix",),
    }
    assert pk.tool_fix(M("f"), "sudo dnf upgrade mpv", "mpv", verb="upgrade") == {
        "fix": M("f"),
        "commands": ("sudo dnf upgrade mpv",),
    }


# ---------------------------------------------------------------- the findings sweep
#: What no finding on a Mac may say: a package manager it has not got, a journal
#: it does not keep, a library directory that does not exist. ``rpm`` is spelled
#: with its separator so that an innocent word ending in "rpm" cannot match.
LINUX_WORDS = (
    "dnf",
    "sudo",
    "rpm ",
    "rpm -",
    "copr",
    "usermod",
    "systemd-journal",
    "journalctl",
    "/usr/lib64",
    "mesa-vulkan",
    "winget",
)


def _texts(f: Finding) -> list[str]:
    out = [f.title.key, f.cause.key, f.fix.key, *f.commands, *f.evidence]
    for msg in (f.title, f.cause, f.fix):
        out += [str(v) for v in msg.params.values()]
    return out


def _complain(findings: Sequence[Finding]) -> list[tuple[str, str]]:
    bad: list[tuple[str, str]] = []
    for f in findings:
        for text in _texts(f):
            low = text.lower()
            bad += [(f.id, text) for w in LINUX_WORDS if w in low]
    return bad


def _hw(*devs: VulkanDevice, threads: int = 8) -> HwProbe:
    info = HardwareInfo((), devs, devs[0].uuid if devs else None, None, threads)
    vk = CmdResult(("vulkaninfo",), 0, "", "")
    return HwProbe(info, (), vk, None, (), None)


def _paths(tmp_path: Path) -> Paths:
    """Paths with ButterEye's private directories pointed at an empty folder.

    ``resolve()`` gets an environment of its own rather than the machine's: a
    ``HOME`` and a private ``XDG_RUNTIME_DIR`` under ``tmp_path``. The folder layout
    is phase 1's and has its own tests; what matters here is that the answer does
    not depend on where the suite runs. Without it, a test that has already
    flipped the platform to macOS asks for ``confstr(_CS_DARWIN_USER_TEMP_DIR)``,
    which a Linux machine cannot answer, and on Linux a shell with no login
    session (a container, a CI step) has no ``XDG_RUNTIME_DIR`` at all.
    """
    root = tmp_path / "empty"
    (root / "plugins").mkdir(parents=True)
    (root / "data").mkdir()
    (tmp_path / "home").mkdir(exist_ok=True)
    run = tmp_path / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    run.chmod(0o700)
    env = {"HOME": str(tmp_path / "home"), "XDG_RUNTIME_DIR": str(run)}
    return dataclasses.replace(
        resolve(env), rpm_plugin_dir=root / "plugins", rpm_data_dir=root / "data"
    )


#: The loader file name a Mac really wants. Planted under the test's own empty
#: folder, so the check looks for a macOS-shaped name that is not there and the
#: finding quotes it - which is how the name reaches the message on every
#: platform, rather than from an import-time constant the fixture cannot reach.
MAC_LOADER = "libvulkan.1.dylib"


def _nothing(paths: Paths, name: str) -> Path:
    """A path under the test's own empty folder, so "is it installed" is No.

    ``vulkan_findings`` falls back to its module constants for a *falsy*
    ``loader_paths``, so "look nowhere" has to be spelled as a path that is not
    there - on the machine this is written for, the real loader is installed.
    """
    return paths.rpm_plugin_dir / name


def _bare_mac_findings(paths: Paths) -> list[Finding]:
    """Every finding a Mac with nothing installed gets, from the pure builders:
    this is the run that produces the most advice, so it is the one that would
    carry Fedora advice if any arm were missing."""
    db = pk.RpmDb(True, {})  # Homebrew is there; none of these formulae is
    out = pk.package_findings(db, paths)
    out.append(pk.mvgpu_finding(db, paths))
    out.append(pk.licence_files_finding(db))
    out += pk.render_findings(
        db,
        CmdResult(("ffmpeg",), None, "", "", missing=True),
        which=dict.fromkeys(("ffmpeg", "mkvmerge", "7z"), None),
        ffms2=False,
        render_available=True,
    )
    out += checks_vulkan.vulkan_findings(
        _hw(),
        loader_paths=(_nothing(paths, MAC_LOADER),),
        icd_dirs=(_nothing(paths, "no-icd.d"),),
    )
    out.append(checks_trt.trt_off_finding())
    out += checks_trt.trt_findings(
        _hw(APPLE_M2), CmdResult(("ldconfig",), None, "", "", missing=True), None, trtexec=None
    )
    out.append(
        gpufault.xid_finding(
            gpufault.XidScan(False, (), gpufault.MACOS_NOT_READ), since_text="this boot"
        )
    )
    return out


def test_no_finding_on_a_bare_mac_carries_fedora_advice(
    tmp_path: Path, xdg_env: Any, cellar: Path, as_macos: None
) -> None:
    findings = _bare_mac_findings(_paths(tmp_path))
    assert len(findings) >= 10
    assert _complain(findings) == []
    for f in findings:
        for c in f.commands:
            assert c.startswith("brew "), (f.id, c)


def test_the_same_run_on_fedora_still_speaks_fedora(
    tmp_path: Path, xdg_env: Any, as_linux: None
) -> None:
    """The guard above would also pass if every fix had been emptied, so here is
    the same run on Fedora: it must still hand out the dnf lines it always did."""
    findings = _bare_mac_findings(_paths(tmp_path))
    bad = _complain(findings)
    assert bad, "no Fedora advice at all: the fixes went missing rather than moving"
    assert any(c.startswith("sudo dnf ") for f in findings for c in f.commands)
    assert any(pk.COPR_ENABLE in f.commands for f in findings)


def test_an_installed_mac_reports_mvtools_and_ffms2_from_homebrew(
    tmp_path: Path, xdg_env: Any, cellar: Path, as_macos: None
) -> None:
    """The other half of the sweep: what a Mac that has Homebrew's MVTools and
    ffms2 is told. The plugin file has to be found under the name VapourSynth
    would load (``osapi.plugin_file``), which is why the fixture asks the seam."""
    _keg(cellar, "vapoursynth-mvtools", "29", licence="GPL-2.0-or-later")
    _keg(cellar, "ffms2", "5.0", licence="MIT")
    paths = _paths(tmp_path)
    (paths.rpm_plugin_dir / pk.MVTOOLS_SO).write_bytes(b"\xcf\xfa\xed\xfe")
    db = pk.brew_db()
    findings = {f.id: f for f in pk.package_findings(db, paths)}
    mv = findings["packages.mvtools"]
    assert mv.severity is Severity.OK, mv
    assert pk.FORMULA_MVTOOLS in " ".join(_texts(mv))
    assert pk.ffms2_present(db)
    rows = pk.render_findings(db, CmdResult(("ffmpeg",), 0, "", ""), ffms2=True)
    render = {f.id: f for f in rows}
    assert render["render.ffms2"].severity is Severity.OK, render["render.ffms2"]
    assert _complain([*findings.values(), *render.values()]) == []


# ---------------------------------------------------------------- the same sweep, statically
#
# ``_bare_mac_findings`` enumerates the builders it calls by hand, so it only
# guards the strings somebody remembered to wire into it, and the real-Mac sweep
# in tests/macos only sees the rows a fully-equipped Mac happens to emit. Three
# macOS strings sat outside both (``smoke.INSTALL_VSPIPE_MACOS``,
# ``licences.MACOS_SOURCE``, the ffms2 install line in ``capabilities``), and the
# list keeps growing with the port. So the second half of the sweep is static, in
# the shape tests/boundary/test_static_bans.py uses: parse ``buttereye/``, pick
# out the parts only a Mac reaches, and ban the Linux tool names there. That
# catches the next string automatically, which a fixture cannot.
PKG_DIR = Path(__file__).resolve().parents[2] / "buttereye"

#: ``LINUX_WORDS`` again, as whole words so that an identifier ending in one of
#: them (``rpm_plugin_dir``, "pseudo") cannot match - which is the same job the
#: ``"rpm "``/``"rpm -"`` spelling does over there. ``yum`` is here as well: it is
#: the other package manager a Fedora instruction reaches for.
LINUX_TOOLS = re.compile(
    r"\b(?:dnf|rpm|yum|copr|sudo|usermod|journalctl|systemd-journald?|mesa-vulkan|winget)\b"
    r"|/usr/lib64",
    re.I,
)
#: A name that says "the thing under me is the macOS arm": the constants phase 2
#: added (``INSTALL_VSPIPE_MACOS``, ``MACOS_SOURCE``, ``BREW_VSSCRIPT_NAME``), the
#: ``macos=``/``macos_commands=`` arguments of the two fix helpers, and functions
#: named for the platform (``trt_macos_finding``).
MACOS_NAMED = re.compile(r"macos|darwin|brew|molten", re.I)
#: Words that make a string macOS-only wherever it sits, so that a literal reached
#: by none of the rules above is still swept.
MACOS_WORDS = re.compile(r"macOS|Homebrew|brew |MoltenVK|\.dylib|Apple")
#: A finding id, which is not prose: ``packages.rpm`` keeps its name on every
#: platform because the GUI, the docs and ``doctor --report`` all key off it, and
#: renaming it per platform would be the bug. The fixture sweep above makes the
#: same exemption by spelling ``rpm`` with its separator.
FINDING_ID = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


def _docstrings(tree: ast.Module) -> frozenset[int]:
    """The ids of every docstring node. Docstrings explain the code to whoever
    reads it and are never shown to a user, so "this is unlike Fedora's dnf" is
    allowed in one and nowhere else."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    out.append(id(first.value))
    return frozenset(out)


def _negated(test: ast.expr) -> bool:
    """Is this condition's *else* the macOS arm rather than its body?

    Only a ``not`` wrapped straight round the platform test counts, and only a
    ``!= "darwin"``. ``not scan.readable and osapi.IS_MACOS`` carries a ``not``
    but is still true on a Mac, and sweeping its else branch would read the Linux
    arm and complain about Linux words that belong there.
    """
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _about_macos(test.operand)
    if isinstance(test, ast.Compare) and any(isinstance(o, ast.NotEq) for o in test.ops):
        return "'darwin'" in ast.dump(test)
    return False


def _about_macos(node: ast.AST) -> bool:
    dumped = ast.dump(node)
    return "IS_MACOS" in dumped or "'darwin'" in dumped


def _macos_arms(tree: ast.Module) -> Iterator[ast.AST]:
    """Every part of one module that only a Mac reaches."""
    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.IfExp):
            if not _about_macos(node.test):
                continue
            negated = _negated(node.test)
            if isinstance(node, ast.IfExp):
                yield node.orelse if negated else node.body
            else:
                yield from node.orelse if negated else node.body
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if MACOS_NAMED.search(node.name):
                yield from node.body
        elif isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if any(MACOS_NAMED.search(n) for n in names):
                yield node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if MACOS_NAMED.search(node.target.id) and node.value is not None:
                yield node.value
        elif isinstance(node, ast.keyword) and node.arg and MACOS_NAMED.search(node.arg):
            yield node.value


def _literals(node: ast.AST, skip: frozenset[int]) -> Iterator[str]:
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip:
            yield n.value


def _macos_source_strings() -> dict[str, list[str]]:
    """Every macOS-only string literal in ``buttereye/``, by file."""
    out: dict[str, list[str]] = {}
    for path in sorted(p for p in PKG_DIR.rglob("*.py") if "__pycache__" not in p.parts):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        skip = _docstrings(tree)
        found = {s for arm in _macos_arms(tree) for s in _literals(arm, skip)}
        found |= {s for s in _literals(tree, skip) if MACOS_WORDS.search(s)}
        if found:
            out[path.relative_to(PKG_DIR.parent).as_posix()] = sorted(found)
    return out


def test_no_macos_string_in_the_source_names_a_linux_tool() -> None:
    from buttereye.core import licences
    from buttereye.core.doctor import smoke

    by_file = _macos_source_strings()
    bad = [
        f"{where}: {text!r}"
        for where, strings in by_file.items()
        for text in strings
        if LINUX_TOOLS.search(text) and not FINDING_ID.match(text)
    ]
    assert not bad, bad

    # Not vacuous, and that matters more here than anywhere else in this file: a
    # collector that reached nothing would pass for ever. These are the three
    # strings a reviewer planted dnf in while the whole 831-test gate stayed green,
    # named by where they live rather than by their wording, so that changing the
    # wording leaves them swept.
    seen = {s for strings in by_file.values() for s in strings}
    assert smoke.INSTALL_VSPIPE_MACOS.key in seen
    assert licences.MACOS_SOURCE in seen
    assert any("ffms2" in s for s in by_file["buttereye/core/capabilities.py"])
    assert LINUX_TOOLS.search("Run sudo dnf install vapoursynth-tools from the ButterEye COPR.")


# ---------------------------------------------------------------- Vulkan


def test_moltenvk_is_a_real_driver_and_the_mock_icd_is_not(tmp_path: Path) -> None:
    """MoltenVK is a translation driver like dzn, and on macOS it is the only
    route to the GPU, so it has to count. The vulkan-tools mock ICD must not:
    its own brew caveat invites a user to point VK_ICD_FILENAMES at it, and it
    answers a software device that would be reported as the Mac's GPU."""
    icd = tmp_path / "icd.d"
    icd.mkdir()
    for name in ("MoltenVK_icd.json", "VkICD_mock_icd.json"):
        (icd / name).write_text("{}", encoding="utf-8")
    assert [p.name for p in checks_vulkan.gpu_icds([icd])] == ["MoltenVK_icd.json"]
    assert not any("molten" in name.lower() for name in checks_vulkan.CPU_ICDS)
    assert "VkICD_mock_icd" in checks_vulkan.CPU_ICDS


def test_the_vulkan_rows_on_a_mac_name_homebrew_and_not_mesa(
    tmp_path: Path, xdg_env: Any, as_macos: None
) -> None:
    paths = _paths(tmp_path)
    rows = {
        f.id: f
        for f in checks_vulkan.vulkan_findings(
            _hw(),
            loader_paths=(_nothing(paths, MAC_LOADER),),
            icd_dirs=(_nothing(paths, "no-icd.d"),),
        )
    }
    loader, icd = rows["vulkan.loader"], rows["vulkan.icd"]
    assert loader.severity is Severity.DEGRADED
    assert loader.commands == ("brew install vulkan-loader molten-vk",)
    assert icd.commands == ("brew install molten-vk",)
    assert "MoltenVK" in icd.fix.key
    assert _complain(list(rows.values())) == []
    # The loader is named by the file that was really looked for, not by a constant
    # fixed when the module was imported - so the row says libvulkan.1.dylib here
    # and libvulkan.so.1 on Fedora, and this test means the same thing on both.
    text = " ".join(_texts(loader))
    assert MAC_LOADER in text
    assert ".so" not in text


def _mac_with_molten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Mac that has MoltenVK installed, where Vulkan then offered no GPU.

    The manifest is planted in the test's own directory and reaches the check
    through ``icd_dirs=``; ``HOMEBREW_PREFIX`` points at a tree with no Cellar,
    so ``_driver_installed`` has to answer from the manifest scan alone, which is
    the half that works for a MoltenVK that Homebrew did not install.
    """
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path / "no-cellar"))
    icd = tmp_path / "icd.d"
    icd.mkdir()
    (icd / "MoltenVK_icd.json").write_text("{}", encoding="utf-8")
    return icd


def _devices_row(paths: Paths, icd: Path) -> Finding:
    """The ``vulkan.devices`` row for a Mac whose Vulkan listed no device."""
    rows = checks_vulkan.vulkan_findings(
        _hw(), loader_paths=(_nothing(paths, MAC_LOADER),), icd_dirs=(icd,)
    )
    return next(f for f in rows if f.id == "vulkan.devices")


def test_an_installed_driver_is_never_offered_for_installing_again(
    tmp_path: Path, xdg_env: Any, as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row that advised "brew install molten-vk" on a Mac that had MoltenVK.

    The same doctor run proves the driver is there by leaving ``vulkan.icd``
    silent, so a fix that sends the user to install it sends them to repair
    something that is not broken. With the driver accounted for there is no
    command left worth printing, which is what the empty tuple means.
    """
    paths = _paths(tmp_path)
    icd = _mac_with_molten(tmp_path, monkeypatch)
    row = _devices_row(paths, icd)
    # No command at all, and a fix that reports the driver as present rather than
    # demanding it be installed. Not a search for the word "install": the honest
    # wording here contains it twice ("is installed", "Homebrew installed").
    assert row.commands == ()
    assert not row.fix.key.startswith("Install")
    assert "is installed" in row.fix.key  # it says so, rather than going quiet
    assert _complain([row]) == []
    # the driver-absent arm is the other side of this: it must still say install
    bare = _devices_row(paths, _nothing(paths, "no-icd.d"))
    assert bare.commands == ("brew install molten-vk",)


def test_a_loader_env_var_pointed_at_the_mock_icd_is_named_rather_than_guessed(
    tmp_path: Path, xdg_env: Any, as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``vulkan-tools``'s own brew caveat invites the user to export
    ``VK_ICD_FILENAMES`` at the mock ICD. A user who did has a working MoltenVK
    that the loader never loads, and naming the variable is the whole fix.

    Only the manifest's file name may appear: the value can run through a home
    directory, and SCOPE keeps machine identifiers out of findings and out of
    ``doctor --report``.
    """
    paths = _paths(tmp_path)
    icd = _mac_with_molten(tmp_path, monkeypatch)
    secret = str(tmp_path / "home" / "someone" / "VkICD_mock_icd.json")
    monkeypatch.setenv("VK_ICD_FILENAMES", secret)
    row = _devices_row(paths, icd)
    assert row.fix.params.get("var") == "VK_ICD_FILENAMES"
    assert row.fix.params.get("icd") == "VkICD_mock_icd.json"
    assert row.commands == ()
    assert all(secret not in text for text in _texts(row))


def test_the_macos_loader_and_icd_paths_are_under_a_derived_prefix() -> None:
    """Not asserted by value: the prefix moves (/opt/homebrew on arm64,
    /usr/local on Intel) and must never be hardcoded to one."""
    if not osapi.IS_MACOS:
        pytest.skip("the module constants are built for the platform that imports them")
    prefix = brew.prefix()
    assert checks_vulkan.LOADER_PATHS[0] == prefix / "lib/libvulkan.1.dylib"
    assert prefix / "etc/vulkan/icd.d" in checks_vulkan.ICD_DIRS
    assert all(p.suffix == ".dylib" for p in checks_vulkan.LOADER_PATHS)


# ---------------------------------------------------------------- GPU fault history


def test_the_boot_id_is_the_per_boot_uuid_and_never_the_machine_one(
    as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``kern.uuid`` identifies the hardware, and SCOPE forbids a machine
    identifier reaching a finding or ``doctor --report``. ``kern.bootsessionuuid``
    is the one that changes at every boot, which is all the scan needs."""
    asked: list[str] = []

    def sysctl(key: str) -> str | None:
        asked.append(key)
        return "B4A0E2F1-0000-4000-8000-000000000001"

    monkeypatch.setattr(gpufault, "_sysctl_string", sysctl)
    boot = gpufault.current_boot_id()
    assert asked == ["kern.bootsessionuuid"]
    assert boot == "b4a0e2f1000040008000000000000001"  # the Linux shape: no dashes, lower case


def test_the_fault_row_says_not_read_yet_and_not_that_macos_has_no_history(
    as_macos: None,
) -> None:
    scan = gpufault.XidScan(False, (), gpufault.MACOS_NOT_READ)
    row = gpufault.xid_finding(scan, since_text="this boot")
    assert row.severity is Severity.INFO and row.commands == ()
    text = " ".join(_texts(row))
    assert "macOS" in row.title.key and "yet" in row.title.key
    # the counters exist (the AGXAccelerator node publishes them); what is true is
    # that ButterEye does not read them, not that a Mac has no fault history
    assert "GPU Restart Count" in text
    assert _complain([row]) == []


async def test_the_scan_never_starts_journalctl_on_a_mac(
    as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*a: object, **k: object) -> Any:
        raise AssertionError(f"ran a program: {a!r}")

    monkeypatch.setattr(gpufault, "run", refuse)
    scan = await gpufault.scan_xid()
    assert not scan.readable and scan.error == gpufault.MACOS_NOT_READ


# ---------------------------------------------------------------- TensorRT


def test_tensorrt_on_a_mac_is_permanent_and_never_a_row_per_nvidia_package(
    as_macos: None,
) -> None:
    rows = checks_trt.trt_findings(
        _hw(APPLE_M2), CmdResult(("ldconfig",), None, "", "", missing=True), None, trtexec=None
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "trt.off" and row.severity is Severity.INFO and row.commands == ()
    # permanent, not pending: Windows says "yet" because vs-mlrt could come, a
    # Mac never can (no CUDA, and no Apple Silicon Mac has an NVIDIA GPU)
    assert "yet" not in row.title.key
    assert "can't run on macOS" in row.title.key
    assert checks_trt.trt_off_finding() == row
    assert _complain(rows) == []


def test_tensorrt_keeps_its_windows_and_fedora_words() -> None:
    assert "yet" in checks_trt.trt_windows_finding().title.key
    if not (osapi.IS_MACOS or osapi.IS_WINDOWS):
        assert checks_trt.trt_off_finding().title.key == "Experimental NVIDIA TensorRT: off"


# ---------------------------------------------------------------- mpv and VSScript


def _fake_mpv(version: str, *, vs_filter: bool) -> Any:
    """A stand-in for ``doctor.proc.run`` that answers for one mpv build."""
    filters = "Available video filters:\n  format           force output format\n" + (
        "  vapoursynth      VapourSynth bridge\n" if vs_filter else ""
    )

    async def run(argv: Sequence[str], **k: object) -> CmdResult:
        if "--version" in argv:
            return CmdResult(tuple(argv), 0, f"mpv v{version} Copyright 2000-2025\n", "")
        return CmdResult(tuple(argv), 0, filters, "")

    return run


async def test_an_mpv_app_bundle_is_recognised_and_told_where_to_get_mpv(
    as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Your mpv was built without VapourSynth" does not tell the user of the
    mpv.io bundle - the mpv a Mac user most likely already has, 0.40.0 with no
    filter in it - what to do. The answer is ``brew install mpv``, and both the
    version row and the filter row have to give the same one."""
    binary = "/Applications/mpv.app/Contents/MacOS/mpv"
    assert checks_mpv.app_bundle(binary) == "/Applications/mpv.app"
    assert checks_mpv.app_bundle("/opt/homebrew/bin/mpv") is None
    assert checks_mpv.app_bundle("/Applications/other/Contents/MacOS/mpv") is None

    monkeypatch.setattr(checks_mpv, "run", _fake_mpv("0.40.0", vs_filter=False))
    rows, facts = await checks_mpv.mpv_checks(mpv=binary)
    by_id = {f.id: f for f in rows}
    assert not facts.usable
    for fid in ("mpv.version", "mpv.vf_vapoursynth"):
        row = by_id[fid]
        assert row.severity is Severity.BLOCKING, fid
        assert row.commands == ("brew install mpv",), fid
        assert "/Applications/mpv.app" in " ".join(_texts(row)), fid
    assert "application bundle" in by_id["mpv.vf_vapoursynth"].fix.key
    assert _complain(rows) == []


async def test_homebrews_mpv_is_not_called_a_bundle(
    as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checks_mpv, "run", _fake_mpv("0.41.0", vs_filter=True))
    rows, facts = await checks_mpv.mpv_checks(mpv="/opt/homebrew/bin/mpv")
    by_id = {f.id: f for f in rows}
    assert facts.usable
    assert by_id["mpv.version"].severity is Severity.OK
    assert by_id["mpv.vf_vapoursynth"].severity is Severity.OK
    assert _complain(rows) == []


async def test_an_mpv_without_the_filter_that_is_no_bundle_keeps_the_plain_answer(
    as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Homebrew mpv built without the filter is a different problem from the
    bundle, and must not be told it is an application bundle."""
    monkeypatch.setattr(checks_mpv, "run", _fake_mpv("0.41.0", vs_filter=False))
    rows, _facts = await checks_mpv.mpv_checks(mpv="/opt/homebrew/bin/mpv")
    row = next(f for f in rows if f.id == "mpv.vf_vapoursynth")
    assert "bundle" not in " ".join(_texts(row))
    assert row.commands == ("brew install mpv",)


def test_the_probe_blames_vsscript_rather_than_a_fedora_package(
    tmp_path: Path, as_macos: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measured reason the in-mpv probe fails on a Homebrew Mac: mpv's
    vapoursynth filter dlopens ``libvapoursynth-script.dylib`` by bare name and
    Homebrew ships no file of that name. BE-1005 must say that, not "sudo dnf
    install python3-vapoursynth"."""
    lib = (
        tmp_path
        / "Cellar/vapoursynth/81/libexec/lib/python3.14/site-packages/vapoursynth"
        / probe.BREW_VSSCRIPT_NAME
    )
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"\xcf\xfa\xed\xfe")
    assert probe.vsscript_library(tmp_path) == lib
    assert probe.vsscript_library(tmp_path / "nowhere") is None

    failed = (
        f"[vapoursynth] Could not load {probe.MPV_VSSCRIPT_NAME}: "
        f"dlopen({probe.MPV_VSSCRIPT_NAME}, 0x0002): tried: ...\n"
    )
    assert probe.vsscript_load_failed(failed)
    assert not probe.vsscript_load_failed("[vapoursynth] Script evaluation failed\n")

    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    # the seam's answer is the real Mac's (``_darwin.vsscript_library`` reads this
    # machine's ``sys.path`` too), so it says nothing here, as it does on Linux,
    # and the row is built from the planted keg on every platform; the seam half
    # is tests/macos's
    monkeypatch.setattr(osapi, "vsscript_library", lambda: None)
    outcome = probe.ProbeOutcome(None, CmdResult(("mpv",), 0, "", failed), "no report")
    rows = probe.probe_findings(outcome, (), vspipe_core=None)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == "probe.run" and row.severity is Severity.BLOCKING
    text = " ".join(_texts(row))
    assert probe.MPV_VSSCRIPT_NAME in text and str(lib) in text
    assert "VSSCRIPT_PATH" in text  # the variable mpv honours on every platform
    assert _complain(rows) == []


def test_the_probe_keeps_its_fedora_answer_for_every_other_failure(as_macos: None) -> None:
    outcome = probe.ProbeOutcome(None, CmdResult(("mpv",), 1, "", "something else"), "boom")
    rows = probe.probe_findings(outcome, (), vspipe_core=None)
    assert rows[0].id == "probe.run"
    assert rows[0].commands == ("brew install vapoursynth",)
    assert _complain(rows) == []


# ---------------------------------------------------------------- setup's command list


def _finding(*cmds: str) -> Finding:
    return Finding("x", Section.PACKAGES, Severity.DEGRADED, None, M("t"), M(""), M(""), cmds)


def test_setup_collects_brew_lines_and_their_formulae() -> None:
    hw = HardwareInfo((), (), None, None, 1)
    report = DoctorReport(
        (
            _finding("brew install vapoursynth-mvtools"),
            _finding("brew install vulkan-loader molten-vk"),
            _finding("brew upgrade mpv"),  # not an install
            _finding("echo not a package line"),
        ),
        hw,
        None,
        0.0,
        False,
    )
    pkgs, lines = dnf_lines(report, trt=False)
    assert pkgs == ("vapoursynth-mvtools", "vulkan-loader", "molten-vk")
    assert lines == (
        "brew install vapoursynth-mvtools",
        "brew install vulkan-loader molten-vk",
    )


# ---------------------------------------------------------------- the model gate


def _report(dev: VulkanDevice | None, models: Sequence[str]) -> DoctorReport:
    devs = (dev,) if dev is not None else ()
    hw = HardwareInfo((), devs, dev.uuid if dev is not None else None, None, 8)
    found = Finding(
        "packages.models",
        Section.PACKAGES,
        Severity.OK,
        None,
        M("models"),
        M(""),
        M(""),
        (),
        tuple(models),
    )
    return DoctorReport((found,), hw, None, 0.0, False)


MODELS = (select.MODEL_LOW, select.MODEL_MID, select.MODEL_HIGH)


def test_the_full_model_is_not_gated_on_a_device_type_a_mac_can_never_report() -> None:
    """An Apple GPU reports INTEGRATED_GPU, so a device-type gate alone would
    keep every Mac on the lite model for ever. Relaxing it is not the answer
    either: MoltenVK reports a device-local heap near the machine's RAM, so a
    16 GB MacBook Air would pass a 16 GiB threshold on shared memory. The full
    model is reached by the benchmark instead (SCOPE F14)."""
    assert not select._high_end_gpu(APPLE_M2)
    assert not select._high_end_gpu(dataclasses.replace(APPLE_M2, heap_bytes=192 * GiB))
    assert select._high_end_gpu(RTX)
    assert not select._high_end_gpu(dataclasses.replace(RTX, heap_bytes=8 * GiB))
    assert not select._high_end_gpu(None)
    assert select._default_model(_report(APPLE_M2, MODELS)) == select.MODEL_MID
    assert select._default_model(_report(RTX, MODELS)) == select.MODEL_HIGH


def _ok(fid: str, section: Section, evidence: tuple[str, ...] = ()) -> Finding:
    return Finding(fid, section, Severity.OK, None, M(fid), M(""), M(""), (), evidence)


def _ready_mac() -> DoctorReport:
    """A Mac whose setup is finished: the Apple GPU chosen for interpolation, both
    plugins installed and loading inside mpv, all three models packaged. Nothing
    is blocking and nothing is degraded, so the only thing left for
    :func:`select.select` to decide is which model to use."""
    hw = HardwareInfo((), (APPLE_M2,), APPLE_M2.uuid, None, 8)
    plugins = (
        PluginStatus("RIFE-ncnn-Vulkan", "rife", "9.33", "buttereye", True, None, ()),
        PluginStatus("MVTools", "vapoursynth-mvtools", "29", "distro", True, None, ()),
    )
    findings = (
        _ok("mpv.version", Section.MPV),
        _ok("packages.rife", Section.PACKAGES),
        _ok("packages.mvtools", Section.PACKAGES),
        _ok("packages.models", Section.PACKAGES, MODELS),
    )
    probe_result = ProbeResult("VapourSynth R81", "3.14.8", plugins, False)
    return DoctorReport(findings, hw, probe_result, 1.0, False)


def _measured(model: str, label: str) -> BenchMeasurement:
    """One real-time measurement with no GPU faults - what ``_from_bench`` accepts."""
    return BenchMeasurement(
        label, BackendId.RIFE_NCNN, model, 80.0, 75.0, 0.01, True, 1.0, 0.5, None, True, 0
    )


def test_the_lite_model_is_the_default_until_a_benchmark_says_otherwise(as_macos: None) -> None:
    """Which is the other half of the gate: a Mac is not stuck on the lite model,
    it is started there. Asserted end to end, because this is the only evidence
    that an Apple GPU can ever get the better model: ``bench.runner.recommend``
    walks the measurements best quality first, and what it names has to survive
    ``select`` and come back out as the full model (SCOPE F14, "we do not quote
    published fps figures")."""
    from buttereye.core.bench import runner

    report, cfg = _ready_mac(), default_config()
    # with no benchmark a Mac is not offered RIFE for live play at all
    # (decide.rife_live_needs_proof); a user who picks RIFE anyway gets the lite
    # model, whatever is packaged
    assert select.select(report, cfg, ()).backend is BackendId.MVTOOLS
    rife = dataclasses.replace(
        cfg, general=dataclasses.replace(cfg.general, backend_override=BackendId.RIFE_NCNN)
    )
    assert select.select(report, rife, ()).model == select.MODEL_MID
    assert select._default_model(_report(APPLE_M2, (select.MODEL_MID,))) == select.MODEL_MID
    assert select._default_model(_report(APPLE_M2, (select.MODEL_HIGH,))) == select.MODEL_HIGH

    # a measurement on this machine, in the order the runner produces them
    measurements = (_measured(select.MODEL_HIGH, "full"), _measured(select.MODEL_MID, "lite"))
    recommended = runner.recommend(measurements)
    assert recommended == "full"
    result = BenchResult(
        BenchRequest(1920, 1080, Fraction(24000, 1001)),
        measurements,
        recommended,
        datetime(2026, 10, 12, tzinfo=UTC),
        APPLE_M2.uuid,
    )
    chosen = select.select(report, cfg, (result,))
    assert chosen.from_benchmark
    assert chosen.model == select.MODEL_HIGH


def test_the_plugin_search_asks_homebrews_autoload_directory_first(
    as_macos: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mac has plugins in two directories at once, and both must be asked.

    This is the regression test for a measured false BLOCKING row: installing any
    tap plugin formula creates ButterEye's own ``lib/buttereye/vapoursynth``, and
    while the search answered one directory that took the doctor off Homebrew's
    autoload directory -- so ``packages.mvtools`` went degraded and
    ``packages.plugins`` blocking on a Mac whose autoloaded ``mvtools.dylib`` was
    interpolating. The product was fixed with no test, and reverting either half
    (``checks_pkgs.plugin_dirs`` to ``(paths.rpm_plugin_dir,)``, or
    ``types.macos_plugin_dirs`` to the own-directory entry) left the whole gate
    green, which is what this closes.

    Both plants are real files, because ``installed_plugin`` asks the disk.
    """
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    autoload = tmp_path / "lib" / "python3.14" / "site-packages" / "vapoursynth" / "plugins"
    own = tmp_path / "lib" / "buttereye" / "vapoursynth"
    for d in (autoload, own):
        d.mkdir(parents=True)
    (autoload / pk.MVTOOLS_SO).write_bytes(b"\xcf\xfa\xed\xfe")  # a formula's plugin
    (own / pk.MVGPU_SO).write_bytes(b"\xcf\xfa\xed\xfe")  # what the tap builds

    paths = dataclasses.replace(_paths(tmp_path), rpm_plugin_dir=own)
    dirs = pk.plugin_dirs(paths)

    # the autoload copy wins a load, so it has to be asked first (template.vpy's
    # _load skips LoadPlugin for a namespace the core already has)
    assert autoload in dirs, dirs
    assert own in dirs, dirs
    assert dirs.index(autoload) < dirs.index(own), dirs

    # and the question the doctor actually asks finds each plugin where it is,
    # which one directory cannot do
    assert pk.installed_plugin(paths, pk.MVTOOLS_SO) == autoload / pk.MVTOOLS_SO
    assert pk.installed_plugin(paths, pk.MVGPU_SO) == own / pk.MVGPU_SO
    assert pk.installed_plugin(paths, pk.RIFE_SO) is None  # nothing planted it


def _plant(d: Path, *names: str) -> None:
    """Plant a plugin file under every name the callers under test ask for.

    One file on a Mac. Two off darwin: ``osapi.plugin_file`` reads
    ``osapi.IS_MACOS`` when it is called, so ``decide.installed_backends`` asks
    for the macOS name under ``as_macos`` while a module that fixed the name at
    import time (``bench.runner.MVTOOLS_PLUGIN``) still holds the Linux one. The
    extension is not what this test is about - the directory is - so each name a
    caller really uses is on disk.
    """
    for name in dict.fromkeys(names):
        (d / name).write_bytes(b"\xcf\xfa\xed\xfe")


def test_the_engine_chooser_asks_every_plugin_directory_a_mac_has(
    as_macos: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The peer of the test above, for the engine chooser instead of the doctor.

    Phase 4 gave the search path to the doctor only, and a reviewer measured what
    that leaves: on a prefix shaped like a real tap install - ``libmvgpu`` and
    ``librife`` in ButterEye's own directory, Homebrew's ``mvtools`` in
    VapourSynth's autoload one - ``installed_backends`` answered
    ``[mvgpu, rife-ncnn]``. MVTools, the engine measured real time at every size
    an M2 reaches it at all (720p, 1080p and 1440p; nothing reaches 4K) and the
    one the chooser picks there (docs/macos-release-bar.md), disappeared the
    moment the tap was installed.

    The shape is the broken one on purpose: ButterEye's own directory exists, so
    a single answer stops being Homebrew's autoload directory - which is where
    the plugin that goes missing lives.
    """
    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    autoload = tmp_path / "lib" / "python3.14" / "site-packages" / "vapoursynth" / "plugins"
    own = tmp_path / "lib" / "buttereye" / "vapoursynth"
    share = tmp_path / "share" / "buttereye"
    model = share / pk.MODELS_SUBDIR / "rife-v4.26_ensembleFalse"
    for d in (autoload, own, model):
        d.mkdir(parents=True)
    (model / "flownet.param").write_bytes(b"")
    # vapoursynth-mvtools, where that formula installs (it cannot install into
    # another formula's keg, so it is never in ButterEye's directory)
    _plant(
        autoload,
        osapi.plugin_file("mvtools.so"),
        runner.MVTOOLS_PLUGIN,
        decide.BACKEND_PLUGIN[BackendId.MVTOOLS],
    )
    # what the tap builds
    _plant(own, osapi.plugin_file("librife.so"), runner.RIFE_PLUGIN, decide.MVGPU_PLUGIN)
    paths = dataclasses.replace(_paths(tmp_path), rpm_plugin_dir=own, rpm_data_dir=share)
    model_dir = share / pk.MODELS_SUBDIR
    assert own.is_dir() and bepaths.plugin_dirs(paths)[-1] == own

    # Not vacuous: the plant alone does not make MVTools appear. This is the
    # answer every selection site gave while only the doctor had the search path.
    one = decide.installed_backends(paths.rpm_plugin_dir, model_dir)
    assert one == {BackendId.MVGPU, BackendId.RIFE_NCNN}
    every = decide.installed_backends(bepaths.plugin_dirs(paths), model_dir)
    assert every == {BackendId.MVGPU, BackendId.RIFE_NCNN, BackendId.MVTOOLS}

    # live play's own call site, not the search repeated here: a GPU engine that
    # stalls falls back to MVTools only if the session can still find it (§11.9)
    stalling = SimpleNamespace(
        backend=BackendId.MVGPU,
        backend_override=None,
        ctx=SimpleNamespace(paths=paths),
    )
    assert live._Session.can_switch_to_cpu(stalling)  # type: ignore[arg-type]

    # and the benchmark's: find_tools builds the search path the candidates use,
    # so the benchmark offers exactly the engines live play would choose from
    monkeypatch.setattr(osapi, "vspipe", lambda: "vspipe")
    monkeypatch.setattr(osapi, "executable", lambda name: name)
    tools = runner.find_tools(paths)
    labels = [c.label for c in runner.discover_candidates(tools.plugin_dirs, tools.model_dir)]
    assert "mvtools" in labels, labels

    # The doctor and a generated script must resolve the same directory for a
    # plugin: naming the other one is how a user gets "installed but not
    # loading". MVTools is the case that matters - its directory is not
    # ``rpm_plugin_dir``, which is what a script named before.
    for name, where in ((pk.MVTOOLS_SO, autoload), (pk.MVGPU_SO, own)):
        found = pk.installed_plugin(paths, name)
        assert found is not None and found.parent == where
        assert bepaths.plugin_dir_for(paths, name) == where
    assert bepaths.plugin_dir_for(paths, pk.MVTOOLS_SO) != paths.rpm_plugin_dir


def test_offline_render_asks_every_plugin_directory_a_mac_has(
    as_macos: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render half of the test above, which the search-path fix missed.

    ``render/jobs.py`` went on asking ``paths.rpm_plugin_dir`` alone in two
    places after live play and the benchmark were given the search path, and the
    same scratch prefix measured what that costs (docs/macos-release-bar.md,
    item 6): ``jobs._installed`` answered ``[mvgpu, rife-ncnn]`` and the render
    script's ``PLUGIN_DIR`` named ButterEye's own directory, so ``buttereye
    render`` - the half SCOPE §1 calls the one that always works - lost MVTools
    the moment the tap was installed. Both call sites are asked here, through
    the render provider's own functions rather than the search repeated.
    """
    from buttereye.core.render import jobs

    monkeypatch.setenv("HOMEBREW_PREFIX", str(tmp_path))
    autoload = tmp_path / "lib" / "python3.14" / "site-packages" / "vapoursynth" / "plugins"
    own = tmp_path / "lib" / "buttereye" / "vapoursynth"
    share = tmp_path / "share" / "buttereye"
    model = share / jobs.MODEL_SUBDIR / "rife-v4.26_ensembleFalse"
    for d in (autoload, own, model):
        d.mkdir(parents=True)
    (model / "flownet.param").write_bytes(b"")
    # under each name a caller asks for (``_plant``): ``installed_backends`` asks
    # ``osapi.plugin_file`` at call time, the script names ``BACKEND_PLUGIN``'s
    _plant(
        autoload, osapi.plugin_file("mvtools.so"), decide.BACKEND_PLUGIN[BackendId.MVTOOLS]
    )
    _plant(
        own,
        osapi.plugin_file("librife.so"),
        decide.BACKEND_PLUGIN[BackendId.RIFE_NCNN],
        decide.BACKEND_PLUGIN[BackendId.MVGPU],
    )
    paths = dataclasses.replace(_paths(tmp_path), rpm_plugin_dir=own, rpm_data_dir=share)
    cfg = default_config()
    cfg = dataclasses.replace(
        cfg, general=dataclasses.replace(cfg.general, trt_experimental=False)
    )
    ctx = SimpleNamespace(paths=paths, config=lambda: cfg)

    # Not vacuous: the single directory render used to ask has no MVTools in it
    old = decide.installed_backends(paths.rpm_plugin_dir, share / jobs.MODEL_SUBDIR)
    assert BackendId.MVTOOLS not in old

    installed = jobs._installed(ctx)  # type: ignore[arg-type]
    assert installed == {BackendId.MVGPU, BackendId.RIFE_NCNN, BackendId.MVTOOLS}

    # a render pinned to the CPU engine finds it, rather than "not installed"
    cpu = next(p for p in cfg.profiles if p.backend == BackendId.MVTOOLS.value)
    assert jobs._engine(ctx, cpu)[0] is BackendId.MVTOOLS  # type: ignore[arg-type]

    # and the script names the directory each engine's plugin is really in,
    # the one the doctor reports it in (paths.plugin_dir_for)
    assert jobs._constants(ctx, BackendId.MVTOOLS).plugin_dir == autoload  # type: ignore[arg-type]
    assert jobs._constants(ctx, BackendId.MVGPU).plugin_dir == own  # type: ignore[arg-type]
    assert jobs._constants(ctx, BackendId.RIFE_NCNN).plugin_dir == own  # type: ignore[arg-type]
    # no plugin to resolve (TensorRT loads vstrt from its install): the packaged one
    assert jobs._constants(ctx, None).plugin_dir == own  # type: ignore[arg-type]


def test_offline_render_keeps_its_one_directory_off_macos(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Fedora and Windows have exactly one plugin directory, and render must go on
    naming exactly it - the fix above may not move a script off it.

    ``as_linux`` is applied after ``_paths``, for the reason ``_paths`` gives: the
    folder layout is resolved for the machine the test runs on."""
    from buttereye.core.render import jobs

    paths = _paths(tmp_path)
    request.getfixturevalue("as_linux")
    own = paths.rpm_plugin_dir
    _plant(own, osapi.plugin_file("mvtools.so"), decide.BACKEND_PLUGIN[BackendId.MVTOOLS])
    cfg = default_config()
    cfg = dataclasses.replace(
        cfg, general=dataclasses.replace(cfg.general, trt_experimental=False)
    )
    ctx = SimpleNamespace(paths=paths, config=lambda: cfg)
    assert bepaths.plugin_dirs(paths) == (own,)
    assert BackendId.MVTOOLS in jobs._installed(ctx)  # type: ignore[arg-type]
    for engine in (None, BackendId.MVTOOLS, BackendId.RIFE_NCNN, BackendId.MVGPU):
        assert jobs._constants(ctx, engine).plugin_dir == own  # type: ignore[arg-type]


# ---------------------------------------------------------------- the chooser and m0s
#: The spike's own file, read rather than copied: the regression below is the
#: measured reproduction in docs/macos-release-bar.md item 4, and a transcribed
#: figure could drift from the rows the document quotes.
M0S = Path(__file__).resolve().parents[2] / "docs" / "spikes" / "m0s-results.json"


def _m0s_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = json.loads(M0S.read_text(encoding="utf-8"))["runs"]
    return runs


def _m0s_result(run: dict[str, Any]) -> BenchResult:
    """One m0s run as the stored ``BenchResult`` the chooser reads. Faults are
    None: the spike records them as not checked on macOS (IOReport unread)."""
    w, h = (int(n) for n in run["size"].split("x"))
    ms = tuple(
        BenchMeasurement(
            e["label"],
            BackendId(e["backend"]),
            e["model"],
            float(e["vspipe_fps"] or 0.0),
            float(e["mpv_fps"] or 0.0),
            float(e["cov"]) if e["cov"] is not None else 0.0,
            bool(e["repeatable"]),
            0.0,
            0.0,
            None,
            bool(e["realtime"]),
            None,
        )
        for e in run["engines"]
    )
    return BenchResult(
        BenchRequest(w, h, Fraction(run["source_fps"])),
        ms,
        run["recommended"],
        datetime(2026, 10, 13, tzinfo=UTC),
        APPLE_M2.uuid,
    )


def _spike_runs() -> list[dict[str, Any]]:
    """The spike's own complete runs: every engine measured. Left out are the
    phase-5 review's ``review-*`` rows (in-mpv figures for one or two engines,
    the rest not reported, so they cannot show what the chooser would weigh) and
    pass ``x``, which the spike itself discarded."""
    return [r for r in _m0s_runs() if not r["pass"].startswith("review") and r["pass"] != "x"]


def _runs_recommending_mvgpu() -> list[Any]:
    return [
        pytest.param(r, id=f"{r['size']}-{r['pass']}")
        for r in _spike_runs()
        if r["recommended"] == "mvgpu"
    ]


@pytest.mark.parametrize("run", _runs_recommending_mvgpu())
def test_a_benchmark_that_recommends_mvgpu_keeps_rife_off_live_playback(
    as_macos: None, run: dict[str, Any]
) -> None:
    """The release bar's step 1, on its own measured reproduction.

    ``_ready_mac`` is the state a tap install is meant to produce: RIFE, MVTools
    and all three models, so ``ranked`` is ``(rife-ncnn, mvtools)``. Fed a real
    m0s run that recommends mvgpu - every 720p and 1080p run, and the 1440p runs
    where mvgpu cleared the bar - ``select`` used to throw the run away whole and
    fall to ``ranked[0]``: live ``rife-ncnn``, ``from_benchmark`` False, "model
    chosen without a benchmark", in a run that had measured every RIFE model at a
    fraction of the 55.14 fps bar. mvgpu is never ranked (live play picks it per
    file), so the answer the run supports among the rankable engines is MVTools,
    which held real time in every one of these runs.
    """
    report, cfg = _ready_mac(), default_config()
    result = _m0s_result(run)
    # the shape the bug needs, read off the file rather than assumed
    assert result.recommended == "mvgpu"
    rife = [m for m in result.measurements if m.backend is BackendId.RIFE_NCNN]
    assert rife and not any(m.realtime for m in rife)
    assert any(m.backend is BackendId.MVTOOLS and m.realtime for m in result.measurements)
    # and the chooser has RIFE to reach for: it is ranked first
    unmeasured = select.select(report, cfg, ())
    assert unmeasured.ranked == (BackendId.RIFE_NCNN, BackendId.MVTOOLS)

    chosen = select.select(report, cfg, (result,))
    assert chosen.backend is BackendId.MVTOOLS, chosen
    assert chosen.from_benchmark
    assert chosen.model is None
    assert chosen.profile_id == "cpu"
    assert chosen.reason.params == {"label": "mvtools"}
    assert "playback picks it per video" in chosen.reason.key


def test_a_benchmark_that_recommends_mvtools_is_still_taken_as_it_is(as_macos: None) -> None:
    """The 1440p shape, which already worked: the recommendation is rankable, so
    it wins unchanged and with the reason it always had."""
    report, cfg = _ready_mac(), default_config()
    run = next(r for r in _spike_runs() if r["recommended"] == "mvtools")
    chosen = select.select(report, cfg, (_m0s_result(run),))
    assert chosen.backend is BackendId.MVTOOLS and chosen.from_benchmark
    assert chosen.reason.key.startswith("Fastest real-time configuration")


def test_a_run_where_nothing_rankable_keeps_up_is_no_evidence_either_way(
    as_macos: None,
) -> None:
    """A 4K run on an M2: RIFE timed out (stored as 0.0, "not measured") and both
    motion-vector engines fell short, so ``recommended`` is None. The run says
    nothing about which engine to prefer and is not stretched to - the chooser
    answers as it does with no benchmark, and live play's per-file choice
    (``decide.pick_engine``, against each engine's measured cap) is what then
    decides. Recorded so the limit of the fix is a test and not a surprise."""
    report, cfg = _ready_mac(), default_config()
    run = next(r for r in _spike_runs() if r["size"] == "3840x2160")
    assert run["recommended"] is None
    chosen = select.select(report, cfg, (_m0s_result(run),))
    assert not chosen.from_benchmark
    assert chosen == select.select(report, cfg, ())


# ---------------------------------------------------------------- the darwin-only names
#: Module-level names bound only by ``if sys.platform == "darwin":`` imports, per file.
_DARWIN_ONLY = {"core/osapi/__init__.py": "d", "core/paths.py": "_darwin"}


def _is_darwin_guard(test: ast.expr) -> bool:
    """``sys.platform == "darwin"``, the one test that binds the names above."""
    return (
        isinstance(test, ast.Compare)
        and ast.unparse(test.left) == "sys.platform"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "darwin"
    )


def _unguarded_uses(tree: ast.AST, name: str) -> list[int]:
    """Lines that bind or read the module-level ``name`` outside the body of an
    ``if sys.platform == "darwin":`` -- skipping functions where ``name`` is a
    local of their own (``osapi`` has a ``d = d.strip()`` that is not the module)."""
    bad: list[int] = []

    def local_to(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        args = fn.args
        params = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
        if any(a is not None and a.arg == name for a in params):
            return True
        return any(
            isinstance(n, ast.Name) and n.id == name and isinstance(n.ctx, ast.Store)
            for n in ast.walk(fn)
        )

    def visit(node: ast.AST, guarded: bool) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and local_to(node):
            return
        if isinstance(node, ast.If) and _is_darwin_guard(node.test):
            for child in node.body:
                visit(child, True)
            for child in node.orelse:
                visit(child, guarded)
            visit(node.test, guarded)
            return
        binds = isinstance(node, ast.ImportFrom) and any(
            (a.asname or a.name) == name for a in node.names
        )
        if binds and not guarded:
            bad.append(node.lineno)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            if not guarded:
                bad.append(node.lineno)
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return bad


@pytest.mark.parametrize("rel", sorted(_DARWIN_ONLY))
def test_darwin_modules_are_used_only_where_they_are_imported(rel: str) -> None:
    """Why production code on Linux and Windows can never reach ``osapi.d`` or
    ``paths._darwin``: both are imported under ``if sys.platform == "darwin":``,
    and every read of them sits in the body of that same test. ``sys.platform``
    does not change in a real process, so the name is bound exactly when the
    code that reads it can run. Only a test that flips ``sys.platform`` can
    break that pairing, which is what ``as_macos``'s stub is for.

    The pairing is static, so it is checked statically: this test fails on the
    first read of either name outside its guard (a ``sys.platform != "win32"``
    or an ``osapi.IS_MACOS`` test is not the same guard, because a fixture can
    set ``IS_MACOS`` alone)."""
    name = _DARWIN_ONLY[rel]
    src = (Path(bepaths.__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == name for a in n.names)
    ]
    assert imports, f"{rel} no longer imports {name}; update _DARWIN_ONLY"
    assert _unguarded_uses(tree, name) == []
