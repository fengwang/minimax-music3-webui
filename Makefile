# Session 1 (S1) — operator commands for the sglang engine. Run from the repo root.
# Shared-file ownership (project_contract §9): Makefile created in S1, finalised in S7.
# GPU targets (up/health/smoke) require this host's RTX 5090; config/scan are host-only.
.RECIPEPREFIX = >
.PHONY: help build config up down health smoke scan

COMPOSE ?= docker compose -f deploy/docker-compose.yml
FRAMES ?= 250
SEED ?= 0
TIMEOUT ?= 3600            # client read timeout (s); >= the ~1150 s a full 9000-frame song needs (S1 R-06/E-16)

help:
> @echo "build | config | up | down | health | smoke [FRAMES=n SEED=n] | scan"

# ── Build both images via compose (sglang GPU engine + app; each service's build context per compose). ──
# Finalised in S7: C7-3 requires BOTH minimax-music3-sglang:local and minimax-music3-app:local to build.
build:
> $(COMPOSE) build

# ── Render (deterministic host-only gate) ──
config:
> $(COMPOSE) config

# ── Up / down (manual lifecycle; `down` is the only way to free VRAM — F11/R-05) ──
up:
> $(COMPOSE) up -d
down:
> $(COMPOSE) down

# ── Readiness: /v1/models ONLY, never /health (INV-6, E-15). Port unpublished → poll inside. ──
# Uses python (guaranteed present) so no dependency on curl in the base image. Non-zero exit until ready.
health:
> $(COMPOSE) exec -T sglang python -c "import urllib.request as u; r=u.urlopen('http://127.0.0.1:8000/v1/models', timeout=5); print(r.status, r.read().decode()[:300])"

# ── Smoke: the authors' harness (E-16) from the :ro weights mount, run inside the container. ──
# WAV written to a writable in-container path (weights are read-only). `make smoke FRAMES=9000 SEED=0`.
smoke:
> $(COMPOSE) exec -T sglang bash -lc 'mkdir -p /workspace/out && python /models/MiniMax-Music3/scripts/end_to_end/minimax_ttm_test.py --server-url http://127.0.0.1:8000 --seed $(SEED) --max-frames $(FRAMES) --timeout $(TIMEOUT) --out /workspace/out/s$(SEED)_f$(FRAMES).wav'

# ── Safety scan: INV-2 — no weight file may be COPY'd/ADDed into any image under deploy/. ──
scan:
> @if grep -REn '^[[:space:]]*(COPY|ADD)[[:space:]].*\.(safetensors|pt|pth|ckpt|bin|gguf)([[:space:]]|$$)' deploy; then \
>   echo "weight-copy: FOUND"; exit 1; else echo "weight-copy: clean"; fi
