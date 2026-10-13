# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""ButterEye's PySide6 front end (docs/design/GUI.md).

Imports only ``buttereye.core.api`` from the core, and only the PySide6
Essentials modules QtCore, QtGui and QtWidgets (F17).
"""

import concurrent.futures
import concurrent.futures.process
import concurrent.futures.thread


def _resolve_lazy_executors() -> None:
    """Make ``concurrent.futures.ThreadPoolExecutor`` a class again under Python
    3.15 with PySide6.

    Python 3.15 binds it (and ``ProcessPoolExecutor``) with ``lazy from .thread
    import ThreadPoolExecutor``, and once shiboken6 is imported such a binding in
    a package is never resolved: the attribute stays the ``lazy_import`` proxy.
    asyncio makes every loop's default executor from that attribute, so in this
    process every ``asyncio.to_thread`` failed with ``TypeError: 'lazy_import'
    object is not callable`` and the bridge reported it as BE-9999: the Storage
    page, Clean and the model list (measured on Fedora 45, python3 3.15.0 and
    python3-shiboken6 6.11.2, with v0.0.2's published RPM too;
    docs/PLATFORMS.md, "Python 3.15 and PySide6").

    This package is imported before any of its modules imports PySide6, so the
    plain attribute read below resolves both names the normal way. The fallback is
    for a process that imported shiboken6 first (pytest-qt does): it stores the
    very class the lazy import names, which is all resolution would have done.
    On 3.14 both names are ordinary classes and nothing changes.
    """
    for name, cls in (
        ("ThreadPoolExecutor", concurrent.futures.thread.ThreadPoolExecutor),
        ("ProcessPoolExecutor", concurrent.futures.process.ProcessPoolExecutor),
    ):
        if getattr(concurrent.futures, name) is not cls:
            setattr(concurrent.futures, name, cls)


_resolve_lazy_executors()
