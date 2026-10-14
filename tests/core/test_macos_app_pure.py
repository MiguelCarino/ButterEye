# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What ButterEye.app needs from the core (docs/PLATFORMS.md, "The .app"), tested
without the app: the seam recognising that it runs from the bundle, the
environment it hands every child, VSScript's configuration line, and the
doctor's and the plugin search's answers in the app. Every test plants a tree
in ``tmp_path`` or fakes ``osapi.MACOS_APP``, so they run on Linux, Windows and
GitHub's bare macOS runner alike. The real bundle's half is
tests/macos/test_app_bundle.py, behind ``BUTTEREYE_TEST_APP``.

The rule these hold: outside an app nothing changes. Every test that fakes the
app also states what the same call answers with ``MACOS_APP`` None.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi, types
from buttereye.core.doctor import checks_pkgs as pk

# POSIX paths throughout (a bundle is a macOS thing, and Windows' isabs rejects
# "/h"); Linux runs them, which is what keeps them pure
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="a macOS bundle's paths")


def _plant(root: Path, *, record: bool = True, plist: bool = True) -> tuple[Path, Path]:
    """A ButterEye.app skeleton: (its python3.x, a module inside its python)."""
    minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    res = root / "ButterEye.app" / "Contents" / "Resources"
    exe = res / "python" / "bin" / minor
    site = res / "python" / "lib" / minor / "site-packages"
    mod = site / "buttereye" / "core" / "osapi" / "__init__.py"
    vs = site / "vapoursynth"
    for d in (exe.parent, mod.parent, vs, res / "bin", res / "lib"):
        d.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"\xcf\xfa\xed\xfe")
    mod.write_text("")
    (vs / "libvsscript.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    (res / "python" / "lib" / f"lib{minor}.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
    if record:
        (res / osapi.INSTALL_RECORD).write_text('{"version": 1, "components": []}')
    if plist:
        (res.parent / "Info.plist").write_text("<plist/>")
    return exe, mod


# ---------------------------------------------------------------- recognising the app
def test_the_app_is_recognised_by_where_its_python_is(tmp_path: Path) -> None:
    exe, mod = _plant(tmp_path)
    res = tmp_path.resolve() / "ButterEye.app" / "Contents" / "Resources"
    assert osapi.app_resources(str(exe), str(mod)) == res


@pytest.mark.parametrize("missing", ["record", "plist"])
def test_a_bundle_without_the_build_s_marks_is_not_the_app(tmp_path: Path, missing: str) -> None:
    exe, mod = _plant(tmp_path, record=missing != "record", plist=missing != "plist")
    assert osapi.app_resources(str(exe), str(mod)) is None


def test_another_checkout_run_by_the_app_s_python_is_not_the_app(tmp_path: Path) -> None:
    # the module that asks must be inside the same Resources/python: the app's
    # interpreter running some other buttereye must not get the app's plugins
    exe, _ = _plant(tmp_path)
    other = tmp_path / "checkout" / "buttereye" / "core" / "osapi" / "__init__.py"
    other.parent.mkdir(parents=True)
    other.write_text("")
    assert osapi.app_resources(str(exe), str(other)) is None


def test_a_python_elsewhere_in_the_bundle_is_not_the_app(tmp_path: Path) -> None:
    _, mod = _plant(tmp_path)
    stray = tmp_path / "ButterEye.app" / "Contents" / "MacOS" / "python3"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"")
    assert osapi.app_resources(str(stray), str(mod)) is None


def test_a_folder_shaped_like_the_app_but_not_an_app_is_not_the_app(tmp_path: Path) -> None:
    exe, mod = _plant(tmp_path)
    plain = tmp_path / "ButterEye"
    (tmp_path / "ButterEye.app").rename(plain)
    moved = [Path(str(p).replace("ButterEye.app", "ButterEye")) for p in (exe, mod)]
    assert osapi.app_resources(str(moved[0]), str(moved[1])) is None


def test_a_symlink_to_the_app_counts_as_the_app_it_points_at(tmp_path: Path) -> None:
    exe, mod = _plant(tmp_path)
    link = tmp_path / "Link.app"
    link.symlink_to(tmp_path / "ButterEye.app")
    via = Path(str(exe).replace("ButterEye.app", "Link.app"))
    res = (tmp_path / "ButterEye.app" / "Contents" / "Resources").resolve()
    assert osapi.app_resources(str(via), str(mod)) == res


def test_this_process_is_not_the_app() -> None:
    # the test suite runs from a checkout or a venv, never from ButterEye.app
    assert osapi.app_resources(sys.executable, osapi.__file__) is None
    if not (osapi.IS_MACOS and osapi.MACOS_APP is not None):
        assert osapi.MACOS_APP is None


# ---------------------------------------------------------------- the children's environment
def _res(tmp_path: Path) -> Path:
    exe, mod = _plant(tmp_path)
    res = osapi.app_resources(str(exe), str(mod))
    assert res is not None
    return res


def test_children_are_pointed_at_what_the_app_carries(tmp_path: Path) -> None:
    res = _res(tmp_path)
    env = {
        "PATH": "/opt/homebrew/bin:/usr/bin",
        "PYTHONHOME": "/opt/homebrew",
        "PYTHONPATH": "/somewhere",
        "VAPOURSYNTH_EXTRA_PLUGIN_PATH": "/opt/homebrew/lib/vapoursynth",
        "DYLD_LIBRARY_PATH": "/opt/homebrew/lib",
        "DYLD_INSERT_LIBRARIES": "/tmp/x.dylib",
        "DYLD_FRAMEWORK_PATH": "/tmp",
        "VSSCRIPT_PATH": "/opt/homebrew/old/libvsscript.dylib",
    }
    osapi.point_at_app(env, res, sep=":")
    assert env["PATH"].split(":")[0] == str(res / "bin")
    assert env["PATH"].split(":")[1:] == ["/opt/homebrew/bin", "/usr/bin"]
    assert env["VSSCRIPT_PATH"] == str(osapi.app_runtime(res)["vsscript"])
    assert env["VK_DRIVER_FILES"] == str(res / osapi.APP_VULKAN_ICD)
    assert env["PYTHONNOUSERSITE"] == "1"
    for gone in osapi.APP_UNSET:
        assert gone not in env
    # the app sets no DYLD_* variable of its own: that was the Homebrew route's
    assert not [k for k in env if k.startswith("DYLD_")]


def test_pointing_twice_changes_nothing(tmp_path: Path) -> None:
    res = _res(tmp_path)
    env = {"PATH": "/usr/bin", "SSL_CERT_FILE": "/mine.pem"}
    osapi.point_at_app(env, res, sep=":")
    once = dict(env)
    osapi.point_at_app(env, res, sep=":")
    assert env == once
    assert env["SSL_CERT_FILE"] == "/mine.pem"  # the user's own is kept


# ---------------------------------------------------------------- VSScript's configuration
def test_the_vsscript_line_is_the_one_vapoursynth_config_writes() -> None:
    line = osapi.vsscript_entry(
        Path("/A/ButterEye.app/Contents/Resources/python/lib/python3.14/site-packages/"
             "vapoursynth/libvsscript.dylib"),
        Path("/A/ButterEye.app/Contents/Resources/python/bin/python3.14"),
        Path("/A/ButterEye.app/Contents/Resources/python/lib/libpython3.14.dylib"),
        1791923320,
    )  # fmt: skip
    assert line == (
        '"/A/ButterEye.app/Contents/Resources/python/lib/python3.14/site-packages/'
        'vapoursynth/libvsscript.dylib" = ["/A/ButterEye.app/Contents/Resources/python/'
        'bin/python3.14","/A/ButterEye.app/Contents/Resources/python/lib/'
        'libpython3.14.dylib","1791923320"]'
    )
    # VapourSynth reads /lib64/ as /lib/ when it matches the key
    lib64 = osapi.vsscript_entry(Path("/x/lib64/l.dylib"), Path("/p"), Path("/l"), 1)
    assert lib64 is not None and lib64.startswith('"/x/lib/l.dylib"')


def test_a_path_vsscript_cannot_read_is_never_written() -> None:
    # its reader stops a string at the first quote
    assert osapi.vsscript_entry(Path('/a"b/l.dylib'), Path("/p"), Path("/l"), 1) is None
    assert osapi.vsscript_entry(Path("/a/l.dylib"), Path("/p\nq"), Path("/l"), 1) is None


def test_merging_keeps_every_other_installation(tmp_path: Path) -> None:
    tail = "/ButterEye.app/Contents/Resources/python/lib/python3.14/site-packages/vapoursynth"
    gone = f"/Old{tail}/libvsscript.dylib"
    kept = f"/Kept{tail}/libvsscript.dylib"
    text = "\n".join(
        [
            "# a comment",
            '"/opt/homebrew/Cellar/vapoursynth/81/libvsscript.dylib" = ["a","b","1"]',
            f'"{gone}" = ["a","b","1"]',
            f'"{kept}" = ["a","b","1"]',
            '"/Me/libvsscript.dylib" = ["old","old","1"]',
        ]
    )
    line = '"/Me/libvsscript.dylib" = ["new","new","2"]'
    out = osapi.merge_vsscript_config(text, line, exists=lambda p: p == kept)
    lines = out.splitlines()
    assert "# a comment" in lines
    assert any(ln.startswith('"/opt/homebrew/') for ln in lines)  # not ours: kept
    assert not any(gone in ln for ln in lines)  # an app copy that is gone
    assert any(kept in ln for ln in lines)  # an app copy that is still there
    assert lines[-1] == line and sum('"/Me/' in ln for ln in lines) == 1


def test_registering_writes_once_and_then_leaves_the_file_alone(tmp_path: Path) -> None:
    res = _res(tmp_path)
    cfg_home = tmp_path / "cfg"
    env = {"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(cfg_home)}
    assert osapi.vsscript_config(env) == cfg_home / "vapoursynth" / "vapoursynth.toml"
    assert osapi.register_vsscript(env, res)
    toml = cfg_home / "vapoursynth" / "vapoursynth.toml"
    text = toml.read_text()
    rt = osapi.app_runtime(res)
    assert text.startswith(f'"{rt["vsscript"]}" = ["{rt["python"]}","{rt["libpython"]}","')
    before = toml.stat().st_mtime_ns
    os.utime(toml, ns=(before - 10**9, before - 10**9))
    assert osapi.register_vsscript(env, res)
    assert toml.stat().st_mtime_ns == before - 10**9  # not rewritten
    assert not list(toml.parent.glob(".*.tmp"))
    # without XDG_CONFIG_HOME it is ~/.config, as VapourSynth reads it
    assert osapi.vsscript_config({"HOME": "/h"}) == Path("/h/.config/vapoursynth/vapoursynth.toml")
    assert osapi.vsscript_config({}) is None


# ---------------------------------------------------------------- the whole branch
def test_use_bundled_runtime_in_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = _res(tmp_path)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "MACOS_APP", res)
    monkeypatch.setattr(osapi, "vsscript_library", lambda: pytest.fail("Homebrew was asked"))
    # a private copy of the environment: use_bundled_runtime writes variables
    # (VK_DRIVER_FILES, VSSCRIPT_PATH, ...) that monkeypatch.setenv never saw and
    # so would not take back, and a later Vulkan test would inherit them
    env = {k: v for k, v in os.environ.items() if k not in osapi.APP_UNSET}
    env.update(
        XDG_CONFIG_HOME=str(tmp_path / "cfg"),
        PATH="/opt/homebrew/bin:/usr/bin",
        DYLD_LIBRARY_PATH="/opt/homebrew/lib",
    )
    monkeypatch.setattr(os, "environ", env)
    osapi.use_bundled_runtime()
    assert os.environ["PATH"].startswith(str(res / "bin"))
    assert os.environ["VSSCRIPT_PATH"] == str(osapi.app_runtime(res)["vsscript"])
    assert "DYLD_LIBRARY_PATH" not in os.environ
    assert (tmp_path / "cfg" / "vapoursynth" / "vapoursynth.toml").is_file()


def test_programs_come_from_the_app_and_never_from_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = _res(tmp_path)
    (res / "bin" / "mpv").write_bytes(b"")
    monkeypatch.setattr(osapi.shutil, "which", lambda name: f"/opt/homebrew/bin/{name}")
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    # outside the app: PATH, as before
    monkeypatch.setattr(osapi, "MACOS_APP", None)
    assert osapi.executable("mpv") == "/opt/homebrew/bin/mpv"
    monkeypatch.setattr(osapi, "MACOS_APP", res)
    assert osapi.executable("mpv") == str(res / "bin" / "mpv")
    # a program the app should carry and does not is missing, not Homebrew's
    assert osapi.executable("ffmpeg") is None
    # a program the app never carries is still looked up
    assert osapi.executable("mkvmerge") == "/opt/homebrew/bin/mkvmerge"


def test_vspipe_is_the_app_s_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = _res(tmp_path)
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "MACOS_APP", res)
    assert osapi.bundled_vspipe() is None  # the wheel's vspipe is not there
    vspipe = osapi.app_runtime(res)["vspipe"]
    vspipe.write_bytes(b"")
    assert osapi.bundled_vspipe() == str(vspipe)
    assert osapi.vspipe_command() == str(vspipe)
    assert osapi.install_root() == str(res)


# ---------------------------------------------------------------- plugins, models, doctor
def test_plugins_and_models_are_the_app_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    res = _res(tmp_path)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "MACOS_APP", res)
    monkeypatch.setattr(types, "homebrew_prefix", lambda: pytest.fail("Homebrew was asked"))
    assert types.macos_plugin_dirs() == (res / "vapoursynth",)
    assert types.macos_plugin_dir() == res / "vapoursynth"
    assert types._packaged_data_dir() == res / "share"


def test_the_doctor_s_fix_in_the_app_is_a_fresh_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    # ``commands.install_command`` reads ``sys.platform`` itself; on a real Mac the
    # two always agree, so a test that fakes one fakes both
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(osapi, "MACOS_APP", None)
    brew = pk.tool_fix(pk.M("x"), "sudo dnf install mpv", "mpv")
    assert brew["commands"] == ("brew install mpv",)
    monkeypatch.setattr(osapi, "MACOS_APP", Path("/A/ButterEye.app/Contents/Resources"))
    assert pk.tool_fix(pk.M("x"), "sudo dnf install mpv", "mpv") == {
        "fix": pk.APP_REPAIR,
        "commands": (),
    }
    assert pk.platform_fix(pk.M("x"), ("sudo dnf install x",)) == {
        "fix": pk.APP_REPAIR,
        "commands": (),
    }
    # what the app does not carry is still somebody else's to install
    assert pk.tool_fix(pk.M("x"), "sudo dnf install mkvtoolnix", "mkvtoolnix")["commands"] == (
        "brew install mkvtoolnix",
    )


async def test_the_app_s_packages_are_its_install_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = _res(tmp_path)
    (res / osapi.INSTALL_RECORD).write_text(
        '{"version": 1, "components": [{"name": "mpv", "version": "0.41.0_10", '
        '"license": "GPL-2.0-or-later", "provides": [], "licence_files": []}]}'
    )
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "MACOS_APP", res)
    monkeypatch.delenv("BUTTEREYE_HOME", raising=False)
    monkeypatch.setattr(pk, "brew_db", lambda *a: pytest.fail("Homebrew was asked"))
    db = await pk.rpm_query()
    assert db.available
    info = db.get("mpv")
    assert info is not None and info.evr == "0.41.0_10"


def test_a_quarantined_app_is_named_with_the_one_command_that_clears_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = _res(tmp_path)
    (res / "bin" / "mpv").write_bytes(b"")
    (res.parent / "MacOS").mkdir()
    (res.parent / "MacOS" / "ButterEye").write_bytes(b"")
    held = {res / "bin" / "mpv"}
    assert osapi.app_quarantined(res, has_quarantine=lambda p: p in held) == (
        res / "bin" / "mpv",
    )
    assert osapi.app_quarantined(res, has_quarantine=lambda p: False) == ()
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: (r / "bin" / "mpv",))
    f = pk.app_quarantine_finding(res)
    assert f is not None and f.severity.value == "blocking"
    assert f.commands == (f"xattr -dr com.apple.quarantine {res.parent.parent}",)
    monkeypatch.setattr(osapi, "app_quarantined", lambda r: ())
    assert pk.app_quarantine_finding(res) is None


def test_first_time_setup_clears_the_whole_app_only_when_something_is_held(
    tmp_path: Path,
) -> None:
    res = _res(tmp_path)
    (res / "bin" / "mpv").write_bytes(b"")
    app = res.parent.parent
    ran: list[list[str]] = []

    class Done:
        def __init__(self, code: int) -> None:
            self.returncode = code
            self.stderr = b"xattr: [Errno 30] Read-only file system"

    def run(code: int) -> Any:
        def fake(argv: list[str], **_kw: Any) -> Done:
            ran.append(argv)
            return Done(code)

        return fake

    held = lambda p: p == res / "bin" / "mpv"  # noqa: E731
    assert osapi.release_app(res, has_quarantine=held, run=run(0)) is True
    assert ran == [["/usr/bin/xattr", "-dr", "com.apple.quarantine", str(app)]]
    # nothing held: nothing run
    ran.clear()
    assert osapi.release_app(res, has_quarantine=lambda p: False, run=run(0)) is False
    assert ran == []
    # a translocated (read-only) copy: xattr fails, which is reported, never raised
    assert osapi.release_app(res, has_quarantine=held, run=run(1)) is False

    def broken(*_a: Any, **_kw: Any) -> Done:
        raise OSError("no xattr")

    assert osapi.release_app(res, has_quarantine=held, run=broken) is False
