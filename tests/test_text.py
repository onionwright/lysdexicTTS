"""Text pipeline tests: normalization, sentence splitting, and unit expansion.

These are the adversarial cases that motivated each piece of the design, so a
regression here is a regression in how documents actually read aloud.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.text.splitter import SentenceSplitter, is_speakable  # noqa: E402
from reader.text.units import build_units  # noqa: E402


@pytest.fixture(scope="module")
def splitter():
    sp = SentenceSplitter()
    sp.warm()
    return sp


def texts(sp, raw):
    sents, _ = sp.split(raw)
    return [s.text for s in sents]


# Second sentences are kept comfortably above the tiny-merge threshold so these
# cases exercise boundary *detection*; merging has its own test below.
@pytest.mark.parametrize(
    "raw,expected",
    [
        # Abbreviations and decimals must not be treated as sentence ends.
        ("Dr. Smith paid 3.14 dollars for it. Then he left without saying a word.",
         ["Dr. Smith paid 3.14 dollars for it.",
          "Then he left without saying a word."]),
        # Ellipsis and a closing quote stay inside their sentence.
        ('He said "Wait... really?" and paused. She nodded and said nothing more.',
         ['He said "Wait... really?" and paused.',
          "She nodded and said nothing more."]),
        # A URL full of dots is one token, and "vs."/"U.S." are not boundaries.
        ("See https://example.com/a.b.c for details vs. the U.S. norm. "
         "The next sentence stands on its own here.",
         ["See https://example.com/a.b.c for details vs. the U.S. norm.",
          "The next sentence stands on its own here."]),
    ],
)
def test_hard_boundary_cases(splitter, raw, expected):
    assert texts(splitter, raw) == expected


def test_short_trailing_sentence_folds_into_its_neighbour(splitter):
    """A 13-character sentence is not worth its own ~0.4s of synthesis
    overhead plus a pause, so it is spoken as part of the previous one."""
    out = texts(splitter, "Dr. Smith paid 3.14 dollars for it. Then he left.")
    assert out == ["Dr. Smith paid 3.14 dollars for it. Then he left."]


def test_pdf_hard_wrap_is_rejoined(splitter):
    """A single newline mid-sentence is a wrap, not a boundary."""
    out = texts(splitter, "This is a hard-wrapped\nline from a PDF that continues here.")
    assert out == ["This is a hard-wrapped line from a PDF that continues here."]


def test_pdf_hyphen_wrap_is_dehyphenated(splitter):
    out = texts(splitter, "Trimming is straightforward once you meas-\nure it.")
    assert out == ["Trimming is straightforward once you measure it."]


def test_bullets_do_not_merge(splitter):
    """spaCy's senter happily swallows a whole list into one sentence, so list
    structure has to become a hard boundary during normalization."""
    out = texts(splitter, "- bullet one\n- bullet two\n- bullet three")
    assert out == ["bullet one", "bullet two", "bullet three"]


def test_numbered_markers_are_kept_symbol_markers_are_dropped(splitter):
    out = texts(splitter, "1. first item\n2. second item")
    assert out == ["1. first item", "2. second item"]
    assert texts(splitter, "* starred item") == ["starred item"]


def test_tiny_fragments_are_merged(splitter):
    """Synthesis costs ~0.4s regardless of length, so one-word sentences are
    folded together rather than each paying that overhead."""
    out = texts(splitter, "Ha. Ok. Yes. No. Fine. And now a full length sentence here.")
    assert len(out) <= 2, out


def test_decorative_lines_are_dropped(splitter):
    out = texts(splitter, "Real sentence here.\n\n--------\n\nAnother real one.")
    assert out == ["Real sentence here.", "Another real one."]


def test_unspeakable_fragments_are_filtered(splitter):
    """Zero-phoneme sentences would yield no Result from kokoro at all
    (pipeline.py:273), desynchronizing sentence index from audio index."""
    assert not is_speakable("---")
    assert not is_speakable("...")
    assert is_speakable("A")
    for s in splitter.split("Hello there.\n\n???\n\nGoodbye now.")[0]:
        assert is_speakable(s.text)


def test_char_offsets_point_into_the_original(splitter):
    raw = "First sentence here. Second sentence follows it."
    sents, _ = splitter.split(raw)
    for s in sents:
        assert raw[s.char_start:s.char_end].strip() == s.text


def test_long_opening_sentence_is_sub_split_for_latency(splitter):
    """The common case -- highlight a paragraph, press Read -- must not wait
    for the whole first sentence to render (measured 5.7s before this)."""
    long_sentence = (
        "Text-to-speech systems have improved dramatically over the last few "
        "years, but the gap between producing a single sentence and reading a "
        "whole document aloud remains surprisingly wide, and most of the "
        "difficulty has nothing to do with the acoustic model itself."
    )
    sents, _ = splitter.split(long_sentence)
    units = build_units(sents)
    assert len(sents) == 1
    assert len(units) > 1, "a long opening sentence must be sub-split"
    assert len(units[0].text) <= 110
    assert units[0].is_sentence_start and not units[0].is_sentence_end
    assert units[-1].is_sentence_end
    assert all(u.sentence_index == 0 for u in units)


def test_units_stop_sub_splitting_once_primed(splitter):
    """Sub-splitting costs prosody, so it stops once the buffer is deep."""
    long_sentence = (
        "This particular sentence is quite long indeed, and it carries several "
        "clauses, which means it would ordinarily be a candidate for splitting "
        "into multiple separate playback units by the builder. "
    )
    sents, _ = splitter.split(long_sentence * 6)
    units = build_units(sents, prime_seconds=10.0)
    tail = [u for u in units if u.sentence_index >= len(sents) - 2]
    assert all(u.is_sentence_start and u.is_sentence_end for u in tail), (
        "late sentences should be whole units"
    )


def test_empty_and_whitespace_input(splitter):
    assert splitter.split("")[0] == []
    assert splitter.split("   \n\n  \t ")[0] == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
