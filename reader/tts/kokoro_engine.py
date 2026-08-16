"""PyTorch Kokoro-82M backend.

Sole owner of ``KPipeline``, ``KModel`` and misaki's spaCy instance. torch
modules are not safe to call concurrently and there is nothing to gain by
trying -- inference already parallelises internally -- so every call into this
class must come from the single synthesis thread.
"""

from __future__ import annotations

import glob
import logging
import os
import threading
import time
import warnings
from typing import List, Optional

import numpy as np

from .base import SynthChunk, TtsEngine, WordTiming
from . import postproc

log = logging.getLogger(__name__)

DEFAULT_REPO_ID = "hexgrad/Kokoro-82M"
DEFAULT_VOICE = "af_heart"
SAMPLE_RATE = 24000

# The full published voice set. Only the ones actually present in the local
# HuggingFace cache can be used offline; the rest trigger a lazy download.
KNOWN_VOICES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]

# Fetched automatically on first run, the same way the model and af_heart are,
# so a fresh install has a real choice of voice rather than exactly one. Two
# accents and both genders, at ~500KB each.
DEFAULT_VOICES = [
    "af_heart",     # American female (the default)
    "af_bella",     # American female, warmer
    "am_michael",   # American male
    "bf_emma",      # British female
    "bm_george",    # British male
]

# Pieces of one over-long sentence get a shorter gap than sentence boundaries.
SUBCHUNK_PAUSE_S = 0.08


class KokoroEngine(TtsEngine):
    sample_rate = SAMPLE_RATE

    def __init__(
        self,
        *,
        lang_code: str = "a",
        repo_id: str = DEFAULT_REPO_ID,
        voice: str = DEFAULT_VOICE,
        speed: float = 1.0,
        torch_threads: int = 4,
        device: Optional[str] = None,
        trim_lead: bool = True,
        allow_voice_download: bool = True,
        prefer_offline: bool = True,
    ) -> None:
        self.lang_code = lang_code
        self.repo_id = repo_id
        self.voice = voice
        self.speed = speed
        self.torch_threads = torch_threads
        self.device = device
        self.trim_lead = trim_lead
        self.allow_voice_download = allow_voice_download
        self.prefer_offline = prefer_offline

        self._pipeline = None
        self._lock = threading.Lock()
        self._model_rev = "unknown"
        self.espeak_fallback_ok = False

    # ------------------------------------------------------------------ setup

    @property
    def engine_id(self) -> str:
        return f"kokoro-torch:{self.repo_id}@{self._model_rev}:{self.lang_code}"

    @property
    def is_loaded(self) -> bool:
        return self._pipeline is not None

    def load(self) -> None:
        """Import torch, build the pipeline, and sanity-check the G2P fallback."""
        with self._lock:
            if self._pipeline is not None:
                return

            # kokoro resolves every file through hf_hub_download, which makes a
            # network round trip to revalidate even when the file is cached.
            # For a tray app that starts at logon that means stalling on DNS --
            # or hanging outright on a captive portal. If the snapshot is
            # already on disk, pin to it and go straight to the cache.
            if self.prefer_offline and _snapshot_dirs(self.repo_id):
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
                log.debug("model snapshot found locally; HF_HUB_OFFLINE=1")
            elif self.prefer_offline:
                # The guard not firing means every hf_hub_download revalidates
                # over the network -- the classic cause of a multi-minute load.
                log.warning(
                    "no local snapshot of %s found; model files will be "
                    "resolved over the network", self.repo_id,
                )

            t0 = time.perf_counter()
            import torch

            # 8 threads benchmarks ~20% faster than 4 on this 4-core part, but
            # oversubscribing directly starves the audio callback thread.
            torch.set_num_threads(max(1, self.torch_threads))
            t_torch = time.perf_counter()

            with warnings.catch_warnings():
                # kokoro's vocoder emits a weight_norm deprecation and an LSTM
                # dropout warning on every construction. Silence those two
                # specifically rather than blanket-suppressing warnings.
                warnings.filterwarnings("ignore", category=FutureWarning)
                warnings.filterwarnings("ignore", category=UserWarning)
                from kokoro import KPipeline

                t_import = time.perf_counter()
                self._pipeline = KPipeline(
                    lang_code=self.lang_code,
                    repo_id=self.repo_id,
                    device=self.device,
                )
            t_pipeline = time.perf_counter()
            log.info(
                "model load: torch import %.1fs, kokoro import %.1fs, "
                "pipeline build %.1fs",
                t_torch - t0, t_import - t_torch, t_pipeline - t_import,
            )

            self._model_rev = _detect_model_revision(self.repo_id)

            # If espeak isn't wired up, out-of-dictionary words are silently
            # dropped from the audio -- the worst possible failure for a reader,
            # because nothing looks wrong. Surface it loudly.
            self.espeak_fallback_ok = getattr(self._pipeline.g2p, "fallback", None) is not None
            if not self.espeak_fallback_ok:
                log.error(
                    "espeak fallback is NOT enabled: out-of-dictionary words will be "
                    "silently skipped during reading."
                )
            log.info(
                "Kokoro engine ready (rev=%s, threads=%d, espeak=%s)",
                self._model_rev, self.torch_threads, self.espeak_fallback_ok,
            )

    def available_voices(self) -> List[str]:
        """Voices already downloaded, so usable right now with no network."""
        local = set(_local_voice_names(self.repo_id))
        english = sorted(v for v in local if v in KNOWN_VOICES)
        return english or sorted(local) or [self.voice]

    def downloadable_voices(self) -> List[str]:
        """English voices that exist upstream but are not installed here."""
        have = set(self.available_voices())
        return [v for v in KNOWN_VOICES if v not in have]

    def download_voice(self, name: str) -> bool:
        """Fetch one voice pack from HuggingFace.

        Startup deliberately pins the process offline so a tray app cannot
        stall on DNS at logon, and that pin has to be lifted for a download the
        user explicitly asked for. The env var alone is not enough --
        huggingface_hub snapshots it into a module constant at import time --
        so both are flipped and then restored.
        """
        if name not in KNOWN_VOICES:
            log.warning("refusing to download an unknown voice: %r", name)
            return False

        previous_env = os.environ.pop("HF_HUB_OFFLINE", None)
        constants = None
        previous_flag = None
        try:
            from huggingface_hub import constants as constants  # noqa: PLC0414

            previous_flag = getattr(constants, "HF_HUB_OFFLINE", None)
            constants.HF_HUB_OFFLINE = False
        except Exception:
            log.debug("could not clear the huggingface offline flag", exc_info=True)

        try:
            from huggingface_hub import hf_hub_download

            path = hf_hub_download(
                repo_id=self.repo_id, filename=f"voices/{name}.pt"
            )
            log.info("downloaded voice %s -> %s", name, path)
            return True
        except Exception:
            log.exception("could not download voice %r", name)
            return False
        finally:
            if previous_env is not None:
                os.environ["HF_HUB_OFFLINE"] = previous_env
            if constants is not None and previous_flag is not None:
                constants.HF_HUB_OFFLINE = previous_flag

    def ensure_default_voices(self) -> List[str]:
        """Fetch the starter set if it isn't already cached.

        The model and ``af_heart`` arrive this way on first run; this simply
        does the same for a few more so a new install has a real choice of
        voices instead of exactly one. Best-effort and silent: no network means
        no extra voices, not a failure.
        """
        have = set(_local_voice_names(self.repo_id))
        added = []
        for name in DEFAULT_VOICES:
            if name in have:
                continue
            if self.download_voice(name):
                added.append(name)
        if added:
            log.info("downloaded starter voices: %s", ", ".join(added))
        return added

    def warm(self) -> None:
        """Run one tiny synthesis so the first real sentence isn't paying for
        lazy CUDA/MKL kernel setup and voice-pack loading."""
        self.load()
        try:
            self.synth("Ready.", pause_after_s=0.0)
        except Exception:
            log.exception("warm-up synthesis failed (non-fatal)")

    # ------------------------------------------------------------------ synth

    def synth(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        speed: Optional[float] = None,
        pause_after_s: float = 0.0,
    ) -> SynthChunk:
        if self._pipeline is None:
            self.load()
        pipeline = self._pipeline
        voice = voice or self.voice
        speed = self.speed if speed is None else speed

        _, tokens = pipeline.g2p(text)

        pieces: List[np.ndarray] = []
        words: List[WordTiming] = []
        elapsed = 0.0
        phoneme_len = 0

        # generate_from_tokens runs en_tokenize internally, which already
        # applies kokoro's own 510-phoneme waterfall chunking. An over-long
        # sentence therefore arrives as several Results that we stitch back
        # into one playable unit rather than splitting the sentence ourselves.
        for result in pipeline.generate_from_tokens(tokens, voice=voice, speed=speed):
            audio = result.audio
            if audio is None:
                continue
            pcm = np.ascontiguousarray(
                audio.detach().cpu().numpy().astype(np.float32, copy=False)
            )
            phoneme_len += len(result.phonemes or "")

            trimmed, lead_s = postproc.trim(
                pcm, self.sample_rate, trim_lead=self.trim_lead, trim_tail=True
            )
            if trimmed.size == 0:
                continue
            if pieces:
                pieces.append(postproc.silence(self.sample_rate, SUBCHUNK_PAUSE_S))
                elapsed += SUBCHUNK_PAUSE_S

            words.extend(
                postproc.shift_timings(
                    _result_word_timings(result), elapsed - lead_s
                )
            )
            pieces.append(trimmed)
            elapsed += trimmed.size / float(self.sample_rate)

        if not pieces:
            # No phonemes at all. The splitter filters most of these out, but a
            # silence chunk here guarantees the playlist always has an entry for
            # every sentence index -- never a hole that stalls playback.
            # Length only, never the content: captured text must not reach the
            # log at any level.
            log.debug(
                "no audio produced for a %d-char sentence; substituting silence",
                len(text),
            )
            return SynthChunk(
                pcm=postproc.silence(self.sample_rate, max(pause_after_s, 0.12)),
                sample_rate=self.sample_rate,
                phoneme_len=0,
                words=[],
                is_silence=True,
            )

        pcm = pieces[0] if len(pieces) == 1 else np.concatenate(pieces)
        pcm = postproc.pad(pcm, self.sample_rate, pause_after_s)
        return SynthChunk(
            pcm=pcm,
            sample_rate=self.sample_rate,
            phoneme_len=phoneme_len,
            words=words,
        )


def _result_word_timings(result) -> List[WordTiming]:
    """Pull per-token timings that ``join_timestamps`` already computed."""
    out: List[WordTiming] = []
    for t in getattr(result, "tokens", None) or ():
        start = getattr(t, "start_ts", None)
        end = getattr(t, "end_ts", None)
        text = (getattr(t, "text", "") or "").strip()
        if start is None or end is None or not text:
            continue
        out.append(WordTiming(float(start), float(end), text))
    return out


def _snapshot_dirs(repo_id: str) -> List[str]:
    cache = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface"
    )
    hub = os.environ.get("HUGGINGFACE_HUB_CACHE") or os.path.join(cache, "hub")
    folder = "models--" + repo_id.replace("/", "--")
    return sorted(glob.glob(os.path.join(hub, folder, "snapshots", "*")))


def _local_voice_names(repo_id: str) -> List[str]:
    """Voice packs already on disk, so the UI can avoid offering ones that
    would trigger a network fetch and fail offline."""
    names = set()
    for snap in _snapshot_dirs(repo_id):
        for path in glob.glob(os.path.join(snap, "voices", "*.pt")):
            names.add(os.path.splitext(os.path.basename(path))[0])
    return sorted(names)


def _detect_model_revision(repo_id: str) -> str:
    dirs = _snapshot_dirs(repo_id)
    return os.path.basename(dirs[-1])[:12] if dirs else "unknown"
