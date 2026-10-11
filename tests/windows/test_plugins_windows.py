# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Windows builds of ButterEye's VapourSynth plugins (docs/PLATFORMS.md,
phase 3), loaded into VapourSynth for Windows and made to interpolate.

Windows only, and only when ``BUTTEREYE_TEST_PLUGINS`` names the folder with the
built DLLs (``packaging/windows/build-plugins.sh``; ``tools/wine-tests.sh`` sets
it). Needs the ``vapoursynth`` and ``numpy`` wheels. The RIFE test also needs
``BUTTEREYE_TEST_MODELS`` (the rife-ncnn-models folder) and a Vulkan GPU, which
Wine passes through from the host. librife.dll and libmvgpu.dll import Windows'
Vulkan loader (vulkan-1.dll, which a GPU driver installs): on a PC without one
(GitHub's Windows runners) their tests skip and MVTools is tested alone.

The check is the one tests/integration/test_mvgpu_real.py makes on Linux: a
textured square moving 16 px a frame must sit 8 px further on in the in-between
frame - much closer to that than to a blend of its two neighbours.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

PLUGINS = os.environ.get("BUTTEREYE_TEST_PLUGINS", "")
MODELS = os.environ.get("BUTTEREYE_TEST_MODELS", "")

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows only"),
    pytest.mark.skipif(not PLUGINS, reason="BUTTEREYE_TEST_PLUGINS not set"),
]

vs = pytest.importorskip("vapoursynth") if sys.platform == "win32" and PLUGINS else None
np = pytest.importorskip("numpy") if sys.platform == "win32" and PLUGINS else None

W, H, STEP, SIDE = 640, 368, 16, 96
BG = 32
#: (namespace, file): the names ButterEye loads (osapi.plugin_file of the Linux names)
FILES = {"mv": "mvtools.dll", "rife": "librife.dll", "mvgpu": "libmvgpu.dll"}
GPU_PLUGINS = ("rife", "mvgpu")


def _no_vulkan_loader() -> str | None:
    """Why the GPU plugins can't load here, or None: they import vulkan-1.dll."""
    import ctypes

    try:
        ctypes.WinDLL("vulkan-1")  # type: ignore[attr-defined,unused-ignore]
    except OSError as exc:
        return f"no Vulkan loader (vulkan-1.dll) on this PC: {exc}"
    return None


def _core(*namespaces: str) -> object:
    """VapourSynth's core with ``namespaces`` loaded (default: all three); a GPU
    plugin skips the test when this PC has no Vulkan loader."""
    core = vs.core  # type: ignore[union-attr]
    for ns in namespaces or tuple(FILES):
        if ns in GPU_PLUGINS and (why := _no_vulkan_loader()):
            pytest.skip(why)
        if not hasattr(core, ns):
            core.std.LoadPlugin(str(Path(PLUGINS) / FILES[ns]))
    return core


def _tex() -> object:
    rng = np.random.default_rng(7)  # type: ignore[union-attr]
    return (rng.random((SIDE, SIDE)) * 160 + 48).astype(np.uint8)  # type: ignore[union-attr]


def _frame_y(k: int, shift: float = 0.0) -> object:
    y = np.full((H, W), BG, np.uint8)  # type: ignore[union-attr]
    x0 = int(round(100 + STEP * k + shift))
    y[136 : 136 + SIDE, x0 : x0 + SIDE] = _tex()
    return y


def _source(core: object) -> object:
    base = core.std.BlankClip(  # type: ignore[attr-defined]
        format=vs.YUV420P8, width=W, height=H, fpsnum=24, fpsden=1, length=6,  # type: ignore[union-attr]
        color=[BG, 128, 128],
    )  # fmt: skip

    def paint(n: int, f: object) -> object:
        out = f.copy()  # type: ignore[attr-defined]
        np.asarray(out[0])[:] = _frame_y(n)  # type: ignore[union-attr]
        return out

    return core.std.ModifyFrame(base, base, paint)  # type: ignore[attr-defined]


def _errors(mid: object) -> tuple[float, float]:
    """(error against the square moved half a step, error of a plain blend)."""
    want = _frame_y(1, STEP / 2).astype(np.float64)  # type: ignore[attr-defined]
    blend = (_frame_y(1).astype(np.float64) + _frame_y(2)) / 2  # type: ignore[attr-defined]
    got = np.asarray(mid).astype(np.float64)  # type: ignore[union-attr]
    rows = slice(136, 136 + SIDE)
    err = float(np.mean(np.abs(got[rows] - want[rows])))  # type: ignore[union-attr]
    base = float(np.mean(np.abs(blend[rows] - want[rows])))  # type: ignore[union-attr]
    return err, base


@pytest.mark.parametrize("ns", list(FILES))
def test_every_plugin_loads_and_reports_itself(ns: str) -> None:
    core = _core(ns)
    names = {p.namespace: p.name for p in core.plugins()}  # type: ignore[attr-defined]
    assert ns in names, names


def test_mvtools_moves_the_square() -> None:
    core = _core("mv")
    src = _source(core)
    sup = core.mv.Super(src, pel=2)  # type: ignore[attr-defined]
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=8)  # type: ignore[attr-defined]
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=8)  # type: ignore[attr-defined]
    out = core.mv.FlowFPS(src, sup, bv, fv, num=48, den=1)  # type: ignore[attr-defined]
    assert out.num_frames == 12
    err, blend = _errors(out.get_frame(3)[0])
    assert err < 0.5 * blend, (err, blend)


def test_mvgpu_moves_the_square() -> None:
    core = _core("mvgpu")
    src = _source(core)
    try:
        out = core.mvgpu.Interpolate(src, fps_num=48, fps_den=1)  # type: ignore[attr-defined]
    except vs.Error as exc:  # type: ignore[union-attr]
        if "Vulkan" in str(exc):
            pytest.skip(f"no Vulkan device here: {exc}")
        raise
    assert out.num_frames == 12
    assert np.array_equal(np.asarray(out.get_frame(2)[0]), _frame_y(1))  # type: ignore[union-attr]
    err, blend = _errors(out.get_frame(3)[0])
    assert err < 0.15 * blend, (err, blend)


@pytest.mark.skipif(not MODELS, reason="BUTTEREYE_TEST_MODELS not set")
def test_rife_moves_the_square() -> None:
    core = _core("rife")
    src = _source(core)
    rgb = core.resize.Bicubic(src, format=vs.RGBS, matrix_in_s="709")  # type: ignore[attr-defined,union-attr]
    model = str(Path(MODELS) / "rife-v4.22_lite_ensembleFalse")
    try:
        out = core.rife.RIFE(rgb, model_path=model, fps_num=48, fps_den=1, gpu_thread=2)  # type: ignore[attr-defined]
        frame = out.get_frame(3)
    except vs.Error as exc:  # type: ignore[union-attr]
        if "GPU" in str(exc) or "Vulkan" in str(exc):
            pytest.skip(f"no Vulkan GPU here: {exc}")
        raise
    yuv = core.resize.Bicubic(out, format=vs.YUV420P8, matrix_s="709")  # type: ignore[attr-defined,union-attr]
    assert out.num_frames == 12
    del frame
    err, blend = _errors(yuv.get_frame(3)[0])
    assert err < 0.5 * blend, (err, blend)


# ---------------------------------------------------------------- through ButterEye's own script
@pytest.mark.parametrize("backend", ["mvtools", "mvgpu", "rife"])
def test_buttereyes_speed_test_script_runs_each_engine(backend: str) -> None:
    """``bench.vpy`` under Windows ``vspipe.exe``, as the speed test runs it: the
    script itself maps the plugin names to the .dll builds. ``vspipe.exe`` is
    pip's (``Scripts``) or, run by the installer's Python, the bundled one in the
    ``vapoursynth`` package folder, with the PATH ButterEye gives its children."""
    import subprocess

    from buttereye.core import osapi
    from buttereye.core.bench import measure

    if backend == "rife" and not MODELS:
        pytest.skip("BUTTEREYE_TEST_MODELS not set")
    if backend != "mvtools" and (why := _no_vulkan_loader()):
        pytest.skip(why)
    vs_dir = Path(vs.__file__).parent  # type: ignore[union-attr]
    vspipe = next(
        (p for p in (Path(sys.prefix) / "Scripts" / "vspipe.exe", vs_dir / "vspipe.exe")
         if p.is_file()),
        None,
    )  # fmt: skip
    if vspipe is None:
        pytest.skip("vspipe.exe not installed (pip install vapoursynth)")
    env = dict(os.environ)
    osapi.point_at_runtime(env, os.path.dirname(sys.executable), str(vs_dir))
    vpy = Path(measure.__file__).with_name("bench.vpy")
    ud = {
        "backend": backend, "plugin_dir": PLUGINS, "gpu_thread": 2, "gpu_id": None,
        "uhd": False, "matrix": "709", "src_num": 24, "src_den": 1, "out_num": 48,
        "out_den": 1, "width": 640, "height": 360, "frames": 48,
        "model_path": str(Path(MODELS) / "rife-v4.22_lite_ensembleFalse") if MODELS else "",
    }  # fmt: skip
    argv = measure.vspipe_argv(str(vspipe), vpy, ud, 48)
    res = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=300, check=False)
    if res.returncode != 0 and backend != "mvtools" and "Vulkan" in res.stderr:
        pytest.skip(f"no Vulkan device here: {res.stderr[-200:]}")
    assert res.returncode == 0, res.stderr[-1500:]
    run = measure.parse_vspipe(res.stderr)
    assert run.frames == 48 and run.fps > 0, run
