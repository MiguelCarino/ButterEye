# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""VK_LAYER_BUTTEREYE_frames, stages S1 (pass-through and present probe), S2 (paced
duplicates) and S3 (blend), on the CPU.

Builds the layer (``meson -Dlayer=enabled``, or uses $FRAMES_LAYER_BUILD, a build
folder that already has it) and runs its headless test program on lavapipe with
every implicit layer turned off, so no GPU is used. Checks that the layer loads only
when asked for, finds the instance the way it finds mpv's (VK_KHR_wayland_surface
enabled), leaves other instances alone, adds the present extensions only with
BUTTEREYE_FRAMES_KMAX >= 2, passes every present through and logs one row per present.
With BUTTEREYE_FRAMES=k it checks the k path: k-1 copies before each app present, and
the app present going through unchanged on the first present, at k=1, after a break in
the cadence and after an event in the control block. With BUTTEREYE_FRAMES_MODE=blend it
checks the blend through the layer's dump of one present: the in-between frames are the
integer mix of the last two app images, bit for bit, the channel layout of every 8-bit
format lavapipe offers decodes the app's clear colours, and the marker has the right
colour. With $FRAMES_VVL_PATH (a folder
holding VkLayer_khronos_validation.json) the same runs go through the validation layer
below ours, with synchronization checks, and must report nothing."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the layer is Linux only"),
]

REPO = Path(__file__).resolve().parents[2]
LVP = Path("/usr/share/vulkan/icd.d/lvp_icd.x86_64.json")
LAYER = "VK_LAYER_BUTTEREYE_frames"
PREFIX = "buttereye-frames: "


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The layer folder of a build: library, manifest and frames-present-test."""
    given = os.environ.get("FRAMES_LAYER_BUILD")
    if given:
        folder = Path(given) / "layer"
    else:
        for tool in ("meson", "ninja"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} is not installed")
        out = tmp_path_factory.mktemp("frames-layer") / "build"
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
                "layer/libVkLayer_buttereye_frames.so",
                "layer/frames-present-test",
            ],
            check=True,
            capture_output=True,
        )
        folder = out / "layer"
    for name in (
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
        if not k.startswith(("VK_", "BUTTEREYE_FRAMES", "NVPRESENT"))
    }
    env.update(
        VK_DRIVER_FILES=str(LVP),
        VK_LOADER_LAYERS_DISABLE="~implicit~",
        VK_ADD_LAYER_PATH=str(build),
        LP_NUM_THREADS="2",
    )
    env.update(extra)
    return env


def _run(build: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        [str(build / "frames-present-test"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    return p


def _lines(stderr: str) -> list[str]:
    return [ln[len(PREFIX) :] for ln in stderr.splitlines() if ln.startswith(PREFIX)]


def _rows(csv: Path) -> list[list[str]]:
    """The P rows of the per-present log: P,t_ns,swapchain,image,kind,present_id,next_ns,
    own_ns,acquire_ns,result,reason,since_app_acquire_ns. kind: a = app image after its
    copies, d = a copy, p = app present passed through unchanged (reason says why)."""
    return [r.split(",") for r in csv.read_text().splitlines() if r.startswith("P,")]


def _control_block(path: Path, k: int = 0, event: int = 0, mode: int = 0) -> None:
    """The 256-byte block ButterEye shares with the layer (layout in mvgpu/layer/shm.cpp).
    mode: 0 keeps BUTTEREYE_FRAMES_MODE, 1 copies, 2 blend."""
    head = struct.pack("<IIIIiII", 0x31464542, 1, 256, 0, k, mode, event)
    path.write_bytes(head + bytes(256 - len(head)))


def _app_rows(rows: list[list[str]]) -> list[list[str]]:
    return [r for r in rows if r[4] in "ap"]


def _k3_env(build: Path, csv: Path, **extra: str) -> dict[str, str]:
    settings = {
        "VK_LOADER_LAYERS_ENABLE": LAYER,
        "BUTTEREYE_FRAMES_KMAX": "3",
        "BUTTEREYE_FRAMES": "3",
        "BUTTEREYE_FRAMES_LOG": str(csv),
        "BUTTEREYE_FRAMES_SHM": str(csv.with_suffix(".shm")),
    }
    settings.update(extra)
    return _env(build, **settings)


def test_explicit_layer_stays_off_unless_enabled(build: Path) -> None:
    p = _run(build, _env(build), "5", "--wayland-ext")
    assert "presented 5 frames" in p.stdout
    assert _lines(p.stderr) == []


def test_marked_instance_is_probed_and_presents_pass_through(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    env = _env(build, VK_LOADER_LAYERS_ENABLE=LAYER, BUTTEREYE_FRAMES_LOG=str(csv))
    p = _run(build, env, "40", "--wayland-ext")
    assert "presented 40 frames" in p.stdout
    lines = _lines(p.stderr)
    assert any(
        ln.startswith("instance 1:") and "marked (hooked)" in ln and "added none" in ln
        for ln in lines
    )
    assert any(ln.startswith("device 1 on instance 1:") and ln.endswith("hooked") for ln in lines)
    assert any(
        ln.startswith("swapchain 1 on device 1: created") and "mode FIFO" in ln for ln in lines
    )
    summary = [ln for ln in lines if ln.startswith("swapchain 1: 40 presents")]
    assert summary and "0 results not VK_SUCCESS" in summary[0]
    rows = _rows(csv)
    assert len(rows) == 40
    times = [int(r[1]) for r in rows]
    assert times == sorted(times)
    assert {r[2] for r in rows} == {"1"} and {r[9] for r in rows} == {"0"}
    # no k path without BUTTEREYE_FRAMES_KMAX: every present is the app's own
    assert {r[4] for r in rows} == {"p"}


def test_other_instances_pass_straight_through(build: Path) -> None:
    # mvgpu and ncnn enable no Wayland surface: their devices are not hooked
    env = _env(build, VK_LOADER_LAYERS_ENABLE=LAYER, BUTTEREYE_FRAMES_KMAX="3")
    p = _run(build, env, "10")
    lines = _lines(p.stderr)
    assert any(
        ln.startswith("instance 1:") and "pass-through" in ln and "added none" in ln for ln in lines
    )
    assert any(
        ln.startswith("device 1 on instance 1:") and ln.endswith("pass-through") for ln in lines
    )
    assert not any(ln.startswith("swapchain") for ln in lines)


def test_kmax_adds_present_extensions(build: Path) -> None:
    env = _env(build, VK_LOADER_LAYERS_ENABLE=LAYER, BUTTEREYE_FRAMES_KMAX="3")
    p = _run(build, env, "10", "--wayland-ext")
    lines = _lines(p.stderr)
    inst = next(ln for ln in lines if ln.startswith("instance 1:"))
    assert "VK_KHR_get_surface_capabilities2" in inst and "VK_KHR_surface_maintenance1" in inst
    added = [ln for ln in lines if ln.startswith("device 1: added")]
    # what lavapipe offers decides the list; whatever was added went through without the retry
    assert added, lines
    assert not any("retrying" in ln for ln in lines)
    assert any(ln.startswith("device 1 on instance 1:") and ln.endswith("hooked") for ln in lines)


@pytest.mark.parametrize("kmax", ["0", "3"])
def test_validation_below_the_layer_is_clean(build: Path, kmax: str) -> None:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    env = _env(
        build,
        VK_ADD_LAYER_PATH=f"{build}:{vvl}",
        VK_INSTANCE_LAYERS=f"{LAYER}:VK_LAYER_KHRONOS_validation",
        VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
        VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        BUTTEREYE_FRAMES_KMAX=kmax,
        BUTTEREYE_FRAMES_QUIET="1",
    )
    p = _run(build, env, "30", "--wayland-ext")
    assert "presented 30 frames" in p.stdout
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []


# ---------------------------------------------------------------- S2: paced duplicates


def test_k3_sends_two_copies_before_each_app_present(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    p = _run(build, _k3_env(build, csv), "30", "--wayland-ext")
    assert "presented 30 frames" in p.stdout
    lines = _lines(p.stderr)
    assert any(ln.startswith("swapchain 1: k path ready; 3 images added") for ln in lines), lines
    created = next(ln for ln in lines if ln.startswith("swapchain 1 on device 1: created"))
    assert "min images 3" in created or "min images 4" in created  # what the app asked
    rows = _rows(csv)
    app = _app_rows(rows)
    assert len(app) == 30
    # the first app present has no gap to compare with and goes through unchanged
    assert app[0][4] == "p" and app[0][10] == "5"
    assert [r[4] for r in app[1:]].count("a") >= 25
    assert {r[9] for r in rows} == {"0"}
    # each 'a' row comes right after its two copies, which share its entry time and
    # show other images of the same swapchain
    for n, r in enumerate(rows):
        if r[4] != "a":
            continue
        d1, d2 = rows[n - 2], rows[n - 1]
        assert d1[4] == d2[4] == "d"
        assert d1[1] == d2[1] == r[1]
        assert len({d1[3], d2[3], r[3]}) == 3
    copies = sum(1 for r in rows if r[4] == "d")
    assert copies == 2 * sum(1 for r in app if r[4] == "a")
    summary = next(ln for ln in lines if ln.startswith("swapchain 1: 30 app presents"))
    assert f"{copies} presents added" in summary


def test_k1_and_off_pass_every_present_through(build: Path, tmp_path: Path) -> None:
    for value in ("1", "off"):
        csv = tmp_path / f"frames-{value}.csv"
        _run(build, _k3_env(build, csv, BUTTEREYE_FRAMES=value), "12", "--wayland-ext")
        app = _app_rows(_rows(csv))
        assert len(app) == 12 and {r[4] for r in app} == {"p"}
        assert {r[10] for r in app[1:]} == {"1"}  # reason: k is 1
        assert not any(r[4] == "d" for r in _rows(csv))


def test_control_block_sets_k_and_its_events_pass_through(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    env = _k3_env(build, csv)
    # k=1 in the block wins over BUTTEREYE_FRAMES=3
    _control_block(csv.with_suffix(".shm"), k=1)
    _run(build, env, "10", "--wayland-ext")
    assert {r[4] for r in _app_rows(_rows(csv))} == {"p"}
    shm = csv.with_suffix(".shm").read_bytes()
    seq, k_active = struct.unpack_from("<II", shm, 64)
    app_presents, k_path, passed = struct.unpack_from("<QQQ", shm, 80)
    assert seq % 2 == 0 and seq > 0 and k_active == 1
    assert (app_presents, k_path, passed) == (10, 0, 10)
    # k=0 keeps the environment's 3; an event before frame 15 passes that present through
    csv.unlink()
    _control_block(csv.with_suffix(".shm"), k=0)
    _run(build, env, "30", "--wayland-ext", "--event-at", "15")
    app = _app_rows(_rows(csv))
    assert app[15][4] == "p" and app[15][10] == "3"
    assert sum(1 for r in app if r[10] == "3") == 1
    assert sum(1 for r in app if r[4] == "a") >= 24
    shm = csv.with_suffix(".shm").read_bytes()
    app_presents, k_path, passed, added = struct.unpack_from("<QQQQ", shm, 80)
    assert app_presents == 30 and k_path + passed == 30 and added == 2 * k_path


def test_a_break_in_the_cadence_passes_through(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    _run(build, _k3_env(build, csv), "40", "--wayland-ext", "--pause-at", "30:400")
    app = _app_rows(_rows(csv))
    assert len(app) == 40
    assert app[30][4] == "p" and app[30][10] == "2"


@pytest.mark.parametrize("hold", ["0", "-1"])
def test_validation_of_the_k_path_is_clean(build: Path, tmp_path: Path, hold: str) -> None:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    csv = tmp_path / "frames.csv"
    env = _k3_env(
        build,
        csv,
        VK_ADD_LAYER_PATH=f"{build}:{vvl}",
        VK_INSTANCE_LAYERS=f"{LAYER}:VK_LAYER_KHRONOS_validation",
        VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
        VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        BUTTEREYE_FRAMES_QUIET="1",
        BUTTEREYE_FRAMES_HOLD=hold,
    )
    del env["VK_LOADER_LAYERS_ENABLE"]
    _control_block(csv.with_suffix(".shm"))
    p = _run(build, env, "45", "--wayland-ext", "--pause-at", "20:200", "--event-at", "30")
    assert "presented 45 frames" in p.stdout
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []
    assert sum(1 for r in _rows(csv) if r[4] == "d") >= 60


# ---------------------------------------------------------------- S3: the blend

# the 8-bit formats lavapipe's headless surface offers: (bits a channel, red at bit 0)
LVP_FORMATS = {
    "B8G8R8A8_UNORM": (8, 0),
    "B8G8R8A8_SRGB": (8, 0),
    "R8G8B8A8_UNORM": (8, 1),
    "R8G8B8A8_SRGB": (8, 1),
}
MARKER = range(24, 24 + 96)  # the marker square, in x and y (blend.comp)


def _run_format(
    build: Path, env: dict[str, str], fmt: str, *args: str
) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        [str(build / "frames-present-test"), *args, "--format", fmt],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if p.returncode == 3:
        pytest.skip(f"the headless surface does not offer {fmt}")
    assert p.returncode == 0, p.stderr[-2000:]
    return p


def _dump(path: Path) -> tuple[dict[str, int], list[list[int]]]:
    """The layer's blend dump: a 64-byte header of little-endian u32, then H[prev],
    H[cur] and each in-between frame, 32 bits a pixel."""
    raw = path.read_bytes()
    head = struct.unpack_from("<11I", raw, 0)
    names = ("magic", "version", "w", "h", "bits", "red_low", "k", "layers", "show", "format", "cs")
    meta = dict(zip(names, head, strict=True))
    assert meta["magic"] == 0x44464542 and meta["version"] == 1
    n = meta["w"] * meta["h"]
    planes = [
        list(struct.unpack_from(f"<{n}I", raw, 64 + 4 * n * i)) for i in range(2 + meta["layers"])
    ]
    return meta, planes


def _channels(px: int, bits: int) -> list[int]:
    mask = (1 << bits) - 1
    return [(px >> (i * bits)) & mask for i in range(3)] + [px >> (3 * bits)]


def _rgb(px: int, bits: int, red_low: int) -> tuple[int, int, int]:
    c = _channels(px, bits)
    return (c[0], c[1], c[2]) if red_low else (c[2], c[1], c[0])


def _encode(x: float, srgb: bool) -> int:
    if srgb:
        x = 12.92 * x if x <= 0.0031308 else 1.055 * x ** (1 / 2.4) - 0.055
    return round(x * 255)


@pytest.mark.parametrize("fmt", sorted(LVP_FORMATS))
def test_blend_mixes_the_last_two_images_bit_for_bit(build: Path, tmp_path: Path, fmt: str) -> None:
    csv = tmp_path / "frames.csv"
    dump = tmp_path / "blend.dump"
    env = _k3_env(
        build,
        csv,
        BUTTEREYE_FRAMES_MODE="blend",
        BUTTEREYE_FRAMES_SHOW="1",
        BUTTEREYE_FRAMES_DUMP=str(dump),
        BUTTEREYE_FRAMES_DUMP_AT="5",
    )
    p = _run_format(build, env, fmt, "30", "--wayland-ext", "--colour")
    lines = _lines(p.stderr)
    assert any(ln.startswith("swapchain 1: blend ready") for ln in lines), lines
    assert any("dump of blended present 5 written" in ln for ln in lines), lines
    meta, planes = _dump(dump)
    bits, red_low = LVP_FORMATS[fmt]
    assert (meta["bits"], meta["red_low"], meta["k"], meta["layers"], meta["show"]) == (
        bits,
        red_low,
        3,
        2,
        1,
    )
    prev, cur = planes[0], planes[1]
    # whole frames cleared to (v, 0.25, 1 - v), v = (f % 16) / 15: the layout must decode
    # them, which shows where the driver keeps red and blue in this format
    assert len(set(prev)) == 1 and len(set(cur)) == 1 and prev[0] != cur[0]
    srgb = fmt.endswith("SRGB")

    def frame_of(px: int) -> int:
        r, g, b = _rgb(px, bits, red_low)
        for f in range(16):
            v = f / 15
            want = (_encode(v, srgb), _encode(0.25, srgb), _encode(1 - v, srgb))
            if all(abs(x - y) <= 1 for x, y in zip((r, g, b), want, strict=True)):
                return f
        raise AssertionError(f"{fmt}: pixel {px:#010x} decodes to {(r, g, b)}, no clear colour")

    assert frame_of(cur[0]) == (frame_of(prev[0]) + 1) % 16
    w = meta["w"]
    for m in (1, 2):
        out = planes[1 + m]
        for i, px in enumerate(out):
            if i % w in MARKER and i // w in MARKER:
                want_rgb = {1: (128, 0, 0), 2: (0, 0, 128)}[m]
                assert _rgb(px, bits, red_low) == want_rgb and px >> 24 == 255
                continue
            a, c = _channels(prev[i], bits), _channels(cur[i], bits)
            want = [(x * (3 - m) + y * m + 1) // 3 for x, y in zip(a, c, strict=True)]
            assert _channels(px, bits) == want, (m, i)
    # a k path present right after a pass-through (the first present, a break in the
    # cadence) has only one image in the history and sends copies; the others blend
    rows = _rows(csv)
    blended = copied = 0
    last_app = ""
    for n, r in enumerate(rows):
        if r[4] == "a":
            pair = [rows[n - 2][4], rows[n - 1][4]]
            assert pair == (["d", "d"] if last_app == "p" else ["g", "g"]), n
            blended += pair == ["g", "g"]
            copied += pair == ["d", "d"]
        if r[4] in "ap":
            last_app = r[4]
    assert blended >= 20 and copied >= 1
    summary = next(ln for ln in lines if ln.startswith("swapchain 1: blend on:"))
    assert f"{blended} app presents blended, {copied} sent copies" in summary
    gpu = [r.split(",") for r in csv.read_text().splitlines() if r.startswith("G,")]
    if "GPU timestamps on" in next(ln for ln in lines if "blend ready" in ln):
        assert sum(1 for g in gpu if g[4] == "1") >= 20
        assert all(int(g[8]) > 0 for g in gpu)


def test_blend_starts_again_after_a_pass_through(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    env = _k3_env(build, csv, BUTTEREYE_FRAMES_MODE="blend")
    _run(build, env, "40", "--wayland-ext", "--pause-at", "30:400")
    rows = _rows(csv)
    app = _app_rows(rows)
    assert app[30][4] == "p" and app[30][10] == "2"
    # every k path present right after a pass-through has only one image in the history
    # and sends copies; every other one sends blends (on a busy CPU lavapipe can add
    # breaks of its own, so the rule is checked present by present)
    last_app = ""
    after_break = 0
    for n, r in enumerate(rows):
        if r[4] == "a":
            pair = [rows[n - 2][4], rows[n - 1][4]]
            assert pair == (["d", "d"] if last_app == "p" else ["g", "g"]), n
            after_break += last_app == "p" and app.index(r) > 30
        if r[4] in "ap":
            last_app = r[4]
    assert after_break >= 1
    assert sum(1 for r in rows if r[4] == "g") >= 40


def test_control_block_mode_beats_the_environment(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    shm = csv.with_suffix(".shm")
    _control_block(shm, mode=2)  # blend, against BUTTEREYE_FRAMES_MODE unset (copies)
    _run(build, _k3_env(build, csv), "20", "--wayland-ext")
    assert sum(1 for r in _rows(csv) if r[4] == "g") >= 30
    blended = struct.unpack_from("<Q", shm.read_bytes(), 136)[0]
    assert blended >= 15
    csv.unlink()
    _control_block(shm, mode=1)  # copies, against BUTTEREYE_FRAMES_MODE=blend
    _run(build, _k3_env(build, csv, BUTTEREYE_FRAMES_MODE="blend"), "20", "--wayland-ext")
    kinds = [r[4] for r in _rows(csv)]
    assert "g" not in kinds and kinds.count("d") >= 30


def test_test_formats_knob_limits_what_the_app_sees(build: Path) -> None:
    env = _env(
        build,
        VK_LOADER_LAYERS_ENABLE=LAYER,
        BUTTEREYE_FRAMES_TEST_FORMATS="B8G8R8A8_SRGB,A2R10G10B10_UNORM",
    )
    p = _run(build, env, "--list-formats", "--wayland-ext")
    listed = {ln.split()[1] for ln in p.stdout.splitlines() if ln.startswith("format ")}
    assert listed == {"50"}  # B8G8R8A8_SRGB; lavapipe has no 10-bit surface format
    # an instance that is not marked keeps the whole list
    p = _run(build, env, "--list-formats")
    assert len([ln for ln in p.stdout.splitlines() if ln.startswith("format ")]) > 1


@pytest.mark.parametrize("fmt", ["B8G8R8A8_UNORM", "R8G8B8A8_SRGB"])
def test_validation_of_the_blend_is_clean(build: Path, tmp_path: Path, fmt: str) -> None:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    csv = tmp_path / "frames.csv"
    env = _k3_env(
        build,
        csv,
        VK_ADD_LAYER_PATH=f"{build}:{vvl}",
        VK_INSTANCE_LAYERS=f"{LAYER}:VK_LAYER_KHRONOS_validation",
        VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
        VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        BUTTEREYE_FRAMES_QUIET="1",
        BUTTEREYE_FRAMES_MODE="blend",
        BUTTEREYE_FRAMES_SHOW="1",
        BUTTEREYE_FRAMES_DUMP=str(tmp_path / "blend.dump"),
        BUTTEREYE_FRAMES_DUMP_AT="5",
    )
    del env["VK_LOADER_LAYERS_ENABLE"]
    _control_block(csv.with_suffix(".shm"))
    p = _run_format(
        build,
        env,
        fmt,
        "45",
        "--wayland-ext",
        "--colour",
        "--pause-at",
        "20:200",
        "--event-at",
        "30",
    )
    assert "presented 45 frames" in p.stdout
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []
    assert sum(1 for r in _rows(csv) if r[4] == "g") >= 60
    assert (tmp_path / "blend.dump").stat().st_size > 64
