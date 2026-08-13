"""Reading the current selection through UI Automation.

This is the preferred path because it has **no side effects at all** -- no
clipboard involvement, no synthetic keystrokes. It works in Word, Acrobat,
Chromium-based browsers, WPF/UWP apps and Win32 edit controls.

``CUIAutomation8`` is used specifically rather than the v1 ``CUIAutomation``,
because only ``IUIAutomation2`` and later expose ``ConnectionTimeout`` and
``TransactionTimeout`` -- the only in-band way to bound a cross-process call
into an application that is hung. Everything here still has to run off the GUI
thread: a UIA call is COM marshalling into another process and can block for
seconds regardless.

Every object created here belongs to the thread that created it and must never
be passed to another thread.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# Fallbacks only: the real values are read from the generated typelib module in
# init(). (Note 10000 is InvokePatternId, not TextPattern -- an easy mix-up.)
UIA_TextPatternId = 10014
UIA_ValuePatternId = 10002

COINIT_APARTMENTTHREADED = 0x2

Rect = Tuple[int, int, int, int]  # left, top, right, bottom (physical pixels)


@dataclass(slots=True)
class UiaSelection:
    text: str
    rect: Optional[Rect]
    source: str  # 'text-pattern' | 'value-pattern' | 'name'


class UiaProbe:
    """Wraps one UI Automation client. Create and use on a single STA thread."""

    def __init__(
        self,
        connection_timeout_ms: int = 1000,
        transaction_timeout_ms: int = 2000,
    ) -> None:
        self._uia = None
        self._mod = None
        self._ok = False
        self.text_pattern_id = UIA_TextPatternId
        self.value_pattern_id = UIA_ValuePatternId
        self.connection_timeout_ms = connection_timeout_ms
        self.transaction_timeout_ms = transaction_timeout_ms

    # ------------------------------------------------------------- startup

    def init(self) -> bool:
        """Initialize COM (STA) and create the automation client."""
        if self._ok:
            return True
        if sys.platform != "win32":
            return False
        try:
            import comtypes
            import comtypes.client

            try:
                comtypes.CoInitializeEx(COINIT_APARTMENTTHREADED)
            except Exception:
                pass  # already initialized on this thread

            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as U

            self._mod = U
            # Prefer the typelib's own constants over our literals.
            self.text_pattern_id = int(
                getattr(U, "UIA_TextPatternId", UIA_TextPatternId)
            )
            self.value_pattern_id = int(
                getattr(U, "UIA_ValuePatternId", UIA_ValuePatternId)
            )
            try:
                self._uia = comtypes.client.CreateObject(
                    U.CUIAutomation8, interface=U.IUIAutomation2
                )
                # The whole point of choosing CUIAutomation8: bounded waits.
                self._uia.ConnectionTimeout = self.connection_timeout_ms
                self._uia.TransactionTimeout = self.transaction_timeout_ms
            except Exception:
                log.debug("CUIAutomation8 unavailable; falling back to v1")
                self._uia = comtypes.client.CreateObject(
                    U.CUIAutomation, interface=U.IUIAutomation
                )
            self._ok = True
            log.info("UI Automation client ready")
        except Exception:
            log.exception("UI Automation unavailable; selection will use clipboard only")
            self._ok = False
        return self._ok

    @property
    def available(self) -> bool:
        return self._ok

    # ------------------------------------------------------------ querying

    def selection(self, retry: bool = True) -> Optional[UiaSelection]:
        """Return the focused element's selected text, or ``None``.

        Chromium and Electron build their accessibility tree lazily on first
        client access, so a first probe often comes back empty and the second
        works. That is what ``retry`` is for.
        """
        if not self._ok and not self.init():
            return None
        result = self._selection_once()
        if result is None and retry:
            result = self._selection_once()
        return result

    def _selection_once(self) -> Optional[UiaSelection]:
        U = self._mod
        try:
            element = self._uia.GetFocusedElement()
        except Exception:
            log.debug("GetFocusedElement failed", exc_info=True)
            return None
        if not element:
            return None

        # 1. TextPattern -- the real answer when it exists.
        try:
            pattern = element.GetCurrentPattern(self.text_pattern_id)
            if pattern:
                tp = pattern.QueryInterface(U.IUIAutomationTextPattern)
                ranges = tp.GetSelection()
                if ranges and ranges.Length > 0:
                    rng = ranges.GetElement(0)
                    text = rng.GetText(-1) or ""
                    if text.strip():
                        return UiaSelection(text, _range_rect(rng), "text-pattern")
        except Exception:
            log.debug("TextPattern selection failed", exc_info=True)

        # 2. ValuePattern -- Win32 edit controls with everything selected.
        try:
            pattern = element.GetCurrentPattern(self.value_pattern_id)
            if pattern:
                vp = pattern.QueryInterface(U.IUIAutomationValuePattern)
                text = vp.CurrentValue or ""
                if text.strip():
                    return UiaSelection(text, _element_rect(element), "value-pattern")
        except Exception:
            log.debug("ValuePattern read failed", exc_info=True)

        return None

    def focused_rect(self) -> Optional[Rect]:
        if not self._ok:
            return None
        try:
            return _element_rect(self._uia.GetFocusedElement())
        except Exception:
            return None


def _range_rect(rng) -> Optional[Rect]:
    """Union of a text range's bounding rectangles, in physical pixels.

    ``GetBoundingRectangles`` returns a flat array of doubles in groups of four:
    (left, top, width, height) -- note width/height, not right/bottom.
    """
    try:
        raw = rng.GetBoundingRectangles()
    except Exception:
        return None
    try:
        vals: List[float] = [float(v) for v in raw]
    except Exception:
        return None
    if len(vals) < 4:
        return None
    left = top = float("inf")
    right = bottom = float("-inf")
    for i in range(0, len(vals) - 3, 4):
        l, t, w, h = vals[i : i + 4]
        if w <= 0 or h <= 0:
            continue
        left = min(left, l)
        top = min(top, t)
        right = max(right, l + w)
        bottom = max(bottom, t + h)
    if left == float("inf"):
        return None
    return (int(left), int(top), int(right), int(bottom))


def _element_rect(element) -> Optional[Rect]:
    try:
        r = element.CurrentBoundingRectangle
        if r.right > r.left and r.bottom > r.top:
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
    except Exception:
        pass
    return None
