# Isolate Local Backend CI Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local backend CI use disposable Postgres and Redis services and isolated file paths for every invocation.

**Architecture:** `scripts/ci.sh` owns a unique Compose project and resolves its published localhost ports before exporting test settings. A separate hosted path invoked via `--hosted` uses only the GitHub Actions service endpoints. Both paths run the existing validation stages after a fail-closed preflight.

**Tech Stack:** Bash, Docker Compose, GitHub Actions, Python/pytest.

**Spec:** [`../specs/Q-072-isolate-local-backend-ci-spec.md`](../specs/Q-072-isolate-local-backend-ci-spec.md)

## Global constraints

- Preserve `ci.slice` and `ci-docker.slice` when available.
- Never operate on `q-*` user units, `q-dev`, or `q-research` from CI.
- Keep validation focused on changed behavior; launcher tests use mocks.

## File map

- `scripts/ci.sh`: service lifecycle, endpoint preflight, isolated environment, existing validation stages.
- `docker-compose.ci.yml`: complete CI-only Postgres/Redis definition with unique project volume and dynamic localhost ports.
- Optional CI slice override: container resource placement when the host slice exists.
- `.github/workflows/ci.yml`: explicit hosted service mode and URLs.
- `tests/streaming/ws/conftest.py`: derive its Redis client from the exported CI URL.
- `tests/conftest.py`: guard destructive integration fixtures against development endpoints.
- `tests/cli/test_ci_launcher.py`: focused fake `docker`, `systemctl`, `uv`, and `make` command checks.
- `README.md`: local CI prerequisites and lifecycle.

## Review focus

- An ambient `CI=true` on a developer machine must not select hosted services; cover in Task 1.
- An open port belonging to `q-dev` must not be reused; cover in Task 1.
- A startup failure must not reach Alembic; cover in Task 1.
- Concurrent invocations must have distinct project names and bindings; cover in Task 1.
- A signal or failed test must clean up only the owned project; cover in Task 1.

---

### Task 1: Own and verify local CI infrastructure

**Files:** `docker-compose.ci.yml`, optional CI slice override, `scripts/ci.sh`, `tests/cli/test_ci_launcher.py`

**Interface:** local `./scripts/ci.sh` exports `Q_DATABASE_URL`, `Q_REDIS_URL`, `Q_DATA_LAKE_ROOT`, `Q_MARKET_DATA_ROOT`, `Q_TICK_CACHE_DIR`, and `Q_RUNTIME_CONFIG_PATH` before `alembic upgrade head`.

- [x] Add focused launcher tests with mocked commands for unique Compose project/ports, ambient URL and `CI` overrides, existing dev ports, startup and health failures, concurrent runs, and success/failure/signal cleanup. Assert no call to `systemctl --user start|stop` and no operation on `q-dev` or `q-research`.
- [x] Run only the focused launcher tests; confirm they fail against the current shared-service path.
- [x] Define CI-only Postgres/Redis Compose services with dynamically published localhost ports and a project-scoped Postgres volume. Keep `ci-docker.slice` in an optional override selected when available.
- [x] Replace local port probing and systemd unit management in `scripts/ci.sh` with unique-project startup, health verification, resolved-port URL export, temporary file roots, and cleanup traps. Refuse to continue if any preflight fails.
- [x] Run the focused launcher tests and confirm all cases pass.

### Task 2: Keep test clients and hosted CI on the selected services

**Files:** `tests/streaming/ws/conftest.py`, `tests/conftest.py`, `.github/workflows/ci.yml`, `tests/cli/test_ci_launcher.py`, `README.md`

**Interface:** the WebSocket integration Redis URL is derived from `Q_REDIS_URL` with database 15; GitHub Actions explicitly invokes `./scripts/ci.sh --hosted` and uses its job-scoped Postgres/Redis URLs.

- [x] Add focused tests proving WebSocket integration fixtures follow `Q_REDIS_URL` rather than port 6380, direct destructive tests reject development endpoints, and missing hosted services fail before migration.
- [x] Run the focused tests and confirm the new assertions fail.
- [x] Update the fixture, hosted workflow, and launcher hosted preflight. Document local Docker requirements, ephemeral project cleanup, and the lack of dependency on `./dev`.
- [x] Run the focused tests and a workflow/Compose configuration check that does not start containers. Review `git diff --check` and commit the task changes.

## Handoff

Use the task branch created by `./work start Q-072 --agent <agent>` after the plan is approved. Do not run the full backend CI suite against existing local services while implementing this isolation.
