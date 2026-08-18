"""S4 static guards: the waveform is hand-rolled, so nothing is vendored (D-11 declines D-02's permission)
and no Web Audio decode API is used (adversarial case #7 — peaks are a direct PCM parse, never a decoded
float copy). Stdlib-only, no browser — runs under `uv run pytest -m "not gpu"`.
"""

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "api" / "app" / "static"


def test_no_vendored_static_files() -> None:
    """D-11: the D-02 vendoring permission stays unused, so api/app/static/vendor/ is absent or empty."""
    vendor = _STATIC / "vendor"
    files = [p for p in vendor.rglob("*") if p.is_file()] if vendor.exists() else []
    assert files == [], f"vendored files present but D-11 declines vendoring: {files}"


def test_no_web_audio_decode_api_in_app_js() -> None:
    """The waveform reads PCM directly; it never decodes into an AudioBuffer, so no fully-decoded float
    copy of a ~38 MB artifact is ever created or retained (adversarial case #7)."""
    js = (_STATIC / "app.js").read_text(encoding="utf-8")
    for token in ("decodeAudioData", "AudioContext"):
        assert token not in js, f"{token!r} in app.js — peaks must be a direct PCM parse, not a decode"
