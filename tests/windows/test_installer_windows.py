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
registers itself under this user's HKCU, adds a Start-menu shortcut and offers
ButterEye under "Open with" for the common video types (a ProgID of its own);
the uninstall at the end removes all three.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
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
PROGID = "ButterEye.Video"
VIDEO_TYPES = ("mp4", "mkv", "webm", "mov", "avi", "ts")
PLUGINS = ("mvtools.dll", "librife.dll", "libmvgpu.dll", "ffms2.dll")
MODEL = "rife-v4.22_lite_ensembleFalse"


def _wine() -> bool:
    import ctypes

    ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined,unused-ignore]
    return hasattr(ntdll, "wine_get_version")


def _no_vulkan(*, device: bool = True) -> str | None:
    """Why the GPU plugins can't run here, or None: librife.dll and libmvgpu.dll
    need a Vulkan device (vulkan-1.dll, from a graphics driver, and a driver that
    lists a GPU; GitHub's runners have the loader but no driver)."""
    import ctypes

    try:
        vk = ctypes.WinDLL("vulkan-1")  # type: ignore[attr-defined,unused-ignore]
    except OSError as exc:
        return f"no Vulkan loader (vulkan-1.dll) on this PC: {exc}"
    if not device:
        return None

    class InstanceInfo(ctypes.Structure):  # VkInstanceCreateInfo, nothing enabled
        _fields_ = [
            ("sType", ctypes.c_int), ("pNext", ctypes.c_void_p), ("flags", ctypes.c_uint32),
            ("pApplicationInfo", ctypes.c_void_p), ("enabledLayerCount", ctypes.c_uint32),
            ("ppEnabledLayerNames", ctypes.c_void_p), ("enabledExtensionCount", ctypes.c_uint32),
            ("ppEnabledExtensionNames", ctypes.c_void_p),
        ]  # fmt: skip

    instance = ctypes.c_void_p()
    rc = vk.vkCreateInstance(ctypes.byref(InstanceInfo(1)), None, ctypes.byref(instance))
    if rc != 0:  # -9: VK_ERROR_INCOMPATIBLE_DRIVER, a loader but no driver
        return f"Vulkan has no driver on this PC (vkCreateInstance returned {rc})"
    try:
        count = ctypes.c_uint32()
        vk.vkEnumeratePhysicalDevices(instance, ctypes.byref(count), None)
        if not count.value:
            return "Vulkan lists no device on this PC"
    finally:
        vk.vkDestroyInstance(instance, None)
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


def _registered_location() -> str | None:
    """The uninstall entry's InstallLocation (any entry at all: "" if it has none)."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTKEY) as key:
            try:
                return str(winreg.QueryValueEx(key, "InstallLocation")[0])
            except OSError:
                return ""
    except OSError:
        return None


def _reg_value(subkey: str, name: str = "") -> str | None:
    """A value under HKCU (``""`` is the key's default value), None when the key
    or the value is missing."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return None


def _open_with_command() -> str | None:
    return _reg_value(rf"Software\Classes\{PROGID}\shell\open\command")


def _offered_for() -> list[str]:
    """The video types whose OpenWithProgids name ButterEye's ProgID."""
    return [
        ext
        for ext in VIDEO_TYPES
        if _reg_value(rf"Software\Classes\.{ext}\OpenWithProgids", PROGID) is not None
    ]


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


def _nsis(exe: Path | str, *args: str, folder: Path, key: str = "/D") -> str:
    """An NSIS command line: ``/D=`` (or ``_?=``) must come last and unquoted, even
    with spaces in it (a quoted one is ignored, and the installer would go to the
    default folder). A list argument would be quoted by ``subprocess``."""
    return " ".join([subprocess.list2cmdline([str(exe), *args]), f"{key}={folder}"])


def _setup(*args: str, folder: Path) -> int:
    cmd = _nsis(INSTALLER, *args, folder=folder)
    return subprocess.run(cmd, timeout=900, check=False).returncode


def _uninstall(folder: Path, *, uninstaller: Path | None = None) -> int:
    """The uninstaller for ``folder``, waiting for it (``_?=``)."""
    exe = uninstaller or folder / "Uninstall.exe"
    cmd = _nsis(exe, "/S", folder=folder, key="_?")
    return subprocess.run(cmd, timeout=300, check=False).returncode


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Installed]:
    root = tmp_path_factory.mktemp("installer")
    # spaces and non-ASCII letters, as in many a Windows user name
    folder = root / "Butter Eye ñ"
    registered = _registered_location()
    if registered is not None and os.path.normcase(registered) != os.path.normcase(str(folder)):
        # one ButterEye per user: this test's copy would be refused, or would take
        # over the entry and shortcuts of the developer's own and remove them
        pytest.skip(f"ButterEye is registered for this user in {registered!r}")
    profile = root / "profile"
    (profile / "Roaming").mkdir(parents=True)
    (profile / "Local").mkdir()
    rc = _setup("/S", folder=folder)
    assert rc == 0, f"setup.exe /S exited with {rc}"
    assert (folder / "buttereye-install.json").is_file(), "not installed into the chosen folder"
    inst = Installed(folder, profile)
    yield inst
    if (folder / "Uninstall.exe").exists():  # a test failed before the uninstall test
        copy = root / "Uninstall-copy.exe"  # _?= runs it in place: run a copy
        shutil.copy2(folder / "Uninstall.exe", copy)
        _uninstall(folder, uninstaller=copy)


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


def test_registers_under_open_with_for_the_video_types(installed: Installed) -> None:
    f = installed.folder
    pythonw = f / "python" / "pythonw.exe"
    command = _open_with_command()
    assert command is not None
    assert os.path.normcase(command) == os.path.normcase(f'"{pythonw}" -m buttereye.gui "%1"')
    icon = _reg_value(rf"Software\Classes\{PROGID}\DefaultIcon")
    assert icon is not None and os.path.normcase(icon) == os.path.normcase(str(f / "buttereye.ico"))
    assert _reg_value(rf"Software\Classes\{PROGID}", "FriendlyTypeName") == "ButterEye video"
    assert _reg_value(rf"Software\Classes\{PROGID}\shell\open", "FriendlyAppName") == "ButterEye"
    assert _reg_value(rf"Software\Classes\{PROGID}\Application", "ApplicationName") == "ButterEye"
    assert _offered_for() == list(VIDEO_TYPES)
    # offered, never made the default: the extension's own default is untouched
    assert _reg_value(r"Software\Classes\.mkv") != PROGID
    # not on every pythonw of every Python on this PC
    assert _reg_value(r"Software\Classes\Applications\pythonw.exe\shell\open\command") is None


def test_install_record_lists_every_component_with_its_licences(installed: Installed) -> None:
    record = json.loads((installed.folder / "buttereye-install.json").read_text("utf-8"))
    names = {c["name"] for c in record["components"]}
    assert {
        "buttereye", "buttereye-vs-rife-ncnn", "buttereye-vs-mvtools", "buttereye-vs-mvgpu",
        "buttereye-rife-ncnn-models", "ffms2", "vulkan-tools", "mingw-w64-runtime",
        "python3", "python3-pyside6", "python3-shiboken6", "microsoft-vcruntime",
        "vapoursynth-libs", "vapoursynth-tools",
    } <= names  # fmt: skip
    for c in record["components"]:
        assert c["licence_files"], c["name"]
        for rel in c["licence_files"]:
            assert (installed.folder / rel).is_file(), rel
    qt = next(c for c in record["components"] if c["name"] == "python3-pyside6")
    assert qt["license"].startswith("LGPL-3.0-only AND ")
    notices = [f for f in qt["licence_files"] if "/third-party/" in f]
    assert len(notices) > 20 and "licenses/pyside6/third-party/freetype.txt" in notices


def test_ships_no_microsoft_runtime_but_pythons(installed: Installed) -> None:
    import re

    vc = re.compile(r"^(vcruntime140|msvcp140|concrt140|vcomp140|vcamp140|vccorlib140)", re.I)
    found = sorted(
        str(p.relative_to(installed.folder)).replace(os.sep, "/").lower()
        for p in installed.folder.rglob("*.dll")
        if vc.match(p.name)
    )
    assert found == ["python/vcruntime140.dll", "python/vcruntime140_1.dll"]


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
    if not _no_vulkan():
        # the bundled vulkaninfo answers (a timeout would be "vulkan.devices")
        assert sev.get("vulkan.loader") == "ok" and "vulkan.devices" not in sev, sev
    if MPV:  # mpv runs the bundled VapourSynth (VSSCRIPT_PATH set by ButterEye)
        assert sev.get("mpv.vf_vapoursynth") == "ok", sev
        plugins = findings["probe.plugins"]
        if _no_vulkan(device=False):
            # RIFE can't load without vulkan-1.dll and the doctor says why
            assert plugins["severity"] == "degraded", plugins
            assert "Vulkan loader" in json.dumps(plugins["title"]), plugins
        elif not _no_vulkan():
            assert plugins["severity"] == "ok", plugins
        # with or without a GPU, MVTools loads inside mpv from the install folder
        assert any(e.startswith("mv: loaded from") for e in plugins["evidence"]), plugins
        assert res.returncode == 0, (res.returncode, sev)


def test_plain_output_survives_a_pipe(installed: Installed) -> None:
    res = installed.run(str(installed.python), "-m", "buttereye.cli", "doctor", "--verbose")
    assert "UnicodeEncodeError" not in res.stderr, res.stderr[-2000:]
    assert "✓" in res.stdout  # the "ok" lines: UTF-8, not the ANSI code page


@pytest.mark.parametrize("backend", ["mvtools", "mvgpu", "rife"])
def test_bundled_vspipe_runs_the_speed_test_script(installed: Installed, backend: str) -> None:
    from buttereye.core.bench import measure

    if backend != "mvtools" and (why := _no_vulkan()):
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


def _no_icu() -> bool:
    """Qt 6 imports Windows' own ICU (icuuc.dll, Windows 10 1903+); Wine has none."""
    import ctypes

    try:
        ctypes.WinDLL("icuuc")  # type: ignore[attr-defined,unused-ignore]
    except OSError:
        return True
    return False


#: Qt with the system's C++ runtime: the SVG icons through the shipped plugins
_QT_PROBE = """\
import sys
from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QImageReader
from PySide6.QtWidgets import QApplication
app = QApplication([sys.argv[0], "-platform", "offscreen"])
formats = {bytes(f).decode() for f in QImageReader.supportedImageFormats()}
assert "svg" in formats and "jpeg" not in formats, sorted(formats)
icon = QIcon(sys.argv[1])
assert not icon.pixmap(QSize(32, 32)).isNull(), "the SVG icon did not render"
print("qt ok", app.platformName())
"""


def test_gui_toolkit_loads(installed: Installed) -> None:
    if _wine() and _no_icu():
        pytest.skip("Wine has no icuuc.dll for Qt")
    res = installed.run(str(installed.python), "-m", "buttereye.gui", "--version")
    assert res.returncode == 0, res.stderr[-1500:]
    assert "ButterEye" in res.stdout
    source = next(ln for ln in res.stdout.splitlines() if ln.startswith("Source: "))
    assert os.path.samefile(
        source.removeprefix("Source: "), installed.folder / "licenses" / "SOURCES.txt"
    )
    svg = installed.folder / "buttereye" / "gui" / "icons" / "buttereye.svg"
    res = installed.run(str(installed.python), "-c", _QT_PROBE, str(svg))
    assert res.returncode == 0, res.stderr[-1500:]
    assert "qt ok offscreen" in res.stdout


def test_refuses_a_second_copy_in_another_folder(installed: Installed, tmp_path: Path) -> None:
    other = tmp_path / "Other ButterEye"
    assert _setup("/S", folder=other) == 2
    assert not (other / "buttereye-install.json").exists()
    entry = _uninstall_entry()
    assert entry is not None
    assert os.path.normcase(entry["InstallLocation"]) == os.path.normcase(str(installed.folder))


def test_an_uninstaller_for_another_folder_leaves_the_entry_and_shortcut(
    installed: Installed, tmp_path: Path
) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    copy = tmp_path / "Uninstall-copy.exe"
    shutil.copy2(installed.folder / "Uninstall.exe", copy)
    assert _uninstall(other, uninstaller=copy) == 0
    entry = _uninstall_entry()
    assert entry is not None
    assert os.path.normcase(entry["InstallLocation"]) == os.path.normcase(str(installed.folder))
    assert _start_menu_link().is_file()
    assert (installed.folder / "python" / "python.exe").is_file()
    # and the active copy's "Open with" registration
    assert _open_with_command() is not None and _offered_for() == list(VIDEO_TYPES)


def _hold_mutex(name: str) -> int:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    handle = k32.CreateMutexW(None, 0, name)
    assert handle, ctypes.get_last_error()  # type: ignore[attr-defined,unused-ignore]
    return int(handle)


def _close(handle: int) -> None:
    k32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined,unused-ignore]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.CloseHandle(handle)


def _blocked(installed: Installed, tmp_path: Path) -> None:
    """Repair and uninstall both refuse (exit code 4) and change nothing."""
    f = installed.folder
    before = sorted(p.name for p in f.iterdir())
    assert _setup("/S", folder=f) == 4
    tmp_path.mkdir(parents=True, exist_ok=True)
    copy = tmp_path / "Uninstall-copy.exe"
    shutil.copy2(f / "Uninstall.exe", copy)
    assert _uninstall(f, uninstaller=copy) == 4
    assert sorted(p.name for p in f.iterdir()) == before
    assert (f / "python" / "python.exe").is_file()
    assert (f / "vapoursynth" / "mvtools.dll").is_file()
    assert _uninstall_entry() is not None


def test_a_running_buttereye_blocks_repair_and_uninstall(
    installed: Installed, tmp_path: Path
) -> None:
    from buttereye.core import osapi
    from buttereye.core.osapi import _win32

    # the name the installer builds from its folder is the one the core holds
    name = osapi.install_mutex_name(_win32.long_path(str(installed.folder)))
    handle = _hold_mutex(name)
    try:
        _blocked(installed, tmp_path / "mutex")
    finally:
        _close(handle)
    # the installed ButterEye's core holds it itself (and has its files open)
    hold = (
        "import sys\n"
        "from buttereye.core import osapi\n"
        "osapi.use_bundled_runtime()\n"
        "print('ready', flush=True)\n"
        "sys.stdin.read()\n"
    )
    env = installed.env()
    p = subprocess.Popen(
        [str(installed.python), "-c", hold], env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == "ready"
        _blocked(installed, tmp_path / "core")
    finally:
        p.communicate("", timeout=60)


def test_a_player_using_its_files_blocks_repair(installed: Installed, tmp_path: Path) -> None:
    # no ButterEye, no mutex: only a process with the bundled Python's files
    # loaded, as mpv has VapourSynth's while it plays
    p = subprocess.Popen(
        [str(installed.python), "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        env=installed.env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        assert p.stdout is not None and p.stdout.readline().strip() == "ready"
        _blocked(installed, tmp_path)
    finally:
        p.communicate("", timeout=60)
    # closed: the repair goes through
    assert _setup("/S", folder=installed.folder) == 0
    assert (installed.folder / "buttereye-install.json").is_file()


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
    assert _reg_value(rf"Software\Classes\{PROGID}") is None
    assert _open_with_command() is None and _offered_for() == []
