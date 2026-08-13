"""Per-monitor DPI awareness.

Must run **before** ``QApplication`` is constructed. Qt 6 sets this itself, but
declaring it explicitly and early makes the coordinate contract unambiguous from
process start -- which matters because this app mixes Win32 and Qt geometry.

The rule to remember: **every Win32 coordinate (hook points, UI Automation
bounding rectangles, monitor rects) is in physical pixels, while every Qt
geometry is in logical pixels.** Mixing them is the classic "the pill lands 1.5x
off on the second monitor" bug. This app resolves it by doing all window
*positioning* natively in physical pixels and letting Qt handle only painting.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)


def set_process_dpi_aware() -> bool:
    """Opt into per-monitor-v2 DPI awareness. Safe to call more than once."""
    if sys.platform != "win32":
        return False
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ):
            return True
    except Exception:
        pass
    try:  # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return True
    except Exception:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:
        log.debug("could not set DPI awareness", exc_info=True)
        return False
