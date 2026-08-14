"""System tray presence.

The tray icon is the app: it appears immediately at launch and stays there, so
the ~7s model load happens once in the background rather than on every read.
Its color doubles as a status light.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import APP_NAME
from .icons import status_icon
from .theme import THEME

_STATE_COLORS = {
    "loading": "#8b93a1",
    "ready": THEME.accent,
    "reading": "#3ec98a",
    "warning": "#e0a33e",
    "error": THEME.danger,
}


class Tray(QSystemTrayIcon):
    read_clipboard = Signal()
    show_panel = Signal()
    stop_reading = Signal()
    quit_requested = Signal()
    open_settings = Signal()
    open_settings_folder = Signal()
    reload_settings = Signal()
    watcher_toggled = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._state = ""
        self.set_state("loading", f"{APP_NAME} — loading model...")

        menu = QMenu()
        self.act_read = QAction("Read clipboard", menu)
        self.act_read.triggered.connect(self.read_clipboard)
        menu.addAction(self.act_read)

        self.act_show = QAction("Show reader panel", menu)
        self.act_show.triggered.connect(self.show_panel)
        menu.addAction(self.act_show)

        self.act_stop = QAction("Stop reading", menu)
        self.act_stop.triggered.connect(self.stop_reading)
        self.act_stop.setEnabled(False)
        menu.addAction(self.act_stop)

        menu.addSeparator()

        self.act_watch = QAction("Select-to-read", menu)
        self.act_watch.setCheckable(True)
        self.act_watch.setChecked(True)
        self.act_watch.toggled.connect(self.watcher_toggled)
        menu.addAction(self.act_watch)

        act_settings = QAction("Settings…", menu)
        act_settings.triggered.connect(self.open_settings)
        menu.addAction(act_settings)

        act_reload = QAction("Reload settings from file", menu)
        act_reload.triggered.connect(self.reload_settings)
        menu.addAction(act_reload)

        menu.addSeparator()
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.quit_requested)
        menu.addAction(act_quit)

        self.setContextMenu(menu)
        self.activated.connect(self._on_activated)
        self._menu = menu

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_panel.emit()

    def set_state(self, state: str, tooltip: str = "") -> None:
        if state != self._state:
            self._state = state
            self.setIcon(status_icon(_STATE_COLORS.get(state, THEME.accent)))
        if tooltip:
            self.setToolTip(tooltip)

    def set_reading(self, reading: bool) -> None:
        self.act_stop.setEnabled(reading)

    def set_watcher_checked(self, checked: bool) -> None:
        self.act_watch.blockSignals(True)
        self.act_watch.setChecked(checked)
        self.act_watch.blockSignals(False)
