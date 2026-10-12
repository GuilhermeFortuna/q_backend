# Q-108: Backtest run progress

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 21 — Backtest run progress
**Depends on:** Q-107
**Repositories:** `q_backend` (owner), with optional status fields in `q_contracts` and the progress display in `q_frontend`, delivered by the same task on same-named branches.
**Implementation plan:** [Plan](../plans/Q-108-backtest-run-progress-plan.md)

## Purpose

After **Run Backtest** the Research UI shows a spinner and "Simulating strategy over historical data..." until the run ends. The worker publishes exactly two things for a backtest: `running` when the job is accepted and `completed` or `failed` at the end (`_persist_progress` in `src/q_backend/api/backtest_jobs.py`). The dock states the gap in `q_frontend/src/hooks/useActiveJobs.ts`: "A single backtest has no granular progress".

A run on 2026-10-12 (`WSP$` H1, four years, Breakeven Stop) took 5 minutes 16 seconds. The logs show where the time went; the UI showed none of it:

| Wall time | What the worker was doing | Duration |
| --- | --- | --- |
| 07:14:21 – 07:15:39 | Waiting for the first candle read while the gateway served another request | 78 s |
| 07:15:39 – 07:16:44 | First tick read of the replay, stalled while the terminal lost its broker connection | 65 s |
| 07:17:00 – 07:17:42 | One tick read stalled while the terminal reconnected | 42 s |
| 07:17:42 – 07:19:36 | About 250 candles replayed at two per second | 114 s |

For three of those minutes nothing distinguished a run waiting on market data from a hung one.

The transport already exists. `set_job_progress` stores a status payload in Redis and publishes it on `jobs.progress`; the Research UI refetches the job status when a progress event arrives and falls back to polling once a second. Optimization, walk-forward and strategy search report through it. A backtest only needs to say something.

After this task a running backtest reports its phase, how far the simulation has advanced, what it is reading, and when it last made progress, and the Research UI shows that in the results area and in the dock.

## Behaviour

### Reported status (`q_backend`)

`GET /api/v1/backtest/{run_id}` returns the fields below beside `run_id`, `status` and `error`. All are optional and `null` when unknown, so a status served from the database row alone (a finished run, or one whose Redis entry expired) stays valid.

| Field | Type | Meaning |
| --- | --- | --- |
| `phase` | `queued`, `loading_data`, `preparing`, `simulating`, `saving` | The step the run is in |
| `progress` | number 0 to 1, or `null` | Share of the run's bars the simulation has passed; `null` when the phase has no measurable extent |
| `message` | string | One line for display, for example `Replaying ticks · 2023-03-22` |
| `started_at` | timestamp | When the API accepted the run |
| `updated_at` | timestamp | When the worker last reported |
| `simulated_until` | timestamp, or `null` | Open time of the last candle the simulation is known to have reached |
| `bars_total` | integer, or `null` | Candles in the run |
| `candles_replayed` | integer | Candles replayed on intrabar history so far |
| `candles_substituted` | integer | Replayed candles that used a substituted price so far (Q-107) |

Phases, in order:

| Phase | Entered | `progress` | `message` |
| --- | --- | --- | --- |
| `queued` | The API accepts the run | `null` | `Waiting for a worker` |
| `loading_data` | The worker picks the job up and reads candles (candle engine) or ticks (tick engine) | `null` | `Loading H1 candles for WSP$` |
| `preparing` | Market data is loaded; the strategy, any ML filter and the indicators are built | `null` | `Preparing strategy and indicators` |
| `simulating` | The engine runs | See below | See below |
| `saving` | The engine has returned; metrics, the equity curve and the lake artifacts are written | `null` | `Saving results` |

During `simulating`:

- A candle run whose strategy has no intrabar replay, and every tick-engine run, reports `progress` `null` and `Simulating N candles` or `Simulating N ticks`. The kernel decides those bars in one call and does not report between them.
- A candle run with intrabar replay reports each time the kernel asks for a candle: `progress` is that candle's position among the run's bars, `simulated_until` its open time, the two counters come from the run's `ReplayReport`, and `message` is `Replaying ticks · <candle date>`. The report is made before the candle's history is read, so a slow read is visible as a status that names the candle and then stops changing.
- `progress` never decreases within a run.

Reports are throttled to one every 500 ms. A phase change is always reported. Reporting is best effort as today: a Redis failure is logged at debug level and never fails or slows the run beyond the bounded publish.

The Redis payload carries `progress` and `message` at top level, so `publish_job_progress` maps them onto the existing `jobs.progress` event without a stream schema change. Terminal behaviour is unchanged: `completed` and `failed` are reported as today and the terminal status comes from the database row.

### Contract (`q_contracts`)

`BacktestStatusResponse` in `schema/api/openapi.yaml` gains the nine optional fields above; `phase` is a closed enumeration of the five values. Generated code is refreshed with the repository's generator. `schema/stream/payloads/job-progress.schema.json` is unchanged.

### Progress display (`q_frontend`)

`useBacktestJob` exposes the status fields of the running job. A `BacktestRunProgress` component renders them, following the active-run panel that `DiscoverProgress` established (`quant-panel--active-run`, the live status dot, the brass progress bar) so the two read as one system.

Expanded results area (`BacktestFocusWorkbench`), replacing the spinner:

- A heading with the live status dot: `Backtest queued` or `Backtest running`, and the strategy, symbol and timeframe of the request.
- The five phases as a stepper: completed, current and pending steps are visually distinct and the current step is named.
- A progress bar: determinate with a percentage when `progress` is a number, indeterminate otherwise. It carries `role="progressbar"` with `aria-valuenow` only when determinate.
- The `message`, and the elapsed time since `started_at`, updated every second.
- When `candles_replayed` is above zero: `Simulated to <date>`, the number of candles replayed, and the number substituted when above zero.
- A stall line when the client has received no changed `updated_at` for 10 seconds while the run is `loading_data` or `simulating`: `Waiting for market data · no update for 42 s`. It disappears with the next update. The interval is measured on the client from the moment it last saw `updated_at` change, so clock differences do not matter.

Collapsed results teaser (`CollapsedResultsTeaser`): the current phase name and the percentage when determinate, in place of "Simulating…".

Dock (`useActiveJobs`): a running backtest is `determinate` with the percentage when `progress` is a number and `indeterminate` otherwise; its caption is `message`, falling back to `Running…` when the status carries none.

A status without the new fields (an older backend, or the database fallback) renders as today's indeterminate state with `Running…`. Animations respect `prefers-reduced-motion`.

The mock backend (`src/mocks/handlers.ts`) steps a mock run through the phases so the display can be exercised without a worker.

## Focused acceptance

1. A status read right after submission has `phase == "queued"`, `started_at` set and `progress` `null`.
2. A candle run without intrabar replay reports `loading_data`, `preparing`, `simulating` and `saving` in that order before `completed`, with `bars_total` set from `simulating` on.
3. A candle run with intrabar replay reports, for a replayed candle, `progress` equal to its position among the run's bars, `simulated_until` equal to its open time and `candles_replayed` and `candles_substituted` matching the `ReplayReport`; the report is published before the candle's history is read.
4. `progress` is non-decreasing across the reports of one run.
5. With a clock advanced by less than 500 ms between replayed candles only the first is published; a phase change inside that window is still published.
6. A failing Redis client does not fail the run and the result is unchanged.
7. A tick-engine run reports the four worker phases with `progress` `null`.
8. A status served from the database row alone validates with every new field `null`.
9. `make check` passes in `q_contracts`; `make contracts-check` passes in `q_backend` and `q_frontend` against the task's contract commit.
10. `BacktestRunProgress` renders the stepper with the current phase marked, a determinate bar with the percentage for a numeric `progress`, an indeterminate bar for `null`, and the replay counters only when `candles_replayed` is above zero.
11. The stall line appears after 10 seconds without a changed `updated_at` in `loading_data` or `simulating`, and disappears on the next change; it never appears in other phases.
12. The dock entry is determinate with the percentage for a numeric `progress` and shows `message` as its caption; a status without the new fields renders the indeterminate `Running…` state in the dock and in the results area.

Backend tests use a fake market-data service and a fake Redis client with an injected clock. No live terminal, Wine, Docker, GPU or desktop run is required.

## Delivery boundary

- Reporting only. No backtest cancellation, no remaining-time estimate (replay time per candle depends on the provider and is too uneven to extrapolate), and no change to what a run computes or stores.
- No per-bar progress from inside the kernel. A candle run without replay and a tick-engine run stay indeterminate while simulating; those calls take milliseconds to seconds.
- No change to the `jobs.progress` stream schema, the job snapshot, or the other job kinds.
- The stall line reports that updates stopped; diagnosing the gateway or terminal is out of scope. A read that returns no ticks because the terminal was disconnected is still indistinguishable from missing history and is substituted as Q-107 specifies; telling them apart is recorded for a later task.
- `q_terminal` and research scripts are unchanged. `research/progress.py` keeps its own terminal display.
