# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Homebrew's install records: what ``rpm -q`` answers on Fedora, read off the disk.

macOS has no system package manager; Homebrew is where every piece ButterEye
drives comes from (mpv, VapourSynth, MVTools, ffms2, the Vulkan loader,
MoltenVK), so its Cellar is the install database the doctor has to read
(docs/PLATFORMS.md, macOS phase 2). It is read as files, never by running
``brew``, for three reasons, all measured on an arm64 Mac on 2026-10-12:

- ``brew info --json=v2 --installed`` lists everything, but it took 1.09 s warm
  and it can go to the network to refresh its API cache. SCOPE forbids both: F1
  gives the doctor 10 s "with no network access", and §2 says ButterEye makes
  "no network connections except downloads the user starts".
- ``brew --prefix <formula>`` looks like the question "is it installed", and is
  not: for a formula that is not installed it prints an error on stderr, answers
  ``<prefix>/opt/<formula>`` anyway, and exits 0.
- ``<prefix>/Cellar/<formula>/<version>/`` exists only for an installed formula,
  and ``INSTALL_RECEIPT.json`` in it carries the version, the install time (the
  ``%{INSTALLTIME}`` that ``gpufault`` splits the kernel log at) and the runtime
  dependencies. Measured on this Mac, which has 172 formulae: globbing every
  receipt 12 ms and parsing all of them 49 ms, so ``installed()`` with no
  argument answers in 70 ms and ``installed(<the ten names the doctor asks
  about>)`` in 0.3 ms - a rounding error inside the doctor's 10 s budget, with
  no subprocess, no network and no ``brew``.

``sbom.spdx.json`` next to the receipt carries the formula's own SPDX licence
expression, and the licence texts sit in the keg's root directory
(``LICENSE``, ``COPYING``, ``Copyright``, ...), which is what the licence-file
check needs; both are read only for the formulae something asks about.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from buttereye.core.types import homebrew_prefix

#: the directory under the prefix that holds one directory per installed formula
CELLAR = "Cellar"
#: the per-keg files this module reads
RECEIPT = "INSTALL_RECEIPT.json"
SBOM = "sbom.spdx.json"
#: a licence text in a keg's root directory starts with one of these (lowercased):
#: measured over the kegs here - LICENSE, LICENSE.GPL, LICENSE.txt, COPYING,
#: COPYING.LESSER, Copyright
LICENCE_PREFIXES = ("licen", "copying", "copyright", "notice")


@dataclass(frozen=True, slots=True)
class BrewKeg:
    """One installed formula: the macOS peer of ``checks_pkgs.RpmInfo``'s source.

    ``version`` is the receipt's ``source.versions.stable`` (``path`` keeps the
    keg directory, whose name adds Homebrew's revision suffix: ``0.41.0_10``).
    ``installed_at`` is the receipt's ``time``, epoch seconds, which is rpm's
    ``%{INSTALLTIME}``; None when a receipt has none.
    """

    name: str
    version: str
    path: Path
    installed_at: int | None = None
    #: ``runtime_dependencies``: (formula, version) pairs, in the receipt's order
    deps: tuple[tuple[str, str], ...] = ()

    def licence(self) -> str:
        """The formula's own SPDX expression from ``sbom.spdx.json`` ("" when it
        has none, or says NOASSERTION, or the file cannot be read).

        The SBOM lists the formula and every dependency it was built against;
        the formula's own entry is ``SPDXRef-Archive-<name>-src`` (measured: mpv
        0.41.0 answers "GPL-2.0-or-later AND LGPL-2.1-or-later" there, with 79
        further packages after it).
        """
        data = _read_json(self.path / SBOM)
        packages = data.get("packages") if isinstance(data, dict) else None
        if not isinstance(packages, list):
            return ""
        want = f"SPDXRef-Archive-{self.name}-src"
        for entry in packages:
            if not isinstance(entry, dict):
                continue
            if entry.get("SPDXID") != want and entry.get("name") != self.name:
                continue
            lic = entry.get("licenseConcluded")
            if isinstance(lic, str) and lic and lic != "NOASSERTION":
                return lic
        return ""

    def licence_files(self) -> tuple[Path, ...]:
        """The licence texts in the keg's root, rpm's ``%license`` files here."""
        try:
            entries = sorted(self.path.iterdir())
        except OSError:
            return ()
        return tuple(
            p for p in entries if p.name.lower().startswith(LICENCE_PREFIXES) and p.is_file()
        )


def prefix() -> Path:
    """Homebrew's prefix on this Mac: ``/opt/homebrew`` on Apple Silicon,
    ``/usr/local`` on Intel, or wherever this machine put it.

    Derived, never assumed - ``buttereye.core.types.homebrew_prefix`` is the one
    answer, because the generated VapourSynth scripts and ``Paths``'s packaged
    directories already have to agree with it (phase 1). It takes
    ``HOMEBREW_PREFIX`` when something exported it, and otherwise recognises a
    prefix by the ``brew`` inside it: the variable alone is not dependable,
    since an app launched from Finder inherits neither it nor a PATH that would
    find ``brew`` (measured: ``launchctl getenv PATH`` answers nothing).
    """
    return homebrew_prefix()


def cellar() -> Path:
    """The Cellar directory. It exists only where Homebrew is installed, which is
    how ``checks_pkgs`` tells "nothing installed" from "no Homebrew at all"."""
    return prefix() / CELLAR


def installed(names: Iterable[str] | None = None) -> Mapping[str, BrewKeg]:
    """The installed formulae, by name: ``names`` only, or every one of them.

    Nothing is cached here except each receipt's parsed contents, keyed by the
    file's modification time (``_parse_receipt``), so a second ``doctor`` run in
    the same GUI session costs one directory scan and sees a formula the user
    installed in between. A doctor that cached a package list for the life of
    the process would answer "not installed" forever after telling the user to
    install it and run the checks again.
    """
    base = cellar()
    if names is None:
        try:
            wanted: Iterable[str] = sorted(p.name for p in base.iterdir() if p.is_dir())
        except OSError:  # no Homebrew here, or no permission to look
            return MappingProxyType({})
    else:
        wanted = dict.fromkeys(names)  # de-duplicated, order kept
    out: dict[str, BrewKeg] = {}
    for name in wanted:
        keg = _newest_keg(base / name, name)
        if keg is not None:
            out[name] = keg
    return MappingProxyType(out)


def keg(name: str) -> BrewKeg | None:
    """One formula, or None when it is not installed."""
    return _newest_keg(cellar() / name, name)


def _newest_keg(formula_dir: Path, name: str) -> BrewKeg | None:
    """The formula's keg. More than one version can be in the Cellar at once
    (an upgrade that kept the old keg), and the newest install is the live one."""
    try:
        versions = [p for p in formula_dir.iterdir() if p.is_dir()]
    except OSError:
        return None
    kegs = [k for k in (_keg_at(p, name) for p in sorted(versions)) if k is not None]
    return max(kegs, key=lambda k: (k.installed_at or 0, k.path.name), default=None)


def _keg_at(keg_dir: Path, name: str) -> BrewKeg | None:
    receipt = keg_dir / RECEIPT
    try:
        mtime_ns = receipt.stat().st_mtime_ns
    except OSError:  # a directory in the Cellar that no install finished
        return None
    return _parse_receipt(receipt, mtime_ns, name)


@functools.lru_cache(maxsize=512)
def _parse_receipt(receipt: Path, mtime_ns: int, name: str) -> BrewKeg | None:
    """One ``INSTALL_RECEIPT.json``, memoised while the file does not change.
    ``mtime_ns`` is part of the key, not of the answer: a reinstall rewrites the
    receipt, which drops the stale entry without anything having to be cleared."""
    data = _read_json(receipt)
    if not isinstance(data, dict):
        return None
    keg_dir = receipt.parent
    source = data.get("source")
    versions = source.get("versions") if isinstance(source, dict) else None
    stable = versions.get("stable") if isinstance(versions, dict) else None
    when = data.get("time")
    deps: list[tuple[str, str]] = []
    for dep in data.get("runtime_dependencies") or ():
        if isinstance(dep, dict) and isinstance(dep.get("full_name"), str):
            deps.append((dep["full_name"], str(dep.get("pkg_version") or dep.get("version") or "")))
    return BrewKeg(
        name=name,
        # the receipt's version first, the keg directory's name as the fallback:
        # that name is always there, and is the version with Homebrew's revision
        version=str(stable) if isinstance(stable, str | int | float) and stable else keg_dir.name,
        path=keg_dir,
        installed_at=int(when) if isinstance(when, int) and when > 0 else None,
        deps=tuple(deps),
    )


def _read_json(path: Path) -> Any:
    """``path`` parsed, or None when it is missing or not JSON (a damaged or
    half-written keg must read as "not installed", never raise into the doctor)."""
    try:
        with path.open("rb") as fh:
            return json.load(fh)
    except OSError, ValueError:
        return None


__all__ = [
    "CELLAR",
    "LICENCE_PREFIXES",
    "RECEIPT",
    "SBOM",
    "BrewKeg",
    "cellar",
    "installed",
    "keg",
    "prefix",
]
