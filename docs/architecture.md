# Architecture

> Component diagrams and runtime sequences will arrive in Phase 9 once
> the system has settled. This document already covers the parts that
> change rarely and that contributors need to know up front: how the
> repository is laid out, which CI/CD workflows guard merges, and which
> branch-protection rules the maintainer enforces.

## Repository layout

```
src/ez1_bridge/
├── adapters/         # I/O layer (httpx, aiomqtt, prometheus_client, aiohttp)
│   ├── ez1_http.py        # EZ1 local HTTP API client
│   ├── mqtt_publisher.py  # aiomqtt publisher with LWT and reconnect hook
│   └── prom_metrics.py    # MetricsRegistry + /metrics aiohttp server
├── application/      # Coroutines + glue, no transport details
│   ├── command_handler.py # set/+ subscriber and dispatcher
│   ├── ha_discovery.py    # table-driven HA discovery payload builder
│   └── poll_service.py    # poll_loop, availability_heartbeat, _wait_or_stop
├── domain/           # Pure logic, no I/O dependencies (100% test coverage)
│   ├── models.py         # frozen Pydantic v2 models for inverter state
│   └── normalizer.py     # raw EZ1 envelope -> InverterState
├── config.py         # pydantic-settings, env-driven Settings
├── logging_setup.py  # structlog config, TTY-aware JSON/text resolver
├── main.py           # entrypoint, signal handling, run_service TaskGroup
├── topics.py         # centralised MQTT topic builders + RETAIN map
└── __main__.py       # 'python -m ez1_bridge' shim
```

The Clean-Architecture-light split (adapters / application / domain) is
load-bearing: domain has no I/O imports, application depends on
adapters, the wiring lives in `main.run_service`. Tests can exercise
the domain layer at full coverage without touching network or filesystem.

## CI/CD workflows

Three GitHub Actions workflows live under `.github/workflows/`:

| Workflow      | Trigger                                                | Purpose                                              |
|---------------|--------------------------------------------------------|------------------------------------------------------|
| `ci.yml`      | push to `main` / `develop` / `feature/**`, PRs         | Lint, type-check, test (3.12 + 3.13), Docker smoke   |
| `release.yml` | tag push matching `v*`, manual `workflow_dispatch`     | Multi-arch image build, GHCR push, SBOM, GH release  |
| `codeql.yml`  | push to `main` / `develop`, weekly cron, manual run    | Python security scanning via CodeQL                  |

### `ci.yml` jobs

| Job                                | Required for `main` merge | Notes                                            |
|------------------------------------|---------------------------|--------------------------------------------------|
| `Lint (ruff)`                      | yes                       | `ruff check` + `ruff format --check`             |
| `Type check (mypy --strict)`       | yes                       | Whole `src` + `tests` tree                       |
| `Test (Python 3.12)`               | yes                       | pytest with global ≥85% coverage gate            |
| `Test (Python 3.13)`               | yes                       | Same suite on 3.13 for forward-compat            |
| `Docker build smoke (linux/amd64)` | yes (Phase 8 onwards)     | `--version` + `probe --help` + ≤80 MB size guard |

Every test job also enforces 100% line+branch coverage on
`src/ez1_bridge/domain/` via a follow-up `coverage report --include`
step -- the domain layer is pure logic, anything below 100% is a
missed test, not a quirk.

### `release.yml` pipeline

Triggered exclusively by a tag push matching `v*` (atomic release in
one shot) or by a manual `workflow_dispatch` with the default
`dry_run=true` (build only, no push, no release-asset upload). The
release path:

1. QEMU + Buildx so a single amd64 runner can produce `linux/arm64`
   images (~3-5x native speed; acceptable once per tag).
2. `docker/metadata-action` computes the tag set:
   `vX.Y.Z`, `X.Y.Z`, `X.Y`, plus `:latest` on real tag pushes.
3. Login to `ghcr.io` via `GITHUB_TOKEN` (no PAT needed -- the token
   has `packages:write` for the same repository's namespace).
4. `docker/build-push-action@v6` builds and pushes the multi-arch
   image, including signed provenance and SBOM attestations.
5. `anchore/sbom-action` generates an SPDX-JSON SBOM via Syft.
6. `softprops/action-gh-release` publishes the GitHub Release with
   auto-generated notes and the SBOM attached.

### `codeql.yml` cadence

CodeQL runs on direct pushes to `main` / `develop` and weekly on a
Monday-04:00 UTC cron. It is intentionally **not** on PRs: a 5-10 min
analysis would dominate per-PR feedback latency for findings that are
still caught when the same code lands on `develop`.

CodeQL is also intentionally **not** a required status check on
`main`. A weekly cron failure should not retroactively block the
merge queue; findings open advisories that are triaged independently.

## Branch protection

| Setting                              | `main`              | `develop`         |
|--------------------------------------|---------------------|-------------------|
| Require PR before merging            | ✅                  | ❌                |
| Required approving reviews           | 0 (solo repo)       | —                 |
| Require status checks to pass        | ✅                  | ❌                |
| Required checks (strict)             | 5 (see below)       | —                 |
| `enforce_admins`                     | ✅                  | ❌                |
| Require conversation resolution      | ✅                  | ❌                |
| `allow_force_pushes`                 | ❌                  | ✅                |
| `allow_deletions`                    | ❌                  | ✅                |

Required status checks on `main`:

1. `Lint (ruff)`
2. `Type check (mypy --strict)`
3. `Test (Python 3.12)`
4. `Test (Python 3.13)`
5. `Docker build smoke (linux/amd64)`

`develop` is intentionally permissive so the maintainer can rebase /
amend / squash freely during integration; `main` is the protected
canonical branch that mirrors the latest tagged release.

The protection rules are managed via the GitHub REST API rather than
the web UI. The single source of truth for the rule set is the
JSON document committed at `docs/_reference/branch-protection-main.json`
(see Phase 9 for the planned export tooling). For now, the
canonical rule set is reproduced here in human-readable form.

## Workflows that are deliberately absent

* **Auto-merge for Dependabot PRs.** Phase 9 introduces `dependabot.yml`,
  but auto-merge stays opt-in -- our small dependency graph deserves a
  human glance before a transitive update lands.
* **Coverage upload to Codecov / SonarCloud.** Coverage is enforced
  inside the CI job (`fail_under`) and the report is dumped to the
  job log. A third-party analytics service is not yet justified.
* **Image signing via cosign.** Phase 10 stretch goal: today's SBOM +
  Buildx-native provenance is already a meaningful supply-chain
  signal; cosign keyless signatures with the OIDC token would be the
  next step.
