# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``M(key, params)``: a ``Msg`` whose params are immutable (it crosses the GUI bridge)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from buttereye.core.types import Msg


def M(key: str, params: Mapping[str, str | int | float] | None = None) -> Msg:
    if not params:
        return Msg(key)
    return Msg(key, MappingProxyType(dict(params)))


__all__ = ["M"]
