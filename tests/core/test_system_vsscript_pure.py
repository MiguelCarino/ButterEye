# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The distribution's VSScript and its configuration lines (Fedora 45, R79):
the package ships ``libvsscript.so.0``, which ``vapoursynth config`` has no
line for."""

from __future__ import annotations

import os
from pathlib import Path

from buttereye.core import osapi


def _layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    package = tmp_path / "usr/lib64/python3.15/site-packages/vapoursynth"
    package.mkdir(parents=True)
    (package / "libvsscript.so.0").write_bytes(b"")
    linker = tmp_path / "lib64"
    linker.mkdir()
    (linker / "libvsscript.so.0").symlink_to(package / "libvsscript.so.0")
    python = tmp_path / "python3"
    python.write_bytes(b"")
    os.utime(python, (1000, 1000))
    libpython = tmp_path / "libpython3.15.so.1.0"
    libpython.write_bytes(b"")
    return package, linker, python, libpython


def test_one_line_for_the_package_file_and_one_for_the_linker_link(tmp_path: Path) -> None:
    package, linker, python, libpython = _layout(tmp_path)
    lines = osapi.system_vsscript_lines(
        package, python, libpython, linker_dirs=(str(linker), str(tmp_path / "nowhere"))
    )
    keys = [line.split(" = ")[0].strip('"') for line in lines]
    # VSScript reads /lib64/ as /lib/ in its own path
    assert keys == [
        str(package / "libvsscript.so.0").replace("/lib64/", "/lib/"),
        str(linker / "libvsscript.so.0").replace("/lib64/", "/lib/"),
    ]
    assert all(line.endswith(f'["{python}","{libpython}","1000"]') for line in lines)


def test_a_link_to_another_file_is_not_this_vsscript(tmp_path: Path) -> None:
    package, linker, python, libpython = _layout(tmp_path)
    (linker / "libvsscript.so.0").unlink()
    (linker / "libvsscript.so.0").write_bytes(b"other")
    lines = osapi.system_vsscript_lines(package, python, libpython, linker_dirs=(str(linker),))
    assert len(lines) == 1


def test_no_vsscript_in_the_package_means_nothing_to_write(tmp_path: Path) -> None:
    package, _linker, python, libpython = _layout(tmp_path)
    (package / "libvsscript.so.0").unlink()
    assert osapi.system_vsscript_lines(package, python, libpython) == []


def test_lines_are_merged_and_other_entries_kept(tmp_path: Path) -> None:
    package, linker, python, libpython = _layout(tmp_path)
    lines = osapi.system_vsscript_lines(package, python, libpython, linker_dirs=(str(linker),))
    config = tmp_path / "cfg" / "vapoursynth" / "vapoursynth.toml"
    config.parent.mkdir(parents=True)
    other = '"/opt/other/libvsscript.so" = ["/opt/py","/opt/libpy.so","5"]'
    stale = lines[0].replace('"1000"', '"999"')
    config.write_text(other + "\n" + stale + "\n", encoding="utf-8")
    assert osapi._write_vsscript_lines(config, lines)
    text = config.read_text(encoding="utf-8").splitlines()
    assert text[0] == other and stale not in text and text[1:] == lines
    before = config.stat().st_mtime_ns
    assert osapi._write_vsscript_lines(config, lines)  # nothing to change: not rewritten
    assert config.stat().st_mtime_ns == before


def test_onnx_installed_for_another_python_is_one_degraded_row() -> None:
    """Found on disk but failing on import (numpy's compiled core after the
    distribution moved to a new Python): not two "found" rows."""
    import pytest

    from buttereye.core.doctor import checks_trt
    from buttereye.core.doctor.proc import CmdResult
    from buttereye.core.hw.detect import HwProbe
    from buttereye.core.types import HardwareInfo, Severity

    if osapi.IS_MACOS or osapi.IS_WINDOWS or osapi.LINUX_APP is not None:
        pytest.skip("TensorRT rows are Linux's")
    raw = {
        "plugins": [],
        "vs_core": "R79",
        "modules": {"onnx": True, "onnxconverter_common": True},
        "module_errors": {"onnx": "ImportError: No module named 'numpy._core._multiarray_umath'"},
    }
    hw = HwProbe(HardwareInfo((), (), None, None, 8), (), None, None, (), None)
    rows = checks_trt.trt_findings(
        hw, CmdResult(("ldconfig",), None, "", "", missing=True), raw, trtexec=None
    )
    row = [r for r in rows if r.id == "trt.onnx"]
    assert len(row) == 1 and row[0].severity is Severity.DEGRADED
    assert "multiarray_umath" in str(row[0].cause.params)
    assert not [r for r in rows if r.id == "trt.fp16"]
