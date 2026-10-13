# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Helpers for build-app.sh (docs/PLATFORMS.md, "The .app"): the macOS peer of
packaging/windows/winstage.py. Standard library only; run with ``python3 -I``.

  collect <prefix> <resources> <out.json> <dest>=<file> ...
        copy each <file> to <resources>/<dest> and every Homebrew library it
        loads, transitively, to <resources>/lib/, and record where each came from
        (its formula and keg version) in <out.json>. A dependency that is neither
        macOS's own (/usr/lib, /System) nor under <prefix> stops the build.
  relocate <resources> <map.json>
        rewrite every load command of the collected files so that nothing names
        a path on the build machine: each library is @rpath/<name>, and each file
        gets the one LC_RPATH its folder needs (bin -> @executable_path/../lib,
        lib -> @loader_path, vapoursynth -> @loader_path/../lib)
  thin <dir>
        lipo every universal Mach-O under <dir> down to arm64
  trim-qt <site-packages>
        drop the PySide6 parts ButterEye never loads (as winstage.py trim does
        for Windows: Qt Core, Gui, Widgets, Svg, SvgWidgets and D-Bus, which
        QtGui links on macOS, and the cocoa, offscreen and minimal platforms, the
        macOS style and the SVG icon and image plugins)
  check <app>
        the bundle's own gate: every Mach-O is arm64 and names no file outside
        the bundle but macOS's own; every @rpath and @loader_path dependency
        resolves inside the bundle. Prints the highest LC_BUILD_VERSION minos,
        which build-app.sh writes as LSMinimumSystemVersion.
  scan <dir> <needle> ...
        fail if any file under <dir> (or inside a .whl/.zip there) contains one
        of the needles: the builder's user name, home folder and host name must
        not travel in the app or in the sources tar
  licences <prefix> <resources> <map.json> <out.txt>
        copy each bundled formula's licence texts from its keg into
        <resources>/licenses/<formula>/, and print one install-record spec per
        formula (name=version=licence=files) into <out.txt>
  formulae <map.json>
        print the formulae the collected files come from, one per line
  copyleft <prefix> <map.json>
        print those of them whose keg declares a GPL, LGPL or MPL licence
  app-components <components.json> <map.json> <out.json>
        the plugin build's component list with the ncnn the app carries named
  record <components.json> <out> <spec> ...
        the install record (checks_pkgs.manifest_db), as winstage.py record
  qt-notices <out> <tar.xz> ...
        the notices of the third-party code inside the shipped Qt binaries, as
        winstage.py qt-notices, with the macOS list of that code
  sources-txt <out> key=value ...
        licenses/SOURCES.txt, the source offer
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: macOS's own: in the dyld shared cache on every Mac of the minimum version
SYSTEM = ("/usr/lib/", "/System/Library/")
#: the load commands that name another file
DYLIB_CMDS = frozenset(
    {
        "LC_LOAD_DYLIB",
        "LC_LOAD_WEAK_DYLIB",
        "LC_REEXPORT_DYLIB",
        "LC_LAZY_LOAD_DYLIB",
        "LC_LOAD_UPWARD_DYLIB",
    }
)
#: where each kind of collected file lives, and the LC_RPATH it gets
RPATH = {
    "bin": "@executable_path/../lib",
    "lib": "@loader_path",
    "vapoursynth": "@loader_path/../lib",
}

#: the Qt frameworks and PySide6 modules ButterEye uses (winstage.QT_KEEP), plus
#: D-Bus, which QtGui links on macOS (measured: otool -L QtGui)
QT_KEEP_FRAMEWORKS = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets", "DBus")
QT_KEEP_MODULES = ("Core", "Gui", "Widgets", "Svg", "SvgWidgets")
#: Qt plugin folders and what to keep in them (None: everything)
PLUGIN_KEEP: dict[str, set[str] | None] = {
    "platforms": {"libqcocoa.dylib", "libqoffscreen.dylib", "libqminimal.dylib"},
    "styles": None,  # libqmacstyle.dylib
    "iconengines": {"libqsvgicon.dylib"},
    "imageformats": {"libqsvg.dylib"},
}
PYSIDE_DROP_DIRS = ("doc", "include", "glue", "typesystems", "scripts", "lib")
QT_DROP_DIRS = ("qml", "metatypes", "libexec")
SHIBOKEN_DROP_DIRS = ("include", "lib")


def _run(*argv: str) -> str:
    return subprocess.run(argv, check=True, capture_output=True, text=True).stdout


def is_macho(path: Path) -> bool:
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head in (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf")


def load_commands(path: Path) -> tuple[str | None, list[str], list[str], str]:
    """(install id or None, dependencies, LC_RPATHs, file type) of an arm64
    Mach-O, from ``otool -l`` (the whole file for a thin one)."""
    text = _run("otool", "-l", "-arch", "arm64", str(path))
    ident: str | None = None
    deps: list[str] = []
    rpaths: list[str] = []
    cmd = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("cmd "):
            cmd = line.split()[1]
        elif line.startswith("name ") and (cmd in DYLIB_CMDS or cmd == "LC_ID_DYLIB"):
            value = line[5:].rsplit(" (offset", 1)[0]
            if cmd == "LC_ID_DYLIB":
                ident = value
            else:
                deps.append(value)
        elif line.startswith("path ") and cmd == "LC_RPATH":
            rpaths.append(line[5:].rsplit(" (offset", 1)[0])
    header = _run("otool", "-hv", "-arch", "arm64", str(path)).splitlines()
    filetype = header[-1].split()[4] if header else "?"
    return ident, deps, rpaths, filetype


# ---------------------------------------------------------------- collect
def _keg(prefix: Path, real: Path) -> tuple[str, str]:
    """(formula, keg version) of a file in ``<prefix>/Cellar``, or ("", "")."""
    try:
        rel = real.relative_to(prefix / "Cellar")
    except ValueError:
        return "", ""
    return rel.parts[0], rel.parts[1]


def _resolve(dep: str, loader: Path, rpaths: list[str]) -> Path | None:
    if dep.startswith("@rpath/"):
        for rp in rpaths:
            base = rp.replace("@loader_path", str(loader.parent)).replace(
                "@executable_path", str(loader.parent)
            )
            cand = Path(base) / dep[len("@rpath/") :]
            if cand.exists():
                return cand
        return None
    if dep.startswith("@loader_path/"):
        cand = loader.parent / dep[len("@loader_path/") :]
        return cand if cand.exists() else None
    if dep.startswith("@executable_path/"):
        cand = loader.parent / dep[len("@executable_path/") :]
        return cand if cand.exists() else None
    cand = Path(dep)
    return cand if cand.exists() else None


def collect(prefix: Path, resources: Path, out: Path, specs: list[str]) -> None:
    files: list[dict[str, object]] = []
    by_real: dict[Path, dict[str, object]] = {}
    names: dict[str, Path] = {}
    todo: list[tuple[Path, str, bool]] = []
    for spec in specs:
        dest, _, src = spec.partition("=")
        todo.append((Path(src), dest, True))
    errors: list[str] = []
    while todo:
        src, dest, root = todo.pop(0)
        real = src.resolve()
        if real in by_real:
            continue
        ident, deps, rpaths, _ = load_commands(real)
        formula, version = _keg(prefix, real)
        entry: dict[str, object] = {
            "dest": dest,
            "source": str(src),
            "formula": formula,
            "version": version,
            "root": root,
            "refs": [],
        }
        by_real[real] = entry
        files.append(entry)
        name = Path(dest).name
        if name in names and names[name] != real:
            errors.append(f"two different files want the name {name}: {names[name]} and {real}")
        names[name] = real
        refs: list[list[str]] = []
        for dep in deps:
            if dep.startswith(SYSTEM):
                continue
            hit = _resolve(dep, real, rpaths)
            if hit is None:
                errors.append(f"{real}: cannot find {dep}")
                continue
            hreal = hit.resolve()
            if not str(hreal).startswith(str(prefix) + "/"):
                errors.append(f"{real}: links {dep} ({hreal}), outside {prefix}")
                continue
            if hreal in by_real:
                lib_name = Path(str(by_real[hreal]["dest"])).name
            else:
                # the library's own install name is the name everything else
                # asks for it by (libavcodec.63.dylib, not .63.1.102)
                lid, _, _, _ = load_commands(hreal)
                lib_name = Path(lid).name if lid else hit.name
                todo.append((hit, f"lib/{lib_name}", False))
            refs.append([dep, lib_name])
        entry["refs"] = refs
    if errors:
        sys.exit("cannot collect the bundle's libraries:\n  " + "\n  ".join(errors))
    for entry in files:
        dst = resources / str(entry["dest"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(str(entry["source"])).resolve(), dst)
        dst.chmod(0o755 if str(entry["dest"]).startswith("bin/") else 0o644)
    out.write_text(json.dumps(files, indent=2) + "\n", encoding="utf-8")
    kegs = sorted({(str(e["formula"]), str(e["version"])) for e in files if e["formula"]})
    print(f"collected {len(files)} files from {len(kegs)} Homebrew kegs")


# ---------------------------------------------------------------- relocate
def relocate(resources: Path, mapping: Path) -> None:
    files = json.loads(mapping.read_text(encoding="utf-8"))
    for entry in files:
        dest = resources / entry["dest"]
        folder = entry["dest"].split("/", 1)[0]
        ident, deps, rpaths, _ = load_commands(dest)
        argv = ["install_name_tool"]
        if ident is not None:
            argv += ["-id", f"@rpath/{dest.name}"]
        for old, name in entry["refs"]:
            argv += ["-change", old, f"@rpath/{name}"]
        for rp in rpaths:
            # /usr/lib/swift stays: it is where macOS keeps the Swift runtime
            # mpv links. Everything else names the build machine (Homebrew's
            # prefix, or the Xcode toolchain mpv was linked against).
            if not rp.startswith(SYSTEM):
                argv += ["-delete_rpath", rp]
        want = RPATH[folder]
        # a program gets its rpath even when it links nothing of the bundle's:
        # vulkaninfo dlopens "libvulkan.1.dylib" by leaf name, and dyld answers a
        # leaf name from the caller's LC_RPATHs (measured: without one it found
        # no loader at all once Homebrew's rpath was gone)
        if (entry["refs"] or folder == "bin") and want not in rpaths:
            argv += ["-add_rpath", want]
        if len(argv) > 1:
            subprocess.run([*argv, str(dest)], check=True, capture_output=True)
    print(f"relocated {len(files)} files")


# ---------------------------------------------------------------- thin / trim
def thin(top: Path) -> None:
    n = 0
    for p in sorted(top.rglob("*")):
        if not is_macho(p):
            continue
        archs = _run("lipo", "-archs", str(p)).split()
        if archs == ["arm64"]:
            continue
        if "arm64" not in archs:
            sys.exit(f"{p}: no arm64 slice ({' '.join(archs)})")
        tmp = p.with_name(p.name + ".thin")
        subprocess.run(["lipo", str(p), "-thin", "arm64", "-output", str(tmp)], check=True)
        mode = stat.S_IMODE(p.stat().st_mode)
        os.replace(tmp, p)
        p.chmod(mode)
        n += 1
    print(f"thinned {n} universal files to arm64")


def _size(path: Path) -> int:
    if path.is_file() or path.is_symlink():
        return path.lstat().st_size
    return sum(q.lstat().st_size for q in path.rglob("*") if q.is_file())


def _remove(path: Path) -> int:
    n = _size(path)
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    return n


def trim_qt(site: Path) -> None:
    saved = 0
    pyside = site / "PySide6"
    qt = pyside / "Qt"
    for d in PYSIDE_DROP_DIRS:
        if (pyside / d).exists():
            saved += _remove(pyside / d)
    for d in QT_DROP_DIRS:
        if (qt / d).exists():
            saved += _remove(qt / d)
    keep_so = {f"Qt{m}.abi3.so" for m in QT_KEEP_MODULES}
    for p in sorted(pyside.iterdir()):
        name = p.name
        if p.is_dir() and name.endswith(".app"):
            saved += _remove(p)
        elif p.is_file() and (
            (name.startswith("Qt") and name.endswith(".abi3.so") and name not in keep_so)
            or name.endswith(".pyi")
            or name.startswith("libpyside6qml")
            or (os.access(p, os.X_OK) and "." not in name)  # lrelease, qmlls, ...
        ):
            saved += _remove(p)
    keep_fw = {f"Qt{m}.framework" for m in QT_KEEP_FRAMEWORKS}
    for fw in sorted((qt / "lib").iterdir()):
        if fw.name not in keep_fw:
            saved += _remove(fw)
    plugins = qt / "plugins"
    for folder in sorted(plugins.iterdir()) if plugins.is_dir() else ():
        keep = PLUGIN_KEEP.get(folder.name, set())
        if keep == set():
            saved += _remove(folder)
            continue
        for f in sorted(folder.iterdir()):
            if keep is not None and f.name not in keep:
                saved += _remove(f)
    tr = qt / "translations"
    for f in sorted(tr.iterdir()) if tr.is_dir() else ():
        if not f.name.startswith("qtbase_"):
            saved += _remove(f)
    shiboken = site / "shiboken6"
    for d in SHIBOKEN_DROP_DIRS:
        if (shiboken / d).exists():
            saved += _remove(shiboken / d)
    for p in sorted(shiboken.glob("*.pyi")):
        saved += _remove(p)
    print(f"trimmed {saved / 2**20:.1f} MiB of PySide6")


# ---------------------------------------------------------------- check
def _executables_of(app: Path) -> list[Path]:
    return [p for p in app.rglob("*") if is_macho(p) and load_commands(p)[3] == "EXECUTE"]


def check(app: Path) -> None:
    contents = (app / "Contents").resolve()
    problems: list[str] = []
    minos: list[tuple[tuple[int, ...], str]] = []
    count = 0
    for p in sorted(contents.rglob("*")):
        if not is_macho(p):
            continue
        count += 1
        rel = str(p.relative_to(contents))
        archs = _run("lipo", "-archs", str(p)).split()
        if archs != ["arm64"]:
            problems.append(f"{rel}: architectures {' '.join(archs)}, not arm64 alone")
        ident, deps, rpaths, filetype = load_commands(p)
        for rp in rpaths:
            if rp.startswith("/") and not rp.startswith(SYSTEM):
                problems.append(f"{rel}: LC_RPATH {rp} names the build machine")
        if ident is not None and ident.startswith("/"):
            problems.append(f"{rel}: install name {ident} is an absolute path")
        for dep in deps:
            if dep.startswith(SYSTEM):
                continue
            if dep.startswith("/"):
                problems.append(f"{rel}: links {dep}, outside the bundle")
                continue
            # @executable_path in a library means "the program that loaded me",
            # which for a dylib is any of several; resolve it against each
            # program that could (the bundle's executables) and want one hit
            uses_exe = dep.startswith("@executable_path/") or (
                dep.startswith("@rpath/") and any(r.startswith("@executable_path") for r in rpaths)
            )
            if uses_exe and filetype != "EXECUTE":
                hits = [
                    _resolve(dep, exe, [r.replace("@loader_path", str(p.parent)) for r in rpaths])
                    for exe in _executables_of_cached(contents)
                ]
                hit = next((h for h in hits if h is not None), None)
            else:
                hit = _resolve(dep, p, rpaths)
            if hit is None:
                # PySide6's and Python's extension modules are loaded by the
                # interpreter, and dyld also searches the rpaths of the image
                # that loads them; accept a library the bundle has under that
                # exact relative name somewhere, and say so
                tail = dep.split("/", 1)[1] if "/" in dep else dep
                found = [q for q in contents.rglob(Path(tail).name) if str(q).endswith(tail)]
                if not found:
                    problems.append(f"{rel}: {dep} resolves to nothing in the bundle")
                continue
            try:
                hit.resolve().relative_to(contents)
            except ValueError:
                problems.append(f"{rel}: {dep} resolves outside the bundle ({hit})")
        build = _run("otool", "-l", "-arch", "arm64", str(p))
        m = re.search(r"\bminos (\S+)", build) or re.search(
            r"\bversion (\d+\.\d+(?:\.\d+)?)", build
        )
        if m:
            minos.append((tuple(int(x) for x in m.group(1).split(".")), rel))
    if problems:
        sys.exit(f"{len(problems)} problem(s) in {app}:\n  " + "\n  ".join(problems))
    top = max(minos)
    version = ".".join(str(x) for x in top[0])
    print(f"checked {count} Mach-O files: arm64, nothing outside the bundle but macOS's own")
    print(f"minos {version} ({top[1]})")


_EXE_CACHE: dict[Path, list[Path]] = {}


def _executables_of_cached(contents: Path) -> list[Path]:
    if contents not in _EXE_CACHE:
        _EXE_CACHE[contents] = _executables_of(contents)
    return _EXE_CACHE[contents]


# ---------------------------------------------------------------- scan
def _hits(data: bytes, needles: list[tuple[str, re.Pattern[bytes]]]) -> list[str]:
    return [name for name, pattern in needles if pattern.search(data)]


def scan(top: Path, needles: list[str]) -> None:
    # a needle counts only as a whole word: a short user name is a substring of
    # ordinary words (a user "ann" is in "channel"), and a leak is a path or a
    # host name, which always stands on its own
    want = [
        (n, re.compile(rb"(?<![A-Za-z0-9_.-])" + re.escape(n.encode()) + rb"(?![A-Za-z0-9_-])"))
        for n in needles
        if n
    ]
    bad: list[str] = []
    n = 0
    for p in sorted(top.rglob("*")):
        if p.is_symlink():
            target = os.readlink(p).encode()
            if hits := _hits(target, want):
                bad.append(f"{p.relative_to(top)} (symlink target): {', '.join(hits)}")
            continue
        if not p.is_file():
            continue
        n += 1
        data = p.read_bytes()
        if hits := _hits(data, want):
            bad.append(f"{p.relative_to(top)}: {', '.join(hits)}")
        if p.suffix in (".whl", ".zip") and zipfile.is_zipfile(p):
            with zipfile.ZipFile(p) as z:
                for info in z.infolist():
                    if hits := _hits(z.read(info), want):
                        bad.append(f"{p.relative_to(top)}!{info.filename}: {', '.join(hits)}")
    if bad:
        sys.exit(f"{len(bad)} file(s) name the build machine:\n  " + "\n  ".join(bad[:50]))
    print(f"scanned {n} files: none names the build machine")


# ---------------------------------------------------------------- licences
#: the names a keg's licence texts go by: brew.py's LICENCE_PREFIXES, plus an
#: SPDX-named text (glib installs only LGPL-2.1-or-later.txt)
_LICENCE = re.compile(r"(?i)^(licen[cs]e|copying|copyright|notice|unlicense|(a|l)?gpl-\d)")


def _brew_licence(prefix: Path, formula: str, version: str) -> str:
    """The formula's own SPDX expression from the keg's ``sbom.spdx.json``: the
    ``licenseConcluded`` of ``SPDXRef-Archive-<formula>-src``, which is what the
    doctor reads too (buttereye/core/doctor/brew.py, ``BrewKeg.licence``)."""
    sbom = prefix / "Cellar" / formula / version / "sbom.spdx.json"
    try:
        data = json.loads(sbom.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    for pkg in data.get("packages", []):
        if pkg.get("SPDXID") == f"SPDXRef-Archive-{formula}-src":
            lic = str(pkg.get("licenseConcluded") or "")
            return "" if lic in ("NOASSERTION", "NONE") else lic
    return ""


def licences(prefix: Path, resources: Path, mapping: Path, out: Path) -> None:
    files = json.loads(mapping.read_text(encoding="utf-8"))
    kegs = sorted({(e["formula"], e["version"]) for e in files if e["formula"]})
    specs: list[str] = []
    for formula, version in kegs:
        keg = prefix / "Cellar" / formula / version
        texts = sorted(p for p in keg.iterdir() if p.is_file() and _LICENCE.match(p.name))
        if not texts:
            sys.exit(f"{formula} {version}: no licence text in {keg}")
        folder = resources / "licenses" / formula
        folder.mkdir(parents=True, exist_ok=True)
        rel = []
        for t in texts:
            shutil.copy2(t, folder / t.name)
            (folder / t.name).chmod(0o644)
            rel.append(f"licenses/{formula}/{t.name}")
        licence = _brew_licence(prefix, formula, version)
        if not licence:
            sys.exit(f"{formula} {version}: its keg declares no licence (sbom.spdx.json)")
        name = RECORD_NAMES.get(formula, formula)
        specs.append(f"{name}={version}={licence}={','.join(rel)}")
    out.write_text("\n".join(specs) + "\n", encoding="utf-8")
    print(f"licence texts of {len(kegs)} Homebrew formulae")


#: install-record names that are not the formula's: the doctor asks for MVTools
#: as ButterEye's package name on every platform (checks_pkgs.PKG_MVTOOLS), and
#: on Fedora and Windows that package is ButterEye's own build
RECORD_NAMES = {"vapoursynth-mvtools": "buttereye-vs-mvtools"}
#: an SPDX expression carrying any of these words asks for its source to travel
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|MPL)-")


def copyleft(prefix: Path, mapping: Path) -> None:
    """The bundled formulae whose source the sources tar must carry."""
    files = json.loads(mapping.read_text(encoding="utf-8"))
    for f, v in sorted({(e["formula"], e["version"]) for e in files if e["formula"]}):
        if COPYLEFT.search(_brew_licence(prefix, f, v)):
            print(f)


def formulae(mapping: Path) -> None:
    files = json.loads(mapping.read_text(encoding="utf-8"))
    for f, v in sorted({(e["formula"], e["version"]) for e in files if e["formula"]}):
        print(f"{f} {v}")


# ---------------------------------------------------------------- winstage reuse
def _winstage():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "winstage", HERE.parent / "windows" / "winstage.py"
    )
    if spec is None or spec.loader is None:
        sys.exit("packaging/windows/winstage.py is missing")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


#: the third-party code inside the shipped Qt binaries on macOS, by
#: qt_attribution.json Id: winstage.QT_THIRD_PARTY without the code that only a
#: Windows build contains (Direct3D 12, Wintab, the X server helper), plus the
#: Cocoa platform plugin's own. More rather than fewer: a notice for code that
#: turns out not to be linked costs a file, a missing one is a licence breach.
QT_THIRD_PARTY_MACOS = (
    # Qt Core
    "blake2", "doubleconversion", "easing", "md4", "md5", "pcre2", "pcre2-sljit",
    "rfc6234", "sha1", "sha3_endian", "sha3_keccak", "siphash", "tinycbor", "zlib",
    "tlexpected", "tika-mimetypes", "unicode-character-database", "unicode-cldr",
    "forkfd",
    # Qt Gui (and the Cocoa platform plugin)
    "freetype", "freetype-zlib", "freetype-bdf", "freetype-pcf", "grayraster",
    "harfbuzz-ng", "emoji-segmenter", "libpng", "md4c", "icc-srgb-color-profile",
    "smooth-scaling-algorithm", "webgradients", "aglfn",
    "opengl-headers", "opengl-es2-headers", "vulkan-xml-spec", "vulkanmemoryallocator",
    # Qt Svg
    "xsvg",
)  # fmt: skip


def qt_notices(out: Path, tarballs: list[Path]) -> None:
    ws = _winstage()
    ws.QT_THIRD_PARTY = QT_THIRD_PARTY_MACOS
    ws.qt_notices(out, tarballs)
    readme = out / "third-party" / "README.txt"
    text = readme.read_bytes().replace(b"\r\n", b"\n").decode()
    text = text.replace(
        "the shipped Qt6Core, Qt6Gui, Qt6Widgets, Qt6Svg,\nQt6SvgWidgets DLLs and Qt plugins",
        "the shipped QtCore, QtGui, QtWidgets, QtSvg,\n"
        "QtSvgWidgets and QtDBus frameworks and Qt plugins",
    )
    readme.write_text(text, encoding="utf-8")
    for f in (out / "third-party").glob("*.txt"):
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))


def app_components(components: Path, mapping: Path, out: Path) -> None:
    """The plugin build's components.json, as the app carries it: there the RIFE
    plugin's ncnn is Homebrew's bottle, copied into the app beside it, so the
    record says which one (``bundled(ncnn)``, the provide the doctor reads to
    name the RIFE variant - checks_pkgs.rife_variant)."""
    data = json.loads(components.read_text(encoding="utf-8"))
    files = json.loads(mapping.read_text(encoding="utf-8"))
    kegs = {e["formula"]: e["version"].split("_")[0] for e in files if e["formula"]}
    for c in data["components"]:
        if c["name"] == "buttereye-vs-rife-ncnn" and "ncnn" in kegs:
            c["provides"] = [p for p in c.get("provides", []) if p[0] != "bundled(ncnn)"]
            c["provides"].append(["bundled(ncnn)", kegs["ncnn"]])
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def record(components: Path, out: Path, extra: list[str]) -> None:
    _winstage().record(components, out, extra)


SOURCES = """\
ButterEye {version} for macOS (Apple Silicon): where the source code is
=====================================================================

ButterEye is free software under the GNU Affero General Public License,
version 3 or later (licenses/buttereye/COPYING). Everything in ButterEye.app is
free software too; each part's licence texts are in licenses/<part>/, and
buttereye-install.json (next to this folder) lists every part with its version
and licence.

The file ButterEye-{version}-macos-sources.tar, published next to
ButterEye-{version}-macos-arm64.dmg, holds the Corresponding Source of every part
of the app that is under the GPL, the LGPL or the MPL, and the recipes and
scripts that built each one:

  buttereye-{version}/        ButterEye itself (also at {repo}), with packaging/
                              (build-app.sh, which made this app, and the
                              plugin build scripts) and mvgpu/
  plugins/                    ButterEye's builds of mvgpu and RIFE-ncnn-Vulkan
                              (Resources/vapoursynth/libmvgpu.dylib, librife.dylib):
                              the exact sources, patches and build scripts
  homebrew/<formula>/         every Homebrew bottle the app carries: the
                              formula's recipe exactly as the bottle was built
                              from it (<formula>.rb), and for each one under a
                              copyleft licence the source archive and patches
                              Homebrew built it from (brew fetch
                              --build-from-source, checked against the
                              formula's sha256)
  vapoursynth-{vapoursynth}.tar.gz      VapourSynth R{vapoursynth} (LGPL-2.1-or-later)
  qtbase-everywhere-src-{pyside}.tar.xz, qtsvg-everywhere-src-{pyside}.tar.xz,
  pyside-setup-everywhere-src-{pyside}.tar.xz
                              Qt {pyside}, PySide6 and Shiboken6 (LGPL-3.0-only)

What the app carries and where each part came from
---------------------------------------------------
  Resources/bin/mpv, ffmpeg, ffprobe, vulkaninfo, and Resources/lib/*.dylib
      Homebrew's bottles for macOS {macos_bottles} (arm64), versions as listed
      in buttereye-install.json:
{brew_list}
      Each binary is the bottle's, with two changes made by
      packaging/macos/build-app.sh and nothing else: its load commands were
      rewritten with install_name_tool so that it finds its libraries inside
      the app (@rpath, @loader_path) instead of under /opt/homebrew, and it was
      signed again, ad hoc, because that rewrite invalidates the signature.
      To use a modified version of any of these libraries, build it from the
      source in homebrew/<formula>/ with the recipe beside it, run
      install_name_tool -id @rpath/<name> on your build, and put it in place of
      Resources/lib/<name> (then codesign -s - --force it, and the app).
  Resources/vapoursynth/mvtools.dylib, libffms2.dylib
      Homebrew's vapoursynth-mvtools and ffms2 bottles, changed the same way
  Resources/vapoursynth/libmvgpu.dylib, librife.dylib
      built for this app by packaging/macos/build-plugins.sh (plugins/)
  Resources/share/rife-ncnn-models/
      RIFE model weights (MIT; data, not a program): provenance in
      licenses/buttereye-rife-ncnn-models/
  Resources/python/
      Python {python} from python-build-standalone {pbs} (PSF-2.0 and the
      licences in licenses/python/), unmodified:
      https://github.com/astral-sh/python-build-standalone/releases/tag/{pbs}
      with, in its site-packages, PySide6 and Shiboken6 {pyside} with Qt {pyside}
      (the macOS wheels from PyPI, thinned to arm64 and trimmed to the Qt
      modules ButterEye uses: Core, Gui, Widgets, Svg, SvgWidgets and D-Bus,
      with the cocoa, offscreen and minimal platform plugins, the macOS style
      and the SVG icon and image plugins), VapourSynth R{vapoursynth} (the
      macOS arm64 wheel from PyPI, unmodified) and ButterEye itself. They are
      ordinary files there, so you can replace them with your own builds of
      the same versions.
  Contents/MacOS/ButterEye
      packaging/macos/launcher.c, compiled by build-app.sh (AGPL-3.0-or-later)

Code under permissive licences (BSD, MIT, Apache-2.0, ISC, Zlib and the like)
asks for no source offer; for those the recipe is in the tar all the same, and
`brew fetch --build-from-source <formula>` retrieves the source it names.

Written offer
-------------
For at least three years after the last time this version is distributed, the
ButterEye contributors will give anyone who asks, at no more than the cost of
providing it, the complete Corresponding Source of any part listed above.
Ask at {repo}/issues.
"""


def sources_txt(out: Path, values: list[str]) -> None:
    v = dict(item.split("=", 1) for item in values)
    kegs = [line.split() for line in Path(v.pop("formulae")).read_text().splitlines() if line]
    v["brew_list"] = "\n".join(f"        {f} {ver}" for f, ver in kegs)
    out.write_text(SOURCES.format(**v), encoding="utf-8")


def unwheel(wheel: Path, dest: Path) -> None:
    """Install a wheel by unpacking it, keeping each file's mode (vspipe must stay
    executable). Not pip: pip records a local wheel's path in direct_url.json,
    and that path is the builder's home folder."""
    with zipfile.ZipFile(wheel) as z:
        for info in z.infolist():
            target = dest / info.filename
            if not target.resolve().is_relative_to(dest.resolve()):
                sys.exit(f"{wheel}: {info.filename} would land outside {dest}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
            mode = (info.external_attr >> 16) & 0o777
            target.chmod(0o755 if mode & 0o111 else 0o644)
    print(f"unpacked {wheel.name}")


def main(argv: list[str]) -> None:
    cmd, *args = argv
    if cmd == "unwheel":
        unwheel(Path(args[0]), Path(args[1]))
    elif cmd == "collect":
        collect(Path(args[0]), Path(args[1]), Path(args[2]), args[3:])
    elif cmd == "relocate":
        relocate(Path(args[0]), Path(args[1]))
    elif cmd == "thin":
        thin(Path(args[0]))
    elif cmd == "trim-qt":
        trim_qt(Path(args[0]))
    elif cmd == "check":
        check(Path(args[0]))
    elif cmd == "scan":
        scan(Path(args[0]), args[1:])
    elif cmd == "licences":
        licences(Path(args[0]), Path(args[1]), Path(args[2]), Path(args[3]))
    elif cmd == "copyleft":
        copyleft(Path(args[0]), Path(args[1]))
    elif cmd == "formulae":
        formulae(Path(args[0]))
    elif cmd == "app-components":
        app_components(Path(args[0]), Path(args[1]), Path(args[2]))
    elif cmd == "record":
        record(Path(args[0]), Path(args[1]), args[2:])
    elif cmd == "qt-notices":
        qt_notices(Path(args[0]), [Path(a) for a in args[1:]])
    elif cmd == "sources-txt":
        sources_txt(Path(args[0]), args[1:])
    else:
        sys.exit(f"unknown command {cmd}")


if __name__ == "__main__":
    os.umask(0o022)
    main(sys.argv[1:])
