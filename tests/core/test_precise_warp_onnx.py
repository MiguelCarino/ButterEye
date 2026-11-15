# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""template.vpy's ONNX patches for TensorRT RIFE, on small synthetic graphs built
here (the RIFE models are not in the repo): the fp16 conversion that keeps the
final warp's sampling positions in fp32 all the way ("precise warp",
docs/spikes/m0x.md) and the input padding of the reduced-flow model
(``_scale_flow``). Needs onnx and onnxconverter-common (ButterEye's own Python
folder has both); the numerical checks also need onnxruntime."""

from __future__ import annotations

from typing import Any

import pytest

from buttereye.core.scriptgen import generator as g

vs = pytest.importorskip("vapoursynth")
np = pytest.importorskip("numpy")
onnx = pytest.importorskip("onnx")
pytest.importorskip("onnxconverter_common")
from onnx import TensorProto, helper  # noqa: E402

H, W = 32, 1024  # wide: a half-float grid near x = 1 is 0.25 px apart here


def _ns() -> dict[str, Any]:
    ns: dict[str, Any] = {}
    text = g.render_script().replace("build().set_output()", "")
    exec(compile(text, "gen.vpy", "exec"), ns)
    return ns


def _const(name: str, value: Any, dtype: Any = None) -> Any:
    arr = np.asarray(value, dtype=dtype or np.float32)
    return helper.make_node(
        "Constant", [], [name], name=name, value=onnx.numpy_helper.from_array(arr, name)
    )


def _mini_rife(seed: int = 0, zero: bool = False) -> Any:
    """RIFE's tail as the vs-mlrt v2 export builds it, one block deep: a base
    grid and a divisor every warp shares, a flow sum the next block reads too,
    a warp between blocks, and the final warp + sigmoid blend the output is."""
    rng = np.random.default_rng(seed)
    xs = np.linspace(-1.0, 1.0, W, dtype=np.float32)
    ys = np.linspace(-1.0, 1.0, H, dtype=np.float32)
    base = np.stack(np.broadcast_arrays(xs[None, :], ys[:, None]))[None].astype(np.float32)
    div = np.array([2.0 / (W - 1), 2.0 / (H - 1)], np.float32).reshape(1, 2, 1, 1)
    w0 = rng.normal(0, 0.3, (4, 7, 3, 3)).astype(np.float32) * (not zero)
    w1 = rng.normal(0, 0.1, (5, 10, 3, 3)).astype(np.float32) * (not zero)
    n = [
        _const("base", base), _const("div", div), _const("s0", 8.0),
        _const("w0", w0), _const("w1", w1),
        _const("i0", [0], np.int64), _const("i2", [2], np.int64), _const("i3", [3], np.int64),
        _const("i6", [6], np.int64), _const("ax1", [1], np.int64),
        _const("r2", [2, 2, H, W], np.int64), _const("r3", [2, 3, H, W], np.int64),
        _const("one", 1.0), _const("sp", [4, 1], np.int64), _const("sp2", [1, 1], np.int64),
        helper.make_node("Conv", ["input", "w0"], ["d0"], name="/block0/conv", pads=[1, 1, 1, 1]),
        helper.make_node("Mul", ["d0", "s0"], ["flow0"], name="/block0/Mul"),
        # warp between blocks: img0 by the first half of the flow
        helper.make_node("Slice", ["flow0", "i0", "i2", "ax1"], ["f01"], name="/Slice_f"),
        helper.make_node("Mul", ["f01", "div"], ["f01n"], name="/Mul_12"),
        helper.make_node("Add", ["base", "f01n"], ["g0"], name="/Add"),
        helper.make_node("Transpose", ["g0"], ["g0t"], name="/Transpose", perm=[0, 2, 3, 1]),
        helper.make_node("Slice", ["input", "i0", "i3", "ax1"], ["img0"], name="/Slice_0"),
        helper.make_node("GridSample", ["img0", "g0t"], ["wimg"], name="/GridSample",
                         align_corners=1, mode="bilinear", padding_mode="border"),
        helper.make_node("Concat", ["input", "wimg"], ["x1"], name="/Concat_29", axis=1),
        helper.make_node("Conv", ["x1", "w1"], ["d1"], name="/block1/conv", pads=[1, 1, 1, 1]),
        helper.make_node("Split", ["d1", "sp"], ["d1f", "logit"], name="/block1/Split", axis=1),
        helper.make_node("Add", ["flow0", "d1f"], ["flow"], name="/Add_7"),
        # the final warp: both frames, batch 2
        helper.make_node("Reshape", ["flow", "r2"], ["f2"], name="/Reshape_25"),
        helper.make_node("Mul", ["f2", "div"], ["f2n"], name="/Mul_19"),
        helper.make_node("Add", ["base", "f2n"], ["grid"], name="/Add_8"),
        helper.make_node("Transpose", ["grid"], ["gridt"], name="/Transpose_5", perm=[0, 2, 3, 1]),
        helper.make_node("Slice", ["input", "i0", "i6", "ax1"], ["imgs6"], name="/Slice_1"),
        helper.make_node("Reshape", ["imgs6", "r3"], ["imgs"], name="/Reshape_11"),
        helper.make_node("GridSample", ["imgs", "gridt"], ["warped"], name="/GridSample_4",
                         align_corners=1, mode="bilinear", padding_mode="border"),
        helper.make_node("Split", ["warped", "sp2"], ["a", "b"], name="/Split_5", axis=0),
        helper.make_node("Sigmoid", ["logit"], ["m"], name="/Sigmoid"),
        helper.make_node("Mul", ["a", "m"], ["am"], name="/Mul_20"),
        helper.make_node("Sub", ["one", "m"], ["m1"], name="/Sub_7"),
        helper.make_node("Mul", ["b", "m1"], ["bm"], name="/Mul_21"),
        helper.make_node("Add", ["am", "bm"], ["output"], name="/Add_9"),
    ]  # fmt: skip
    graph = helper.make_graph(
        n, "mini_rife",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 7, H, W])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 3, H, W])],
    )  # fmt: skip
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 16)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def _convert(tmp_path: Any, model: Any, **fp16: bool) -> Any:
    ns = _ns()
    ns["_FP16"].clear()
    ns["_FP16"].update(fp16)
    src, dst = tmp_path / "in.onnx", tmp_path / "out.onnx"
    onnx.save(model, src)
    ns["_fp16_model"](str(src), str(dst), fp16=True, output_format=1)
    out = onnx.load(dst)
    onnx.checker.check_model(out)
    return out


def _half_on_the_way(model: Any) -> list[str]:
    """The first fp16 tensors on the way back from the final warp's positions,
    back to the network's own layers (Conv here)."""
    m = onnx.shape_inference.infer_shapes(model)
    types = {v.name: v.type.tensor_type.elem_type for v in m.graph.value_info}
    types.update((v.name, v.type.tensor_type.elem_type) for v in m.graph.input)
    for n in m.graph.node:
        if n.op_type == "Constant":
            types[n.output[0]] = n.attribute[0].t.data_type
    prod = {o: n for n in m.graph.node for o in n.output}
    final = next(n for n in m.graph.node if n.name == "/GridSample_4")
    half, todo, seen = [], [final.input[1]], set()
    while todo:
        x = todo.pop()
        n = prod.get(x)
        if n is None or x in seen or n.op_type == "Conv":
            continue
        seen.add(x)
        if types.get(x) == TensorProto.FLOAT16:
            half.append(x)  # where the fp16 part begins
            continue
        todo.extend(i for i in n.input if i)
    return sorted(half)


def test_final_warp_is_found_through_the_blend() -> None:
    ns = _ns()
    model = _mini_rife()
    by_output = {o: n for n in model.graph.node for o in n.output}
    assert [n.name for n in ns["_final_warps"](model, by_output)] == ["/GridSample_4"]
    final = ns["_warp_nodes"](model)
    assert "/GridSample_4" in final and "/GridSample" not in final
    for name in ("/Add_8", "/Mul_19", "/Reshape_25", "/Add_7", "/block1/Split", "base", "div"):
        assert name in final
    # the network's own layers are never part of it, nor the flow sum the
    # warp between blocks and the next block read too (it stays in fp16)
    assert "/block1/conv" not in final and "/Sigmoid" not in final
    assert "/block0/Mul" not in final
    every = ns["_grid_nodes"](model)
    assert "/GridSample" in every and set(final) <= set(every)


def test_no_final_warp_is_refused() -> None:
    ns = _ns()
    model = _mini_rife()
    for n in model.graph.node:
        if n.name == "/GridSample_4":
            n.op_type = "Identity"
            del n.attribute[:]
            del n.input[1:]
    by_output = {o: n for n in model.graph.node for o in n.output}
    with pytest.raises(vs.Error, match="no final warp"):
        ns["_final_warps"](model, by_output)


def test_converter_alone_rounds_the_shared_grid_to_half(tmp_path: Any) -> None:
    # what TensorRT RIFE ran before precise warp: even with every warp node
    # kept ('grid', DRBA's setting), the base grid, the divisor and the flow
    # sum each pass through fp16 because more than one node reads them
    today = _convert(tmp_path, _mini_rife())
    assert "gridt" in _half_on_the_way(today)
    grid = _convert(tmp_path, _mini_rife(), grid=True)
    assert {"base", "div", "flow0"} <= set(_half_on_the_way(grid))


def test_precise_warp_has_no_half_step_to_the_final_warp(tmp_path: Any) -> None:
    model = _convert(tmp_path, _mini_rife(), warp=True)
    # only the flow sum the network shares with the earlier warp is fp16
    # (as the network made it); the grid, divisor and every step after the
    # last block's flow are fp32
    assert _half_on_the_way(model) == ["flow0"]
    nodes = {n.name: n for n in model.graph.node}
    # the warp between blocks and the network stay in fp16 (cheap)
    assert "/GridSample_input_cast0" not in nodes and "/block0/Mul_output_cast0" not in nodes
    assert sum(n.op_type == "GridSample" for n in model.graph.node) == 2


def test_precise_warp_conversion_is_stable(tmp_path: Any) -> None:
    a = _convert(tmp_path, _mini_rife(), warp=True)
    b = _convert(tmp_path, _mini_rife(), warp=True)
    assert a.SerializeToString() == b.SerializeToString()


def test_keep_list_from_model_metadata(tmp_path: Any) -> None:
    # a patched model (net scale) names the nodes it needs in fp32
    model = _mini_rife()
    entry = model.metadata_props.add()
    entry.key, entry.value = "buttereye.fp32_nodes", "/Sigmoid,/Mul_20"
    out = _convert(tmp_path, model)
    sig = next(n for n in out.graph.node if n.name == "/Sigmoid")
    assert any(n.op_type == "Cast" and n.output[0] == sig.input[0] for n in out.graph.node)


def test_precise_warp_puts_the_pixels_back_where_fp32_does(tmp_path: Any) -> None:
    ort = pytest.importorskip("onnxruntime")
    # no motion: the warp reads every pixel exactly where it is, so any
    # difference to fp32 in the warped frame is a rounded sampling position
    model = _mini_rife(zero=True)
    x = np.random.default_rng(5).random((1, 7, H, W), dtype=np.float32)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 2

    def run(m: Any, inp: Any) -> Any:
        s = ort.InferenceSession(m.SerializeToString(), opts, providers=["CPUExecutionProvider"])
        return s.run(None, {"input": inp})[0].astype(np.float64)

    ref = run(model, x.astype(np.float16).astype(np.float32))
    today = np.abs(run(_convert(tmp_path, model), x.astype(np.float16)) - ref).max()
    precise = np.abs(run(_convert(tmp_path, model, warp=True), x.astype(np.float16)) - ref).max()
    assert today > 0.02  # positions up to 1/8 px off at 1024 wide on noise
    assert precise < 0.002  # what is left is the fp16 blend
    assert precise * 10 < today


# ------------------------------------------------- _scale_flow's input padding
def _padded_rife() -> Any:
    """The parts of the v2 export _scale_flow rewrites: the input padding
    (64 for height and width), four blocks' down/up Resize and flow Mul, and
    the flow rescale between blocks."""
    n: list[Any] = [_const("i2", 2, np.int64), _const("i3", 3, np.int64)]
    pads = []
    for axis, i in (("h", "i2"), ("w", "i3")):
        n += [
            _const(f"m{axis}1", 64, np.int64), _const(f"m{axis}2", 64, np.int64),
            _const(f"m{axis}3", 64, np.int64),
            helper.make_node("Shape", ["input"], [f"shape_{axis}"], name=f"/Shape_{axis}"),
            helper.make_node("Gather", [f"shape_{axis}", i], [axis], name=f"/Gather_{axis}"),
            helper.make_node("Mod", [axis, f"m{axis}1"], [f"{axis}m"], name=f"/Mod_{axis}"),
            helper.make_node("Sub", [f"m{axis}2", f"{axis}m"], [f"{axis}s"], name=f"/Sub_{axis}"),
            helper.make_node("Mod", [f"{axis}s", f"m{axis}3"], [f"{axis}p"], name=f"/Mod2_{axis}"),
            _const(f"ax_{axis}", [0], np.int64),
            helper.make_node("Unsqueeze", [f"{axis}p", f"ax_{axis}"], [f"{axis}u"],
                             name=f"/Unsqueeze_{axis}"),
        ]  # fmt: skip
        pads.append(f"{axis}u")
    n += [
        _const("z6", [0] * 6, np.int64),
        helper.make_node("Concat", ["z6", *pads], ["pads"], name="/Concat_pads", axis=0),
        helper.make_node("Pad", ["input", "pads"], ["x"], name="/Pad", mode="reflect"),
    ]
    prev = None
    for i, s in enumerate((16.0, 8.0, 4.0, 2.0)):
        b = f"/block{i}"
        n += [
            _const(f"sd{i}", [1, 1, 1 / s, 1 / s]), _const(f"su{i}", [1, 1, s, s]),
            _const(f"sm{i}", s),
            helper.make_node("Resize", ["x", "", f"sd{i}"], [f"d{i}"], name=f"{b}/Resize",
                             mode="linear"),
            helper.make_node("Resize", [f"d{i}", "", f"su{i}"], [f"u{i}"], name=f"{b}/Resize_1",
                             mode="linear"),
            helper.make_node("Mul", [f"u{i}", f"sm{i}"], [f"f{i}"], name=f"{b}/Mul"),
        ]  # fmt: skip
        if prev is None:
            prev = f"f{i}"
        else:
            n.append(helper.make_node("Add", [prev, f"f{i}"], [f"sum{i}"], name=f"/Add_s{i}"))
            prev = f"sum{i}"
        if i < 3:
            k = (0.125, 0.25, 0.5)[i]
            n += [
                _const(f"nx{i}", k),
                helper.make_node("Mul", [prev, f"nx{i}"], [f"next{i}"], name=f"/Mul_n{i}"),
            ]
            n.append(helper.make_node("Add", [prev, f"next{i}"], [f"acc{i}"], name=f"/Add_n{i}"))
            prev = f"acc{i}"
    n.append(helper.make_node("Identity", [prev], ["output"], name="/Out"))
    graph = helper.make_graph(
        n, "padded",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 7, "h", "w"])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 7, "a", "b"])],
    )  # fmt: skip
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 16)])
    model.ir_version = 8
    onnx.checker.check_model(model)
    return model


def _multiples(model: Any) -> list[int]:
    ns = _ns()
    const = {n.output[0]: n for n in model.graph.node if n.op_type == "Constant"}
    return [int(onnx.numpy_helper.to_array(c.attribute[0].t)) for c in
            ns["_pad_multiples"](model.graph, const)]  # fmt: skip


def test_half_flow_pads_the_input_to_128() -> None:
    # 1080 lines pad to 1088 for RIFE, but at flow scale 0.5 the first block
    # sees 1/32 of it and its two halvings need whole pixels: 1152 (64/k). With
    # 1088, TensorRT stopped at the first warp: "reshape changes volume
    # [1,4,1152,1920] -> [2,2,1088,1920]" (1440 and 800 lines too)
    ns = _ns()
    assert _multiples(_padded_rife()) == [64] * 6
    model = ns["_scale_flow"](_padded_rife(), 0.5)
    assert _multiples_after(model) == [128] * 6


def _multiples_after(model: Any) -> list[int]:
    const = {n.output[0]: n for n in model.graph.node if n.op_type == "Constant"}
    names = [n.input[1] for n in model.graph.node if n.op_type == "Mod"]
    names += [n.input[0] for n in model.graph.node if n.op_type == "Sub"]
    return sorted(int(onnx.numpy_helper.to_array(const[x].attribute[0].t)) for x in names)


@pytest.mark.parametrize("height", [1080, 720, 1440, 2160, 800])
def test_half_flow_padded_sizes(height: int) -> None:
    ort = pytest.importorskip("onnxruntime")
    ns = _ns()
    model = ns["_scale_flow"](_padded_rife(), 0.5)
    model.graph.output.append(
        helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, 7, "c", "d"])
    )
    s = ort.InferenceSession(model.SerializeToString(), providers=["CPUExecutionProvider"])
    x = s.run(["x"], {"input": np.zeros((1, 7, height, 128), np.float32)})[0]
    assert x.shape[2:] == (-(-height // 128) * 128, 128)


def test_flow_scale_without_whole_padding_is_refused() -> None:
    ns = _ns()
    with pytest.raises(vs.Error, match="not supported"):
        ns["_scale_flow"](_padded_rife(), 0.3)


def test_changed_padding_is_refused() -> None:
    ns = _ns()
    model = _padded_rife()
    for n in model.graph.node:
        if n.name == "mh2":
            n.attribute[0].t.CopyFrom(onnx.numpy_helper.from_array(np.asarray(32, np.int64), "mh2"))
    with pytest.raises(vs.Error, match="input pad"):
        ns["_scale_flow"](model, 0.5)
