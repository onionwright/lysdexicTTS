"""Where the app keeps things.

Config and logs live under the user profile rather than beside the code, so they
survive a reinstall or a fresh clone and nothing user-specific ever lands in the
repository.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "KokoroReader"


def _base(env: str, fallback: str) -> Path:
    root = os.environ.get(env)
    if not root:
        root = os.path.join(os.path.expanduser("~"), fallback)
    return Path(root) / APP_NAME


def config_dir() -> Path:
    return _base("APPDATA", "AppData/Roaming")


def data_dir() -> Path:
    return _base("LOCALAPPDATA", "AppData/Local")


def settings_file() -> Path:
    return config_dir() / "settings.toml"


def log_file() -> Path:
    return data_dir() / "logs" / "reader.log"


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    (data_dir() / "logs").mkdir(parents=True, exist_ok=True)
