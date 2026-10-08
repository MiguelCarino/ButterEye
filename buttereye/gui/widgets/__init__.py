# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shared GUI widgets (docs/design/GUI.md §7 U5; API frozen for U6-U9).

Every widget gets an accessible name by construction, draws only from
``palette()`` and shows state as glyph + word, never colour alone.
"""

from __future__ import annotations

from buttereye.gui.widgets.banner import Action, Banner
from buttereye.gui.widgets.bench_plot import BenchPlot
from buttereye.gui.widgets.cli_hint import CliHint
from buttereye.gui.widgets.copy_field import CopyField
from buttereye.gui.widgets.drop_zone import DropZone
from buttereye.gui.widgets.fraction_edit import FractionEdit
from buttereye.gui.widgets.op_row import OpRow, format_duration, human_bytes
from buttereye.gui.widgets.state_panel import PanelState, StatePanel
from buttereye.gui.widgets.status_badge import BADGE_KINDS, BadgeKind, StatusBadge

__all__ = [
    "Action",
    "BADGE_KINDS",
    "BadgeKind",
    "Banner",
    "BenchPlot",
    "CliHint",
    "CopyField",
    "DropZone",
    "FractionEdit",
    "OpRow",
    "PanelState",
    "StatePanel",
    "StatusBadge",
    "format_duration",
    "human_bytes",
]
