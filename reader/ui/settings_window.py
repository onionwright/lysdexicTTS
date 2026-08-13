"""Visual settings.

This app is for people who find walls of text hard work, so its own settings
must not be a wall of text. Every option here is a real control with a
plain-language name and one short line saying what it does. No key names, no
syntax, nothing to parse.

Design rules followed throughout, all of them ordinary dyslexia-friendly
practice: generous spacing, short left-aligned lines (never justified), no
italics, sans-serif type, off-white on dark grey rather than maximum contrast,
and a live value beside every slider so nothing has to be inferred.

Changes apply and save immediately. There is no Save button to forget, and no
way to be left wondering whether something took effect.

The raw TOML editor still exists, one click away under Advanced, for anyone who
prefers it.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSlider,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, config as configmod, paths
from ..tts.kokoro_engine import KNOWN_VOICES
from .theme import THEME

log = logging.getLogger(__name__)

WINDOW_QSS = f"""
QWidget#settingsRoot {{ background: {THEME.panel_bg}; }}
QListWidget#navList {{
    background: {THEME.header_bg};
    color: {THEME.text};
    border: none;
    outline: none;
    padding: 8px 6px;
    font-size: 15px;
}}
QListWidget#navList::item {{
    padding: 11px 14px;
    border-radius: 8px;
    margin: 2px 4px;
}}
QListWidget#navList::item:selected {{
    background: {THEME.speaking_bg};
    color: #ffffff;
}}
QListWidget#navList::item:hover:!selected {{ background: {THEME.button_hover}; }}

QLabel#pageTitle {{
    color: {THEME.text};
    font-size: 21px;
    font-weight: 600;
    padding-bottom: 2px;
}}
QLabel#settingName {{ color: {THEME.text}; font-size: 15px; }}
QLabel#settingHelp {{ color: {THEME.text_dim}; font-size: 13px; }}
QLabel#settingValue {{
    color: {THEME.accent};
    font-size: 15px;
    font-weight: 600;
}}
QLabel#savedFlag {{ color: {THEME.accent}; font-size: 13px; }}

QComboBox {{
    background: {THEME.header_bg};
    color: {THEME.text};
    border: 1px solid {THEME.panel_border};
    border-radius: 7px;
    padding: 8px 12px;
    font-size: 15px;
    min-width: 240px;
}}
QComboBox:hover {{ border-color: {THEME.accent}; }}
QComboBox QAbstractItemView {{
    background: {THEME.header_bg};
    color: {THEME.text};
    selection-background-color: {THEME.speaking_bg};
    padding: 4px;
    font-size: 15px;
}}
QCheckBox {{ color: {THEME.text}; font-size: 15px; spacing: 10px; }}
QCheckBox::indicator {{ width: 20px; height: 20px; }}
QCheckBox::indicator:unchecked {{
    border: 2px solid {THEME.text_dim};
    border-radius: 5px;
    background: transparent;
}}
QCheckBox::indicator:checked {{
    border: 2px solid {THEME.accent};
    border-radius: 5px;
    background: {THEME.accent};
}}
QSlider::groove:horizontal {{
    height: 6px;
    background: {THEME.button_down};
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{ background: {THEME.accent}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 18px;
    height: 18px;
    margin: -7px 0;
    border-radius: 9px;
}}
QPushButton {{
    background: {THEME.button_hover};
    color: {THEME.text};
    border: none;
    border-radius: 7px;
    padding: 9px 16px;
    font-size: 14px;
}}
QPushButton:hover {{ background: {THEME.button_down}; }}
QFrame#divider {{ background: {THEME.panel_border}; max-height: 1px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""


class SettingsWindow(QWidget):
    """Form-based settings. Changes apply immediately."""

    applied = Signal()
    open_raw_editor = Signal()

    def __init__(self, engine=None, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("settingsRoot")
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setStyleSheet(WINDOW_QSS)
        self.resize(940, 660)
        self.setMinimumSize(760, 520)

        self.engine = engine
        self.cfg = configmod.load()
        self._loading = False

        # Saving is debounced so dragging a slider doesn't rewrite the file on
        # every pixel of movement.
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._commit)

        self._build()
        self._load_values()

    # --------------------------------------------------------------- layout

    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.nav = QListWidget(self)
        self.nav.setObjectName("navList")
        self.nav.setFixedWidth(210)
        outer.addWidget(self.nav)

        right = QVBoxLayout()
        right.setContentsMargins(28, 24, 28, 18)
        right.setSpacing(14)

        self.pages = QStackedWidget(self)
        right.addWidget(self.pages, 1)

        footer = QHBoxLayout()
        self.saved_flag = QLabel("", self)
        self.saved_flag.setObjectName("savedFlag")
        footer.addWidget(self.saved_flag)
        footer.addStretch(1)
        btn_close = QPushButton("Close", self)
        btn_close.clicked.connect(self.hide)
        footer.addWidget(btn_close)
        right.addLayout(footer)

        outer.addLayout(right, 1)

        self._add_page("Voice", self._page_voice)
        self._add_page("Reading", self._page_reading)
        self._add_page("Selecting text", self._page_selection)
        self._add_page("Text size", self._page_appearance)
        self._add_page("Starting up", self._page_startup)
        self._add_page("Advanced", self._page_advanced)

        self.nav.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.nav.setCurrentRow(0)

    def _add_page(self, name: str, builder: Callable[[QVBoxLayout], None]) -> None:
        item = QListWidgetItem(name)
        self.nav.addItem(item)

        page = QWidget()
        column = QVBoxLayout(page)
        column.setContentsMargins(0, 0, 12, 0)
        column.setSpacing(4)

        title = QLabel(name, page)
        title.setObjectName("pageTitle")
        column.addWidget(title)
        column.addSpacing(6)

        builder(column)
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        self.pages.addWidget(scroll)

    # ------------------------------------------------------------ controls

    def _row(self, column: QVBoxLayout, name: str, help_text: str) -> QVBoxLayout:
        """One setting: bold name, one short line of help, then the control."""
        column.addSpacing(12)
        label = QLabel(name)
        label.setObjectName("settingName")
        column.addWidget(label)
        if help_text:
            helper = QLabel(help_text)
            helper.setObjectName("settingHelp")
            helper.setWordWrap(True)
            column.addWidget(helper)
        holder = QVBoxLayout()
        holder.setContentsMargins(0, 6, 0, 0)
        column.addLayout(holder)
        return holder

    def _divider(self, column: QVBoxLayout) -> None:
        column.addSpacing(10)
        line = QFrame()
        line.setObjectName("divider")
        line.setFrameShape(QFrame.Shape.HLine)
        column.addWidget(line)

    def _combo(
        self, column: QVBoxLayout, name: str, help_text: str,
        options: List[tuple], section: str, key: str,
    ) -> QComboBox:
        holder = self._row(column, name, help_text)
        box = QComboBox()
        for label, value in options:
            box.addItem(label, value)
        box.currentIndexChanged.connect(
            lambda _i, b=box: self._set(section, key, b.currentData())
        )
        holder.addWidget(box, 0, Qt.AlignmentFlag.AlignLeft)
        return box

    def _check(
        self, column: QVBoxLayout, name: str, help_text: str,
        section: str, key: str,
    ) -> QCheckBox:
        column.addSpacing(12)
        box = QCheckBox(name)
        box.toggled.connect(lambda v: self._set(section, key, bool(v)))
        column.addWidget(box)
        if help_text:
            helper = QLabel(help_text)
            helper.setObjectName("settingHelp")
            helper.setWordWrap(True)
            helper.setContentsMargins(30, 0, 0, 0)
            column.addWidget(helper)
        return box

    def _slider(
        self, column: QVBoxLayout, name: str, help_text: str,
        section: str, key: str, lo: float, hi: float, step: float,
        fmt: Callable[[float], str],
    ) -> QSlider:
        holder = self._row(column, name, help_text)
        line = QHBoxLayout()
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setMinimum(0)
        slider.setMaximum(int(round((hi - lo) / step)))
        slider.setMaximumWidth(360)
        value_label = QLabel("")
        value_label.setObjectName("settingValue")
        value_label.setMinimumWidth(96)

        def on_change(raw: int) -> None:
            value = lo + raw * step
            value_label.setText(fmt(value))
            self._set(section, key, round(value, 3))

        slider.valueChanged.connect(on_change)
        slider._to_raw = lambda v: int(round((v - lo) / step))  # type: ignore[attr-defined]
        slider._label = value_label  # type: ignore[attr-defined]
        slider._fmt = fmt  # type: ignore[attr-defined]
        line.addWidget(slider)
        line.addSpacing(14)
        line.addWidget(value_label)
        line.addStretch(1)
        holder.addLayout(line)
        return slider

    # --------------------------------------------------------------- pages

    def _page_voice(self, column: QVBoxLayout) -> None:
        cached = []
        if self.engine is not None:
            try:
                cached = self.engine.available_voices()
            except Exception:
                cached = []
        cached = cached or ["af_heart"]
        options = [(_pretty_voice(v), v) for v in cached]
        self.voice_box = self._combo(
            column, "Voice",
            "The speaking voice. Only downloaded voices are listed.",
            options, "engine", "voice",
        )
        missing = len([v for v in KNOWN_VOICES if v not in cached])
        if missing:
            note = QLabel(
                f"{missing} more voices exist but are not downloaded yet."
            )
            note.setObjectName("settingHelp")
            note.setWordWrap(True)
            column.addWidget(note)

        self._divider(column)
        self.speed_slider = self._slider(
            column, "Speaking speed",
            "How fast the voice talks. 1.0 is the normal pace.",
            "engine", "speed", 0.5, 2.0, 0.05, lambda v: f"{v:.2f} ×",
        )
        self.volume_slider = self._slider(
            column, "Volume", "How loud the reading is.",
            "audio", "volume", 0.0, 1.0, 0.05, lambda v: f"{int(v * 100)} %",
        )

    def _page_reading(self, column: QVBoxLayout) -> None:
        self.sentence_pause = self._slider(
            column, "Pause between sentences",
            "A longer pause gives you more time to take each sentence in.",
            "audio", "trailing_pause_s", 0.0, 1.2, 0.02, lambda v: f"{v:.2f} s",
        )
        self.paragraph_pause = self._slider(
            column, "Pause between paragraphs",
            "A longer break where a new paragraph or heading starts.",
            "audio", "paragraph_pause_s", 0.0, 2.0, 0.05, lambda v: f"{v:.2f} s",
        )
        self._divider(column)
        self.restart_threshold = self._slider(
            column, "Back button restarts the sentence after",
            "Press back before this and it goes to the previous sentence; "
            "after it, it repeats the one you are on.",
            "playback", "prev_restart_threshold_s", 0.5, 8.0, 0.5,
            lambda v: f"{v:.1f} s",
        )
        self.lookahead = self._slider(
            column, "Sentences prepared ahead",
            "How much is made ready in the background. Higher is smoother but "
            "uses more of the processor.",
            "playback", "lookahead_sentences", 1, 8, 1, lambda v: f"{int(v)}",
        )

    def _page_selection(self, column: QVBoxLayout) -> None:
        self.mode_box = self._combo(
            column, "When to show the Read button",
            "The small button that appears beside text you have selected.",
            [
                ("Whenever I select text", "aggressive"),
                ("Only when it is certain text is selected", "uia_only"),
                ("Only while I hold the Ctrl key", "modifier"),
                ("Never — I will use the tray menu", "off"),
            ],
            "selection", "mode",
        )
        self._divider(column)
        self.dbl_check = self._check(
            column, "Show it when I double-click a word",
            "", "selection", "enable_double_click",
        )
        self.trip_check = self._check(
            column, "Show it when I triple-click a line",
            "", "selection", "enable_triple_click",
        )
        self._divider(column)
        self.hide_slider = self._slider(
            column, "Hide the button after",
            "How long the Read button waits before disappearing.",
            "selection", "pill_auto_hide_ms", 1500, 12000, 500,
            lambda v: f"{v / 1000:.1f} s",
        )

    def _page_appearance(self, column: QVBoxLayout) -> None:
        self.font_slider = self._slider(
            column, "Text size",
            "The size of the text in the reading window.",
            "ui", "panel_font_pt", 10, 28, 1, lambda v: f"{int(v)} pt",
        )
        self.spacing_slider = self._slider(
            column, "Space between lines",
            "More space between lines makes it easier to keep your place.",
            "ui", "panel_line_spacing", 1.0, 2.5, 0.1, lambda v: f"{v:.1f} ×",
        )
        self._divider(column)

        holder = self._row(column, "Preview", "")
        self.preview = QLabel(
            "The quick brown fox jumps over the lazy dog.\n"
            "This is how your reading text will look."
        )
        self.preview.setWordWrap(True)
        self.preview.setStyleSheet(
            f"background: {THEME.header_bg}; color: {THEME.text};"
            f"border: 1px solid {THEME.panel_border}; border-radius: 8px;"
            f"padding: 14px;"
        )
        holder.addWidget(self.preview)

        self._divider(column)
        self.show_panel_check = self._check(
            column, "Open the reading window when reading starts",
            "Turn this off if you only want to listen.",
            "ui", "show_panel_on_read",
        )

    def _page_startup(self, column: QVBoxLayout) -> None:
        self.autostart_check = self._check(
            column, "Start automatically when I sign in to Windows",
            "The app waits quietly in the tray until you select some text.",
            "app", "autostart",
        )
        self.notify_check = self._check(
            column, "Show a notification when it is ready",
            "Useful because the tray icon can be hidden behind the ^ arrow.",
            "app", "notify_on_ready",
        )
        self._divider(column)
        self.hotkey_check = self._check(
            column, "Allow Ctrl + Alt + Esc to stop reading",
            "An emergency stop that works even when another window is in front.",
            "app", "stop_hotkey_enabled",
        )

    def _page_advanced(self, column: QVBoxLayout) -> None:
        devices = [("System default", "")]
        try:
            import sounddevice as sd

            for index, dev in enumerate(sd.query_devices()):
                if dev.get("max_output_channels", 0) > 0:
                    devices.append((dev["name"], dev["name"]))
        except Exception:
            log.debug("could not list audio devices", exc_info=True)
        self.device_box = self._combo(
            column, "Sound output", "Where the reading is played.",
            devices, "audio", "device",
        )
        self._divider(column)
        self.threads_slider = self._slider(
            column, "Processor threads",
            "More can be faster, but too many can make the audio stutter. "
            "4 is a safe choice.",
            "engine", "torch_threads", 1, 8, 1, lambda v: f"{int(v)}",
        )
        self._divider(column)

        holder = self._row(
            column, "Settings file",
            "Everything above is stored in a plain text file. You can edit it "
            "directly if you prefer.",
        )
        row = QHBoxLayout()
        btn_raw = QPushButton("Edit settings file…")
        btn_raw.clicked.connect(self.open_raw_editor)
        row.addWidget(btn_raw)
        btn_folder = QPushButton("Open its folder")
        btn_folder.clicked.connect(self._open_folder)
        row.addWidget(btn_folder)
        row.addStretch(1)
        holder.addLayout(row)

        path = QLabel(str(paths.settings_file()))
        path.setObjectName("settingHelp")
        path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path.setWordWrap(True)
        column.addWidget(path)

        self._divider(column)
        holder = self._row(
            column, "Start over",
            "Put every setting on this page back the way it came.",
        )
        btn_reset = QPushButton("Restore all defaults")
        btn_reset.clicked.connect(self._restore_defaults)
        holder.addWidget(btn_reset, 0, Qt.AlignmentFlag.AlignLeft)

    # ------------------------------------------------------------- values

    def _load_values(self) -> None:
        """Populate every control from the current config."""
        self._loading = True
        try:
            _select_data(self.voice_box, self.cfg.get("engine", "voice"))
            _select_data(self.mode_box, self.cfg.get("selection", "mode"))
            _select_data(self.device_box, self.cfg.get("audio", "device") or "")

            for slider, section, key in (
                (self.speed_slider, "engine", "speed"),
                (self.volume_slider, "audio", "volume"),
                (self.sentence_pause, "audio", "trailing_pause_s"),
                (self.paragraph_pause, "audio", "paragraph_pause_s"),
                (self.restart_threshold, "playback", "prev_restart_threshold_s"),
                (self.lookahead, "playback", "lookahead_sentences"),
                (self.hide_slider, "selection", "pill_auto_hide_ms"),
                (self.font_slider, "ui", "panel_font_pt"),
                (self.spacing_slider, "ui", "panel_line_spacing"),
                (self.threads_slider, "engine", "torch_threads"),
            ):
                raw = slider._to_raw(float(self.cfg.get(section, key)))
                slider.setValue(max(slider.minimum(), min(raw, slider.maximum())))
                slider._label.setText(
                    slider._fmt(float(self.cfg.get(section, key)))
                )

            self.dbl_check.setChecked(
                bool(self.cfg.get("selection", "enable_double_click"))
            )
            self.trip_check.setChecked(
                bool(self.cfg.get("selection", "enable_triple_click"))
            )
            self.show_panel_check.setChecked(
                bool(self.cfg.get("ui", "show_panel_on_read"))
            )
            self.notify_check.setChecked(
                bool(self.cfg.get("app", "notify_on_ready"))
            )
            self.hotkey_check.setChecked(
                bool(self.cfg.get("app", "stop_hotkey_enabled"))
            )
            from ..win import autostart

            self.autostart_check.setChecked(autostart.is_enabled())
        finally:
            self._loading = False
        self._update_preview()

    def _set(self, section: str, key: str, value) -> None:
        if self._loading:
            return
        if key in ("panel_font_pt", "lookahead_sentences", "torch_threads",
                   "pill_auto_hide_ms"):
            value = int(round(float(value)))
        self.cfg.set(section, key, value)
        self._update_preview()
        self._save_timer.start()

    def _commit(self) -> None:
        if configmod.save(self.cfg):
            self.saved_flag.setText("Saved")
            QTimer.singleShot(1600, lambda: self.saved_flag.setText(""))
            self.applied.emit()
        else:
            self.saved_flag.setText("Could not save")

    def _update_preview(self) -> None:
        if not hasattr(self, "preview"):
            return
        font = QFont()
        font.setStyleHint(QFont.StyleHint.SansSerif)
        font.setPointSize(int(self.cfg.get("ui", "panel_font_pt")))
        self.preview.setFont(font)
        spacing = float(self.cfg.get("ui", "panel_line_spacing"))
        self.preview.setMinimumHeight(int(font.pointSize() * spacing * 4.4))

    def _restore_defaults(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        confirm = QMessageBox.question(
            self, "Restore defaults",
            "Put every setting back the way it came?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self.cfg = configmod.Config()
        configmod.save(self.cfg)
        self._load_values()
        self.applied.emit()
        self.saved_flag.setText("Defaults restored")
        QTimer.singleShot(2000, lambda: self.saved_flag.setText(""))

    def _open_folder(self) -> None:
        from ..win import shell

        paths.ensure_dirs()
        shell.reveal_in_explorer(paths.config_dir())

    # ---------------------------------------------------------------- show

    def show_focused(self) -> None:
        self.cfg = configmod.load()
        self._load_values()
        self.show()
        self.raise_()
        self.activateWindow()


def _pretty_voice(code: str) -> str:
    """'af_heart' -> 'Heart (American, female)'."""
    accents = {"a": "American", "b": "British"}
    genders = {"f": "female", "m": "male"}
    name = code.split("_", 1)[-1].replace("_", " ").title()
    if len(code) > 2 and code[1] in genders and code[0] in accents:
        return f"{name} ({accents[code[0]]}, {genders[code[1]]})"
    return name


def _select_data(box: QComboBox, value) -> None:
    index = box.findData(value)
    box.setCurrentIndex(index if index >= 0 else 0)
