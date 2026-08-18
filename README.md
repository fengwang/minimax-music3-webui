# MiniMax-Music3-WebUI

Run **MiniMax-Music3** on one local RTX 5090 behind both a MiniMax-cloud-compatible
`POST /v1/music_generation` endpoint and a minimal browser UI, launched by a single `docker compose up`.
Generated audio stays on your own disk; an existing cloud-schema client keeps working by pointing at a
local base URL instead of the cloud.

Powered by **MiniMax-Music3** (fine-tuned from Qwen3-8B). See **[Licence](#licence)** for the attribution,
acceptable-use, revenue-threshold and safeguards obligations that bind any deployment of this software.

---

## What this is

Two containers, launched by one compose file:

| Container | Role |
| --- | --- |
| `minimax-music3-app` | FastAPI: the cloud-compatible API, a single-slot job queue, ffmpeg post-processing, and the static WebUI. Publishes the app port. |
| `minimax-music3-sglang` | The SGLang-Omni GPU engine. Its port is **never** published to the host; only the app reaches it, on the compose network. |

The app also starts and stops the GPU container to free VRAM when idle, over a bind-mounted
`/var/run/docker.sock` confined to a fixed verb allow-list against the one fixed container name (INV-5).

## Requirements

- The host verified for this project: Arch Linux, one **NVIDIA RTX 5090 (32 GB)**, recent NVIDIA driver,
  Docker + Docker Compose, and ffmpeg. A different GPU is out of scope.
- The **MiniMax-Music3 weights on local disk** (the 7-component pipeline directory containing
  `modular_model_index.json`). They are bind-mounted **read-only**; no weight file is ever baked into an
  image (INV-2). The weights are **not** distributed with this repository.

## Quick start (from a clean checkout)

```bash
# download the MiniMax-Music3 weights to a local path, e.g. /data/models/MiniMax-Music3
hf download MiniMaxAI/MiniMax-Music3  --local-dir /data/models/MiniMax-Music3
cp .env.example .env
# Edit .env: set MUSIC3_WEIGHTS_DIR to your real weights path (see the stale-path warning in .env.example).
docker compose -f deploy/docker-compose.yml up -d
```

Both services reach running state with no edit to any file other than `.env`. Open
`http://<host>:8080/` in a browser (replace `<host>` with the machine's LAN address), type a caption and
lyrics, and generate. The first request after an idle period pays a full model reload — the UI shows a
distinct **warming** state while the GPU container starts (there is no sleep mode; stopping the container is
the only way to free VRAM, so every cold start reloads the model — R-05).

Bring it down with `docker compose -f deploy/docker-compose.yml down` (this is also the only way to free
the GPU's VRAM).

## Configuration

`.env.example` defines exactly the variables the compose file reads. Copy it to `.env` and adjust:

| Variable | Default | Purpose |
| --- | --- | --- |
| `MUSIC3_WEIGHTS_DIR` | `/data/models/MiniMax-Music3` | Host weights directory, bind-mounted read-only (INV-2). Must contain `modular_model_index.json`. |
| `MUSIC3_ARTIFACTS_HOST_DIR` | `/data/minimax-music3/artifacts` | Host directory where generated audio + sidecar JSON persist (read-write). |
| `MUSIC3_BIND_ADDR` | `0.0.0.0` | Host interface the app port binds to. `0.0.0.0` = LAN-reachable (see the security notice); `127.0.0.1` = this host only. |
| `MUSIC3_APP_PORT` | `8080` | Host port the app is published on. |

Both `MUSIC3_WEIGHTS_DIR` and `MUSIC3_ARTIFACTS_HOST_DIR` carry a **stale bind-mount warning** in
`.env.example`: Docker silently bind-mounts a wrong or missing path, so verify each path exists first
(R-11).

## Typical Usage with Your Agent

After setting up the docker app, verify the app is accessible from your webui, then send message like this to you agent:

```
At http://YOU_IP_ADDRESS:8080/ I have deployed a miniMax-Music3 WebUI, with which we can generate musics calling APIs.
It is a bit slower with my local 5090, and we might need 3-10 minutes to generate a 6 mininute music.
The WebUI source code is from from https://github.com/fengwang/minimax-music3-webui, and you can clone it to checkout more details on how to use the API, which is under the section "API compatibility" in the README.md file.

For your convenience, I want you clone the official Music Caption Rewrite Skill from https://github.com/MiniMax-AI/MiniMax-Music3.git, and checkout the skill from the path "skills/music-caption-rewriter". You can install this skill and use it to generate music based on a given theme.

After you have installed the skill, we can try to generate a song with the following settings:
- Genre: Eastern Folk Song
- Theme: Moon, River
- Length: 3-5 minutes

Were any problems arise during the process, please ask me immediately.
```


## Acceptance demonstration

The project's definition of success is one owner-run, end-to-end demonstration from a clean checkout
(GATE-S7):

1. `cp .env.example .env` and bring the stack up with the one compose command above.
2. From **another machine on the LAN**, open the WebUI and generate a **full-length** song (about five
   minutes, not a 60-second clip). Playable audio is returned in the browser.
3. From the same machine, generate again through `POST /v1/music_generation`; the response envelope matches
   the cloud schema with `base_resp.status_code` 0.
4. Re-run one request with the same seed and inputs: the audio file is **byte-identical** by checksum
   (seed determinism was measured true; INV-10).
5. Inspect the host artifacts directory: one audio file plus one sidecar JSON per generation, the sidecar
   carrying engine identity/version, every request parameter, the seed, and wall-clock timings (INV-9).
6. Leave the service idle, then send one more request: `nvidia-smi` shows VRAM released while idle, and the
   next request succeeds with **no** manual `docker start` (the app cold-starts the engine itself).

The owner then listens to the full-length result and judges quality — a human judgement (D12), not a
metric. No automated audio-quality score is in scope.

## API compatibility

`POST /v1/music_generation` accepts and returns the MiniMax cloud envelope. `music-3.0` is the **only**
accepted `model` id.

- **Honoured natively:** `model` (`music-3.0`), `prompt` → the engine's caption, `lyrics` → the engine's
  input, `stream: false`, and **both** `output_format` values `hex` (default) and `url`.
- **Honoured via ffmpeg post-processing:** `audio_setting.format` (`wav`/`mp3`/`pcm`), `sample_rate`,
  `bitrate`. `extra_info` is measured from the delivered file, never from a constant.
- **Refused with `base_resp` code `2013` (invalid params), never silently defaulted:** `stream: true`,
  `lyrics_optimizer`, `is_instrumental`, `cover_feature_id`, `audio_url`, `audio_base64`, and every
  `model` id other than `music-3.0` (`music-2.6`, `music-cover`, `music-3.0-free`, `music-2.6-free`,
  `music-cover-free`). A field that is absent or carries its documented cloud default is accepted as a
  no-op. (INV-7.)
- **Two additive optional fields** extend the cloud schema: `seed` (int) and `max_new_tokens` (int, acoustic
  frames at 25 fps, maximum 9000). Cloud clients never send them; their absence leaves cloud behaviour
  unchanged.

The WebUI never calls this blocking route (INV-12); it submits a job, streams progress, and can cancel.

## Behaviour and deliberate divergences from the cloud spec

- **No link expiry, no TTL reaper.** `url` artifacts persist until you delete them — a documented divergence
  from the cloud spec's 24-hour URL expiry (D6). Retention is manual; no artifact is ever deleted by this
  service.
- **Client read timeout.** `POST /v1/music_generation` is blocking. A blocking client must set a read
  timeout of **at least 1200 seconds** on an otherwise idle system (the model authors' own test client
  defaults to `--timeout 1200`; D9, F13). This is a required client read-timeout expectation, **not** a
  measured or promised generation time — no generation-latency figure is published. With concurrency one, a
  queued caller additionally waits out the render ahead of it plus any cold start, so its total wall clock
  can far exceed 1200 s; use the job-submit + progress path if you cannot block.
- **Concurrency is exactly one.** At most one generation runs at any moment (INV-1); a second submission
  queues.
- **Seed determinism holds.** Identical seed, inputs, `max_new_tokens`, and engine version reproduce a
  byte-identical WAV (INV-10), measured true on this stack.

## Engine and backend

The GPU kernel/attention backend is **explicitly set and recorded** — auto-selection is forbidden (INV-3).
It is pinned in the committed serving config `deploy/sglang/pipeline.yaml` (passed to the engine as
`--config`; there is no CLI flag for it), and confirmed in force from the engine's startup merged-config
print on this `sm_120` (Blackwell) card:

- **Autoregressive (Qwen3 backbone) stage:** `attention_backend: triton` — chosen because FlashInfer has
  open sm_120 correctness bugs and `trtllm_mha` is SM100-only (E-11/E-12). The backbone is fp8-quantized
  (`fp8_gemm_runner_backend: triton`) so it stays resident without PCIe offload.
- **Acoustic (flow-matching DIT + DAV) stage:** `attention_backend: torch_sdpa` — sm_120-portable PyTorch
  SDPA; this stage stays fp32 to preserve acoustic quality.

The measured native output of the engine is **32000 Hz, 2 channels, 16-bit** (`pcm_s16le`); this settles the
model folder's internal 32 kHz-vs-44100 contradiction in favour of 32 kHz (the value is measured from a real
generated WAV, not taken from model-card prose — F14/R-08). Cloud `audio_setting.sample_rate` values are
treated as resample targets, and `extra_info.music_sample_rate` reports the file actually produced.

## Operator commands

From the repo root (`Makefile`):

| Command | Effect |
| --- | --- |
| `make build` | Build both images (each `FROM` pinned by digest; no weight file is ever copied in). |
| `make config` | Render and validate the compose file. |
| `make up` / `make down` | Bring the stack up / down (`down` is the only way to free VRAM). |
| `make health` | Poll engine readiness via `/v1/models` (never `/health`, which triggers a real generation — INV-6). |
| `make smoke [FRAMES=n SEED=n]` | Run the authors' end-to-end generation harness inside the GPU container. |
| `make scan` | Fail if any Dockerfile would copy a weight file into an image (INV-2). |

---

## ⚠ Security: unauthenticated and LAN-reachable

**This service has NO authentication.** By owner decision (D5) it binds all interfaces (`0.0.0.0`) by
default so the WebUI is reachable from other machines on your LAN — this is an **accepted risk (R-09)**, not
an oversight. Anyone who can reach the port can generate audio, control the GPU container's lifecycle, and
read every artifact. At startup the app logs exactly this, e.g.:

```
MiniMax-Music3 API listening on 0.0.0.0:8080 — UNAUTHENTICATED (LAN-exposed by owner decision D5; accepted risk R-09)
```

To restrict to this host only, set `MUSIC3_BIND_ADDR=127.0.0.1` in `.env` — no code change needed. Do not
expose the port to an untrusted network. There is no rate limiting, quota, or account model of any kind.

---

## Licence

This source code in this repo is **MIT** (SPDX: `MIT`), but the model weights are **not**.

MiniMax-Music3 is released under the custom **"MiniMax-Music3 COMMUNITY LICENSE"** (no SPDX identifier); the
full text ships with the weights (`LICENSE` in the model directory). Self-hosting is permitted **subject to
all of the following obligations**, which bind this deployment (F16/E-19, R-15):

1. **Prominent attribution.** You must prominently display **"MiniMax-Music3"** on the user interface of any
   commercial product or service built on it. This WebUI shows the string in an always-visible region
   (INV-11).
2. **Acceptable Use Policy (Exhibit A) — 19 categories.** You must comply with the AUP. You must **not** use
   the software or its outputs to: (1) violate any applicable law or regulation; (2) harm yourself or
   others; (3) generate, repurpose or distribute content to harm yourself or others; (4) circumvent or
   bypass safety guardrails; (5) exploit or harm minors; (6) generate or disseminate verifiably false
   information; (7) manufacture false online engagement (e.g. fake reviews); (8) defame, disparage, or
   harass others; (9) generate or disseminate malware; (10) generate or disseminate personally identifiable
   information to harm someone; (11) disseminate machine-generated content in any public environment (e.g.
   bot posts) without clearly and prominently disclosing that it is machine-generated; (12) impersonate
   another person without consent; (13) make high-risk automated decisions in critical domains affecting
   individual safety; (14) violate the social/ethical/moral standards of other cultures; (15) carry out or
   incite violent extremism or terrorism; (16) discriminate against or harm individuals or groups; (17)
   exploit the vulnerabilities of specific populations; (18) use for military purposes; and (19) engage in
   unauthorized or unlicensed professional activity. (MiniMax may update the AUP; the shipped `LICENSE` is
   authoritative.)
3. **Revenue threshold.** You must obtain **separate, prior written authorization** from MiniMax
   (api@minimax.io, subject "MiniMax-Music3 licensing - authorization request") if the aggregate yearly
   revenue from such products/services, across you and your affiliates, exceeds **US$20,000,000** (or
   equivalent).
4. **Safeguards duty.** If you provide a product, service, or hosted service to any third party that lets
   them generate output, you must implement **reasonable and proportionate technical and organizational
   safeguards** to prevent infringing or otherwise unlawful output, and you must **not** knowingly disable,
   weaken, or permit circumvention of those safeguards.

This project performs no automated content classification (D12 scope); the safeguards obligation above is
therefore the operator's responsibility for any multi-user or hosted exposure.

