"""Playback units: what actually goes in the playlist.

A unit is usually a whole sentence. The exception exists because of a measured
problem: synthesis runs at ~2.5x real time with ~0.4s of fixed overhead, so a
long opening sentence takes 5.7s to render before a single word is heard. That
is the most common selection shape there is -- highlight a paragraph, press
Read -- and 5.7s of silence makes the app feel broken.

So while the buffer is still cold, long sentences are cut at clause boundaries
into smaller units. The first unit of that 5.7s sentence renders in well under a
second and playback starts there while the rest streams in behind it. Once
roughly ``prime_seconds`` of audio has been queued, the lookahead window is deep
enough to stay ahead and sentences are left whole.

Splitting preferentially at commas, semicolons and colons keeps the prosody
honest: kokoro renders a comma-terminated fragment with continuation intonation
rather than a full stop, which is the same priority order its own internal
chunker (``waterfall_last``) uses.

Transport and highlighting stay sentence-level -- ``Unit.sentence_index`` maps
every playlist entry back to the sentence the user sees highlighted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Sequence

from .types import Sentence

# Audio budget that must be queued before we stop sub-splitting.
DEFAULT_PRIME_SECONDS = 10.0
# Target size of a primed unit. ~110 chars is about 7s of speech, which renders
# in ~2.5s -- comfortably faster than the previous unit takes to play.
DEFAULT_PRIME_MAX_CHARS = 110
# Below this a unit isn't worth its own ~0.4s of fixed synthesis overhead.
_MIN_UNIT_CHARS = 32

SUBCHUNK_PAUSE_S = 0.08

# Shared with the splitter's block cap: both cut at the same punctuation so a
# cut reads as a clause break, which kokoro renders with continuation
# intonation rather than a full stop.
CLAUSE_CHARS = ",;:—–"
CLAUSE_BREAK_RE = re.compile(r"[%s]\s+" % CLAUSE_CHARS)
_CLAUSE_BREAK_RE = CLAUSE_BREAK_RE  # old private name, kept for callers
_WORD_BREAK_RE = re.compile(r"\s+")


@dataclass(slots=True)
class Unit:
    """One playlist entry."""

    index: int
    sentence_index: int
    text: str
    pause_after_s: float
    is_sentence_start: bool
    is_sentence_end: bool


def _split_clauses(text: str, max_chars: int) -> List[str]:
    """Greedily cut ``text`` into <= ``max_chars`` pieces at clause boundaries."""
    if len(text) <= max_chars:
        return [text]
    clause = [m.end() for m in _CLAUSE_BREAK_RE.finditer(text)]
    words = [m.end() for m in _WORD_BREAK_RE.finditer(text)]
    out: List[str] = []
    start = 0
    while len(text) - start > max_chars:
        lo = start + max(_MIN_UNIT_CHARS, max_chars // 3)
        hi = start + max_chars
        cands = [b for b in clause if lo < b <= hi]
        if not cands:
            cands = [b for b in words if lo < b <= hi]
        brk = cands[-1] if cands else hi
        piece = text[start:brk].strip()
        if piece:
            out.append(piece)
        start = brk
    tail = text[start:].strip()
    if tail:
        # Don't leave a runt at the end; fold it back into the previous unit.
        if out and len(tail) < _MIN_UNIT_CHARS:
            out[-1] = out[-1] + " " + tail
        else:
            out.append(tail)
    return out or [text]


def estimate_seconds(text: str) -> float:
    """Rough speech duration. Kokoro at speed 1.0 runs near 15 chars/second."""
    return max(0.4, len(text) / 15.0)


def build_units(
    sentences: Sequence[Sentence],
    *,
    prime_seconds: float = DEFAULT_PRIME_SECONDS,
    prime_max_chars: int = DEFAULT_PRIME_MAX_CHARS,
    first_unit_max_chars: int = 0,
) -> List[Unit]:
    """Expand sentences into playlist units, sub-splitting only while cold.

    ``first_unit_max_chars`` (0 = same as ``prime_max_chars``) trades opening
    latency against opening prosody. Smaller values start speaking sooner but
    are likelier to cut mid-clause, where kokoro gives the fragment a falling
    sentence-final intonation instead of a continuation. Measured on this
    machine: 110 chars gives ~1.7s to first audio on a long opening sentence.
    """
    units: List[Unit] = []
    queued = 0.0
    for si, sentence in enumerate(sentences):
        limit = (
            first_unit_max_chars
            if (si == 0 and first_unit_max_chars > 0)
            else prime_max_chars
        )
        if queued < prime_seconds and len(sentence.text) > limit:
            parts = _split_clauses(sentence.text, limit)
        else:
            parts = [sentence.text]
        for k, part in enumerate(parts):
            last = k == len(parts) - 1
            units.append(
                Unit(
                    index=len(units),
                    sentence_index=si,
                    text=part,
                    pause_after_s=(
                        sentence.pause_after_s if last else SUBCHUNK_PAUSE_S
                    ),
                    is_sentence_start=(k == 0),
                    is_sentence_end=last,
                )
            )
            queued += estimate_seconds(part)
    return units


def first_unit_of_sentence(units: Sequence[Unit], n_sentences: int) -> List[int]:
    """Index of the first unit belonging to each sentence."""
    firsts = [0] * n_sentences
    seen = set()
    for u in units:
        if u.sentence_index not in seen:
            seen.add(u.sentence_index)
            firsts[u.sentence_index] = u.index
    return firsts
