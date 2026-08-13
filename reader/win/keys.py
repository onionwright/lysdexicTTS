"""Synthetic Ctrl+C via ``SendInput``.

``SendInput`` rather than the legacy ``keybd_event``: it injects the whole
sequence atomically so nothing interleaves, and it supports ``KEYEVENTF_SCANCODE``
which some DirectInput and remote-desktop clients require.

Two hazards that are easy to miss:

* Because the pill is a no-activate window, the source application keeps focus
  -- which is exactly what makes this work -- but it also means any modifier the
  user is *physically holding* is still live, so injecting Ctrl+C would actually
  send Ctrl+Shift+C. Held modifiers are released first.
* ``SendInput`` silently fails with ``ERROR_ACCESS_DENIED`` against an elevated
  foreground window (UIPI). That must be reported, not swallowed.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from typing import List

log = logging.getLogger(__name__)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12  # Alt
VK_LWIN = 0x5B
VK_RWIN = 0x5C
VK_C = 0x43

ERROR_ACCESS_DENIED = 5

_MODIFIERS = (VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN)

ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class InputBlockedError(RuntimeError):
    """SendInput was refused, almost always because the target is elevated."""


def _key(vk: int, up: bool) -> INPUT:
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if up else 0)
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return inp


def held_modifiers() -> List[int]:
    """Modifier keys the user is physically holding right now."""
    if sys.platform != "win32":
        return []
    gaks = ctypes.windll.user32.GetAsyncKeyState
    return [vk for vk in _MODIFIERS if gaks(vk) & 0x8000]


def _send(inputs: List[INPUT]) -> None:
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    user32 = ctypes.windll.user32
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error() or ctypes.GetLastError()
        if err == ERROR_ACCESS_DENIED:
            raise InputBlockedError(
                "Windows blocked the keystroke: the focused window is running "
                "elevated, and this app deliberately does not run elevated."
            )
        raise InputBlockedError(f"SendInput sent {sent}/{n} events (error {err})")


def send_ctrl_c(settle_s: float = 0.02) -> None:
    """Inject Ctrl+C into whatever currently has focus."""
    if sys.platform != "win32":
        raise InputBlockedError("not supported on this platform")

    stuck = held_modifiers()
    seq: List[INPUT] = [_key(vk, True) for vk in stuck]
    seq += [
        _key(VK_CONTROL, False),
        _key(VK_C, False),
        _key(VK_C, True),
        _key(VK_CONTROL, True),
    ]
    if stuck:
        log.debug("releasing physically-held modifiers before copy: %s", stuck)
    _send(seq)
    # Give the target a moment to service the keystroke before we read the
    # clipboard; the poll on the sequence number does the real waiting.
    time.sleep(settle_s)


def foreground_window() -> int:
    if sys.platform != "win32":
        return 0
    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p  # 64-bit safe
    return int(u.GetForegroundWindow() or 0)
