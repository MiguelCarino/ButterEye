# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Config, profiles and rules (SCOPE §4.9, F15; docs/design/GUI.md §7 U2).

- ``defaults``: the four shipped profiles and ``default_config()``.
- ``schema``: TOML text <-> ``Config`` (unknown-key capture, v0 migration, writer).
- ``config``: ``load``/``save`` of ``config.toml`` (atomic, sha256 revision, ``.bak``).
- ``rules``: ``validate()`` and the first-match ``explain()`` trace.

Pure Python, stdlib only. Never imports Qt.
"""
