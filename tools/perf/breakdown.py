# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Phase 0 performance breakdown (SCOPE §15.1, spike M0(i)).

Times each step of the smoothing pipeline on its own with ``tools/perf/stage.vpy``:
the source alone, YUV↔RGBS conversion, RIFE with and without the conversion,
MVTools; per model, per ``gpu_thread`` and with implicit Vulkan layers on and
off; in vspipe and (``--mpv``) inside mpv. While each run lasts it samples
``nvidia-smi dmon`` (SM %, PCIe rx/tx) in a subprocess; ButterEye's no-NVIDIA-
in-process rule (SCOPE §8.1) holds because nothing NVIDIA is loaded here.

Runs are sequential (the dev box is shared) and run under the memory failsafes
of ``tools/memguard.py``: it refuses to start when memory is short, runs inside
a capped systemd scope (``--mem-max``), and a watchdog kills every vspipe/mpv it
started when the system runs low. Each vspipe/mpv also gets a bounded
VapourSynth frame cache (``--cache-mb``) and vspipe a bounded request count.

The result is a Markdown table on stdout, ready for ``docs/spikes/m0i.md``, and the raw
numbers as JSON.

Usage::

    python3 tools/perf/breakdown.py                  # default matrix, ~10 min
    python3 tools/perf/breakdown.py --quick          # 1080p only, fewer runs
    python3 tools/perf/breakdown.py --mpv --json out.json --filter-time

Needs the ButterEye plugin RPMs (``buttereye-vs-rife-ncnn``,
``buttereye-rife-ncnn-models``, ``buttereye-vs-mvtools``), ``vspipe`` and, for
``--mpv``, an mpv with the vapoursynth filter. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import memguard  # noqa: E402  (tools/memguard.py)

STAGE_VPY = HERE / "stage.vpy"
PLUGIN_DIR = Path("/usr/lib64/buttereye/vapoursynth")
MODEL_DIR = Path("/usr/share/buttereye/rife-ncnn-models")
DEFAULT_MODELS = (
    "rife-v4.26_ensembleFalse",
    "rife-v4.22_lite_ensembleFalse",
    "rife-v4.18_ensembleFalse",
)
SIZES = {"720": (1280, 720), "1080": (1920, 1080), "1440": (2560, 1440), "2160": (3840, 2160)}
SRC = Fraction(24000, 1001)
MEM_MAX_MIB = 8192  # hard cap for this tool and everything it starts
#: VapourSynth frame cache per vspipe/mpv in MB (VS default: 4096) and vspipe
#: concurrent frame requests (as mpv's concurrent-frames for RIFE); --cache-mb, --requests
LIMITS = {"cache_mb": 1024, "requests": 8}
#: vs-mlrt TensorRT path (--trt): vstrt from contrib/build-vstrt.sh, vs-mlrt's ONNX
#: models and its Python deps (onnx, onnxconverter-common) for vspipe's Python
DEV = Path.home() / ".cache/buttereye-dev/p2"
VSTRT_DIR = Path.home() / ".local/share/buttereye/plugins/vstrt/v16.3.test1"
TRT = {
    "vstrt_dir": VSTRT_DIR,
    "vspy_dir": DEV / "vspy",
    "onnx_models": DEV / "models",
    "engine_dir": DEV / "engines",
}
LAYERS_OFF = {"VK_LOADER_LAYERS_DISABLE": "~implicit~"}
VSPIPE_DONE = re.compile(r"Output (\d+) frames in ([\d.]+) seconds \(([\d.]+) fps\)")


@dataclass
class Run:
    host: str  # "vspipe" | "mpv"
    stage: str
    size: str
    model: str | None
    gpu_thread: int | None
    layers: bool
    target: str
    variant: str | None = None  # TensorRT: "fp16-h" (fp16, RGBH frames), "fp16-s", "fp32-s"
    concurrent: int | None = None  # mpv concurrent-frames (None: 8 for RIFE, 4 otherwise)
    mv_mode: str | None = None  # MVTools: "block" = BlockFPS (spike M0(p)), else FlowFPS
    out_fps: float | None = None
    src_fps: float | None = None  # source frames per second through the stage
    interp_fps: float | None = None  # new (interpolated) frames per second
    sm_pct: float | None = None
    mem_pct: float | None = None
    rx_mbs: float | None = None
    tx_mbs: float | None = None
    wall_s: float | None = None
    error: str | None = None
    filter_time: list[str] = field(default_factory=list)


class Dmon:
    """``nvidia-smi dmon -s ut`` in the background; averages over the run."""

    def __init__(self) -> None:
        self.rows: list[list[float]] = []
        self.proc: subprocess.Popen[str] | None = None
        self.thread: threading.Thread | None = None

    def __enter__(self) -> Dmon:
        if shutil.which("nvidia-smi") is None:
            return self
        self.proc = subprocess.Popen(
            ["nvidia-smi", "dmon", "-i", "0", "-s", "ut", "-d", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()
        return self

    def _read(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            if line.startswith("#"):
                continue
            parts = line.split()
            # gpu sm mem enc dec jpg ofa rxpci txpci
            if len(parts) >= 9:
                try:
                    self.rows.append([float(p) if p != "-" else 0.0 for p in parts[1:9]])
                except ValueError:
                    pass

    def __exit__(self, *_exc: object) -> None:
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread is not None:
            self.thread.join(timeout=3)

    def fill(self, run: Run) -> None:
        rows = self.rows[1:] if len(self.rows) > 2 else self.rows  # skip the ramp-up sample
        if not rows:
            return
        avg = [sum(col) / len(rows) for col in zip(*rows, strict=False)]
        run.sm_pct, run.mem_pct, run.rx_mbs, run.tx_mbs = avg[0], avg[1], avg[6], avg[7]


def target_rate(name: str) -> Fraction:
    if name == "2x":
        return SRC * 2
    return Fraction(name)


def user_data(run: Run, frames: int, width: int, height: int) -> str:
    out = target_rate(run.target)
    data: dict[str, object] = {
        "stage": run.stage,
        "plugin_dir": str(PLUGIN_DIR),
        "src_num": SRC.numerator,
        "src_den": SRC.denominator,
        "out_num": out.numerator,
        "out_den": out.denominator,
        "width": width,
        "height": height,
        "frames": frames,
        "matrix": "709",
        "cache_mb": LIMITS["cache_mb"],
    }
    if run.mv_mode:
        data["mv_mode"] = run.mv_mode
    if run.stage.startswith("trt"):
        precision, io = (run.variant or "fp16-h").split("-")
        data.update(
            perf_dir=str(HERE),
            vstrt_dir=str(TRT["vstrt_dir"]),
            vspy_dir=str(TRT["vspy_dir"]),
            onnx_models=str(TRT["onnx_models"]),
            engine_dir=str(TRT["engine_dir"]),
            trt_model=run.model,
            fp16=precision == "fp16",
            half_io=io == "h",
            streams=run.gpu_thread or 1,
        )
    elif run.model is not None:
        data["model_path"] = str(MODEL_DIR / run.model)
        data["gpu_thread"] = run.gpu_thread or 4
    return json.dumps(data, separators=(",", ":"))


def multiplier(run: Run) -> Fraction:
    if run.stage in ("source", "convert"):
        return Fraction(1)
    return target_rate(run.target) / SRC


def finish(run: Run, out_fps: float) -> None:
    m = multiplier(run)
    run.out_fps = round(out_fps, 2)
    run.src_fps = round(out_fps / float(m), 2)
    if m > 1:
        # At an integer multiplier the source frames pass through; at other
        # rates nearly every output frame is new.
        new_per_out = float((m - 1) / m) if m.denominator == 1 else 1.0
        run.interp_fps = round(out_fps * new_per_out, 2)


def env_for(run: Run) -> dict[str, str]:
    env = dict(os.environ)
    if not run.layers:
        env.update(LAYERS_OFF)
    return env


def run_vspipe(run: Run, frames: int, filter_time: bool, timeout: float) -> Run:
    width, height = SIZES[run.size]
    m = multiplier(run)
    out_frames = int(frames * m)
    argv = [
        "vspipe",
        "-a",
        f"user_data={user_data(run, frames, width, height)}",
        "-e",
        str(out_frames - 1),
        "-r",
        str(LIMITS["requests"]),
    ]
    if filter_time:
        argv.append("--filter-time")
    argv += [str(STAGE_VPY), "--"]
    t0 = time.monotonic()
    with Dmon() as dmon:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, env=env_for(run), check=False
            )
        except subprocess.TimeoutExpired:
            run.error = f"timed out after {timeout:.0f} s"
            return run
    run.wall_s = round(time.monotonic() - t0, 2)
    dmon.fill(run)
    text = proc.stderr + proc.stdout
    match = VSPIPE_DONE.search(text)
    if proc.returncode != 0 or match is None:
        run.error = last_lines(text)
        return run
    finish(run, float(match.group(3)))
    if filter_time:
        run.filter_time = [
            ln
            for ln in text.splitlines()
            if ln.strip()
            and "Output" not in ln
            and "[dlssnr" not in ln
            and "[0 " not in ln
            and "[1 " not in ln
        ][-20:]
    return run


def mpv_once(run: Run, frames: int, timeout: float) -> float:
    width, height = SIZES[run.size]
    concurrent = run.concurrent or (8 if run.model is not None else 4)
    vf = (
        f"vapoursynth=file=%{len(str(STAGE_VPY))}%{STAGE_VPY}"
        f":buffered-frames=4:concurrent-frames={concurrent}"
    )
    data = user_data(run, frames, width, height)
    vf += f":user-data=%{len(data.encode())}%{data}"
    src = (
        f"av://lavfi:testsrc2=size={width}x{height}:rate={SRC.numerator}/{SRC.denominator},"
        "format=nv12"
    )
    argv = [
        "mpv",
        "--no-config",
        "--untimed",
        "--vo=null",
        "--ao=null",
        "--no-input-terminal",
        "--term-status-msg=",
        f"--frames={frames}",
        f"--vf={vf}",
        "--",
        src,
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, env=env_for(run), check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(last_lines(proc.stdout + proc.stderr))
    return time.monotonic() - t0


def run_mpv(run: Run, frames: int, timeout: float) -> Run:
    # Two lengths; the difference cancels startup and filter init.
    short = max(24, frames // 8)
    try:
        t_short = mpv_once(run, short, timeout)
        with Dmon() as dmon:
            t_long = mpv_once(run, frames, timeout)
        dmon.fill(run)
    except subprocess.TimeoutExpired:
        run.error = f"timed out after {timeout:.0f} s"
        return run
    except RuntimeError as exc:
        run.error = str(exc)
        return run
    run.wall_s = round(t_long, 2)
    if t_long <= t_short:
        run.error = "no measurable difference between the two lengths"
        return run
    finish(run, (frames - short) / (t_long - t_short))
    return run


def last_lines(text: str, n: int = 4) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-n:]) or "no output"


def plan_trt(args: argparse.Namespace) -> list[Run]:
    """TensorRT rows only: streams, precision and frame format, then mpv."""
    sizes = ["1080"] if args.quick else args.sizes
    model = args.trt_models[0]
    runs: list[Run] = []

    def add(host: str, stage: str, size: str, m: str, streams: int, variant: str) -> None:
        runs.append(Run(host, stage, size, m, streams, True, args.target, variant))

    for size in sizes:
        for streams in (1, 2, 4):
            add("vspipe", "trt", size, model, streams, "fp16-h")
        add("vspipe", "trt", size, model, 2, "fp16-s")
        add("vspipe", "trt", size, model, 2, "fp32-s")
        add("vspipe", "trt-rgb", size, model, 2, "fp16-h")
    for m in args.trt_models[1:]:
        add("vspipe", "trt", sizes[0], m, 2, "fp16-h")
    if not args.quick and args.target != "60":
        runs.append(Run("vspipe", "trt", sizes[0], model, 2, True, "60", "fp16-h"))
    if args.mpv:
        for size in sizes:
            add("mpv", "trt", size, model, 2, "fp16-h")
    return runs


def plan(args: argparse.Namespace) -> list[Run]:
    if args.trt:
        return plan_trt(args)
    sizes = ["1080"] if args.quick else args.sizes
    models = args.models
    best = models[0]
    runs: list[Run] = []
    for size in sizes:
        runs.append(Run("vspipe", "source", size, None, None, True, args.target))
        runs.append(Run("vspipe", "convert", size, None, None, True, args.target))
        runs.append(Run("vspipe", "rife", size, best, 4, True, args.target))
        runs.append(Run("vspipe", "rife-rgb", size, best, 4, True, args.target))
        runs.append(Run("vspipe", "mvtools", size, None, None, True, args.target))
    base = sizes[0]
    for model in models[1:]:  # does model cost show at all?
        runs.append(Run("vspipe", "rife", base, model, 4, True, args.target))
        runs.append(Run("vspipe", "rife-rgb", base, model, 4, True, args.target))
    for threads in args.threads:  # GPU queue depth
        if threads != 4:
            runs.append(Run("vspipe", "rife-rgb", base, best, threads, True, args.target))
    runs.append(Run("vspipe", "rife", base, best, 4, False, args.target))  # layers off
    if not args.quick and "60" != args.target:
        runs.append(Run("vspipe", "rife", base, best, 4, True, "60"))  # 2.5x cost
    if args.mpv:
        for stage, model in (
            ("source", None),
            ("convert", None),
            ("rife", best),
            ("mvtools", None),
        ):
            runs.append(Run("mpv", stage, base, model, 4 if model else None, True, args.target))
        runs.append(Run("mpv", "rife", base, best, 4, False, args.target))
    return runs


def table(runs: list[Run]) -> str:
    head = (
        "| host | stage | size | model | gpu_thread/streams | layers | target | out fps | src fps "
        "| new fps | SM % | rx MB/s | tx MB/s |"
    )
    rows = [head, "|" + "---|" * 13]

    def f(v: float | None) -> str:
        return "—" if v is None else f"{v:.1f}"

    for r in runs:
        model = (r.model or "—").replace("_ensembleFalse", "")
        if r.variant:
            model = f"{model} {r.variant}"
        if r.error:
            rows.append(
                f"| {r.host} | {r.stage} | {r.size} | {model} | {r.gpu_thread or '—'} "
                f"| {'on' if r.layers else 'off'} | {r.target} | error: {r.error} "
                "| | | | | |"
            )
            continue
        rows.append(
            f"| {r.host} | {r.stage} | {r.size}p | {model} | {r.gpu_thread or '—'} "
            f"| {'on' if r.layers else 'off'} | {r.target} | {f(r.out_fps)} | {f(r.src_fps)} "
            f"| {f(r.interp_fps)} | {f(r.sm_pct)} | {f(r.rx_mbs)} | {f(r.tx_mbs)} |"
        )
    return "\n".join(rows)


def preflight(args: argparse.Namespace) -> list[str]:
    problems = []
    if shutil.which("vspipe") is None:
        problems.append("vspipe is not installed (sudo dnf install vapoursynth-tools)")
    if args.trt:
        if not (TRT["vstrt_dir"] / "libvstrt.so").exists():
            problems.append(f"{TRT['vstrt_dir']}/libvstrt.so is missing (contrib/build-vstrt.sh)")
        if shutil.which("trtexec") is None:
            problems.append("trtexec is missing (libnvinfer-bin, docs/spikes/m0k.md)")
        if not (TRT["onnx_models"] / "rife_v2").is_dir():
            problems.append(f"vs-mlrt RIFE models missing in {TRT['onnx_models']}/rife_v2")
        TRT["engine_dir"].mkdir(parents=True, exist_ok=True)
        if args.mpv and shutil.which("mpv") is None:
            problems.append("mpv is not installed")
        return problems
    for name in ("librife.so", "mvtools.so"):
        if not (PLUGIN_DIR / name).exists():
            problems.append(f"{PLUGIN_DIR / name} is missing (install the buttereye-vs-* RPMs)")
    for model in args.models:
        if not (MODEL_DIR / model).is_dir():
            problems.append(f"model {MODEL_DIR / model} is missing")
    if args.mpv and shutil.which("mpv") is None:
        problems.append("mpv is not installed")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--sizes",
        type=lambda s: s.split(","),
        default=["1080", "2160"],
        help="comma-separated heights: 720,1080,1440,2160 (default 1080,2160)",
    )
    ap.add_argument(
        "--models",
        type=lambda s: s.split(","),
        default=list(DEFAULT_MODELS),
        help="model directory names; the first is used for size/thread runs",
    )
    ap.add_argument(
        "--threads",
        type=lambda s: [int(x) for x in s.split(",")],
        default=[1, 2, 4, 8],
        help="gpu_thread values to sweep (default 1,2,4,8)",
    )
    ap.add_argument("--target", default="2x", help='"2x" or a rate such as 60 or 60000/1001')
    ap.add_argument("--frames", type=int, default=240, help="source frames per run at 1080p")
    ap.add_argument("--quick", action="store_true", help="1080p only, no 60 fps run")
    ap.add_argument("--mpv", action="store_true", help="also time stages inside mpv")
    ap.add_argument(
        "--trt",
        action="store_true",
        help="TensorRT rows only (vs-mlrt vstrt; see contrib/build-vstrt.sh)",
    )
    ap.add_argument(
        "--trt-models",
        type=lambda s: s.split(","),
        default=["v4_26", "v4_22_lite"],
        help="vsmlrt.RIFEModel names; the first is used for every TensorRT row",
    )
    ap.add_argument(
        "--filter-time",
        action="store_true",
        help="keep vspipe --filter-time per-filter times in the JSON",
    )
    ap.add_argument("--timeout", type=float, default=600.0, help="seconds per run")
    ap.add_argument("--json", type=Path, help="write the raw results here")
    ap.add_argument(
        "--mem-max",
        type=int,
        default=memguard.max_mib(MEM_MAX_MIB),
        help=f"hard memory cap in MiB for the whole run (default {MEM_MAX_MIB})",
    )
    ap.add_argument(
        "--cache-mb",
        type=int,
        default=LIMITS["cache_mb"],
        help=f"VapourSynth frame cache per process in MB (default {LIMITS['cache_mb']})",
    )
    ap.add_argument(
        "--requests",
        type=int,
        default=LIMITS["requests"],
        help=f"vspipe concurrent frame requests (default {LIMITS['requests']})",
    )
    args = ap.parse_args(argv)
    LIMITS.update(cache_mb=args.cache_mb, requests=args.requests)
    for size in args.sizes:
        if size not in SIZES:
            ap.error(f"unknown size {size}; use {','.join(SIZES)}")

    # memory failsafes (tools/memguard.py): refuse when short, cap, watchdog
    short = memguard.preflight()
    if short is not None:
        print(f"Can't run the breakdown: {short}", file=sys.stderr)
        return 2
    script = [
        sys.executable,
        str(Path(__file__).resolve()),
        *(sys.argv[1:] if argv is None else argv),
    ]
    why = memguard.reexec_capped(script, args.mem_max, "perf")  # returns only when not re-run
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    memguard.Watchdog(label="breakdown").start()

    problems = preflight(args)
    if problems:
        print("Can't run the breakdown:", *problems, sep="\n  ", file=sys.stderr)
        return 2

    runs = plan(args)
    done: list[Run] = []
    for i, run in enumerate(runs, 1):
        w, h = SIZES[run.size]
        # keep long sizes short: same work per run at every size
        frames = max(48, int(args.frames * (1920 * 1080) / (w * h)))
        label = (
            f"[{i}/{len(runs)}] {run.host} {run.stage} {run.size}p "
            f"{run.model or ''} {run.variant or ''} t={run.gpu_thread or '-'} "
            f"layers={'on' if run.layers else 'off'} {run.target}"
        )
        print(label, file=sys.stderr, flush=True)
        if run.host == "vspipe":
            run_vspipe(run, frames, args.filter_time, args.timeout)
        else:
            run_mpv(run, int(frames * multiplier(run)), args.timeout)
        print(
            f"    -> {run.error or f'{run.out_fps} fps out, SM {run.sm_pct}%'}",
            file=sys.stderr,
            flush=True,
        )
        done.append(run)

    print(table(done))
    if args.json:
        meta = {
            "when": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "src_fps": str(SRC),
            "argv": sys.argv[1:],
        }
        args.json.write_text(
            json.dumps({"meta": meta, "runs": [asdict(r) for r in done]}, indent=1) + "\n"
        )
    return 0 if all(r.error is None for r in done) else 1


if __name__ == "__main__":
    sys.exit(main())
