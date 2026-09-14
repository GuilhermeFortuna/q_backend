# Q-018: systemd user units and readiness

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §8, §8.1, §9 invariant 8](https://github.com/GuilhermeFortuna/q_contracts/blob/d71ad64f11e7129549fa5ec8515875bdcec74cb0/docs/system-architecture.md#8-service-lifecycle)  
**Depends on:** Q-011, Q-013  
**Implementation plan:** [`../plans/Q-018-systemd-user-units-and-readiness-plan.md`](../plans/Q-018-systemd-user-units-and-readiness-plan.md)

## Purpose

The backend has no supervisor. Postgres and Redis start from `docker compose`,
either by hand, from `scripts/ci.sh`, or from the Tauri shell. The shell also
runs `uv sync` and migrations and spawns the API and the Dramatiq worker as its
own children, so closing the research UI stops the backend. The outbox relay
(Q-011) and the market publisher (Q-013) are long-lived processes that nothing
starts, and nothing restarts them when they crash. None of these processes tells
anything when it is actually ready. This task makes every backend service a
systemd user unit with dependencies, restart policy, and a readiness signal that
means the service can do its job, as the Wine gateway already is. Q-019 can then
remove process ownership from the shell, and `q_terminal` has services to connect
to that exist independently of any UI.

## Requirements

### Units

- Postgres, Redis, a one-shot migration step, the API, the outbox relay, the
  market publisher, and the research worker each run as a systemd user unit.
  Postgres and Redis run as rootless containers defined as podman quadlets.
- One target starts the whole backend stack with a single command. Enabling it
  starts the stack at boot without an active login session.
- Postgres and Redis keep the host ports, credentials, database name, and image
  major versions that the compose stack uses today, so every existing default
  connection setting keeps working unchanged.
- Postgres data survives stopping, restarting, and upgrading its unit.
- The unit dependency graph matches the architecture's service-lifecycle table.
  Services that can run degraded without a dependency want it rather than
  require it, so stopping Redis does not stop the API.
- Units carry no secrets and no machine-specific paths in the repository.
  Machine-specific values come from one environment file outside the checkout.
- No unit runs a process through a wrapper that would become the unit's main
  process in place of the service itself.

### Readiness

- A unit reports ready only when its service can serve its purpose:
  - Postgres when it accepts connections.
  - Redis when it answers a ping.
  - The migration step when the schema is at head.
  - The API when it is accepting HTTP connections, is connected to Postgres, and
    has confirmed the schema is at head.
  - The relay after its first successful pass, whether that pass appended
    entries or confirmed an empty backlog.
  - The market publisher when its Redis connection is confirmed.
  - The research worker when a worker process is connected to the broker.
- The API does not report ready, and exits with a clear message, when the schema
  is behind head. It never serves requests against an unmigrated database.
- The API reports ready when Redis is unavailable, because REST works without it.
- The market publisher reports ready when the MT5 gateway is unavailable,
  because its degraded behavior (Q-013) is to keep running and retry.
- Every readiness signal is a no-op when the process is not started by systemd,
  so `uv run` by hand behaves as it does today.

### Restart and shutdown

- Every long-running unit restarts on failure with increasing delay, up to a
  cap of about thirty seconds.
- A process that fails because of invalid configuration, such as a publisher
  with no symbols, stops without being restarted in a loop, and the reason is
  visible in the unit status.
- Stopping any unit sends a termination signal, and each service shuts down
  cleanly within its stop timeout. The worker's timeout allows a running actor
  to finish or be interrupted, and interrupted jobs are reconciled by the API's
  existing startup reconciliation.

### Installation and operation

- One repository command installs or updates the units for the current
  checkout, and another removes them. Neither needs root, and both are
  idempotent.
- Installation refuses to proceed while the compose Postgres or Redis
  containers hold the stack's ports, and names the command that stops them.
- There is a documented, tested procedure to move the existing compose Postgres
  data into the quadlet volume without loss. The same document says how to
  point the units at the lake, tick cache, and runtime-config paths the Tauri
  shell used, so research data the shell wrote stays visible.
- `scripts/ci.sh` uses the installed units to bring up Postgres and Redis when
  they are not running, and falls back to compose only when the units are not
  installed.
- Logs for every service are available through the user journal.

## Constraints and non-goals

- **No change to the Tauri shell.** It keeps spawning compose and `uv` until
  Q-019. That is why the compose file and its ports are kept working in this
  task.
- **The compose file is not deleted.** It remains for the containerized profile
  and for the shell until Q-019. Removing it is a follow-up once nothing refers
  to it.
- **No execution worker unit and no MT5 edge unit.** The execution edge does not
  exist yet (phase 4), and the execution worker's readiness depends on lease
  semantics that belong with it.
- **No change to the Wine gateway units.** The market publisher only declares a
  soft ordering on them.
- **No watchdog.** `WATCHDOG=1` keepalives are tempting because the notify
  socket is already there, but a stuck-loop detector needs a heartbeat inside
  each loop and its own failure analysis.
- **No timer for the catalog sweep or outbox pruning.** The relay already
  prunes, and the catalog sweep (Q-017) is a later task.
- **No container image for backend services, no system-level units, no
  Kubernetes-style manifests.** The services run from the checkout's virtual
  environment as the logged-in user.
- **No Redis persistence.** Redis is fan-out and bounded replay only (§4.3), and
  the compose stack does not persist it either.
- **No change to service behavior beyond readiness and exit codes.** Poll
  intervals, backoff inside the relay and publisher, and API routes are
  unchanged.

## Acceptance criteria

### Agent-verifiable

1. The readiness helper sends exactly `READY=1` to the socket named by
   `NOTIFY_SOCKET`, including abstract socket names, and does nothing when the
   variable is unset.
2. Started with a notify socket against a migrated database, the API sends
   `READY=1` only once a TCP connection to its port succeeds. Against a
   database one revision behind head, it exits non-zero, prints the current and
   head revisions, and sends nothing.
3. The API sends `READY=1` with Redis unreachable.
4. The relay sends `READY=1` after its first successful pass on an empty
   outbox, and sends nothing while Redis is unreachable.
5. The market publisher sends `READY=1` with Redis reachable and the gateway
   unreachable. With no symbols configured, it exits with the configuration
   status code that the unit excludes from restart.
6. A research worker started under the unit's command line sends `READY=1`
   after a worker process connects to the broker.
7. Rendered unit files pass `systemd-analyze --user verify`. Each `ExecStart`
   is an absolute path into the checkout's virtual environment and not a
   `uv run` or shell wrapper.
8. The installer, run against a temporary destination, renders every unit with
   no unexpanded placeholder, is idempotent (a second run changes no file), and
   refuses to install while the stack's ports are held by a non-unit process.
9. A test asserts that the dependency directives of every rendered unit match
   the architecture table rows for the units in scope.
10. The full validation suite passes.

### Human-verifiable

1. The quadlets generate valid services on the target machine, and the whole
   stack reaches active on a clean start. Time to ready per unit is reported
   from the journal.
   Command: `/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun && systemctl --user start q-backend.target && systemd-analyze --user blame | grep -E 'q-'`
2. Existing compose Postgres data is migrated with the documented procedure, and
   row counts for the backtest, optimization, and outbox tables match before
   and after.
   Command: `$EDITOR docs/operations/systemd-user-units.md` (follow "Migrating from compose")
3. After a reboot with linger enabled and no login, the stack is active, and the
   API health endpoint answers.
   Command: `systemctl --user is-active q-backend.target && curl -s localhost:8000/api/v1/system/health`
4. Killing each long-running service's main process with SIGKILL leads to a
   restart within the configured delay, and the time is recorded per unit.
   Command: `systemctl --user kill -s KILL q-outbox-relay.service && journalctl --user -u q-outbox-relay.service -n 20`
5. Stopping Redis leaves the API active and serving REST, the stream endpoint
   answers 503, and restarting Redis recovers the relay without manual action.
   Command: `systemctl --user stop q-redis.service && curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/api/v1/stream/jobs.progress/latest`
