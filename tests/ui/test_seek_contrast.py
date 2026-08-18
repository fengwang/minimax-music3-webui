"""C4-7 (spec: seek-control, P2-INV-7): the seek affordance's control boundary is >= 3:1 against the
adjacent surface, and its focus indicator is >= 3:1 against BOTH the control and the surface AND >= 2px —
asserted against post-cascade computed styles. P2-E09 (neumorphic edges ~1.10-1.16:1) is EXTERNAL EVIDENCE
quoted as rationale only; the binding requirement is the project floors."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import (
    assert_rendered,
    effective_background,
    focus_via_keyboard,
    length_px,
    resolved_color,
)
from contrast import FLOOR_BOUNDARY, FLOOR_FOCUS, FOCUS_MIN_PX, contrast_ratio
from stub_backend import make_pcm_wav


def _activate(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.audio_bodies = {"job-0": make_pcm_wav(2.0)}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_function("() => document.getElementById('player-audio').duration > 0")


def test_seek_boundary_contrast(page, base_url, stub) -> None:
    _activate(page, base_url, stub)
    border = resolved_color(page, "#waveform", "border-top-color")  # --border-control
    surface = effective_background(page, ".player")  # the player surface the control sits on
    ratio = contrast_ratio(border, surface)
    assert ratio >= FLOOR_BOUNDARY, f"seek boundary {ratio:.2f}:1 < {FLOOR_BOUNDARY}:1"


def test_seek_focus_indicator_contrast(page, base_url, stub) -> None:
    _activate(page, base_url, stub)
    focus_via_keyboard(page, "waveform")  # keyboard focus => :focus-visible matches
    assert length_px(page, "#waveform", "outline-width") >= FOCUS_MIN_PX, "focus ring thinner than 2px"
    ring = resolved_color(page, "#waveform", "outline-color")
    control = effective_background(page, "#waveform")  # the control's own surface (--surface-field)
    surface = effective_background(page, ".player")  # the adjacent player surface (--surface-raised)
    assert contrast_ratio(ring, control) >= FLOOR_FOCUS, f"focus ring {contrast_ratio(ring, control):.2f}:1 < 3:1 vs control"
    assert contrast_ratio(ring, surface) >= FLOOR_FOCUS, f"focus ring {contrast_ratio(ring, surface):.2f}:1 < 3:1 vs surface"
