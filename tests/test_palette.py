"""Reading colours: the arithmetic that is supposed to make bad combinations
impossible to reach through the settings window.

No Qt here -- this is the part that must be provable without a display.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.ui import palette  # noqa: E402
from reader.ui.palette import (  # noqa: E402
    HIGHLIGHT,
    INK_DARK,
    INK_LIGHT,
    MIN_CONTRAST,
    PAPER,
    ReadingColors,
    body_text_on,
    contrast_ratio,
    is_color,
    mix,
    readable_text_on,
    relative_luminance,
    shade,
)

ALL_SWATCHES = PAPER + HIGHLIGHT


def test_every_shipped_swatch_parses():
    for name, value in ALL_SWATCHES:
        assert is_color(value), f"{name} is not a colour"


def test_swatch_names_are_unique():
    """They are the entire label in the settings grid."""
    for group in (PAPER, HIGHLIGHT):
        names = [n for n, _ in group]
        assert len(names) == len(set(names))


@pytest.mark.parametrize("name,value", ALL_SWATCHES, ids=[n for n, _ in ALL_SWATCHES])
def test_every_swatch_is_readable_as_a_highlight(name, value):
    """The point of deriving the ink instead of asking for it: there must be no
    reachable choice that produces unreadable text."""
    ratio = contrast_ratio(value, readable_text_on(value))
    assert ratio >= MIN_CONTRAST, f"{name} only reaches {ratio:.2f}:1"


@pytest.mark.parametrize("name,value", PAPER, ids=[n for n, _ in PAPER])
def test_every_paper_is_readable_as_a_page(name, value):
    """Body text is softened toward the page, so it has to be checked
    separately -- softening costs contrast."""
    ratio = contrast_ratio(value, body_text_on(value))
    assert ratio >= MIN_CONTRAST, f"{name} only reaches {ratio:.2f}:1"


def test_every_paper_and_highlight_pair_works():
    """Every combination the grids can produce, not just each grid alone."""
    worst = None
    for pname, page in PAPER:
        for hname, highlight in HIGHLIGHT:
            colors = ReadingColors(highlight, page)
            assert not colors.is_hard_to_read(), (
                f"{hname} on {pname} is below AA "
                f"({colors.highlight_contrast:.2f}:1 / "
                f"{colors.body_contrast:.2f}:1)"
            )
            score = min(colors.highlight_contrast, colors.body_contrast)
            if worst is None or score < worst[0]:
                worst = (score, hname, pname)
    assert worst is not None


def test_luminance_endpoints():
    assert relative_luminance("#000000") == pytest.approx(0.0)
    assert relative_luminance("#ffffff") == pytest.approx(1.0)
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)


def test_contrast_is_symmetric_and_self_is_one():
    assert contrast_ratio("#2f5aa8", "#faf3e0") == pytest.approx(
        contrast_ratio("#faf3e0", "#2f5aa8")
    )
    assert contrast_ratio("#2f5aa8", "#2f5aa8") == pytest.approx(1.0)


def test_ink_flips_with_the_background():
    assert readable_text_on("#000000") == INK_LIGHT
    assert readable_text_on("#ffffff") == INK_DARK
    assert readable_text_on("#faf3e0") == INK_DARK, "pale paper takes dark ink"
    assert readable_text_on("#0072b2") == INK_LIGHT, "deep blue takes light ink"


def test_ink_choice_beats_a_luminance_threshold():
    """The awkward middle is why this measures rather than thresholds.

    Red-orange sits close enough to mid-luminance that picking by a fixed
    cutoff gets it wrong; picking by measured contrast cannot.
    """
    for value in ("#d55e00", "#009e73", "#e69f00", "#cc79a7"):
        chosen = readable_text_on(value)
        other = INK_LIGHT if chosen == INK_DARK else INK_DARK
        assert contrast_ratio(value, chosen) >= contrast_ratio(value, other)


def test_parsing_accepts_short_form_and_a_missing_hash():
    assert mix("#fff", "#000", 0.0) == "#ffffff"
    assert mix("fff", "#000", 1.0) == "#000000"
    assert not is_color("nonsense")
    assert not is_color("#ff")


def test_mix_is_a_blend():
    assert mix("#000000", "#ffffff", 0.5) == "#808080"
    assert mix("#000000", "#ffffff", 0.0) == "#000000"
    assert mix("#000000", "#ffffff", 1.0) == "#ffffff"


def test_shade_moves_the_right_way():
    assert relative_luminance(shade("#808080", 0.4)) > relative_luminance("#808080")
    assert relative_luminance(shade("#808080", -0.4)) < relative_luminance("#808080")


def test_derived_colours_are_distinct_from_the_page():
    """The wash marks captured text; if it matched the page it would mark
    nothing, and the header would vanish into the body."""
    for _name, page in PAPER:
        colors = ReadingColors("#2f5aa8", page)
        assert colors.wash != colors.page
        assert colors.header != colors.page
        assert colors.edge != colors.page


def test_the_edge_reverses_direction_with_the_page():
    """A light line reads as an edge on dark paper, a dark line on pale paper."""
    dark = ReadingColors("#2f5aa8", "#23262e")
    pale = ReadingColors("#2f5aa8", "#faf3e0")
    assert relative_luminance(dark.edge) > relative_luminance(dark.page)
    assert relative_luminance(pale.edge) < relative_luminance(pale.page)


def test_nonsense_colours_fall_back_instead_of_raising():
    """A hand-edited settings file must not stop the app from starting."""
    colors = ReadingColors("not a colour", "also not a colour")
    assert colors.highlight == HIGHLIGHT[0][1]
    assert colors.page == PAPER[0][1]


def test_the_advisory_can_actually_fire():
    """is_hard_to_read has to mean something -- a check that never trips is not
    a check. Nothing reachable from the grids trips it, so this reaches past
    them, the way a hand-edited settings file could."""
    assert ReadingColors("#808080", "#7d7d7d").is_hard_to_read()
    assert not ReadingColors("#2f5aa8", "#faf3e0").is_hard_to_read()


def test_defaults_reproduce_the_original_look():
    """The first paper swatch and the app's original blue must still be the
    familiar panel, so 'put it back' is one click."""
    colors = ReadingColors(HIGHLIGHT[0][1], PAPER[0][1])
    assert colors.page == "#23262e"
    assert colors.highlight == "#2f5aa8"
    assert colors.spoken_text == INK_LIGHT
    assert palette.is_dark(colors.page)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
