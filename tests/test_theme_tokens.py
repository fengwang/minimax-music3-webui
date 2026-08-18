"""Machine-asserts the S1 sage light-theme token floors (spec: token-vocabulary.md,
accessibility-floors.md; PRD FR-08; project_contract §6).

PURE STDLIB ONLY — no Playwright, no colour library, no ``import pytest`` (S2 adds the
browser layer). ACD structure like ``app.js``: pure Calculations (parse / WCAG /
literal-sweep) first, then the assert-shell (``test_*`` functions read files and assert).

STRUCTURAL LIMIT handed to S2: this reads *declarations*, not the resolved cascade. A token
can clear a floor in isolation and still be overridden by a later rule, and ``:focus-visible``
*rendered* geometry (the offset ring on the mint button) cannot be seen here. S2 re-asserts
those against computed styles in a real browser.
"""

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "api" / "app" / "static"
_CSS = (_STATIC / "style.css").read_text()
_HTML = (_STATIC / "index.html").read_text()
_JS = (_STATIC / "app.js").read_text()

# ===================== PURE CALCULATIONS (no I/O) =====================

_BEGIN, _END = "/* TOKENS:BEGIN */", "/* TOKENS:END */"


def _strip_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def token_block(css: str) -> str:
    """The text between the two fixed sentinels. Absent sentinels are a hard failure."""
    i, j = css.find(_BEGIN), css.find(_END)
    if i == -1 or j == -1 or j <= i:
        return ""
    return css[i + len(_BEGIN):j]


def rules_outside_block(css: str) -> str:
    """Stylesheet with the whole sentinel block removed, THEN comments stripped.

    Order matters: the sentinels are themselves comments, so stripping first would delete
    them and leave the token block's own literals looking like leaks. Remove the block from
    the raw text first (sentinels still intact), then strip the remaining comments.
    """
    i, j = css.find(_BEGIN), css.find(_END)
    if i != -1 and j != -1 and j > i:
        css = css[:i] + css[j + len(_END):]
    return _strip_comments(css)


def parse_tokens(block: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip()
            for m in re.finditer(r"--([a-z0-9-]+)\s*:\s*([^;]+);", block)}


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    h = color.strip().lstrip("#")
    if len(h) in (3, 4):
        h = "".join(ch * 2 for ch in h[:3])
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    r, g, b = hex_to_rgb(color)
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def ratio(fg: str, bg: str) -> float:
    a, b = luminance(fg), luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


# The 148 CSS named colours + ``transparent`` — the notations C1-4's grep cannot see.
_NAMED = frozenset("""aliceblue antiquewhite aqua aquamarine azure beige bisque black
blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse chocolate coral
cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgrey
darkgreen darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon
darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink
deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro
ghostwhite gold goldenrod gray grey green greenyellow honeydew hotpink indianred indigo ivory
khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
lightgoldenrodyellow lightgray lightgrey lightgreen lightpink lightsalmon lightseagreen
lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen
magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream
mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid
palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum
powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen
seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan
teal thistle tomato transparent turquoise violet wheat white whitesmoke yellow
yellowgreen""".split())  # noqa: SIM905 - a readable word list beats a 148-item literal


def colour_literals_outside_block(css: str) -> list[str]:
    """Every hex / rgb() / hsl() / named-colour occurrence outside the token block.

    Lower-cased first ON PURPOSE: `White`, `Gainsboro` and `RGB(` are colour literals too, and the
    shell C1-4 grep does not scan named colours at all, so this closes a notation C1-4 cannot see.
    """
    outside = rules_outside_block(css).lower()
    found = re.findall(r"#[0-9a-f]{3,8}\b", outside)
    found += re.findall(r"\b(?:rgba?|hsla?)\(", outside)
    for name in _NAMED:
        if re.search(r"(?<![\w-])" + name + r"(?![\w-])", outside):
            found.append(name)
    return found


SURFACES = ("surface-page", "surface-panel", "surface-raised", "surface-sunken", "surface-field")
ANCHORS = {"surface-page": "#DCE4DE", "surface-panel": "#E8EDE9",
           "accent-fill": "#65B192", "accent-ink": "#2F6F5B"}
# The full role vocabulary S1 must install (§6). A parse below this count is a broken parser.
EXPECT_TOKENS = (
    list(SURFACES) + ["ink", "ink-muted", "border-control", "border-subtle",
                      "accent-fill", "accent-ink", "state-ok", "state-warn", "state-warming",
                      "shadow-raised", "shadow-raised-sm", "shadow-sunken",
                      "radius-sm", "radius-md", "radius-pill"]
    + [f"space-{n}" for n in range(1, 7)]
)
# Every data-state app.js writes (deriveStatus kinds + cancel()'s "cancelling").
DATASTATES = ("idle", "queued", "warming", "unavailable", "generating",
              "succeeded", "failed", "cancelled", "cancelling")
# Classes app.js creates at runtime via className (must stay styled after the rewrite).
DYN_CLASSES = ("meta", "k", "v", "row-error", "actions", "preview")

_TOKENS = parse_tokens(token_block(_CSS))


def _status_rules(css: str) -> tuple[dict[str, str], dict[str, str]]:
    """(base color per data-state, ::before content per data-state)."""
    base = {m.group(1): m.group(2)
            for m in re.finditer(r'\.status\[data-state="([^"]+)"\]\s*\{([^}]*)\}', css)}
    before = {m.group(1): m.group(2)
              for m in re.finditer(r'\.status\[data-state="([^"]+)"\]::before\s*\{([^}]*)\}', css)}
    return base, before


def _class_styled(css: str, cls: str) -> bool:
    return re.search(r"\." + re.escape(cls) + r"(?![\w-])", css) is not None


# ===================== ASSERT-SHELL (one test per floor, named) =====================

def test_token_block_and_parser_are_not_vacuous():
    assert _BEGIN in _CSS and _END in _CSS, "TOKENS:BEGIN/END sentinels must both exist"
    missing = [t for t in EXPECT_TOKENS if t not in _TOKENS]
    assert not missing, f"missing §6 tokens: {missing}"
    assert len(_TOKENS) >= len(EXPECT_TOKENS), (
        f"parsed only {len(_TOKENS)} tokens (< {len(EXPECT_TOKENS)}); parser found too little")


def test_anchors_exact():
    for name, want in ANCHORS.items():
        assert _TOKENS.get(name, "").lower() == want.lower(), f"anchor --{name} must be exactly {want}"


# Each text-role token -> the surfaces it is actually painted on (mirrors the rules). Which surface a
# colour-only rule INHERITS is the cascade; re-checking that against computed styles is S2's obligation.
TEXT_ON = {
    "ink": ("surface-page", "surface-panel", "surface-raised", "surface-field", "surface-sunken"),
    "ink-muted": ("surface-page", "surface-panel", "surface-raised"),
    "accent-ink": ("surface-panel", "surface-raised"),   # generating status, brand, links (normal size)
    "state-ok": ("surface-panel",),
    "state-warn": ("surface-panel", "surface-raised"),    # .warn/status on panel; .row-error on raised
    "state-warming": ("surface-panel",),
}


def test_floor1_body_and_muted_text_contrast():
    pairs = 0
    for tok, surfs in TEXT_ON.items():
        for surf in surfs:
            r = ratio(_TOKENS[tok], _TOKENS[surf])
            assert r >= 4.5, f"floor 1: --{tok} on --{surf} = {r:.2f} < 4.5:1"
            pairs += 1
    assert pairs >= 14, f"floor 1 asserted only {pairs} text/surface pairs"


def test_floor1_local_color_on_background_pairs():
    # Any rule that sets BOTH color and background from tokens is checkable with no cascade knowledge.
    # This is the guard that would have caught a chip/button/field painting text below 4.5:1 on its fill.
    checked = 0
    for block in re.findall(r"\{([^}]*)\}", rules_outside_block(_CSS)):
        c = re.search(r"(?<![\w-])color:\s*var\(--([a-z0-9-]+)\)", block)
        b = re.search(r"(?<![\w-])background(?:-color)?:\s*var\(--([a-z0-9-]+)\)", block)
        if c and b and c.group(1) in _TOKENS and b.group(1) in _TOKENS:
            r = ratio(_TOKENS[c.group(1)], _TOKENS[b.group(1)])
            assert r >= 4.5, f"floor 1 (local): --{c.group(1)} on --{b.group(1)} = {r:.2f} < 4.5:1"
            checked += 1
    assert checked >= 1, "expected at least one rule setting both color and background from tokens"


def test_floor2_large_text_contrast():
    for surf in ("surface-panel", "surface-page"):
        r = ratio(_TOKENS["accent-ink"], _TOKENS[surf])
        assert r >= 3.0, f"floor 2: brand --accent-ink on --{surf} = {r:.2f} < 3:1"


def test_floor3_control_boundary_contrast():
    for surf in SURFACES:
        r = ratio(_TOKENS["border-control"], _TOKENS[surf])
        assert r >= 3.0, f"floor 3: --border-control vs --{surf} = {r:.2f} < 3:1"


def test_floor4_focus_indicator():
    # "against the SURFACE": the ring colour clears 3:1 against every surface token.
    for surf in SURFACES:
        r = ratio(_TOKENS["accent-ink"], _TOKENS[surf])
        assert r >= 3.0, f"floor 4: focus ring --accent-ink vs --{surf} = {r:.2f} < 3:1"
    m = re.search(r":focus-visible\s*\{([^}]*)\}", _CSS)
    assert m, "a :focus-visible rule must exist"
    body = m.group(1)
    # width + colour from the `outline:` shorthand (not `outline-offset:`), token-order-independent.
    decl = re.search(r"(?<![\w-])outline:\s*([^;}]+)", body)
    assert decl and "var(--accent-ink)" in decl.group(1), "focus ring must use --accent-ink"
    width = re.search(r"(\d+(?:\.\d+)?)px", decl.group(1))
    assert width and float(width.group(1)) >= 2.0, "focus outline must be >= 2px thick"
    # "against the CONTROL": accent-ink vs the mint primary fill is only ~2.33:1 (asserted below), so a
    # positive outline-offset is the DELIBERATE stand-in — it lifts the ring onto the surface (checked
    # above). Whether that offset ring renders visibly around the mint button is handed to S2's browser.
    assert ratio(_TOKENS["accent-ink"], _TOKENS["accent-fill"]) < 3.0, \
        "premise: ring-vs-mint-fill is the sub-3:1 case the offset compensates for"
    off = re.search(r"outline-offset:\s*(\d+(?:\.\d+)?)px", body)
    assert off and float(off.group(1)) > 0, "focus ring must declare a positive outline-offset"


def _visible_glyph(content: str) -> str:
    """Drop separators only — literal whitespace and the \\00a0 / \\0020 escape forms — so a
    whitespace-only cue (invisible) does not count as a differentiator."""
    return re.sub(r"\\0*a0|\\0*20|\s", "", content, flags=re.IGNORECASE)


def test_floor5_no_state_conveyed_by_hue_alone():
    _, before = _status_rules(_CSS)
    glyphs = []
    for st in DATASTATES:
        assert st in before, f"floor 5: [data-state={st}] needs a ::before non-colour cue"
        c = re.search(r'content:\s*"([^"]*)"', before[st])
        assert c, f"floor 5: [data-state={st}]::before has no content string"
        g = _visible_glyph(c.group(1))
        assert g, f"floor 5: [data-state={st}]::before is whitespace-only (an invisible cue)"
        glyphs.append(g)
    assert len(set(glyphs)) == len(glyphs), f"floor 5: ::before glyphs not distinct: {glyphs}"


def test_floor6_no_colour_literal_outside_token_block():
    leaks = colour_literals_outside_block(_CSS)
    assert not leaks, f"floor 6: colour literals outside the token block: {leaks}"


def test_links_route_colour_through_a_token():
    # A bare `a { }` rule must set colour from a token, so no link (e.g. #result-download, which is not
    # inside .artifacts) falls back to the UA-default blue — colour reaches the DOM only via a token
    # (D-05 / P2-INV-3). A parser can't see UA defaults, so it guards the presence of the routing rule.
    for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", rules_outside_block(_CSS)):
        if sel.strip() == "a":
            assert re.search(r"color:\s*var\(--[a-z0-9-]+\)", body), "link `a` colour must use a token"
            return
    raise AssertionError("no bare `a { }` rule — links would fall back to the UA-default colour (D-05)")


def test_accent_fill_is_fill_only():
    # every declaration that references the fill anchor must be a fill property
    for m in re.finditer(r"([a-z-]+)\s*:\s*[^;{}]*var\(--accent-fill\)", _CSS):
        prop = m.group(1)
        assert prop in ("background", "background-color", "fill"), (
            f"--accent-fill used in non-fill property '{prop}' (fills only, D-09)")


def test_min_contrast_pair_count():
    # a fresh, order-independent recomputation: proves the suite really exercises pairs
    pairs = [ratio(_TOKENS[t], _TOKENS[s]) for t in ("ink", "ink-muted", "border-control",
             "accent-ink") for s in SURFACES]
    assert len(pairs) >= 20, f"contrast pair set collapsed to {len(pairs)} (parser found too little)"


def test_wcag_helper_can_pass_and_fail():
    assert round(ratio("#000000", "#FFFFFF"), 2) == 21.0, "black/white must be 21:1"
    assert ratio("#777777", "#888888") < 4.5, "a known-bad pair must be flagged below 4.5:1"


def test_appjs_hooks_preserved():
    ids = set(re.findall(r'\$\("([^"]+)"\)', _JS))
    assert ids, "expected app.js to reference element ids"
    for i in sorted(ids):
        assert f'id="{i}"' in _HTML, f"app.js hook id='{i}' missing from index.html"
    for cls in DYN_CLASSES:
        assert _class_styled(_CSS, cls), f"class '.{cls}' app.js writes is no longer styled"
    base, before = _status_rules(_CSS)
    styled_states = set(base) | set(before)
    for st in DATASTATES:
        assert st in styled_states, f"data-state '{st}' app.js writes is no longer styled"
        assert f'"{st}"' in _JS, f"expected data-state literal \"{st}\" still in app.js"


def test_single_media_query_and_no_color_scheme():
    assert _CSS.count("@media") == 1, "exactly one @media (the 880px collapse) must remain"
    assert "max-width: 880px" in _CSS or "max-width:880px" in _CSS, "the 880px collapse must exist"
    assert not re.search(r"color-scheme", _CSS, re.IGNORECASE), "no (prefers-)color-scheme (D-04)"
