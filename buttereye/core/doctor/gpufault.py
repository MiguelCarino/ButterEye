# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""NVIDIA Xid scan of the kernel log (``journalctl -k``) - SCOPE F1, spike M0(f).

A RIFE-ncnn session can finish with the right frame rate and still have faulted
the GPU, so ButterEye counts kernel Xid lines instead of trusting exit codes.
An unreadable journal is never reported as "no faults" (BE-1031, INFO).

Reusable API (U10 live health, U11 bench call these):

``async scan_xid(*, since_wall=None, since_monotonic=None, pids=None,
                 timeout_s=3.0, journalctl="journalctl") -> XidScan``
    Xid lines of the **current boot** logged after ``since_wall`` (seconds since
    the epoch, ``time.time()``) and/or after ``since_monotonic`` (seconds,
    ``time.monotonic()``; CLOCK_MONOTONIC is the journal's monotonic clock).
    ``pids``: keep only events whose ``pid=`` is in the set, plus the pid-less
    detail lines that belong to the same fault (same PCI address, within 1 s of a
    kept line). Never raises: ``XidScan.readable`` is False when journalctl is
    missing, times out, is denied, or fails. ``XidScan.count`` is the number of
    distinct faults (grouped per (pci, xid) within 1 s), ``XidScan.lines`` the
    raw messages for ``Finding.evidence``.

``parse_xid(message) -> XidLine | None`` parses one kernel message (both the old
``NVRM: Xid 13, ...`` and the 6xx ``NVRM: Xid (PCI:0000:08:00): 13, ...`` forms).

``parse_journal_json(text) -> tuple[XidEvent, ...]`` parses ``journalctl -o json``.

``xid_finding(scan, *, since_text, blame=None) -> Finding`` builds one GPU-fault row.

``doctor_xid_findings(scan, *, marker, installed_at) -> tuple[Finding, ...]`` is the
doctor's view of a whole-boot scan: faults before the cutoff (the later of the last
RIFE session start and the install time of the RIFE plugin package) go into a
separate INFO row ``gpu_fault.xid_older`` and are never blamed on the current
build; only faults after the cutoff can make ``gpu_fault.xid`` DEGRADED.

``write_session_marker(paths, *, pids=(), started=None)`` records the start of a
RIFE-ncnn session (live play, bench). Call it when the RIFE graph is actually
loaded, with the pids of the processes that run it (mpv / vspipe).

Usage, e.g. after a bench run::

    t0 = time.monotonic()
    ... run vspipe ...
    scan = await scan_xid(since_monotonic=t0, pids={proc.pid})
    gpu_faults = scan.count if scan.readable else None
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.doctor.proc import CmdResult, run
from buttereye.core.doctor.text import M
from buttereye.core.errors import ErrorCode
from buttereye.core.types import Finding, Paths, Section, Severity

#: Marker written (``write_session_marker``) when a RIFE-ncnn session starts.
#: ASCII: line 1 wall-clock seconds since the epoch, line 2 (optional) the pids
#: that run the RIFE graph, space separated. Doctor scans the current boot
#: without it, bounded by the RIFE package's install time.
SESSION_MARKER = "rife-session-start"

#: Process names that mean "this fault came from a ButterEye pipeline".
RIFE_PROCESS_NAMES = frozenset({"vspipe", "mpv", "vo", "mpv/vo", "VideoDecoder"})

_XID = re.compile(
    r"NVRM: Xid\s*(?:\((?P<pci>PCI:[0-9A-Fa-f:.]+)\))?\s*:?\s*(?P<xid>\d+)\s*,\s*(?P<rest>.*)$"
)
_PID = re.compile(r"\bpid=(?P<pid>\d+|'<unknown>')")
_NAME = re.compile(r"\bname=(?P<name>[^,]+)")

_DENIED = (
    "insufficient permissions",
    "not seeing messages from other users and the system",
    "no journal files were found",
    "permission denied",
)


@dataclass(frozen=True, slots=True)
class XidLine:
    xid: int
    pci: str | None  # "PCI:0000:08:00" when the driver prints it
    pid: int | None
    process: str | None


@dataclass(frozen=True, slots=True)
class XidEvent:
    xid: int
    pci: str | None
    pid: int | None
    process: str | None
    realtime_us: int | None  # __REALTIME_TIMESTAMP
    monotonic_us: int | None  # __MONOTONIC_TIMESTAMP (CLOCK_MONOTONIC, this boot)
    boot_id: str | None
    message: str


@dataclass(frozen=True, slots=True)
class XidScan:
    readable: bool
    events: tuple[XidEvent, ...]
    error: str | None = None  # why unreadable (untranslated detail)

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(e.message for e in self.events)

    @property
    def count(self) -> int:
        """Distinct faults: events grouped per (pci, xid) within 1 s."""
        return len(group_faults(self.events))

    @property
    def processes(self) -> frozenset[str]:
        return frozenset(e.process for e in self.events if e.process)


def parse_xid(message: str) -> XidLine | None:
    m = _XID.search(message)
    if not m:
        return None
    rest = m.group("rest")
    pid_m = _PID.search(rest)
    pid = int(pid_m.group("pid")) if pid_m and pid_m.group("pid").isdigit() else None
    name_m = _NAME.search(rest)
    return XidLine(
        xid=int(m.group("xid")),
        pci=m.group("pci"),
        pid=pid,
        process=name_m.group("name").strip() if name_m else None,
    )


def _as_int(v: object) -> int | None:
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    return None


def _message(v: object) -> str | None:
    if isinstance(v, str):
        return v
    if isinstance(v, list) and all(isinstance(b, int) for b in v):  # binary-safe form
        return bytes(v).decode("utf-8", "replace")
    return None


def parse_journal_json(text: str) -> tuple[XidEvent, ...]:
    """Xid events from ``journalctl -o json`` output (other lines ignored)."""
    out: list[XidEvent] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        msg = _message(rec.get("MESSAGE"))
        if msg is None:
            continue
        x = parse_xid(msg)
        if x is None:
            continue
        boot = rec.get("_BOOT_ID")
        out.append(
            XidEvent(
                xid=x.xid,
                pci=x.pci,
                pid=x.pid,
                process=x.process,
                realtime_us=_as_int(rec.get("__REALTIME_TIMESTAMP")),
                monotonic_us=_as_int(rec.get("__MONOTONIC_TIMESTAMP")),
                boot_id=boot if isinstance(boot, str) else None,
                message=msg.strip(),
            )
        )
    return tuple(out)


def _t(e: XidEvent) -> float:
    if e.monotonic_us is not None:
        return e.monotonic_us / 1e6
    if e.realtime_us is not None:
        return e.realtime_us / 1e6
    return 0.0


def group_faults(events: Iterable[XidEvent], window_s: float = 1.0) -> list[list[XidEvent]]:
    """Group the several kernel lines one fault produces (same pci + xid, <= 1 s)."""
    groups: list[list[XidEvent]] = []
    for e in sorted(events, key=_t):
        for g in reversed(groups[-4:]):
            last = g[-1]
            if last.pci == e.pci and last.xid == e.xid and _t(e) - _t(last) <= window_s:
                g.append(e)
                break
        else:
            groups.append([e])
    return groups


def filter_events(
    events: Iterable[XidEvent],
    *,
    since_wall: float | None = None,
    since_monotonic: float | None = None,
    boot_id: str | None = None,
    pids: Collection[int] | None = None,
) -> tuple[XidEvent, ...]:
    """Pure filter used by ``scan_xid`` (exposed for tests and callers with logs)."""
    kept: list[XidEvent] = []
    for e in events:
        if boot_id is not None and e.boot_id is not None and e.boot_id != boot_id:
            continue
        if since_wall is not None and (e.realtime_us is None or e.realtime_us < since_wall * 1e6):
            continue
        if since_monotonic is not None and (
            e.monotonic_us is None or e.monotonic_us < since_monotonic * 1e6
        ):
            continue
        kept.append(e)
    if pids is None:
        return tuple(kept)
    wanted = set(pids)
    out: list[XidEvent] = []
    for g in group_faults(kept):
        if any(e.pid in wanted for e in g):
            out.extend(g)
    return tuple(sorted(out, key=_t))


def current_boot_id() -> str | None:
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return raw.replace("-", "").lower() or None


def _denied(res: CmdResult) -> str | None:
    if res.missing:
        return "journalctl is not installed"
    if res.timed_out:
        return "journalctl timed out"
    err = res.stderr.lower()
    for marker in _DENIED:
        if marker in err:
            return res.stderr.strip().splitlines()[0] if res.stderr.strip() else marker
    # -g with no match exits 1 with empty output; anything else non-zero is a failure.
    if res.returncode not in (0, 1):
        return f"journalctl exited with status {res.returncode}: {res.stderr.strip()[:200]}"
    if res.returncode == 1 and res.stdout.strip():
        return f"journalctl exited with status 1: {res.stderr.strip()[:200]}"
    return None


async def scan_xid(
    *,
    since_wall: float | None = None,
    since_monotonic: float | None = None,
    pids: Collection[int] | None = None,
    timeout_s: float = 3.0,
    journalctl: str = "journalctl",
) -> XidScan:
    """See the module docstring. Read-only; never raises for journal problems."""
    argv = [journalctl, "-k", "--no-pager", "-q", "-o", "json", "--grep=NVRM: Xid"]
    if since_wall is not None:
        argv.append(f"--since=@{max(0, int(since_wall) - 1)}")
    res = await run(argv, timeout_s=timeout_s)
    if "grep" in res.stderr.lower() and "not supported" in res.stderr.lower():
        # journalctl built without PCRE2: fetch the kernel log and filter here.
        argv = [a for a in argv if not a.startswith("--grep")]
        res = await run(argv, timeout_s=timeout_s)
    why = _denied(res)
    if why is not None:
        return XidScan(False, (), why)
    events = parse_journal_json(res.stdout)
    events = filter_events(
        events,
        since_wall=since_wall,
        since_monotonic=since_monotonic,
        boot_id=current_boot_id(),
        pids=pids,
    )
    return XidScan(True, events)


@dataclass(frozen=True, slots=True)
class SessionMarker:
    started: float  # epoch seconds
    pids: frozenset[int] = frozenset()


def write_session_marker(
    paths: Paths, *, pids: Iterable[int] = (), started: float | None = None
) -> float:
    """Record that a RIFE-ncnn session started now (or at ``started``).

    Atomic (0600 temp file in ``paths.state_dir`` + ``os.replace``); creates the
    state folder first. Returns the recorded time. Raises ``OSError`` if the
    state folder can't be written (callers treat that as "not recorded").
    """
    when = time.time() if started is None else float(started)
    pid_line = " ".join(str(int(p)) for p in sorted(set(pids)) if int(p) > 0)
    body = f"{when:.3f}\n{pid_line}\n"
    paths.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{SESSION_MARKER}.", dir=paths.state_dir)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(body)
        os.replace(tmp, paths.state_dir / SESSION_MARKER)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return when


def read_session_marker(paths: Paths) -> SessionMarker | None:
    """The last recorded RIFE-ncnn session start, if any (bad or future: None)."""
    try:
        raw = (paths.state_dir / SESSION_MARKER).read_text(encoding="ascii")
    except OSError, ValueError:
        return None
    lines = raw.splitlines()
    try:
        value = float(lines[0].strip()) if lines else 0.0
    except ValueError:
        return None
    if not (0 < value <= time.time() + 60):
        return None
    pids = frozenset(
        int(tok) for ln in lines[1:2] for tok in ln.split() if tok.isdigit() and int(tok) > 0
    )
    return SessionMarker(value, pids)


def session_marker_time(paths: Paths) -> float | None:
    """Start time (epoch s) of the last RIFE-ncnn session, if one was recorded."""
    m = read_session_marker(paths)
    return m.started if m is not None else None


def _clock(epoch: float, now: float | None = None) -> str:
    """Local clock time; with the date when it isn't today."""
    t = time.localtime(epoch)
    today = time.localtime(time.time() if now is None else now)
    if (t.tm_year, t.tm_yday) == (today.tm_year, today.tm_yday):
        return time.strftime("%H:%M", t)
    return time.strftime("%Y-%m-%d %H:%M", t)


def doctor_xid_findings(
    scan: XidScan,
    *,
    marker: SessionMarker | None,
    installed_at: float | None,
    max_evidence: int = 12,
) -> tuple[Finding, ...]:
    """Doctor rows for a whole-boot ``scan`` (see the module docstring).

    The cutoff is the later of ``marker.started`` and ``installed_at`` (the RIFE
    plugin package's INSTALLTIME). Faults before it are listed in an INFO row
    (``gpu_fault.xid_older``) with no engine advice; they never make
    ``gpu_fault.xid`` DEGRADED, so backend selection ignores them. The "GPU fault
    in RIFE-ncnn" title is used only when a fault after the cutoff comes from a
    recorded session pid, or from vspipe (which only ButterEye pipelines run
    with the RIFE plugin); generic mpv/vo names alone stay neutral.
    """
    if not scan.readable:
        return (xid_finding(scan, since_text="since this boot"),)
    cands: list[tuple[float, str]] = []
    if installed_at is not None and installed_at > 0:
        cands.append((float(installed_at), "install"))
    if marker is not None:
        cands.append((marker.started, "session"))
    cutoff, source = max(cands) if cands else (None, "boot")
    recent: tuple[XidEvent, ...]
    older: tuple[XidEvent, ...]
    if cutoff is None:
        recent, older = scan.events, ()
    else:
        lim = cutoff * 1e6
        recent = tuple(e for e in scan.events if e.realtime_us is None or e.realtime_us >= lim)
        older = tuple(e for e in scan.events if e.realtime_us is not None and e.realtime_us < lim)
    if source == "session":
        assert cutoff is not None
        since = f"since the last RIFE-ncnn session started ({_clock(cutoff)})"
        before = "before the last RIFE-ncnn session started"
    elif source == "install":
        assert cutoff is not None
        since = f"since the current RIFE build was installed ({_clock(cutoff)})"
        before = "before the current RIFE build was installed"
    else:
        since, before = "since this boot", ""
    pids = marker.pids if marker is not None else frozenset()
    blame = cutoff is not None and any(
        (e.pid is not None and e.pid in pids) or e.process == "vspipe" for e in recent
    )
    out = [
        xid_finding(XidScan(True, recent), since_text=since, blame=blame, max_evidence=max_evidence)
    ]
    if older:
        old = XidScan(True, older)
        stamps = [e.realtime_us / 1e6 for e in older if e.realtime_us is not None]
        first, last = _clock(min(stamps)), _clock(max(stamps))
        span = first if first == last else f"{first}\u2013{last}"
        lines = old.lines
        out.append(
            Finding(
                id="gpu_fault.xid_older",
                section=Section.GPU_FAULT,
                severity=Severity.INFO,
                code=None,
                title=M("Older NVIDIA GPU faults in the kernel log"),
                cause=M(
                    "The kernel log has {n} older NVIDIA GPU fault(s) (Xid {xids}) at "
                    "{span}, {before}. They don't count against the current setup.",
                    {
                        "n": old.count,
                        "xids": ", ".join(str(x) for x in sorted({e.xid for e in older})),
                        "span": span,
                        "before": before,
                    },
                ),
                fix=M("Nothing to do. If new faults appear, they are reported above."),
                commands=("journalctl -k -g 'NVRM: Xid'",),
                evidence=lines if len(lines) <= max_evidence else lines[-max_evidence:],
            )
        )
    return tuple(out)


def xid_finding(
    scan: XidScan, *, since_text: str, max_evidence: int = 12, blame: bool | None = None
) -> Finding:
    """A GPU-fault row for ``scan`` (never OK when unreadable).

    ``blame``: whether the faults are known to come from a ButterEye RIFE
    pipeline. ``None`` (callers whose scan is already limited to their own
    pids and time window: bench, smoke test, live health) decides from the
    process names."""
    if not scan.readable:
        return Finding(
            id="gpu_fault.xid",
            section=Section.GPU_FAULT,
            severity=Severity.INFO,
            code=ErrorCode.JOURNAL_UNREADABLE,
            title=M("Couldn't read the kernel log; GPU faults can't be checked"),
            cause=M(
                "ButterEye looks for NVIDIA Xid errors in journalctl -k, but it could not "
                "read it: {why}",
                {"why": scan.error or "unknown"},
            ),
            fix=M(
                "Add yourself to the systemd-journal group (then log in again) to let "
                "ButterEye detect GPU faults."
            ),
            commands=("sudo usermod -aG systemd-journal $USER",),
            evidence=(scan.error,) if scan.error else (),
        )
    n = scan.count
    if n == 0:
        return Finding(
            id="gpu_fault.xid",
            section=Section.GPU_FAULT,
            severity=Severity.OK,
            code=None,
            title=M("No GPU faults in the kernel log"),
            cause=M("No NVIDIA Xid errors {since}.", {"since": since_text}),
            fix=M(""),
        )
    ours = bool(scan.processes & RIFE_PROCESS_NAMES) if blame is None else blame
    xids = sorted({e.xid for e in scan.events})
    lines = scan.lines
    evidence = lines if len(lines) <= max_evidence else lines[-max_evidence:]
    return Finding(
        id="gpu_fault.xid",
        section=Section.GPU_FAULT,
        severity=Severity.DEGRADED,
        code=ErrorCode.RIFE_GPU_FAULT,
        title=M("GPU fault in RIFE-ncnn") if ours else M("NVIDIA GPU faults in the kernel log"),
        cause=M(
            "The kernel log has {n} NVIDIA GPU fault(s) (Xid {xids}) {since}{who}. "
            "A video can keep playing sound over a frozen picture after such a fault.",
            {
                "n": n,
                "xids": ", ".join(str(x) for x in xids),
                "since": since_text,
                "who": (
                    ", raised by " + ", ".join(sorted(scan.processes)) if scan.processes else ""
                ),
            },
        ),
        fix=(
            M(
                "Use MVTools for affected files, or try a lighter RIFE profile; "
                "if faults repeat, report them with the lines below."
            )
            if ours
            else M(
                "ButterEye can't tie these faults to its own pipeline; another app or "
                "the driver may have caused them. If the picture froze while ButterEye "
                "was playing, report it with the lines below."
            )
        ),
        commands=("journalctl -k -g 'NVRM: Xid'",),
        evidence=evidence,
    )


__all__ = [
    "SESSION_MARKER",
    "SessionMarker",
    "write_session_marker",
    "read_session_marker",
    "doctor_xid_findings",
    "XidLine",
    "XidEvent",
    "XidScan",
    "parse_xid",
    "parse_journal_json",
    "group_faults",
    "filter_events",
    "current_boot_id",
    "scan_xid",
    "session_marker_time",
    "xid_finding",
]
