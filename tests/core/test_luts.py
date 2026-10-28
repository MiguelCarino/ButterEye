# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Imported 3D LUTs: the luts folder, names, import and the path check."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from buttereye.core.profiles import luts
from buttereye.core.testing.scenarios import fake_paths
from buttereye.core.types import Paths

CUBE = b'TITLE "test"\nLUT_3D_SIZE 2\n0 0 0\n1 0 0\n0 1 0\n1 1 0\n0 0 1\n1 0 1\n0 1 1\n1 1 1\n'

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return fake_paths(tmp_path)


def _source(tmp_path: Path, name: str = "Film Look.cube", data: bytes = CUBE) -> Path:
    src = tmp_path / "downloads" / name
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(data)
    return src


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("film.cube", True),
        ("Film Look (v2).CUBE", True),
        ("a+b_c-d.cube", True),
        (".film.cube", False),
        ("../film.cube", False),
        ("dir/film.cube", False),
        ("film.cube.png", False),
        ("film", False),
        ("", False),
        (None, False),
        ("x" * 200 + ".cube", False),
    ],
)
def test_lut_names(name: object, ok: bool) -> None:
    assert luts.valid_lut_name(name) is ok


def test_import_copies_into_a_private_folder(paths: Paths, tmp_path: Path) -> None:
    name = luts.import_lut(paths, _source(tmp_path))
    assert name == "Film Look.cube"
    folder = luts.lut_dir(paths)
    assert folder == paths.data_dir / "luts"
    assert (folder / name).read_bytes() == CUBE
    assert luts.resolve_lut(paths, name) == folder / name
    assert luts.is_lut_path(str(folder / name))
    assert [p.name for p in folder.iterdir()] == [name]  # no temporary file left
    if sys.platform != "win32":
        assert folder.stat().st_mode & 0o777 == 0o700


def test_import_never_overwrites_a_different_lut(paths: Paths, tmp_path: Path) -> None:
    first = luts.import_lut(paths, _source(tmp_path))
    assert luts.import_lut(paths, _source(tmp_path)) == first  # the same file again
    other = CUBE.replace(b"1 1 1\n", b"0.9 0.9 0.9\n")
    second = luts.import_lut(paths, _source(tmp_path, data=other))
    assert second == "Film Look-2.cube"
    assert (luts.lut_dir(paths) / first).read_bytes() == CUBE


def test_import_renames_what_is_not_a_lut_name(paths: Paths, tmp_path: Path) -> None:
    assert luts.import_lut(paths, _source(tmp_path, "._weird*name.CUBE")) == "weird_name.cube"


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("film.3dl", CUBE),
        ("film.cube", b"LUT_1D_SIZE 1024\n0 0 0\n"),
        ("film.cube", b"\x89PNG\r\n"),
    ],
)
def test_import_refuses_what_is_not_a_3d_cube(
    paths: Paths, tmp_path: Path, name: str, data: bytes
) -> None:
    with pytest.raises(luts.LutError):
        luts.import_lut(paths, _source(tmp_path, name, data))
    assert not luts.lut_dir(paths).exists() or not any(luts.lut_dir(paths).iterdir())


def test_import_refuses_a_huge_file(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(luts, "MAX_LUT_BYTES", 16)
    with pytest.raises(luts.LutError, match="larger than"):
        luts.import_lut(paths, _source(tmp_path))


def test_a_huge_file_is_refused_before_it_is_read(
    paths: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setattr(luts, "MAX_LUT_BYTES", 16)

    def no_read(self: Path, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"read {self}")

    monkeypatch.setattr(Path, "read_bytes", no_read)
    monkeypatch.setattr(Path, "open", no_read)
    with pytest.raises(luts.LutError, match="larger than"):
        luts.import_lut(paths, source)


def test_resolve_refuses_names_and_missing_files(paths: Paths, tmp_path: Path) -> None:
    luts.import_lut(paths, _source(tmp_path))
    assert luts.resolve_lut(paths, None) is None
    assert luts.resolve_lut(paths, "missing.cube") is None
    assert luts.resolve_lut(paths, "../Film Look.cube") is None


@posix_only
def test_resolve_refuses_symlinks_and_shared_folders(paths: Paths, tmp_path: Path) -> None:
    name = luts.import_lut(paths, _source(tmp_path))
    folder = luts.lut_dir(paths)
    (folder / "link.cube").symlink_to(folder / name)
    assert luts.resolve_lut(paths, "link.cube") is None
    folder.chmod(0o770)
    try:
        assert luts.resolve_lut(paths, name) is None
        assert not luts.is_lut_path(str(folder / name))
        with pytest.raises(luts.LutError, match="private"):
            luts.import_lut(paths, _source(tmp_path, "other.cube"))
    finally:
        folder.chmod(0o700)


def test_is_lut_path_wants_an_absolute_path_in_a_luts_folder(paths: Paths, tmp_path: Path) -> None:
    name = luts.import_lut(paths, _source(tmp_path))
    assert luts.is_lut_path("")
    assert not luts.is_lut_path(name)  # a bare name is not a path
    assert not luts.is_lut_path(str(_source(tmp_path)))  # not in a luts folder
    assert not luts.is_lut_path(str(luts.lut_dir(paths) / ".." / "luts" / name))


def test_the_facade_imports_luts_and_answers_gui_dirs(paths: Paths, tmp_path: Path) -> None:
    """The GUI may import only ``core.api``: LUT import and the GUI's own files
    are reached through it."""
    from buttereye.core import api
    from buttereye.core import paths as core_paths

    name = api.import_lut(paths, _source(tmp_path))
    assert name == "Film Look.cube" and luts.resolve_lut(paths, name) is not None
    with pytest.raises(ValueError):
        api.import_lut(paths, _source(tmp_path, "notes.txt"))
    env = {"HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state")}
    assert api.gui_dirs(env) == core_paths.gui_dirs(env)
    assert {"import_lut", "gui_dirs"} <= set(api.__all__)
