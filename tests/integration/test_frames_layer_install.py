# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""VK_LAYER_BUTTEREYE_frames as ``meson install`` writes it (the RPM's and the
AppImage's layout), on the CPU.

Builds mvgpu with ``-Dlayer=enabled`` and installs it twice into temporary folders:
the package layout (``--prefix=/usr``: the library in ``lib64/buttereye``, the manifest
in ``share/vulkan/explicit_layer.d`` naming it by absolute path) and the relocatable one
(``-Dlayer_library_path=relative``: the manifest names it relative to itself, as the
AppImage needs). Checks the files, that the manifest stays an explicit layer, that
``osapi.frames_layer`` finds each where ButterEye looks, and, through the layer's
headless test program on lavapipe, that the installed layer loads only when asked for."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from buttereye.core import osapi

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the layer is Linux only"),
]

REPO = Path(__file__).resolve().parents[2]
LVP = Path("/usr/share/vulkan/icd.d/lvp_icd.x86_64.json")
LIB = "libVkLayer_buttereye_frames.so"
LIB_REL = Path("lib64/buttereye") / LIB
MANIFEST_REL = Path("share/vulkan/explicit_layer.d") / osapi.FRAMES_MANIFEST
PREFIX = "buttereye-frames: "


def _meson(*args: str) -> None:
    subprocess.run(["meson", *args], check=True, capture_output=True)


def _ninja(out: Path) -> None:
    subprocess.run(
        [
            "ninja",
            "-C",
            str(out),
            "-j4",
            "libmvgpu.so",
            f"layer/{LIB}",
            "layer/frames-present-test",
        ],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """``pkg`` and ``rel``: DESTDIR trees of the two installs (prefix /usr inside each);
    ``build``: the build folder (for frames-present-test)."""
    for tool in ("meson", "ninja"):
        if not shutil.which(tool):
            pytest.skip(f"{tool} is not installed")
    top = tmp_path_factory.mktemp("frames-install")
    out = top / "build"
    glslang = shutil.which("glslang") or shutil.which("glslangValidator") or "glslang"
    _meson(
        "setup",
        str(out),
        str(REPO / "mvgpu"),
        "--prefix=/usr",
        "--libdir=lib64",
        "--datadir=share",
        "-Dplugindir=lib64/buttereye/vapoursynth",
        "-Dlayer=enabled",
        f"-Dglslang={glslang}",
    )
    _ninja(out)
    trees = {"build": out}
    for name, mode in (("pkg", "absolute"), ("rel", "relative")):
        _meson("configure", str(out), f"-Dlayer_library_path={mode}")
        _ninja(out)  # regenerates the installed manifest
        _meson("install", "-C", str(out), "--no-rebuild", "--destdir", str(top / name))
        trees[name] = top / name
    return trees


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def _layer(manifest: Path) -> dict[str, object]:
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["file_format_version"] == "1.2.0"
    layer = data["layer"]
    assert isinstance(layer, dict)
    return layer


@pytest.mark.parametrize("tree", ["pkg", "rel"])
def test_installs_library_and_manifest_only(installed: dict[str, Path], tree: str) -> None:
    root = installed[tree] / "usr"
    # the plugin, the layer and its manifest; none of the developer programs, and
    # nothing in implicit_layer.d or under etc/
    assert _files(root) == sorted(
        [
            "lib64/buttereye/vapoursynth/libmvgpu.so",
            str(LIB_REL),
            str(MANIFEST_REL),
        ]
    )
    assert os.access(root / LIB_REL, os.X_OK)
    layer = _layer(root / MANIFEST_REL)
    assert layer["name"] == osapi.FRAMES_LAYER
    assert layer["type"] == "INSTANCE"
    # explicit: an implicit layer's manifest carries these, an explicit one never does
    assert "enable_environment" not in layer and "disable_environment" not in layer


def test_package_manifest_names_the_installed_library(installed: dict[str, Path]) -> None:
    layer = _layer(installed["pkg"] / "usr" / MANIFEST_REL)
    assert layer["library_path"] == f"/usr/{LIB_REL}"


def test_relocatable_manifest_points_beside_itself(installed: dict[str, Path]) -> None:
    root = installed["rel"] / "usr"
    manifest = root / MANIFEST_REL
    lib = _layer(manifest)["library_path"]
    assert isinstance(lib, str) and not lib.startswith("/") and "/" in lib
    assert (manifest.parent / lib).resolve() == (root / LIB_REL).resolve()


def _rooted_copy(installed: dict[str, Path], at: Path) -> Path:
    """The package manifest as it would sit on a system where DESTDIR is /: copied under
    ``at/share/vulkan/explicit_layer.d`` with the library path given the DESTDIR."""
    src = installed["pkg"] / "usr" / MANIFEST_REL
    data = json.loads(src.read_text(encoding="utf-8"))
    data["layer"]["library_path"] = str(installed["pkg"]) + data["layer"]["library_path"]
    dest = at / MANIFEST_REL
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps(data), encoding="utf-8")
    return dest


def test_osapi_finds_both_layouts(
    installed: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(osapi, "LINUX_APP", None)
    data_dir = tmp_path / "data"
    manifest = _rooted_copy(installed, data_dir)
    env = {"HOME": str(tmp_path / "home"), "XDG_DATA_DIRS": str(data_dir / "share")}
    # in a system data folder (/usr/share for the RPM): the loader finds it unaided
    assert osapi.frames_layer(env) == osapi.FramesLayer(manifest, None)
    # the AppImage's tree: found inside it, and mpv must be pointed at the folder
    app = installed["rel"] / "usr"
    monkeypatch.setattr(osapi, "LINUX_APP", app)
    env_app = {"HOME": str(tmp_path / "home"), "XDG_DATA_DIRS": str(tmp_path / "none")}
    found = osapi.frames_layer(env_app)
    assert found == osapi.FramesLayer(app / MANIFEST_REL, (app / MANIFEST_REL).parent)


def _present_env(home: Path, **extra: str) -> dict[str, str]:
    """The environment for frames-present-test on lavapipe. The loader takes the first
    manifest it finds for a layer name and looks in XDG_CONFIG_HOME, XDG_CONFIG_DIRS,
    /etc and XDG_DATA_HOME (``~/.local/share``) before XDG_DATA_DIRS, so a developer's
    own copy of the layer there would stand in for the tree under test: all of those
    but /etc point into ``home``, and ``_present`` checks which library was loaded."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VK_", "BUTTEREYE_FRAMES", "NVPRESENT", "XDG_"))
    }
    for name in ("config", "config-dirs", "data"):
        (home / name).mkdir(parents=True, exist_ok=True)
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / "config"),
        XDG_CONFIG_DIRS=str(home / "config-dirs"),
        XDG_DATA_HOME=str(home / "data"),
        XDG_DATA_DIRS=str(home / "none"),
        VK_DRIVER_FILES=str(LVP),
        VK_LOADER_LAYERS_DISABLE="~implicit~",
        VK_LOADER_DEBUG="layer",
        LP_NUM_THREADS="2",
    )
    env.update(extra)
    return env


LOADING = "Loading layer library "


def _present(installed: dict[str, Path], env: dict[str, str]) -> tuple[list[str], list[Path]]:
    """The layer's own log lines and the layer libraries the loader opened."""
    if not LVP.exists():
        pytest.skip("lavapipe (mesa-vulkan-drivers) is not installed")
    p = subprocess.run(
        [str(installed["build"] / "layer" / "frames-present-test"), "5", "--wayland-ext"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    assert "presented 5 frames" in p.stdout
    out = (p.stdout + p.stderr).splitlines()
    lines = [ln[len(PREFIX) :] for ln in p.stderr.splitlines() if ln.startswith(PREFIX)]
    libs = [Path(ln.split(LOADING, 1)[1].strip()).resolve() for ln in out if LOADING in ln]
    return lines, libs


def _loads(installed: dict[str, Path], env: dict[str, str], lib: Path) -> bool:
    """Whether the run loaded ``lib`` (and no other copy of the layer) and the layer
    reported its instance."""
    lines, libs = _present(installed, env)
    assert set(libs) <= {lib.resolve()}, f"another copy of the layer was loaded: {libs}"
    return bool(libs) and any(ln.startswith("instance 1:") for ln in lines)


def test_installed_layer_loads_only_when_enabled(
    installed: dict[str, Path], tmp_path: Path
) -> None:
    # the package layout in a system data folder: listed, never loaded by itself
    data_dir = tmp_path / "data"
    _rooted_copy(installed, data_dir)
    pkg_lib = installed["pkg"] / "usr" / LIB_REL
    system = _present_env(tmp_path / "home", XDG_DATA_DIRS=str(data_dir / "share"))
    assert _present(installed, system) == ([], [])
    on = dict(system, VK_LOADER_LAYERS_ENABLE=osapi.FRAMES_LAYER)
    assert _loads(installed, on, pkg_lib)
    # the relocatable layout through VK_ADD_LAYER_PATH, as ButterEye runs the AppImage's
    folder = installed["rel"] / "usr" / MANIFEST_REL.parent
    added = _present_env(tmp_path / "home", VK_ADD_LAYER_PATH=str(folder))
    assert _present(installed, added) == ([], [])
    on = dict(added, VK_LOADER_LAYERS_ENABLE=osapi.FRAMES_LAYER)
    assert _loads(installed, on, installed["rel"] / "usr" / LIB_REL)


def test_a_copy_of_the_layer_elsewhere_does_not_stand_in(
    installed: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A developer's own install of the layer (``meson install --prefix ~/.local``, or
    one in ~/.config) is found before XDG_DATA_DIRS; it must not make a broken
    manifest under test look as if it loads."""
    decoy = tmp_path / "decoy"
    lib = decoy / LIB
    lib.parent.mkdir()
    shutil.copy2(installed["pkg"] / "usr" / LIB_REL, lib)
    data = json.loads((installed["pkg"] / "usr" / MANIFEST_REL).read_text(encoding="utf-8"))
    data["layer"]["library_path"] = str(lib)
    for where in ("config/vulkan/explicit_layer.d", "share/vulkan/explicit_layer.d"):
        (decoy / where).mkdir(parents=True)
        (decoy / where / osapi.FRAMES_MANIFEST).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setenv("HOME", str(decoy))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(decoy / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(decoy / "share"))
    # the manifest under test names a library that is not there
    data_dir = tmp_path / "data"
    manifest = _rooted_copy(installed, data_dir)
    broken = json.loads(manifest.read_text(encoding="utf-8"))
    broken["layer"]["library_path"] = str(tmp_path / "missing" / LIB)
    manifest.write_text(json.dumps(broken), encoding="utf-8")
    env = _present_env(
        tmp_path / "home",
        XDG_DATA_DIRS=str(data_dir / "share"),
        VK_LOADER_LAYERS_ENABLE=osapi.FRAMES_LAYER,
    )
    assert not _loads(installed, env, installed["pkg"] / "usr" / LIB_REL)
