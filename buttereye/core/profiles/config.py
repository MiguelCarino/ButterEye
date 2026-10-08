# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Load and save ``config.toml`` (SCOPE §4.6, §4.9; docs/design/GUI.md §1, §7 U2).

- ``load`` never writes and never raises for a bad file: invalid TOML gives the
  defaults plus a ``BE-2001`` issue with line and column; a newer
  ``schema_version`` loads read-only (``BE-2002``); a v0 file is migrated in
  memory. The revision is the sha256 of the file bytes (``""`` when there is no
  file).
- ``save`` refuses a stale ``expected_revision`` (``ConfigConflict``, ``BE-2003``),
  an invalid config (``BE-2004``) and a newer-schema file (``BE-2002``). It writes
  a ``.bak`` copy first when it replaces a v0 or unreadable file, then writes a
  0600 temporary file in the same directory, fsyncs it and renames it over
  ``config.toml`` (a symlinked ``config.toml`` is followed, so the link survives).
  Writers serialise on an ``flock`` of the config directory.
"""

from __future__ import annotations

import dataclasses
import fcntl
import hashlib
import os
import secrets
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from types import MappingProxyType

from buttereye.core.errors import ButterEyeError, ConfigConflict, ConfigError, ErrorCode
from buttereye.core.profiles import defaults, rules, schema
from buttereye.core.types import Config, ConfigIssue, ConfigLoad, Msg, Paths


def _msg(key: str, params: Mapping[str, str | int | float] | None = None) -> Msg:
    """A ``Msg`` with immutable params (it crosses the GUI bridge)."""
    return Msg(key, MappingProxyType(dict(params or {})))


BACKUP_SUFFIX = ".bak"


def revision_of(data: bytes) -> str:
    """The config revision: sha256 of the file bytes, hex."""
    return hashlib.sha256(data).hexdigest()


def backup_path(paths: Paths) -> Path:
    target = _write_target(paths.config_file)
    return target.with_name(target.name + BACKUP_SUFFIX)


def load(paths: Paths) -> ConfigLoad:
    """Read ``paths.config_file``. Never writes; never raises for bad content."""
    try:
        data = paths.config_file.read_bytes()
    except FileNotFoundError:
        return ConfigLoad(
            config=defaults.default_config(),
            exists=False,
            revision="",
            read_only=False,
            used_defaults=False,
            issues=(),
        )
    except OSError as exc:
        issue = ConfigIssue(
            ErrorCode.CONFIG_INVALID,
            _msg(
                "config.toml can't be read ({error}); defaults are in use.",
                {"error": exc.strerror or type(exc).__name__},
            ),
        )
        return ConfigLoad(
            config=defaults.default_config(),
            exists=True,
            revision="",
            read_only=False,
            used_defaults=True,
            issues=(issue,),
        )
    parsed = schema.parse_bytes(data)
    issues = list(parsed.issues)
    if not parsed.used_defaults:
        for issue in rules.validate(parsed.config):
            at = parsed.locate_field(issue.field)
            if at is not None:
                issue = ConfigIssue(issue.code, issue.message, at[0], at[1], issue.field)
            issues.append(issue)
    return ConfigLoad(
        config=parsed.config,
        exists=True,
        revision=revision_of(data),
        read_only=parsed.read_only,
        used_defaults=parsed.used_defaults,
        issues=tuple(issues),
    )


def save(paths: Paths, cfg: Config, *, expected_revision: str) -> str:
    """Atomically write ``cfg``; returns the new revision (see module doc)."""
    problems = rules.validate(cfg)
    if problems:
        raise ConfigError(
            ErrorCode.CONFIG_VALUE,
            _msg("{n} setting(s) need fixing; nothing was saved.", {"n": len(problems)}),
            _msg("Correct the marked settings, then save again."),
            detail="\n".join(f"{p.field}: {p.message.key}" for p in problems),
        )
    try:
        text = schema.dumps_config(cfg)
    except schema.TomlWriteError as exc:
        raise ConfigError(
            ErrorCode.CONFIG_VALUE,
            _msg("The setting {field} can't be written to config.toml.", {"field": exc.path}),
            detail=repr(exc.value),
        ) from exc
    new = text.encode("utf-8")
    check = schema.parse_bytes(new)
    if (
        check.used_defaults
        or any(i.code is ErrorCode.CONFIG_VALUE for i in check.issues)
        or not _reads_back_as(check.config, cfg)
    ):
        raise ButterEyeError(  # a writer bug: never write a file that reads back differently
            ErrorCode.INTERNAL,
            _msg("ButterEye produced a config.toml it can't read back; nothing was saved."),
            detail=text,
        )

    target = _write_target(paths.config_file)
    try:
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _locked(target.parent):
            current = _read_optional(target)
            on_disk = revision_of(current) if current is not None else ""
            if on_disk != expected_revision:
                raise ConfigConflict(
                    ErrorCode.CONFIG_CONFLICT,
                    _msg("config.toml changed outside ButterEye."),
                    _msg("Reload it to see the changes, or save again to overwrite them."),
                    on_disk_revision=on_disk,
                )
            if current is not None:
                old = schema.parse_bytes(current)
                if old.read_only:
                    raise ConfigError(
                        ErrorCode.CONFIG_NEWER_SCHEMA,
                        _msg("config.toml was created by a newer ButterEye and is read-only."),
                        _msg("Update ButterEye, or edit the file by hand."),
                    )
                if current == new:
                    return on_disk
                if old.needs_backup:
                    _atomic_write(target.with_name(target.name + BACKUP_SUFFIX), current)
            _atomic_write(target, new)
    except ButterEyeError:
        raise
    except OSError as exc:
        raise ButterEyeError(
            ErrorCode.INTERNAL,
            _msg(
                "config.toml could not be written ({error}); nothing was changed.",
                {"error": exc.strerror or type(exc).__name__},
            ),
            detail=f"{type(exc).__name__}: {exc}",
        ) from exc
    return revision_of(new)


def _reads_back_as(back: Config, cfg: Config) -> bool:
    """Whether the written file means exactly ``cfg``.

    Every known field must survive. ``schema_version`` is always written as the
    current version, and unknown keys the writer drops on purpose (those of a
    deleted profile or rule) are compared through the document they produce.
    """
    if dataclasses.replace(back, schema_version=cfg.schema_version, unknown=cfg.unknown) != cfg:
        return False
    return schema.to_document(back) == schema.to_document(cfg)


def _write_target(path: Path) -> Path:
    """Follow a symlinked config.toml so the link (e.g. a dotfiles repo) survives."""
    try:
        if path.is_symlink():
            return path.resolve()
    except OSError:
        pass
    return path


def _read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
