# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Generated script contract (SCOPE §4.4): static template, data only via user_data."""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from buttereye.core.mpvctl.ipc import VF_ADD_PREFIX, valid_vf_add
from buttereye.core.scriptgen import generator as g
from buttereye.core.types import BackendId

NASTY = [
    "quote \" and ' single",
    "new\nline",
    'triple """ quotes',
    "percent %10% sign",
    "colon: and, comma = equals",
    "backslash \\ and ünïcødé ✓",
    "%3%abc",
]


def _params(**kw: object) -> g.FilterParams:
    base: dict[str, object] = dict(
        gen=7,
        backend=BackendId.RIFE_NCNN,
        src_fps=Fraction(24000, 1001),
        target_fps=Fraction(120000, 1001),
        buffered_frames=4,
        concurrent_frames=4,
        matrix="709",
        range="limited",
        model_path=Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse"),
        gpu_thread=4,
        sc_threshold=0.12,
        title="film.mkv",
    )
    base.update(kw)
    return g.FilterParams(**base)  # type: ignore[arg-type]


def _unquote_vf(arg: str) -> dict[str, str]:
    """Parse ``@buttereye:vapoursynth=k=%n%v:...`` the way mpv's option parser does."""
    assert arg.startswith(VF_ADD_PREFIX)
    data = arg[len(VF_ADD_PREFIX) :].encode()
    out: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        eq = data.index(b"=", pos)
        key = data[pos:eq].decode()
        pos = eq + 1
        if data[pos : pos + 1] == b"%":
            end = data.index(b"%", pos + 1)
            n = int(data[pos + 1 : end])
            out[key] = data[end + 1 : end + 1 + n].decode()
            pos = end + 1 + n
        else:
            end = data.find(b":", pos)
            end = len(data) if end < 0 else end
            out[key] = data[pos:end].decode()
            pos = end
        if pos < len(data):
            assert data[pos : pos + 1] == b":"
            pos += 1
    return out


def test_spdx_header_with_output_permission() -> None:
    first = g.template_text().splitlines()[0]
    assert first == (
        "# SPDX-License-Identifier: AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output"
    )


def test_only_the_data_block_differs() -> None:
    a = g.render_script(g.ScriptConstants(Path("/usr/lib64/buttereye/vapoursynth")))
    b = g.render_script(g.ScriptConstants(Path("/opt/x'y\"z/\n")))
    a_lines, b_lines = a.splitlines(), b.splitlines()
    begin = a_lines.index(g.BLOCK_BEGIN)
    end = a_lines.index(g.BLOCK_END)
    assert a_lines[:begin] == b_lines[:begin]
    assert a_lines[end:] == b_lines[b_lines.index(g.BLOCK_END) :]
    # constants are repr() literals: the odd path stays one valid string literal
    compile(b, "gen.vpy", "exec")
    assert "PLUGIN_DIR = '/opt/x\\'y\"z/\\n'" in b


@pytest.mark.parametrize("title", NASTY)
def test_titles_never_reach_the_script(title: str) -> None:
    script = g.render_script()
    assert script == g.render_script()  # deterministic
    p = _params(title=title)
    assert title not in script
    ud = g.user_data(p)
    assert ud.isascii()
    assert json.loads(ud)["title"] == title


@pytest.mark.parametrize("title", NASTY)
def test_vf_argument_quoting_round_trips(title: str) -> None:
    script = Path("/run/user/1000/buttereye/mpv-ab:c,d=e%f.vpy")
    arg = g.vf_argument(script, _params(title=title))
    assert valid_vf_add(arg)
    parsed = _unquote_vf(arg)
    assert parsed["file"] == os.fspath(script)
    assert parsed["buffered-frames"] == "4" and parsed["concurrent-frames"] == "4"
    data = json.loads(parsed["user-data"])
    assert data["title"] == title and data["gen"] == 7 and data["v"] == g.USER_DATA_VERSION


def test_user_data_fields() -> None:
    rife = json.loads(g.user_data(_params()))
    assert rife["backend"] == "rife-ncnn"
    assert rife["src_fps"] == [24000, 1001] and rife["target_fps"] == [120000, 1001]
    assert rife["gpu_thread"] == 4 and rife["matrix"] == "709" and rife["uhd"] is False
    mv = json.loads(g.user_data(_params(backend=BackendId.MVTOOLS, model_path=None)))
    assert "model_path" not in mv and "gpu_thread" not in mv


def test_default_frames_per_backend() -> None:
    assert g.default_frames(BackendId.RIFE_NCNN) == (4, 8)  # mpv concurrent-frames 8 (§4.4)
    assert g.default_frames(BackendId.MVTOOLS, cpu_count=24) == (4, 16)
    assert g.default_frames(BackendId.MVTOOLS, cpu_count=12) == (4, 12)
    assert g.default_frames(BackendId.MVTOOLS, cpu_count=2) == (4, 2)


def test_template_has_no_fixed_matrix() -> None:
    text = g.template_text()
    assert "matrix_in_s='" not in text and 'matrix_in_s="' not in text
    assert "matrix_s='" not in text and 'matrix_s="' not in text
    assert "matrix_in_s=matrix" in text and "matrix_s=matrix" in text
    # guarded, absolute-path plugin loading from the private directory
    assert "if not hasattr(core, namespace):" in text
    assert "core.std.LoadPlugin(os.path.join(PLUGIN_DIR, filename))" in text


def test_write_atomic_mode_and_no_leftovers(tmp_path: Path) -> None:
    target = tmp_path / "mpv-x.vpy"
    g.write_script(target)
    assert (target.stat().st_mode & 0o777) == 0o600
    before = target.stat().st_mtime_ns
    g.write_script(target)  # identical: not rewritten
    assert target.stat().st_mtime_ns == before
    g.write_atomic(target, "x = 1\n")
    assert target.read_text() == "x = 1\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["mpv-x.vpy"]


def test_entry_gen_parsing() -> None:
    vf = [
        {"name": "format", "label": "other", "params": {}},
        {"name": "vapoursynth", "label": "buttereye", "enabled": True,
         "params": {"file": "/x.vpy", "user-data": json.dumps({"gen": 12})}},
    ]  # fmt: skip
    entry = g.filter_entry(vf)
    assert entry is not None and g.entry_gen(entry) == 12
    assert g.filter_entry([]) is None and g.entry_gen(None) is None
    assert g.entry_gen({"params": {"user-data": "not json"}}) is None


def test_user_data_size_only_when_smaller() -> None:
    assert "size" not in json.loads(g.user_data(_params()))
    small = json.loads(g.user_data(_params(size=(1920, 1080))))
    assert small["size"] == [1920, 1080]


def test_template_shrinks_to_the_asked_size() -> None:
    vs = pytest.importorskip("vapoursynth")
    text = g.render_script()
    ns: dict[str, object] = {}
    exec(compile(text.replace("build().set_output()", ""), "gen.vpy", "exec"), ns)
    shrink = ns["_shrink"]
    clip = vs.core.std.BlankClip(format=vs.YUV420P10, width=3840, height=2160, length=2)
    out = shrink(clip, {"size": [1920, 1080]})  # type: ignore[operator]
    assert (out.width, out.height, out.format.id) == (1920, 1080, vs.YUV420P10.value)
    assert shrink(clip, {}) is clip  # type: ignore[operator]
    assert shrink(clip, {"size": [3840, 2160]}) is clip  # type: ignore[operator]


def test_user_data_dither_and_mv_pel_only_when_asked() -> None:
    plain = json.loads(g.user_data(_params()))
    assert "dither" not in plain and "mv_pel" not in plain
    assert json.loads(g.user_data(_params(dither="error_diffusion")))["dither"] == (
        "error_diffusion"
    )
    mv = json.loads(g.user_data(_params(backend=BackendId.MVTOOLS, mv_pel=2)))
    assert mv["mv_pel"] == 2 and "dither" not in mv


def _template_ns() -> dict[str, object]:
    text = g.render_script()
    ns: dict[str, object] = {}
    exec(compile(text.replace("build().set_output()", ""), "gen.vpy", "exec"), ns)
    return ns


def test_template_dither_by_output_depth() -> None:
    vs = pytest.importorskip("vapoursynth")
    dither = _template_ns()["_dither"]
    assert dither(vs.core.get_video_format(vs.YUV420P10), {}) == "none"  # type: ignore[operator]
    p8 = vs.core.get_video_format(vs.YUV420P8)
    assert dither(p8, {}) == "ordered"  # type: ignore[operator]
    assert dither(p8, {"dither": "error_diffusion"}) == "error_diffusion"  # type: ignore[operator]
    assert dither(p8, {"dither": "bogus"}) == "ordered"  # type: ignore[operator]


def test_template_passes_source_frames_through_at_integer_multipliers() -> None:
    vs = pytest.importorskip("vapoursynth")
    passthrough = _template_ns()["_passthrough"]
    core = vs.core
    src = core.std.BlankClip(format=vs.YUV420P8, width=64, height=32, length=4,
                             fpsnum=24, fpsden=1, color=[200, 128, 128])  # fmt: skip
    interp = core.std.BlankClip(src, length=8, fpsnum=48, color=[16, 128, 128])
    out = passthrough(src, interp, Fraction(48))  # type: ignore[operator]
    assert out.num_frames == 8 and (out.fps_num, out.fps_den) == (48, 1)

    def luma(clip: object, n: int) -> int:
        return int(clip.get_frame(n)[0][0, 0])  # type: ignore[attr-defined]

    assert [luma(out, n) for n in range(8)] == [200, 16] * 4
    # not an integer multiplier, another format or another length: unchanged
    assert passthrough(src, interp, Fraction(60)) is interp  # type: ignore[operator]
    ten = core.std.BlankClip(interp, format=vs.YUV420P10)
    assert passthrough(src, ten, Fraction(48)) is ten  # type: ignore[operator]
    longer = core.std.BlankClip(interp, length=9)  # more than the source covers
    assert passthrough(src, longer, Fraction(48)) is longer  # type: ignore[operator]


# ---- the template itself, through vspipe (blank synthetic source) ----


def _vspipe_info(script: Path, user_data: dict[str, object]) -> str:
    res = subprocess.run(
        ["vspipe", "-a", "user_data=" + json.dumps(user_data), "--info", os.fspath(script), "-"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    return res.stdout


def _blank(**kw: object) -> dict[str, object]:
    src = {"kind": "blank", "width": 640, "height": 360, "fps": [24, 1], "length": 48}
    data: dict[str, object] = {"v": 1, "gen": 1, "target_fps": [60, 1], "source": src}
    data.update(kw)
    return data


@pytest.mark.integration
@pytest.mark.devbox
def test_template_mvtools_vspipe(tmp_path: Path) -> None:
    if shutil.which("vspipe") is None:
        pytest.skip("vspipe missing")
    script = tmp_path / "gen.vpy"
    g.write_script(script)
    info = _vspipe_info(script, _blank(backend="mvtools"))
    assert "FPS: 60/1" in info and "Frames: 120" in info and "YUV420P8" in info


@pytest.mark.integration
@pytest.mark.devbox
def test_template_rejects_bad_user_data(tmp_path: Path) -> None:
    script = tmp_path / "gen.vpy"
    g.write_script(script)
    res = subprocess.run(
        ["vspipe", "-a", 'user_data={"v": 99}', "--info", os.fspath(script), "-"],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert res.returncode != 0 and "unsupported user_data version" in res.stderr


@pytest.mark.integration
@pytest.mark.devbox
@pytest.mark.gpu
def test_template_rife_vspipe(tmp_path: Path) -> None:
    model = Path("/usr/share/buttereye/rife-ncnn-models/rife-v4.22_lite_ensembleFalse")
    if not model.is_dir():
        pytest.skip("RIFE models not installed")
    script = tmp_path / "gen.vpy"
    g.write_script(script)
    data = _blank(backend="rife-ncnn", model_path=os.fspath(model), gpu_thread=4,
                  sc_threshold=0.12, matrix=None)  # fmt: skip
    res = subprocess.run(
        ["vspipe", "-a", "user_data=" + json.dumps(data), "-p", os.fspath(script), "--"],
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert res.returncode == 0, res.stderr
    assert "Output 120 frames" in res.stderr


def test_rife_gpu_thread_is_independent_of_concurrent_frames() -> None:
    """§4.4 (owner decision 2026-10-07): mpv concurrent-frames 8, plugin gpu_thread 4.
    gpu_thread follows its own default, never concurrent-frames (8 was not soak-tested)."""
    params = g.FilterParams(
        gen=1,
        backend=BackendId.RIFE_NCNN,
        src_fps=Fraction(24000, 1001),
        target_fps=Fraction(48000, 1001),
        buffered_frames=4,
        concurrent_frames=8,
    )
    assert json.loads(g.user_data(params))["gpu_thread"] == g.RIFE_GPU_THREAD_DEFAULT == 4
    explicit = dataclasses.replace(params, gpu_thread=2)
    assert json.loads(g.user_data(explicit))["gpu_thread"] == 2
