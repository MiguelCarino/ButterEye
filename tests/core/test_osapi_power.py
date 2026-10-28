# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``osapi``'s power and heat seam, with each system's answers mocked.

Runs everywhere. On macOS the ``_darwin`` calls are replaced; the Linux readers
get a fake ``/sys`` tree in ``tmp_path``; the Windows decoding gets made-up
``GetSystemPowerStatus`` fields, and the facade's win32 branch a stub ``_win32``.
What is checked is how the facade puts the answers together, not what this
machine's battery says (``tests/macos/test_osapi_macos.py`` and
``tests/windows/test_power_windows.py`` ask the real APIs).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from buttereye.core import osapi
from buttereye.core.types import PowerSnapshot

on_macos = pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
on_linux = pytest.mark.skipif(sys.platform != "linux", reason="Linux only")


# ---------------------------------------------------------------- Linux: /sys
def _supply(root: Path, name: str, **files: str) -> None:
    folder = root / "class" / "power_supply" / name
    folder.mkdir(parents=True)
    for key, value in files.items():
        (folder / key).write_text(value + "\n")


def _laptop(root: Path, *, online: str, status: str) -> None:
    _supply(root, "AC", type="Mains", online=online)
    _supply(root, "BAT0", type="Battery", scope="System", status=status)


@pytest.mark.parametrize(
    ("online", "status", "answer"),
    [
        ("0", "Discharging", True),
        ("1", "Charging", False),
        ("1", "Not charging", False),  # held at a charge limit
        ("1", "Discharging", False),  # the wall wins over what the battery says
        ("0", "Full", False),
    ],
)
def test_linux_laptop(tmp_path: Path, online: str, status: str, answer: bool) -> None:
    _laptop(tmp_path, online=online, status=status)
    assert osapi._linux_on_battery(tmp_path) is answer
    assert osapi._linux_has_battery(tmp_path)


def test_linux_usb_c_charger_counts_as_the_wall(tmp_path: Path) -> None:
    _supply(tmp_path, "ucsi-source-psy-USBC000:001", type="USB", scope="Unknown", online="1")
    _supply(tmp_path, "BAT1", type="Battery", status="Discharging")
    assert osapi._linux_on_battery(tmp_path) is False
    (tmp_path / "class/power_supply/ucsi-source-psy-USBC000:001/online").write_text("0\n")
    assert osapi._linux_on_battery(tmp_path) is True


def test_linux_device_batteries_and_ups_are_not_the_machine(tmp_path: Path) -> None:
    # a desktop with a wireless mouse and a UPS: neither is a battery it runs from
    _supply(tmp_path, "hidpp_battery_0", type="Battery", scope="Device", status="Discharging")
    _supply(tmp_path, "ups", type="UPS", status="Discharging")
    assert osapi._linux_on_battery(tmp_path) is None
    assert not osapi._linux_has_battery(tmp_path)


def test_linux_nothing_to_read_is_unknown(tmp_path: Path) -> None:
    assert osapi._linux_on_battery(tmp_path) is None  # no class/power_supply at all
    (tmp_path / "class" / "power_supply").mkdir(parents=True)
    assert osapi._linux_on_battery(tmp_path) is None  # a desktop: an empty folder
    assert not osapi._linux_has_battery(tmp_path)
    _supply(tmp_path, "AC", type="Mains", online="1")
    assert osapi._linux_on_battery(tmp_path) is False  # mains, and no battery to ask
    assert not osapi._linux_has_battery(tmp_path)


def _profile(root: Path, profile: str | None, choices: str) -> None:
    acpi = root / "firmware" / "acpi"
    acpi.mkdir(parents=True)
    if profile is not None:
        (acpi / "platform_profile").write_text(profile + "\n")
    (acpi / "platform_profile_choices").write_text(choices + "\n")


@pytest.mark.parametrize(
    ("profile", "choices", "answer"),
    [
        ("low-power", "low-power balanced performance", True),
        ("balanced", "low-power balanced performance", False),
        ("performance", "low-power balanced performance", False),
        ("quiet", "quiet balanced performance", True),  # the quietest there is
        ("cool", "cool quiet balanced performance", True),
        ("quiet", "low-power quiet balanced performance", False),
        ("custom", "custom balanced", None),
        ("something-new", "something-new balanced", None),
        (None, "low-power balanced", None),
    ],
)
def test_linux_platform_profile(
    tmp_path: Path, profile: str | None, choices: str, answer: bool | None
) -> None:
    _profile(tmp_path, profile, choices)
    assert osapi._linux_low_power(tmp_path) is answer


def test_linux_without_acpi_profiles_is_unknown(tmp_path: Path) -> None:
    assert osapi._linux_low_power(tmp_path) is None


@on_linux
def test_linux_facade_reads_the_sys_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from buttereye.core.hw import detect

    monkeypatch.setattr(detect, "SYS_ROOT", tmp_path)
    assert osapi.power_snapshot() == PowerSnapshot()
    assert not osapi.has_system_battery()
    _laptop(tmp_path, online="0", status="Discharging")
    _profile(tmp_path, "low-power", "low-power balanced")
    snap = osapi.power_snapshot()
    assert snap == PowerSnapshot(on_battery=True, low_power=True, thermal=None)
    assert snap.constrained
    assert osapi.has_system_battery()


# ---------------------------------------------------------------- Windows
@pytest.mark.parametrize(
    ("status", "battery", "saver"),
    [
        ((0, 0, 0), True, False),  # discharging, high charge
        ((1, 8, 0), False, False),  # charging
        ((0, 2, 1), True, True),  # low, Energy Saver on
        ((255, 0, 0), None, False),  # Windows does not know the line
        ((1, 128, 0), None, False),  # a desktop: no system battery
        ((1, 255, 1), None, True),  # battery state unknown; the saver still says
        (None, None, None),  # the call failed
    ],
)
def test_windows_power_status(
    status: tuple[int, int, int] | None, battery: bool | None, saver: bool | None
) -> None:
    assert osapi._win_on_battery(status) is battery
    assert osapi._win_low_power(status) is saver


def _stub_win32(monkeypatch: pytest.MonkeyPatch, **calls: Any) -> None:
    """The facade's win32 branch over a stub ``_win32``; undo before asserting,
    since pytest itself asks ``sys.platform``."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(osapi, "IS_WINDOWS", True)
    monkeypatch.setattr(osapi, "w", types.SimpleNamespace(**calls), raising=False)


def test_windows_facade(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    _stub_win32(
        monkeypatch,
        power_status=lambda: (0, 1, 1),
        begin_activity=lambda r: calls.append(("begin", r)) or 0x1F4,
        end_activity=lambda t: calls.append(("end", t)),
    )
    snap = osapi.power_snapshot()
    has = osapi.has_system_battery()
    awake = osapi.keep_awake(4242)
    with osapi.activity("Saving a smooth copy") as token:
        assert token == 0x1F4
    monkeypatch.undo()
    assert snap == PowerSnapshot(on_battery=True, low_power=True, thermal=None)
    assert has
    assert awake is None  # the power request holds it on Windows
    assert calls == [("begin", "Saving a smooth copy"), ("end", 0x1F4)]


def test_windows_refused_request_ends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[object] = []
    _stub_win32(
        monkeypatch,
        power_status=lambda: (1, 128, 0),
        begin_activity=lambda _r: None,
        end_activity=ended.append,
    )
    with osapi.activity("x") as token:
        refused = token
    has = osapi.has_system_battery()
    on = osapi.on_battery()
    monkeypatch.undo()
    assert refused is None and ended == []
    assert not has and on is None


# ---------------------------------------------------------------- Linux: keep awake
def test_inhibit_argv_waits_on_the_pid() -> None:
    assert osapi.inhibit_argv(4242) == [
        "/usr/bin/systemd-inhibit", "--what=idle:sleep", "--mode=block", "--who=ButterEye",
        "--why=Saving a smooth copy", "--", "/usr/bin/tail", "--pid=4242", "-f", "/dev/null",
    ]  # fmt: skip


class _FakePopen:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    def __init__(self, argv: list[str], **kwargs: Any) -> None:
        self.seen.append((argv, kwargs))

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired("x", timeout or 0)


@on_linux
def test_keep_awake_runs_systemd_inhibit(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePopen.seen = []
    monkeypatch.setattr(os.path, "exists", lambda _p: True)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    proc = osapi.keep_awake(4242)
    assert isinstance(proc, _FakePopen)
    argv, kwargs = _FakePopen.seen[0]
    assert argv == osapi.inhibit_argv(4242)
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL and kwargs["close_fds"] is True


@on_linux
def test_keep_awake_without_systemd_inhibit_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("nothing to start")

    monkeypatch.setattr(os.path, "exists", lambda p: p != osapi.SYSTEMD_INHIBIT)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert osapi.keep_awake(4242) is None


@on_linux
def test_systemd_inhibit_failing_to_start_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError(osapi.SYSTEMD_INHIBIT)

    monkeypatch.setattr(os.path, "exists", lambda _p: True)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert osapi.keep_awake(4242) is None


@on_linux
def test_a_refused_lock_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(1)"])
    with caplog.at_level("WARNING", logger=osapi.__name__):
        osapi._watch_inhibit(proc, 4242)
    assert "may sleep" in caplog.text
    caplog.clear()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    proc.terminate()  # the caller ending it early is not a refusal
    with caplog.at_level("WARNING", logger=osapi.__name__):
        osapi._watch_inhibit(proc, 4242)
    assert caplog.text == ""


def _inhibitors() -> str:
    try:
        return subprocess.run(
            [osapi.SYSTEMD_INHIBIT, "--list", "--no-pager"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout  # fmt: skip
    except OSError, subprocess.TimeoutExpired:
        return ""


def _wait_for(check: Any, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return bool(check())


@on_linux
@pytest.mark.skipif(not Path(osapi.SYSTEMD_INHIBIT).exists(), reason="no systemd-inhibit")
@pytest.mark.skipif(not Path("/run/systemd/seats").exists(), reason="no logind session")
@pytest.mark.skipif(shutil.which("tail") is None, reason="no tail")
def test_keep_awake_live_holds_and_releases_the_lock() -> None:
    """Real ``systemd-inhibit``: the lock shows for a dummy child, ends with it, and
    ``terminate`` releases it early as well, ending ``tail`` too."""
    for early in (False, True):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        lock = osapi.keep_awake(child.pid)
        try:
            assert lock is not None
            assert _wait_for(lambda: osapi.INHIBIT_WHY in _inhibitors()), "lock not held"
            if early:
                lock.terminate()
            else:
                child.terminate()
                child.wait()  # tail --pid sees a zombie as alive; the job's loop reaps
            assert lock.wait(timeout=10) is not None
            assert _wait_for(lambda: osapi.INHIBIT_WHY not in _inhibitors())
        finally:
            child.kill()
            child.wait()
            if lock is not None and lock.poll() is None:
                lock.kill()
                lock.wait()


@pytest.mark.parametrize(
    ("snap", "constrained"),
    [
        (PowerSnapshot(), False),
        (PowerSnapshot(thermal=1), False),
        (PowerSnapshot(thermal=2), True),
        (PowerSnapshot(thermal=3), True),
        (PowerSnapshot(low_power=True, thermal=0), True),
        (PowerSnapshot(on_battery=True), False),  # battery alone is not throttling
    ],
)
def test_constrained(snap: PowerSnapshot, constrained: bool) -> None:
    assert snap.constrained is constrained


@on_macos
def test_keep_awake_runs_caffeinate_for_the_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[list[str], dict[str, Any]]] = []

    class FakePopen:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            seen.append((argv, kwargs))

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    proc = osapi.keep_awake(4242)
    assert isinstance(proc, FakePopen)
    argv, kwargs = seen[0]
    assert argv == ["/usr/bin/caffeinate", "-i", "-w", "4242"]
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["close_fds"] is True


@on_macos
def test_keep_awake_failing_to_start_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("/usr/bin/caffeinate")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert osapi.keep_awake(4242) is None


@on_macos
def test_activity_ends_what_it_began(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(osapi.d, "begin_activity", lambda r: calls.append(("begin", r)) or 77)
    monkeypatch.setattr(osapi.d, "end_activity", lambda t: calls.append(("end", t)))
    with pytest.raises(RuntimeError), osapi.activity("Rendering film.mkv") as token:
        assert token == 77
        raise RuntimeError("the render failed")
    assert calls == [("begin", "Rendering film.mkv"), ("end", 77)]


@on_macos
def test_refused_activity_ends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ended: list[object] = []
    monkeypatch.setattr(osapi.d, "begin_activity", lambda _r: None)
    monkeypatch.setattr(osapi.d, "end_activity", ended.append)
    with osapi.activity("x") as token:
        assert token is None
    assert ended == []
