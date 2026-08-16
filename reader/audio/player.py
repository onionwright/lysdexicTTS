"""Callback-driven streaming playback with sentence-level transport.

Design constraints that shaped this file:

* **The callback runs on PortAudio's thread and must acquire the GIL**, so it
  does bounded work only: slice copies into a preallocated view, integer
  bookkeeping, no allocation, no locks, no logging, and no exception may escape
  (sounddevice aborts the stream if one does). Headroom comes from ``blocksize``
  -- 2048 frames is 85ms at 24kHz, which swamps any plausible GIL wait while
  torch is busy. There is no latency requirement here to spend it on.
* **Control threads never take a lock either.** They write plain attributes
  (``_pending``, ``_paused``); attribute assignment is atomic under the GIL.
  The callback publishes progress as plain integers that the UI polls on a
  timer. Callbacks never emit signals.
* **The device is never stopped for pause.** WASAPI stop/start is audible and
  costs 50-150ms to resume, so pausing writes silence with the stream running.
* **Starvation degrades to silence, not to a stopped stream.** If the next
  sentence hasn't finished synthesizing, the callback holds position and emits
  zeros. Stopping the device there would guarantee a click plus a resume delay.

Every discontinuity (pause, resume, stop, next, prev, seek) is routed through a
single ``_pending`` slot so it lands on a raised-cosine fade and cannot click.
The fade is 8ms (192 frames), always shorter than one block, so a fade-out, the
transition, and the fade-in all complete inside a single callback.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Callable, List, Optional

import numpy as np

from ..tts.postproc import make_fade

log = logging.getLogger(__name__)

DEFAULT_BLOCKSIZE = 2048  # 85ms at 24kHz
DEFAULT_FADE_MS = 8.0

# Length of the precomputed comfort-noise table. Long enough that its repeat is
# not a perceptible period, small enough to stay trivial in memory (~480KB).
KEEPALIVE_SECONDS = 5

# Spectral slope per colour, as the exponent in 1/f**(n/2) applied to amplitude.
# White is flat, which puts most of its energy in the top octaves and is why it
# sounds harsh and electrical. Pink falls at 3dB/octave and is what people
# normally mean by "white noise" -- rain, a fan. Brown falls at 6dB/octave.
NOISE_COLORS = {"white": 0.0, "pink": 1.0, "brown": 2.0}

# Brown by default: at the same RMS level it is the least perceptible of the
# three, which is what you want from a signal whose only job is to stop the
# audio path going to sleep.
DEFAULT_NOISE_COLOR = "brown"

# Below this the energy is inaudible rumble that does nothing to keep a device
# awake, and risks pushing DC-ish wander into the output.
_NOISE_HIGHPASS_HZ = 60.0


def _default_endpoint_id() -> Optional[str]:
    """Id of the endpoint Windows currently calls the default output.

    Imported lazily and behind a catch-all: this is a nicety, and nothing about
    playing audio should fail because COM did.
    """
    try:
        from ..win.audiodev import default_output_id

        return default_output_id()
    except Exception:
        return None


def _rescan_devices() -> None:
    """Make PortAudio re-enumerate the machine's audio devices.

    PortAudio builds its device list once, at ``Pa_Initialize``, and the index
    it calls "the default output" is part of that snapshot. There is no public
    refresh, so reopening on a device that appeared (or became default) after
    startup means tearing the library down and bringing it back up. That is
    only safe with every stream closed, which is why the only caller is
    :meth:`StreamPlayer.reopen`, between its close and its open.
    """
    import sounddevice as sd

    try:
        sd._terminate()
    except Exception:
        # Nothing was torn down, so the library is still in a usable state and
        # reopening on the stale device list is better than not reopening.
        log.debug("could not terminate PortAudio for a device rescan", exc_info=True)
        return
    # Deliberately not guarded: if this fails there is no audio at all, and the
    # caller's next attempt is the recovery path.
    sd._initialize()


def _comfort_noise(
    frames: int, sample_rate: int, color: str = DEFAULT_NOISE_COLOR
) -> np.ndarray:
    """Spectrally shaped noise at unit RMS, generated once at startup.

    Built in the frequency domain, which makes the table exactly periodic, so
    looping it is seamless -- no discontinuity at the wrap point.
    """
    n = max(2, int(frames))
    exponent = NOISE_COLORS.get(color, NOISE_COLORS[DEFAULT_NOISE_COLOR])
    rng = np.random.default_rng(20260813)
    spectrum = np.fft.rfft(rng.standard_normal(n))
    freqs = np.fft.rfftfreq(n, d=1.0 / float(sample_rate))

    scale = np.zeros_like(freqs)
    audible = freqs >= _NOISE_HIGHPASS_HZ
    scale[audible] = freqs[audible] ** (-exponent / 2.0)

    shaped = np.fft.irfft(spectrum * scale, n)
    rms = float(np.sqrt(np.mean(shaped ** 2)))
    if rms > 0.0:
        shaped = shaped / rms  # unit RMS, so the dB setting means dBFS RMS
    return shaped.astype(np.float32)


class StreamPlayer:
    """Plays an index-addressed playlist of per-sentence buffers."""

    def __init__(
        self,
        sample_rate: int = 24000,
        *,
        blocksize: int = DEFAULT_BLOCKSIZE,
        latency: str | float = "high",
        device: Optional[str | int] = None,
        fade_ms: float = DEFAULT_FADE_MS,
    ) -> None:
        self.sample_rate = sample_rate
        self.blocksize = blocksize
        self.latency = latency
        self.device = device

        self._fade_in = make_fade(max(1, int(sample_rate * fade_ms / 1000.0)))
        self._fade_out = self._fade_in[::-1].copy()
        self._fade_n = len(self._fade_in)

        # Comfort noise. Some audio hardware -- hearing aids and Bluetooth
        # devices especially -- treats a run of digital zeros as "no signal"
        # and powers down its audio path or re-engages noise cancelling. That
        # makes the 0.16s gap between sentences audible as the processing
        # switching off and on again. Mixing in an inaudible noise floor keeps
        # the path continuously engaged. Precomputed, because the callback must
        # not allocate or call into the RNG.
        self._noise_color = DEFAULT_NOISE_COLOR
        self._noise = _comfort_noise(
            sample_rate * KEEPALIVE_SECONDS, sample_rate, self._noise_color
        )
        self._noise_pos = 0
        self._keepalive = 0.0

        # --- playlist (written by the scheduler thread, read by the callback)
        self._playlist: List[Optional[np.ndarray]] = []
        self._n = 0
        # Per-unit word timings, published alongside the pcm for the UI's
        # word-level display. Never touched by the audio callback.
        self._words: List[Optional[list]] = []

        # --- published state (callback writes, control threads read)
        self.cur_index = 0
        self.pos = 0
        self.boundary_seq = 0
        self.starved = False
        self.finished = False
        self.xruns = 0
        # Frames of silence emitted because the next sentence wasn't ready yet.
        # This is the honest measure of pipeline health: xruns stay at zero even
        # when the reader is audibly stalling, because the device is still fed.
        self.starved_frames = 0
        self.starve_events = 0
        self.callback_error: Optional[str] = None

        # --- control state (control threads write, callback reads)
        self._paused = True
        self._pending: Optional[str] = None
        self._pending_target = 0
        self._pending_since = 0.0
        self._volume = 1.0
        self._was_starved = False

        self._stream = None
        self._open_lock = threading.Lock()
        # Endpoint the open stream is bound to, so a later default change can be
        # spotted. Only meaningful while following the default (device is None).
        self._opened_on: Optional[str] = None

    # -------------------------------------------------------------- playlist

    def set_playlist(self, size: int) -> None:
        """Reset to an empty playlist of ``size`` sentences.

        The caller's stop() is asynchronous, so the callback can still be
        running while this executes. Zeroing ``_n`` first closes the window in
        which a control thread could pair the old size with the new (possibly
        shorter) list; the callback itself sizes off the list it snapshots.
        """
        self._n = 0
        self._playlist = [None] * size
        self._words = [None] * size
        self._n = size
        self.cur_index = 0
        self.pos = 0
        self.boundary_seq += 1
        self.starved = False
        self._was_starved = False
        self.finished = False
        self.starved_frames = 0
        self.starve_events = 0

    def set_chunk(
        self, index: int, pcm: np.ndarray, words: Optional[list] = None
    ) -> None:
        """Publish synthesized audio for one sentence. Safe from any thread --
        list item assignment is atomic under the GIL. Words go in first, so any
        thread that sees the chunk also sees its timings."""
        if 0 <= index < self._n:
            self._words[index] = words
            self._playlist[index] = pcm

    def has_chunk(self, index: int) -> bool:
        playlist = self._playlist
        return 0 <= index < len(playlist) and playlist[index] is not None

    def get_words(self, index: int) -> Optional[list]:
        """Word timings for one unit, or None if not (yet) published."""
        words = self._words
        return words[index] if 0 <= index < len(words) else None

    # ------------------------------------------------------------- transport

    def play(self) -> None:
        """Start or resume. At the end of the playlist, this replays from the
        top -- pressing play on a finished document must not be a no-op."""
        self._ensure_stream()
        if self.finished or self.cur_index >= self._n:
            self._pending = "restart"
            self._pending_since = time.monotonic()
        elif self._paused:
            self._pending = "resume"
            self._pending_since = time.monotonic()

    def play_request_stale(self, age_s: float) -> bool:
        """True when a play/resume request has sat unconsumed for ``age_s``.

        A stream can open, report itself active, and still never invoke the
        callback (a wedged WASAPI endpoint does exactly this). The device-alive
        check cannot see that state, but the unconsumed intent can. Re-arms its
        own timer, so a caller acting on True retries at most once per
        ``age_s``.
        """
        if self._pending not in ("resume", "restart") or not self._paused:
            return False
        now = time.monotonic()
        if now - self._pending_since < age_s:
            return False
        self._pending_since = now
        return True

    def ensure_ready(self) -> None:
        """Open the device if it isn't already.

        Transport actions are applied by the audio callback, so a jump issued
        while the stream is closed would sit in ``_pending`` forever and the UI
        would look stuck.
        """
        self._ensure_stream()

    def pause(self) -> None:
        if not self._paused:
            self._pending = "pause"

    def toggle(self) -> bool:
        """Flip play/pause. Returns True if now playing."""
        if self._paused:
            self.play()
            return True
        self.pause()
        return False

    def stop(self) -> None:
        """Silence immediately and rewind. Leaves the device open."""
        self._pending = "stop"

    def jump_to(self, index: int) -> None:
        self._pending_target = max(0, min(index, self._n))
        self._pending = "jump"

    def next_sentence(self) -> None:
        # Chain off the last *requested* target, not the current position, so
        # rapid presses advance by one each rather than collapsing into one.
        base = self._pending_target if self._pending == "jump" else self.cur_index
        self.jump_to(min(base + 1, self._n))

    def prev_sentence(self, restart_threshold_s: float = 2.0) -> None:
        """Restart the current sentence, or step back if barely into it.

        This is the standard music-player rule and it is what makes the button
        useful: most of the time you want to hear that sentence again.
        """
        if self._pending == "jump":
            self.jump_to(max(0, self._pending_target - 1))
            return
        elapsed = self.pos / float(self.sample_rate)
        if elapsed > restart_threshold_s:
            self.jump_to(self.cur_index)
        else:
            self.jump_to(max(0, self.cur_index - 1))

    def pending_jump_target(self) -> Optional[int]:
        """Target of an unconsumed jump, so callers can chain off it rather
        than off the stale current position."""
        return self._pending_target if self._pending == "jump" else None

    @property
    def is_playing(self) -> bool:
        return not self._paused and not self.finished

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, v: float) -> None:
        self._volume = max(0.0, min(1.0, float(v)))

    @property
    def keepalive_db(self) -> float:
        """Comfort-noise level in dBFS, or -inf when off."""
        if self._keepalive <= 0.0:
            return float("-inf")
        return 20.0 * math.log10(self._keepalive)

    def set_keepalive(
        self,
        enabled: bool,
        level_db: float = -70.0,
        color: str = DEFAULT_NOISE_COLOR,
    ) -> None:
        """Hold the audio path open with a near-inaudible noise floor.

        Deliberately applied *after* volume and after every fade, so it is
        continuous even while paused, stopped or silent between sentences --
        which is the entire point. It is what stops a hearing aid or Bluetooth
        headset from gating its processing off and on around each sentence.

        ``level_db`` is RMS dBFS. ``color`` selects the spectral slope; see
        NOISE_COLORS.
        """
        color = color if color in NOISE_COLORS else DEFAULT_NOISE_COLOR
        if color != self._noise_color:
            # Rebuilt here, never in the audio callback.
            self._noise_color = color
            self._noise = _comfort_noise(
                self.sample_rate * KEEPALIVE_SECONDS, self.sample_rate, color
            )
            self._noise_pos = 0
        self._keepalive = (
            0.0 if not enabled else float(10.0 ** (float(level_db) / 20.0))
        )

    def position_seconds(self) -> float:
        return self.pos / float(self.sample_rate)

    def output_latency_s(self) -> float:
        """PortAudio's estimate of write-to-speaker delay for the open stream.

        ``pos`` counts frames *written* to the device, which runs ahead of what
        is audible by the buffering ("high" latency here). Anything displayed
        against the playhead has to subtract this or it leads the voice.
        Bluetooth sinks add further delay PortAudio cannot see; that part is
        the user-facing display-delay setting's job.
        """
        stream = self._stream
        if stream is None:
            return 0.0
        try:
            return float(stream.latency or 0.0)
        except Exception:
            return 0.0

    # ---------------------------------------------------------------- device

    def _ensure_stream(self) -> None:
        with self._open_lock:
            if self._stream is not None and self._stream.active:
                return
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            import sounddevice as sd

            self._stream = sd.OutputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                blocksize=self.blocksize,
                latency=self.latency,
                device=self.device,
                callback=self._callback,
            )
            self._stream.start()
            self._opened_on = _default_endpoint_id()
            log.debug(
                "output stream open (device=%s, blocksize=%d, latency=%s)",
                self.device, self.blocksize, self.latency,
            )

    def is_device_alive(self) -> bool:
        return self._stream is not None and self._stream.active

    def default_device_changed(self) -> bool:
        """True when Windows has moved the default output somewhere other than
        where the open stream is playing.

        A stream stays on the endpoint it was opened on for as long as it lives,
        so following the system default is something this has to do on purpose.
        An unreadable default reads as "no change": leaving the stream where it
        is beats tearing the audio down on a failed COM call.
        """
        if self.device is not None or self._stream is None:
            return False
        if self._opened_on is None:
            return False
        current = _default_endpoint_id()
        return current is not None and current != self._opened_on

    def reopen(self) -> None:
        """Move to the current default device.

        Covers both device loss (Bluetooth drop, dock undock) and the default
        simply moving under us. Position is preserved, so it costs one fade-in
        and nothing else.
        """
        idx, pos, paused = self.cur_index, self.pos, self._paused
        with self._open_lock:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            # Between the close and the open, the only window in which PortAudio
            # can safely be restarted -- and without restarting it, reopening
            # would land right back on the device that was default at startup.
            _rescan_devices()
        self._ensure_stream()
        self.cur_index, self.pos = idx, pos
        if not paused:
            self._pending = "resume"

    def close(self) -> None:
        with self._open_lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

    # -------------------------------------------------------------- callback

    def _apply_pending(self, action: str) -> None:
        if action == "pause":
            self._paused = True
        elif action == "resume":
            self._paused = False
            self.finished = False
        elif action == "restart":
            self.cur_index = 0
            self.pos = 0
            self.finished = False
            self._paused = False
            self.boundary_seq += 1
        elif action == "stop":
            self._paused = True
            self.cur_index = 0
            self.pos = 0
            self.finished = False
            self.boundary_seq += 1
        elif action == "jump":
            target = self._pending_target
            if target >= len(self._playlist):
                self._paused = True
                self.finished = True
                self.cur_index = len(self._playlist)
            else:
                self.cur_index = target
                self.finished = False
            self.pos = 0
            self.boundary_seq += 1

    def _emit(self, out: np.ndarray, offset: int, count: int) -> int:
        """Copy up to ``count`` frames into ``out[offset:]``. Returns frames written."""
        if self._paused:
            return 0
        written = 0
        # Local snapshot: set_playlist swaps the list under us; sizing off the
        # snapshot keeps index and length consistent within this callback.
        playlist = self._playlist
        while written < count:
            idx = self.cur_index
            if idx >= len(playlist):
                self.finished = True
                self._paused = True
                return written
            buf = playlist[idx]
            if buf is None:
                # Not synthesized yet: hold position and let the caller pad
                # with silence. Never stop the device for this.
                self.starved = True
                return written
            avail = len(buf) - self.pos
            if avail <= 0:
                self.cur_index = idx + 1
                self.pos = 0
                self.boundary_seq += 1
                continue
            self.starved = False
            k = avail if avail < (count - written) else (count - written)
            dst = offset + written
            out[dst : dst + k] = buf[self.pos : self.pos + k]
            self.pos += k
            written += k
        return written

    def _callback(self, outdata, frames, time_info, status) -> None:
        try:
            if status:
                self.xruns += 1
            out = outdata[:, 0]
            i = 0

            action = self._pending
            if action is not None:
                self._pending = None
                # Ramp the outgoing audio down so the cut can't click...
                f = self._fade_n if self._fade_n < frames else frames
                w = self._emit(out, 0, f)
                if w > 0:
                    out[:w] *= self._fade_out[:w]
                i = w
                self._apply_pending(action)
                # ...then ramp the incoming audio up.
                f2 = self._fade_n if self._fade_n < (frames - i) else (frames - i)
                w2 = self._emit(out, i, f2)
                if w2 > 0:
                    out[i : i + w2] *= self._fade_in[:w2]
                i += w2

            while i < frames:
                w = self._emit(out, i, frames - i)
                if w == 0:
                    if self.starved and not self._paused:
                        if not self._was_starved:
                            self.starve_events += 1
                        self.starved_frames += frames - i
                        self._was_starved = True
                    out[i:] = 0.0
                    break
                i += w
            else:
                self._was_starved = False

            if self._volume != 1.0:
                out *= self._volume

            # Comfort noise last, so pauses and silences are never digitally
            # zero. Slice-add into an existing view: no allocation.
            level = self._keepalive
            if level > 0.0:
                size = self._noise.size
                pos = self._noise_pos
                if pos + frames <= size:
                    out += self._noise[pos : pos + frames] * level
                    pos += frames
                else:
                    head = size - pos
                    out[:head] += self._noise[pos:] * level
                    out[head:] += self._noise[: frames - head] * level
                    pos = frames - head
                self._noise_pos = 0 if pos >= size else pos
        except Exception as exc:  # must never propagate: it would abort the stream
            try:
                outdata.fill(0)
                self.callback_error = repr(exc)
            except Exception:
                pass
