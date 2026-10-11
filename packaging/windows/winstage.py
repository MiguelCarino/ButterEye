# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Helpers for build-installer.sh (docs/PLATFORMS.md, phase 4). Standard library
only; run with ``python3 -I``.

  trim <site-packages>          drop what ButterEye never loads: the PySide6 parts
                                beyond QtCore/QtGui/QtWidgets/QtSvg (QML, Quick,
                                Designer, tools, image formats but SVG, other
                                translations), the wheels' copies of Microsoft's
                                C++ runtime (the system's is used, see VC_RUNTIME)
                                and VapourSynth's debug symbols (.pdb); note in the
                                wheels' licence file which licence this copy is under
  check-imports <stage>         every Windows binary's imported DLLs that belong to
                                the bundle (Qt, PySide6, Python, VapourSynth) are in
                                it: the trim removed nothing that something kept
                                still needs; and no Microsoft runtime DLL is shipped
                                but Python's own
  qt-notices <out> <tar.xz>...  licence notices of the third-party code inside the
                                shipped Qt binaries, from the Qt source tarballs'
                                qt_attribution.json (and Qt's LGPL/GPL texts); prints
                                the record's licence expression and provides
  vc-note <out> <python-version> <vcruntime140.dll>
                                the note on Python's copies of Microsoft's runtime;
                                prints their version
  record <components.json> <out> name=version=licence=file[,file...][=provides] ...
                                the install record (checks_pkgs.manifest_db):
                                the plugin build's entries plus the given ones;
                                provides: what(version),... (e.g. bundled(zlib)(1.3))
  sources-txt <out> key=value ...
                                licenses/SOURCES.txt, the source offer
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path

#: the Qt modules ButterEye's GUI uses (QtCore, QtGui, QtWidgets) plus QtSvg,
#: which draws its SVG icons
QT_KEEP = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets")
#: Qt plugin folders and what to keep in them (None: everything)
#: ButterEye draws only SVG icons (buttereye/gui/icons/*.svg, through QIcon, which
#: uses iconengines/qsvgicon.dll and imageformats/qsvg.dll, both Qt SVG); PNG and BMP
#: are built into Qt Gui. The other image formats (JPEG, TIFF, WebP, GIF, ICO, ICNS,
#: TGA, WBMP: qtbase and qtimageformats, with libjpeg-turbo, libtiff, libwebp inside)
#: are never used, so they are not shipped.
PLUGIN_KEEP: dict[str, set[str] | None] = {
    "platforms": {"qwindows.dll", "qoffscreen.dll", "qminimal.dll"},
    "styles": None,  # qmodernwindowsstyle.dll (qtbase)
    "iconengines": {"qsvgicon.dll"},
    "imageformats": {"qsvg.dll"},
}
#: PySide6 folders ButterEye never needs at run time (QML, tools, build files)
PYSIDE_DROP_DIRS = (
    "qml", "doc", "include", "glue", "typesystems", "metatypes", "lib", "scripts", "resources",
)  # fmt: skip
#: Shiboken6's build files (C++ headers, CMake config)
SHIBOKEN_DROP_DIRS = ("include", "lib")
#: Microsoft's Visual C++ runtime. The PySide6 and Shiboken6 wheels each carry a
#: copy; ButterEye ships neither: Qt uses the system's (VapourSynth needs Microsoft's
#: redistributable, 14.50 or newer, installed machine-wide anyway: the installer
#: offers it and the doctor checks it, packages.vcruntime). The only copies shipped
#: are vcruntime140.dll and vcruntime140_1.dll in python\, part of Python's official
#: embeddable build and redistributed under the terms in its LICENSE.txt.
VC_RUNTIME = re.compile(
    r"^(vcruntime140\w*|msvcp140\w*|concrt140|vcomp140\w*|vcamp140\w*|vccorlib140\w*|"
    r"ucrtbase\w*|api-ms-win-crt-\S+)\.dll$",
    re.I,
)
#: where the Python build's own copies sit (relative to the stage)
PYTHON_VC_RUNTIME = ("python/vcruntime140.dll", "python/vcruntime140_1.dll")
#: an import whose name matches this must be satisfied by the bundle itself
#: (msvcp140, concrt140 and friends come from the system's Visual C++ runtime)
BUNDLED_DLL = re.compile(
    r"^(qt6\w+|pyside6\S*|shiboken6\S*|python3\d*|vcruntime140(_1)?|"
    r"libvapoursynth\w*|vsscript|avscompat|ffms2|mvtools|librife|libmvgpu)\.dll$",
    re.I,
)
#: the wheels' licence file: Qt's commercial licence, which doesn't apply to this copy
QT_COMMERCIAL = "LicenseRef-Qt-Commercial.txt"
QT_COMMERCIAL_NOTE = """\
NOTE (added by ButterEye's installer build)
==========================================

The {wheel} wheel this copy comes from carries, under this file
name, the text of The Qt Company's commercial licence. That licence does not apply
here: ButterEye's Windows installer distributes this unmodified copy of {what}
under the GNU Lesser General Public License, version 3 (LGPL-3.0-only), one of the
licences the wheel offers (its METADATA: "LGPL-3.0-only OR GPL-2.0-only OR
GPL-3.0-only").

The licence texts are in the installation's licenses\\pyside6\\ folder
(LGPL-3.0-only.txt and the GPL-3.0-only.txt it builds on), and the notices of the
third-party code inside Qt are in licenses\\pyside6\\third-party\\.
licenses\\SOURCES.txt says where the source code is.
"""


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
            if keep is not None and f.name not in keep:
                saved += _remove(f)
    shiboken = site / "shiboken6"
    for d in SHIBOKEN_DROP_DIRS:
        if (shiboken / d).exists():
            saved += _remove(shiboken / d)
    for folder in (pyside, shiboken):
        for p in sorted(folder.iterdir()) if folder.is_dir() else ():
            if p.is_file() and (VC_RUNTIME.match(p.name) or p.suffix in (".lib", ".pyi")):
                saved += _remove(p)
    _note_qt_commercial(site)
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


def _note_qt_commercial(site: Path) -> None:
    """Replace the wheels' copy of Qt's commercial licence with a note saying which
    licence this copy is distributed under, and keep their RECORD true to it."""
    for dist in sorted(site.glob("*.dist-info")):
        lic = dist / "licenses" / QT_COMMERCIAL
        if not lic.is_file():
            continue
        wheel = dist.name.split("-")[0]
        what = "PySide6 and Qt" if wheel.startswith("pyside6") else "Shiboken6"
        data = QT_COMMERCIAL_NOTE.format(wheel=wheel, what=what).replace("\n", "\r\n").encode()
        lic.write_bytes(data)
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        rel = f"{dist.name}/licenses/{QT_COMMERCIAL}"
        record = dist / "RECORD"
        lines = record.read_text(encoding="utf-8").splitlines()
        hits = [i for i, line in enumerate(lines) if line.split(",")[0] == rel]
        if len(hits) != 1:
            sys.exit(f"{record}: no single entry for {rel}")
        lines[hits[0]] = f"{rel},sha256={digest},{len(data)}"
        record.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    vc = sorted(
        str(p.relative_to(stage)).replace(os.sep, "/")
        for p in stage.rglob("*")
        if p.is_file() and VC_RUNTIME.match(p.name)
    )
    unexpected = [f for f in vc if f not in PYTHON_VC_RUNTIME]
    if unexpected:
        sys.exit(
            "Microsoft runtime DLLs in the bundle besides Python's own (see VC_RUNTIME):\n  "
            + "\n  ".join(unexpected)
        )
    print(f"imports of {checked} binaries are satisfied; Microsoft runtime DLLs: {', '.join(vc)}")


#: the third-party code inside the shipped Qt binaries (Qt6Core, Qt6Gui, Qt6Widgets,
#: Qt6Svg, Qt6SvgWidgets and the kept plugins, Windows x64), by qt_attribution.json
#: Id. Left out, as not in those binaries on Windows: Android, Wasm, Cocoa, XCB,
#: Wayland and D-Bus code, forkfd (Unix), Pixman (ARM NEON only), libjpeg-turbo (the
#: qjpeg plugin, not shipped), libpsl (Qt Network), SQLite (Qt SQL), build tools.
QT_THIRD_PARTY = (
    # Qt Core
    "blake2", "doubleconversion", "easing", "md4", "md5", "pcre2", "pcre2-sljit",
    "rfc6234", "sha1", "sha3_endian", "sha3_keccak", "siphash", "tinycbor", "zlib",
    "tlexpected", "tika-mimetypes", "unicode-character-database", "unicode-cldr",
    # Qt Gui (and the Windows platform plugin)
    "freetype", "freetype-zlib", "freetype-bdf", "freetype-pcf", "grayraster",
    "harfbuzz-ng", "emoji-segmenter", "libpng", "md4c", "icc-srgb-color-profile",
    "smooth-scaling-algorithm", "xserverhelper", "webgradients", "aglfn",
    "opengl-headers", "opengl-es2-headers", "vulkan-xml-spec", "vulkanmemoryallocator",
    "d3d12memoryallocator", "rhi-miniengine-d3d12-mipmap", "wintab",
    # Qt Svg
    "xsvg",
)  # fmt: skip
#: Qt's own licence texts, from qtbase's LICENSES folder
QT_OWN = ("LGPL-3.0-only.txt", "GPL-3.0-only.txt")
_SPDX_WORD = re.compile(r"[A-Za-z0-9.+-]+")


def _qt_sources(tarballs: list[Path]) -> dict[str, bytes]:
    """The qt_attribution.json files, the files next to them and the LICENSES
    folders of the given Qt source tarballs, by path (one pass over each)."""
    files: dict[str, bytes] = {}
    for tb in tarballs:
        with tarfile.open(tb, "r:xz") as tar:
            for m in tar:
                parts = m.name.split("/")
                wanted = (
                    m.isfile()
                    and "testlib" not in parts
                    and (parts[1:2] == ["LICENSES"] or "src" in parts or "util" in parts)
                    and (
                        parts[1:2] == ["LICENSES"]
                        or m.name.endswith("qt_attribution.json")
                        or re.search(r"(?i)(licen[cs]e|copying|copyright|notice)", parts[-1])
                    )
                )
                f = tar.extractfile(m) if wanted else None
                if f is not None:
                    files[m.name] = f.read()
    return files


def _attributions(files: dict[str, bytes]) -> dict[str, tuple[str, dict[str, object]]]:
    out: dict[str, tuple[str, dict[str, object]]] = {}
    for name, data in sorted(files.items()):
        if not name.endswith("/qt_attribution.json"):
            continue
        entries = json.loads(data.decode("utf-8"), strict=False)
        for e in entries if isinstance(entries, list) else [entries]:
            out.setdefault(str(e.get("Id")), (name.rsplit("/", 1)[0], e))
    return out


def qt_notices(out: Path, tarballs: list[Path]) -> None:
    files = _qt_sources(tarballs)
    attr = _attributions(files)
    licenses_dirs = sorted({n.split("/")[0] for n in files if n.split("/")[1:2] == ["LICENSES"]})

    def spdx_text(word: str) -> bytes | None:
        for top in licenses_dirs:
            if f"{top}/LICENSES/{word}.txt" in files:
                return files[f"{top}/LICENSES/{word}.txt"]
        return None

    third = out / "third-party"
    third.mkdir(parents=True, exist_ok=True)
    for name in QT_OWN:
        text = spdx_text(name.removesuffix(".txt"))
        if text is None:
            sys.exit(f"Qt's {name} is not in the Qt sources")
        (out / name).write_bytes(text)
    missing = [i for i in QT_THIRD_PARTY if i not in attr]
    if missing:
        sys.exit(f"not in the Qt sources' qt_attribution.json: {', '.join(missing)}")
    exprs: list[str] = []
    provides: list[str] = []
    index = [
        "Third-party code inside Qt (the shipped Qt6Core, Qt6Gui, Qt6Widgets, Qt6Svg,",
        "Qt6SvgWidgets DLLs and Qt plugins), from Qt's qt_attribution.json files",
        "",
    ]
    for ident in QT_THIRD_PARTY:
        where, e = attr[ident]
        lic_id = str(e.get("LicenseId", "")).strip()
        lic_files = e.get("LicenseFiles") or ([e["LicenseFile"]] if e.get("LicenseFile") else [])
        texts: list[tuple[str, bytes]] = []
        for lf in lic_files if isinstance(lic_files, list) else []:
            path = os.path.normpath(f"{where}/{lf}").replace(os.sep, "/")
            if path not in files:
                sys.exit(f"{ident}: licence file {path} is not in the Qt sources")
            texts.append((str(lf), files[path]))
        for word in _SPDX_WORD.findall(lic_id):
            if word in ("AND", "OR", "WITH"):
                continue
            text = spdx_text(word)
            if text is not None:
                texts.append((f"LICENSES/{word}.txt", text))
            elif not texts:
                sys.exit(f"{ident}: no text for licence {word}")
        version = str(e.get("Version", "")).strip()
        copyright_ = e.get("Copyright", "")
        if isinstance(copyright_, list):
            copyright_ = "\n".join(map(str, copyright_))
        head = [
            f"{e.get('Name', ident)}",
            "=" * len(str(e.get("Name", ident))),
            f"Qt attribution id: {ident} ({where.split('/', 1)[1]})",
            f"Version: {version or 'not stated'}",
            f"Licence: {lic_id}",
            f"How Qt uses it: {e.get('QtUsage', '')}".strip(),
            f"Homepage: {e.get('Homepage', '') or e.get('DownloadLocation', '')}".rstrip(),
            "",
            "Copyright:",
            str(copyright_).strip() or "(see the licence text below)",
            "",
        ]
        body = "\n".join(head).encode()
        for label, text in texts:
            body += f"\n----- {label} -----\n\n".encode() + text.rstrip(b"\n") + b"\n"
        (third / f"{ident}.txt").write_bytes(body.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        index.append(f"  {ident + '.txt':34} {e.get('Name', ident)} ({lic_id})")
        for part in [f"({lic_id})"] if " OR " in lic_id else lic_id.split(" AND "):
            if part not in exprs:
                exprs.append(part)
        if re.fullmatch(r"[\w.+~^-]+", version):
            provides.append(f"bundled({ident})({version})")
    (third / "README.txt").write_bytes("\r\n".join(index + [""]).encode())
    print(" AND ".join(exprs))
    print(",".join(provides))


VC_NOTE = """\
python\\vcruntime140.dll and python\\vcruntime140_1.dll (version {version})
============================================================================

These two files are Microsoft's Visual C++ runtime, copyright Microsoft
Corporation. They come unmodified with Python's official embeddable build for
Windows (python-{python}-embed-amd64.zip, from python.org), which ButterEye's
installer includes as it is, and are redistributed under the "Additional
Conditions for this Windows binary build" in LICENSE.txt (in this folder): among
them, they may run only on Microsoft's operating systems, and Microsoft's
copyright notices in them must not be altered. They are not free software, are
not covered by ButterEye's licence, and have no source code to offer.

The rest of Microsoft's Visual C++ runtime that ButterEye needs (msvcp140.dll and
the others, for Qt and VapourSynth) is not included: it comes from Microsoft's
own redistributable, installed on the PC (the installer offers to download it
from Microsoft; "buttereye doctor" checks it).
"""


def vc_note(out: Path, python: str, dll: Path) -> None:
    version = vc_version(dll)
    out.write_text(VC_NOTE.format(version=version, python=python), encoding="utf-8", newline="\r\n")
    print(version)


def vc_version(path: Path) -> str:
    """A PE file's FileVersion string (its VS_VERSION_INFO StringFileInfo)."""
    data = path.read_bytes()
    key = "FileVersion".encode("utf-16-le") + b"\0\0"
    at = data.find(key)
    if at < 0:
        sys.exit(f"{path}: no FileVersion")
    at += len(key)
    at += (-at) % 4  # the value is DWORD-aligned
    end = at
    while data[end : end + 2] != b"\0\0":
        end += 2
    return data[at:end].decode("utf-16-le").split()[0]


def dedupe_and(expr: str) -> str:
    """``A AND B AND A`` -> ``A AND B``; expressions with OR or parentheses as they are."""
    if " OR " in expr or "(" in expr:
        return expr
    return " AND ".join(dict.fromkeys(p.strip() for p in expr.split(" AND ")))


def _provides(spec: str) -> list[list[str]]:
    out = []
    for item in filter(None, spec.split(",")):
        m = re.fullmatch(r"(.+\))\((.+)\)", item)
        if not m:
            sys.exit(f"provides entry {item!r} is not what(version)")
        out.append([m[1], m[2]])
    return out


def record(components: Path, out: Path, extra: list[str]) -> None:
    data = json.loads(components.read_text(encoding="utf-8"))
    install = out.parent
    for spec in extra:
        name, version, licence, files, *prov = spec.split("=", 4)
        rel = sorted(f for f in files.split(",") if f)
        for f in rel:
            if not (install / f).is_file():
                sys.exit(f"{name}: licence file {f} is not in {install}")
        data["components"].append(
            {"name": name, "version": version, "license": licence,
             "provides": _provides(prov[0]) if prov else [], "licence_files": rel}
        )  # fmt: skip
    for c in data["components"]:
        c["license"] = dedupe_and(c["license"])
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
contains is free software too, except Python's two copies of Microsoft's C++
runtime (licenses/python/MICROSOFT-VCRUNTIME.txt). Each part's licence texts are
in licenses/<part>/ and buttereye-install.json lists them.

1. Built for this installer
---------------------------
The file ButterEye-{version}-windows-sources.tar, published next to
ButterEye-{version}-setup.exe, holds the complete Corresponding Source of what was
compiled for this installer, with the scripts that built it:

  buttereye/                  ButterEye {version} itself (also at {repo}),
                              with packaging/ (the build scripts, the plugins'
                              specs, patches and licence texts) and mvgpu/
  vapoursynth/mvtools.dll     MVTools (GPL-2.0-or-later) with FFTW
                              (GPL-2.0-or-later; Fedora's {fftw_srpm})
  vapoursynth/librife.dll     RIFE-ncnn-Vulkan with ncnn and glslang
  vapoursynth/libmvgpu.dll    mvgpu (AGPL-3.0-or-later)
  vapoursynth/ffms2.dll       FFMS2 {ffms2} with FFmpeg {ffmpeg} (LGPL-2.1-or-later)
                              and dav1d {dav1d} linked in
  vulkaninfo.exe              Vulkan-Tools {vulkan_tools} with volk, and one patch

Its sources/ folder also has the archive the RIFE models in share/rife-ncnn-models/
come from ({models}: model weights, data rather than compiled code; the
script that picks them is packaging/plugins/repack-rife-ncnn.sh).

ffms2.dll also links zlib {zlib} (Zlib licence, which asks for no source), from
Fedora's mingw64-zlib-static package; its source is Fedora's mingw-zlib source
package (dnf download --source mingw-zlib on Fedora 44).

To rebuild: unpack the tar, copy sources/*.tar.gz into
$XDG_CACHE_HOME/buttereye/packaging/SOURCES (~/.cache/... by default), and run
packaging/windows/build-plugins.sh on Fedora 44 with mock (packaging/README.md).
The build still downloads the FFmpeg, dav1d, ffms2, Vulkan-Tools and volk
sources (the pinned URLs and SHA-256 sums in packaging/windows/pins.sh), so the
result is the same files; copies of them are also under sources/. ffms2.dll links FFmpeg statically: to use a
modified FFmpeg, rebuild ffms2.dll from that source with
packaging/windows/build-tools-in-chroot.sh (it records the exact FFmpeg
configuration in licenses/ffms2/FFMPEG-CONFIGURATION.txt) and put your build in
place of vapoursynth\\ffms2.dll.

2. Included unchanged from their publishers
-------------------------------------------
  Python {python} (PSF-2.0 and the licences in its LICENSE.txt)
      https://www.python.org/ftp/python/{python}/Python-{python}.tar.xz
  PySide6 and Shiboken6 {pyside} with Qt {pyside} (LGPL-3.0-only): Qt Core, Gui,
  Widgets, Svg and SvgWidgets, and the Qt plugins qwindows, qoffscreen, qminimal
  (platforms), qmodernwindowsstyle (styles), qsvgicon (iconengines) and qsvg
  (imageformats), all from qtbase and qtsvg
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
    elif cmd == "qt-notices":
        qt_notices(Path(args[0]), [Path(a) for a in args[1:]])
    elif cmd == "vc-note":
        vc_note(Path(args[0]), args[1], Path(args[2]))
    elif cmd == "record":
        record(Path(args[0]), Path(args[1]), args[2:])
    elif cmd == "sources-txt":
        sources_txt(Path(args[0]), args[1:])
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    os.umask(0o022)
    main(sys.argv[1:])
