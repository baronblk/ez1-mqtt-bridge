# ez1-mqtt-bridge

[![CI](https://github.com/baronblk/ez1-mqtt-bridge/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/baronblk/ez1-mqtt-bridge/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://www.mypy-lang.org/static/mypy_badge.svg)](http://mypy-lang.org/)

Async Python service that bridges the **APsystems EZ1-M** micro inverter's local
HTTP API to MQTT, with native Home Assistant auto-discovery and Prometheus metrics.

> **Status:** in development. Initial release `v0.1.0` is tracked under
> [milestones](https://github.com/baronblk/ez1-mqtt-bridge/milestones). The
> active development branch is `develop`; `main` mirrors the latest tagged release.

## Features (target for `v0.1.0`)

- Polls the EZ1 local API on TCP/8050 every 20 s (configurable).
- Publishes a structured JSON state topic plus flat per-metric topics, retained.
- Sends Home Assistant MQTT discovery on first successful poll (11 sensors,
  4 binary sensors).
- Accepts MQTT command topics for `setMaxPower` and `setOnOff` with result topics.
- Exposes Prometheus metrics on `:9100/metrics`.
- Survives nightly inverter offline windows, MQTT disconnects, and bridge restarts
  via LWT and exponential backoff.
- Multi-arch Docker image (`linux/amd64`, `linux/arm64`), `python:3.12-slim`
  runtime as a non-root user. Image size approx. 50 MB compressed.

## Tech stack

Python 3.12+ · `httpx` · `aiomqtt` · `pydantic` v2 · `structlog` ·
`prometheus-client` · `aiohttp` · `uv` · `ruff` · `mypy --strict` · `pytest`.

## Documentation

- [`docs/_reference/apsystems-ez1-local-api.md`](docs/_reference/apsystems-ez1-local-api.md)
  — full reference for all seven EZ1 endpoints, including verified payloads and
  empirical edge cases.
- `docs/architecture.md`, `docs/mqtt-topics.md`, `docs/home-assistant.md` —
  populated in Phase 9.

## Container deployment

```bash
# Copy and edit the env template
cp .env.example .env
$EDITOR .env

# Bring up the bridge (uses ghcr.io/baronblk/ez1-mqtt-bridge once Phase 10 publishes)
docker compose up -d

# Tail logs
docker compose logs -f bridge

# Shut down cleanly (LWT triggers + explicit availability=offline publish)
docker compose down
```

The `/metrics` endpoint binds to `0.0.0.0:9100` inside the container and is
forwarded to `127.0.0.1:9100` on the host -- intended for a Prometheus scraper
on the same host or co-located in the `ez1-bridge-net` Docker network.
Healthcheck pulls `/metrics` once every 30 s and stays green even when the
inverter is night-offline.

To build the image locally instead of pulling:

```bash
docker build -t ez1-mqtt-bridge:local .
docker run --rm ez1-mqtt-bridge:local --version
```

## Development

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
# Install dependencies into the project venv
uv sync

# Run the quality gates locally (matches CI)
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest

# Install pre-commit hooks (once per clone)
uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg
```

> **macOS + exFAT note:** if your project lives on an exFAT-formatted drive,
> set `UV_PROJECT_ENVIRONMENT=$HOME/Library/Caches/ez1-mqtt-bridge-venv` before
> running `uv sync`. Apple Double resource forks on exFAT break wheel
> installation for some packages (e.g. bandit) when the venv lives on the same
> filesystem.

## License

MIT — see [LICENSE](LICENSE).
