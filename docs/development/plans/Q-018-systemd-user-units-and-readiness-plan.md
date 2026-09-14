# Q-018 implementation plan: systemd user units and readiness

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-018-systemd-user-units-and-readiness-spec.md`](../specs/Q-018-systemd-user-units-and-readiness-spec.md)  
**Depends on:** Q-011, Q-013

## Current-system context

`docker-compose.yml` defines `postgres` (`postgres:16-alpine`, user, password,
and database `q`, host port 5434, named volume `postgres_data`, which appears on
this machine as `q_backend_postgres_data`, health `pg_isready -U q -d q`) and
`redis` (`redis:7-alpine`, host port 6380, no volume, health `redis-cli ping`),
plus `backend` and `worker` services under the `containerized` profile.
`scripts/ci.sh` starts `docker compose up -d postgres redis` when ports 5434 or
6380 are closed and `CI` is unset. `q_frontend/src-tauri/src/backend.rs`
locates the checkout, runs compose, `uv sync`, and `uv run alembic upgrade head`,
then spawns `uv run dev` and `uv run worker`. It sets `Q_DATA_LAKE_ROOT`,
`Q_TICK_CACHE_DIR`, `Q_MARKET_DATA_ROOT`, and `Q_RUNTIME_CONFIG_PATH` under the
app data directory (`~/.local/share/com.quant.desktop/data/…`). The only existing
units are `gateway/systemd/mt5-terminal.service` and `mt5-gateway.service`
(`Type=simple`, `EnvironmentFile=%h/.config/mt5-gateway/mt5-gateway.env`,
`Restart=on-failure`, `RestartSec=5`), installed by hand as described in
`docs/mt5-wine-gateway.md`. The host runs systemd 259 on Ubuntu 26.04. Docker 29
is installed, podman is not, and apt offers podman 5.7.

Entry points are in `pyproject.toml` `[project.scripts]`. `dev` runs uvicorn
through `api.main:run_dev`, and startup work happens in `api/lifespan.py`
(market data init, orphan-run reconciliation, registry sync), which never checks
the migration revision. `worker` (`cli/worker.py`) `execv`s
`python -m dramatiq q_backend.tasks --processes N --threads 1`, so the Dramatiq
master keeps the launcher's PID, and actors run in forked children. The
middleware is added in `tasks/broker.py`. `q-outbox-relay` (`cli/q_outbox_relay.py`)
takes a Postgres advisory lock, installs SIGTERM and SIGINT handlers, and calls
`OutboxRelay.run_forever(stop)`, which catches Redis and SQLAlchemy errors with
backoff up to 30 s. `q-market-publisher` exits 1 with "no symbols configured"
and otherwise calls `MarketDataPublisher.run_forever(stop)`.
`storage/health.py` already has `check_postgres` and `check_redis`. Nothing sends
`sd_notify`. The gap is that no backend process is supervised, and none says
when it is ready.

## Interfaces produced

```python
# src/q_backend/observability/systemd.py
EX_CONFIG: Final = 78                       # sysexits.h; excluded from restart in units

def notify(state: str) -> bool: ...
    """Send one datagram to $NOTIFY_SOCKET ('@' prefix = abstract). False, and no error, when unset."""
def notify_ready() -> bool: ...             # notify("READY=1")
def notify_status(text: str) -> bool: ...   # notify(f"STATUS={text}"), shown by systemctl status
```

```python
# src/q_backend/storage/db/migrations.py
@dataclass(frozen=True)
class SchemaRevision:
    current: str | None
    head: str

def schema_revision(engine: Engine, alembic_ini: Path) -> SchemaRevision: ...
def alembic_ini_path() -> Path: ...         # checkout-relative, same resolution as _project_root
```

```python
# src/q_backend/cli/q_api.py   (project script "q-api")
class NotifyingServer(uvicorn.Server):
    async def startup(self, sockets: list[socket.socket] | None = None) -> None: ...  # super(), then notify_ready if started
def main(argv: list[str] | None = None) -> int: ...
    """Check Postgres and schema head (EX_CONFIG with both revisions if behind), then serve without reload."""
```

```python
# src/q_backend/streaming/relay.py  (change)
def run_forever(self, stop: threading.Event, on_first_success: Callable[[], None] | None = None) -> None: ...

# src/q_backend/cli/q_outbox_relay.py  (change): passes notify_ready as on_first_success
# src/q_backend/cli/q_market_publisher.py  (change): exits EX_CONFIG on no symbols;
#   pings Redis with capped backoff until success or stop, then notify_ready, then run_forever
```

```python
# src/q_backend/tasks/broker.py  (addition)
class ReadinessMiddleware(Middleware):
    def after_worker_boot(self, broker: dramatiq.Broker, worker: dramatiq.Worker) -> None: ...
    """Ping the broker's Redis client, then notify_ready. Runs in each forked worker process."""
```

```
deploy/systemd/quadlet/q-postgres.container      Image postgres:16-alpine, PublishPort 127.0.0.1:5434:5432,
                                                 Volume q-postgres-data.volume, EnvironmentFile %h/.config/q/postgres.env,
                                                 HealthCmd pg_isready, Notify=healthy
deploy/systemd/quadlet/q-postgres-data.volume
deploy/systemd/quadlet/q-redis.container         Image redis:7-alpine, PublishPort 127.0.0.1:6380:6379,
                                                 HealthCmd redis-cli ping, Notify=healthy
deploy/systemd/user/q-migrate.service.in         Type=oneshot, RemainAfterExit=yes, Requires/After q-postgres
deploy/systemd/user/q-api.service.in             Type=notify, Requires q-postgres q-migrate, Wants q-redis, After all three
deploy/systemd/user/q-outbox-relay.service.in    Type=notify, Requires q-postgres q-redis, After both
deploy/systemd/user/q-market-publisher.service.in Type=notify, Requires q-redis, Wants mt5-gateway, After q-redis mt5-gateway
deploy/systemd/user/q-research-worker.service.in Type=notify, NotifyAccess=all, Requires q-postgres q-redis, After both,
                                                 KillMode=mixed, TimeoutStopSec=60
deploy/systemd/user/q-backend.target             Wants every unit above; WantedBy default.target
deploy/systemd/backend.env.example               Q_DATABASE_URL, Q_REDIS_URL, lake/tick/market/runtime-config roots, Q_STREAM_SYMBOLS
deploy/systemd/postgres.env.example              POSTGRES_USER/PASSWORD/DB
scripts/install-user-units.sh                    [--dest DIR] [--quadlet-dest DIR] [--uninstall] [--no-sync]
docs/operations/systemd-user-units.md            install, operate, migrate from compose, Tauri data roots, troubleshooting
tests/deploy/test_units.py                       rendering, verify, dependency table, ExecStart shape
tests/deploy/notify_socket.py                    fixture: bound AF_UNIX datagram socket collecting messages
```

Every long-running `.service.in` shares: `EnvironmentFile=%h/.config/q/backend.env`,
`WorkingDirectory=@Q_BACKEND_DIR@`, `ExecStart=@Q_BACKEND_DIR@/.venv/bin/<script>`,
`Restart=on-failure`, `RestartSec=1`, `RestartSteps=5`, `RestartMaxDelaySec=30`,
`StartLimitIntervalSec=0`, `RestartPreventExitStatus=78`, and `TimeoutStartSec=90`.

## Implementation decisions

- **Readiness is a 20-line stdlib datagram sender, not `systemd-python` or
  `sdnotify`.** `systemd-python` needs libsystemd headers to build in every
  environment that syncs the lockfile, including CI and the containerized
  profile. The protocol is one `sendto` on an `AF_UNIX` `SOCK_DGRAM` socket, with
  `@` mapped to a leading NUL for abstract names. Without `NOTIFY_SOCKET` it
  returns `False`, which is the spec's "no-op outside systemd".

- **`ExecStart` points at the checkout's `.venv/bin/<script>`, rendered at
  install time, and never at `uv run` or `bash -c`.** `uv run` stays the parent
  of the Python process, so systemd's main PID would be `uv`. `Type=notify`
  would then reject `READY=1` from the child under `NotifyAccess=main`, and
  SIGTERM would reach `uv` first. Units need absolute paths, so templates carry
  `@Q_BACKEND_DIR@`, and the installer substitutes it. The gateway units' `bash -c`
  pattern is not copied because it has the same main-PID problem.

- **The installer runs `uv sync --frozen --no-dev` before rendering, unless
  `--no-sync` is passed.** The units run `.venv` directly, so a pull that adds a
  dependency would otherwise fail at the next restart. The Tauri shell's per-launch
  `uv sync` is what this replaces, done once per install.

- **The API gets a new `q-api` entry point that runs `uvicorn.Server`
  programmatically. `dev` is unchanged.** Sending `READY=1` from the FastAPI
  lifespan would be too early, because uvicorn completes lifespan startup before
  it binds its sockets. The `startup` override runs after the listeners exist,
  which is the spec's "accepting HTTP connections". Keeping `run_dev` means
  `uv run dev` with reload still works for development.

- **The API checks Postgres and the migration head before building the
  server, and exits `EX_CONFIG` (78) when behind. It does not migrate.** A
  separate `q-migrate.service` oneshot applies migrations, with `q-api` requiring
  it and ordered after it. Migrating inside the API would run DDL from every API
  start, including concurrent restarts. Dropping migrations entirely would leave
  the schema behind after every pull once the Tauri shell stops running them
  (Q-019). The API's own check stays as the guard the architecture names. It
  exits 78 because retrying cannot fix a missing migration, so restarting in a
  loop only floods the journal.

- **The relay's readiness is an `on_first_success` callback invoked once inside
  `run_forever` after the first `run_once` that returns without raising.** That
  is exactly "first successful XADD or confirmed empty backlog", because
  `run_once` either appends or finds nothing, and raises on outage. Moving the
  check into the CLI would duplicate the loop's error handling.

- **The market publisher is ready after a Redis ping, not after gateway health,
  and it retries the ping with the same capped backoff before notifying.** Its
  output is Redis, and Q-013 specifies that a gateway outage is a running,
  degraded state. Gating readiness on the gateway would make `q-backend.target`
  fail to start outside market hours or with the terminal logged out. The unit
  `Wants=mt5-gateway.service` rather than requiring it, for the same reason.

- **Empty symbol configuration exits 78 instead of 1.** The existing Q-013 test
  asserts non-zero, which still holds. With `RestartPreventExitStatus=78`,
  a misconfigured publisher shows as failed with its message in
  `systemctl status`, instead of restarting every second.

- **The worker's readiness comes from a Dramatiq `after_worker_boot` middleware
  in the forked worker processes, and the unit sets `NotifyAccess=all`.**
  `cli/worker.py` `execv`s into the Dramatiq master, so the main PID is correct,
  but the master never touches the broker. "Connected to the broker" is only
  true in a worker process. systemd accepts the first `READY=1` and ignores
  repeats, so N workers sending it is harmless.

- **The worker unit uses `KillMode=mixed` and `TimeoutStopSec=60`.** Dramatiq's
  master forwards SIGTERM to its children and waits for them. `mixed` sends
  SIGTERM to the main process only, and SIGKILL to the whole cgroup at the
  timeout, so a six-hour actor cannot block shutdown indefinitely. Interrupted
  runs are marked cancelled by `reconcile_orphaned_runs` on the next API start,
  which already exists for this case.

- **Restart uses `RestartSteps=5` and `RestartMaxDelaySec=30` (systemd ≥254)
  with `StartLimitIntervalSec=0`.** This gives the spec's increasing delay capped
  near 30 s. The default start limit (5 starts in 10 s) would otherwise leave a
  service permanently failed after a dependency outage, which is the opposite of
  supervision.

- **Quadlets are named `q-postgres` and `q-redis`, not `postgres` and `redis`
  as in the architecture table.** User quadlets share one namespace per user,
  and this machine already runs other projects' Postgres stacks. A generic
  `postgres.service` would collide with the first of them to adopt quadlets. The
  dependency shape is otherwise the table's, and the handoff records the naming
  deviation for `q_contracts/docs/system-architecture.md`.

- **Quadlet readiness uses `Notify=healthy` with `HealthCmd` equal to the
  compose health checks, which requires podman ≥5.0.** It makes the container
  unit's activation wait for `pg_isready` and `redis-cli ping`, which are the
  architecture's readiness probes, without a wrapper script. Podman 5.7 is the
  distribution candidate on the target machine.

- **Container ports bind to `127.0.0.1` only.** Compose publishes on all
  interfaces. Every client (settings defaults, Tauri, CI) uses `localhost`, and a
  database with the password `q` must not be reachable from the LAN.

- **Postgres credentials come from `%h/.config/q/postgres.env` through the
  quadlet's `EnvironmentFile`. The example file carries today's `q`/`q` values.**
  The spec rules out secrets in the repository. The example keeps the defaults
  that `Settings.database_url` already assumes, so nothing else changes.

- **The Postgres volume is a quadlet-managed named volume
  `q-postgres-data`, and migration from compose is `pg_dumpall` piped from the
  compose container into the quadlet container, with table row counts compared
  before and after.** Reusing the Docker volume directly is not possible
  because podman and Docker keep separate storage, and copying data directories
  between runtimes risks UID mapping errors under rootless podman. Both sides
  run Postgres 16, so a logical dump restores cleanly.

- **The installer refuses when 5434 or 6380 is listening and the matching
  quadlet unit is not active, and prints the `docker compose … stop postgres
  redis` command.** Starting a quadlet against a held port fails with a podman
  bind error deep in the journal. Checking up front turns that into one clear
  line, which is the spec's requirement.

- **`scripts/ci.sh` checks `systemctl --user cat q-postgres.service` and starts
  `q-postgres q-redis` when they are installed, and otherwise keeps the compose
  path.** Developers who have not installed the units, and the `CI` path, are
  unaffected. Once the units are installed, running CI does not bring compose
  containers back to fight over the ports.

- **`tests/deploy/test_units.py` parses rendered units with `configparser`
  (strict off, duplicate keys allowed) and asserts the dependency directives
  against a table in the test, copied from architecture §8 with the documented
  deviations.** Running `systemd-analyze --user verify` checks syntax and
  referenced executables but not intent. A table assertion is what catches a
  `Requires=` changed to `Wants=` in review. `verify` runs when the binary exists
  and is skipped otherwise.

- **Readiness tests bind a real datagram socket in `tmp_path`, set
  `NOTIFY_SOCKET` for a subprocess, and assert on received datagrams with a
  timeout.** Mocking `notify` would prove only that it was called, not that the
  bytes reach a socket systemd would read. The API test polls a TCP connect to
  the ephemeral port and requires it to succeed no later than the first
  `READY=1` arrives.

## Ordered implementation

1. [x] Work on the branch `Q-018-systemd-user-units-and-readiness` in `q_backend`,
   created from `development` by `./work start`.
2. [x] Write failing tests in `tests/observability/test_systemd_notify.py`: with
   `NOTIFY_SOCKET` pointing at a bound socket in `tmp_path`, `notify_ready()`
   returns `True` and the socket receives exactly `b"READY=1"`; with an abstract
   name `@q-test-<uuid>`, the same holds; with the variable unset, it returns
   `False` and raises nothing. Confirm they fail, implement, and confirm they pass.
   Commit.
3. [x] Write failing tests in `tests/storage/test_migrations_check.py` (SQLite file
   database): after `alembic upgrade head`, `schema_revision().current == head`;
   after `alembic downgrade -1`, `current` equals the previous revision id and
   differs from `head`. Confirm they fail, implement, and confirm they pass.
   Commit.
4. [ ] Write failing `integration` tests in `tests/cli/test_q_api_readiness.py`: run
   `q-api --port <free>` as a subprocess with a notify socket against a migrated
   Postgres; `READY=1` arrives within 30 s, and a TCP connect to the port succeeds
   at that moment. With `Q_REDIS_URL=redis://127.0.0.1:1/0`, `READY=1` still
   arrives. Against a database downgraded by one revision, the process exits 78
   within 10 s, stderr contains both revision ids, and no datagram arrives.
   Confirm they fail, implement `q-api` and `NotifyingServer`, add the script to
   `pyproject.toml`, and confirm they pass. Commit.
5. [ ] Write failing tests in `tests/streaming/test_relay_readiness.py` (SQLite and
   `fakeredis`): `run_forever` with an empty outbox calls `on_first_success`
   exactly once across three passes; with the Redis client raising
   `ConnectionError` for 2 s, the callback is not called, and it is called once
   after the client recovers. Confirm they fail, implement, wire `notify_ready`
   in `q_outbox_relay.py`, and confirm they pass. Commit.
6. [ ] Write failing tests in `tests/cli/test_q_market_publisher_readiness.py`: with
   no symbols, `main([])` returns 78; with `fakeredis` and the fake gateway
   stopped (from Q-013's `tests/streaming/fake_gateway.py`), `READY=1` reaches the
   notify socket before the first poll. Confirm they fail, implement, and confirm
   they pass. Update the existing no-symbols test only if it asserts exit code 1
   exactly. Commit.
7. [ ] Write a failing `integration` test in `tests/tasks/test_worker_readiness.py`:
   run `.venv/bin/worker` with `Q_WORKER_PROCESSES=1` and a notify socket against
   local Redis; `READY=1` arrives within 60 s; SIGTERM makes it exit 0 within
   60 s. Confirm it fails, add `ReadinessMiddleware`, and confirm it passes.
   Commit.
8. [ ] Write the quadlet files, `.service.in` templates, the target, and both env
   examples. Write failing tests in `tests/deploy/test_units.py`: rendering into
   `tmp_path` with `Q_BACKEND_DIR=/opt/q_backend` leaves no `@…@`; every
   `ExecStart` starts with `/opt/q_backend/.venv/bin/` and contains neither
   `uv ` nor `bash`; every long-running unit has `Type=notify`,
   `Restart=on-failure`, `RestartMaxDelaySec=30`, and
   `RestartPreventExitStatus=78`; the worker has `NotifyAccess=all`; the
   `Requires`, `Wants`, and `After` sets equal the test's architecture table;
   `systemd-analyze --user verify` exits 0 on the rendered services (skipped
   without the binary). Confirm they fail, implement, and confirm they pass.
   Commit.
9. [ ] Write `scripts/install-user-units.sh` with `--dest`, `--quadlet-dest`,
   `--no-sync`, and `--uninstall`. Write failing tests: two runs into temporary
   destinations produce identical trees with unchanged modification times on the
   second run; `--uninstall` removes exactly the installed files; with a
   listening socket on a free port substituted for 5434 through
   `Q_UNITS_PG_PORT`, the script exits non-zero and prints `docker compose`.
   Confirm they fail, implement, and confirm they pass. Commit.
10. [ ] Change `scripts/ci.sh` to prefer installed units over compose. Run it with the
    units not installed and confirm that the compose path is unchanged. Commit.
11. [ ] Write `docs/operations/systemd-user-units.md`: install (podman, linger, env
    files, installer), operate (`start`, `status`, `journalctl`), migrating from
    compose (stop compose, start `q-postgres`, `pg_dumpall` pipe, row-count
    query), using the Tauri app data roots, and troubleshooting (port held,
    exit 78, schema behind). Update the README's local-stack section to point to
    it and keep the compose instructions marked as the fallback. Commit.
12. [ ] Regression: `uv run pytest tests/streaming tests/cli tests/api -q` passes, and
    `uv run dev` and `uv run q-outbox-relay` still start by hand without
    `NOTIFY_SOCKET`. Commit any fixes.
13. [ ] Human step, matching human-verifiable criterion 1: `sudo apt install
    podman`, create the env files, run the installer, run the generator dry run,
    `systemctl --user start q-backend.target`, and record `systemd-analyze --user
    blame` for the `q-` units.
14. [ ] Human step, matching human-verifiable criterion 2: follow "Migrating from
    compose". Record row counts for `backtest_runs`, `optimization_studies`, and
    `stream_outbox` before and after.
15. [ ] Human step, matching human-verifiable criterion 3: `loginctl enable-linger`,
    `systemctl --user enable q-backend.target`, reboot without logging in, then
    over SSH check `is-active` and the health endpoint.
16. [ ] Human step, matching human-verifiable criterion 4: SIGKILL each of `q-api`,
    `q-outbox-relay`, `q-market-publisher`, and `q-research-worker` in turn, and
    record the time from kill to active from the journal.
17. [ ] Human step, matching human-verifiable criterion 5: stop `q-redis`; confirm
    `q-api` is active, `/api/v1/system/health` answers 200, and
    `/api/v1/stream/jobs.progress/latest` answers 503; start `q-redis` and record
    how long the relay takes to log recovery.
18. [ ] Run the full validation suite. Commit.

## Validation

- **Unit:** notify datagrams, including abstract sockets, and the no-op path;
  schema revision comparison; relay first-success callback; publisher exit code
  78; rendered unit content, dependency table, and `ExecStart` shape; installer
  idempotence and port refusal.
- **Integration:** `q-api` is ready only once listening, not ready and exits 78
  when behind head, and ready without Redis; the worker is ready after broker
  connection and stops cleanly on SIGTERM.
- **Regression:** existing relay, publisher, API, and CLI suites pass; `uv run dev`
  and the hand-run CLIs behave as before; `ci.sh` compose fallback is unchanged.
- **Manual:** steps 13 to 17.
- **Measurement:** time to ready per unit on a clean start; time from SIGKILL to
  active per unit; relay recovery time after Redis returns.
- **Pins:** `CONTRACTS_REV` is unchanged. If step 17's handoff leads to an
  architecture doc edit for the quadlet names, that is a separate `q_contracts`
  change.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/observability/test_systemd_notify.py tests/deploy tests/streaming/test_relay_readiness.py \
  tests/cli/test_q_market_publisher_readiness.py -v
uv run pytest -m integration tests/cli/test_q_api_readiness.py tests/tasks/test_worker_readiness.py -v
scripts/install-user-units.sh --dest "$(mktemp -d)" --quadlet-dest "$(mktemp -d)" --no-sync

# human, on the target machine
sudo apt install podman
scripts/install-user-units.sh
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
systemctl --user start q-backend.target
systemd-analyze --user blame | grep -E 'q-'
systemctl --user kill -s KILL q-outbox-relay.service && journalctl --user -u q-outbox-relay.service -n 20
```

## Handoff

Report the notify-socket tests' received datagrams and the `q-api` test's
readiness timing relative to the first successful TCP connect. Report the exit
code and stderr from the schema-behind case. Report the rendered dependency
table as asserted, and state the quadlet naming deviation from architecture §8
plainly so the architecture doc can be amended. From the human steps, report the
podman version, time to ready per unit, row counts before and after the compose
migration, whether the stack came up after an unattended reboot, time from
SIGKILL to active per unit, and relay recovery time after Redis returned. List
every existing test that changed and why. Name the exact command that Q-019's
offline message should show to start the stack.
