"""The floating selection pill.

Appears next to text you just selected, offering **Read** and **Copy**. It is a
``WS_EX_NOACTIVATE`` window, which is the whole trick: clicking it does not move
focus, so the source application keeps its selection alive and can still be read
from (by UI Automation, or by a synthetic Ctrl+C if UIA is blind there).

All positioning is done in **physical** pixels through ``SetWindowPos`` so it
lands correctly on scaled and multi-monitor setups. Where it lands is
:func:`place_pill`, kept pure and separate from the widget for the same reason
``DragDetector`` is separate from the watcher thread: five anchors times the
flip-when-there-is-no-room case is not something worth testing by hand with a
mouse.

Three dismissal rules, independent because people want different combinations
of them: a timer, a click somewhere else, and the pointer moving away. "Stay
there until I click away" is the timer off and the click rule on.
"""

from __future__ import annotations

import logging
import math
from typing import Optional, Tuple

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..win import window as winwin
from .theme import THEME

log = logging.getLogger(__name__)

Point = Tuple[int, int]
Rect = Tuple[int, int, int, int]  # left, top, right, bottom

# Where the pill sits.
ANCHOR_SELECTION = "selection"              # under the selection's own rectangle
ANCHOR_SELECTION_START = "selection_start"  # where the drag began
ANCHOR_SELECTION_END = "selection_end"      # where the drag ended
ANCHOR_MOUSE = "mouse"                      # wherever the pointer is now
ANCHOR_CORNER = "corner"                    # fixed, by the notification area

ANCHORS = (
    ANCHOR_SELECTION,
    ANCHOR_SELECTION_START,
    ANCHOR_SELECTION_END,
    ANCHOR_MOUSE,
    ANCHOR_CORNER,
)

DEFAULT_OFFSET = (12, 8)
EDGE_MARGIN = 8

# How often the pointer is checked while the pill is on screen. Deliberately a
# timer and not the mouse hook: the hook drops WM_MOUSEMOVE on purpose, because
# queueing moves at 100-500Hz is exactly the pressure that trips the 300ms
# LowLevelHooksTimeout and gets the whole hook silently unhooked -- taking
# select-to-read with it. Polling only while the pill is visible costs nothing
# and cannot endanger that.
PROXIMITY_POLL_MS = 120


def pill_qss(font_pt: int) -> str:
    """Pill stylesheet at a given text size, with padding scaled to match.

    Scaled rather than fixed because someone reading at 20pt in the panel is
    not going to enjoy an 11px button.
    """
    pad_y = max(4, round(font_pt * 0.5))
    pad_x = max(8, round(font_pt * 1.0))
    return f"""
QPushButton {{
    background: transparent;
    color: {THEME.text};
    border: none;
    padding: {pad_y}px {pad_x}px;
    border-radius: 6px;
    font-size: {font_pt}pt;
    font-weight: 600;
}}
QPushButton:hover {{ background: {THEME.button_hover}; }}
QPushButton:pressed {{ background: {THEME.button_down}; color: {THEME.accent}; }}
QLabel#pillFlash {{
    color: {THEME.accent};
    padding: {pad_y}px {pad_x}px;
    font-size: {font_pt}pt;
}}
QLabel#pillSep {{ color: {THEME.panel_border}; }}
"""


# --------------------------------------------------------------- placement


def reference_point(
    anchor: str, rect: Optional[Rect], start: Optional[Point],
    end: Optional[Point], cursor: Optional[Point],
) -> Point:
    """A point on the monitor the pill is about to appear on.

    Needed before placement, because which monitor's work area applies is
    itself a function of where the pill is going.
    """
    if anchor == ANCHOR_CORNER:
        return cursor or end or start or (0, 0)
    if anchor == ANCHOR_MOUSE:
        return cursor or end or start or (0, 0)
    if anchor == ANCHOR_SELECTION_START:
        return start or end or cursor or (0, 0)
    if anchor == ANCHOR_SELECTION and rect is not None:
        return (rect[0], rect[3])
    return end or cursor or start or (0, 0)


def place_pill(
    anchor: str,
    *,
    size: Point,
    work_area: Rect,
    rect: Optional[Rect] = None,
    start: Optional[Point] = None,
    end: Optional[Point] = None,
    cursor: Optional[Point] = None,
    offset: Point = DEFAULT_OFFSET,
    above: bool = False,
) -> Point:
    """Top-left corner for the pill, in physical pixels.

    ``rect`` is the selection's bounding rectangle from UI Automation and is
    frequently ``None`` -- Electron apps and several PDF viewers report nothing.
    The selection anchor degrades to the release point when that happens, which
    is what the pill has always done; the difference now is that it is a
    fallback rather than the only behaviour.
    """
    w, h = size
    _left, _top, right, bottom = work_area
    off_x, off_y = offset

    if anchor == ANCHOR_CORNER:
        # The work area already excludes the taskbar, so the bottom-right
        # corner of it *is* just above the notification area.
        return winwin.clamp_within(
            right - w - off_x, bottom - h - off_y, w, h, work_area, EDGE_MARGIN
        )

    if anchor == ANCHOR_SELECTION and rect is not None:
        sel_left, sel_top, _sel_right, sel_bottom = rect
        x = sel_left + off_x
        if above:
            y = sel_top - h - off_y
        else:
            y = sel_bottom + off_y
            # Flip above the selection rather than off the bottom of the screen.
            if y + h > bottom - EDGE_MARGIN:
                y = sel_top - h - off_y
        return winwin.clamp_within(x, y, w, h, work_area, EDGE_MARGIN)

    point = reference_point(anchor, rect, start, end, cursor)
    x = point[0] + off_x
    y = point[1] - h - off_y if above else point[1] + off_y
    return winwin.clamp_within(x, y, w, h, work_area, EDGE_MARGIN)


def pointer_distance(rect: Rect, point: Point) -> float:
    """Distance from ``point`` to ``rect``, zero anywhere inside it.

    Measured to the nearest edge rather than to the centre, so the threshold
    means the same thing whatever size the pill happens to be.
    """
    left, top, right, bottom = rect
    dx = max(left - point[0], 0, point[0] - right)
    dy = max(top - point[1], 0, point[1] - bottom)
    return math.hypot(dx, dy)


# ------------------------------------------------------------------ widget


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

        # --- behaviour, all pushed in from settings
        self.anchor = ANCHOR_SELECTION
        self.above = False
        self.offset: Point = DEFAULT_OFFSET
        self.auto_hide_enabled = True
        self.auto_hide_ms = auto_hide_ms
        self.hide_on_click_away = True
        self.hide_when_pointer_away = False
        self.pointer_distance_px = 220
        self.show_copy = True
        self.font_pt = 12

        # Physical rect of the pill as last placed, for the proximity check.
        # Qt reports logical pixels and the pointer arrives in physical ones.
        self._placed: Optional[Rect] = None

        self.setStyleSheet(pill_qss(self.font_pt))

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

        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.timeout.connect(self.hide)

        self._proximity = QTimer(self)
        self._proximity.setInterval(PROXIMITY_POLL_MS)
        self._proximity.timeout.connect(self._check_pointer)

    # ------------------------------------------------------------- settings

    def set_appearance(self, font_pt: int, show_copy: bool) -> None:
        self.font_pt = max(8, int(font_pt))
        self.show_copy = bool(show_copy)
        self.setStyleSheet(pill_qss(self.font_pt))
        self._reset_buttons()
        self.adjustSize()

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
        self,
        rect: Optional[Rect],
        end_xy: Point,
        start_xy: Optional[Point] = None,
    ) -> None:
        """Show the pill for a fresh selection."""
        self._reset_buttons()
        self.adjustSize()
        # UIA rects and SetWindowPos are physical pixels; Qt sizes are logical.
        w, h = winwin.physical_size(self)
        cursor = winwin.cursor_pos()

        ref = reference_point(self.anchor, rect, start_xy, end_xy, cursor)
        x, y = place_pill(
            self.anchor,
            size=(w, h),
            work_area=winwin.work_area_at(*ref),
            rect=rect,
            start=start_xy,
            end=end_xy,
            cursor=cursor,
            offset=self.offset,
            above=self.above,
        )
        self._placed = (x, y, x + w, y + h)

        winwin.show_no_activate(self)
        winwin.apply_no_activate(self)
        winwin.move_physical(self, x, y)
        winwin.raise_topmost(self)

        if self.auto_hide_enabled:
            self._auto_hide.start(self.auto_hide_ms)
        if self.hide_when_pointer_away:
            self._proximity.start()

    def _reset_buttons(self) -> None:
        self.flash_label.hide()
        self.btn_read.show()
        self.sep.setVisible(self.show_copy)
        self.btn_copy.setVisible(self.show_copy)

    def flash(self, message: str, ms: int = 900) -> None:
        """Confirm an action, then dismiss."""
        self._auto_hide.stop()
        self._proximity.stop()
        self.btn_read.hide()
        self.sep.hide()
        self.btn_copy.hide()
        self.flash_label.setText(message)
        self.flash_label.show()
        self.adjustSize()
        self._flash_timer.start(ms)

    # ----------------------------------------------------------- dismissal

    def dismiss_if_clicked_away(self, hwnd: int) -> None:
        """Hide on a press that landed anywhere but on the pill itself.

        The pill never takes focus, so a click on its own buttons still arrives
        here through the global hook; without the hwnd check, pressing Read
        would dismiss the pill out from under the press.
        """
        if not self.hide_on_click_away or not self.isVisible():
            return
        if hwnd and hwnd == self.hwnd():
            return
        self.hide()

    def _check_pointer(self) -> None:
        if not self.isVisible() or self._placed is None:
            self._proximity.stop()
            return
        if pointer_distance(self._placed, winwin.cursor_pos()) > self.pointer_distance_px:
            self.hide()

    def hideEvent(self, event):
        # Every timer stops here rather than at each call site, because the pill
        # is hidden from six different places and a proximity timer left
        # running would hide the *next* selection's pill.
        self._auto_hide.stop()
        self._flash_timer.stop()
        self._proximity.stop()
        super().hideEvent(event)

    def _on_read(self) -> None:
        self._auto_hide.stop()
        self._proximity.stop()
        self.read_clicked.emit()

    def _on_copy(self) -> None:
        self._auto_hide.stop()
        self._proximity.stop()
        self.copy_clicked.emit()

    def hwnd(self) -> int:
        try:
            return winwin.hwnd_of(self)
        except Exception:
            return 0
