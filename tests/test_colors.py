"""Reading colours reaching the panel.

The palette arithmetic is covered in test_palette.py; this is about the panel
actually using it -- and about a colour change landing on the sentence being
spoken right now, rather than at the next sentence boundary.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from reader.text.types import Sentence  # noqa: E402
from reader.ui.palette import HIGHLIGHT, PAPER, ReadingColors  # noqa: E402
from reader.ui.reader_panel import ReaderPanel  # noqa: E402

TEXT = "First sentence here. Second sentence here. Third sentence here."
CREAM = "#faf3e0"
DEEP_BLUE = "#2f5aa8"


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def sentences_of(text):
    """Three fixed spans; the splitter is tested elsewhere."""
    out, pos = [], 0
    for i, part in enumerate(text.split(". ")):
        part = part if part.endswith(".") else part + "."
        out.append(
            Sentence(index=i, text=part, char_start=pos, char_end=pos + len(part))
        )
        pos += len(part) + 1
    return out


def loaded_panel(text=TEXT):
    panel = ReaderPanel()
    panel.set_document(text, sentences_of(text))
    return panel


# Read in one expression, holding the list: the ExtraSelection objects Qt hands
# back are temporaries, and reaching into .format after the list has been
# released is a shiboken "C++ object already deleted", not a test failure.
def backgrounds(panel):
    return [
        s.format.background().color().name()
        for s in panel.text.extraSelections()
    ]


def foregrounds(panel):
    return [
        s.format.foreground().color().name()
        for s in panel.text.extraSelections()
    ]


def test_panel_starts_on_the_original_look(qapp):
    panel = loaded_panel()
    assert panel.colors.page == PAPER[0][1]
    assert panel.colors.highlight == HIGHLIGHT[0][1]
    panel.close()


def test_set_colors_reaches_the_wash_and_the_highlight(qapp):
    panel = loaded_panel()
    panel.set_sentence(1)
    before = backgrounds(panel)

    panel.set_colors(highlight="#d55e00", page_tint=CREAM)
    after = backgrounds(panel)

    assert before != after, "the painted selections must actually change"
    expected = ReadingColors("#d55e00", CREAM)
    assert after[-1] == expected.highlight, "spoken sentence takes the new highlight"
    assert after[0] == expected.wash, "the captured wash follows the new paper"
    panel.close()


def test_colour_change_lands_on_the_sentence_being_spoken(qapp):
    """Regression risk: rebuilding only the wash would leave the sentence you
    are listening to in the old colour until the next boundary -- which is the
    one moment you are looking straight at it."""
    panel = loaded_panel()
    panel.set_sentence(1)
    assert len(backgrounds(panel)) == 4, "three wash spans plus the spoken one"

    panel.set_colors(highlight="#009e73", page_tint=CREAM)

    after = backgrounds(panel)
    assert len(after) == 4, "the spoken highlight must survive"
    assert after[-1] == ReadingColors("#009e73", CREAM).highlight
    panel.close()


def test_spoken_text_colour_flips_with_the_highlight(qapp):
    """The reason there is no fourth picker."""
    panel = loaded_panel()
    panel.set_sentence(0)

    panel.set_colors(highlight="#f0e442", page_tint=CREAM)  # pale yellow
    pale = foregrounds(panel)[-1]

    panel.set_colors(highlight="#0072b2", page_tint=CREAM)  # deep blue
    deep = foregrounds(panel)[-1]

    assert pale != deep, "ink must follow the highlight, not stay put"
    assert pale == "#111318" and deep == "#ffffff"
    panel.close()


def test_colours_survive_a_new_document(qapp):
    """set_document rebuilds the wash; it must not rebuild it in the old
    colours."""
    panel = loaded_panel()
    panel.set_colors(highlight="#cc79a7", page_tint=CREAM)
    expected = ReadingColors("#cc79a7", CREAM)

    other = "A different document entirely. With two sentences in it."
    panel.set_document(other, sentences_of(other))

    assert all(bg == expected.wash for bg in backgrounds(panel))
    panel.close()


def test_setting_colours_before_any_document_does_not_raise(qapp):
    """_apply_settings runs at startup, well before anything is read."""
    panel = ReaderPanel()
    panel.set_colors(highlight="#000000", page_tint="#e6e7e9")
    assert panel.colors.page == "#e6e7e9"
    assert backgrounds(panel) == []
    panel.close()


def test_header_icons_follow_the_paper(qapp):
    """A grey glyph on cream paper is very close to no glyph at all."""
    panel = ReaderPanel()
    dark_ink = panel.btn_play._ink
    panel.set_colors(highlight=DEEP_BLUE, page_tint=CREAM)
    assert panel.btn_play._ink != dark_ink
    assert panel.btn_play._ink == panel.colors.dim_text
    panel.close()


def test_a_broken_settings_file_falls_back_instead_of_crashing(qapp):
    panel = ReaderPanel()
    panel.set_colors(highlight="", page_tint="not a colour")
    assert panel.colors.page == PAPER[0][1]
    assert panel.colors.highlight == HIGHLIGHT[0][1]
    panel.close()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
