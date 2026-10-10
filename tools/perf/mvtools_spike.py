# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""MVTools at 4K (performance track P3, spike M0(p)): speed and quality of
cheaper MVTools settings.

Speed: vspipe throughput (output fps, 2x) of each variant on the panning texture
at 1080p and 2160p. Quality: frame triples (A, B, C) from real videos; each
variant rebuilds B from A and C, scored against the real B (PSNR, SSIM), next to
a plain A/C blend. Dev-box tool in the dev venv (``ort_ceiling.py``) under
``tools/memguard.py``; .npy hand-over files are deleted afterwards and labels in
the output are neutral.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import memguard  # noqa: E402  (tools/memguard.py)

DEV = Path.home() / ".cache/buttereye-dev/p2"
STAGE = HERE / "mv_stage.vpy"
DONE = re.compile(r"\(([\d.]+) fps\)")
#: name -> settings (live default first)
VARIANTS: dict[str, dict[str, Any]] = {
    "default (blk16 ov4 flow)": {"blksize": 16, "overlap": 4},
    "blk16 ov0 flow": {"blksize": 16, "overlap": 0},
    "blk16 ov4 luma-only": {"blksize": 16, "overlap": 4, "chroma": False},
    "blk32 ov8 flow": {"blksize": 32, "overlap": 8},
    "blk32 ov8 luma-only": {"blksize": 32, "overlap": 8, "chroma": False},
    "blk32 ov0 flow": {"blksize": 32, "overlap": 0},
    "blk16 ov4 block": {"blksize": 16, "overlap": 4, "fps": "block"},
    "blk32 ov8 block": {"blksize": 32, "overlap": 8, "fps": "block"},
}


def vspipe(cfg: dict[str, Any], out: str = "--", requests: int = 16) -> str:
    env = {k: v for k, v in os.environ.items() if k not in ("VIRTUAL_ENV", "PYTHONHOME")}
    env["PATH"] = ":".join(p for p in env.get("PATH", "").split(":") if "/p2/venv/" not in p)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = ["vspipe", "-r", str(requests), "-a", f"user_data={json.dumps(cfg)}", str(STAGE), out]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=900, env=env)
    if proc.returncode != 0:
        raise RuntimeError(" | ".join(proc.stderr.strip().splitlines()[-3:]))
    return proc.stderr + proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clip", action="append", default=[], help="PATH@t1,t2")
    ap.add_argument("--label", action="append", default=[])
    ap.add_argument("--json", type=Path)
    ap.add_argument("--mem-max", type=int, default=memguard.max_mib(8192))
    args = ap.parse_args()
    short = memguard.preflight()
    if short is not None:
        print(f"Can't run: {short}", file=sys.stderr)
        return 2
    why = memguard.reexec_capped(
        [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]], args.mem_max, "mvtools"
    )
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="mvtools_spike").start()

    base = {"plugin_dir": "/usr/lib64/buttereye/vapoursynth"}
    out: dict[str, Any] = {"speed": [], "quality": []}
    for name, v in VARIANTS.items():
        for w, h, frames in ((1920, 1080, 240), (3840, 2160, 96)):
            runs = []
            for _ in range(3):
                cfg = base | v | {"mode": "speed", "width": w, "height": h, "frames": frames}
                m = DONE.search(vspipe(cfg))
                runs.append(float(m.group(1)) if m else 0.0)
            med = statistics.median(runs)
            print(f"speed {name} {h}p: {runs} median {med}", file=sys.stderr, flush=True)
            out["speed"].append({"variant": name, "size": f"{w}x{h}", "runs": runs, "fps": med})

    if args.clip:
        import fp16_quality as fq
        import numpy as np

        a_npy, c_npy, raw = (DEV / "scratch" / n for n in ("mv_a.npy", "mv_c.npy", "mv.rgbs"))
        a_npy.parent.mkdir(parents=True, exist_ok=True)
        try:
            for i, spec in enumerate(args.clip):
                path, _, times = spec.rpartition("@")
                label = args.label[i] if i < len(args.label) else f"clip{i + 1}"
                w, h = fq.probe(path)
                for t in times.split(","):
                    a, b, c = fq.frames(np, path, float(t), w, h)
                    np.save(a_npy, a)
                    np.save(c_npy, c)
                    row: dict[str, Any] = {"clip": label, "t": float(t),
                                           "blend": fq.psnr(np, (a + c) / 2, b)}  # fmt: skip
                    for name, v in VARIANTS.items():
                        cfg = base | v | {"mode": "quality", "a_npy": str(a_npy),
                                          "c_npy": str(c_npy)}  # fmt: skip
                        vspipe(cfg, str(raw), requests=2)
                        frames = np.fromfile(raw, dtype=np.float32).reshape(-1, 3, h, w)
                        mid = np.clip(frames[1][[2, 0, 1]], 0, 1)  # vspipe RGB is G, B, R
                        row[name] = fq.psnr(np, mid, b)
                    print(f"quality {label} @{t}: {row}", file=sys.stderr, flush=True)
                    out["quality"].append(row)
        finally:
            for f in (a_npy, c_npy, raw):
                f.unlink(missing_ok=True)

    sizes = ("1920x1080", "3840x2160")
    print("| variant | 1080p fps | 2160p fps |" + (" mean PSNR vs real | vs default |"
          if out["quality"] else ""))  # fmt: skip
    print("|---|---|---|" + ("---|---|" if out["quality"] else ""))
    default = next(iter(VARIANTS))
    for name in VARIANTS:
        fps = {r["size"]: r["fps"] for r in out["speed"] if r["variant"] == name}
        line = f"| {name} | {fps.get(sizes[0], 0):.1f} | {fps.get(sizes[1], 0):.1f} |"
        if out["quality"]:
            mean = statistics.mean(q[name] for q in out["quality"])
            ref = statistics.mean(q[default] for q in out["quality"])
            line += f" {mean:.2f} | {mean - ref:+.2f} |"
        print(line)
    if out["quality"]:
        print(f"\nplain A/C blend: {statistics.mean(q['blend'] for q in out['quality']):.2f} dB")
    if args.json:
        args.json.write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
