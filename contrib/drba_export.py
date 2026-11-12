#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Export a DistilDRBA model to ONNX for ButterEye's TensorRT path.

DistilDRBA (MIT, routineLife1) is an interpolation network for 2D animation:
it reads three frames and keeps drawings that are held on twos or threes at
their own pace while what moves on every frame (pans, zooms) is smoothed.
Upstream ships PyTorch weights; ButterEye runs ONNX through vs-mlrt's
TensorRT plugin, so playback needs no PyTorch. This script needs it once,
CPU-only is enough (contrib/build-drba.sh sets that up and removes it again).

The exported network takes one input of ten planes, 1x10xHxW in 0..1 (RGB of
the frame before, RGB of the frame the output is nearest to, RGB of the frame
after, one plane holding the time), any H and W, and returns 1x3xHxW. Time
is 1.0 at the middle frame, 0.5 halfway back to the frame before and 1.5
halfway on to the frame after.

Usage: drba_export.py --source DIR/vsdrba_distilled --weights v1.pkl --out drba_v1.onnx
"""

from __future__ import annotations

import argparse
import importlib
import sys
import types
from pathlib import Path

import torch
import torch.nn.functional as F  # noqa: N812
from torch import nn

ARCH = {"v1": "distilDRBA", "v2_lite": "distilDRBA_v2_lite"}
MODULO = 64


def load_arch(source: Path, name: str) -> type[nn.Module]:
    """The upstream IFNet class, without importing the package (its __init__
    wants VapourSynth, CUDA and a downloader)."""
    pkg = types.ModuleType("drba_src")
    pkg.__path__ = [str(source)]
    sys.modules["drba_src"] = pkg
    module = importlib.import_module("drba_src." + ARCH[name])
    module.warp = warp  # type: ignore[attr-defined]
    return module.IFNet  # type: ignore[no-any-return]


def warp(image: torch.Tensor, flow: torch.Tensor, div: torch.Tensor, base: torch.Tensor
         ) -> torch.Tensor:  # fmt: skip
    """Upstream's warp through F.grid_sample, which the ONNX exporter knows
    (bilinear, border padding, corners aligned: the same sampling)."""
    flow = torch.cat([flow[:, 0:1] / div[0], flow[:, 1:2] / div[1]], 1)
    return F.grid_sample(image, (base + flow).permute(0, 2, 3, 1), mode="bilinear",
                         padding_mode="border", align_corners=True)  # fmt: skip


def grid(self: nn.Module, x: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Upstream's get_grid with the size read from the tensor, so that one
    exported file serves every frame size."""
    ones = torch.ones_like(x[:, :1], dtype=torch.float)
    half_w = (ones.sum(3, keepdim=True)[:, :, :1] - 1.0) / 2.0  # (W - 1) / 2
    half_h = (ones.sum(2, keepdim=True)[:, :, :, :1] - 1.0) / 2.0
    horizontal = (ones.cumsum(3) - 1.0) / half_w - 1.0
    vertical = (ones.cumsum(2) - 1.0) / half_h - 1.0
    div = torch.cat([half_w.reshape(1), half_h.reshape(1)])
    return div, torch.cat([horizontal, vertical], 1)


class Planes(nn.Module):
    """Ten planes in, picture out; pads to the network's multiple of 64 inside."""

    def __init__(self, net: nn.Module) -> None:
        super().__init__()
        self.net = net

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h, w = x.shape[2], x.shape[3]
        pad_h = (MODULO - h % MODULO) % MODULO
        pad_w = (MODULO - w % MODULO) % MODULO
        t = x[:, 9:10, :1, :1]
        x = F.pad(x[:, :9], (0, pad_w, 0, pad_h))
        out = self.net(x[:, :3], x[:, 3:6], x[:, 6:9], t)
        return out[:, :, :h, :w]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--source", type=Path, required=True, help="upstream's vsdrba_distilled folder")
    ap.add_argument("--weights", type=Path, required=True, help="v1.pkl or v2_lite.pkl")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", choices=sorted(ARCH), help="default: from the weights' name")
    ap.add_argument("--check", type=Path, help="also save an input and PyTorch's output (.npz)")
    args = ap.parse_args()
    name = args.model or args.weights.stem
    if name not in ARCH:
        ap.error(f"unknown model {name!r}: pass --model")
    arch = load_arch(args.source, name)
    arch.get_grid = grid  # type: ignore[attr-defined]
    net = arch(1.0)
    net.load_state_dict(torch.load(args.weights, map_location="cpu", weights_only=True))
    model = Planes(net).eval()
    sample = torch.rand(1, 10, 192, 320)
    sample[:, 9] = 0.5
    with torch.inference_mode():
        torch.onnx.export(
            model, (sample,), str(args.out), input_names=["input"], output_names=["output"],
            dynamic_axes={"input": {2: "h", 3: "w"}, "output": {2: "h", 3: "w"}},
            opset_version=17, dynamo=False,
        )  # fmt: skip
        if args.check:
            import numpy as np

            torch.manual_seed(1)
            x = torch.rand(1, 10, 270, 480)  # not the traced size, not a multiple of 64
            # three shifted copies of one smooth picture: something to interpolate
            base = F.interpolate(torch.rand(1, 3, 34, 60), size=(270, 480), mode="bicubic")
            for i in range(3):
                x[:, 3 * i : 3 * i + 3] = torch.roll(base, 6 * i, 3).clamp(0, 1)
            outs = {}
            for t in (0.5, 0.75, 1.25):
                x[:, 9] = t
                outs[f"t{t}"] = model(x).numpy()
            np.savez(args.check, x=x.numpy(), **outs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
