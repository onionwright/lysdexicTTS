"""Where the pill lands, and what makes it go away.

``place_pill`` is pure for the same reason ``DragDetector`` is: five anchors
times a flip-when-there-is-no-room case times multi-monitor clamping is not
something anyone should be checking by hand with a mouse.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.ui.pill import (  # noqa: E402
    ANCHOR_CORNER,
    ANCHOR_MOUSE,
    ANCHOR_SELECTION,
    ANCHOR_SELECTION_END,
    ANCHOR_SELECTION_START,
    ANCHORS,
    EDGE_MARGIN,
    place_pill,
    pointer_distance,
    reference_point,
)

SCREEN = (0, 0, 1920, 1040)  # work area: 1080 tall minus a 40px taskbar
SIZE = (160, 40)
RECT = (400, 300, 700, 330)  # a selected line of text
START = (400, 310)
END = (700, 320)
CURSOR = (900, 500)
NO_OFFSET = (0, 0)


def place(anchor, **kw):
    kw.setdefault("size", SIZE)
    kw.setdefault("work_area", SCREEN)
    kw.setdefault("rect", RECT)
    kw.setdefault("start", START)
    kw.setdefault("end", END)
    kw.setdefault("cursor", CURSOR)
    kw.setdefault("offset", NO_OFFSET)
    return place_pill(anchor, **kw)


# ------------------------------------------------------------------ anchors


def test_selection_anchor_sits_under_the_selection():
    assert place(ANCHOR_SELECTION) == (400, 330)


def test_selection_start_and_end_are_different_places():
    """The whole reason drag-start had to be threaded through the watcher."""
    assert place(ANCHOR_SELECTION_START)[0] == START[0]
    assert place(ANCHOR_SELECTION_END)[0] == END[0]
    assert place(ANCHOR_SELECTION_START) != place(ANCHOR_SELECTION_END)


def test_mouse_anchor_follows_the_pointer():
    assert place(ANCHOR_MOUSE) == (CURSOR[0], CURSOR[1])


def test_corner_anchor_is_fixed_and_ignores_the_selection():
    """It should not move when the selection does -- that is the point of it."""
    here = place(ANCHOR_CORNER)
    there = place(ANCHOR_CORNER, rect=(10, 10, 90, 30), start=(10, 20), end=(90, 20))
    assert here == there
    # Bottom-right of the work area, held off the edge by the same margin
    # every other anchor is.
    assert here == (1920 - 160 - EDGE_MARGIN, 1040 - 40 - EDGE_MARGIN)


def test_corner_anchor_stays_above_the_taskbar():
    """The work area already excludes it, so respecting the work area is all
    'above the notification area' takes."""
    x, y = place(ANCHOR_CORNER)
    assert y + SIZE[1] <= SCREEN[3]
    assert x + SIZE[0] <= SCREEN[2]


@pytest.mark.parametrize("anchor", ANCHORS)
def test_every_anchor_stays_on_screen(anchor):
    """Including with a selection that is off the edge, which happens with a
    stale UIA rectangle from a window that just scrolled."""
    for rect in (RECT, (-500, -500, -400, -470), (5000, 5000, 5200, 5030)):
        x, y = place(anchor, rect=rect, start=(-800, -800), end=(4000, 4000),
                     cursor=(-100, 3000))
        assert SCREEN[0] <= x and x + SIZE[0] <= SCREEN[2]
        assert SCREEN[1] <= y and y + SIZE[1] <= SCREEN[3]


# ------------------------------------------------------------------ flipping


def test_selection_flips_above_when_there_is_no_room_below():
    low = (400, 990, 700, 1020)  # a selection near the bottom of the screen
    x, y = place(ANCHOR_SELECTION, rect=low)
    assert y + SIZE[1] <= low[1], "must sit above the selection, not over it"


def test_above_puts_it_above_without_being_asked_twice():
    _x, below = place(ANCHOR_SELECTION)
    _x, above = place(ANCHOR_SELECTION, above=True)
    assert above < below
    assert above + SIZE[1] <= RECT[1]


def test_above_applies_to_point_anchors_too():
    _x, below = place(ANCHOR_MOUSE, offset=(0, 8))
    _x, above = place(ANCHOR_MOUSE, offset=(0, 8), above=True)
    assert above + SIZE[1] < CURSOR[1] < below, "the pointer must end up between"


# ------------------------------------------------------------------ offsets


def test_offsets_nudge_every_anchor():
    for anchor in (ANCHOR_SELECTION, ANCHOR_SELECTION_START, ANCHOR_MOUSE):
        base = place(anchor)
        moved = place(anchor, offset=(20, 12))
        assert moved == (base[0] + 20, base[1] + 12)


def test_corner_offset_pushes_inward_not_off_the_edge():
    """The corner anchor measures from the far edge, so a positive offset has
    to move it further into the screen rather than past the boundary."""
    base = place(ANCHOR_CORNER)
    moved = place(ANCHOR_CORNER, offset=(20, 12))
    assert moved[0] < base[0] and moved[1] < base[1]


def test_a_negative_offset_cannot_push_it_off_screen():
    x, y = place(ANCHOR_MOUSE, cursor=(10, 10), offset=(-500, -500))
    assert x >= SCREEN[0] + EDGE_MARGIN and y >= SCREEN[1] + EDGE_MARGIN


# --------------------------------------------------------- missing UIA rect


def test_selection_anchor_falls_back_to_where_you_stopped_dragging():
    """UIA reports nothing in Electron apps and several PDF viewers. That used
    to be the only behaviour; now it is the fallback for one anchor."""
    assert place(ANCHOR_SELECTION, rect=None) == (END[0], END[1])


def test_start_anchor_still_works_without_a_rect():
    assert place(ANCHOR_SELECTION_START, rect=None) == (START[0], START[1])


def test_placement_survives_having_nothing_to_go_on():
    """Belt and braces: every input optional, nothing may raise."""
    for anchor in ANCHORS:
        x, y = place_pill(
            anchor, size=SIZE, work_area=SCREEN,
            rect=None, start=None, end=None, cursor=None,
        )
        assert SCREEN[0] <= x <= SCREEN[2] and SCREEN[1] <= y <= SCREEN[3]


def test_reference_point_picks_the_monitor_the_pill_will_land_on():
    """Which work area applies depends on where the pill is going, so this has
    to agree with place_pill about that."""
    assert reference_point(ANCHOR_MOUSE, RECT, START, END, CURSOR) == CURSOR
    assert reference_point(ANCHOR_SELECTION_START, RECT, START, END, CURSOR) == START
    assert reference_point(ANCHOR_SELECTION_END, RECT, START, END, CURSOR) == END
    assert reference_point(ANCHOR_SELECTION, RECT, START, END, CURSOR) == (400, 330)
    assert reference_point(ANCHOR_SELECTION, None, START, END, CURSOR) == END


# ---------------------------------------------------------------- proximity


def test_pointer_inside_the_pill_is_zero_away():
    assert pointer_distance((100, 100, 260, 140), (180, 120)) == 0.0
    assert pointer_distance((100, 100, 260, 140), (100, 100)) == 0.0


def test_pointer_distance_is_measured_from_the_edge():
    """Measuring from the centre would make the threshold mean different
    things for a small pill and a large one."""
    rect = (100, 100, 260, 140)
    assert pointer_distance(rect, (300, 120)) == pytest.approx(40.0)
    assert pointer_distance(rect, (180, 200)) == pytest.approx(60.0)
    assert pointer_distance(rect, (300, 180)) == pytest.approx(56.57, abs=0.01)


def test_a_wider_pill_does_not_change_what_the_threshold_means():
    narrow = pointer_distance((100, 100, 160, 140), (260, 120))
    wide = pointer_distance((100, 100, 240, 140), (340, 120))
    assert narrow == wide == 100.0


# ------------------------------------------------------------------ widget

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from reader.ui.pill import SelectionPill  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_copy_can_be_taken_off_the_pill(qapp):
    pill = SelectionPill()
    pill.set_appearance(font_pt=12, show_copy=False)
    assert not pill.btn_copy.isVisibleTo(pill)
    assert not pill.sep.isVisibleTo(pill)
    assert pill.btn_read.isVisibleTo(pill)

    pill.set_appearance(font_pt=12, show_copy=True)
    assert pill.btn_copy.isVisibleTo(pill)
    pill.close()


def test_bigger_text_makes_a_bigger_pill(qapp):
    pill = SelectionPill()
    pill.set_appearance(font_pt=9, show_copy=True)
    small = pill.sizeHint().height()
    pill.set_appearance(font_pt=20, show_copy=True)
    assert pill.sizeHint().height() > small
    pill.close()


def test_clicking_the_pill_itself_never_dismisses_it(qapp):
    """It is a no-activate window, so a click on Read still arrives through the
    global hook -- without the hwnd check it would dismiss itself out from
    under the press."""
    pill = SelectionPill()
    pill.show()
    pill.hide_on_click_away = True

    pill.dismiss_if_clicked_away(pill.hwnd())
    assert pill.isVisible()

    pill.dismiss_if_clicked_away(pill.hwnd() + 1)
    assert not pill.isVisible()
    pill.close()


def test_click_away_can_be_switched_off(qapp):
    pill = SelectionPill()
    pill.show()
    pill.hide_on_click_away = False
    pill.dismiss_if_clicked_away(12345)
    assert pill.isVisible()
    pill.close()


def test_hiding_stops_every_timer(qapp):
    """A proximity timer left running would hide the *next* selection's pill."""
    pill = SelectionPill()
    pill.show()
    pill._auto_hide.start(10000)
    pill._proximity.start()
    pill.hide()
    assert not pill._auto_hide.isActive()
    assert not pill._proximity.isActive()
    pill.close()


def test_auto_hide_off_means_no_timer(qapp):
    """'Stay until I click away' is this plus the click rule."""
    pill = SelectionPill()
    pill.auto_hide_enabled = False
    pill.show_for(RECT, END, START)
    assert not pill._auto_hide.isActive()
    pill.close()


def test_auto_hide_on_starts_the_timer(qapp):
    pill = SelectionPill()
    pill.auto_hide_enabled = True
    pill.auto_hide_ms = 5000
    pill.show_for(RECT, END, START)
    assert pill._auto_hide.isActive()
    pill.close()


def test_proximity_timer_only_runs_when_that_rule_is_on(qapp):
    pill = SelectionPill()
    pill.hide_when_pointer_away = False
    pill.show_for(RECT, END, START)
    assert not pill._proximity.isActive()
    pill.close()

    pill = SelectionPill()
    pill.hide_when_pointer_away = True
    pill.show_for(RECT, END, START)
    assert pill._proximity.isActive()
    pill.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
