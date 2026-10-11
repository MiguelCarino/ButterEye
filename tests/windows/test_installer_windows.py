# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""ButterEye's Windows installer (docs/PLATFORMS.md, phase 4), end to end: a
silent install into a new folder, the installed runtime at work (the doctor,
the bundled vspipe running the speed-test script, ffms2 reading a video, Qt),
then a silent uninstall.

Windows only, and only when ``BUTTEREYE_TEST_INSTALLER`` names the setup.exe
(``packaging/windows/build-installer.sh``; ``tools/wine-tests.sh`` sets it).
``BUTTEREYE_TEST_MPV`` (a Windows mpv.exe) adds the in-mpv probe;
``BUTTEREYE_TEST_VIDEO`` adds the ffms2 check.

The installed runtime is run with nothing of this test's own Python: its folder
is taken off PATH (and Wine's WINEPATH dropped), and APPDATA/LOCALAPPDATA point
at an empty profile, so the first-run path is exercised too. The installer
registers itself under this user's HKCU and adds a Start-menu shortcut; the
uninstall at the end removes both.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

INSTALLER = os.environ.get("BUTTEREYE_TEST_INSTALLER", "")
MPV = os.environ.get("BUTTEREYE_TEST_MPV", "")
VIDEO = os.environ.get("BUTTEREYE_TEST_VIDEO", "")

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
    pytest.mark.skipif(not INSTALLER, reason="BUTTEREYE_TEST_INSTALLER not set"),
]

UNINSTKEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\ButterEye"
PLUGINS = ("mvtools.dll", "librife.dll", "libmvgpu.dll", "ffms2.dll")
MODEL = "rife-v4.22_lite_ensembleFalse"


def _wine() -> bool:
    import ctypes

    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined,unused-ignore]
    return hasattr(ntdll, "wine_get_version")


def _no_vulkan_loader() -> str | None:
    """Why the GPU plugins can't run here, or None: librife.dll and libmvgpu.dll
    import vulkan-1.dll, which a GPU driver installs (GitHub's runners have none)."""
    import ctypes

    try:
        ctypes.WinDLL("vulkan-1")  # type: ignore[attr-defined,unused-ignore]
    except OSError as exc:
        return f"no Vulkan loader (vulkan-1.dll) on this PC: {exc}"
    return None


def _uninstall_entry() -> dict[str, Any] | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTKEY) as key:
            return {
                name: winreg.QueryValueEx(key, name)[0]
                for name in ("DisplayName", "DisplayVersion", "InstallLocation", "UninstallString")
            }
    except OSError:
        return None


def _start_menu_link() -> Path:
    return (
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        / "ButterEye.lnk"
    )  # fmt: skip


class Installed:
    def __init__(self, folder: Path, profile: Path) -> None:
        self.folder = folder
        self.python = folder / "python" / "python.exe"
        self.vs_dir = folder / "python" / "Lib" / "site-packages" / "vapoursynth"
        self.profile = profile

    def env(self) -> dict[str, str]:
        """What a user's fresh session would hand the installed ButterEye: no
        trace of this test's Python, an empty profile, mpv on PATH if we have one."""
        mine = os.path.normcase(os.path.dirname(sys.executable))
        drop = {"WINEPATH", "PYTHONPATH", "PYTHONHOME", "BUTTEREYE_HOME", "VSSCRIPT_PATH"}
        env = {k: v for k, v in os.environ.items() if k.upper() not in drop}
        path = [p for p in env.get("PATH", "").split(os.pathsep) if p]
        path = [p for p in path if os.path.normcase(p.rstrip("\\/")) != mine]
        if MPV:
            path.insert(0, os.path.dirname(MPV))
        env["PATH"] = os.pathsep.join(path)
        env["APPDATA"] = str(self.profile / "Roaming")
        env["LOCALAPPDATA"] = str(self.profile / "Local")
        return env

    def run(self, *argv: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv, env=self.env(), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )  # fmt: skip


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Installed]:
    root = tmp_path_factory.mktemp("installer")
    folder = root / "ButterEye"
    profile = root / "profile"
    (profile / "Roaming").mkdir(parents=True)
    (profile / "Local").mkdir()
    res = subprocess.run([INSTALLER, "/S", f"/D={folder}"], timeout=900, check=False)
    assert res.returncode == 0, f"setup.exe /S exited with {res.returncode}"
    inst = Installed(folder, profile)
    yield inst
    uninstaller = folder / "Uninstall.exe"
    if uninstaller.exists():  # a test failed before the uninstall test: clean up
        subprocess.run([str(uninstaller), "/S", f"_?={folder}"], timeout=300, check=False)


def test_installs_silently_into_the_chosen_folder(installed: Installed) -> None:
    f = installed.folder
    for rel in (
        "python/python.exe", "python/pythonw.exe", "buttereye/__init__.py",
        "vulkaninfo.exe", "buttereye.cmd", "buttereye.ico", "buttereye-install.json",
        "Uninstall.exe", "licenses/SOURCES.txt", "licenses/buttereye/COPYING",
        f"share/rife-ncnn-models/{MODEL}/flownet.param",
        *(f"vapoursynth/{p}" for p in PLUGINS),
    ):  # fmt: skip
        assert (f / rel).is_file(), rel
    assert (installed.vs_dir / "vspipe.exe").is_file()
    assert not list(installed.vs_dir.glob("*.pdb")), "debug symbols are not shipped"
    entry = _uninstall_entry()
    assert entry is not None and entry["DisplayName"] == "ButterEye"
    assert os.path.normcase(entry["InstallLocation"]) == os.path.normcase(str(f))
    assert _start_menu_link().is_file()


def test_install_record_lists_every_component_with_its_licences(installed: Installed) -> None:
    record = json.loads((installed.folder / "buttereye-install.json").read_text("utf-8"))
    names = {c["name"] for c in record["components"]}
    assert {
        "buttereye", "buttereye-vs-rife-ncnn", "buttereye-vs-mvtools", "buttereye-vs-mvgpu",
        "buttereye-rife-ncnn-models", "ffms2", "vulkan-tools", "mingw-w64-runtime",
        "python3", "python3-pyside6", "vapoursynth-libs", "vapoursynth-tools",
    } <= names  # fmt: skip
    for c in record["components"]:
        assert c["licence_files"], c["name"]
        for rel in c["licence_files"]:
            assert (installed.folder / rel).is_file(), rel


def _doctor(
    installed: Installed,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str], dict[str, Any]]:
    # through buttereye.cmd, the command-line wrapper, with stdout a pipe
    res = installed.run("cmd", "/c", str(installed.folder / "buttereye.cmd"), "doctor", "--json")
    assert res.stdout.strip(), res.stderr[-2000:]
    findings = {f["id"]: f for f in json.loads(res.stdout)["data"]["findings"]}
    return res, {k: f["severity"] for k, f in findings.items()}, findings


def test_doctor_reports_the_installed_plugins(installed: Installed) -> None:
    res, sev, findings = _doctor(installed)
    for fid in (
        "packages.rife", "packages.mvtools", "packages.mvgpu", "packages.models",
        "packages.licence_files", "packages.vcruntime", "render.ffms2",
    ):  # fmt: skip
        assert sev.get(fid) == "ok", (fid, sev)
    if (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "vulkan-1.dll").exists():
        # the bundled vulkaninfo answers (a timeout would be "vulkan.devices")
        assert sev.get("vulkan.loader") == "ok" and "vulkan.devices" not in sev, sev
    if MPV:  # mpv runs the bundled VapourSynth (VSSCRIPT_PATH set by ButterEye)
        assert sev.get("mpv.vf_vapoursynth") == "ok", sev
        if _no_vulkan_loader():
            # RIFE can't load without vulkan-1.dll and the doctor says why; MVTools must
            plugins = findings["probe.plugins"]
            assert plugins["severity"] == "degraded", plugins
            assert "Vulkan loader" in json.dumps(plugins["title"]), plugins
            assert any(e.startswith("mv: loaded from") for e in plugins["evidence"]), plugins
        else:
            assert sev.get("probe.plugins") == "ok", sev
        assert res.returncode == 0, (res.returncode, sev)


def test_plain_output_survives_a_pipe(installed: Installed) -> None:
    res = installed.run(str(installed.python), "-m", "buttereye.cli", "doctor", "--verbose")
    assert "UnicodeEncodeError" not in res.stderr, res.stderr[-2000:]
    assert "✓" in res.stdout  # the "ok" lines: UTF-8, not the ANSI code page


@pytest.mark.parametrize("backend", ["mvtools", "mvgpu", "rife"])
def test_bundled_vspipe_runs_the_speed_test_script(installed: Installed, backend: str) -> None:
    from buttereye.core.bench import measure

    if backend != "mvtools" and (why := _no_vulkan_loader()):
        pytest.skip(why)
    f = installed.folder
    ud = {
        "backend": backend, "plugin_dir": str(f / "vapoursynth"), "gpu_thread": 2,
        "gpu_id": None, "uhd": False, "matrix": "709", "src_num": 24, "src_den": 1,
        "out_num": 48, "out_den": 1, "width": 640, "height": 360, "frames": 48,
        "model_path": str(f / "share" / "rife-ncnn-models" / MODEL),
    }  # fmt: skip
    vpy = f / "buttereye" / "core" / "bench" / "bench.vpy"
    argv = measure.vspipe_argv(str(installed.vs_dir / "vspipe.exe"), vpy, ud, 48)
    env = installed.env()
    # what ButterEye does for its children (osapi.point_at_runtime)
    env["PATH"] = os.pathsep.join([str(f / "python"), str(installed.vs_dir), env["PATH"]])
    res = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300, check=False)
    if res.returncode != 0 and backend != "mvtools" and "Vulkan" in res.stderr:
        pytest.skip(f"no Vulkan device here: {res.stderr[-200:]}")
    assert res.returncode == 0, res.stderr[-1500:]
    run = measure.parse_vspipe(res.stderr)
    assert run.frames == 48 and run.fps > 0, run


@pytest.mark.skipif(not VIDEO, reason="BUTTEREYE_TEST_VIDEO not set")
def test_bundled_ffms2_reads_a_video(installed: Installed) -> None:
    script = (
        "import sys, vapoursynth as vs\n"
        "core = vs.core\n"
        "core.std.LoadPlugin(sys.argv[1])\n"
        "clip = core.ffms2.Source(sys.argv[2], cache=False)\n"
        "clip.get_frame(clip.num_frames - 1)\n"
        "print(clip.num_frames, clip.width, clip.height)\n"
    )
    res = installed.run(
        str(installed.python), "-c", script, str(installed.folder / "vapoursynth" / "ffms2.dll"),
        VIDEO,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr[-1500:]
    frames, width, height = map(int, res.stdout.split()[-3:])
    assert frames > 0 and width > 0 and height > 0


def test_gui_toolkit_loads(installed: Installed) -> None:
    res = installed.run(str(installed.python), "-m", "buttereye.gui", "--version")
    if res.returncode != 0 and _wine() and "DLL load failed" in res.stderr:
        # Qt 6 imports Windows' own ICU (icuuc.dll, Windows 10 1903+); Wine has none
        pytest.skip("Wine has no icuuc.dll for Qt")
    assert res.returncode == 0, res.stderr[-1500:]
    assert "ButterEye" in res.stdout


def test_uninstalls_silently(installed: Installed) -> None:
    folder = installed.folder
    res = subprocess.run([str(folder / "Uninstall.exe"), "/S"], timeout=300, check=False)
    assert res.returncode == 0
    deadline = time.monotonic() + 180  # the uninstaller copies itself away and returns
    while folder.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    assert not folder.exists(), sorted(str(p) for p in folder.rglob("*"))[:20]
    assert _uninstall_entry() is None
    assert not _start_menu_link().exists()
