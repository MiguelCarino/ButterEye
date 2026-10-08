# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Error codes and the exception tree (docs/design/GUI.md §2.2, FROZEN).

``docs/errors.md`` (F21) lists every code with its fix text. Tests assert codes,
never text. Exit codes follow SCOPE §4.1: 1 runtime, 2 usage/config, 3 blocking
doctor issue, 4 dependency missing, 130 cancelled.
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    # value = (code, exit_code)
    MPV_TOO_OLD = ("BE-1001", 3)
    MPV_NO_VS_FILTER = ("BE-1002", 3)
    MPV_SANDBOXED = ("BE-1003", 3)
    MPV_NOT_FOUND = ("BE-1004", 4)
    PROBE_FAILED = ("BE-1005", 3)
    VULKAN_LOADER_MISSING = ("BE-1010", 4)
    VULKAN_CPU_ONLY = ("BE-1011", 4)  # degraded: MVTools only (§5.3.2)
    PKG_MISSING = ("BE-1020", 4)
    LICENCE_FILE_MISSING = ("BE-1021", 1)  # %license absent (F1/F18)
    VARIANT_SYSTEM_NCNN = ("BE-1022", 1)  # degraded: Fedora-ncnn build active (m0f)
    RIFE_GPU_FAULT = ("BE-1030", 1)  # Xid in journalctl -k (F1)
    JOURNAL_UNREADABLE = ("BE-1031", 1)  # info: fault history unknown
    USER_CONF_CONFLICT = ("BE-1040", 1)  # hwdec/interpolation/save-position (F1)
    MPVSOCKETS_IN_USE = ("BE-1041", 1)
    TRT_UNSUPPORTED = ("BE-1050", 4)  # TRT section items (a)-(g), experimental
    CONFIG_INVALID = ("BE-2001", 2)
    CONFIG_NEWER_SCHEMA = ("BE-2002", 2)
    CONFIG_CONFLICT = ("BE-2003", 2)  # revision changed since load
    CONFIG_VALUE = ("BE-2004", 2)  # field-level validation
    CONFIG_UNKNOWN_KEY = ("BE-2005", 2)  # warning only
    RUNTIME_DIR_UNSAFE = ("BE-2010", 1)  # §4.3 0700/owner
    GUI_ALREADY_RUNNING = ("BE-2020", 1)
    SOCKET_UNSAFE = ("BE-3001", 1)
    INSTANCE_UNSUPPORTED = ("BE-3002", 3)
    VF_ROLLED_BACK = ("BE-3003", 1)  # F4
    FILTER_STALLED = ("BE-3004", 1)
    IPC_LOST = ("BE-3005", 1)
    DEVICE_LOST = ("BE-3006", 1)
    FILE_NOT_LOCAL = ("BE-3007", 2)  # URLs refused (§2 non-goals)
    FFMS2_MISSING = ("BE-4001", 4)
    CODEC_NOT_DECODABLE = ("BE-4002", 4)
    HDR_CLASS_REFUSED = ("BE-4003", 2)
    NO_SPACE = ("BE-4004", 1)
    VSPIPE_FRAME_ERROR = ("BE-4005", 1)
    ENCODER_FAILED = ("BE-4006", 1)
    REMUX_FAILED = ("BE-4007", 1)
    OUTPUT_EXISTS = ("BE-4008", 2)
    BENCH_FAILED = ("BE-5001", 1)
    HASH_MISMATCH = ("BE-6001", 1)
    HOST_NOT_ALLOWED = ("BE-6002", 1)
    DOWNLOAD_FAILED = ("BE-6003", 1)
    SEVENZIP_MISSING = ("BE-6004", 4)
    NOT_IMPLEMENTED = ("BE-9001", 1)
    INTERNAL = ("BE-9999", 1)

    @property
    def code(self) -> str:
        return self.value[0]

    @property
    def exit_code(self) -> int:
        return self.value[1]

    @classmethod
    def from_code(cls, code: str) -> ErrorCode:
        """Look up a member by its ``BE-xxxx`` string (KeyError if unknown)."""
        for member in cls:
            if member.code == code:
                return member
        raise KeyError(code)


class ButterEyeError(Exception):
    """Base of every error the core raises. ``cause``/``fix`` are translatable."""

    code: ErrorCode
    cause: Msg
    fix: Msg | None
    detail: str | None
    commands: tuple[str, ...]

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,  # raw stderr/log tail, untranslated
        commands: tuple[str, ...] = (),
    ) -> None:
        super().__init__(f"{code.code} {code.name}: {cause.key}")
        self.code = code
        self.cause = cause
        self.fix = fix
        self.detail = detail
        self.commands = tuple(commands)

    @property
    def exit_code(self) -> int:
        """CLI exit code for this error (SCOPE §4.1)."""
        return self.code.exit_code


class ConfigError(ButterEyeError):
    line: int | None
    column: int | None

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.line = line
        self.column = column


class ConfigConflict(ConfigError):
    on_disk_revision: str

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        line: int | None = None,
        column: int | None = None,
        on_disk_revision: str = "",
    ) -> None:
        super().__init__(
            code, cause, fix, detail=detail, commands=commands, line=line, column=column
        )
        self.on_disk_revision = on_disk_revision


class DependencyMissing(ButterEyeError):
    """``commands`` holds the dnf lines; the core never runs dnf."""


class BlockingIssue(ButterEyeError):
    findings: tuple[Finding, ...]

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        findings: tuple[Finding, ...] = (),
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.findings = tuple(findings)


class InstanceRefused(ButterEyeError):
    reason: RefusalReason

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        reason: RefusalReason,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.reason = reason


class FilterFailed(ButterEyeError):
    rolled_back: bool

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        rolled_back: bool = False,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.rolled_back = rolled_back


class RenderRefused(ButterEyeError): ...


class InsufficientSpace(ButterEyeError):
    need_bytes: int
    free_bytes: int

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        need_bytes: int,
        free_bytes: int,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.need_bytes = need_bytes
        self.free_bytes = free_bytes


class DownloadFailed(ButterEyeError):
    url: str

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        url: str,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.url = url


class NotAvailable(ButterEyeError):
    """A feature is unavailable; ``code`` = NOT_IMPLEMENTED or the dependency code."""

    feature: Feature
    state: CapState

    def __init__(
        self,
        code: ErrorCode,
        cause: Msg,
        fix: Msg | None = None,
        *,
        detail: str | None = None,
        commands: tuple[str, ...] = (),
        feature: Feature,
        state: CapState,
    ) -> None:
        super().__init__(code, cause, fix, detail=detail, commands=commands)
        self.feature = feature
        self.state = state

    @classmethod
    def for_state(cls, feature: Feature, state: CapState) -> NotAvailable:
        """Build the error the facade raises for an unavailable ``feature``."""
        cause = state.message or Msg("This part of ButterEye isn't available.")
        return cls(
            state.code or ErrorCode.NOT_IMPLEMENTED,
            cause,
            None,
            commands=state.commands,
            feature=feature,
            state=state,
        )


class OperationCancelled(ButterEyeError):
    """The user cancelled an operation. CLI exit 130 regardless of ``code``.

    Raised with the op's own code context when a provider has one; the generic
    ``Operation`` wrapper uses ``ErrorCode.INTERNAL`` as a neutral placeholder.
    Callers must branch on the type, never on ``code``.
    """

    @property
    def exit_code(self) -> int:
        return 130


# Runtime names for annotation resolution. Imported last: types.py imports this
# module at its own end, so either import order resolves.
from buttereye.core.types import CapState, Feature, Finding, Msg, RefusalReason  # noqa: E402

__all__ = [
    "ErrorCode",
    "ButterEyeError",
    "ConfigError",
    "ConfigConflict",
    "DependencyMissing",
    "BlockingIssue",
    "InstanceRefused",
    "FilterFailed",
    "RenderRefused",
    "InsufficientSpace",
    "DownloadFailed",
    "NotAvailable",
    "OperationCancelled",
]
