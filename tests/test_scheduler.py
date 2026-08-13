"""Scheduler session-lifetime tests.

These cover the difference between "stop the audio" and "throw the document
away", which is the distinction that made a stopped or finished read
unrecoverable.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.audio.player import StreamPlayer  # noqa: E402
from reader.core.cache import AudioCache  # noqa: E402
from reader.core.scheduler import SynthScheduler  # noqa: E402
from reader.text.units import Unit  # noqa: E402
from reader.tts.base import SynthChunk, TtsEngine  # noqa: E402


class FakeEngine(TtsEngine):
    """Synthesizes instantly, so no torch and no audio device are involved."""

    sample_rate = 24000

    def __init__(self):
        self.calls = []

    @property
    def engine_id(self):
        return "fake"

    @property
    def is_loaded(self):
        return True

    def load(self):
        pass

    def available_voices(self):
        return ["fake_voice"]

    def synth(self, text, *, voice=None, speed=None, pause_after_s=0.0):
        self.calls.append(text)
        return SynthChunk(
            pcm=np.zeros(2400, dtype=np.float32), sample_rate=self.sample_rate
        )


def make_units(n=4):
    return [
        Unit(
            index=i,
            sentence_index=i,
            text=f"Sentence number {i} with enough text to be real.",
            pause_after_s=0.16,
            is_sentence_start=True,
            is_sentence_end=True,
        )
        for i in range(n)
    ]


@pytest.fixture
def rig():
    engine = FakeEngine()
    player = StreamPlayer(24000, blocksize=1024)
    sched = SynthScheduler(engine, AudioCache(), player)
    return engine, player, sched


def test_cancel_keeps_the_document_replayable(rig):
    """Regression: cancel() cleared the sentence list, so ensure_ahead had
    nothing left to schedule and anything not yet synthesized when the user
    pressed Stop could never be rendered -- replay would stall forever."""
    _engine, _player, sched = rig
    sched.begin_session(make_units(), "fake_voice", 1.0)

    sched.cancel()

    assert sched._sentences, "the session must survive a cancel"
    while not sched._q.empty():
        sched._q.get_nowait()
    sched.ensure_ahead(0)
    assert not sched._q.empty(), "prefetch must resume after a cancel"


def test_cancel_can_still_abandon_the_document(rig):
    _engine, _player, sched = rig
    sched.begin_session(make_units(), "fake_voice", 1.0)

    sched.cancel(keep_session=False)

    assert not sched._sentences
    sched.ensure_ahead(0)
    assert sched._q.empty(), "nothing should be scheduled once abandoned"


def test_cancel_invalidates_work_already_queued(rig):
    """Stale requests must be dropped rather than played into a new session."""
    _engine, _player, sched = rig
    sched.begin_session(make_units(), "fake_voice", 1.0)
    before = sched.generation

    sched.cancel()

    assert sched.generation > before
    assert sched._q.empty(), "the queue is drained on cancel"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
