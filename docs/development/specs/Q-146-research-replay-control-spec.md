# Q-146 — Market replay control page in the Research UI

**Status:** written spec and plan awaiting human review; the Q project board is the status of record.
**Batch:** 42 — Market replay.
**Owner:** q_backend. **Repositories:** q_backend, q_contracts, q_frontend, q (launcher).
**Depends on:** Q-145 (including its full-stack launcher, clock and broker fixes).
**Plan:** [Implementation plan](../plans/Q-146-research-replay-control-plan.md).

## Purpose

Q-145 controls replay only through the `q-replay` CLI and `./dev replay`. This task lets the operator prepare,
start, drive and shut down replay runs from a new page in the Research desktop UI. It supersedes the Q-145
statement that no research UI is required. Every other Q-145 boundary stands.

Strategy deployments stay in q_terminal. The page never creates, starts, stops or flattens a deployment, reads
the control API on 8002, or shows orders and positions. q_terminal and the live control API stay free of
replay controls.

## Current system

- The simulator's administrative listener (18914) exposes create, status, commands and journal for one run per
  store. A new run needs a stopped simulator and fresh run-scoped state, which `./dev replay` provisions.
- `freeze_tickstore` freezes one symbol's TickStore days into a manifest and NPZ.
- The Research API (8001) is a long-running host process, and the UI reaches it through `apiClient` and
  TanStack Query. Workspaces register in `app/router.tsx`, `app/lazyWorkspaces.tsx` and `AppDock.tsx`.

## Design

### Run manager inside the Research API

No new daemon. A `q_backend.api.services.replay` service and `/api/v1/replay` router run inside the Research
API. The router is mounted only when `Q_REPLAY_CONTROL_ENABLED=1`, which `tools/dev/research.sh` sets in host
mode. The live API never mounts it, and neither does `--container` mode, because that container cannot reach
the host's loopback listeners or user systemd. The page treats a 404 as unavailable and says so.

The manager has one lifecycle owner: it runs the launcher as an argument list (no shell) from
`Q_REPLAY_LAUNCHER`, also set by `research.sh`. It does not reimplement provisioning. The router does not import
broker or timeline code, and the simulator and stack keep running if Research stops.

The launcher publishes `<state>/replay/current.json` (0600): phase, run directory, generation, admin URL and
per-run admin token. The launcher generates that token, and the browser never sees it. On startup the manager
rebuilds its view from this file and the admin listener, so a restarted Research API reattaches to a running run.

Launcher additions: `./dev replay --no-terminal`, `./dev replay terminal` and the state file. CLI behaviour
without the new flag is unchanged.

### API

New contracts in q_contracts under `schema/api/replay/`, reusing `run-config`, `run-state`, `command` and
`journal-entry`, and added to `openapi.yaml`:

| Route | Purpose |
| --- | --- |
| `GET /overview` | Phase (`idle`, `starting`, `ready`, `stopping`, `failed`), run state or null, last error |
| `GET /tickstore` | Symbols and session days available to freeze, read-only |
| `GET /datasets`, `POST /datasets` | List frozen datasets; freeze one symbol and day range (202, `preparing` to `ready` or `failed`) |
| `POST /runs` | Validate a config against a ready dataset and start the stack without a terminal; refused unless `idle` |
| `POST /runs/current/commands` | Forward pause, resume, speed, step or stop with `command_id` and `expected_revision` |
| `GET /runs/current/journal` | Journal page with `after_seq` and `limit` |
| `POST /stack/stop` | `./dev down replay`; run state is kept |
| `POST /stack/terminal` | Open q_terminal on the replay stack |

Freezing writes to a temporary directory and renames on success, under `Q_REPLAY_DATASETS_DIR` (default
`<state>/replay/datasets`). It never syncs or mutates TickStore. Symbol specs come from the request. The form
prefills them from existing Research instrument metadata where available, and the values are recorded in the
manifest. TickStore datasets hold one symbol; existing multi-symbol manifests in that directory can be selected.

Stale revisions, invalid configs, a busy stack and launcher failures return their contracted errors with the
reason. The admin journal route gains `after_seq` and `limit`.

### Page

`/replay` with a Replay dock entry, in `src/workspaces/replay/`, following `DESIGN.md`.

- **Setup (idle):** choose a dataset or freeze one; set interval, optional warmup, balance, account login and
  currency, the five latency values and initial speed. Start.
- **Transport (active):** resume, pause, speed (0.1x to 10x), step by market duration (enabled only while
  paused), end run, shut down stack, open terminal.
- **Status:** state, market time in exchange-local time and UTC, visible cursor, lag, queue depth, outstanding
  intents, generation, fixed latency values and last error. A failed or completed run stays on screen until the
  stack is shut down.
- **Journal:** incremental, newest first, with event kind and market time.

Each command sends a fresh `command_id` and the last revision seen. A stale refusal refreshes the status and
reports it; it is not retried. Polling is 1 s while active and 5 s while idle. A run created in `prepared` shows
resume and pause; step becomes available once paused. The setup step tells the operator to deploy from q_terminal
before resuming, but does not enforce it.

## Acceptance criteria

1. From the page, an operator freezes a TickStore range, starts a run, opens q_terminal, resumes, changes speed,
   pauses, steps, ends the run and shuts the stack down, with the state on screen matching the simulator's at
   each step.
2. CLI commands and page commands act on the same run, and the page reflects CLI changes within one poll.
3. Restarting the Research API while a run is active reattaches to it and does not affect the run. `./dev down
   research` leaves replay running.
4. The live API has no replay routes. In `--container` mode the page shows an unavailable notice.
5. No page or route creates or changes a deployment, and q_terminal gains no replay controls.
6. The browser never receives the admin token. Launcher runs without a shell, so request values cannot inject
   arguments or commands.
7. A second start while a stack is up, a stale revision, a failed launcher call and a failed freeze each show
   their reason. TickStore files are unchanged after a freeze.

## Verification

Focused pytest for the service and router with a fake launcher and a real simulator admin listener; launcher
mock tests in `tools/tests/test-replay`; Vitest with MSW for the page (`TZ=America/Sao_Paulo`); contract checks
in q_backend and q_frontend; format and lint for touched packages. One human session through acceptance 1
needs a person at the UI and is recorded in the board message. No GPU, Wine, MT5 or full CI during
implementation.

## Exclusions

Deployment controls, order and position views, charts, browsing earlier runs' journals, deleting datasets,
multi-symbol TickStore freezing, backward seek, in-place reset and container-mode support.
