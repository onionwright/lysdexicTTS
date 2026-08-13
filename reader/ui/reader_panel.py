"""The floating reader panel: captured text, live sentence highlight, transport.

Two highlight levels, which is the point of the panel:

* a muted wash over everything you captured, standing in for your original
  selection, and
* a distinct accent on the sentence being spoken right now, auto-scrolled into
  view.

The window never takes focus (``WS_EX_NOACTIVATE``), so clicking its buttons
cannot disturb whatever application you were reading from. That also means it is
not keyboard-focusable, which is consistent with transport being on-screen
buttons rather than hotkeys.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Sequence

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QTextCursor, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QSizeGrip,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME
from ..text.types import Sentence
from ..win import window as winwin
from .icons import IconButton
from .theme import PANEL_QSS, THEME

log = logging.getLogger(__name__)


class ReaderPanel(QWidget):
    play_pause_clicked = Signal()
    stop_clicked = Signal()
    next_clicked = Signal()
    prev_clicked = Signal()
    sentence_clicked = Signal(int)
    hidden_by_user = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_NAME)
        self.setObjectName("panelRoot")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setStyleSheet(PANEL_QSS)
        self.resize(560, 420)
        self.setMinimumSize(360, 200)

        self._sentences: List[Sentence] = []
        self._current = -1
        self._drag_offset: Optional[QPoint] = None
        # Built once per document; rebuilding it on every sentence boundary
        # would mean thousands of cursor objects per read.
        self._wash: List[QTextEdit.ExtraSelection] = []

        self._build()

    # ------------------------------------------------------------------ ui

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # --- header: title, status, transport. Always visible while reading.
        header = QWidget(self)
        header.setFixedHeight(44)
        hb = QHBoxLayout(header)
        hb.setContentsMargins(12, 0, 6, 0)
        hb.setSpacing(2)

        self.title = QLabel(APP_NAME, header)
        self.title.setObjectName("titleLabel")
        hb.addWidget(self.title)

        self.status = QLabel("", header)
        self.status.setObjectName("statusLabel")
        hb.addSpacing(10)
        hb.addWidget(self.status)
        hb.addStretch(1)

        self.btn_prev = IconButton("prev", "Previous sentence (restarts if >2s in)")
        self.btn_play = IconButton("play", "Play / pause")
        self.btn_next = IconButton("next", "Next sentence")
        self.btn_stop = IconButton("stop", "Stop")
        self.btn_close = IconButton("close", "Hide panel", size=26)
        for b in (self.btn_prev, self.btn_play, self.btn_next, self.btn_stop):
            hb.addWidget(b)
        hb.addSpacing(6)
        hb.addWidget(self.btn_close)

        self.btn_prev.clicked.connect(self.prev_clicked)
        self.btn_play.clicked.connect(self.play_pause_clicked)
        self.btn_next.clicked.connect(self.next_clicked)
        self.btn_stop.clicked.connect(self.stop_clicked)
        self.btn_close.clicked.connect(self._on_close)

        self._header = header
        outer.addWidget(header)

        # --- body
        self.text = QTextEdit(self)
        self.text.setObjectName("readerText")
        self.text.setReadOnly(True)
        self.text.setFrameShape(QTextEdit.Shape.NoFrame)
        self.text.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.text.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.text.viewport().installEventFilter(self)
        outer.addWidget(self.text, 1)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 2, 2)
        footer.addStretch(1)
        footer.addWidget(QSizeGrip(self), 0, Qt.AlignmentFlag.AlignBottom)
        outer.addLayout(footer)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(THEME.panel_bg))
        p.setPen(QColor(THEME.panel_border))
        p.drawRoundedRect(r, THEME.radius, THEME.radius)
        # Header strip, with the top corners following the panel radius.
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(THEME.header_bg))
        hr = QRectF(r.x(), r.y(), r.width(), 44)
        p.save()
        p.setClipRect(hr)
        p.drawRoundedRect(r, THEME.radius, THEME.radius)
        p.restore()
        p.setPen(QColor(THEME.panel_border))
        p.drawLine(int(r.x()), 44, int(r.right()), 44)

    # ------------------------------------------------------------- content

    def set_document(self, raw_text: str, sentences: Sequence[Sentence]) -> None:
        """Show the captured text and prepare highlighting."""
        self._sentences = list(sentences)
        self._current = -1
        self.text.setPlainText(raw_text)
        self.text.verticalScrollBar().setValue(0)
        self._build_wash()
        self._apply_highlight(-1)

    def set_sentence(self, index: int) -> None:
        if index == self._current:
            return
        self._current = index
        self._apply_highlight(index)
        self._scroll_to(index)

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def set_playing(self, playing: bool) -> None:
        self.btn_play.set_shape("pause" if playing else "play")

    def set_enabled_transport(self, enabled: bool) -> None:
        for b in (self.btn_prev, self.btn_play, self.btn_next, self.btn_stop):
            b.setEnabled(enabled)

    # ---------------------------------------------------------- highlight

    def _build_wash(self) -> None:
        """Level 1: a muted wash marking everything that will be read.

        Applied per sentence rather than as one span from first to last, so it
        doesn't bleed across blank lines, list markers, or the ragged right edge
        of wrapped lines.
        """
        self._wash = []
        doc = self.text.document()
        doc_end = doc.characterCount() - 1
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(THEME.captured_bg))
        for s in self._sentences:
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            cur = QTextCursor(doc)
            cur.setPosition(max(0, min(s.char_start, doc_end)))
            cur.setPosition(
                max(0, min(s.char_end, doc_end)), QTextCursor.MoveMode.KeepAnchor
            )
            sel.cursor = cur
            self._wash.append(sel)

    def _apply_highlight(self, index: int) -> None:
        selections: List[QTextEdit.ExtraSelection] = list(self._wash)
        doc_end = self.text.document().characterCount() - 1

        # Level 2: the sentence being spoken, painted on top.
        if 0 <= index < len(self._sentences):
            s = self._sentences[index]
            speaking = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(QColor(THEME.speaking_bg))
            fmt.setForeground(QColor(THEME.speaking_fg))
            speaking.format = fmt
            cur = QTextCursor(self.text.document())
            cur.setPosition(max(0, min(s.char_start, doc_end)))
            cur.setPosition(
                max(0, min(s.char_end, doc_end)), QTextCursor.MoveMode.KeepAnchor
            )
            speaking.cursor = cur
            selections.append(speaking)

        # setExtraSelections is O(1)-ish and rebuilds no document structure,
        # which is what keeps an 800-sentence selection responsive.
        self.text.setExtraSelections(selections)

    def _scroll_to(self, index: int) -> None:
        if not (0 <= index < len(self._sentences)):
            return
        doc_end = self.text.document().characterCount() - 1
        s = self._sentences[index]
        cur = QTextCursor(self.text.document())
        cur.setPosition(max(0, min(s.char_start, doc_end)))
        self.text.setTextCursor(cur)
        self.text.ensureCursorVisible()

    # -------------------------------------------------------------- events

    def eventFilter(self, obj, event):
        """Click a sentence in the panel to jump to it."""
        from PySide6.QtCore import QEvent

        if obj is self.text.viewport() and event.type() == QEvent.Type.MouseButtonPress:
            pos = self.text.cursorForPosition(event.position().toPoint()).position()
            for i, s in enumerate(self._sentences):
                if s.char_start <= pos < s.char_end:
                    self.sentence_clicked.emit(i)
                    break
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= self._header.height()
        ):
            self._drag_offset = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def _on_close(self) -> None:
        self.hide()
        self.hidden_by_user.emit()

    # --------------------------------------------------------------- show

    def show_floating(self) -> None:
        """Show without stealing focus from whatever the user was reading."""
        if not self.isVisible():
            self._place_default()
        winwin.show_no_activate(self)
        winwin.apply_no_activate(self)
        winwin.raise_topmost(self)

    def _place_default(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(
            area.right() - self.width() - 24,
            area.bottom() - self.height() - 24,
        )
