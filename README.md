# ez1-mqtt-bridge

[![CI](https://github.com/baronblk/ez1-mqtt-bridge/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/baronblk/ez1-mqtt-bridge/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/baronblk/ez1-mqtt-bridge?include_prereleases&sort=semver)](https://github.com/baronblk/ez1-mqtt-bridge/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)

Async Python service that bridges the **APsystems EZ1-M** micro
inverter's local HTTP API to MQTT, with native Home Assistant
auto-discovery and Prometheus metrics.

## Features

- **Local-only.** Talks to the EZ1's HTTP API on TCP/8050; no cloud
  dependencies and no third-party telemetry.
- **State on MQTT.** Structured JSON state topic plus 16 flat
  per-metric topics, all retained so a fresh subscriber gets the
  current snapshot immediately.
- **Home Assistant auto-discovery.** 11 sensors and 4 binary sensors
  appear automatically as one device card on first connect; refreshed
  every 24 h to track firmware-version changes.
- **Bidirectional control.** Accepts `set/max_power` and `set/on_off`
  MQTT commands with three-layer validation (topic / payload / range)
  and structured result payloads with stable error codes for HA
  automations to match against.
- **Read-back verify.** A `setMaxPower` write is followed by a 2-second
  read-back; the result topic surfaces a `verify_mismatch` event if
  the inverter silently rejected the value. Configurable via
  `EZ1_BRIDGE_SETMAXPOWER_VERIFY`.
- **Prometheus on `/metrics`.** Eleven metric families covering
  bridge liveness, per-channel power and energy gauges, alarm bits,
  API request histograms with WLAN-tuned buckets, MQTT publish/
  reconnect counters.
- **Resilient.** Survives nightly inverter offline windows
  (`availability=offline`, no crash), MQTT disconnects (LWT-driven
  graceful shutdown), and bridge restarts. Explicit `offline` publish
  on graceful shutdown so HA's availability badge does not lie.
- **Container-native.** Multi-arch image (`linux/amd64`,
  `linux/arm64`) on `python:3.12-slim`, runs as a non-root user,
  ~50 MB compressed. `/metrics`-based healthcheck with stdlib
  `urllib` -- no extra dependencies.

## Documentation

| Document | Purpose |
|----------|---------|
| [`docs/architecture.md`](docs/architecture.md) | Repository layout, CI/CD workflows, branch protection |
| [`docs/mqtt-topics.md`](docs/mqtt-topics.md) | Every published / subscribed / discovery topic with payload schemas |
| [`docs/home-assistant.md`](docs/home-assistant.md) | Integration guide with copy-paste automation examples |
| [`docs/api-reference.md`](docs/api-reference.md) | EZ1 endpoint summary + firmware compatibility |
| [`docs/troubleshooting.md`](docs/troubleshooting.md) | Multi-VLAN setup, EZ1 hardware quirks, common failure-mode diagnoses |
| [`docs/_reference/apsystems-ez1-local-api.md`](docs/_reference/apsystems-ez1-local-api.md) | Canonical local-API reference (verified payloads + edge cases) |

## Container deployment

```bash
# Copy and edit the env template
cp .env.example .env
$EDITOR .env

# Bring up the bridge
docker compose up -d

# Tail logs
docker compose logs -f bridge

# Shut down cleanly (explicit availability=offline publish before disconnect)
docker compose down
```

The `/metrics` endpoint binds to `0.0.0.0:9100` inside the container
and is forwarded to `127.0.0.1:9100` on the host -- intended for a
Prometheus scraper on the same host or co-located in the
`ez1-bridge-net` Docker network. The healthcheck polls `/metrics`
every 30 s and stays green when the inverter is night-offline (the
bridge stays healthy; the inverter is what's missing, surfaced via
`availability=offline`).

To build the image locally instead of pulling:

```bash
docker build -t ez1-mqtt-bridge:local .
docker run --rm ez1-mqtt-bridge:local --version
```

## Configuration

All settings flow through environment variables prefixed with
`EZ1_BRIDGE_`. Copy [`.env.example`](.env.example) to `.env` and
edit. The required minimum is `EZ1_BRIDGE_EZ1_HOST` and
`EZ1_BRIDGE_MQTT_HOST`; everything else has a sensible default.

| Variable | Default | Purpose |
|----------|---------|---------|
| `EZ1_BRIDGE_EZ1_HOST` | (required) | Inverter IP or hostname |
| `EZ1_BRIDGE_EZ1_PORT` | `8050` | Inverter HTTP port |
| `EZ1_BRIDGE_POLL_INTERVAL` | `20` | Seconds between poll cycles |
| `EZ1_BRIDGE_REQUEST_TIMEOUT` | `5` | Per-request HTTP timeout, seconds |
| `EZ1_BRIDGE_SETMAXPOWER_VERIFY` | `true` | Read-back verify after setMaxPower writes |
| `EZ1_BRIDGE_MQTT_HOST` | (required) | MQTT broker IP or hostname |
| `EZ1_BRIDGE_MQTT_PORT` | `1883` | MQTT broker port |
| `EZ1_BRIDGE_MQTT_USER` | _empty_ | MQTT username (optional) |
| `EZ1_BRIDGE_MQTT_PASSWORD` | _empty_ | MQTT password (optional, treated as `SecretStr`) |
| `EZ1_BRIDGE_MQTT_BASE_TOPIC` | `ez1` | Topic root |
| `EZ1_BRIDGE_MQTT_DISCOVERY_PREFIX` | `homeassistant` | HA discovery prefix |
| `EZ1_BRIDGE_METRICS_BIND` | `0.0.0.0` | `/metrics` bind address |
| `EZ1_BRIDGE_METRICS_PORT` | `9100` | `/metrics` port |
| `EZ1_BRIDGE_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `EZ1_BRIDGE_LOG_FORMAT` | `auto` | `auto` (TTY → text, else JSON), `json`, `text` |

## CLI

The container's entrypoint is `python -m ez1_bridge`, with two
subcommands plus `--version`:

```bash
# Run the bridge service (default)
python -m ez1_bridge run

# Read-only health check (the same path the Docker HEALTHCHECK uses
# in spirit, but querying the EZ1 directly)
python -m ez1_bridge probe --host 192.168.3.24 --json

# Print version and exit
python -m ez1_bridge --version
```

## Development

Requires Python 3.12+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                              # install deps into .venv
uv run ruff check .                  # lint
uv run ruff format --check .         # format check
uv run mypy src tests                # type check
uv run pytest                        # run all tests (~339 tests, ~20 s)
uv run pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg
```

Integration tests under `tests/integration/` spin up an
`eclipse-mosquitto:2.0.20` container via `testcontainers`; they skip
automatically if Docker is unavailable.

> **macOS + exFAT note:** if your project lives on an exFAT-formatted
> drive, set
> `UV_PROJECT_ENVIRONMENT=$HOME/Library/Caches/ez1-mqtt-bridge-venv`
> before running `uv sync`. Apple Double resource forks on exFAT break
> wheel installation for some packages (e.g. bandit) when the venv
> lives on the same filesystem.

## License

MIT — see [LICENSE](LICENSE).
