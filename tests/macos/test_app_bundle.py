# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""A built ButterEye.app, measured from the outside (docs/PLATFORMS.md, "The .app").

Opt-in: ``BUTTEREYE_TEST_APP=/path/to/ButterEye.app`` (packaging/macos/build-app.sh
leaves one in $XDG_CACHE_HOME/buttereye/macos-app/stage, and the disk image holds
another). Nothing else needs to be installed - that is the point - and every
command runs the way Finder would start it: a scrubbed environment with only
``HOME`` and the system ``PATH``, and a ``HOME`` made fresh for the test, so no
configuration, cache, benchmark or Homebrew on the developer's own account takes
part. What each test holds:

- the bundle is signed (ad hoc) and its Info.plist says what the build promised;
- no Mach-O in it names a file outside it but macOS's own (appstage.py check);
- no library from Homebrew or ``/usr/local`` is *loaded* while its programs run
  (``DYLD_PRINT_LIBRARIES``), including VapourSynth started inside mpv;
- every engine's plugin loads in the app's own VapourSynth;
- the doctor, run from the app, has nothing blocking and finds everything in it;
- live smoothing through the app's mpv, and an offline render through its
  vspipe and ffmpeg, both end to end.

The pure half - the seam's decisions with the app faked - is
tests/core/test_macos_app_pure.py and runs everywhere.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core import osapi

APP = os.environ.get("BUTTEREYE_TEST_APP", "")
pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="a macOS app bundle"),
    pytest.mark.skipif(not APP, reason="set BUTTEREYE_TEST_APP=/path/to/ButterEye.app"),
]

REPO = Path(__file__).resolve().parents[2]
#: the PATH an app started from Finder gets (launchd's default)
FINDER_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
#: where a library from outside the app would come from
FOREIGN = ("/opt/homebrew/", "/usr/local/")


@pytest.fixture(scope="module")
def app() -> Path:
    return Path(APP).resolve()


@pytest.fixture(scope="module")
def res(app: Path) -> Path:
    return app / "Contents" / "Resources"


@pytest.fixture
def home() -> Iterator[Path]:
    # short, so the sockets mpv binds under it stay inside macOS's 103 bytes
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="be-app-"))
    try:
        yield path
    finally:
        subprocess.run(["rm", "-rf", str(path)], check=False)


def _env(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), "PATH": FINDER_PATH, **extra}


def _cli(
    app: Path, home: Path, *args: str, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(app / "Contents" / "MacOS" / "buttereye"), *args],
        env=_env(home),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/",
    )


def _child_env(res: Path, home: Path) -> dict[str, str]:
    """What ButterEye hands the children it starts (``osapi.point_at_app``), with
    VSScript's configuration line written for this HOME."""
    env = _env(home)
    osapi.point_at_app(env, res)
    assert osapi.register_vsscript(env, res)
    return env


def _make_clip(res: Path, out: Path, seconds: int) -> Path:
    """A 640x360 24 fps H.264 clip with audio, made by the app's own ffmpeg."""
    subprocess.run(
        [str(res / "bin" / "ffmpeg"), "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)],
        env={"PATH": FINDER_PATH}, check=True, timeout=120,
    )  # fmt: skip
    return out


@pytest.fixture(scope="module")
def clip_dir() -> Iterator[Path]:
    folder = Path(tempfile.mkdtemp(dir="/tmp", prefix="be-clip-"))
    try:
        yield folder
    finally:
        subprocess.run(["rm", "-rf", str(folder)], check=False)


@pytest.fixture(scope="module")
def clips(res: Path, clip_dir: Path) -> Path:
    """6 s: enough to render and to load a filter in."""
    return _make_clip(res, clip_dir / "clip.mkv", 6)


@pytest.fixture(scope="module")
def long_clip(res: Path, clip_dir: Path) -> Path:
    """40 s: live play is sampled well inside it, so no loop or end interferes."""
    return _make_clip(res, clip_dir / "long.mkv", 40)


# ---------------------------------------------------------------- the bundle itself
def test_the_bundle_is_signed_and_says_what_it_is(app: Path) -> None:
    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(app)], check=True, capture_output=True
    )
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "io.github.buttereye.ButterEye"
    assert info["CFBundleExecutable"] == "ButterEye"
    assert info["LSArchitecturePriority"] == ["arm64"]
    # Open With / the Dock icon (gui/app.py FileOpenRouter)
    (doc,) = info["CFBundleDocumentTypes"]
    assert doc["LSHandlerRank"] == "Alternate" and "public.movie" in doc["LSItemContentTypes"]
    out = _cli(app, Path("/tmp"), "--version").stdout
    assert f"ButterEye {info['CFBundleShortVersionString']}" in out
    # the oldest macOS dyld will start it on: the newest minos in the bundle
    check = subprocess.run(
        [str(app / "Contents/Resources/python/bin/python3.14"), "-I", "-B",
         str(REPO / "packaging/macos/appstage.py"), "check", str(app)],
        capture_output=True, text=True, check=True, timeout=600,
    )  # fmt: skip
    minos = re.search(r"^minos (\S+)", check.stdout, re.M)
    assert minos is not None and minos.group(1) == info["LSMinimumSystemVersion"]


def test_the_bundled_mkvmerge_runs_from_the_app(res: Path) -> None:
    """The offline render's remux tool, relocated like mpv: nothing from Homebrew.
    Carried only by a BUTTEREYE_BUNDLE_MKVMERGE=1 build (packaging/macos/build-app.sh)."""
    if not (res / "bin" / "mkvmerge").exists():
        pytest.skip("built without BUTTEREYE_BUNDLE_MKVMERGE=1")
    env = {"PATH": "/usr/bin:/bin", "DYLD_PRINT_LIBRARIES": "1"}
    done = subprocess.run(
        [str(res / "bin" / "mkvmerge"), "--version"],
        capture_output=True, text=True, env=env, timeout=60, check=True,
    )  # fmt: skip
    assert done.stdout.startswith("mkvmerge v")
    assert "/opt/homebrew" not in done.stderr and "/usr/local/" not in done.stderr


def test_no_library_from_outside_the_app_is_loaded(res: Path, home: Path, clips: Path) -> None:
    env = _child_env(res, home)
    env["DYLD_PRINT_LIBRARIES"] = "1"
    vpy = home / "pass.vpy"
    vpy.write_text("import vapoursynth as vs\nvideo_in.set_output()\n")
    runs = [
        [str(res / "bin" / "ffmpeg"), "-hide_banner", "-version"],
        [str(res / "bin" / "vulkaninfo"), "--summary"],
        [os.fspath(osapi.app_runtime(res)["vspipe"]), "--version"],
        # VapourSynth started inside mpv: VSScript, the app's Python and its module
        [str(res / "bin" / "mpv"), "--no-config", "--vo=null", "--ao=null", "--frames=10",
         f"--vf=vapoursynth={vpy}", str(clips)],
    ]  # fmt: skip
    for argv in runs:
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, (argv[0], done.stderr[-2000:])
        loaded = re.findall(r"^dyld\[\d+\]: <[^>]*> (\S+)$", done.stderr, re.M)
        loaded += re.findall(r"^dyld\[\d+\]: (/\S+)$", done.stderr, re.M)
        assert loaded, f"{argv[0]}: DYLD_PRINT_LIBRARIES printed nothing"
        foreign = [p for p in loaded if p.startswith(FOREIGN)]
        assert not foreign, (argv[0], foreign)
        if "mpv" in argv[0]:
            assert any(p.endswith("/libvsscript.dylib") and p.startswith(str(res)) for p in loaded)


def test_every_engine_loads_in_the_app_s_vapoursynth(res: Path, home: Path) -> None:
    plugins = res / "vapoursynth"
    vpy = home / "engines.vpy"
    vpy.write_text(
        "import vapoursynth as vs\n"
        "core = vs.core\n"
        f"for f in ('mvtools.dylib', 'libffms2.dylib', 'librife.dylib', 'libmvgpu.dylib'):\n"
        f"    core.std.LoadPlugin({str(plugins)!r} + '/' + f)\n"
        "import sys\n"
        "sys.stderr.write(' '.join(sorted(p.namespace for p in core.plugins())) + '\\n')\n"
        "core.std.BlankClip(length=1).set_output()\n"
    )
    vspipe = os.fspath(osapi.app_runtime(res)["vspipe"])
    done = subprocess.run(
        [vspipe, "--info", str(vpy)], env=_child_env(res, home),
        capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr[-2000:]
    names = set(done.stderr.split())
    assert {"mv", "ffms2", "rife", "mvgpu"} <= names, done.stderr


@pytest.mark.parametrize("backend", ["mvgpu", "mvtools"])
def test_the_benchmark_times_the_live_engines_inside_the_app_s_mpv(
    res: Path, home: Path, backend: str
) -> None:
    """The in-mpv pass the benchmark runs (core/bench/measure.py), for real: its
    generated source is endless, so bench.vpy must bound what it hands the
    engine. Unbounded, mvgpu refused the INT_MAX-frame clip and mpv aborted on
    every pass, so no engine was ever measured in mpv on a Mac."""
    from buttereye.core.bench import measure

    vpy = res / "python/lib/python3.14/site-packages/buttereye/core/bench/bench.vpy"
    data = {
        "backend": backend, "frames": 50, "gpu_id": None, "gpu_thread": 1,
        "width": 640, "height": 360, "matrix": "170m", "model_path": None,
        "src_num": 24000, "src_den": 1001, "out_num": 48000, "out_den": 1001,
        "plugin_dir": str(res / "vapoursynth"), "uhd": False,
    }  # fmt: skip
    argv = measure.mpv_argv(
        str(res / "bin" / "mpv"), vpy, data, width=640, height=360,
        source_fps=Fraction(24000, 1001), frames=96, concurrent_frames=4,
    )  # fmt: skip
    done = subprocess.run(
        argv, env=_child_env(res, home), capture_output=True, text=True, timeout=120
    )
    log = done.stdout + done.stderr
    assert done.returncode == 0, log[-3000:]
    assert measure.parse_mpv(log, 96, prefilled=4).fps > 0


# ---------------------------------------------------------------- ButterEye, from the app
def test_the_doctor_from_the_app_has_nothing_blocking(app: Path, res: Path, home: Path) -> None:
    done = _cli(app, home, "doctor", "--json")
    data = json.loads(done.stdout)["data"]
    by_id = {f["id"]: f for f in data["findings"]}
    blocking = [f["id"] for f in data["findings"] if f["severity"] == "blocking"]
    assert not blocking, blocking
    assert by_id["mpv.host_binary"]["evidence"] == [str(res / "bin" / "mpv")]
    for row in ("probe.vs_version", "probe.plugins", "packages.mvtools", "packages.rife",
                "packages.mvgpu", "packages.models", "render.ffms2", "vulkan.loader"):  # fmt: skip
        assert by_id[row]["severity"] == "ok", by_id[row]
    for ev in by_id["probe.plugins"]["evidence"]:
        assert str(res / "vapoursynth") in ev, ev
    # nothing in it tells the user to install anything with Homebrew
    assert not [f["id"] for f in data["findings"]
                if any("brew" in c for c in f.get("commands") or ())
                and f["id"] not in ("render.mkvmerge",)]  # fmt: skip


def _mpv_ask(sock: str, *props: str) -> dict[str, object]:
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.settimeout(5)
    out: dict[str, object] = {}
    buf = b""
    for i, p in enumerate(props):
        s.sendall(json.dumps({"command": ["get_property", p], "request_id": i}).encode() + b"\n")
    while len(out) < len(props):
        buf += s.recv(65536)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = json.loads(line)
            if "request_id" in m:
                out[props[m["request_id"]]] = m.get("data")
    s.close()
    return out


def test_live_smoothing_through_the_app_s_mpv(
    app: Path, res: Path, home: Path, long_clip: Path
) -> None:
    """``buttereye play`` with the MVTools profile: mpv from the app, the filter
    created and producing twice the source's rate, in real time. mpv draws to
    nothing and plays to nothing (the test's own mpv.conf)."""
    conf = home / ".config" / "mpv"
    conf.mkdir(parents=True)
    (conf / "mpv.conf").write_text("vo=null\nao=null\n")
    started = time.time()
    proc = subprocess.Popen(
        [str(app / "Contents/MacOS/buttereye"), "play", "-v", "--profile", "cpu", str(long_clip)],
        env=_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/",
    )  # fmt: skip
    sock: str | None = None
    try:
        deadline = time.monotonic() + 30
        tmp = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"], capture_output=True, text=True)
        folder = Path(tmp.stdout.strip()) / "buttereye"
        while time.monotonic() < deadline and sock is None:
            for s in sorted(folder.glob("mpv-*.sock"), key=lambda p: p.stat().st_mtime):
                if s.stat().st_mtime >= started - 1:
                    sock = str(s)
            time.sleep(0.5)
        assert sock is not None, "no mpv socket appeared"
        time.sleep(6)
        first = _mpv_ask(sock, "time-pos")
        t0 = time.monotonic()
        time.sleep(6)
        state = _mpv_ask(sock, "time-pos", "estimated-vf-fps", "container-fps", "vf", "path")
        wall = time.monotonic() - t0
        assert isinstance(state["vf"], list) and state["vf"], state
        assert "mvtools" in json.dumps(state["vf"])
        fps = float(state["estimated-vf-fps"])  # type: ignore[arg-type]
        assert abs(fps - 2 * float(state["container-fps"])) < 1.0, state  # type: ignore[arg-type]
        played = float(state["time-pos"]) - float(first["time-pos"])  # type: ignore[arg-type]
        assert played > 0.9 * wall, (played, wall)  # real time, not a slow crawl
    finally:
        if sock is not None:
            s = socket.socket(socket.AF_UNIX)
            s.connect(sock)
            s.sendall(b'{"command": ["quit"]}\n')
            s.close()
        out, err = proc.communicate(timeout=30)
    assert "with mvtools (active" in out + err, out + err


def test_offline_render_through_the_app(app: Path, res: Path, home: Path, clips: Path) -> None:
    out = home / "out.mkv"
    done = _cli(app, home, "render", "--json", "--profile", "cpu", "--target", "2x",
                "-o", str(out), str(clips), timeout=600)  # fmt: skip
    data = json.loads(done.stdout)["data"]
    assert data["state"] == "succeeded", data
    probe = subprocess.run(
        [str(res / "bin" / "ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,r_frame_rate,nb_read_frames", "-of", "json", str(out)],
        env={"PATH": FINDER_PATH}, capture_output=True, text=True, check=True,
    )  # fmt: skip
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["r_frame_rate"] == "48/1"
    assert int(stream["nb_read_frames"]) == 288  # 6 s at 24 fps, doubled
