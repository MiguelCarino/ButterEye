# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Core text rendering through gettext (SCOPE §2 goal 9, GUI.md ruling R5).

The core returns ``Msg(key, params)``; ``key`` is the English msgid. ``render()``
translates it with the ``buttereye`` gettext domain and fills ``{name}``
placeholders with ``str.format_map(params)``. Without a catalogue the msgid
itself is used. Pure and thread-safe after ``set_language`` (called once at start).
"""

from __future__ import annotations

import gettext
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from buttereye.core.types import Msg

DOMAIN = "buttereye"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "data" / "locale"

_log = logging.getLogger(__name__)

_translations: gettext.NullTranslations = gettext.NullTranslations()
_language: str | None = None


class _Params(dict[str, object]):
    """Missing placeholders stay visible as ``{name}`` instead of raising."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def set_language(tag: str | None) -> None:
    """Select the catalogue for ``tag`` (e.g. ``"de"``, ``"pt_BR"``); ``None`` uses
    the environment (``LANGUAGE``/``LC_ALL``/``LC_MESSAGES``/``LANG``)."""
    global _translations, _language
    languages: list[str] | None
    if tag:
        languages = [tag.replace("-", "_")]
    else:
        languages = None  # gettext reads the environment
    _translations = gettext.translation(
        DOMAIN, localedir=os.fspath(LOCALE_DIR), languages=languages, fallback=True
    )
    _language = tag


def current_language() -> str | None:
    return _language


def _format(template: str, params: Mapping[str, str | int | float]) -> str:
    if not params:
        # No params: keep literal braces untouched (a msgid may contain "{n}" only
        # when it expects params, so this is the msgid as written).
        return template
    try:
        return template.format_map(_Params(params))
    except (ValueError, IndexError, KeyError, AttributeError) as exc:
        _log.warning("bad placeholder in %r: %s", template, exc)
        return template


def render(msg: Msg) -> str:
    """Translate ``msg`` and fill its params (current language)."""
    return _format(_translations.gettext(msg.key), msg.params)


__all__ = ["DOMAIN", "LOCALE_DIR", "render", "set_language", "current_language"]
