# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The AppImage trial's tests (packaging/appimage/test-appimage.sh runs this).

Standard library only, run by the AppImage's own Python (``--tree``, an
extraction of the same image), so the machine needs no Python of its own.
Every test starts the AppImage file itself (``--appimage``) through its runtime,
the way a user would; the extracted tree is used only for the harness, for
ffprobe on the results, and for the static library check.

  version      ``ButterEye.AppImage --version``
  doctor       ``doctor --json``: mpv, VapourSynth, MVTools, FFMS2 and the
               render tools are found inside the AppImage and are not blocking;
               no finding offers a dnf, COPR or rpm line
  render       an offline ``render --profile cpu --target 2x`` of a generated
               6 s clip: the output exists, 48 fps, 288 frames
  mpv-filter   the AppImage's mpv with ``--vf=vapoursynth`` and an MVTools
               script, in mpv's own encoding mode (headless and exact): the
               filter loads inside mpv and doubles the frame count
  live         ButterEye's own live path, ``play --profile cpu`` with mpv's video
               and audio output null: the filter is in mpv's chain and runs at
               twice the source rate in real time (mpv's IPC)
  gui          the window, started with no arguments under
               QT_QPA_PLATFORM=offscreen: up, no traceback, then ended
  libs         every library each ELF file needs (ldd on this host) and every
               library the running mpv and GUI processes have mapped
               (/proc/<pid>/maps) is inside the AppImage, on host-libs.txt, or a
               dependency of a host library that is
Writes <results>/summary.json and each test's output beside it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from collections.abc import Callable
from pathlib import Path

A = argparse.ArgumentParser()
A.add_argument("--appimage", type=Path, required=True)
A.add_argument("--tree", type=Path, required=True)
A.add_argument("--mode", choices=("extract", "fuse"), required=True)
A.add_argument("--results", type=Path, required=True)
A.add_argument("--host-libs", type=Path, required=True)
ARGS = A.parse_args()
IMG: Path = ARGS.appimage
TREE: Path = ARGS.tree.resolve()
RES: Path = ARGS.results
RES.mkdir(parents=True, exist_ok=True)

#: a clean user: a fresh home, config and runtime folder for every run
HOME = Path(tempfile.mkdtemp(prefix="be-home-"))
RUN = HOME / "run"
RUN.mkdir(mode=0o700)
ENV = {
    k: v
    for k, v in os.environ.items()
    if k in ("PATH", "APPIMAGE_EXTRACT_AND_RUN", "TMPDIR", "LANG", "LC_ALL", "TERM")
}
ENV.update(HOME=str(HOME), XDG_RUNTIME_DIR=str(RUN), XDG_CONFIG_HOME=str(HOME / ".config"),
           XDG_CACHE_HOME=str(HOME / ".cache"), XDG_DATA_HOME=str(HOME / ".local/share"),
           XDG_STATE_HOME=str(HOME / ".local/state"))  # fmt: skip
ENV.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
FFMPEG = TREE / "bin" / "ffmpeg"
FFPROBE = TREE / "bin" / "ffprobe"
#: a shared object's file name (not ld.so.cache, not locale-archive)
SHARED = re.compile(r"\.so(\.\d+)*$")

results: dict[str, dict[str, object]] = {}


def save(name: str, text: str) -> None:
    (RES / name).write_text(text, encoding="utf-8", errors="replace")


def run(argv: list[str], *, timeout: float = 300, env: dict[str, str] | None = None,
        name: str | None = None) -> subprocess.CompletedProcess[str]:  # fmt: skip
    done = subprocess.run(
        argv, env=env or ENV, capture_output=True, text=True, timeout=timeout, cwd=str(HOME)
    )
    if name:
        save(f"{name}.log", f"$ {' '.join(argv)}\nexit {done.returncode}\n--- stdout\n"
             f"{done.stdout}\n--- stderr\n{done.stderr}")  # fmt: skip
    return done


def test(fn: Callable[[], str]) -> Callable[[], str]:
    name = fn.__name__.removeprefix("t_").replace("_", "-")
    t0 = time.monotonic()
    try:
        detail = fn()
        results[name] = {"ok": True, "detail": detail, "s": round(time.monotonic() - t0, 1)}
        print(f"PASS {name}: {detail}", flush=True)
    except Exception as exc:  # every test runs, whatever the one before did
        results[name] = {"ok": False, "detail": f"{type(exc).__name__}: {exc}",
                         "s": round(time.monotonic() - t0, 1)}  # fmt: skip
        print(f"FAIL {name}: {type(exc).__name__}: {exc}", flush=True)
        save(f"{name}.traceback", traceback.format_exc())
    return fn


def check(cond: bool, what: str) -> None:
    if not cond:
        raise AssertionError(what)


def make_clip(path: Path, seconds: int) -> Path:
    """A 640x360 24 fps H.264 clip with audio, by the AppImage's own ffmpeg."""
    done = run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=24:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(path)], name=f"clip-{path.stem}")  # fmt: skip
    check(done.returncode == 0 and path.is_file(), f"ffmpeg made no clip: {done.stderr[-500:]}")
    return path


def probe(path: Path) -> dict[str, str]:
    done = run([str(FFPROBE), "-v", "error", "-count_frames", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name,r_frame_rate,nb_read_frames",
                "-of", "json", str(path)])  # fmt: skip
    check(done.returncode == 0, f"ffprobe failed: {done.stderr[-500:]}")
    return json.loads(done.stdout)["streams"][0]  # type: ignore[no-any-return]


CLIPS = Path(tempfile.mkdtemp(prefix="be-clips-"))
VERSION = ""


# ---------------------------------------------------------------- tests
@test
def t_version() -> str:
    global VERSION
    done = run([str(IMG), "--version"], name="version")
    check(done.returncode == 0, f"exit {done.returncode}: {done.stderr[-800:]}")
    m = re.search(r"ButterEye (\d+\.\d+\.\d+)", done.stdout)
    check(m is not None, f"no version in {done.stdout!r}")
    assert m is not None
    VERSION = m.group(1)
    return done.stdout.strip().splitlines()[0]


#: rows that must be OK: what the AppImage carries, found inside it
MUST_BE_OK = ("mpv.version", "mpv.vf_vapoursynth", "packages.mvtools", "render.ffms2")
#: what a blocking row may be about in a GPU-less container (anything else fails)
GPU_PREFIXES = ("gpu.", "vulkan.", "hw.", "trt.", "display.")
FOREIGN_WORDS = re.compile(r"\b(dnf|copr|rpm)\b", re.I)


@test
def t_doctor() -> str:
    done = run([str(IMG), "doctor", "--json"], timeout=300, name="doctor")
    save("doctor.json", done.stdout)
    data = json.loads(done.stdout)["data"]
    rows = data["findings"]
    table = "\n".join(f"{f['severity']:9s} {f['id']:32s} {json.dumps(f['title'])[:110]}" for f in rows)
    save("doctor-table.txt", table + f"\nexit code {done.returncode}\n")
    print(table)
    by_id = {f["id"]: f for f in rows}
    for row in MUST_BE_OK:
        check(row in by_id, f"{row} is not in the report")
        check(by_id[row]["severity"] == "ok", f"{row} is {by_id[row]['severity']}: {by_id[row]}")
    host = by_id.get("mpv.host_binary")
    if host is not None:
        ev = " ".join(host.get("evidence") or ())
        check(ev.endswith("/bin/mpv") and not ev.startswith("/usr/"), f"mpv.host_binary: {ev}")
    blocking = [f["id"] for f in rows if f["severity"] == "blocking"]
    bad = [i for i in blocking if not i.startswith(GPU_PREFIXES)]
    check(not bad, f"blocking, and not about the GPU: {bad}")
    foreign = [f["id"] for f in rows
               if any(FOREIGN_WORDS.search(c) for c in f.get("commands") or ())
               or FOREIGN_WORDS.search(json.dumps(f.get("fix")))]  # fmt: skip
    check(not foreign, f"advice naming dnf/COPR/rpm inside the AppImage: {foreign}")
    return f"{len(rows)} findings, blocking: {blocking or 'none'}"


@test
def t_render() -> str:
    clip = make_clip(CLIPS / "clip.mkv", 6)
    out = HOME / "out.mkv"
    done = run([str(IMG), "render", "--json", "--profile", "cpu", "--target", "2x",
                "-o", str(out), str(clip)], timeout=900, name="render")  # fmt: skip
    data = json.loads(done.stdout)["data"]
    check(data.get("state") == "succeeded", f"render: {data}")
    check(out.is_file(), "no output file")
    s = probe(out)
    check(s["r_frame_rate"] == "48/1", f"rate {s}")
    check(int(s["nb_read_frames"]) == 288, f"frames {s}")
    return f"{s['codec_name']} {s['r_frame_rate']} {s['nb_read_frames']} frames, {out.stat().st_size} bytes"


@test
def t_mpv_filter() -> str:
    clip = CLIPS / "clip.mkv" if (CLIPS / "clip.mkv").is_file() else make_clip(CLIPS / "clip.mkv", 6)
    marker = HOME / "vpy-ran.txt"
    vpy = HOME / "mvtools.vpy"
    vpy.write_text(
        "import vapoursynth as vs\n"
        "core = vs.core\n"
        f"core.std.LoadPlugin({str(TREE / 'vapoursynth' / 'mvtools.so')!r})\n"
        # mpv hands the script a clip without a frame rate; FlowFPS needs one
        "c = core.std.AssumeFPS(video_in, fpsnum=24, fpsden=1)\n"
        "sup = core.mv.Super(c)\n"
        "bv = core.mv.Analyse(sup, isb=True)\n"
        "fv = core.mv.Analyse(sup, isb=False)\n"
        "out = core.mv.FlowFPS(c, sup, bv, fv, num=48, den=1)\n"
        f"open({str(marker)!r}, 'w').write(f'{{vs.__version__}} {{out.fps}}')\n"
        "out.set_output()\n",
        encoding="utf-8",
    )
    enc = HOME / "mpv-out.mkv"
    mpv = TREE / "bin" / "mpv"
    # mpv inside the AppImage file is not reachable as a program of its own: the
    # runtime starts AppRun, which is ButterEye. So this is the extracted tree's
    # mpv, started the way ButterEye starts it: the tree's Python, running the
    # tree's ButterEye (so osapi.LINUX_APP is the tree), sets up the children's
    # environment with use_bundled_runtime() - VSScript's configuration line for
    # this tree's Python included - and then becomes mpv.
    starter = (
        "import os, sys\n"
        "from buttereye.core import osapi\n"
        "assert osapi.LINUX_APP is not None, 'not detected as the AppImage'\n"
        "osapi.use_bundled_runtime()\n"
        "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    done = run([str(TREE / "python" / "bin" / "python3.14"), "-I", "-B", "-c", starter,
                str(mpv), "--no-config", "--msg-level=all=v", f"--vf=vapoursynth={vpy}",
                "--aid=no", f"--o={enc}", "--of=matroska", "--ovc=libx264",
                "--ovcopts=preset=ultrafast", str(clip)],
               timeout=600, name="mpv-filter")  # fmt: skip
    check(done.returncode == 0, f"mpv exit {done.returncode}: {done.stderr[-1500:]}")
    check(marker.is_file(), "the .vpy never ran inside mpv")
    s = probe(enc)
    check(s["r_frame_rate"] == "48/1", f"rate {s}")
    check(int(s["nb_read_frames"]) >= 280, f"frames {s}")
    return f"VapourSynth {marker.read_text()} inside mpv; {s['nb_read_frames']} frames at {s['r_frame_rate']}"


def _ask(sock: str, *props: str) -> dict[str, object]:
    s = socket.socket(socket.AF_UNIX)
    s.connect(sock)
    s.settimeout(5)
    out: dict[str, object] = {}
    buf = b""
    for i, p in enumerate(props):
        s.sendall(json.dumps({"command": ["get_property", p], "request_id": i}).encode() + b"\n")
    while len(out) < len(props):
        buf += s.recv(65536)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            m = json.loads(line)
            if "request_id" in m:
                out[props[m["request_id"]]] = m.get("data")
    s.close()
    return out


MAPS: dict[str, list[str]] = {}


def maps_of(pid: int) -> list[str]:
    out = []
    try:
        for line in Path(f"/proc/{pid}/maps").read_text().splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) == 6 and SHARED.search(parts[5].replace(" (deleted)", "")):
                out.append(parts[5].replace(" (deleted)", ""))
    except OSError:
        return []
    return sorted(set(out))


def procs() -> list[tuple[int, str, list[str]]]:
    out = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            exe = os.readlink(d / "exe")
            cmd = (d / "cmdline").read_bytes().split(b"\0")
            out.append((int(d.name), exe, [c.decode(errors="replace") for c in cmd if c]))
        except OSError:
            continue
    return out


@test
def t_live() -> str:
    clip = make_clip(CLIPS / "long.mkv", 40)
    conf = HOME / ".config" / "mpv"
    conf.mkdir(parents=True, exist_ok=True)
    (conf / "mpv.conf").write_text("vo=null\nao=null\n")
    started = time.time()
    proc = subprocess.Popen([str(IMG), "play", "-v", "--profile", "cpu", str(clip)], env=ENV,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            cwd=str(HOME))  # fmt: skip
    sock: str | None = None
    try:
        deadline = time.monotonic() + 60
        folder = RUN / "buttereye"
        while time.monotonic() < deadline and sock is None:
            for s in sorted(folder.glob("mpv-*.sock"), key=lambda p: p.stat().st_mtime):
                if s.stat().st_mtime >= started - 1:
                    sock = str(s)
            time.sleep(0.5)
        check(sock is not None, "no mpv socket appeared")
        assert sock is not None
        time.sleep(8)
        first = _ask(sock, "time-pos")
        t0 = time.monotonic()
        time.sleep(8)
        state = _ask(sock, "time-pos", "estimated-vf-fps", "container-fps", "vf", "path")
        wall = time.monotonic() - t0
        for pid, exe, _ in procs():
            if exe.endswith("/bin/mpv"):
                MAPS[f"mpv ({exe})"] = maps_of(pid)
        save("live-state.json", json.dumps(state, indent=2))
        check(isinstance(state["vf"], list) and bool(state["vf"]), f"no filter: {state}")
        check("mvtools" in json.dumps(state["vf"]), f"no mvtools in {state['vf']}")
        fps = float(state["estimated-vf-fps"])  # type: ignore[arg-type]
        src = float(state["container-fps"])  # type: ignore[arg-type]
        check(abs(fps - 2 * src) < 1.0, f"filter output {fps} fps for a {src} fps source")
        played = float(state["time-pos"]) - float(first["time-pos"])  # type: ignore[arg-type]
        check(played > 0.9 * wall, f"played {played:.1f} s in {wall:.1f} s: not real time")
    finally:
        if sock is not None:
            try:
                s = socket.socket(socket.AF_UNIX)
                s.connect(sock)
                s.sendall(b'{"command": ["quit"]}\n')
                s.close()
            except OSError:
                pass
        try:
            out, err = proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        save("live.log", f"exit {proc.returncode}\n--- stdout\n{out}\n--- stderr\n{err}")
    check("with mvtools (active" in out + err, "ButterEye never reported mvtools active")
    return f"{fps:.2f} fps out of {src:g}, {played:.1f} s played in {wall:.1f} s, mpv exit via quit"


@test
def t_gui() -> str:
    env = dict(ENV, QT_QPA_PLATFORM="offscreen")
    proc = subprocess.Popen([str(IMG)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(HOME))  # fmt: skip
    py: int | None = None
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            check(proc.poll() is None, f"the GUI exited by itself with {proc.returncode}")
            for pid, exe, cmd in procs():
                if exe.endswith("/python/bin/python3.14") and "buttereye.gui" in cmd:
                    m = maps_of(pid)
                    if any("libqoffscreen.so" in x for x in m) and any("libQt6Widgets" in x for x in m):
                        py = pid
                        MAPS[f"gui ({exe})"] = m
            if py is not None:
                break
            time.sleep(1)
        check(py is not None, "no GUI process with Qt Widgets and the offscreen platform loaded")
        time.sleep(5)  # it stays up
        check(proc.poll() is None, f"the GUI exited by itself with {proc.returncode}")
        assert py is not None
        MAPS[f"gui ({py})"] = maps_of(py)
        os.kill(py, signal.SIGTERM)
        out, err = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            out, err = proc.communicate()
    save("gui.log", f"exit {proc.returncode}\n--- stdout\n{out}\n--- stderr\n{err}")
    check("Traceback" not in err, f"a traceback: {err[-1500:]}")
    return f"up with Qt Widgets and the offscreen platform loaded; ended by SIGTERM (exit {proc.returncode})"


# ---------------------------------------------------------------- libraries
def host_patterns() -> list[re.Pattern[str]]:
    out = []
    for line in ARGS.host_libs.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(re.compile(line))
    return out


PATTERNS = host_patterns()
LDD = re.compile(r"^\s*(\S+)\s+=>\s+(not found|\S+)")


def ldd(path: str) -> list[tuple[str, str]]:
    done = subprocess.run(["ldd", path], capture_output=True, text=True, check=False)
    return [(m.group(1), m.group(2)) for line in done.stdout.splitlines() if (m := LDD.match(line))]


def is_host_name(name: str) -> bool:
    """Does a library file name match the allow-list? Matched as the soname
    (``libEGL.so.1``) and, for a mapped file, as its real name with the trailing
    version numbers taken off one by one (``libEGL.so.1.1.0`` -> ``libEGL.so.1``)."""
    while True:
        if any(p.fullmatch(name) for p in PATTERNS):
            return True
        if not re.search(r"\.so\.\d+$|\.so(\.\d+)+$", name) or name.endswith(".so"):
            return False
        name = name.rsplit(".", 1)[0]



def host_closure(paths: set[str]) -> set[str]:
    """The real files of the allowed host libraries and of every library they
    load in turn (ldd): the host's to choose, not the AppImage's."""
    out: set[str] = set()
    for p in paths:
        out.add(os.path.realpath(p))
        for _, where in ldd(p):
            if where != "not found":
                out.add(os.path.realpath(where))
    return out


@test
def t_libs() -> str:
    roots = {str(TREE)}
    mounted = [x for xs in MAPS.values() for x in xs]
    # the running processes ran from the runtime's mount or extraction folder,
    # not from TREE: whatever folder holds their python/ or bin/ counts as inside
    for x in mounted:
        m = re.match(r"(.*?/(?:\.mount_[^/]+|appimage_extracted_[0-9a-f]+))/", x)
        if m:
            roots.add(m.group(1))

    def inside(p: str) -> bool:
        return any(p == r or p.startswith(r + "/") for r in roots)

    elfs = []
    for p in TREE.rglob("*"):
        if p.is_file() and not p.is_symlink():
            with p.open("rb") as fh:
                if fh.read(4) == b"\x7fELF":
                    elfs.append(p)
    static: dict[str, list[str]] = {}
    missing: list[str] = []
    for e in elfs:
        for name, where in ldd(str(e)):
            if where == "not found":
                missing.append(f"{e.relative_to(TREE)}: {name}")
            elif not inside(where):
                static.setdefault(where, []).append(str(e.relative_to(TREE)))
    # ldd flattens: a library on the list and everything it loads are the
    # host's; anything else from outside means the AppImage itself needs it
    allowed = {p for p in static if is_host_name(os.path.basename(p))}
    runtime_host = {p for xs in MAPS.values() for p in xs
                    if not inside(p) and is_host_name(os.path.basename(p))}  # fmt: skip
    closure = host_closure(allowed | runtime_host)
    bad_static = sorted(p for p in static if p not in allowed and os.path.realpath(p) not in closure)
    bad_runtime: dict[str, list[str]] = {}
    for who, libs in MAPS.items():
        for p in libs:
            if inside(p) or is_host_name(os.path.basename(p)) or os.path.realpath(p) in closure:
                continue
            bad_runtime.setdefault(who, []).append(p)
    report = {
        "roots": sorted(roots),
        "elf_files": len(elfs),
        "host_libraries_needed_directly": sorted(os.path.basename(p) for p in allowed),
        "host_libraries_needed": {p: sorted(set(v))[:5] for p, v in sorted(static.items())},
        "not_found": missing,
        "not_allowed_static": bad_static,
        "runtime_maps": MAPS,
        "not_allowed_runtime": bad_runtime,
    }
    save("libs.json", json.dumps(report, indent=2))
    check(not missing, f"ldd cannot resolve: {sorted(set(missing))[:15]}")
    check(not bad_static, f"host libraries not on the allow-list: {bad_static}")
    check(not bad_runtime, f"mapped at run time, not allowed: {bad_runtime}")
    check(bool(MAPS), "no running process was traced (the live and gui tests failed?)")
    outside_rt = sorted({os.path.basename(p) for xs in MAPS.values() for p in xs if not inside(p)})
    save("libs-host-runtime.txt", "\n".join(outside_rt) + "\n")
    return (f"{len(elfs)} ELF files; host libraries needed directly: "
            f"{len({os.path.basename(p) for p in allowed})}, all allowed; "
            f"{sum(len(v) for v in MAPS.values())} mapped libraries in {len(MAPS)} processes, "
            f"{len(outside_rt)} from the host, all allowed or loaded by an allowed one")


# ---------------------------------------------------------------- summary
ok = all(r["ok"] for r in results.values())
summary = {"mode": ARGS.mode, "version": VERSION, "ok": ok, "tests": results}
for f in ("/etc/os-release",):
    summary["os"] = dict(
        line.split("=", 1) for line in Path(f).read_text().splitlines() if "=" in line
    ).get("PRETTY_NAME", "?").strip('"')
(RES / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps({k: ("PASS" if v["ok"] else "FAIL") for k, v in results.items()}))
shutil.rmtree(CLIPS, ignore_errors=True)
sys.exit(0 if ok else 1)
