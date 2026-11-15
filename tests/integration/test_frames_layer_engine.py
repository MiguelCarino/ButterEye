# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""mvgpu's engine outside the plugin (stage S4a/S4b of VK_LAYER_BUTTEREYE_frames), on
the CPU.

Builds mvgpu with ``-Dlayer=enabled`` (or uses $FRAMES_LAYER_BUILD) and runs on
lavapipe with every implicit layer off, so no GPU is used:

* ``engine-check``: the brightness difference read from a buffer (GDC_FROM_BUFFER)
  must give the same bytes as the plugin's push constants when the buffer holds the
  same means, and the means reduced on the GPU must match the plugin's CPU means;
* the layer, with BUTTEREYE_FRAMES_ENGINE_TEST=1, runs the engine once on the app's
  device and queue through the table it fills from the next layer, and logs means
  that match the CPU's;
* the layer still links no Vulkan loader and exports one symbol.

With $FRAMES_VVL_PATH (a folder holding VkLayer_khronos_validation.json) both runs
also go through the validation layer, with synchronization checks, and must report
nothing."""

from __future__ import annotations

import json
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
VVL = "VK_LAYER_KHRONOS_validation"


@pytest.fixture(scope="module")
def build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A build folder with engine-check and the layer folder."""
    given = os.environ.get("FRAMES_LAYER_BUILD")
    if given:
        out = Path(given)
    else:
        for tool in ("meson", "ninja"):
            if not shutil.which(tool):
                pytest.skip(f"{tool} is not installed")
        out = tmp_path_factory.mktemp("frames-engine") / "build"
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
                "engine-check",
                "layer/libVkLayer_buttereye_frames.so",
                "layer/frames-present-test",
            ],
            check=True,
            capture_output=True,
        )
    for name in (
        "engine-check",
        "layer/libVkLayer_buttereye_frames.so",
        "layer/VkLayer_buttereye_frames.json",
        "layer/frames-present-test",
    ):
        if not (out / name).exists():
            pytest.skip(f"{out / name} was not built")
    if not LVP.exists():
        pytest.skip("lavapipe (mesa-vulkan-drivers) is not installed")
    return out


def _vvl() -> str:
    vvl = os.environ.get("FRAMES_VVL_PATH")
    if not vvl or not (Path(vvl) / "VkLayer_khronos_validation.json").exists():
        pytest.skip("set FRAMES_VVL_PATH to a folder with VkLayer_khronos_validation.json")
    return vvl


def _env(**extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("VK_", "BUTTEREYE_FRAMES", "NVPRESENT", "MVGPU_"))
    }
    env.update(VK_DRIVER_FILES=str(LVP), VK_LOADER_LAYERS_DISABLE="~implicit~", LP_NUM_THREADS="2")
    env.update(extra)
    return env


def _validation(**extra: str) -> dict[str, str]:
    return dict(
        VK_KHRONOS_VALIDATION_REPORT_FLAGS="error,warn,perf",
        VK_KHRONOS_VALIDATION_VALIDATE_SYNC="true",
        **extra,
    )


def _check(build: Path, env: dict[str, str], *args: str) -> dict[str, object]:
    p = subprocess.run(
        [str(build / "engine-check"), *args], env=env, capture_output=True, text=True, timeout=300
    )
    assert p.returncode == 0, (p.stdout + p.stderr)[-2000:]
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []
    result: dict[str, object] = json.loads(p.stdout.strip().splitlines()[-1])
    return result


def test_gdc_from_a_buffer_matches_the_push_constants(build: Path) -> None:
    r = _check(build, _env())
    assert r["ok"] is True
    runs = r["runs"]
    assert isinstance(runs, list) and [x["rules"] for x in runs] == ["default", "tri+edge"]
    for run in runs:
        assert run["bytes"] > 0
        # the same means through the buffer: the same bytes as the plugin's path
        assert run["push_vs_buffer_differ"] == 0
        # the GPU's float sum against the CPU's double one: a few ulps at most
        assert run["mean_err"] <= 1e-5
        # the drawn frames alternate in brightness, so gdc is far from 0
        means = run["means"]
        assert abs(means[1] - means[0]) > 0.05
        # a mean a few ulps off may move a pixel by a level, never more
        assert run["push_vs_gpu_max"] <= 1


def test_engine_check_validation_is_clean(build: Path) -> None:
    vvl = _vvl()
    env = _env(VK_ADD_LAYER_PATH=vvl, VK_INSTANCE_LAYERS=VVL, **_validation())
    r = _check(build, env, "--size", "96x64")
    assert r["ok"] is True


def _present_test(build: Path, tmp_path: Path, env: dict[str, str]) -> list[str]:
    shm = tmp_path / "frames.shm"
    head = struct.pack("<IIIIiII", 0x31464542, 1, 256, 0, 0, 0, 0)
    shm.write_bytes(head + bytes(256 - len(head)))
    env.update(
        BUTTEREYE_FRAMES_KMAX="3",
        BUTTEREYE_FRAMES="3",
        BUTTEREYE_FRAMES_MODE="blend",
        BUTTEREYE_FRAMES_SHM=str(shm),
        BUTTEREYE_FRAMES_LOG=str(tmp_path / "frames.csv"),
        BUTTEREYE_FRAMES_ENGINE_TEST="1",
    )
    p = subprocess.run(
        [str(build / "layer" / "frames-present-test"), "12", "--wayland-ext"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert p.returncode == 0, p.stderr[-2000:]
    assert "presented 12 frames" in p.stdout
    noise = [ln for ln in (p.stdout + p.stderr).splitlines() if "Validation" in ln]
    assert noise == []
    return [ln[len(PREFIX) :] for ln in p.stderr.splitlines() if ln.startswith(PREFIX)]


def _engine_line(lines: list[str]) -> str:
    found = [ln for ln in lines if "engine test" in ln]
    assert len(found) == 1, lines
    return found[0]


def test_the_engine_runs_inside_the_layer(build: Path, tmp_path: Path) -> None:
    env = _env(VK_ADD_LAYER_PATH=str(build / "layer"), VK_LOADER_LAYERS_ENABLE=LAYER)
    line = _engine_line(_present_test(build, tmp_path, env))
    assert line.startswith("device 1: engine test 160x96 ok;"), line
    gpu = line.split("GPU means ")[1].split(",")[0].split()
    cpu = line.split(", CPU ")[1].split(";")[0].split()
    assert len(gpu) == len(cpu) == 4
    assert all(abs(float(a) - float(b)) <= 1e-5 for a, b in zip(gpu, cpu, strict=True))


def test_the_engine_inside_the_layer_validation_is_clean(build: Path, tmp_path: Path) -> None:
    vvl = _vvl()
    env = _env(
        VK_ADD_LAYER_PATH=f"{build / 'layer'}:{vvl}",
        VK_INSTANCE_LAYERS=f"{LAYER}:{VVL}",
        **_validation(),
    )
    line = _engine_line(_present_test(build, tmp_path, env))
    assert " ok;" in line


def test_the_layer_links_no_loader(build: Path) -> None:
    nm = shutil.which("nm")
    if not nm:
        pytest.skip("nm is not installed")
    lib = str(build / "layer" / "libVkLayer_buttereye_frames.so")
    undefined = subprocess.run(
        [nm, "-D", "--undefined-only", lib], capture_output=True, text=True, check=True
    ).stdout
    assert not [ln for ln in undefined.splitlines() if ln.split()[-1].startswith("vk")]
    defined = subprocess.run(
        [nm, "-D", "--defined-only", lib], capture_output=True, text=True, check=True
    ).stdout
    exported = [ln.split()[-1] for ln in defined.splitlines() if ln.split()[1] in "TDB"]
    assert exported == ["vkNegotiateLoaderLayerInterfaceVersion"]
