"""Data types shared by the text pipeline.

The one invariant worth stating up front: ``Sentence.char_start`` / ``char_end``
always index the *original* captured string, never the normalized one. The UI
highlights what the user actually selected, so every transformation in
``normalize`` has to carry an offset map back to the source.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any, List


@dataclass(slots=True)
class Piece:
    """A run of characters copied verbatim from the original into a block.

    ``out_start`` is the offset within the block's text; ``orig_start`` is the
    offset within the original captured string. Characters that normalization
    *inserted* (a joining space, say) get a zero-length piece so they still map
    to a sensible source position.
    """

    out_start: int
    orig_start: int
    length: int


@dataclass(slots=True)
class Block:
    """A hard boundary unit — a paragraph, a bullet, or a heading.

    Sentence splitting never crosses a block, which is what stops bullet lists
    from being glued into one sentence (spaCy's senter merges them happily).
    """

    text: str
    pieces: List[Piece]
    kind: str  # 'para' | 'bullet' | 'heading'
    is_paragraph_end: bool

    def to_orig(self, out_pos: int) -> int:
        """Map an offset in ``self.text`` back to the original string."""
        if not self.pieces:
            return 0
        starts = [p.out_start for p in self.pieces]
        i = bisect.bisect_right(starts, out_pos) - 1
        if i < 0:
            return self.pieces[0].orig_start
        p = self.pieces[i]
        delta = out_pos - p.out_start
        if delta > p.length:
            delta = p.length
        return p.orig_start + delta


@dataclass(slots=True)
class Normalized:
    """Result of ``normalize.normalize()``."""

    blocks: List[Block]
    original: str

    @property
    def text(self) -> str:
        return "\n\n".join(b.text for b in self.blocks)


@dataclass(slots=True)
class Sentence:
    """One playable unit. Index-aligned 1:1 with the player's playlist.

    Keeping that alignment is load-bearing — see ``phonemes.py`` for why
    zero-phoneme sentences must be dropped before this list is built.
    """

    index: int
    text: str
    char_start: int  # into the ORIGINAL captured string
    char_end: int
    phoneme_len: int = 0
    tokens: Any = None  # misaki MTokens, when pre-computed by phonemes.py
    pause_after_s: float = 0.16
    is_paragraph_end: bool = False
    block_kind: str = "para"
    # Set when a long sentence was safety-split; pieces of one sentence share
    # a source range and get a shorter pause between them.
    subchunk_of: int = -1

    def __repr__(self) -> str:  # keeps debug logs readable
        preview = self.text if len(self.text) <= 48 else self.text[:45] + "..."
        return (
            f"Sentence(#{self.index} ph={self.phoneme_len} "
            f"[{self.char_start}:{self.char_end}] {preview!r})"
        )
