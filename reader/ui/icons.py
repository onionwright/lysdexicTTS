"""Vector glyphs drawn with QPainter.

Deliberately not font glyphs or image files: emoji rendering varies by Windows
build and shipping .png assets means DPI variants. These are a dozen lines of
path geometry that stay crisp at any scale.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QAbstractButton

from .theme import THEME


def _path(shape: str, r: QRectF) -> QPainterPath:
    p = QPainterPath()
    w, h = r.width(), r.height()
    x, y = r.x(), r.y()
    bar = w * 0.16

    if shape == "play":
        p.moveTo(x + w * 0.24, y)
        p.lineTo(x + w * 0.92, y + h / 2)
        p.lineTo(x + w * 0.24, y + h)
        p.closeSubpath()
    elif shape == "pause":
        p.addRect(QRectF(x + w * 0.20, y, bar * 1.35, h))
        p.addRect(QRectF(x + w * 0.60, y, bar * 1.35, h))
    elif shape == "stop":
        p.addRoundedRect(QRectF(x + w * 0.18, y + h * 0.08, w * 0.64, h * 0.84), 2, 2)
    elif shape == "next":
        p.moveTo(x + w * 0.12, y)
        p.lineTo(x + w * 0.70, y + h / 2)
        p.lineTo(x + w * 0.12, y + h)
        p.closeSubpath()
        p.addRect(QRectF(x + w * 0.74, y, bar, h))
    elif shape == "prev":
        p.moveTo(x + w * 0.88, y)
        p.lineTo(x + w * 0.30, y + h / 2)
        p.lineTo(x + w * 0.88, y + h)
        p.closeSubpath()
        p.addRect(QRectF(x + w * 0.10, y, bar, h))
    elif shape == "close":
        t = w * 0.14
        p.moveTo(x, y + t)
        p.lineTo(x + t, y)
        p.lineTo(x + w / 2, y + h / 2 - t)
        p.lineTo(x + w - t, y)
        p.lineTo(x + w, y + t)
        p.lineTo(x + w / 2 + t, y + h / 2)
        p.lineTo(x + w, y + h - t)
        p.lineTo(x + w - t, y + h)
        p.lineTo(x + w / 2, y + h / 2 + t)
        p.lineTo(x + t, y + h)
        p.lineTo(x, y + h - t)
        p.lineTo(x + w / 2 - t, y + h / 2)
        p.closeSubpath()
    elif shape == "speaker":
        p.moveTo(x + w * 0.08, y + h * 0.36)
        p.lineTo(x + w * 0.30, y + h * 0.36)
        p.lineTo(x + w * 0.55, y + h * 0.10)
        p.lineTo(x + w * 0.55, y + h * 0.90)
        p.lineTo(x + w * 0.30, y + h * 0.64)
        p.lineTo(x + w * 0.08, y + h * 0.64)
        p.closeSubpath()
        p.addEllipse(QPointF(x + w * 0.62, y + h * 0.5), w * 0.14, h * 0.14)
        p.addEllipse(QPointF(x + w * 0.62, y + h * 0.5), w * 0.28, h * 0.28)
    elif shape in ("noise", "noise_off"):
        # An equaliser-style set of bars: reads as "background sound" without
        # being confusable with the transport controls beside it.
        heights = (0.42, 0.78, 1.0, 0.62)
        bar_w = w * 0.15
        gap = (w - bar_w * len(heights)) / (len(heights) - 1)
        for i, rel in enumerate(heights):
            bar_h = h * rel
            p.addRoundedRect(
                QRectF(
                    x + i * (bar_w + gap),
                    y + (h - bar_h) / 2.0,
                    bar_w,
                    bar_h,
                ),
                bar_w * 0.45,
                bar_w * 0.45,
            )
        if shape == "noise_off":
            # A slash through it, drawn as a thin rotated quad so it stays
            # crisp at any size.
            t = w * 0.11
            p.moveTo(x - t * 0.2, y + h * 0.02)
            p.lineTo(x + t * 0.8, y - t * 0.2)
            p.lineTo(x + w + t * 0.2, y + h - t * 0.1)
            p.lineTo(x + w - t * 0.6, y + h + t * 0.2)
            p.closeSubpath()
    elif shape == "gear":
        cx, cy = x + w / 2.0, y + h / 2.0
        r_out, r_in, r_hole = w * 0.50, w * 0.34, w * 0.17
        teeth = 8
        points = []
        for i in range(teeth * 2):
            angle = math.pi * i / teeth
            radius = r_out if i % 2 == 0 else r_in
            points.append(
                (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
            )
        p.moveTo(*points[0])
        for point in points[1:]:
            p.lineTo(*point)
        p.closeSubpath()
        # Default odd-even fill turns this into the gear's centre hole.
        p.addEllipse(QPointF(cx, cy), r_hole, r_hole)
    elif shape == "copy":
        p.addRoundedRect(QRectF(x + w * 0.06, y, w * 0.62, h * 0.78), 2, 2)
        p.addRoundedRect(QRectF(x + w * 0.32, y + h * 0.22, w * 0.62, h * 0.78), 2, 2)
    return p


class IconButton(QAbstractButton):
    """Flat, hover-lit button that paints a vector glyph."""

    def __init__(self, shape: str, tooltip: str = "", size: int = 30, parent=None):
        super().__init__(parent)
        self.shape_name = shape
        self._glyph = max(9, int(size * 0.42))
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if tooltip:
            self.setToolTip(tooltip)

    def set_shape(self, shape: str) -> None:
        if shape != self.shape_name:
            self.shape_name = shape
            self.update()

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()

        if not self.isEnabled():
            color = QColor(THEME.text_dim)
            color.setAlpha(90)
        elif self.isDown():
            p.setBrush(QColor(THEME.button_down))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(rect, 6, 6)
            color = QColor(THEME.accent)
        elif self.underMouse():
            p.setBrush(QColor(THEME.button_hover))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawRoundedRect(rect, 6, 6)
            color = QColor(THEME.text)
        else:
            color = QColor(THEME.text_dim)

        g = self._glyph
        box = QRectF(
            (rect.width() - g) / 2.0, (rect.height() - g) / 2.0, float(g), float(g)
        )
        p.setBrush(color)
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(_path(self.shape_name, box))

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)


def app_icon(size: int = 64) -> QIcon:
    """Tray/window icon, rendered at a few sizes."""
    icon = QIcon()
    for s in (16, 24, 32, 48, size):
        pm = QPixmap(s, s)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(QColor(THEME.accent))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(0, 0, s, s), s * 0.24, s * 0.24)
        p.setBrush(QColor("#ffffff"))
        inset = s * 0.22
        p.drawPath(_path("speaker", QRectF(inset, inset, s - 2 * inset, s - 2 * inset)))
        p.end()
        icon.addPixmap(pm)
    return icon


def status_icon(color: str, size: int = 32) -> QIcon:
    """Tray icon tinted to signal state (loading / ready / degraded)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.setBrush(QColor(color))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.24, size * 0.24)
    p.setBrush(QColor("#ffffff"))
    inset = size * 0.22
    p.drawPath(_path("speaker", QRectF(inset, inset, size - 2 * inset, size - 2 * inset)))
    p.end()
    icon = QIcon()
    icon.addPixmap(pm)
    return icon
