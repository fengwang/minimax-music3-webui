"""FR-05 follow-up (owner change 2026-08-17): with a tall On disk library, the live Status card must stay
visible without scrolling. The right column is ordered Status-on-top, On disk below, so a list of ~10
records cannot push the generation status below the fold. Regression guard for the card ordering."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered


def test_status_visible_without_scrolling_with_many_records(page, base_url, stub) -> None:
    seed_completed(stub, 10)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(base_url)
    assert_rendered(page)
    page.wait_for_selector("#artifact-list li:first-child .play")
    assert page.evaluate("() => window.scrollY") == 0, "page must not be scrolled at load"
    status = page.locator("#status").evaluate(
        "el => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; }"
    )
    assert status["top"] >= 0 and status["bottom"] <= 800, f"Status not visible without scrolling: {status}"
    # And the Status card sits ABOVE the On disk library (the owner's requested ordering).
    status_bottom = page.locator("section[aria-labelledby='status-h']").evaluate(
        "el => el.getBoundingClientRect().bottom"
    )
    newest_row_top = page.locator("#artifact-list li").first.evaluate("el => el.getBoundingClientRect().top")
    assert status_bottom <= newest_row_top + 1, (
        f"Status card is not above the On disk library ({status_bottom} vs {newest_row_top})"
    )
