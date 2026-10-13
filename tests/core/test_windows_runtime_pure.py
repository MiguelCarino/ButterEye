# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""What the Windows installer's runtime needs from the core (docs/PLATFORMS.md,
phase 4), tested on Linux: child processes pointed at the bundled VapourSynth,
the first-run runtime folder, UTF-8 output when redirected, vulkaninfo written
to a file, the Visual C++ runtime check and the installer's Repair as the fix.
The Windows half runs in tests/windows (test_installer_windows.py).

``use_bundled_runtime`` has three platform branches, so all three are stated
here with the platform faked: Windows points children at the bundled Python,
macOS hands mpv the VSScript library it cannot find for itself (phase 4), and
Linux does nothing. The real Mac's half of the macOS branch - mpv loading the
file and playing - is tests/macos/test_runtime_macos.py.

The search *underneath* that macOS branch is here too, and that part is not
pure: ``osapi._darwin`` cannot be imported off a Mac at all (it reads libproc's
symbols out of libSystem while the module body runs), so those tests skip off
Darwin. They are in this file rather than in tests/macos/test_runtime_macos.py
because they need no Homebrew, no VapourSynth and no mpv - a directory, a mode
and ``sys.path`` are the whole apparatus - so they run on GitHub's bare
``macos-latest`` runner, where the whole of tests/macos skips for want of
bottles. That is deliberate: what they hold shut is a code-execution hole (the
search once answered with a dylib planted in the directory ButterEye was started
in, and what it answers is ``dlopen``ed inside mpv), and a security rule checked
only on the maintainer's own Mac is a rule nobody notices breaking."""

from __future__ import annotations

import io
import os
import stat
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from buttereye.cli import utf8_stdio
from buttereye.core import osapi
from buttereye.core.bench import runner
from buttereye.core.doctor import checks_pkgs as pk
from buttereye.core.doctor.proc import CmdResult
from buttereye.core.errors import ButterEyeError
from buttereye.core.hw import detect
from buttereye.core.mpvctl import launcher
from buttereye.core.render import jobs
from buttereye.core.types import Finding, Severity


# ---------------------------------------------------------------- the bundled runtime
def _bundle(tmp_path: Path) -> tuple[str, str]:
    py = tmp_path / "python"
    vs = py / "Lib" / "site-packages" / "vapoursynth"
    vs.mkdir(parents=True)
    (vs / "vsscript.dll").write_bytes(b"MZ")
    return str(py), str(vs)


def test_children_find_the_bundled_vapoursynth_first(tmp_path: Path) -> None:
    py, vs = _bundle(tmp_path)
    env = {"PATH": r"C:\Windows\system32;C:\mpv", "VSSCRIPT_PATH": r"C:\Other\vsscript.dll"}
    osapi.point_at_runtime(env, py, vs, sep=";")
    assert env["VSSCRIPT_PATH"] == os.path.join(vs, "vsscript.dll")
    assert env["PATH"].split(";") == [py, vs, r"C:\Windows\system32", r"C:\mpv"]


def test_pointing_twice_changes_nothing(tmp_path: Path) -> None:
    py, vs = _bundle(tmp_path)
    env = {"PATH": f"C:\\mpv;{vs}"}
    osapi.point_at_runtime(env, py, vs, sep=";")
    once = dict(env)
    osapi.point_at_runtime(env, py, vs, sep=";")
    assert env == once
    assert env["PATH"].split(";") == [py, vs, r"C:\mpv"]


def test_no_vapoursynth_package_leaves_vsscript_alone(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    osapi.point_at_runtime(env, str(tmp_path), None, sep=";")
    assert env == {"PATH": str(tmp_path)}
    vs = tmp_path / "vapoursynth"
    vs.mkdir()  # a package without vsscript.dll (not a Windows build)
    osapi.point_at_runtime(env, str(tmp_path), str(vs), sep=";")
    assert "VSSCRIPT_PATH" not in env


def test_linux_environment_is_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    """Linux uses the distribution's VapourSynth and its mpv finds VSScript by
    name, so the call has nothing to do and may change nothing.

    The platform is faked rather than inherited, which is what makes this a
    statement about Linux wherever it runs: on a real Mac the same call sets
    VSSCRIPT_PATH and DYLD_FALLBACK_LIBRARY_PATH (the test below), and on Windows
    it sets PATH, so without the two monkeypatches this asserts the Linux
    contract only when it happens to be running on Linux - and *writes those
    variables into the pytest session's own environment* on a Mac, where the next
    test to ask what a real run looks like would then be reading this one's
    leftovers rather than the production call.
    """
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", False)
    before = dict(os.environ)
    osapi.use_bundled_runtime()
    assert dict(os.environ) == before
    assert osapi.runtime_dirs()[0] == os.path.dirname(sys.executable)


def test_macos_children_are_handed_the_vsscript_library_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The macOS half of the same call, faked the way the Windows half above is
    faked, so this pure file can state it on every platform. The real Mac's copy
    of this question - the file mpv really loads, and mpv really playing - is
    tests/macos/test_runtime_macos.py.

    mpv's vapoursynth filter dlopens VSScript by two bare leaf names, and
    Homebrew's only copy is inside VapourSynth's libexec virtualenv, on no dyld
    search path, so a child has to be handed the file: the exact file in
    VSSCRIPT_PATH, and its own directory in front of dyld's fallback list for an
    mpv that reads only that. Both names are set with monkeypatch first, so the
    call cannot leak into the rest of the session however it writes them.
    """
    folder = tmp_path / "Cellar" / "vapoursynth" / "81" / "libexec" / "vapoursynth"
    folder.mkdir(parents=True)
    lib = folder / "libvsscript.dylib"
    lib.write_bytes(b"\xcf\xfa\xed\xfe")
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "vsscript_library", lambda: lib)
    stale = str(tmp_path / "upgraded-away" / "libvsscript.dylib")
    monkeypatch.setenv("VSSCRIPT_PATH", stale)
    monkeypatch.setenv(osapi.DYLD_FALLBACK, f"/keep{os.pathsep}{folder}")
    before = dict(os.environ)

    osapi.use_bundled_runtime()

    assert os.environ["VSSCRIPT_PATH"] == str(lib)
    # mpv does not look past a VSSCRIPT_PATH it cannot load, so the inherited one
    # naming a keg a brew upgrade has moved may not be left in place
    assert Path(os.environ["VSSCRIPT_PATH"]).is_file()
    # the library's own directory first, the user's list kept, the duplicate gone
    assert os.environ[osapi.DYLD_FALLBACK].split(os.pathsep) == [str(folder), "/keep"]
    # and nothing else: not NO_CWD_SEARCH, not PATH, not DYLD_LIBRARY_PATH (which
    # was measured to rewrite even absolute dlopen paths in every child)
    changed = {k for k in set(before) | set(os.environ) if before.get(k) != os.environ.get(k)}
    assert changed == {"VSSCRIPT_PATH", osapi.DYLD_FALLBACK}


def test_macos_without_vapoursynth_is_left_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Mac with no VapourSynth has no file to hand over, and ButterEye.open()
    must still open there: no variable is invented, because a directory with no
    VSScript in it on a child's library search would read as a fix to anything
    that only asks whether the variable is set."""
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi, "IS_MACOS", True)
    monkeypatch.setattr(osapi, "vsscript_library", lambda: None)
    # setenv then delenv, rather than delenv alone: monkeypatch records nothing for
    # a name that is already absent, so this is what makes a broken implementation
    # that *did* set it fail here without also leaking it into the session
    monkeypatch.setenv("VSSCRIPT_PATH", "deleted on the next line")
    monkeypatch.delenv("VSSCRIPT_PATH")
    monkeypatch.setenv(osapi.DYLD_FALLBACK, "/keep")
    before = dict(os.environ)
    osapi.use_bundled_runtime()
    assert dict(os.environ) == before


# ------------------------------------------------- the VSScript search as a trust boundary
#: a Mach-O magic number. The search only stats a candidate, so any bytes would
#: do - but a plant that dyld would really open is what the attack looks like,
#: and the file has to exist at all for ``is_file`` to answer.
MACHO_MAGIC = b"\xcf\xfa\xed\xfe"

needs_darwin = pytest.mark.skipif(
    sys.platform != "darwin", reason="the VSScript search exists only on macOS"
)


def _plant_vsscript(folder: Path) -> Path:
    """A ``vapoursynth/libvsscript.dylib`` under ``folder``; the file's path.

    A real directory with real bytes in it, because the production search stats
    both, and 0o755 - ours, and nobody else's to write - so that the only thing
    able to refuse it is the rule the calling test is about.
    """
    pkg = folder / "vapoursynth"
    pkg.mkdir(parents=True)
    pkg.chmod(0o755)
    lib = pkg / "libvsscript.dylib"
    lib.write_bytes(MACHO_MAGIC)
    return lib


@needs_darwin
def test_macos_vsscript_is_never_taken_from_the_directory_it_was_started_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What ``vsscript_library`` answers is handed to mpv as ``VSSCRIPT_PATH``
    and ``dlopen``ed, and the search reads ``sys.path``, whose first entry under
    ``python -m`` - which is how ButterEye runs from a checkout - is the working
    directory. So running it from a directory holding
    ``./vapoursynth/libvsscript.dylib`` loaded that file into the player:
    measured before the fix, the answer was a planted copy under /private/tmp.

    The control comes first and is the point of the test. The same plant, on
    ``sys.path`` the same way, is returned while it is somewhere else - so when
    it stops being returned the reason is that it became the working directory,
    and not that the search never looked or that the plant was refused for being
    a stub. ``os.getcwd()`` is the spelling ``python -m`` puts on the path; the
    symlinked spelling is the next test.
    """
    work = tmp_path.resolve() / "work"
    work.mkdir()
    planted = _plant_vsscript(work)
    monkeypatch.syspath_prepend(os.fspath(work))
    assert osapi.vsscript_library() == planted, "the search does not read sys.path at all"

    monkeypatch.chdir(work)
    assert os.getcwd() == os.fspath(work)  # the entry ``python -m`` would add

    answer = osapi.vsscript_library()
    assert answer is None or work not in answer.parents, (
        f"started in {work}, and {answer} from it would be handed to mpv and dlopened"
    )


@needs_darwin
def test_macos_vsscript_skips_the_working_directory_under_either_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sys.path`` can name the working directory in a way ``os.getcwd()`` does
    not spell it - through a symlink, which is how ``/var/folders/...`` names
    ``/private/var/folders/...`` on every Mac, and so how ``tmp_path`` itself is
    spelled - so the skip compares each entry as given *and* resolved. This is
    the half that needs the second comparison: with only the literal one, anyone
    who starts ButterEye through a symlinked path still has the hole.
    """
    work = tmp_path.resolve() / "work"
    work.mkdir()
    _plant_vsscript(work)
    link = tmp_path.resolve() / "link"
    link.symlink_to(work)
    monkeypatch.syspath_prepend(os.fspath(link))
    through_link = link / "vapoursynth" / "libvsscript.dylib"
    assert osapi.vsscript_library() == through_link, "the plant is not reachable through the link"

    monkeypatch.chdir(work)  # the directory the link leads to, named the other way
    answer = osapi.vsscript_library()
    assert answer is None or link not in answer.parents, (
        f"{answer} is the working directory's own, reached through {link}"
    )


@needs_darwin
@pytest.mark.parametrize(
    ("mode", "who"),
    [(0o775, "the group"), (0o707, "anyone")],
    ids=["group-writable", "other-writable"],
)
def test_macos_vsscript_is_never_taken_from_a_directory_others_can_write(
    mode: int, who: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the rule, and the one that still matters once the
    working directory is out of the way: a directory writable by group or by
    other is a place any other process on this machine can drop a dylib, and
    what the search finds there is ``dlopen``ed inside mpv - SCOPE 4.3's threat
    model is a same-machine process, not a remote one.

    The directory is not the working directory, so the skip above cannot be what
    refuses it, and the mode is the only thing that differs between the refusal
    and the control: the same directory, the same plant, 0o750, is returned. The
    *owner* half cannot be arranged in ``tmp_path`` - owning a directory as
    somebody else needs a second account on the machine - so it is tested
    against a real system directory in the test below.
    """
    loose = tmp_path.resolve() / "loose"
    loose.mkdir()
    planted = _plant_vsscript(loose)
    planted.parent.chmod(mode)
    monkeypatch.syspath_prepend(os.fspath(loose))
    assert Path.cwd() != loose, "the working-directory skip would be what refuses this"

    answer = osapi.vsscript_library()
    assert answer is None or loose not in answer.parents, (
        f"{answer} sits in a directory {who} can write to, and mpv would dlopen it"
    )

    planted.parent.chmod(0o750)  # the same plant, in a directory now ours alone
    assert osapi.vsscript_library() == planted, (
        "the same directory is not found even when its mode is ours alone, so the refusal "
        "above was not the mode"
    )


@needs_darwin
def test_macos_vsscript_is_never_taken_from_a_directory_another_user_owns() -> None:
    """The owner half, against a real directory, because ``tmp_path`` cannot
    provide one: a directory owned by somebody else needs a second account, and
    this suite creates no users. Read-only - nothing here writes to /usr/local.

    ``/usr/local`` on an arm64 Homebrew Mac is root's and 0o755, the wrong owner
    with a tight mode, which is what isolates the half under test: the mode rule
    alone accepts it. Accepting it is the real mistake, because its owner can
    loosen those permissions whenever they like, and on an Intel Mac this is
    Homebrew's own prefix - where a ``brew`` install of anything at all would put
    a dylib ButterEye then hands to mpv. Skipped there, where it is the user's.
    """
    from buttereye.core.osapi import _darwin

    probe = Path("/usr/local")
    if not probe.is_dir():
        pytest.skip("no /usr/local on this Mac")
    st = probe.stat()
    if st.st_uid == os.getuid():
        pytest.skip("/usr/local is this user's own (Homebrew's prefix on an Intel Mac)")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        pytest.skip(f"/usr/local is mode {st.st_mode & 0o777:o} here: the mode rule refuses it")
    assert _darwin._trustworthy_dir(probe) is False


@needs_darwin
def test_macos_vsscript_answers_none_rather_than_raising_when_nothing_is_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ButterEye.open()`` calls this on every Mac before anything else, and the
    doctor calls it while reporting a problem, so "nothing I can trust" has to be
    None rather than an exception or a guess.

    ``sys.path`` is replaced rather than prepended to, because "nothing
    anywhere" is what is being stated: all that is left is a world-writable
    plant and the empty string an interactive interpreter puts there, and
    ``HOMEBREW_PREFIX`` names a prefix with no VapourSynth in it. So the answer
    is None for the reason the test is named after and not because the machine
    happens to have nothing - and an implementation that fell back to its best
    remaining candidate, having found nothing it trusted, hands back the plant
    and fails here.
    """
    loose = tmp_path.resolve() / "loose"
    loose.mkdir()
    _plant_vsscript(loose).parent.chmod(0o777)
    empty = tmp_path.resolve() / "prefix"
    (empty / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOMEBREW_PREFIX", os.fspath(empty))
    monkeypatch.setattr(sys, "path", ["", os.fspath(loose)])
    assert osapi.vsscript_library() is None


def test_windows_runtime_takes_the_current_folder_out_of_the_search(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    py, vs = _bundle(tmp_path)
    # setenv first: monkeypatch records nothing for a name that is already absent,
    # and the call below really writes this one into os.environ, where on Windows
    # it would then change how every later test's children are searched for
    monkeypatch.setenv(osapi.NO_CWD_SEARCH, "deleted on the next line")
    monkeypatch.delenv(osapi.NO_CWD_SEARCH)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(osapi, "runtime_dirs", lambda: (py, vs))
    monkeypatch.setattr(osapi, "point_at_runtime", lambda env, p, v: None)
    osapi.use_bundled_runtime()
    assert os.environ[osapi.NO_CWD_SEARCH] == "1"


def test_windows_vspipe_is_the_bundled_one_by_its_full_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    py, vs = _bundle(tmp_path)
    monkeypatch.setattr(osapi, "runtime_dirs", lambda: (py, vs))
    monkeypatch.setattr(osapi.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Linux: the distribution's, by name, as before
    assert osapi.bundled_vspipe() is None
    assert osapi.vspipe_command() == "vspipe"
    assert osapi.vspipe() == "/usr/bin/vspipe"
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(osapi, "executable", lambda name: None)
    assert osapi.bundled_vspipe() is None  # no vspipe.exe in the package: not searched for
    assert osapi.vspipe() is None and osapi.vspipe_command() == "vspipe"
    (Path(vs) / "vspipe.exe").write_bytes(b"MZ")
    exe = os.path.join(vs, "vspipe.exe")
    assert osapi.bundled_vspipe() == exe
    assert osapi.vspipe() == exe and osapi.vspipe_command() == exe
    assert runner.find_tools(_paths(tmp_path)).vspipe == exe


def test_install_mutex_name_is_what_the_installer_builds() -> None:
    # buttereye.nsi MutexName: the long path, lower case, "/" for "\\"
    name = osapi.install_mutex_name(r"C:\Users\Ana Núñez\AppData\Local\Programs\ButterEye")
    assert name == "Local\\ButterEye-c:/users/ana núñez/appdata/local/programs/buttereye"
    assert "\\" not in name.removeprefix("Local\\")
    assert osapi.install_mutex_name("D:\\Apps\\BE\\") == "Local\\ButterEye-d:/apps/be"


def test_install_root_is_the_folder_with_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    py, vs = _bundle(tmp_path)
    monkeypatch.setattr(osapi, "runtime_dirs", lambda: (py, vs))
    assert osapi.install_root() is None  # a Python that no installer put there
    (tmp_path / osapi.INSTALL_RECORD).write_text("{}")
    assert osapi.install_root() == str(tmp_path)
    osapi.hold_install_mutex()  # Linux: nothing to hold


def _paths(tmp_path: Path) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(rpm_plugin_dir=tmp_path, rpm_data_dir=tmp_path)


def test_windows_search_never_uses_the_current_folder() -> None:
    files = {r"C:\Users\me\Downloads\vspipe.exe", r"C:\BE\vs\vspipe.exe", r"C:\mpv\mpv.com",
             r"C:\mpv\mpv.exe", r"bin\tool.exe", r"C:\x\tool.exe"}  # fmt: skip
    lower = {f.lower() for f in files}

    def isfile(p: str) -> bool:  # Windows file names ignore case
        return p.lower() in lower

    def search(name: str, path: str, pathext: str, *, isfile: Callable[[str], bool]) -> str | None:
        found = osapi.search_windows_path(name, path, pathext, isfile=isfile)
        return found and found.lower()

    ext = ".COM;.EXE;.BAT"
    # relative PATH entries ("." and "bin") would be the current folder: skipped
    assert search("vspipe", r".;bin;C:\BE\vs", ext, isfile=isfile) == r"c:\be\vs\vspipe.exe"
    assert search("tool", r"bin;C:\x", ext, isfile=isfile) == r"c:\x\tool.exe"
    assert search("tool", "bin", ext, isfile=isfile) is None
    assert search("vspipe", "", ext, isfile=isfile) is None
    # PATHEXT order, an extension given, quoted entries, a folder in the name
    assert search("mpv", r'"C:\mpv"', ext, isfile=isfile) == r"c:\mpv\mpv.com"
    assert search("mpv.exe", r"C:\mpv", ext, isfile=isfile) == r"c:\mpv\mpv.exe"
    assert search(r"bin\tool", r"C:\x", ext, isfile=isfile) is None
    assert search(r"C:\x\tool", "", ext, isfile=isfile) == r"c:\x\tool.exe"
    assert search(r"\x\tool", "", ext, isfile=isfile) is None  # no drive: relative


def test_windows_executable_prefers_the_exe_and_is_always_absolute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found: list[str] = []

    def fake(name: str, path: str, pathext: str) -> str | None:
        found.append(name)
        return r"C:\mpv\mpv.exe" if name == "mpv.exe" else None

    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(osapi, "search_windows_path", fake)
    assert osapi.executable("mpv") == r"C:\mpv\mpv.exe"
    assert osapi.executable("vspipe") is None and found[-2:] == ["vspipe.exe", "vspipe"]
    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(osapi.shutil, "which", lambda name: "./mpv")
    assert osapi.executable("mpv") == "./mpv"  # Linux: shutil.which, unchanged


# ---------------------------------------------------------------- first run
def test_first_play_creates_the_runtime_folder_with_its_parents_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "LocalAppData" / "ButterEye" / "run"
    with pytest.raises(ButterEyeError):  # POSIX: $XDG_RUNTIME_DIR must already exist
        launcher.ensure_private_dir(run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    assert launcher.ensure_private_dir(run) == run
    assert run.is_dir()


# ---------------------------------------------------------------- redirected output
def test_redirected_output_is_utf8() -> None:
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    utf8_stdio(stream, None)
    stream.write("23.976 → 47.952 fps ✓\n")
    stream.flush()
    assert raw.getvalue().decode("utf-8") == "23.976 → 47.952 fps ✓\n"


def test_utf8_streams_are_left_alone() -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    utf8_stdio(stream)
    assert stream.errors == "strict"


# ---------------------------------------------------------------- vulkaninfo
async def test_windows_vulkaninfo_report_comes_from_a_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, ...]] = []
    report = "GPU0:\n\tdeviceName = Test GPU\n"

    async def fake_run(argv: tuple[str, ...], *, timeout_s: float) -> CmdResult:
        seen.append(tuple(argv))
        if "-o" in argv:
            Path(argv[argv.index("-o") + 1]).write_text(report, encoding="utf-8")
        return CmdResult(tuple(argv), 0, "", "")

    monkeypatch.setattr(detect, "run", fake_run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(detect, "vulkaninfo_exe", lambda: "vulkaninfo.exe")
    res = await detect.run_vulkaninfo(5.0)
    assert seen[0][:3] == ("vulkaninfo.exe", "--text", "-o")
    assert res.ok and res.stdout == report
    assert not Path(seen[0][3]).exists()  # the private temporary folder is gone

    monkeypatch.setattr(osapi, "IS_WINDOWS", False)
    monkeypatch.setattr(detect, "vulkaninfo_exe", lambda: "vulkaninfo")
    await detect.run_vulkaninfo(5.0)
    assert seen[1] == ("vulkaninfo",)  # Linux: unchanged


async def test_windows_vulkaninfo_without_a_report_keeps_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run(argv: tuple[str, ...], *, timeout_s: float) -> CmdResult:
        return CmdResult(tuple(argv), None, "", "", timed_out=True)

    monkeypatch.setattr(detect, "run", fake_run)
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    res = await detect.run_vulkaninfo(1.0)
    assert res.timed_out and not res.ok and res.stdout == ""


# ---------------------------------------------------------------- Visual C++ runtime
def test_vcruntime_check() -> None:
    assert pk.read_vcruntime() is None  # not Windows
    ok = pk.vcruntime_finding((14, 51, 36247))
    assert ok.id == "packages.vcruntime" and ok.severity is Severity.OK
    missing = pk.vcruntime_finding(None)
    assert missing.severity is Severity.BLOCKING
    assert missing.commands == ("winget install --exact --id Microsoft.VCRedist.2015+.x64",)
    old = pk.vcruntime_finding((14, 44, 35211))  # what PySide6 carries: too old
    assert old.severity is Severity.BLOCKING
    assert old.commands == ("winget upgrade --exact --id Microsoft.VCRedist.2015+.x64",)
    assert pk.vcruntime_finding(pk.VCRUNTIME_MIN).severity is Severity.OK


# ---------------------------------------------------------------- fixes say Repair on Windows
def test_missing_vspipe_or_plugins_point_at_the_installer_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fix, commands = runner._install_fix(runner.REPAIR, runner.INSTALL_VSPIPE)
    assert commands == (runner.INSTALL_VSPIPE,)  # Linux: the dnf line
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    fix, commands = runner._install_fix(runner.REPAIR, runner.INSTALL_VSPIPE)
    assert commands == () and fix == runner.REPAIR
    err = jobs._missing("vspipe", None)
    assert err.commands == ()
    assert not any("dnf" in c or "winget" in c for c in err.commands)


# ---------------------------------------------------------------- no Vulkan loader
def _probe_plugins(monkeypatch: pytest.MonkeyPatch, windows: bool, loader: bool) -> Finding:
    from buttereye.core.doctor import probe
    from buttereye.core.types import PluginStatus

    monkeypatch.setattr(osapi, "IS_WINDOWS", windows)
    raw = {
        "vs_core": "81",
        "python": "3.14.8",
        "plugins": [
            {"ns": "rife", "loaded": False, "error": "GetLastError() returned 126"},
            {"ns": "mv", "loaded": True, "loaded_from": "C:/ButterEye/vapoursynth/mvtools.dll"},
        ],
    }
    status = [
        PluginStatus("RIFE-ncnn-Vulkan", pk.PKG_RIFE, "1", "buttereye", False, None, ()),
        PluginStatus("MVTools", pk.PKG_MVTOOLS, "1", "buttereye", True, None, ()),
    ]
    out = probe.probe_findings(
        probe.ProbeOutcome(raw, None, None), status, vspipe_core="81", vulkan_loader=loader
    )
    return next(f for f in out if f.id == "probe.plugins")


def test_rife_without_the_vulkan_loader_is_the_driver_not_a_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    f = _probe_plugins(monkeypatch, windows=True, loader=False)
    assert f.severity is Severity.DEGRADED
    assert "Vulkan loader" in f.title.key
    assert "graphics driver" in f.fix.key and f.commands == ()
    assert any(e.startswith("mv: loaded from") for e in f.evidence)


@pytest.mark.parametrize(("windows", "loader"), [(True, True), (False, False), (False, True)])
def test_a_rife_load_failure_otherwise_stays_a_reinstall(
    monkeypatch: pytest.MonkeyPatch, windows: bool, loader: bool
) -> None:
    f = _probe_plugins(monkeypatch, windows=windows, loader=loader)
    assert f.severity is Severity.DEGRADED
    assert f.title.key == "{names} did not load inside mpv"
