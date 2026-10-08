# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""U3: NVIDIA Xid parsing from journalctl -k (F1, M0(f)); unreadable is never OK."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from buttereye.core.doctor import gpufault
from buttereye.core.doctor.gpufault import (
    XidEvent,
    XidScan,
    filter_events,
    parse_journal_json,
    parse_xid,
    scan_xid,
    xid_finding,
)
from buttereye.core.errors import ErrorCode
from buttereye.core.types import Severity

# m0f.md, spike-era driver format
M0F = (
    "NVRM: Xid 13, Graphics Exception: SKEDCHECK36_DEPENDENCE_COUNTER_UNDERFLOW failed",
    "NVRM: Xid 13, pid=48211, name=vo, Graphics Exception: channel 0x00000049, "
    "Class 0000c9c0 (compute)",
)
# 615.71.09 format, recorded on the dev box
R615 = (
    "NVRM: Xid (PCI:0000:08:00): 13, Graphics Exception: "
    "SKEDCHECK36_DEPENDENCE_COUNTER_UNDERFLOW failed",
    "NVRM: Xid (PCI:0000:08:00): 13, Graphics Exception: ESR 0x407020=0x100 0x407028=0x0",
    "NVRM: Xid (PCI:0000:08:00): 13, pid=152333, name=vspipe, Graphics Exception: channel "
    "0x0000001e, Class 0000c9c0, Offset 00000000, Data 00000000",
)
BOOT = "9ad7ff4c02ee4d868393958985948ca7"


def rec(msg: str | list[int], mono_us: int, wall_us: int, boot: str = BOOT) -> str:
    return json.dumps(
        {
            "MESSAGE": msg,
            "__MONOTONIC_TIMESTAMP": str(mono_us),
            "__REALTIME_TIMESTAMP": str(wall_us),
            "_BOOT_ID": boot,
            "_TRANSPORT": "kernel",
        }
    )


def test_parse_old_and_new_formats() -> None:
    a = parse_xid(M0F[0])
    assert a is not None and a.xid == 13 and a.pid is None and a.pci is None
    b = parse_xid(M0F[1])
    assert b is not None and (b.pid, b.process) == (48211, "vo")
    c = parse_xid(R615[2])
    assert c is not None
    assert (c.xid, c.pci, c.pid, c.process) == (13, "PCI:0000:08:00", 152333, "vspipe")
    d = parse_xid(
        "NVRM: Xid (PCI:0000:01:00): 79, pid='<unknown>', name=<unknown>, "
        "GPU has fallen off the bus."
    )
    assert d is not None and d.xid == 79 and d.pid is None
    assert parse_xid("nvidia-modeset: Allocated GPU:0") is None
    assert parse_xid("NVRM: loading NVIDIA UNIX Open Kernel Module") is None


def _journal() -> str:
    t = 3_800_000_000
    w = 1_791_365_900_000_000
    lines = [
        rec("usb 1-1: new device", t, w),
        # fault 1 (m0f format, pid 48211), three lines within 1 ms
        rec(M0F[0], t + 1000, w + 1000),
        rec(M0F[1], t + 2000, w + 2000),
        # fault 2 (615 format, pid 152333) 20 s later
        *(rec(m, t + 20_000_000 + i, w + 20_000_000 + i) for i, m in enumerate(R615)),
        # same fault text but a different boot: ignored when boot filtering applies
        rec(R615[2], t + 30_000_000, w + 30_000_000, boot="other"),
        # binary-safe MESSAGE form
        rec(list(R615[2].encode()), t + 40_000_000, w + 40_000_000),
        "not json",
    ]
    return "\n".join(lines)


def test_journal_json_and_grouping() -> None:
    events = parse_journal_json(_journal())
    assert len(events) == 7
    same_boot = filter_events(events, boot_id=BOOT)
    scan = XidScan(True, same_boot)
    assert len(scan.events) == 6
    assert scan.count == 3  # m0f fault, 615 fault, the binary-form one 20 s later
    assert scan.processes == {"vo", "vspipe"}


def test_since_filters() -> None:
    events = filter_events(parse_journal_json(_journal()), boot_id=BOOT)
    late = filter_events(events, since_monotonic=3_810.0)  # seconds
    assert XidScan(True, late).count == 2
    wall = filter_events(events, since_wall=1_791_365_930.0)
    assert XidScan(True, wall).count == 1
    assert filter_events(events, since_monotonic=10**9) == ()


def test_pid_filter_keeps_pidless_detail_lines() -> None:
    events = filter_events(parse_journal_json(_journal()), boot_id=BOOT)
    mine = filter_events(events, pids={152333})
    # the 615 fault: 3 lines (two without pid) + the later binary-form line
    assert [e.pid for e in mine] == [None, None, 152333, 152333]
    assert XidScan(True, mine).count == 2
    assert filter_events(events, pids={1}) == ()


def test_finding_unreadable_is_info_never_ok() -> None:
    f = xid_finding(XidScan(False, (), "insufficient permissions"), since_text="since boot")
    assert f.severity is Severity.INFO
    assert f.code is ErrorCode.JOURNAL_UNREADABLE
    assert f.id == "gpu_fault.xid"


def test_finding_ok_and_fault() -> None:
    ok = xid_finding(XidScan(True, ()), since_text="since this boot")
    assert ok.severity is Severity.OK and ok.code is None
    events = filter_events(parse_journal_json(_journal()), boot_id=BOOT)
    bad = xid_finding(XidScan(True, events), since_text="since this boot")
    assert bad.severity is Severity.DEGRADED
    assert bad.code is ErrorCode.RIFE_GPU_FAULT
    assert bad.title.key == "GPU fault in RIFE-ncnn"
    assert R615[2] in bad.evidence
    other = XidEvent(31, "PCI:0000:01:00", 999, "steam", 1, 1, BOOT, "NVRM: Xid ... 31, pid=999")
    g = xid_finding(XidScan(True, (other,)), since_text="since this boot")
    assert g.code is ErrorCode.RIFE_GPU_FAULT
    assert g.title.key == "NVIDIA GPU faults in the kernel log"


def _stub(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return str(p)


async def test_scan_unreadable_variants(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpufault, "current_boot_id", lambda: BOOT)
    missing = await scan_xid(journalctl=str(tmp_path / "nope"))
    assert not missing.readable
    denied = await scan_xid(
        journalctl=_stub(
            tmp_path,
            "denied",
            'echo "No journal files were opened due to insufficient permissions." >&2\nexit 1\n',
        )
    )
    assert not denied.readable and denied.error
    failing = await scan_xid(journalctl=_stub(tmp_path, "fail", "exit 3\n"))
    assert not failing.readable
    for s in (missing, denied, failing):
        assert xid_finding(s, since_text="x").severity is Severity.INFO


async def test_scan_reads_stub_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpufault, "current_boot_id", lambda: BOOT)
    data = tmp_path / "journal.json"
    data.write_text(_journal())
    j = _stub(tmp_path, "journalctl", f'cat "{data}"\n')
    scan = await scan_xid(journalctl=j)
    assert scan.readable and scan.count == 3
    none = await scan_xid(journalctl=_stub(tmp_path, "empty", "exit 1\n"))  # -g: no match
    assert none.readable and none.count == 0


def test_session_marker(xdg_env: object) -> None:
    from buttereye.core.paths import resolve

    p = resolve()
    assert gpufault.session_marker_time(p) is None
    p.state_dir.mkdir(parents=True)
    (p.state_dir / gpufault.SESSION_MARKER).write_text("1791365900.5\n")
    assert gpufault.session_marker_time(p) == 1791365900.5
    (p.state_dir / gpufault.SESSION_MARKER).write_text("garbage")
    assert gpufault.session_marker_time(p) is None


def test_session_marker_writer_is_atomic_and_round_trips(xdg_env: object) -> None:
    from buttereye.core.paths import resolve

    p = resolve()
    assert not p.state_dir.exists()
    when = gpufault.write_session_marker(p, pids=(4321, 77, 4321), started=1791367000.25)
    assert when == 1791367000.25
    m = gpufault.read_session_marker(p)
    assert m == gpufault.SessionMarker(1791367000.25, frozenset({77, 4321}))
    assert gpufault.session_marker_time(p) == 1791367000.25
    # atomic: no temp files left behind, and the file is private
    assert sorted(x.name for x in p.state_dir.iterdir()) == [gpufault.SESSION_MARKER]
    assert stat.S_IMODE((p.state_dir / gpufault.SESSION_MARKER).stat().st_mode) == 0o600
    now = gpufault.write_session_marker(p)  # default: time.time(), no pids
    m2 = gpufault.read_session_marker(p)
    assert m2 is not None and m2.started == pytest.approx(now, abs=0.01) and not m2.pids


def _ev(xid: int, pid: int | None, name: str | None, wall_s: float) -> XidEvent:
    who = f"pid={pid}, name={name}, " if pid is not None else ""
    return XidEvent(
        xid,
        "PCI:0000:08:00",
        pid,
        name,
        int(wall_s * 1e6),
        int(wall_s * 1e6) - 1_791_000_000_000_000,
        BOOT,
        f"NVRM: Xid (PCI:0000:08:00): {xid}, {who}Graphics Exception",
    )


INSTALLED = 1_791_366_229.0  # buttereye-vs-rife-ncnn 9.33-0.2.spike on the dev box (03:43:49)
OLD_SOAK = (
    _ev(13, 121562, "vo", INSTALLED - 2500),  # Fedora-ncnn A/B soak, 03:01
    _ev(31, 130001, "vspipe", INSTALLED - 300),  # 03:39
)


def test_faults_before_install_are_older_info_not_blamed() -> None:
    fs = gpufault.doctor_xid_findings(XidScan(True, OLD_SOAK), marker=None, installed_at=INSTALLED)
    f = {x.id: x for x in fs}
    main, older = f["gpu_fault.xid"], f["gpu_fault.xid_older"]
    assert main.severity is Severity.OK and main.code is None
    assert "since the current RIFE build was installed" in str(main.cause.params["since"])
    assert older.severity is Severity.INFO and older.code is None
    assert older.cause.params["n"] == 2
    assert older.cause.params["before"] == "before the current RIFE build was installed"
    assert "MVTools" not in older.fix.key
    assert set(older.evidence) == {e.message for e in OLD_SOAK}


def test_vspipe_fault_after_install_is_blamed_on_rife() -> None:
    new = _ev(109, 200001, "vspipe", INSTALLED + 600)
    fs = gpufault.doctor_xid_findings(
        XidScan(True, (*OLD_SOAK, new)), marker=None, installed_at=INSTALLED
    )
    f = {x.id: x for x in fs}
    main = f["gpu_fault.xid"]
    assert main.severity is Severity.DEGRADED and main.code is ErrorCode.RIFE_GPU_FAULT
    assert main.title.key == "GPU fault in RIFE-ncnn"
    assert "MVTools" in main.fix.key
    assert main.cause.params["n"] == 1 and main.evidence == (new.message,)
    assert f["gpu_fault.xid_older"].cause.params["n"] == 2


def test_generic_vo_fault_is_neutral_unless_pid_was_recorded() -> None:
    vo = _ev(13, 300001, "vo", INSTALLED + 600)
    scan = XidScan(True, (vo,))
    neutral = gpufault.doctor_xid_findings(scan, marker=None, installed_at=INSTALLED)[0]
    assert neutral.severity is Severity.DEGRADED
    assert neutral.title.key == "NVIDIA GPU faults in the kernel log"
    assert "MVTools" not in neutral.fix.key
    marker = gpufault.SessionMarker(INSTALLED + 500, frozenset({300001}))
    ours = gpufault.doctor_xid_findings(scan, marker=marker, installed_at=INSTALLED)[0]
    assert ours.title.key == "GPU fault in RIFE-ncnn"
    assert "since the last RIFE-ncnn session started" in str(ours.cause.params["since"])


def test_marker_after_install_moves_the_cutoff() -> None:
    during_old_session = _ev(13, 1, "vspipe", INSTALLED + 100)
    marker = gpufault.SessionMarker(INSTALLED + 1000, frozenset())
    fs = {
        x.id: x
        for x in gpufault.doctor_xid_findings(
            XidScan(True, (during_old_session,)), marker=marker, installed_at=INSTALLED
        )
    }
    assert fs["gpu_fault.xid"].severity is Severity.OK
    assert fs["gpu_fault.xid_older"].cause.params["before"] == (
        "before the last RIFE-ncnn session started"
    )


def test_boot_fallback_is_neutral() -> None:
    scan = XidScan(True, (_ev(31, 5, "vspipe", INSTALLED),))
    (f,) = gpufault.doctor_xid_findings(scan, marker=None, installed_at=None)
    assert f.severity is Severity.DEGRADED and f.cause.params["since"] == "since this boot"
    assert f.title.key == "NVIDIA GPU faults in the kernel log"
    unreadable = gpufault.doctor_xid_findings(
        XidScan(False, (), "denied"), marker=None, installed_at=INSTALLED
    )
    assert [x.code for x in unreadable] == [ErrorCode.JOURNAL_UNREADABLE]


@pytest.mark.devbox
async def test_real_journal_readable_on_devbox() -> None:
    scan = await scan_xid()
    assert scan.readable, scan.error
