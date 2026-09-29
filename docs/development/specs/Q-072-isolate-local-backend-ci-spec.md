# Q-072: Isolate local backend CI services

**Status:** implemented (Q-072 task branch)  
**Implementation plan:** [`../plans/Q-072-isolate-local-backend-ci-plan.md`](../plans/Q-072-isolate-local-backend-ci-plan.md)

## Problem

`scripts/ci.sh` probes the same host ports used by `./dev` (Postgres 5434 and Redis
6380), starts the development systemd units or Compose services when absent, and
pauses the API and workers when present. It then applies migrations and runs tests
that drop tables, truncate data, and flush Redis. Local CI can therefore alter
the running development or trading environment. `ci.slice` and
`ci-docker.slice` control resources; they do not isolate data.

The GitHub Actions workflow already provisions job-scoped Postgres and Redis.

## Requirements

1. Local `./scripts/ci.sh` provisions a disposable, dedicated Postgres and Redis
   pair in a uniquely named Docker Compose project. Use its own volume, localhost
   host bindings with dynamically allocated ports, and explicit connection URLs.
   Parallel local CI invocations must not share state or collide on ports.
2. CI must never discover and reuse services merely because ports are open. It
   must never start or stop `q-*` user services, or start, stop, or remove the
   `q-dev` or `q-research` Compose projects. Cleanup removes only resources
   created by that invocation, even after a failed stage or signal.
3. Set the database and Redis URLs explicitly for migrations and every test
   process. Keep test Redis clients that currently hard-code port 6380 on the
   dedicated instance. Give CI its own temporary lake, market-data, tick-cache,
   and runtime-config paths so it cannot write into the running environment.
4. Fail closed before migrations and tests if dedicated services cannot be
   started or their resolved endpoints cannot be verified. Ambient `.env`,
   `Q_DATABASE_URL`, `Q_REDIS_URL`, `CI`, or development services must not
   redirect local CI to another database or Redis instance.
   Destructive integration fixtures must also reject direct test runs against
   the development default endpoints unless an isolated test environment has
   been explicitly established.
5. Retain host `ci.slice` and container `ci-docker.slice` resource controls
   when available, without making systemd a prerequisite for local CI.
6. Preserve the hosted GitHub Actions job's own service containers and run the
   same validation stages against their explicit URLs via `./scripts/ci.sh --hosted`.
   The hosted path must never fall back to local development defaults if a service is
   unavailable.
7. Document the local CI lifecycle, Docker prerequisite, and isolation
   guarantees in the backend README. Normal `./dev` and `./research` behavior
   remains unchanged.

## Acceptance criteria

- A focused mocked launcher check demonstrates a local run selects a unique
  Compose project and dynamic ports, exports isolated URLs and paths before
  migration, and cleans up only that project on success and failure.
- A focused mocked launcher check demonstrates open development ports,
  conflicting ambient URLs, unavailable Docker, and failed health checks never
  cause a migration or test against the development services.
- Test configuration uses the exported Redis endpoint, including WebSocket
  integration tests that clear Redis; direct destructive tests fail closed
  against development defaults.
- The hosted workflow's explicit service URLs remain valid and no longer rely
  on the local service fallback.

## Out of scope

- Changing normal development or research infrastructure.
- Running GPU, Wine, MT5, Docker, or the full CI suite as a requirement for
  launcher verification; focused command mocks cover the launcher's behavior.
