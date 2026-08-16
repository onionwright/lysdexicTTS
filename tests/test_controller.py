"""Controller behaviour that does not need a real TTS engine or audio device."""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.audio.player import StreamPlayer  # noqa: E402
from reader.core.cache import AudioCache  # noqa: E402
import reader.core.controller as controller_mod  # noqa: E402
from reader.core.controller import ReaderController  # noqa: E402
from reader.text.splitter import SentenceSplitter  # noqa: E402
from reader.tts.base import SynthChunk, TtsEngine, WordTiming  # noqa: E402

TEXT = (
    "The first sentence is here for testing purposes. "
    "The second sentence follows along nicely after it. "
    "A third sentence closes out this short document."
)


class FakeEngine(TtsEngine):
    sample_rate = 24000

    def __init__(self):
        self.voice = "af_heart"
        self.speed = 1.0
        self.requests = []

    @property
    def engine_id(self):
        return "fake"

    @property
    def is_loaded(self):
        return True

    def load(self):
        pass

    def available_voices(self):
        return ["af_heart", "bm_george"]

    def synth(self, text, *, voice=None, speed=None, pause_after_s=0.0):
        self.requests.append((text, voice, speed))
        return SynthChunk(
            pcm=np.zeros(2400, dtype=np.float32), sample_rate=self.sample_rate
        )


@pytest.fixture
def ctl():
    splitter = SentenceSplitter()
    splitter.warm()
    player = StreamPlayer(24000, blocksize=1024)
    # Never open a real audio device here. jump_to_sentence calls ensure_ready,
    # and leaked PortAudio streams take the interpreter down with an access
    # violation at exit rather than a test failure.
    player._ensure_stream = lambda: None
    controller = ReaderController(
        FakeEngine(),
        splitter=splitter,
        cache=AudioCache(),
        player=player,
        voice="af_heart",
    )
    yield controller
    controller.shutdown()


def test_changing_voice_re_renders_the_loaded_document(ctl):
    """Regression: a new voice used to apply only to the *next* read, so
    choosing one did nothing to the text already on screen."""
    ctl.read(TEXT, autoplay=False)
    generation = ctl.scheduler.generation
    ctl.player.set_chunk(0, np.zeros(100, dtype=np.float32))

    assert ctl.set_voice_and_speed("bm_george", 1.0) is True

    assert ctl.voice == "bm_george"
    assert ctl.scheduler.generation > generation, "a new session must start"
    assert ctl.player._playlist[0] is None, "stale audio must be discarded"
    assert ctl.scheduler._voice == "bm_george", "scheduler renders the new voice"


def test_changing_voice_keeps_your_place(ctl):
    ctl.read(TEXT, autoplay=False)
    assert ctl.total_sentences >= 3
    ctl.jump_to_sentence(2)
    ctl.player._callback(np.zeros((1024, 1), dtype=np.float32), 1024, None, None)
    assert ctl.current_sentence == 2

    ctl.set_voice_and_speed("bm_george", 1.0)
    ctl.player._callback(np.zeros((1024, 1), dtype=np.float32), 1024, None, None)

    assert ctl.current_sentence == 2, "must resume where the reader was"


def test_setting_the_same_voice_is_a_no_op(ctl):
    ctl.read(TEXT, autoplay=False)
    generation = ctl.scheduler.generation
    assert ctl.set_voice_and_speed("af_heart", 1.0) is False
    assert ctl.scheduler.generation == generation, "no needless re-render"


def test_speed_change_also_re_renders(ctl):
    ctl.read(TEXT, autoplay=False)
    generation = ctl.scheduler.generation
    assert ctl.set_voice_and_speed("af_heart", 1.25) is True
    assert ctl.scheduler.generation > generation
    assert abs(ctl.speed - 1.25) < 1e-9


def test_voice_change_without_a_document_just_records_it(ctl):
    assert ctl.set_voice_and_speed("bm_george", 1.0) is True
    assert ctl.voice == "bm_george"
    assert not ctl.has_document


def test_tick_follows_the_default_output_device(ctl):
    """Regression: hearing aids that connect after the app started were ignored
    for as long as the process lived, because a stream stays on the endpoint it
    was opened on."""
    reopened = []
    ctl.player.is_device_alive = lambda: True
    ctl.player.default_device_changed = lambda: True
    ctl.player.reopen = lambda: reopened.append(True)

    ctl.tick()
    assert reopened == [True]

    ctl.tick()  # same second: polling Windows 30 times a second buys nothing
    assert reopened == [True]

    ctl._last_device_poll -= controller_mod.DEVICE_POLL_S
    ctl.tick()
    assert reopened == [True, True]


def test_a_failed_reopen_does_not_take_the_tick_down(ctl):
    """tick() drives the whole UI; an unplugged device must not stop it."""
    ctl.player.is_device_alive = lambda: True
    ctl.player.default_device_changed = lambda: True

    def boom():
        raise RuntimeError("no such device")

    ctl.player.reopen = boom
    ctl.read(TEXT, autoplay=False)

    state = ctl.tick()
    assert state.total_sentences >= 3


def test_autoplay_retries_then_surfaces_a_device_error(ctl):
    """Regression: the latency stamp used to be recorded *before* play(), so
    one device error at the wrong moment disabled autoplay forever and the
    reader sat on 'paused' with no trace of why."""
    calls = []

    def boom():
        calls.append(True)
        raise RuntimeError("device busy")

    ctl.player.play = boom
    ctl.read(TEXT)
    ctl.player.set_chunk(0, np.zeros(100, dtype=np.float32))

    state = ctl.tick()
    assert calls, "tick must attempt to start playback"
    assert ctl.first_audio_latency is None, "failure must not consume autoplay"
    assert not state.audio_error, "first failure retries before surfacing"

    ctl.tick()
    assert len(calls) == 1, "retries wait out the backoff"

    for _ in range(controller_mod.AUTOPLAY_MAX_ATTEMPTS + 2):
        ctl._next_autoplay_t = 0.0
        state = ctl.tick()
    assert len(calls) == controller_mod.AUTOPLAY_MAX_ATTEMPTS
    assert state.audio_error, "giving up must be visible, not silent"


def test_autoplay_records_latency_only_on_success(ctl):
    ctl.player.play = lambda: None
    ctl.read(TEXT)
    ctl.player.set_chunk(0, np.zeros(100, dtype=np.float32))
    ctl.tick()
    assert ctl.first_audio_latency is not None


def test_unconsumed_play_request_reopens_the_device(ctl):
    """A stream can open, claim to be active, and never run the callback; the
    device-alive check can't see that, the stale play intent can."""
    reopened = []
    ctl.read(TEXT, autoplay=False)
    ctl.player.reopen = lambda: reopened.append(True)
    ctl.player._pending = "resume"
    ctl.player._pending_since = 0.0
    ctl.tick()
    assert reopened == [True]
    ctl.tick()
    assert reopened == [True], "re-armed: at most one reopen per interval"


def test_word_text_follows_the_playhead(ctl):
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True  # keep the tick off PortAudio
    sr = ctl.player.sample_rate
    words = [
        WordTiming(0.0, 0.1, "The"),
        WordTiming(0.1, 0.2, "first"),
        WordTiming(0.2, 0.3, "—"),  # punctuation token: filtered out
        WordTiming(0.3, 0.4, "sentence"),
    ]
    ctl.player.set_chunk(0, np.zeros(sr, dtype=np.float32), words)
    ctl.player._paused = False

    ctl.player.pos = 0
    assert ctl.tick().word_text == "The"
    ctl.player.pos = int(0.15 * sr)
    assert ctl.tick().word_text == "first"
    ctl.player.pos = int(0.25 * sr)
    assert ctl.tick().word_text == "first", "holds through filtered tokens"
    ctl.player.pos = int(0.35 * sr)
    assert ctl.tick().word_text == "sentence"


def test_word_text_switches_at_unit_boundaries(ctl):
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True  # keep the tick off PortAudio
    ctl.player.set_chunk(
        0, np.zeros(100, dtype=np.float32), [WordTiming(0.0, 0.1, "one")]
    )
    ctl.player.set_chunk(
        1, np.zeros(100, dtype=np.float32), [WordTiming(0.0, 0.1, "two")]
    )
    ctl.player._paused = False
    ctl.player.pos = 10
    assert ctl.tick().word_text == "one"
    ctl.player.cur_index = 1
    ctl.player.pos = 10
    assert ctl.tick().word_text == "two"


def test_word_display_compensates_for_output_latency(ctl):
    """pos counts frames *written* to the device, which leads the ear by the
    buffering; the displayed word must be looked up at the audible time."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    ctl.player.output_latency_s = lambda: 0.2
    sr = ctl.player.sample_rate
    words = [WordTiming(0.0, 0.1, "one"), WordTiming(0.3, 0.4, "two")]
    ctl.player.set_chunk(0, np.zeros(sr, dtype=np.float32), words)
    ctl.player._paused = False

    ctl.player.pos = int(0.35 * sr)  # written past "two"; the ear is at 0.15
    assert ctl.tick().word_text == "one"
    ctl.player.pos = int(0.55 * sr)
    assert ctl.tick().word_text == "two"


def test_extra_display_delay_is_applied(ctl):
    """Bluetooth sinks add delay PortAudio cannot see; the user-set extra
    delay shifts the display the same way."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    ctl.rsvp_extra_delay_s = 0.2
    sr = ctl.player.sample_rate
    words = [WordTiming(0.0, 0.1, "one"), WordTiming(0.3, 0.4, "two")]
    ctl.player.set_chunk(0, np.zeros(sr, dtype=np.float32), words)
    ctl.player._paused = False

    ctl.player.pos = int(0.35 * sr)
    assert ctl.tick().word_text == "one"


def test_word_display_holds_across_unit_boundaries(ctl):
    """Right after a unit switch the new unit's frames haven't reached the
    speaker; the strip keeps the old unit's word rather than going blank."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    ctl.player.output_latency_s = lambda: 0.2
    sr = ctl.player.sample_rate
    ctl.player.set_chunk(
        0, np.zeros(sr, dtype=np.float32), [WordTiming(0.0, 0.1, "one")]
    )
    ctl.player.set_chunk(
        1, np.zeros(sr, dtype=np.float32), [WordTiming(0.0, 0.1, "two")]
    )
    ctl.player._paused = False
    ctl.player.pos = int(0.5 * sr)
    assert ctl.tick().word_text == "one"

    ctl.player.cur_index = 1
    ctl.player.pos = int(0.05 * sr)  # written 50ms into unit 1; ear still behind
    assert ctl.tick().word_text == "one", "the ear hasn't reached unit 1 yet"
    ctl.player.pos = int(0.25 * sr)
    assert ctl.tick().word_text == "two"


def test_words_arriving_after_playback_starts_are_picked_up(ctl):
    """Regression: the first tick could run before chunk 0 was published; the
    miss was cached and the whole first block stayed blank on the strip."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    ctl.player._paused = False
    ctl.player.pos = 10
    assert ctl.tick().word_text == ""  # nothing published yet

    ctl.player.set_chunk(
        0, np.zeros(100, dtype=np.float32), [WordTiming(0.0, 0.1, "one")]
    )
    assert ctl.tick().word_text == "one", "must refetch once the chunk lands"


def test_zero_width_first_word_still_gets_displayed(ctl):
    """Regression: lead-trimming can collapse a quiet opening word to a
    zero-width span sharing start 0.0 with the next word; it must still get a
    minimum display slot instead of being skipped."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    sr = ctl.player.sample_rate
    words = [
        WordTiming(0.0, 0.0, "The"),      # collapsed by trim
        WordTiming(0.0, 0.142, "quick"),  # clamped to the same start
        WordTiming(0.3, 0.5, "brown"),
    ]
    ctl.player.set_chunk(0, np.zeros(sr, dtype=np.float32), words)
    ctl.player._paused = False

    ctl.player.pos = 1
    assert ctl.tick().word_text == "The"
    ctl.player.pos = int(0.1 * sr)
    assert ctl.tick().word_text == "quick"
    ctl.player.pos = int(0.35 * sr)
    assert ctl.tick().word_text == "brown"


def test_backwards_jump_does_not_hold_a_stale_word(ctl):
    """After jumping back, the pre-jump word must not linger through the
    latency window -- blank until the restarted audio reaches its first word."""
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True
    ctl.player.output_latency_s = lambda: 0.2
    sr = ctl.player.sample_rate
    ctl.player.set_chunk(
        0, np.zeros(sr, dtype=np.float32), [WordTiming(0.05, 0.1, "one")]
    )
    ctl.player.set_chunk(
        1, np.zeros(sr, dtype=np.float32), [WordTiming(0.0, 0.1, "two")]
    )
    ctl.player._paused = False
    ctl.player.cur_index = 1
    ctl.player.pos = int(0.5 * sr)
    assert ctl.tick().word_text == "two"

    ctl.player.cur_index = 0  # jumped back to the start
    ctl.player.pos = 0
    assert ctl.tick().word_text == "", "stale word must not survive a jump"
    ctl.player.pos = int(0.3 * sr)  # audible time now past "one"'s start
    assert ctl.tick().word_text == "one"


def test_word_text_is_empty_without_timings(ctl):
    ctl.read(TEXT, autoplay=False)
    ctl.player.is_device_alive = lambda: True  # keep the tick off PortAudio
    ctl.player.set_chunk(0, np.zeros(100, dtype=np.float32))
    ctl.player._paused = False
    assert ctl.tick().word_text == ""


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
