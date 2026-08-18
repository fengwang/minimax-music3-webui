"""FR-05 (spec: layout-above-fold): the sub-880px collapse is a don't-regress invariant. At 800px the grid
computes to a single column, and exactly one media query (at max-width: 880px) remains. These are
regression guards: they assert current-correct behaviour and MUST stay green through the restructure."""
from __future__ import annotations

from pathlib import Path

from _gen_helpers import seed_completed
from computed_style import assert_rendered

_STYLE = Path(__file__).resolve().parents[2] / "api" / "app" / "static" / "style.css"


def test_grid_collapses_to_one_column_at_800(page, base_url, stub) -> None:
    seed_completed(stub, 3)
    page.set_viewport_size({"width": 800, "height": 800})
    page.goto(base_url)
    assert_rendered(page)
    cols = page.evaluate("() => getComputedStyle(document.querySelector('.grid')).gridTemplateColumns")
    tracks = [t for t in cols.split() if t]
    assert len(tracks) == 1, f"grid did not collapse to one column at 800px: {cols!r}"


def test_exactly_one_media_query_in_style_css() -> None:
    assert _STYLE.read_text().count("max-width: 880px") == 1
