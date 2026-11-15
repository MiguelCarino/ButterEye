# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Driver trial gate 5: seek, pause, resize, fullscreen and other screens, looking
for hangs.

Plays one arm through ButterEye's core (as ``nvtrial_run.py``, same options) and
runs a fixed script of actions over mpv's IPC, all of them app-side (no desktop
setting changes; mpv moves its own fullscreen window by ``fs-screen-name``):

  steady, seek +30 s, seek -20 s, pause 3 s, windowed (tiled: a resize), window
  scale 0.5 (another resize request), fullscreen, the 60 Hz screen, its override
  (refresh / 2 when the driver doubles), the 75 Hz screen, its override, back to the
  first screen and its override.

After each action it waits, then records: whether mpv answered over IPC (a hang if
it did not for 10 s), how far playback moved against the wall clock, mpv's drop
counter, the screen the compositor shows mpv on (read only), and from the timing
layer (``--probe``) the presents per second below the driver's layer in that
window and their screen intervals. A player that hangs is ended (only the mpv this
script started, by its pid). Writes ``gate5.json`` in ``OUT/<name>/``.

usage: nvtrial_gate5.py NAME [nvtrial_run.py options] [--doubling] [--screens A,B,C]
       -- [mpv options]
       nvtrial_gate5.py --report NAME
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent))
import nvtrial_probe as probe  # noqa: E402
import nvtrial_run as runner  # noqa: E402

HANG_S = 10.0


def _call(ipc: probe.Ipc, *args: object) -> tuple[bool, object]:
    """One IPC command; (answered, data). Retries until HANG_S passes."""
    end = time.monotonic() + HANG_S
    while time.monotonic() < end:
        try:
            ipc.rid += 1
            rid = ipc.rid
            ipc.s.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
            t = time.monotonic() + 2.0
            while time.monotonic() < t:
                while b"\n" in ipc.buf:
                    line, ipc.buf = ipc.buf.split(b"\n", 1)
                    with contextlib.suppress(ValueError):
                        j = json.loads(line)
                        if j.get("request_id") == rid:
                            return True, j.get("data")
                chunk = ipc.s.recv(65536)
                if not chunk:
                    return False, None
                ipc.buf += chunk
        except TimeoutError:
            continue
        except OSError:
            return False, None
    return False, None


def _probe_window(path: Path, t0: float, t1: float) -> dict[str, object]:
    """Presents below the driver's layer between t0 and t1 (CLOCK_MONOTONIC), per
    swapchain, and the screen intervals of consecutive timed presents."""
    if not path.exists():
        return {}
    p_rows, t_rows = [], {}
    for ln in path.open():
        if not ln.endswith("\n") or ln.count(",") < 4:
            continue  # the line being written
        if ln.startswith("P,"):
            if ln.count(",") < 10:
                continue
            f = ln.split(",")
            t = int(f[1]) / 1e9
            if t0 <= t <= t1:
                p_rows.append((f[2], t, int(f[5])))
        elif ln.startswith("T,"):
            f = ln.split(",")
            t_rows[(f[1], int(f[2]))] = int(f[3])
    if not p_rows:
        return {"presents": 0}
    by = Counter(r[0] for r in p_rows)
    sc = by.most_common(1)[0][0]
    rows = [r for r in p_rows if r[0] == sc]
    fp = [(r[2], t_rows.get((sc, r[2]), 0)) for r in rows]
    slots: Counter[int] = Counter()
    for (i0, a), (i1, b) in zip(fp, fp[1:], strict=False):
        if i1 == i0 + 1 and a and b:
            slots[round((b - a) / 1e9 * 144)] += 1  # in 144 Hz refreshes
    return {
        "swapchains": len(by),
        "presents_per_s": round(len(rows) / (t1 - t0), 1),
        "gap_in_144hz_refreshes": dict(sorted(slots.items())),
        "never_shown": sum(1 for i, v in fp if v == 0 and (sc, i) in t_rows),
    }


async def script(ns, ipc: probe.Ipc, pid: int, out: Path, core, sid) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    screens = ns.screens.split(",")
    rates: dict[str, float] = {}
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True,
                                         text=True, timeout=5).stdout)  # fmt: skip
        rates = {m["name"]: float(m["refreshRate"]) for m in mons}

    def override_for(name: str) -> list[object]:
        hz = rates.get(name) or 144.0
        return ["set_property", "display-fps-override", round(hz / 2, 3) if ns.doubling else 0]

    steps: list[tuple[str, list[list[object]], float]] = [
        ("steady", [], 8),
        ("seek +30 s", [["seek", 30, "relative"]], 6),
        ("seek -20 s", [["seek", -20, "relative"]], 6),
        ("pause 3 s", [["set_property", "pause", True]], 3),
        ("unpause", [["set_property", "pause", False]], 6),
        ("windowed", [["set_property", "fullscreen", False]], 6),
        ("window scale 0.5", [["set_property", "window-scale", 0.5]], 5),
        ("fullscreen", [["set_property", "fullscreen", True]], 6),
    ]
    for name in screens[1:] + screens[:1]:
        steps.append((f"to {name}", [["set_property", "fs-screen-name", name],
                                     ["set_property", "fullscreen", False],
                                     ["set_property", "fullscreen", True]], 8))  # fmt: skip
        if ns.doubling:
            steps.append((f"override on {name}", [override_for(name)], 8))
    log: list[dict[str, object]] = []
    frames = out / "frames.csv"
    for label, cmds, wait in steps:
        t0 = time.monotonic()
        answered = True
        for c in cmds:
            ok, _ = await asyncio.to_thread(_call, ipc, *c)
            answered &= ok
            await asyncio.sleep(0.3)
        ok0, pt0 = await asyncio.to_thread(_call, ipc, "get_property", "playback-time")
        ok_d0, d0 = await asyncio.to_thread(_call, ipc, "get_property", "frame-drop-count")
        w0 = time.monotonic()
        await asyncio.sleep(wait)
        ok1, pt1 = await asyncio.to_thread(_call, ipc, "get_property", "playback-time")
        ok_d1, d1 = await asyncio.to_thread(_call, ipc, "get_property", "frame-drop-count")
        w1 = time.monotonic()
        snap = next((x for x in await core.sessions() if x.sid == sid), None)
        _, dfps = await asyncio.to_thread(_call, ipc, "get_property", "display-fps")
        _, ov = await asyncio.to_thread(_call, ipc, "get_property", "display-fps-override")
        row = {
            "step": label,
            "answered": answered and ok0 and ok1,
            "alive": os.path.exists(f"/proc/{pid}"),
            "advance_s": round(pt1 - pt0, 2) if isinstance(pt0, float) and isinstance(pt1, float) else None,  # noqa: E501
            "wall_s": round(w1 - w0, 2),
            "drops": (d1 - d0) if isinstance(d0, int) and isinstance(d1, int) else None,
            "display_fps": dfps,
            "override": ov,
            "window": probe.window_facts(pid),
            "butter": None if snap is None else {
                "filter": snap.filter.value, "target": str(snap.target_fps),
                "multiplier": snap.multiplier, "health": getattr(snap.health, "value", None)},
            "below_driver": _probe_window(frames, w0, w1),
            "t": round(t0, 3),
        }  # fmt: skip
        log.append(row)
        print(json.dumps({k: row[k] for k in ("step", "answered", "advance_s", "drops")}
                         | {"mon": (row["window"] or {}).get("monitor")}), flush=True)  # fmt: skip
        if not row["answered"] or not row["alive"]:
            row["hang"] = True
            break
    return log


async def main_async(ns, mpv_extra: list[str], out: Path) -> dict[str, object]:  # type: ignore[no-untyped-def]
    from buttereye.core import paths as paths_mod
    from buttereye.core.api import ButterEye, DetachPolicy
    from buttereye.core.mpvctl import session as live

    env = runner.arm_env(ns, out)
    core = await ButterEye.open(paths_mod.resolve())
    reg = live.registry(core._ctx)
    reg.options.env = env
    reg.options.extra_args = runner.mpv_args(ns, out, mpv_extra)
    facts: dict[str, object] = {"cmd_extra": list(reg.options.extra_args)}
    pid = None
    try:
        sid = await core.play(Path(ns.clip), profile_id=None).result()
        held = reg.sessions[sid]
        pid = held.pid
        assert pid is not None
        facts["pid"] = pid
        ipc = None
        for _ in range(100):
            with contextlib.suppress(OSError):
                ipc = probe.Ipc(str(held.socket))
                break
            await asyncio.sleep(0.1)
        assert ipc is not None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 60:
            snap = next((x for x in await core.sessions() if x.sid == sid), None)
            if snap is not None and snap.filter.value == "active":
                break
            await asyncio.sleep(0.5)
        await asyncio.sleep(3)
        facts["steps"] = await script(ns, ipc, pid, out, core, sid)
    finally:
        with contextlib.suppress(Exception):
            await core.close(DetachPolicy.KEEP_FILTER, cancel_jobs=True, timeout_s=3.0)
        if pid is not None and os.path.exists(f"/proc/{pid}"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)  # the player this script started
            await asyncio.sleep(3)
            if os.path.exists(f"/proc/{pid}"):
                facts["needed_sigkill"] = True
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
    return facts


def report(run: Path) -> int:
    """Per step, from the whole log after the run: presents below the driver's layer,
    never shown, and screen intervals in refreshes of the screen mpv was on; the
    first 1.5 s after each action (the change itself) are reported apart as the
    longest screen gap."""
    g = json.loads((run / "gate5.json").read_text())
    steps = g.get("steps") or []
    end = steps[-1]["t"] + 10 if steps else 0
    print("| step | screen | ButterEye target | presents/s | never shown | gaps (refreshes: count) "
          "| longest gap in the first 1.5 s | IPC answered | playback moved |")  # fmt: skip
    print("|---|---|---|---|---|---|---|---|---|")
    for i, st in enumerate(steps):
        t0 = st["t"]
        t1 = steps[i + 1]["t"] if i + 1 < len(steps) else end
        mon = (st.get("window") or {}).get("monitor")
        hz = float((st.get("window") or {}).get("refresh") or 144.0)
        settle = _probe_rows(run / "frames.csv", t0, t0 + 1.5)
        steady = _probe_rows(run / "frames.csv", t0 + 1.5, t1)
        b = st.get("butter") or {}
        cells = ["-", "-", "-", "-"]
        if steady:
            fp = [v for _, v in steady]
            shown = [v for v in fp if v]
            gaps = Counter(
                round((b2 - a) / 1e9 * hz) for a, b2 in zip(shown, shown[1:], strict=False)
            )
            cells = [f"{len(steady) / (t1 - t0 - 1.5):.1f}", f"{len(fp) - len(shown)}",
                     ", ".join(f"{k}: {v}" for k, v in sorted(gaps.items())), ""]  # fmt: skip
        if settle:
            sh = [v for _, v in settle if v]
            cells[3] = (
                f"{max((b2 - a) / 1e6 for a, b2 in zip(sh, sh[1:], strict=False)):.0f} ms"
                if len(sh) > 1
                else "-"
            )  # noqa: E501
        print(f"| {st['step']} | {mon} {hz:g} Hz | {b.get('target')} (x{b.get('multiplier')}) | "
              + " | ".join(cells) + f" | {st['answered']} | {st['advance_s']} s |")  # fmt: skip
    return 0


def _probe_rows(path: Path, t0: float, t1: float) -> list[tuple[int, int]]:
    """(present id, first-pixel ns or 0) of the presents in [t0, t1) on the swapchain
    that had most of them."""
    rows, tim = [], {}
    for ln in path.open():
        f = ln.rstrip("\n").split(",")
        if f[0] == "P" and len(f) >= 11:
            rows.append((f[2], int(f[1]) / 1e9, int(f[5])))
        elif f[0] == "T" and len(f) >= 4:
            tim[(f[1], int(f[2]))] = int(f[3])
    inside = [r for r in rows if t0 <= r[1] < t1]
    sc = Counter(r[0] for r in inside).most_common(1)[0][0] if inside else None
    return [(i, tim.get((sc, i), 0)) for c, _, i in inside if c == sc]


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--report"]:
        return report(probe.OUT / argv[1])
    mpv_extra: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, mpv_extra = argv[:i], argv[i + 1 :]
    ap = runner.parser()
    ap.add_argument(
        "--doubling",
        action="store_true",
        help="set the override to half the new screen's refresh after each move",
    )
    ap.add_argument("--screens", default="DP-1,HDMI-A-1,DP-3")
    ns = ap.parse_args(argv)
    out = probe.OUT / ns.name
    if out.exists():
        print(f"{out} exists", file=sys.stderr)
        return 2
    out.mkdir(parents=True)
    root = runner.private_profile()
    try:
        facts = asyncio.run(main_async(ns, mpv_extra, out))
    finally:
        shutil.rmtree(root, ignore_errors=True)
    nv = out / "nvpresent.log"
    facts["nvpresent_lines"] = nv.read_text(errors="replace").splitlines() if nv.exists() else []
    (out / "gate5.json").write_text(json.dumps(facts, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
