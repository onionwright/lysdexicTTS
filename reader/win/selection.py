"""Turning raw mouse events into "the user just selected some text".

Two pieces: a pure state machine (``DragDetector``) that is testable without
Windows, and a worker thread that drains the hook, runs the detector, and does
the UI Automation probe off the GUI thread -- a UIA call is cross-process COM
and can block for seconds, so it must never run where it could freeze the UI.
"""

from __future__ import annotations

import logging
import math
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence, Set, Tuple

from PySide6.QtCore import QThread, Signal

from . import hook as hookmod
from .hook import (
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_RBUTTONDOWN,
    MouseHook,
    double_click_slop,
    double_click_time_ms,
)
from .uia import Rect, UiaProbe

log = logging.getLogger(__name__)

# How the pill decides whether to appear.
MODE_AGGRESSIVE = "aggressive"  # show even when UIA can't confirm a selection
MODE_UIA_ONLY = "uia_only"      # only when UIA actually reports selected text
MODE_MODIFIER = "modifier"      # only while a modifier is held
MODE_OFF = "off"

VK_CONTROL = 0x11


@dataclass(slots=True)
class Gesture:
    kind: str  # 'drag' | 'double' | 'triple'
    x: int
    y: int
    hwnd: int
    # Where the button went *down*. A selection has two ends and people reach
    # for either: the pill can be anchored to where you started dragging rather
    # than where you stopped. Equal to (x, y) for double and triple clicks.
    x0: int = 0
    y0: int = 0


@dataclass(slots=True)
class SelectionCandidate:
    """What the watcher hands to the UI.

    ``text`` may be empty in aggressive mode: the gesture looked like a
    selection but UI Automation could not confirm it. The pill still appears,
    and pressing Read falls back to a synthetic Ctrl+C -- which is deliberately
    deferred until the user actually asks, so a passive probe never touches the
    clipboard.
    """

    gen: int
    text: str
    rect: Optional[Rect]
    x: int
    y: int
    hwnd: int
    source: str  # 'text-pattern' | 'value-pattern' | 'gesture'
    process: str
    # Where the selection gesture began, for anchoring the pill there.
    start_x: int = 0
    start_y: int = 0


class DragDetector:
    """Pure state machine over button events. No Windows calls at feed time."""

    def __init__(
        self,
        min_px: int = 12,
        min_ms: int = 60,
        double_ms: Optional[int] = None,
        slop: Optional[Tuple[int, int]] = None,
        ignore_injected: bool = True,
    ) -> None:
        self.min_px = min_px
        self.min_ms = min_ms
        # Synthetic input is ignored by default so automation tools (and our own
        # Ctrl+C) can't trigger a selection. Some Remote Desktop clients and
        # input remappers flag *real* input as injected, though, so this has to
        # be switchable.
        self.ignore_injected = ignore_injected
        # Honour the user's actual mouse settings rather than hardcoding 500ms.
        self.double_ms = double_ms if double_ms is not None else double_click_time_ms()
        self.slop = slop if slop is not None else double_click_slop()

        self._down: Optional[Tuple[int, int, int]] = None
        self._last_click_t = 0
        self._last_click_pos = (0, 0)
        self._click_count = 0

    def feed(
        self, msg: int, x: int, y: int, t_ms: int, injected: bool = False
    ) -> Optional[Gesture]:
        if injected and self.ignore_injected:
            return None  # our own or another tool's synthetic input

        if msg == WM_LBUTTONDOWN:
            near = (
                abs(x - self._last_click_pos[0]) <= self.slop[0]
                and abs(y - self._last_click_pos[1]) <= self.slop[1]
            )
            if near and (t_ms - self._last_click_t) <= self.double_ms:
                self._click_count += 1
            else:
                self._click_count = 1
            self._down = (x, y, t_ms)
            return None

        if msg != WM_LBUTTONUP or self._down is None:
            return None

        dx0, dy0, t0 = self._down
        self._down = None
        dist = math.hypot(x - dx0, y - dy0)
        dt = t_ms - t0

        self._last_click_t = t_ms
        self._last_click_pos = (x, y)

        if dist >= self.min_px and dt >= self.min_ms:
            self._click_count = 0
            return Gesture("drag", x, y, 0, x0=dx0, y0=dy0)
        if self._click_count >= 3:
            return Gesture("triple", x, y, 0, x0=dx0, y0=dy0)
        if self._click_count == 2:
            return Gesture("double", x, y, 0, x0=dx0, y0=dy0)
        return None


class SelectionWatcher(QThread):
    """Drains the mouse hook and probes UI Automation on an STA thread."""

    candidate = Signal(object)  # SelectionCandidate
    hook_state_changed = Signal(bool)
    # Any real mouse press, with the top-level window under it. The pill uses
    # this to dismiss itself when you click somewhere else, which is the only
    # way to offer "stay until I click away" -- the hook is the one place that
    # sees a click landing in another application.
    mouse_pressed = Signal(int)

    def __init__(
        self,
        *,
        mode: str = MODE_AGGRESSIVE,
        probe_delay_ms: int = 120,
        min_chars: int = 1,
        ignore_classes: Sequence[str] = (),
        ignore_processes: Sequence[str] = (),
        enable_double_click: bool = True,
        enable_triple_click: bool = True,
        ignore_injected: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.mode = mode
        self.probe_delay_ms = probe_delay_ms
        self.min_chars = min_chars
        self.ignore_classes = {c.lower() for c in ignore_classes}
        self.ignore_processes = {p.lower() for p in ignore_processes}
        self.enable_double_click = enable_double_click
        self.enable_triple_click = enable_triple_click

        self.hook = MouseHook()
        self.detector = DragDetector(ignore_injected=ignore_injected)
        self._own_hwnds: Set[int] = set()
        self._running = False
        self._gen = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------- controls

    def set_own_windows(self, hwnds: Sequence[int]) -> None:
        """Our own windows must never trigger a probe."""
        with self._lock:
            self._own_hwnds = {int(h) for h in hwnds if h}

    def set_mode(self, mode: str) -> None:
        self.mode = mode

    def stop(self) -> None:
        self._running = False
        self.hook.stop()
        self.hook.wake.set()
        self.wait(3000)

    # ----------------------------------------------------------------- run

    def run(self) -> None:  # executes on the watcher thread
        if sys.platform != "win32":
            return
        probe = UiaProbe()
        probe.init()  # CoInitializeEx(STA) happens here, on this thread

        ok = self.hook.start()
        self.hook_state_changed.emit(ok)
        if not ok:
            log.error("could not install the mouse hook; select-to-read is disabled")
            return

        self._running = True
        last_installed = True
        while self._running:
            self.hook.wake.wait(0.15)
            self.hook.wake.clear()

            if self.hook.installed != last_installed:
                last_installed = self.hook.installed
                self.hook_state_changed.emit(last_installed)

            gesture = None
            pressed_at = None
            while self.hook.events:
                try:
                    msg, x, y, t_ms, injected = self.hook.events.popleft()
                except IndexError:
                    break
                if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN) and not (
                    injected and self.detector.ignore_injected
                ):
                    pressed_at = (x, y)
                g = self.detector.feed(msg, x, y, t_ms, injected)
                if g is not None:
                    gesture = g  # keep only the most recent

            # Before _handle, deliberately: that sleeps for probe_delay_ms on
            # this thread, and a pill that takes an extra 120ms to get out of
            # the way after you have clicked past it reads as broken. A fresh
            # drag both dismisses here and re-shows on its release, and the
            # re-show is later, so it wins.
            if pressed_at is not None:
                self.mouse_pressed.emit(hookmod.window_at(*pressed_at))

            if gesture is None or self.mode == MODE_OFF:
                continue
            if gesture.kind == "double" and not self.enable_double_click:
                continue
            if gesture.kind == "triple" and not self.enable_triple_click:
                continue
            try:
                self._handle(probe, gesture)
            except Exception:
                log.exception("selection probe failed")

    # ------------------------------------------------------------ internals

    def _handle(self, probe: UiaProbe, gesture: Gesture) -> None:
        hwnd = hookmod.window_at(gesture.x, gesture.y)
        with self._lock:
            if hwnd in self._own_hwnds:
                return  # a click on our own pill or panel
        cls = hookmod.window_class(hwnd).lower()
        proc = hookmod.process_name_of_window(hwnd)
        if cls in self.ignore_classes or proc in self.ignore_processes:
            return

        if self.mode == MODE_MODIFIER and not _ctrl_held():
            return

        # Applications update their selection asynchronously; probing on the
        # same tick as the button release frequently reads the previous state.
        time.sleep(self.probe_delay_ms / 1000.0)

        selection = probe.selection() if probe.available else None
        text = (selection.text if selection else "") or ""
        rect = selection.rect if selection else None
        source = selection.source if selection else "gesture"

        if text.strip() and len(text.strip()) < self.min_chars:
            return
        if not text.strip():
            if self.mode != MODE_AGGRESSIVE:
                return
            # Aggressive mode: UIA is blind here (common in Electron apps and
            # some PDF readers), so trust the gesture and let Read fall back to
            # the clipboard if the user actually asks for it.
            source = "gesture"

        with self._lock:
            self._gen += 1
            gen = self._gen

        self.candidate.emit(
            SelectionCandidate(
                gen=gen,
                text=text,
                rect=rect,
                x=gesture.x,
                y=gesture.y,
                hwnd=hwnd,
                source=source,
                process=proc,
                start_x=gesture.x0,
                start_y=gesture.y0,
            )
        )


def _ctrl_held() -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
