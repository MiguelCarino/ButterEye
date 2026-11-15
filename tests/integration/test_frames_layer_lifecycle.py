# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""VK_LAYER_BUTTEREYE_frames over a swapchain's life, on the CPU (review fixes).

Builds mvgpu with ``-Dlayer=enabled`` (or uses $FRAMES_LAYER_BUILD) and runs
``frames-present-test`` on lavapipe with every implicit layer off, so no GPU is used:

* a new video rectangle mid-run makes a second mc engine; retiring the first one waits for the
  layer's fences and resets them, so (with $FRAMES_VVL_PATH) validation stays clean: no
  submit or present with a fence that is still signaled;
* the P rows of the added presents (blends 'g', mc frames 'm') carry no app acquire time:
  the layer acquired those images itself;
* an app that sends its own present ids is seen as such at k = 1 too (the layer must not put
  its own VkPresentId2KHR next to the app's);
* when the layer's create of a replacing swapchain fails, the retry does not name the old
  swapchain again: the failed create already retired it (validation stays clean)."""

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
APP_IDS = 9  # the reason code "app ids" (mvgpu/layer/swapchain.cpp)


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The layer folder of a build: the layer and frames-present-test."""
    given = os.environ.get("FRAMES_LAYER_BUILD")
    if given:
        folder = Path(given) / "layer"
    else:
        for tool in ("meson", "ninja"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} is not installed")
        out = tmp_path_factory.mktemp("frames-life") / "build"
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


def _vvl() -> str:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    return vvl


def _env(build: Path, csv: Path, vvl: str | None = None, **extra: str) -> dict[str, str]:
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
        VK_LOADER_LAYERS_ENABLE=LAYER,
        BUTTEREYE_FRAMES_KMAX="3",
        BUTTEREYE_FRAMES="3",
        BUTTEREYE_FRAMES_LOG=str(csv),
        BUTTEREYE_FRAMES_SHM=str(csv.with_suffix(".shm")),
        BUTTEREYE_FRAMES_MC_THREAD="0",
    )
    if vvl:
        # the validation layer below the frames layer, sync checks on
        del env["VK_LOADER_LAYERS_ENABLE"]
        env.update(
            VK_ADD_LAYER_PATH=f"{build}:{vvl}",
            VK_INSTANCE_LAYERS=f"{LAYER}:VK_LAYER_KHRONOS_validation",
            VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
            VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
            BUTTEREYE_FRAMES_QUIET="1",
        )
    env.update(extra)
    return env


def _control_block(
    path: Path, mode: int = 0, rect: tuple[int, int, int, int] = (0, 0, 0, 0)
) -> None:
    """The 256-byte block (layout in mvgpu/layer/shm.cpp); mode 3 is mc."""
    head = struct.pack("<IIIIiIIiiiii", 0x31464542, 1, 256, 0, 0, mode, 0, *rect, 0)
    path.write_bytes(head + bytes(256 - len(head)))


def _run(build: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(
        [str(build / "frames-present-test"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert p.returncode == 0, p.stderr[-3000:]
    return p


def _rows(csv: Path) -> list[list[str]]:
    return [r.split(",") for r in csv.read_text().splitlines() if r.startswith("P,")]


def _validation(p: subprocess.CompletedProcess[str]) -> list[str]:
    return [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]


@pytest.mark.parametrize("thread", ["0", "1"])
def test_engine_replaced_mid_run_keeps_fences_valid(
    build: Path, tmp_path: Path, thread: str
) -> None:
    """A rectangle change makes a second engine (inside the present, or on the worker thread);
    the first one is retired inside a present."""
    vvl = _vvl()
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"), mode=3, rect=(8, 8, 48, 32))
    # the worker thread is slow under validation: pauses give each engine time to be made
    frames, extra = (
        ("40", []) if thread == "0" else ("60", ["--pause-at", "10:3000", "--pause-at", "21:3000"])
    )
    env = _env(build, csv, vvl, BUTTEREYE_FRAMES_MC_THREAD=thread, BUTTEREYE_FRAMES_QUIET="0")
    p = _run(build, env, frames, "--wayland-ext", "--colour", "--rect-at", "20:0,0,64,48", *extra)
    assert f"presented {frames} frames" in p.stdout
    assert _validation(p) == [], _validation(p)[:5]
    ready = [ln for ln in p.stderr.splitlines() if ln.startswith(PREFIX) and "mc ready" in ln]
    # the second engine replaced the first one during the run
    assert len(ready) == 2, ready
    assert "over the rectangle 0,0 64x48" in ready[1]
    assert [r[4] for r in _rows(csv)].count("m") >= 20


def test_engine_replaced_mid_run_logs_both(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"), mode=3, rect=(8, 8, 48, 32))
    p = _run(
        build, _env(build, csv), "30", "--wayland-ext", "--colour", "--rect-at", "15:0,0,64,48"
    )
    ready = [ln for ln in p.stderr.splitlines() if ln.startswith(PREFIX) and "mc ready" in ln]
    assert len(ready) == 2, ready
    assert "over the rectangle 8,8 48x32" in ready[0]
    assert "over the rectangle 0,0 64x48" in ready[1]


@pytest.mark.parametrize("mode", ["blend", "mc"])
def test_added_presents_carry_no_app_acquire_time(build: Path, tmp_path: Path, mode: str) -> None:
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"))
    _run(build, _env(build, csv, BUTTEREYE_FRAMES_MODE=mode), "30", "--wayland-ext", "--colour")
    rows = _rows(csv)
    added = [r for r in rows if r[4] in ("g", "m", "d")]
    made = "m" if mode == "mc" else "g"
    assert sum(1 for r in added if r[4] == made) >= 20
    # column 11: the time since the app acquired the image; the layer acquired these itself
    assert [r[11] for r in added if r[11] != "0"] == []
    # the app's own presents do have it
    assert any(r[11] != "0" for r in rows if r[4] == "a")


def test_app_present_ids_seen_at_k1(build: Path, tmp_path: Path) -> None:
    """At k = 1 (frames off) a present with the app's own ids is passed as 'app ids', so the
    layer adds no present id of its own next to the app's."""
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"))
    p = _run(build, _env(build, csv, BUTTEREYE_FRAMES="off"), "12", "--wayland-ext", "--present-id")
    assert "presented 12 frames" in p.stdout
    rows = _rows(csv)
    assert len(rows) == 12
    # column 10: the reason; column 5: the layer's present id (0: none added)
    assert {r[10] for r in rows} == {str(APP_IDS)}
    assert {r[5] for r in rows} == {"0"}
    summary = next(ln for ln in p.stderr.splitlines() if "passed through:" in ln)
    assert "app ids 12" in summary and "k=1" not in summary


def test_app_without_ids_at_k1_stays_off(build: Path, tmp_path: Path) -> None:
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"))
    p = _run(build, _env(build, csv, BUTTEREYE_FRAMES="off"), "12", "--wayland-ext")
    summary = next(ln for ln in p.stderr.splitlines() if "passed through:" in ln)
    assert "k=1 12" in summary


def test_create_retry_after_failure_drops_the_retired_old_swapchain(
    build: Path, tmp_path: Path
) -> None:
    vvl = _vvl()
    csv = tmp_path / "frames.csv"
    _control_block(csv.with_suffix(".shm"))
    env = _env(build, csv, vvl, BUTTEREYE_FRAMES_TEST_CREATE_FAIL="1", BUTTEREYE_FRAMES_QUIET="0")
    p = _run(build, env, "24", "--wayland-ext", "--recreate-at", "12")
    assert "presented 24 frames" in p.stdout
    lines = [ln for ln in p.stderr.splitlines() if ln.startswith(PREFIX)]
    assert any("retrying as the app asked" in ln for ln in lines), lines
    assert any(
        "swapchain 2" in ln and "created" in ln and "replaces an old one yes" in ln for ln in lines
    )
    assert _validation(p) == [], _validation(p)[:5]
