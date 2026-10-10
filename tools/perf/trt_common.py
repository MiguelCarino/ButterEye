# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""vs-mlrt TensorRT setup shared by the perf stage scripts (runs inside vspipe/mpv).

``rife(core, rgb, cfg, multi)`` loads vstrt, imports the vsmlrt.py installed next
to it (contrib/build-vstrt.sh), points it at ``trtexec`` and the ONNX models, and
replaces its fp16 conversion with ``convert_model`` below, which keeps RIFE's
Cast nodes out of the conversion (spikes M0(k), M0(l)). The installed vsmlrt.py
is never edited.

cfg keys: vstrt_dir, vspy_dir (onnx + onnxconverter-common, appended to sys.path),
onnx_models, engine_dir, trt_model (vsmlrt.RIFEModel name), fp16, half_io,
streams, trtexec (optional).
"""

from __future__ import annotations

import os
import sys
from fractions import Fraction
from typing import Any


def convert_model(
    network_path: str,
    target_network_path: str,
    fp16: bool = False,
    bf16: bool = False,
    input_format: int = 0,
    output_format: int = 0,
) -> None:
    """vsmlrt.convert_model, with RIFE's Cast nodes left in fp32.

    vsmlrt 3.23.2 retypes the outputs of the graph's Cast nodes but not their
    ``to`` attribute; TensorRT 11 then refuses the model ("/Mul ... must have same
    input types ... Float and Half").
    """
    import onnx  # type: ignore[import-not-found]  # inside vspipe only
    from onnxconverter_common.float16 import (  # type: ignore[import-not-found]
        convert_float_to_float16,
    )

    if bf16 or not fp16:
        raise ValueError("ButterEye: only fp16 conversion is supported")
    model = onnx.load(network_path)
    casts = [n.name for n in model.graph.node if n.op_type == "Cast"]
    model = convert_float_to_float16(model, keep_io_types=output_format != 1, node_block_list=casts)
    onnx.save(model, target_network_path)


def rife(core: Any, rgb: Any, cfg: dict[str, Any], multi: Fraction | int) -> Any:
    # vsmlrt looks for a loaded plugin at import time, so load vstrt first
    if not hasattr(core, "trt"):
        core.std.LoadPlugin(os.path.join(cfg["vstrt_dir"], "libvstrt.so"))
    if cfg["vstrt_dir"] not in sys.path:
        sys.path.insert(0, cfg["vstrt_dir"])
    if cfg.get("vspy_dir") and cfg["vspy_dir"] not in sys.path:
        sys.path.append(cfg["vspy_dir"])  # last: the system's own packages win
    import vsmlrt  # type: ignore[import-not-found]  # installed next to vstrt

    vsmlrt.trtexec_path = cfg.get("trtexec", "/usr/bin/trtexec")
    vsmlrt.models_path = cfg["onnx_models"]
    vsmlrt.convert_model = convert_model
    backend = vsmlrt.Backend.TRT(
        fp16=bool(cfg["fp16"]),
        num_streams=int(cfg["streams"]),
        output_format=1 if cfg["half_io"] else 0,
        engine_folder=cfg["engine_dir"],
    )
    return vsmlrt.RIFE(
        rgb,
        multi=multi,
        model=vsmlrt.RIFEModel[cfg["trt_model"]],
        backend=backend,
        video_player=True,
        _implementation=2,  # rife_v2: internal padding, any frame size
    )
