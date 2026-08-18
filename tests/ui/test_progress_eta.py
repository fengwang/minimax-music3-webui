"""FR-01 (specs: two-phase-progress, eta-estimate): warming vs generating, the elapsed timer anchored on
the running event, the delivered-vs-maximum completion readout, and the self-calibrating ETA.

The elapsed timer is driven by a LOCAL interval (the SSE heartbeat fires no callback), so a stream that
stops emitting must not freeze the clock; the tests drive non-terminal ``queued,running`` sequences to
observe the generating state, and terminal sequences to observe stop behaviour.
"""
from __future__ import annotations

import re

from _gen_helpers import fill_form, seed_completed, start_generating
from computed_style import assert_rendered
from stub_backend import make_listing_item, make_sidecar


def _text(page, selector: str) -> str:
    return page.locator(selector).inner_text()


def _mmss(text: str) -> int:
    m = re.search(r"(\d+):(\d{2})", text)
    assert m, f"no m:ss clock in {text!r}"
    return int(m.group(1)) * 60 + int(m.group(2))


# ----------------------------------------------------------------- warming vs generating

def test_warming_label_is_distinct_and_quotes_the_range(page, base_url) -> None:
    """Pure deriveStatus: warming differs from generating and quotes the P2-E05 71-87 s range (not a
    countdown, not a single number)."""
    page.goto(base_url)
    assert_rendered(page)
    labels = page.evaluate(
        "() => ({ warming: MUSIC3.deriveStatus('queued','warming').label,"
        " generating: MUSIC3.deriveStatus('running','ready').label })"
    )
    assert labels["warming"] != labels["generating"], "warming must read differently from generating"
    assert "71" in labels["warming"] and "87" in labels["warming"], "warming should quote the 71-87 s range"


def test_warming_renders_distinct_state_in_dom(page, base_url, stub) -> None:
    stub.engine = "warming"
    stub.events = [("queued", {})]  # stays queued; /health=warming drives the warming state
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.click("#generate")
    page.wait_for_selector('#status[data-state="warming"]', timeout=5000)
    assert "71" in _text(page, "#status") and "87" in _text(page, "#status")
    assert page.locator("#progress").is_hidden(), "no elapsed timer before the running event"


# ----------------------------------------------------------------- elapsed timer

def test_timer_hidden_while_queued(page, base_url, stub) -> None:
    seed_completed(stub, 0)
    stub.events = [("queued", {})]  # no running event -> the timer must never start
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.click("#generate")
    page.wait_for_selector('#status[data-state="queued"]', timeout=5000)
    assert page.locator("#progress").is_hidden(), "timer must not run before running (not anchored on submit)"


def test_elapsed_timer_starts_at_zero_on_running_and_advances(page, base_url, stub) -> None:
    seed_completed(stub, 0)  # no history: the TIMER is independent of the ETA
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub)  # events [queued, running], non-terminal
    page.wait_for_selector("#progress:not([hidden])", timeout=5000)
    first = _text(page, "#elapsed")
    assert re.match(r"^0:0\d$", first), f"timer should read ~0:00 at the running event, got {first!r}"
    page.wait_for_timeout(1300)  # advances with NO further SSE events (local interval)
    later = _text(page, "#elapsed")
    assert _mmss(later) > _mmss(first), f"elapsed timer did not advance: {first!r} -> {later!r}"


def test_progress_stops_on_cancelled(page, base_url, stub) -> None:
    seed_completed(stub, 0)
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub, events=[("queued", {}), ("running", {}), ("cancelled", {})])
    page.wait_for_selector('#status[data-state="cancelled"]', timeout=5000)
    assert page.locator("#progress").is_hidden(), "timer/ETA must stop on cancelled"


def test_progress_stops_on_failed(page, base_url, stub) -> None:
    seed_completed(stub, 0)
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub, events=[("queued", {}), ("running", {}), ("failed", {"error": "engine oom"})])
    page.wait_for_selector('#status[data-state="failed"]', timeout=5000)
    assert page.locator("#progress").is_hidden(), "timer/ETA must stop on failed"


# ----------------------------------------------------------------- delivered vs requested maximum

def test_delivered_duration_shown_beside_requested_maximum(page, base_url, stub) -> None:
    """P2-E03: a 360 s (9000-frame) request delivers 279.29 s. Both numbers must be visible and distinct,
    delivered taken from output.duration_seconds (never recomputed from frames)."""
    stub.artifacts = [make_listing_item("job-demo", max_new_tokens=9000, duration_seconds=279.29)]
    stub.sidecars = {"job-demo": make_sidecar("job-demo", max_new_tokens=9000, duration_seconds=279.29)}
    stub.events = [("queued", {}), ("running", {}), ("succeeded", {})]
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "360")
    page.click("#generate")
    page.wait_for_selector("#result-delivered:not([hidden])", timeout=5000)
    text = _text(page, "#result-delivered")
    assert "maximum" in text.lower(), f"shared honesty vocabulary missing: {text!r}"
    assert "6:00" in text and "360" in text, f"requested maximum (360 s) missing: {text!r}"
    assert "4:39" in text and "279" in text, f"delivered duration (279.29 s) missing: {text!r}"


# ----------------------------------------------------------------- self-calibrating ETA (C3-4, C3-8)

def test_eta_suppressed_with_zero_one_two_artifacts(page, base_url, stub) -> None:
    """C3-4: with 0/1/2 completed artifacts the ETA estimate is absent and the indeterminate state shows."""
    for n in (0, 1, 2):
        seed_completed(stub, n, generation_seconds=180.0, max_new_tokens=7500)
        page.goto(base_url)
        assert_rendered(page)
        start_generating(page, stub)
        page.wait_for_selector("#progress:not([hidden])", timeout=5000)
        page.wait_for_timeout(400)  # let the async sample collection settle; must NOT become bounded
        assert page.get_attribute("#eta", "data-eta") == "indeterminate", f"n={n}: ETA should be suppressed"
        assert "within ~" not in _text(page, "#eta"), f"n={n}: an ETA estimate was rendered"


def test_eta_suppressed_threshold_three_present(page, base_url, stub) -> None:
    """C3-4: with 3 completed artifacts the ETA estimate appears while generating within it."""
    seed_completed(stub, 3, generation_seconds=180.0, max_new_tokens=7500)
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub)  # default 300 s -> 7500 frames -> estimate ~180 s (bounded)
    page.wait_for_selector('#eta[data-eta="bounded"]', timeout=8000)
    eta_text = _text(page, "#eta")
    assert "within ~" in eta_text and "maximum" in eta_text.lower(), eta_text


def test_eta_derived_from_history_doubling(page, base_url, stub) -> None:
    """C3-8: doubling the historical generation_seconds (max_new_tokens unchanged) strictly enlarges the
    ETA for the same requested duration. A constant or hardcoded rate cannot pass this."""

    def eta_for(gen: float) -> int:
        seed_completed(stub, 3, generation_seconds=gen, max_new_tokens=7500)
        page.goto(base_url)
        assert_rendered(page)
        start_generating(page, stub)  # same requested 300 s both runs
        page.wait_for_selector('#eta[data-eta="bounded"]', timeout=8000)
        return _mmss(_text(page, "#eta"))

    base = eta_for(120.0)
    doubled = eta_for(240.0)
    assert doubled > base, f"ETA did not respond to doubled history: {base}s -> {doubled}s"


def test_eta_flips_to_indeterminate_when_elapsed_exceeds(page, base_url, stub) -> None:
    """Adversarial #1: a generation longer than the fitted estimate flips to indeterminate, never 0:00."""
    seed_completed(stub, 3, generation_seconds=3.0, max_new_tokens=7500)  # estimate ~3 s
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub)
    page.wait_for_selector("#progress:not([hidden])", timeout=5000)
    page.wait_for_selector('#eta[data-eta="indeterminate"]', timeout=8000)  # elapsed passes ~3 s
    # 'exceeded' has a wording distinct from 'suppressed' (both map to data-eta=indeterminate); assert it,
    # so the exceeded message cannot regress into the suppressed one unnoticed.
    assert "longer than the estimated maximum" in _text(page, "#eta").lower()


def test_eta_uses_no_extra_request_beyond_row_sidecars(page, base_url, stub) -> None:
    """The ETA fit reuses the per-row sidecar fetches (shared cache): exactly one sidecar request per
    artifact, none added (P2-E11)."""
    seed_completed(stub, 3, generation_seconds=120.0, max_new_tokens=7500)
    hits: list[str] = []
    page.on("request", lambda r: hits.append(r.url) if "sidecar.json" in r.url else None)
    page.goto(base_url)
    assert_rendered(page)
    page.wait_for_timeout(600)  # rows + ETA sample collection settle
    assert len(hits) == 3, f"expected one sidecar fetch per row (3), got {len(hits)}: {hits}"


def test_shared_maximum_vocabulary_across_surfaces(page, base_url, stub) -> None:
    """PRD §6: the word 'maximum' appears on all three honesty surfaces — the duration control, the ETA,
    and the delivered readout — so the cap-not-target concept speaks in one voice."""
    page.goto(base_url)
    assert_rendered(page)
    assert "maximum" in page.locator(".controls").inner_text().lower(), "duration control missing 'maximum'"

    seed_completed(stub, 3, generation_seconds=180.0, max_new_tokens=7500)
    page.goto(base_url)
    assert_rendered(page)
    start_generating(page, stub)
    page.wait_for_selector('#eta[data-eta="bounded"]', timeout=8000)
    assert "maximum" in _text(page, "#eta").lower(), "ETA missing 'maximum'"

    stub.artifacts = stub.artifacts + [make_listing_item("job-demo", max_new_tokens=9000, duration_seconds=279.29)]
    stub.sidecars = {**stub.sidecars, "job-demo": make_sidecar("job-demo", max_new_tokens=9000, duration_seconds=279.29)}
    stub.events = [("queued", {}), ("running", {}), ("succeeded", {})]
    page.goto(base_url)
    assert_rendered(page)
    fill_form(page)
    page.fill("#max_seconds", "360")
    page.click("#generate")
    page.wait_for_selector("#result-delivered:not([hidden])", timeout=5000)
    assert "maximum" in _text(page, "#result-delivered").lower(), "delivered readout missing 'maximum'"


def test_music3_eta_calculations(page, base_url) -> None:
    """Off-DOM (globalThis.MUSIC3): suppression < 3, positive upper bound >= 3, history responsiveness, and
    the never-count-to-zero state transition."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        """() => {
            const mk = (seconds, frames, mtime) => ({seconds, frames, mtime});
            const two = [mk(100,5000,3), mk(100,5000,2)];
            const three = [mk(120,7500,3), mk(120,7500,2), mk(120,7500,1)];
            const doubled = three.map(h => ({seconds: h.seconds*2, frames: h.frames, mtime: h.mtime}));
            return {
              suppressed2: MUSIC3.fitEtaSeconds(two, 7500),
              est3: MUSIC3.fitEtaSeconds(three, 7500),
              estDoubled: MUSIC3.fitEtaSeconds(doubled, 7500),
              bounded: MUSIC3.etaState(1, 10).kind,
              exceeded: MUSIC3.etaState(20, 10).kind,
              noEstimate: MUSIC3.etaState(5, null).kind,
            };
        }"""
    )
    assert r["suppressed2"] is None
    assert r["est3"] is not None and r["est3"] > 0
    assert r["estDoubled"] > r["est3"]
    assert r["bounded"] == "bounded"
    assert r["exceeded"] == "exceeded"        # elapsed past the estimate: a distinct kind, never 0:00
    assert r["noEstimate"] == "suppressed"    # no estimate (< 3 samples)


def test_music3_eta_fit_ignores_order_and_outlier(page, base_url) -> None:
    """Adversarial #8: the fit is invariant to list order and one pathological outlier is trimmed, so the
    upper bound is not skewed. Off-DOM (MUSIC3.fitEtaSeconds), fully deterministic."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        """() => {
            const mk = (seconds, frames, mtime) => ({seconds, frames, mtime});
            const base = [mk(150,7500,5), mk(150,7500,4), mk(150,7500,3), mk(150,7500,2), mk(150,7500,1)];
            const outlier = mk(1500,7500,6);  // 10x slower, and the newest by mtime
            const shuffledA = [base[2], outlier, base[0], base[4], base[1], base[3]];
            const shuffledB = [outlier, base[3], base[1], base[4], base[0], base[2]];
            return {
              a: MUSIC3.fitEtaSeconds(shuffledA, 7500),
              b: MUSIC3.fitEtaSeconds(shuffledB, 7500),
              clean: MUSIC3.fitEtaSeconds(base, 7500),
            };
        }"""
    )
    assert r["a"] == r["b"], f"fit depends on list order: {r['a']} vs {r['b']}"
    assert abs(r["a"] - r["clean"]) < 1e-6, f"one outlier skewed the fit: {r['a']} vs clean {r['clean']}"
