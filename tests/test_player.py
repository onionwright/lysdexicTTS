"""Deterministic transport tests: the callback is driven by hand, no device.

Run directly (``python tests/test_player.py``) or under pytest.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_callback_never_raises():
    """An exception escaping the callback would abort the whole stream."""
    p = make_player()
    p._playlist[0] = "not an array"
    b = pull(p)
    assert np.allclose(b, 0.0), "degrades to silence"
    assert p.callback_error is not None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
