"""Start with Windows, via the per-user Run key.

``HKCU\\...\\CurrentVersion\\Run`` is chosen over the Startup folder (which would
need ``IShellLink`` COM to write a shortcut, and users delete shortcuts by
accident) and over Task Scheduler (heavier, and it draws more scrutiny from
endpoint protection). It also shows up in Settings > Apps > Startup, so the user
can turn it off somewhere they'd think to look.

``pythonw.exe`` is used so no console window flashes at logon. The entry must
stay **non-elevated**: an elevated process can neither install a hook for, nor
send input to, normal windows -- elevating would break the common case.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from .. import paths

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "KokoroReader"


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def command() -> str:
    launcher = paths.app_root() / "run_reader.pyw"
    return f'"{_pythonw()}" "{launcher}"'


def is_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        return False


def current_command() -> Optional[str]:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except (FileNotFoundError, OSError):
        return None


def enable() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        log.info("autostart enabled: %s", command())
        return True
    except Exception:
        log.exception("could not enable autostart")
        return False


def disable() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.info("autostart disabled")
        return True
    except FileNotFoundError:
        return True
    except Exception:
        log.exception("could not disable autostart")
        return False


def sync(enabled: bool) -> bool:
    """Make the registry match the setting, refreshing a stale path if needed."""
    if enabled:
        if is_enabled() and current_command() == command():
            return True
        return enable()
    return disable() if is_enabled() else True
