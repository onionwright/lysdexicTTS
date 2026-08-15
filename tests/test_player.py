"""Deterministic transport tests: the callback is driven by hand, no device.

Run directly (``python tests/test_player.py``) or under pytest.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import reader.audio.player as player_mod  # noqa: E402
from reader.audio.player import StreamPlayer  # noqa: E402

SR = 24000
FRAMES = 1024


def pull(p, frames=FRAMES):
    """Run one callback and return the mono output."""
    out = np.zeros((frames, 1), dtype=np.float32)
    p._callback(out, frames, None, None)
    return out[:, 0].copy()


def make_player(lens=(3000, 3000, 3000), vals=(1.0, 2.0, 3.0), missing=()):
    p = StreamPlayer(SR, blocksize=FRAMES, fade_ms=8.0)
    p.set_playlist(len(lens))
    for i, (n, v) in enumerate(zip(lens, vals)):
        if i not in missing:
            p.set_chunk(i, np.full(n, v, dtype=np.float32))
    # play() would open a real device; this is what it sets afterwards.
    p._pending = "resume"
    return p


def test_fade_in_then_steady():
    p = make_player()
    b = pull(p)
    assert abs(b[0]) < 1e-6, "fade must start at silence"
    assert abs(b[191] - 1.0) < 0.02, "8ms fade completes in ~192 frames"
    assert np.allclose(b[300:], 1.0)
    assert p.pos == FRAMES


def test_sentence_boundary_advances():
    p = make_player()
    seq = p.boundary_seq
    pull(p), pull(p), pull(p)  # past the 3000-frame first chunk
    assert p.cur_index == 1
    assert p.boundary_seq > seq
    assert p.callback_error is None


def test_starvation_emits_silence_and_holds_position():
    p = make_player(missing=(1,))
    pull(p), pull(p), pull(p)
    idx, pos = p.cur_index, p.pos
    b = pull(p)
    assert p.starved
    assert np.allclose(b, 0.0), "must emit silence, not stop the device"
    assert (p.cur_index, p.pos) == (idx, pos), "position must be held"
    assert p.starve_events == 1
    p.set_chunk(1, np.full(3000, 2.0, dtype=np.float32))
    b = pull(p)
    assert abs(b[500] - 2.0) < 1e-6, "resumes once the chunk lands"
    assert not p.starved


def test_next_fades_both_sides_of_the_cut():
    p = make_player()
    pull(p)
    p.next_sentence()
    b = pull(p)
    assert abs(b[0] - 1.0) < 0.02, "fade-out starts from the old audio"
    assert abs(b[191]) < 0.05, "old audio ramps to zero"
    assert abs(b[192]) < 0.05, "new audio starts from zero"
    assert abs(b[383] - 2.0) < 0.05, "new audio ramps up"
    assert p.cur_index == 1


def test_rapid_next_presses_do_not_collapse():
    p = make_player()
    pull(p)
    p.next_sentence()
    p.next_sentence()  # before the callback consumed the first
    pull(p)
    assert p.cur_index == 2


@pytest.mark.parametrize(
    "elapsed_s,expected_index",
    [(3.0, 1), (0.5, 0)],  # >2s restarts the current sentence; <2s steps back
)
def test_prev_restart_rule(elapsed_s, expected_index):
    p = make_player(lens=(SR * 5, SR * 5, SR * 5))
    p.cur_index = 1
    p.pos = int(SR * elapsed_s)
    p.prev_sentence(restart_threshold_s=2.0)
    pull(p)
    assert p.cur_index == expected_index


def test_pause_holds_position_and_resume_fades_in():
    p = make_player()
    pull(p)
    p.pause()
    b = pull(p)
    assert abs(b[0] - 1.0) < 0.02, "pause fade-out starts at full amplitude"
    assert np.allclose(b[192:], 0.0)
    frozen = p.pos
    assert np.allclose(pull(p), 0.0), "stays silent while paused"
    assert p.pos == frozen, "position frozen while paused"
    p._pending = "resume"
    b = pull(p)
    assert abs(b[0]) < 1e-6 and abs(b[300] - 1.0) < 0.02


def test_stop_silences_and_rewinds():
    p = make_player()
    pull(p)
    p.stop()
    b = pull(p)
    assert np.allclose(b[192:], 0.0)
    assert (p.cur_index, p.pos) == (0, 0)
    assert not p.is_playing


def test_end_of_playlist_sets_finished():
    p = make_player(lens=(1000, 1000, 1000))
    for _ in range(6):
        pull(p)
    assert p.finished
    assert p.callback_error is None


def test_play_after_finishing_replays_from_the_beginning():
    """Pressing play on a finished document must not be a no-op.

    Regression: play() only set 'resume', which cleared the pause flag while
    cur_index was still past the last sentence, so the callback immediately
    re-finished and nothing ever played again.
    """
    # Chunks longer than one callback block, so a single pull cannot itself
    # cross a sentence boundary and muddy the assertion.
    p = make_player(lens=(3000, 3000, 3000))
    for _ in range(12):
        pull(p)
    assert p.finished

    p._ensure_stream = lambda: None  # don't open a real device in tests
    p.play()
    b = pull(p)

    assert p.cur_index == 0, "should be back at the first sentence"
    assert not p.finished
    assert p.is_playing
    assert abs(b[300] - 1.0) < 0.05, "audio must actually resume"


def test_play_midway_resumes_and_does_not_rewind():
    """The replay behaviour must not turn an ordinary un-pause into a restart."""
    p = make_player(lens=(SR, SR, SR))
    pull(p)
    p.pause()
    pull(p)
    frozen = p.pos
    assert frozen > 0

    p._ensure_stream = lambda: None
    p.play()
    pull(p)

    assert p.cur_index == 0
    assert p.pos > frozen, "resumed from where it paused, not from the start"


def test_jump_after_finishing_clears_finished():
    """Using next/back to pick a restart point has to revive the player."""
    p = make_player(lens=(3000, 3000, 3000))
    for _ in range(12):
        pull(p)
    assert p.finished

    p.jump_to(1)
    pull(p)
    assert p.cur_index == 1
    assert not p.finished


class FakeStream:
    """Stands in for an open sounddevice stream. Only truthiness and ``active``
    are ever looked at outside the callback."""

    active = True


def test_default_device_change_is_noticed(monkeypatch):
    """Regression: the reader kept playing to whatever was default when it
    started. Autostart made that reliably wrong -- it opens the device at
    sign-in, and hearing aids finish pairing seconds later."""
    p = make_player()
    p._stream = FakeStream()
    p._opened_on = "endpoint-tv"
    monkeypatch.setattr(player_mod, "_default_endpoint_id", lambda: "endpoint-aids")
    assert p.default_device_changed()


def test_a_pinned_device_is_never_second_guessed(monkeypatch):
    """Choosing a device explicitly means that device, not whatever Windows
    currently prefers."""
    p = make_player()
    p._stream = FakeStream()
    p._opened_on = "endpoint-tv"
    p.device = "Speakers (USB Audio)"
    monkeypatch.setattr(player_mod, "_default_endpoint_id", lambda: "endpoint-aids")
    assert not p.default_device_changed()


@pytest.mark.parametrize("current", [None, "endpoint-tv"])
def test_no_answer_and_no_change_both_leave_the_stream_alone(monkeypatch, current):
    """None means "could not read it" -- every output unplugged, or COM
    unavailable. Reopening on that would tear down working audio for nothing."""
    p = make_player()
    p._stream = FakeStream()
    p._opened_on = "endpoint-tv"
    monkeypatch.setattr(player_mod, "_default_endpoint_id", lambda: current)
    assert not p.default_device_changed()


def test_closed_stream_is_not_a_device_change(monkeypatch):
    """Nothing to move: the next open picks the right device by itself."""
    p = make_player()
    monkeypatch.setattr(player_mod, "_default_endpoint_id", lambda: "endpoint-aids")
    assert not p.default_device_changed()


def test_callback_never_raises():
    """An exception escaping the callback would abort the whole stream."""
    p = make_player()
    p._playlist[0] = "not an array"
    b = pull(p)
    assert np.allclose(b, 0.0), "degrades to silence"
    assert p.callback_error is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
