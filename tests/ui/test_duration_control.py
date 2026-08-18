"""FR-02 (spec: duration-seconds-control): the seconds control and its exact conversion to the
``max_new_tokens`` wire field. Gated by C3-5 (``seconds_to_frames``) and C3-10 (default 300 = 7500 frames).

The named-control-set change (``max_new_tokens`` -> ``max_seconds``) is asserted structurally in
``tests/test_static_structure.py`` — the single deliberate edit called out in review (P2-INV-9).
"""
from __future__ import annotations

from _gen_helpers import fill_form
from computed_style import assert_rendered


def _post_frames(page) -> int:
    """Click Generate and return ``max_new_tokens`` from the POST /jobs body."""
    with page.expect_request(
        lambda r: r.method == "POST" and r.url.rstrip("/").endswith("/jobs")
    ) as info:
        page.click("#generate")
    return info.value.post_data_json["max_new_tokens"]


def test_seconds_to_frames_upper_boundary(page, base_url) -> None:
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "360")
    assert _post_frames(page) == 9000  # ceil(360*25), clamped into 1..9000


def test_seconds_to_frames_lower_boundary(page, base_url) -> None:
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "10")
    frames = _post_frames(page)
    assert frames >= 1 and frames == 250  # smallest accepted seconds posts max_new_tokens >= 1


def test_seconds_to_frames_default_preserves_7500(page, base_url) -> None:
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)  # leave #max_seconds at its default 300 s
    assert _post_frames(page) == 7500  # 300 s = the phase-1 7500-frame default (P2-E08)


def test_seconds_to_frames_above_max_refused_before_dispatch(page, base_url) -> None:
    posted: list[str] = []
    page.on(
        "request",
        lambda r: posted.append(r.url)
        if (r.method == "POST" and r.url.rstrip("/").endswith("/jobs"))
        else None,
    )
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "500")  # 500 s > 360 s hard cap
    page.click("#generate")
    page.wait_for_selector("#form-error:not([hidden])", timeout=3000)
    assert posted == [], f"a job was dispatched for an out-of-range duration: {posted}"


def test_seconds_to_frames_pure_calc(page, base_url) -> None:
    """The exposed pure Calculations agree with the boundaries, off-DOM (globalThis.MUSIC3)."""
    page.goto(base_url)
    assert_rendered(page)
    calc = page.evaluate(
        "() => [MUSIC3.secondsToFrames(10), MUSIC3.secondsToFrames(300), MUSIC3.secondsToFrames(360),"
        " MUSIC3.validateSeconds('500').ok, MUSIC3.validateSeconds('300').ok, MUSIC3.validateSeconds('5').ok]"
    )
    assert calc == [250, 7500, 9000, False, True, False]
