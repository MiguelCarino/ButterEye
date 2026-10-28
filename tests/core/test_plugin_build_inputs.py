# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The plugin packages' versions, and the guards that keep the Windows installer
and the macOS app from packing a plugin cache built from older sources:
packaging/windows/build-plugins.sh --inputs-digest (run on a copy of the files it
reads), the check in build-installer.sh, and the check in macos/build-app.sh (its
shell block run on synthetic directories)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGINS = REPO / "packaging" / "plugins"
#: the section rule the build scripts number their steps with
_RULE = "# " + "-" * 64


def _spec(name: str) -> str:
    return (PLUGINS / f"{name}.spec").read_text(encoding="utf-8")


def _tag(spec: str, tag: str) -> str:
    m = re.search(rf"^{tag}:\s*(\S+)", spec, re.M)
    assert m is not None, tag
    return m[1].replace("%{?dist}", "")


def _meson_version() -> str:
    text = (REPO / "mvgpu" / "meson.build").read_text(encoding="utf-8")
    m = re.search(r"^project\('mvgpu'.*version: '([^']+)'", text, re.M)
    assert m is not None
    return m[1]


@pytest.mark.parametrize("name", ["buttereye-vs-rife-ncnn", "buttereye-vs-mvgpu"])
def test_the_newest_changelog_entry_is_the_package_version(name: str) -> None:
    spec = _spec(name)
    vr = f"{_tag(spec, 'Version')}-{_tag(spec, 'Release')}"
    first = re.search(r"^%changelog\n\* .* - (\S+)$", spec, re.M)
    assert first is not None
    assert first[1] == vr


def test_mvgpu_spec_and_meson_agree() -> None:
    # packaging/build.sh refuses a mismatch, and the Mac build reads meson's
    assert _tag(_spec("buttereye-vs-mvgpu"), "Version") == _meson_version()


def test_the_app_recommends_the_current_plugin_builds() -> None:
    app = (REPO / "packaging" / "buttereye.spec").read_text(encoding="utf-8")
    for name, with_release in (("buttereye-vs-rife-ncnn", True), ("buttereye-vs-mvgpu", False)):
        spec = _spec(name)
        want = _tag(spec, "Version")
        if with_release:
            want += "-" + _tag(spec, "Release")
        assert re.search(rf"^Recommends:\s+{name} >= {re.escape(want)}$", app, re.M), name


# ------------------------------------------------------------ Windows digest
_DIGEST_FILES = [
    "packaging/windows/build-plugins.sh",
    "packaging/windows/pins.sh",
    "packaging/windows/chroot.sh",
    "packaging/windows/build-in-chroot.sh",
    "packaging/windows/build-tools-in-chroot.sh",
    "packaging/windows/vulkaninfo-mingw-user32.patch",
    "packaging/plugins/buttereye-vs-mvtools.spec",
    "packaging/plugins/buttereye-vs-rife-ncnn.spec",
    "packaging/plugins/buttereye-vs-mvgpu.spec",
    "packaging/plugins/buttereye-rife-ncnn-models.spec",
    "packaging/plugins/buttereye-vs-mvtools-pkgconfig.patch",
    "packaging/plugins/buttereye-vs-rife-ncnn-host-pool.patch",
    "packaging/plugins/buttereye-vs-rife-ncnn-windows-paths.patch",
    "COPYING",
]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """The files --inputs-digest reads, outside git (the find branch, as in an
    unpacked sources tar)."""
    if shutil.which("rpmspec") is None:
        pytest.skip("needs rpmspec (rpm-build)")
    root = tmp_path / "src"
    for rel in _DIGEST_FILES:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    shutil.copytree(
        REPO / "mvgpu", root / "mvgpu", ignore=shutil.ignore_patterns("build*", "__pycache__")
    )
    return root


def _digest(root: Path) -> str:
    proc = subprocess.run(
        ["bash", os.fspath(root / "packaging/windows/build-plugins.sh"), "--inputs-digest"],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "XDG_CACHE_HOME": os.fspath(root / "cache")},
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{64}", out), out
    assert not (root / "cache").exists(), "--inputs-digest must build nothing"
    return out


def test_the_digest_is_stable(tree: Path) -> None:
    assert _digest(tree) == _digest(tree)


def test_an_mvgpu_source_edit_changes_the_digest_without_a_version_bump(tree: Path) -> None:
    before = _digest(tree)
    shader = tree / "mvgpu" / "shaders" / "search.comp"
    shader.write_text(shader.read_text() + "\n// edited\n")
    assert _digest(tree) != before


def test_a_new_mvgpu_file_changes_the_digest(tree: Path) -> None:
    before = _digest(tree)
    (tree / "mvgpu" / "shaders" / "extra.comp").write_text("// new pass\n")
    assert _digest(tree) != before


def test_a_mvgpu_build_directory_does_not(tree: Path) -> None:
    before = _digest(tree)
    (tree / "mvgpu" / "build-x").mkdir()
    (tree / "mvgpu" / "build-x" / "libmvgpu.so").write_text("object\n")
    assert _digest(tree) == before


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("packaging/plugins/buttereye-vs-rife-ncnn.spec", "Release:        3", "Release:        4"),
        ("packaging/plugins/buttereye-vs-mvtools.spec", "Release:        1", "Release:        2"),
        ("packaging/windows/build-in-chroot.sh", "set -euo pipefail", "set -euo pipefail\n:"),
        ("packaging/plugins/buttereye-vs-rife-ncnn-host-pool.patch", "\n", "\n \n"),
    ],
)
def test_a_build_input_edit_changes_the_digest(tree: Path, rel: str, old: str, new: str) -> None:
    before = _digest(tree)
    path = tree / rel
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    assert _digest(tree) != before


def test_a_comment_in_build_plugins_sh_does_not(tree: Path) -> None:
    # the script itself is not an input: only what it hands the chroot is
    before = _digest(tree)
    script = tree / "packaging/windows/build-plugins.sh"
    script.write_text(script.read_text() + "\n# a note\n")
    assert _digest(tree) == before


def test_a_failing_rpmspec_fails_the_digest(tree: Path, tmp_path: Path) -> None:
    # set -e does not reach into $(...), so without its own checks the script
    # printed a digest of empty versions and exited 0
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "rpmspec").write_text("#!/bin/sh\nexit 1\n")
    (fake / "rpmspec").chmod(0o755)
    proc = subprocess.run(
        ["bash", os.fspath(tree / "packaging/windows/build-plugins.sh"), "--inputs-digest"],
        capture_output=True, text=True, timeout=60, check=False,
        env={**os.environ, "PATH": f"{fake}{os.pathsep}{os.environ['PATH']}"},
    )  # fmt: skip
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "rpmspec can't read" in proc.stderr


def test_inputs_digest_takes_no_other_argument(tree: Path) -> None:
    script = os.fspath(tree / "packaging/windows/build-plugins.sh")
    proc = subprocess.run(
        ["bash", script, "--inputs-digest", "--no-mock"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 2


def test_the_digest_is_taken_before_the_build_and_stored() -> None:
    script = (REPO / "packaging/windows/build-plugins.sh").read_text(encoding="utf-8")
    taken = script.index("inputs=$(inputs_digest)")
    assert taken < script.index('"$repo/packaging/build.sh" --srpm-only')
    assert taken < script.index("win_init\n")
    assert 'data["build_inputs"] = {"sha256": inputs}' in script


def test_the_installer_refuses_a_cache_from_other_sources() -> None:
    script = (REPO / "packaging/windows/build-installer.sh").read_text(encoding="utf-8")
    guard = script.index('wanted=$("$here/build-plugins.sh" --inputs-digest)')
    assert script.index("sha256sum --quiet --strict") < guard
    assert guard < script.index(f"{_RULE} 2.")
    assert 'if [[ $built != "$wanted" ]]; then' in script
    # rpmspec is what --inputs-digest runs
    assert re.search(r"^win_host_install .*\brpm-build\b", script, re.M)
    # the install record is made from a copy without build_inputs
    assert 'data.pop("build_inputs", None)' in script
    assert 'stagepy record "$work/components.json"' in script
    assert 'stagepy record "$win/components.json"' not in script


# ------------------------------------------------------------ macOS guard
_APP = (REPO / "packaging/macos/build-app.sh").read_text(encoding="utf-8")


def _mac_guard() -> str:
    start = r"^stale=\(\)\n"
    end = r"\nif \(\(\$\{#stale\[@\]\}\)\); then\n.*?\nfi\n"
    m = re.search(start + ".*?" + end, _APP, re.M | re.S)
    assert m is not None
    return m[0]


@pytest.fixture
def mac(tmp_path: Path) -> dict[str, Path]:
    """A repository, a packaging/macos and a plugin cache that match."""
    repo = tmp_path / "repo"
    here = repo / "packaging" / "macos"
    cache = tmp_path / "cache"
    (repo / "mvgpu" / "shaders").mkdir(parents=True)
    (repo / "mvgpu" / "meson.build").write_text("project('mvgpu')\n")
    (repo / "mvgpu" / "shaders" / "search.comp").write_text("void main() {}\n")
    (repo / "packaging" / "plugins").mkdir(parents=True)
    here.mkdir(parents=True)
    for f in ("build-plugins.sh", "build-rife.sh"):
        (here / f).write_text(f"# {f}\n")
    for p in ("host-pool", "model-path"):
        (repo / "packaging/plugins" / f"buttereye-vs-rife-ncnn-{p}.patch").write_text(f"{p}\n")
    (cache / "vapoursynth").mkdir(parents=True)
    (cache / "vapoursynth" / "librife.dylib").write_text("dylib\n")
    (cache / "sources").mkdir()
    shutil.copytree(repo / "mvgpu", cache / "sources" / "mvgpu")
    for f in ("build-plugins.sh", "build-rife.sh"):
        shutil.copy2(here / f, cache / "sources" / f)
    for p in ("host-pool", "model-path"):
        name = f"buttereye-vs-rife-ncnn-{p}.patch"
        shutil.copy2(repo / "packaging/plugins" / name, cache / "sources" / name)
    return {"repo": repo, "here": here, "mac": cache}


_HOST_POOL = "buttereye-vs-rife-ncnn-host-pool.patch"


def _run_mac_guard(d: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    code = f"set -euo pipefail\nrepo={d['repo']}\nhere={d['here']}\nmac={d['mac']}\n"
    return subprocess.run(
        ["bash", "-c", code + _mac_guard() + "echo passed\n"],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip


def test_mac_guard_passes_a_matching_cache(mac: dict[str, Path]) -> None:
    # a build directory in the tree is not a source
    (mac["repo"] / "mvgpu" / "build").mkdir()
    (mac["repo"] / "mvgpu" / "build" / "libmvgpu.dylib").write_text("x\n")
    proc = _run_mac_guard(mac)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "passed"


@pytest.mark.parametrize(
    ("edit", "named"),
    [
        (lambda d: (d["repo"] / "mvgpu/shaders/search.comp").write_text("changed\n"), "mvgpu/"),
        (lambda d: (d["repo"] / "mvgpu/shaders/subpel.comp").write_text("new\n"), "mvgpu/"),
        (lambda d: (d["here"] / "build-rife.sh").write_text("# a new patch\n"), "build-rife.sh"),
        (
            lambda d: (d["repo"] / "packaging/plugins" / _HOST_POOL).write_text("v2\n"),
            _HOST_POOL,
        ),
    ],
)
def test_mac_guard_refuses_a_cache_from_other_sources(
    mac: dict[str, Path], edit: Callable[[dict[str, Path]], object], named: str
) -> None:
    edit(mac)
    proc = _run_mac_guard(mac)
    assert proc.returncode == 1
    assert named in proc.stderr and "run build-plugins.sh again" in proc.stderr


def test_mac_guard_refuses_rife_without_its_patches(mac: dict[str, Path]) -> None:
    for p in (mac["mac"] / "sources").glob("*.patch"):
        p.unlink()
    proc = _run_mac_guard(mac)
    assert proc.returncode == 1 and "none recorded" in proc.stderr


def test_mac_guard_skips_rife_patches_when_rife_was_not_built(mac: dict[str, Path]) -> None:
    (mac["mac"] / "vapoursynth" / "librife.dylib").unlink()
    for p in (mac["mac"] / "sources").glob("*.patch"):
        p.unlink()
    assert _run_mac_guard(mac).returncode == 0


def test_mac_guard_runs_after_the_checksums_and_before_the_downloads() -> None:
    guard = _APP.index("stale=()")
    assert _APP.index("shasum -a 256 --quiet --strict") < guard
    assert guard < _APP.index(f"{_RULE} 2. downloads")
