"""Getting the actual text once the user asks for it.

Order matters and is deliberate:

1. **UI Automation**, if the watcher already read the selection. Zero side
   effects -- no clipboard, no keystrokes.
2. **Synthetic Ctrl+C**, only as a fallback, and only at this point: when the
   user has actually clicked Read or Copy. The passive probe that decides
   whether to show the pill never touches the clipboard, so merely selecting
   text can never clobber what you had copied.

The clipboard is snapshotted and restored around the fallback, and terminals are
excluded entirely because Ctrl+C means "interrupt" there, not "copy".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from . import clipboard, keys

log = logging.getLogger(__name__)

# Ctrl+C is destructive in these: it sends an interrupt rather than copying.
DEFAULT_CLIPBOARD_BLOCKLIST = (
    "windowsterminal.exe",
    "conhost.exe",
    "cmd.exe",
    "powershell.exe",
    "pwsh.exe",
    "putty.exe",
    "mintty.exe",
)


@dataclass(slots=True)
class Capture:
    text: str
    method: str  # 'uia' | 'clipboard' | 'none'
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())


def capture_text(
    candidate,
    *,
    allow_clipboard: bool = True,
    restore_clipboard: bool = True,
    blocklist: Sequence[str] = DEFAULT_CLIPBOARD_BLOCKLIST,
    copy_timeout_ms: int = 400,
) -> Capture:
    """Resolve a :class:`SelectionCandidate` into text."""
    if candidate is not None and candidate.text.strip():
        return Capture(candidate.text, "uia")

    if not allow_clipboard:
        return Capture("", "none", "no text available and clipboard fallback disabled")

    process = (getattr(candidate, "process", "") or "").lower()
    if process in {p.lower() for p in blocklist}:
        return Capture(
            "", "none",
            f"{process} treats Ctrl+C as interrupt, so it is not used there",
        )

    # Never inject into a window other than the one the selection came from.
    expected = getattr(candidate, "hwnd", 0)
    current = keys.foreground_window()
    if expected and current and expected != current:
        log.debug("foreground changed (%s -> %s); not injecting", expected, current)
        return Capture("", "none", "focus moved before the text could be read")

    saved = clipboard.snapshot() if restore_clipboard else None
    before = clipboard.sequence_number()
    try:
        keys.send_ctrl_c()
    except keys.InputBlockedError as exc:
        if saved is not None:
            clipboard.restore(saved)
        return Capture("", "none", str(exc))

    deadline = time.time() + copy_timeout_ms / 1000.0
    while time.time() < deadline and clipboard.sequence_number() == before:
        time.sleep(0.02)

    text = clipboard.get_text()
    if saved is not None:
        clipboard.restore(saved)

    if not text.strip():
        return Capture("", "none", "nothing was selected, or the app did not copy")
    return Capture(text, "clipboard")


def copy_to_clipboard(candidate) -> Capture:
    """Back the Copy button.

    If UI Automation already has the text we simply place it on the clipboard.
    Otherwise a plain Ctrl+C does the job -- and here we deliberately do **not**
    restore, because putting the text on the clipboard is the whole point.
    """
    if candidate is not None and candidate.text.strip():
        ok = clipboard.set_text(candidate.text)
        return Capture(
            candidate.text if ok else "",
            "uia" if ok else "none",
            None if ok else "could not open the clipboard",
        )
    return capture_text(candidate, restore_clipboard=False)
