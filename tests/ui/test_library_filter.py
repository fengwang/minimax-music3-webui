"""FR-07 (spec: library-filter): a client-side filter over the already-fetched listing.

Zero added requests, an explicit no-matches state, playback undisturbed when the playing row is filtered
out, and a keyboard-reachable control with a conforming focus ring. The filter is a UI-only affordance: it
posts nothing and issues no fetch — it only toggles `hidden` on rows already in the DOM."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered, focus_via_keyboard, length_px, resolved_color
from stub_backend import make_pcm_wav

_ACCENT_INK = (47, 111, 91)  # --accent-ink #2F6F5B: the token the focus ring must resolve to


def _settle(page) -> None:
    page.wait_for_selector('#artifact-list li[data-job-id="job-0"] .play')
    page.wait_for_timeout(250)  # let the /artifacts + per-row sidecar + eta fetches finish


def test_filter_no_request_and_visible_count_changes(page, base_url, stub) -> None:
    seed_completed(stub, 5)
    page.goto(base_url)
    assert_rendered(page)
    _settle(page)
    reqs: list[str] = []
    page.on("request", lambda r: reqs.append(r.url))
    total = page.locator("#artifact-list > li:not([hidden])").count()
    page.fill("#artifact-filter", "job-0")
    page.wait_for_timeout(100)
    visible = page.locator("#artifact-list > li:not([hidden])").count()
    assert visible < total, f"filter did not reduce visible rows ({visible} of {total})"
    assert reqs == [], f"filter issued network requests: {reqs}"


def test_filter_no_request_clear_restores_full_list(page, base_url, stub) -> None:
    seed_completed(stub, 5)
    page.goto(base_url)
    assert_rendered(page)
    _settle(page)
    total = page.locator("#artifact-list > li").count()
    page.fill("#artifact-filter", "job-0")
    page.wait_for_timeout(50)
    page.fill("#artifact-filter", "")
    page.wait_for_timeout(50)
    visible = page.locator("#artifact-list > li:not([hidden])").count()
    assert visible == total, f"clearing the filter did not restore all rows ({visible} of {total})"


def test_filter_no_matches_shows_explicit_state(page, base_url, stub) -> None:
    seed_completed(stub, 3)
    page.goto(base_url)
    assert_rendered(page)
    _settle(page)
    page.fill("#artifact-filter", "zzz-nonexistent")
    page.wait_for_timeout(100)
    assert page.locator("#artifact-list > li:not([hidden])").count() == 0, "rows still visible on no-match"
    assert page.locator("#artifact-none").is_visible(), "explicit no-matches state not shown"


def test_filter_playing_artifact_survives(page, base_url, stub) -> None:
    seed_completed(stub, 3)
    stub.audio_bodies = {"job-0": make_pcm_wav(4.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('#artifact-list li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    # muted media may autoplay in headless without a lingering user gesture, so playback is reliably live
    page.evaluate("() => { const a = document.getElementById('player-audio'); a.muted = true; return a.play(); }")
    page.wait_for_function("() => !document.getElementById('player-audio').paused", timeout=3000)
    t_before = page.evaluate("() => document.getElementById('player-audio').currentTime")
    page.fill("#artifact-filter", "job-1")  # excludes job-0, the playing row
    page.wait_for_timeout(200)
    assert page.locator('#artifact-list li[data-job-id="job-0"]').is_hidden(), "playing row not filtered out"
    assert page.evaluate("() => document.getElementById('player-audio').paused") is False, "filter paused playback"
    t_after = page.evaluate("() => document.getElementById('player-audio').currentTime")
    assert t_after >= t_before, f"currentTime reset by filter: {t_after} < {t_before}"


def test_filter_keyboard_focus_ring(page, base_url, stub) -> None:
    seed_completed(stub, 2)
    page.goto(base_url)
    assert_rendered(page)
    focus_via_keyboard(page, "artifact-filter")  # Tab until #artifact-filter is the active element
    assert length_px(page, "#artifact-filter", "outline-width") >= 2.0, "focus ring thinner than 2px"
    assert length_px(page, "#artifact-filter", "outline-offset") >= 2.0, "ring not offset onto surface"
    ring = resolved_color(page, "#artifact-filter", "outline-color")
    assert (round(ring.r), round(ring.g), round(ring.b)) == _ACCENT_INK, "focus ring is not --accent-ink"
