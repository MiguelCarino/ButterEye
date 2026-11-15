# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Motion-compensated frames in VK_LAYER_BUTTEREYE_frames (stage S4c), on the CPU.

Builds mvgpu with ``-Dlayer=enabled`` (or uses $FRAMES_LAYER_BUILD) and runs on lavapipe
with every implicit layer off, so no GPU is used:

* ``frames-test`` (the layer's mc code without a window) on drawn frames: a textured
  square moving over a textured background must land where it is half-way, much closer
  to the drawn truth than the plain blend; pixels that do not change keep their bits;
  a cut gives the frame before it; the video rectangle, the scale-down to source size
  and the HDR10 (PQ) luma path work;
* the layer itself with BUTTEREYE_FRAMES_MODE=mc in ``frames-present-test``: k = 3 app
  presents send two in-between frames each, the summary and the control block's mode
  agree, and (with $FRAMES_VVL_PATH) the validation layer reports nothing, with the work
  in one submit or split into one per in-between frame;
* the split submits give the same bytes as one submit, the engine made on a worker thread
  keeps the presents going (blends until it is ready), and the cut counter in the control
  block counts what the GPU cut test decided."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

np: Any = pytest.importorskip("numpy")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the layer is Linux only"),
]

REPO = Path(__file__).resolve().parents[2]
LVP = Path("/usr/share/vulkan/icd.d/lvp_icd.x86_64.json")
LAYER = "VK_LAYER_BUTTEREYE_frames"
PREFIX = "buttereye-frames: "
W, H = 320, 184
SQUARE = (60, 108)  # rows of the moving square
STEP = 8  # px the square moves between drawn frames (16 between source frames)


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The layer folder of a build: frames-test, the layer and frames-present-test."""
    given = os.environ.get("FRAMES_LAYER_BUILD")
    if given:
        folder = Path(given) / "layer"
    else:
        for tool in ("meson", "ninja"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} is not installed")
        out = tmp_path_factory.mktemp("frames-mc") / "build"
        glslang = shutil.which("glslang") or shutil.which("glslangValidator") or "glslang"
        subprocess.run(
            [
                "meson",
                "setup",
                str(out),
                str(REPO / "mvgpu"),
                "-Dlayer=enabled",
                f"-Dglslang={glslang}",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "ninja",
                "-C",
                str(out),
                "layer/frames-test",
                "layer/libVkLayer_buttereye_frames.so",
                "layer/frames-present-test",
            ],
            check=True,
            capture_output=True,
        )
        folder = out / "layer"
    for name in (
        "frames-test",
        "libVkLayer_buttereye_frames.so",
        "VkLayer_buttereye_frames.json",
        "frames-present-test",
    ):
        if not (folder / name).exists():
            pytest.skip(f"{folder / name} was not built")
    if not LVP.exists():
        pytest.skip("lavapipe (mesa-vulkan-drivers) is not installed")
    return folder


def _env(build: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VK_", "BUTTEREYE_FRAMES", "NVPRESENT", "MVGPU_"))
    }
    env.update(
        VK_DRIVER_FILES=str(LVP),
        VK_LOADER_LAYERS_DISABLE="~implicit~",
        VK_ADD_LAYER_PATH=str(build),
        LP_NUM_THREADS="2",
    )
    env.update(extra)
    return env


def _scene(n: int, w: int = W, h: int = H) -> list[Any]:
    """Frames 0..n-1 (RGB floats): a smooth random background and a textured square at
    x = 40 + i * STEP, rows SQUARE."""
    rng = np.random.default_rng(3)
    small = rng.random((h // 8 + 2, w // 8 + 2))
    bg = np.kron(small, np.ones((8, 8)))[:h, :w]
    k = np.zeros((h, w))
    k[:5, :5] = 1 / 25
    bg = np.real(np.fft.ifft2(np.fft.fft2(bg) * np.fft.fft2(k)))
    bg = 0.3 + 0.4 * (bg - bg.min()) / (bg.max() - bg.min())
    sq = rng.random((48, 48)) * 0.4 + 0.4
    out = []
    for i in range(n):
        x0 = 40 + i * STEP
        img = np.stack([bg, bg * 0.95, bg * 0.9], 2)
        for c, gain in enumerate((1.0, 0.3, 0.1)):
            img[SQUARE[0] : SQUARE[1], x0 : x0 + 48, c] = sq * gain
        out.append(img)
    return out


def _write(path: Path, img: Any, maxval: int = 1023) -> None:
    v = np.clip(np.round(img * maxval), 0, maxval).astype(">u2")
    path.write_bytes(b"P6\n%d %d\n%d\n" % (img.shape[1], img.shape[0], maxval) + v.tobytes())


def _read(path: Path) -> Any:
    raw = path.read_bytes()
    parts = raw.split(maxsplit=4)
    w, h, maxval = int(parts[1]), int(parts[2]), int(parts[3])
    return np.frombuffer(parts[4], dtype=">u2").reshape(h, w, 3).astype(np.int64), maxval


def _frames_test(build: Path, tmp: Path, imgs: list[Any], *args: str, **env: str) -> list[Any]:
    """Runs frames-test on imgs (10-bit); the in-between frame of each pair (k = 2)."""
    names = []
    for i, img in enumerate(imgs):
        names.append(str(tmp / f"in{i}.ppm"))
        _write(Path(names[-1]), img)
    p = subprocess.run(
        [str(build / "frames-test"), "--out", str(tmp / "o"), *args, *names],
        env=_env(build, **env),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    outs = []
    for i in range(len(imgs) - 1):
        a, maxval = _read(tmp / f"o-{i}-1.ppm")
        assert maxval == 1023
        outs.append(a)
    return outs


def _q(img: Any) -> Any:
    return np.clip(np.round(img * 1023), 0, 1023).astype(np.int64)


def test_square_lands_half_way(build: Path, tmp_path: Path) -> None:
    drawn = _scene(7)
    outs = _frames_test(build, tmp_path, drawn[0::2])
    for i, out in enumerate(outs):
        truth, a, c = _q(drawn[2 * i + 1]), _q(drawn[2 * i]), _q(drawn[2 * i + 2])
        err = np.abs(out - truth).mean()
        blend = np.abs((a + c) // 2 - truth).mean()
        # where the square is: the in-between frame is far closer to the truth than the blend
        rows = slice(SQUARE[0], SQUARE[1])
        sq_err = np.abs(out[rows] - truth[rows]).mean()
        sq_blend = np.abs((a[rows] + c[rows]) // 2 - truth[rows]).mean()
        assert err < blend / 3, (i, err, blend)
        assert sq_err < sq_blend / 3, (i, sq_err, sq_blend)
        # pixels that are the same in A and C (with their neighbours) keep their bits
        same = np.all(a == c, axis=2)
        cross = same.copy()
        cross[1:] &= same[:-1]
        cross[:-1] &= same[1:]
        cross[:, 1:] &= same[:, :-1]
        cross[:, :-1] &= same[:, 1:]
        assert cross.mean() > 0.8
        # away from the square all of them; next to it the rule gives way where the vector's
        # samples say something passes there
        away = cross.copy()
        away[SQUARE[0] - 16 : SQUARE[1] + 16] = False
        assert np.all(out[away] == a[away])
        assert np.mean(np.all(out[cross] == a[cross], axis=1)) > 0.98


def test_cut_gives_the_frame_before(build: Path, tmp_path: Path) -> None:
    drawn = _scene(5)
    other = [np.flipud(f)[:, ::-1] * 0.5 + 0.1 for f in drawn]
    # frames 0, 2 of the scene, then a different picture: the pair across it is a cut
    seq = [drawn[0], drawn[2], other[4]]
    outs = _frames_test(build, tmp_path, seq)
    assert np.all(outs[1] == _q(seq[1]))
    # the test knob: every pair is a cut
    forced_dir = tmp_path / "forced"
    forced_dir.mkdir()
    forced = _frames_test(build, forced_dir, [drawn[0], drawn[2]], BUTTEREYE_FRAMES_CUT="force")
    assert np.all(forced[0] == _q(drawn[0]))


def test_video_rectangle_and_source_scale(build: Path, tmp_path: Path) -> None:
    # the scene twice as large, in a letterbox: 40 black rows above and below
    drawn = _scene(5)
    big = []
    for f in drawn:
        up = np.repeat(np.repeat(f, 2, axis=0), 2, axis=1)
        frame = np.zeros((up.shape[0] + 80, up.shape[1], 3))
        frame[40:-40] = up
        big.append(frame)
    p = subprocess.run(
        [str(build / "frames-test"), "--rect", f"0,40,{2 * W},{2 * H}", "--src-h", str(H)]
        + ["--out", str(tmp_path / "o")]
        + [str(_save(tmp_path / f"in{i}.ppm", big[i])) for i in (0, 2, 4)],
        env=_env(build),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    assert f"engine {W}x{H} over 0,40 {2 * W}x{2 * H}" in p.stderr
    for i in range(2):
        out, _ = _read(tmp_path / f"o-{i}-1.ppm")
        truth, a, c = _q(big[2 * i + 1]), _q(big[2 * i]), _q(big[2 * i + 2])
        assert np.all(out[:40] == 0) and np.all(out[-40:] == 0)
        err = np.abs(out - truth).mean()
        blend = np.abs((a + c) // 2 - truth).mean()
        assert err < blend / 3, (i, err, blend)


def _save(path: Path, img: Any) -> Path:
    _write(path, img)
    return path


def _pq(rgb709: Any) -> Any:
    """SDR BT.709 (2.4 gamma) as an HDR10 swapchain holds it: BT.2020 primaries, SDR white
    at 203 cd/m2, PQ."""
    lin = np.clip(rgb709, 0, 1) ** 2.4
    m = np.array([[0.6274, 0.3293, 0.0433], [0.0691, 0.9195, 0.0114], [0.0164, 0.0880, 0.8956]])
    y = np.clip(lin @ m.T * 203 / 10000, 0, 1)
    m1, m2, c1, c2, c3 = 0.1593017578125, 78.84375, 0.8359375, 18.8515625, 18.6875
    return ((c1 + c2 * y**m1) / (1 + c3 * y**m1)) ** m2


def test_pq_path_finds_the_same_motion(build: Path, tmp_path: Path) -> None:
    drawn = [_pq(f) for f in _scene(5)]
    outs = _frames_test(build, tmp_path, drawn[0::2], "--pq")
    for i, out in enumerate(outs):
        truth, a, c = _q(drawn[2 * i + 1]), _q(drawn[2 * i]), _q(drawn[2 * i + 2])
        err = np.abs(out - truth).mean()
        blend = np.abs((a + c) // 2 - truth).mean()
        assert err < blend / 3, (i, err, blend)


def _k3_env(build: Path, csv: Path, **extra: str) -> dict[str, str]:
    """k = 3, with the engine made inside the first present that asks for it, so a short run
    has in-between frames from its second app present on (the worker thread has a test of its
    own)."""
    settings = {
        "VK_LOADER_LAYERS_ENABLE": LAYER,
        "BUTTEREYE_FRAMES_KMAX": "3",
        "BUTTEREYE_FRAMES": "3",
        "BUTTEREYE_FRAMES_LOG": str(csv),
        "BUTTEREYE_FRAMES_SHM": str(csv.with_suffix(".shm")),
        "BUTTEREYE_FRAMES_MC_THREAD": "0",
    }
    settings.update(extra)
    return _env(build, **settings)


def _control_block(
    path: Path, mode: int = 0, rect: tuple[int, int, int, int] = (0, 0, 0, 0)
) -> None:
    """The 256-byte block (layout in mvgpu/layer/shm.cpp); mode 3 is mc."""
    head = struct.pack("<IIIIiIIiiiii", 0x31464542, 1, 256, 0, 0, mode, 0, *rect, 0)
    path.write_bytes(head + bytes(256 - len(head)))


def _lines(stderr: str) -> list[str]:
    return [ln[len(PREFIX) :] for ln in stderr.splitlines() if ln.startswith(PREFIX)]


def _kinds(csv: Path) -> list[str]:
    return [r.split(",")[4] for r in csv.read_text().splitlines() if r.startswith("P,")]


def test_layer_sends_mc_frames(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"), mode=3, rect=(8, 8, 48, 32))
    p = subprocess.run(
        [str(build / "frames-present-test"), "30", "--wayland-ext", "--colour"],
        env=_k3_env(build, csv),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    lines = _lines(p.stderr)
    assert any("mc ready" in ln and "over the rectangle 8,8 48x32" in ln for ln in lines), lines
    kinds = _kinds(csv)
    made = 0
    for n, kind in enumerate(kinds):
        if kind == "a" and kinds[n - 1] == "m":
            assert kinds[n - 2] == "m"
            made += 1
    assert made >= 20
    summary = next(ln for ln in lines if ln.startswith("swapchain 1: mc on:"))
    assert f"; {made} app presents made in-between frames" in summary
    rows = [r.split(",") for r in csv.read_text().splitlines() if r.startswith("M,")]
    if any("GPU timestamps on" in ln for ln in lines if "mc ready" in ln):
        assert sum(1 for r in rows if r[4] == "1") >= 20


@pytest.mark.parametrize("split", ["1", "0"])
def test_validation_of_mc_is_clean(build: Path, tmp_path: Path, split: str) -> None:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    csv = tmp_path / "frames.csv"
    env = _k3_env(
        build,
        csv,
        BUTTEREYE_FRAMES_SPLIT=split,
        VK_ADD_LAYER_PATH=f"{build}:{vvl}",
        VK_INSTANCE_LAYERS=f"{LAYER}:VK_LAYER_KHRONOS_validation",
        VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
        VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        BUTTEREYE_FRAMES_QUIET="1",
        BUTTEREYE_FRAMES_MODE="mc",
        BUTTEREYE_FRAMES_DUMP=str(tmp_path / "mc.dump"),
        BUTTEREYE_FRAMES_DUMP_AT="5",
    )
    del env["VK_LOADER_LAYERS_ENABLE"]
    _control_block(csv.with_suffix(".shm"))
    p = subprocess.run(
        [
            str(build / "frames-present-test"),
            "40",
            "--wayland-ext",
            "--colour",
            "--pause-at",
            "20:200",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    assert "presented 40 frames" in p.stdout
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []
    assert sum(1 for k in _kinds(csv) if k == "m") >= 40
    assert (tmp_path / "mc.dump").stat().st_size > 64

    # frames-test (the same mc code, on its own device) under validation too
    drawn = _scene(5)
    names = [str(_save(tmp_path / f"v{i}.ppm", drawn[i])) for i in (0, 2, 4)]
    q = subprocess.run(
        [str(build / "frames-test"), *names],
        env=_env(
            build,
            VK_ADD_LAYER_PATH=vvl,
            VK_INSTANCE_LAYERS="VK_LAYER_KHRONOS_validation",
            VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
            VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        ),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert q.returncode == 0, q.stderr[-2000:]
    assert [ln for ln in (q.stdout + q.stderr).splitlines() if "Validation" in ln] == []


def _shm_u64(path: Path, offset: int) -> int:
    return int(struct.unpack_from("<Q", path.read_bytes(), offset)[0])


def _made_rows(csv: Path) -> list[list[str]]:
    """The M rows (GPU times) of app presents that made in-between frames."""
    rows = [r.split(",") for r in csv.read_text().splitlines() if r.startswith("M,")]
    return [r for r in rows if r[4] == "1"]


def _run_mc(build: Path, csv: Path, frames: str, *args: str, **extra: str) -> list[str]:
    p = subprocess.run(
        [str(build / "frames-present-test"), frames, "--wayland-ext", "--colour", *args],
        env=_k3_env(build, csv, BUTTEREYE_FRAMES_MODE="mc", **extra),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    return _lines(p.stderr)


def test_split_submits_give_the_same_bytes(build: Path, tmp_path: Path) -> None:
    dumps = []
    for split in ("0", "1"):
        csv = tmp_path / f"split{split}.csv"
        _control_block(csv.with_suffix(".shm"))
        dump = tmp_path / f"split{split}.dump"
        lines = _run_mc(
            build,
            csv,
            "12",
            BUTTEREYE_FRAMES_SPLIT=split,
            BUTTEREYE_FRAMES_DUMP=str(dump),
            BUTTEREYE_FRAMES_DUMP_AT="6",
        )
        how = "one submit" if split == "0" else "split submits"
        assert any(how in ln for ln in lines if "mc ready" in ln)
        rows = _made_rows(csv)
        if rows:
            # column 16: 1 when the pair and each frame were submits of their own
            assert {r[16] for r in rows} == {split}
        dumps.append(dump.read_bytes())
    assert len(dumps[0]) > 64
    assert dumps[0] == dumps[1]


def test_engine_made_on_a_worker_thread(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"))
    # the engine takes long to make on lavapipe; the pause gives it time while presents go on
    lines = _run_mc(build, csv, "40", "--pause-at", "6:3000", BUTTEREYE_FRAMES_MC_THREAD="1")
    ready = next(ln for ln in lines if "mc ready" in ln)
    assert "(worker thread)" in ready
    made_ms = float(ready.split("mc ready in ")[1].split(" ms")[0])
    kinds = _kinds(csv)
    # blends (g) while the engine is made, then in-between frames (m)
    assert "g" in kinds[: kinds.index("m")]
    assert kinds.count("m") >= 40
    # no present waited for the engine: the layer's own time per present stays far under it
    own = [int(r.split(",")[7]) for r in csv.read_text().splitlines() if r.startswith("P,")]
    assert max(own) / 1e6 < made_ms / 2, (max(own), made_ms)


@pytest.mark.parametrize("force", [False, True])
def test_cut_counter_in_the_control_block(build: Path, tmp_path: Path, force: bool) -> None:
    csv = tmp_path / "frames.csv"
    shm = csv.with_suffix(".shm")
    _control_block(shm)
    extra = {"BUTTEREYE_FRAMES_CUT": "force"} if force else {}
    lines = _run_mc(build, csv, "40", **extra)
    summary = next(ln for ln in lines if ln.startswith("swapchain 1: mc on:"))
    made = int(summary.split("; ")[1].split(" app presents made")[0])
    cuts = int(summary.split(" cuts in ")[0].rsplit(" ", 1)[1])
    tested = int(summary.split(" cuts in ")[1].split(" tested")[0])
    assert made >= 30
    # every pair's result is read once its fence is done; the last few only when the swapchain goes
    assert tested == made
    assert _shm_u64(shm, 144) == made
    assert _shm_u64(shm, 152) <= cuts
    rows = _made_rows(csv)
    assert sum(1 for r in rows if r[17] == "1") == cuts
    if force:
        assert cuts == tested
    else:
        # --colour runs a 16-frame ramp; its wrap from v = 1 back to 0 is the only cut
        assert 1 <= cuts <= 3
        assert cuts < tested
