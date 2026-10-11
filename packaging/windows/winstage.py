# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Helpers for build-installer.sh (docs/PLATFORMS.md, phase 4). Standard library
only; run with ``python3 -I``.

  trim <site-packages>          drop what ButterEye never loads: the PySide6 parts
                                beyond QtCore/QtGui/QtWidgets/QtSvg (QML, Quick,
                                Designer, tools, other translations) and
                                VapourSynth's debug symbols (.pdb)
  check-imports <stage>         every Windows binary's imported DLLs that belong to
                                the bundle (Qt, PySide6, Python, VapourSynth, the
                                C++ runtime) are in it: the trim removed nothing
                                that something kept still needs
  record <components.json> <out> name=version=licence=file[,file...] ...
                                the install record (checks_pkgs.manifest_db):
                                the plugin build's entries plus the given ones
  sources-txt <out> key=value ...
                                licenses/SOURCES.txt, the source offer
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

#: the Qt modules ButterEye's GUI uses (QtCore, QtGui, QtWidgets) plus QtSvg,
#: which draws its SVG icons
QT_KEEP = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets")
#: Qt plugin folders and what to keep in them (None: everything)
PLUGIN_KEEP: dict[str, set[str] | None] = {
    "platforms": {"qwindows.dll", "qoffscreen.dll", "qminimal.dll"},
    "styles": None,
    "iconengines": None,
    "imageformats": None,  # but qpdf.dll, which needs Qt Pdf (below)
}
PLUGIN_DROP = {"imageformats/qpdf.dll"}
#: PySide6 folders ButterEye never needs at run time (QML, tools, build files)
PYSIDE_DROP_DIRS = (
    "qml", "doc", "include", "glue", "typesystems", "metatypes", "lib", "scripts", "resources",
)  # fmt: skip
#: an import whose name matches this must be satisfied by the bundle itself
BUNDLED_DLL = re.compile(
    r"^(qt6\w+|pyside6\S*|shiboken6\S*|python3\d*|vcruntime140\w*|msvcp140\w*|concrt140|"
    r"libvapoursynth\w*|vsscript|avscompat|vcomp140|ffms2|mvtools|librife|libmvgpu)\.dll$",
    re.I,
)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _remove(path: Path) -> int:
    n = _size(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return n


def trim(site: Path) -> None:
    saved = 0
    pyside = site / "PySide6"
    keep_dll = {f"qt6{m}.dll".lower() for m in QT_KEEP}
    keep_pyd = {f"qt{m}.pyd".lower() for m in QT_KEEP}
    for d in PYSIDE_DROP_DIRS:
        if (pyside / d).exists():
            saved += _remove(pyside / d)
    for p in sorted(pyside.iterdir()):
        name = p.name.lower()
        if not p.is_file():
            continue
        if (
            (name.startswith("qt6") and name.endswith(".dll") and name not in keep_dll)
            or (name.endswith(".pyd") and name.startswith("qt") and name not in keep_pyd)
            or name.endswith((".exe", ".lib", ".pyi"))
            or name in ("opengl32sw.dll", "pyside6qml.abi3.dll")
        ):
            saved += _remove(p)
    plugins = pyside / "plugins"
    for folder in sorted(plugins.iterdir()) if plugins.is_dir() else ():
        keep = PLUGIN_KEEP.get(folder.name, set())
        if keep == set():
            saved += _remove(folder)
            continue
        for f in sorted(folder.iterdir()):
            if (
                keep is not None and f.name not in keep
            ) or f"{folder.name}/{f.name}" in PLUGIN_DROP:
                saved += _remove(f)
    tr = pyside / "translations"
    for f in sorted(tr.iterdir()) if tr.is_dir() else ():
        if not f.name.startswith("qtbase_"):
            saved += _remove(f)
    for pdb in sorted(site.rglob("*.pdb")):
        saved += _remove(pdb)
    for junk in (site / "bin", *site.rglob("__pycache__")):
        if junk.exists():
            saved += _remove(junk)
    print(f"trimmed {saved / 2**20:.1f} MiB")


def pe_imports(path: Path) -> list[str]:
    """The DLL names in a PE file's import table (not the delay-load one)."""
    data = path.read_bytes()
    if data[:2] != b"MZ":
        return []

    def u16(o: int) -> int:
        return int.from_bytes(data[o : o + 2], "little")

    def u32(o: int) -> int:
        return int.from_bytes(data[o : o + 4], "little")

    pe = u32(0x3C)
    if data[pe : pe + 4] != b"PE\0\0":
        return []
    coff = pe + 4
    sections, opt_size = u16(coff + 2), u16(coff + 16)
    opt = coff + 20
    dirs = opt + (112 if u16(opt) == 0x20B else 96)
    import_rva = u32(dirs + 8)
    table = [
        (u32(s + 12), max(u32(s + 8), u32(s + 16)), u32(s + 20))
        for s in range(opt + opt_size, opt + opt_size + 40 * sections, 40)
    ]

    def offset(rva: int) -> int:
        for va, size, raw in table:
            if va <= rva < va + size:
                return rva - va + raw
        raise ValueError(f"{path}: RVA {rva:#x} outside every section")

    names: list[str] = []
    if not import_rva:
        return names
    d = offset(import_rva)
    while any(data[d : d + 20]):
        name_off = offset(u32(d + 12))
        names.append(data[name_off : data.index(b"\0", name_off)].decode("ascii"))
        d += 20
    return names


def check_imports(stage: Path) -> None:
    have = {p.name.lower() for p in stage.rglob("*") if p.suffix.lower() in (".dll", ".pyd")}
    missing: list[str] = []
    checked = 0
    for p in sorted(stage.rglob("*")):
        if p.suffix.lower() not in (".dll", ".pyd", ".exe") or not p.is_file():
            continue
        checked += 1
        for name in pe_imports(p):
            if BUNDLED_DLL.match(name) and name.lower() not in have:
                missing.append(f"{p.relative_to(stage)} needs {name}")
    if missing:
        sys.exit("DLLs missing from the bundle:\n  " + "\n  ".join(missing))
    print(f"imports of {checked} binaries are satisfied")


def record(components: Path, out: Path, extra: list[str]) -> None:
    data = json.loads(components.read_text(encoding="utf-8"))
    install = out.parent
    for spec in extra:
        name, version, licence, files = spec.split("=", 3)
        rel = sorted(f for f in files.split(",") if f)
        for f in rel:
            if not (install / f).is_file():
                sys.exit(f"{name}: licence file {f} is not in {install}")
        data["components"].append(
            {"name": name, "version": version, "license": licence, "provides": [],
             "licence_files": rel}
        )  # fmt: skip
    for c in data["components"]:
        for f in c["licence_files"]:
            if not (install / f).is_file():
                sys.exit(f"{c['name']}: licence file {f} is not in {install}")
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"install record: {len(data['components'])} components")


SOURCES = """\
ButterEye {version} for Windows: where the source code is
=========================================================

ButterEye is free software under the GNU Affero General Public License,
version 3 or later (licenses/buttereye/COPYING). Everything this installer
contains is free software too; each part's licence texts are in licenses/<part>/
and buttereye-install.json lists them.

1. Built for this installer
---------------------------
The file ButterEye-{version}-windows-sources.tar, published next to
ButterEye-{version}-setup.exe, is the complete Corresponding Source of what was
compiled for this installer, with the scripts that built it
(packaging/windows/ in ButterEye's source):

  buttereye/                  ButterEye {version} itself (also at {repo})
  vapoursynth/mvtools.dll     MVTools (GPL-2.0-or-later) with FFTW (GPL-2.0-or-later)
  vapoursynth/librife.dll     RIFE-ncnn-Vulkan with ncnn and glslang
  vapoursynth/libmvgpu.dll    mvgpu (AGPL-3.0-or-later)
  vapoursynth/ffms2.dll       FFMS2 {ffms2} with FFmpeg {ffmpeg} (LGPL-2.1-or-later),
                              dav1d {dav1d} and zlib linked in
  vulkaninfo.exe              Vulkan-Tools {vulkan_tools} with volk, and one patch
  share/rife-ncnn-models/     the RIFE models

ffms2.dll links FFmpeg statically. To use a modified FFmpeg, rebuild ffms2.dll
from that source with packaging/windows/build-tools-in-chroot.sh (it records the
exact FFmpeg configuration in licenses/ffms2/FFMPEG-CONFIGURATION.txt) and put
your build in place of vapoursynth\\ffms2.dll.

2. Included unchanged from their publishers
-------------------------------------------
  Python {python} (PSF-2.0)
      https://www.python.org/ftp/python/{python}/Python-{python}.tar.xz
  PySide6 and Shiboken6 {pyside} with Qt {pyside} (LGPL-3.0-only; Qt Core, Gui,
  Widgets and Svg)
      https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{pyside}-src/
      https://download.qt.io/official_releases/qt/{pyside_mm}/{pyside}/submodules/
          (qtbase-everywhere-src-{pyside}.tar.xz, qtsvg-everywhere-src-{pyside}.tar.xz)
  VapourSynth R{vapoursynth} (LGPL-2.1-or-later)
      https://github.com/vapoursynth/vapoursynth/releases/tag/R{vapoursynth}
      https://pypi.org/project/vapoursynth/{vapoursynth}/#files (vapoursynth-{vapoursynth}.tar.gz)

They are ordinary files in python\\ and python\\Lib\\site-packages\\, so you
can replace them with your own builds of the same versions.

3. Written offer
----------------
For at least three years after the last time this version is distributed, the
ButterEye contributors will give anyone who asks, at no more than the cost of
providing it, the complete Corresponding Source of any part listed above.
Ask at {repo}/issues.
"""


def sources_txt(out: Path, values: list[str]) -> None:
    v = dict(item.split("=", 1) for item in values)
    v["pyside_mm"] = ".".join(v["pyside"].split(".")[:2])
    out.write_text(SOURCES.format(**v), encoding="utf-8", newline="\r\n")


def main(argv: list[str]) -> None:
    cmd, *args = argv
    if cmd == "trim":
        trim(Path(args[0]))
    elif cmd == "check-imports":
        check_imports(Path(args[0]))
    elif cmd == "record":
        record(Path(args[0]), Path(args[1]), args[2:])
    elif cmd == "sources-txt":
        sources_txt(Path(args[0]), args[1:])
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    os.umask(0o022)
    main(sys.argv[1:])
