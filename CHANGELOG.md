# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-04-27

Bundled quality-fix release. Three follow-ups from the v0.1.1
hardware smoke against E17010000783 (firmware EZ1 1.12.2t).

### Added

- Success-level logging for the three MQTT publish entry points.
  `MQTTPublisher.publish_state` now emits a `state_published`
  event with `device_id`, `power_w` (total), `energy_today_kwh`
  (total), `status`, and `any_alarm`. `publish_availability`
  emits `availability_published` with `online: bool`.
  `publish_result` emits `command_result_published` with
  `command`, `ok`, and `error`. Pinned by three new caplog tests
  in `tests/unit/test_mqtt_publisher.py`.
  Closes [#19].
- New [`docs/troubleshooting.md`](docs/troubleshooting.md)
  consolidating the field-verified failure modes:
  multi-VLAN deployment requiring `network_mode: host`,
  the four EZ1 hardware quirks (BLE app kills HTTP, parallel-
  request rejection, WLAN volatility, `e1`/`e2` reset on cold
  start), and the two diagnostic surfaces fixed in this release
  (silent success path, hard-coded healthcheck port).
  Closes [#17].

### Fixed

- Docker `HEALTHCHECK` now reads `EZ1_BRIDGE_METRICS_PORT` from
  the container's environment instead of hard-coding port 9100.
  Operators relocating the metrics port no longer get a false
  `unhealthy` status. The probe stays on `127.0.0.1` because
  in-container loopback is universal.
  Closes [#18].

### Why this matters

The Phase 10 hardware smoke that surfaced v0.1.1's parallel-poll
bug also surfaced an observability hole that masked the bug for
~30 minutes: the bridge was working correctly but emitted no
success-level log lines, so the diagnosis chased phantom causes
(idle-connection timeout, TaskGroup hang, `_wait_or_stop` bug,
keep-alive issue) before noticing the broker had retained state
with live values. v0.1.2 closes that hole and writes down the
EZ1 hardware quirks discovered along the way so the next operator
saves the same time.

[#17]: https://github.com/baronblk/ez1-mqtt-bridge/issues/17
[#18]: https://github.com/baronblk/ez1-mqtt-bridge/issues/18
[#19]: https://github.com/baronblk/ez1-mqtt-bridge/issues/19

## [0.1.1] - 2026-04-27

### Fixed

- **Poll cycle now serialises the four EZ1 read endpoints instead of
  fanning them out via `asyncio.gather`.** The EZ1-M's local HTTP
  server cannot handle parallel TCP connections — concurrent SYN
  packets are dropped at the device, leaving every request in
  connect-timeout. The bridge handled this correctly as a transient
  transport error (no crash, `availability=offline` flipped, retry
  next cycle), but no `state_published` event ever fired against
  real hardware. Verified against firmware EZ1 1.12.2t. Worst-case
  sequential latency ~2.8 s per cycle, well within the default 20 s
  poll interval. Fixes [#14].
- New regression test `test_poll_loop_serializes_ez1_endpoint_requests`
  pins both the call order and the maximum in-flight count to 1; a
  future return to `asyncio.gather` (or `asyncio.create_task`) trips
  it deterministically.

[#14]: https://github.com/baronblk/ez1-mqtt-bridge/issues/14

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

[Unreleased]: https://github.com/baronblk/ez1-mqtt-bridge/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/baronblk/ez1-mqtt-bridge/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/baronblk/ez1-mqtt-bridge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/baronblk/ez1-mqtt-bridge/releases/tag/v0.1.0
