"""Settings and hotkey-parsing tests."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reader import config as configmod  # noqa: E402
from reader.win.hotkey import MOD_ALT, MOD_CONTROL, parse  # noqa: E402


def test_defaults_are_complete():
    cfg = configmod.Config()
    assert cfg.get("engine", "voice") == "af_heart"
    assert cfg.get("audio", "blocksize") == 2048
    assert cfg.get("selection", "mode") == "aggressive"
    # Captured text must never be written to disk.
    assert cfg.get("app", "log_text") is False


def test_missing_keys_fall_back_to_defaults():
    """A partial settings file must not strip out everything it omits."""
    cfg = configmod.Config(configmod._merge(
        configmod.DEFAULTS, {"engine": {"speed": 1.4}}
    ))
    assert cfg.get("engine", "speed") == 1.4
    assert cfg.get("engine", "voice") == "af_heart"
    assert cfg.get("audio", "blocksize") == 2048


def test_unknown_key_lookup_uses_default_argument():
    cfg = configmod.Config()
    assert cfg.get("engine", "nonexistent", "fallback") == "fallback"


def test_merge_does_not_mutate_defaults():
    before = configmod.DEFAULTS["engine"]["voice"]
    configmod._merge(configmod.DEFAULTS, {"engine": {"voice": "bm_george"}})
    assert configmod.DEFAULTS["engine"]["voice"] == before


def test_a_tuned_pill_timeout_survives_the_move_to_its_own_section():
    """It used to live under [selection]; someone who had already changed it
    must not be quietly put back on the default."""
    raw = {"selection": {"pill_auto_hide_ms": 9000, "mode": "modifier"}}
    cfg = configmod.Config(configmod._merge(configmod.DEFAULTS,
                                            configmod._migrate(raw)))
    assert cfg.get("pill", "auto_hide_ms") == 9000
    assert cfg.get("selection", "mode") == "modifier", "the rest is untouched"


def test_the_new_key_wins_when_both_are_present():
    raw = {"selection": {"pill_auto_hide_ms": 9000}, "pill": {"auto_hide_ms": 2500}}
    cfg = configmod.Config(configmod._merge(configmod.DEFAULTS,
                                            configmod._migrate(raw)))
    assert cfg.get("pill", "auto_hide_ms") == 2500


def test_migration_leaves_a_file_that_never_had_the_old_key_alone():
    cfg = configmod.Config(configmod._merge(configmod.DEFAULTS,
                                            configmod._migrate({})))
    assert cfg.get("pill", "auto_hide_ms") == 4000


def test_migration_tolerates_nonsense_in_the_old_key():
    """A hand-edited settings file must never stop the app from starting."""
    raw = {"selection": {"pill_auto_hide_ms": "four thousand"}}
    cfg = configmod.Config(configmod._merge(configmod.DEFAULTS,
                                            configmod._migrate(raw)))
    assert cfg.get("pill", "auto_hide_ms") == 4000


def test_the_old_key_does_not_linger_after_migration():
    """It would be written straight back out on the next save, and then it is
    ambiguous which of the two the app is honouring."""
    raw = {"selection": {"pill_auto_hide_ms": 9000}}
    migrated = configmod._migrate(raw)
    assert "pill_auto_hide_ms" not in migrated["selection"]
    assert "pill_auto_hide_ms" not in configmod.DEFAULTS["selection"]


@pytest.mark.parametrize(
    "spec,mods,vk",
    [
        ("ctrl+alt+esc", MOD_CONTROL | MOD_ALT, 0x1B),
        ("Ctrl+Alt+Escape", MOD_CONTROL | MOD_ALT, 0x1B),
        ("ctrl+shift+s", MOD_CONTROL | 0x0004, ord("S")),
        ("ctrl+alt+f9", MOD_CONTROL | MOD_ALT, 0x78),
    ],
)
def test_hotkey_parsing(spec, mods, vk):
    parsed = parse(spec)
    assert parsed is not None
    got_mods, got_vk = parsed
    assert got_mods & mods == mods
    assert got_vk == vk


@pytest.mark.parametrize("spec", ["esc", "s", "", "ctrl+", "ctrl+nonsense"])
def test_hotkey_rejects_unusable_specs(spec):
    """A bare key would be taken away from every other application."""
    assert parse(spec) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
