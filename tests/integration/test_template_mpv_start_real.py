# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The first frames mpv shows once the filter starts are pictures, not black.

MVTools reads frames while the script is built, before mpv can serve any; mpv
hands over black dummy frames then ("Frame requested during init!"), and every
filter of the graph used to keep what it made of them: about half a second of
black at each start. mpv writes its first output frames to PNG here
(``--vo=image``) and each one is checked for a picture."""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core import osapi
from buttereye.core.scriptgen import generator as g
from buttereye.core.types import BackendId

np = pytest.importorskip("numpy")
Image = pytest.importorskip("PIL.Image")

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")
MODEL = Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse")
FRAMES = 24
ENGINES = {
    BackendId.MVTOOLS: osapi.plugin_file("mvtools.so"),
    BackendId.MVGPU: osapi.plugin_file("libmvgpu.so"),
    BackendId.RIFE_NCNN: osapi.plugin_file("librife.so"),
}


@pytest.fixture(scope="module")
def clip(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        pytest.skip("mpv and ffmpeg are needed")
    path = tmp_path_factory.mktemp("start") / "testsrc.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=24000/1001:d=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-y", str(path)],
        check=True, timeout=60,
    )  # fmt: skip
    return path


def _first_frames(clip: Path, out: Path, params: g.FilterParams) -> list[float]:
    script = out / "gen.vpy"
    g.write_script(script)
    shots = out / "shots"
    shots.mkdir()
    subprocess.run(
        ["mpv", "--no-config", "--ao=null", "--vo=image", "--vo-image-format=png",
         f"--vo-image-outdir={shots}", f"--frames={FRAMES}",
         f"--vf={g.vf_argument(script, params)}", str(clip)],
        check=True, timeout=120, capture_output=True,
    )  # fmt: skip
    files = sorted(shots.glob("*.png"))
    assert len(files) == FRAMES
    return [float(np.asarray(Image.open(f).convert("L")).mean()) for f in files]


@pytest.mark.parametrize("backend", list(ENGINES), ids=lambda b: b.value)
@pytest.mark.parametrize("extras", [False, True], ids=["plain", "fill+still"])
def test_no_black_frames_at_the_start(
    clip: Path, tmp_path: Path, backend: BackendId, extras: bool
) -> None:
    if not (PLUGIN_DIR / ENGINES[backend]).exists():
        pytest.skip(f"{backend.value} is not installed")
    if backend is BackendId.RIFE_NCNN and not MODEL.is_dir():
        pytest.skip("no RIFE model")
    buffered, concurrent = g.default_frames(backend)
    params = g.FilterParams(
        gen=1, backend=backend, src_fps=Fraction(24000, 1001), target_fps=Fraction(48000, 1001),
        buffered_frames=max(buffered, 10), concurrent_frames=concurrent, matrix="709",
        range="limited", sc_threshold=0.12, title="start",
        model_path=MODEL if backend is BackendId.RIFE_NCNN else None, hold_fill=extras,
        still_skip=extras,
    )  # fmt: skip
    means = _first_frames(clip, tmp_path, params)
    # testsrc2 is mid-grey on average; a black dummy frame is ~0 (limited range: 16)
    assert min(means) > 60, [round(m) for m in means]
