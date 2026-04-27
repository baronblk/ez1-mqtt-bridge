# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Multi-stage build for ez1-mqtt-bridge.
#
# Stage 1 (builder)  — python:3.12-slim + uv: resolves the locked dependency
#                      tree, populates /app/.venv with pre-compiled bytecode,
#                      and copies the source.
#
# Stage 2 (runtime)  — python:3.12-slim with a non-root user. uv, the wheel
#                      cache, and build artefacts stay in the builder stage
#                      so they never reach the runtime layer.
#
# Why python:3.12-slim and not gcr.io/distroless/python3-debian12:
#   distroless/python3-debian12 ships Debian 12's system Python (3.11), and
#   this codebase requires >= 3.12 (asyncio.TaskGroup features land here, and
#   pyproject pins requires-python = ">=3.12"). Slim keeps the attack surface
#   small (~50 MB compressed base) without an interpreter mismatch. A move to
#   a distroless base with a bundled 3.12 runtime is a Phase-10 hardening
#   item, not a Phase-7 blocker.
#
# Image-size target (project NFR-2): <= 80 MB compressed.
# ---------------------------------------------------------------------------

ARG PYTHON_IMAGE=python:3.12-slim

# --- Builder stage --------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

# Pin uv to a known-good minor; uv reads uv.lock from the repo so the lock
# format compatibility window matters. Tag is intentionally hard-coded
# rather than threaded through an ARG: BuildKit's --from= reference does
# not substitute global ARGs, and a pinned tag is the right level of
# friction for a security-sensitive build dependency.
COPY --from=ghcr.io/astral-sh/uv:0.10 /uv /usr/local/bin/uv

WORKDIR /app

# uv sync settings:
#   UV_LINK_MODE=copy        — avoid hardlink complaints on cross-FS layers
#   UV_COMPILE_BYTECODE=1    — pre-compile .pyc so distroless-style runtimes
#                              have nothing to do at first import
#   UV_PYTHON_DOWNLOADS=never — don't fetch a managed interpreter; use the
#                              one already in the slim image
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# Two-step install for layer-cache friendliness:
# (1) deps only (no project) — invalidated only when uv.lock or pyproject.toml
#     change, which is much less frequent than source edits.
# (2) source + project install — invalidated on every source change.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# README.md and LICENSE are referenced by pyproject.toml's project metadata
# (readme = "README.md", license-files = ["LICENSE"]); hatchling validates
# both before producing the project wheel during the second sync.
COPY README.md LICENSE ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# --- Runtime stage --------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# Non-root user (UID/GID 65532 mirrors the distroless-nonroot convention so
# bind-mounted volumes are interchangeable if we move to a distroless base
# in Phase 10).
RUN groupadd --gid 65532 nonroot && \
    useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin nonroot

WORKDIR /app

COPY --from=builder --chown=nonroot:nonroot /app/.venv /app/.venv
COPY --from=builder --chown=nonroot:nonroot /app/src /app/src

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OCI image labels per the project spec (NFR-9 / Phase-7 deliverable).
LABEL org.opencontainers.image.title="ez1-mqtt-bridge" \
      org.opencontainers.image.description="MQTT bridge for the APsystems EZ1-M micro inverter" \
      org.opencontainers.image.source="https://github.com/baronblk/ez1-mqtt-bridge" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.authors="René Süß <baronblk@gmail.com>"

USER nonroot

# Healthcheck: poll the bridge's own /metrics endpoint. A 200 response
# implies (a) the aiohttp server is up, (b) the TaskGroup orchestrator
# has reached run_service's main body, and (c) the metric pipeline is
# wired and rendering -- a single round-trip covers liveness + readiness
# + functional correctness. The check uses python's stdlib urllib because
# slim has no curl by default and pulling one would bloat the image.
#
# Importantly, this must NOT fail when the EZ1 inverter is offline at
# night: the bridge stays healthy (bridge_up=1, /metrics responds) even
# while availability=offline -- that is the intended split.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:9100/metrics', timeout=4).status == 200 else 1)"]

EXPOSE 9100

ENTRYPOINT ["python", "-m", "ez1_bridge"]
CMD ["run"]
