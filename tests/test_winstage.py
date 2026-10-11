# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""packaging/windows/winstage.py, the Windows installer's staging helpers: the
licence-relevant parts (what the trim keeps, the Microsoft-runtime guard, the Qt
notices, the install record), on synthetic inputs."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "winstage.py"


@pytest.fixture(scope="module")
def ws() -> ModuleType:
    spec = importlib.util.spec_from_file_location("winstage", _PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _site(tmp: Path) -> Path:
    site = tmp / "site-packages"
    py = site / "PySide6"
    for name in (
        "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Svg.dll", "Qt6Qml.dll",
        "QtCore.pyd", "QtQml.pyd", "msvcp140.dll", "msvcp140_1.dll", "vcruntime140.dll",
        "concrt140.dll", "vcomp140.dll", "vcamp140.dll", "vccorlib140.dll", "designer.exe",
        "plugins/imageformats/qsvg.dll", "plugins/imageformats/qjpeg.dll",
        "plugins/imageformats/qtiff.dll", "plugins/imageformats/qwebp.dll",
        "plugins/iconengines/qsvgicon.dll", "plugins/platforms/qwindows.dll",
        "plugins/platforms/qdirect2d.dll", "plugins/styles/qmodernwindowsstyle.dll",
        "plugins/tls/qschannelbackend.dll",
    ):  # fmt: skip
        _touch(py / name)
    for name in ("Shiboken.pyd", "shiboken6.abi3.dll", "msvcp140.dll", "vcruntime140_1.dll",
                 "shiboken6.abi3.lib", "Shiboken.pyi", "include/sbkpython.h",
                 "lib/cmake/Shiboken6/Shiboken6Config.cmake"):  # fmt: skip
        _touch(site / "shiboken6" / name)
    for dist in ("pyside6_essentials-6.11.2.dist-info", "shiboken6-6.11.2.dist-info"):
        rel = f"{dist}/licenses/LicenseRef-Qt-Commercial.txt"
        _touch(site / rel, b"Qt commercial licence")
        (site / dist / "RECORD").write_text(f"{dist}/METADATA,sha256=abc,1\n{rel},sha256=old,21\n")
    return site


def test_trim_keeps_svg_only_and_drops_the_wheels_msvc_runtime(
    ws: ModuleType, tmp_path: Path
) -> None:
    site = _site(tmp_path)
    ws.trim(site)
    left = {str(p.relative_to(site)) for p in site.rglob("*") if p.is_file()}
    py = {p.removeprefix("PySide6/") for p in left if p.startswith("PySide6/")}
    assert py == {
        "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Svg.dll", "QtCore.pyd",
        "plugins/imageformats/qsvg.dll", "plugins/iconengines/qsvgicon.dll",
        "plugins/platforms/qwindows.dll", "plugins/styles/qmodernwindowsstyle.dll",
    }  # fmt: skip
    sb = {p.removeprefix("shiboken6/") for p in left if p.startswith("shiboken6/")}
    assert sb == {"Shiboken.pyd", "shiboken6.abi3.dll"}


def test_trim_notes_which_licence_the_qt_copy_is_under(ws: ModuleType, tmp_path: Path) -> None:
    site = _site(tmp_path)
    ws.trim(site)
    for dist, what in (
        ("pyside6_essentials-6.11.2.dist-info", "PySide6 and Qt"),
        ("shiboken6-6.11.2.dist-info", "Shiboken6"),
    ):
        lic = site / dist / "licenses" / "LicenseRef-Qt-Commercial.txt"
        data = lic.read_bytes()
        text = data.decode()
        assert text.startswith("NOTE") and "LGPL-3.0-only" in text and what in text
        assert "That licence does not apply here" in text.replace("\r\n", " ")
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record = (site / dist / "RECORD").read_text().splitlines()
        rel = f"{dist}/licenses/LicenseRef-Qt-Commercial.txt"
        assert f"{rel},sha256={digest},{len(data)}" in record
        assert record[0].endswith("METADATA,sha256=abc,1")


def test_check_imports_refuses_a_microsoft_runtime_besides_pythons(
    ws: ModuleType, tmp_path: Path
) -> None:
    stage = tmp_path / "stage"
    _touch(stage / "python" / "vcruntime140.dll")
    _touch(stage / "python" / "vcruntime140_1.dll")
    ws.check_imports(stage)  # Python's own copies are fine
    _touch(stage / "python" / "Lib" / "site-packages" / "PySide6" / "msvcp140.dll")
    with pytest.raises(SystemExit, match="msvcp140"):
        ws.check_imports(stage)


def test_bundled_dll_leaves_the_c_plus_plus_runtime_to_the_system(ws: ModuleType) -> None:
    assert ws.BUNDLED_DLL.match("VCRUNTIME140.dll") and ws.BUNDLED_DLL.match("Qt6Core.dll")
    for system in ("MSVCP140.dll", "MSVCP140_1.dll", "CONCRT140.dll", "VCOMP140.DLL"):
        assert not ws.BUNDLED_DLL.match(system)


def test_record_dedupes_and_takes_provides(ws: ModuleType, tmp_path: Path) -> None:
    comps = tmp_path / "components.json"
    install = tmp_path / "install"
    _touch(install / "licenses" / "mv" / "LICENSE")
    _touch(install / "licenses" / "qt" / "LGPL-3.0-only.txt")
    comps.write_text(
        json.dumps(
            {
                "version": 1,
                "components": [
                    {
                        "name": "buttereye-vs-mvtools",
                        "version": "29.2-1",
                        "license": "GPL-2.0-or-later AND ISC AND GPL-2.0-or-later",
                        "provides": [["bundled(fftw)", "3.3.8"]],
                        "licence_files": ["licenses/mv/LICENSE"],
                    }
                ],
            }
        )
    )
    out = install / "buttereye-install.json"
    ws.record(
        comps,
        out,
        [
            "python3-pyside6=6.11.2=LGPL-3.0-only AND (FTL OR GPL-2.0-only) AND MIT"
            "=licenses/qt/LGPL-3.0-only.txt=bundled(qt6)(6.11.2),bundled(freetype)(2.14.3)"
        ],
    )
    rec = {c["name"]: c for c in json.loads(out.read_text())["components"]}
    assert rec["buttereye-vs-mvtools"]["license"] == "GPL-2.0-or-later AND ISC"
    qt = rec["python3-pyside6"]
    assert qt["license"] == "LGPL-3.0-only AND (FTL OR GPL-2.0-only) AND MIT"
    assert qt["provides"] == [["bundled(qt6)", "6.11.2"], ["bundled(freetype)", "2.14.3"]]
    with pytest.raises(SystemExit):
        ws.record(comps, out, ["x=1=MIT=licenses/nowhere.txt"])


def _qt_tarball(tmp: Path) -> Path:
    files = {
        "qtbase-everywhere-src-6.11.2/LICENSES/LGPL-3.0-only.txt": b"LGPL3",
        "qtbase-everywhere-src-6.11.2/LICENSES/GPL-3.0-only.txt": b"GPL3",
        "qtbase-everywhere-src-6.11.2/LICENSES/FTL.txt": b"FreeType licence",
        "qtbase-everywhere-src-6.11.2/LICENSES/MIT.txt": b"MIT licence",
        "qtbase-everywhere-src-6.11.2/src/3rdparty/freetype/qt_attribution.json": json.dumps(
            [
                {
                    "Id": "freetype",
                    "Name": "Freetype 2",
                    "Version": "2.14.3",
                    "LicenseId": "FTL OR GPL-2.0-only",
                    "LicenseFile": "LICENSE.txt",
                    "Copyright": "Copyright The FreeType Project",
                }
            ]
        ).encode(),
        "qtbase-everywhere-src-6.11.2/src/3rdparty/freetype/LICENSE.txt": b"FT dual",
        "qtbase-everywhere-src-6.11.2/src/3rdparty/freetype/src/base/ftbase.c": b"code",
    }
    path = tmp / "qtbase.tar.xz"
    with tarfile.open(path, "w:xz") as tar:
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    return path


def test_qt_notices(
    ws: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tarball = _qt_tarball(tmp_path)
    out = tmp_path / "pyside6"
    monkeypatch.setattr(ws, "QT_THIRD_PARTY", ("freetype",))
    ws.qt_notices(out, [tarball])
    expr, provides = capsys.readouterr().out.splitlines()
    assert expr == "(FTL OR GPL-2.0-only)"
    assert provides == "bundled(freetype)(2.14.3)"
    assert (out / "LGPL-3.0-only.txt").read_bytes() == b"LGPL3"
    assert (out / "GPL-3.0-only.txt").read_bytes() == b"GPL3"
    text = (out / "third-party" / "freetype.txt").read_text()
    assert "Freetype 2" in text and "Copyright The FreeType Project" in text
    assert "FT dual" in text and "FreeType licence" in text
    assert "freetype.txt" in (out / "third-party" / "README.txt").read_text()


def test_qt_notices_refuses_an_unknown_id(ws: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="qt_attribution"):
        ws.qt_notices(tmp_path / "out", [_qt_tarball(tmp_path)])  # the real list: not all there


def test_sources_txt_is_truthful_about_zlib_and_the_models(ws: ModuleType, tmp_path: Path) -> None:
    out = tmp_path / "SOURCES.txt"
    ws.sources_txt(
        out,
        [
            "version=0.0.1", "repo=https://example.invalid/be", "ffms2=5.0", "ffmpeg=8.1.3",
            "dav1d=1.5.4", "vulkan_tools=1.4.328.1", "python=3.14.8", "pyside=6.11.2",
            "vapoursynth=81", "zlib=1.3.2", "fftw_srpm=mingw-fftw-3.3.8-20.fc44.src.rpm",
            "models=buttereye-rife-ncnn-models-r9_mod_v33.tar.gz",
        ],
    )  # fmt: skip
    text = out.read_text()
    assert "mingw-fftw-3.3.8-20.fc44.src.rpm" in text
    assert "buttereye-rife-ncnn-models-r9_mod_v33.tar.gz" in text
    assert "zlib 1.3.2 (Zlib licence, which asks for no source)" in text
    assert "SvgWidgets" in text and "qsvgicon" in text and "qtimageformats" not in text
