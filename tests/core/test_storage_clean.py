# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: storage sizes, clean (never RPM-owned, symlinks never followed), models."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import storage
from buttereye.core.paths import resolve
from buttereye.core.types import BackendId, CleanTarget, ModelKind, Paths, StorageKind


@pytest.fixture
def paths(xdg_env: Any, tmp_path: Path) -> Paths:
    usr = tmp_path / "usr"
    p = dataclasses.replace(
        resolve(),
        rpm_plugin_dir=usr / "lib64/buttereye/vapoursynth",
        rpm_data_dir=usr / "share/buttereye",
    )
    p.rpm_plugin_dir.mkdir(parents=True)
    (p.rpm_plugin_dir / "librife.so").write_bytes(b"x" * 1000)
    for m in ("rife-v4.26_ensembleFalse", "rife-v4.22_lite_ensembleFalse"):
        d = p.rpm_data_dir / "rife-ncnn-models" / m
        d.mkdir(parents=True)
        (d / "flownet.bin").write_bytes(b"m" * 300)
    return p


def write(p: Path, n: int) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"z" * n)
    return p


async def test_entries_sizes_and_rpm_rows(paths: Paths) -> None:
    write(paths.cache_dir / "engines/a/b.engine", 100)
    write(paths.cache_dir / "engines/c.engine", 50)
    write(paths.logs_dir / "setup.log", 7)
    rows = await storage.entries(paths)
    by = {(r.kind, r.path): r for r in rows}
    eng = by[(StorageKind.ENGINES, paths.cache_dir / "engines")]
    assert eng.bytes == 150 and eng.deletable and eng.clean_target is CleanTarget.ENGINES
    assert by[(StorageKind.MODELS, paths.data_dir / "models")].bytes == 0  # missing = 0 bytes
    assert by[(StorageKind.LOGS, paths.logs_dir)].bytes == 7
    rpm = [r for r in rows if r.kind is StorageKind.RPM]
    assert len(rpm) == 2
    assert all(r.rpm_owned and not r.deletable and r.clean_target is None for r in rpm)
    assert by[(StorageKind.RPM, paths.rpm_plugin_dir)].bytes == 1000
    assert by[(StorageKind.RPM, paths.rpm_data_dir)].bytes == 600
    assert {r.kind for r in rows} >= {StorageKind.CONFIG, StorageKind.RUNTIME, StorageKind.BENCH}


async def test_size_does_not_follow_symlinks(paths: Paths, tmp_path: Path) -> None:
    big = write(tmp_path / "outside/big.bin", 10_000)
    (paths.data_dir / "models").mkdir(parents=True)
    (paths.data_dir / "models/link").symlink_to(big)
    (paths.data_dir / "models/dirlink").symlink_to(big.parent)
    size = storage.tree_size(paths.data_dir / "models")
    assert size is not None and size < 1000


async def test_clean_removes_user_data_only(paths: Paths, tmp_path: Path) -> None:
    write(paths.cache_dir / "engines/x/e.engine", 100)
    write(paths.cache_dir / "downloads/a.part", 10)
    write(paths.data_dir / "models/extra-m/model.bin", 40)
    outside = write(tmp_path / "outside/keep.bin", 5)
    (paths.data_dir / "models/extra-m/link").symlink_to(outside)
    (paths.data_dir / "models/dirlink").symlink_to(outside.parent)
    progress: list[int] = []
    res = await storage.clean(
        paths,
        frozenset(
            {
                CleanTarget.ENGINES,
                CleanTarget.MODELS,
                CleanTarget.DOWNLOADS,
                CleanTarget.JOBS,
                CleanTarget.PLUGINS,
            }
        ),
        lambda p: progress.append(p.done or 0),
    )
    assert res.freed_bytes >= 150
    assert not any((paths.cache_dir / "engines").iterdir())
    assert not any((paths.data_dir / "models").iterdir())
    assert (paths.cache_dir / "engines").is_dir()  # the folder itself stays
    # symlink targets survive; RPM files survive
    assert outside.read_bytes() == b"z" * 5
    assert (tmp_path / "outside").is_dir()
    assert (paths.rpm_plugin_dir / "librife.so").exists()
    assert len(list((paths.rpm_data_dir / "rife-ncnn-models").iterdir())) == 2
    assert progress[-1] == 5


async def test_symlinked_target_dir_skipped(paths: Paths, tmp_path: Path) -> None:
    victim = write(tmp_path / "victim/important.txt", 3)
    paths.cache_dir.mkdir(parents=True)
    (paths.cache_dir / "engines").symlink_to(victim.parent)
    res = await storage.clean(paths, frozenset({CleanTarget.ENGINES}), lambda p: None)
    assert victim.exists()
    assert res.removed == ()
    assert res.skipped and res.skipped[0][0] == paths.cache_dir / "engines"


def test_rpm_and_outside_locations_refused(paths: Paths, tmp_path: Path) -> None:
    assert storage.safe_target(paths, paths.rpm_data_dir / "rife-ncnn-models") is not None
    assert storage.safe_target(paths, paths.rpm_plugin_dir) is not None
    assert storage.safe_target(paths, Path("/usr/share/buttereye")) is not None
    assert storage.safe_target(paths, tmp_path / "elsewhere") is not None
    assert storage.safe_target(paths, paths.data_dir) is not None  # the root itself
    assert storage.safe_target(paths, paths.cache_dir / "engines") is None


async def test_clean_refuses_rpm_dir_even_if_misconfigured(paths: Paths) -> None:
    # a Paths whose cache lives inside the RPM tree must still never be cleaned
    bad = dataclasses.replace(paths, cache_dir=paths.rpm_data_dir)
    write(bad.cache_dir / "engines/should-stay", 1)
    res = await storage.clean(bad, frozenset({CleanTarget.ENGINES}), lambda p: None)
    assert (bad.cache_dir / "engines/should-stay").exists()
    assert res.skipped


async def test_models_listing(paths: Paths) -> None:
    write(paths.data_dir / "models/rife-v4.6/rife_v4.6.onnx", 20)
    (paths.data_dir / "models/rife-v4.6/buttereye-model.json").write_text(
        '{"sha256": "ab" , "licence": "MIT", "pinned": true}'
    )
    write(paths.data_dir / "models/my-ncnn/flownet.param", 5)
    rows = {m.name: m for m in await storage.models(paths)}
    pk = rows["rife-v4.26_ensembleFalse"]
    assert pk.kind is ModelKind.PACKAGED and not pk.removable and pk.licence == "MIT"
    assert pk.backend is BackendId.RIFE_NCNN and pk.size_bytes == 300
    onnx = rows["rife-v4.6"]
    assert onnx.kind is ModelKind.DOWNLOADED and onnx.backend is BackendId.RIFE_TRT
    assert onnx.removable and onnx.sha256 == "ab"
    assert rows["my-ncnn"].kind is ModelKind.UNPINNED


@pytest.mark.devbox
async def test_real_packaged_models(xdg_env: Any) -> None:
    rows = await storage.models(resolve())
    packaged = [m for m in rows if m.kind is ModelKind.PACKAGED]
    assert "rife-v4.26_ensembleFalse" in {m.name for m in packaged}
    entries = await storage.entries(resolve())
    assert all(e.bytes == 0 for e in entries if e.kind in (StorageKind.ENGINES, StorageKind.MODELS))


async def test_symlinked_user_root_into_rpm_tree_refused(paths: Paths) -> None:
    # ~/.local/share/buttereye -> the RPM data folder: nothing there may be deleted
    keep = write(paths.rpm_data_dir / "models/rife-v4/flownet.bin", 9)
    paths.data_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.data_dir.symlink_to(paths.rpm_data_dir, target_is_directory=True)
    assert storage.safe_target(paths, paths.data_dir / "models") is not None
    res = await storage.clean(paths, frozenset({CleanTarget.MODELS}), lambda p: None)
    assert keep.exists()
    assert res.removed == () and res.skipped


async def test_symlinked_user_root_to_plain_folder_still_cleanable(
    paths: Paths, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "other-disk/buttereye-data"
    write(elsewhere / "models/extra/model.bin", 11)
    paths.data_dir.parent.mkdir(parents=True, exist_ok=True)
    paths.data_dir.symlink_to(elsewhere, target_is_directory=True)
    assert storage.safe_target(paths, paths.data_dir / "models") is None
    res = await storage.clean(paths, frozenset({CleanTarget.MODELS}), lambda p: None)
    assert res.freed_bytes == 11
    assert list((elsewhere / "models").iterdir()) == []


def test_symlinked_component_below_root_refused(paths: Paths, tmp_path: Path) -> None:
    # $XDG_STATE_HOME/buttereye/jobs is fine, but not when an inner folder is a link
    real = tmp_path / "elsewhere-state"
    real.mkdir()
    paths.state_dir.mkdir(parents=True)
    (paths.state_dir / "inner").symlink_to(real, target_is_directory=True)
    assert storage.safe_target(paths, paths.state_dir / "inner" / "jobs") is not None


def test_dotdot_target_normalised_before_checks(paths: Paths) -> None:
    sneaky = paths.cache_dir / ".." / ".." / ".." / "usr"
    assert storage.safe_target(paths, sneaky) is not None


async def test_dir_swapped_for_symlink_mid_clean_not_followed(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = write(tmp_path / "sentinel/precious.txt", 4)
    victim = paths.cache_dir / "engines/victim"
    write(victim / "e.engine", 5)
    real_open = os.open
    swapped: list[str] = []

    def racing_open(path: Any, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        if dir_fd is not None and path == "victim" and not swapped:
            # the race: after the lstat said "directory", swap it for a link
            swapped.append(path)
            os.rename(victim, victim.with_name("victim.moved"))
            victim.symlink_to(sentinel.parent, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)
    res = await storage.clean(paths, frozenset({CleanTarget.ENGINES}), lambda p: None)
    assert swapped
    assert sentinel.exists() and sentinel.read_bytes() == b"zzzz"
    assert not victim.is_symlink() and not victim.exists()  # the link itself was removed
    assert victim in res.removed
