"""Generate ``app.ico`` from the same vector source as the tray icon.

A Windows shortcut cannot use vector art -- ``IconLocation`` wants an .ico or an
icon resource -- so unlike the tray glyphs this one has to exist as a raster file
on disk. Rendering it from the same ``status_icon`` geometry keeps the geometry
itself the single source of truth, so the two cannot drift.

``app.ico`` is committed rather than ignored: it is 8 KB, output is
byte-identical between runs so regenerating never dirties the tree, and an
install that stops early still has an icon. Re-run this after changing the
speaker path in ``reader/ui/icons.py``.

    venv\\Scripts\\python.exe tools\\make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Rendering QPixmap needs a QGuiApplication, but not a visible display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPixmap  # noqa: E402

from reader.ui.icons import _path  # noqa: E402
from reader.ui.theme import THEME  # noqa: E402

SIZES = (16, 32, 48, 256)


def _render(size: int) -> QImage:
    """One tile, matching status_icon() but with the app's resting accent."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor(THEME.accent))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    p.setBrush(QColor("#ffffff"))
    inset = size * 0.22
    p.drawPath(_path("speaker", QRectF(inset, inset, size - 2 * inset, size - 2 * inset)))
    p.end()
    return pm.toImage()


def _png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _assemble_ico(tiles: "list[tuple[int, bytes]]") -> bytes:
    """Pack PNG tiles into a multi-resolution .ico.

    Qt writes only one image per .ico, which would leave Windows downscaling a
    256px tile for the 16px taskbar -- soft, and a needlessly large file. The
    container format is simple enough to write directly, and PNG-compressed
    tiles have been valid since Vista.
    """
    count = len(tiles)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, png in tiles:
        # 0 encodes 256 in the single byte the format allows.
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    return header + entries + blobs


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])  # noqa: F841
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "app.ico")
    out = os.path.normpath(out)

    tiles = [(s, _png_bytes(_render(s))) for s in SIZES]
    with open(out, "wb") as fh:
        fh.write(_assemble_ico(tiles))
    print(f"wrote {out} ({', '.join(str(s) for s in SIZES)} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
