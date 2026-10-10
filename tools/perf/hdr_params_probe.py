#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Spike M0(d) probe: play a video in mpv (no window, no audio) with a VapourSynth
filter or none, and print mpv's video-params and video-out-params.

Usage: [WAIT=15] [LOG=mpv.log] tools/perf/hdr_params_probe.py VIDEO {none|FILTER.vpy}
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from typing import Any

KEYS = (
    "pixelformat", "w", "h", "colormatrix", "colorlevels", "primaries", "gamma",
    "sig-peak", "light", "chroma-location", "max-cll", "max-fall", "min-luma", "max-luma",
)  # fmt: skip


def get(sock: str, prop: str) -> Any:
    with socket.socket(socket.AF_UNIX) as c:
        c.connect(sock)
        c.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
        data = b""
        while b"\n" not in data:
            data += c.recv(65536)
    return json.loads(data.split(b"\n")[0]).get("data")


def main(src: str, vf: str) -> None:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    sock = os.path.join(runtime, f"buttereye-m0d-{os.getpid()}.sock")
    args = [
        "mpv", "--no-config", f"--log-file={os.environ.get('LOG', '/dev/null')}",
        "--vo=null", "--ao=null", "--hwdec=nvdec-copy", f"--input-ipc-server={sock}",
        "--start=1", src,
    ]  # fmt: skip
    if vf != "none":
        args.insert(1, f"--vf=vapoursynth=file={vf}:buffered-frames=4:concurrent-frames=8")
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(float(os.environ.get("WAIT", "4")))
        out = {
            k: get(sock, k)
            for k in ("video-params", "video-out-params", "estimated-vf-fps", "container-fps")
        }
    finally:
        proc.terminate()
        proc.wait()
    for k in ("video-params", "video-out-params"):
        v = out[k] or {}
        print(k, {x: v.get(x) for x in KEYS if x in v})
    print("vf-fps", out["estimated-vf-fps"], "container", out["container-fps"])


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
