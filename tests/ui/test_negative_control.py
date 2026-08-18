"""C2-7 negative control: prove the contrast helper can FAIL. A helper that cannot be made to fail is not
a check. Because the helper reads the RESOLVED value (getComputedStyle), an injected override changes the
input and flips the verdict — a declaration-reading helper (S1's blind spot, adversarial case #4) would
not change its verdict at all.
"""
from __future__ import annotations

from computed_style import assert_rendered, effective_background, resolved_color
from contrast import FLOOR_BODY, check_ratio, contrast_ratio


def test_contrast_helper_reports_injected_violation(page, base_url):
    page.goto(base_url)
    assert_rendered(page)
    selector = ".hint"

    # Un-overridden: the hint text passes its floor, so the flip below is caused by the override, not by
    # construction (negative-control.md, scenario 2).
    fg0 = resolved_color(page, selector, "color")
    bg0 = effective_background(page, selector)
    assert check_ratio(selector, fg0, bg0, FLOOR_BODY).ok, "baseline hint should pass before the override"

    # Inject a deliberately bad override: hint colour ~= its own panel background (contrast ~1:1).
    page.add_style_tag(content=".hint { color: rgb(232, 237, 233) !important; }")

    fg1 = resolved_color(page, selector, "color")
    bg1 = effective_background(page, selector)
    ratio1 = contrast_ratio(fg1, bg1)
    assert ratio1 < FLOOR_BODY, f"override failed to lower resolved contrast (got {ratio1:.2f}:1)"
    verdict = check_ratio(selector, fg1, bg1, FLOOR_BODY)
    assert not verdict.ok, "the contrast helper did not report the injected violation — it cannot fail"
