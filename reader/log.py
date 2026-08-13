"""Logging setup.

One rule stands out: **captured text is never written to disk.** It is the
user's private content, and a log file containing everything they ever selected
would both leak and look exactly like a keylogger dump -- which matters for an
app that already installs a global mouse hook.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys

from . import paths

MAX_BYTES = 1_000_000
BACKUPS = 3

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def ensure_std_streams() -> None:
    """Give the process real stdout/stderr objects.

    Under ``pythonw.exe`` -- which is exactly how the app starts at logon --
    ``sys.stdout`` and ``sys.stderr`` are ``None``. That is fatal here, because
    kokoro's package ``__init__`` calls ``logger.add(sys.stderr, ...)`` and
    loguru raises ``TypeError: Cannot log to objects of type 'NoneType'``. The
    engine would then fail to load on every autostart while working perfectly
    from a console. Must run before anything imports kokoro.
    """
    devnull = None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            if devnull is None:
                devnull = open(os.devnull, "w", encoding="utf-8")
            setattr(sys, name, devnull)
            setattr(sys, "__%s__" % name, devnull)


def setup(level: str = "INFO", to_console: bool = True) -> None:
    paths.ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            paths.log_file(), maxBytes=MAX_BYTES, backupCount=BACKUPS,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(file_handler)
    except Exception:
        pass  # a read-only profile must not stop the app from running

    if to_console and sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(console)

    # These are noisy and say nothing useful about our behaviour.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    logging.getLogger("filelock").setLevel(logging.WARNING)


def redact(text: str) -> str:
    """Describe captured text without recording it."""
    stripped = (text or "").strip()
    return f"<{len(stripped)} chars>" if stripped else "<empty>"
