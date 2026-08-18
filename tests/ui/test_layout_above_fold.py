"""FR-05 (spec: layout-above-fold): the newest generated output and its primary play control sit wholly
inside the 1280x800 reference viewport (dpr 1) at scroll position zero. The newest-output region is the
first artifact row; its primary play control is that row's ▶ Play button."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered


def test_newest_output_above_fold_at_1280x800(page, base_url, stub) -> None:
    seed_completed(stub, 4)
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(base_url)
    assert_rendered(page)
    page.wait_for_selector("#artifact-list li:first-child .play")
    assert page.evaluate("() => window.devicePixelRatio") == 1, "reference viewport is dpr 1"
    assert page.evaluate("() => window.scrollY") == 0, "page must not be scrolled at load"
    row_top = page.locator("#artifact-list li").first.evaluate("el => el.getBoundingClientRect().top")
    play_bottom = page.locator("#artifact-list li:first-child .play").evaluate(
        "el => el.getBoundingClientRect().bottom"
    )
    assert row_top >= 0, f"newest-output region top {row_top} is above the viewport top"
    assert play_bottom <= 800, f"newest play control bottom {play_bottom} > 800 (below the fold)"
