"""adversarial case #12 (spec: lazy-waveform): a fetched audio body that 404s (or is truncated) must show a
visible, escaped message in the player — never a silent blank canvas."""
from __future__ import annotations

from _gen_helpers import seed_completed
from computed_style import assert_rendered


def test_activation_404_shows_visible_message_not_blank_canvas(page, base_url, stub) -> None:
    seed_completed(stub, 1)
    stub.audio_missing = {"job-0"}   # /artifacts/job-0/audio.wav returns 404
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_selector("#player-error:not([hidden])", timeout=3000)
    message = page.locator("#player-error").inner_text()
    assert message.strip(), "player error must not be empty on a 404"
    assert "404" in message or "missing" in message.lower(), f"unexpected error text: {message!r}"


def test_error_message_is_inserted_as_text_not_markup(page, base_url, stub) -> None:
    """The message reaches the DOM via textContent (INV-8): a role="alert" <p> with no child elements."""
    seed_completed(stub, 1)
    stub.audio_missing = {"job-0"}
    page.goto(base_url)
    assert_rendered(page)
    page.click('li[data-job-id="job-0"] .play')
    page.wait_for_selector("#player-error:not([hidden])", timeout=3000)
    child_elements = page.locator("#player-error").evaluate("el => el.children.length")
    assert child_elements == 0, "error text must be textContent, never parsed markup (INV-8)"
