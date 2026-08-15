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

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
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
from .palette import HIGHLIGHT, PAPER, ReadingColors
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


class _VoiceDownloader(QThread):
    """One voice fetch, off the GUI thread."""

    done = Signal(str, bool)

    def __init__(self, engine, name: str, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.name = name

    def run(self) -> None:
        ok = False
        try:
            ok = bool(self.engine.download_voice(self.name))
        except Exception:
            log.exception("voice download failed for %r", self.name)
        self.done.emit(self.name, ok)


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
        # (section, key) -> {hex: button}, so _load_values can restore which
        # swatch is ringed without the buttons having to know about config.
        self._swatch_buttons: dict = {}

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
        self._add_page("Read button", self._page_pill)
        self._add_page("Text Settings", self._page_appearance)
        self._add_page("Colours", self._page_colors)
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

    def _swatches(
        self, column: QVBoxLayout, name: str, help_text: str,
        group: List[tuple], section: str, key: str, per_row: int = 5,
    ) -> None:
        """A grid of colour swatches, each labelled in plain words.

        Deliberately not a colour wheel. A wheel puts an unreadable choice one
        drag away and asks the reader to judge contrast by eye -- which is the
        one judgement this app should be making for them, not delegating.
        """
        holder = self._row(column, name, help_text)
        grid = QGridLayout()
        grid.setContentsMargins(0, 4, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        buttons = {}
        for i, (label, value) in enumerate(group):
            cell = QWidget()
            stack = QVBoxLayout(cell)
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(4)

            button = QPushButton()
            button.setCheckable(True)
            button.setFixedSize(58, 42)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setToolTip(label)
            button.setStyleSheet(_swatch_qss(value))
            button.clicked.connect(
                lambda _checked=False, s=section, k=key, v=value:
                self._pick_swatch(s, k, v)
            )
            stack.addWidget(button, 0, Qt.AlignmentFlag.AlignHCenter)

            caption = QLabel(label)
            caption.setObjectName("settingHelp")
            caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            stack.addWidget(caption)

            grid.addWidget(cell, i // per_row, i % per_row)
            buttons[value] = button

        grid.setColumnStretch(per_row, 1)
        holder.addLayout(grid)
        self._swatch_buttons[(section, key)] = buttons

    def _pick_swatch(self, section: str, key: str, value: str) -> None:
        self._check_swatch(section, key, value)
        self._set(section, key, value)

    def _check_swatch(self, section: str, key: str, value: str) -> None:
        """Ring exactly one swatch. Not QButtonGroup's autoExclusive, because
        that cannot express 'none of these' when the settings file holds a
        colour that is not in the grid."""
        buttons = self._swatch_buttons.get((section, key), {})
        target = str(value).strip().lower()
        for swatch, button in buttons.items():
            button.setChecked(swatch == target)

    # --------------------------------------------------------------- pages

    def _page_voice(self, column: QVBoxLayout) -> None:
        self.voice_box = self._combo(
            column, "Voice", "The voice that reads to you.",
            [], "engine", "voice",
        )

        self._divider(column)

        holder = self._row(
            column, "Get another voice",
            "Voices are about half a megabyte each and download in a second or "
            "two. You need to be online just for the download.",
        )
        row = QHBoxLayout()
        self.add_box = QComboBox()
        row.addWidget(self.add_box)
        self.btn_get = QPushButton("Download")
        self.btn_get.clicked.connect(self._download_voice)
        row.addWidget(self.btn_get)
        row.addStretch(1)
        holder.addLayout(row)

        self.voice_status = QLabel("")
        self.voice_status.setObjectName("settingHelp")
        self.voice_status.setWordWrap(True)
        column.addWidget(self.voice_status)

        self._refresh_voice_lists()
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

        self._divider(column)
        self.keepalive_check = self._check(
            column, "Keep the sound connection open",
            "For hearing aids, Bluetooth headphones and anything with noise "
            "cancelling. Without this, the silence between sentences can read "
            "as 'no sound at all' and make the device switch its processing "
            "off and on between every sentence. This holds the connection open "
            "with a sound too quiet to hear.",
            "audio", "keep_audio_alive",
        )
        self.keepalive_slider = self._slider(
            column, "How quiet that sound is",
            "Lower is quieter. If your device still cuts out between "
            "sentences, move this up a little until it stops. Around -70 works "
            "for most hearing aids.",
            "audio", "keep_alive_db", -90.0, -40.0, 5.0, _describe_db,
        )
        self.keepalive_color = self._combo(
            column, "What that sound is like",
            "If it sounds harsh or electrical, try a softer one.",
            [
                ("Deep rumble — softest, hardest to notice", "brown"),
                ("Steady rain — soft and even", "pink"),
                ("Hiss — bright, like radio static", "white"),
            ],
            "audio", "keep_alive_color",
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
        # This page is about *noticing* a selection. Everything about the
        # button itself lives on the Read button page.
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

    def _page_pill(self, column: QVBoxLayout) -> None:
        self.anchor_box = self._combo(
            column, "Where it appears",
            "Some apps cannot tell us exactly where your selected text is; "
            "when that happens the first choice falls back to where you "
            "finished selecting.",
            [
                ("Beside the text I selected", "selection"),
                ("Where I started selecting", "selection_start"),
                ("Where I finished selecting", "selection_end"),
                ("Wherever the mouse pointer is", "mouse"),
                ("Always in the corner, by the clock", "corner"),
            ],
            "pill", "anchor",
        )
        self.above_check = self._check(
            column, "Put it above rather than below",
            "", "pill", "above",
        )
        self.offset_x = self._slider(
            column, "Nudge it sideways", "",
            "pill", "offset_x", -60, 60, 4, lambda v: f"{int(v)} px",
        )
        self.offset_y = self._slider(
            column, "Nudge it up or down", "",
            "pill", "offset_y", -60, 60, 4, lambda v: f"{int(v)} px",
        )

        self._divider(column)
        self.autohide_check = self._check(
            column, "Hide it on its own after a while",
            "Turn this off and it stays until you dismiss it another way.",
            "pill", "auto_hide_enabled",
        )
        self.hide_slider = self._slider(
            column, "Hide it after",
            "How long the Read button waits before disappearing.",
            "pill", "auto_hide_ms", 1500, 12000, 500,
            lambda v: f"{v / 1000:.1f} s",
        )
        self.clickaway_check = self._check(
            column, "Hide it when I click somewhere else",
            "Clicking the button itself never counts.",
            "pill", "hide_on_click_away",
        )
        self.pointer_check = self._check(
            column, "Hide it when I move the mouse away",
            "", "pill", "hide_when_pointer_away",
        )
        self.pointer_slider = self._slider(
            column, "How far away counts",
            "Measured from the button, and from where your pointer was when it "
            "appeared — so a button that opens away from your pointer does not "
            "vanish before you have moved, and moving over to press it never "
            "dismisses it.",
            "pill", "pointer_distance_px", 60, 600, 20, lambda v: f"{int(v)} px",
        )

        self._divider(column)
        self.pill_font = self._slider(
            column, "Button text size",
            "The Read button does not have to be small just because it floats.",
            "pill", "font_pt", 8, 22, 1, lambda v: f"{int(v)} pt",
        )
        self.copy_check = self._check(
            column, "Offer Copy as well as Read",
            "Turn this off for a smaller button with one thing on it.",
            "pill", "show_copy",
        )

        # The two sliders only mean anything when their rule is switched on.
        self.autohide_check.toggled.connect(self.hide_slider.setEnabled)
        self.pointer_check.toggled.connect(self.pointer_slider.setEnabled)

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

    def _page_colors(self, column: QVBoxLayout) -> None:
        self._swatches(
            column, "Paper colour",
            "The background behind the reading text. Tinted paper helps many "
            "people who find black on white unsteady to look at — which colour "
            "helps is personal, so try a few.",
            PAPER, "colors", "page_tint",
        )
        self._divider(column)
        self._swatches(
            column, "Highlight colour",
            "The sentence being read aloud. These stay easy to tell apart if "
            "you are colour blind.",
            HIGHLIGHT, "colors", "highlight",
        )

        self._divider(column)
        holder = self._row(column, "Preview", "")
        self.color_preview = QLabel()
        self.color_preview.setWordWrap(True)
        self.color_preview.setTextFormat(Qt.TextFormat.RichText)
        self.color_preview.setMinimumHeight(96)
        holder.addWidget(self.color_preview)

        # Always present rather than only on failure. Nothing reachable from
        # the grids above is unreadable, so a warning that only appeared on
        # trouble would never appear at all -- and would then be untrustworthy
        # on the day a hand-edited settings file did make trouble.
        self.contrast_label = QLabel("")
        self.contrast_label.setObjectName("settingHelp")
        self.contrast_label.setWordWrap(True)
        column.addWidget(self.contrast_label)

    def _page_startup(self, column: QVBoxLayout) -> None:
        # No "start with Windows" control here. Writing the HKCU Run value is
        # easy, but Explorer did not act on it at three consecutive logons on
        # the development machine while every other enabled entry in the same
        # key launched normally -- and the app could not tell, because a
        # successful registry write is not evidence that anything starts.
        # A Startup-folder shortcut does work; see the README.
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

    # -------------------------------------------------------------- voices

    def _refresh_voice_lists(self) -> None:
        """Rebuild both voice dropdowns from what is actually on disk."""
        installed, more = ["af_heart"], []
        if self.engine is not None:
            try:
                installed = self.engine.available_voices() or installed
                more = self.engine.downloadable_voices()
            except Exception:
                log.debug("could not list voices", exc_info=True)

        was_loading, self._loading = self._loading, True
        try:
            current = self.cfg.get("engine", "voice")
            self.voice_box.clear()
            for name in installed:
                self.voice_box.addItem(_pretty_voice(name), name)
            _select_data(self.voice_box, current)

            self.add_box.clear()
            for name in more:
                self.add_box.addItem(_pretty_voice(name), name)
        finally:
            self._loading = was_loading

        self.add_box.setEnabled(bool(more))
        self.btn_get.setEnabled(bool(more))
        if not more:
            self.add_box.addItem("All voices installed", None)
            self.voice_status.setText(
                f"All {len(installed)} voices are installed."
            )
        elif not self.voice_status.text():
            self.voice_status.setText(
                f"{len(installed)} installed, {len(more)} more available."
            )

    def _download_voice(self) -> None:
        name = self.add_box.currentData()
        if not name or self.engine is None:
            return
        self.btn_get.setEnabled(False)
        self.add_box.setEnabled(False)
        self.voice_status.setText(f"Downloading {_pretty_voice(name)}…")

        # Off the GUI thread: this is a network call.
        self._downloader = _VoiceDownloader(self.engine, name, self)
        self._downloader.done.connect(self._on_voice_downloaded)
        self._downloader.start()

    def _on_voice_downloaded(self, name: str, ok: bool) -> None:
        if ok:
            self.voice_status.setText(
                f"{_pretty_voice(name)} is ready, and is now selected."
            )
            self.cfg.set("engine", "voice", name)
            self._refresh_voice_lists()
            _select_data(self.voice_box, name)
            self._save_timer.start()
        else:
            self.voice_status.setText(
                f"Could not download {_pretty_voice(name)}. "
                "Check your internet connection and try again."
            )
            self._refresh_voice_lists()
        self.btn_get.setEnabled(True)
        self.add_box.setEnabled(True)

    # ------------------------------------------------------------- values

    def _load_values(self) -> None:
        """Populate every control from the current config."""
        self._loading = True
        try:
            _select_data(self.mode_box, self.cfg.get("selection", "mode"))
            _select_data(self.anchor_box, self.cfg.get("pill", "anchor"))
            _select_data(
                self.keepalive_color, self.cfg.get("audio", "keep_alive_color")
            )
            _select_data(self.device_box, self.cfg.get("audio", "device") or "")

            for section, key in (("colors", "page_tint"), ("colors", "highlight")):
                self._check_swatch(section, key, str(self.cfg.get(section, key)))

            for slider, section, key in (
                (self.speed_slider, "engine", "speed"),
                (self.volume_slider, "audio", "volume"),
                (self.sentence_pause, "audio", "trailing_pause_s"),
                (self.paragraph_pause, "audio", "paragraph_pause_s"),
                (self.restart_threshold, "playback", "prev_restart_threshold_s"),
                (self.lookahead, "playback", "lookahead_sentences"),
                (self.hide_slider, "pill", "auto_hide_ms"),
                (self.offset_x, "pill", "offset_x"),
                (self.offset_y, "pill", "offset_y"),
                (self.pointer_slider, "pill", "pointer_distance_px"),
                (self.pill_font, "pill", "font_pt"),
                (self.keepalive_slider, "audio", "keep_alive_db"),
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
            self.keepalive_check.setChecked(
                bool(self.cfg.get("audio", "keep_audio_alive"))
            )
            self.notify_check.setChecked(
                bool(self.cfg.get("app", "notify_on_ready"))
            )
            self.hotkey_check.setChecked(
                bool(self.cfg.get("app", "stop_hotkey_enabled"))
            )

            for check, key in (
                (self.above_check, "above"),
                (self.autohide_check, "auto_hide_enabled"),
                (self.clickaway_check, "hide_on_click_away"),
                (self.pointer_check, "hide_when_pointer_away"),
                (self.copy_check, "show_copy"),
            ):
                check.setChecked(bool(self.cfg.get("pill", key)))
            # toggled() does not fire while _loading suppresses writes, so the
            # dependent sliders are greyed here rather than relying on the
            # signal that keeps them in step afterwards.
            self.hide_slider.setEnabled(self.autohide_check.isChecked())
            self.pointer_slider.setEnabled(self.pointer_check.isChecked())
        finally:
            self._loading = False
        # Rebuilds the dropdowns from disk, so a voice downloaded elsewhere
        # (or on first run) shows up without a restart.
        self._refresh_voice_lists()
        self._update_preview()

    def _set(self, section: str, key: str, value) -> None:
        if self._loading:
            return
        if key in ("panel_font_pt", "lookahead_sentences", "torch_threads",
                   "auto_hide_ms", "offset_x", "offset_y",
                   "pointer_distance_px", "font_pt"):
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
        self._update_color_preview()

    def _update_color_preview(self) -> None:
        """Show the paper and the highlight together, at the size they will
        actually be read at. Judging either one on its own is the mistake this
        preview exists to prevent."""
        if not hasattr(self, "color_preview"):
            return
        colors = ReadingColors(
            str(self.cfg.get("colors", "highlight")),
            str(self.cfg.get("colors", "page_tint")),
        )
        font_pt = int(self.cfg.get("ui", "panel_font_pt"))
        spacing = float(self.cfg.get("ui", "panel_line_spacing"))

        self.color_preview.setStyleSheet(
            f"background: {colors.page};"
            f"border: 1px solid {colors.edge}; border-radius: 8px;"
            f"padding: 14px; font-size: {font_pt}pt;"
            f"line-height: {int(spacing * 100)}%;"
        )
        self.color_preview.setText(
            f'<span style="color:{colors.body_text}">'
            "This is what your reading text will look like. </span>"
            f'<span style="background-color:{colors.highlight};'
            f'color:{colors.spoken_text}">'
            "And this is the sentence being read aloud.</span>"
            f'<span style="color:{colors.body_text}"> '
            "The rest carries on afterwards.</span>"
        )

        self.contrast_label.setText(
            f"Highlight: {_describe_contrast(colors.highlight_contrast)}. "
            f"Reading text: {_describe_contrast(colors.body_contrast)}."
        )

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


def _swatch_qss(color: str) -> str:
    """One swatch button. Padding and min-width are reset explicitly because
    WINDOW_QSS styles every QPushButton in this window as a text button."""
    return f"""
QPushButton {{
    background: {color};
    border: 2px solid {THEME.panel_border};
    border-radius: 8px;
    padding: 0px;
    min-width: 0px;
}}
QPushButton:hover {{ border-color: {THEME.text}; }}
QPushButton:checked {{ border: 3px solid {THEME.accent}; }}
"""


def _describe_contrast(ratio: float) -> str:
    """A contrast ratio in words. '4.8:1' is not a thing anyone should have to
    interpret to choose a colour."""
    if ratio >= 7.0:
        word = "easy to read"
    elif ratio >= 4.5:
        word = "clear"
    elif ratio >= 3.0:
        word = "faint — may be hard work"
    else:
        word = "too faint to read comfortably"
    return f"{word} ({ratio:.1f}:1)"


def _describe_db(value: float) -> str:
    """dBFS with a plain-language hint, since 'dB' means nothing to most people."""
    if value <= -80:
        word = "silent"
    elif value <= -65:
        word = "inaudible"
    elif value <= -55:
        word = "very faint"
    else:
        word = "faint"
    return f"{int(value)} dB · {word}"


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
