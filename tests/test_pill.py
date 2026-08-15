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
    keepalive_zone,
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


def test_selection_anchor_is_under_the_text_and_beside_the_release():
    """Vertically clear of the last line so it never covers what it is offering
    to read; horizontally where the hand already is."""
    assert place(ANCHOR_SELECTION) == (END[0], RECT[3])


def test_selection_anchor_does_not_land_at_the_far_end_of_a_sweep():
    """Regression: the selection rectangle is the union of the selected text,
    so its left edge is where the drag *began*. Anchoring to that put the
    button a screen away from the pointer that had just finished the gesture."""
    wide = (200, 500, 1700, 530)
    released_at = (1700, 520)
    x, y = place(ANCHOR_SELECTION, rect=wide, start=(200, 515), end=released_at)
    assert x == released_at[0], "must follow the release, not the left edge"
    assert y == wide[3], "and still sit below the last line"


def test_a_backwards_selection_follows_the_release_too():
    """Selecting right-to-left ends on the left, and that is where the hand
    is."""
    wide = (200, 500, 1700, 530)
    x, _y = place(ANCHOR_SELECTION, rect=wide, start=(1700, 515), end=(200, 520))
    assert x == 200


def test_selection_anchor_matches_the_others_for_a_short_selection():
    """The change only shows up on wide selections; for a few words the left
    edge and the release are the same place."""
    short = (400, 300, 460, 330)
    x, _y = place(ANCHOR_SELECTION, rect=short, start=(400, 315), end=(455, 320))
    assert abs(x - short[0]) < 60


def test_selection_start_and_end_are_different_places():
    """The whole reason drag-start had to be threaded through the watcher."""
    assert place(ANCHOR_SELECTION_START)[0] == START[0]
    assert place(ANCHOR_SELECTION_END)[0] == END[0]
    assert place(ANCHOR_SELECTION_START) != place(ANCHOR_SELECTION_END)


def test_selection_and_selection_end_still_differ_vertically():
    """Otherwise the two anchors would be the same setting under two names."""
    under = place(ANCHOR_SELECTION)
    at_end = place(ANCHOR_SELECTION_END)
    assert under[0] == at_end[0]
    assert under[1] != at_end[1], "one clears the last line, one sits at the pointer"


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
    assert reference_point(ANCHOR_SELECTION, RECT, START, END, CURSOR) == (700, 330)
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


# ------------------------------------------------- proximity keepalive zone

PILL = (400, 340, 560, 380)   # placed at the left end of a wide selection
SWEEP_END = (1700, 320)       # where the pointer finished the drag
RADIUS = 220


def test_a_pill_that_appears_far_away_does_not_instantly_vanish():
    """Regression, reported in a browser PDF: swipe across the screen, and the
    selection's bounding rectangle starts where you *began*, so the pill lands
    at one end of the sweep while the pointer is at the other. Measuring
    pointer-to-pill alone made it die on the first tick, having never moved."""
    zone = keepalive_zone(PILL, SWEEP_END)
    assert pointer_distance(zone, SWEEP_END) == 0.0

    naive = pointer_distance(PILL, SWEEP_END)
    assert naive > RADIUS, "the bug this guards: pill-only distance is enormous"


def test_moving_from_the_selection_to_the_button_keeps_it_alive():
    """The corridor between the two has to count, or the pill is snatched away
    while you are on your way to press it."""
    zone = keepalive_zone(PILL, SWEEP_END)
    for x in range(560, 1700, 50):
        assert pointer_distance(zone, (x, 350)) == 0.0


def test_wandering_off_still_hides_it():
    """The rule has to keep meaning something."""
    zone = keepalive_zone(PILL, SWEEP_END)
    assert pointer_distance(zone, (900, 900)) > RADIUS
    assert pointer_distance(zone, (900, 20)) > RADIUS


def test_hovering_the_pill_itself_always_counts_as_near():
    zone = keepalive_zone(PILL, (480, 360))  # pointer started on the pill
    assert zone == PILL, "an origin inside the pill adds nothing"
    assert pointer_distance(zone, (480, 360)) == 0.0


def test_the_zone_contains_both_ends():
    zone = keepalive_zone(PILL, SWEEP_END)
    assert pointer_distance(zone, (PILL[0], PILL[1])) == 0.0
    assert pointer_distance(zone, SWEEP_END) == 0.0


def test_a_corner_anchored_pill_is_not_killed_on_sight():
    """The failure was total for this anchor: the pill is always in the corner
    and the pointer never is."""
    corner_pill = (1752, 992, 1912, 1032)
    zone = keepalive_zone(corner_pill, (300, 200))
    assert pointer_distance(zone, (300, 200)) == 0.0


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


def test_the_proximity_check_does_not_fire_where_the_pointer_already_is(qapp, monkeypatch):
    """End to end for the browser-PDF report: the pill lands at the far end of
    a sweep, the pointer has not moved, and the very next tick must not hide
    it."""
    import reader.ui.pill as pill_mod

    pill = SelectionPill()
    pill.hide_when_pointer_away = True
    pill.pointer_distance_px = RADIUS
    pill.show()
    pill._placed = PILL
    pill._pointer_origin = SWEEP_END

    monkeypatch.setattr(pill_mod.winwin, "cursor_pos", lambda: SWEEP_END)
    pill._check_pointer()
    assert pill.isVisible(), "it must survive a tick with a stationary pointer"

    monkeypatch.setattr(pill_mod.winwin, "cursor_pos", lambda: (1000, 900))
    pill._check_pointer()
    assert not pill.isVisible(), "and still go when the pointer really leaves"
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
