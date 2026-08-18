# syntax=docker/dockerfile:1
# Session 1 (S1) — controlled SGLang-Omni engine image for MiniMax-Music3 on RTX 5090 (sm_120).
#
# Axis A = A3 (controlled build), chosen over A1/A2 — see docs/session_1/brainstorming.md:
#   * base pinned by immutable digest (INV-4); no personal-namespace image (unlike A1's
#     hongccc flashinfer-cache and A2's hongccc/sglang-omni:dev, R-02);
#   * sglang-omni pinned by immutable commit SHA and baked at build — no runtime `git clone`
#     of a mutable branch (unlike the upstream docker/Dockerfile, which floats `main`);
#   * NO weights are COPY'd (INV-2) — weights are a runtime read-only mount.
# The winning digest is recorded in docs/session_1_measurements.md and re-resolved before GATE-S1.

# lmsysorg/sglang @ 687efca = tag nightly-dev-cu13-20260803-12eadf86 (CUDA 13, Blackwell-era). This
# is the base the upstream sglang-omni docker/Dockerfile pins; it matches sglang-omni's dependency
# set (sglang==0.5.16), so the base's CUDA-matched runtime is reused rather than churned.
ARG SGLANG_DIGEST=sha256:687efca081e85f4e3126456ff389b1af515fc08a604de4c61f947f531963aba7
FROM lmsysorg/sglang@${SGLANG_DIGEST}

# sglang-omni pinned by immutable commit. NOTE: PyPI sglang-omni==0.1.1 (2026-08-08) ships 18 model
# packages but NOT minimax_music3 (verified against the wheel), so Music3 requires a source install
# at a commit that includes it. 68abc7e is main HEAD at S1 time and ships sglang_omni/models/
# minimax_music3/. flashinfer pinned for parity with upstream; on sm_120 the AR stage is forced to
# `triton` (deploy/sglang/pipeline.yaml), so flashinfer is not on the hot path.
ARG SGLANG_OMNI_REF=68abc7eec59ff7b0dce484cb501ffbbb338f9e46
ARG FLASHINFER_VERSION=0.6.14
# TORCH_CUDA_ARCH_LIST is defensive: sglang-omni is a pure-Python wheel, but a transitive dep could
# compile; 12.0 is this card's compute capability (sm_120).
ARG TORCH_CUDA_ARCH_LIST=12.0
# FLASHINFER_WORKSPACE_BASE gives flashinfer 0.6.14 a JIT workspace (upstream parity); on sm_120 it
# JIT-compiles its cubins on first use because we deliberately skip upstream's SM89/SM90a cubin cache.
ENV PYTHONUNBUFFERED=1 TORCH_CUDA_ARCH_LIST=${TORCH_CUDA_ARCH_LIST} \
    FLASHINFER_WORKSPACE_BASE=/root FLASHINFER_JIT_DEBUG=0

# git for the pinned checkout; the base already ships uv + the CUDA/torch/sglang runtime.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Install deps from the pinned pyproject (mirrors the upstream recipe known to resolve on this base),
# then the package itself --no-deps so the base's CUDA-matched torch/sglang are not re-resolved. UCX
# (upstream's multi-GPU comms build) is intentionally omitted: S1 is single-GPU colocated.
RUN git clone --filter=blob:none https://github.com/sgl-project/sglang-omni.git /opt/sglang-omni \
    && git -C /opt/sglang-omni checkout ${SGLANG_OMNI_REF} \
    && uv pip install --system --break-system-packages --no-build-isolation -r /opt/sglang-omni/pyproject.toml \
    && uv pip install --system --break-system-packages --no-deps /opt/sglang-omni \
    && uv pip install --system --break-system-packages --no-deps --reinstall flashinfer-python==${FLASHINFER_VERSION} \
    && python3 -m pip uninstall -y flashinfer-cubin flashinfer-jit-cache

# Clear the base ENTRYPOINT so the compose `command` (sgl-omni serve ...) is the whole command and
# is not appended as args to a base launcher (reference-repo pattern).
ENTRYPOINT []
