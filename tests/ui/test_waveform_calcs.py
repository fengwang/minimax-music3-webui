"""FR-03 pure Calculations (specs: lazy-waveform, seek-control), exercised off-DOM via globalThis.MUSIC3.

No JS runtime exists outside the browser, so these run as `ui` tests through page.evaluate; MUSIC3 stays
DOM-free (no document, no fetch), which is what makes them unit tests of the calculations, not the shell.
"""
from __future__ import annotations

from computed_style import assert_rendered


def test_extract_peaks_shape_and_loudness(page, base_url) -> None:
    """extractPeaks returns `buckets` normalized values (0..1); a louder region peaks higher (rate-agnostic:
    a fixed bucket count over all samples, no sample rate assumed)."""
    page.goto(base_url)
    assert_rendered(page)
    out = page.evaluate(
        "() => { const s = new Int16Array(800);"
        " for (let i = 0; i < 400; i++) s[i] = 30000;"   # loud first half, silent second half
        " const p = MUSIC3.extractPeaks(s, 1, 4);"
        " return [p.length, p[0] > p[3], p[0] <= 1, p[3]]; }"
    )
    assert out == [4, True, True, 0]


def test_seek_time_for_key_is_clamped(page, base_url) -> None:
    """seekTimeForKey: ArrowRight/Left ±5, PageUp/Down ±30, Home->0, End->duration, else null; clamped."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        "() => [MUSIC3.seekTimeForKey('ArrowRight', 10, 100), MUSIC3.seekTimeForKey('ArrowLeft', 2, 100),"
        " MUSIC3.seekTimeForKey('Home', 50, 100), MUSIC3.seekTimeForKey('End', 5, 100),"
        " MUSIC3.seekTimeForKey('PageUp', 10, 100), MUSIC3.seekTimeForKey('PageDown', 10, 100),"
        " MUSIC3.seekTimeForKey('x', 5, 100)]"
    )
    assert r == [15, 0, 0, 100, 40, 0, None]


def test_seek_time_for_x_playhead_and_bytes(page, base_url) -> None:
    """seekTimeForX / playheadX map through duration and width and clamp; formatBytes is human-readable."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        "() => [MUSIC3.seekTimeForX(50, 100, 120), MUSIC3.playheadX(60, 120, 100),"
        " MUSIC3.formatBytes(38400000), MUSIC3.seekTimeForX(999, 100, 120),"
        " MUSIC3.playheadX(999, 120, 100)]"
    )
    assert r[0] == 60 and r[1] == 50
    assert r[2].endswith("MiB")
    assert r[3] == 120 and r[4] == 100  # clamped: x beyond width -> full duration / full width


def test_parse_wav_pcm_roundtrip(page, base_url) -> None:
    """parseWavPcm accepts a canonical 16-bit PCM WAV built in JS and reports its rate/channels; a
    too-short / non-RIFF buffer returns null (drives the visible error path, not a blank canvas)."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        """() => {
            // build a tiny 2-channel 16-bit PCM WAV: 8 frames at 8000 Hz
            const frames = 8, ch = 2, rate = 8000, n = frames * ch * 2;
            const buf = new ArrayBuffer(44 + n), v = new DataView(buf);
            const put4 = (o, s) => { for (let i = 0; i < 4; i++) v.setUint8(o + i, s.charCodeAt(i)); };
            put4(0, 'RIFF'); v.setUint32(4, 36 + n, true); put4(8, 'WAVE');
            put4(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, ch, true);
            v.setUint32(24, rate, true); v.setUint32(28, rate * ch * 2, true);
            v.setUint16(32, ch * 2, true); v.setUint16(34, 16, true);
            put4(36, 'data'); v.setUint32(40, n, true);
            for (let i = 0; i < frames * ch; i++) v.setInt16(44 + i * 2, i * 100, true);
            const ok = MUSIC3.parseWavPcm(buf);
            const bad = MUSIC3.parseWavPcm(new ArrayBuffer(8));
            return [ok ? ok.sampleRate : null, ok ? ok.channels : null, ok ? ok.samples.length : null, bad];
        }"""
    )
    assert r == [8000, 2, 16, None]


def test_lru_set_bounds_and_evicts_oldest(page, base_url) -> None:
    """The peak-retention bound (adversarial case #8): lruSet keeps at most `max` entries, evicting the
    oldest, and re-inserting an existing key marks it most-recently-used. cachePeaks uses this exact fn."""
    page.goto(base_url)
    assert_rendered(page)
    r = page.evaluate(
        """() => {
            const m = new Map();
            for (let i = 0; i < 10; i++) MUSIC3.lruSet(m, 'k' + i, i, 8);   // insert 10 into a max-8 LRU
            const afterFill = [m.size, m.has('k0'), m.has('k1'), m.has('k9')];
            MUSIC3.lruSet(m, 'k2', 2, 8);                                   // touch an existing key
            return afterFill.concat([m.size, [...m.keys()].pop()]);
        }"""
    )
    assert r == [8, False, False, True, 8, "k2"]  # bounded to 8; k0/k1 evicted; touch keeps size, k2 newest
