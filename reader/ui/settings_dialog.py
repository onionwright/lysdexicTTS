"""Built-in settings editor.

Handing the settings file to an external editor turned out to be unreliable on
a default Windows install, for two compounding reasons:

* ``.toml`` has no registered file handler, so ``os.startfile`` shows the "How
  do you want to open this file?" chooser instead of opening anything -- and it
  does not raise, so a try/except fallback never fires. Choosing Notepad from
  that dialog can fail with "the system cannot find the path specified".
* Launching ``notepad.exe`` directly is no better: Windows 11's tabbed Notepad
  forwards the file to an already-running instance and exits 0, and since a
  background process cannot take the foreground, the file silently never
  appears.

Editing in-app sidesteps all of it, and has the added benefit of validating the
TOML before saving, so a typo can't leave the app unable to start.

Unlike the reader panel and the pill, this is a normal focusable window -- you
have to be able to type into it.
"""

from __future__ import annotations

import logging
import tomllib
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import APP_NAME, config as configmod, paths
from .theme import THEME

log = logging.getLogger(__name__)

DIALOG_QSS = f"""
QDialog {{ background: {THEME.panel_bg}; }}
QPlainTextEdit {{
    background: {THEME.header_bg};
    color: {THEME.text};
    border: 1px solid {THEME.panel_border};
    border-radius: 6px;
    padding: 8px;
    selection-background-color: {THEME.speaking_bg};
}}
QLabel {{ color: {THEME.text_dim}; }}
QLabel#errorLabel {{ color: {THEME.danger}; }}
QLabel#okLabel {{ color: {THEME.accent}; }}
QPushButton {{
    background: {THEME.button_hover};
    color: {THEME.text};
    border: none;
    border-radius: 6px;
    padding: 7px 14px;
}}
QPushButton:hover {{ background: {THEME.button_down}; }}
QPushButton:default {{ background: {THEME.speaking_bg}; color: #ffffff; }}
"""


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setStyleSheet(DIALOG_QSS)
        self.resize(720, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.path_label = QLabel(str(paths.settings_file()), self)
        self.path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self.path_label)

        self.editor = QPlainTextEdit(self)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(10)
        self.editor.setFont(font)
        self.editor.setTabStopDistance(28)
        layout.addWidget(self.editor, 1)

        self.status = QLabel("", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.btn_defaults = QPushButton("Restore defaults", self)
        self.btn_defaults.clicked.connect(self._restore_defaults)
        row.addWidget(self.btn_defaults)
        self.btn_reload = QPushButton("Reload from disk", self)
        self.btn_reload.clicked.connect(self.load_from_disk)
        row.addWidget(self.btn_reload)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        row.addWidget(buttons)
        layout.addLayout(row)

        self.load_from_disk()

    # ------------------------------------------------------------- loading

    def load_from_disk(self) -> None:
        path = paths.settings_file()
        if not path.exists():
            configmod.save(configmod.Config())
        try:
            self.editor.setPlainText(path.read_text(encoding="utf-8"))
            self._say("", ok=True)
        except Exception as exc:
            self.editor.setPlainText("")
            self._say(f"Could not read the settings file: {exc}", ok=False)

    def _restore_defaults(self) -> None:
        confirm = QMessageBox.question(
            self,
            "Restore defaults",
            "Replace the editor contents with the default settings?\n"
            "Nothing is written until you press Save.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        import io

        try:
            import tomli_w

            buf = io.BytesIO()
            buf.write(configmod._HEADER.encode("utf-8"))
            tomli_w.dump(configmod.Config().data, buf)
            self.editor.setPlainText(buf.getvalue().decode("utf-8"))
            self._say("Defaults loaded into the editor. Press Save to apply.", ok=True)
        except Exception as exc:
            self._say(f"Could not build defaults: {exc}", ok=False)

    # -------------------------------------------------------------- saving

    def _save(self) -> None:
        text = self.editor.toPlainText()
        try:
            # Validate before writing: a typo here would otherwise be discovered
            # only at the next launch.
            tomllib.loads(text)
        except Exception as exc:
            self._say(f"Not valid TOML — nothing was saved.\n{exc}", ok=False)
            return

        path = paths.settings_file()
        tmp = path.with_suffix(".toml.tmp")
        try:
            paths.ensure_dirs()
            tmp.write_text(text, encoding="utf-8")
            import os

            os.replace(tmp, path)  # atomic
        except Exception as exc:
            self._say(f"Could not save: {exc}", ok=False)
            return

        log.info("settings saved from the built-in editor")
        self._say("Saved and applied.", ok=True)
        self.saved.emit()
        self.accept()

    def _say(self, message: str, ok: bool) -> None:
        self.status.setObjectName("okLabel" if ok else "errorLabel")
        self.status.setStyleSheet("")  # force a re-evaluation of the QSS
        self.status.setText(message)

    # --------------------------------------------------------------- show

    def show_focused(self) -> None:
        self.load_from_disk()
        self.show()
        self.raise_()
        self.activateWindow()
