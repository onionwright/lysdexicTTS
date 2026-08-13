"""Colors and stylesheet for the floating windows."""

from __future__ import annotations

from dataclasses import dataclass


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


PANEL_QSS = f"""
QWidget#panelRoot {{
    background: transparent;
}}
QTextEdit#readerText {{
    background: {THEME.panel_bg};
    color: {THEME.text};
    border: none;
    padding: 14px 16px;
    selection-background-color: {THEME.speaking_bg};
}}
QLabel#titleLabel {{
    color: {THEME.text};
    font-weight: 600;
}}
QLabel#statusLabel {{
    color: {THEME.text_dim};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {THEME.button_down};
    border-radius: 5px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {THEME.text_dim};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    height: 0px;
    background: none;
}}
"""
