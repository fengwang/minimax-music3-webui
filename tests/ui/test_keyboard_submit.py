"""FR-06 (spec: keyboard-submit): Ctrl+Enter submits from either textarea through the same validation as
the button; plain Enter still inserts a newline; and the single-slot runner is honoured (no double-submit,
no in-flight second job).
"""
from __future__ import annotations

from _gen_helpers import fill_form
from computed_style import assert_rendered


def _watch_posts(page) -> list[str]:
    posted: list[str] = []
    page.on(
        "request",
        lambda r: posted.append(r.url)
        if (r.method == "POST" and r.url.rstrip("/").endswith("/jobs"))
        else None,
    )
    return posted


def test_ctrl_enter_submits_from_textarea(page, base_url, stub) -> None:
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    with page.expect_request(lambda r: r.method == "POST" and r.url.rstrip("/").endswith("/jobs")):
        page.locator("#instructions").press("Control+Enter")


def test_plain_enter_inserts_newline_and_does_not_submit(page, base_url, stub) -> None:
    posted = _watch_posts(page)
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    box = page.locator("#input")
    box.fill("[Verse]")
    box.press("End")
    box.press("Enter")
    page.wait_for_timeout(200)
    assert posted == [], "plain Enter must not submit"
    assert box.input_value() == "[Verse]\n", "plain Enter should insert a newline"


def test_ctrl_enter_refused_by_over_limit_validation(page, base_url, stub) -> None:
    """Exit criteria 7: the keyboard path is refused by the same 5,000-token guard as the button."""
    posted = _watch_posts(page)
    page.goto(base_url)
    assert_rendered(page)
    page.fill("#input", "x" * 21000)  # ~5,255 estimated tokens > the 5,000 limit
    page.fill("#instructions", "Global Metadata: pop")
    page.locator("#input").press("Control+Enter")
    page.wait_for_selector("#form-error:not([hidden])", timeout=3000)
    assert posted == [], "over-limit Ctrl+Enter must be refused before dispatch"


def test_ctrl_enter_refused_by_out_of_range_duration(page, base_url, stub) -> None:
    """Adversarial #11 (duration half): an out-of-range max_seconds is refused on the keyboard path too."""
    posted = _watch_posts(page)
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "500")  # 500 s > 360 s hard cap
    page.locator("#instructions").press("Control+Enter")
    page.wait_for_selector("#form-error:not([hidden])", timeout=3000)
    assert posted == [], "out-of-range duration via Ctrl+Enter must be refused before dispatch"


def test_ctrl_enter_no_double_submit(page, base_url, stub) -> None:
    posted = _watch_posts(page)
    stub.events = [("queued", {}), ("running", {})]  # stays in flight after the first submit
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    box = page.locator("#input")
    box.press("Control+Enter")
    box.press("Control+Enter")  # rapid second press
    page.wait_for_timeout(500)
    assert len(posted) == 1, f"rapid double Ctrl+Enter created {len(posted)} jobs (expected exactly 1)"


def test_ctrl_enter_no_second_job_while_in_flight(page, base_url, stub) -> None:
    posted = _watch_posts(page)
    stub.events = [("queued", {}), ("running", {})]
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.click("#generate")
    page.wait_for_selector("#progress:not([hidden])", timeout=5000)  # first job generating
    assert len(posted) == 1
    page.locator("#instructions").press("Control+Enter")  # attempt a second job by keyboard
    page.wait_for_timeout(400)
    assert len(posted) == 1, "Ctrl+Enter must not create a second job while one is in flight (single slot)"
