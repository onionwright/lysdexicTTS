"""Background synthesis with bounded lookahead.

This is the piece that makes reading feel instant: sentence 0 is synthesized
alone and playback starts the moment it lands, while a worker renders the next
few sentences during playback. At the measured 2.5-2.9x real-time factor, three
sentences of lookahead means the player never starves after the first.

**Cancellation is about audio, not compute.** A torch forward pass cannot be
interrupted, so this does not pretend otherwise. Every request carries a
generation number that is checked immediately *before* inference starts; stale
requests are dropped having cost nothing. A pass already in flight is allowed to
finish and its result is stored in the cache anyway -- keyed by text, so it costs
nothing and may well be wanted later. Meanwhile the player silences in 8ms, so
Stop feels instant and the orphaned forward is invisible.
"""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Sequence

from ..audio.player import StreamPlayer
from ..text.types import Sentence
from ..tts.base import TtsEngine
from .cache import AudioCache, make_key

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SynthRequest:
    gen: int
    index: int
    text: str
    voice: str
    speed: float
    pause_after_s: float


class SynthScheduler:
    """Owns the synthesis thread and decides what to render next."""

    def __init__(
        self,
        engine: TtsEngine,
        cache: AudioCache,
        player: StreamPlayer,
        *,
        lookahead_sentences: int = 3,
        lookahead_seconds: float = 25.0,
        keep_played_sentences: int = 5,
        on_chunk_ready: Optional[Callable[[int, int], None]] = None,
        on_first_chunk: Optional[Callable[[int], None]] = None,
        on_error: Optional[Callable[[int, BaseException], None]] = None,
    ) -> None:
        self.engine = engine
        self.cache = cache
        self.player = player
        self.lookahead_sentences = lookahead_sentences
        self.lookahead_seconds = lookahead_seconds
        self.keep_played_sentences = keep_played_sentences
        self.on_chunk_ready = on_chunk_ready
        self.on_first_chunk = on_first_chunk
        self.on_error = on_error

        self._q: "queue.PriorityQueue[tuple]" = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        self._gen = 0
        self._sentences: List[Sentence] = []
        self._queued: set[int] = set()
        self._state_lock = threading.Lock()
        self._voice = ""
        self._speed = 1.0

        # Diagnostics, useful for verifying the pipeline under load.
        self.synth_count = 0
        self.synth_seconds = 0.0
        self.audio_seconds = 0.0

    # ----------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="tts-synth", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._running = False
        self._q.put((-1, next(self._seq), None))
        t = self._thread
        if t is not None:
            t.join(timeout=5.0)
        self._thread = None

    # ------------------------------------------------------------- session

    def begin_session(
        self,
        sentences: Sequence[Sentence],
        voice: str,
        speed: float,
        start_index: int = 0,
    ) -> int:
        """Start reading ``sentences``. Returns the new generation number.

        ``start_index`` is where playback will actually resume from, which is
        not always the beginning -- changing voice mid-document restarts the
        session from wherever the reader had got to.
        """
        start_index = max(0, min(start_index, max(0, len(sentences) - 1)))
        with self._state_lock:
            self._gen += 1
            gen = self._gen
            self._sentences = list(sentences)
            self._queued.clear()
            self._voice = voice
            self._speed = speed
        self._drain()
        self.player.set_playlist(len(sentences))
        # That one unit alone, at top priority, so audio can start as early as
        # possible; the rest is queued once it lands.
        self._enqueue(gen, start_index)
        return gen

    def cancel(self, *, keep_session: bool = True) -> None:
        """Invalidate everything in flight. Audio is stopped by the caller.

        The session (the sentence list) is kept by default so the document can
        still be replayed or resumed afterwards. Dropping it would leave
        ``ensure_ahead`` with nothing to schedule, so anything not already
        synthesized when the user pressed Stop could never be rendered -- the
        reader would play its buffered audio and then stall silently forever.
        Pass ``keep_session=False`` when genuinely abandoning the document.
        """
        with self._state_lock:
            self._gen += 1
            self._queued.clear()
            if not keep_session:
                self._sentences = []
        self._drain()

    @property
    def generation(self) -> int:
        return self._gen

    def ensure_ahead(self, from_index: int) -> None:
        """Top up the lookahead window. Cheap; call from a UI timer."""
        with self._state_lock:
            gen = self._gen
            sentences = self._sentences
            if not sentences:
                return
            budget = self.lookahead_seconds
            wanted: List[int] = []
            end = min(len(sentences), from_index + self.lookahead_sentences + 1)
            for i in range(max(0, from_index), end):
                if i in self._queued or self.player.has_chunk(i):
                    if self.player.has_chunk(i):
                        # Already rendered: it still consumes buffer budget.
                        budget -= _chunk_seconds(self.player, i)
                        if budget <= 0:
                            break
                    continue
                wanted.append(i)
                budget -= _estimate_seconds(sentences[i].text)
                if budget <= 0:
                    break
        for i in wanted:
            self._enqueue(gen, i)

        # Keep the recently played window resident so Prev is instant.
        self._repin(from_index)

    # ------------------------------------------------------------- internal

    def _repin(self, cur: int) -> None:
        with self._state_lock:
            sentences = self._sentences
            voice, speed = self._voice, self._speed
            if not sentences:
                return
            lo = max(0, cur - self.keep_played_sentences)
            hi = min(len(sentences), cur + self.lookahead_sentences + 1)
            keys = [
                make_key(self.engine.engine_id, voice, speed, sentences[i].text)
                for i in range(lo, hi)
            ]
        self.cache.pin(keys)

    def _enqueue(self, gen: int, index: int) -> None:
        with self._state_lock:
            if gen != self._gen or not (0 <= index < len(self._sentences)):
                return
            if index in self._queued:
                return
            self._queued.add(index)
            s = self._sentences[index]
            req = SynthRequest(
                gen=gen,
                index=index,
                text=s.text,
                voice=self._voice,
                speed=self._speed,
                pause_after_s=s.pause_after_s,
            )
        # Priority is distance from the playhead: index order is exactly right.
        self._q.put((index, next(self._seq), req))

    def _drain(self) -> None:
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        while self._running:
            try:
                _prio, _seq, req = self._q.get()
            except Exception:
                continue
            if req is None:
                break
            try:
                self._handle(req)
            except Exception as exc:
                # The worker must never die: one bad sentence (a failed lazy
                # voice download, say) must not silently end all future reading.
                log.exception("synthesis failed for sentence %d", req.index)
                if self.on_error is not None:
                    try:
                        self.on_error(req.index, exc)
                    except Exception:
                        pass
            finally:
                with self._state_lock:
                    self._queued.discard(req.index)

    def _handle(self, req: SynthRequest) -> None:
        # Checked immediately before the (uninterruptible) forward pass.
        if req.gen != self._gen:
            return

        key = make_key(self.engine.engine_id, req.voice, req.speed, req.text)
        chunk = self.cache.get(key)
        if chunk is None:
            t0 = time.perf_counter()
            chunk = self.engine.synth(
                req.text,
                voice=req.voice,
                speed=req.speed,
                pause_after_s=req.pause_after_s,
            )
            elapsed = time.perf_counter() - t0
            self.synth_count += 1
            self.synth_seconds += elapsed
            self.audio_seconds += chunk.duration
            self.cache.put(key, chunk)
            log.debug(
                "synth #%d in %.2fs -> %.2fs audio (rtf %.2fx)",
                req.index, elapsed, chunk.duration,
                chunk.duration / elapsed if elapsed else 0.0,
            )

        # Publish only if still current; a stale result stays in the cache.
        if req.gen != self._gen:
            return
        self.player.set_chunk(req.index, chunk.pcm)
        if req.index == 0 and self.on_first_chunk is not None:
            self.on_first_chunk(req.gen)
        if self.on_chunk_ready is not None:
            self.on_chunk_ready(req.gen, req.index)


def _chunk_seconds(player: StreamPlayer, index: int) -> float:
    buf = player._playlist[index] if 0 <= index < player._n else None
    return 0.0 if buf is None else len(buf) / float(player.sample_rate)


def _estimate_seconds(text: str) -> float:
    """Rough duration estimate used only for the prefetch budget.

    Kokoro at speed 1.0 runs near 15 characters per second of speech.
    """
    return max(0.4, len(text) / 15.0)
