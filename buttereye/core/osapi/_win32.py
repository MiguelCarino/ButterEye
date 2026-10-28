# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Windows calls ``osapi`` needs, through ``ctypes`` only (no pywin32).

Imported only on Windows. Every function here either answers a question about
this user's processes and files or this machine's power, or acts on a process
ButterEye started or on ButterEye's own power request; nothing touches another
user's objects. Failures read as "not alive" / "not ours", the
safe side for each caller.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterable
from ctypes import wintypes
from pathlib import Path

assert sys.platform == "win32"  # mypy: the rest is checked only for --platform win32

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_nt = ctypes.WinDLL("ntdll")

HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SUSPEND_RESUME = 0x0800
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
TOKEN_QUERY = 0x0008
TOKEN_USER_CLASS = 1  # TokenUser
TOKEN_OWNER_CLASS = 4  # TokenOwner
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = HANDLE(-1).value
SE_FILE_OBJECT = 1
OWNER_SECURITY_INFORMATION = 0x00000001
WM_CLOSE = 0x0010
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JobObjectExtendedLimitInformation = 9
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# creation flags (subprocess has most of them; spelled out so the set is visible here)
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

# power requests (winnt.h); ExecutionRequired is Windows 8 and later
POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x00000001
POWER_REQUEST_SYSTEM_REQUIRED = 1
POWER_REQUEST_EXECUTION_REQUIRED = 3


class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", DWORD), ("hi", DWORD)]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]  # fmt: skip


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", DWORD),
        ("SchedulingClass", DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _REASON_DETAILED(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", wintypes.HMODULE),
        ("LocalizedReasonId", wintypes.ULONG),
        ("ReasonStringCount", wintypes.ULONG),
        ("ReasonStrings", ctypes.POINTER(wintypes.LPWSTR)),
    ]


class _REASON_UNION(ctypes.Union):
    _fields_ = [("Detailed", _REASON_DETAILED), ("SimpleReasonString", wintypes.LPWSTR)]


class _REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", wintypes.ULONG), ("Flags", DWORD), ("Reason", _REASON_UNION)]


class _SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", DWORD),
        ("BatteryFullLifeTime", DWORD),
    ]


def _proto(fn: object, res: object, *args: object) -> None:
    fn.restype = res  # type: ignore[attr-defined]
    fn.argtypes = list(args)  # type: ignore[attr-defined]


_proto(_k32.OpenProcess, HANDLE, DWORD, BOOL, DWORD)
_proto(_k32.CloseHandle, BOOL, HANDLE)
_proto(_k32.GetExitCodeProcess, BOOL, HANDLE, ctypes.POINTER(DWORD))
_proto(_k32.TerminateProcess, BOOL, HANDLE, wintypes.UINT)
_proto(_k32.GetProcessTimes, BOOL, HANDLE, *([ctypes.POINTER(_FILETIME)] * 4))
_proto(_k32.CreateToolhelp32Snapshot, HANDLE, DWORD, DWORD)
_proto(_k32.Process32FirstW, BOOL, HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_proto(_k32.Process32NextW, BOOL, HANDLE, ctypes.POINTER(_PROCESSENTRY32W))
_proto(_k32.GetCurrentProcess, HANDLE)
_proto(_k32.CreateJobObjectW, HANDLE, ctypes.c_void_p, wintypes.LPCWSTR)
_proto(_k32.SetInformationJobObject, BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD)
_proto(_k32.AssignProcessToJobObject, BOOL, HANDLE, HANDLE)
_proto(_k32.GetNamedPipeServerProcessId, BOOL, HANDLE, ctypes.POINTER(wintypes.ULONG))
_proto(_k32.LocalFree, ctypes.c_void_p, ctypes.c_void_p)
_proto(_k32.CreateMutexW, HANDLE, ctypes.c_void_p, BOOL, wintypes.LPCWSTR)
_proto(_k32.GetLongPathNameW, DWORD, wintypes.LPCWSTR, wintypes.LPWSTR, DWORD)
_proto(_adv.OpenProcessToken, BOOL, HANDLE, DWORD, ctypes.POINTER(HANDLE))
_proto(_adv.GetTokenInformation, BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, DWORD,
       ctypes.POINTER(DWORD))  # fmt: skip
_proto(_adv.ConvertSidToStringSidW, BOOL, ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
_proto(_adv.GetNamedSecurityInfoW, DWORD, wintypes.LPCWSTR, ctypes.c_int, DWORD,
       ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
       ctypes.POINTER(ctypes.c_void_p))  # fmt: skip
_WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, wintypes.HWND, wintypes.LPARAM)
_proto(_u32.EnumWindows, BOOL, _WNDENUMPROC, wintypes.LPARAM)
_proto(_u32.GetWindowThreadProcessId, DWORD, wintypes.HWND, ctypes.POINTER(DWORD))
_proto(_u32.PostMessageW, BOOL, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_proto(_k32.PowerCreateRequest, HANDLE, ctypes.POINTER(_REASON_CONTEXT))
_proto(_k32.PowerSetRequest, BOOL, HANDLE, ctypes.c_int)
_proto(_k32.PowerClearRequest, BOOL, HANDLE, ctypes.c_int)
_proto(_k32.GetSystemPowerStatus, BOOL, ctypes.POINTER(_SYSTEM_POWER_STATUS))
# NTSTATUS (a LONG, negative on failure); undocumented but stable since Windows XP
_proto(_nt.NtSuspendProcess, ctypes.c_long, HANDLE)
_proto(_nt.NtResumeProcess, ctypes.c_long, HANDLE)


# ---------------------------------------------------------------- SIDs
def _sid_string(psid: int | None) -> str | None:
    if not psid:
        return None
    out = wintypes.LPWSTR()
    if not _adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return None
    try:
        return out.value
    finally:
        _k32.LocalFree(ctypes.cast(out, ctypes.c_void_p))


def _token_user(process: int, info_class: int = TOKEN_USER_CLASS) -> str | None:
    """The SID ``info_class`` names in ``process``'s token: TokenUser (who it runs
    as) or TokenOwner (the owner its new files get)."""
    token = HANDLE()
    if not _adv.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = DWORD(0)
        _adv.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not _adv.GetTokenInformation(token, info_class, buf, size, ctypes.byref(size)):
            return None
        # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is the PSID;
        # TOKEN_OWNER is just the PSID
        return _sid_string(ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        _k32.CloseHandle(token)


_ME: list[str | None] = []


def my_sid() -> str | None:
    """This process's user SID (``S-1-5-21-…``), cached."""
    if not _ME:
        _ME.append(_token_user(_k32.GetCurrentProcess()))
    return _ME[0]


_OWNER: list[str | None] = []


def my_owner_sid() -> str | None:
    """The owner this process's new files and folders get, cached. Usually
    ``my_sid()``; for an administrator on Windows Server (the default policy) or
    an elevated process, the Administrators group (``S-1-5-32-544``)."""
    if not _OWNER:
        _OWNER.append(_token_user(_k32.GetCurrentProcess(), TOKEN_OWNER_CLASS))
    return _OWNER[0]


def process_sid(pid: int) -> str | None:
    """The user SID ``pid`` runs as; None if it can't be read (gone, or not ours to ask)."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return _token_user(h)
    finally:
        _k32.CloseHandle(h)


def file_owner_sid(path: Path) -> str | None:
    owner = ctypes.c_void_p()
    sd = ctypes.c_void_p()
    rc = _adv.GetNamedSecurityInfoW(
        os.fspath(path), SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner), None, None, None, ctypes.byref(sd),
    )  # fmt: skip
    if rc != 0:
        return None
    try:
        return _sid_string(owner.value)
    finally:
        _k32.LocalFree(sd)


# ---------------------------------------------------------------- processes
def pid_alive(pid: int) -> bool:
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # gone, or a process we may not even query (then it is certainly not ours)
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists
    try:
        code = DWORD()
        if not _k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def _born(h: int) -> int | None:
    """Creation time of the process behind handle ``h`` (100 ns ticks)."""
    c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
    if not _k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k),
                                ctypes.byref(u)):  # fmt: skip
        return None
    return int((c.hi << 32) | c.lo)


def _created(pid: int) -> int | None:
    """Creation time of ``pid`` (100 ns ticks), to tell a child from a reused pid."""
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return _born(h)
    finally:
        _k32.CloseHandle(h)


def _parents() -> dict[int, list[int]]:
    """pid -> child pids, from one process snapshot."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return {}
    kids: dict[int, list[int]] = {}
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID != entry.th32ParentProcessID:
                kids.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return kids


def tree(pid: int) -> list[int]:
    """``pid`` and its descendants, root first. A recorded parent pid that has been
    reused by a newer process is not followed (a child is always younger)."""
    kids = _parents()
    out = [pid]
    born = {pid: _created(pid)}
    i = 0
    while i < len(out):
        parent = out[i]
        i += 1
        pborn = born.get(parent)
        for child in kids.get(parent, ()):
            if child in born:
                continue
            cborn = _created(child)
            if pborn is not None and cborn is not None and cborn < pborn:
                continue
            born[child] = cborn
            out.append(child)
    return out


def _suspend_or_resume(pid: int, suspend: bool, born: int | None = None) -> int | None:
    """``NtSuspendProcess`` (``suspend``) or ``NtResumeProcess`` on ``pid``; its
    creation time when that worked, else None. With ``born``, only the process
    created at that time is touched, never a newer one that reused the pid."""
    h = _k32.OpenProcess(PROCESS_SUSPEND_RESUME | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        created = _born(h)
        if created is None or (born is not None and created != born):
            return None
        code = DWORD()
        # an exited process lingers while a handle to it is open (asyncio's)
        if suspend and (not _k32.GetExitCodeProcess(h, ctypes.byref(code))
                        or code.value != STILL_ACTIVE):  # fmt: skip
            return None
        status = (_nt.NtSuspendProcess if suspend else _nt.NtResumeProcess)(h)
        return created if status >= 0 else None
    finally:
        _k32.CloseHandle(h)


def suspend_pids(pids: Iterable[int]) -> list[tuple[int, int]]:
    """Suspend every thread of each of ``pids``, as Resource Monitor's "Suspend
    process" does: Windows' stand-in for SIGSTOP, which it doesn't have. Returns
    (pid, creation time) of each one suspended, for ``resume_pids``; a pid that
    is gone or not ours to suspend is skipped. Suspensions count: each needs its
    own resume."""
    held: list[tuple[int, int]] = []
    for pid in dict.fromkeys(pids):
        born = _suspend_or_resume(pid, True)
        if born is not None:
            held.append((pid, born))
    return held


def resume_pids(held: Iterable[tuple[int, int]]) -> int:
    """Resume what ``suspend_pids`` returned, each process only if it is still
    the one suspended (same creation time); how many were resumed."""
    return sum(_suspend_or_resume(pid, False, born) is not None for pid, born in held)


def terminate(pid: int, code: int = 1) -> bool:
    h = _k32.OpenProcess(PROCESS_TERMINATE, False, pid)
    if not h:
        return False
    try:
        return bool(_k32.TerminateProcess(h, code))
    finally:
        _k32.CloseHandle(h)


def post_close(pids: set[int]) -> int:
    """Post WM_CLOSE to every top-level window of ``pids``; how many got one."""
    hit: list[int] = []

    def visit(hwnd: int, _lparam: int) -> bool:
        owner = DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and _u32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
            hit.append(hwnd)
        return True

    _u32.EnumWindows(_WNDENUMPROC(visit), 0)
    return len(hit)


_JOB: list[int] = []


def ensure_job() -> bool:
    """Put ButterEye in a job that ends its helper processes with it (kill on
    close) but lets mpv break away, so a crashed window never leaves a render
    or a probe running. Once per process; False if Windows refused."""
    if _JOB:
        return _JOB[0] != 0
    job = _k32.CreateJobObjectW(None, None)
    ok = False
    if job:
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
        )
        ok = bool(
            _k32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )  # fmt: skip
            and _k32.AssignProcessToJobObject(job, _k32.GetCurrentProcess())
        )
        if not ok:
            _k32.CloseHandle(job)
    # the handle stays open for the life of the process: closing it is the kill
    _JOB.append(job if ok else 0)
    return ok


_MUTEXES: list[int] = []


def hold_mutex(name: str) -> bool:
    """Create (or open) the named mutex ``name`` and keep a handle to it for the
    life of the process; it exists while any process holds one. Not inherited.
    False if Windows refused (a name too long, say)."""
    handle = _k32.CreateMutexW(None, False, name)
    if not handle:
        return False
    _MUTEXES.append(handle)
    return True


def long_path(path: str) -> str:
    """``path`` with every 8.3 short name spelled out (``GetLongPathNameW``), as
    the installer sees its folder; ``path`` itself when Windows can't tell."""
    size = _k32.GetLongPathNameW(path, None, 0)
    if not size:
        return path
    buf = ctypes.create_unicode_buffer(size)
    got = _k32.GetLongPathNameW(path, buf, size)
    return buf.value if 0 < got < size else path


# ---------------------------------------------------------------- power
#: what each power request ``begin_activity`` makes asks for
_ACTIVITY_REQUESTS = (POWER_REQUEST_SYSTEM_REQUIRED, POWER_REQUEST_EXECUTION_REQUIRED)


def begin_activity(reason: str) -> int | None:
    """A power request that keeps Windows from sleeping until ``end_activity``:
    ``SystemRequired``, and ``ExecutionRequired`` for a Modern Standby laptop,
    which otherwise stops this process's work when the screen goes off.
    ``reason`` is what ``powercfg /requests`` shows. The display may still turn
    off. The request's handle, or None when Windows refused the request or the
    first of the two; ExecutionRequired alone failing (before Windows 8) keeps
    the rest."""
    text = ctypes.create_unicode_buffer(reason)  # alive until the request is made
    ctx = _REASON_CONTEXT()
    ctx.Version = POWER_REQUEST_CONTEXT_VERSION
    ctx.Flags = POWER_REQUEST_CONTEXT_SIMPLE_STRING
    ctx.Reason.SimpleReasonString = ctypes.cast(text, wintypes.LPWSTR)
    handle = _k32.PowerCreateRequest(ctypes.byref(ctx))
    del text
    if not handle or handle == INVALID_HANDLE_VALUE:
        return None
    if not _k32.PowerSetRequest(handle, POWER_REQUEST_SYSTEM_REQUIRED):
        _k32.CloseHandle(handle)
        return None
    _k32.PowerSetRequest(handle, POWER_REQUEST_EXECUTION_REQUIRED)
    return int(handle)


def end_activity(handle: int) -> None:
    """Clear what ``begin_activity`` asked for and close its handle. Clearing a
    request that was never set fails harmlessly. Never raises."""
    for kind in _ACTIVITY_REQUESTS:
        _k32.PowerClearRequest(handle, kind)
    _k32.CloseHandle(handle)


def power_status() -> tuple[int, int, int] | None:
    """``GetSystemPowerStatus``: (ACLineStatus, BatteryFlag, SystemStatusFlag),
    or None when it fails. ``osapi`` reads them."""
    status = _SYSTEM_POWER_STATUS()
    if not _k32.GetSystemPowerStatus(ctypes.byref(status)):
        return None
    return status.ACLineStatus, status.BatteryFlag, status.SystemStatusFlag


def pipe_server_pid(handle: int) -> int | None:
    pid = wintypes.ULONG()
    if not _k32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        return None
    return int(pid.value) or None


__all__ = [
    "BELOW_NORMAL_PRIORITY_CLASS",
    "CREATE_BREAKAWAY_FROM_JOB",
    "CREATE_NEW_PROCESS_GROUP",
    "CREATE_NO_WINDOW",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "begin_activity",
    "end_activity",
    "ensure_job",
    "file_owner_sid",
    "hold_mutex",
    "long_path",
    "my_owner_sid",
    "my_sid",
    "pid_alive",
    "pipe_server_pid",
    "post_close",
    "power_status",
    "process_sid",
    "resume_pids",
    "suspend_pids",
    "terminate",
    "tree",
]
