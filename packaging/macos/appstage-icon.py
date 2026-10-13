# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Draw ButterEye's icon into a macOS .iconset for build-app.sh (iconutil makes
the .icns from it).

  appstage-icon.py <buttereye.svg> <out.iconset> <site-packages>

Drawn by the Qt the app bundles (QtSvg, offscreen), so the build needs no SVG
tool of its own - the Windows build has Fedora's rsvg-convert for this, a Mac
has nothing that reads SVG. The droplet sits inside Apple's icon grid: 824 of
1024 points, centred, the margin macOS leaves round every app icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

svg, out, site = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, site)

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

app = QGuiApplication([])
renderer = QSvgRenderer(str(svg))
if not renderer.isValid():
    sys.exit(f"{svg}: not an SVG Qt can draw")
out.mkdir(parents=True, exist_ok=True)
for points in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        px = points * scale
        img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        inner = px * 824 / 1024
        off = (px - inner) / 2
        renderer.render(p, QRectF(off, off, inner, inner))
        p.end()
        name = f"icon_{points}x{points}{'@2x' if scale == 2 else ''}.png"
        if not img.save(str(out / name)):
            sys.exit(f"could not write {name}")
print(f"icon: {len(list(out.glob('*.png')))} sizes")
