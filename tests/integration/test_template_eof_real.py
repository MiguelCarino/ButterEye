# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mpv plays to the end of the video with the frame analysis on.

In mpv the source clip's length is a placeholder, and a request for a frame
past the real end fails; the output frame that needs it fails with it and mpv
ends playback there. The analysis (cuts, still pairs, hold fill) reads up to six
source frames ahead of the engine, which cost the last 4-6 source frames of
every video (a 6-frame clip played 1 output frame of 12). The engine on its
own ends at the output frame between the last two source frames: 2n - 3 of 2n
at 2x. Every engine, with and without hold fill, has to reach at least that.
Also after a start in the middle (mpv numbers frames from 0 again there, so a
frame count from the core would be wrong) and on a clip that ends held."""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core import osapi
from buttereye.core.scriptgen import generator as g
from buttereye.core.types import BackendId

pytestmark = [pytest.mark.integration, pytest.mark.devbox]

PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")
MODEL = Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse")
ENGINES = {
    BackendId.MVTOOLS: osapi.plugin_file("mvtools.so"),
    BackendId.MVGPU: osapi.plugin_file("libmvgpu.so"),
    BackendId.RIFE_NCNN: osapi.plugin_file("librife.so"),
}
#: name: (lavfi graph at 24 fps, mpv options, source frames mpv plays)
CLIPS = {
    "36": ("testsrc2=s=320x180:r=24:d=1.5", (), 36),
    "6": ("testsrc2=s=320x180:r=24:d=0.25", (), 6),
    # the last 6 frames one picture: the analysis sees still pairs to the end
    "held-end": ("testsrc2=s=320x180:r=24:d=1.25,tpad=stop_mode=clone:stop=6", (), 36),
    # a keyframe every 6 frames, started 12 frames before the end
    "start": ("testsrc2=s=320x180:r=24:d=3", ("--start=2.5",), 12),
}


@pytest.fixture(scope="module")
def clips(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if not (shutil.which("mpv") and shutil.which("ffmpeg")):
        pytest.skip("mpv and ffmpeg are needed")
    folder = tmp_path_factory.mktemp("eof")
    out = {}
    for name, (graph, _, _) in CLIPS.items():
        path = folder / f"{name}.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", graph, "-c:v", "libx264", "-g", "6",
             "-pix_fmt", "yuv420p", "-y", str(path)],
            check=True, timeout=60,
        )  # fmt: skip
        out[name] = path
    return out


def _played(clip: Path, out: Path, params: g.FilterParams, options: tuple[str, ...]) -> int:
    script = out / "gen.vpy"
    g.write_script(script)
    shots = out / "shots"
    shots.mkdir()
    subprocess.run(
        ["mpv", "--no-config", "--no-audio", "--vo=image", "--vo-image-format=png",
         f"--vo-image-outdir={shots}", *options, f"--vf={g.vf_argument(script, params)}",
         str(clip)],
        check=True, timeout=180, capture_output=True,
    )  # fmt: skip
    return len(list(shots.glob("*.png")))


@pytest.mark.parametrize("name", list(CLIPS))
@pytest.mark.parametrize("backend", list(ENGINES), ids=lambda b: b.value)
@pytest.mark.parametrize("fill", [False, True], ids=["plain", "fill"])
@pytest.mark.parametrize("buffered", [4, 10])
def test_plays_to_the_end(
    clips: dict[str, Path],
    tmp_path: Path,
    name: str,
    backend: BackendId,
    fill: bool,
    buffered: int,
) -> None:
    if not (PLUGIN_DIR / ENGINES[backend]).exists():
        pytest.skip(f"{backend.value} is not installed")
    if backend is BackendId.RIFE_NCNN and not MODEL.is_dir():
        pytest.skip("no RIFE model")
    _, concurrent = g.default_frames(backend)
    # as the session sends them: still skip and a scene threshold on every engine
    params = g.FilterParams(
        gen=1, backend=backend, src_fps=Fraction(24), target_fps=Fraction(48),
        buffered_frames=buffered, concurrent_frames=min(concurrent, 8), matrix="709",
        range="limited", sc_threshold=0.12, title="eof",
        model_path=MODEL if backend is BackendId.RIFE_NCNN else None, gpu_thread=2,
        hold_fill=fill, still_skip=True,
    )  # fmt: skip
    _, options, frames = CLIPS[name]
    played = _played(clips[name], tmp_path, params, options)
    assert 2 * frames - 3 <= played <= 2 * frames
