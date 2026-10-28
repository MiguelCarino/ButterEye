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
from types import SimpleNamespace
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
    root = tmp_path / "appimage_extracted_0123abcd"
    exe, mod = _plant(root)
    assert osapi.appimage_root(str(exe), str(mod)) == root.resolve()


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


def test_appdir_alone_makes_nothing_an_appimage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    vss = "lib/python3.14/site-packages/vapoursynth/libvsscript.so"
    gone = f'"/tmp/.mount_ButterAbCdEf/python/{vss}" = ["a","b","1"]'
    kept = f'"/home/u/venv/{vss}" = ["a","b","1"]'
    new = f'"/tmp/.mount_ButterXyZ123/python/{vss}" = ["c","d","2"]'
    out = osapi.merge_vsscript_config(f"{gone}\n{kept}\n", new, exists=lambda p: False)
    assert out.splitlines() == [kept, new]


def test_inside_the_appimage_the_doctor_offers_no_dnf(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    """TensorRT: one INFO row, not two DEGRADED ones about API 3. RIFE: the
    AppImage never carries it, so "download it again" would do nothing, and the
    advice says where RIFE is instead."""
    from buttereye.core.doctor import checks_trt
    from buttereye.core.types import Severity

    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "LINUX_APP", tmp_path)
    off = checks_trt.trt_off_finding()
    assert off.id == "trt.off" and off.severity is Severity.INFO
    assert "AppImage" in render(off.title)
    paths: Any = SimpleNamespace(rpm_plugin_dir=tmp_path / "vapoursynth",
                                 rpm_data_dir=tmp_path / "share")  # fmt: skip
    rows = pk.package_findings(pk.RpmDb(True, {}), paths)
    rife = next(f for f in rows if f.id == "packages.rife")
    assert rife.severity is Severity.INFO
    assert (rife.fix, rife.commands) == (pk.APPIMAGE_NO_RIFE, ())
    assert "mvgpu" in render(pk.APPIMAGE_NO_RIFE) and "RIFE" in render(pk.APPIMAGE_NO_RIFE)


def test_the_appimage_carries_mvgpu_so_a_missing_one_is_a_damaged_copy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """build-appimage.sh puts libmvgpu.so in vapoursynth/ (B12): found there it is
    OK with the install record's version; missing, the fix is a fresh copy and
    nothing to type, never "use the Fedora packages"."""
    from buttereye.core.types import Severity

    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    monkeypatch.setattr(osapi, "LINUX_APP", tmp_path)
    plugins = tmp_path / "vapoursynth"
    plugins.mkdir()
    paths: Any = SimpleNamespace(rpm_plugin_dir=plugins)
    gone = pk.mvgpu_finding(pk.RpmDb(True, {}), paths)
    assert gone.severity is Severity.INFO
    assert (gone.fix, gone.commands) == (pk.APPIMAGE_REPAIR, ())
    (plugins / pk.MVGPU_SO).write_bytes(b"")
    found = pk.mvgpu_finding(pk.RpmDb(True, {}), paths)
    assert found.severity is Severity.OK
    assert found.evidence == (str(plugins / pk.MVGPU_SO),)


def test_a_mac_outside_the_app_is_told_where_mvgpu_comes_from(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ButterEye.app ships libmvgpu.dylib since 0.0.6, so "ButterEye doesn't build
    mvgpu for macOS yet" is no longer true (A3). Inside the app a missing plugin is
    a damaged app; outside it, the advice names the app and offers no command."""
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "LINUX_APP", None)
    paths: Any = SimpleNamespace(rpm_plugin_dir=tmp_path)
    from buttereye.core import paths as _paths

    monkeypatch.setattr(_paths, "plugin_dirs", lambda _p: (tmp_path,))
    monkeypatch.setattr(osapi, "MACOS_APP", None)
    outside = pk.mvgpu_finding(pk.RpmDb(True, {}), paths)
    text = render(outside.fix)
    assert "ButterEye.app" in text and "yet" not in text
    assert outside.commands == ()
    monkeypatch.setattr(osapi, "MACOS_APP", tmp_path / "ButterEye.app")
    inside = pk.mvgpu_finding(pk.RpmDb(True, {}), paths)
    assert (inside.fix, inside.commands) == (pk.APP_REPAIR, ())


# ---------------------------------------------------------------- the build script
# build-appimage.sh only runs in an ubuntu:22.04 container, so these read it as
# text: the NVENC encoders (B2) and mvgpu (B12) are configured, checked and
# recorded, and the guards that keep NVIDIA's libraries out stay in place.
BUILD = Path(__file__).resolve().parents[2] / "packaging" / "appimage" / "build-appimage.sh"
STAGE = Path(__file__).resolve().parents[2] / "packaging" / "appimage" / "stage.py"


def _ffmpeg_flags(text: str) -> list[str]:
    start = text.index("ffmpeg_flags=(")
    return text[start : text.index("\n)", start)].split()[1:]


def test_the_appimage_ffmpeg_has_nvenc_and_checks_it() -> None:
    text = BUILD.read_text(encoding="utf-8")
    flags = _ffmpeg_flags(text)
    assert {"--enable-ffnvcodec", "--enable-nvdec", "--enable-nvenc"} <= set(flags)
    assert "--disable-autodetect" in flags and "--enable-nonfree" not in flags
    start = text.index("for want in CONFIG_GPL")
    loop = text[start : text.index("; do", start)]
    for want in ("CONFIG_NVENC", "CONFIG_HEVC_NVENC_ENCODER", "CONFIG_AV1_NVENC_ENCODER"):
        assert want in loop.split()
    # no libav* may link a driver library: FFmpeg dlopens libnvidia-encode too
    assert "lib(cuda|nvcuvid|nvidia-encode|vulkan)\\.so" in text


def test_the_appimage_builds_mvgpu_with_its_own_glslang() -> None:
    text = BUILD.read_text(encoding="utf-8")
    assert "-DENABLE_GLSLANG_BINARIES=ON" in text and "-DENABLE_GLSLANG_BINARIES=OFF" not in text
    setup = text[text.index("logged mvgpu-setup") : text.index("logged mvgpu-build")]
    assert '"$repo/mvgpu"' in setup
    assert '-Dglslang="$deps/bin/glslang"' in setup
    assert "-Dplugindir=lib/buttereye/vapoursynth" in setup
    assert 'cp "$mvgpu_so" "$appdir/vapoursynth/libmvgpu.so"' in text
    # built before the AppDir and after what it compiles against
    marks = (
        "logged glslang-build",
        "PKG_CONFIG_PATH=$PKG_CONFIG_PATH:$vsdir",
        "logged mvgpu-setup",
        "-- 2. the AppDir",
    )
    order = [text.index(m) for m in marks]
    assert order == sorted(order)
    # the RPM's %check, here too
    assert "VapourSynthPluginInit2" in text[text.index("logged mvgpu-build") :]


def test_the_appimage_records_and_offers_mvgpu() -> None:
    text = BUILD.read_text(encoding="utf-8")
    row = '"buttereye-vs-mvgpu=$mvgpu_ver=AGPL-3.0-or-later=$(files_of buttereye-vs-mvgpu)"'
    assert row in text
    assert 'cp "$repo/COPYING" "$lic/buttereye-vs-mvgpu/COPYING"' in text
    assert ",buttereye-vs-mvgpu," in text[text.index('mkdir -p "$lic"/{') :].splitlines()[0]
    assert pk.PKG_MVGPU == "buttereye-vs-mvgpu"
    # mvgpu/ is in the Corresponding Source (ButterEye's own tree) and the offer says so
    assert "THIRD-PARTY.md packaging mvgpu" in text
    assert 'mvgpu="$mvgpu_ver"' in text
    sources = STAGE.read_text(encoding="utf-8")
    assert "mvgpu {mvgpu}" in sources and "vapoursynth/libmvgpu.so" in sources


def test_the_source_offer_gets_every_value_it_names(tmp_path: Path) -> None:
    """``stagepy sources-txt`` fills SOURCES.txt with ``str.format``: a name the
    text uses that the build does not pass (``mvgpu``, new with B12) would stop
    the build at its very end. The call in build-appimage.sh is read as text and
    run through stage.py's own sources_txt with dummy values."""
    import importlib.util
    import re

    spec = importlib.util.spec_from_file_location("appimage_stage", STAGE)
    assert spec is not None and spec.loader is not None
    stage = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stage)
    text = BUILD.read_text(encoding="utf-8")
    start = text.index("stagepy sources-txt")
    call = text[start : text.index("\n\n", start)]
    keys = re.findall(r"\s([a-z0-9_]+)=", call)
    assert "mvgpu" in keys and "debs" in keys
    debs = tmp_path / "deb-sources.txt"
    debs.write_text("libass=1:0.15.2-1\n", encoding="utf-8")
    values = [f"{k}={debs if k == 'debs' else k.upper()}" for k in keys]
    out = tmp_path / "SOURCES.txt"
    stage.sources_txt(out, values)
    offer = out.read_text(encoding="utf-8")
    assert "mvgpu MVGPU (AGPL-3.0-or-later): vapoursynth/libmvgpu.so" in " ".join(offer.split())
    assert "NVDEC and NVENC" in offer
