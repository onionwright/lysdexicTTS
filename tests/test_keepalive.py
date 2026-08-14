"""Comfort-noise ("keep the sound connection open") tests.

The property that matters: with keep-alive on, the player must never emit a
block of pure digital zeros. Hearing aids and Bluetooth devices read a run of
zeros as "no signal" and gate their processing off and on, which is audible as
the noise cancelling switching around every sentence.
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
    out = np.zeros((frames, 1), dtype=np.float32)
    p._callback(out, frames, None, None)
    return out[:, 0].copy()


def make_player(**kw):
    p = StreamPlayer(SR, blocksize=FRAMES, fade_ms=8.0)
    p.set_playlist(3)
    for i in range(3):
        p.set_chunk(i, np.full(3000, 0.5, dtype=np.float32))
    p._ensure_stream = lambda: None
    return p


def test_off_by_default_emits_true_silence():
    """Unchanged behaviour for everyone who doesn't need this."""
    p = make_player()
    assert np.all(pull(p) == 0.0), "paused output should be exactly zero"


def test_silence_is_never_digitally_zero_when_enabled():
    p = make_player()
    p.set_keepalive(True, -75.0)
    for _ in range(5):
        block = pull(p)          # paused: would otherwise be pure zeros
        assert np.any(block != 0.0), "a block of digital zeros slipped through"


def test_keepalive_covers_the_gap_between_sentences():
    """The reported symptom: processing gating during inter-sentence pauses."""
    p = make_player()
    p.set_chunk(1, np.zeros(3000, dtype=np.float32))  # a silent pause chunk
    p.set_keepalive(True, -75.0)
    p._pending = "resume"
    zero_blocks = 0
    for _ in range(12):
        if not np.any(pull(p) != 0.0):
            zero_blocks += 1
    assert zero_blocks == 0, f"{zero_blocks} silent blocks during playback"


def test_keepalive_survives_pause_stop_and_finish():
    p = make_player()
    p.set_keepalive(True, -75.0)
    p._pending = "resume"
    pull(p)
    for action in ("pause", "stop"):
        getattr(p, action)()
        for _ in range(3):
            assert np.any(pull(p) != 0.0), f"went digitally silent after {action}"

    p.jump_to(p._n)          # park past the end, i.e. finished
    pull(p)
    assert p.finished
    for _ in range(3):
        assert np.any(pull(p) != 0.0), "went digitally silent after finishing"


def test_the_noise_is_inaudible_and_does_not_distort_speech():
    p = make_player()
    p.set_keepalive(True, -75.0)
    quiet = pull(p)
    peak = float(np.abs(quiet).max())
    assert peak < 0.002, f"comfort noise should be inaudible, peak was {peak}"

    p._pending = "resume"
    speech = pull(p)
    # Speech is 0.5; the noise must not meaningfully perturb it.
    steady = speech[300:]
    assert np.allclose(steady, 0.5, atol=0.005), "speech was audibly altered"


@pytest.mark.parametrize("db,louder", [(-90, False), (-75, False), (-50, True)])
def test_level_control_changes_the_floor(db, louder):
    p = make_player()
    p.set_keepalive(True, db)
    peak = float(np.abs(pull(p)).max())
    assert peak > 0.0
    assert (peak > 0.001) is louder, f"{db} dB gave peak {peak}"


def test_volume_zero_still_keeps_the_connection_open():
    """Muting the reading must not let the device gate off."""
    p = make_player()
    p.volume = 0.0
    p.set_keepalive(True, -75.0)
    p._pending = "resume"
    assert np.any(pull(p) != 0.0)


def test_can_be_turned_back_off():
    p = make_player()
    p.set_keepalive(True, -75.0)
    assert np.any(pull(p) != 0.0)
    p.set_keepalive(False)
    assert np.all(pull(p) == 0.0)


@pytest.mark.parametrize(
    "color,expected_slope",
    [("white", 0.0), ("pink", -3.0), ("brown", -6.0)],
)
def test_noise_colours_have_the_spectral_slope_they_claim(color, expected_slope):
    """Flat white noise puts its energy in the top octaves, which is why it
    sounds like an electrical buzz rather than like rain."""
    from reader.audio.player import _comfort_noise

    signal = _comfort_noise(SR * 5, SR, color)
    spectrum = np.abs(np.fft.rfft(signal)) ** 2
    freqs = np.fft.rfftfreq(len(signal), 1 / SR)

    centres, levels = [], []
    f = 125.0
    while f * 2 <= 8000:
        band = (freqs >= f) & (freqs < f * 2)
        if band.any():
            centres.append(np.log2(f))
            levels.append(10 * np.log10(spectrum[band].mean() + 1e-30))
        f *= 2
    slope = float(np.polyfit(centres, levels, 1)[0])
    assert abs(slope - expected_slope) < 1.2, (
        f"{color} measured {slope:.2f} dB/octave, expected {expected_slope}"
    )


def test_noise_is_unit_rms_so_the_db_setting_is_meaningful():
    from reader.audio.player import _comfort_noise

    for color in ("white", "pink", "brown"):
        signal = _comfort_noise(SR, SR, color)
        rms = float(np.sqrt(np.mean(signal ** 2)))
        assert abs(rms - 1.0) < 0.01, f"{color} rms was {rms}"


def test_requested_level_is_delivered_as_rms_dbfs():
    p = make_player()
    p.set_keepalive(True, -70.0, "pink")
    blocks = [pull(p) for _ in range(20)]
    signal = np.concatenate(blocks)
    rms_db = 20 * np.log10(np.sqrt(np.mean(signal ** 2)))
    assert abs(rms_db + 70.0) < 1.0, f"asked for -70 dBFS, got {rms_db:.1f}"


def test_changing_colour_rebuilds_the_table_outside_the_callback():
    p = make_player()
    p.set_keepalive(True, -70.0, "pink")
    pink = p._noise.copy()
    p.set_keepalive(True, -70.0, "brown")
    assert p._noise.shape == pink.shape
    assert not np.array_equal(p._noise, pink), "table should have been rebuilt"
    assert p._noise_pos == 0
    assert p.callback_error is None


def test_unknown_colour_falls_back_to_the_default():
    from reader.audio.player import DEFAULT_NOISE_COLOR

    p = make_player()
    p.set_keepalive(True, -70.0, "chartreuse")
    assert p._noise_color == DEFAULT_NOISE_COLOR


def test_the_default_colour_is_the_least_perceptible_one():
    """Brown carries the least high-frequency energy at a given RMS, which is
    what makes it hardest to notice -- the whole point of this signal."""
    from reader.audio.player import DEFAULT_NOISE_COLOR, _comfort_noise

    assert DEFAULT_NOISE_COLOR == "brown"

    def high_frequency_energy(color):
        signal = _comfort_noise(SR, SR, color)
        spectrum = np.abs(np.fft.rfft(signal)) ** 2
        freqs = np.fft.rfftfreq(len(signal), 1 / SR)
        return float(spectrum[freqs >= 2000].sum() / spectrum.sum())

    brown = high_frequency_energy("brown")
    assert brown < high_frequency_energy("pink") < high_frequency_energy("white")


def test_noise_table_wraps_without_error():
    """The table is finite; the callback must cycle it cleanly."""
    p = make_player()
    p.set_keepalive(True, -60.0)
    total = p._noise.size
    pulled = 0
    while pulled <= total + 2 * FRAMES:
        pull(p)
        pulled += FRAMES
    assert p.callback_error is None
    assert 0 <= p._noise_pos < p._noise.size


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
