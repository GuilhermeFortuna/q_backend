# Q-129 implementation plan: Trade order reasons

> **For implementation agents:** Read the linked specification and repository instructions. Launch only through `./work start Q-129 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** strategies label entries and closes with `reason`; the labels reach trades, exports, a sortable Trade List and a "By reason" chart.
**Architecture:**
- `TradeOrder` carries the label.
- The research adapter records it per deciding bar, next to the levels it already records.
- `candle_kernel.py` attributes it to trades with the existing decided-bar logic.
- Analytics groups by both reasons, and the contract and frontend show them.

**Tech stack:** Python 3.12, Pydantic v2, NumPy, pytest; OpenAPI and the q_contracts generators; React 19, TypeScript, Recharts, Vitest.
**Spec:** [Specification](../specs/Q-129-trade-order-reasons-spec.md)
**Status:** implemented across q_contracts, q_backend and q_frontend.

## Global constraints

- The task spans `q_contracts`, `q_backend` and `q_frontend`. Create the same task branch in `q_contracts` and `q_frontend`.
  - Consumers vendor the unpushed contract commit with `make contracts CONTRACTS_REPO=<local q_contracts clone>`.
  - Never hand-edit vendored code.
- No q_core change and no change to engine exit labels.
- Test with synthetic bars and ticks (`tmp_path` tick stores). No worker, Redis, gateway, Docker, GPU, Wine or desktop.
- Commit focused units on each task branch; no push, merge or protected-branch checkout.

## Review focus

- Exit attribution:
  - only `SIGNAL` exits take a close reason;
  - a plain close maps from `exit_bar − 1` and a priced close from `exit_bar`;
  - a stop or target hit on the same bar as a reasoned close keeps its engine label.
- Entry attribution for resting orders goes through `placed_bars`, exactly as `stop_loss` does.
- Reason lists stay aligned with trades through `merge_chunk_runs` and parallel sessions.
- The read-path recompute triggers on a missing `entry_reasons` and never writes.
- The sort comparator places nulls last in both directions and is stable for equal values.

## Ordered implementation

### 1. Contract

**Files:** `q_contracts/schema/api/openapi.yaml`, the generated outputs, and `q_contracts/tests/test_backtest_performance_contract.py`.

- [x] Rename `ExitReasonStats` to `ReasonStats`. Add the required `entry_reasons` to `BacktestPerformance`, and update the contract test fixture.
- [x] Run the generators and `make check`, then commit.

### 2. Orders, adapter and kernel attribution

**Files:**
- `src/q_backend/research/orders.py`, `src/q_backend/research/adapter.py`;
- `src/q_backend/backtesting/candle_kernel.py`, `src/q_backend/backtesting/models.py`, `src/q_backend/research/results.py`;
- tests in `tests/research/` (the order tests, the priced, resting-order and parallel-session modules).

- [x] Write failing tests for spec acceptance items 1–3.
- [x] Add `reason` and its validation to `TradeOrder` and the seven factories (`_resting` passes it on), and reject it on `amend` and `cancel`.
- [x] In `_RuntimeHooks`, record the entry and close reasons per bar in object arrays sized like `stop_price`.
- [x] Carry them into `ChunkRun` and `merge_chunk_runs`:
  - map entry reasons with `decided_bar`;
  - replace `SIGNAL` text with the close reason of the deciding bar.
- [x] Set `Trade.entry_reason` in `ledger_to_registry`, add the column to the research trades frame, and commit.

### 3. Analytics, API and CSV

**Files:**
- `src/q_backend/backtesting/analytics.py`, `src/q_backend/api/schemas/backtest.py`;
- the result endpoint in `src/q_backend/api/routers/backtest.py`;
- `src/q_backend/backtesting/run_service.py` (`TRADES_CSV_COLUMNS`);
- their tests.

- [x] Write failing tests for spec acceptance items 4 and 5.
- [x] Generalize `_exit_reason_stats` into one grouping helper used for both lists, and rename the model `ReasonStats`.
- [x] Recompute on read when the stored `performance` lacks `entry_reasons`, and add the CSV column.
- [x] Pin and vendor the contract in q_backend, run `make contracts-check`, then commit.

### 4. Frontend

**Files:**
- `CONTRACTS_REV` and the vendored contracts;
- `src/types/backtesting.ts`, `src/components/ui/DataTable.tsx`;
- `src/components/backtests/BacktestResultsTabs.tsx`, `chartUtils.ts`, `TradeDistributionPanels.tsx`, `TradeHoverCard.tsx`;
- `src/mocks/backtest.ts`, and the matching `__tests__`.

- [x] Pin and vendor the contract, add `entry_reason`, and switch the `ExitReasonStats` re-export to `ReasonStats`.
- [x] Write failing tests for spec acceptance item 6.
- [x] `DataTable`: add the optional `sortValue` and the nulls-last comparator. Existing internal-sort users must not change.
- [x] Trade List: add the Entry Reason column, and make Size, Entry Reason, Exit Reason and PnL sortable, with `sortValue` set to the displayed label for both reasons.
- [x] `formatExitReason`: map every q_core rule id and engine code, and return other strings verbatim.
- [x] Turn `ExitReasonPanel` into a "By reason" panel with `SegmentedToggle` (Entry / Exit, default Exit) and an empty-list message.
- [x] Add the Entry Reason row to the hover card, and update the mocks.
- [x] Run `make contracts-check`, `pnpm typecheck`, lint and focused vitest with `TZ=America/Sao_Paulo`, then commit.

### 5. Docs

**Files:** `docs/research/user_guide/strategy.rst`, and the `TradeOrder` docstrings (done in step 2).

- [x] Document `reason`: where each label appears and which exits keep engine labels. Add an example, then commit.

## Verification

- In q_contracts: `make check`.
- In q_backend: `make contracts-check`, the backend lint, and pytest for the research order, priced, resting-order and parallel-session modules, the analytics module, and the backtest router and run-service modules.
- In q_frontend: `make contracts-check`, `pnpm typecheck`, lint, and vitest for `DataTable`, the backtest results tabs and the trade distribution panels, with `TZ=America/Sao_Paulo`.
- Review the whole change, then run `./work board set Q-129 in-review` with the results and any open follow-ups.
