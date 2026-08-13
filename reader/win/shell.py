"""Opening files and folders without depending on file associations.

``os.startfile`` is the obvious way to open the settings file, and it is the
wrong one here: **.toml has no registered handler on a default Windows install**.
Rather than failing, the shell pops the "How do you want to open this file?"
chooser, and picking Notepad from that dialog can fail outright with "the system
cannot find the path specified" because the shell resolves an app-execution
alias that isn't valid in that context.

Worse, ``os.startfile`` does not raise in that situation, so a try/except
fallback never fires. The association therefore has to be checked *before*
deciding how to open the file.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def has_association(extension: str) -> bool:
    """True if Windows knows how to open ``extension`` (e.g. '.toml')."""
    if sys.platform != "win32":
        return False
    import winreg

    # An explicit user choice takes precedence over the machine association.
    user_choice = (
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
        rf"\{extension}\UserChoice"
    )
    for root, key in (
        (winreg.HKEY_CURRENT_USER, user_choice),
        (winreg.HKEY_CLASSES_ROOT, extension),
    ):
        try:
            with winreg.OpenKey(root, key):
                return True
        except (FileNotFoundError, OSError):
            continue
    return False


def open_for_editing(path: Path | str) -> bool:
    """Open ``path`` in a text editor. Returns True if something launched.

    Honours the user's own editor when the extension is associated, and falls
    back to Notepad, which is always present.
    """
    path = Path(path)
    if not path.exists():
        log.warning("cannot open a file that does not exist: %s", path)
        return False

    if sys.platform != "win32":
        try:
            subprocess.Popen(["xdg-open", str(path)])
            return True
        except OSError:
            return False

    if has_association(path.suffix):
        try:
            os.startfile(str(path))  # noqa: S606 - the user's own config file
            return True
        except OSError:
            log.debug("startfile failed for %s", path, exc_info=True)

    # No handler registered: go straight to Notepad rather than letting the
    # shell show a chooser that cannot successfully launch anything.
    for editor in ("notepad.exe", "write.exe"):
        try:
            subprocess.Popen([editor, str(path)])
            return True
        except OSError:
            continue

    log.error("could not open %s in any editor", path)
    return False


def reveal_in_explorer(path: Path | str) -> bool:
    """Open Explorer with ``path`` selected (or the folder itself)."""
    path = Path(path)
    if sys.platform != "win32":
        return False
    try:
        if path.is_dir():
            subprocess.Popen(["explorer.exe", str(path)])
        else:
            # /select, must stay glued to the path as one argument pair.
            subprocess.Popen(f'explorer.exe /select,"{path}"')
        return True
    except OSError:
        log.debug("could not reveal %s", path, exc_info=True)
        return False
