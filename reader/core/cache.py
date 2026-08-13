"""LRU cache of synthesized sentences, bounded by total audio duration.

Keyed on the sentence *text* rather than its index. That one choice buys several
behaviours for free: re-reading the same paragraph is instant, hammering
next/prev costs nothing, the cache survives a re-split when normalization
settings change, and a synthesis that was already in flight when the user
cancelled can still be stored usefully instead of thrown away.

Bounded by seconds, not entry count, because sentence lengths vary by an order
of magnitude. float32 at 24kHz is 96 KB/s, so the 300s default is about 29 MB.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from typing import Iterable, Optional, Set

from ..tts.base import SynthChunk


def make_key(engine_id: str, voice: str, speed: float, text: str) -> str:
    h = hashlib.sha1()
    h.update(engine_id.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(voice.encode("utf-8", "replace"))
    h.update(b"\x00")
    h.update(f"{speed:.3f}".encode("ascii"))
    h.update(b"\x00")
    h.update(text.encode("utf-8", "replace"))
    return h.hexdigest()


class AudioCache:
    def __init__(self, max_seconds: float = 300.0) -> None:
        self.max_seconds = max_seconds
        self._items: "OrderedDict[str, SynthChunk]" = OrderedDict()
        self._seconds = 0.0
        self._pinned: Set[str] = set()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[SynthChunk]:
        with self._lock:
            chunk = self._items.get(key)
            if chunk is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return chunk

    def put(self, key: str, chunk: SynthChunk) -> None:
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return
            self._items[key] = chunk
            self._seconds += chunk.duration
            self._evict_locked()

    def pin(self, keys: Iterable[str]) -> None:
        """Protect these keys from eviction (the recently played window, so
        stepping backwards is instant)."""
        with self._lock:
            self._pinned = set(keys)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._pinned.clear()
            self._seconds = 0.0

    @property
    def seconds(self) -> float:
        return self._seconds

    def __len__(self) -> int:
        return len(self._items)

    def _evict_locked(self) -> None:
        if self._seconds <= self.max_seconds:
            return
        for key in list(self._items.keys()):
            if self._seconds <= self.max_seconds:
                break
            if key in self._pinned:
                continue
            chunk = self._items.pop(key)
            self._seconds -= chunk.duration
