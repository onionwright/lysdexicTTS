"""Everything above the audio device, in one object.

The controller is what a UI talks to. It owns the splitter, the unit expansion,
the scheduler and the player, and it translates *sentence*-level intent ("next
sentence") into *unit*-level playlist jumps, since a long sentence may occupy
several playlist entries.

It deliberately exposes no callbacks into the audio path. Progress is published
as plain state that a caller polls from ``tick()`` on a timer -- the audio
callback must never emit signals or take locks, so a 33ms poll is how the UI
stays in sync.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..audio.player import StreamPlayer
from ..text.splitter import SentenceSplitter
from ..text.types import Sentence
from ..text.units import Unit, build_units, first_unit_of_sentence
from ..tts.base import TtsEngine
from .cache import AudioCache
from .scheduler import SynthScheduler

log = logging.getLogger(__name__)


@dataclass(slots=True)
class ReaderState:
    """Snapshot returned by :meth:`ReaderController.tick`."""

    sentence_index: int
    sentence_changed: bool
    playing: bool
    finished: bool
    starved: bool
    total_sentences: int


class ReaderController:
    def __init__(
        self,
        engine: TtsEngine,
        *,
        splitter: Optional[SentenceSplitter] = None,
        cache: Optional[AudioCache] = None,
        player: Optional[StreamPlayer] = None,
        voice: Optional[str] = None,
        speed: float = 1.0,
        lookahead_sentences: int = 3,
        prev_restart_threshold_s: float = 2.0,
        blocksize: int = 2048,
        device: Optional[str | int] = None,
    ) -> None:
        self.engine = engine
        self.splitter = splitter or SentenceSplitter()
        self.cache = cache or AudioCache()
        self.player = player or StreamPlayer(
            engine.sample_rate, blocksize=blocksize, device=device
        )
        self.voice = voice or getattr(engine, "voice", "af_heart")
        self.speed = speed
        self.prev_restart_threshold_s = prev_restart_threshold_s

        self.scheduler = SynthScheduler(
            engine,
            self.cache,
            self.player,
            lookahead_sentences=lookahead_sentences,
        )

        self.raw_text = ""
        self._sentences: List[Sentence] = []
        self._units: List[Unit] = []
        self._firsts: List[int] = []
        self._last_boundary = -1
        self._last_sentence = -1
        self._autoplay = False
        self._request_t = 0.0
        self.first_audio_latency: Optional[float] = None
        self.truncated = False

    # ---------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.scheduler.start()

    def shutdown(self) -> None:
        try:
            self.player.stop()
            time.sleep(0.05)
            self.player.close()
        finally:
            self.scheduler.shutdown()

    # ------------------------------------------------------------ reading

    @property
    def sentences(self) -> Sequence[Sentence]:
        return self._sentences

    @property
    def total_sentences(self) -> int:
        return len(self._sentences)

    def read(self, raw_text: str, *, autoplay: bool = True) -> int:
        """Begin reading ``raw_text``. Returns the sentence count."""
        self.player.stop()
        # Normalize line endings once, here, before anything computes an offset.
        # Sentence char offsets index this string, and a UI showing the text in
        # a QTextDocument counts CRLF as one character -- so leaving CR in would
        # drift every highlight by one per preceding line.
        raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        self.raw_text = raw_text
        sentences, truncated = self.splitter.split(raw_text)
        self.truncated = truncated
        self._sentences = list(sentences)
        self._units = build_units(self._sentences)
        self._firsts = first_unit_of_sentence(self._units, len(self._sentences))
        self._last_boundary = -1
        self._last_sentence = -1
        self._autoplay = autoplay
        self.first_audio_latency = None
        self._request_t = time.perf_counter()
        if not self._sentences:
            return 0
        log.debug(
            "reading %d sentences as %d units", len(self._sentences), len(self._units)
        )
        self.scheduler.begin_session(self._units, self.voice, self.speed)
        return len(self._sentences)

    @property
    def has_document(self) -> bool:
        """True while something is loaded and can be played, whether or not it
        is currently playing. Transport should be driven by this, not by
        whether audio happens to be running."""
        return bool(self._units)

    def set_voice_and_speed(self, voice: str, speed: float) -> bool:
        """Change voice or speed, re-rendering the loaded document in place.

        Returns True if anything changed. Audio already rendered belongs to the
        old voice, so a loaded document has to be re-synthesized from wherever
        the reader currently is -- otherwise choosing a new voice appears to do
        nothing until the next read, which is exactly how it feels broken.

        The old audio stays in the cache (keyed by voice and speed as well as
        text), so switching back is instant.
        """
        speed = float(speed)
        if voice == self.voice and abs(speed - self.speed) < 1e-6:
            return False

        self.voice = voice
        self.speed = speed
        if not self._units:
            return True

        # Re-render from the sentence being read, not from the top.
        sentence = min(self.current_sentence, max(0, len(self._sentences) - 1))
        start_unit = self._firsts[sentence] if self._firsts else 0
        was_playing = self.player.is_playing

        log.debug(
            "re-rendering from sentence %d with voice=%s speed=%.2f",
            sentence, voice, speed,
        )
        self.scheduler.begin_session(self._units, voice, speed, start_unit)
        self._last_boundary = -1
        self._last_sentence = -1
        self.player.jump_to(start_unit)
        if was_playing:
            self.player.play()
        return True

    def stop(self) -> None:
        """Silence immediately and rewind, keeping the document replayable."""
        self.player.stop()
        self.scheduler.cancel(keep_session=True)
        self._autoplay = False
        self._last_sentence = -1

    def clear(self) -> None:
        """Abandon the document entirely."""
        self.player.stop()
        self.scheduler.cancel(keep_session=False)
        self._autoplay = False
        self._sentences = []
        self._units = []
        self._firsts = []

    # ---------------------------------------------------------- transport

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def toggle(self) -> bool:
        return self.player.toggle()

    @property
    def is_playing(self) -> bool:
        return self.player.is_playing

    @property
    def current_sentence(self) -> int:
        return self._sentence_of_unit(self.player.cur_index)

    def _sentence_of_unit(self, unit_index: int) -> int:
        if not self._units:
            return 0
        if unit_index >= len(self._units):
            return len(self._sentences)
        return self._units[max(0, unit_index)].sentence_index

    def _base_sentence(self) -> int:
        """Sentence the next transport press should be measured from."""
        pending = self.player.pending_jump_target()
        if pending is None:
            return self.current_sentence
        return self._sentence_of_unit(pending)

    def jump_to_sentence(self, sentence_index: int) -> None:
        if not self._units:
            return
        # Jumps are applied by the audio callback, so the device has to be open
        # or the request would never be consumed.
        self.player.ensure_ready()
        if sentence_index >= len(self._sentences):
            self.player.jump_to(len(self._units))  # past the end == finished
        else:
            self.player.jump_to(self._firsts[max(0, sentence_index)])

    def next_sentence(self) -> None:
        self.jump_to_sentence(self._base_sentence() + 1)

    def prev_sentence(self) -> None:
        """Restart the current sentence, or step back if barely into it.

        Same rule every music player uses, and correct for the same reason:
        reaching for "back" usually means "say that again".
        """
        si = self._base_sentence()
        if si >= len(self._sentences):
            self.jump_to_sentence(max(0, len(self._sentences) - 1))
            return
        first_unit = self._firsts[si]
        # Being past the sentence's first unit already means we're well into it.
        into_sentence = (
            self.player.cur_index > first_unit
            or self.player.position_seconds() > self.prev_restart_threshold_s
        )
        self.jump_to_sentence(si if into_sentence else si - 1)

    # --------------------------------------------------------------- tick

    def tick(self) -> ReaderState:
        """Advance bookkeeping. Call about every 33ms from the UI thread."""
        cur_unit = self.player.cur_index
        self.scheduler.ensure_ahead(cur_unit)

        if (
            self._autoplay
            and self.first_audio_latency is None
            and self.player.has_chunk(0)
        ):
            self.first_audio_latency = time.perf_counter() - self._request_t
            self.player.play()

        # Device loss (Bluetooth drop, dock undock) shows up as an inactive
        # stream; reopening preserves position, so it costs one fade-in.
        if self.player.is_device_alive() is False and self.player.is_playing:
            log.warning("output device went away; reopening")
            try:
                self.player.reopen()
            except Exception:
                log.exception("failed to reopen the output device")

        sentence = self.current_sentence
        changed = (
            self.player.boundary_seq != self._last_boundary
            and sentence != self._last_sentence
        )
        if changed:
            self._last_sentence = sentence
        self._last_boundary = self.player.boundary_seq

        return ReaderState(
            sentence_index=sentence,
            sentence_changed=changed,
            playing=self.player.is_playing,
            finished=self.player.finished,
            starved=self.player.starved,
            total_sentences=len(self._sentences),
        )
