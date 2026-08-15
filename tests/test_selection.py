"""Gesture recognition tests.

``DragDetector`` is deliberately free of Windows calls at feed time so the
selection heuristics can be tested exhaustively without a mouse.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.win.hook import WM_LBUTTONDOWN, WM_LBUTTONUP  # noqa: E402
from reader.win.selection import DragDetector  # noqa: E402


def det(**kw):
    kw.setdefault("double_ms", 500)
    kw.setdefault("slop", (4, 4))
    return DragDetector(**kw)


def click(d, x, y, t, up_x=None, up_y=None, up_t=None, injected=False):
    d.feed(WM_LBUTTONDOWN, x, y, t, injected)
    return d.feed(
        WM_LBUTTONUP,
        x if up_x is None else up_x,
        y if up_y is None else up_y,
        t + 20 if up_t is None else up_t,
        injected,
    )


def test_drag_is_detected():
    d = det()
    g = click(d, 100, 100, 1000, up_x=300, up_y=100, up_t=1200)
    assert g is not None and g.kind == "drag"
    assert (g.x, g.y) == (300, 100)


def test_a_drag_reports_both_of_its_ends():
    """A selection has two ends and people reach for either, so the pill can be
    anchored to where the drag started rather than where it finished."""
    d = det()
    g = click(d, 100, 100, 1000, up_x=300, up_y=140, up_t=1200)
    assert (g.x0, g.y0) == (100, 100), "where the button went down"
    assert (g.x, g.y) == (300, 140), "where it came up"


def test_a_backwards_drag_still_reports_where_it_began():
    """Selecting right-to-left is not unusual, and 'start' must mean the
    gesture's start, not the leftmost point."""
    d = det()
    g = click(d, 500, 200, 1000, up_x=200, up_y=200, up_t=1200)
    assert (g.x0, g.y0) == (500, 200)
    assert (g.x, g.y) == (200, 200)


def test_a_click_gesture_starts_where_it_ends():
    """Double and triple clicks have no span, so both ends are the same point
    and every anchor lands in the same place."""
    d = det()
    click(d, 50, 50, 1000)
    g = click(d, 50, 50, 1200)
    assert g.kind == "double"
    assert (g.x0, g.y0) == (g.x, g.y) == (50, 50)


def test_short_drag_is_not_a_selection():
    """Below the distance threshold it's a click, not a drag-select."""
    d = det(min_px=12)
    assert click(d, 100, 100, 1000, up_x=105, up_y=100, up_t=1200) is None


def test_fast_flick_is_not_a_selection():
    """Below the time threshold it's a stray movement during a click."""
    d = det(min_ms=60)
    assert click(d, 100, 100, 1000, up_x=400, up_y=100, up_t=1010) is None


def test_plain_click_produces_nothing():
    d = det()
    assert click(d, 50, 50, 1000) is None


def test_double_click_is_detected():
    d = det()
    assert click(d, 50, 50, 1000) is None
    g = click(d, 50, 50, 1200)
    assert g is not None and g.kind == "double"


def test_double_click_requires_proximity():
    """Two clicks far apart are two clicks, not a word selection."""
    d = det(slop=(4, 4))
    click(d, 50, 50, 1000)
    assert click(d, 400, 400, 1200) is None


def test_double_click_requires_speed():
    d = det(double_ms=500)
    click(d, 50, 50, 1000)
    assert click(d, 50, 50, 3000) is None


def test_triple_click_is_detected():
    d = det()
    click(d, 50, 50, 1000)
    click(d, 50, 50, 1150)
    g = click(d, 50, 50, 1300)
    assert g is not None and g.kind == "triple"


def test_injected_input_is_ignored_by_default():
    """Our own synthetic Ctrl+C and other automation must not self-trigger."""
    d = det()
    assert click(d, 100, 100, 1000, up_x=300, up_t=1200, injected=True) is None


def test_injected_input_can_be_accepted():
    """Some Remote Desktop clients flag real input as injected."""
    d = det(ignore_injected=False)
    g = click(d, 100, 100, 1000, up_x=300, up_t=1200, injected=True)
    assert g is not None and g.kind == "drag"


def test_unmatched_button_up_is_ignored():
    """A drag that began before the hook was installed must not fire."""
    d = det()
    assert d.feed(WM_LBUTTONUP, 300, 100, 1200, False) is None


def test_drag_then_click_does_not_report_double():
    """A drag resets the click run, so a following click isn't a double."""
    d = det()
    assert click(d, 100, 100, 1000, up_x=300, up_t=1200).kind == "drag"
    assert click(d, 300, 100, 1250) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
