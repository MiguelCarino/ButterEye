# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Storage overview, cleanup and model listing (SCOPE §4.6, F10).

Sizes are measured with ``lstat`` and never follow symbolic links. ``clean``
only empties ButterEye's own user directories; RPM-owned locations (``/usr``)
and anything outside the user's XDG folders are never touched, and a symlink is
removed as a link, never followed.

Providers (GUI.md §1)::

    async entries(paths) -> tuple[StorageEntry, ...]
    async clean(paths, targets, progress) -> CleanResult
    async models(paths) -> tuple[ModelEntry, ...]
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from buttereye.core.doctor.checks_pkgs import MODELS_SUBDIR
from buttereye.core.doctor.text import M
from buttereye.core.events import Progress
from buttereye.core.ops import ProgressSink
from buttereye.core.types import (
    BackendId,
    CleanResult,
    CleanTarget,
    ModelEntry,
    ModelKind,
    Msg,
    OpId,
    Paths,
    StorageEntry,
    StorageKind,
)

MODEL_META = "buttereye-model.json"  # written by the model installer (manifest entry)


@dataclass(frozen=True, slots=True)
class Location:
    kind: StorageKind
    path: Path
    note: Msg
    target: CleanTarget | None
    rpm_owned: bool = False


def locations(paths: Paths) -> tuple[Location, ...]:
    return (
        Location(
            StorageKind.CONFIG, paths.config_file.parent, M("Settings, profiles and rules"), None
        ),
        Location(
            StorageKind.ENGINES,
            paths.cache_dir / "engines",
            M("TensorRT engines (rebuilt when needed)"),
            CleanTarget.ENGINES,
        ),
        Location(
            StorageKind.MODELS,
            paths.data_dir / "models",
            M("Models you downloaded or installed from a file"),
            CleanTarget.MODELS,
        ),
        Location(
            StorageKind.PLUGINS,
            paths.data_dir / "plugins",
            M("Plugins you built yourself (vstrt)"),
            CleanTarget.PLUGINS,
        ),
        Location(
            StorageKind.DOWNLOADS,
            paths.cache_dir / "downloads",
            M("Unfinished and verified downloads"),
            CleanTarget.DOWNLOADS,
        ),
        Location(
            StorageKind.JOBS,
            paths.jobs_dir,
            M("Offline render jobs: scripts, manifests, logs"),
            CleanTarget.JOBS,
        ),
        Location(
            StorageKind.LOGS,
            paths.logs_dir,
            M("ButterEye and mpv logs (old ones pruned)"),
            None,
        ),
        Location(
            StorageKind.BENCH,
            paths.bench_file,
            M("Your GPU measurements"),
            None,
        ),
        Location(
            StorageKind.RUNTIME,
            paths.runtime_dir,
            M("Live scripts and sockets (cleared at logout)"),
            None,
        ),
        Location(
            StorageKind.RPM,
            paths.rpm_plugin_dir,
            M("Plugins installed by dnf"),
            None,
            rpm_owned=True,
        ),
        Location(
            StorageKind.RPM,
            paths.rpm_data_dir,
            M("RIFE models installed by dnf"),
            None,
            rpm_owned=True,
        ),
    )


def tree_size(path: Path) -> int | None:
    """Bytes under ``path`` (lstat, symlinks not followed); 0 if missing; None
    if it can't be measured."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        est = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        stack.append(Path(e.path))
                    else:
                        total += est.st_size
        except PermissionError:
            return None
        except OSError:
            continue
    return total


def _entries_sync(paths: Paths) -> tuple[StorageEntry, ...]:
    out: list[StorageEntry] = []
    for loc in locations(paths):
        out.append(
            StorageEntry(
                kind=loc.kind,
                path=loc.path,
                bytes=tree_size(loc.path),
                deletable=loc.target is not None and not loc.rpm_owned,
                rpm_owned=loc.rpm_owned,
                note=loc.note,
                clean_target=None if loc.rpm_owned else loc.target,
            )
        )
    return tuple(out)


async def entries(paths: Paths) -> tuple[StorageEntry, ...]:
    return await asyncio.to_thread(_entries_sync, paths)


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------


def _user_roots(paths: Paths) -> tuple[Path, ...]:
    return (paths.data_dir, paths.cache_dir, paths.state_dir)


def _inside(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def _components(root: Path, target: Path) -> list[Path]:
    """The paths strictly below ``root`` down to ``target`` (inclusive)."""
    rel = target.relative_to(root)
    out: list[Path] = []
    cur = root
    for part in rel.parts:
        cur = cur / part
        out.append(cur)
    return out


def safe_target(paths: Paths, target: Path) -> Msg | None:
    """Why ``target`` must not be cleaned (None = safe).

    Lexically the target must be strictly inside a user root (data, cache,
    state) and outside every RPM root. A user root may itself be a symlink
    (e.g. ``~/.cache`` on another disk), but no component below it may be one,
    and the fully resolved target must still be strictly inside a resolved user
    root and outside every resolved RPM root (``/usr``, ``/etc``, the RPM plugin
    and data folders).
    """
    target = Path(os.path.normpath(os.path.abspath(target)))
    rpm_roots = (paths.rpm_plugin_dir, paths.rpm_data_dir, Path("/usr"), Path("/etc"))
    dnf = M("Installed by dnf; remove the package instead.")
    if any(_inside(target, r) for r in rpm_roots):
        return dnf
    roots = [r for r in _user_roots(paths) if _inside(target, r) and target != r]
    if not roots:
        return M("Outside ButterEye's own folders.")
    for root in roots:
        for comp in _components(root, target):
            try:
                st = os.lstat(comp)
            except FileNotFoundError:
                break  # nothing below a missing component, nothing to delete
            except OSError:
                return M("This folder can't be inspected; it is left alone.")
            if stat.S_ISLNK(st.st_mode):
                return M("This folder is a symbolic link; it is left alone.")
            if comp != target and not stat.S_ISDIR(st.st_mode):
                return M("Outside ButterEye's own folders.")
    real = Path(os.path.realpath(target))
    if any(_inside(real, Path(os.path.realpath(r))) for r in rpm_roots):
        return dnf
    real_roots = [Path(os.path.realpath(r)) for r in _user_roots(paths)]
    if not any(_inside(real, r) and real != r for r in real_roots):
        return M("This folder resolves outside ButterEye's own folders.")
    return None


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _remove_tree(d: Path, removed: list[Path], skipped: list[tuple[Path, Msg]]) -> int:
    """Delete everything inside ``d`` without following links; returns bytes freed.

    Works relative to directory file descriptors opened with ``O_NOFOLLOW``, so
    a folder swapped for a symlink while cleaning is unlinked as a link and
    never descended into; other mounted file systems are left alone."""
    try:
        fd = os.open(d, _DIR_FLAGS)
    except FileNotFoundError:
        return 0
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            skipped.append((d, M("This folder is a symbolic link; it is left alone.")))
        else:
            skipped.append((d, M("Can't read this folder: {why}", {"why": exc.strerror or ""})))
        return 0
    try:
        return _remove_in(fd, d, removed, skipped)
    finally:
        os.close(fd)


def _remove_in(fd: int, d: Path, removed: list[Path], skipped: list[tuple[Path, Msg]]) -> int:
    freed = 0
    try:
        dev = os.fstat(fd).st_dev
        with os.scandir(fd) as it:
            names = [e.name for e in it]
    except OSError as exc:
        skipped.append((d, M("Can't read this folder: {why}", {"why": exc.strerror or ""})))
        return 0
    for name in names:
        p = d / name
        try:
            st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except OSError:
            continue
        try:
            if stat.S_ISDIR(st.st_mode):
                if st.st_dev != dev:
                    skipped.append((p, M("Another drive is mounted here; it is left alone.")))
                    continue
                try:
                    cfd = os.open(name, _DIR_FLAGS, dir_fd=fd)
                except OSError as exc:
                    if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                        raise
                    # swapped for a link or file since the stat: remove the entry itself
                    os.unlink(name, dir_fd=fd)
                    removed.append(p)
                    continue
                try:
                    cst = os.fstat(cfd)
                    if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                        skipped.append(
                            (p, M("This folder changed while it was cleaned; it is left alone."))
                        )
                        continue
                    freed += _remove_in(cfd, p, removed, skipped)
                finally:
                    os.close(cfd)
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)  # files and symlinks (the link, never its target)
                freed += st.st_size
            removed.append(p)
        except OSError as exc:
            skipped.append((p, M("Couldn't delete: {why}", {"why": exc.strerror or ""})))
    return freed


def target_dir(paths: Paths, t: CleanTarget) -> Path:
    loc = next(loc for loc in locations(paths) if loc.target is t)
    return loc.path


async def clean(
    paths: Paths, targets: frozenset[CleanTarget], progress: ProgressSink
) -> CleanResult:
    removed: list[Path] = []
    skipped: list[tuple[Path, Msg]] = []
    freed = 0
    order = [t for t in CleanTarget if t in targets]
    for i, t in enumerate(order, 1):
        d = target_dir(paths, t)
        progress(
            Progress(
                OpId(""),
                M("Cleaning {path}", {"path": str(d)}),
                i - 1,
                len(order),
                "steps",
                None,
                None,
            )
        )
        why = safe_target(paths, d)
        if why is not None:
            skipped.append((d, why))
            continue
        freed += await asyncio.to_thread(_remove_tree, d, removed, skipped)
    progress(Progress(OpId(""), M("Done"), len(order), len(order), "steps", None, None))
    return CleanResult(freed, tuple(removed), tuple(skipped))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def _user_model(d: Path) -> ModelEntry:
    meta: dict[str, object] = {}
    try:
        raw = json.loads((d / MODEL_META).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            meta = raw
    except OSError, ValueError:
        pass
    has_onnx = any(d.glob("*.onnx"))
    backend = BackendId.RIFE_TRT if has_onnx else BackendId.RIFE_NCNN
    sha = meta.get("sha256")
    pinned = bool(meta.get("pinned", sha is not None))
    if not pinned:
        kind = ModelKind.UNPINNED
    elif has_onnx:
        kind = ModelKind.DOWNLOADED
    else:
        kind = ModelKind.EXTRA
    licence = meta.get("licence")
    return ModelEntry(
        name=d.name,
        kind=kind,
        backend=backend,
        path=d,
        installed=True,
        size_bytes=tree_size(d),
        licence=licence if isinstance(licence, str) else "unknown",
        sha256=sha if isinstance(sha, str) else None,
        removable=True,
    )


def _models_sync(paths: Paths) -> tuple[ModelEntry, ...]:
    out: list[ModelEntry] = []
    packaged = paths.rpm_data_dir / MODELS_SUBDIR
    try:
        dirs = sorted(p for p in packaged.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        dirs = []
    for d in dirs:
        out.append(
            ModelEntry(
                name=d.name,
                kind=ModelKind.PACKAGED,
                backend=BackendId.RIFE_NCNN,
                path=d,
                installed=True,
                size_bytes=tree_size(d),
                licence="MIT",
                sha256=None,
                removable=False,
            )
        )
    user = paths.data_dir / "models"
    try:
        udirs = sorted(p for p in user.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        udirs = []
    out += [_user_model(d) for d in udirs]
    return tuple(out)


async def models(paths: Paths) -> tuple[ModelEntry, ...]:
    return await asyncio.to_thread(_models_sync, paths)


__all__ = [
    "MODEL_META",
    "Location",
    "locations",
    "tree_size",
    "safe_target",
    "target_dir",
    "entries",
    "clean",
    "models",
]
