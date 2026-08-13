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
import re
import threading
from typing import List, Optional, Tuple

from .normalize import normalize
from .types import Block, Sentence

log = logging.getLogger(__name__)

# Sentences shorter than this get merged into a neighbour. Synthesis carries
# ~0.4s of fixed overhead regardless of length, so a three-word sentence costs
# nearly as much as a thirty-word one -- over-splitting is pure loss.
DEFAULT_TINY_MERGE_CHARS = 30

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
    spans: List[Tuple[int, int]], text: str, threshold: int, protect_first: bool
) -> List[Tuple[int, int]]:
    """Glue very short fragments onto a neighbour, preferring the *next* one.

    ``protect_first`` keeps the opening sentence of the whole document short --
    a short first sentence is the cheapest way to cut time-to-first-audio.
    """
    if len(spans) <= 1:
        return spans
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
        ):
            i += 1
            end = spans[i][1]
        if len(text[start:end].strip()) < threshold and out:
            # Nothing left to absorb -- fold backwards instead.
            prev_start, _ = out.pop()
            out.append((prev_start, end))
        else:
            out.append((start, end))
        i += 1
    return out


class SentenceSplitter:
    """Normalizes text and cuts it into index-stable :class:`Sentence` records."""

    def __init__(
        self,
        tiny_merge_chars: int = DEFAULT_TINY_MERGE_CHARS,
        trailing_pause_s: float = 0.16,
        paragraph_pause_s: float = 0.32,
        max_sentences: int = 2000,
    ) -> None:
        self.tiny_merge_chars = tiny_merge_chars
        self.trailing_pause_s = trailing_pause_s
        self.paragraph_pause_s = paragraph_pause_s
        self.max_sentences = max_sentences

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
            )
            for j, (s, e) in enumerate(spans):
                if len(sentences) >= self.max_sentences:
                    truncated = True
                    break
                body = block.text[s:e].strip()
                if not body or not is_speakable(body):
                    continue
                # Re-anchor onto the stripped body so highlights don't include
                # the whitespace between sentences.
                lead = block.text[s:e].index(body) if body in block.text[s:e] else 0
                is_last = j == len(spans) - 1
                sentences.append(
                    Sentence(
                        index=len(sentences),
                        text=body,
                        char_start=block.to_orig(s + lead),
                        char_end=block.to_orig(s + lead + len(body)),
                        pause_after_s=(
                            self.paragraph_pause_s
                            if is_last and block.is_paragraph_end
                            else self.trailing_pause_s
                        ),
                        is_paragraph_end=is_last and block.is_paragraph_end,
                        block_kind=block.kind,
                    )
                )
        return sentences, truncated
