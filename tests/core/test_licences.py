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

#: The COPR table, named rather than taken from the running platform: ``COMPONENTS``
#: is the Windows table on Windows and the Homebrew-tap one on macOS, and these tests
#: are about the Fedora rows wherever they run.
_FEDORA = licences.components(windows=False)


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
    rows = {r.name: r for r in licences.build_third_party(make_db(tmp_path), None, _FEDORA)}
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
        for r in licences.build_third_party(
            make_db(tmp_path, drop="LICENSE.ncnn.txt"), None, _FEDORA
        )
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
    rows = {r.name: r for r in licences.build_third_party(db, None, _FEDORA)}
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


# ---------------------------------------------------------------------------
# Windows: the rows the installer's record (buttereye-install.json) drives,
# simulated on Linux with the same record shape build-installer.sh writes
# ---------------------------------------------------------------------------

_WIN_RECORD = {
    "buttereye-vs-mvtools": (
        "29.2-1",
        "GPL-2.0-or-later AND ISC",
        [["bundled(fftw)", "3.3.8"]],
        ["LICENSE", "x86inc.asm", "fftw-COPYING", "fftw-COPYRIGHT"],
    ),
    "ffms2": (
        "5.0",
        "MIT AND LGPL-2.1-or-later AND BSD-2-Clause AND Zlib",
        [["bundled(ffmpeg)", "8.1.3"], ["bundled(dav1d)", "1.5.4"], ["bundled(zlib)", "1.3.2"]],
        [
            "COPYING.ffms2",
            "COPYING.LGPLv2.1.ffmpeg",
            "LICENSE.ffmpeg.md",
            "FFMPEG-CONFIGURATION.txt",
            "COPYING.dav1d",
            "LICENSE.zlib",
        ],
    ),
    "vulkan-tools": (
        "1.4.328.1",
        "Apache-2.0 AND MIT",
        [["bundled(volk)", "1.4.328.1"]],
        ["LICENSE.txt", "LICENSE.volk.md"],
    ),
    "python3": ("3.14.8", "PSF-2.0 AND 0BSD AND MIT", [], ["LICENSE.txt"]),
    "microsoft-vcruntime": (
        "14.51.36247.0",
        "LicenseRef-Microsoft-Distributable-Code",
        [],
        ["LICENSE.txt", "MICROSOFT-VCRUNTIME.txt"],
    ),
    "python3-pyside6": ("6.11.2", "LGPL-3.0-only AND MIT", [], ["LGPL-3.0-only.txt"]),
    "python3-shiboken6": ("6.11.2", "LGPL-3.0-only", [], ["LGPL-3.0-only.txt"]),
}


def _windows_db(install: Path) -> RpmDb:
    import json

    from buttereye.core.doctor.checks_pkgs import INSTALL_MANIFEST, manifest_db

    comps = []
    for name, (version, lic, provides, files) in _WIN_RECORD.items():
        rel = [f"licenses/{name}/{f}" for f in files]
        for f in rel:
            (install / f).parent.mkdir(parents=True, exist_ok=True)
            (install / f).write_text("text")
        comps.append(
            {
                "name": name,
                "version": version,
                "license": lic,
                "provides": provides,
                "licence_files": rel,
            }
        )
    (install / INSTALL_MANIFEST).write_text(json.dumps({"version": 1, "components": comps}))
    return manifest_db(install)


def test_windows_ffmpeg_is_the_lgpl_build_inside_ffms2(tmp_path: Path) -> None:
    rows = {
        r.name: r
        for r in licences.build_third_party(
            _windows_db(tmp_path), None, licences.components(windows=True)
        )
    }
    ff = rows["FFmpeg"]
    assert ff.spdx == "LGPL-2.1-or-later" and ff.version == "8.1.3"
    assert ff.conveyed and ff.detected and ff.finding is None
    assert ff.relation == licences.REL_IN_FFMS2
    assert {p.name for p in ff.text_files} == {
        "COPYING.LGPLv2.1.ffmpeg",
        "LICENSE.ffmpeg.md",
        "FFMPEG-CONFIGURATION.txt",
    }
    assert {p.name for p in rows["ffms2"].text_files} == {"COPYING.ffms2"}
    assert rows["ffms2"].spdx == "MIT" and rows["ffms2"].conveyed


@pytest.mark.parametrize(
    ("name", "version", "spdx", "text"),
    [
        ("dav1d", "1.5.4", "BSD-2-Clause", "COPYING.dav1d"),
        ("zlib", "1.3.2", "Zlib", "LICENSE.zlib"),
        ("fftw", "3.3.8", "GPL-2.0-or-later", "fftw-COPYING"),
        ("volk", "1.4.328.1", "MIT", "LICENSE.volk.md"),
    ],
)
def test_windows_bundled_components_have_rows(
    tmp_path: Path, name: str, version: str, spdx: str, text: str
) -> None:
    rows = {
        r.name: r
        for r in licences.build_third_party(
            _windows_db(tmp_path), None, licences.components(windows=True)
        )
    }
    r = rows[name]
    assert r.conveyed and r.detected and r.finding is None
    assert r.version == version and r.spdx == spdx
    assert text in {p.name for p in r.text_files}


def test_windows_runtime_rows(tmp_path: Path) -> None:
    rows = {
        r.name: r
        for r in licences.build_third_party(
            _windows_db(tmp_path), None, licences.components(windows=True)
        )
    }
    assert "fftw" not in " ".join(p.name for p in rows["MVTools"].text_files)
    assert {p.name for p in rows["Vulkan-Tools (vulkaninfo)"].text_files} == {"LICENSE.txt"}
    vc = rows["Microsoft Visual C++ runtime (Python's copy)"]
    assert vc.conveyed and vc.version == "14.51.36247.0" and vc.finding is None
    assert vc.spdx == "LicenseRef-Microsoft-Distributable-Code"
    assert rows["Shiboken6"].spdx == "LGPL-3.0-only" and rows["Shiboken6"].conveyed
    assert rows["PySide6 / Qt"].spdx == "LGPL-3.0-only AND MIT"  # the record's
    assert rows["PySide6 / Qt"].conveyed and rows["VapourSynth"].conveyed


def test_linux_rows_unchanged() -> None:
    names = [c.name for c in licences.components(windows=False)]
    assert names == [
        "RIFE-ncnn-Vulkan",
        "ncnn",
        "glslang",
        "MVTools",
        "RIFE ncnn models",
        "mvgpu",
        "vsmlrt.py",
        "onnxconverter-common",
        "FSRCNNX x2 8-0-4-1 shader",
        "PySide6 / Qt",
        "mpv",
        "VapourSynth",
        "FFmpeg",
        "ffms2",
        "mkvtoolnix",
    ]
    linux = {c.name: c for c in licences.components(windows=False)}
    assert linux["FFmpeg"].spdx == "GPL-3.0-or-later" and not linux["FFmpeg"].conveyed
    assert not linux["ffms2"].conveyed and not linux["PySide6 / Qt"].conveyed
    if not (licences.osapi.IS_WINDOWS or licences.osapi.IS_MACOS):
        assert licences.COMPONENTS == licences.components(windows=False)


def test_windows_source_offer_is_sources_txt(tmp_path: Path) -> None:
    assert licences._source_link(windows=True, install=tmp_path) == licences._source_link(
        windows=False
    )  # no SOURCES.txt: the project's link
    offer = tmp_path / "licenses" / "SOURCES.txt"
    offer.parent.mkdir()
    offer.write_text("where the source is")
    assert licences._source_link(windows=True, install=tmp_path) == str(offer)


# ---------------------------------------------------------------------------
# macOS: what the Homebrew tap conveys, and what it only drives
# (packaging/macos/SOURCES.md). Pure, so these run on every platform: the macOS
# legal statement must not be changeable from a Fedora checkout without a red test.
# ---------------------------------------------------------------------------

_MACOS = licences.components(windows=False, macos=True)

#: What the tap's own formulae build, and nothing else (SOURCES.md, "What the tap
#: conveys, and where its source is", plus the shader inside ButterEye itself).
_TAP_CONVEYS = {"RIFE-ncnn-Vulkan", "RIFE ncnn models", "mvgpu", "FSRCNNX x2 8-0-4-1 shader"}


def test_macos_conveys_only_what_the_tap_builds() -> None:
    """Every other row is a Homebrew bottle the user installs: Homebrew declares the
    licence, carries the source duty and ships the text in its own keg."""
    assert {c.name for c in _MACOS if c.conveyed} == _TAP_CONVEYS
    assert [c.name for c in _MACOS if not c.conveyed] == [
        "ncnn",
        "libomp",
        "MVTools",
        "PySide6 / Qt",
        "mpv",
        "VapourSynth",
        "FFmpeg",
        "ffms2",
        "mkvtoolnix",
    ]
    # The tap links nothing statically (-Duse_system_ncnn=true), so the relation the
    # Fedora and Windows tables give ncnn and glslang cannot appear on any row here.
    assert licences.REL_STATIC not in [c.relation for c in _MACOS]


def test_macos_rife_rows_match_the_tap_formula() -> None:
    rows = {c.name: c for c in _MACOS}
    rife = rows["RIFE-ncnn-Vulkan"]
    assert rife.spdx == "MIT AND LGPL-2.1-or-later AND WTFPL"  # the formula's license all_of
    assert rife.conveyed
    assert "glslang" not in rows  # the tap's build neither links nor ships it
    ncnn = rows["ncnn"]
    assert ncnn.package == "ncnn"  # the Homebrew formula, which is what a user installs
    assert ncnn.spdx == "BSD-3-Clause" and ncnn.spdx_from_rpm
    assert not ncnn.conveyed and ncnn.relation == licences.REL_LINKED_RIFE
    assert rows["libomp"].relation == licences.REL_LINKED_RIFE and not rows["libomp"].conveyed
    mv = rows["MVTools"]
    assert not mv.conveyed and mv.spdx == "GPL-2.0-or-later" and mv.spdx_from_rpm
    # TensorRT only, and osapi.HAS_VSTRT is False on macOS; no tap formula has either.
    assert "vsmlrt.py" not in rows and "onnxconverter-common" not in rows


def test_macos_table_shares_the_fedora_rows_it_does_not_change() -> None:
    """The rows that are already true on a Mac are the Fedora objects themselves, so
    the two tables cannot drift apart."""
    fedora = {c.name: c for c in _FEDORA}
    mac = {c.name: c for c in _MACOS}
    for name in (
        "FSRCNNX x2 8-0-4-1 shader",
        "PySide6 / Qt",
        "mpv",
        "VapourSynth",
        "FFmpeg",
        "ffms2",
        "mkvtoolnix",
    ):
        assert mac[name] is fedora[name], name


def test_macos_tap_keg_without_root_licence_texts_is_not_a_packaging_bug() -> None:
    """A tap keg installs its licence texts under
    ``<keg>/share/doc/<formula>/licenses``, which ``brew.BrewKeg.licence_files`` does
    not read: it reports the keg's root. Counting texts here would therefore report a
    packaging bug (BE-1021) against formulae that do install them."""
    db = RpmDb(
        True,
        MappingProxyType(
            {
                "buttereye-vs-rife-ncnn": info("buttereye-vs-rife-ncnn", [], evr="9.33"),
                "buttereye-rife-ncnn-models": info(
                    "buttereye-rife-ncnn-models", [], evr="r9_mod_v33"
                ),
                "buttereye-vs-mvgpu": info(
                    "buttereye-vs-mvgpu", [], evr="0.0.3", lic="AGPL-3.0-or-later"
                ),
            }
        ),
    )
    rows = {r.name: r for r in licences.build_third_party(db, None, _MACOS)}
    for name in ("RIFE-ncnn-Vulkan", "RIFE ncnn models", "mvgpu"):
        assert rows[name].detected and rows[name].conveyed, name
        assert rows[name].finding is None, name
