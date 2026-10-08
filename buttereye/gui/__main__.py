# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""``buttereye-gui`` and ``python -m buttereye.gui``."""

from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    from buttereye.gui.app import main as app_main

    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main())
