# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Benchmark (SCOPE F14, §5.3): headless vspipe + in-mpv throughput on generated clips.

``runner`` is the facade provider (GUI.md §11.5); ``measure`` runs and parses the
subprocesses; ``store`` keeps ``$XDG_STATE_HOME/buttereye/bench.json`` (§4.6);
``faults`` asks the doctor's GPU-fault scanner for new Xid lines. Every script
runs out of process (§8.1): this package never imports vapoursynth.
"""
