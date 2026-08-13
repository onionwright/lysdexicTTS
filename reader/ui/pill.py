"""The floating selection pill.

Appears next to text you just selected, offering **Read** and **Copy**. It is a
``WS_EX_NOACTIVATE`` window, which is the whole trick: clicking it does not move
focus, so the source application keeps its selection alive and can still be read
from (by UI Automation, or by a synthetic Ctrl+C if UIA is blind there).

Placement prefers the selection's own bounding rectangle from UI Automation and
falls back to the cursor. All positioning is done in **physical** pixels through
``SetWindowPos`` so it lands correctly on scaled and multi-monitor setups.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..win import window as winwin
from .theme import THEME

log = logging.getLogger(__name__)

PILL_QSS = f"""
QPushButton {{
    background: transparent;
    color: {THEME.text};
    border: none;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
}}
QPushButton:hover {{ background: {THEME.button_hover}; }}
QPushButton:pressed {{ background: {THEME.button_down}; color: {THEME.accent}; }}
QLabel#pillFlash {{ color: {THEME.accent}; padding: 6px 10px; font-size: 12px; }}
QLabel#pillSep {{ color: {THEME.panel_border}; }}
"""

OFFSET_X = 12
OFFSET_Y = 14


class SelectionPill(QWidget):
    read_clicked = Signal()
    copy_clicked = Signal()

    def __init__(self, auto_hide_ms: int = 4000, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(PILL_QSS)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(2)

        self.btn_read = QPushButton("▶  Read", self)
        self.btn_read.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_read.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_read.clicked.connect(self._on_read)
        row.addWidget(self.btn_read)

        self.sep = QLabel("|", self)
        self.sep.setObjectName("pillSep")
        row.addWidget(self.sep)

        self.btn_copy = QPushButton("Copy", self)
        self.btn_copy.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_copy.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_copy.clicked.connect(self._on_copy)
        row.addWidget(self.btn_copy)

        self.flash_label = QLabel("", self)
        self.flash_label.setObjectName("pillFlash")
        self.flash_label.hide()
        row.addWidget(self.flash_label)

        self.adjustSize()

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.hide)
        self.auto_hide_ms = auto_hide_ms

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self.hide)

    # ------------------------------------------------------------- painting

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(THEME.header_bg))
        p.setPen(QColor(THEME.panel_border))
        p.drawRoundedRect(r, r.height() / 2.0, r.height() / 2.0)

    # -------------------------------------------------------------- showing

    def show_for(
        self, rect: Optional[Tuple[int, int, int, int]], cursor_xy: Tuple[int, int]
    ) -> None:
        """Show next to the selection (preferred) or the cursor (fallback)."""
        self._reset_buttons()
        self.adjustSize()
        # UIA rects and SetWindowPos are physical pixels; Qt sizes are logical.
        w, h = winwin.physical_size(self)

        if rect is not None:
            left, top, _right, bottom = rect
            x, y = left, bottom + 6
            # Flip above the selection if there is no room below it.
            _, _, _, work_bottom = winwin.work_area_at(x, y)
            if y + h > work_bottom - 8:
                y = top - h - 6
        else:
            x, y = cursor_xy[0] + OFFSET_X, cursor_xy[1] + OFFSET_Y

        x, y = winwin.clamp_to_work_area(x, y, w, h)

        winwin.show_no_activate(self)
        winwin.apply_no_activate(self)
        winwin.move_physical(self, x, y)
        winwin.raise_topmost(self)
        self._auto_hide.start(self.auto_hide_ms)

    def _reset_buttons(self) -> None:
        self.flash_label.hide()
        self.btn_read.show()
        self.sep.show()
        self.btn_copy.show()

    def flash(self, message: str, ms: int = 900) -> None:
        """Confirm an action, then dismiss."""
        self._auto_hide.stop()
        self.btn_read.hide()
        self.sep.hide()
        self.btn_copy.hide()
        self.flash_label.setText(message)
        self.flash_label.show()
        self.adjustSize()
        self._flash_timer.start(ms)

    def _on_read(self) -> None:
        self._auto_hide.stop()
        self.read_clicked.emit()

    def _on_copy(self) -> None:
        self._auto_hide.stop()
        self.copy_clicked.emit()

    def hwnd(self) -> int:
        try:
            return winwin.hwnd_of(self)
        except Exception:
            return 0
