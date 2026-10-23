# Q-123 implementation plan: Typed backtest performance analytics

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-123 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** Every backtest result carries a typed `performance` object, computed by one backend function and served for new and stored runs.
**Architecture:** A pure analytics module over closed trades and the existing closed-trade equity curve; job code stores its output in `result.json`; the result endpoint fills it on read for older runs. The contract gains matching schemas.
**Tech stack:** Python 3.12, FastAPI, Pydantic v2, pandas, pytest; OpenAPI and the q_contracts generators.
**Spec:** [Specification](../specs/Q-123-typed-backtest-performance-analytics-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The task spans `q_contracts`, `q_backend` and `q_frontend`. Create the same task branch in `q_contracts` and `q_frontend`.
  - Consumers vendor the unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
  - Never hand-edit vendored code.
- Legacy `metrics`, `result_summary` and optimization metrics keep their values. Shared helpers move; their behaviour does not change.
- Analytics are pure functions of trades, capital and span; they do no I/O.
- Tests use synthetic trades, the API test client and a temporary lake root. Run no worker, Redis, gateway, Docker, GPU or desktop.
- Commit focused units on each task branch; no push, merge or protected-branch checkout.

## Review focus

- Null rules for zero denominators, empty groups and short spans, and that they serialize as JSON `null`, never `NaN` or `Infinity`.
- Drawdown period boundaries: equal-equity recoveries, an unrecovered final period, and the run-end duration.
- The time-in-market union for overlapping trades.
- Timezone handling: trade times and the config start and end are normalized the way `build_equity_curve` normalizes them.
- Recompute-on-read never writes, and degrades to `null` instead of returning 500.

## Ordered implementation

### 1. Contract

**Files:** `q_contracts/schema/api/openapi.yaml`, the generated outputs, `q_contracts/schema/api/FINDINGS.md`.

- [ ] Add `BacktestPerformance` and its component schemas, and make `BacktestResponse.performance` optional and nullable.
- [ ] Run the generators and `make check`, then commit.

### 2. Analytics module

**Files:**
- New: `src/q_backend/backtesting/analytics.py`, `tests/backtesting/test_analytics.py`.
- Modified: `src/q_backend/api/schemas/backtest.py`, `src/q_backend/optimization/metrics.py`.

- [x] Write failing tests for spec acceptance items 1, 2 and 5.
- [x] Add the Pydantic models. Implement trade stats, the side split, exit reasons, the drawdown period scan and the portfolio ratios. Move the daily-return helper and keep optimization importing it. Commit.

### 3. Jobs and the result endpoint

**Files:** `src/q_backend/api/backtest_jobs.py`, `src/q_backend/api/routers/backtest.py`, and the job and router tests.

- [x] Write failing tests for spec acceptance items 3 and 4.
- [x] Add `performance` to the candle and tick payloads. In `get_backtest_result`, fill a missing `performance` from the run row's config with no write-back, and degrade to `null` when capital is unknown. Commit.

### 4. Consumers and docs

**Files:**
- q_backend: `CONTRACTS_REV` and the vendored contracts; `docs/research/user_guide/backtesting.rst`.
- q_frontend: `CONTRACTS_REV`, the vendored contracts, and `src/types/backtesting.ts`.

- [ ] Pin and vendor in both consumers, and add `performance?: BacktestPerformance | null` to the frontend type.
- [x] Document the metric definitions and basis.
- [ ] Run `make contracts-check` in both consumers and `pnpm typecheck` in q_frontend, then commit.

## Verification

- In q_contracts: `make check`.
- In q_backend: `make contracts-check`, the backend lint, and pytest for the analytics, backtest job, router and optimization metrics modules.
- In q_frontend: `make contracts-check` and `pnpm typecheck`.
- Review the whole change, then run `./work board set Q-123 in-review` with the results and any open follow-ups.
