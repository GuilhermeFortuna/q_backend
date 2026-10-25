# Q-130 implementation plan: Trade excursion analytics

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-130 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** every closed trade carries deal-accurate MFE, MAE, MFE capture, time to MFE and post-exit move, with an explicit resolution. The values reach records, exports, performance aggregates and the Research UI.
**Architecture:**
- A post-simulation pass in `q_backend.backtesting.excursions` measures trades from bars plus an optional deal source. Whole bars come from their high and low; only partial and extreme bars read deals.
- Three deal-source adapters cover the three run paths: `MarketTickReplay` for service runs, `TickStore` for research, and the tick engine's arrays.
- Analytics aggregates the stored per-trade values, so stored runs need no bars to recompute.
- Each trade also stores the deal extremes of its exit bar after the exit. With the stored bars, these let a synchronous endpoint recompute the post-exit window for any bar count without reading deals. The Research UI uses that endpoint for its Post-exit bars control.
- The contract and the frontend expose the results.

**Tech stack:** Python 3.12, Pydantic v2, NumPy, pandas, pytest; OpenAPI and the q_contracts generators; React 19, TypeScript, Recharts, Vitest.
**Spec:** [Specification](../specs/Q-130-trade-excursion-analytics-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The task spans `q_contracts`, `q_backend` and `q_frontend`. Create the same task branch in `q_contracts` and `q_frontend`.
  - Consumers vendor the unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
  - Never hand-edit vendored code.
- No q_core change. Measurement never changes fills, PnL or existing trade fields.
- Test with synthetic bars and deals, a fake deal source and `tmp_path` tick stores. No worker, Redis, gateway, MT5, Docker, GPU, Wine or desktop.
- Commit focused units on each task branch; no push, merge or protected-branch checkout.

## Review focus

- **Path boundaries.** Partial entry and exit bars include only deals at or after the entry moment and at or before the exit moment. The entry and exit fill prices are always on the path. Check the fixtures that prove this changes the answer.
- **Fill conventions.** These are pinned against `run_candle`, not assumed: market entries at the bar open, open-fill closed-bar exits, and tick-timed intrabar exits.
- **Resolution.** It is the coarsest source used, and a `"candle"` fallback is never reported as `"tick"`.
- **Bounded reads.** No interior bar is read, a partial bar is read only when it could move an extreme, and each bar is read at most once.
- **Post-exit truncation.** The window is cut at `post_exit_bars`, at the day-trade close and at the end of the data.
- **Read-path recompute.** It triggers on a missing `excursions` and never writes.
- **One post-exit computation.** Run-time measurement and `/post-exit` share `post_exit_from_stored`, and the parity test compares them across exit kinds and day-trade truncation.
- **Frontend control.** Requests are debounced, out-of-range values are never sent, and a stale response must not overwrite a newer one.

## Ordered implementation

### 1. Contract

**Files:** `q_contracts/schema/api/openapi.yaml`, the generated outputs, and `q_contracts/tests/test_backtest_performance_contract.py`.

- [x] Add the new `TradeStats` fields, `ExcursionSummary`, `BacktestPerformance.excursions` (required), `BacktestRequest.post_exit_bars`, and `measuring` in the phase enums.
- [x] Add `GET /api/v1/backtests/{run_id}/post-exit` with `PostExitAnalysis`, and the optional `post_exit_bars` on the trades export. Update the contract test fixture.
- [x] Run the generators and `make check`, then commit.

### 2. Measurement module

**Files:** `src/q_backend/backtesting/excursions.py` (new), `src/q_backend/backtesting/models.py`, and `tests/backtesting/test_excursions.py` (new).

- [x] Write failing tests for spec acceptance items 1, 3, 4 and 5 with a fake, call-counting deal source.
- [x] Add the `Trade` fields.
- [x] Implement the deal-source protocol: one bar's times, prices and resolution, cached per bar.
- [x] Implement `measure_trades`:
  - per-trade entry and exit moments;
  - vectorised whole-bar extremes;
  - pruned partial-bar reads;
  - extreme-bar timing;
  - the exit-bar rest extremes;
  - the post-exit window with its truncation, through `post_exit_from_stored`;
  - the coarsest resolution.
- [x] Commit.

### 3. Run-path integration

**Files:**
- `src/q_backend/backtesting/intrabar.py`: a measurement replay factory that is independent of the breakeven gate;
- `src/q_backend/api/backtest_jobs.py`: the candle and tick paths, and the `measuring` phase;
- `src/q_backend/backtesting/tick/engine.py`;
- `src/q_backend/research/engine.py` and `src/q_backend/research/results.py`;
- `src/q_backend/backtesting/run_service.py` (`TRADES_CSV_COLUMNS`);
- tests: `tests/backtesting/test_excursions.py` (kernel conventions), `tests/backtesting/tick/test_tick_engine.py`, `tests/research/test_backtest.py`, `tests/research/test_publishing.py`, `tests/api/test_backtest_progress.py`, `tests/api/test_backtest_import.py`.

- [x] Write failing tests for spec acceptance items 2, 6, 7 and 9.
- [x] Candle jobs:
  - reuse the run's replay when one exists;
  - otherwise build one for measurement only;
  - report progress per measured trade.
- [x] Tick jobs: measure from the engine's tick arrays.
- [x] Research `backtest()`: measure with the `TickStore` when one is given, and from bars only otherwise. Add `post_exit_bars` and the frame columns.
- [x] Add the CSV columns and `BacktestRequest.post_exit_bars`, then commit.

### 4. Analytics and API

**Files:** `src/q_backend/backtesting/analytics.py`, `src/q_backend/api/schemas/backtest.py`, `src/q_backend/backtesting/run_service.py`, and `tests/backtesting/test_analytics.py` and `tests/api/test_backtest_performance.py`.

- [x] Write failing tests for spec acceptance item 8.
- [x] Add the `TradeStats` aggregates and `ExcursionSummary`. Recompute on read when `excursions` is missing, taking `post_exit_bars` from the run config (default 10).
- [x] Pin and vendor the contract in q_backend, run `make contracts-check`, then commit.

### 5. Post-exit recalculation endpoint

**Files:** `src/q_backend/api/routers/backtest.py`, `src/q_backend/backtesting/run_service.py`, `src/q_backend/api/schemas/backtest.py`, and `tests/api/test_backtest_post_exit.py` (new).

- [x] Write failing tests for spec acceptance item 6, including the parity test against a fresh run and a market-data service that fails on any call.
- [x] Read the stored trades and `market_data` artifacts and the run config, call `post_exit_from_stored`, and aggregate per side. Add `post_exit_bars` to the trades CSV export.
- [x] Commit.

### 6. Frontend

**Files:**
- `CONTRACTS_REV` and the vendored contracts;
- `src/types/backtesting.ts`;
- `src/components/backtests/BacktestResultsTabs.tsx` (Trade List), `TradeHoverCard.tsx`, `TradeDistributionPanels.tsx` (the excursions panel);
- `src/api/queries/backtests.ts`: `usePostExitAnalysis` and the export parameter;
- `src/lib/backtesting/backtestProgress.ts`;
- `src/mocks/backtest.ts`, and the matching `__tests__`.

- [x] Pin and vendor the contract, and add the `Trade` fields.
- [x] Write failing tests for spec acceptance item 10.
- [x] Add the five sortable columns with the resolution marker, the hover-card rows, the Trade excursions panel (aggregates, two scatters, resolution note), and the `measuring` phase label.
- [x] Add the Post-exit bars control: per-run view state, a debounced `usePostExitAnalysis` that keeps previous data while loading, Reset, and an inline error. Feed the selected figures to the panel, the Post-exit column, the hover card and the CSV export.
- [x] Update the mocks. Run `make contracts-check`, `pnpm typecheck`, lint and focused vitest with `TZ=America/Sao_Paulo`, then commit.

### 7. Docs

**Files:** `docs/research/user_guide/`, plus the docstrings in `excursions.py` and `research/engine.py`.

- [x] Document the definitions, the resolution levels and when each occurs, `post_exit_bars` and recalculating it from the results, with a short research example. Commit.

## Verification

- In q_contracts: `make check`.
- In q_backend: `make contracts-check`, the backend lint, and pytest for `tests/backtesting/test_excursions.py`, `test_analytics.py`, `tests/backtesting/tick/test_tick_engine.py`, `tests/research/test_backtest.py`, `test_publishing.py`, and `tests/api/test_backtest_performance.py`, `test_backtest_post_exit.py`, `test_backtest_progress.py`, `test_backtest_import.py`.
- In q_frontend: `make contracts-check`, `pnpm typecheck`, lint, and vitest for the backtest results tabs, the trade distribution panels, the hover card, the backtest queries and the backtest progress helpers, with `TZ=America/Sao_Paulo`.
- Review the whole change, then run `./work board set Q-130 in-review` with the results and any open follow-ups.
