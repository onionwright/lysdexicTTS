"""Reading colours, and the arithmetic that keeps them readable.

Two colours are chosen by the user -- the paper behind the reading text and the
highlight on the sentence being spoken -- and everything else is derived from
them here: body text, spoken text, the header strip, the panel edge, and the
muted wash over captured text. Deriving rather than asking is the whole design.
Four colour pickers is four chances to build something unreadable, and the
person most likely to be hurt by that is exactly the person this app is for.

Two palettes are offered, and neither is arbitrary:

* **Paper** is the pale tint set from coloured-overlay practice. Tinted paper is
  one of the few interventions with real evidence behind it for readers who
  find black-on-white unstable, and *which* tint helps is individual -- there is
  no correct one to ship, only a spread to choose from.
* **Highlight** is the Okabe-Ito palette, designed to stay distinguishable under
  deuteranopia, protanopia and tritanopia, plus the app's original blue.

No Qt import: this is arithmetic, and it should be testable without a display.

Contrast follows WCAG 2.1. ``BODY_LIGHT``/``BODY_DARK`` are deliberately pulled
back off pure white and pure black -- maximum contrast is its own source of
visual stress, and the panel has always used off-white on dark grey for that
reason. The highlight is the one place that keeps full strength, because it is
one sentence rather than a page of it, and it has to win against the paper.
"""

from __future__ import annotations

from typing import List, Tuple

# Text placed on a highlight: full strength, because a highlight has to read as
# a distinct band and it is never more than a sentence long.
INK_DARK = "#111318"
INK_LIGHT = "#ffffff"

# How far body text is pulled back toward its background. Enough to take the
# glare off a full page, not enough to soften the letterforms.
BODY_SOFTEN = 0.12

# WCAG AA for normal text. Used as an advisory threshold, never as a veto.
MIN_CONTRAST = 4.5

Swatch = Tuple[str, str]  # (plain-language name, hex)

# Pale tints for the page. "Dark" is the app's original panel colour, kept first
# among equals so the familiar look is always one click away.
PAPER: List[Swatch] = [
    ("Dark", "#23262e"),
    ("Cream", "#faf3e0"),
    ("Off-white", "#f4f3ee"),
    ("Peach", "#ffe8d6"),
    ("Rose", "#ffe3e8"),
    ("Pale yellow", "#fbf5c4"),
    ("Mint", "#dcf2e1"),
    ("Sky", "#dcecfa"),
    ("Lilac", "#e9e2f6"),
    ("Light grey", "#e6e7e9"),
]

# Okabe-Ito, plus the original. Named in plain words rather than by their usual
# labels ("vermillion", "reddish purple") because a colour name nobody uses is
# just another thing to decode.
HIGHLIGHT: List[Swatch] = [
    ("Deep blue", "#2f5aa8"),
    ("Blue", "#0072b2"),
    ("Sky blue", "#56b4e9"),
    ("Green", "#009e73"),
    ("Yellow", "#f0e442"),
    ("Orange", "#e69f00"),
    ("Red-orange", "#d55e00"),
    ("Pink-purple", "#cc79a7"),
    ("Black", "#000000"),
]


# ------------------------------------------------------------------- parsing


def _rgb(color: str) -> Tuple[int, int, int]:
    """Parse ``#rgb`` or ``#rrggbb``, with or without the hash."""
    value = str(color).strip().lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    if len(value) != 6:
        raise ValueError(f"not a colour: {color!r}")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _hex(r: float, g: float, b: float) -> str:
    clamp = lambda v: max(0, min(255, int(round(v))))  # noqa: E731
    return f"#{clamp(r):02x}{clamp(g):02x}{clamp(b):02x}"


def is_color(value: str) -> bool:
    """True if ``value`` parses. Used to fall back rather than crash on a
    hand-edited settings file."""
    try:
        _rgb(value)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ contrast


def relative_luminance(color: str) -> float:
    """WCAG 2.1 relative luminance, 0.0 (black) to 1.0 (white)."""
    out = []
    for channel in _rgb(color):
        c = channel / 255.0
        out.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast between two colours, 1.0 (identical) to 21.0."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def is_dark(color: str) -> bool:
    return relative_luminance(color) < 0.32


def mix(a: str, b: str, t: float) -> str:
    """Blend ``t`` of ``b`` into ``a``, straight sRGB."""
    t = max(0.0, min(1.0, float(t)))
    ra, ga, ba = _rgb(a)
    rb, gb, bb = _rgb(b)
    return _hex(ra + (rb - ra) * t, ga + (gb - ga) * t, ba + (bb - ba) * t)


def shade(color: str, amount: float) -> str:
    """Lighten (positive) or darken (negative) toward white or black."""
    return mix(color, INK_LIGHT if amount >= 0 else "#000000", abs(amount))


def readable_text_on(background: str) -> str:
    """Whichever of the two inks reads better on ``background``.

    Picked by measured contrast rather than by a luminance threshold, because
    the two inks are not symmetric about mid-grey and a threshold gets the
    awkward middle -- saturated orange, mid green -- wrong in exactly the cases
    that matter.
    """
    if contrast_ratio(background, INK_DARK) >= contrast_ratio(background, INK_LIGHT):
        return INK_DARK
    return INK_LIGHT


def body_text_on(background: str) -> str:
    """Reading text: the readable ink, eased back toward its background.

    A page of pure white on near-black is harsh enough to be its own reading
    problem, which is why the panel has always used off-white on dark grey.
    """
    return mix(readable_text_on(background), background, BODY_SOFTEN)


# ------------------------------------------------------------------ derived


class ReadingColors:
    """Every colour the panel needs, from the two the user actually chose."""

    __slots__ = (
        "highlight", "page", "spoken_text", "body_text",
        "header", "edge", "wash", "dim_text",
    )

    def __init__(self, highlight: str, page: str) -> None:
        if not is_color(highlight):
            highlight = HIGHLIGHT[0][1]
        if not is_color(page):
            page = PAPER[0][1]

        self.highlight = _hex(*_rgb(highlight))  # normalized
        self.page = _hex(*_rgb(page))
        self.spoken_text = readable_text_on(self.highlight)
        self.body_text = body_text_on(self.page)
        # The wash marks captured text, so it has to be *just* visible against
        # the paper and no more -- it covers everything being read.
        #
        # A straight blend toward the highlight will not do that. The step it
        # produces is proportional to the distance between paper and highlight,
        # which is small on dark paper and enormous on pale paper: a deep blue
        # highlight mixed into cream turns the whole reading area grey and
        # throws away the tint the user chose. So the size of the step is fixed
        # here, away from the paper's own luminance, and only a trace of the
        # highlight goes in on top to keep the two levels looking related.
        step = 0.07
        self.wash = mix(
            shade(self.page, step if is_dark(self.page) else -step),
            self.highlight,
            0.08,
        )
        self.header = shade(self.page, -0.18)
        # On a dark page an edge reads as a lighter line, on a light page as a
        # darker one. Same shade() in opposite directions.
        self.edge = shade(self.page, 0.22 if is_dark(self.page) else -0.15)
        self.dim_text = mix(self.body_text, self.page, 0.38)

    @property
    def highlight_contrast(self) -> float:
        """Contrast of the spoken sentence's text against its highlight."""
        return contrast_ratio(self.highlight, self.spoken_text)

    @property
    def body_contrast(self) -> float:
        return contrast_ratio(self.page, self.body_text)

    def is_hard_to_read(self) -> bool:
        """True when either level falls below WCAG AA.

        Advisory. The user's own eyes beat a formula, and someone who needs an
        unusual combination must still be able to have it.
        """
        return min(self.highlight_contrast, self.body_contrast) < MIN_CONTRAST
