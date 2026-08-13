"""Low-level global mouse hook.

``WH_MOUSE_LL`` is used rather than raw input because ``MSLLHOOKSTRUCT`` carries
the screen coordinates, timestamp and message atomically with the event. Raw
input would force a separate ``GetCursorPos`` call afterwards, racing the user's
next movement.

Three rules here are non-negotiable, and each corresponds to a failure that is
silent rather than loud:

1. **The hook procedure must be trivial.** Windows drops a hook whose callback
   exceeds ``LowLevelHooksTimeout`` (300ms by default). The handle stays valid
   and events simply stop arriving, with no error anywhere. So the proc appends
   to a deque and returns -- no UI Automation, no clipboard, no Qt, no logging.
2. **The ctypes callback object must stay referenced.** A garbage-collected
   ``WINFUNCTYPE`` instance is the single most common bug in this kind of code
   and it manifests as an access violation with no Python traceback.
3. **Never return non-zero**, which would swallow the user's click.

A watchdog covers rule 1 failing anyway: if the cursor is moving but no hook
events have arrived for several seconds, the hook is presumed dead and
reinstalled.

Note that a non-elevated hook receives no events while an elevated window has
focus. That is accepted deliberately -- an elevated process could not
``SendInput`` into normal windows either, so elevating would break the common
case to fix the rare one.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections import deque
from ctypes import wintypes
from typing import Deque, Optional, Tuple

log = logging.getLogger(__name__)

WH_MOUSE_LL = 14

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSEWHEEL = 0x020A

LLMHF_INJECTED = 0x00000001

PM_REMOVE = 0x0001
QS_ALLINPUT = 0x04FF
WM_QUIT = 0x0012

WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t

HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

# Module-level anchor: see rule 2 above. Never let these be collected while a
# hook is installed.
_LIVE_PROCS = []


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


# (message, x, y, time_ms, injected)
MouseEvent = Tuple[int, int, int, int, bool]

WATCHDOG_SILENCE_S = 5.0


class MouseHook:
    """Installs WH_MOUSE_LL on its own message-pumping thread."""

    def __init__(self, maxlen: int = 1024) -> None:
        self.events: Deque[MouseEvent] = deque(maxlen=maxlen)
        self.wake = threading.Event()
        self.installed = False
        self.reinstalls = 0
        self.dropped = 0
        self.total_events = 0  # every message seen, including moves

        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._hook = None
        self._proc = None
        self._thread_id = 0
        self._last_event_t = 0.0
        self._last_cursor = (0, 0)
        self._last_event_count = 0
        self._suspect = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        if sys.platform != "win32" or self._thread is not None:
            return False
        self._running = True
        self._thread = threading.Thread(target=self._run, name="win-hook", daemon=True)
        self._thread.start()
        # Give the thread a moment to report whether installation succeeded.
        for _ in range(50):
            if self.installed:
                return True
            time.sleep(0.02)
        return self.installed

    def stop(self) -> None:
        self._running = False
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, WM_QUIT, 0, 0
                )
            except Exception:
                pass
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    # -------------------------------------------------------------- internals

    def _install(self) -> bool:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int, WPARAM, LPARAM
        ]
        user32.CallNextHookEx.restype = LRESULT
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p

        proc = HOOKPROC(self._hook_proc)
        self._proc = proc
        _LIVE_PROCS.append(proc)

        handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, proc, kernel32.GetModuleHandleW(None), 0
        )
        if not handle:
            err = ctypes.GetLastError()
            log.error("SetWindowsHookExW failed (error %s)", err)
            return False
        self._hook = handle
        self.installed = True
        self._last_event_t = time.monotonic()
        # Seed from the real cursor position. Leaving this at (0, 0) makes the
        # first watchdog tick see phantom movement and reinstall a hook that
        # was never dead.
        self._last_cursor = _cursor_pos()
        self._last_event_count = self.total_events
        self._suspect = 0
        log.info("global mouse hook installed")
        return True

    def _uninstall(self) -> None:
        if self._hook:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
            except Exception:
                pass
        self._hook = None
        self.installed = False
        if self._proc in _LIVE_PROCS:
            _LIVE_PROCS.remove(self._proc)
        self._proc = None

    def _hook_proc(self, code, wparam, lparam):
        # Keep this under ~50us. Anything slower risks a silent unhook.
        try:
            if code >= 0:
                msg = int(wparam)
                if msg != WM_MOUSEMOVE:
                    info = ctypes.cast(
                        lparam, ctypes.POINTER(MSLLHOOKSTRUCT)
                    ).contents
                    self.events.append(
                        (
                            msg,
                            info.pt.x,
                            info.pt.y,
                            int(info.time),
                            bool(info.flags & LLMHF_INJECTED),
                        )
                    )
                    self.wake.set()
                self._last_event_t = time.monotonic()
                self.total_events += 1
        except Exception:
            pass  # never let anything escape into the hook chain
        return ctypes.windll.user32.CallNextHookEx(None, code, wparam, lparam)

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        if not self._install():
            return

        msg = wintypes.MSG()
        try:
            while self._running:
                # The 1s timeout doubles as the watchdog tick.
                user32.MsgWaitForMultipleObjects(0, None, False, 1000, QS_ALLINPUT)
                while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                    if msg.message == WM_QUIT:
                        self._running = False
                        break
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                self._watchdog()
        finally:
            self._uninstall()
            log.info("global mouse hook removed")

    def _watchdog(self) -> None:
        """Detect a silent unhook and reinstall.

        Windows gives no notification when it drops a slow hook, so the only
        available signal is the absence of events while the cursor demonstrably
        moves. That signal races, though: a program calling ``SetCursorPos``
        jumps the pointer, and if this tick samples the new position before the
        hook has processed the corresponding event, one sample looks exactly
        like a dead hook. Requiring two consecutive suspicious ticks *with no
        events at all in between* removes the race -- a live hook always
        delivers something within a second of the pointer moving.
        """
        try:
            cursor = _cursor_pos()
            previous = self._last_cursor
            moved = cursor != previous
            self._last_cursor = cursor

            events = self.total_events
            saw_events = events != self._last_event_count
            self._last_event_count = events

            if not moved or saw_events:
                self._suspect = 0
                return
            if time.monotonic() - self._last_event_t < WATCHDOG_SILENCE_S:
                self._suspect = 0
                return

            self._suspect += 1
            if self._suspect < 2:
                log.debug(
                    "hook looks quiet (cursor %s -> %s, %.1fs silent); "
                    "waiting for confirmation",
                    previous, cursor, time.monotonic() - self._last_event_t,
                )
                return

            log.warning(
                "mouse hook is dead (cursor moved %s -> %s, no events for "
                "%.1fs across two checks); reinstalling",
                previous, cursor, time.monotonic() - self._last_event_t,
            )
            self._suspect = 0
            self._uninstall()
            if self._install():
                self.reinstalls += 1
        except Exception:
            log.debug("watchdog failed", exc_info=True)


def _cursor_pos() -> Tuple[int, int]:
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def window_at(x: int, y: int) -> int:
    """Top-level window under a physical screen point."""
    if sys.platform != "win32":
        return 0
    u = ctypes.windll.user32
    u.WindowFromPoint.argtypes = [wintypes.POINT]
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetAncestor.restype = ctypes.c_void_p
    hwnd = u.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not hwnd:
        return 0
    return int(u.GetAncestor(ctypes.c_void_p(hwnd), 2) or hwnd)  # GA_ROOT


def double_click_time_ms() -> int:
    if sys.platform != "win32":
        return 500
    return int(ctypes.windll.user32.GetDoubleClickTime())


def double_click_slop() -> Tuple[int, int]:
    if sys.platform != "win32":
        return (4, 4)
    u = ctypes.windll.user32
    return (int(u.GetSystemMetrics(36)), int(u.GetSystemMetrics(37)))


def process_name_of_window(hwnd: int) -> str:
    """Executable name owning a window, for the ignore lists."""
    if sys.platform != "win32" or not hwnd:
        return ""
    try:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(
            ctypes.c_void_p(hwnd), ctypes.byref(pid)
        )
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if k.QueryFullProcessImageNameW(
                ctypes.c_void_p(h), 0, buf, ctypes.byref(size)
            ):
                return buf.value.rsplit("\\", 1)[-1].lower()
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:
        pass
    return ""


def window_class(hwnd: int) -> str:
    if sys.platform != "win32" or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(ctypes.c_void_p(hwnd), buf, 255)
    return buf.value
