# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: licence surfacing (F18, §8.2): ncnn + glslang conveyed rows, BE-1021."""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType

import pytest

from buttereye.core import licences
from buttereye.core.doctor.checks_pkgs import RpmDb, RpmInfo
from buttereye.core.errors import ErrorCode
from buttereye.core.types import Severity


def info(
    name: str,
    files: list[Path],
    *,
    provides: tuple[tuple[str, str], ...] = (),
    lic: str = "MIT",
    evr: str = "1-1",
) -> RpmInfo:
    return RpmInfo(name, evr, lic, provides, (), tuple(files))


def make_db(tmp_path: Path, *, drop: str | None = None) -> RpmDb:
    lic = tmp_path / "licenses"
    rife = [
        lic / "rife" / n
        for n in ("LICENSE", "LICENSE.nihui", "LICENSE.ncnn.txt", "LICENSE.glslang.txt")
    ]
    mv = [lic / "mv" / "LICENSE", lic / "mv" / "x86inc.asm"]
    models = [
        lic / "models" / n
        for n in (
            "LICENSE",
            "Practical-RIFE.LICENSE",
            "ECCV2022-RIFE.LICENSE",
            "rife-ncnn-vulkan.LICENSE",
        )
    ]
    mpv = [lic / "mpv" / "Copyright"]
    for p in rife + mv + models + mpv:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.name != drop:
            p.write_text("text")
    pk = {
        "buttereye-vs-rife-ncnn": info(
            "buttereye-vs-rife-ncnn",
            rife,
            evr="9.33-0.2.spike.fc44",
            provides=(
                ("bundled(ncnn)", "0^20250503git305837fd"),
                ("bundled(glslang)", "0^gita9ac7d5f"),
            ),
        ),
        "buttereye-vs-mvtools": info("buttereye-vs-mvtools", mv),
        "buttereye-rife-ncnn-models": info("buttereye-rife-ncnn-models", models),
        "mpv": info("mpv", mpv, lic="GPL-2.0-or-later AND LGPL-2.1-or-later"),
    }
    return RpmDb(True, MappingProxyType(pk))


def test_ncnn_and_glslang_rows_conveyed(tmp_path: Path) -> None:
    rows = {r.name: r for r in licences.build_third_party(make_db(tmp_path), None)}
    for name in ("ncnn", "glslang"):
        r = rows[name]
        assert r.conveyed and r.detected and r.finding is None
        assert len(r.text_files) == 1 and name in r.text_files[0].name
    assert rows["ncnn"].version == "0^20250503git305837fd"
    assert rows["glslang"].version == "0^gita9ac7d5f"
    assert rows["ncnn"].spdx == "BSD-3-Clause AND BSD-2-Clause AND Zlib"
    assert "GPL-3.0-or-later WITH Bison-exception-2.2" in rows["glslang"].spdx
    assert {p.name for p in rows["RIFE-ncnn-Vulkan"].text_files} == {"LICENSE", "LICENSE.nihui"}
    assert rows["RIFE ncnn models"].finding is None
    assert rows["mpv"].spdx == "GPL-2.0-or-later AND LGPL-2.1-or-later"  # from rpm
    assert not rows["mpv"].conveyed
    assert not rows["ffms2"].detected and rows["ffms2"].finding is None
    assert "vsmlrt.py" not in rows  # only listed when installed
    for required in (
        "RIFE-ncnn-Vulkan",
        "MVTools",
        "PySide6 / Qt",
        "VapourSynth",
        "FFmpeg",
        "mkvtoolnix",
    ):
        assert required in rows


def test_missing_licence_file_is_be1021(tmp_path: Path) -> None:
    rows = {
        r.name: r
        for r in licences.build_third_party(make_db(tmp_path, drop="LICENSE.ncnn.txt"), None)
    }
    f = rows["ncnn"].finding
    assert f is not None and f.code is ErrorCode.LICENCE_FILE_MISSING
    assert f.severity is Severity.DEGRADED
    assert rows["ncnn"].text_files == ()
    assert rows["glslang"].finding is None


def test_incomplete_model_texts_flagged(tmp_path: Path) -> None:
    db = make_db(tmp_path)
    only_one = info("buttereye-rife-ncnn-models", [tmp_path / "licenses/models/LICENSE"])
    db = RpmDb(True, MappingProxyType({**db.packages, "buttereye-rife-ncnn-models": only_one}))
    rows = {r.name: r for r in licences.build_third_party(db, None)}
    f = rows["RIFE ncnn models"].finding
    assert f is not None and f.code is ErrorCode.LICENCE_FILE_MISSING
    assert f.cause.params["need"] == 4


def test_legal_notices(tmp_path: Path) -> None:
    n = licences.legal_notices()
    assert n.name == "ButterEye"
    assert n.copyright == "Copyright (C) 2026 The ButterEye contributors"
    assert n.licence_name == "GNU Affero General Public License v3.0 or later"
    assert n.no_warranty.key
    assert n.source_link
    for p in n.missing:  # missing files are listed, never hidden
        assert not p.exists()
    (tmp_path / "LICENSES").mkdir()
    (tmp_path / "COPYING").write_text("AGPL")
    (tmp_path / licences.OUTPUT_PERMISSION).write_text("permission")
    ok = licences.legal_notices(roots=(tmp_path,))
    assert ok.missing == ()
    assert ok.agpl_text == tmp_path / "COPYING"
    assert ok.output_permission_text == tmp_path / licences.OUTPUT_PERMISSION
    empty = tmp_path / "empty"
    empty.mkdir()
    gone = licences.legal_notices(roots=(empty,))
    if not (Path(licences.__file__).resolve().parents[2] / "COPYING").exists():
        assert empty / "COPYING" in gone.missing


@pytest.mark.devbox
async def test_real_third_party(xdg_env: object) -> None:
    from buttereye.core.paths import resolve

    rows = {r.name: r for r in await licences.third_party(resolve())}
    for name in ("RIFE-ncnn-Vulkan", "ncnn", "glslang", "MVTools", "RIFE ncnn models"):
        assert rows[name].detected and rows[name].conveyed, name
    assert rows["ncnn"].text_files and rows["glslang"].text_files
    assert rows["ncnn"].version == "0^20250503git305837fd"
    assert rows["mpv"].detected and not rows["mpv"].conveyed
