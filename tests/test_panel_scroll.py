"""Auto-scroll tests for the reader panel.

The behaviour being pinned down: scrolling must bring the *whole* spoken
sentence into view. Scrolling until merely the first word is visible leaves a
long sentence running off the bottom edge, which is exactly when a reader loses
their place.

Needs a QApplication, so these are skipped where no Qt platform is available.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from reader.text.types import Sentence  # noqa: E402
from reader.ui.reader_panel import ReaderPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


def build(qapp, n_sentences=40, words=14):
    """A document far taller than the panel, with known sentence offsets."""
    parts, sentences, pos = [], [], 0
    for i in range(n_sentences):
        text = ("Sentence %02d " % i) + " ".join(["word"] * words) + "."
        parts.append(text)
        sentences.append(
            Sentence(index=i, text=text, char_start=pos, char_end=pos + len(text))
        )
        pos += len(text) + 1

    panel = ReaderPanel()
    panel.resize(420, 260)          # deliberately small, so text must scroll
    panel.set_typography(13, 1.5)
    panel.set_document(" ".join(parts), sentences)
    panel.show()
    qapp.processEvents()
    return panel, sentences


def rects(panel, sentence):
    from PySide6.QtGui import QTextCursor

    doc = panel.text.document()
    end_of_doc = doc.characterCount() - 1
    a = QTextCursor(doc)
    a.setPosition(min(sentence.char_start, end_of_doc))
    b = QTextCursor(doc)
    b.setPosition(min(sentence.char_end, end_of_doc))
    return panel.text.cursorRect(a), panel.text.cursorRect(b)


def test_scrolling_reveals_the_end_of_the_sentence(qapp):
    """The reported bug: one word in frame was treated as good enough."""
    panel, sentences = build(qapp)
    viewport = panel.text.viewport().height()

    for index in (5, 12, 25, 39):
        panel.set_sentence(index)
        qapp.processEvents()
        r_start, r_end = rects(panel, sentences[index])
        assert r_end.bottom() <= viewport, (
            f"sentence {index} still runs past the bottom edge "
            f"(ends at {r_end.bottom()}, viewport {viewport})"
        )
        assert r_start.top() >= 0, f"sentence {index} start scrolled off the top"


def test_no_scrolling_when_the_sentence_already_fits(qapp):
    """Don't jitter the view when nothing needs to move."""
    panel, _ = build(qapp)
    panel.set_sentence(0)
    qapp.processEvents()
    before = panel.text.verticalScrollBar().value()
    panel.set_sentence(0)
    panel._scroll_to(0)
    qapp.processEvents()
    assert panel.text.verticalScrollBar().value() == before


def test_a_sentence_taller_than_the_window_shows_its_start(qapp):
    """When it cannot all fit, reading starts at the beginning."""
    long_text = "Start here. " + " ".join(["word"] * 400) + " end here."
    sentence = Sentence(index=0, text=long_text, char_start=0,
                        char_end=len(long_text))
    panel = ReaderPanel()
    panel.resize(380, 200)
    panel.set_typography(13, 1.5)
    panel.set_document(long_text, [sentence])
    panel.show()
    qapp.processEvents()

    panel.text.verticalScrollBar().setValue(
        panel.text.verticalScrollBar().maximum()
    )
    panel.set_sentence(0)
    qapp.processEvents()

    r_start, _ = rects(panel, sentence)
    assert 0 <= r_start.top() <= panel.text.viewport().height(), (
        "the start of an over-long sentence must be on screen"
    )


def test_backwards_navigation_scrolls_up(qapp):
    panel, sentences = build(qapp)
    panel.set_sentence(30)
    qapp.processEvents()
    far = panel.text.verticalScrollBar().value()
    panel.set_sentence(2)
    qapp.processEvents()
    assert panel.text.verticalScrollBar().value() < far
    r_start, r_end = rects(panel, sentences[2])
    assert r_start.top() >= 0 and r_end.bottom() <= panel.text.viewport().height()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
