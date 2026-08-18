"""Pure WCAG contrast Calculations over RESOLVED colour values (no DOM, no I/O).

Mirrors S1's stdlib formula (tests/test_theme_tokens.py: channel/255, threshold 0.03928, coefficients
0.2126/0.7152/0.0722, ratio (hi+0.05)/(lo+0.05)), so a pair's computed ratio equals S1's recorded value.
The one difference from S1 is the INPUT: these functions consume the strings getComputedStyle returns
POST-CASCADE, so a cascade override changes the input and therefore the verdict. Reaching the DOM is
computed_style.py's job; nothing here touches a browser, which is what makes the negative control real.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# getComputedStyle resolves colours to rgb()/rgba() in Chromium, with commas or spaces and an optional
# alpha after a comma or a slash. Only that family is accepted; anything else raises (see parse below).
_RGB_RE = re.compile(
    r"rgba?\(\s*([\d.]+)\s*[,\s]\s*([\d.]+)\s*[,\s]\s*([\d.]+)\s*(?:[,/]\s*([\d.]+%?)\s*)?\)"
)


@dataclass(frozen=True)
class Rgb:
    """An sRGB colour: channels in 0..255, alpha in 0..1."""

    r: float
    g: float
    b: float
    a: float = 1.0


def parse_computed_color(value: str) -> Rgb:
    """Parse a computed 'rgb(...)'/'rgba(...)' string into an Rgb.

    Raises on anything unparseable, because a helper that silently swallows an unreadable colour cannot be
    trusted to report a violation (the whole point of the negative control).
    """
    match = _RGB_RE.search(value.strip())
    if match is None:
        raise ValueError(f"unparseable computed colour: {value!r}")
    r, g, b = (float(match.group(i)) for i in (1, 2, 3))
    alpha_raw = match.group(4)
    if alpha_raw is None:
        alpha = 1.0
    elif alpha_raw.endswith("%"):
        alpha = float(alpha_raw[:-1]) / 100.0
    else:
        alpha = float(alpha_raw)
    return Rgb(r, g, b, alpha)


def _linearize(channel: float) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: Rgb) -> float:
    """WCAG relative luminance of an OPAQUE colour (composite any alpha first)."""
    return 0.2126 * _linearize(colour.r) + 0.7152 * _linearize(colour.g) + 0.0722 * _linearize(colour.b)


def composite_over(top: Rgb, bottom: Rgb) -> Rgb:
    """Alpha-composite `top` over an opaque `bottom`; returns an opaque colour."""
    a = top.a
    return Rgb(
        top.r * a + bottom.r * (1.0 - a),
        top.g * a + bottom.g * (1.0 - a),
        top.b * a + bottom.b * (1.0 - a),
        1.0,
    )


def contrast_ratio(one: Rgb, two: Rgb) -> float:
    """WCAG contrast ratio between two OPAQUE colours."""
    lum_one, lum_two = relative_luminance(one), relative_luminance(two)
    hi, lo = max(lum_one, lum_two), min(lum_one, lum_two)
    return (hi + 0.05) / (lo + 0.05)


# Floors — contract §6, the numbers a script asserts.
FLOOR_BODY = 4.5
FLOOR_LARGE = 3.0
FLOOR_BOUNDARY = 3.0
FLOOR_FOCUS = 3.0
FOCUS_MIN_PX = 2.0


@dataclass(frozen=True)
class Verdict:
    """The result of judging one colour pair against one floor."""

    name: str
    ratio: float
    floor: float
    ok: bool


def check_ratio(name: str, foreground: Rgb, background: Rgb, floor: float) -> Verdict:
    """Composite a translucent `foreground` over `background`, compute the ratio, and judge it. Pure."""
    fg = foreground if foreground.a >= 1.0 else composite_over(foreground, background)
    ratio = contrast_ratio(fg, background)
    return Verdict(name=name, ratio=round(ratio, 4), floor=floor, ok=ratio >= floor)
