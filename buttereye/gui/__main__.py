# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``buttereye-gui`` and ``python -m buttereye.gui``."""

from __future__ import annotations

import sys
from collections.abc import Sequence

#: Windows: Qt uses Microsoft's Visual C++ runtime, which the installer does not
#: carry (docs/PLATFORMS.md). Without it Qt fails to load, and a window started
#: from the Start menu has no console to say why, so say it in a message box.
VCREDIST_URL = "https://aka.ms/vc14/vc_redist.x64.exe"
MISSING_RUNTIME = (
    "ButterEye can't start: the Microsoft Visual C++ runtime is missing or too old.\n\n"
    "Install it from {url}\n"
    "or run:  winget install --exact --id Microsoft.VCRedist.2015+.x64\n\n"
    "Then start ButterEye again."
)


def missing_runtime_text(exc: BaseException) -> str | None:
    """The message to show when ``exc`` is Qt failing to load its DLLs on Windows."""
    if sys.platform != "win32" or not isinstance(exc, ImportError):
        return None
    if "DLL load failed" not in str(exc):
        return None
    return MISSING_RUNTIME.format(url=VCREDIST_URL)


def _message_box(text: str) -> None:
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.user32.MessageBoxW(None, text, "ButterEye", 0x10)  # MB_ICONERROR


def main(argv: Sequence[str] | None = None) -> int:
    try:
        from buttereye.gui.app import main as app_main
    except ImportError as exc:
        text = missing_runtime_text(exc)
        if text is None:
            raise
        print(f"{text}\n({exc})", file=sys.stderr)
        _message_box(text)
        return 1
    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main())
