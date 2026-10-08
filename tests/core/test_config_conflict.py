# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U2: stale ``expected_revision`` -> ``ConfigConflict`` (BE-2003); atomic writes;
the facade path through the real provider (docs/design/GUI.md §6, §4.4, §8 risk 11)."""

from __future__ import annotations

import dataclasses
import os
import stat
import threading
from pathlib import Path

import pytest

from buttereye.core import api
from buttereye.core.errors import ButterEyeError, ConfigConflict, ConfigError, ErrorCode
from buttereye.core.events import ConfigChanged
from buttereye.core.profiles import config, defaults
from buttereye.core.testing.scenarios import fake_paths
from buttereye.core.types import Config, Feature, GeneralSettings, Paths


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return fake_paths(tmp_path)


def _cfg(gpu: str) -> Config:
    return dataclasses.replace(defaults.default_config(), general=GeneralSettings(gpu=gpu))


def _listing(paths: Paths) -> list[str]:
    return sorted(os.listdir(paths.config_file.parent))


def test_stale_revision_conflicts_and_leaves_file(paths: Paths) -> None:
    rev1 = config.save(paths, _cfg("a"), expected_revision="")
    rev2 = config.save(paths, _cfg("b"), expected_revision=rev1)
    before = paths.config_file.read_bytes()
    with pytest.raises(ConfigConflict) as exc:
        config.save(paths, _cfg("c"), expected_revision=rev1)
    err = exc.value
    assert err.code is ErrorCode.CONFIG_CONFLICT and err.code.code == "BE-2003"
    assert isinstance(err, ConfigError)
    assert err.on_disk_revision == rev2
    assert paths.config_file.read_bytes() == before
    assert _listing(paths) == ["config.toml"]


def test_first_run_revision_conflicts_when_file_appeared(paths: Paths) -> None:
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text("schema_version = 1\n")
    with pytest.raises(ConfigConflict) as exc:
        config.save(paths, _cfg("a"), expected_revision="")
    assert exc.value.on_disk_revision == config.revision_of(b"schema_version = 1\n")


def test_deleted_file_conflicts(paths: Paths) -> None:
    rev = config.save(paths, _cfg("a"), expected_revision="")
    paths.config_file.unlink()
    with pytest.raises(ConfigConflict) as exc:
        config.save(paths, _cfg("b"), expected_revision=rev)
    assert exc.value.on_disk_revision == ""
    assert not paths.config_file.exists()


def test_external_edit_then_reload_theirs_or_overwrite(paths: Paths) -> None:
    rev = config.save(paths, _cfg("mine"), expected_revision="")
    # the CLI (or an editor) changes the file behind the GUI's back
    paths.config_file.write_text('schema_version = 1\n[general]\ngpu = "theirs"\n')
    with pytest.raises(ConfigConflict) as exc:
        config.save(paths, _cfg("mine-2"), expected_revision=rev)
    # "Reload theirs"
    theirs = config.load(paths)
    assert theirs.revision == exc.value.on_disk_revision
    assert theirs.config.general.gpu == "theirs"
    # "Overwrite"
    new = config.save(paths, _cfg("mine-2"), expected_revision=exc.value.on_disk_revision)
    assert config.load(paths).config.general.gpu == "mine-2"
    assert config.load(paths).revision == new


def test_identical_save_keeps_revision_and_file(paths: Paths) -> None:
    rev = config.save(paths, _cfg("a"), expected_revision="")
    mtime = paths.config_file.stat().st_mtime_ns
    assert config.save(paths, _cfg("a"), expected_revision=rev) == rev
    assert paths.config_file.stat().st_mtime_ns == mtime


def test_failed_rename_keeps_old_file_and_no_temp(
    paths: Paths, monkeypatch: pytest.MonkeyPatch
) -> None:
    rev = config.save(paths, _cfg("a"), expected_revision="")
    before = paths.config_file.read_bytes()

    def boom(src: object, dst: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ButterEyeError) as exc:
        config.save(paths, _cfg("b"), expected_revision=rev)
    assert exc.value.code is ErrorCode.INTERNAL
    assert exc.value.cause.params["error"] == "No space left on device"
    assert paths.config_file.read_bytes() == before
    assert _listing(paths) == ["config.toml"]


def test_failed_fsync_keeps_old_file(paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    rev = config.save(paths, _cfg("a"), expected_revision="")
    before = paths.config_file.read_bytes()

    def boom(fd: int) -> None:
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "fsync", boom)
    with pytest.raises(ButterEyeError):
        config.save(paths, _cfg("b"), expected_revision=rev)
    assert paths.config_file.read_bytes() == before
    assert _listing(paths) == ["config.toml"]


def test_written_file_is_private(paths: Paths) -> None:
    paths.config_file.parent.mkdir(parents=True)
    paths.config_file.write_text("schema_version = 1\n")
    paths.config_file.chmod(0o644)
    config.save(paths, _cfg("a"), expected_revision=config.load(paths).revision)
    assert stat.S_IMODE(paths.config_file.stat().st_mode) == 0o600


def test_concurrent_writers_one_wins(paths: Paths) -> None:
    rev = config.save(paths, _cfg("start"), expected_revision="")
    results: list[str] = []
    conflicts: list[ConfigConflict] = []
    barrier = threading.Barrier(8)

    def writer(n: int) -> None:
        barrier.wait()
        try:
            results.append(config.save(paths, _cfg(f"w{n}"), expected_revision=rev))
        except ConfigConflict as exc:
            conflicts.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 1 and len(conflicts) == 7
    assert config.load(paths).revision == results[0]
    assert all(c.on_disk_revision == results[0] for c in conflicts)
    assert _listing(paths) == ["config.toml"]


def test_invalid_config_is_not_saved(paths: Paths) -> None:
    bad = dataclasses.replace(
        defaults.default_config(),
        profiles=(
            *defaults.builtin_profiles(),
            dataclasses.replace(defaults.builtin_profiles()[0], id="x", builtin=False, scale=0.5),
        ),
    )
    with pytest.raises(ConfigError) as exc:
        config.save(paths, bad, expected_revision="")
    assert exc.value.code is ErrorCode.CONFIG_VALUE
    assert exc.value.detail is not None and "profiles[4].scale" in exc.value.detail
    assert not paths.config_file.exists()


# ---------------------------------------------------------------- through the facade


async def test_facade_uses_real_provider(paths: Paths) -> None:
    core = await api.ButterEye.open(paths)
    try:
        caps = await core.capabilities()
        assert caps.ok(Feature.CONFIG)
        events = core.subscribe()
        load = await core.load_config()
        assert load.exists is False and load.config == defaults.default_config()
        rev = await core.save_config(_cfg("a"), expected_revision=load.revision)
        ev = await anext(events)
        while not isinstance(ev, ConfigChanged):
            ev = await anext(events)
        assert ev.revision == rev
        with pytest.raises(ConfigConflict):
            await core.save_config(_cfg("b"), expected_revision="")
        assert (await core.load_config()).config.general.gpu == "a"
        assert api.validate_config(_cfg("a")) == ()
        assert api.builtin_profiles() == defaults.builtin_profiles()
    finally:
        await core.close(cancel_jobs=True)
