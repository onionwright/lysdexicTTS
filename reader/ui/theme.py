"""Colors and stylesheet for the floating windows.

``THEME`` is the fixed palette for the chrome -- the tray, the settings window,
the pill -- and it is also the *default* reading palette. The reading colours
themselves are no longer fixed: the panel takes a paper colour and a highlight
from settings and derives the rest through :mod:`reader.ui.palette`, so the
reader panel's stylesheet has to be built per instance rather than baked at
import. That is what :func:`panel_qss` is for.
"""

from __future__ import annotations

from dataclasses import dataclass

from .palette import ReadingColors


@dataclass(frozen=True)
class Theme:
    # Surfaces
    panel_bg: str = "#23262e"
    panel_border: str = "#343945"
    header_bg: str = "#1b1e24"

    # Text
    text: str = "#d7dce5"
    text_dim: str = "#8b93a1"

    # The two highlight levels the reader needs: a muted wash over everything
    # the user selected, and a distinct accent on the sentence being spoken.
    captured_bg: str = "#2b303a"
    speaking_bg: str = "#2f5aa8"
    speaking_fg: str = "#ffffff"

    accent: str = "#4C8DFF"
    danger: str = "#e5534b"
    button_hover: str = "#2f3540"
    button_down: str = "#3a4150"

    radius: int = 10


THEME = Theme()

# The reading palette the app ships with, and what "Restore all defaults" and a
# missing settings file both fall back to.
DEFAULT_COLORS = ReadingColors(THEME.speaking_bg, THEME.panel_bg)


def panel_qss(colors: ReadingColors) -> str:
    """Reader-panel stylesheet for one choice of reading colours.

    A function rather than a constant because the paper colour reaches
    everything: on cream paper the header text has to darken, or it disappears
    into its own background.
    """
    return f"""
QWidget#panelRoot {{
    background: transparent;
}}
QTextEdit#readerText {{
    background: {colors.page};
    color: {colors.body_text};
    border: none;
    padding: 14px 16px;
    selection-background-color: {colors.highlight};
    selection-color: {colors.spoken_text};
}}
QLabel#titleLabel {{
    color: {colors.body_text};
    font-weight: 600;
}}
QLabel#statusLabel {{
    color: {colors.dim_text};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {colors.edge};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {colors.dim_text};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0px;
    background: none;
}}
"""
