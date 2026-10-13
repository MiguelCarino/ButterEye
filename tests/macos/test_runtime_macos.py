# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The phase-4 runtime fix: making mpv able to load VapourSynth's VSScript
(docs/PLATFORMS.md, macOS phase 4).

This is the gate for the one change without which live smoothing does not work
on a Mac at all. Homebrew's mpv has the ``vapoursynth`` filter compiled in, but
it does not link VSScript: it ``dlopen``s it by bare leaf name, so dyld searches
``/usr/lib``, the Swift toolchain directories and the dyld cache and never
reaches Homebrew, where the only copy is - inside the formula's own ``libexec``
virtual environment. Out of the box the filter is therefore removed from the
chain (BE-3003), which is what every phase of this port measured. The failure,
reproduced by the first half of ``test_mpv_runs_a_vapoursynth_script_...``::

    [vapoursynth] Failed to load VapourSynth VSScript library: dlopen(
      libvapoursynth-script.dylib, 0x0006): tried: … (no such file) …
    [user_filter_wrapper] Creating filter 'vapoursynth' failed.

``osapi.use_bundled_runtime`` is where the Windows build already points children
at its bundled VapourSynth, and its macOS branch is what is tested here:
``osapi.point_at_vsscript`` sets ``VSSCRIPT_PATH`` and puts the library's own
directory on ``DYLD_FALLBACK_LIBRARY_PATH``. ``osapi.vsscript_library`` is the
question underneath it - where the file actually is - which the doctor and the
Homebrew tap ask the same way.

The assertions are behavioural on purpose. A variable being set proves nothing:
a variable naming an empty directory, or a file a ``brew upgrade`` has taken
away, is set just as firmly as a working one. So the central test runs mpv twice
- once without the fix, which must fail, once with the environment the fix
builds, which must play - and the script mpv runs writes a file naming the clip
it was handed, because a log line can be matched by a test that has been fooled
and a file on disk cannot.

Three things measured here while writing these tests are worth writing down.

* **a stale ``VSSCRIPT_PATH`` is fatal, not inert.** With it naming a file that
  does not exist, mpv tries that one path, fails, and drops the filter - even
  with ``DYLD_FALLBACK_LIBRARY_PATH`` correctly set alongside it, which on its
  own loads the filter. So the variable is not a hint mpv falls back from; it
  replaces the search. That is also the staleness that can still happen, because
  the value can come from the user's own shell, so
  ``test_a_stale_vsscript_path_...`` makes one by hand, proves through mpv that
  it really does break playback, and then requires the next
  ``use_bundled_runtime`` to replace it.
* **a dangling symlink is no better.** dyld reports it as "no such file" and mpv
  fails exactly as it does with nothing set, so a managed link whose keg has
  been upgraded away has to be replaced rather than trusted
  (``test_nothing_it_leaves_on_disk_can_go_stale_unnoticed``).
* **mpv reaches the second name too.** The failure above shows only
  ``libvapoursynth-script.dylib``, which made the second look unreachable, but
  ``DYLD_FALLBACK_LIBRARY_PATH`` pointed straight at the library's own directory
  - where the file is called ``libvsscript.dylib`` and no symlink exists - also
  loads the filter and plays. So a symlink is defence rather than necessity, and
  the tests here assert through whichever of the two shapes the fix uses rather
  than requiring one.

The search underneath all of this is also a trust boundary, which is the other
thing these tests are for. ``vsscript_library`` reads ``sys.path``, whose first
entry under ``python -m`` is the working directory, and what it answers is
``dlopen``ed inside mpv: before the skip now in ``_darwin._vsscript_dirs``,
running ButterEye from a directory holding ``./vapoursynth/libvsscript.dylib``
loaded that file into the player (measured; the answer was a planted copy under
/private/tmp). The two tests named ``..._loses_to_...`` are that attack on a Mac
that has the real library, so they can assert the answer is Homebrew's file
rather than only that it is not the plant. Their Homebrew-free halves - and the
owner and mode rules of ``_darwin._trustworthy_dir``, which need no VapourSynth
at all - are in tests/core/test_windows_runtime_pure.py, so that they also run
on a Mac with no bottles.

macOS only, and only when ``BUTTEREYE_TEST_BREW`` is set: that is the variable
phase 2 gave ``test_doctor_macos.py`` to mean "Homebrew's bottles are installed
on this machine", and nothing here can run without a real VapourSynth and a real
mpv. GitHub's ``macos-latest`` runner has neither (``.github/workflows/macos.yml``
installs no bottle) and never sets the variable, so the whole file skips there
rather than failing - the mistake phases 2 and 3 each made once.

Nothing here binds a Unix socket, so the ``short_dir`` fixture and the 104-byte
``sun_path`` limit it exists for do not apply. With the fix as it stands nothing
here writes outside ``tmp_path`` at all - it touches only ``os.environ``, which
``clean_env`` restores. Should it start managing a file,
``test_nothing_it_leaves_on_disk_can_go_stale_unnoticed`` breaks that file on
purpose and repairs it in a ``finally``, because the integration tests and the
user's own next run read the same directory.

Measured on the machine these were written for (macOS 27.0.1, Apple M2,
2026-10-12): mpv 0.41.0 and VapourSynth R81 from Homebrew, VSScript at
``<prefix>/Cellar/vapoursynth/81/libexec/lib/python3.14/site-packages/``
``vapoursynth/libvsscript.dylib``, and nothing of either name in ``<prefix>/lib``.
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

BREW = os.environ.get("BUTTEREYE_TEST_BREW", "")

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
    pytest.mark.skipif(not BREW, reason="BUTTEREYE_TEST_BREW not set"),
]

if sys.platform == "darwin":
    from buttereye.core import osapi

#: the two bare leaf names mpv's ``vapoursynth`` filter dlopens, in the order its
#: strings carry them. Only the first appears in the failure message, which is
#: what made the second look unreachable.
MPV_LOOKS_FOR = ("libvapoursynth-script.dylib", "libvsscript.dylib")
#: the name Homebrew's formula actually installs it under (mpv's second guess)
BREW_SHIPS = "libvsscript.dylib"
#: the symbol mpv resolves once the library is open (also in its strings). A path
#: leading to some other dylib would load and not have it.
VSSCRIPT_ENTRY = "getVSScriptAPI"
#: mpv's own words when the dlopen fails. Nothing in the script has run at that
#: point, so it is the one failure that means "the library was not found".
MPV_FAILURE = "Failed to load VapourSynth VSScript library"

#: every variable the fix could plausibly use. Cleared before each test, so the
#: "without the fix" half is really without it however the user's shell is set
#: up, and so the diff against the environment afterwards is only what the call
#: itself added.
RUNTIME_VARS = (
    "DYLD_LIBRARY_PATH",
    "DYLD_FALLBACK_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "VSSCRIPT_PATH",
)

#: one second of mpv's own generated video. ``av://lavfi:`` decodes through the
#: ffmpeg libraries mpv already links, so no clip has to be encoded first and no
#: ``ffmpeg`` binary is needed - the question here is the library, not the codec.
MPV_SOURCE = "av://lavfi:testsrc=size=320x240:rate=24:duration=1"

needs_mpv = pytest.mark.skipif(shutil.which("mpv") is None, reason="needs mpv on PATH")


# ---------------------------------------------------------------- helpers
@pytest.fixture
def clean_env() -> Iterator[dict[str, str]]:
    """``os.environ`` with every variable the fix might set removed; the whole
    environment is put back afterwards.

    ``use_bundled_runtime`` writes into ``os.environ`` itself - that is how the
    mpv and vspipe it starts inherit the setting - and ``monkeypatch`` cannot
    undo a change it did not make, so the snapshot and the restore are done here.
    The yielded dict is that cleared baseline, which the tests both hand to mpv
    and diff against.
    """
    before = dict(os.environ)
    for name in RUNTIME_VARS:
        os.environ.pop(name, None)
    try:
        yield dict(os.environ)
    finally:
        os.environ.clear()
        os.environ.update(before)


def _added(base: Mapping[str, str]) -> dict[str, str]:
    """What ``os.environ`` now has that ``base`` did not, by value.

    ``PYTEST_`` keys are pytest's own bookkeeping, which it rewrites as the run
    moves from test to test: counting them as changes would put noise in every
    assertion message and make the idempotency check depend on when it looked.
    """
    return {
        k: v for k, v in os.environ.items() if base.get(k) != v and not k.startswith("PYTEST_")
    }


def _dyld_dirs(added: Mapping[str, str]) -> list[Path]:
    """Every directory named by a ``DYLD_*_PATH`` the call set. Which of those
    variables it chose is the implementation's business; that each one leads to
    the library is not."""
    dirs: list[Path] = []
    for name, value in sorted(added.items()):
        if name.startswith("DYLD_") and name.endswith("_PATH"):
            dirs += [Path(p) for p in value.split(os.pathsep) if p]
    return dirs


def _reachable(added: Mapping[str, str]) -> list[Path]:
    """Every path the new environment actually leads mpv to: the file
    ``VSSCRIPT_PATH`` names, and the entries on the DYLD directories under either
    name mpv dlopens. ``lexists``: a dangling symlink is one of the failures this
    file exists to catch and must not read as absent."""
    found: list[Path] = []
    exact = added.get("VSSCRIPT_PATH")
    if exact:
        found.append(Path(exact))
    for folder in _dyld_dirs(added):
        found += [folder / n for n in MPV_LOOKS_FOR if os.path.lexists(folder / n)]
    return found


def _vpy(tmp_path: Path, marker: Path) -> Path:
    """A passthrough script that records what mpv handed it.

    ``video_in`` is injected by mpv's ``vapoursynth`` filter and exists nowhere
    else, so a script that reads it cannot have been run by anything but that
    filter, and the marker it writes names the clip's real size: a stub that
    pretended to be VSScript would have to know mpv's frame geometry to pass.
    """
    script = tmp_path / "passthrough.vpy"
    script.write_text(
        "\n".join(
            [
                "import vapoursynth as vs  # noqa: F401 - every .vpy opens with it",
                "clip = video_in  # noqa: F821 - mpv's vapoursynth filter injects it",
                f"marker = {str(marker)!r}",
                "with open(marker, 'w') as fh:",
                "    fh.write('%d %dx%d' % (clip.num_frames, clip.width, clip.height))",
                "clip.set_output()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return script


def _mpv(script: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """Play ``MPV_SOURCE`` through ``script`` under exactly ``env``.

    ``--no-config`` keeps the user's ``mpv.conf`` out of it, ``--vo=null
    --ao=null`` keeps a window and the speakers out of it, and ``--frames=5``
    makes a run that gets as far as decoding stop in well under a second.
    """
    return subprocess.run(
        [
            "mpv", "--no-config", "--vo=null", "--ao=null", "--frames=5",
            "--msg-level=all=v", f"--vf=vapoursynth={script}", MPV_SOURCE,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=dict(env),
    )  # fmt: skip


def _dyld_only(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` without ``VSSCRIPT_PATH``, so only the library search is left.

    The two halves of the fix overlap on purpose, and that hides each other: as
    long as ``VSSCRIPT_PATH`` resolves, mpv never reaches the search path, so a
    broken search path is invisible. Taking the variable away is what an mpv
    build that does not read it looks like, and it is the only way to ask whether
    the other half works at all.
    """
    return {k: v for k, v in env.items() if k != "VSSCRIPT_PATH"}


def _plant(prefix: Path, relative: str) -> Path:
    """A file at ``prefix/relative``, parents made. Stands in for a keg."""
    path = prefix / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def _loads_vsscript(path: Path) -> bool:
    """Does ``path`` open as a library exporting what mpv looks up in VSScript?
    The same two steps mpv takes, so a path leading to the wrong dylib fails."""
    return hasattr(ctypes.CDLL(os.fspath(path)), VSSCRIPT_ENTRY)


# ---------------------------------------------------------------- vsscript_library
def test_vsscript_library_finds_the_file_homebrew_really_ships() -> None:
    """On this machine the answer is a file that exists, is called what Homebrew
    calls it, and is a loadable VSScript - not a path that merely looks right.

    The ``ctypes`` load is the same question mpv asks: open the file and resolve
    ``getVSScriptAPI``. A glob that happened to match some other dylib, or a
    path assembled from a version that is no longer installed, fails here.
    """
    lib = osapi.vsscript_library()
    assert lib is not None, "BUTTEREYE_TEST_BREW says Homebrew's VapourSynth is installed here"
    assert lib.is_file(), f"{lib} is not a file"
    assert lib.name == BREW_SHIPS
    assert _loads_vsscript(lib), f"{lib} exports no {VSSCRIPT_ENTRY}"


def test_vsscript_library_finds_a_keg_whose_version_it_cannot_know(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Cellar path carries both the formula's version and Python's minor
    version, and a ``brew upgrade`` changes either. ``HOMEBREW_PREFIX`` is how a
    Mac says where Homebrew is (``types.homebrew_prefix``), so a planted tree
    under a prefix of our own is a VapourSynth from a future neither this code
    nor this test knows the numbers of. An implementation that spelled out
    today's 81 and python3.14 passes the test above and fails this one.
    """
    prefix = tmp_path / "prefix"
    keg = prefix / "Cellar" / "vapoursynth" / "99"
    planted = _plant(
        keg, f"libexec/lib/python3.99/site-packages/vapoursynth/{BREW_SHIPS}"
    )
    # a finished install leaves its receipt; planted too, so the keg is a keg
    # however the code recognises one
    (keg / "INSTALL_RECEIPT.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    assert osapi.vsscript_library() == planted


def test_vsscript_library_finds_the_copy_a_tap_puts_in_lib(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``<prefix>/lib`` as well as the keg: that is where a from-source
    ``make install`` leaves VSScript and where the Homebrew tap can put a copy of
    its own, and on an Intel Mac - prefix ``/usr/local`` - it is a directory dyld
    already searches, so the layout needs no variable at all.

    The runtime fix and the tap have to be two answers to one question: whatever
    the formula installs, ``vsscript_library`` is what the doctor reports and
    what mpv is pointed at, so a layout only one of them knows about would have
    the doctor describing a file ButterEye does not use.
    """
    prefix = tmp_path / "prefix"
    planted = _plant(prefix, f"lib/{BREW_SHIPS}")
    monkeypatch.setenv("HOMEBREW_PREFIX", str(prefix))
    assert osapi.vsscript_library() == planted


def test_vsscript_library_is_none_when_nothing_is_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulated rather than uninstalled: an empty prefix is a Mac with Homebrew
    and no VapourSynth, which is what GitHub's runner is. None, and no
    exception - the doctor asks this question while reporting a problem."""
    empty = tmp_path / "prefix"
    (empty / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOMEBREW_PREFIX", str(empty))
    assert osapi.vsscript_library() is None


def test_vsscript_library_is_homebrews_and_not_something_on_sys_path() -> None:
    """Where the answer on this machine comes from, which is the part the planted
    prefixes above cannot say.

    The search asks ``sys.path`` first and the Cellar second, and inside a plain
    ``python -m venv`` - which is what this repository's test environment is, and
    what ``buttereye/core/osapi/_darwin.py`` says it measured - the ``vapoursynth``
    package is not importable and not on the path at all, so the keg glob is the
    only route left and the answer proves the glob works against a real Cellar
    rather than a planted one. Hence both halves: the file is under Homebrew's
    prefix, and no ``sys.path`` entry held one for it to have found more easily.

    If this fires with a path on ``sys.path``, the environment has a VapourSynth
    of its own (a ``pip install vapoursynth``, or a venv made with
    ``--system-site-packages``). That is a legitimate machine and the search
    prefers it on purpose - but then this test is no longer about the keg glob,
    and the glob is what every Homebrew user depends on.
    """
    from buttereye.core.types import homebrew_prefix

    lib = osapi.vsscript_library()
    assert lib is not None, "BUTTEREYE_TEST_BREW says Homebrew's VapourSynth is installed here"
    prefix = homebrew_prefix()
    assert lib.is_relative_to(prefix), f"{lib} is not under Homebrew's prefix {prefix}"
    assert _loads_vsscript(lib), f"{lib} exports no {VSSCRIPT_ENTRY}"
    on_path = [
        folder / name
        for entry in sys.path
        if entry
        for folder in [Path(entry) / "vapoursynth"]
        for name in MPV_LOOKS_FOR
        if (folder / name).is_file()
    ]
    assert not on_path, f"{on_path} answers before the keg, so this says nothing about the glob"


def test_a_plant_in_the_working_directory_loses_to_the_library_homebrew_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The attack on a Mac that has the real thing. What ``vsscript_library``
    answers becomes ``VSSCRIPT_PATH`` and mpv ``dlopen``s it, and the search
    reads ``sys.path``, whose first entry under ``python -m`` - how ButterEye
    runs from a checkout - is the working directory. Measured before the fix in
    ``_darwin.py``: started in a directory holding
    ``./vapoursynth/libvsscript.dylib``, the answer was that planted file.

    Here the assertion is the *identity* of the answer rather than the absence of
    the plant, which is what having a real VapourSynth buys: a search that had
    simply stopped working would satisfy "not the plant" and fail this. The
    plant's directory is checked trustworthy first, so the only thing left
    between it and mpv is the working-directory skip. The Homebrew-free halves of
    this - and the owner and mode rules - are in
    tests/core/test_windows_runtime_pure.py, which runs on a Mac with no bottles.
    """
    from buttereye.core.osapi import _darwin as d

    real = osapi.vsscript_library()
    assert real is not None
    work = tmp_path.resolve() / "work"
    planted = _plant(work, f"vapoursynth/{BREW_SHIPS}")
    assert d._trustworthy_dir(planted.parent), "nothing but the cwd skip may refuse this plant"

    monkeypatch.chdir(work)
    monkeypatch.syspath_prepend(os.getcwd())  # the entry ``python -m`` would add

    assert osapi.vsscript_library() == real, f"{planted} would be handed to mpv and dlopened"


def test_a_plant_in_a_directory_the_group_can_write_loses_to_homebrews(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same attack from a directory that is not the working one: on
    ``sys.path``, which the search prefers over Homebrew, and writable by group -
    so any other process on this machine can replace what is in it, and what is
    in it is ``dlopen``ed inside the player.

    The control is the second half, and it is why the first half means anything:
    with the mode tightened and nothing else changed, the same plant *is*
    returned, which proves the search really does prefer ``sys.path`` to the keg
    and that the refusal above was the mode and nothing else. It runs after the
    refusal rather than before so that both halves are asking about the same
    directory, with one ``chmod`` between them; the mode is left tightened
    because the plant is inside ``tmp_path`` and goes with it.
    """
    real = osapi.vsscript_library()
    assert real is not None
    loose = tmp_path.resolve() / "loose"
    planted = _plant(loose, f"vapoursynth/{BREW_SHIPS}")
    planted.parent.chmod(0o775)
    monkeypatch.syspath_prepend(os.fspath(loose))
    assert Path.cwd() != loose, "the working-directory skip would be what refuses this"

    assert osapi.vsscript_library() == real, f"{planted} would be handed to mpv and dlopened"

    planted.parent.chmod(0o750)  # the same plant, in a directory now ours alone
    assert osapi.vsscript_library() == planted, (
        "the same directory is not preferred to the keg even when its mode is ours alone, "
        "so the refusal above was not the mode"
    )


# ---------------------------------------------------------------- the fix itself
@needs_mpv
def test_mpv_runs_a_vapoursynth_script_with_the_fix_and_cannot_without_it(
    clean_env: dict[str, str], tmp_path: Path
) -> None:
    """The whole phase in one test, both halves, same mpv and same script.

    Without the fix mpv must fail on the library and the script must not have
    run. With the environment ``use_bundled_runtime`` builds, mpv must play and
    the script must have written the marker. Only the pair is evidence: the
    first half alone is a broken Mac, the second alone could be a Mac where
    something else had already set a variable, which is why ``clean_env``
    strips every candidate first.
    """
    marker = tmp_path / "ran"
    script = _vpy(tmp_path, marker)

    without = _mpv(script, clean_env)
    both = without.stderr + without.stdout
    assert MPV_FAILURE in both, f"the blocker is gone - mpv found VSScript unaided:\n{both[-2000:]}"
    assert without.returncode != 0
    assert not marker.exists(), "the script ran without the fix, so this proves nothing"

    osapi.use_bundled_runtime()
    added = _added(clean_env)
    assert added, "use_bundled_runtime changed nothing, so mpv is no better off"

    with_fix = _mpv(script, os.environ)
    both = with_fix.stderr + with_fix.stdout
    assert MPV_FAILURE not in both, f"added {added}, and mpv still cannot load it:\n{both[-2000:]}"
    assert with_fix.returncode == 0, both[-2000:]
    assert marker.exists(), f"mpv did not run the script:\n{both[-2000:]}"
    frames, size = marker.read_text(encoding="utf-8").split()
    assert size == "320x240", f"the script was handed {size}, not mpv's clip"
    assert int(frames) > 0


def test_every_path_the_fix_names_is_a_loadable_vsscript(clean_env: dict[str, str]) -> None:
    """What the variables point at, rather than that they are set.

    An empty directory, a path to the wrong dylib and a symlink whose target has
    gone all leave the environment looking identical from outside. So every path
    the call put there is resolved, compared against ``vsscript_library``, and
    opened the way mpv opens it - and every directory it put on the child's
    library search has to hold one of the two names mpv asks for, which is what
    an implementation that set the variable to a directory with nothing in it
    would fail.
    """
    lib = osapi.vsscript_library()
    assert lib is not None
    osapi.use_bundled_runtime()
    added = _added(clean_env)
    assert added, "use_bundled_runtime changed nothing"

    reached = _reachable(added)
    assert reached, (
        f"nothing it set leads to VSScript: no VSSCRIPT_PATH and none of {MPV_LOOKS_FOR} "
        f"on {_dyld_dirs(added)} (it added {added})"
    )
    for path in reached:
        target = path.resolve()
        assert target.is_file(), f"{path} leads to {target}, which is not a file"
        assert target == lib.resolve(), f"{path} leads to {target}, not to {lib}"
        assert _loads_vsscript(path), f"{path} opens but exports no {VSSCRIPT_ENTRY}"

    for folder in _dyld_dirs(added):
        names = [n for n in MPV_LOOKS_FOR if os.path.lexists(folder / n)]
        assert names, f"{folder} is on the child's library search and holds no VSScript"


@needs_mpv
def test_nothing_it_leaves_on_disk_can_go_stale_unnoticed(
    clean_env: dict[str, str], tmp_path: Path
) -> None:
    """A ``brew upgrade vapoursynth`` changes the version in the Cellar path, so
    any file ButterEye wrote naming today's keg is wrong tomorrow.

    Two shapes are acceptable and the test takes whichever it finds. Either the
    fix writes nothing - every directory it names is the library's own, derived
    fresh on each call, and there is no state to go stale - or it manages a
    symlink of its own, and then a target an upgrade has taken away has to be
    replaced rather than trusted. What is *not* acceptable is a managed file left
    dangling, because dyld reports a dangling symlink as "no such file" and mpv
    then fails exactly as it does with no fix at all: a stale pointer is not a
    weaker fix, it is no fix wearing the shape of one, and nothing in the
    environment distinguishes the two from outside.

    Every mpv here runs without ``VSSCRIPT_PATH`` (``_dyld_only``). With it the
    library search is never reached and this test would pass over a search path
    pointing anywhere at all - which is exactly the mistake it exists to catch.

    Restored in a ``finally``: the integration tests and the user's own next run
    read the same directory, and a failure here must not leave it poisoned.
    """
    lib = osapi.vsscript_library()
    assert lib is not None
    osapi.use_bundled_runtime()
    folders = _dyld_dirs(_added(clean_env))
    assert folders, "nothing was put on the child's library search path"

    managed = [
        folder / name
        for folder in folders
        if folder.resolve() != lib.parent.resolve()
        for name in MPV_LOOKS_FOR
        if (folder / name).is_symlink()
    ]
    for folder in folders:
        assert folder.resolve() == lib.parent.resolve() or any(
            link.parent == folder for link in managed
        ), f"{folder} is on the child's library search and holds nothing ButterEye explains"

    marker = tmp_path / "ran"
    script = _vpy(tmp_path, marker)

    # the search path alone has to work, or the fix has only one half
    first = _mpv(script, _dyld_only(os.environ))
    both = first.stderr + first.stdout
    assert MPV_FAILURE not in both, (
        f"without VSSCRIPT_PATH the library search is all mpv has, and {folders} "
        f"does not get it there:\n{both[-2000:]}"
    )
    assert marker.exists(), both[-2000:]

    if not managed:  # nothing of ButterEye's on disk: nothing can go stale
        return

    try:
        for link in managed:
            link.unlink()
            link.symlink_to(tmp_path / "upgraded-away" / BREW_SHIPS)  # never created
            assert os.path.lexists(link) and not link.exists(), "it is meant to dangle"
        marker.unlink()

        broken = _mpv(script, _dyld_only(os.environ))
        both = broken.stderr + broken.stdout
        assert MPV_FAILURE in both, (
            f"a dangling {managed} turns out to be harmless, so this test proves "
            f"nothing about staleness:\n{both[-2000:]}"
        )
        assert not marker.exists()

        osapi.use_bundled_runtime()  # the next ButterEye.open(), after the upgrade
        for link in managed:
            assert link.resolve() == lib.resolve(), (
                f"{link} was left pointing at {link.resolve()}, not at {lib}"
            )

        fixed = _mpv(script, _dyld_only(os.environ))
        both = fixed.stderr + fixed.stdout
        assert MPV_FAILURE not in both, both[-2000:]
        assert fixed.returncode == 0, both[-2000:]
        assert marker.exists(), f"mpv did not run the script after the repair:\n{both[-2000:]}"
    finally:
        for link in managed:
            if link.is_symlink() and link.resolve() != lib.resolve():
                link.unlink(missing_ok=True)
        osapi.use_bundled_runtime()


@needs_mpv
def test_a_stale_vsscript_path_breaks_mpv_and_is_replaced_rather_than_trusted(
    clean_env: dict[str, str], tmp_path: Path
) -> None:
    """The staleness that can still happen, and it is fatal rather than inert.

    ``VSSCRIPT_PATH`` names one file and can come from the user's shell, and the
    Cellar path it would name has VapourSynth's version in it, so a value
    exported before a ``brew upgrade`` points at nothing afterwards. Measured
    here: mpv does not fall back past a ``VSSCRIPT_PATH`` that does not resolve.
    It tries that one path, fails, and removes the filter - with
    ``DYLD_FALLBACK_LIBRARY_PATH`` correctly set alongside it, which on its own
    would have worked. So ButterEye must replace such a value rather than defer
    to it, and the test proves the harm first and the repair second: without the
    mpv run in the middle, "it was replaced" is a claim about a string.
    """
    lib = osapi.vsscript_library()
    assert lib is not None
    marker = tmp_path / "ran"
    script = _vpy(tmp_path, marker)

    osapi.use_bundled_runtime()  # the whole fix, in working order
    stale = tmp_path / "vapoursynth-80" / BREW_SHIPS  # never created
    os.environ["VSSCRIPT_PATH"] = os.fspath(stale)

    broken = _mpv(script, os.environ)
    both = broken.stderr + broken.stdout
    assert MPV_FAILURE in both, f"a stale VSSCRIPT_PATH turns out to be harmless:\n{both[-2000:]}"
    assert not marker.exists()

    osapi.use_bundled_runtime()  # the next ButterEye.open(), after the upgrade
    now = os.environ.get("VSSCRIPT_PATH")
    assert now != os.fspath(stale), "the stale value was left for mpv to choke on"
    if now is not None:  # removing it is as good as replacing it
        assert Path(now).resolve() == lib.resolve(), f"VSSCRIPT_PATH is {now}, not {lib}"

    fixed = _mpv(script, os.environ)
    both = fixed.stderr + fixed.stdout
    assert MPV_FAILURE not in both, both[-2000:]
    assert fixed.returncode == 0, both[-2000:]
    assert marker.exists(), f"mpv did not run the script after the repair:\n{both[-2000:]}"


def test_calling_it_twice_does_not_repeat_the_directory(clean_env: dict[str, str]) -> None:
    """``ButterEye.open()`` can be called more than once in a process (the GUI
    reopens the core), and a DYLD path that grew an entry each time would end up
    a kilobyte of the same directory."""
    osapi.use_bundled_runtime()
    first = _added(clean_env)
    osapi.use_bundled_runtime()
    assert _added(clean_env) == first, "the second call changed the environment again"
    for name, value in first.items():
        if name.startswith("DYLD_"):
            entries = [p for p in value.split(os.pathsep) if p]
            assert len(entries) == len(set(entries)), f"{name} repeats a directory: {value}"


# ---------------------------------------------------------------- the quiet cases
def test_it_does_nothing_at_all_when_vapoursynth_is_absent(
    clean_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ButterEye.open()`` calls this before anything else, on every Mac, so on
    one with no VapourSynth it must neither raise nor leave a variable behind.

    Naming a directory with nothing in it would be the worst of both: it reads
    as a fix to anything that only checks whether the variable is set, and every
    entry on a child's library search path is paid for by every lookup that
    child makes.
    """
    empty = tmp_path / "prefix"
    (empty / "bin").mkdir(parents=True)
    monkeypatch.setenv("HOMEBREW_PREFIX", str(empty))
    before = dict(os.environ)

    osapi.use_bundled_runtime()  # must not raise: ButterEye.open() has nowhere to put it

    added = _added(before)
    assert added == {}, f"with no library to point at it still set {added}"


def test_it_does_not_raise_when_the_prefix_cannot_be_read(
    clean_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The filesystem is the one thing in this call that can fail, and
    ``ButterEye.open()`` is the caller, so a prefix this user cannot read has to
    answer None rather than raise - a Homebrew someone installed as another user
    is a real machine, not a hypothetical one."""
    if os.getuid() == 0:
        pytest.skip("root can read it anyway")
    locked = tmp_path / "prefix"
    (locked / "Cellar").mkdir(parents=True)
    locked.chmod(0o000)
    monkeypatch.setenv("HOMEBREW_PREFIX", str(locked))
    try:
        assert osapi.vsscript_library() is None
        before = dict(os.environ)
        osapi.use_bundled_runtime()
        assert _added(before) == {}
    finally:
        locked.chmod(0o700)  # else tmp_path cannot be cleaned up


def test_it_keeps_a_dyld_path_the_user_already_set(
    clean_env: dict[str, str], tmp_path: Path
) -> None:
    """Someone with a hand-built library of their own on a DYLD path keeps it:
    these variables are the user's, and ButterEye is adding one directory to
    them, not claiming them."""
    mine = tmp_path / "my-libs"
    mine.mkdir()
    for name in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        os.environ[name] = os.fspath(mine)

    osapi.use_bundled_runtime()

    for name in ("DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH"):
        entries = os.environ[name].split(os.pathsep)
        assert os.fspath(mine) in entries, f"{name} lost the user's directory: {os.environ[name]}"
