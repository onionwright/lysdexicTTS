"""A single global panic-stop hotkey, live only while audio is playing.

Transport is on-screen buttons by design. This is the one exception: if a read
starts by accident -- a stray drag over forty pages -- reaching for the mouse is
strictly worse than one key. It is registered when playback starts and released
the moment it stops, so it holds a global key for the shortest possible time.

Deliberately **not** bare Escape. Registering Escape globally would swallow it in
every other application for the whole duration of playback, breaking dialogs,
editors and games. The default keeps the Escape mnemonic behind modifiers.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from typing import Callable, Optional, Tuple

from PySide6.QtCore import QAbstractNativeEventFilter

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
HOTKEY_ID = 0xC0DE

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}

_KEYS = {
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "tab": 0x09,
    "enter": 0x0D, "return": 0x0D, "backspace": 0x08, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pause": 0x13, "insert": 0x2D,
    "pgup": 0x21, "pgdn": 0x22, "up": 0x26, "down": 0x28,
    "left": 0x25, "right": 0x27, ".": 0xBE, ",": 0xBC, "/": 0xBF,
    ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD, "\\": 0xDC, "`": 0xC0,
}
for _i in range(1, 25):
    _KEYS[f"f{_i}"] = 0x6F + _i


def parse(spec: str) -> Optional[Tuple[int, int]]:
    """Turn 'ctrl+alt+esc' into (modifiers, virtual-key). None if unparseable."""
    mods = 0
    vk = None
    for part in (p.strip().lower() for p in spec.split("+") if p.strip()):
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        else:
            return None
    if vk is None or mods == 0:
        # Requiring a modifier is intentional: a bare global key would be taken
        # away from every other application.
        return None
    return mods | MOD_NOREPEAT, vk


class StopHotkey(QAbstractNativeEventFilter):
    def __init__(self, on_trigger: Callable[[], None]) -> None:
        super().__init__()
        self.on_trigger = on_trigger
        self.registered = False
        self._spec = ""

    def register(self, spec: str) -> bool:
        if sys.platform != "win32" or self.registered:
            return self.registered
        parsed = parse(spec)
        if parsed is None:
            log.warning("stop hotkey %r is not valid (a modifier is required)", spec)
            return False
        mods, vk = parsed
        # hwnd=NULL posts WM_HOTKEY to this thread's queue, which Qt's event
        # dispatcher passes through native event filters.
        if not ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
            log.info("stop hotkey %r is already taken by another app", spec)
            return False
        self.registered = True
        self._spec = spec
        log.debug("stop hotkey registered: %s", spec)
        return True

    def unregister(self) -> None:
        if sys.platform != "win32" or not self.registered:
            return
        try:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        except Exception:
            pass
        self.registered = False

    def nativeEventFilter(self, event_type, message):
        if event_type == b"windows_generic_MSG" and self.registered:
            try:
                msg = ctypes.cast(
                    int(message), ctypes.POINTER(_MSG)
                ).contents
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.on_trigger()
                    return True, 0
            except Exception:
                log.debug("hotkey filter failed", exc_info=True)
        return False, 0


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hWnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]
