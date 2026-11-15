# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""One arm of the driver frame-doubling trial through ButterEye's core.

The real core plays the clip in a throw-away profile (temporary XDG folders; the
user's models, plugins, Python folder and TensorRT engines are linked read-only and
the speed-test results copied), with mpv fullscreen and unscaled on one screen. The
environment and the extra mpv options of the arm go through ``LiveOptions``
(``env`` = this environment plus the arm's variables; ``extra_args`` after
ButterEye's own flags, so a ``--video-sync`` given here wins).

Records into ``OUT/<name>/`` the same files as ``nvtrial_probe.py`` (mpv's counters
over IPC, ``nvidia-smi``, the driver layer's log, optional grim captures) plus what
the session decided (engine, model, target, multiplier), ``pmon.txt`` (every process
on the GPU, once a second), the machine's pressure-stall totals and mpv's CPU time
in each IPC sample, with ``--mango`` MangoHud's log of mpv's own presents, and with
``--probe DIR`` the timing layer's per-present log (``frames.csv``), the layer placed
below the driver's present layer so it times the driver's frames too.

With ``--timing`` the layer also asks for present timing (``T`` rows: when each
present's first pixel left for the screen), and ``--probe-top DIR`` adds a renamed
copy of the layer above the driver's layer, which logs mpv's own presents to
``top.csv``; ``nvtrial_screen.py`` reads both.

usage: nvtrial_run.py NAME [--set K=V ...] [--unset K ...] [--seconds 60]
       [--grim N] [--mango] [--probe DIR [--timing] [--probe-top DIR]]
       [--screen DP-1] [--clip FILE] [--keep-audio] [--owner-config]
       -- [mpv options]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
import nvtrial_probe as probe  # noqa: E402

CONFIG = """schema_version = 1

[general]

[[profiles]]
id = "simple"
name = "ButterEye"
backend = "auto"
target = "display"
sc_threshold = 0.12

[[rules]]
profile = "simple"
match = {}
"""


def private_profile() -> Path:
    real = {k: Path(os.environ.get(v) or Path.home() / d) for k, v, d in (
        ("data", "XDG_DATA_HOME", ".local/share"), ("cache", "XDG_CACHE_HOME", ".cache"),
        ("state", "XDG_STATE_HOME", ".local/state"))}  # fmt: skip
    root = Path(tempfile.mkdtemp(prefix="be-nvtrial-", dir=os.environ["XDG_RUNTIME_DIR"]))
    for name in ("config", "data", "cache", "state"):
        (root / name / "buttereye").mkdir(parents=True)
        os.environ[f"XDG_{name.upper()}_HOME"] = str(root / name)
    for sub in ("models", "plugins", "python"):
        (root / "data/buttereye" / sub).symlink_to(real["data"] / "buttereye" / sub)
    (root / "cache/buttereye/engines").symlink_to(real["cache"] / "buttereye/engines")
    bench = real["state"] / "buttereye/bench.json"
    if bench.exists():
        shutil.copy(bench, root / "state/buttereye/bench.json")
    (root / "config/buttereye/config.toml").write_text(os.environ.get("NVTRIAL_CONFIG") or CONFIG)
    return root


def clip_size(clip: Path) -> tuple[int, int]:
    """The clip's frame size from the JSON ``nvtrial_clip.py`` wrote next to it."""
    try:
        meta = json.loads(clip.with_suffix(".json").read_text())
        return int(meta["width"]), int(meta["height"])
    except OSError, ValueError, KeyError:
        return 1920, 1080


def system_sample(pid: int | None) -> dict[str, object]:
    """Machine load at this instant: pressure-stall totals (microseconds stalled,
    "some" and "full"), load average, and the CPU time mpv has used (seconds)."""
    out: dict[str, object] = {"wall": time.time()}
    for res in ("cpu", "memory", "io"):
        with contextlib.suppress(OSError):
            for ln in Path(f"/proc/pressure/{res}").read_text().splitlines():
                kind, *fields = ln.split()
                vals = dict(f.split("=") for f in fields)
                out[f"psi_{res}_{kind}"] = int(vals["total"])
    with contextlib.suppress(OSError):
        out["load1"] = float(Path("/proc/loadavg").read_text().split()[0])
    if pid is not None:
        with contextlib.suppress(OSError):
            st = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            out["mpv_cpu_s"] = (int(st[11]) + int(st[12])) / os.sysconf("SC_CLK_TCK")
    return out


def arm_env(ns: argparse.Namespace, out: Path) -> dict[str, str]:
    """The environment of one run: the arm's variables, the logs, the probes."""
    env = dict(os.environ)
    for k in ns.unset:
        env.pop(k, None)
    for kv in ns.set:
        k, v = kv.split("=", 1)
        env[k] = v
    env["NVPRESENT_LOG_FILE"] = str(out / "nvpresent.log")
    env["NVPRESENT_LOG_LEVEL"] = "3"
    env["VK_LOADER_DEBUG"] = "layer"
    if ns.probe:
        # the pass-through timing layer, placed below the driver's present layer so it
        # sees every present that reaches the driver (mpv's and the driver's own)
        layers = [x for x in env.get("VK_INSTANCE_LAYERS", "").split(":") if x]
        layers = [x for x in layers if x != "VK_LAYER_BUTTEREYE_frames"]
        if "VK_LAYER_NV_present" not in layers:
            layers.append("VK_LAYER_NV_present")
        env["VK_INSTANCE_LAYERS"] = ":".join([*layers, "VK_LAYER_BUTTEREYE_frames"])
        env["VK_ADD_LAYER_PATH"] = ns.probe
        env["BUTTEREYE_FRAMES_LOG"] = str(out / "frames.csv")
        env["BUTTEREYE_FRAMES_QUIET"] = "1"
        if ns.timing:
            # present timing (first pixel out) on the swapchain below the driver's layer:
            # the layer's k path at k=1 with no images added passes every present
            # through and adds only its present id and timing query
            env |= {"BUTTEREYE_FRAMES_KMAX": "3", "BUTTEREYE_FRAMES": "1",
                    "BUTTEREYE_FRAMES_IMAGES": "0"}  # fmt: skip
        if ns.probe_top:
            # a renamed copy of the layer (its variables read BUTTEREYE_FRAMEZ_*) above
            # the driver's layer: the CPU times of mpv's own presents
            i = env["VK_INSTANCE_LAYERS"].split(":").index("VK_LAYER_NV_present")
            names = env["VK_INSTANCE_LAYERS"].split(":")
            env["VK_INSTANCE_LAYERS"] = ":".join([*names[:i], "VK_LAYER_BUTTEREYE_top", *names[i:]])
            env["VK_ADD_LAYER_PATH"] = f"{ns.probe}:{ns.probe_top}"
            env["BUTTEREYE_FRAMEZ_LOG"] = str(out / "top.csv")
            env["BUTTEREYE_FRAMEZ_QUIET"] = "1"
    if ns.mango:
        # MangoHud sits above the driver's layer: it logs mpv's own presents
        env["MANGOHUD"] = "1"
        env["MANGOHUD_CONFIG"] = (
            "fps_only,position=bottom-right,autostart_log=1,log_interval=0,"
            f"log_duration={int(ns.settle + ns.seconds + 30)},output_folder={out}"
        )
    return env


def mpv_args(ns: argparse.Namespace, out: Path, mpv_extra: list[str]) -> tuple[str, ...]:
    """mpv's options after ButterEye's own (these win)."""
    return (
        *(() if ns.owner_config else ("--no-config",)), "--vo=gpu-next", "--gpu-api=vulkan",
        "--gpu-context=waylandvk", "--interpolation=no",
        *(() if ns.keep_audio else ("--ao=null",)), "--fs", f"--fs-screen-name={ns.screen}",
        "--video-unscaled=yes", "--osd-level=0", "--no-osc", "--no-resume-playback",
        "--save-position-on-quit=no", "--title=ButterEye trial (closes itself)",
        f"--log-file={out / 'mpv.log'}", *mpv_extra,
    )  # fmt: skip


async def run(ns: argparse.Namespace, mpv_extra: list[str], out: Path, root: Path) -> dict:
    from buttereye.core import paths as paths_mod
    from buttereye.core.api import ButterEye, DetachPolicy
    from buttereye.core.mpvctl import session as live

    env = arm_env(ns, out)
    core = await ButterEye.open(paths_mod.resolve())
    reg = live.registry(core._ctx)
    reg.options.env = env
    reg.options.extra_args = mpv_args(ns, out, mpv_extra)
    facts: dict[str, object] = {
        "cmd_extra": list(reg.options.extra_args),
        "env_changes": {"set": ns.set, "unset": ns.unset},
    }
    smi = subprocess.Popen(
        ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,power.draw,clocks.gr",
         "--format=csv,noheader,nounits", "-lms", "100"],
        stdout=open(out / "smi.csv", "wb"), stderr=subprocess.DEVNULL)  # fmt: skip
    # every process on the GPU, once a second (SM and memory share)
    pmon = subprocess.Popen(
        ["nvidia-smi", "pmon", "-s", "um", "-d", "1", "-o", "T"],
        stdout=open(out / "pmon.txt", "wb"), stderr=subprocess.DEVNULL)  # fmt: skip
    facts["wall_start"] = time.time()
    pid = None
    try:
        sid = await core.play(Path(ns.clip), profile_id=None).result()
        held = reg.sessions[sid]
        pid = held.pid
        facts["pid"] = pid
        sock = str(held.socket)
        ipc = None
        for _ in range(100):
            with contextlib.suppress(OSError):
                ipc = probe.Ipc(sock)
                break
            await asyncio.sleep(0.1)
        assert ipc is not None
        # wait until the filter is active and steady
        t0 = time.monotonic()
        decided = None
        timeline = []
        while time.monotonic() - t0 < ns.settle:
            snap = next((x for x in await core.sessions() if x.sid == sid), None)
            if snap is None:
                break
            line = (
                snap.filter.value,
                getattr(snap.backend, "value", None),
                snap.model,
                str(snap.target_fps),
                snap.multiplier,
                getattr(snap.health, "value", None),
            )
            if not timeline or timeline[-1][1] != line:
                timeline.append((round(time.monotonic() - t0, 1), line))
            if snap.filter.value == "active" and decided is None:
                decided = time.monotonic()
            if decided is not None and time.monotonic() - decided > ns.warmup:
                break
            await asyncio.sleep(0.5)
        facts["timeline"] = timeline
        facts["maps"] = probe.layer_maps(pid)
        facts["window"] = probe.window_facts(pid)
        start = ipc.snapshot()
        samples = [{"t": 0.0, **start, **system_sample(pid)}]
        t_start = time.monotonic()
        facts["wall_steady"] = time.time()
        facts["mono_steady"] = t_start  # CLOCK_MONOTONIC, the timing layer's clock
        if ns.grim:
            win = facts["window"] or {}
            vw, vh = clip_size(Path(ns.clip))
            mx, my = win.get("mon_x", 0) or 0, win.get("mon_y", 0) or 0  # type: ignore[union-attr]
            mw, mh = win.get("mon_w", 3440) or 3440, win.get("mon_h", 1440) or 1440  # type: ignore[union-attr]
            region = f"{mx + (mw - vw) // 2},{my + (mh - vh) // 2} {vw}x340"
            facts["grim_region"] = region
            with tempfile.TemporaryDirectory(dir=out) as td:
                g = await asyncio.to_thread(
                    probe.grim_samples, region, ns.grim, (slice(0, 60), slice(310, 330)), Path(td)
                )
            import numpy as np

            np.savez_compressed(out / "grim.npz", **g)
            facts["grim_n"] = int(len(g["stamps"]))
            samples.append({"t": time.monotonic() - t_start, **ipc.snapshot()})
        while time.monotonic() - t_start < ns.seconds:
            await asyncio.sleep(1.0)
            snap = next((x for x in await core.sessions() if x.sid == sid), None)
            state = (snap.filter.value, getattr(snap.health, "value", None)) if snap else None
            samples.append({"t": time.monotonic() - t_start, "state": state, **ipc.snapshot(),
                            **system_sample(pid)})  # fmt: skip
        end = samples[-1]
        facts["window_end"] = probe.window_facts(pid)
        facts["deltas"] = {c: (end.get(c) or 0) - (start.get(c) or 0) for c in probe.COUNTERS}
        facts["steady_s"] = round(float(end["t"]), 1)  # type: ignore[arg-type]
        facts["last"] = {k: end.get(k) for k in probe.PROPS}
        snap = next((x for x in await core.sessions() if x.sid == sid), None)
        if snap is not None:
            facts["final"] = {"filter": snap.filter.value,
                              "backend": getattr(snap.backend, "value", None),
                              "model": snap.model, "target": str(snap.target_fps),
                              "multiplier": snap.multiplier,
                              "health": getattr(snap.health, "value", None)}  # fmt: skip
        vf = ipc.cmd("get_property", "vf")
        facts["vf"] = json.dumps(vf)[:1500]
        with open(out / "ipc.jsonl", "w") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)  # the player this script started
        for p in (smi, pmon):
            p.terminate()
            p.wait()
    logs = root / "state/buttereye/logs"
    for lg in sorted(logs.glob("*.log")) if logs.exists() else []:
        shutil.copy(lg, out / f"be-{lg.name}")
    return facts


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--unset", action="append", default=[])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--settle", type=float, default=60.0)
    ap.add_argument("--warmup", type=float, default=4.0)
    ap.add_argument("--grim", type=int, default=0)
    ap.add_argument("--mango", action="store_true", help="MangoHud log of mpv's presents")
    ap.add_argument("--probe", help="folder of the VK_LAYER_BUTTEREYE_frames manifest")
    ap.add_argument(
        "--timing",
        action="store_true",
        help="with --probe: present timing (first pixel out) below the driver's layer",
    )
    ap.add_argument("--probe-top", help="with --probe: folder of the renamed copy placed above it")
    ap.add_argument(
        "--keep-audio",
        action="store_true",
        help="play the clip's audio on the default device instead of --ao=null",
    )
    ap.add_argument(
        "--owner-config",
        action="store_true",
        help="let mpv read the user's mpv.conf (read only) instead of --no-config",
    )
    ap.add_argument("--screen", default="DP-1")
    ap.add_argument("--clip", default=str(probe.CLIP))
    return ap


def main() -> int:
    argv = sys.argv[1:]
    mpv_extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, mpv_extra = argv[:i], argv[i + 1 :]
    ap = parser()
    ns = ap.parse_args(argv)
    out = probe.OUT / ns.name
    if out.exists():
        print(f"{out} exists", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    root = private_profile()
    try:
        facts = asyncio.run(run(ns, mpv_extra, out, root))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    nvlog = out / "nvpresent.log"
    facts["nvpresent_lines"] = (
        nvlog.read_text(errors="replace").splitlines()[:40] if nvlog.exists() else []
    )
    (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str))
    print(
        json.dumps(
            {k: facts.get(k) for k in ("timeline", "final", "maps", "deltas", "last", "grim_n")},
            default=str,
        )[:4000]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
