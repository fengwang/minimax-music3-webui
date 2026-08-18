"""Structural assertions over the static assets (specs: static-ui-shell.md, generation-form.md).

Stdlib-only (`html.parser`); no browser. The *behavioral* checks (submit refusal, token tiers, warming,
escaping, cancel) are executed in the throwaway headless-chromium harness recorded under docs/session_6,
because no JS runtime/test-runner can be added (pyproject is frozen). These tests assert the static
contract: control names, attribution, no blocking-route reference, no external origin, guidance text, and
that app.js uses no HTML-injection sink.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "api" / "app" / "static"
_HTML = (_STATIC / "index.html").read_text()
_CSS = (_STATIC / "style.css").read_text()
_JS = (_STATIC / "app.js").read_text()

_CONTROL_TAGS = {"input", "select", "textarea"}


class _FormControls(HTMLParser):
    """Collect names of form controls and the option values of each named <select>."""

    def __init__(self) -> None:
        super().__init__()
        self.names: list[str] = []
        self.select_options: dict[str, list[str | None]] = {}
        self._cur_select: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag in _CONTROL_TAGS and "name" in a:
            self.names.append(a["name"])
        if tag == "select":
            self._cur_select = a.get("name")
            if self._cur_select is not None:
                self.select_options[self._cur_select] = []
        if tag == "option" and self._cur_select is not None:
            self.select_options[self._cur_select].append(a.get("value"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._cur_select = None


def _controls() -> _FormControls:
    p = _FormControls()
    p.feed(_HTML)
    return p


def test_exactly_five_named_controls() -> None:  # C6-5; S3 name-set edit (P2-INV-9, deliberate)
    names = _controls().names
    # S3/FR-02 replaces the frames control with a SECONDS control labelled a maximum. The frames value is
    # computed in JS and posted as max_new_tokens (the wire field); it is NOT a named DOM control, and there
    # is no hidden input for it. Still exactly five named controls (adversarial cases #5/#6).
    assert set(names) == {"input", "instructions", "response_format", "seed", "max_seconds"}
    assert len(names) == 5  # no duplicate/extra named control


def test_response_format_is_wav_only() -> None:  # Q1 / C6-5
    assert _controls().select_options.get("response_format") == ["wav"]


def test_no_reference_to_blocking_route() -> None:  # C6-2 / INV-12
    for asset in (_HTML, _CSS, _JS):
        assert "music_generation" not in asset


def test_attribution_present_in_source() -> None:  # C6-3 (source half)
    assert "MiniMax-Music3" in _HTML


def test_no_external_origin() -> None:
    for asset in (_HTML, _CSS, _JS):
        assert not re.search(r"https?://", asset)
        assert not re.search(r"""(src|href)\s*=\s*["']//""", asset)
    assert "@import" not in _CSS
    assert not re.search(r"""url\(\s*["']?https?:""", _CSS)


def test_guidance_present() -> None:  # generation-form: in-page guidance (E-08, E-18)
    for token in ("[Verse]", "Global Metadata", "Vocal Details", "Arrangement", "5,000", "250", "750", "9000"):
        assert token in _HTML, token


def test_no_html_injection_sink_in_js() -> None:  # INV-8 static guard (C6-9)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in _JS, sink


class _FilterInput(HTMLParser):
    """Locate the S5 library filter input and record whether it carries a `name` attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.found = False
        self.has_name = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "input" and dict(attrs).get("id") == "artifact-filter":
            self.found = True
            self.has_name = "name" in dict(attrs)


def test_filter_input_is_nameless() -> None:  # S5/FR-07: the filter is UI-only, never a 6th named control
    # The client-side filter posts nothing, so #artifact-filter must have NO name — otherwise it would join
    # the named-control set and break test_exactly_five_named_controls (S3's deliberate five-control set).
    p = _FilterInput()
    p.feed(_HTML)
    assert p.found, "filter input #artifact-filter not present in index.html"
    assert not p.has_name, "filter input must have no name attribute (keeps the exactly-five named controls)"
