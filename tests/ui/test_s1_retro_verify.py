"""S1 retro-verification: assert the §6 floors against POST-CASCADE computed styles in a real browser —
the cascade overrides and the :focus-visible state S1's text parser structurally cannot see (FR-09).

Floor 6 (no hex literal outside the token block) is a SOURCE property, not a rendered one, and remains
S1's static test's job (tests/test_theme_tokens.py). Floors 1-5 and the focus indicator are re-asserted
here on what the browser actually paints. Any violation found here is a DEFECT REPORT against S1
(docs/session_2/s1_defect_report.md), never a fix inside S2.
"""
from __future__ import annotations

from computed_style import (
    assert_rendered,
    effective_background,
    focus_via_keyboard,
    length_px,
    resolved_color,
)
from contrast import (
    FLOOR_BODY,
    FLOOR_BOUNDARY,
    FLOOR_FOCUS,
    FLOOR_LARGE,
    FOCUS_MIN_PX,
    contrast_ratio,
)

# --accent-ink (D-09), the token the focus ring must resolve to.
_ACCENT_INK = (0x2F, 0x6F, 0x5B)


def test_body_text_floor_post_cascade(page, base_url):
    page.goto(base_url)
    assert_rendered(page)
    fg = resolved_color(page, ".hint", "color")  # --ink-muted, normal size
    bg = effective_background(page, ".hint")
    ratio = contrast_ratio(fg, bg)
    assert ratio >= FLOOR_BODY, f"body text {ratio:.2f}:1 < {FLOOR_BODY}:1 post-cascade"


def test_large_text_floor_post_cascade(page, base_url):
    page.goto(base_url)
    assert_rendered(page)
    fg = resolved_color(page, ".brand", "color")  # --accent-ink, 1.5rem => large text
    bg = effective_background(page, ".brand")
    ratio = contrast_ratio(fg, bg)
    assert ratio >= FLOOR_LARGE, f"large text {ratio:.2f}:1 < {FLOOR_LARGE}:1 post-cascade"


def test_control_boundary_floor_post_cascade(page, base_url):
    page.goto(base_url)
    assert_rendered(page)
    border = resolved_color(page, "#input", "border-top-color")  # --border-control
    surface = effective_background(page, ".panel")  # the surface the control sits on
    ratio = contrast_ratio(border, surface)
    assert ratio >= FLOOR_BOUNDARY, f"control boundary {ratio:.2f}:1 < {FLOOR_BOUNDARY}:1 post-cascade"


def test_focus_indicator_on_focused_control(page, base_url):
    """The focus ring, confirmed on a KEYBOARD-focused mint primary button (S1 handed this forward).

    S1's design puts the ring at --accent-ink with outline-offset:2px BECAUSE accent-ink vs the mint
    --accent-fill is only ~2.33:1 (style.css:71-74; test_theme_tokens.py:228 asserts that < 3). The offset
    lifts the ring onto the surrounding SURFACE, where accent-ink clears >= 3.93:1, so the operative floor
    is ring-vs-surface. This test asserts that established floor post-cascade — width, colour, a >= 2px
    offset, and >= 3:1 vs the surface — and records the fill contrast rather than asserting a floor the
    accepted design deliberately does not rely on. It does not loosen a floor and does not re-open S1.
    """
    page.goto(base_url)
    assert_rendered(page)
    focus_via_keyboard(page, "generate")  # keyboard focus => :focus-visible matches
    assert length_px(page, "#generate", "outline-width") >= FOCUS_MIN_PX, "focus ring thinner than 2px"
    assert length_px(page, "#generate", "outline-offset") >= FOCUS_MIN_PX, "ring not offset onto surface"
    ring = resolved_color(page, "#generate", "outline-color")
    assert (round(ring.r), round(ring.g), round(ring.b)) == _ACCENT_INK, "focus ring is not --accent-ink"
    surface = effective_background(page, ".panel")
    ring_vs_surface = contrast_ratio(ring, surface)
    assert ring_vs_surface >= FLOOR_FOCUS, f"focus ring {ring_vs_surface:.2f}:1 < 3:1 vs the surface"
    # The ring is NOT asserted against the mint --accent-fill: the >= 2px offset paints it on the surface,
    # not the fill (accent-ink vs accent-fill is ~2.33:1 — S1's accepted design, style.css:71-74, recorded
    # in checks.md). The operative floor for an offset ring is ring-vs-surface, asserted above. A `< 3`
    # assertion here would wrongly FAIL if a later S1 change improved ring-vs-fill contrast, so it is
    # deliberately absent (sharded review, correctness axis).


def test_no_state_conveyed_by_hue_alone(page, base_url):
    page.goto(base_url)
    assert_rendered(page)
    states = [
        "idle", "queued", "warming", "unavailable", "generating",
        "succeeded", "failed", "cancelled", "cancelling",
    ]
    glyphs = {}
    for state in states:
        glyph = page.locator("#status").first.evaluate(
            "(el, s) => { el.setAttribute('data-state', s); "
            "return getComputedStyle(el, '::before').getPropertyValue('content'); }",
            state,
        )
        assert glyph and glyph not in ("none", "normal", '""'), (
            f"state {state!r} has no non-hue ::before glyph (got {glyph!r})"
        )
        glyphs[state] = glyph
    # Presence is not enough: several states share a hue (idle/queued/cancelled/cancelling use --ink-muted;
    # unavailable/failed use --state-warn, style.css:168-185), so the glyph is the sole distinguisher and
    # MUST be unique per state — otherwise two states would be indistinguishable without colour (floor 5).
    assert len(set(glyphs.values())) == len(states), f"states share a ::before glyph (hue-alone): {glyphs}"
