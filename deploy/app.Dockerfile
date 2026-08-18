# syntax=docker/dockerfile:1
# App image (S7 finalisation) — FastAPI cloud-compatible API + single-slot job queue + ffmpeg transcode
# + the static WebUI. Shared-file ownership (project_contract §9): the app service was added to compose in
# S5; its build input (this file) is created in S7.
#
# LEAN, torch-free, CLI-free:
#   * runtime deps are only fastapi / uvicorn[standard] / httpx (pyproject [project]); no model runtime
#     ever runs in the app process (project_contract §4 prohibition);
#   * the lifecycle controller speaks httpx over the bind-mounted /var/run/docker.sock (api/lifecycle/
#     controller.py, base URL "http://docker"), so NO docker binary is baked — unlike the reference
#     api.Dockerfile's CLI controller;
#   * ffmpeg/ffprobe ARE required (api/transcode/ffmpeg.py shells out to them for S4 post-processing);
#   * NO weights are COPY'd (INV-2) — weights are the GPU service's runtime read-only mount, not the app's;
#   * every image is pinned by immutable digest (INV-4); no floating tag.
# Build context = repo ROOT (deploy/docker-compose.yml sets app.build.context: ..), so `COPY api/` and the
# manifests resolve. Runs as root: the docker socket is root-equivalent by R-10 regardless of uid, and the
# real control is INV-5's committed verb allow-list + single fixed container name, not the process user.

# python:3.12-slim (matches requires-python >=3.12,<3.13), pinned by digest.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS runtime
# UV_NO_CACHE keeps uv's wheel cache out of the layer; UV_PYTHON_DOWNLOADS=never forces uv to use the
# digest-pinned base's Python instead of fetching an unpinned managed interpreter at build (INV-4).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_NO_CACHE=1 UV_PYTHON_DOWNLOADS=never

# ffmpeg/ffprobe for the S4 transcode path. apt package pinning is not practical and is not required by
# INV-4 (which governs images and git/PyPI sources); deploy/sglang.Dockerfile installs `git` the same way.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# uv pinned by immutable digest for a frozen, lockfile-exact install (INV-4).
COPY --from=ghcr.io/astral-sh/uv@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /usr/local/bin/uv

WORKDIR /app
# Manifests first for layer caching. package=false → uv installs deps only, never the app itself.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Only the app source — never the repo root, weights, or tests (INV-2; narrow COPY).
COPY api/ ./api/
ENV PYTHONPATH=/app/api
EXPOSE 8080
# create_app is a FACTORY (api/app/main.py) → --factory. Bind 0.0.0.0 INSIDE the container; the LAN vs
# loopback host binding is controlled by compose `ports:` (${MUSIC3_BIND_ADDR}). The app echoes
# MUSIC3_BIND_ADDR in its INV-13 startup banner.
CMD ["/app/.venv/bin/uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
