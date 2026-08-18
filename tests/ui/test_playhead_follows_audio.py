"""C4-5 (spec: lazy-waveform): the drawn playhead is derived from audio.currentTime — setting it moves the
playhead with no other call — and the time axis derives from audio.duration, never a hardcoded 32000."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered
from stub_backend import make_pcm_wav


def _activate(page, base_url, job: str = "job-0") -> None:
    page.goto(base_url)
    assert_rendered(page)
    page.click(f'li[data-job-id="{job}"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")


def test_playhead_follows_programmatic_currenttime(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.audio_bodies = {"job-0": make_pcm_wav(2.0)}
    _activate(page, base_url)
    before = page.get_attribute("#waveform", "data-playhead-x")
    # setting currentTime is the ONLY call; the playhead must move from it (no separate clock).
    page.evaluate(
        "() => { const a = document.getElementById('player-audio'); a.pause(); a.currentTime = a.duration / 2; }"
    )
    page.wait_for_function(
        "(b) => document.getElementById('waveform').getAttribute('data-playhead-x') !== b", arg=before
    )
    after = float(page.get_attribute("#waveform", "data-playhead-x"))
    width = page.evaluate("() => document.getElementById('waveform').width")
    assert abs(after - width / 2) <= width * 0.08, f"playhead {after} not near mid {width / 2}"


def test_time_axis_is_not_hardcoded_to_32000(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.artifacts[0]["output"]["sample_rate"] = 16000
    stub.audio_bodies = {"job-0": make_pcm_wav(2.0, rate=16000)}
    _activate(page, base_url)
    dur = page.evaluate("() => document.getElementById('player-audio').duration")
    assert 1.8 <= dur <= 2.2, f"duration {dur} not ~2s — axis must come from audio.duration, not a fixed rate"
    rect = page.locator("#waveform").evaluate("el => { const r = el.getBoundingClientRect(); return {w: r.width, h: r.height}; }")
    page.locator("#waveform").click(position={"x": rect["w"] * 0.25, "y": rect["h"] / 2})
    ct = page.evaluate("() => document.getElementById('player-audio').currentTime")
    assert abs(ct - dur * 0.25) <= dur * 0.08, f"click at 25% mapped to {ct}, expected ~{dur * 0.25}"
