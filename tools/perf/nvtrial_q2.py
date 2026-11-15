# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Picture quality of "an engine to 3x, then the driver doubles" (driver trial, Q2).

Real moments of seven frames (``truth.py --around --save-set``) with the sources
taken six apart, as m0u scored its cascade: frames 0 and 6 go in, frames 1 to 5 are
the truth, and every in-between frame is scored (m0u's figures are the mean over
all five positions; position 3 alone is reported too).

Two arms reach x6 through the driver:

- ``E``: mvgpu x3 makes 2 and 4; the clip 0, 2, 4, 6, 4, 2, 0, ... plays through mpv
  with the driver's frame generation on, so the driver makes 1, 3 and 5;
- ``B``: the same with RIFE x3 making 2 and 4.

The captures come from the screen (``grim``), so they are in the screen's colour
path, not the file's. To compare like with like, the truth and the other engines'
frames are shown as still frames in the same clip and captured the same way:

- ``y1``..``y5``: the real frames;
- ``x6_*``: RIFE x6;
- ``c_*``: the shipped cascade, RIFE x2 then mvgpu x3;
- ``m6_*``: mvgpu x6;
- ``d_1``, ``d_3``, ``d_5``: RIFE x3 then mvgpu x2 (its 2 and 4 are RIFE's, shown in
  arm B's cycle).

Every frame carries a strip above the picture (64 rows): a barcode of 7 bits of
moment number, 5 bits of frame code, a white and a black calibration block, and a
bar whose position belongs to the frame. Arm E's cycle has the bar at steps 0..3,
arm B's at 6..9 and the stills at 12 and up, so a frame the driver makes between
two frames shows the bar half-way between their steps: at 0.5, 1.5 and 2.5 it is
arm E's 1, 3 and 5, at 6.5, 7.5 and 8.5 arm B's. Frames the driver makes between
unrelated frames (a cycle's end and the next stills) land elsewhere. A still or a
cycle frame is used only when two captures of it agree (a made frame cannot match a
real capture), and made frames only when the moment bits are crisp.

Steps (files under ``~/.cache/buttereye-dev/nvtrial/q2/``):

  make   (system Python with VapourSynth; GPU) every engine's frames
  clip   (CPU) the playback clip of one batch, x264 lossless, 72000/1001 fps
  play   (GPU, real window) plays it with the driver on and captures the screen
  score  scores the captures (truth.py's metrics, luma) and the file frames
  pool   the scored batches together, with paired differences

usage: nvtrial_q2.py make  --batch NAME --start I --count N [--mvgpu SO]
       nvtrial_q2.py clip  --batch NAME
       nvtrial_q2.py play  --batch NAME [--run hdr] [--screen DP-1] [-- mpv options]
       nvtrial_q2.py score --batch NAME [--run hdr] [--keep]
       nvtrial_q2.py pool  --batch NAME,NAME,... [--run hdr]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import truth  # noqa: E402

ROOT = Path.home() / ".cache/buttereye-dev/nvtrial/q2"
SET = Path.home() / ".cache/buttereye-dev/truthset7"
W, H = 1920, 1080
STRIP = 64
BLOCK = 60
MOMENT_BITS = 7
CODE_BITS = 5
POS = (1, 2, 3, 4, 5)
# frame name -> bar step; the code is the index in this table
STEPS: dict[str, int] = {"e0": 0, "e2": 1, "e4": 2, "e6": 3, "b0": 6, "b2": 7, "b4": 8, "b6": 9}
STILLS = (
    [f"y{j}" for j in POS] + [f"x6_{j}" for j in POS] + [f"c_{j}" for j in POS]
    + [f"m6_{j}" for j in POS] + ["d_1", "d_3", "d_5"]
)  # fmt: skip
for _i, _n in enumerate(STILLS):
    STEPS[_n] = 12 + _i
CODES = {n: i for i, n in enumerate(STEPS)}
BY_CODE = {i: n for n, i in CODES.items()}
CYCLE = {"E": ("e0", "e2", "e4", "e6", "e4", "e2"), "B": ("b0", "b2", "b4", "b6", "b4", "b2")}
# driver frames: arm -> {bar position: truth position}
MADE = {"E": {0.5: 1, 1.5: 3, 2.5: 5}, "B": {6.5: 1, 7.5: 3, 8.5: 5}}
CYCLES = 32  # each arm's cycle 32 times (2.7 s at 72 fps)
HOLD = 22  # each still 0.3 s
LEAD = 144  # 2 s of grey before the first moment
FPS = "72000/1001"
G_MAX = 8  # driver frames kept per moment, arm and position
REAL_MAX = 3  # captures kept of each still or cycle frame
BAR_X, BAR_STEP, BAR_W = 880, 28, 12
BAR_SPAN = min(W - BAR_X, (max(STEPS.values()) + 2) * BAR_STEP)


# ---------------------------------------------------------------- make


def make(ns: argparse.Namespace) -> int:
    import vapoursynth as vs

    core = vs.core
    files = sorted(SET.glob("*.npz"), key=truth.set_key)[ns.start : ns.start + ns.count]
    out = ROOT / ns.batch
    out.mkdir(parents=True, exist_ok=False)
    targs = argparse.Namespace(
        trt_models=truth._data_home() / "models" / "vsmlrt",
        trt_engines=Path.home() / ".cache/buttereye-dev/trt-engines",
        mvgpu=None,
    )
    vsmlrt = truth._vsmlrt(core, vs, targs)
    truth._load(core, "mvgpu", Path(ns.mvgpu) if ns.mvgpu else truth.PLUGINS / "libmvgpu.so")
    backend = vsmlrt.Backend.TRT(fp16=True, num_streams=1,
                                 engine_folder=os.fspath(targs.trt_engines))  # fmt: skip

    def rife(clip: object, multi: int) -> object:
        rgb = core.resize.Spline36(clip, format=vs.RGBS, matrix_in_s="709", range_in_s="limited")
        o = vsmlrt.RIFE(rgb, multi=multi, model=vsmlrt.RIFEModel.v4_26, backend=backend,
                        _implementation=2)  # fmt: skip
        return core.resize.Spline36(o, format=vs.YUV420P8, matrix_s="709", range_s="limited",
                                    dither_type="none")  # fmt: skip

    def mv(clip: object, k: int) -> object:
        return core.mvgpu.Interpolate(clip, fps_num=clip.fps_num * k,  # type: ignore[attr-defined]
                                      fps_den=clip.fps_den)  # type: ignore[attr-defined]  # fmt: skip

    def planes(clip: object, n: int) -> tuple[np.ndarray, ...]:
        f = clip.get_frame(n)  # type: ignore[attr-defined]
        return tuple(np.asarray(f[p]).copy() for p in range(3))

    names, checks = [], []
    for i, path in enumerate(files):
        d = np.load(path)
        src = [tuple(d[k][j] for k in "yuv") for j in range(7)]
        pair = truth.as_clip(core, vs, np, [src[0], src[6]])
        x3, x6, mv3, mv6 = rife(pair, 3), rife(pair, 6), mv(pair, 3), mv(pair, 6)
        casc, x3mv2 = mv(rife(pair, 2), 3), mv(x3, 2)
        fr: dict[str, tuple[np.ndarray, ...]] = {"f0": src[0], "f6": src[6]}
        for j in POS:
            fr[f"y{j}"] = src[j]
            fr[f"x6_{j}"] = planes(x6, j)
            fr[f"c_{j}"] = planes(casc, j)
            fr[f"m6_{j}"] = planes(mv6, j)
            fr[f"d_{j}"] = planes(x3mv2, j)
        fr["e2"], fr["e4"] = planes(mv3, 1), planes(mv3, 2)
        fr["r2"], fr["r4"] = planes(x3, 1), planes(x3, 2)
        arrays = {f"{k}_{p}": v[j] for k, v in fr.items() for j, p in enumerate("yuv")}
        np.savez(out / f"m{i:03d}.npz", **arrays)
        # RIFE x3 then mvgpu x2 shows RIFE's own frames at 2 and 4 (if mvgpu passes them on)
        checks.append({
            "moment": path.name,
            "d2_vs_r2": int(np.abs(fr["d_2"][0].astype(int) - fr["r2"][0]).max()),
            "d4_vs_r4": int(np.abs(fr["d_4"][0].astype(int) - fr["r4"][0]).max()),
        })  # fmt: skip
        names.append(path.name)
        print(f"{i} {path.name} {checks[-1]}", flush=True)
    (out / "moments.json").write_text(json.dumps(names, indent=1))
    (out / "make.json").write_text(json.dumps({"mvgpu": ns.mvgpu, "checks": checks}, indent=1))
    return 0


# ---------------------------------------------------------------- clip


def strip_bits(moment: int, code: int) -> list[int]:
    bits = [(moment >> (MOMENT_BITS - 1 - b)) & 1 for b in range(MOMENT_BITS)]
    bits += [(code >> (CODE_BITS - 1 - b)) & 1 for b in range(CODE_BITS)]
    return bits + [1, 0]  # calibration: white, black


def with_strip(fr: tuple[np.ndarray, ...], moment: int, name: str) -> bytes:
    y = np.full((H + STRIP, W), 16, np.uint8)
    y[STRIP:] = fr[0]
    for b, bit in enumerate(strip_bits(moment, CODES[name])):
        y[2 : STRIP - 2, b * BLOCK + 2 : (b + 1) * BLOCK - 2] = 235 if bit else 16
    x = BAR_X + STEPS[name] * BAR_STEP
    y[8 : STRIP - 8, x : x + BAR_W] = 235
    u = np.full(((H + STRIP) // 2, W // 2), 128, np.uint8)
    v = u.copy()
    u[STRIP // 2 :] = fr[1]
    v[STRIP // 2 :] = fr[2]
    return y.tobytes() + u.tobytes() + v.tobytes()


def frames_of(d: object) -> dict[str, tuple[np.ndarray, ...]]:
    """The playback frames of one moment by name (``e0``.. cycle names, stills)."""
    src = {"e0": "f0", "e2": "e2", "e4": "e4", "e6": "f6",
           "b0": "f0", "b2": "r2", "b4": "r4", "b6": "f6"}  # fmt: skip
    src |= {n: n for n in STILLS}
    return {n: tuple(d[f"{k}_{p}"] for p in "yuv") for n, k in src.items()}  # type: ignore[index]


def clip(ns: argparse.Namespace) -> int:
    out = ROOT / ns.batch
    dst = out / "play.mkv"
    if dst.exists():
        print(f"{dst} exists", file=sys.stderr)
        return 2
    cmd = [
        "nice", "-n", "19", "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{W}x{H + STRIP}", "-framerate", FPS,
        "-i", "-", "-c:v", "libx264", "-preset", "faster", "-qp", "0", "-bf", "0",
        "-refs", "6", "-g", "100000", "-threads", "6", "-color_range", "tv",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(dst),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    moments = sorted(out.glob("m*.npz"))
    # a lead-in the captures cannot read (no calibration blocks) while mpv starts
    blank = np.full((H + STRIP) * W * 3 // 2, 16, np.uint8)
    blank[(H + STRIP) * W :] = 128
    for _ in range(LEAD):
        proc.stdin.write(blank.tobytes())
    for i, path in enumerate(moments):
        fr = frames_of(np.load(path))
        enc = {n: with_strip(f, i, n) for n, f in fr.items()}
        # stills first (steps 12 up), then arm E's cycle (0..3), then arm B's (6..9):
        # the driver's frames across those joins land at 6, 3.5 and (into the next
        # moment's stills) 9.5, none of them a scored position
        for n in STILLS:
            for _ in range(HOLD):
                proc.stdin.write(enc[n])
        for arm in ("E", "B"):
            for _ in range(CYCLES):
                for n in CYCLE[arm]:
                    proc.stdin.write(enc[n])
    proc.stdin.close()
    return proc.wait()


# ---------------------------------------------------------------- play


class Ipc:
    def __init__(self, path: str) -> None:
        self.s = socket.socket(socket.AF_UNIX)
        self.s.settimeout(2.0)
        self.s.connect(path)
        self.buf = b""

    def get(self, prop: str) -> object:
        self.s.sendall((json.dumps({"command": ["get_property", prop]}) + "\n").encode())
        end = time.monotonic() + 2
        while time.monotonic() < end:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                with contextlib.suppress(ValueError):
                    j = json.loads(line)
                    if "error" in j and "event" not in j:
                        return j.get("data")
            try:
                chunk = self.s.recv(65536)
            except TimeoutError, OSError:
                return None
            if not chunk:
                return None
            self.buf += chunk
        return None


def read_strip(y: np.ndarray) -> tuple[list[float], float, float]:
    """Block means of the strip (rows 8..56 of each block), and the calibration
    white and black levels."""
    m = 10
    vals = [float(y[8 : STRIP - 8, b * BLOCK + m : (b + 1) * BLOCK - m].mean())
            for b in range(MOMENT_BITS + CODE_BITS + 2)]  # fmt: skip
    return vals[:-2], vals[-2], vals[-1]


def bar_pos(y: np.ndarray, white: float, black: float) -> float | None:
    """Where the strip's bar is, in steps."""
    row = y[12 : STRIP - 12, BAR_X : BAR_X + BAR_SPAN].mean(axis=0)
    w = np.clip(row - black - 0.15 * (white - black), 0, None)
    if w.sum() <= 0:
        return None
    cx = float((w * np.arange(w.size)).sum() / w.sum())
    return (cx - (BAR_W - 1) / 2) / BAR_STEP


def classify(y: np.ndarray) -> tuple[str, int, str, float] | None:
    """('real', moment, name, pos) for a decoded frame; ('made', moment, 'E1'.., pos)
    for a driver frame at a scored position; ('other', ...) for any other frame the
    driver made; None when unreadable."""
    vals, white, black = read_strip(y)
    if white - black < 40:
        return None
    lo, hi = black + 0.2 * (white - black), white - 0.2 * (white - black)
    bits = [1 if v >= hi else 0 if v <= lo else -1 for v in vals]
    moment = 0
    for b in bits[:MOMENT_BITS]:
        if b < 0:
            return None
        moment = (moment << 1) | b
    pos = bar_pos(y, white, black)
    if pos is None:
        return None
    if abs(pos - round(pos)) < 0.1:
        code = bits[MOMENT_BITS:]
        if -1 in code:
            return None
        name = BY_CODE.get(int("".join(str(b) for b in code), 2))
        if name is None or STEPS[name] != round(pos):
            return None
        return "real", moment, name, pos
    for arm, made in MADE.items():
        for at, j in made.items():
            if abs(pos - at) < 0.15:
                return "made", moment, f"{arm}{j}", pos
    return "other", moment, "", pos


def play(ns: argparse.Namespace, mpv_extra: list[str]) -> int:
    out = ROOT / ns.batch
    caps = out / f"caps-{ns.run}"
    caps.mkdir(exist_ok=False)
    src = out / "play.mkv"
    env = dict(os.environ)
    env |= {"NVPRESENT_ENABLE_SMOOTH_MOTION": "1", "NVPRESENT_LOG_LEVEL": "3",
            "NVPRESENT_LOG_FILE": str(out / "nvpresent.log")}  # fmt: skip
    sock = tempfile.mktemp(prefix="nvq2-", suffix=".sock", dir=os.environ["XDG_RUNTIME_DIR"])
    cmd = [
        "mpv", "--no-config", f"--input-ipc-server={sock}", f"--log-file={out / 'mpv.log'}",
        "--vo=gpu-next", "--gpu-api=vulkan", "--gpu-context=waylandvk", "--interpolation=no",
        "--hwdec=no", "--fs", f"--fs-screen-name={ns.screen}", "--video-unscaled=yes",
        "--osd-level=0", "--no-osc", "--sub=no", "--dither-depth=no", "--deband=no",
        "--video-sync=audio", "--display-fps-override=72", "--no-resume-playback",
        "--save-position-on-quit=no", "--title=ButterEye trial (closes itself)",
        *mpv_extra, str(src),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)  # fmt: skip
    from nvtrial_probe import window_facts

    counts: dict[str, int] = {}
    seen: dict[tuple[int, str], int] = {}
    stats = {"captures": 0, "unreadable": 0, "real": 0, "made": 0, "other": 0, "failed": 0}
    other_pos: list[float] = []
    made_pos: list[float] = []
    t_cap = []
    meta: dict[str, object] = {"cmd": cmd}
    try:
        ipc = None
        for _ in range(100):
            with contextlib.suppress(OSError):
                ipc = Ipc(sock)
                break
            time.sleep(0.1)
        if ipc is None:
            print("no IPC", file=sys.stderr)
            return 1
        for _ in range(100):
            pt = ipc.get("playback-time")
            if isinstance(pt, (int, float)) and pt > 0.3:
                break
            time.sleep(0.1)
        win = window_facts(proc.pid) or {}
        mx, my = win.get("mon_x") or 0, win.get("mon_y") or 0
        mw, mh = win.get("mon_w") or 3440, win.get("mon_h") or 1440
        x0, y0 = int(mx) + (int(mw) - W) // 2, int(my) + (int(mh) - H - STRIP) // 2
        region = f"{x0},{y0} {W}x{H + STRIP}"
        meta |= {"window": win, "region": region}
        while proc.poll() is None:
            t = time.monotonic()
            r = subprocess.run(["grim", "-t", "ppm", "-g", region, "-"], capture_output=True)
            t_cap.append(time.monotonic() - t)
            stats["captures"] += 1
            if r.returncode != 0 or not r.stdout:
                stats["failed"] += 1
                continue
            parts = r.stdout.split(b"\n", 3)
            w, h = (int(v) for v in parts[1].split())
            rgb = np.frombuffer(parts[3], np.uint8).reshape(h, w, 3).astype(np.float32)
            y = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
            c = classify(y)
            if c is None:
                stats["unreadable"] += 1
                continue
            kind, moment, name, pos = c
            stats[kind] += 1
            if kind == "other":
                other_pos.append(round(pos, 3))
                continue
            if kind == "made":
                made_pos.append(round(pos, 3))
            elif name in ("e0", "e6", "b0", "b6"):
                continue  # the sources are not scored
            key = (moment, name)
            limit = G_MAX if kind == "made" else REAL_MAX
            if seen.get(key, 0) >= limit:
                continue
            seen[key] = seen.get(key, 0) + 1
            np.save(caps / f"m{moment:03d}_{name}_{seen[key]}.npy",
                    np.clip(np.rint(y[STRIP:]), 0, 255).astype(np.uint8))  # fmt: skip
            counts[name] = counts.get(name, 0) + 1
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(5)
        with contextlib.suppress(OSError):
            os.unlink(sock)
    med = round(float(np.median(t_cap)) * 1000, 1) if t_cap else None
    meta |= {"stats": stats, "kept": counts, "capture_ms_median": med}
    if made_pos:
        meta["made_phase_hist"] = np.histogram(np.array(made_pos) % 1.0, bins=10,
                                               range=(0, 1))[0].tolist()  # fmt: skip
    if other_pos:
        meta["other_pos_hist"] = np.histogram(np.array(other_pos), bins=40,
                                              range=(0, 40))[0].tolist()  # fmt: skip
    nv = out / "nvpresent.log"
    meta["nvpresent"] = nv.read_text(errors="replace").splitlines()[:30] if nv.exists() else []
    (out / f"play-{ns.run}.json").write_text(json.dumps(meta, indent=1, default=str))
    print(json.dumps({k: meta[k] for k in ("stats", "capture_ms_median")}))
    return 0


# ---------------------------------------------------------------- score

# configuration -> where its frame at each position comes from, on screen:
# ("still", name) a still; ("cycle", name) a cycle frame; ("made", "E1") driver frames
SCREEN: dict[str, dict[int, tuple[str, str]]] = {
    "RIFE x6": {j: ("still", f"x6_{j}") for j in POS},
    "RIFE x2, mvgpu x3 (shipped)": {j: ("still", f"c_{j}") for j in POS},
    "mvgpu x6": {j: ("still", f"m6_{j}") for j in POS},
    "RIFE x3, mvgpu x2": {1: ("still", "d_1"), 2: ("cycle", "b2"), 3: ("still", "d_3"),
                          4: ("cycle", "b4"), 5: ("still", "d_5")},
    "E: mvgpu x3, driver x2": {1: ("made", "E1"), 2: ("cycle", "e2"), 3: ("made", "E3"),
                               4: ("cycle", "e4"), 5: ("made", "E5")},
    "B: RIFE x3, driver x2": {1: ("made", "B1"), 2: ("cycle", "b2"), 3: ("made", "B3"),
                              4: ("cycle", "b4"), 5: ("made", "B5")},
}  # fmt: skip
FILE: dict[str, dict[int, str]] = {
    "RIFE x6": {j: f"x6_{j}" for j in POS},
    "RIFE x2, mvgpu x3 (shipped)": {j: f"c_{j}" for j in POS},
    "mvgpu x6": {j: f"m6_{j}" for j in POS},
    "RIFE x3, mvgpu x2": {j: f"d_{j}" for j in POS},
    "mvgpu x3 (positions 2, 4)": {2: "e2", 4: "e4"},
}


def pick(paths: list[Path]) -> tuple[np.ndarray | None, int | None]:
    """The capture of a still or cycle frame: one that another capture of the same
    frame agrees with (largest difference 4 levels or less). Returns it and the
    agreement (largest difference), or (None, None) without such a pair."""
    arrs = [np.load(p) for p in paths]
    best: tuple[np.ndarray | None, int | None] = (None, None)
    for a in range(len(arrs)):
        for b in range(a + 1, len(arrs)):
            d = int(np.abs(arrs[a].astype(np.int16) - arrs[b]).max())
            if d <= 4 and (best[1] is None or d < best[1]):
                best = (arrs[a], d)
    return best


def score(ns: argparse.Namespace) -> int:
    out = ROOT / ns.batch
    caps = out / f"caps-{ns.run}"
    names = json.loads((out / "moments.json").read_text())
    rows = []
    for i, name in enumerate(names):
        d = np.load(out / f"m{i:03d}.npz")
        row: dict[str, object] = {"moment": name}
        fil: dict[str, dict[str, dict[str, float]]] = {}
        for cfg, src in FILE.items():
            fil[cfg] = {str(j): truth.score(np, d[f"{k}_y"], d[f"y{j}_y"]) for j, k in src.items()}
        row["file"] = fil
        got: dict[str, np.ndarray] = {}
        agree: dict[str, int] = {}
        for n in [*STILLS, "e2", "e4", "b2", "b4"]:
            a, diff = pick(sorted(caps.glob(f"m{i:03d}_{n}_*.npy")))
            if a is not None and diff is not None:
                got[n], agree[n] = a, diff
        row["agree_max"] = max(agree.values()) if agree else None
        row["missing"] = [n for n in [*STILLS, "e2", "e4", "b2", "b4"] if n not in got]
        scr: dict[str, dict[str, object]] = {}
        made_n: dict[str, int] = {}
        for cfg, src2 in SCREEN.items():
            per: dict[str, object] = {}
            for j, (kind, n) in src2.items():
                truth_c = got.get(f"y{j}")
                if truth_c is None:
                    continue
                if kind == "made":
                    g = [truth.score(np, np.load(p), truth_c)
                         for p in sorted(caps.glob(f"m{i:03d}_{n}_*.npy"))]  # fmt: skip
                    made_n[n] = len(g)
                    if g:
                        per[str(j)] = {m: round(float(np.mean([x[m] for x in g])), 4)
                                       for m in ("worst", "mean", "psnr")}  # fmt: skip
                        per[str(j)]["spread"] = round(  # type: ignore[index]
                            max(x["worst"] for x in g) - min(x["worst"] for x in g), 4
                        )
                elif n in got:
                    per[str(j)] = truth.score(np, got[n], truth_c)
            scr[cfg] = per
        row["screen"] = scr
        row["made_n"] = made_n
        rows.append(row)
    (out / f"score-{ns.run}.json").write_text(json.dumps(rows, indent=1))
    complete = [r for r in rows if all(len(r["screen"][c]) == 5 for c in SCREEN)]  # type: ignore[index]
    print(f"{ns.batch}: {len(rows)} moments, {len(complete)} complete on screen")
    if not ns.keep:
        shutil.rmtree(caps, ignore_errors=True)
    return 0


def _mean5(per: dict[str, dict[str, float]], metric: str) -> float | None:
    if len(per) < 5:
        return None
    return float(np.mean([per[str(j)][metric] for j in POS]))


def pool(ns: argparse.Namespace) -> int:
    """The scored batches together (``--batch b1,b2,...``): per configuration the mean
    over moments of the five positions' mean (m0u's figure), each position, and the
    paired differences of the driver arms against the others on screen."""
    rows = []
    for b in ns.batch.split(","):
        rows += json.loads((ROOT / b / f"score-{ns.run}.json").read_text())
    print(f"{len(rows)} moments\n")
    print("File domain (m0u's method: frames from the file, every position):\n")
    print("| frame from | worst areas, mean of 5 | at 1 | 2 | 3 | 4 | 5 | whole frame | PSNR | n |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for cfg, src in FILE.items():
        per = [r["file"][cfg] for r in rows]
        pos = [float(np.mean([p[str(j)]["worst"] for p in per])) if str(j) in per[0] else None
               for j in POS]  # fmt: skip
        ks = [str(j) for j in src]
        w = np.mean([np.mean([p[k]["worst"] for k in ks]) for p in per])
        m = np.mean([np.mean([p[k]["mean"] for k in ks]) for p in per])
        ps = np.mean([np.mean([p[k]["psnr"] for k in ks]) for p in per])
        cells = " | ".join("-" if v is None else f"{v:.3f}" for v in pos)
        print(f"| {cfg} | {w:.3f} | {cells} | {m:.4f} | {ps:.2f} | {len(per)} |")
    print("\nScreen domain (every frame captured from the screen, against the captured truth):\n")
    print("| frame from | worst areas, mean of 5 | at 1 | 2 | 3 | 4 | 5 | whole frame | PSNR | n |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    full = [r for r in rows if all(len(r["screen"][c]) == 5 for c in SCREEN)]
    for cfg in SCREEN:
        per = [r["screen"][cfg] for r in full]
        pos = [float(np.mean([p[str(j)]["worst"] for p in per])) for j in POS]
        w = np.mean([_mean5(p, "worst") for p in per])
        m = np.mean([_mean5(p, "mean") for p in per])
        ps = np.mean([_mean5(p, "psnr") for p in per])
        cells = " | ".join(f"{v:.3f}" for v in pos)
        print(f"| {cfg} | {w:.3f} | {cells} | {m:.4f} | {ps:.2f} | {len(per)} |")
    print("\nPaired on screen (same moments), driver arm minus the other; SE over moments:\n")
    print("| arm | minus | metric | positions | mean difference | SE | arm better in |")
    print("|---|---|---|---|---|---|---|")
    for arm in ("E: mvgpu x3, driver x2", "B: RIFE x3, driver x2"):
        for other in SCREEN:
            if other == arm:
                continue
            for metric in ("worst", "mean"):
                for label, js in (("mean of 5", POS), ("3 only", (3,)), ("1, 3, 5", (1, 3, 5))):
                    dd = np.array([
                        np.mean([r["screen"][arm][str(j)][metric] for j in js])
                        - np.mean([r["screen"][other][str(j)][metric] for j in js])
                        for r in full
                    ])  # fmt: skip
                    se = float(dd.std(ddof=1) / np.sqrt(dd.size)) if dd.size > 1 else float("nan")
                    better = int((dd > 0).sum())
                    print(f"| {arm.split(':')[0]} | {other} | {metric} | {label} | "
                          f"{dd.mean():+.4f} | {se:.4f} | {better} of {dd.size} |")  # fmt: skip
    agree = [r["agree_max"] for r in rows if r.get("agree_max") is not None]
    made = [n for r in rows for n in r.get("made_n", {}).values()]
    miss = [len(r.get("missing", [])) for r in rows]
    print(
        f"\nagreeing capture pairs: largest difference {max(agree or [0])} levels; "
        f"driver frames per moment and position: {min(made or [0])}-{max(made or [0])}, "
        f"median {int(np.median(made or [0]))}; moments with a missing still or cycle "
        f"frame: {sum(1 for m in miss if m)}; complete on screen: {len(full)} of {len(rows)}"
    )
    return 0


def main() -> int:
    argv = sys.argv[1:]
    extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1 :]
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=("make", "clip", "play", "score", "pool"))
    ap.add_argument("--batch", required=True)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--mvgpu", help="a libmvgpu.so to use instead of the installed one")
    ap.add_argument("--screen", default="DP-1")
    ap.add_argument("--run", default="hdr", help="name of this play (captures, results)")
    ap.add_argument("--keep", action="store_true", help="keep the captures after scoring")
    ns = ap.parse_args(argv)
    if ns.step == "make":
        return make(ns)
    if ns.step == "clip":
        return clip(ns)
    if ns.step == "play":
        return play(ns, extra)
    if ns.step == "pool":
        return pool(ns)
    return score(ns)


if __name__ == "__main__":
    raise SystemExit(main())
