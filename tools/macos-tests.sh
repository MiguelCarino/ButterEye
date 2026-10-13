#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# Run the macOS gate on a Mac: the three-platform type check, then the test
# suite (docs/PLATFORMS.md, "macOS: the realistic path").
#
# tools/wine-tests.sh exists because the Windows tests have to run on the Linux
# dev box, so it carries a Wine prefix, a Windows Python and a drive-letter
# translation. This script needs none of that: the machine is the target, and
# there is nothing to emulate. What it is for is that the gate is two commands
# with a measured exclusion list, and a contributor should not have to copy that
# list out of a workflow file by hand.
#
# .github/workflows/macos.yml runs the same two commands with the same
# exclusions. Keep the list below and the one in that file in step; neither can
# call the other (CI installs its own pinned tools with setup-python, and wants
# the type check as its own step so all three platforms report).
#
# Unlike CI, this exits non-zero only at the end: a failing type check still
# lets the tests run, so one command tells you everything that is wrong.
#
# "the macOS gate is green" is printed only when the run really was the whole
# gate; pass a path or a -k and you get the results and an explicit note that
# this was not the gate. See the counting just above the pytest call.
#
# Two tests can fail here for reasons that are nobody's port and are not excluded
# (macos.yml explains both): tests/core/test_ops_shutdown.py's
# test_kill_owned_groups_is_scoped_to_loop times out under full-suite load about
# one run in three, and a tests/gui run can rarely abort outright in shiboken6.
#
# One thing to expect that is wider than CI: with mpv and vspipe installed, the
# tests marked `integration` run instead of skipping, and
# tests/integration/test_hdr_live_real.py is red on a Mac today. Homebrew's mpv
# cannot load VSScript (its `vapoursynth` filter dlopens the library by bare leaf
# name and the formula keeps it inside its libexec virtualenv), so the filter
# never creates and the session rolls back with BE-3003. That is a packaging
# problem for a later phase, not a fixture problem, and it means the gate is
# green with the Homebrew bottles absent and red with them present.
#
# This script sets BUTTEREYE_TEST_BREW, which is what opts
# tests/macos/test_doctor_macos.py in: that file runs the whole doctor against
# this machine's real Homebrew, vulkaninfo and mpv, so it says "the bottles are
# here" rather than assuming it. .github/workflows/macos.yml installs no bottle
# and so never sets it, and those tests skip there by design. A developer's Mac
# is the machine they want, which is why they are asked for here.
#
# BUTTEREYE_TEST_APP=/path/to/ButterEye.app (not set here: there may be no app
# built) opts tests/macos/test_app_bundle.py in, which measures a built app from
# the outside - packaging/macos/build-app.sh leaves one in
# ~/.cache/buttereye/macos-app/stage. Without it those six tests skip.
#
# ASSUMED INSTALLED: Homebrew, and a Python 3.14 (brew install python@3.14).
# The test tools go in a venv this script makes and then reuses:
#
#   BUTTEREYE_MACOS_VENV=~/.cache/buttereye-macos-venv   # override to move it
#
# It installs no Homebrew package itself. tests/core and tests/boundary need
# none; the tests marked `integration` need mpv and vspipe on PATH, and
# tests/conftest.py skips them for the missing binary rather than failing, so
# the gate is green without them and wider with them:
#
#   brew install mpv vapoursynth molten-vk mkvtoolnix ffms2 vapoursynth-mvtools
#
# mpv for the `vapoursynth` filter (the formula's 0.41.0 has it; the mpv.io app
# bundle does not), vapoursynth for vspipe, vapoursynth-mvtools and ffms2 for
# the plugins the scripts load, mkvtoolnix for the render pipeline's mkvmerge,
# molten-vk because mpv's formula requires it and because it is the only Vulkan
# there is here. arm64 is the only architecture this was measured on.
#
# Usage: tools/macos-tests.sh [pytest args...]
set -uo pipefail

if [[ $(uname -s) != Darwin ]]; then
    echo "tools/macos-tests.sh is for macOS; on Linux run pytest directly" >&2
    exit 2
fi

repo=$(cd "$(dirname "$0")/.." && pwd)
venv=${BUTTEREYE_MACOS_VENV:-$HOME/.cache/buttereye-macos-venv}
python=$venv/bin/python

# the versions the port was measured with, as in macos.yml. PySide6-Essentials
# because tests/boundary imports Qt to check what the core may not and tests/gui
# drives the real widgets; pytest-qt because tests/gui needs its qtbot.
tools=(pytest==9.1.1 pytest-asyncio==1.4.0 pytest-qt==4.5.0 mypy==2.4.0
       PySide6-Essentials==6.12.0)

if [[ ! -x $python ]]; then
    base=$(command -v python3.14 || command -v python3) || {
        echo "no python3 found; brew install python@3.14" >&2
        exit 2
    }
    echo "--- making the test venv in $venv ($base)"
    "$base" -m venv "$venv" || exit 2
fi
# -c for every module at once: a venv made by an earlier run with a different
# list is topped up rather than rebuilt
if ! "$python" -c 'import pytest, pytest_asyncio, pytestqt, mypy, PySide6' 2>/dev/null; then
    echo "--- installing the test tools"
    "$python" -m pip install --disable-pip-version-check -q --only-binary=:all: \
        "${tools[@]}" || exit 2
fi
"$python" -VV

cd "$repo" || exit 2

# A Mac has a window server, unlike the CI runner, so tests/gui could use the
# real cocoa plugin here - but then every widget test raises a window and takes
# the focus off whatever you were doing. Offscreen is the default for that
# reason and because it is what CI measures; set it yourself to override.
export QT_QPA_PLATFORM=${QT_QPA_PLATFORM:-offscreen}
export QT_ACCESSIBILITY=${QT_ACCESSIBILITY:-1}

# The opt-in gate for tests/macos/test_doctor_macos.py, in the shape
# tests/windows uses (BUTTEREYE_TEST_PLUGINS and its neighbours). No colon in the
# default, so BUTTEREYE_TEST_BREW= opts out: the doctor against a real Homebrew
# is the point of running on a Mac, but a machine part-way through the brew
# install line above can turn it off rather than read three failures.
export BUTTEREYE_TEST_BREW=${BUTTEREYE_TEST_BREW-1}

# all three platforms run even when one fails, so a broken platform is never
# hidden behind the one before it. This is the part that polices the port from
# any machine: it fails the moment a macOS fix breaks Linux or Windows.
typing_failed=
for platform in linux win32 darwin; do
    echo "--- mypy --platform $platform"
    "$python" -m mypy --platform "$platform" buttereye || typing_failed="$typing_failed $platform"
done

# No test path, so this is pyproject's testpaths (["tests"]): what `pytest` with
# no arguments runs for a contributor on a Mac. The exclusions are the measured
# list macos.yml explains case by case; the short version:
#   * the seven Alt+letter mnemonic cases - a Mac's menus answer to Cmd (GUI phase)
#   * two tests whose own helpers read /proc for a process's state (not osapi's,
#     which phase 1 fixed; these are tests/test_live_review_fixes.py's own `_state`
#     and tools/memguard.py's `descendants`)
#   * one tests/gui cancel race, measured 4 passes in 10 here
# tests/test_bench_runner.py::test_cancel_kills_the_process_group was on this list
# and is not any more: phase 1 gave that file's `_alive` a macOS branch.
deselects=(
    --deselect tests/gui/test_mnemonics.py::test_alt_menu_letters_open_the_menu
    --deselect tests/test_live_review_fixes.py::test_spawned_process_survives_gc_and_is_reaped
    --deselect tests/test_memguard.py::test_descendants_and_kill_reach_grandchildren
    --deselect tests/gui/test_bench_page.py::test_cancel_stops_the_run_and_keeps_old_results
)

# Whether this run may call itself the gate. "$@" lands after the deselects so a
# contributor can add -x or -k, but a path or a -k also NARROWS the run, and an
# earlier version of this script printed "the macOS gate is green" for any of
# them - `tools/macos-tests.sh tests/macos` ran 27 of 1771 tests and claimed the
# gate. So ask pytest to count what it would select with the arguments and
# without them, and keep the gate claim for the runs where those agree. Counting
# rather than testing for "$# -eq 0" is what keeps -x and -q eligible while a
# path or a -k is not; collection is one line of output and under a second.
selected() {
    "$python" -B -m pytest -q -p no:cacheprovider --collect-only \
        "${deselects[@]}" "$@" 2>/dev/null |
        sed -n 's/^\([0-9]*\)\(\/[0-9]*\)\{0,1\} tests\{0,1\} collected.*/\1/p' | tail -1
}
whole=$(selected)
this=$(selected "$@")
# An empty count means collection itself failed or its summary line moved; either
# way this run cannot claim the gate, and the pytest below will say why.
if [[ -n $whole && -n $this && $whole == "$this" ]]; then
    narrowed=
else
    narrowed="selected $this of the gate's $whole tests"
fi

echo "--- pytest"
"$python" -B -m pytest -v -rs "${deselects[@]}" "$@"
tests_status=$?

if [[ -n $typing_failed ]]; then
    echo "mypy failed for:$typing_failed" >&2
fi
[[ -z $typing_failed && $tests_status -eq 0 ]] || exit 1
if [[ -n $narrowed ]]; then
    echo "those tests passed, but this run was not the gate: $narrowed"
    echo "run tools/macos-tests.sh with no test path for the gate"
    exit 0
fi
echo "the macOS gate is green"
