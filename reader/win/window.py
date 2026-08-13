"""No-activate, always-on-top window plumbing.

``WS_EX_NOACTIVATE`` is the single most important flag in this application. It
is what lets you click **Read** on the floating pill without the source
application losing focus -- which is what keeps your text selection alive long
enough to read it. Without it, clicking the pill would deselect the very text
you asked it to speak.

Qt's ``WindowDoesNotAcceptFocus`` maps to the same flag, but the mapping has
shifted across Qt versions, so the native styles are applied directly as well.
Mouse messages are still delivered to a NOACTIVATE window, so buttons and
scrolling work normally; the window simply never takes focus. The consequence is
that such a window is not keyboard-focusable.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Tuple

log = logging.getLogger(__name__)

GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080  # also removes it from Alt-Tab and the taskbar
WS_EX_TOPMOST = 0x00000008

SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

MONITOR_DEFAULTTONEAREST = 2

_IS_WIN = sys.platform == "win32"


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _user32():
    return ctypes.windll.user32


def _get_set_long():
    """GetWindowLongPtrW/SetWindowLongPtrW where available (64-bit safe)."""
    u = _user32()
    get = getattr(u, "GetWindowLongPtrW", None) or u.GetWindowLongW
    put = getattr(u, "SetWindowLongPtrW", None) or u.SetWindowLongW
    get.restype = ctypes.c_ssize_t
    get.argtypes = [ctypes.c_void_p, ctypes.c_int]
    put.restype = ctypes.c_ssize_t
    put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    return get, put


def hwnd_of(widget) -> int:
    """Force native window creation and return the HWND."""
    return int(widget.winId())


def apply_no_activate(widget, *, tool_window: bool = True) -> None:
    """Make ``widget`` never steal focus and always float on top."""
    if not _IS_WIN:
        return
    try:
        hwnd = hwnd_of(widget)
        get, put = _get_set_long()
        ex = get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        ex |= WS_EX_NOACTIVATE | WS_EX_TOPMOST
        if tool_window:
            ex |= WS_EX_TOOLWINDOW
        put(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)
    except Exception:
        log.debug("apply_no_activate failed", exc_info=True)


def show_no_activate(widget) -> None:
    """Show without activating. Never use ``SetForegroundWindow`` here."""
    if not _IS_WIN:
        widget.show()
        return
    try:
        widget.setAttribute(_wa_show_without_activating(), True)
    except Exception:
        pass
    widget.show()
    try:
        hwnd = hwnd_of(widget)
        _user32().ShowWindow(ctypes.c_void_p(hwnd), SW_SHOWNOACTIVATE)
        _user32().SetWindowPos(
            ctypes.c_void_p(hwnd), ctypes.c_void_p(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
    except Exception:
        log.debug("show_no_activate failed", exc_info=True)


def _wa_show_without_activating():
    from PySide6.QtCore import Qt

    return Qt.WidgetAttribute.WA_ShowWithoutActivating


def raise_topmost(widget) -> None:
    """Re-assert topmost so other always-on-top windows don't bury us."""
    if not _IS_WIN:
        widget.raise_()
        return
    try:
        _user32().SetWindowPos(
            ctypes.c_void_p(hwnd_of(widget)), ctypes.c_void_p(HWND_TOPMOST),
            0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def move_physical(widget, x: int, y: int) -> None:
    """Position in **physical** pixels, bypassing Qt's logical coordinates."""
    if not _IS_WIN:
        widget.move(x, y)
        return
    try:
        _user32().SetWindowPos(
            ctypes.c_void_p(hwnd_of(widget)), ctypes.c_void_p(HWND_TOPMOST),
            int(x), int(y), 0, 0, SWP_NOSIZE | SWP_NOACTIVATE,
        )
    except Exception:
        widget.move(x, y)


def work_area_at(x: int, y: int) -> Tuple[int, int, int, int]:
    """Work area (excluding the taskbar) of the monitor under a physical point."""
    if not _IS_WIN:
        return (0, 0, 1920, 1080)
    try:
        u = _user32()
        u.MonitorFromPoint.restype = ctypes.c_void_p
        u.MonitorFromPoint.argtypes = [_POINT, ctypes.c_ulong]
        mon = u.MonitorFromPoint(_POINT(int(x), int(y)), MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        u.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
        if u.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(info)):
            r = info.rcWork
            return (r.left, r.top, r.right, r.bottom)
    except Exception:
        log.debug("work_area_at failed", exc_info=True)
    return (0, 0, 1920, 1080)


def physical_size(widget) -> Tuple[int, int]:
    """Widget size in **physical** pixels.

    Qt reports logical pixels, so on a 200%-scaled display a 120px-wide widget
    actually occupies 240 physical pixels. Comparing the logical number against
    a physical monitor rect is the bug that lets a window hang off the edge of
    a scaled screen.
    """
    try:
        ratio = float(widget.devicePixelRatioF())
    except Exception:
        ratio = 1.0
    return int(round(widget.width() * ratio)), int(round(widget.height() * ratio))


def clamp_to_work_area(x: int, y: int, w: int, h: int, margin: int = 8):
    """Keep a window fully on screen."""
    left, top, right, bottom = work_area_at(x, y)
    x = max(left + margin, min(x, right - w - margin))
    y = max(top + margin, min(y, bottom - h - margin))
    return int(x), int(y)
