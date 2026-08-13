"""The seam between the reader and whatever actually makes sound.

Only ``KokoroEngine`` implements this today, but keeping the interface thin
means an ONNX backend can be dropped in later without the scheduler, player, or
UI noticing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


@dataclass(slots=True)
class WordTiming:
    """Word-level timing, in seconds relative to the start of its chunk.

    kokoro computes these on every call via ``KPipeline.join_timestamps`` and
    the original spike threw them away by unpacking the legacy 3-tuple. They
    cost nothing to keep and are what word-level highlighting will need.
    """

    start: float
    end: float
    text: str


@dataclass(slots=True)
class SynthChunk:
    """Rendered audio for exactly one sentence."""

    pcm: np.ndarray  # float32, mono
    sample_rate: int
    phoneme_len: int = 0
    words: List[WordTiming] = field(default_factory=list)
    # True when the sentence produced no phonemes and this is filler silence.
    is_silence: bool = False

    @property
    def duration(self) -> float:
        return len(self.pcm) / float(self.sample_rate)

    @property
    def nbytes(self) -> int:
        return int(self.pcm.nbytes)


class TtsEngine(ABC):
    """Synthesizes one sentence at a time, synchronously."""

    sample_rate: int = 24000

    @property
    @abstractmethod
    def engine_id(self) -> str:
        """Stable identifier that participates in the audio cache key."""

    @abstractmethod
    def load(self) -> None:
        """Load models. Slow (~7s here); call off the critical path."""

    @property
    @abstractmethod
    def is_loaded(self) -> bool: ...

    @abstractmethod
    def available_voices(self) -> List[str]:
        """Voices that can be used right now without a network fetch."""

    @abstractmethod
    def synth(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        pause_after_s: float = 0.0,
    ) -> SynthChunk:
        """Render ``text``. Must never return ``None`` and never raise for
        ordinary input -- the playlist relies on getting *something* back for
        every sentence index."""
