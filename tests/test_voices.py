"""Voice naming and availability logic (no network required)."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader.tts.kokoro_engine import (  # noqa: E402
    DEFAULT_VOICES,
    KNOWN_VOICES,
    KokoroEngine,
)
from reader.ui.settings_window import _pretty_voice  # noqa: E402


@pytest.mark.parametrize(
    "code,expected",
    [
        ("af_heart", "Heart (American, female)"),
        ("am_michael", "Michael (American, male)"),
        ("bf_emma", "Emma (British, female)"),
        ("bm_george", "George (British, male)"),
    ],
)
def test_voice_names_are_human_readable(code, expected):
    """Users should never be shown 'bm_george'."""
    assert _pretty_voice(code) == expected


def test_unrecognised_codes_degrade_gracefully():
    assert "_" not in _pretty_voice("zz_mystery")


def test_default_voices_cover_both_accents_and_genders():
    """The starter set should offer a real choice, not five similar voices."""
    assert DEFAULT_VOICES[0] == "af_heart", "the configured default comes first"
    assert len(DEFAULT_VOICES) >= 5
    assert all(v in KNOWN_VOICES for v in DEFAULT_VOICES)
    prefixes = {v[:2] for v in DEFAULT_VOICES}
    assert {"af", "am", "bf", "bm"} <= prefixes, (
        f"expected American and British, male and female; got {prefixes}"
    )


def test_downloadable_excludes_what_is_installed(monkeypatch):
    engine = KokoroEngine()
    monkeypatch.setattr(
        engine, "available_voices", lambda: ["af_heart", "bm_george"]
    )
    more = engine.downloadable_voices()
    assert "af_heart" not in more and "bm_george" not in more
    assert "af_bella" in more
    assert len(more) == len(KNOWN_VOICES) - 2


def test_download_refuses_unknown_names(monkeypatch):
    """Guards the filename that gets interpolated into a download path."""
    engine = KokoroEngine()

    def explode(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("must not reach the network for an unknown voice")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", explode)
    assert engine.download_voice("../../etc/passwd") is False
    assert engine.download_voice("no_such_voice") is False


def test_offline_pin_is_restored_after_a_failed_download(monkeypatch):
    """A failed download must not leave the process permanently un-pinned.

    Note the library snapshots HF_HUB_OFFLINE into a module constant at import
    time, so the constant does not track the environment variable -- which is
    exactly why download_voice has to flip both. The contract asserted here is
    restoration of whatever the previous values were.
    """
    import huggingface_hub.constants as constants

    engine = KokoroEngine()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    constants.HF_HUB_OFFLINE = True

    def explode(*args, **kwargs):
        raise RuntimeError("no network")

    monkeypatch.setattr("huggingface_hub.hf_hub_download", explode)
    try:
        assert engine.download_voice("af_bella") is False
        assert os.environ.get("HF_HUB_OFFLINE") == "1", "env var restored"
        assert constants.HF_HUB_OFFLINE is True, "library flag restored"
    finally:
        constants.HF_HUB_OFFLINE = False


def test_download_lifts_the_offline_pin_while_it_runs(monkeypatch):
    """Otherwise a download would always fail on a machine that has the model
    cached, which is every machine after the first run."""
    import huggingface_hub.constants as constants

    engine = KokoroEngine()
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    constants.HF_HUB_OFFLINE = True
    seen = {}

    def capture(*args, **kwargs):
        seen["env"] = os.environ.get("HF_HUB_OFFLINE")
        seen["flag"] = constants.HF_HUB_OFFLINE
        return "fake/path/af_bella.pt"

    monkeypatch.setattr("huggingface_hub.hf_hub_download", capture)
    try:
        assert engine.download_voice("af_bella") is True
        assert seen["env"] is None, "env pin lifted during the download"
        assert seen["flag"] is False, "library flag lifted during the download"
    finally:
        constants.HF_HUB_OFFLINE = False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
