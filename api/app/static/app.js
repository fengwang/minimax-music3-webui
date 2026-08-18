"use strict";
// MiniMax-Music3 WebUI — single hand-written module, no build step, no framework, no remote import.
// ACD structure: pure Calculations first (no DOM, no fetch), then the Action shell (browser only).
// The pure Calculations are exposed on globalThis.MUSIC3 so they can be unit-tested off-DOM; all DOM
// wiring is guarded behind `typeof document`, so loading this file without a document is inert.
//
// SECURITY (INV-8, C6-9): every server-supplied string reaches the DOM only through textContent / element
// property assignment (.value/.src/.href) — never through an HTML-parsing sink — so markup or quotes in
// lyrics, captions, causes, or filenames render as literal text, never as markup.
(function () {
  // ===================== PURE CALCULATIONS (no DOM, no fetch) =====================
  const CHARS_PER_TOKEN = 4;   // conservative English heuristic; the count is labeled "estimated"
  // Documented prompt limit (E-18). An over-limit prompt is REFUSED before a job is created — the frozen
  // acceptance criterion (session_6_contract.yaml) and project_contract §5 require refusal before dispatch,
  // not merely a warning (resolves codex S6-CR-01; supersedes the earlier two-tier idea).
  const TOKEN_LIMIT = 5000;
  const FRAME_MIN = 1;
  const FRAME_MAX = 9000;      // documented acoustic-frame ceiling (E-08)

  const estimateTokens = (input, instructions) =>
    Math.ceil(((input ? input.length : 0) + (instructions ? instructions.length : 0)) / CHARS_PER_TOKEN);

  const isOverTokenLimit = (est) => est > TOKEN_LIMIT;

  // Accept only a canonical whole number; reject floats, blanks, NaN, and scientific notation.
  const parseWholeNumber = (raw) => {
    const s = String(raw).trim();
    if (!/^-?\d+$/.test(s)) return null;
    const n = Number(s);
    return Number.isInteger(n) ? n : null;
  };

  // ---- duration in SECONDS, labelled a MAXIMUM (FR-02; spec: duration-seconds-control) ----
  const SECONDS_MIN = 10;
  const SECONDS_MAX = 360;          // SECONDS_MAX * FRAMES_PER_SECOND = 9000 = FRAME_MAX (P2-E02)
  const FRAMES_PER_SECOND = 25;     // _FRAMES_PER_SECOND at api/compat/minimax.py:32 (P2-E01)

  // seconds -> frames is EXACT and safe; frames -> seconds is only an upper bound (P2-E03). Clamp into the
  // representable 1..9000 range so the transmitted max_new_tokens is always valid regardless of input.
  const secondsToFrames = (seconds) =>
    Math.min(FRAME_MAX, Math.max(FRAME_MIN, Math.ceil(Number(seconds) * FRAMES_PER_SECOND)));

  const validateSeconds = (raw) => {
    const n = parseWholeNumber(raw);
    return n !== null && n >= SECONDS_MIN && n <= SECONDS_MAX
      ? { ok: true, value: n }
      : { ok: false, message: `maximum length must be a whole number of seconds in ${SECONDS_MIN}..${SECONDS_MAX} (got "${raw}")` };
  };

  const validateSeed = (raw) => {
    const n = parseWholeNumber(raw);
    return n !== null && n >= 0
      ? { ok: true, value: n }
      : { ok: false, message: `seed must be a whole number ≥ 0 (got "${raw}")` };
  };

  // deriveStatus maps (jobStatus, engineState) -> a visible label + CSS kind + whether cancel applies.
  // Keeping it pure makes the three distinct states (Queued/Warming/Generating) and INV-1 (a queued job is
  // never shown as generating) properties of a function, not of timing.
  const deriveStatus = (jobStatus, engineState) => {
    if (!jobStatus) return { kind: "idle", label: "Idle — nothing submitted yet.", canCancel: false };
    if (jobStatus === "running") return { kind: "generating", label: "Generating…", canCancel: true };
    if (jobStatus === "queued") {
      if (engineState === "warming") return { kind: "warming", label: "Warming up the GPU… (typically 71–87 s)", canCancel: true };
      if (engineState === "unavailable") return { kind: "unavailable", label: "Engine unavailable", canCancel: true };
      return { kind: "queued", label: "Queued — waiting for the engine slot", canCancel: true };
    }
    const labels = { succeeded: "Succeeded", failed: "Failed", cancelled: "Cancelled" };
    return { kind: jobStatus, label: labels[jobStatus] || jobStatus, canCancel: false };
  };

  const formatDuration = (seconds) => {
    const s = Math.max(0, Math.round(Number(seconds) || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  // ---- self-calibrating upper-bound ETA (FR-01 SHOULD; spec: eta-estimate) ----
  // history: [{frames, seconds, mtime}] over COMPLETED artifacts — seconds is timings.generation_seconds,
  // frames is request.max_new_tokens. Returns a positive upper-bound estimate (seconds) or null to SUPPRESS
  // (fewer than 3 samples, D-08). The per-frame rate is FITTED from history, never hardcoded: doubling the
  // historical durations doubles the estimate (C3-8), and the stale P2-E07 coefficient is never used. The
  // fit orders by recency and drops the extreme rates so one weird run cannot skew the bound.
  const fitEtaSeconds = (history, frames) => {
    const done = (history || []).filter(
      (h) => Number.isFinite(h.seconds) && h.seconds > 0 && Number.isFinite(h.frames) && h.frames > 0,
    );
    if (done.length < 3) return null;                                      // suppression floor (D-08)
    const recent = done.slice().sort((a, b) => (b.mtime || 0) - (a.mtime || 0)).slice(0, 10);
    let rates = recent.map((h) => h.seconds / h.frames);                   // seconds per requested frame
    if (rates.length >= 5) rates = rates.slice().sort((a, b) => a - b).slice(1, -1);  // drop min & max rate
    const est = Math.max(...rates) * (Number(frames) || 0);                // worst inlier rate -> upper bound
    return est > 0 ? est : null;
  };

  // etaState decides how the estimate is DISPLAYED, as a complete 3-valued pure state (D-S3-4): suppressed
  // (no honest estimate — fewer than 3 samples), bounded (elapsed within the ceiling), exceeded (elapsed
  // passed it). It NEVER counts down to zero: exceeded degrades to an indeterminate label (D-08, FR-01).
  const etaState = (elapsed, estimate) =>
    !(estimate > 0) ? { kind: "suppressed" }
      : elapsed <= estimate ? { kind: "bounded", estimate }
        : { kind: "exceeded", estimate };

  // ---- waveform peaks + seek (FR-03; specs: lazy-waveform, seek-control) ----
  const PEAK_BUCKETS = 1000;   // fixed extraction resolution; drawn dpr-aware, aggregated to canvas width
  const SEEK_STEP_S = 5;       // ArrowLeft/Right increment, seconds (documented; C4-6)
  const SEEK_PAGE_S = 30;      // PageUp/PageDown increment, seconds
  const INT16_SCALE = 32768;   // 2^15: magnitude range of a signed 16-bit PCM sample (peak normaliser)

  // Parse a canonical PCM WAV (RIFF/WAVE) into a view over its 16-bit samples. Returns null on anything that
  // is not 16-bit integer PCM (audioFormat 1), so the Action shell shows a message instead of a blank canvas.
  // No Web Audio decode step (D-11): peaks are a loop over PCM, so no decoded float buffer is created or kept.
  const parseWavPcm = (buf) => {
    if (!buf || buf.byteLength < 44) return null;
    const view = new DataView(buf);
    // 'RIFF' .... 'WAVE' (compared big-endian so the four ASCII bytes read in order)
    if (view.getUint32(0, false) !== 0x52494646 || view.getUint32(8, false) !== 0x57415645) return null;
    let off = 12, fmt = null, dataOff = -1, dataLen = 0;
    while (off + 8 <= view.byteLength) {
      const id = view.getUint32(off, false);
      const size = view.getUint32(off + 4, true);
      const body = off + 8;
      if (id === 0x666d7420 && body + 16 <= view.byteLength) {          // 'fmt '
        fmt = {
          audioFormat: view.getUint16(body, true),
          channels: view.getUint16(body + 2, true),
          sampleRate: view.getUint32(body + 4, true),
          bitsPerSample: view.getUint16(body + 14, true),
        };
      } else if (id === 0x64617461) {                                    // 'data'
        dataOff = body;
        dataLen = Math.min(size, view.byteLength - body);
      }
      off = body + size + (size & 1);                                    // chunks are word-aligned
    }
    if (!fmt || fmt.audioFormat !== 1 || fmt.bitsPerSample !== 16 || dataOff < 0 || dataLen <= 0) return null;
    const channels = fmt.channels > 0 ? fmt.channels : 1;
    const sampleLen = Math.floor(dataLen / 2);
    const samples = dataOff % 2 === 0
      ? new Int16Array(buf, dataOff, sampleLen)                          // zero-copy view (canonical even offset)
      : new Int16Array(buf.slice(dataOff, dataOff + sampleLen * 2));     // rare odd offset: aligned copy
    return { sampleRate: fmt.sampleRate, channels, sampleCount: Math.floor(sampleLen / channels), samples };
  };

  // Downsample interleaved Int16 PCM to `buckets` normalized peak magnitudes (0..1). RATE-AGNOSTIC: a fixed
  // bucket count over ALL samples, so no sample rate is assumed. Max |sample| on channel 0 reads as the
  // symmetric shape of the song. Consumes Int16 directly — no float32 decode is produced or retained.
  const extractPeaks = (samples, channels, buckets) => {
    const out = new Float32Array(buckets > 0 ? buckets : 0);
    const ch = channels > 0 ? channels : 1;
    const frames = Math.floor((samples ? samples.length : 0) / ch);
    if (frames <= 0 || out.length === 0) return out;
    const per = frames / out.length;
    for (let b = 0; b < out.length; b += 1) {
      const start = Math.floor(b * per);
      const end = Math.min(frames, Math.max(start + 1, Math.floor((b + 1) * per)));
      let peak = 0;
      for (let i = start; i < end; i += 1) {
        const v = Math.abs(samples[i * ch]);
        if (v > peak) peak = v;
      }
      out[b] = peak / INT16_SCALE;
    }
    return out;
  };

  // Playhead x for a time. Single source of truth: currentTime & duration come from the <audio> element;
  // clamp so a currentTime past duration cannot draw off-canvas.
  const playheadX = (currentTime, duration, width) =>
    !(duration > 0) || !(width > 0) ? 0
      : Math.min(1, Math.max(0, (Number(currentTime) || 0) / duration)) * width;

  // Seconds for a click at pixel x — inverse of playheadX; clamped into 0..duration.
  const seekTimeForX = (x, width, duration) =>
    !(width > 0) || !(duration > 0) ? 0
      : Math.min(1, Math.max(0, (Number(x) || 0) / width)) * duration;

  // Pure keyboard seek: the new currentTime for a documented key, clamped to 0..duration, or null for any
  // other key (so the handler ignores it and does not preventDefault — e.g. Tab still moves focus).
  const seekTimeForKey = (key, currentTime, duration, step = SEEK_STEP_S, pageStep = SEEK_PAGE_S) => {
    const d = duration > 0 ? duration : 0;
    const t = Number(currentTime) || 0;
    const clamp = (x) => Math.min(d, Math.max(0, x));
    switch (key) {
      case "ArrowRight": return clamp(t + step);
      case "ArrowLeft": return clamp(t - step);
      case "PageUp": return clamp(t + pageStep);
      case "PageDown": return clamp(t - pageStep);
      case "Home": return 0;
      case "End": return d;
      default: return null;
    }
  };

  // Human-readable byte size for the Play affordance so cost is known BEFORE the one fetch (lazy-waveform).
  const formatBytes = (n) => {
    const b = Number(n);
    if (!(b >= 0)) return "";
    if (b < 1024) return `${b} B`;
    const units = ["KiB", "MiB", "GiB"];
    let v = b / 1024, i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(1)} ${units[i]}`;
  };

  // Insert into a Map-based LRU (most-recent last), evicting the oldest beyond `max`. Mutates and returns
  // the caller-owned map; exposed on MUSIC3 so the peak-retention bound (adversarial case #8) is unit-testable.
  const lruSet = (map, key, value, max) => {
    if (map.has(key)) map.delete(key);
    map.set(key, value);
    while (map.size > max) map.delete(map.keys().next().value);
    return map;
  };

  // ---- client-side library filter (FR-07; spec: library-filter) ----
  // Pure predicate: case-insensitive substring; an empty/whitespace query matches every row. Kept pure and
  // exposed on MUSIC3 so the matching rule is unit-testable off-DOM, like the other calculations here.
  const filterMatch = (text, query) => {
    const q = String(query == null ? "" : query).trim().toLowerCase();
    return q === "" ? true : String(text == null ? "" : text).toLowerCase().includes(q);
  };

  const MUSIC3 = {
    estimateTokens, isOverTokenLimit, validateSeconds, secondsToFrames, validateSeed,
    deriveStatus, formatDuration, fitEtaSeconds, etaState,
    parseWavPcm, extractPeaks, playheadX, seekTimeForX, seekTimeForKey, formatBytes, lruSet, filterMatch,
    TOKEN_LIMIT, FRAME_MIN, FRAME_MAX, SECONDS_MIN, SECONDS_MAX, FRAMES_PER_SECOND,
    PEAK_BUCKETS, SEEK_STEP_S, SEEK_PAGE_S,
  };
  if (typeof globalThis !== "undefined") { globalThis.MUSIC3 = MUSIC3; }

  // ===================== ACTION SHELL (browser only) =====================
  if (typeof document === "undefined") { return; }

  const $ = (id) => document.getElementById(id);
  const els = {
    input: $("input"), instructions: $("instructions"), responseFormat: $("response_format"),
    seed: $("seed"), maxSeconds: $("max_seconds"),
    tokenEst: $("token-est"), tokenWarning: $("token-warning"), formError: $("form-error"),
    generate: $("generate"), cancel: $("cancel"),
    status: $("status"), statusCause: $("status-cause"),
    progress: $("progress"), elapsed: $("elapsed"), eta: $("eta"),
    resultEmpty: $("result-empty"), resultDownload: $("result-download"),
    resultDelivered: $("result-delivered"),
    resultParams: $("result-params"), engineBadge: $("engine-badge"),
    artifactList: $("artifact-list"), artifactEmpty: $("artifact-empty"),
    artifactNone: $("artifact-none"), artifactFilter: $("artifact-filter"),
    player: $("player"), playerAudio: $("player-audio"), waveform: $("waveform"),
    playerTitle: $("player-title"), playerTime: $("player-time"), playerError: $("player-error"),
  };

  const TERMINAL = new Set(["succeeded", "failed", "cancelled"]);
  const state = { job: null, engine: null };  // job:{id,status,error?}  engine:{state,cause}
  let eventSource = null;
  let healthTimer = null;
  let elapsedTimer = null;   // local interval driving the elapsed clock (the SSE heartbeat fires no callback)
  let runningAtMs = null;    // client clock latched on the FIRST running event; the timer anchor (§4.7)
  const etaSamples = new Map();  // job_id -> {frames, seconds, mtime}: the ETA fit inputs (spec: eta-estimate)
  const nowMs = () => (typeof performance !== "undefined" && performance.now ? performance.now() : Date.now());
  const limit = TOKEN_LIMIT.toLocaleString("en-US");  // "5,000"

  // ---- consolidated player state (FR-03/FR-04; specs: lazy-waveform, seek-control, persistent-player) ----
  // The <audio> element (els.playerAudio) is the SINGLE SOURCE OF TRUTH for playback position; `player`
  // holds only view state (the retained peaks + canvas 2d context for the loaded artifact). peakCache is a
  // bounded LRU so opening many artifacts cannot grow memory without limit (adversarial case #8); at most
  // one object URL (blob) is alive at a time, revoked when the player switches artifacts.
  const PEAK_CACHE_MAX = 8;
  const PLAYER_GAP_PX = 8;       // gap reserved below the fixed player so the newest row is never covered
  const peakCache = new Map();   // job_id -> Float32Array, LRU by re-insertion order
  const player = { jobId: null, url: null, peaks: null, raf: 0, ctx: null };
  let playerTokens = null;       // {bars, playhead} colour strings, read once from the role tokens

  const setText = (el, text) => { if (el) el.textContent = text == null ? "" : String(text); };
  const show = (el, visible) => { if (el) el.hidden = !visible; };
  const parseData = (ev) => { try { return JSON.parse(ev.data || "{}"); } catch (_e) { return {}; } };

  // ---- live token estimate + two-tier guard ----
  function updateTokenEstimate() {
    const est = estimateTokens(els.input.value, els.instructions.value);
    setText(els.tokenEst, `~${est} estimated tokens (limit ${limit})`);
    if (isOverTokenLimit(est)) {
      setText(els.tokenWarning,
        `Over the ${limit}-token limit: ~${est} estimated tokens — shorten before submitting; the request ` +
        `will be refused (E-18, R-20).`);
      show(els.tokenWarning, true);
    } else {
      show(els.tokenWarning, false);
    }
  }

  // ---- status render (single source for the status region + button state) ----
  function renderStatus() {
    const jobStatus = state.job && state.job.status;
    const engineState = state.engine && state.engine.state;
    const { kind, label, canCancel } = deriveStatus(jobStatus, engineState);
    setText(els.status, label);
    els.status.setAttribute("data-state", kind);
    let cause = "";
    if ((kind === "warming" || kind === "unavailable") && state.engine && state.engine.cause) cause = state.engine.cause;
    if (kind === "failed" && state.job && state.job.error) cause = state.job.error;
    setText(els.statusCause, cause);
    show(els.cancel, canCancel);
    els.generate.disabled = canCancel;  // INV-1: no second submit while a job is in flight
    renderProgress();
  }

  // ---- elapsed timer + progress region (FR-01; spec: two-phase-progress) ----
  // The timer is anchored on the running event (elapsed excludes engine cold start, §4.7) and driven by a
  // LOCAL interval, because the SSE heartbeat is a comment that fires no client callback (§4.2).
  function renderProgress() {
    const generating = !!(state.job && state.job.status === "running");
    show(els.progress, generating);
    if (!generating) return;
    const elapsed = runningAtMs != null ? (nowMs() - runningAtMs) / 1000 : 0;
    setText(els.elapsed, formatDuration(elapsed));
    renderEta(elapsed);
  }
  // The ETA is a STATIC soft ceiling: a fixed upper bound computed from history, shown beside the count-up
  // timer. The decaying quantity is the implicit margin (estimate - elapsed) the timer closes — never a
  // separate shrinking number (PRD §12.2). Once elapsed passes it, it degrades to indeterminate, never 0:00.
  function renderEta(elapsed) {
    const frames = state.job && state.job.params && Number(state.job.params.max_new_tokens);
    const st = etaState(elapsed, fitEtaSeconds([...etaSamples.values()], frames));
    if (st.kind === "bounded") {
      setText(els.eta, `usually done within ~${formatDuration(st.estimate)} — estimated maximum`);
    } else if (st.kind === "exceeded") {       // had an estimate, but elapsed passed it: degrade, don't zero
      setText(els.eta, "longer than the estimated maximum — still generating");
    } else {                                   // suppressed (< 3 samples): no honest estimate to show yet
      setText(els.eta, "still generating…");
    }
    // data-eta collapses the two indeterminate kinds for the DOM contract the ui tests key on.
    els.eta.setAttribute("data-eta", st.kind === "bounded" ? "bounded" : "indeterminate");
  }
  function startElapsed() {
    if (runningAtMs == null) runningAtMs = nowMs();   // latch on the FIRST running (idempotent under replay)
    if (elapsedTimer == null) { renderProgress(); elapsedTimer = setInterval(renderProgress, 250); }
  }
  function stopElapsed() {
    if (elapsedTimer != null) { clearInterval(elapsedTimer); elapsedTimer = null; }
  }

  // ---- watching a job: SSE lifecycle + health poll ----
  function stopWatching() {
    if (eventSource) { eventSource.close(); eventSource = null; }
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
    stopElapsed();  // a terminal event or a new watch stops the local clock (adversarial case #9)
  }

  async function pollHealth() {
    try {
      const res = await fetch("/health", { headers: { accept: "application/json" } });
      if (!res.ok) return;
      const j = await res.json();
      state.engine = { state: j.engine, cause: j.engine_cause || "" };
      setText(els.engineBadge, `engine: ${j.engine}`);
      renderStatus();
    } catch (_e) { /* transient; the next tick retries */ }
  }

  function startWatching(jobId) {
    stopWatching();
    eventSource = new EventSource(`/jobs/${encodeURIComponent(jobId)}/events`);
    for (const type of ["queued", "running", "succeeded", "failed", "cancelled"]) {
      eventSource.addEventListener(type, (ev) => onJobEvent(jobId, type, ev));
    }
    pollHealth();
    healthTimer = setInterval(pollHealth, 1500);
  }

  function onJobEvent(jobId, type, ev) {
    if (!state.job || state.job.id !== jobId) return;
    state.job.status = type;
    if (type === "failed") state.job.error = parseData(ev).error || "generation failed";
    if (type === "running") startElapsed();  // anchor the elapsed timer at zero on running, not on submit
    renderStatus();
    if (type === "succeeded") onSucceeded(jobId);
    if (TERMINAL.has(type)) stopWatching();
  }

  async function onSucceeded(jobId) {
    const url = `/artifacts/${encodeURIComponent(jobId)}/audio.wav`;
    els.resultDownload.href = url;
    show(els.resultDownload, true);
    show(els.resultEmpty, false);
    renderResultParams(state.job && state.job.params);
    const reqMax = state.job && state.job.maxSeconds;  // capture BEFORE the await: a new submit during
    const items = await loadArtifacts();               // loadArtifacts must not mispair this job's output
    const mine = (items || []).find((i) => i.job_id === jobId);
    renderDelivered(reqMax, mine && mine.output);
    // The player is intentionally NOT re-pointed here: a completed generation must not interrupt playback
    // (FR-04, adversarial case #11). The new result is the newest row and is played on an explicit action.
  }

  // Delivered length beside the requested MAXIMUM so the cap-not-target gap is visible (P2-E03); delivered
  // is the backend measurement output.duration_seconds, never recomputed from frames (contract §5). "Maximum"
  // is the shared honesty vocabulary across the duration control, the ETA and this readout (PRD §6).
  function renderDelivered(reqMax, output) {
    if (reqMax == null || !output || output.duration_seconds == null) { show(els.resultDelivered, false); return; }
    const delivered = output.duration_seconds;
    setText(els.resultDelivered,
      `Maximum requested: ${formatDuration(reqMax)} (${reqMax} s) · ` +
      `Delivered: ${formatDuration(delivered)} (${Math.round(delivered)} s)`);
    show(els.resultDelivered, true);
  }

  // Echo the submitted params in the result view — as TEXT (C6-9: markup renders literally, never injected).
  function renderResultParams(params) {
    els.resultParams.replaceChildren();
    if (!params) { show(els.resultParams, false); return; }
    const field = (label, value) => {
      const wrap = document.createElement("div");
      const key = document.createElement("strong");
      setText(key, `${label}: `);
      const val = document.createElement("span");
      setText(val, value);  // textContent — a <script>/&/quote in the caption stays literal text
      wrap.append(key, val);
      return wrap;
    };
    els.resultParams.append(
      field("seed", String(params.seed)),
      field("caption", params.instructions),
      field("lyrics", params.input),
    );  // requested-maximum vs delivered length is shown by renderDelivered(), not as a raw frame count
    show(els.resultParams, true);
  }

  // ---- submit (client-side guards BEFORE any request; C6-6/C6-7) ----
  async function safeDetail(res) {
    try {
      const j = await res.json();
      return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch (_e) { return ""; }
  }

  async function submit() {
    setText(els.formError, "");
    show(els.formError, false);
    const secs = validateSeconds(els.maxSeconds.value);
    const seed = validateSeed(els.seed.value);
    const est = estimateTokens(els.input.value, els.instructions.value);
    const problems = [];
    if (!els.input.value.trim()) problems.push("input (lyrics) is required");
    if (!els.instructions.value.trim()) problems.push("instructions (caption) is required");
    if (!seed.ok) problems.push(seed.message);
    if (!secs.ok) problems.push(secs.message);
    if (isOverTokenLimit(est)) {  // over the documented limit → refuse before any job is created
      problems.push(`prompt over the ${limit}-token limit: ~${est} estimated tokens — refused before submission`);
    }
    if (problems.length) {  // refused before any job is created; no request reaches the engine
      setText(els.formError, problems.join(" · "));
      show(els.formError, true);
      return;
    }
    const body = {
      input: els.input.value, instructions: els.instructions.value,
      seed: seed.value, max_new_tokens: secondsToFrames(secs.value),
    };  // response_format is deliberately omitted — the native route forbids it and the engine is wav-only
    els.generate.disabled = true;
    try {
      const res = await fetch("/jobs", {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body),
      });
      if (!res.ok) {
        const detail = await safeDetail(res);
        setText(els.formError, `submission refused (HTTP ${res.status})${detail ? ": " + detail : ""}`);
        show(els.formError, true);
        els.generate.disabled = false;
        return;
      }
      const job = await res.json();
      // Stop discarding the timestamps POST /jobs returns (P2-E19): started_at is the documented fallback
      // anchor for the elapsed timer if a mid-job page load never observes the running event (contract §5).
      state.job = {
        id: job.id, status: job.status, params: body, maxSeconds: secs.value,
        submitted_at: job.submitted_at, started_at: job.started_at, ended_at: job.ended_at,
      };
      state.engine = null;
      show(els.resultDownload, false);
      els.resultParams.replaceChildren();
      show(els.resultParams, false);
      show(els.resultDelivered, false);
      setText(els.resultEmpty, "No result yet.");
      show(els.resultEmpty, true);
      runningAtMs = null;   // a new job re-anchors the elapsed timer on its own running event
      renderStatus();
      startWatching(job.id);
    } catch (e) {
      setText(els.formError, `submission failed: ${e}`);
      show(els.formError, true);
      els.generate.disabled = false;
    }
  }

  // ---- cancel (Cancelled shown ONLY after the server confirms terminal cancelled; C6-11) ----
  async function cancel() {
    if (!state.job) return;
    const jobId = state.job.id;
    setText(els.status, "Cancelling…");
    els.status.setAttribute("data-state", "cancelling");
    els.cancel.disabled = true;
    try {
      const res = await fetch(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
      if (res.ok) {
        const view = await res.json();  // returned only after the runner transitioned the job (terminal)
        if (state.job && state.job.id === jobId && view.status) state.job.status = view.status;
      } else if (res.status === 409) {  // already terminal server-side — reconcile from the job view
        const v = await (await fetch(`/jobs/${encodeURIComponent(jobId)}`)).json();
        if (state.job && state.job.id === jobId) state.job.status = v.status;
      }
    } catch (_e) {
      /* leave the label as "Cancelling…"; the SSE terminal event will reconcile the true state */
    } finally {
      els.cancel.disabled = false;
      renderStatus();  // shows "Cancelled" only if the status is now the server-confirmed terminal state
    }
  }

  // ---- artifact library (newest-first from GET /artifacts; seed + re-run from the sidecar) ----
  const sidecarCache = new Map();
  function fetchSidecar(item) {
    if (sidecarCache.has(item.job_id)) return sidecarCache.get(item.job_id);
    const p = (async () => {
      const res = await fetch(item.sidecar_url, { headers: { accept: "application/json" } });
      if (!res.ok) throw new Error(`sidecar HTTP ${res.status}`);
      return res.json();
    })();
    sidecarCache.set(item.job_id, p);
    p.catch(() => sidecarCache.delete(item.job_id));  // do not cache a failed fetch
    return p;
  }

  function appendMeta(container, label, value) {
    const span = document.createElement("span");
    const k = document.createElement("span");
    k.className = "k";
    setText(k, `${label}: `);
    const v = document.createElement("span");
    v.className = "v";
    setText(v, value);
    span.append(k, v);
    container.append(span);
    return v;  // caller may update this value node later (e.g. the seed after the sidecar loads)
  }

  function rowError(li, message) {
    let err = li.querySelector(".row-error");
    if (!err) {
      err = document.createElement("div");
      err.className = "row-error";
      li.append(err);
    }
    setText(err, message);  // a server refusal (e.g. a 404'd path) surfaced as text, never a blank (C6-10)
  }

  function renderArtifactRow(item) {
    const li = document.createElement("li");
    li.dataset.jobId = item.job_id;   // row identity for the player + the lazy-waveform ui tests

    const meta = document.createElement("div");
    meta.className = "meta";
    const out = item.output || {};
    appendMeta(meta, "duration", out.duration_seconds != null ? formatDuration(out.duration_seconds) : "—");
    appendMeta(meta, "rate", out.sample_rate != null ? `${out.sample_rate} Hz` : "—");
    appendMeta(meta, "engine", (item.engine && item.engine.name) || "—");
    appendMeta(meta, "job", item.job_id);
    const seedV = appendMeta(meta, "seed", "…");

    const actions = document.createElement("div");
    actions.className = "actions";
    const play = document.createElement("button");
    play.type = "button";
    play.className = "play";
    const size = out.byte_size != null ? formatBytes(out.byte_size) : "";
    setText(play, size ? `▶ Play · ${size}` : "▶ Play");  // size shown BEFORE any fetch (lazy-waveform, C4-4)
    play.addEventListener("click", () => activateArtifact(item));
    const dl = document.createElement("a");
    dl.href = item.audio_url;
    dl.setAttribute("download", "");
    setText(dl, "Download");
    const rerun = document.createElement("button");
    rerun.type = "button";
    setText(rerun, "Re-run this seed");
    rerun.addEventListener("click", () => rerunFromSidecar(item, li));
    actions.append(play, dl, rerun);

    const preview = document.createElement("div");
    preview.className = "preview";
    setText(preview, "…");

    li.append(meta, preview, actions);
    els.artifactList.append(li);

    fetchSidecar(item)
      .then((sc) => {
        const req = (sc && sc.request) || {};
        setText(seedV, req.seed != null ? String(req.seed) : "?");
        // Caption/lyrics preview rendered as TEXT (C6-9): markup in the caption never becomes markup here.
        const caption = req.instructions || req.input || "";
        setText(preview, caption ? caption.slice(0, 200) : "(no caption recorded)");
        // A caption match becomes possible only once the sidecar resolves; re-apply an ACTIVE filter so it
        // reflects the now-loaded text. Guarded on a non-empty query to avoid a needless pass at rest.
        if (els.artifactFilter && els.artifactFilter.value.trim() !== "") applyFilter();
      })
      .catch(() => { setText(seedV, "?"); setText(preview, "(sidecar unavailable)"); });
  }

  async function rerunFromSidecar(item, li) {
    let sc;
    try { sc = await fetchSidecar(item); } catch (_e) { sc = null; }
    if (!sc || !sc.request) { rowError(li, "cannot re-run: sidecar unavailable"); return; }
    const r = sc.request;
    els.input.value = r.input != null ? r.input : "";              // .value assignment — never HTML
    els.instructions.value = r.instructions != null ? r.instructions : "";
    els.seed.value = r.seed != null ? String(r.seed) : "0";
    // The control is now seconds; reproduce the recorded frames as their seconds equivalent, clamped to the
    // accepted range. Reproduction is approximate in frames by the accepted unit change (see design.md).
    const reFrames = r.max_new_tokens != null ? r.max_new_tokens : 7500;
    els.maxSeconds.value = String(Math.min(SECONDS_MAX, Math.max(SECONDS_MIN, Math.round(reFrames / FRAMES_PER_SECOND))));
    els.responseFormat.value = "wav";
    updateTokenEstimate();
    els.input.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // Build the ETA fit inputs from the SAME cached sidecar fetches the rows use: timings.generation_seconds
  // from the listing joined to request.max_new_tokens from the sidecar. fetchSidecar is cached per job_id,
  // so this adds NO HTTP request beyond the per-row sidecar fetches already made (P2-E11).
  async function collectEtaSamples(items) {
    await Promise.all((items || []).map(async (item) => {
      const gen = Number(item.timings && item.timings.generation_seconds);
      if (!(gen > 0)) return;
      let sc;
      try { sc = await fetchSidecar(item); } catch (_e) { return; }
      const frames = Number(sc && sc.request && sc.request.max_new_tokens);
      if (!(frames > 0)) return;
      etaSamples.set(item.job_id, { frames, seconds: gen, mtime: Number(item.mtime) || 0 });
    }));
  }

  async function loadArtifacts() {
    let items = [];
    try {
      const res = await fetch("/artifacts", { headers: { accept: "application/json" } });
      if (res.ok) items = await res.json();
    } catch (_e) { /* show empty on failure */ }
    els.artifactList.replaceChildren();  // clear the list via a DOM API (no HTML-parsing sink)
    show(els.artifactEmpty, items.length === 0);
    etaSamples.clear();
    for (const item of items) renderArtifactRow(item);  // preserve the server's newest-first order
    applyFilter();  // re-apply a live filter to the freshly rendered rows (DOM-only, no request)
    await collectEtaSamples(items);  // populate the ETA fit from already-fetched data (no new request)
    if (state.job && state.job.status === "running") renderProgress();  // refresh a live ETA once fitted
    return items;   // callers (onSucceeded, the ETA fit) reuse this listing — no extra request (P2-E11)
  }

  // ---- client-side library filter Action (FR-07): DOM-only, issues NO request. The sticky player is a
  // separate element, so hiding a row never touches playback (adversarial: filter-hides-the-playing-row). ----
  function applyFilter() {
    if (!els.artifactList) return;
    const q = els.artifactFilter ? els.artifactFilter.value : "";
    const rows = els.artifactList.children;
    let visible = 0;
    for (const li of rows) {
      const match = filterMatch(li.textContent, q);
      li.hidden = !match;
      if (match) visible += 1;
    }
    // Explicit no-matches state, distinct from the empty-library state; never both visible at once.
    show(els.artifactNone, rows.length > 0 && visible === 0 && String(q).trim() !== "");
  }

  // ---- consolidated persistent player: lazy waveform + seek (FR-03/FR-04) ----
  // Canvas colours are read once from the role tokens (never a literal): the waveform FILL is the decorative
  // --accent-fill; the playhead is the higher-contrast --accent-ink.
  function playerColors() {
    if (!playerTokens) {
      const s = getComputedStyle(document.documentElement);
      playerTokens = {
        bars: s.getPropertyValue("--accent-fill").trim(),
        playhead: s.getPropertyValue("--accent-ink").trim(),
      };
    }
    return playerTokens;
  }

  // Reserve bottom space equal to the fixed bar's MEASURED height so the newest row is never covered (FR-04).
  // Re-measured whenever the bar's content (title, error line) may have changed its height, because a bar
  // that grew AFTER the padding was fixed would let the newest row slip behind it.
  function reservePlayerSpace() {
    if (!document.body || !els.player) return;
    document.body.style.paddingBottom = els.player.hidden ? "" : `${els.player.offsetHeight + PLAYER_GAP_PX}px`;
  }

  function showPlayer(visible) {
    show(els.player, visible);
    reservePlayerSpace();
  }

  function playerError(message) {
    setText(els.playerError, message);   // textContent → escaped (INV-8); never a blank canvas (adversarial #12)
    show(els.playerError, !!message);
    reservePlayerSpace();                // an error line changes the bar height; re-reserve so no row is covered
  }

  // Now-playing label as TEXT (escaped): job id, delivered length, engine.
  function playerLabel(item) {
    const secs = item.output && item.output.duration_seconds;
    const dur = secs != null ? ` · ${formatDuration(secs)}` : "";
    const eng = item.engine && item.engine.name ? ` · ${item.engine.name}` : "";
    return `${item.job_id}${dur}${eng}`;
  }

  function cachePeaks(jobId, peaks) {
    lruSet(peakCache, jobId, peaks, PEAK_CACHE_MAX);  // bounded LRU (tested via MUSIC3.lruSet); most-recent last
    return peaks;
  }

  // Hand the SAME fetched bytes to the <audio> element via a blob URL, so playback needs no second network
  // request (C4-4). Exactly one object URL lives at a time: the previous one is revoked here.
  function swapAudioSource(buf) {
    if (player.url) URL.revokeObjectURL(player.url);
    player.url = URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
    els.playerAudio.src = player.url;
    els.playerAudio.play().catch(() => { /* autoplay may be blocked; the native controls stay operable */ });
  }

  // The ONE audio-body request happens here, on an explicit action; its bytes feed BOTH peak extraction and
  // playback. A re-activation reuses cached peaks (bounded LRU) rather than re-parsing; otherwise peaks come
  // from a direct Int16 parse (no AudioBuffer), and the raw buffer is released to GC after the blob is built
  // — only the tiny peaks arrays and the single playing blob survive.
  async function activateArtifact(item) {
    showPlayer(true);
    playerError("");
    let buf;
    try {
      const res = await fetch(item.audio_url);
      if (!res.ok) {
        playerError(`Couldn't load audio — the server refused or the file is missing (HTTP ${res.status}).`);
        return;   // a failed activation must not repoint the title or disturb the currently playing track
      }
      buf = await res.arrayBuffer();
    } catch (e) {
      playerError(`Couldn't load audio — ${e}.`);
      return;
    }
    let peaks = peakCache.get(item.job_id);
    if (peaks) {
      cachePeaks(item.job_id, peaks);   // LRU touch: re-mark most-recently-used without re-parsing the bytes
    } else {
      const parsed = parseWavPcm(buf);
      if (parsed) peaks = cachePeaks(item.job_id, extractPeaks(parsed.samples, parsed.channels, PEAK_BUCKETS));
      else playerError("Couldn't read the waveform — the audio could not be decoded (empty or not 16-bit PCM).");
    }
    player.peaks = peaks || null;
    player.jobId = item.job_id;
    setText(els.playerTitle, playerLabel(item));   // title set only after a successful fetch (no mismatch)
    reservePlayerSpace();                            // re-measure now the (possibly wrapping) title is set
    swapAudioSource(buf);
    sizeCanvas();
    drawWaveform();
    syncAria();
  }

  function sizeCanvas() {
    const c = els.waveform;
    const rect = c.getBoundingClientRect();
    const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
    const w = Math.max(1, Math.round(rect.width * dpr));
    const h = Math.max(1, Math.round(rect.height * dpr));
    if (c.width !== w) c.width = w;
    if (c.height !== h) c.height = h;
  }

  // Draw the waveform + playhead. The playhead is DERIVED from audio.currentTime/duration every frame — the
  // element is the single source of truth, there is no separate clock. data-playhead-x reflects that derived
  // position for the ui test (C4-5); it is an output, never an input.
  function drawWaveform() {
    const c = els.waveform;
    if (!c || !c.getContext) return;
    if (!player.ctx) player.ctx = c.getContext("2d");
    const ctx = player.ctx;
    if (!ctx) return;
    // Size is set on activation / loadedmetadata / resize, NOT per frame, so the rAF path does no layout read.
    const w = c.width, h = c.height, mid = h / 2;
    ctx.clearRect(0, 0, w, h);
    const peaks = player.peaks;
    if (peaks && peaks.length) {
      ctx.fillStyle = playerColors().bars;
      for (let x = 0; x < w; x += 1) {                       // aggregate the fixed peaks to device px columns
        const b0 = Math.floor((x / w) * peaks.length);
        const b1 = Math.max(b0 + 1, Math.floor(((x + 1) / w) * peaks.length));
        let p = 0;
        for (let b = b0; b < b1 && b < peaks.length; b += 1) if (peaks[b] > p) p = peaks[b];
        const barH = Math.max(1, p * (h - 2));
        ctx.fillRect(x, mid - barH / 2, 1, barH);            // symmetric bar around the vertical centre
      }
    }
    const dur = els.playerAudio ? els.playerAudio.duration : 0;
    const x = playheadX(els.playerAudio ? els.playerAudio.currentTime : 0, dur, w);
    if (dur > 0) {
      ctx.fillStyle = playerColors().playhead;
      ctx.fillRect(Math.min(w - 2, Math.max(0, x - 1)), 0, 2, h);
    }
    c.dataset.playheadX = String(Math.round(x));   // output-only reflection of currentTime for C4-5
  }

  // Sync the slider's ARIA + the time readout FROM the element. Called on discrete events (metadata, seek,
  // ~4 Hz timeupdate) and after a seek — never inside the rAF path, so assistive tech is not spammed at
  // frame rate (the visual playhead still updates every frame in drawWaveform).
  function syncAria() {
    const c = els.waveform;
    const a = els.playerAudio;
    const dur = a && a.duration > 0 ? a.duration : 0;
    const cur = a ? a.currentTime : 0;
    c.setAttribute("aria-valuemin", "0");
    c.setAttribute("aria-valuemax", String(Math.round(dur)));
    c.setAttribute("aria-valuenow", String(Math.round(cur)));
    c.setAttribute("aria-valuetext", `${formatDuration(cur)} of ${formatDuration(dur)}`);
    setText(els.playerTime, `${formatDuration(cur)} / ${formatDuration(dur)}`);
  }

  // A rAF loop only while playing (smooth); event-driven redraws cover seek/pause and a programmatic
  // currentTime change (C4-5) — all reading the element, so no parallel clock is ever created.
  function scheduleDraw() {
    if (player.raf) return;
    const tick = () => {
      player.raf = 0;
      drawWaveform();
      if (els.playerAudio && !els.playerAudio.paused && !els.playerAudio.ended) {
        player.raf = requestAnimationFrame(tick);
      }
    };
    player.raf = requestAnimationFrame(tick);
  }

  function seekToPointer(e) {
    const dur = els.playerAudio.duration;
    if (!(dur > 0)) return;
    const rect = els.waveform.getBoundingClientRect();
    els.playerAudio.currentTime = seekTimeForX(e.clientX - rect.left, rect.width, dur);  // write the element
    els.waveform.focus();
    drawWaveform();
    syncAria();
  }

  function seekByKey(e) {
    const t = seekTimeForKey(e.key, els.playerAudio.currentTime, els.playerAudio.duration);
    if (t == null) return;                 // not a seek key → let it bubble (Tab still moves focus)
    e.preventDefault();
    els.playerAudio.currentTime = t;       // single source of truth; the playhead redraws from it
    drawWaveform();
    syncAria();
  }

  function main() {
    els.generate.addEventListener("click", submit);
    els.cancel.addEventListener("click", cancel);
    els.input.addEventListener("input", updateTokenEstimate);
    els.instructions.addEventListener("input", updateTokenEstimate);
    // Client-side library filter (FR-07): each input event re-runs applyFilter over rows already in the DOM.
    if (els.artifactFilter) els.artifactFilter.addEventListener("input", applyFilter);
    // Ctrl+Enter (Cmd+Enter on macOS) submits from either textarea through the SAME submit() + validation as
    // the button; plain Enter keeps inserting a newline. Guarded by els.generate.disabled so an in-flight job
    // (single slot) and a rapid double-press cannot create a second job (FR-06; adversarial cases #10/#11).
    const onCtrlEnter = (e) => {
      if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();
      if (els.generate.disabled) return;
      submit();
    };
    els.input.addEventListener("keydown", onCtrlEnter);
    els.instructions.addEventListener("keydown", onCtrlEnter);
    // Player wiring: the <audio> element drives every redraw, so the canvas is a VIEW plus a seek controller
    // over it — pointer/keyboard seek WRITE audio.currentTime and every redraw READS it (single source of
    // truth). A refused/truncated body is surfaced by activateArtifact as escaped text, not a blank canvas.
    if (els.waveform && els.playerAudio) {
      const redrawWithAria = () => { drawWaveform(); syncAria(); };
      els.waveform.addEventListener("pointerdown", seekToPointer);
      els.waveform.addEventListener("keydown", seekByKey);
      els.playerAudio.addEventListener("timeupdate", redrawWithAria);  // ~4 Hz: aria at event rate, not 60 Hz
      els.playerAudio.addEventListener("seeked", redrawWithAria);
      els.playerAudio.addEventListener("loadedmetadata", () => { sizeCanvas(); redrawWithAria(); });
      els.playerAudio.addEventListener("play", scheduleDraw);
      els.playerAudio.addEventListener("playing", scheduleDraw);
      els.playerAudio.addEventListener("error", () => {
        if (els.playerAudio.getAttribute("src")) playerError("Playback failed — the audio could not be decoded.");
      });
      window.addEventListener("resize", () => { if (player.jobId) { sizeCanvas(); drawWaveform(); } });
    }
    updateTokenEstimate();
    renderStatus();
    pollHealth();     // populate the engine badge on load (one shot; the interval starts with a job)
    loadArtifacts();
  }

  document.addEventListener("DOMContentLoaded", main);
})();
