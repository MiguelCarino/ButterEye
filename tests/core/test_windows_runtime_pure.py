# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What the Windows installer's runtime needs from the core (docs/PLATFORMS.md,
phase 4), tested on Linux: child processes pointed at the bundled VapourSynth,
the first-run runtime folder, UTF-8 output when redirected, vulkaninfo written
to a file, the Visual C++ runtime check and the installer's Repair as the fix.
The Windows half runs in tests/windows (test_installer_windows.py)."""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

import pytest

from buttereye.cli import utf8_stdio
from buttereye.core import osapi
from buttereye.core.bench import runner
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.doctor.proc import CmdResult
from buttereye.core.errors import ButterEyeError
from buttereye.core.hw import detect
from buttereye.core.mpvctl import launcher
from buttereye.core.render import jobs
from buttereye.core.types import Finding, Severity


# ---------------------------------------------------------------- the bundled runtime
def _bundle(tmp_path: Path) -> tuple[str, str]:
    py = tmp_path / "python"
    vs = py / "Lib" / "site-packages" / "vapoursynth"
    vs.mkdir(parents=True)
    (vs / "vsscript.dll").write_bytes(b"MZ")
    return str(py), str(vs)


def test_children_find_the_bundled_vapoursynth_first(tmp_path: Path) -> None:
    py, vs = _bundle(tmp_path)
    env = {"PATH": r"C:\Windows\system32;C:\mpv", "VSSCRIPT_PATH": r"C:\Other\vsscript.dll"}
    osapi.point_at_runtime(env, py, vs, sep=";")
    assert env["VSSCRIPT_PATH"] == os.path.join(vs, "vsscript.dll")
    assert env["PATH"].split(";") == [py, vs, r"C:\Windows\system32", r"C:\mpv"]


def test_pointing_twice_changes_nothing(tmp_path: Path) -> None:
    py, vs = _bundle(tmp_path)
    env = {"PATH": f"C:\\mpv;{vs}"}
    osapi.point_at_runtime(env, py, vs, sep=";")
    once = dict(env)
    osapi.point_at_runtime(env, py, vs, sep=";")
    assert env == once
    assert env["PATH"].split(";") == [py, vs, r"C:\mpv"]


def test_no_vapoursynth_package_leaves_vsscript_alone(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    osapi.point_at_runtime(env, str(tmp_path), None, sep=";")
    assert env == {"PATH": str(tmp_path)}
    vs = tmp_path / "vapoursynth"
    vs.mkdir()  # a package without vsscript.dll (not a Windows build)
    osapi.point_at_runtime(env, str(tmp_path), str(vs), sep=";")
    assert "VSSCRIPT_PATH" not in env


def test_linux_environment_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    before = dict(os.environ)
    osapi.use_bundled_runtime()
    assert dict(os.environ) == before
    assert osapi.runtime_dirs()[0] == os.path.dirname(sys.executable)


# ---------------------------------------------------------------- first run
def test_first_play_creates_the_runtime_folder_with_its_parents_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "LocalAppData" / "ButterEye" / "run"
    with pytest.raises(ButterEyeError):  # POSIX: $XDG_RUNTIME_DIR must already exist
        launcher.ensure_private_dir(run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    assert launcher.ensure_private_dir(run) == run
    assert run.is_dir()


# ---------------------------------------------------------------- redirected output
def test_redirected_output_is_utf8() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    utf8_stdio(stream, None)
    stream.write("23.976 → 47.952 fps ✓\n")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "23.976 → 47.952 fps ✓\n"


def test_utf8_streams_are_left_alone() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    utf8_stdio(stream)
    assert stream.errors == "strict"


# ---------------------------------------------------------------- vulkaninfo
async def test_windows_vulkaninfo_report_comes_from_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []
    report = "GPU0:\n\tdeviceName = Test GPU\n"

    async def fake_run(argv: tuple[str, ...], *, timeout_s: float) -> CmdResult:
        seen.append(tuple(argv))
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(report, encoding="utf-8")
        return CmdResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(detect, "run", fake_run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(detect, "vulkaninfo_exe", lambda: "vulkaninfo.exe")
    res = await detect.run_vulkaninfo(5.0)
    assert seen[0][:3] == ("vulkaninfo.exe", "--text", "-o")
    assert res.ok and res.stdout == report
    assert not Path(seen[0][3]).exists()  # the private temporary folder is gone

    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(detect, "vulkaninfo_exe", lambda: "vulkaninfo")
    await detect.run_vulkaninfo(5.0)
    assert seen[1] == ("vulkaninfo",)  # Linux: unchanged


async def test_windows_vulkaninfo_without_a_report_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(argv: tuple[str, ...], *, timeout_s: float) -> CmdResult:
        return CmdResult(tuple(argv), None, "", "", timed_out=True)

    monkeypatch.setattr(detect, "run", fake_run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    res = await detect.run_vulkaninfo(1.0)
    assert res.timed_out and not res.ok and res.stdout == ""


# ---------------------------------------------------------------- Visual C++ runtime
def test_vcruntime_check() -> None:
    assert pk.read_vcruntime() is None  # not Windows
    ok = pk.vcruntime_finding((14, 51, 36247))
    assert ok.id == "packages.vcruntime" and ok.severity is Severity.OK
    missing = pk.vcruntime_finding(None)
    assert missing.severity is Severity.BLOCKING
    assert missing.commands == ("winget install --exact --id Microsoft.VCRedist.2015+.x64",)
    old = pk.vcruntime_finding((14, 44, 35211))  # what PySide6 carries: too old
    assert old.severity is Severity.BLOCKING
    assert old.commands == ("winget upgrade --exact --id Microsoft.VCRedist.2015+.x64",)
    assert pk.vcruntime_finding(pk.VCRUNTIME_MIN).severity is Severity.OK


# ---------------------------------------------------------------- fixes say Repair on Windows
def test_missing_vspipe_or_plugins_point_at_the_installer_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, commands = runner._install_fix(runner.REPAIR, runner.INSTALL_VSPIPE)
    assert commands == (runner.INSTALL_VSPIPE,)  # Linux: the dnf line
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    fix, commands = runner._install_fix(runner.REPAIR, runner.INSTALL_VSPIPE)
    assert commands == () and fix == runner.REPAIR
    err = jobs._missing("vspipe", None)
    assert err.commands == ()
    assert not any("dnf" in c or "winget" in c for c in err.commands)


# ---------------------------------------------------------------- no Vulkan loader
def _probe_plugins(monkeypatch: pytest.MonkeyPatch, windows: bool, loader: bool) -> Finding:
    from buttereye.core.doctor import probe
    from buttereye.core.types import PluginStatus

    monkeypatch.setattr(osapi, "IS_WINDOWS", windows)
    raw = {
        "vs_core": "81",
        "python": "3.14.8",
        "plugins": [
            {"ns": "rife", "loaded": False, "error": "GetLastError() returned 126"},
            {"ns": "mv", "loaded": True, "loaded_from": "C:/ButterEye/vapoursynth/mvtools.dll"},
        ],
    }
    status = [
        PluginStatus("RIFE-ncnn-Vulkan", pk.PKG_RIFE, "1", "buttereye", False, None, ()),
        PluginStatus("MVTools", pk.PKG_MVTOOLS, "1", "buttereye", True, None, ()),
    ]
    out = probe.probe_findings(
        probe.ProbeOutcome(raw, None, None), status, vspipe_core="81", vulkan_loader=loader
    )
    return next(f for f in out if f.id == "probe.plugins")


def test_rife_without_the_vulkan_loader_is_the_driver_not_a_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = _probe_plugins(monkeypatch, windows=True, loader=False)
    assert f.severity is Severity.DEGRADED
    assert "Vulkan loader" in f.title.key
    assert "graphics driver" in f.fix.key and f.commands == ()
    assert any(e.startswith("mv: loaded from") for e in f.evidence)


@pytest.mark.parametrize(("windows", "loader"), [(True, True), (False, False), (False, True)])
def test_a_rife_load_failure_otherwise_stays_a_reinstall(
    monkeypatch: pytest.MonkeyPatch, windows: bool, loader: bool
) -> None:
    f = _probe_plugins(monkeypatch, windows=windows, loader=loader)
    assert f.severity is Severity.DEGRADED
    assert f.title.key == "{names} did not load inside mpv"
