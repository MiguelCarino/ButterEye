# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Per-file logs the user can copy: what ButterEye did with one playing video
or one conversion, plus the end of that player's or conversion's own log.

Log records about one item carry its key (``session:<sid>`` or ``job:<id>``) in
``extra``; ``FileLogBook`` keeps the latest lines per key in memory. The tool's
own log (mpv's ``mpv-<sid>.log``, vspipe and ffmpeg's ``render-<id>.log``) is
read from disk only when the user asks.
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import Any

#: the ``extra`` attribute carrying an item key
ITEM_ATTR = "be_item"
#: lines kept per item, and items kept (oldest dropped first)
MAX_LINES = 400
MAX_ITEMS = 64
#: how much of a tool's log is read and shown
TOOL_TAIL_LINES = 200
TOOL_TAIL_BYTES = 256 * 1024


def session_key(sid: str) -> str:
    return f"session:{sid}"


def job_key(job: str) -> str:
    return f"job:{job}"


class ItemLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A logger whose records belong to one item."""

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> tuple[Any, Any]:
        extra = dict(kwargs.get("extra") or {})
        extra.update(self.extra or {})
        kwargs["extra"] = extra
        return msg, kwargs


def for_item(logger: logging.Logger, key: str) -> ItemLogger:
    return ItemLogger(logger, {ITEM_ATTR: key})


class FileLogBook(logging.Handler):
    """The latest log lines per item, in memory (never written anywhere)."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self._lines: OrderedDict[str, deque[str]] = OrderedDict()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        key = getattr(record, ITEM_ATTR, None)
        if not isinstance(key, str):
            return
        try:
            line = self.format(record)
        except Exception:  # a broken record must never break the caller
            return
        lines = self._lines.get(key)
        if lines is None:
            lines = self._lines[key] = deque(maxlen=MAX_LINES)
            while len(self._lines) > MAX_ITEMS:
                self._lines.popitem(last=False)
        else:
            self._lines.move_to_end(key)
        lines.append(line)

    def lines(self, key: str) -> list[str]:
        return list(self._lines.get(key, ()))


def tail(
    path: Path, *, lines: int = TOOL_TAIL_LINES, max_bytes: int = TOOL_TAIL_BYTES
) -> list[str]:
    """The last ``lines`` lines of a text file (reading at most ``max_bytes``);
    empty when it can't be read. Carriage-return progress lines collapse."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    out = [ln.rsplit("\r", 1)[-1] for ln in text.split("\n")]
    if size > max_bytes and out:
        out = out[1:]  # the first line is cut
    return [ln for ln in out if ln.strip()][-lines:]


def compose(
    *,
    version: str,
    title: str,
    facts: Iterable[tuple[str, str]],
    own: list[str],
    tool_name: str,
    tool_path: Path,
    tool: list[str],
) -> str:
    """The text that goes on the clipboard."""
    out = [f"ButterEye {version} — log for {title}"]
    out += [f"{k}: {v}" for k, v in facts if v]
    out.append(f"Copied: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    out += ["", "== ButterEye =="]
    out += own or ["(nothing logged for this file yet)"]
    out += ["", f"== {tool_name} ({tool_path.name}, last {len(tool)} lines) =="]
    out += tool or ["(no log yet)"]
    return "\n".join(out) + "\n"


__all__ = [
    "ITEM_ATTR",
    "FileLogBook",
    "ItemLogger",
    "compose",
    "for_item",
    "job_key",
    "session_key",
    "tail",
]
