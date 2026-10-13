# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The macOS build of ButterEye's VapourSynth plugins (docs/PLATFORMS.md, macOS
phase 3): read as Mach-O files, then loaded into VapourSynth and made to
interpolate on this Mac's GPU.

macOS only, and only when ``BUTTEREYE_TEST_PLUGINS`` names the directory
``packaging/macos/build-plugins.sh`` wrote - ``$XDG_CACHE_HOME/buttereye/macos``,
whose ``vapoursynth/`` subdirectory holds the dylibs; either level may be named,
every ``*.dylib`` below it is checked. That is the variable ``tests/windows``
uses for the same job, and it is needed here for the reason phase 2 needed
``BUTTEREYE_TEST_BREW``: GitHub's ``macos-latest`` runner builds no plugin, and
a test that fails there rather than skipping fails the gate for a machine that
was never asked to have one.

What phase 3 delivers on macOS, and so what is checked here, is one built
plugin: ``libmvgpu.dylib``. MVTools and ffms2 are Homebrew bottles - nothing is
built, so there is nothing here to check that ``tests/macos/test_doctor_macos.py``
and the integration tests do not already check against the installed ones - and
RIFE-ncnn is a spike. When RIFE does build, its test belongs here next to
mvgpu's and ``BREW_LIBS`` below has to grow the libraries it links; until then a
test for it would be a failing test, not a to-do.

Three checks, each refusing a different broken build:

* the Mach-O header and symbol table, parsed here rather than read out of
  ``otool``/``nm``, so this test and ``packaging/macos/check-dylibs.sh`` are two
  independent readings of the same file. An x86_64 build, a build that exports
  no ``_VapourSynthPluginInit2`` (VapourSynth skips the file in silence), and a
  build that links a library a user's Mac has not got all fail here.
* VapourSynth loads the file *by its absolute path* and the plugin it got is the
  one in the directory - asserted through ``Plugin.plugin_path``, because
  Homebrew's autoload directory supplies ``mv`` and ``ffms2`` to the same core
  and a namespace alone would not say which file answered.
* the interpolated frame really is interpolated: a textured square moving 16 px
  a frame has to land 8 px on in the in-between frame. The error is measured
  against the square at nine candidate positions and the lowest has to be the
  true one, which a plugin that duplicates a source frame (lowest at 0 or 16) or
  blends its neighbours (a flat profile, no clear minimum) fails.

The second and third run in a subprocess, under a Python that can import
``vapoursynth``: the venv ``tools/macos-tests.sh`` makes cannot, since Homebrew
keeps the module in its own interpreter's ``site-packages``. That Python has no
numpy either, so the frames are painted and read through the frame's own
memoryview instead - 96x96 writes into a BlankClip that is already the
background colour, which is fast enough that the whole script is a quarter of a
second.

A Vulkan failure is only sometimes a skip. macOS has no native Vulkan: the one
driver is MoltenVK, and if the loader or its ICD manifest is not installed the
GPU tests have nothing to run on and skip. But with both installed, a plugin
that cannot create an instance is the phase-3 bug - ``vkCreateInstance failed
(-9)``, VK_ERROR_INCOMPATIBLE_DRIVER, the loader hiding a portability driver
from an application that did not ask for it - and that fails here instead.

No ``short_dir``: nothing in this file binds a Unix socket, so the 104-byte
``sun_path`` limit that fixture exists for does not apply.

Measured on the machine this was written for (macOS 27.0, Apple M2, 2026-10-12):
meson/ninja built ``libmvgpu.dylib`` from ``mvgpu/`` with no source change, arm64,
exporting ``_VapourSynthPluginInit2`` and linking only ``libvulkan.1.dylib``,
``libc++`` and ``libSystem``; VapourSynth R81 loaded it and through MoltenVK
1.4.2 the in-between frame matched the square's true position exactly (error
0.0 of 255) where a blend of its two neighbours was 8.43 off.
"""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi

PLUGINS = os.environ.get("BUTTEREYE_TEST_PLUGINS", "")

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
    pytest.mark.skipif(not PLUGINS, reason="BUTTEREYE_TEST_PLUGINS not set"),
]

#: the plugin phase 3 builds, under the name this platform gives it. Asking
#: osapi rather than writing "libmvgpu.dylib" keeps the test on the name the
#: application loads: if ``plugin_file`` stopped translating the extension, the
#: build would be fine and ButterEye would still not find it.
MVGPU = osapi.plugin_file("libmvgpu.so")

CPU_ARM64 = 0x0100_000C
MH_MAGIC_64 = 0xFEED_FACF
FAT_MAGIC = 0xCAFE_BABE
#: MH_DYLIB and MH_BUNDLE: meson links a plugin as a bundle, and VapourSynth
#: loads either. Anything else (an executable, a .o) is not a plugin.
PLUGIN_FILETYPES = (6, 8)
LC_SYMTAB = 0x02
LC_RPATH = 0x8000_001C
#: LC_LOAD_DYLIB and its weak, re-export and upward variants: every command that
#: makes dyld open another library at load time
LC_LOAD_DYLIBS = frozenset({0x0C, 0x8000_0018, 0x8000_001F, 0x8000_0023})
N_EXT, N_TYPE, N_SECT = 0x01, 0x0E, 0x0E

#: Libraries every Mac has, wherever the SDK moves them
SYSTEM_ROOTS = ("/usr/lib/", "/System/Library/")
#: Homebrew libraries a ButterEye plugin may link, by the start of the file name,
#: and the formula that installs each. The Windows peer refuses every Microsoft
#: DLL but two; the same rule here is that a link has to be a library that is on
#: a user's Mac once they have run the brew line in docs/PLATFORMS.md. A new
#: entry is a decision - a plugin that links something else would load on the
#: machine that built it and fail on everyone else's.
BREW_LIBS = {
    "libvulkan.": "vulkan-loader",  # mvgpu: the loader, which finds MoltenVK
    "libomp.": "libomp",  # RIFE, when it builds: clang's OpenMP runtime
    "libncnn.": "ncnn",  # RIFE: the inference engine
    "libfftw3f.": "fftw",  # MVTools' FFT, if a build ever ships it
}


@dataclass(frozen=True)
class Macho:
    """What a Mach-O file says about itself, read from the file."""

    cputype: int
    filetype: int
    exports: frozenset[str]
    libs: tuple[str, ...]
    #: nothing asserts on these - they are here because a refused ``@rpath``
    #: load is only diagnosable next to the search paths dyld would have used,
    #: and pytest prints the whole dataclass when an assertion fails
    rpaths: tuple[str, ...]


def _slice(raw: bytes) -> bytes:
    """``raw``, or its arm64 slice when it is a universal binary.

    Our build is thin, but a universal file is a legitimate thing to ship and
    the arm64 half of it is what this Mac would run. A fat file with no arm64
    slice falls through to the magic check below and fails there.
    """
    magic, = struct.unpack_from(">I", raw, 0)
    if magic != FAT_MAGIC:
        return raw
    count, = struct.unpack_from(">I", raw, 4)
    for i in range(count):
        cpu, _sub, offset, size, _align = struct.unpack_from(">iiIII", raw, 8 + i * 20)
        if cpu == CPU_ARM64:
            return raw[offset : offset + size]
    return raw


def _macho(path: Path) -> Macho:
    """Parse ``path``'s Mach-O header, symbol table and load commands.

    Thirty lines against ``otool -L`` plus ``nm -gU`` is a deliberate trade:
    ``packaging/macos/check-dylibs.sh`` asks those tools, and a test that shelled
    out to the same tools (or to the script) could only find a plugin broken in a
    way the tools agreed to report. This reads the bytes.
    """
    raw = _slice(path.read_bytes())
    magic, = struct.unpack_from("<I", raw, 0)
    assert magic == MH_MAGIC_64, (
        f"{path.name}: not a 64-bit little-endian Mach-O ({magic:#x}) - a universal "
        "binary with no arm64 slice in it arrives here too"
    )
    cputype, _sub, filetype, ncmds = struct.unpack_from("<iiII", raw, 4)
    exports: set[str] = set()
    libs: list[str] = []
    rpaths: list[str] = []
    off = 32  # past mach_header_64
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", raw, off)
        if cmd == LC_SYMTAB:
            symoff, nsyms, stroff, strsize = struct.unpack_from("<IIII", raw, off + 8)
            strings = raw[stroff : stroff + strsize]
            for i in range(nsyms):  # nlist_64: n_strx, n_type, n_sect, n_desc, n_value
                n_strx, n_type = struct.unpack_from("<IB", raw, symoff + i * 16)
                if n_type & N_EXT and n_type & N_TYPE == N_SECT:  # external, defined here
                    exports.add(strings[n_strx : strings.index(b"\0", n_strx)].decode())
        elif cmd in LC_LOAD_DYLIBS or cmd == LC_RPATH:
            name_off, = struct.unpack_from("<I", raw, off + 8)
            name = raw[off + name_off : off + cmdsize].split(b"\0")[0].decode()
            (rpaths if cmd == LC_RPATH else libs).append(name)
        off += cmdsize
    return Macho(cputype, filetype, frozenset(exports), tuple(libs), tuple(rpaths))


def _brew_prefix() -> Path:
    """Homebrew's prefix, from ``brew`` itself (``<prefix>/bin/brew``)."""
    exe = shutil.which("brew")
    if exe:
        return Path(exe).parent.parent
    return Path(os.environ.get("HOMEBREW_PREFIX") or "/opt/homebrew")


def _refuse(lib: str) -> str | None:
    """Why a user's Mac might not have ``lib``, or None if it will.

    A load path is either one of the system libraries, or a Homebrew library
    from ``BREW_LIBS`` reached through ``<prefix>/opt/<formula>/lib`` - the
    symlink that survives an upgrade. A Cellar path carries the version that was
    installed on the build machine and breaks on the next ``brew upgrade``; an
    ``@rpath``/``@loader_path`` name is not a file at all until dyld resolves it,
    and nothing we build needs one.
    """
    prefix = _brew_prefix()
    if lib.startswith(SYSTEM_ROOTS):
        return None
    if lib.startswith("@"):
        return f"{lib}: resolved by dyld at load time, not by a path we can check"
    if not lib.startswith(f"{prefix}/"):
        return f"{lib}: outside the system libraries and Homebrew's prefix ({prefix})"
    if lib.startswith(f"{prefix}/Cellar/"):
        return f"{lib}: a version-pinned Cellar path, invalid after a brew upgrade"
    formula = next((f for start, f in BREW_LIBS.items() if Path(lib).name.startswith(start)), None)
    if formula is None:
        return f"{lib}: not a library in BREW_LIBS, so no formula puts it on a user's Mac"
    if not lib.startswith(f"{prefix}/opt/{formula}/"):
        return f"{lib}: {formula}'s library, but not through {prefix}/opt/{formula}"
    if not Path(lib).exists():
        return f"{lib}: {formula} is the right formula and this path is not on this Mac"
    return None


def _dylibs() -> list[Path]:
    """Every built plugin under ``BUTTEREYE_TEST_PLUGINS``.

    Recursive, so the variable may name the cache directory the packaging script
    writes or the ``vapoursynth/`` subdirectory inside it. Collected at import
    time because the file checks are parametrised over it; empty off macOS and
    with the variable unset, where pytest skips the parametrised tests rather
    than running them against nothing.
    """
    if sys.platform != "darwin" or not PLUGINS:
        return []
    return sorted(Path(PLUGINS).rglob("*.dylib"))


DYLIBS = _dylibs()
IDS = [p.name for p in DYLIBS]


def _mvgpu_path() -> Path | None:
    """The built ``libmvgpu.dylib``, wherever under ``PLUGINS`` it was found."""
    return next((p for p in DYLIBS if p.name == MVGPU), None)


def _no_vulkan() -> str | None:
    """Why the GPU plugins cannot run on this Mac, or None.

    macOS has no Vulkan of its own: the only driver is MoltenVK, which Homebrew's
    ``molten-vk`` installs as an ICD manifest beside the ``vulkan-loader``
    dylib. Both are files, and asking the disk for them keeps this skip
    condition independent of the plugin being tested - and narrow, so that a
    plugin which *has* a loader and a driver and still cannot create an instance
    is a failure and not a quiet skip.
    """
    prefix = _brew_prefix()
    loader = [p for p in (prefix / "lib", prefix / "opt/vulkan-loader/lib")
              if (p / "libvulkan.1.dylib").is_file()]  # fmt: skip
    icds = list((prefix / "etc/vulkan/icd.d").glob("*.json"))
    icds += list((prefix / "share/vulkan/icd.d").glob("*.json"))
    if not loader:
        return f"no Vulkan loader on this Mac (brew install vulkan-loader): {prefix}"
    if not icds:
        return "no Vulkan driver on this Mac (brew install molten-vk): no ICD manifest"
    return None


def _python_with_vapoursynth() -> str | None:
    """A Python on this Mac that can ``import vapoursynth``, or None.

    The test venv cannot: Homebrew's vapoursynth keeps its module in the
    interpreter it was built for. Candidates are this interpreter first (someone
    may be running the suite under one that has it), then Homebrew's Pythons,
    newest first, then whatever ``python3`` is on PATH.
    """
    prefix = _brew_prefix()
    brewed = sorted((prefix / "bin").glob("python3.*[0-9]"), reverse=True)
    path3 = shutil.which("python3")
    candidates = [sys.executable, *(str(p) for p in brewed)] + ([path3] if path3 else [])
    for exe in candidates:
        probe = subprocess.run(
            [exe, "-I", "-c", "import vapoursynth"],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        if probe.returncode == 0:
            return exe
    return None


#: Run inside that Python: paint a moving textured square, interpolate it to
#: twice the rate and to five times, and report what came out as JSON. The
#: numbers are the Linux peer's (tests/integration/test_mvgpu_real.py) so the two
#: platforms are compared against the same square; numpy is the one thing that
#: cannot be reused, hence the memoryview painting.
SCRIPT = r"""
import json, sys
import vapoursynth as vs

core = vs.core
core.std.LoadPlugin(sys.argv[1])

W, H, SIDE, STEP, TOP, LEFT, BG, LEN = 640, 368, 96, 16, 136, 100, 32, 6
SHIFTS = (0, 2, 4, 6, 8, 10, 12, 14, 16)


def texture():
    # a deterministic textured tile: block matching needs something to match,
    # and a flat square would move without the search ever being right
    rows, state = [], 0x2545F491
    for _ in range(SIDE):
        row = bytearray(SIDE)
        for x in range(SIDE):
            state = (state * 1103515245 + 12345) & 0xFFFFFFFF
            row[x] = 48 + (state >> 13) % 160
        rows.append(bytes(row))
    return rows


TEX = texture()


def square_rows(k, shift=0):
    # the SIDE rows of the frame the square sits in, for source frame k moved on
    # by shift pixels: what the in-between frame should look like at shift STEP/2
    x = LEFT + STEP * k + shift
    return [bytes([BG]) * x + row + bytes([BG]) * (W - x - SIDE) for row in TEX]


def paint(n, f):
    # BlankClip is already BG everywhere, so only the square is written: SIDE*SIDE
    # memoryview stores, because this Python has no numpy
    out = f.copy()
    plane, x0 = out[0], LEFT + STEP * n
    for i, row in enumerate(TEX):
        for j, value in enumerate(row):
            plane[TOP + i, x0 + j] = value
    return out


def rows_of(frame, top=TOP, count=SIDE):
    plane = frame[0]
    if plane.c_contiguous:  # a flat cast can be sliced; a 2-D sub-view cannot
        flat, stride = plane.cast("B"), plane.strides[0]
        return [bytes(flat[(top + i) * stride : (top + i) * stride + W]) for i in range(count)]
    return [bytes(plane[top + i, x] for x in range(W)) for i in range(count)]


def err(got, want):
    total = sum(abs(a - b) for g, w in zip(got, want) for a, b in zip(g, w))
    return total / (len(got) * W)


base = core.std.BlankClip(format=vs.YUV420P8, width=W, height=H, fpsnum=24, fpsden=1,
                          length=LEN, color=[BG, 128, 128])
src = core.std.ModifyFrame(base, base, paint)
x2 = core.mvgpu.Interpolate(src, fps_num=48, fps_den=1)
x5 = core.mvgpu.Interpolate(src, fps_num=120, fps_den=1)

mid = rows_of(x2.get_frame(3))  # between source frames 1 and 2
blend = [bytes((a + b) // 2 for a, b in zip(p, q))
         for p, q in zip(square_rows(1), square_rows(2))]
want = square_rows(1, STEP // 2)

plugins = {p.namespace: p for p in core.plugins()}
mvgpu = plugins.get("mvgpu")

device_error = ""
try:
    core.mvgpu.Interpolate(base, fps_num=48, fps_den=1, device=99)
except vs.Error as exc:
    device_error = str(exc)

print(json.dumps({
    "namespaces": sorted(plugins),
    "plugin_path": getattr(mvgpu, "plugin_path", None),
    "version": list(mvgpu.version) if mvgpu is not None else None,
    "len2": x2.num_frames, "len5": x5.num_frames, "len_src": src.num_frames,
    "fps2": [x2.fps.numerator, x2.fps.denominator],
    "out_format": [x2.format.name, x2.format.id, x2.width, x2.height],
    "src_format": [src.format.name, src.format.id, src.width, src.height],
    "err_by_shift": {str(s): err(mid, square_rows(1, s)) for s in SHIFTS},
    "mid_err": err(mid, want), "blend_err": err(blend, want),
    "src_kept": rows_of(x2.get_frame(2), 0, H) == rows_of(src.get_frame(1), 0, H),
    "device_error": device_error,
}))
"""


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """``SCRIPT`` run once against the built ``libmvgpu.dylib``.

    One subprocess for the whole module: the script is a quarter of a second,
    but a VapourSynth core and a Vulkan device per test are not free, and every
    assertion below is about the same run.
    """
    plugin = _mvgpu_path()
    assert plugin is not None, f"{MVGPU} is not in {PLUGINS}: {[p.name for p in DYLIBS]}"
    if why := _no_vulkan():
        pytest.skip(why)
    python = _python_with_vapoursynth()
    if python is None:
        pytest.skip("no Python on this Mac can import vapoursynth (brew install vapoursynth)")
    script = tmp_path_factory.mktemp("mvgpu") / "interpolate.py"
    script.write_text(SCRIPT)
    proc = subprocess.run(
        [python, "-I", os.fspath(script), os.fspath(plugin)],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    if proc.returncode != 0 and "no Vulkan device" in proc.stderr:
        # the loader and MoltenVK are installed but list no GPU: a VM, say. Any
        # other Vulkan failure - vkCreateInstance above all - is a real failure
        pytest.skip(f"Vulkan lists no device on this Mac: {proc.stderr.strip()[-200:]}")
    assert proc.returncode == 0, proc.stderr[-2000:]
    out: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])
    return out


# ---------------------------------------------------------------- the files


def test_the_build_produced_the_plugin_the_contract_names() -> None:
    """``BUTTEREYE_TEST_PLUGINS`` says plugins were built, so mvgpu has to be
    among them - the one plugin phase 3 builds on macOS."""
    assert DYLIBS, f"no *.dylib under {PLUGINS}"
    assert MVGPU in [p.name for p in DYLIBS], [p.name for p in DYLIBS]


@pytest.mark.parametrize("dylib", DYLIBS, ids=IDS)
def test_every_dylib_is_an_arm64_vapoursynth_plugin(dylib: Path) -> None:
    """arm64, a loadable Mach-O image, and the one symbol VapourSynth looks for.

    A plugin without ``VapourSynthPluginInit2`` is not reported as broken: R81's
    ``vscore.cpp`` leaves the namespace absent, which looks exactly like a plugin
    nobody asked for.
    """
    macho = _macho(dylib)
    assert macho.cputype == CPU_ARM64, f"{dylib.name}: cputype {macho.cputype:#x}, not arm64"
    assert macho.filetype in PLUGIN_FILETYPES, f"{dylib.name}: filetype {macho.filetype}"
    assert "_VapourSynthPluginInit2" in macho.exports, sorted(macho.exports)[:20]


@pytest.mark.parametrize("dylib", DYLIBS, ids=IDS)
def test_no_dylib_links_anything_a_users_mac_would_not_have(dylib: Path) -> None:
    """Every load path is a system library or an allowed Homebrew one, and it is
    on this Mac. The Windows peer's rule, in macOS terms."""
    macho = _macho(dylib)
    assert macho.libs, f"{dylib.name}: no load commands at all, which cannot be right"
    assert [why for lib in macho.libs if (why := _refuse(lib))] == []


# ---------------------------------------------------------------- VapourSynth


def test_vapoursynth_loads_mvgpu_from_this_build(measured: dict[str, Any]) -> None:
    """Loaded by absolute path, and the plugin answering to ``mvgpu`` is the file
    in the build directory - not something Homebrew's autoload found."""
    built = _mvgpu_path()
    assert built is not None
    assert "mvgpu" in measured["namespaces"], measured["namespaces"]
    assert Path(measured["plugin_path"]).resolve() == built.resolve(), measured["plugin_path"]
    assert measured["version"] == [0, 1], "0.0.1, as mvgpu/meson.build reports it"


def test_mvgpu_keeps_the_clip_it_was_given(measured: dict[str, Any]) -> None:
    """The rate changes and nothing else: frame counts follow it, the format and
    the size are the source's, and the frames at source positions are the source
    frames untouched. A plugin that re-encoded, resized or blurred everything
    would still interpolate and still be wrong."""
    assert (measured["len_src"], measured["len2"], measured["len5"]) == (6, 12, 30)
    assert measured["fps2"] == [48, 1]
    assert measured["out_format"] == measured["src_format"]
    assert measured["out_format"][0] == "YUV420P8"
    assert measured["src_kept"], "the frames at source positions must be the source frames"


def test_mvgpu_interpolates_the_square_to_the_true_half_way_position(
    measured: dict[str, Any],
) -> None:
    """The assertion the phase stands on.

    The square moves 16 px a frame, so the frame between source 1 and source 2
    has to hold it 8 px on. The error is measured against the square at nine
    positions: the lowest must be 8 and it must be a real minimum, which is what
    separates a motion-compensated frame from the two failures that pass a frame
    count. A plugin that duplicates a source frame is lowest at 0 or at 16; a
    plugin that blends the neighbours sits near the blend's own error at every
    shift, with no minimum to find.
    """
    by_shift = {int(k): v for k, v in measured["err_by_shift"].items()}
    best = min(by_shift, key=lambda s: by_shift[s])
    assert best == 8, by_shift
    assert by_shift[8] < 0.5 * min(v for s, v in by_shift.items() if s != 8), by_shift
    # and it is the square, not an average of two of them
    assert measured["mid_err"] < 0.15 * measured["blend_err"], measured
    assert measured["blend_err"] > 1.0, "the blend has to be a real alternative to beat"


def test_mvgpu_refuses_a_device_that_is_not_there(measured: dict[str, Any]) -> None:
    """``device=99`` is turned down by name, which also says the plugin really
    enumerated the Mac's Vulkan devices: a stub that ignored the argument could
    not know that 99 is not one of them."""
    assert measured["device_error"] == "mvgpu.Interpolate: no Vulkan device 99"
