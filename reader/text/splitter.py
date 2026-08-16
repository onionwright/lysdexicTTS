"""Sentence splitting via spaCy's statistical sentence recognizer.

``en_core_web_sm`` is already installed for misaki's G2P, so this costs no new
dependency — but it must be loaded with only ``tok2vec`` + ``senter``. The
obvious call raises, because the packaged config already lists ``senter`` in its
disable list::

    spacy.load('en_core_web_sm', enable=['tok2vec', 'senter'])
    # ValueError: [E1042] ... enable and disable are inconsistent

Hence the exclude-then-``enable_pipe`` dance in ``_load_nlp``.

The rule-based ``sentencizer`` was rejected: it breaks on "Dr.", "U.S.", decimals
and URLs. The full ``parser`` was rejected: 10-20x slower for identical
boundaries on this workload.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from typing import List, Optional, Tuple

from .normalize import normalize
from .types import Block, Sentence
from .units import CLAUSE_CHARS

log = logging.getLogger(__name__)

# Sentences shorter than this get merged into a neighbour. Synthesis carries
# ~0.4s of fixed overhead regardless of length, so a three-word sentence costs
# nearly as much as a thirty-word one -- over-splitting is pure loss.
DEFAULT_TINY_MERGE_CHARS = 30

# Longest highlight block, in words; longer sentences are split into balanced
# pieces at clause boundaries. Small even blocks are easier to follow and to
# click back to than one block that runs three lines. 0 disables the cap.
DEFAULT_MAX_BLOCK_WORDS = 10

_WORD_RE = re.compile(r"\S+")

# spaCy's tok2vec is O(n) in memory; beyond this we pre-chunk with the regex
# fallback rather than hand it a whole book at once.
_MAX_BLOCK_CHARS = 50_000

_FALLBACK_SPLIT_RE = re.compile(r"(?<=[.!?…])[\"'”’\)\]]*\s+(?=[\"'“‘\(\[]*[A-Z0-9])")

# A "sentence" with no letters or digits ("---", "•", "🙂") phonemizes to the
# empty string. That matters more than it looks: kokoro's generate_from_tokens
# does `if not ps: continue` (pipeline.py:273) and yields *no* Result at all, so
# such a sentence would silently desynchronize the sentence index from the audio
# playlist index. Dropping them here keeps sentences[i] <-> playlist[i] 1:1 by
# construction; the engine keeps a silence fallback as a second line of defence.
_SPEAKABLE_RE = re.compile(r"[0-9A-Za-zÀ-ɏͰ-ϿЀ-ӿ]")


def is_speakable(text: str) -> bool:
    """True if ``text`` contains anything that can produce phonemes."""
    return bool(_SPEAKABLE_RE.search(text))

_nlp = None
_nlp_lock = threading.Lock()
_nlp_failed = False


def _load_nlp():
    """Load and cache the senter pipeline. Returns ``None`` if spaCy is unusable."""
    global _nlp, _nlp_failed
    if _nlp is not None or _nlp_failed:
        return _nlp
    with _nlp_lock:
        if _nlp is not None or _nlp_failed:
            return _nlp
        try:
            import spacy

            nlp = spacy.load(
                "en_core_web_sm",
                exclude=["parser", "ner", "attribute_ruler", "lemmatizer", "tagger"],
            )
            nlp.enable_pipe("senter")
            log.info("spaCy senter loaded: pipes=%s", nlp.pipe_names)
            _nlp = nlp
        except Exception:
            log.exception("spaCy unavailable; falling back to regex sentence splitting")
            _nlp_failed = True
    return _nlp


def _sent_spans(text: str) -> List[Tuple[int, int]]:
    """Return ``(start, end)`` character spans of each sentence in ``text``."""
    nlp = _load_nlp()
    if nlp is None or len(text) > _MAX_BLOCK_CHARS:
        return _fallback_spans(text)
    try:
        doc = nlp(text)
    except Exception:
        log.exception("senter failed on a %d-char block; using regex fallback", len(text))
        return _fallback_spans(text)
    spans = [
        (s.start_char, s.end_char)
        for s in doc.sents
        if text[s.start_char : s.end_char].strip()
    ]
    return spans or _fallback_spans(text)


def _fallback_spans(text: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    pos = 0
    for m in _FALLBACK_SPLIT_RE.finditer(text):
        if text[pos : m.start()].strip():
            spans.append((pos, m.start()))
        pos = m.end()
    if text[pos:].strip():
        spans.append((pos, len(text)))
    return spans


def _merge_tiny(
    spans: List[Tuple[int, int]],
    text: str,
    threshold: int,
    protect_first: bool,
    max_words: int = 0,
) -> List[Tuple[int, int]]:
    """Glue very short fragments onto a neighbour, preferring the *next* one.

    ``protect_first`` keeps the opening sentence of the whole document short --
    a short first sentence is the cheapest way to cut time-to-first-audio.
    ``max_words`` (0 = no limit) refuses any merge whose result the block cap
    would immediately have to split again.
    """
    if len(spans) <= 1:
        return spans

    def fits(start: int, end: int) -> bool:
        return max_words <= 0 or len(text[start:end].split()) <= max_words

    out: List[Tuple[int, int]] = []
    i = 0
    while i < len(spans):
        start, end = spans[i]
        if protect_first and not out and i == 0:
            out.append((start, end))
            i += 1
            continue
        # Absorb following fragments while this unit is still too short.
        while (
            len(text[start:end].strip()) < threshold
            and i + 1 < len(spans)
            and fits(start, spans[i + 1][1])
        ):
            i += 1
            end = spans[i][1]
        if (
            len(text[start:end].strip()) < threshold
            and out
            and fits(out[-1][0], end)
        ):
            # Nothing left to absorb -- fold backwards instead.
            prev_start, _ = out.pop()
            out.append((prev_start, end))
        else:
            out.append((start, end))
        i += 1
    return out


def _word_spans(text: str, start: int, end: int) -> List[Tuple[int, int]]:
    """``(start, end)`` offsets of each whitespace-delimited word."""
    return [
        (start + m.start(), start + m.end())
        for m in _WORD_RE.finditer(text[start:end])
    ]


def _cap_spans(
    spans: List[Tuple[int, int]], text: str, max_words: int
) -> List[Tuple[int, int, bool]]:
    """Split any span longer than ``max_words`` words into balanced pieces.

    Balanced, not greedy: a 12-word sentence becomes 6+6, never 10+2 --
    evenness is the point of the cap. Each cut lands on a word gap, snapping to
    a nearby clause boundary (comma, semicolon, colon, dash) when one falls
    within two words of the balanced target.

    Returns ``(start, end, is_final_piece)`` triples. ``is_final_piece`` is
    True for unsplit spans and for the last piece of a split one, so the caller
    can keep the sentence-final pause where the sentence actually ends.
    """
    if max_words <= 0:
        return [(s, e, True) for s, e in spans]
    cap = max(2, max_words)
    out: List[Tuple[int, int, bool]] = []
    for s, e in spans:
        words = _word_spans(text, s, e)
        n = len(words)
        if n <= cap:
            out.append((s, e, True))
            continue
        pieces = math.ceil(n / cap)
        target = math.ceil(n / pieces)
        i = 0
        while n - i > cap:
            # Never below 2 words, above the cap, or leaving a 1-word tail.
            cut = max(min(i + target, i + cap, n - 2), i + 2)
            lo = max(i + 2, cut - 2)
            hi = min(i + cap, cut + 2, n - 2)
            chosen = cut
            for c in sorted(range(lo, hi + 1), key=lambda c: (abs(c - cut), -c)):
                if text[words[c - 1][0] : words[c - 1][1]][-1] in CLAUSE_CHARS:
                    chosen = c
                    break
            out.append((words[i][0], words[chosen - 1][1], False))
            i = chosen
        out.append((words[i][0], words[n - 1][1], True))
    return out


class SentenceSplitter:
    """Normalizes text and cuts it into index-stable :class:`Sentence` records."""

    def __init__(
        self,
        tiny_merge_chars: int = DEFAULT_TINY_MERGE_CHARS,
        trailing_pause_s: float = 0.16,
        paragraph_pause_s: float = 0.32,
        max_sentences: int = 2000,
        max_block_words: int = DEFAULT_MAX_BLOCK_WORDS,
        intra_sentence_pause_s: float = 0.08,
    ) -> None:
        self.tiny_merge_chars = tiny_merge_chars
        self.trailing_pause_s = trailing_pause_s
        self.paragraph_pause_s = paragraph_pause_s
        self.max_sentences = max_sentences
        # 0 disables the cap. Non-final pieces of a capped sentence get the
        # short intra pause: they end mid-sentence, and a full stop there
        # would read as the voice halting.
        self.max_block_words = max_block_words
        self.intra_sentence_pause_s = intra_sentence_pause_s

    def warm(self) -> None:
        """Pay the ~1.9s spaCy load up front, off the critical path."""
        _load_nlp()

    def split(self, raw: str) -> Tuple[List[Sentence], bool]:
        """Split ``raw`` into sentences.

        Returns ``(sentences, truncated)`` where ``truncated`` is True if the
        selection exceeded ``max_sentences`` and was cut short.
        """
        norm = normalize(raw)
        sentences: List[Sentence] = []
        truncated = False

        for block in norm.blocks:
            if len(sentences) >= self.max_sentences:
                truncated = True
                break
            spans = _sent_spans(block.text)
            spans = _merge_tiny(
                spans,
                block.text,
                self.tiny_merge_chars,
                protect_first=not sentences,
                max_words=self.max_block_words,
            )
            pieces = _cap_spans(spans, block.text, self.max_block_words)
            for j, (s, e, final_piece) in enumerate(pieces):
                if len(sentences) >= self.max_sentences:
                    truncated = True
                    break
                body = block.text[s:e].strip()
                if not body or not is_speakable(body):
                    continue
                # Re-anchor onto the stripped body so highlights don't include
                # the whitespace between sentences.
                lead = block.text[s:e].index(body) if body in block.text[s:e] else 0
                is_last = j == len(pieces) - 1
                sentences.append(
                    Sentence(
                        index=len(sentences),
                        text=body,
                        char_start=block.to_orig(s + lead),
                        char_end=block.to_orig(s + lead + len(body)),
                        pause_after_s=(
                            self.intra_sentence_pause_s
                            if not final_piece
                            else self.paragraph_pause_s
                            if is_last and block.is_paragraph_end
                            else self.trailing_pause_s
                        ),
                        is_paragraph_end=(
                            final_piece and is_last and block.is_paragraph_end
                        ),
                        block_kind=block.kind,
                    )
                )
        return sentences, truncated
