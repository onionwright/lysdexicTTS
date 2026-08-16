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

import bisect
import logging
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from ..audio.player import StreamPlayer
from ..text.splitter import SentenceSplitter, is_speakable
from ..text.types import Sentence
from ..text.units import Unit, build_units, first_unit_of_sentence
from ..tts.base import TtsEngine
from .cache import AudioCache
from .scheduler import SynthScheduler

log = logging.getLogger(__name__)

# How often to ask Windows whether the default output has moved. A COM call is
# far too cheap to matter at this rate and far too expensive to make 30 times a
# second, and a second of audio still coming out of the wrong speaker is not
# something anyone notices on top of the pairing delay that caused it.
DEVICE_POLL_S = 1.0

# Starting playback can fail transiently (a Bluetooth endpoint mid-handoff,
# say). Retry on a slow clock, then surface the failure -- the one unacceptable
# outcome is a silent, permanent "paused".
AUTOPLAY_RETRY_S = 1.0
AUTOPLAY_MAX_ATTEMPTS = 5
# How long a play request may sit unconsumed before the stream is presumed
# wedged and reopened.
PLAY_STALE_S = 1.0

# Minimum display time for one word. Timings can degenerate -- lead-trimming
# occasionally cuts into a quiet opening word, collapsing it to a zero-width
# span that shares its start with the next word -- and a word with no span can
# never be shown.
MIN_WORD_SLOT_S = 0.06


@dataclass(slots=True)
class ReaderState:
    """Snapshot returned by :meth:`ReaderController.tick`."""

    sentence_index: int
    sentence_changed: bool
    playing: bool
    finished: bool
    starved: bool
    total_sentences: int
    word_text: str = ""
    audio_error: bool = False


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
        self._last_device_poll = 0.0
        self._audio_error = False
        self._autoplay_attempts = 0
        self._next_autoplay_t = 0.0
        self._last_callback_error: Optional[str] = None
        # Word-timing lookup for the unit currently playing, rebuilt lazily.
        self._word_unit = -1
        self._word_starts: List[float] = []
        self._word_texts: List[str] = []
        self._word_hold = ""
        # Extra display delay on top of the stream's reported latency, for
        # sink-side buffering (Bluetooth) that PortAudio cannot see.
        self.rsvp_extra_delay_s = 0.0

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
        self._audio_error = False
        self._autoplay_attempts = 0
        self._next_autoplay_t = 0.0
        self._reset_word_lookup()
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
        self._reset_word_lookup()
        self.player.jump_to(start_unit)
        if was_playing:
            self.play()
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
        # Guarded: play() opens the audio device, and a device failure raised
        # into a Qt slot goes to a stderr that points nowhere under pythonw.
        try:
            self.player.play()
            self._audio_error = False
        except Exception:
            self._audio_error = True
            log.exception("could not start playback")

    def pause(self) -> None:
        self.player.pause()

    def toggle(self) -> bool:
        try:
            result = self.player.toggle()
            self._audio_error = False
            return result
        except Exception:
            self._audio_error = True
            log.exception("could not toggle playback")
            return False

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
        try:
            self.player.ensure_ready()
        except Exception:
            self._audio_error = True
            log.exception("could not open the audio device for a jump")
            return
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

    def _follow_default_device(self) -> None:
        """Move playback to the default output when Windows moves it.

        A stream is bound to the endpoint it was opened on, so this does not
        happen by itself. It matters most in the case that looks least like a
        bug: the reader starts at sign-in and opens the device straight away,
        hearing aids or a headset finish pairing a few seconds later and become
        the default, and every other application follows while this one carries
        on playing to whatever the machine was using at boot.

        Checked whether or not audio is playing, because with the comfort noise
        on, the stream is held open the whole time the app is running -- so by
        the time anything is read aloud it is already on the wrong device.
        """
        now = time.monotonic()
        if now - self._last_device_poll < DEVICE_POLL_S:
            return
        self._last_device_poll = now
        try:
            if not self.player.default_device_changed():
                return
            self.player.reopen()
        except Exception:
            log.exception("failed to follow the default output device")
            return
        log.info("default output device changed; moved playback to it")

    def _try_autoplay(self) -> None:
        """Start playback once the first chunk exists -- and keep trying.

        The latency stamp is recorded only after play() *succeeds*. Stamping it
        first (as this once did) turned any device error at exactly the wrong
        moment into a permanent silent "paused": the guard became false forever
        and the exception vanished into a stderr that points nowhere.
        """
        if (
            not self._autoplay
            or self.first_audio_latency is not None
            or not self.player.has_chunk(0)
        ):
            return
        now = time.monotonic()
        if now < self._next_autoplay_t:
            return
        try:
            self.player.play()
        except Exception:
            self._autoplay_attempts += 1
            if self._autoplay_attempts >= AUTOPLAY_MAX_ATTEMPTS:
                self._autoplay = False
                self._audio_error = True
                log.exception(
                    "could not start playback after %d attempts; giving up",
                    self._autoplay_attempts,
                )
            else:
                self._next_autoplay_t = now + AUTOPLAY_RETRY_S
                log.warning(
                    "could not start playback (attempt %d/%d); retrying",
                    self._autoplay_attempts, AUTOPLAY_MAX_ATTEMPTS,
                    exc_info=True,
                )
        else:
            self.first_audio_latency = time.perf_counter() - self._request_t
            self._audio_error = False

    def _surface_callback_error(self) -> None:
        err = self.player.callback_error
        if err and err != self._last_callback_error:
            self._last_callback_error = err
            log.error("audio callback fault: %s", err)

    def tick(self) -> ReaderState:
        """Advance bookkeeping. Call about every 33ms from the UI thread."""
        cur_unit = self.player.cur_index
        self.scheduler.ensure_ahead(cur_unit)

        self._try_autoplay()
        self._surface_callback_error()

        # Device loss (Bluetooth drop, dock undock) shows up as an inactive
        # stream; reopening preserves position, so it costs one fade-in.
        if self.player.is_device_alive() is False and self.player.is_playing:
            log.warning("output device went away; reopening")
            try:
                self.player.reopen()
            except Exception:
                log.exception("failed to reopen the output device")
        elif self.player.play_request_stale(PLAY_STALE_S):
            # The stream claims to be active but never consumed the play
            # request: a wedged endpoint. The check above can't see it because
            # is_playing is still False.
            log.warning("play request unconsumed; reopening the output device")
            try:
                self.player.reopen()
            except Exception:
                log.exception("failed to reopen the output device")
        else:
            self._follow_default_device()

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
            word_text=self._current_word(cur_unit),
            audio_error=self._audio_error,
        )

    # ---------------------------------------------------------------- words

    def _reset_word_lookup(self) -> None:
        self._word_unit = -1
        self._word_starts = []
        self._word_texts = []
        self._word_hold = ""

    def _current_word(self, cur_unit: int) -> str:
        """Word being spoken right now, or "" when there isn't one.

        Pure function of the player's published position, recomputed per tick,
        which is what makes seeks resync for free -- and why pausing keeps the
        word under the held playhead on show. The rule is "last word whose
        start is behind the playhead": ignoring end times deliberately holds
        the word through inter-word gaps and pause padding instead of
        flickering blank between words.
        """
        if cur_unit != self._word_unit:
            if cur_unit != self._word_unit + 1:
                # A seek or restart, not a natural roll-over: the held word
                # belongs to unrelated text now.
                self._word_hold = ""
            words = self.player.get_words(cur_unit)
            if words is None:
                # Not published yet -- the session just started or the unit is
                # still synthesizing. Retry next tick; caching this miss would
                # leave the whole unit blank even after its words arrive.
                return self._word_hold
            self._word_unit = cur_unit
            starts: List[float] = []
            texts: List[str] = []
            last: Optional[float] = None
            for w in words:
                # misaki emits punctuation tokens with timestamps too; a strip
                # flashing "--" between words is noise, so keep speech only.
                if not is_speakable(w.text):
                    continue
                s = w.start
                if last is not None and s < last + MIN_WORD_SLOT_S:
                    s = last + MIN_WORD_SLOT_S
                starts.append(s)
                texts.append(w.text)
                last = s
            self._word_starts = starts
            self._word_texts = texts
        if not self._word_starts:
            return ""
        # cur_unit and pos are read without a lock; at a unit boundary this can
        # pair the old unit's words with the new unit's position for one tick,
        # which self-corrects on the next.
        #
        # pos runs ahead of the ear by the device buffering, so the displayed
        # word must be looked up at the *audible* time, not the written one --
        # without this the strip led the voice by a couple of words.
        t = (
            self.player.position_seconds()
            - self.player.output_latency_s()
            - self.rsvp_extra_delay_s
        )
        i = bisect.bisect_right(self._word_starts, t) - 1
        if i < 0:
            # Audibly still in the previous unit's tail (the new unit's frames
            # haven't reached the speaker yet): keep showing its last word.
            return self._word_hold
        self._word_hold = self._word_texts[i]
        return self._word_hold
