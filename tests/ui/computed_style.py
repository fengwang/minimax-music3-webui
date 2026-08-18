"""Actions that read RESOLVED styles from a rendered page and feed the pure contrast Calculations.

Nothing here computes a ratio; it reads what the browser actually PAINTED (getComputedStyle) and resolves
the effective background stack. Keeping the maths in contrast.py pure is what makes the negative control
meaningful: an injected override changes the strings these readers return, so the verdict flips.
"""
from __future__ import annotations

from contrast import Rgb, composite_over, parse_computed_color

# The browser's default canvas under the page's own backgrounds.
_WHITE = Rgb(255.0, 255.0, 255.0, 1.0)


def resolved(page, selector: str, prop: str) -> str:
    """Return one resolved CSS property of the first element matching `selector`."""
    return page.locator(selector).first.evaluate(
        "(el, name) => getComputedStyle(el).getPropertyValue(name)", prop
    )


def resolved_color(page, selector: str, prop: str = "color") -> Rgb:
    """Return one resolved colour property of the first matching element as an Rgb."""
    return parse_computed_color(resolved(page, selector, prop))


def length_px(page, selector: str, prop: str) -> float:
    """Return a resolved length property (e.g. outline-width, outline-offset) of the first match, in px."""
    raw = resolved(page, selector, prop).strip()
    return float(raw[:-2]) if raw.endswith("px") else float(raw)


def effective_background(page, selector: str) -> Rgb:
    """Composite the element's own and its ancestors' background-colors into the opaque colour painted
    behind it. Chromium reports 'rgba(0, 0, 0, 0)' for a transparent background, which contributes
    nothing, so a text element with no background resolves to its nearest opaque ancestor surface.

    Only ``background-color`` is composited; ``background-image``/gradients are out of scope because S1's
    theme paints every surface with a solid role token and uses box-shadow (not gradients) for elevation.
    A later session that introduces a gradient behind text would need to extend this reader."""
    layers = page.locator(selector).first.evaluate(
        """(el) => {
            const out = [];
            let node = el;
            while (node) { out.push(getComputedStyle(node).backgroundColor); node = node.parentElement; }
            return out;
        }"""
    )
    result = _WHITE
    for layer in reversed(layers):  # furthest ancestor first, element last
        colour = parse_computed_color(layer)
        if colour.a > 0:
            result = composite_over(colour, result)
    return result


def assert_rendered(page) -> None:
    """Fail loudly unless the known index.html shell actually rendered, so an empty page (wrong served
    directory, selectors matching nothing) cannot yield a green run (adversarial case #3)."""
    generate = page.locator("#generate").first
    generate.wait_for(state="visible", timeout=5000)
    assert generate.is_visible(), "#generate did not render — is the stub serving api/app/static/?"
    brand = page.locator(".brand").first.inner_text()
    assert "MiniMax-Music3" in brand, f"brand licence string missing (got {brand!r})"


def focus_via_keyboard(page, target_id: str, max_tabs: int = 25) -> None:
    """Move focus with the Tab key until document.activeElement.id == target_id, so Chromium's
    :focus-visible heuristic (which distinguishes keyboard from pointer focus) actually matches. A
    programmatic element.focus() may not trigger :focus-visible, so the ring would not render."""
    page.evaluate(
        "() => { const a = document.activeElement; if (a && a.blur) a.blur(); "
        "if (document.body && document.body.focus) document.body.focus(); }"
    )
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        if page.evaluate("() => (document.activeElement && document.activeElement.id) || ''") == target_id:
            return
    raise AssertionError(f"could not move keyboard focus to #{target_id} within {max_tabs} tabs")
