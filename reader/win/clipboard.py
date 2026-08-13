"""Clipboard read with save/restore.

The clipboard is the user's, not ours. This module borrows it for a moment and
puts it back, with one deliberate limitation: **it does not attempt a perfect
restore.** Delayed-rendered formats return NULL from ``GetClipboardData``, and
OLE ``IDataObject`` formats and non-memory handles (``CF_BITMAP``,
``CF_ENHMETAFILE``, ``CF_PALETTE``) cannot be round-tripped from Python.
Attempting completeness is how you corrupt someone's clipboard, so only an
allowlist of memory-backed formats is saved and everything else is logged and
left alone.

The synthetic copy is also marked so clipboard managers and Windows Clipboard
History don't record it.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15

GMEM_MOVEABLE = 0x0002

# Formats we can faithfully save and put back.
_NAMED_ALLOW = ("HTML Format", "Rich Text Format", "PNG", "image/png")
_NUMERIC_ALLOW = (CF_UNICODETEXT, CF_TEXT, CF_DIB, CF_HDROP)

# Asking clipboard managers not to record our temporary copy.
_IGNORE_FORMATS = (
    "Clipboard Viewer Ignore",
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
)


def _u32():
    return ctypes.windll.user32


def _k32():
    return ctypes.windll.kernel32


def _declare() -> None:
    """Declare argument and return types for every call used here.

    This is not optional hygiene. Without it ctypes assumes ``c_int`` returns,
    which **truncates 64-bit HANDLEs and pointers to 32 bits** -- GlobalAlloc,
    GlobalLock and GetClipboardData all silently hand back garbage, and every
    clipboard operation fails in a way that looks like an empty clipboard.
    """
    if sys.platform != "win32":
        return
    u, k = _u32(), _k32()
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.OpenClipboard.restype = wintypes.BOOL
    u.CloseClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.restype = wintypes.BOOL
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = ctypes.c_void_p
    u.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    u.EnumClipboardFormats.argtypes = [wintypes.UINT]
    u.EnumClipboardFormats.restype = wintypes.UINT
    u.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    u.RegisterClipboardFormatW.restype = wintypes.UINT
    u.GetClipboardFormatNameW.argtypes = [
        wintypes.UINT, wintypes.LPWSTR, ctypes.c_int
    ]
    u.GetClipboardFormatNameW.restype = ctypes.c_int
    u.GetClipboardSequenceNumber.restype = wintypes.DWORD

    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k.GlobalUnlock.restype = wintypes.BOOL
    k.GlobalSize.argtypes = [ctypes.c_void_p]
    k.GlobalSize.restype = ctypes.c_size_t


_declare()


def _open(retries: int = 10, delay: float = 0.02) -> bool:
    """OpenClipboard fails while another process holds it; retry briefly."""
    for _ in range(retries):
        if _u32().OpenClipboard(None):
            return True
        time.sleep(delay)
    return False


def sequence_number() -> int:
    if sys.platform != "win32":
        return 0
    return int(_u32().GetClipboardSequenceNumber())


def get_text() -> str:
    """Read CF_UNICODETEXT. Returns '' if the clipboard holds no text."""
    if sys.platform != "win32" or not _open():
        return ""
    try:
        handle = _u32().GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _k32().GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            _k32().GlobalUnlock(ctypes.c_void_p(handle))
    except Exception:
        log.debug("clipboard read failed", exc_info=True)
        return ""
    finally:
        _u32().CloseClipboard()


def _format_name(fmt: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    n = _u32().GetClipboardFormatNameW(fmt, buf, 255)
    return buf.value if n else ""


def _allowed(fmt: int) -> bool:
    if fmt in _NUMERIC_ALLOW:
        return True
    return _format_name(fmt) in _NAMED_ALLOW


def snapshot() -> Optional[List[Tuple[int, bytes]]]:
    """Copy the allowlisted clipboard formats out. ``None`` if unavailable."""
    if sys.platform != "win32" or not _open():
        return None
    saved: List[Tuple[int, bytes]] = []
    skipped: List[str] = []
    try:
        fmt = _u32().EnumClipboardFormats(0)
        while fmt:
            if _allowed(fmt):
                try:
                    handle = _u32().GetClipboardData(fmt)
                    if handle:
                        size = _k32().GlobalSize(ctypes.c_void_p(handle))
                        ptr = _k32().GlobalLock(ctypes.c_void_p(handle))
                        if ptr and size:
                            buf = (ctypes.c_char * size)()
                            ctypes.memmove(buf, ptr, size)
                            saved.append((fmt, bytes(buf)))
                        if ptr:
                            _k32().GlobalUnlock(ctypes.c_void_p(handle))
                except Exception:
                    skipped.append(_format_name(fmt) or str(fmt))
            else:
                skipped.append(_format_name(fmt) or str(fmt))
            fmt = _u32().EnumClipboardFormats(fmt)
    finally:
        _u32().CloseClipboard()
    if skipped:
        log.debug("clipboard formats not preserved: %s", ", ".join(skipped))
    return saved


def restore(saved: Optional[List[Tuple[int, bytes]]]) -> bool:
    """Put a snapshot back. Ownership of each allocation passes to the
    clipboard, so the memory must **not** be freed here."""
    if sys.platform != "win32" or saved is None or not _open():
        return False
    try:
        _u32().EmptyClipboard()
        for fmt, data in saved:
            handle = _k32().GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                continue
            ptr = _k32().GlobalLock(ctypes.c_void_p(handle))
            if not ptr:
                continue
            ctypes.memmove(ptr, data, len(data))
            _k32().GlobalUnlock(ctypes.c_void_p(handle))
            _u32().SetClipboardData(fmt, ctypes.c_void_p(handle))
        return True
    except Exception:
        log.debug("clipboard restore failed", exc_info=True)
        return False
    finally:
        _u32().CloseClipboard()


def mark_transient() -> None:
    """Ask clipboard managers not to record the copy we are about to make."""
    if sys.platform != "win32" or not _open():
        return
    try:
        for name in _IGNORE_FORMATS:
            fmt = _u32().RegisterClipboardFormatW(name)
            if not fmt:
                continue
            handle = _k32().GlobalAlloc(GMEM_MOVEABLE, 4)
            if not handle:
                continue
            ptr = _k32().GlobalLock(ctypes.c_void_p(handle))
            if ptr:
                ctypes.memset(ptr, 0, 4)
                _k32().GlobalUnlock(ctypes.c_void_p(handle))
                _u32().SetClipboardData(fmt, ctypes.c_void_p(handle))
    except Exception:
        log.debug("could not mark clipboard as transient", exc_info=True)
    finally:
        _u32().CloseClipboard()


def set_text(text: str) -> bool:
    if sys.platform != "win32" or not _open():
        return False
    try:
        _u32().EmptyClipboard()
        data = ctypes.create_unicode_buffer(text)
        size = ctypes.sizeof(data)
        handle = _k32().GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            return False
        ptr = _k32().GlobalLock(ctypes.c_void_p(handle))
        if not ptr:
            return False
        ctypes.memmove(ptr, data, size)
        _k32().GlobalUnlock(ctypes.c_void_p(handle))
        _u32().SetClipboardData(CF_UNICODETEXT, ctypes.c_void_p(handle))
        return True
    finally:
        _u32().CloseClipboard()
