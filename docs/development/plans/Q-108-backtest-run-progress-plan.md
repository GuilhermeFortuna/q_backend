# Q-108 implementation plan: Backtest run progress

> **For implementation agents:** Read the linked specification and the `AGENTS.md` and `README.md` of all three repositories. Use superpowers:executing-plans when available. Launch only through `./work start Q-108 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** A running backtest reports its phase, simulation progress, current activity and last update, and the Research UI shows them in the results area and the dock.
**Architecture:** A reporter owned by the job layer builds the status payload, throttles it and writes it through the existing `set_job_progress`. The job calls it at phase boundaries; `MarketTickReplay` notifies it per replayed candle through an observer. Optional fields on `BacktestStatusResponse` carry the payload to a `BacktestRunProgress` component.
**Tech stack:** Python 3.12, pydantic, pytest; OpenAPI and the `q_contracts` generators; React 19, TypeScript, TanStack Query, Vitest.
**Spec:** [Specification](../specs/Q-108-backtest-run-progress-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Q-107 must be merged into `development` first; this task builds on its `ReplayReport`.
- One task, three repositories. `./work start` creates the task branch in `q_backend`. Create the same-named branch in `q_contracts` and `q_frontend` as worktrees under `.worktrees/<repository>/<branch>`; `./work inspect` and `./work finish` discover every repository that has it.
- Contracts flow one way. Commit the schema change in `q_contracts` first, set `CONTRACTS_REV` in each consumer to that commit and vendor with `make contracts CONTRACTS_REPO=<path to the q_contracts worktree>`. Never hand-edit vendored code. The pin resolves remotely once the human runs `./work finish Q-108 --push`.
- Reporting is observational: it never raises into the run, never changes a result, and adds no provider reads.
- Field names, phase values, messages, the 500 ms throttle and the 10 s stall threshold are exactly as the spec states.
- Reuse the existing transport (`set_job_progress`, `jobs.progress`, the stream-aware status query). No new endpoint, topic or polling loop.
- `q_frontend` tests run with `TZ=America/Sao_Paulo`.
- Commit focused units on the task branches; no push, merge or protected-branch checkout.

## Review focus

- The replay report is published before the candle's M1 and tick reads, so a stalled read leaves a status that names the candle.
- The throttle uses an injected monotonic clock and never drops a phase change.
- `progress` and `message` sit at the top level of the Redis payload so `publish_job_progress` maps them unchanged.
- `started_at` is written once by the API and preserved by every later report.
- A status without the new fields still renders: the database fallback in `get_status_payload` and the UI's indeterminate state.
- The stall timer is driven by observed changes of `updated_at`, not by comparing server and client clocks.
- The display reuses the `DiscoverProgress` tokens and classes; no new colours or one-off animation.

## Ordered implementation

### 1. Contract fields (`q_contracts`)

**Files:** Modify `schema/api/openapi.yaml`, the generated outputs and the contract tests that cover `BacktestStatusResponse`.

- [ ] Add the nine optional fields, with `phase` as a closed enumeration; add or extend a test asserting a status with and without them validates and an unknown phase is rejected.
- [ ] Regenerate with the repository's generator and run `make check`.
- [ ] Commit; record the commit hash for the consumers.

### 2. Reporter and job phases (`q_backend`)

**Files:** Add `src/q_backend/api/backtest_progress.py` and `tests/api/test_backtest_progress.py`. Modify `src/q_backend/api/backtest_jobs.py`, `src/q_backend/api/schemas/backtest.py`, `CONTRACTS_REV`, the vendored `contracts/` and the status tests.
**Interfaces:** `BacktestProgressReporter(run_id, started_at, *, publish, clock)` with `phase(name, message, **fields)` and `replay(bar, bars_total, candle_time, replayed, substituted)`. `publish` defaults to the existing `_persist_progress` path and `clock` to `time.monotonic`.

- [ ] Pin and vendor the contract commit; run `make contracts-check CONTRACTS_REPO=<path>`.
- [x] Write failing tests for spec acceptance 1, 2, 5, 6, 7 and 8 with a fake Redis client and an injected clock.
- [x] Implement the reporter: payload assembly, the throttle, non-decreasing `progress`, and the preserved `started_at`.
- [x] Extend `BacktestStatusResponse`; have `start_job` report `queued`, and `run_backtest_job`, `_execute_candle` and `_execute_tick` report `loading_data`, `preparing`, `simulating` and `saving` at the boundaries the spec names.
- [x] Run `uv run pytest tests/api -q -k backtest` and confirm it passes.
- [x] Commit this unit.

### 3. Replay reports (`q_backend`)

**Files:** Modify `src/q_backend/backtesting/intrabar.py` and `src/q_backend/api/backtest_jobs.py`; extend `tests/backtesting/test_breakeven_intrabar.py`.
**Interfaces:** `MarketTickReplay` and `breakeven_market_inputs` accept an optional `on_replay: Callable[[int, int, pd.Timestamp], None]` called with the bar index, the frame length and the candle's open time before the candle's history is read. The default is no observer, so the ML-filter evaluation and other callers are unchanged.

- [x] Write failing tests for spec acceptance 3 and 4, asserting the observer fires before the fake service is read.
- [x] Call the observer in `MarketTickReplay.__call__` for a candle not served from the one-candle cache; wire it in `_execute_candle` to `reporter.replay` with the counts from the run's `ReplayReport`.
- [x] Run `uv run pytest tests/backtesting tests/api -q -k "intrabar or backtest"` and confirm it passes.
- [x] Commit this unit.

### 4. Progress display (`q_frontend`)

**Files:** Add `src/components/backtests/BacktestRunProgress.tsx` and its test. Modify `CONTRACTS_REV`, the vendored contracts, `src/api/queries/backtests.ts`, `src/hooks/useActiveJobs.ts`, `src/components/backtests/focus/BacktestFocusWorkbench.tsx`, `src/components/backtests/focus/CollapsedResultsTeaser.tsx`, `src/mocks/handlers.ts` and their tests.

- [ ] Pin and vendor the contract commit; run `make contracts-check CONTRACTS_REPO=<path>`.
- [ ] Write failing tests for spec acceptance 10, 11 and 12, using fake timers for the elapsed time and the stall line.
- [ ] Expose the running status from `useBacktestJob`; implement `BacktestRunProgress` and use it in the expanded results area; update the teaser and `useActiveJobs`.
- [ ] Step the mock run through the phases in `src/mocks/handlers.ts`.
- [ ] Run the repository's unit tests and type check with `TZ=America/Sao_Paulo`.
- [x] Commit this unit.

## Verification and handoff

- [ ] Run each repository's focused checks listed above plus its lint and format checks on the touched paths.
- [ ] Record the commands actually run and their results in this plan; do not claim unrun checks passed.

**Backend record (`q_backend`, commits `40dcc92`, `5e10460`):**

- Steps 1 and 4 (`q_contracts` schema, `q_frontend` display) and the `CONTRACTS_REV` pin are **not done**: the `q_contracts` and `q_frontend` worktrees for `Q-108-backtest-run-progress` do not exist, and the launch scope covered only the `q_backend` worktree.
- `BacktestStatusResponse` is hand-written in `q_backend` (`src/q_backend/api/schemas/backtest.py`) and is not vendored from contracts, so the backend fields do not depend on the pin.
- Tests were written alongside the implementation in the same unit, not observed failing first.
- `uv run pytest -p no:warnings -m "not integration"`: 2700 passed, 15 skipped, 7 failed. All 7 failures are `Connection refused` on the local Postgres port 5434 (`test_stream_replay::test_job_snapshot_route_validates`, `test_genetic_search`, `test_hypothesis::test_genetic_seeding_preserves_diversity`, `test_parallel_genetic`, `test_prescreen`), an environment gap, not caused by this change.
- Streaming integration tests (`tests/streaming`, `tests/storage` marked integration) need the isolated CI Postgres and Redis and were not run here; `./scripts/ci.sh` (the pre-push hook) was not run.
- Focused run after the final change: `tests/api/test_backtest_progress.py`, `tests/backtesting/test_breakeven_intrabar.py`, persistence, router, payload, multi-entry, parity, local candle, ML filter, local/API tick backtest, redis progress and streaming progress tests: 132 passed.
- `make contracts-check`: exit 0 against the unchanged `CONTRACTS_REV`.
- `ruff check` (`src`, touched tests) and `black --check` / `ruff format --check` on touched files: clean.
- Remaining: `q_contracts` commit and pin (step 1), the `q_frontend` display (step 4), and the handoff note that `q_contracts` must be pushed with the consumers (`./work finish Q-108 --push`).
- [ ] Note in the handoff that `q_contracts` must be pushed with the consumers (`./work finish Q-108 --push`) for remote `contracts-check` to resolve the pin.
- [ ] Use `./work board set Q-108 in-review -m "<changes per repository; checks and results; follow-ups>"`.
