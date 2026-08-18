"""C4-6 (spec: seek-control): the seek affordance is reachable by Tab and operable by ArrowRight, ArrowLeft,
Home and End — each changing audio.currentTime in the documented direction with no pointer event."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered, focus_via_keyboard
from stub_backend import make_pcm_wav


def _activate_and_focus(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.audio_bodies = {"job-0": make_pcm_wav(8.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")
    page.evaluate("() => document.getElementById('player-audio').pause()")


def test_slider_reachable_by_tab_and_is_a_slider(page, base_url, stub) -> None:
    _activate_and_focus(page, base_url, stub)
    focus_via_keyboard(page, "waveform")  # raises if Tab cannot reach it
    assert page.get_attribute("#waveform", "role") == "slider"


def test_arrow_home_end_seek_without_pointer(page, base_url, stub) -> None:
    _activate_and_focus(page, base_url, stub)
    focus_via_keyboard(page, "waveform")

    def cur() -> float:
        return page.evaluate("() => document.getElementById('player-audio').currentTime")

    page.keyboard.press("Home")
    assert cur() == 0
    page.keyboard.press("ArrowRight")
    assert abs(cur() - 5) <= 0.1, "ArrowRight should advance ~5s"
    page.keyboard.press("ArrowLeft")
    assert cur() <= 0.1, "ArrowLeft should return to ~0"
    dur = page.evaluate("() => document.getElementById('player-audio').duration")
    page.keyboard.press("End")
    assert abs(cur() - dur) <= 0.1, "End should jump to the duration"
