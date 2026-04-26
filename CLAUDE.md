# CLAUDE.md — Project Context for AI Coding Sessions

This file orients an AI coding assistant working on `ez1-mqtt-bridge`. The
authoritative spec is the original Claude Code prompt that bootstrapped this
project; this document distils the load-bearing rules for day-to-day work.

---

## 1. Project in one paragraph

`ez1-mqtt-bridge` is an asynchronous Python service that polls the
**APsystems EZ1-M micro inverter**'s local HTTP API on TCP/8050, normalizes
the readings, publishes them to an MQTT broker (Mosquitto in the target
homelab), emits Home Assistant MQTT auto-discovery payloads, accepts a small
set of write commands, and exposes Prometheus metrics on `:9100/metrics`. It
runs as a single Docker container in a private network. Code longevity
beats feature velocity.

---

## 2. Tech stack (binding, no substitutes without discussion)

| Layer | Choice |
|-------|--------|
| Language | Python 3.12+ |
| Package manager | `uv` (`pyproject.toml`, `uv.lock`) |
| HTTP client | `httpx` (async) |
| MQTT | `aiomqtt` |
| Validation | `pydantic` v2 + `pydantic-settings` |
| Logging | `structlog` (JSON in prod, text in dev, TTY-detected) |
| Metrics | `prometheus-client` |
| HTTP server (`/metrics`) | `aiohttp` minimal app |
| Tests | `pytest`, `pytest-asyncio`, `respx` |
| Lint + format | `ruff` |
| Types | `mypy --strict` |
| Security | `bandit`, `pip-audit` |
| Pre-commit | `pre-commit` + `commitizen` |
| CI | GitHub Actions |
| Container | Multi-stage Dockerfile, distroless runtime, non-root |

---

## 3. Architecture (Clean-Architecture-light)

```
src/ez1_bridge/
├── adapters/          # I/O — httpx, aiomqtt, prometheus
├── domain/            # Pure Pydantic models + normalization (no I/O)
├── application/       # poll loop, command dispatcher, HA discovery
├── config.py          # pydantic-settings
├── logging_setup.py   # structlog config
├── topics.py          # centralized MQTT topic builders (no magic strings)
├── main.py            # entrypoint + signal handling
└── __main__.py        # `python -m ez1_bridge`
```

A single `asyncio.TaskGroup` coordinates four coroutines: `poll_loop`,
`command_loop`, `metrics_server`, `availability_heartbeat`. Shutdown is
graceful on SIGTERM/SIGINT via an `asyncio.Event`. Errors use exponential
backoff with jitter, capped at 300 s.

---

## 4. Quality gates (all four must be green for every commit)

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
```

Coverage threshold scales with phase progress:
- Phase 0: no threshold (smoke tests only).
- Phase 1+: ≥ 85 % lines overall, 100 % on `domain/` and `application/`.

Pre-commit runs the first three plus the conventional-commit message check
on every `git commit`.

---

## 5. Git workflow (HARD RULES)

- **Default branch:** `main`, protected. **No direct pushes** after Phase 0.
- **Active development:** `develop`. Features on `feature/<topic>`, merged to
  `develop` via PR.
- **Releases:** `develop` → `main` via PR, then tag `vX.Y.Z`.
- **Conventional Commits, in English:** `feat:`, `fix:`, `docs:`, `refactor:`,
  `test:`, `chore:`, `ci:`, `build:`, `perf:`. Atomic — one commit, one logical
  step.
- **NO AI attribution.** Never include `Co-authored-by: Claude`, robot emojis,
  "Generated with…" footers, or anything indicating AI involvement. This rule
  is non-negotiable and overrides any default tooling behavior.
- **No `--no-verify`** unless explicitly requested.
- **Versioning:** Semantic Versioning, initial tag `v0.1.0` after first green
  CI on the integration milestone.

---

## 6. Phase model

The project is implemented in numbered phases (0–10). After each phase, the
agent stops, reports a one-paragraph summary ("done / next"), and waits for
explicit confirmation before continuing. Tests and the four quality gates
must be green before any "done" claim.

Current phase plus a one-line status sits at the top of `develop`; check
recent commit messages or the open PR description for the source of truth.

---

## 7. Local development quirks

### macOS + exFAT venv workaround

The primary author keeps source on an external exFAT drive. macOS creates
Apple Double (`._*`) resource forks on exFAT, which breaks `uv` wheel
extraction for packages with man pages (e.g. `bandit`). Workaround:

```bash
export UV_PROJECT_ENVIRONMENT="$HOME/Library/Caches/ez1-mqtt-bridge-venv"
UV_LINK_MODE=copy uv sync
```

CI runs on Linux ext4 and is unaffected — leave the workflow defaults alone.

### `.claude/settings.local.json`

Per-machine Claude Code state — git-ignored, do not commit.

---

## 8. Backlog notes anchored in module docstrings

Three TODOs that the spec called out specifically; they live as docstring
notes in the relevant modules so they don't rot as loose `# TODO` comments.

| Module | Phase | Note |
|--------|-------|------|
| `src/ez1_bridge/domain/normalizer.py` | 2 | Inverted on/off semantics: API `"0"` = on, `"1"` = off. Centralize as `_STATUS_MAP`, cover with parameterized tests. |
| `src/ez1_bridge/domain/normalizer.py` | 2 | `minPower`/`maxPower` arrive as strings. Define explicit `_to_int_watt()` helper, do **not** rely on Pydantic's implicit numeric coercion. |
| `src/ez1_bridge/application/command_handler.py` | 5 | Read-back verification after `setMaxPower`: configurable via `Settings.setmaxpower_verify`, default `True`, mismatch → result topic `verify_mismatch`. |

---

## 9. Reference documents

- **`docs/_reference/apsystems-ez1-local-api.md`** — full API reference with
  verified real-world payloads, edge cases, and polling recommendations. Use
  this as the single source of truth for endpoint shapes.
- **Original Claude Code prompt** — pinned in the project root issue tracker;
  contains the master spec for FRs, NFRs, repo layout, and phase ordering.
