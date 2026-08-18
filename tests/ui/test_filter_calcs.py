"""FR-07 pure Calculation (spec: library-filter), exercised off-DOM via globalThis.MUSIC3.filterMatch.

Same pattern as the S4 waveform-calc tests: no JS runtime exists outside the browser, so this runs as a
`ui` test through page.evaluate; filterMatch stays DOM-free (no document, no fetch)."""
from __future__ import annotations

from computed_style import assert_rendered


def test_filter_match_semantics(page, base_url) -> None:
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        "() => ["
        "MUSIC3.filterMatch('job-3 gentle pop', ''),"           # empty query -> match all
        "MUSIC3.filterMatch('job-3 gentle pop', '   '),"        # whitespace-only -> match all
        "MUSIC3.filterMatch('job-3 GENTLE pop', 'gentle'),"     # case-insensitive haystack
        "MUSIC3.filterMatch('job-3 gentle pop', 'POP'),"        # case-insensitive query
        "MUSIC3.filterMatch('job-3 gentle pop', 'job-3'),"      # substring on the id
        "MUSIC3.filterMatch('job-3 gentle pop', 'techno'),"     # no match
        "MUSIC3.filterMatch(null, 'x'),"                        # null haystack -> no match
        "MUSIC3.filterMatch('x', null)"                         # null query -> match all
        "]"
    )
    assert r == [True, True, True, True, True, False, False, True]
