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
from reader.tts.base import SynthChunk, TtsEngine  # noqa: E402

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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
