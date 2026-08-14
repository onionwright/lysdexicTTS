"""Settings, stored as hand-editable TOML.

TOML rather than JSON because the interesting values here are things a person
will want to tune by hand -- ignore lists, pause lengths, lookahead depth -- and
TOML supports comments. Reading uses the standard library; only writing needs a
dependency.

Writes are atomic (temp file plus ``os.replace``) so a crash mid-save cannot
leave a truncated settings file that stops the app from starting.
"""

from __future__ import annotations

import copy
import logging
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any, Dict, List

from . import paths

log = logging.getLogger(__name__)

DEFAULTS: Dict[str, Any] = {
    "engine": {
        # Passing repo_id explicitly silences kokoro's startup warning.
        "repo_id": "hexgrad/Kokoro-82M",
        "lang_code": "a",
        "voice": "af_heart",  # the only voice cached locally out of the box
        "speed": 1.0,
        # 8 threads benchmarks ~20% faster on this 4-core part but oversubscribes
        # it and competes with the audio callback. 4 is the safe default.
        "torch_threads": 4,
        "warm_on_start": True,
        "prefer_offline": True,
        # Download a small starter set of voices on first run, the same way the
        # model itself arrives. Set false to stay strictly offline.
        "fetch_default_voices": True,
    },
    "audio": {
        "device": "",  # empty = system default
        "blocksize": 2048,  # 85ms at 24kHz; the main defence against underruns
        "latency": "high",
        "volume": 1.0,
        "trim_lead": True,
        "trailing_pause_s": 0.16,
        "paragraph_pause_s": 0.32,
        "fade_ms": 8,
        # Hold the audio connection open with an inaudible noise floor. Some
        # hearing aids and Bluetooth devices treat digital silence as "no
        # signal" and gate their processing off and on around every sentence,
        # which is audible as the noise cancelling switching. Off by default
        # because it keeps the audio device awake.
        "keep_audio_alive": False,
        # -70 dB RMS is the level measured to hold real hearing aids awake;
        # quieter and some of them still gate off between sentences.
        "keep_alive_db": -70.0,
        # white | pink | brown. Brown by default: at the same RMS level it is
        # the least perceptible of the three, which is the whole point of a
        # signal that exists only to stop the audio path sleeping. White is
        # flat, so most of its energy sits in the top octaves and it sounds
        # like an electrical buzz.
        "keep_alive_color": "brown",
    },
    "playback": {
        "lookahead_sentences": 3,
        "cache_max_seconds": 300,
        "prev_restart_threshold_s": 2.0,
        "prime_seconds": 10.0,
        "prime_max_chars": 110,
        "first_unit_max_chars": 0,  # 0 = same as prime_max_chars
    },
    "selection": {
        # aggressive | uia_only | modifier | off
        "mode": "aggressive",
        "drag_min_px": 12,
        "drag_min_ms": 60,
        "probe_delay_ms": 120,
        "min_chars": 1,
        "pill_auto_hide_ms": 4000,
        "enable_double_click": True,
        "enable_triple_click": True,
        # Turn off if you use Remote Desktop or an input remapper, which can
        # flag genuine input as injected.
        "ignore_injected": True,
        "ignore_classes": [
            "Shell_TrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow",
        ],
        "ignore_processes": [],
    },
    "clipboard": {
        "fallback_enabled": True,
        "restore": True,
        "copy_timeout_ms": 400,
        # Ctrl+C means "interrupt" in a terminal, never "copy".
        "blocklist": [
            "windowsterminal.exe", "conhost.exe", "cmd.exe",
            "powershell.exe", "pwsh.exe", "putty.exe", "mintty.exe",
        ],
    },
    "ui": {
        "max_sentences": 2000,
        "panel_geometry": "",
        "show_panel_on_read": True,
        # Text presentation in the reader panel. Larger text and looser line
        # spacing are among the few things with real evidence behind them for
        # dyslexic readers, so they are first-class settings rather than a
        # theme detail.
        "panel_font_pt": 13,
        "panel_line_spacing": 1.5,
        "panel_font_family": "",  # empty = system default sans-serif
    },
    "app": {
        "log_level": "INFO",
        # Windows 11 files new tray icons into the hidden overflow flyout, so a
        # tray-only app looks like it never started. On the first run we show
        # the panel and a notification so there is unmistakable proof of life.
        "first_run": True,
        "notify_on_ready": True,
        # Never write captured text to disk: it is the user's private content,
        # and a log full of everything they selected would look exactly like a
        # keylogger dump.
        "log_text": False,
        # A panic stop, registered only while audio is actually playing.
        # Deliberately not bare Escape: a global bare-Esc hotkey would swallow
        # Escape in every other application for the duration of playback.
        "stop_hotkey": "ctrl+alt+esc",
        "stop_hotkey_enabled": True,
    },
}


@dataclass
class Config:
    data: Dict[str, Any] = field(default_factory=lambda: copy.deepcopy(DEFAULTS))

    def section(self, name: str) -> Dict[str, Any]:
        return self.data.get(name, {})

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.data.get(section, {}).get(
            key, DEFAULTS.get(section, {}).get(key, default)
        )

    def set(self, section: str, key: str, value: Any) -> None:
        self.data.setdefault(section, {})[key] = value


def _merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> Config:
    """Load settings, falling back to defaults for anything missing or broken."""
    path = paths.settings_file()
    if not path.exists():
        cfg = Config()
        try:
            save(cfg)  # write a commented starting point the user can edit
        except Exception:
            log.debug("could not write initial settings", exc_info=True)
        return cfg
    try:
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        return Config(_merge(DEFAULTS, raw))
    except Exception:
        # A malformed settings file must never stop the app from starting.
        log.exception("settings file is invalid; using defaults: %s", path)
        return Config()


def save(cfg: Config) -> bool:
    try:
        import tomli_w
    except ImportError:
        log.error("tomli-w is not installed; cannot save settings")
        return False

    paths.ensure_dirs()
    path = paths.settings_file()
    tmp = path.with_suffix(".toml.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(_HEADER.encode("utf-8"))
            tomli_w.dump(cfg.data, fh)
        os.replace(tmp, path)  # atomic: never leaves a half-written file
        return True
    except Exception:
        log.exception("could not save settings to %s", path)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


_HEADER = """# Lysdexic TTS settings.
# Edit and save; the app reloads this file automatically.
# Delete the file to restore defaults.
#
# selection.mode:
#   aggressive  show the pill on any drag-select, even when UI Automation
#               cannot confirm one (needed for some Electron and PDF apps)
#   uia_only    only when UI Automation actually reports selected text
#   modifier    only while Ctrl is held as you release the selection
#   off         disable select-to-read entirely

"""
