"""Logging setup.

One rule stands out: **captured text is never written to disk.** It is the
user's private content, and a log file containing everything they ever selected
would both leak and look exactly like a keylogger dump -- which matters for an
app that already installs a global mouse hook.
"""

from __future__ import annotations

import datetime
import logging
import logging.handlers
import os
import sys
import threading
import traceback

from . import paths

MAX_BYTES = 1_000_000
BACKUPS = 3

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# The devnull object installed by ensure_std_streams, so setup() can tell a
# real console apart from the placeholder. A StreamHandler on the placeholder
# would satisfy "logging is configured" while writing every record to NUL.
_devnull_stream = None


def ensure_std_streams() -> None:
    """Give the process real stdout/stderr objects.

    Under ``pythonw.exe`` -- which is exactly how the app starts at logon --
    ``sys.stdout`` and ``sys.stderr`` are ``None``. That is fatal here, because
    kokoro's package ``__init__`` calls ``logger.add(sys.stderr, ...)`` and
    loguru raises ``TypeError: Cannot log to objects of type 'NoneType'``. The
    engine would then fail to load on every autostart while working perfectly
    from a console. Must run before anything imports kokoro.
    """
    global _devnull_stream
    devnull = None
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            if devnull is None:
                devnull = open(os.devnull, "w", encoding="utf-8")
                _devnull_stream = devnull
            setattr(sys, name, devnull)
            setattr(sys, "__%s__" % name, devnull)


def _fallback_note(message: str) -> None:
    """Last-resort breadcrumb next to the repo, for when the log dir itself is
    the problem. Mirrors run_reader.pyw's fallback so both land in one file."""
    try:
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "startup-fallback.log",
        )
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (stamp, message))
    except Exception:
        pass


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
        # A read-only profile must not stop the app from running, but a whole
        # session with no log anywhere once made a failure undiagnosable --
        # leave a breadcrumb saying WHY the log file could not be opened.
        _fallback_note(
            "reader.log unavailable at %s\n%s"
            % (paths.log_file(), traceback.format_exc())
        )

    if to_console and sys.stderr is not None and sys.stderr is not _devnull_stream:
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.setLevel(getattr(logging, level.upper(), logging.INFO))
        root.addHandler(console)

    # These are noisy and say nothing useful about our behaviour.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    logging.getLogger("filelock").setLevel(logging.WARNING)


def install_excepthooks() -> None:
    """Route uncaught exceptions -- main thread and workers -- into the log.

    Under pythonw the default hooks print to a stderr that goes nowhere, which
    is how a crash can leave a running-looking process and an empty log.
    """
    def _hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        logging.getLogger("reader.uncaught").critical(
            "uncaught exception", exc_info=(exc_type, exc, tb)
        )

    def _thread_hook(args) -> None:
        if args.exc_type is SystemExit:
            return
        logging.getLogger("reader.uncaught").critical(
            "uncaught exception in thread %r",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _hook
    threading.excepthook = _thread_hook


def redact(text: str) -> str:
    """Describe captured text without recording it."""
    stripped = (text or "").strip()
    return f"<{len(stripped)} chars>" if stripped else "<empty>"
