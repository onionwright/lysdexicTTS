"""Console-less launcher.

Run with ``pythonw.exe`` so no console window flashes at logon; this is the
target of the HKCU Run registry value that the tray's "Start with Windows"
option installs.

That combination -- no console, and imports that run before the logging in
``reader.log`` exists -- used to mean a failure here left no trace at all: no
log line, no console output, no crash dialog. An autostart that quietly does
nothing is indistinguishable from one that never fired, which makes the
difference impossible to diagnose after the fact.

So this file keeps its own breadcrumb trail in ``startup.log``, next to
``reader.log``. It uses nothing but the standard library and never raises,
because the whole point is to still work when importing the app does not.
"""

import datetime
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Deliberately not reader.paths: resolving the log location must not depend on
# the package whose import may be the thing that failed. Kept in step with
# paths.data_dir() by hand -- it is two lines and changes about never.
_MAX_BYTES = 200_000


def _startup_log() -> str:
    root = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local"
    )
    return os.path.join(root, "KokoroReader", "logs", "startup.log")


def _fallback_log() -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "startup-fallback.log"
    )


def _append(path: str, message: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # One line per launch, so this grows very slowly; truncate rather than
    # rotate to keep the whole thing dependency-free.
    try:
        if os.path.getsize(path) > _MAX_BYTES:
            os.remove(path)
    except OSError:
        pass
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("%s %s\n" % (stamp, message))


def _note(message: str) -> None:
    """Append one line; if the normal location fails, fall back to a file next
    to this script. A launch that leaves no trace anywhere once made a whole
    session undiagnosable, so silence is only acceptable when both fail."""
    try:
        _append(_startup_log(), message)
    except Exception:
        try:
            _append(
                _fallback_log(),
                "%s\n  (normal startup.log unwritable: %s)"
                % (message, traceback.format_exc().strip().splitlines()[-1]),
            )
        except Exception:
            pass


# Written before anything else can fail, so a missing line here means the
# process never started -- which distinguishes "the Run key did not fire" from
# "it fired and the app died", the exact question that was unanswerable before.
_note("launch: pid=%d exe=%s" % (os.getpid(), sys.executable))

try:
    # pythonw.exe gives the process no stdout/stderr at all. Several libraries --
    # kokoro's loguru setup among them -- assume those exist and raise at import
    # time if they are None, so they must be replaced before anything else loads.
    from reader.log import ensure_std_streams  # noqa: E402

    ensure_std_streams()

    from reader.app import main  # noqa: E402
except Exception:
    _note("IMPORT FAILED\n%s" % traceback.format_exc())
    raise

if __name__ == "__main__":
    try:
        code = main()
    except SystemExit as exc:  # main() is also allowed to exit directly
        _note("exit: code=%s (SystemExit)" % exc.code)
        raise
    except BaseException:
        _note("RUN FAILED\n%s" % traceback.format_exc())
        raise
    # A code of 0 arriving within a second or two of the launch line means the
    # single-instance check turned it away, not that the app ran and quit.
    _note("exit: code=%s" % code)
    sys.exit(code)
