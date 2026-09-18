# Q-045 implementation plan: Execution worker service and health

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Specification:** [`../specs/Q-045-execution-worker-service-and-health-spec.md`](../specs/Q-045-execution-worker-service-and-health-spec.md)  
**Depends on:** Q-042

## Current-system context

`deploy/systemd/user/` holds the Q-018 units as `.in` templates
(`q-api`, `q-migrate`, `q-outbox-relay`, `q-market-publisher`,
`q-research-worker`) and `q-backend.target`, which `Wants=` all of them.
`scripts/install-user-units.sh` substitutes `@Q_BACKEND_DIR@` and installs
them. `tests/deploy/test_units.py` checks dependencies, `Type=notify`, restart
policy and exit statuses, and `tests/deploy/notify_socket.py` fakes the notify
socket. `observability/systemd.py` provides `notify(state)`, `notify_ready()`,
`notify_status(text)` and `EX_CONFIG`, used by `q_api`, `q_outbox_relay` and
`q_market_publisher`.

`cli/q_execution.py::cmd_run` builds components and calls `worker.run()`.
`ExecutionWorker.run` (`execution/worker.py`) runs `recovery.recover`, raises
`RuntimeError` on `failed_closed`, reconciles pending orders, then loops
`poll_once` with `shutdown_event.wait(interval)`. `_refresh_deployments`
acquires leases inside `poll_once`, so "leases acquired" first becomes true
after the first poll. `WorkerHealth` is in memory only.

`api/services/execution.py::get_execution_health` sets
`worker_status = "healthy" if active_leases > 0 else "offline"`, and never
`stale`. `market_data_status` comes from the MT5 client. After Q-042 the worker
has an `EdgeClient` with `health()`.

## Interfaces produced

```
deploy/systemd/user/q-execution-worker.service.in
    Requires=q-postgres.service; Wants=q-redis.service mt5-edge.service
    After=q-postgres.service q-redis.service mt5-edge.service
    Type=notify; NotifyAccess=main; WatchdogSec=30; Restart=on-failure; RestartSec=1; RestartSteps=5
    RestartMaxDelaySec=30; RestartPreventExitStatus=78 79; TimeoutStartSec=180
    (not listed in q-backend.target; [Install] WantedBy=default.target)
alembic/versions/<date>_<next>_execution_worker_heartbeat.py   (next free revision at implementation time)
    execution_worker_heartbeats(worker_id PK, started_at, heartbeat_at, stopped_at NULL, version,
                                edge_reachable bool, edge_mt5_connected bool NULL, edge_terminal_build int NULL,
                                edge_checked_at)
```

```python
# src/q_backend/observability/systemd.py   (changed)
EX_FAILED_CLOSED = 79
def notify_watchdog() -> bool: ...

# src/q_backend/storage/db/execution_repositories.py   (changed)
def record_worker_heartbeat(session, *, worker_id, started_at, version, edge: EdgeHealthSnapshot, now) -> None: ...
def record_worker_stopped(session, *, worker_id, now) -> None: ...
def get_worker_heartbeat(session, worker_id: str | None = None) -> ExecutionWorkerHeartbeat | None: ...

# src/q_backend/execution/worker.py   (changed)
class ExecutionWorker:
    on_ready: Callable[[], None] = lambda: None        # called once, after recovery and the first successful poll
    on_poll: Callable[[], None] = lambda: None         # called after every poll, success or handled failure
# poll_once records the heartbeat, with the edge health checked at most every edge_health_interval_s

# src/q_backend/api/schemas/execution.py   (changed)
class EdgeStatusResponse: reachable: bool; mt5_connected: Optional[bool]; terminal_build: Optional[int]; checked_at: Optional[datetime]
ExecutionHealthResponse += worker_heartbeat_age_s: Optional[float]; worker_started_at: Optional[datetime]; edge: EdgeStatusResponse

# src/q_backend/storage/settings.py   (changed)
execution_heartbeat_stale_after_s: float = 10.0
execution_edge_health_interval_s: float = 5.0
```

```
tests/deploy/test_units.py                     extended for the new unit
tests/execution/test_worker_heartbeat.py       new: heartbeat writes, stopped marker, on_ready ordering
tests/cli/test_q_execution_notify.py           new: READY after recovery+first poll, WATCHDOG per poll, exit 79
tests/api/test_execution_health.py             extended: healthy / stale / offline / stopped, edge fields
docs/operations/                               execution worker unit section
q_contracts: schema/api/openapi.yaml, COMPAT.md    on branch Q-045-execution-worker-service-and-health
```

## Implementation decisions

- **Ready after the first successful poll, not after recovery alone.** §8
  says ready means recovery completed and a lease acquired or confirmed idle.
  Leases are taken in `_refresh_deployments`, inside `poll_once`, so the first
  poll that returns without an exception is the earliest moment that holds.
  `on_ready` fires once, there.

- **A fail-closed recovery exits with 79, not 1.** `RuntimeError` from recovery
  is mapped in `cmd_run` to `EX_FAILED_CLOSED = 79`, with the message as
  `STATUS=` and in the log. `RestartPreventExitStatus=78 79` stops systemd from
  looping a worker that refuses to trade for a reason a restart does not fix,
  such as an unavailable quote at startup. 78 stays for configuration errors,
  as in the other units.

- **Watchdog per poll, with `WatchdogSec=30`.** The default poll interval is 1 s,
  and Q-031 measured a full bar path under 50 ms at p95. Thirty seconds
  tolerates a slow Postgres or edge call without restarting, while catching a
  hung loop within a lease TTL (30 s default). `on_poll` fires in `run`'s loop
  after `poll_once` returns or its exception is logged, so a poll that fails
  and is handled still counts as progress. A hang does not.

- **The heartbeat is a separate table, not the lease.** Leases exist per
  deployment and only while one runs, so a worker with nothing to trade looks
  offline under today's rule. The heartbeat exists per worker and says the loop
  is alive. It is an upsert per poll: one row, one index.

- **The edge health check is rate-limited** to `execution_edge_health_interval_s`,
  so a 1 s poll does not double the edge's request load. The last result is
  carried forward on the heartbeat.

- **Stale after 10 s by default.** That is well above the 1 s poll and well below
  the 30 s lease TTL, so the terminal shows "stale" before leases expire and
  before the watchdog fires. `stopped_at` set, or no row, means offline.

- **Not in `q-backend.target`.** `./research` and the research stack must never
  start a trading process as a side effect. The unit installs with
  `WantedBy=default.target`, and the operations doc says to enable it
  explicitly.

## Ordered implementation

- [ ] 1. Work on the branch `Q-045-execution-worker-service-and-health` in
   `q_backend`, created from `development` by `./work start`. Confirm Q-042 is
   merged.
- [ ] 2. Write the migration, model and repository functions, with tests for
   upsert and the stopped marker. Commit.
- [ ] 3. Write failing tests for the heartbeat in `test_worker_heartbeat.py`, and for
   health status in `test_execution_health.py`, including criterion 4.
   Implement the heartbeat in `poll_once`, `record_worker_stopped` in
   `_shutdown`, and the health derivation. Confirm they pass. Commit.
- [ ] 4. Write failing notify tests with `tests/deploy/notify_socket.py`. Add
   `on_ready` and `on_poll` to the worker, and `notify_watchdog` and exit 79
   to `cmd_run`. Confirm they pass. Commit.
- [ ] 5. Write `q-execution-worker.service.in`, extend `install-user-units.sh`
   and `test_units.py` (criterion 1, including absence from the target).
   Commit.
- [ ] 6. Document the unit in `docs/operations/`: enable and disable, logs,
   meaning of exit 79, the watchdog, and how health reads. Update `README.md`.
   Commit.
- [ ] 7. Recapture the OpenAPI in `q_contracts` on the branch
   `Q-045-execution-worker-service-and-health`, and update `COMPAT.md`. Rebase
   and capture again if another batch-07 recapture merged first. Commit in
   `q_contracts`.
- [ ] 8. Run `scripts/ci.sh`. Fix, re-run, commit.
- [ ] 9. **Human:** human-verifiable criterion 1.

## Validation

- **Unit:** health derivation at the boundary of the stale bound; exit-status
  mapping.
- **Integration:** notify sequence against a fake socket; heartbeat through a
  real poll loop with a fake edge; unit structure.
- **Regression:** `tests/execution`, `tests/api`, `tests/deploy`;
  `make contracts-check`.
- **Manual:** unit start, edge stop, watchdog restart after `SIGSTOP`.

```bash
cd /home/gui/projects/q/q_backend
scripts/ci.sh
uv run pytest tests/deploy tests/execution/test_worker_heartbeat.py tests/cli/test_q_execution_notify.py \
  tests/api/test_execution_health.py -q

# human (step 9)
scripts/install-user-units.sh && systemctl --user daemon-reload
systemctl --user start q-execution-worker
systemctl --user stop mt5-edge; kill -STOP "$(systemctl --user show -p MainPID --value q-execution-worker)"
journalctl --user -u q-execution-worker -f
```

## Handoff

Give the unit file. Report the notify sequence the test observed. Report the
health responses for healthy, stale, offline and stopped. Report the recapture
commit. From the human step, give the time from edge stop to
`reachable: false`, the time from `SIGSTOP` to stale and to the watchdog
restart, and the journal line of the restart.
