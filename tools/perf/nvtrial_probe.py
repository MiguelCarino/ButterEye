# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""One mpv run for the driver frame-doubling trial: engagement facts and cheap samples.

Starts mpv directly (``--no-config`` plus the options given), fullscreen and unscaled
on one screen, with the driver layer's log and the Vulkan loader's layer log on, and
records into ``OUT/<name>/``:

- ``mpv.log`` (mpv's own log), ``stderr.txt`` (loader messages), ``nvpresent.log``;
- ``maps.txt``: the Vulkan layer libraries mapped into mpv (``/proc/<pid>/maps``);
- ``ipc.jsonl``: mpv's counters once a second (``frame-drop-count``,
  ``vo-delayed-frame-count``, ``display-fps``, ...);
- ``smi.csv``: GPU load, clocks and power every 100 ms (``nvidia-smi``);
- ``grim.npz`` when ``--grim N``: N lossless region captures of the video area taken
  as fast as ``grim`` allows, reduced to the barcode row and a band through the bar;
- ``facts.json``: the summary (layers mapped, present mode, engagement lines, counter
  deltas over the steady part, window placement).

The runner never changes desktop settings. It only signals the mpv it started.

usage: nvtrial_probe.py NAME [--set K=V ...] [--unset K ...] [--seconds 20]
       [--grim 300] [--screen DP-1] [--set-override-at T:VALUE] -- [mpv options]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

OUT = Path.home() / ".cache/buttereye-dev/nvtrial/runs"
CLIP = Path.home() / ".cache/buttereye-dev/nvtrial/bar1080.mkv"
PROPS = (
    "playback-time", "frame-drop-count", "decoder-frame-drop-count", "mistimed-frame-count",
    "vo-delayed-frame-count", "display-fps", "estimated-display-fps", "vsync-jitter",
    "avsync", "estimated-vf-fps", "video-sync", "display-fps-override", "speed",
    "audio-speed-correction", "video-speed-correction",
)  # fmt: skip
COUNTERS = ("frame-drop-count", "decoder-frame-drop-count", "mistimed-frame-count",
            "vo-delayed-frame-count")  # fmt: skip


class Ipc:
    def __init__(self, path: str) -> None:
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(2.0)
        self.s.connect(path)
        self.buf = b""
        self.rid = 0

    def cmd(self, *args: object) -> object:
        self.rid += 1
        rid = self.rid
        self.s.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        end = time.monotonic() + 2.0
        while time.monotonic() < end:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                with contextlib.suppress(ValueError):
                    j = json.loads(line)
                    if j.get("request_id") == rid:
                        return j.get("data") if j.get("error") == "success" else None
            try:
                chunk = self.s.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            self.buf += chunk
        return None

    def snapshot(self) -> dict[str, object]:
        return {p: self.cmd("get_property", p) for p in PROPS}


def window_facts(pid: int) -> dict[str, object] | None:
    """Where the compositor put mpv (read only)."""
    try:
        clients = json.loads(
            subprocess.run(
                ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=5
            ).stdout
        )
        mons = json.loads(
            subprocess.run(
                ["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=5
            ).stdout
        )
    except OSError, ValueError, subprocess.SubprocessError:
        return None
    byid = {m["id"]: m for m in mons}
    for c in clients:
        if c.get("pid") == pid:
            m = byid.get(c.get("monitor"), {})
            return {"monitor": m.get("name"), "refresh": m.get("refreshRate"),
                    "vrr_monitor": m.get("vrr"), "at": c.get("at"), "size": c.get("size"),
                    "fullscreen": c.get("fullscreen"), "mon_x": m.get("x"), "mon_y": m.get("y"),
                    "mon_w": m.get("width"), "mon_h": m.get("height")}  # fmt: skip
    return None


def layer_maps(pid: int) -> list[str]:
    try:
        text = Path(f"/proc/{pid}/maps").read_text()
    except OSError:
        return []
    libs = {ln.split()[-1] for ln in text.splitlines() if ln.count(" ") >= 5}
    keep = ("present", "dlssnr", "MangoHud", "VkLayer", "vkbasalt", "nvidia-ngx", "libcuda")
    return sorted(x for x in libs if any(k in x for k in keep))


def grim_samples(
    region: str, n: int, rows: tuple[slice, slice], tmp: Path
) -> dict[str, np.ndarray]:
    """n captures of ``region`` (x,y wxh); keeps two row bands of each as grey arrays."""
    bands: list[list[np.ndarray]] = [[], []]
    stamps = []
    path = tmp / "g.ppm"
    for _ in range(n):
        t = time.monotonic()
        r = subprocess.run(["grim", "-t", "ppm", "-g", region, str(path)], capture_output=True)
        if r.returncode != 0:
            continue
        data = path.read_bytes()
        # P6\n<w> <h>\n255\n
        parts = data.split(b"\n", 3)
        w, h = (int(v) for v in parts[1].split())
        img = np.frombuffer(parts[3], np.uint8).reshape(h, w, 3)
        y = (0.2126 * img[..., 0] + 0.7152 * img[..., 1] + 0.0722 * img[..., 2]).astype(np.float32)
        for i, sl in enumerate(rows):
            bands[i].append(y[sl].mean(axis=0) if i == 1 else y[sl])
        stamps.append((t, time.monotonic()))
    path.unlink(missing_ok=True)
    return {"top": np.array(bands[0], np.uint8), "barrow": np.array(bands[1], np.float32),
            "stamps": np.array(stamps)}  # fmt: skip


def summarise_log(text: str) -> dict[str, object]:
    pick = []
    for ln in text.splitlines():
        if re.search(
            r"Picked surface|present mode|fifo|mailbox|FIFO|MAILBOX|swapchain|"
            r"Swapchain|surface format|Unsupported|passthrough",
            ln,
            re.I,
        ):
            pick.append(ln.strip()[:240])
    return {"lines": pick[:40]}


def main() -> int:
    argv = sys.argv[1:]
    mpv_extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, mpv_extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--unset", action="append", default=[])
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--warmup", type=float, default=4.0)
    ap.add_argument("--grim", type=int, default=0)
    ap.add_argument("--screen", default="DP-1")
    ap.add_argument("--clip", default=str(CLIP))
    ap.add_argument("--log-level", default="4")
    ap.add_argument("--no-loader-debug", action="store_true")
    ap.add_argument("--set-override-at", default="")
    ap.add_argument("--no-smi", action="store_true")
    ns = ap.parse_args(argv)
    out = OUT / ns.name
    if out.exists():
        print(f"{out} exists", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    env = dict(os.environ)
    for k in ns.unset:
        env.pop(k, None)
    for kv in ns.set:
        k, v = kv.split("=", 1)
        env[k] = v
    env["NVPRESENT_LOG_FILE"] = str(out / "nvpresent.log")
    if ns.log_level != "none":
        env["NVPRESENT_LOG_LEVEL"] = ns.log_level
    if not ns.no_loader_debug:
        env["VK_LOADER_DEBUG"] = "layer"
    sock = tempfile.mktemp(prefix="nvtrial-", suffix=".sock", dir=os.environ["XDG_RUNTIME_DIR"])
    cmd = [
        "mpv", "--no-config", f"--input-ipc-server={sock}", f"--log-file={out / 'mpv.log'}",
        "--vo=gpu-next", "--gpu-api=vulkan", "--gpu-context=waylandvk", "--interpolation=no",
        "--ao=null", "--fs", f"--fs-screen-name={ns.screen}", "--video-unscaled=yes",
        "--osd-level=0", "--no-osc", "--loop-file=inf", "--title=ButterEye trial (closes itself)",
        "--msg-level=all=v", *mpv_extra, ns.clip,
    ]  # fmt: skip
    meta = {"name": ns.name, "cmd": cmd,
            "env_changes": {"set": ns.set, "unset": ns.unset,
                            "VK_INSTANCE_LAYERS": env.get("VK_INSTANCE_LAYERS")},
            "started": time.strftime("%Y-%m-%d %H:%M:%S")}  # fmt: skip
    err = open(out / "stderr.txt", "wb")
    proc = subprocess.Popen(cmd, env=env, stdout=err, stderr=err, start_new_session=True)
    smi = None
    if not ns.no_smi:
        smi = subprocess.Popen(
            ["nvidia-smi", "--query-gpu=timestamp,utilization.gpu,power.draw,clocks.gr",
             "--format=csv,noheader,nounits", "-lms", "100"],
            stdout=open(out / "smi.csv", "wb"), stderr=subprocess.DEVNULL)  # fmt: skip
    facts: dict[str, object] = {}
    try:
        ipc = None
        for _ in range(100):
            if proc.poll() is not None:
                break
            with contextlib.suppress(OSError):
                ipc = Ipc(sock)
                break
            time.sleep(0.1)
        if ipc is None:
            facts["error"] = f"no IPC; mpv rc={proc.poll()}"
            return 1
        t0 = time.monotonic()
        while time.monotonic() - t0 < 15:
            pt = ipc.cmd("get_property", "playback-time")
            if isinstance(pt, (int, float)) and pt > 0.5:
                break
            time.sleep(0.2)
        time.sleep(ns.warmup)
        facts["maps"] = layer_maps(proc.pid)
        facts["window"] = window_facts(proc.pid)
        (out / "maps.txt").write_text("\n".join(facts["maps"]))  # type: ignore[arg-type]
        samples = []
        start = ipc.snapshot()
        samples.append({"t": 0.0, **start})
        t_start = time.monotonic()
        override_done = not ns.set_override_at
        grim_data = None
        if ns.grim:
            win = facts["window"] or {}
            vw, vh = 1920, 1080
            mx, my = win.get("mon_x", 0) or 0, win.get("mon_y", 0) or 0  # type: ignore[union-attr]
            mw, mh = win.get("mon_w", 3440) or 3440, win.get("mon_h", 1440) or 1440  # type: ignore[union-attr]
            x, y = mx + (mw - vw) // 2, my + (mh - vh) // 2
            region = f"{x},{y} {vw}x340"
            facts["grim_region"] = region
            with tempfile.TemporaryDirectory(dir=out) as td:
                grim_data = grim_samples(region, ns.grim, (slice(0, 60), slice(310, 330)), Path(td))
            samples.append({"t": time.monotonic() - t_start, **ipc.snapshot()})
        while time.monotonic() - t_start < ns.seconds:
            time.sleep(1.0)
            if not override_done and time.monotonic() - t_start >= float(
                ns.set_override_at.split(":")[0]
            ):
                val = float(ns.set_override_at.split(":")[1])
                facts["override_set_reply"] = ipc.cmd("set_property", "display-fps-override", val)
                override_done = True
            samples.append({"t": time.monotonic() - t_start, **ipc.snapshot()})
            if proc.poll() is not None:
                break
        end = samples[-1]
        facts["deltas"] = {
            c: (end.get(c) or 0) - (start.get(c) or 0)  # type: ignore[operator]
            for c in COUNTERS
        }
        facts["steady_s"] = round(end["t"], 1)  # type: ignore[arg-type]
        facts["last"] = {k: end.get(k) for k in PROPS}
        with open(out / "ipc.jsonl", "w") as f:
            for s in samples:
                f.write(json.dumps(s) + "\n")
        if grim_data is not None:
            np.savez_compressed(out / "grim.npz", **grim_data)
            facts["grim_n"] = int(len(grim_data["stamps"]))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
        if smi is not None:
            smi.terminate()
            smi.wait()
        err.close()
        with contextlib.suppress(OSError):
            os.unlink(sock)
        nvlog = out / "nvpresent.log"
        nvtext = nvlog.read_text(errors="replace") if nvlog.exists() else ""
        facts["nvpresent_lines"] = nvtext.splitlines()[:60]
        mpvlog = (out / "mpv.log").read_text(errors="replace") if (out / "mpv.log").exists() else ""
        facts["mpv_log"] = summarise_log(mpvlog)
        stderr = (out / "stderr.txt").read_text(errors="replace")
        facts["stderr_nv"] = [
            ln
            for ln in stderr.splitlines()
            if re.search(r"NvPresent|nvpresent|Smooth Motion|dlssnr", ln)
        ][:30]
        (out / "facts.json").write_text(json.dumps(meta | facts, indent=1, default=str))
        print(
            json.dumps(
                {
                    k: facts.get(k)
                    for k in ("maps", "window", "deltas", "last", "nvpresent_lines", "grim_n")
                },
                default=str,
            )[:4000]
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
