# Q-045: Execution worker service and health

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §8, §8.1, §9 invariant 8, §10 Phase 4](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#8-service-lifecycle)  
**Depends on:** Q-042  
**Implementation plan:** [`../plans/Q-045-execution-worker-service-and-health-plan.md`](../plans/Q-045-execution-worker-service-and-health-plan.md)

## Purpose

Q-018 put the API, the relay, the market publisher and the research worker
under systemd. The execution worker, the one process that trades, is still
started by hand in a terminal. Its health is guessed. The API reports the worker
healthy whenever any lease row exists, and offline otherwise. It never reports
"stale", so a worker that hangs while holding a lease looks healthy until the
lease expires. Nothing reports whether the worker can reach the edge. §8.1
makes the terminal's controls depend on exactly these facts: worker down
disables start and deploy, and an unreachable edge disables flatten. This task
runs the worker as the unit §8 describes, and makes its health something the
worker reports rather than something the API infers.

## Requirements

### Unit

- A systemd user unit runs the execution worker. It requires Postgres, wants
  Redis and the execution edge, and starts after all three.
- The unit is ready only after startup recovery has completed and every
  running deployment has a lease acquired or was confirmed idle. A worker whose
  recovery fails closed exits with a status that systemd does not restart in a
  loop, and says why.
- A worker whose poll loop stops making progress is restarted by systemd's
  watchdog. A long bar does not trigger it, because the watchdog is fed per
  poll, not per bar.
- The execution worker is not part of the backend target that `./research` and
  the research stack start. Enabling trading is a separate, explicit act.
- The install script and the operations docs cover the unit.

### Heartbeat and health

- The worker records a heartbeat in Postgres on every poll: its identifier, when
  it started, the time of the heartbeat, its version, and the last result of
  its edge health check (reachable, terminal connected, terminal build).
- The execution health endpoint derives worker status from that heartbeat:
  healthy within a declared freshness bound, stale beyond it, offline with no
  heartbeat or after a clean shutdown. It reports the edge status the worker
  last saw, and the heartbeat's age.
- A clean shutdown records itself, so an operator can tell a stopped worker
  from a crashed one.
- The OpenAPI capture in `q_contracts` is refreshed with the new health fields.

## Constraints and non-goals

- **No change to trading behaviour, leases, recovery or reconciliation.**
- **The kill switch stays a Postgres flag the worker reads.** It does not go
  through the unit.
- **No multi-worker coordination.** One worker, as today. The heartbeat table is
  keyed by worker identifier, so that a second worker would be visible, not
  supported.
- **The Wine units are unchanged.** `mt5-edge.service` exists since Q-040.

## Acceptance criteria

### Agent-verifiable

1. The unit file passes the unit-structure tests: `Requires=` Postgres,
   `Wants=` Redis and `mt5-edge`, matching `After=`, `Type=notify`, a
   `WatchdogSec=`, `Restart=on-failure` with backoff, and
   `RestartPreventExitStatus=` for the fail-closed exit. It is absent from
   `q-backend.target`.
2. With a notify socket, the worker sends `READY=1` only after recovery and
   lease acquisition, sends `WATCHDOG=1` once per poll, and exits with the
   declared status when recovery fails closed.
3. Health reports healthy with a fresh heartbeat, stale after the bound
   elapses without one, and offline with none or after a clean shutdown. The
   edge fields reflect the last check.
4. A worker with a lease but a stopped poll loop is reported stale, where today's
   code reports it healthy.
5. The recaptured OpenAPI contains the new fields, and `make contracts-check`
   passes.
6. The full validation suite passes.

### Human-verifiable

1. The worker runs under systemd with the edge up. Health shows it healthy with
   the terminal build. Stopping `mt5-edge` shows the edge unreachable within
   two polls. `kill -STOP` on the worker shows it stale, and the watchdog
   restarts it.
   Command: `systemctl --user start q-execution-worker && watch -n1 'curl -s http://127.0.0.1:8000/api/v1/execution/health | jq "{worker_status, worker_heartbeat_age_s, edge}"'`
