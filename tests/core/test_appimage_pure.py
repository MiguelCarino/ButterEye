# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What the Linux AppImage trial needs from the core (docs/appimage-trial.md),
tested without an AppImage: the seam recognising that it runs from one (and not
from ``$APPDIR``), the environment it hands every child, VSScript's
configuration lines, and the doctor's advice inside it. Every test plants a tree
in ``tmp_path`` or fakes ``osapi.LINUX_APP``, so they run anywhere POSIX. The real
AppImage's half is packaging/appimage/appimage_tests.py, in CI.

The rule these hold, as tests/core/test_macos_app_pure.py holds it for the
macOS app: outside an AppImage nothing changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.i18n import render
from buttereye.core.types import Msg

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership and paths")


def _plant(root: Path, *, record: bool = True, apprun: bool = True) -> tuple[Path, Path]:
    """An AppDir skeleton: (its python3.x, a module inside its python)."""
    minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    exe = root / "python" / "bin" / minor
    site = root / "python" / "lib" / minor / "site-packages"
    mod = site / "buttereye" / "core" / "osapi" / "__init__.py"
    for d in (exe.parent, mod.parent, site / "vapoursynth", root / "bin"):
        d.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"\x7fELF")
    mod.write_text("")
    if record:
        (root / osapi.INSTALL_RECORD).write_text('{"version": 1, "components": []}')
    if apprun:
        (root / "AppRun").write_text("#!/bin/sh\n")
    for d in (root, root / "python"):
        d.chmod(0o755)
    return exe, mod


def test_the_appimage_is_recognised_by_where_its_python_is(tmp_path: Path) -> None:
    exe, mod = _plant(tmp_path / "appimage_extracted_0123abcd")
    assert osapi.appimage_root(str(exe), str(mod)) == (tmp_path / "appimage_extracted_0123abcd").resolve()


@pytest.mark.parametrize("missing", ["record", "apprun"])
def test_a_tree_without_the_build_s_marks_is_not_the_appimage(tmp_path: Path, missing: str) -> None:
    exe, mod = _plant(tmp_path, record=missing != "record", apprun=missing != "apprun")
    assert osapi.appimage_root(str(exe), str(mod)) is None


def test_another_checkout_run_by_its_python_is_not_the_appimage(tmp_path: Path) -> None:
    exe, _ = _plant(tmp_path / "img")
    other = tmp_path / "checkout" / "buttereye" / "core" / "osapi" / "__init__.py"
    other.parent.mkdir(parents=True)
    other.write_text("")
    assert osapi.appimage_root(str(exe), str(other)) is None


def test_appdir_alone_makes_nothing_an_appimage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the runtime exports APPDIR, and so can anyone: pointing it at a perfect
    # planted tree changes nothing, because the answer comes from where the
    # interpreter running this code is, and that is not in the tree
    _plant(tmp_path / "planted")
    monkeypatch.setenv("APPDIR", str(tmp_path / "planted"))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "x.AppImage"))
    assert osapi.appimage_root(sys.executable, osapi.__file__) is None


@pytest.mark.parametrize("folder", ["root", "python"])
def test_a_tree_others_can_write_to_is_not_trusted(tmp_path: Path, folder: str) -> None:
    root = tmp_path / "appimage_extracted_feed"
    exe, mod = _plant(root)
    (root if folder == "root" else root / "python").chmod(0o777)
    assert osapi.appimage_root(str(exe), str(mod)) is None


def test_a_tree_owned_by_someone_else_is_not_trusted(tmp_path: Path) -> None:
    exe, mod = _plant(tmp_path)
    # the tree is ours; seen by another (non-root) user it is somebody else's
    assert osapi.appimage_root(str(exe), str(mod), uid=os.getuid() + 4242) is None


def test_the_children_get_the_appimage_s_programs_and_vsscript(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin:/bin", "LD_LIBRARY_PATH": "/opt/ffmpeg/lib", "LD_PRELOAD": "x.so",
           "PYTHONPATH": "/x", "VAPOURSYNTH_EXTRA_PLUGIN_PATH": "/y"}  # fmt: skip
    osapi.point_at_appimage(env, tmp_path)
    osapi.point_at_appimage(env, tmp_path)  # idempotent
    assert env["PATH"] == f"{tmp_path / 'bin'}:/usr/bin:/bin"
    assert env["VSSCRIPT_PATH"].endswith("/site-packages/vapoursynth/libvsscript.so")
    assert env["PYTHONNOUSERSITE"] == "1"
    for gone in osapi.APPIMAGE_UNSET:
        assert gone not in env


def test_stale_appimage_vsscript_lines_go_and_others_stay() -> None:
    gone = '"/tmp/.mount_ButterAbCdEf/python/lib/python3.14/site-packages/vapoursynth/libvsscript.so" = ["a","b","1"]'
    kept = '"/home/u/venv/lib/python3.14/site-packages/vapoursynth/libvsscript.so" = ["a","b","1"]'
    new = '"/tmp/.mount_ButterXyZ123/python/lib/python3.14/site-packages/vapoursynth/libvsscript.so" = ["c","d","2"]'
    out = osapi.merge_vsscript_config(f"{gone}\n{kept}\n", new, exists=lambda p: False)
    assert out.splitlines() == [kept, new]


def test_inside_the_appimage_the_doctor_offers_no_dnf(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "LINUX_APP", None)
    assert pk.platform_fix(Msg("Install it."), ("sudo dnf install x",))["commands"] == (
        "sudo dnf install x",
    )
    monkeypatch.setattr(osapi, "LINUX_APP", tmp_path)
    assert pk.platform_fix(Msg("Install it."), ("sudo dnf install x",)) == {
        "fix": pk.APPIMAGE_REPAIR,
        "commands": (),
    }
    carried = pk.tool_fix(Msg("Install mpv."), "sudo dnf install mpv", "mpv")
    assert carried == {"fix": pk.APPIMAGE_REPAIR, "commands": ()}
    other = pk.tool_fix(Msg("Install mkvtoolnix."), "sudo dnf install mkvtoolnix", "mkvtoolnix")
    assert other == {"fix": Msg("Install mkvtoolnix."), "commands": ()}


def test_the_appimage_never_looks_for_vstrt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A vstrt built for the host's VapourSynth is on disk, but the AppImage's R81
    refuses its API 3: found, it was picked for live play and every session rolled
    back. Outside the AppImage the same tree is found as before."""
    from types import SimpleNamespace

    from buttereye.core.backends import trt

    data = tmp_path / "data"
    lib = data / trt.VSTRT_SUBDIR / "v16" / osapi.VSTRT_LIB
    lib.parent.mkdir(parents=True)
    lib.write_bytes(b"")
    paths: Any = SimpleNamespace(data_dir=data)
    monkeypatch.setattr(osapi, "LINUX_APP", None)
    assert trt._vstrt_dirs(paths) == [lib.parent]
    monkeypatch.setattr(osapi, "LINUX_APP", tmp_path / "app")
    assert trt._vstrt_dirs(paths) == []


def test_the_appimage_s_doctor_is_honest_about_what_it_lacks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """TensorRT: one INFO row, not two DEGRADED ones about API 3. RIFE and mvgpu:
    the AppImage never carries them, so "download it again" would do nothing."""
    from buttereye.core.doctor import checks_trt
    from buttereye.core.types import Severity

    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "LINUX_APP", tmp_path)
    off = checks_trt.trt_off_finding()
    assert off.id == "trt.off" and off.severity is Severity.INFO
    assert "AppImage" in render(off.title)
    no_gpu = pk.platform_fix(
        Msg("Install it."), ("sudo dnf install x",), appimage=pk.APPIMAGE_NO_GPU
    )
    assert no_gpu == {"fix": pk.APPIMAGE_NO_GPU, "commands": ()}
