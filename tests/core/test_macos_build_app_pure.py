# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What packaging/macos/build-app.sh promises, read from the script itself so it
runs anywhere: the Info.plist it writes (document types for Open With and the
Dock icon, the Matroska and WebM imports), mkvmerge collected only on request,
and ``strip -x`` before the ad-hoc signature. The built bundle's half is
tests/macos/test_app_bundle.py, behind ``BUTTEREYE_TEST_APP``."""

from __future__ import annotations

import hashlib
import plistlib
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = (REPO / "packaging" / "macos" / "build-app.sh").read_text(encoding="utf-8")


def _plist() -> dict[str, object]:
    body = re.search(r'cat >"\$contents/Info.plist" <<EOF\n(.*?)\nEOF\n', SCRIPT, re.S)
    assert body is not None
    text = body.group(1).replace("$version", "1.2.3").replace("$minos", "13.0")
    return plistlib.loads(text.encode())


def test_finder_may_open_any_movie_with_butter_eye_but_never_by_default() -> None:
    info = _plist()
    (doc,) = info["CFBundleDocumentTypes"]  # type: ignore[misc]
    assert doc["CFBundleTypeRole"] == "Viewer"
    assert doc["LSHandlerRank"] == "Alternate"
    assert set(doc["LSItemContentTypes"]) == {
        "public.movie",
        "org.matroska.mkv",
        "org.webmproject.webm",
    }


def test_matroska_and_webm_are_declared_as_movies() -> None:
    info = _plist()
    by_id = {t["UTTypeIdentifier"]: t for t in info["UTImportedTypeDeclarations"]}  # type: ignore[attr-defined]
    for uti, ext in (("org.matroska.mkv", "mkv"), ("org.webmproject.webm", "webm")):
        t = by_id[uti]
        assert t["UTTypeConformsTo"] == ["public.movie"]
        assert t["UTTypeTagSpecification"]["public.filename-extension"] == [ext]
    # an import, never an export: an installed player that owns the type wins
    assert "UTExportedTypeDeclarations" not in info


def test_the_rest_of_the_plist_is_unchanged() -> None:
    info = _plist()
    assert info["CFBundleIdentifier"] == "io.github.buttereye.ButterEye"
    assert info["CFBundleShortVersionString"] == "1.2.3"
    assert info["LSMinimumSystemVersion"] == "13.0"


def test_mkvmerge_is_carried_only_on_request() -> None:
    # 61 MiB (Homebrew's QtCore and ICU come with it), so it is opt-in
    assert "bundle_mkvmerge=${BUTTEREYE_BUNDLE_MKVMERGE:-0}" in SCRIPT
    optin = re.search(
        r"if \[\[ \$bundle_mkvmerge == 1 \]\]; then\n"
        r'\s+optional\+=\("bin/mkvmerge=\$prefix/bin/mkvmerge"\)',
        SCRIPT,
    )
    assert optin is not None
    required = re.search(r"formulae=\((mpv ffmpeg[^)]*)\)", SCRIPT)
    assert required is not None and "mkvtoolnix" not in required.group(1).split()


def test_local_symbols_are_stripped_before_the_signature_and_the_timestamps() -> None:
    strip = SCRIPT.index('strip -x "$f"')
    assert strip < SCRIPT.index('find "$app" -exec touch -h -t "$stamp" {} +')
    assert strip < SCRIPT.index('codesign -s - --force --timestamp=none "$f"')
    assert re.search(r"for tool in brew [^;]*\bstrip\b", SCRIPT)  # checked for up front


# ---------------------------------------------------------------- znedi3 (opt-in)
PLUGINS = (REPO / "packaging" / "macos" / "build-plugins.sh").read_text(encoding="utf-8")
PINS = (REPO / "packaging" / "macos" / "pins.sh").read_text(encoding="utf-8")


def test_znedi3_sources_are_pinned_by_commit_and_sha256() -> None:
    for name in ("ZNEDI3", "ZNEDI3_VSXX", "ZNEDI3_GRAPHENGINE"):
        assert re.search(rf"^{name}_SHA256=[0-9a-f]{{64}}$", PINS, re.M), name
        commit = re.search(rf"^{name}_COMMIT=([0-9a-f]{{40}})$", PINS, re.M)
        assert commit is not None, name
        assert re.search(
            rf"^{name}_URL=https://codeload\.github\.com/.*/\${name}_COMMIT$", PINS, re.M
        )
    assert re.search(r"^NNEDI3_WEIGHTS_SHA256=[0-9a-f]{64}$", PINS, re.M)


def test_znedi3_is_opt_in_and_carries_its_licence_duties() -> None:
    step = PLUGINS[
        PLUGINS.index("# ---------------------------------------------------------------- znedi3") :
    ]
    step = step[
        : step.index("# ---------------------------------------------------------------- the check")
    ]
    assert "if [[ ${BUTTEREYE_ZNEDI3:-0} == 1 ]]; then" in step
    for duty in ("licenses/vapoursynth-znedi3", "sources/znedi3", "components-znedi3.json",
                 "SOURCES.znedi3.txt", "nnedi3_weights.bin"):  # fmt: skip
        assert duty in step, duty
    # the weights travel beside the plugin, where vsznedi3 looks for them
    assert '"$res/vapoursynth/nnedi3_weights.bin"' in SCRIPT
    assert "libznedi3.dylib" in SCRIPT


def _znedi3_step() -> str:
    start = PLUGINS.index('znedi3_note="NOT BUILT')
    end = PLUGINS.index(
        "# ---------------------------------------------------------------- the check"
    )
    return PLUGINS[start:end]


@pytest.mark.skipif(
    not all(shutil.which(t) for t in ("bash", "make", "tar", "shasum")), reason="no build tools"
)
def test_a_znedi3_step_failing_after_the_download_leaves_the_build_standing(
    tmp_path: Path,
) -> None:
    """build-plugins.sh runs under set -euo pipefail: a failure after the
    downloads (here the archive lacks gpl2.txt, after the plugin is installed)
    must end only the znedi3 step, remove what it already installed, and let the
    build go on."""
    weights = b"weights"
    src = tmp_path / "src"
    src.mkdir()
    (src / "nnedi3_weights.bin").write_bytes(weights)
    (src / "Makefile").write_text("vsznedi3.so:\n\tprintf x > vsznedi3.so\n")
    archive = tmp_path / "znedi3.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname="znedi3-x")
    here = tmp_path / "here"
    here.mkdir()
    pins = [f"ZNEDI3{n}_{k}=x" for n in ("", "_VSXX", "_GRAPHENGINE") for k in ("URL", "SHA256")]
    (here / "pins.sh").write_text(
        "\n".join(pins)
        + f"\nNNEDI3_WEIGHTS_SHA256={hashlib.sha256(weights).hexdigest()}\n"
        + f"fetch() {{ printf '%s\\n' '{archive}'; }}\n"
    )
    out = tmp_path / "out"
    harness = (
        "set -euo pipefail\n"
        f"here='{here}'; work='{tmp_path / 'work'}'; out='{out}'; jobs=\n"
        'mkdir -p "$work" "$out/vapoursynth"\n'
        "BUTTEREYE_ZNEDI3=1\n" + _znedi3_step() + 'echo "after: $znedi3_note"\n'
    )
    res = subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=60, check=False
    )
    assert res.returncode == 0, res.stderr
    assert "after: FAILED: a step after the download exited" in res.stdout
    assert not (out / "vapoursynth" / "libznedi3.dylib").exists()
    assert not (out / "vapoursynth" / "nnedi3_weights.bin").exists()
    assert not (out / "licenses" / "vapoursynth-znedi3").exists()
