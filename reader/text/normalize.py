"""Turn captured text into clean, block-structured text for sentence splitting.

Everything here exists because real selections are messy: PDFs hard-wrap
mid-sentence and hyphenate across lines, web pages carry non-breaking spaces and
zero-width joiners, and bullet lists have no terminal punctuation at all (spaCy's
senter cheerfully merges an entire list into one sentence, so list structure has
to become a *hard* boundary before the model ever sees it).

The whole module is a single pass so the offset map stays simple: composing maps
across several regex passes is where this kind of code usually goes wrong.
"""

from __future__ import annotations

import re
from typing import Iterator, List, Tuple

from .types import Block, Normalized, Piece

# Characters that should vanish entirely rather than become spaces.
_ZERO_WIDTH = frozenset("​‌‍⁠﻿­")

# A symbol bullet is noise when spoken, so we drop the marker. Numeric and
# alphabetic markers ("1.", "a)") carry meaning and are kept.
_SYMBOL_BULLET_RE = re.compile(r"^\s*[-*•‣◦▪▸·–—]\s+(?=\S)")
_ENUM_BULLET_RE = re.compile(r"^\s*\(?(?:\d{1,3}|[a-zA-Z]|[ivxIVX]{1,5})[.)]\s+(?=\S)")

# Horizontal rules and ASCII-art separators phonemize into noise.
_DECOR_RE = re.compile(r"^\s*[-=_*~#·—–+<>|]{3,}\s*$")

_MD_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(?=\S)")

# A trailing hyphen that a PDF inserted to wrap a word, e.g. "hyphen-\nation".
_WRAP_HYPHEN_TAIL = re.compile(r"[^\W\d_]-$")

_HEADING_MAX_LEN = 60
_SENTENCE_TERMINALS = ".!?,;:…"


class _BlockBuilder:
    """Accumulates cleaned text while recording how it maps back to the source."""

    __slots__ = ("buf", "pieces", "out_len", "kind", "_first_orig")

    def __init__(self, kind: str = "para") -> None:
        self.buf: List[str] = []
        self.pieces: List[Piece] = []
        self.out_len = 0
        self.kind = kind
        self._first_orig = -1

    def __bool__(self) -> bool:
        return self.out_len > 0

    def _last_is_space(self) -> bool:
        return (not self.buf) or self.buf[-1] == " "

    def add_span(self, raw: str, start: int, end: int) -> None:
        """Append ``raw[start:end]``, cleaning characters and collapsing spaces."""
        run_orig = -1
        run_out = -1
        run_len = 0
        for i in range(start, end):
            ch = raw[i]
            if ch in _ZERO_WIDTH:
                out = None
            elif ch.isspace():
                out = None if self._last_is_space() else " "
            else:
                out = ch
            if out is None:
                if run_len:
                    self.pieces.append(Piece(run_out, run_orig, run_len))
                    run_len = 0
                continue
            if run_len == 0:
                run_orig = i
                run_out = self.out_len
                if self._first_orig < 0:
                    self._first_orig = i
            self.buf.append(out)
            self.out_len += 1
            run_len += 1
        if run_len:
            self.pieces.append(Piece(run_out, run_orig, run_len))

    def add_join_space(self, orig_pos: int) -> None:
        """Insert the space that replaces a soft line break."""
        if self._last_is_space():
            return
        self.buf.append(" ")
        self.pieces.append(Piece(self.out_len, orig_pos, 0))
        self.out_len += 1

    def pop_char(self) -> None:
        """Remove the last emitted character (used to undo a wrap hyphen)."""
        if not self.buf:
            return
        self.buf.pop()
        self.out_len -= 1
        if self.pieces:
            last = self.pieces[-1]
            if last.length <= 1:
                self.pieces.pop()
            else:
                self.pieces[-1] = Piece(last.out_start, last.orig_start, last.length - 1)

    def ends_with_wrap_hyphen(self) -> bool:
        tail = "".join(self.buf[-2:])
        return bool(_WRAP_HYPHEN_TAIL.search(tail))

    def finish(self, is_paragraph_end: bool) -> Block | None:
        text = "".join(self.buf).strip()
        if not text:
            return None
        # ``strip()`` only ever removes trailing space here (leading spaces are
        # collapsed away during add_span), so out offsets stay valid.
        return Block(
            text=text,
            pieces=self.pieces,
            kind=self.kind,
            is_paragraph_end=is_paragraph_end,
        )


def _iter_lines(raw: str) -> Iterator[Tuple[int, int]]:
    """Yield ``(start, end)`` offsets of each line, excluding its terminator."""
    start = 0
    n = len(raw)
    i = 0
    while i < n:
        if raw[i] == "\n":
            end = i
            if end > start and raw[end - 1] == "\r":
                end -= 1
            yield start, end
            i += 1
            start = i
        else:
            i += 1
    if start < n:
        yield start, n


def _classify(line: str, prev_blank: bool, next_blank: bool) -> str:
    """Return 'blank' | 'decor' | 'bullet' | 'heading' | 'text'."""
    stripped = line.strip()
    if not stripped:
        return "blank"
    if _DECOR_RE.match(line):
        return "decor"
    if _MD_HEADING_RE.match(line):
        return "heading"
    if _SYMBOL_BULLET_RE.match(line) or _ENUM_BULLET_RE.match(line):
        return "bullet"
    # A short, unpunctuated line standing alone between blanks reads as a title.
    if (
        prev_blank
        and next_blank
        and len(stripped) <= _HEADING_MAX_LEN
        and stripped[-1] not in _SENTENCE_TERMINALS
    ):
        return "heading"
    return "text"


def _marker_width(line: str, kind: str) -> int:
    """How many leading characters to drop so the marker isn't spoken."""
    if kind == "heading":
        m = _MD_HEADING_RE.match(line)
        return m.end() if m else 0
    if kind == "bullet":
        m = _SYMBOL_BULLET_RE.match(line)
        if m:
            return m.end()  # symbol bullets are noise — drop them
        # Enumerated markers ("1.", "a)") are meaningful, so keep them.
    return 0


def normalize(raw: str) -> Normalized:
    """Clean ``raw`` and cut it into hard-boundary blocks.

    Returns blocks whose ``text`` is what gets spoken and whose ``pieces`` map
    every character back to an offset in ``raw``.
    """
    lines = list(_iter_lines(raw))
    kinds: List[str] = []
    for idx, (s, e) in enumerate(lines):
        line = raw[s:e]
        prev_blank = idx == 0 or not raw[lines[idx - 1][0] : lines[idx - 1][1]].strip()
        next_blank = idx == len(lines) - 1 or not raw[
            lines[idx + 1][0] : lines[idx + 1][1]
        ].strip()
        kinds.append(_classify(line, prev_blank, next_blank))

    blocks: List[Block] = []
    cur = _BlockBuilder()

    def flush(paragraph_end: bool) -> None:
        nonlocal cur
        blk = cur.finish(paragraph_end)
        if blk is not None:
            blocks.append(blk)
        cur = _BlockBuilder()

    for idx, (s, e) in enumerate(lines):
        kind = kinds[idx]
        line = raw[s:e]

        if kind in ("blank", "decor"):
            flush(True)
            continue

        if kind in ("bullet", "heading"):
            # Structural lines always start a fresh block. Each list item gets a
            # paragraph-length pause after it, which is how a list should read.
            flush(True)
            cur.kind = kind
        elif not cur:
            cur.kind = "para"

        content_start = s + _marker_width(line, kind)

        if cur:
            # Continuation line inside the current block.
            if cur.ends_with_wrap_hyphen() and raw[content_start:e][:1].islower():
                cur.pop_char()  # drop the wrap hyphen, join with no space
            else:
                cur.add_join_space(s)
        cur.add_span(raw, content_start, e)

        # A heading is its own block and always ends a paragraph.
        if kind == "heading":
            flush(True)

    flush(True)

    if blocks:
        blocks[-1].is_paragraph_end = True

    return Normalized(blocks=blocks, original=raw)
