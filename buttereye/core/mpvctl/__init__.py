# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Live control of mpv (SCOPE §4.2, §4.3, §4.10, §4.11; GUI.md §11.4).

``ipc`` is the JSON-IPC client and the single command allowlist, ``launcher``
starts mpv, ``decide`` turns observed properties into a filter plan, ``health``
detects stalls and drops, and ``session`` is the provider the facade calls.
"""
