"""FR-04 (spec: persistent-player): one player with native controls, staying visible while scrolling,
surviving a completed regeneration, and consolidated (no per-row or standalone legacy audio)."""
from __future__ import annotations

from _gen_helpers import seed_completed, start_generating
from computed_style import assert_rendered
from stub_backend import make_pcm_wav


def _rect(page, selector: str) -> dict:
    return page.locator(selector).first.evaluate(
        "el => { const r = el.getBoundingClientRect(); return {top: r.top, bottom: r.bottom}; }"
    )


def test_native_controls_present_and_no_legacy_players(page, base_url, stub) -> None:
    seed_completed(stub, 2)
    stub.audio_bodies = {"job-0": make_pcm_wav(1.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    assert page.get_attribute("#player-audio", "controls") is not None, "native controls must stay reachable"
    assert page.locator(".artifacts audio").count() == 0, "no per-row inline <audio> after consolidation"
    assert page.locator("#result-audio").count() == 0, "standalone #result-audio removed"


def test_player_stays_in_viewport_and_row_reachable(page, base_url, stub) -> None:
    seed_completed(stub, 8)
    stub.audio_bodies = {"job-0": make_pcm_wav(1.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(100)
    vh = page.evaluate("() => window.innerHeight")
    pl = _rect(page, "#player")
    assert pl["top"] < vh and pl["bottom"] <= vh + 1, f"player not within viewport: {pl} vh={vh}"
    newest = page.locator(".artifacts li").last.evaluate("el => el.getBoundingClientRect().bottom")
    assert newest <= pl["top"] + 1, f"newest row bottom {newest} hidden behind player top {pl['top']}"


def test_playback_survives_a_completed_regeneration(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.audio_bodies = {"job-0": make_pcm_wav(4.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    page.evaluate("() => { const a = document.getElementById('player-audio'); a.pause(); a.currentTime = 1.5; }")
    src_before = page.evaluate("() => document.getElementById('player-audio').currentSrc")
    # a new generation completes and refreshes the listing; the player must not be disturbed.
    start_generating(page, stub, events=[("queued", {}), ("running", {}), ("succeeded", {})])
    page.wait_for_selector('#status[data-state="succeeded"]', timeout=5000)
    assert page.evaluate("() => document.getElementById('player-audio').currentSrc") == src_before, "src changed"
    assert page.evaluate("() => document.getElementById('player-audio').currentTime") >= 1.4, "currentTime reset"
