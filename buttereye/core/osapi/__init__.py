# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The operating-system seam: every call that differs between Linux, Windows and macOS.

The rest of the core asks this module instead of reaching for POSIX directly, so
the Linux behaviour stays exactly what SCOPE §4.3/§4.6/§2.4 describe and Windows
gets the nearest equivalent (docs/PLATFORMS.md). macOS is POSIX, so it takes the
Linux path everywhere the rows below do not say otherwise:

- process group: Linux runs the child in a new session (pgid = leader pid) and
  sends SIGTERM, then SIGKILL. Windows uses the leader pid and its process tree;
  ButterEye sits in a kill-on-close job so its helpers end with it, and "soft"
  means WM_CLOSE when the tree has windows, else terminate at once.
- mpv lifetime: ``posix_spawnp(setsid)`` and a pidfd watch on Linux; on Windows a
  ``Popen`` that breaks away from the job, polled. macOS spawns as Linux does and
  has no pidfd, but a kqueue watching ``EVFILT_PROC``/``NOTE_EXIT`` is the same
  watchable descriptor (``_darwin.exit_watch``), so it does not poll either.
- is a process still there: on POSIX it takes two questions, because a process that
  has exited and has not been reaped still answers ``os.kill(pid, 0)``. Linux reads
  the state out of ``/proc/<pid>/stat``; macOS has no ``/proc`` and asks libproc
  (``_darwin.pid_alive``). Windows reads the exit code of a process handle.
- mpv control: Unix socket + ``SO_PEERCRED`` on Linux. macOS keeps the socket but
  has no ``SO_PEERCRED``: the peer's uid comes from ``LOCAL_PEERCRED`` and its pid
  from ``LOCAL_PEERPID`` (``_darwin.peer_cred``), and a peer that has already gone
  reads as ``ConnectionResetError`` rather than an answer from the kernel's cache.
  On Windows a named pipe, checked by its server pid and that process's user SID,
  with a marker file in the runtime folder standing for the socket (discovery,
  cleanup).
- private folder: 0700, own uid, not a symlink on Linux; on Windows owned by this
  user's SID and not a reparse point (it lives under %LOCALAPPDATA%, per-user by ACL).
  macOS checks it as Linux does, but has no ``$XDG_RUNTIME_DIR`` to put it in: the
  kernel's per-user temporary directory stands in (``_darwin.user_temp_dir``).
- locks: ``flock`` on Linux and macOS; ``msvcrt.locking`` on byte 0 of a lock file
  on Windows.
- durable renames: ``fsync`` on the folder, and on macOS ``F_FULLFSYNC`` as well,
  where ``fsync`` alone stops at the drive's own cache; Windows cannot open a folder
  and leaves the ordering to NTFS (``fsync_dir``).
- plugin files: ``mvtools.so`` on Linux, the same stem with ``.dll`` on Windows and
  ``.dylib`` on macOS (``plugin_file``). TensorRT's plugin has no macOS build and
  never will, so ``HAS_VSTRT`` is False there.
- VapourSynth for child processes: Linux has the distribution's; on Windows the
  installer bundles Python with VapourSynth, and ``use_bundled_runtime`` points
  every child (mpv, vspipe) at it. macOS has Homebrew's, which ``vspipe`` and the
  plugins find on their own -- but mpv's in-process ``vapoursynth`` filter does
  not, because it ``dlopen``s VSScript by a bare leaf name and dyld never looks
  inside Homebrew. ``use_bundled_runtime`` hands mpv the file instead
  (``vsscript_library``, ``point_at_vsscript``).
- the macOS app: ButterEye.app carries its own Python, VapourSynth, mpv, ffmpeg,
  plugins and Vulkan, as the Windows installer does. ``MACOS_APP`` says whether
  this process runs from it (decided by where the interpreter is, like
  ``install_root`` on Windows), and when it does every program, plugin and model
  is the app's by full path (``executable``, ``bundled_vspipe``,
  ``types.macos_plugin_dir``) and the children's environment points at nothing
  else (``point_at_app``). Outside the app none of this runs.
- the Linux AppImage (a trial, docs/appimage-trial.md): the same idea on Linux.
  ``LINUX_APP`` says whether this process runs from one, decided the same way
  (where the interpreter and this file are, never ``$APPDIR``), and when it does
  mpv, ffmpeg, ffprobe, vspipe and the plugins are the AppImage's by full path
  and the children's environment is ``point_at_appimage``'s. Fedora's RPMs never
  meet any of it: their Python is ``/usr/bin/python3``.

- power and heat: whether the machine runs from a battery and whether the
  system's power saver is on (``on_battery``, ``low_power_mode``) come from IOKit
  and Low Power Mode on macOS, ``GetSystemPowerStatus`` on Windows, and
  ``/sys/class/power_supply`` and the ACPI platform profile on Linux. How hot it
  is (``thermal_state``) only macOS says; Linux and Windows answer None there.
  All three come in one ``power_snapshot``, and a rule reads an unknown as a cool
  machine on mains power (``rules.py``). ``has_system_battery`` is whether there
  is a battery at all. The machine is kept from idle sleep while ButterEye works:
  ``caffeinate -w`` on macOS and a ``systemd-inhibit`` lock on Linux, both tied
  to a process (``keep_awake``), and a power request on Windows and an
  ``NSProcessInfo`` activity on macOS for as long as a job runs (``activity``).

mypy checks all three: ``mypy --platform linux``, ``mypy --platform win32``
and ``mypy --platform darwin``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import logging
import ntpath
import os
import re
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from buttereye.core.types import PowerSnapshot

_log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

#: open() flags that exist only on POSIX; 0 where they don't (Windows handles are
#: not inherited unless asked for, and there are no symlinked-path races to stop
#: with O_NOFOLLOW in a per-user folder)
O_CLOEXEC: int = getattr(os, "O_CLOEXEC", 0)
O_NOFOLLOW: int = getattr(os, "O_NOFOLLOW", 0)
O_BINARY: int = getattr(os, "O_BINARY", 0)

if sys.platform == "win32":
    import msvcrt

    from buttereye.core.osapi import _win32 as w
else:
    import fcntl

    if sys.platform == "darwin":
        from buttereye.core.osapi import _darwin as d


# ======================================================================== files
def is_link(st: os.stat_result) -> bool:
    """A symbolic link, or on Windows any reparse point (junction, mount point)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return IS_WINDOWS and bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def owned_by_me(path: Path, st: os.stat_result) -> bool:
    """``path`` (already ``lstat``-ed as ``st``) belongs to this user. On Windows
    its owner is this user, or the owner this process gives what it creates: for
    an administrator on Windows Server, or elevated, that is the Administrators
    group, so a folder ButterEye just made would otherwise not count as its own."""
    if sys.platform == "win32":
        mine = {w.my_sid(), w.my_owner_sid()} - {None}
        return bool(mine) and w.file_owner_sid(path) in mine
    return st.st_uid == os.getuid()


def shared_mode_bits(st: os.stat_result) -> int:
    """Group/other permission bits (0: private). Windows has no mode bits to
    read; privacy there comes from the per-user profile folder's ACL."""
    if IS_WINDOWS:
        return 0
    return stat.S_IMODE(st.st_mode) & 0o077


def set_mode(fd: int, mode: int) -> None:
    """``fchmod`` where modes mean something (not Windows)."""
    if sys.platform != "win32":
        os.fchmod(fd, mode)


def fsync_dir(directory: Path) -> None:
    """Make a rename in ``directory`` durable (POSIX); Windows can't open folders.

    On macOS ``fsync`` only hands the data to the drive, which may still be
    holding it in a volatile cache; ``F_FULLFSYNC`` is the call that waits for
    the platter or the flash. Without it a profile saved seconds before the power
    went could come back as the old one -- never as a damaged one, since the
    rename itself stays atomic."""
    if sys.platform == "win32":
        return
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
        if sys.platform == "darwin":
            with contextlib.suppress(OSError):
                fcntl.fcntl(dfd, fcntl.F_FULLFSYNC, 0)
    finally:
        os.close(dfd)


def replace(src: Path | str, dst: Path | str) -> None:
    """``os.replace``; on Windows retried briefly, because a reader holding the
    target open (an editor, the virus scanner) makes the rename fail for a moment."""
    if not IS_WINDOWS:
        os.replace(src, dst)
        return
    for attempt in range(20):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.025)


def try_lock(fd: int) -> bool:
    """Exclusive lock on ``fd`` without waiting; False if someone else holds it."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    else:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def lock(fd: int) -> None:
    """Exclusive lock on ``fd``, waiting as long as it takes."""
    if sys.platform == "win32":
        while not try_lock(fd):
            time.sleep(0.02)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX)


def unlock(fd: int) -> None:
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked_dir(directory: Path) -> Iterator[None]:
    """Serialise writers of ``directory``: ``flock`` on the folder itself on POSIX,
    on ``<directory>/.lock`` on Windows (folders can't be opened there)."""
    if IS_WINDOWS:
        fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
    else:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | O_CLOEXEC)
    try:
        lock(fd)
        yield
    finally:
        os.close(fd)  # closing releases the lock


# ======================================================================== libraries
def plugin_file(linux_name: str) -> str:
    """A VapourSynth plugin's file name on this system. Plugins are named for Linux
    (``librife.so``); the Windows builds use the same stem with ``.dll``, the macOS
    ones with ``.dylib``. The extension is VapourSynth's own, not a convention: R81
    fixes it per platform in ``src/core/vscore.cpp`` and uses it both for a manifest
    load and to filter the autoload scan, so a plugin under any other name is unseen.
    Only the extension changes -- Homebrew's are ``mvtools.dylib`` and
    ``libffms2.dylib``, the same stems Linux has."""
    if IS_WINDOWS and linux_name.endswith(".so"):
        return linux_name[:-3] + ".dll"
    if IS_MACOS and linux_name.endswith(".so"):
        return linux_name[:-3] + ".dylib"
    return linux_name


#: vs-mlrt's TensorRT plugin: ``vstrt.dll`` is the name its Windows releases use
VSTRT_LIB = "vstrt.dll" if IS_WINDOWS else "libvstrt.so"

#: Can a TensorRT backend exist on this system at all? Not on macOS: TensorRT needs
#: CUDA, no Apple Silicon Mac has an NVIDIA GPU, and that is permanent rather than
#: pending (docs/PLATFORMS.md). Nothing is ever found under ``VSTRT_LIB`` there, so
#: callers must ask this before they look. It is a flag and not ``VSTRT_LIB = None``
#: because every caller joins that name onto a path, and making it optional moves
#: the type error into files this seam has no business editing.
HAS_VSTRT = not IS_MACOS


# ======================================================================== bundled runtime
def runtime_dirs() -> tuple[str, str | None]:
    """(the folder of this Python, the folder of its ``vapoursynth`` package or
    None). Looks the package up without importing it: the GUI process must not
    load VapourSynth (SCOPE §8.1)."""
    try:
        spec = importlib.util.find_spec("vapoursynth")
    except ImportError, ValueError:
        spec = None
    vs_dir = os.path.dirname(spec.origin) if spec is not None and spec.origin else None
    return os.path.dirname(sys.executable), vs_dir


def point_at_runtime(
    env: MutableMapping[str, str], python_dir: str, vs_dir: str | None, *, sep: str = os.pathsep
) -> None:
    """Make ``env`` lead children to this Python's VapourSynth (Windows):

    - ``VSSCRIPT_PATH``: Windows mpv builds load VSScript at run time from this
      variable (else the registry, else the DLL search), so without it mpv finds
      no VapourSynth or someone else's;
    - ``PATH`` starts with the Python folder (``vsscript.dll`` loads ``python3.dll``,
      which forwards to ``python3XX.dll`` next to it) and the package folder
      (``vspipe.exe``), so ``vspipe`` resolves to the bundled one everywhere.

    Idempotent: folders already on PATH move to the front instead of repeating."""
    first = [python_dir] + ([vs_dir] if vs_dir else [])
    if vs_dir is not None:
        vsscript = os.path.join(vs_dir, "vsscript.dll")
        if os.path.isfile(vsscript):
            env["VSSCRIPT_PATH"] = vsscript

    def key(p: str) -> str:
        return os.path.normcase(p.rstrip("\\/"))

    lead = {key(p) for p in first}
    rest = [p for p in env.get("PATH", "").split(sep) if p and key(p) not in lead]
    env["PATH"] = sep.join(first + rest)


#: Windows: with this set, ``CreateProcess`` and ``shutil.which`` stop looking for a
#: bare program name in the current folder first (a ``vspipe.exe`` or ``mpv.exe``
#: sitting in Downloads would otherwise win over the bundled one or PATH's)
NO_CWD_SEARCH = "NoDefaultCurrentDirectoryInExePath"


#: macOS: dyld's list of directories to try for a library it did not find at its
#: install path, which is also where a bare-name ``dlopen`` ends up last
DYLD_FALLBACK = "DYLD_FALLBACK_LIBRARY_PATH"


def vsscript_library() -> Path | None:
    """The VSScript library to hand mpv, or None when there is nothing to hand it.

    An answer on macOS only (``_darwin.vsscript_library``), because macOS is the
    one platform where the file exists and nothing can find it. Linux's mpv
    ``dlopen``s ``libvapoursynth-script.so`` and the distribution has put it where
    the loader looks; on Windows the file comes from the Python that is running
    and ``point_at_runtime`` already names it. The doctor and the Homebrew tap ask
    this the same question the runtime asks, so a Mac user is told about the file
    ButterEye would actually use."""
    if sys.platform == "darwin":
        if MACOS_APP is not None:
            # the app's own, and only that: it is part of the bundle whose Python
            # is already running this code, so it is trusted exactly as far as
            # this code is (``point_at_app``), and nothing outside the app is
            # asked at all
            lib = app_runtime(MACOS_APP)["vsscript"]
            return lib if lib.is_file() else None
        return d.vsscript_library()
    return None


def point_at_vsscript(
    env: MutableMapping[str, str], library: Path | None, *, sep: str = os.pathsep
) -> None:
    """macOS: make ``env`` lead a child mpv to VSScript at ``library``.

    Homebrew's mpv has the ``vapoursynth`` filter compiled in, but it does not
    link VSScript: it ``dlopen``s ``libvsscript.dylib`` and then
    ``libvapoursynth-script.dylib`` by those bare leaf names, which sends dyld to
    ``/usr/lib``, the Swift toolchain directories and the dyld cache and never
    inside Homebrew, where the only copy is. Out of the box the filter therefore
    fails to load and ButterEye's smoothing is dropped from the chain (BE-3003).
    Two variables are set, because they fail in different ways and the second
    costs one entry in a list:

    - ``VSSCRIPT_PATH``, the exact file. mpv reads it on every platform and not
      only on Windows, where ``point_at_runtime`` uses the same variable
      (measured: the string is in the macOS binary, and setting it alone loads
      the filter). It is also the half that survives most, because it is not a
      ``DYLD_*`` variable: dyld strips those for a child that is SIP-protected or
      built with the hardened runtime, and leaves this one.
    - ``DYLD_FALLBACK_LIBRARY_PATH``, carrying the library's own directory, which
      answers the bare-name ``dlopen`` for an mpv that does not read
      ``VSSCRIPT_PATH``. The directory is the one the file is already in, so
      ButterEye writes nothing and owns no file that a ``brew upgrade`` could
      leave pointing at a keg that has gone.

    ``VSSCRIPT_PATH`` is always ButterEye's answer, never an inherited value, and
    that is a measured decision rather than a preference: mpv does not look past
    a ``VSSCRIPT_PATH`` it cannot load. Measured here with the fallback path
    correctly set alongside it, a value naming a file that is not there gives
    "Failed to load VapourSynth VSScript library: dlopen(<that path>) ... no such
    file" and the filter is removed, and a value naming some other real dylib
    gets as far as "dlsym(getVSScriptAPILastError): symbol not found" and is
    removed just the same. Since the Cellar path carries VapourSynth's version, a
    value exported from a shell before an upgrade is exactly that first case: a
    single stale string that kills live smoothing on a machine where everything
    else is in order. Deferring to it would mean deferring to something with no
    fallback behind it, where ButterEye's own answer is derived from the
    VapourSynth that is installed now.

    Why ``DYLD_FALLBACK_LIBRARY_PATH`` and not ``DYLD_LIBRARY_PATH``: the fallback
    list is consulted only after dyld's normal search has failed, so it cannot
    shadow a library a child would otherwise have found, where
    ``DYLD_LIBRARY_PATH`` is searched by leaf name *before* a library's own path
    is honoured. That is not a worry in the abstract -- it is what the stale
    measurement above shows from the other side: with ``DYLD_LIBRARY_PATH`` set
    to the keg directory, mpv's ``dlopen`` of a dangling absolute
    ``VSSCRIPT_PATH`` silently loaded the keg's library instead, because dyld had
    replaced the path with its own leaf-name match. A variable that rewrites
    absolute loads for every library in every child is too blunt to set for one
    file; both were measured, each loads the filter on its own, and the surgical
    one is kept.

    Nothing has to be written back when a variable was unset. ``dyld(1)``: a
    binary from Fall 2023 or later has no default fallback list to preserve, and
    measured here the bare-name ``dlopen`` still tried ``/usr/lib`` and the Swift
    directories with the variable set, so setting it takes nothing away. Adding
    ``/usr/local/lib`` would be inventing a library search path for every child,
    not restoring one.

    Why an environment variable reaches mpv at all: ``<prefix>/bin/mpv`` is
    user-installed and ad-hoc signed with no hardened runtime, so dyld keeps the
    ``DYLD_*`` entry for it. A user who points ButterEye at a protected mpv -- one
    inside a signed app bundle, or anything under ``/usr/bin`` -- has that entry
    stripped and keeps only ``VSSCRIPT_PATH``, which is enough for an mpv that
    reads it, and otherwise fails as it does today, with mpv's own "Failed to
    load VapourSynth VSScript library" in the session log. The doctor is the
    place to say that out loud: it already resolves the mpv that would run, and
    ``vsscript_library`` is this same answer.

    ``library`` None (no VapourSynth installed): nothing is touched, so a Mac
    without it is left exactly as it was -- a directory with no VSScript in it on
    a child's search path would read as a fix to anything that only asks whether
    the variable is set. Idempotent: the directory moves to the front of the list
    instead of repeating.
    """
    if library is None:
        return
    env["VSSCRIPT_PATH"] = os.fspath(library)
    folder = os.path.dirname(os.fspath(library))
    rest = [p for p in env.get(DYLD_FALLBACK, "").split(sep) if p and p != folder]
    env[DYLD_FALLBACK] = sep.join([folder] + rest)


# ======================================================================== the macOS app
#: macOS: the folders of ButterEye.app's Contents/Resources (packaging/macos/
#: build-app.sh lays them out): ``bin`` holds the programs ButterEye starts,
#: ``vapoursynth`` the plugins, ``share`` the models and the Vulkan manifest
APP_BIN = "bin"
#: the programs the app carries, which are never looked for anywhere else while
#: ButterEye runs from it (``executable``)
APP_TOOLS = frozenset({"mpv", "ffmpeg", "ffprobe", "vulkaninfo", "mkvmerge"})
#: the bundle's MoltenVK manifest, relative to Resources
APP_VULKAN_ICD = "share/vulkan/icd.d/MoltenVK_icd.json"
#: variables that would make a child load something other than what the app
#: carries, removed from the children's environment (``point_at_app`` says why)
APP_UNSET = (
    "PYTHONHOME",
    "PYTHONPATH",
    "VAPOURSYNTH_EXTRA_PLUGIN_PATH",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "DYLD_INSERT_LIBRARIES",
)


def app_resources(executable: str, module: str) -> Path | None:
    """``Contents/Resources`` of the ButterEye.app this Python runs from, or None.

    The macOS peer of ``install_root`` on Windows, and decided the same way: by
    where the running interpreter is, never by an environment variable. The
    app's launcher (packaging/macos/launcher.c) embeds
    ``Contents/Resources/python``, and sets ``sys.executable`` to its
    ``bin/python3.<minor>``, so all of these must hold:

    - ``executable`` (``sys.executable``) is ``<x>.app/Contents/Resources/python/
      bin/<file>``, resolved, so a symlink to a bundle elsewhere counts as that
      bundle and nothing else;
    - ``module`` (this file) is inside that same ``Resources/python``: an app's
      Python made to run some other checkout's ``buttereye`` is not the app, and
      must not hand that checkout the app's plugins;
    - ``Resources`` holds the install record build-app.sh writes, and
      ``Contents`` an ``Info.plist``.

    Pure but for the ``stat`` calls, so tests answer it for a planted tree on any
    platform. None for anything else: Homebrew's tap, a development venv, Linux,
    Windows.
    """
    try:
        exe = Path(executable).resolve()
        mod = Path(module).resolve()
    except OSError, RuntimeError:
        return None
    if len(exe.parts) < 7 or exe.parent.name != "bin" or exe.parent.parent.name != "python":
        return None
    resources = exe.parent.parent.parent
    contents = resources.parent
    if resources.name != "Resources" or contents.name != "Contents":
        return None
    if not contents.parent.name.endswith(".app"):
        return None
    if not mod.is_relative_to(resources / "python"):
        return None
    try:
        ok = (resources / INSTALL_RECORD).is_file() and (contents / "Info.plist").is_file()
    except OSError:
        return None
    return resources if ok else None


def app_runtime(resources: Path) -> dict[str, Path]:
    """The files of the app's own Python and VapourSynth, by role: the
    interpreter (``python``), the library VSScript loads Python from
    (``libpython``), VapourSynth's package folder (``vapoursynth``), and in it
    ``vsscript`` and ``vspipe``. Paths only: whether each exists is the doctor's
    question. The version is this interpreter's, which is the app's."""
    minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    py = resources / "python"
    vs = py / "lib" / minor / "site-packages" / "vapoursynth"
    return {
        "python": py / "bin" / minor,
        "libpython": py / "lib" / f"lib{minor}.dylib",
        "vapoursynth": vs,
        "vsscript": vs / "libvsscript.dylib",
        "vspipe": vs / "vspipe",
    }


def point_at_app(env: MutableMapping[str, str], resources: Path, *, sep: str = os.pathsep) -> None:
    """macOS app: make ``env`` lead every child ButterEye starts to what the app
    carries, and to nothing else.

    - ``PATH`` starts with ``Resources/bin``. ButterEye itself starts mpv, ffmpeg,
      ffprobe and vulkaninfo by full path (``executable``); this is for the code
      that still asks ``shutil.which`` and for a child's own children, so that the
      first answer is the app's on every route.
    - ``VSSCRIPT_PATH``: the app's ``libvsscript.dylib``, the exact file, as on
      Windows. No ``DYLD_FALLBACK_LIBRARY_PATH`` is set: that was the Homebrew
      route's answer for an mpv that ignores ``VSSCRIPT_PATH``, and the app's mpv
      is known to read it, so the app sets no ``DYLD_*`` variable at all.
    - ``VK_DRIVER_FILES``: the app's MoltenVK manifest, which makes the bundled
      Vulkan loader use that driver and only that one. Without it the loader
      searches ``/usr/local``, ``/opt/homebrew`` and the user's folders for
      manifests, and a Mac with Homebrew's MoltenVK would hand the app's mpv a
      driver from outside the app.
    - ``PYTHONNOUSERSITE=1``, and no ``PYTHONHOME`` or ``PYTHONPATH``: VSScript
      starts the app's Python *inside* mpv and vspipe, and that Python reads the
      environment. Any of the three could make it import a different
      ``vapoursynth`` package than the one beside the ``libvsscript.dylib`` it
      came from (the user's ``~/Library/Python`` comes first on ``sys.path``).
    - no ``VAPOURSYNTH_EXTRA_PLUGIN_PATH``: VapourSynth autoloads every plugin it
      names, and a plugin already loaded is one ``_load`` in the generated script
      skips -- so a variable pointing at Homebrew's plugins would put Homebrew's
      MVTools in place of the app's.
    - no ``DYLD_LIBRARY_PATH``, ``DYLD_FRAMEWORK_PATH`` or
      ``DYLD_INSERT_LIBRARIES``: dyld searches the first two by leaf name before
      a library's own path (phase 4 measured it replacing even an absolute
      ``dlopen``), so a value left over from a shell would swap Homebrew's
      ``libavcodec`` in under the app's mpv; the third loads code into every
      child.

    Idempotent. Never touches anything outside ``env``.
    """
    rt = app_runtime(resources)
    first = os.fspath(resources / APP_BIN)
    rest = [p for p in env.get("PATH", "").split(sep) if p and p.rstrip("/") != first]
    env["PATH"] = sep.join([first] + rest)
    env["VSSCRIPT_PATH"] = os.fspath(rt["vsscript"])
    env["VK_DRIVER_FILES"] = os.fspath(resources / APP_VULKAN_ICD)
    env["PYTHONNOUSERSITE"] = "1"
    for name in APP_UNSET:
        env.pop(name, None)


#: the programs of ButterEye.app that ButterEye starts, relative to Contents/
APP_PROGRAMS = (
    "MacOS/ButterEye",
    "Resources/bin/mpv",
    "Resources/bin/ffmpeg",
    "Resources/bin/ffprobe",
    "Resources/bin/vulkaninfo",
    "Resources/bin/mkvmerge",
)


def app_quarantined(
    resources: Path, *, has_quarantine: Callable[[Path], bool] | None = None
) -> tuple[Path, ...]:
    """The app's programs that still carry ``com.apple.quarantine``.

    A copy that came through a browser carries it on every file, and Gatekeeper
    holds each quarantined program at its first ``exec`` until the user answers a
    prompt -- measured, an ``mpv --version`` from such a copy waits there
    indefinitely. That includes the programs ButterEye starts itself (mpv,
    vspipe, ffmpeg), which is why the doctor names the one command that clears
    the whole app. ``has_quarantine`` is for tests; off macOS nothing is read."""
    if has_quarantine is None:
        if sys.platform == "darwin":
            has_quarantine = d.quarantined
        else:
            return ()
    contents = resources.parent
    programs = [contents / p for p in APP_PROGRAMS] + [app_runtime(resources)["vspipe"]]
    return tuple(p for p in programs if p.exists() and has_quarantine(p))


#: the folder macOS runs a quarantined app from until it is moved (Gatekeeper
#: path randomisation); ``doctor.checks_pkgs.translocated`` reads the same name
APP_TRANSLOCATION_DIR = "AppTranslocation"

#: the command that clears the whole app, the same one the README and the
#: doctor's ``packages.app_quarantine`` give
XATTR = "/usr/bin/xattr"


def release_app(
    resources: Path,
    *,
    has_quarantine: Callable[[Path], bool] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    """First-time setup on a Mac: clear ``com.apple.quarantine`` from the whole
    ButterEye.app when any of its programs still carries it, so mpv, vspipe and
    ffmpeg are not held at their first ``exec`` (``app_quarantined``) behind a
    prompt nobody sees. That is the case after System Settings ▸ Open Anyway,
    which lets the app start but leaves the programs inside it marked.

    Runs ``xattr -dr com.apple.quarantine <app>``, nothing else, and only when
    something is held. True when it ran and succeeded. Any failure (a
    translocated read-only copy, an app owned by someone else) is False and
    never raised: the doctor still names the same command for the user to run.
    A translocated copy (a path through ``AppTranslocation``, the read-only copy
    macOS runs an app from until it is moved out of Downloads) is False before
    anything runs: xattr cannot change it, and the fix is to move ButterEye.app
    to Applications. ``has_quarantine`` and ``run`` are for tests; off macOS
    nothing is read or run."""
    if not app_quarantined(resources, has_quarantine=has_quarantine):
        return False
    app = resources.parent.parent
    if APP_TRANSLOCATION_DIR in app.parts:
        _log.warning(
            "%s is a translocated read-only copy; move ButterEye.app to Applications "
            "and open it from there",
            app,
        )
        return False
    try:
        done = run(
            [XATTR, "-dr", "com.apple.quarantine", str(app)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.warning("could not clear the download mark from %s: %s", app, exc)
        return False
    if done.returncode != 0:
        _log.warning(
            "xattr could not clear the download mark from %s (exit %s): %s",
            app,
            done.returncode,
            (done.stderr or b"").decode(errors="replace").strip(),
        )
        return False
    _log.info("cleared the download mark from %s", app)
    return True


# ======================================================================= the Linux AppImage (trial)
#: Linux AppImage: the programs it carries in ``<root>/bin``, which are never
#: looked for anywhere else while ButterEye runs from it (``executable``)
APPIMAGE_TOOLS = frozenset({"mpv", "ffmpeg", "ffprobe"})
#: variables that would make a child load something other than what the
#: AppImage carries, removed from the children's environment
#: (``point_at_appimage`` says why)
APPIMAGE_UNSET = (
    "PYTHONHOME",
    "PYTHONPATH",
    "VAPOURSYNTH_EXTRA_PLUGIN_PATH",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
)


def appimage_root(executable: str, module: str, *, uid: int | None = None) -> Path | None:
    """The root of the ButterEye AppImage this Python runs from (the mounted or
    extracted AppDir), or None.

    The Linux peer of ``app_resources``, and decided the same way: by where the
    running interpreter and this file are, never by an environment variable. The
    AppImage runtime exports ``$APPDIR``, but so can anyone who starts a process,
    so a variable alone must never make ButterEye hand its children a plugin
    folder, a VSScript library or a ``PATH`` -- that would be a way to make it run
    somebody else's code. ``packaging/appimage/AppRun`` execs
    ``<root>/python/bin/python3.<minor>``, so all of these must hold:

    - ``executable`` (``sys.executable``) is ``<root>/python/bin/<file>``,
      resolved, so a symlink counts as what it points to;
    - ``module`` (this file) is inside that same ``<root>/python``: the
      AppImage's Python made to run some other checkout's ``buttereye`` is not
      the AppImage;
    - ``<root>`` holds the install record the build writes and ``AppRun``;
    - ``<root>`` and ``<root>/python`` belong to this user or to root and nobody
      else can write to them. A FUSE mount of the image shows root-owned
      read-only files; ``--appimage-extract-and-run`` extracts into this user's
      own ``$TMPDIR/appimage_extracted_<hash>``. A tree another local user
      planted in a shared ``/tmp`` under that name is refused here rather than
      trusted -- though the runtime itself would already have run its ``AppRun``,
      which is the runtime's problem and is written down in the trial's report.

    Pure but for the ``stat`` calls, so tests answer it for a planted tree on any
    platform. ``uid``: the user to compare owners with (default: this one).
    """
    try:
        exe = Path(executable).resolve()
        mod = Path(module).resolve()
    except OSError, RuntimeError:
        return None
    if len(exe.parts) < 5 or exe.parent.name != "bin" or exe.parent.parent.name != "python":
        return None
    root = exe.parent.parent.parent
    if not mod.is_relative_to(root / "python"):
        return None
    me = os.getuid() if uid is None and hasattr(os, "getuid") else uid
    try:
        if not ((root / INSTALL_RECORD).is_file() and (root / "AppRun").is_file()):
            return None
        for folder in (root, root / "python"):
            st = folder.stat()
            if st.st_uid not in (0, me) or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                return None
    except OSError:
        return None
    return root


def appimage_runtime(root: Path) -> dict[str, Path]:
    """The files of the AppImage's own Python and VapourSynth, by role, as
    ``app_runtime`` names them for the macOS app: the interpreter, the
    ``libpython`` VSScript loads, VapourSynth's package folder, and in it
    ``libvsscript.so`` and ``vspipe`` (the manylinux wheel's layout)."""
    minor = f"python{sys.version_info.major}.{sys.version_info.minor}"
    py = root / "python"
    vs = py / "lib" / minor / "site-packages" / "vapoursynth"
    return {
        "python": py / "bin" / minor,
        "libpython": py / "lib" / f"lib{minor}.so.1.0",
        "vapoursynth": vs,
        "vsscript": vs / "libvsscript.so",
        "vspipe": vs / "vspipe",
    }


def point_at_appimage(env: MutableMapping[str, str], root: Path, *, sep: str = os.pathsep) -> None:
    """Linux AppImage: make ``env`` lead every child ButterEye starts to what the
    AppImage carries. ``point_at_app`` is the macOS model; the differences are
    Linux's:

    - ``PATH`` starts with ``<root>/bin`` (mpv, ffmpeg, ffprobe). ButterEye
      starts those by full path (``executable``); this is for ``shutil.which``
      callers -- render's ffmpeg and ffprobe -- and a child's own children.
    - ``VSSCRIPT_PATH``: the AppImage's ``libvsscript.so``. Its mpv 0.41 links
      VSScript itself, found through its RUNPATH, and does not read the variable;
      it is set for anything that does, as on the other two platforms.
    - ``PYTHONNOUSERSITE=1``, no ``PYTHONHOME``/``PYTHONPATH``: VSScript starts
      the AppImage's Python inside mpv and vspipe, and must import the
      ``vapoursynth`` beside its own ``libvsscript.so``.
    - no ``VAPOURSYNTH_EXTRA_PLUGIN_PATH``: an autoloaded system MVTools would
      take the place of the AppImage's (``_load`` skips a loaded namespace).
    - no ``LD_LIBRARY_PATH`` or ``LD_PRELOAD``. The loader searches
      ``LD_LIBRARY_PATH`` before an ELF file's RUNPATH, so a value left in a
      shell (a self-built FFmpeg in ``/opt``, say) would put that
      ``libavcodec.so`` under the AppImage's mpv; ``LD_PRELOAD`` loads code into
      every child. This also reaches the host programs ButterEye starts
      (``vulkaninfo``, ``nvidia-smi``): a setup that needs either variable for
      those is not served by the trial.

    Nothing else is touched, deliberately: the host's GPU drivers, ``libva``
    drivers and the Vulkan loader's manifests are found the host's own way.
    Idempotent. Never touches anything outside ``env``.
    """
    rt = appimage_runtime(root)
    first = os.fspath(root / APP_BIN)
    rest = [p for p in env.get("PATH", "").split(sep) if p and p.rstrip("/") != first]
    env["PATH"] = sep.join([first] + rest)
    env["VSSCRIPT_PATH"] = os.fspath(rt["vsscript"])
    env["PYTHONNOUSERSITE"] = "1"
    for name in APPIMAGE_UNSET:
        env.pop(name, None)


#: VSScript's own configuration file, relative to ``$XDG_CONFIG_HOME`` (or
#: ``~/.config``): VapourSynth R81 reads it on macOS and Linux to learn which
#: Python to start (``readEnvConfig`` in src/vsscript/vsscript.cpp)
VSSCRIPT_CONFIG = Path("vapoursynth") / "vapoursynth.toml"
#: the key of an entry for some ButterEye.app's VSScript: one ``register_vsscript``
#: may remove once that file is gone
_APP_VSSCRIPT_KEY = re.compile(
    r'^"(/[^"]*\.app/Contents/Resources/python/lib/python3\.\d+/site-packages/'
    r'vapoursynth/libvsscript\.dylib)"\s*='
)
#: the same for a ButterEye AppImage's VSScript: under the runtime's FUSE mount
#: point (``.mount_<name><random>``, a new one every start) or its extraction
#: folder (``appimage_extracted_<hash>``). Every start of a mounted AppImage
#: writes one line and drops the lines of mounts that have gone.
_APPIMAGE_VSSCRIPT_KEY = re.compile(
    r'^"(/[^"]*/(?:\.mount_[^/"]+|appimage_extracted_[0-9a-f]+)/python/lib/python3\.\d+/'
    r'site-packages/vapoursynth/libvsscript\.so)"\s*='
)


def vsscript_config(env: Mapping[str, str]) -> Path | None:
    """Where VSScript looks for its configuration with ``env``: VapourSynth
    joins ``$XDG_CONFIG_HOME`` (else ``$HOME/.config``) with
    ``vapoursynth/vapoursynth.toml``. None when neither gives an absolute path."""
    base = env.get("XDG_CONFIG_HOME", "")
    if not base:
        home = env.get("HOME", "")
        base = os.path.join(home, ".config") if home else ""
    return Path(base) / VSSCRIPT_CONFIG if base and os.path.isabs(base) else None


def vsscript_entry(vsscript: Path, python: Path, libpython: Path, mtime: int) -> str | None:
    """One line of VSScript's configuration, exactly as ``vapoursynth config``
    writes it: ``"<vsscript>" = ["<python>","<libpython>","<mtime>"]``.

    VSScript accepts an entry only when its first string is the path it was
    loaded from (``dladdr``, with ``/lib64/`` read as ``/lib/``) and the last is
    the interpreter's modification time in seconds; otherwise it runs
    ``vapoursynth config`` through the shell and, finding none, gives up. Its
    reader unescapes backslashes but stops a string at the first quote, so a path
    with a quote or a line break in it cannot be written at all (None)."""

    def q(text: str) -> str | None:
        if '"' in text or "\n" in text or "\r" in text:
            return None
        return '"' + text.replace("\\", "\\\\") + '"'

    key = q(os.fspath(vsscript).replace("/lib64/", "/lib/"))
    parts = [q(os.fspath(python)), q(os.fspath(libpython)), q(str(mtime))]
    if key is None or any(p is None for p in parts):
        return None
    return f"{key} = [{','.join(p for p in parts if p is not None)}]"


def merge_vsscript_config(text: str, line: str, *, exists: Callable[[str], bool]) -> str:
    """``text`` (the configuration as it is) with ``line`` in it: an older entry
    for the same VSScript replaced, an entry for a ButterEye.app (or AppImage)
    whose VSScript no longer ``exists`` dropped (a copy moved, deleted, run once
    from a translocated path, or an AppImage mount that has gone), and every
    other line -- other VapourSynth installations,
    comments -- kept as it was. Pure."""
    key = line.split(" = ", 1)[0]
    out: list[str] = []
    for old in text.splitlines():
        if old.split(" = ", 1)[0].strip() == key:
            continue
        m = _APP_VSSCRIPT_KEY.match(old) or _APPIMAGE_VSSCRIPT_KEY.match(old)
        if m is not None and not exists(m.group(1)):
            continue
        out.append(old)
    out.append(line)
    return "\n".join(out) + "\n"


def register_vsscript(
    env: Mapping[str, str], resources: Path, rt: Mapping[str, Path] | None = None
) -> bool:
    """macOS app: make sure VSScript's configuration names the app's Python for
    the app's VSScript, so that mpv's ``vapoursynth`` filter and ``vspipe`` can
    start it. True when the entry is there (already, or now). The Linux AppImage
    asks the same with ``rt=appimage_runtime(root)``: R81's VSScript reads the
    same file on Linux, and its manylinux wheel has no other way to find Python.

    This is the one file the app writes outside its own folders, and it is
    VapourSynth's design rather than ButterEye's: R81's VSScript has no other way
    to learn which Python to load, and ``vapoursynth config`` -- which a user of
    a pip-installed VapourSynth runs by hand -- writes exactly this line. The
    file is the user's, shared with every other VapourSynth they have; only the
    app's own line is replaced, stale ButterEye.app lines are dropped
    (``merge_vsscript_config``), and the write is atomic (a temporary file in
    the same folder, then ``os.replace``). Nothing is written when the line is
    already there, which is every start after the first.
    """
    config = vsscript_config(env)
    if config is None:
        return False
    if rt is None:
        rt = app_runtime(resources)
    line = vsscript_entry(
        rt["vsscript"], rt["python"], rt["libpython"], int(rt["python"].stat().st_mtime)
    )
    if line is None:
        return False
    try:
        text = config.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new = merge_vsscript_config(text, line, exists=os.path.isfile)
    if new == text:
        return True
    config.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.with_name(f".{config.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, config)
    finally:
        tmp.unlink(missing_ok=True)
    return True


def use_bundled_runtime() -> None:
    """Point the children ButterEye starts (mpv, vspipe) at the VapourSynth they
    are meant to use, through ``os.environ``, which they inherit.

    Windows: the VapourSynth of the Python that runs ButterEye -- the installer's
    bundled runtime (``point_at_runtime``) -- and take the current folder out of
    the program search (``NO_CWD_SEARCH``, inherited by the children).

    macOS, running from ButterEye.app (``MACOS_APP``): everything the app carries
    and nothing else (``point_at_app``), and VSScript's configuration line for
    the app's Python (``register_vsscript``).

    Linux, running from the AppImage (``LINUX_APP``): everything it carries
    (``point_at_appimage``) and VSScript's configuration line for its Python
    (``register_vsscript``), as the macOS app does.

    macOS otherwise (Homebrew): there is no bundle to point at, because
    Homebrew's ``python@3.14`` already *is* an interpreter whose VapourSynth is
    the one ``vspipe`` uses. What mpv needs is the VSScript file, which it cannot
    find for itself (``point_at_vsscript``). A no-op when VapourSynth is not
    installed.

    Nothing on Linux otherwise: it uses the distribution's, and mpv finds
    VSScript by name.
    """
    if not IS_WINDOWS:
        # Windows is decided first and macOS inside the non-Windows branch, so
        # that the Windows half is reached unchanged by a test that monkeypatches
        # IS_WINDOWS on a Mac (tests/core/test_windows_runtime_pure.py does).
        if IS_MACOS:
            # ButterEye.open() calls this, and a Mac with no VapourSynth, or one
            # whose cache directory cannot be written, must still open -- the
            # doctor is what tells the user. The filesystem is the only thing
            # here that can fail; the rest is string work on a mapping.
            if MACOS_APP is not None:
                point_at_app(os.environ, MACOS_APP)
                with contextlib.suppress(OSError):
                    register_vsscript(os.environ, MACOS_APP)
                return
            with contextlib.suppress(OSError):
                point_at_vsscript(os.environ, vsscript_library())
        elif LINUX_APP is not None:
            point_at_appimage(os.environ, LINUX_APP)
            with contextlib.suppress(OSError):
                register_vsscript(os.environ, LINUX_APP, appimage_runtime(LINUX_APP))
        return
    os.environ[NO_CWD_SEARCH] = "1"
    python_dir, vs_dir = runtime_dirs()
    point_at_runtime(os.environ, python_dir, vs_dir)
    hold_install_mutex()


#: the installer's record, in the folder it installed into (next to ``python``)
INSTALL_RECORD = "buttereye-install.json"


def install_root() -> str | None:
    """Windows: the folder the installer put this Python in (the parent of the
    ``python`` folder, with the install record), else None. macOS:
    ``Contents/Resources`` of the ButterEye.app this runs from (``MACOS_APP``),
    which holds the same install record, else None. The Linux AppImage: its root
    (``LINUX_APP``), which holds it too."""
    if IS_MACOS and not IS_WINDOWS and MACOS_APP is not None:
        return os.fspath(MACOS_APP)
    if LINUX_APP is not None:
        return os.fspath(LINUX_APP)
    python_dir, _ = runtime_dirs()
    root = os.path.dirname(python_dir)
    return root if os.path.isfile(os.path.join(root, INSTALL_RECORD)) else None


#: macOS: ``Contents/Resources`` of the ButterEye.app this process runs from, or
#: None (Homebrew, a development venv, and every other platform). Decided once,
#: at import, from where the interpreter and this file are (``app_resources``):
#: the app does not move while it runs. Every macOS answer that differs between
#: the app and Homebrew -- the programs, the plugins, the models, the doctor's
#: package database and its advice -- asks this one value.
MACOS_APP: Path | None = app_resources(sys.executable, __file__) if IS_MACOS else None

#: Linux: the root of the ButterEye AppImage this process runs from, or None
#: (Fedora's RPMs, a development venv, and every other platform). Decided once,
#: at import, from where the interpreter and this file are (``appimage_root``),
#: never from ``$APPDIR``. Every Linux answer that differs inside the AppImage --
#: the programs, the plugins, the doctor's package database and its advice --
#: asks this one value.
LINUX_APP: Path | None = (
    appimage_root(sys.executable, __file__) if sys.platform == "linux" else None
)


def install_mutex_name(folder: str) -> str:
    """The named mutex an installed ButterEye holds while it runs from ``folder``
    (its long path). The installer and uninstaller build the same name from
    ``$INSTDIR`` (packaging/windows/buttereye.nsi, ``MutexName``) and won't
    change files a running ButterEye uses: the path in lower case with ``/`` for
    ``\\``, since a mutex name can't hold a backslash; ``Local\\``: this session."""
    return "Local\\ButterEye-" + ntpath.normpath(folder).lower().replace("\\", "/")


def hold_install_mutex() -> None:
    """Windows: hold ``install_mutex_name`` of this installation for the life of
    the process (the GUI's and the command line's core both open through
    ``use_bundled_runtime``). Nothing outside an installed copy, or on Linux."""
    if sys.platform == "win32":
        root = install_root()
        if root is not None:
            w.hold_mutex(install_mutex_name(w.long_path(root)))


def bundled_vspipe() -> str | None:
    """Windows: ``vspipe.exe`` of the running Python's VapourSynth package (the
    installer's), by its full path, else None. Never searched for by name. The
    macOS app and the Linux AppImage: their own ``vspipe``, the same way. Linux
    and Homebrew: None (the vspipe on PATH)."""
    if not IS_WINDOWS:
        if IS_MACOS and MACOS_APP is not None:
            # the app's vspipe, from VapourSynth's wheel: it links the
            # libvsscript.dylib beside it by @loader_path
            app_vspipe = app_runtime(MACOS_APP)["vspipe"]
            return os.fspath(app_vspipe) if app_vspipe.is_file() else None
        if LINUX_APP is not None:
            # the AppImage's, from the manylinux wheel (RUNPATH $ORIGIN)
            img_vspipe = appimage_runtime(LINUX_APP)["vspipe"]
            return os.fspath(img_vspipe) if img_vspipe.is_file() else None
        return None
    _, vs_dir = runtime_dirs()
    if vs_dir is None:
        return None
    exe = os.path.join(vs_dir, "vspipe.exe")
    return exe if os.path.isabs(exe) and os.path.isfile(exe) else None


def vspipe_command() -> str:
    """What to start as vspipe: the bundled one by its full path on Windows,
    else plain ``vspipe`` (Linux, unchanged)."""
    return bundled_vspipe() or "vspipe"


def vspipe() -> str | None:
    """The vspipe to run, as a full path, or None: the bundled one on Windows,
    else the one on PATH (``executable``)."""
    return bundled_vspipe() or executable("vspipe")


# ======================================================================== processes
def search_windows_path(
    name: str, path: str, pathext: str, *, isfile: Callable[[str], bool] = os.path.isfile
) -> str | None:
    """Where Windows would find program ``name`` on ``path`` (``;``-separated),
    as a full path, or None. Unlike ``shutil.which`` (and ``CreateProcess``) it
    never looks in the current folder, and it skips PATH entries that are not
    absolute (``.``, ``bin``), which would mean the current folder too. A ``name``
    with a folder in it counts only when that folder is absolute. ``pathext``:
    the PATHEXT list, tried when ``name`` has none of those extensions."""
    exts = [e for e in (pathext or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    upper = name.upper()
    names = [name] if any(upper.endswith(e.upper()) for e in exts) else [name + e for e in exts]
    folder = ntpath.dirname(name)
    if folder:
        if not ntpath.isabs(name):
            return None
        return next((n for n in names if isfile(n)), None)
    for d in path.split(";"):
        d = d.strip().strip('"')
        if not d or not ntpath.isabs(d):
            continue
        for n in names:
            candidate = ntpath.join(d, n)
            if isfile(candidate):
                return candidate
    return None


def executable(name: str) -> str | None:
    """``shutil.which`` on Linux and macOS, except that ButterEye.app answers
    for the programs it carries (``APP_TOOLS``) with its own copy and nothing
    else, and so does the AppImage (``APPIMAGE_TOOLS``). On Windows a full path
    or None, never from the current folder (``search_windows_path``), and
    preferring the windowed ``.exe``: mpv ships ``mpv.com`` (a console wrapper)
    next to ``mpv.exe``, and PATHEXT finds the ``.com`` first."""
    if MACOS_APP is not None and name in APP_TOOLS:
        # the app's own, by full path and never from PATH: a Homebrew mpv
        # earlier on a user's PATH must not stand in for the one the app carries
        tool = MACOS_APP / APP_BIN / name
        return os.fspath(tool) if tool.is_file() else None
    if LINUX_APP is not None and name in APPIMAGE_TOOLS:
        # the same rule for the AppImage: the distribution's mpv, which may lack
        # the vapoursynth filter, never stands in for the one it carries
        tool = LINUX_APP / APP_BIN / name
        return os.fspath(tool) if tool.is_file() else None
    if not IS_WINDOWS:
        return shutil.which(name)
    path, pathext = os.environ.get("PATH", ""), os.environ.get("PATHEXT", "")
    if not os.path.splitext(name)[1]:
        found = search_windows_path(name + ".exe", path, pathext)
        if found is not None:
            return found
    return search_windows_path(name, path, pathext)


def group_spawn_kwargs(*, low_priority: bool = False) -> dict[str, Any]:
    """``subprocess``/``asyncio`` keyword arguments that make the child lead its
    own process group (§2.4). On Windows: its own console group, no console
    window, and inside ButterEye's kill-on-close job; ``low_priority`` asks for
    below-normal priority from the start (POSIX renices after the spawn)."""
    if sys.platform == "win32":
        w.ensure_job()
        flags = w.CREATE_NEW_PROCESS_GROUP | w.CREATE_NO_WINDOW
        if low_priority:
            flags |= w.BELOW_NORMAL_PRIORITY_CLASS
        return {"creationflags": flags}
    return {"start_new_session": True}


def lower_group_priority(pgid: int, nice: int) -> None:
    """Renice a whole POSIX process group; Windows set it at spawn already."""
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PGRP, pgid, nice)


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return w.pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform == "darwin":
        return d.pid_alive(pid)
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError, IndexError:
        return True
    return state not in ("Z", "X")


def group_alive(pgid: int | None) -> bool:
    """Is any process of the group led by ``pgid`` still there? On Windows the
    leader stands for its tree (a leader that exited leaves its children to the
    kill-on-close job)."""
    if not pgid:
        return False
    if sys.platform == "win32":
        return w.pid_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def signal_group(pgid: int, *, kill: bool) -> None:
    """End the group led by ``pgid``: SIGTERM (``kill=False``) or SIGKILL.

    Windows has no SIGTERM. The polite form closes the tree's windows (mpv then
    quits as if the user closed it); a tree without windows (vspipe, ffmpeg,
    probes) is terminated at once, since there is nothing to ask. Never raises
    for a group that is already gone."""
    if sys.platform == "win32":
        pids = w.tree(pgid)
        if not kill and w.post_close(set(pids)):
            return
        for pid in pids:
            w.terminate(pid)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, signal.SIGKILL if kill else signal.SIGTERM)


# ======================================================================== mpv
class DetachedChild:
    """A process started to outlive ButterEye (mpv). ``poll`` reaps it once it
    has exited: the exit code, negative signal number when killed (POSIX)."""

    def __init__(self, pid: int, popen: subprocess.Popen[bytes] | None = None) -> None:
        self.pid = pid
        self._popen = popen

    def open_pidfd(self) -> int | None:
        """A new descriptor the event loop can watch, else None: poll instead. A
        pidfd on Linux, a kqueue on macOS (``_darwin.exit_watch``); both go
        readable when the process exits and stay that way."""
        if sys.platform == "darwin":
            return d.exit_watch(self.pid)
        if sys.platform == "win32" or not hasattr(os, "pidfd_open"):
            return None
        try:
            fd: int = os.pidfd_open(self.pid)  # annotated: Any where the call is absent (macOS)
        except OSError:
            return None
        return fd

    def poll(self) -> int | None:
        if self._popen is not None:
            return self._popen.poll()
        if sys.platform == "win32":
            return None  # unreachable: Windows children always come with a Popen
        try:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            # reaped elsewhere (or not our child): judge by existence
            return None if pid_alive(self.pid) else -1
        if pid == 0:
            return None
        return os.waitstatus_to_exitcode(status)


def spawn_detached(
    argv: Sequence[str], log_path: Path, *, env: Mapping[str, str] | None = None
) -> DetachedChild:
    """Start ``argv`` so that it outlives ButterEye: stdin from the null device,
    stdout+stderr appended to ``log_path``, no other descriptor or handle
    inherited. POSIX: ``posix_spawnp`` with ``setsid`` (no asyncio transport or
    ``Popen`` finaliser can kill it). Windows: ``Popen`` that breaks away from
    ButterEye's job; if a parent job forbids that, mpv starts inside it and a
    warning says it will close with ButterEye. Raises ``FileNotFoundError``."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | O_CLOEXEC | O_BINARY, 0o600)
    try:
        if sys.platform == "win32":
            base = w.CREATE_NEW_PROCESS_GROUP | w.CREATE_NO_WINDOW
            for flags in (base | w.CREATE_BREAKAWAY_FROM_JOB, base):
                try:
                    p = subprocess.Popen(
                        list(argv), stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                        env=dict(env) if env is not None else None, close_fds=True,
                        creationflags=flags,
                    )  # fmt: skip
                except PermissionError:
                    if flags & w.CREATE_BREAKAWAY_FROM_JOB:
                        import logging

                        logging.getLogger(__name__).warning(
                            "Windows refused to let mpv leave ButterEye's job; "
                            "mpv will close when ButterEye does"
                        )
                        continue
                    raise
                return DetachedChild(p.pid, p)
            raise AssertionError("unreachable")
        try:
            pid = os.posix_spawnp(
                argv[0],
                list(argv),
                dict(env) if env is not None else dict(os.environ),
                file_actions=[
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, fd, 1),
                    (os.POSIX_SPAWN_DUP2, fd, 2),
                ],
                setsid=True,
            )
        except NotImplementedError:
            # python-build-standalone's Linux CPython (the AppImage trial's) is
            # built against glibc 2.17 headers, where POSIX_SPAWN_SETSID does not
            # exist yet, so CPython raises this before starting anything. The
            # same child, by fork+exec: its own session, the same descriptors,
            # and a Popen that reaps it (the Windows shape). Fedora's Python
            # never comes here.
            p = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                env=dict(env) if env is not None else None, close_fds=True,
                start_new_session=True,
            )  # fmt: skip
            return DetachedChild(p.pid, p)
        return DetachedChild(pid)
    finally:
        os.close(fd)


# ======================================================================== mpv control channel
def ipc_address(endpoint: Path) -> str:
    """What mpv's ``--input-ipc-server`` gets for ``endpoint``: the socket path on
    POSIX; on Windows a named pipe whose name is derived from the endpoint path
    (which already names this user's profile folder, so users never collide)."""
    if not IS_WINDOWS:
        return os.fspath(endpoint)
    digest = hashlib.sha256(os.fspath(endpoint).lower().encode("utf-8")).hexdigest()[:24]
    return rf"\\.\pipe\buttereye-{digest}"


def is_endpoint(st: os.stat_result) -> bool:
    """The runtime-folder entry for a session: a socket (POSIX) or the marker
    file standing for the pipe (Windows)."""
    return stat.S_ISREG(st.st_mode) if IS_WINDOWS else stat.S_ISSOCK(st.st_mode)


def prepare_endpoint(endpoint: Path) -> None:
    """Windows: create the marker file for the pipe (mpv creates the pipe itself),
    so discovery and cleanup find the session the way they find a socket."""
    if IS_WINDOWS:
        fd = os.open(endpoint, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
        os.close(fd)


async def open_ipc(
    endpoint: Path, *, limit: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to mpv's control channel. A pipe or socket nobody serves raises
    ``FileNotFoundError`` / ``ConnectionRefusedError``."""
    if sys.platform == "win32":
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=limit, loop=loop)
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        create = getattr(loop, "create_pipe_connection", None)
        if create is None:  # a selector loop: named pipes need the proactor
            raise ConnectionError("named pipes need the Windows proactor event loop")
        transport, _ = await create(lambda: protocol, ipc_address(endpoint))
        return reader, asyncio.StreamWriter(transport, protocol, reader, loop)
    return await asyncio.open_unix_connection(os.fspath(endpoint), limit=limit)


def ipc_peer(writer: asyncio.StreamWriter) -> tuple[int | None, bool] | None:
    """``(pid, same_user)`` of the process serving the connection, or None when it
    can't be told (then the caller refuses). Linux: ``SO_PEERCRED``. macOS:
    ``LOCAL_PEERCRED`` and ``LOCAL_PEERPID``, which raise ``ConnectionResetError``
    once the peer has gone. Windows: the pipe server's pid and the user its
    process runs as."""
    if sys.platform == "win32":
        pipe = writer.get_extra_info("pipe")
        handle = getattr(pipe, "handle", None)
        if handle is None:
            return None
        pid = w.pipe_server_pid(int(handle))
        if pid is None:
            return None
        me = w.my_sid()
        return pid, me is not None and w.process_sid(pid) == me
    sock = writer.get_extra_info("socket")
    if sock is None:
        return None
    if sys.platform == "darwin":
        return d.peer_cred(sock)
    raw = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", raw)
    return (pid if pid > 0 else None), uid == os.getuid()


# ======================================================================== power and heat
#: ``/sys/class/power_supply/*/type`` values that are the wall (or a charger on a
#: USB port): one of these online means the machine is not on its battery.
#: ``USB_C``, ``USB_PD`` and the rest of the ``USB_*`` family are what kernels
#: before 5.x reported for a USB charger, so any ``USB`` prefix counts.
_LINUX_WALL_TYPES = frozenset({"Mains"})
#: ``/sys/firmware/acpi/platform_profile`` values that are a power saver outright
#: (the kernel's ``platform_profile_names``)
_LINUX_SAVER_PROFILES = frozenset({"low-power"})
#: values that are a power saver only on firmware that offers no ``low-power``:
#: then the quietest profile is the one a saver applet picks
_LINUX_QUIET_PROFILES = frozenset({"quiet", "cool"})
#: values that are not a power saver
_LINUX_FULL_PROFILES = frozenset({"balanced", "balanced-performance", "performance"})


def _sys_root(sys_root: Path | None) -> Path:
    if sys_root is not None:
        return sys_root
    from buttereye.core.hw import detect  # detect imports this module

    return detect.SYS_ROOT


def _read_sys(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


def _linux_supplies(sys_root: Path | None = None) -> list[tuple[str, str, Path]] | None:
    """(type, scope, folder) of each power supply that powers the machine itself,
    from ``<sys>/class/power_supply``: a ``Device`` scope (a wireless mouse's or a
    controller's battery) and a ``UPS`` are left out. None when the folder cannot
    be read; empty on a desktop."""
    folder = _sys_root(sys_root) / "class" / "power_supply"
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return None
    out: list[tuple[str, str, Path]] = []
    for entry in entries:
        kind = _read_sys(entry / "type")
        scope = _read_sys(entry / "scope") or "Unknown"
        if kind is None or kind == "UPS" or scope == "Device":
            continue
        out.append((kind, scope, entry))
    return out


def _linux_on_battery(sys_root: Path | None = None) -> bool | None:
    """Linux: is this machine running from its battery? Pure but for reading
    ``<sys>/class/power_supply`` (``hw.detect.SYS_ROOT`` unless given).

    False as soon as a wall supply (``Mains``, ``USB``, ``USB_C`` ...) is online,
    whatever a battery says: a battery that is full, held at a charge limit or
    between charges reads ``Not charging`` or ``Unknown`` on mains. Otherwise True
    when a system battery is ``Discharging`` and False when it is not. None
    without a system battery (a desktop) or when nothing can be read."""
    supplies = _linux_supplies(sys_root)
    if not supplies:
        return None
    batteries: list[Path] = []
    for kind, _scope, entry in supplies:
        if kind in _LINUX_WALL_TYPES or kind.startswith("USB"):
            if _read_sys(entry / "online") == "1":
                return False
        elif kind == "Battery":
            batteries.append(entry)
    if not batteries:
        return None
    return any(_read_sys(b / "status") == "Discharging" for b in batteries)


def _linux_has_battery(sys_root: Path | None = None) -> bool:
    """Linux: does a battery power this machine (not a mouse's, not a UPS)?"""
    return any(kind == "Battery" for kind, _s, _e in _linux_supplies(sys_root) or ())


def _linux_low_power(sys_root: Path | None = None) -> bool | None:
    """Linux: is the ACPI platform profile a power saver
    (``<sys>/firmware/acpi/platform_profile``, what GNOME's and KDE's power
    profile switch sets on most laptops)? ``low-power`` is; ``quiet`` and
    ``cool`` are only on firmware that has no ``low-power`` among its
    ``platform_profile_choices``; ``balanced`` and ``performance`` are not.
    None without the file (a desktop, or a laptop whose saver is only
    power-profiles-daemon's own setting) and for ``custom`` or a value this
    does not know."""
    acpi = _sys_root(sys_root) / "firmware" / "acpi"
    profile = _read_sys(acpi / "platform_profile")
    if profile is None:
        return None
    if profile in _LINUX_SAVER_PROFILES:
        return True
    if profile in _LINUX_QUIET_PROFILES:
        choices = (_read_sys(acpi / "platform_profile_choices") or "").split()
        return "low-power" not in choices
    if profile in _LINUX_FULL_PROFILES:
        return False
    return None


def _win_power() -> tuple[int, int, int] | None:
    """Windows: ``GetSystemPowerStatus``'s (ACLineStatus, BatteryFlag,
    SystemStatusFlag), or None when it fails."""
    if sys.platform == "win32":
        return w.power_status()
    return None


#: ``SYSTEM_POWER_STATUS.BatteryFlag``: 128 no system battery, 255 unknown
_WIN_NO_BATTERY = frozenset({128, 255})


def _win_on_battery(status: tuple[int, int, int] | None) -> bool | None:
    """Windows: ``ACLineStatus`` 0 (offline) is on battery, 1 is on mains, 255
    is not known; None too when there is no system battery or Windows does not
    know its state (``BatteryFlag`` 128 or 255). Pure."""
    if status is None:
        return None
    ac_line, battery_flag, _saver = status
    if battery_flag in _WIN_NO_BATTERY:
        return None
    return {0: True, 1: False}.get(ac_line)


def _win_low_power(status: tuple[int, int, int] | None) -> bool | None:
    """Windows: is Battery Saver (Windows 10) or Energy Saver (Windows 11) on
    (bit 0 of ``SystemStatusFlag``)? Windows before 10 leaves the field 0, which
    reads as off. Pure."""
    if status is None:
        return None
    return bool(status[2] & 1)


def thermal_state() -> int | None:
    """How hot macOS says this Mac is: 0 nominal, 1 fair, 2 serious, 3 critical
    (``NSProcessInfo.thermalState``; ``types.THERMAL_STATES`` names them). None
    on Linux and Windows, which have no such scale, and when macOS does not
    answer."""
    if sys.platform == "darwin":
        return d.thermal_state()
    return None


def low_power_mode() -> bool | None:
    """Is the system's power saver on? macOS's Low Power Mode, Windows' Battery
    Saver or Energy Saver, and on Linux an ACPI platform profile of
    ``low-power`` (``_linux_low_power``). None when it cannot be read."""
    if sys.platform == "darwin":
        return d.low_power_mode()
    if sys.platform == "win32":
        return _win_low_power(_win_power())
    return _linux_low_power()


def on_battery() -> bool | None:
    """Is this machine running from a battery? IOKit's providing power source on
    macOS, ``GetSystemPowerStatus`` on Windows, ``/sys/class/power_supply`` on
    Linux (``_linux_on_battery``). None when it cannot be read, and off macOS on
    a machine without a battery."""
    if sys.platform == "darwin":
        return d.on_battery()
    if sys.platform == "win32":
        return _win_on_battery(_win_power())
    return _linux_on_battery()


def has_system_battery() -> bool:
    """Does a battery power this machine, so that ``on_battery`` can ever be
    True? For what the GUI offers: ``on_battery() is not None`` is not the
    question, because macOS answers False on mains on a Mac mini too. On macOS
    this is whether a power source can be read at all, which is every Mac; the
    settings offer the power preset on every Mac anyway, for heat."""
    if sys.platform == "darwin":
        return d.on_battery() is not None
    if sys.platform == "win32":
        status = _win_power()
        return status is not None and status[1] not in _WIN_NO_BATTERY
    return _linux_has_battery()


def power_snapshot() -> PowerSnapshot:
    """The three answers above at once, as a ``types.PowerSnapshot``.

    Cheap enough to ask whenever a decision is made: on the dev Mac all three
    together take about 0.1 ms (two Objective-C messages and one IOKit call that
    asks powerd); on Linux they are a few small reads in ``/sys``, on Windows one
    ``GetSystemPowerStatus`` each. So nothing caches them and an answer is never
    stale. ``thermal`` is None off macOS."""
    from buttereye.core.types import PowerSnapshot  # types imports this module

    return PowerSnapshot(
        on_battery=on_battery(), low_power=low_power_mode(), thermal=thermal_state()
    )


#: Linux: what holds the sleep lock, by full path, so an AppImage runs the host's
#: (logind is the host's) rather than whatever ``PATH`` finds first
SYSTEMD_INHIBIT = "/usr/bin/systemd-inhibit"
#: what the lock waits on: ``tail --pid`` ends when that process does
TAIL = "/usr/bin/tail"
#: what ``systemd-inhibit --list`` shows for a render's lock
INHIBIT_WHY = "Saving a smooth copy"


def inhibit_argv(pid: int) -> list[str]:
    """Linux: ``systemd-inhibit`` holding an idle and sleep lock for as long as
    ``tail --pid=<pid>`` runs, which is until ``pid`` exits. ``--mode=block``:
    logind refuses to suspend while it is held, as ``caffeinate -i`` does on
    macOS; the screen may still blank and lock."""
    return [
        SYSTEMD_INHIBIT, "--what=idle:sleep", "--mode=block", "--who=ButterEye",
        f"--why={INHIBIT_WHY}", "--", TAIL, f"--pid={pid}", "-f", "/dev/null",
    ]  # fmt: skip


#: how long after it starts a lock that ends counts as refused (no logind)
INHIBIT_CHECK_S = 2.0


def _watch_inhibit(proc: subprocess.Popen[bytes], pid: int) -> None:
    """Log when ``systemd-inhibit`` gives up at once (no logind, or no D-Bus to
    reach it): the render goes on, but the machine may sleep under it. An end
    by a signal is the caller's ``terminate``, not a refusal."""
    try:
        rc = proc.wait(timeout=INHIBIT_CHECK_S)
    except subprocess.TimeoutExpired:
        return
    if rc > 0:
        _log.warning(
            "systemd-inhibit for pid %d exited at once (%d): the computer may sleep "
            "during the render (is systemd-logind running?)",
            pid, rc,
        )  # fmt: skip


def keep_awake(pid: int) -> subprocess.Popen[bytes] | None:
    """Keep the machine from idle-sleeping until ``pid`` exits: on macOS
    ``caffeinate -i -w <pid>``, on Linux a ``systemd-inhibit`` lock around
    ``tail --pid=<pid>`` (``inhibit_argv``).

    The caller owns the returned process and may end it early (``terminate``;
    on Linux that ends ``tail`` as well and releases the lock); otherwise it
    exits by itself with ``pid``, so a ButterEye that crashes leaves nothing
    holding the machine awake past the job it was watching. The display still
    sleeps. None on Windows (``begin_activity`` holds it there), and when the
    program is missing or cannot be started."""
    if IS_WINDOWS:
        return None
    if sys.platform == "darwin":
        argv, name = d.caffeinate_argv(pid), "caffeinate"
    else:
        if not os.path.exists(SYSTEMD_INHIBIT) or not os.path.exists(TAIL):
            _log.info("no %s: the computer may sleep during the render", SYSTEMD_INHIBIT)
            return None
        argv, name = inhibit_argv(pid), "systemd-inhibit"
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        _log.warning("could not start %s for pid %d: %s", name, pid, exc)
        return None
    if sys.platform != "darwin":
        threading.Thread(
            target=_watch_inhibit, args=(proc, pid), name="inhibit-watch", daemon=True
        ).start()
    return proc


def begin_activity(reason: str) -> int | None:
    """Tell the system this process is doing work the user asked for, until
    ``end_activity``. macOS: no idle system sleep and no App Nap
    (``NSProcessInfo`` ``beginActivityWithOptions:reason:``,
    ``NSActivityUserInitiated``); ``reason`` is what ``pmset -g assertions``
    shows. Windows: no sleep, also on a Modern Standby laptop whose lid closes
    (``PowerCreateRequest`` with ``SystemRequired`` and ``ExecutionRequired``);
    ``reason`` is what ``powercfg /requests`` shows. Linux has nothing
    per-process: ``keep_awake`` holds the lock there. Returns a token for
    ``end_activity``, or None (on Linux, or when the system refused), which
    ``end_activity`` accepts."""
    if sys.platform == "darwin":
        return d.begin_activity(reason)
    if sys.platform == "win32":
        return w.begin_activity(reason)
    return None


def end_activity(token: int | None) -> None:
    """End an activity ``begin_activity`` started. None does nothing; never raises."""
    if token is None:
        return
    if sys.platform == "darwin":
        d.end_activity(token)
    elif sys.platform == "win32":
        w.end_activity(token)


@contextlib.contextmanager
def activity(reason: str) -> Iterator[int | None]:
    """``begin_activity`` for the length of a ``with`` block; yields the token."""
    token = begin_activity(reason)
    try:
        yield token
    finally:
        end_activity(token)


__all__ = [
    "APP_BIN",
    "APP_PROGRAMS",
    "APP_TOOLS",
    "APP_TRANSLOCATION_DIR",
    "APP_UNSET",
    "APP_VULKAN_ICD",
    "APPIMAGE_TOOLS",
    "APPIMAGE_UNSET",
    "INSTALL_RECORD",
    "IS_MACOS",
    "LINUX_APP",
    "MACOS_APP",
    "VSSCRIPT_CONFIG",
    "IS_WINDOWS",
    "NO_CWD_SEARCH",
    "VSTRT_LIB",
    "HAS_VSTRT",
    "DYLD_FALLBACK",
    "O_BINARY",
    "O_CLOEXEC",
    "O_NOFOLLOW",
    "DetachedChild",
    "activity",
    "begin_activity",
    "end_activity",
    "has_system_battery",
    "inhibit_argv",
    "keep_awake",
    "low_power_mode",
    "on_battery",
    "power_snapshot",
    "thermal_state",
    "app_quarantined",
    "release_app",
    "app_resources",
    "app_runtime",
    "appimage_root",
    "appimage_runtime",
    "bundled_vspipe",
    "executable",
    "fsync_dir",
    "group_alive",
    "group_spawn_kwargs",
    "hold_install_mutex",
    "install_mutex_name",
    "install_root",
    "ipc_address",
    "ipc_peer",
    "is_endpoint",
    "is_link",
    "lock",
    "locked_dir",
    "lower_group_priority",
    "open_ipc",
    "owned_by_me",
    "pid_alive",
    "plugin_file",
    "merge_vsscript_config",
    "point_at_app",
    "point_at_appimage",
    "point_at_runtime",
    "point_at_vsscript",
    "prepare_endpoint",
    "register_vsscript",
    "replace",
    "runtime_dirs",
    "search_windows_path",
    "set_mode",
    "shared_mode_bits",
    "signal_group",
    "spawn_detached",
    "try_lock",
    "unlock",
    "use_bundled_runtime",
    "vsscript_config",
    "vsscript_entry",
    "vspipe",
    "vspipe_command",
    "vsscript_library",
]
