"""The background-sound pause button in the reader panel.

Separate from the reading transport on purpose: it exists for the moment
someone starts talking to you and you need actual quiet, without opening
settings and without stopping what is being read.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from reader.audio.player import StreamPlayer  # noqa: E402
from reader.ui.reader_panel import ReaderPanel  # noqa: E402

SR = 24000
FRAMES = 1024


@pytest.fixture(scope="module")
def qapp():
    yield QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def pull(p, frames=FRAMES):
    out = np.zeros((frames, 1), dtype=np.float32)
    p._callback(out, frames, None, None)
    return out[:, 0].copy()


def test_button_is_hidden_unless_the_feature_is_on(qapp):
    """No extra clutter for anyone who doesn't need it."""
    panel = ReaderPanel()
    assert not panel.btn_noise.isVisible()
    panel.set_noise_control(False)
    assert not panel.btn_noise.isVisible()


def test_button_appears_and_reflects_state(qapp):
    panel = ReaderPanel()
    panel.show()
    panel.set_noise_control(True, playing=True)
    assert panel.btn_noise.isVisible()
    assert panel.btn_noise.shape_name == "noise"
    assert "Pause" in panel.btn_noise.toolTip()

    panel.set_noise_control(True, playing=False)
    assert panel.btn_noise.shape_name == "noise_off"
    assert "Resume" in panel.btn_noise.toolTip()
    panel.close()


def test_button_emits_a_signal(qapp):
    panel = ReaderPanel()
    panel.show()
    panel.set_noise_control(True, True)
    fired = []
    panel.noise_toggled.connect(lambda: fired.append(True))
    panel.btn_noise.click()
    assert fired, "clicking must ask the app to toggle"
    panel.close()


def test_pausing_actually_silences_the_noise():
    p = StreamPlayer(SR, blocksize=FRAMES)
    p.set_playlist(1)
    p._ensure_stream = lambda: None

    p.set_keepalive(True, -70.0, "pink")
    assert np.any(pull(p) != 0.0), "noise should be present"

    p.set_keepalive(False)          # what the button does
    assert np.all(pull(p) == 0.0), "paused noise must be truly silent"

    p.set_keepalive(True, -70.0, "pink")
    assert np.any(pull(p) != 0.0), "and it must come back"


def test_pausing_the_noise_does_not_touch_the_reading():
    """The whole point: silence the background sound, keep reading."""
    p = StreamPlayer(SR, blocksize=FRAMES)
    p.set_playlist(2)
    for i in range(2):
        p.set_chunk(i, np.full(6000, 0.5, dtype=np.float32))
    p._ensure_stream = lambda: None
    p.set_keepalive(True, -70.0, "pink")
    p._pending = "resume"
    pull(p)

    index_before, playing_before = p.cur_index, p.is_playing
    p.set_keepalive(False)
    block = pull(p)

    assert p.is_playing == playing_before, "reading must keep going"
    assert p.cur_index == index_before
    assert np.allclose(block, 0.5, atol=0.01), "speech continues untouched"


def test_toggling_does_not_disturb_playback_position():
    p = StreamPlayer(SR, blocksize=FRAMES)
    p.set_playlist(1)
    p.set_chunk(0, np.full(SR, 0.5, dtype=np.float32))
    p._ensure_stream = lambda: None
    p._pending = "resume"
    pull(p)
    position = p.pos
    for _ in range(4):
        p.set_keepalive(True, -70.0, "pink")
        p.set_keepalive(False)
    assert p.pos == position, "toggling must not move the playhead"
    assert p.callback_error is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
