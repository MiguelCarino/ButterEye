# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Helpers for build-appimage.sh (the AppImage trial, docs/appimage-trial.md):
the Linux peer of packaging/macos/appstage.py. Standard library only, plus
``readelf``, ``patchelf``, ``ldconfig`` and ``dpkg`` from the Ubuntu 22.04 build
container; run with ``python3 -I``.

  trim-qt <site-packages>
        drop the PySide6 parts ButterEye never loads: what is left is Qt Core,
        Gui, Widgets, Svg, SvgWidgets and D-Bus, with what the xcb, Wayland,
        offscreen and minimal platform plugins need, and the SVG icon and image
        plugins (packaging/macos/appstage.py trim-qt is the macOS list)
  closure <appdir> <out.json>
        make every ELF file under <appdir> self-sufficient: each library one of
        them needs is either the host's (host-libs.txt, beside this file) or is
        found inside <appdir> through that file's own RUNPATH. A library that is
        neither is copied in from the build system - next to the libraries the
        file already finds, or into <appdir>/lib with a RUNPATH entry added - and
        recorded in <out.json> with the file it came from
  check <appdir>
        the AppImage's gate, read-only: the rule above holds for every ELF file,
        no RUNPATH names a directory outside <appdir>, and prints the newest
        glibc and libstdc++ symbol versions anything asks for - the oldest host
        it can start on, measured rather than claimed
  debs <closure.json> <appdir> <specs.txt> <sources.txt>
        for each library `closure` copied from the system: the Ubuntu package it
        came from, its copyright file into <appdir>/licenses/<source package>/,
        one install-record spec per source package into <specs.txt>, and the
        `source=version` list apt-get source fetches into <sources.txt>
  unwheel, scan, record, qt-notices
        as packaging/macos/appstage.py (which this imports)
  sources-txt <out> key=value ...
        licenses/SOURCES.txt, the source offer
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_LIBS = HERE / "host-libs.txt"


def _appstage():  # type: ignore[no-untyped-def]
    """packaging/macos/appstage.py, for the parts that are not Mach-O-specific."""
    spec = importlib.util.spec_from_file_location("appstage", HERE.parent / "macos" / "appstage.py")
    if spec is None or spec.loader is None:
        sys.exit("packaging/macos/appstage.py is missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def host_patterns(path: Path = HOST_LIBS) -> list[re.Pattern[str]]:
    """The allow-list, one compiled regular expression per non-comment line."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(re.compile(line))
    return out


def is_host(name: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.fullmatch(name) for p in patterns)


# ---------------------------------------------------------------- ELF
def is_elf(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def elf_files(top: Path) -> list[Path]:
    return sorted(p for p in top.rglob("*") if is_elf(p))


_NEEDED = re.compile(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]")
_RUNPATH = re.compile(r"\((?:RUNPATH|RPATH)\)\s+Library (?:runpath|rpath): \[([^\]]*)\]")


def dynamic(path: Path) -> tuple[list[str], list[str]]:
    """(DT_NEEDED entries, RUNPATH/RPATH entries) of an ELF file. A file with no
    dynamic section (a static executable, an object) has neither."""
    out = subprocess.run(
        ["readelf", "-dW", str(path)], capture_output=True, text=True, check=False
    ).stdout
    needed = _NEEDED.findall(out)
    runpath: list[str] = []
    for m in _RUNPATH.findall(out):
        runpath += [p for p in m.split(":") if p]
    return needed, runpath


def expand(entry: str, elf: Path) -> Path:
    origin = str(elf.parent)
    return Path(os.path.normpath(entry.replace("${ORIGIN}", origin).replace("$ORIGIN", origin)))


def search_dirs(elf: Path, appdir: Path) -> tuple[list[Path], list[str]]:
    """(the RUNPATH directories of ``elf`` inside ``appdir``, the entries that
    point anywhere else)."""
    inside: list[Path] = []
    outside: list[str] = []
    for entry in dynamic(elf)[1]:
        d = expand(entry, elf)
        if d == appdir or appdir in d.parents:
            inside.append(d)
        else:
            outside.append(entry)
    return inside, outside


def resolve(name: str, dirs: list[Path], elf: Path | None = None) -> Path | None:
    if "/" in name:
        # a path, not a soname: python-build-standalone's libpython3.so needs
        # "$ORIGIN/../lib/libpython3.14.so.1.0", which the loader opens as is
        if elf is None:
            return None
        path = expand(name, elf)
        return path if path.exists() else None
    for d in dirs:
        if (d / name).exists():
            return d / name
    return None


def ldconfig_index() -> dict[str, Path]:
    """soname -> path, for the x86-64 libraries the build system's loader knows."""
    out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True, check=True).stdout
    index: dict[str, Path] = {}
    for line in out.splitlines():
        m = re.match(r"\s*(\S+) \(([^)]*)\) => (\S+)$", line)
        if m and "x86-64" in m.group(2):
            index.setdefault(m.group(1), Path(m.group(3)))
    return index


def set_runpath(elf: Path, entries: list[str]) -> None:
    subprocess.run(["patchelf", "--set-rpath", ":".join(entries), str(elf)], check=True)


def rel_origin(elf: Path, target: Path) -> str:
    return "$ORIGIN/" + os.path.relpath(target, elf.parent) if target != elf.parent else "$ORIGIN"


def closure(appdir: Path, out: Path) -> None:
    appdir = appdir.resolve()
    patterns = host_patterns()
    system = ldconfig_index()
    lib = appdir / "lib"
    copied: dict[str, str] = {}
    queue = elf_files(appdir)
    seen: set[Path] = set()
    while queue:
        elf = queue.pop(0)
        if elf in seen:
            continue
        seen.add(elf)
        needed, _ = dynamic(elf)
        inside, outside = search_dirs(elf, appdir)
        if outside:
            # a build-tree path (meson's, ffmpeg's) would make the loader look on
            # the user's machine wherever the build happened to be
            set_runpath(elf, [rel_origin(elf, d) for d in inside])
        for name in needed:
            if is_host(name, patterns) or resolve(name, inside, elf):
                continue
            target_dir = inside[0] if inside else lib
            if not inside:
                inside = [lib]
                set_runpath(elf, [rel_origin(elf, lib)])
            dest = target_dir / name
            # already in the AppImage somewhere else (lib/, say): copy it from there
            src = next((p for p in appdir.rglob(name) if p.is_file()), None)
            if src is None:
                found = system.get(name)
                if found is None:
                    sys.exit(f"{elf.relative_to(appdir)} needs {name}, which is neither on the "
                             f"host allow-list nor installed on the build system")
                src = found.resolve()
                copied[str(dest.relative_to(appdir))] = str(found)
            else:
                copied[str(dest.relative_to(appdir))] = copied.get(
                    str(src.relative_to(appdir)), f"<appdir>/{src.relative_to(appdir)}"
                )
            if dest.exists():
                continue  # copied for another file a moment ago
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            dest.chmod(0o755)
            # a copied library finds what it needs beside itself
            set_runpath(dest, ["$ORIGIN"])
            print(f"  + {dest.relative_to(appdir)}  ({copied[str(dest.relative_to(appdir))]}, "
                  f"for {elf.relative_to(appdir)})")
            queue.append(dest)
    out.write_text(json.dumps(copied, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"closure: {len(seen)} ELF files, {len(copied)} libraries copied in")


_GLIBC = re.compile(r"Name: (GLIBC|GLIBCXX|CXXABI)_(\d+(?:\.\d+)*)")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def check(appdir: Path) -> None:
    appdir = appdir.resolve()
    patterns = host_patterns()
    bad: list[str] = []
    newest: dict[str, tuple[tuple[int, ...], str]] = {}
    files = elf_files(appdir)
    for elf in files:
        rel = elf.relative_to(appdir)
        needed, _ = dynamic(elf)
        inside, outside = search_dirs(elf, appdir)
        if outside:
            bad.append(f"{rel}: RUNPATH outside the AppImage: {':'.join(outside)}")
        for name in needed:
            if not is_host(name, patterns) and resolve(name, inside, elf) is None:
                bad.append(f"{rel}: needs {name}, not inside the AppImage and not a host library")
        out = subprocess.run(
            ["readelf", "-VW", str(elf)], capture_output=True, text=True, check=False
        ).stdout
        for kind, ver in _GLIBC.findall(out):
            v = _version(ver)
            if kind not in newest or v > newest[kind][0]:
                newest[kind] = (v, str(rel))
    if bad:
        sys.exit(f"{len(bad)} problem(s):\n  " + "\n  ".join(bad[:80]))
    print(f"checked {len(files)} ELF files: every dependency is inside or on the host allow-list")
    for kind in ("GLIBC", "GLIBCXX", "CXXABI"):
        if kind in newest:
            v, who = newest[kind]
            print(f"newest {kind} {'.'.join(map(str, v))} ({who})")


# ---------------------------------------------------------------- Ubuntu packages
#: the licence of each Ubuntu source package whose libraries the AppImage may
#: carry, as Fedora's spec for the same library declares it
#: (src.fedoraproject.org, 2026-10-14; x264's from its own COPYING). A package
#: not listed here stops the build: a library nobody looked at the licence of
#: does not get conveyed.
DEB_LICENCES = {
    "libass": "ISC",
    "fribidi": "LGPL-2.1-or-later AND Unicode-DFS-2016",
    "lua5.2": "MIT",
    "x264": "GPL-2.0-or-later",
    "fftw3": "GPL-2.0-or-later AND MIT AND BSD-2-Clause",
    "libxrandr": "HPND-sell-variant",
    "libxrender": "HPND-sell-variant",
    "libxss": "X11",
    "libxpresent": "MIT",
    "libsm": "MIT AND MIT-open-group",
    "libice": "MIT-open-group",
    "xcb-util": "X11-distribute-modifications-variant",
    "xcb-util-cursor": "X11-distribute-modifications-variant",
    "xcb-util-wm": "X11-distribute-modifications-variant",
    "xcb-util-image": "X11-distribute-modifications-variant",
    "xcb-util-keysyms": "X11-distribute-modifications-variant",
    "xcb-util-renderutil": "X11-distribute-modifications-variant AND HPND-sell-variant",
}


def _dpkg_owner(path: str) -> str:
    out = subprocess.run(["dpkg", "-S", path], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        # /lib is a symlink to /usr/lib on 22.04; dpkg knows one spelling
        alt = path.replace("/usr/lib/", "/lib/", 1) if path.startswith("/usr/lib/") else (
            path.replace("/lib/", "/usr/lib/", 1))
        out = subprocess.run(["dpkg", "-S", alt], capture_output=True, text=True, check=True)
    return out.stdout.split(":", 1)[0].strip()


def debs(closure_json: Path, appdir: Path, specs: Path, sources: Path) -> None:
    copied: dict[str, str] = json.loads(closure_json.read_text(encoding="utf-8"))
    by_source: dict[tuple[str, str], list[str]] = {}
    for rel, origin in sorted(copied.items()):
        if origin.startswith("<appdir>/"):
            continue
        pkg = _dpkg_owner(origin)
        q = subprocess.run(
            ["dpkg-query", "-W", "-f", "${source:Package}\t${source:Version}\t${Version}", pkg],
            capture_output=True, text=True, check=True,
        ).stdout.split("\t")
        src, srcver = q[0], q[1]
        if src not in DEB_LICENCES:
            sys.exit(f"{rel} comes from Ubuntu's {pkg} (source {src}), whose licence "
                     f"stage.py does not know: add it to DEB_LICENCES after reading "
                     f"/usr/share/doc/{pkg}/copyright")
        lic_dir = appdir / "licenses" / src
        lic_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f"/usr/share/doc/{pkg}/copyright", lic_dir / "copyright")
        by_source.setdefault((src, srcver), []).append(rel)
    lines, srcs = [], []
    for (src, ver), files in sorted(by_source.items()):
        lines.append(f"{src}={ver}={DEB_LICENCES[src]}=licenses/{src}/copyright")
        srcs.append(f"{src}={ver}")
        print(f"  {src} {ver}: {', '.join(files)}")
    specs.write_text("\n".join(lines) + "\n", encoding="utf-8")
    sources.write_text("\n".join(srcs) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- Qt
#: the Qt libraries ButterEye uses (winstage.QT_KEEP, appstage.QT_KEEP_FRAMEWORKS)
#: and what the kept platform plugins link: XcbQpa (xcb), WaylandClient and
#: WlShellIntegration (wayland), OpenGL (the Wayland EGL client integration)
QT_KEEP_LIBS = (
    "Core", "Gui", "Widgets", "Svg", "SvgWidgets", "DBus",
    "XcbQpa", "WaylandClient", "WlShellIntegration", "OpenGL",
)  # fmt: skip
QT_KEEP_MODULES = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets")
#: Qt plugin folders and what to keep in them (None: everything)
PLUGIN_KEEP: dict[str, set[str] | None] = {
    "platforms": {"libqxcb.so", "libqwayland.so", "libqoffscreen.so", "libqminimal.so"},
    "xcbglintegrations": None,
    "wayland-shell-integration": None,
    "wayland-decoration-client": None,
    "wayland-graphics-integration-client": None,
    "platforminputcontexts": {"libcomposeplatforminputcontextplugin.so",
                              "libibusplatforminputcontextplugin.so"},
    "platformthemes": {"libqxdgdesktopportal.so"},
    "iconengines": {"libqsvgicon.so"},
    "imageformats": {"libqsvg.so"},
}  # fmt: skip
PYSIDE_DROP_DIRS = ("doc", "include", "glue", "typesystems", "scripts")
QT_DROP_DIRS = ("qml", "metatypes", "libexec")
SHIBOKEN_DROP_DIRS = ("include",)


def trim_qt(site: Path) -> None:
    st = _appstage()
    saved = 0
    pyside = site / "PySide6"
    qt = pyside / "Qt"
    for d in PYSIDE_DROP_DIRS:
        if (pyside / d).exists():
            saved += st._remove(pyside / d)
    for d in QT_DROP_DIRS:
        if (qt / d).exists():
            saved += st._remove(qt / d)
    keep_so = {f"Qt{m}.abi3.so" for m in QT_KEEP_MODULES}
    for p in sorted(pyside.iterdir()):
        name = p.name
        if p.is_file() and (
            (name.startswith("Qt") and name.endswith(".abi3.so") and name not in keep_so)
            or name.endswith(".pyi")
            or name.startswith("libpyside6qml")
            or (os.access(p, os.X_OK) and "." not in name)  # designer, lrelease, ...
        ):
            saved += st._remove(p)
    keep_lib = {f"libQt6{m}.so.6" for m in QT_KEEP_LIBS}
    for f in sorted((qt / "lib").iterdir()):
        stem = re.sub(r"(\.so\.6)(\..*)?$", r"\1", f.name)
        if f.name.startswith("libQt6") and stem not in keep_lib:
            saved += st._remove(f)
    plugins = qt / "plugins"
    for folder in sorted(plugins.iterdir()) if plugins.is_dir() else ():
        keep = PLUGIN_KEEP.get(folder.name, set())
        if keep == set():
            saved += st._remove(folder)
            continue
        for f in sorted(folder.iterdir()):
            if keep is not None and f.name not in keep:
                saved += st._remove(f)
    tr = qt / "translations"
    for f in sorted(tr.iterdir()) if tr.is_dir() else ():
        if not f.name.startswith("qtbase_"):
            saved += st._remove(f)
    shiboken = site / "shiboken6"
    for d in SHIBOKEN_DROP_DIRS:
        if (shiboken / d).exists():
            saved += st._remove(shiboken / d)
    for p in sorted(shiboken.glob("*.pyi")):
        saved += st._remove(p)
    print(f"trimmed {saved / 2**20:.1f} MiB of PySide6")


def qt_notices(out: Path, tarballs: list[Path]) -> None:
    """winstage.py qt-notices with the macOS list of third-party code inside Qt
    (appstage.QT_THIRD_PARTY_MACOS: Core, Gui, Svg, no Windows-only code).
    TRIAL SHORTCUT: the list has not been re-derived for the Linux wheel, whose
    xcb and Wayland plugins carry qtbase's xcb code and qtwayland's protocol
    files; qtwayland's sources are not pinned here at all."""
    st = _appstage()
    ws = st._winstage()
    ws.QT_THIRD_PARTY = st.QT_THIRD_PARTY_MACOS
    ws.qt_notices(out, tarballs)
    readme = out / "third-party" / "README.txt"
    text = readme.read_bytes().replace(b"\r\n", b"\n").decode()
    text = text.replace(
        "the shipped Qt6Core, Qt6Gui, Qt6Widgets, Qt6Svg,\nQt6SvgWidgets DLLs and Qt plugins",
        "the shipped libQt6Core, libQt6Gui, libQt6Widgets,\n"
        "libQt6Svg, libQt6SvgWidgets libraries and Qt plugins",
    )
    readme.write_text(text, encoding="utf-8")
    for f in (out / "third-party").glob("*.txt"):
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))


# ---------------------------------------------------------------- the source offer
SOURCES = """\
ButterEye {version} as an AppImage (x86_64 Linux, TRIAL BUILD): where the source is
=================================================================================

This AppImage is a feasibility trial (docs/appimage-trial.md in the repository),
not a release. ButterEye is free software under the GNU Affero General Public
License, version 3 or later (licenses/buttereye/COPYING). Everything in the
AppImage is free software too; each part's licence texts are in
licenses/<part>/, and buttereye-install.json (next to this folder) lists every
part with its version and licence.

The file ButterEye-{version}-appimage-sources.tar, published next to the
AppImage, holds the Corresponding Source of every part of it:

  buttereye-{version}/        ButterEye itself (also at {repo}), with
                              packaging/appimage/ (build-appimage.sh, which made
                              this AppImage, and its helpers) and
                              packaging/plugins/ (MVTools' patch)
  ffmpeg-{ffmpeg}.tar.xz       FFmpeg {ffmpeg}, built with --enable-gpl (libx264):
                              bin/ffmpeg, bin/ffprobe, lib/libav*.so, lib/libsw*.so
  dav1d-{dav1d}.tar.xz         dav1d {dav1d}, linked statically into libavcodec
  mpv-{mpv}.tar.gz             mpv {mpv}: bin/mpv
  libplacebo-{libplacebo}.tar.gz
                              libplacebo {libplacebo} (git {libplacebo_commit}, with
                              its 3rdparty submodules), linked statically into mpv;
                              its 3rdparty/Vulkan-Headers are also the Vulkan
                              headers FFmpeg and mpv were compiled against
  glslang-{glslang}.tar.gz      glslang {glslang}, linked statically into mpv
  wayland-{wayland}.tar.xz, wayland-protocols-{wayland_protocols}.tar.xz
                              Wayland {wayland} and wayland-protocols
                              {wayland_protocols}: mpv carries the code
                              wayland-scanner generated from their protocol XML
                              (libwayland itself is the host's)
  ffms2-{ffms2}.tar.gz          FFMS2 {ffms2}: vapoursynth/libffms2.so
  vapoursynth-mvtools-{mvtools}.tar.gz
                              MVTools {mvtools} with
                              packaging/plugins/buttereye-vs-mvtools-pkgconfig.patch:
                              vapoursynth/mvtools.so
  vapoursynth-{vapoursynth}.tar.gz      VapourSynth R{vapoursynth} (LGPL-2.1-or-later)
  qtbase-everywhere-src-{pyside}.tar.xz, qtsvg-everywhere-src-{pyside}.tar.xz,
  pyside-setup-everywhere-src-{pyside}.tar.xz
                              Qt {pyside}, PySide6 and Shiboken6 (LGPL-3.0-only)
  ubuntu/<package>/           the Ubuntu 22.04 source package of every library
                              the AppImage carries from Ubuntu's archive, at the
                              exact version it was copied from (apt-get source):
{deb_list}

What the AppImage carries and where each part came from
-------------------------------------------------------
  bin/mpv, bin/ffmpeg, bin/ffprobe, lib/, vapoursynth/
      built from the sources above by packaging/appimage/build-appimage.sh in an
      ubuntu:22.04 container, or copied from that container's Ubuntu packages
      (the libraries under ubuntu/). Their RUNPATH entries were set with
      patchelf so that they find each other inside the AppImage; nothing else
      was changed. To use a modified version of any library, build it from its
      source here and put it in place of lib/<name>.
  python/
      Python {python} from python-build-standalone {pbs} (PSF-2.0 and the
      licences in licenses/python/), unmodified:
      https://github.com/astral-sh/python-build-standalone/releases/tag/{pbs}
      with, in its site-packages, PySide6 and Shiboken6 {pyside} with Qt {pyside}
      (the manylinux_2_34 x86_64 wheels from PyPI, trimmed to the Qt parts
      ButterEye uses; RUNPATH entries of a few Qt libraries may have been set
      with patchelf when they needed a library copied in beside them),
      VapourSynth R{vapoursynth} (the manylinux_2_28 x86_64 wheel from PyPI,
      unmodified) and ButterEye itself. They are ordinary files there, so you
      can replace them with your own builds of the same versions.
  AppRun
      packaging/appimage/AppRun (AGPL-3.0-or-later)
  the AppImage runtime (the first part of the file)
      AppImage/type2-runtime {runtime} (MIT), unmodified:
      https://github.com/AppImage/type2-runtime/releases/tag/{runtime}

Written offer
-------------
For at least three years after the last time this version is distributed, the
ButterEye contributors will give anyone who asks, at no more than the cost of
providing it, the complete Corresponding Source of any part listed above.
Ask at {repo}/issues.
"""


def sources_txt(out: Path, values: list[str]) -> None:
    v = dict(item.split("=", 1) for item in values)
    debs_list = [line.split("=", 1) for line in Path(v.pop("debs")).read_text().splitlines() if line]
    v["deb_list"] = "\n".join(f"                                {s} {ver}" for s, ver in debs_list)
    out.write_text(SOURCES.format(**v), encoding="utf-8")


def main(argv: list[str]) -> None:
    cmd, *args = argv
    if cmd == "trim-qt":
        trim_qt(Path(args[0]))
    elif cmd == "closure":
        closure(Path(args[0]), Path(args[1]))
    elif cmd == "check":
        check(Path(args[0]))
    elif cmd == "debs":
        debs(Path(args[0]), Path(args[1]), Path(args[2]), Path(args[3]))
    elif cmd == "sources-txt":
        sources_txt(Path(args[0]), args[1:])
    elif cmd == "unwheel":
        _appstage().unwheel(Path(args[0]), Path(args[1]))
    elif cmd == "scan":
        _appstage().scan(Path(args[0]), args[1:])
    elif cmd == "record":
        _appstage().record(Path(args[0]), Path(args[1]), args[2:])
    elif cmd == "qt-notices":
        qt_notices(Path(args[0]), [Path(a) for a in args[1:]])
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    os.umask(0o022)
    main(sys.argv[1:])
