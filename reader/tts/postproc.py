"""Silence trimming and pause shaping.

Kokoro pads every chunk with roughly 0.31-0.38s of leading and 0.49s of trailing
silence. Concatenating raw chunks therefore leaves ~0.85s of dead air between
every pair of sentences, which sounds broken. Trimming and re-adding a chosen
pause fixes that, and trimming the *lead* additionally cuts ~0.38s off
time-to-first-audio -- the single cheapest latency win available.

Measured separately: kokoro's output starts and ends at exactly +/-0.0
amplitude, so butt-joining trimmed chunks introduces no click. Clicks can only
come from cutting mid-waveform (next/prev/stop/pause), which the player handles
with a short fade, not from concatenation.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np

from .base import WordTiming

# Relative to the chunk peak; the absolute floor keeps very quiet chunks from
# having their actual speech trimmed away.
_REL_THRESHOLD = 0.01
_ABS_FLOOR = 1e-3
_BACKOFF_MS = 10.0


def speech_bounds(
    pcm: np.ndarray,
    sample_rate: int,
    rel_threshold: float = _REL_THRESHOLD,
    abs_floor: float = _ABS_FLOOR,
    backoff_ms: float = _BACKOFF_MS,
) -> Tuple[int, int]:
    """Return ``(start, end)`` frame indices bounding the audible speech."""
    if pcm.size == 0:
        return 0, 0
    mag = np.abs(pcm)
    peak = float(mag.max())
    if peak <= 0.0:
        return 0, 0
    threshold = max(peak * rel_threshold, abs_floor)
    loud = np.flatnonzero(mag > threshold)
    if loud.size == 0:
        return 0, 0
    backoff = int(sample_rate * backoff_ms / 1000.0)
    start = max(0, int(loud[0]) - backoff)
    end = min(pcm.size, int(loud[-1]) + 1 + backoff)
    return start, end


def trim(
    pcm: np.ndarray,
    sample_rate: int,
    *,
    trim_lead: bool = True,
    trim_tail: bool = True,
) -> Tuple[np.ndarray, float]:
    """Strip padding silence.

    Returns the trimmed audio and the number of *seconds* removed from the
    front, which callers must subtract from word timings to keep them valid.
    """
    start, end = speech_bounds(pcm, sample_rate)
    if end <= start:
        return pcm[:0], 0.0
    lo = start if trim_lead else 0
    hi = end if trim_tail else pcm.size
    return pcm[lo:hi], lo / float(sample_rate)


def pad(pcm: np.ndarray, sample_rate: int, pause_s: float) -> np.ndarray:
    """Append ``pause_s`` of digital silence."""
    if pause_s <= 0:
        return pcm
    frames = int(round(sample_rate * pause_s))
    if frames <= 0:
        return pcm
    return np.concatenate([pcm, np.zeros(frames, dtype=np.float32)])


def shift_timings(words: List[WordTiming], delta: float) -> List[WordTiming]:
    """Offset every timing by ``delta`` seconds, clamping at zero."""
    if not words or delta == 0.0:
        return words
    return [
        WordTiming(max(0.0, w.start + delta), max(0.0, w.end + delta), w.text)
        for w in words
    ]


def make_fade(frames: int) -> np.ndarray:
    """Raised-cosine ramp from 0 to 1, precomputed once and reused.

    Used by the player at every audio discontinuity (pause, stop, next, prev)
    so a mid-waveform cut doesn't click.
    """
    if frames <= 0:
        return np.ones(0, dtype=np.float32)
    n = np.arange(frames, dtype=np.float32)
    return (0.5 - 0.5 * np.cos(np.pi * n / max(1, frames - 1))).astype(np.float32)


def silence(sample_rate: int, seconds: float) -> np.ndarray:
    return np.zeros(max(0, int(round(sample_rate * seconds))), dtype=np.float32)
