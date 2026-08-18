"""C4-4 / P2-INV-11 (spec: lazy-waveform): a listing render fetches no audio body; activating one row
fetches exactly one, for that row's audio_url only. Laziness is the load-bearing invariant of S4."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered
from stub_backend import make_pcm_wav


def _audio_collector(page) -> list[str]:
    reqs: list[str] = []
    page.on("request", lambda r: reqs.append(r.url) if r.url.endswith("/audio.wav") else None)
    return reqs


def test_no_audio_body_on_listing_render(page, base_url, stub) -> None:
    seed_completed(stub, 3)
    reqs = _audio_collector(page)
    page.goto(base_url)
    assert_rendered(page)
    page.wait_for_selector('li[data-job-id="job-0"] .play')
    page.wait_for_timeout(250)  # let any late sidecar / eta-sample fetches settle
    assert reqs == [], f"audio bodies fetched on render: {reqs}"


def test_exactly_one_body_for_the_activated_row(page, base_url, stub) -> None:
    seed_completed(stub, 3)
    stub.audio_bodies = {f"job-{i}": make_pcm_wav(1.0) for i in range(3)}
    reqs = _audio_collector(page)
    page.goto(base_url)
    assert_rendered(page)
    page.wait_for_selector('li[data-job-id="job-1"] .play')
    page.click('li[data-job-id="job-1"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    page.wait_for_timeout(150)
    assert len(reqs) == 1, f"expected exactly one audio body, got {reqs}"
    assert reqs[0].endswith("/job-1/audio.wav"), reqs[0]


def test_byte_size_shown_on_play_before_any_fetch(page, base_url, stub) -> None:
    """adversarial case #9: output.byte_size is read from the listing and shown on the Play affordance at
    render time, BEFORE any audio body is fetched — so the size decision cannot depend on starting the
    download."""
    seed_completed(stub, 1, duration_seconds=279.29)  # byte_size = 279.29 * 32000 * 2 * 2 ≈ 34 MiB
    reqs = _audio_collector(page)
    page.goto(base_url)
    assert_rendered(page)
    play = page.locator('li[data-job-id="job-0"] .play')
    play.wait_for(state="visible")
    label = play.inner_text()
    assert "MiB" in label, f"human-readable size not shown on Play: {label!r}"
    assert reqs == [], f"an audio body was fetched before the size was shown: {reqs}"
