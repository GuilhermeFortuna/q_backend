# Systemd User Units and Service Lifecycle

This document describes the production systemd user unit stack for `q_backend`. It replaces process ownership by UI wrappers or manual `docker compose` invocations, providing supervised lifecycle, exponential backoff restarts, dependency ordering, and `sd_notify` readiness signaling.

---

## 1. Stack Architecture

The backend stack runs as rootless systemd user units under the logged-in user:

| Unit | Type | Role | Dependencies | Readiness |
|---|---|---|---|---|
| `q-postgres.container` | Podman quadlet | PostgreSQL 16 database | Named volume `q-postgres-data` | `pg_isready -U q -d q` (`Notify=healthy`) |
| `q-redis.container` | Podman quadlet | Redis 7 streams & caching | None | `redis-cli ping` (`Notify=healthy`) |
| `q-migrate.service` | oneshot | Database migration to head | `Requires=q-postgres.service` `After=q-postgres.service` | Exits 0 on completion |
| `q-api.service` | notify | FastAPI HTTP server | `Requires=q-postgres.service q-migrate.service` `Wants=q-redis.service` `After=q-postgres.service q-migrate.service q-redis.service` | `sd_notify(READY=1)` once HTTP socket is bound and listening |
| `q-outbox-relay.service` | notify | Outbox to Redis stream relay | `Requires=q-postgres.service q-redis.service` `After=q-postgres.service q-redis.service` | `sd_notify(READY=1)` after first successful relay pass |
| `q-market-publisher.service` | notify | Live market data publisher | `Requires=q-redis.service` `Wants=mt5-gateway.service` `After=q-redis.service mt5-gateway.service` | `sd_notify(READY=1)` once Redis connection is confirmed |
| `q-research-worker.service` | notify | Dramatiq CPU-bound worker pool | `Requires=q-postgres.service q-redis.service` `After=q-postgres.service q-redis.service` | `sd_notify(READY=1)` when worker process connects to broker |
| `q-execution-worker.service` | notify | Forward execution worker (the only process that trades). **Not** in `q-backend.target` | `Requires=q-postgres.service` `Wants=q-redis.service mt5-edge.service` `After=` all three | `READY=1` after startup recovery and the first successful poll (leases acquired or confirmed idle); `WATCHDOG=1` once per poll |
| `q-backend.target` | target | Master stack coordinator | `Wants` all units above | Active when all wanted units are reached |

> **Note on Quadlet Names:** Quadlets are named `q-postgres` and `q-redis` (rather than generic `postgres` and `redis`) to avoid collisions in the user's rootless Podman quadlet namespace where other projects may reside.

---

## 2. Installation & Prerequisites

### Prerequisites

1. **Podman (>= 5.0):**
   Install podman:
   ```bash
   sudo apt install podman
   ```

2. **Enable User Linger:**
   Enable linger so that user systemd units start on boot and remain running without an active desktop/SSH login session:
   ```bash
   loginctl enable-linger "$USER"
   ```

3. **Configure Environment Files:**
   Environment files live outside the checkout in `~/.config/q/`:
   ```bash
   mkdir -p ~/.config/q
   cp deploy/systemd/postgres.env.example ~/.config/q/postgres.env
   cp deploy/systemd/backend.env.example ~/.config/q/backend.env
   ```
   Edit `~/.config/q/backend.env` and `~/.config/q/postgres.env` if passwords, ports, or directory roots differ from defaults.

### Running the Installer

From the repository root, run:
```bash
./scripts/install-user-units.sh
```

The installer:
- Verifies that host ports 5434 and 6380 are not held by conflicting processes (such as Docker compose).
- Syncs dependencies (`uv sync --frozen --no-dev`).
- Renders `.service.in` unit templates with absolute paths into `.venv/bin/`.
- Installs user units to `~/.config/systemd/user/` and quadlets to `~/.config/containers/systemd/`.
- Is idempotent: repeated runs do not touch unmodified unit files.
- Reloads the user systemd daemon (`systemctl --user daemon-reload`).

To verify quadlet generation without starting services:
```bash
/usr/lib/systemd/system-generators/podman-system-generator --user --dryrun
```

---

## 3. Operation

### Starting and Stopping the Stack

Start the entire stack with one command:
```bash
systemctl --user start q-backend.target
```

Enable starting automatically at system boot:
```bash
systemctl --user enable q-backend.target
```

Stop the entire stack:
```bash
systemctl --user stop q-backend.target
```

Restart a specific service (e.g. after code update):
```bash
systemctl --user restart q-api.service
```

### Inspecting Status and Logs

Check the status of the entire stack or individual services:
```bash
systemctl --user status q-backend.target
systemctl --user status q-api.service q-outbox-relay.service q-market-publisher.service q-research-worker.service
```

Follow journal logs for a service:
```bash
journalctl --user -u q-api.service -f
journalctl --user -u q-outbox-relay.service -f
journalctl --user -u q-market-publisher.service -f
journalctl --user -u q-research-worker.service -f
```

Check unit initialization timing:
```bash
systemd-analyze --user blame | grep -E 'q-'
```

---

## 3b. Execution Worker

`q-execution-worker.service` is installed with the other units but is never started by `q-backend.target`, `./research` or the research stack. Enabling trading is a separate, explicit act:

```bash
systemctl --user enable --now q-execution-worker.service   # start, and start at login
systemctl --user disable --now q-execution-worker.service  # stop trading, stop starting at login
journalctl --user -u q-execution-worker.service -f
```

- **Readiness:** the unit is `active` only after recovery completed and the first poll acquired (or confirmed no need for) leases. `TimeoutStartSec=180`.
- **Watchdog:** `WatchdogSec=30`. The worker feeds it once per poll (default poll 1 s), not per bar, so a long bar does not trigger a restart; a hung poll loop does within 30 s.
- **Exit 79 (fail-closed recovery):** startup recovery refused to let the worker trade (for example, an unavailable quote). The reason is in `systemctl --user status` (`STATUS=`) and the journal. `RestartPreventExitStatus=78 79` keeps systemd from restart-looping; fix the cause, then `systemctl --user restart q-execution-worker.service`. Exit 78 remains configuration errors.
- **Kill switch:** unchanged. It is a Postgres flag the worker reads, not a unit action.
- **Health:** each poll upserts a row in `execution_worker_heartbeats` (worker id, start time, heartbeat time, version, last edge check). `GET /api/v1/execution/health` derives `worker_status` from it: `healthy` when the heartbeat is younger than `Q_EXECUTION_HEARTBEAT_STALE_AFTER_S` (10 s), `stale` beyond that, `offline` with no heartbeat or after a clean shutdown (`stopped_at` set, so a stopped worker is distinguishable from a crashed one that goes stale). The response also carries `worker_heartbeat_age_s`, `worker_started_at` and `edge` (`reachable`, `mt5_connected`, `terminal_build`, `checked_at`) as the worker last saw it. The edge is probed at most every `Q_EXECUTION_EDGE_HEALTH_INTERVAL_S` (5 s).

```bash
watch -n1 'curl -s http://127.0.0.1:8000/api/v1/execution/health | jq "{worker_status, worker_heartbeat_age_s, edge}"'
```

---

## 4. Migrating from Docker Compose

If you currently have data stored in the Docker compose PostgreSQL volume (`postgres_data` / `q_backend_postgres_data`), follow this procedure to migrate your data into the Quadlet-managed volume `q-postgres-data` without data loss.

1. **Stop compose services:**
   ```bash
   docker compose stop postgres redis
   ```

2. **Start the quadlet PostgreSQL service:**
   ```bash
   systemctl --user start q-postgres.service
   ```

3. **Stream logical dump from Docker into Podman Quadlet:**
   ```bash
   docker compose exec -T postgres pg_dumpall -U q | podman exec -i q-postgres psql -U q -d q
   ```
   *(Or if Docker compose container is stopped, run `docker compose start postgres`, run the pipe, then stop Docker compose).*

4. **Verify row counts before and after:**
   Run the following query in both databases to confirm matching counts:
   ```sql
   SELECT 'backtest_runs' AS tbl, COUNT(*) FROM backtest_runs
   UNION ALL
   SELECT 'optimization_studies', COUNT(*) FROM optimization_studies
   UNION ALL
   SELECT 'stream_outbox', COUNT(*) FROM stream_outbox;
   ```

5. **Start the rest of the systemd stack:**
   ```bash
   systemctl --user start q-backend.target
   ```

---

## 5. Connecting to Tauri UI Research Data Roots

The Tauri desktop UI (`q_frontend`) stores research data (lake parquet files, tick caches, local market store, and runtime config) under the application data directory:
`~/.local/share/com.quant.desktop/data/`

To allow the systemd backend services to access and serve the exact same data artifacts created through the UI, set the root paths in `~/.config/q/backend.env`:

```ini
Q_DATA_LAKE_ROOT=/home/gui/.local/share/com.quant.desktop/data/lake
Q_MARKET_DATA_ROOT=/home/gui/.local/share/com.quant.desktop/data/market
Q_TICK_CACHE_DIR=/home/gui/.local/share/com.quant.desktop/data/tick_cache
Q_RUNTIME_CONFIG_PATH=/home/gui/.local/share/com.quant.desktop/data/runtime_config.json
```

Then restart the backend services:
```bash
systemctl --user restart q-api.service q-research-worker.service
```

---

## 6. Troubleshooting

### Port Held Error on Install
```
Error: PostgreSQL port 5434 is held by an existing process.
Stop existing containers with: docker compose stop postgres redis
```
**Cause:** Docker compose containers (or another service) are bound to port 5434 or 6380.  
**Resolution:** Run `docker compose stop postgres redis` before running `./scripts/install-user-units.sh`.

### Exit Code 78 (`EX_CONFIG`)
Systemd units configure `RestartPreventExitStatus=78`. When a process exits with code 78, systemd marks it as failed and does not enter a rapid restart loop, preserving error diagnostics in the journal.
- **Empty symbols in market publisher:** If `Q_STREAM_SYMBOLS` is unset or empty, `q-market-publisher` exits 78 with `"no symbols configured"`. Set `Q_STREAM_SYMBOLS` in `~/.config/q/backend.env`.
- **Schema behind head in API:** If the database schema has unapplied migrations, `q-api` logs the current and head revisions to stderr and exits 78. Run `systemctl --user start q-migrate.service` to apply migrations.

### Redis Degraded State
If Redis stops or crashes:
- `q-api` remains active and serves REST endpoints normally. Requests to stream WebSocket endpoints return HTTP 503 `stream_unavailable`.
- `q-outbox-relay` backs off exponentially and resumes relaying as soon as Redis is back online without requiring manual restart.

### Uninstalling Units
To uninstall all installed systemd units and quadlets:
```bash
./scripts/install-user-units.sh --uninstall
```
