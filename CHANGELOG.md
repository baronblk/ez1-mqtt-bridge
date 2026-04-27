# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-04-27

Initial release of the ez1-mqtt-bridge service.

### Added

#### Inverter integration

- Async HTTP client for the APsystems EZ1-M local API on TCP/8050,
  covering all seven endpoints (`getDeviceInfo`, `getOutputData`,
  `getMaxPower`, `setMaxPower`, `getAlarm`, `getOnOff`, `setOnOff`).
- Explicit retry classifier: timeouts and 5xx retry with exponential
  backoff (1 s / 2 s / 4 s, capped at 300 s, three attempts);
  `ConnectError` and 4xx fail fast.
- Connection-pooled `httpx.AsyncClient` reused across the bridge's
  lifetime so TCP keep-alive amortises the WLAN round-trip.

#### MQTT publishing

- `aiomqtt` publisher with LWT preset on the availability topic
  (`offline` retained on ungraceful disconnect) and an explicit
  `availability=offline` publish on graceful shutdown.
- Centralised topic builders in `src/ez1_bridge/topics.py` with a
  machine-readable `RETAIN` map -- the publisher reads the map
  directly, no scattered hard-coded retain flags.
- Structured JSON state topic plus 16 retained flat per-metric
  topics for non-JSON consumers.
- Generic `publish(topic, payload, *, retain, qos)` for arbitrary
  topics (used by HA discovery), `publish_state` / `publish_result`
  / `publish_availability` typed convenience methods.

#### Home Assistant auto-discovery

- 11 sensors and 4 binary sensors auto-discovered as one device card.
- Table-driven payload builder over `_SENSOR_SPECS` and
  `_BINARY_SENSOR_SPECS` -- adding a new HA field is a column in a
  tuple, not a refactor.
- Discovery refresh on first successful poll and every 24 h to
  track `getDeviceInfo` changes (firmware upgrades, IP changes).

#### Command handler

- Three-layer validation pipeline for `set/+` commands: topic parse
  → payload parse (rejects empty, units, decimals, non-numeric) →
  range check against the live `DeviceInfo.min_power_w` /
  `max_power_w` bounds.
- Optional read-back verify after `setMaxPower` writes (default on);
  `EZ1_BRIDGE_SETMAXPOWER_VERIFY=false` for fire-and-forget.
- Stable `error` codes on the result topic (`invalid_payload`,
  `out_of_range`, `transport_error`, `verify_mismatch`) for HA
  automations to match against.

#### Observability

- 11 Prometheus metric families covering bridge liveness, per-channel
  power and energy gauges, alarm bits, API request duration histogram
  with WLAN-tuned buckets (25 ms-5 s), API error counter labelled by
  exception class, MQTT publish counter labelled by topic kind, MQTT
  reconnect counter.
- `aiohttp` server on `:9100/metrics` with the
  `prometheus-client.CONTENT_TYPE_LATEST` content type. Doubles as
  the container's healthcheck.
- `MetricsRegistry` owns its own `CollectorRegistry` -- two instances
  in the same process do not collide, which makes test isolation
  trivial.
- structlog configuration with TTY-aware format resolver: JSON in
  containers (no TTY), ANSI-coloured `ConsoleRenderer` in dev
  terminals.

#### Orchestration

- Single `asyncio.TaskGroup` in `run_service` coordinating four
  sibling coroutines: poll loop, availability heartbeat,
  `/metrics` server, command handler.
- Graceful `SIGINT` / `SIGTERM` handling via a shared `asyncio.Event`;
  the command-loop iterator (which blocks on `async for`) is broken
  via explicit `task.cancel()` from the parent coroutine.
- Resolves `device_id` via `getDeviceInfo` before bringing up MQTT so
  the LWT topic baked into CONNECT is correct from the first packet.

#### Containerisation

- Multi-stage Dockerfile: `python:3.12-slim` builder with
  `uv:0.10` from `ghcr.io/astral-sh`, `python:3.12-slim` runtime
  with a non-root user (UID/GID 65532, mirrors the
  distroless-nonroot convention for a future migration).
- `UV_COMPILE_BYTECODE=1` so first-import has no compile path; only
  `/app/.venv` and `/app/src` cross the stage boundary.
- `/metrics`-based `HEALTHCHECK` via stdlib `urllib` -- no `curl`
  dependency, no extra layer. Healthcheck stays green when the
  inverter is night-offline (the bridge is the thing being checked,
  not the inverter).
- Multi-arch image (`linux/amd64`, `linux/arm64`) at
  `ghcr.io/baronblk/ez1-mqtt-bridge`. Compressed image size ~50 MB
  (NFR-2 ceiling: 80 MB, enforced as a CI gate).
- `docker-compose.yml` in Dockhand style with map-syntax env, port
  bind on `127.0.0.1:9100` only, JSON-file logging with rotation.

#### Quality gates

- `ruff` (full PL-rule set), `mypy --strict`, `pytest` with
  `pytest-asyncio` and `respx`, `bandit`, `pip-audit`,
  `pre-commit` with conventional-commit enforcement via
  `commitizen`, `testcontainers` for real-broker integration tests.
- 339 tests covering unit and integration paths; 100 % coverage on
  the `domain/` layer (lines + branches), 98 %+ overall.
- GitHub Actions: lint, type check, test (Python 3.12 + 3.13),
  Docker build smoke with image-size guard, weekly CodeQL scan,
  tag-triggered release with multi-arch image, GHCR push, SBOM
  attached to the GitHub release.

#### Documentation

- `docs/architecture.md`, `docs/mqtt-topics.md`,
  `docs/home-assistant.md`, `docs/api-reference.md`, plus the
  hand-curated EZ1 local-API reference at
  `docs/_reference/apsystems-ez1-local-api.md`.
- README with feature list, badges, configuration table, and CLI
  reference.

[Unreleased]: https://github.com/baronblk/ez1-mqtt-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/baronblk/ez1-mqtt-bridge/releases/tag/v0.1.0
