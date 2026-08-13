"""One instance only.

Not optional: two instances would mean two global mouse hooks fighting over the
same gestures, two pills appearing side by side, and two 300MB copies of the
model resident at once.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\KokoroReader.SingleInstance"
SHOW_MESSAGE = "KokoroReader.ShowPanel"
ERROR_ALREADY_EXISTS = 183
HWND_BROADCAST = 0xFFFF

_mutex = None


def acquire() -> bool:
    """True if this process is the first instance."""
    global _mutex
    if sys.platform != "win32":
        return True
    k = ctypes.windll.kernel32
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k.CreateMutexW(None, False, MUTEX_NAME)
    if not _mutex:
        return True  # can't tell; better to run than to refuse
    if ctypes.GetLastError() == ERROR_ALREADY_EXISTS:
        return False
    return True


def show_message_id() -> int:
    if sys.platform != "win32":
        return 0
    return int(ctypes.windll.user32.RegisterWindowMessageW(SHOW_MESSAGE))


def signal_existing_instance() -> None:
    """Ask the instance that is already running to show its panel."""
    if sys.platform != "win32":
        return
    msg = show_message_id()
    if msg:
        ctypes.windll.user32.PostMessageW(HWND_BROADCAST, msg, 0, 0)


def release() -> None:
    global _mutex
    if _mutex and sys.platform == "win32":
        try:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(_mutex))
        except Exception:
            pass
    _mutex = None
