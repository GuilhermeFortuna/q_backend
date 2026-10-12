# Q-107 implementation plan: Intrabar replay with incomplete tick history

> **For implementation agents:** Read the linked specification and the `AGENTS.md` and `README.md` of all three repositories. Use superpowers:executing-plans when available. Launch only through `./work start Q-107 --agent <agent> --worktree` after written-plan approval. Implement natively; delegation requires separate authorization.

**Goal:** Breakeven backtests complete on incomplete MetaTrader 5 tick history by falling back to one-minute bars per minute, mark trades decided on substituted prices, and report it in a Notes section.
**Architecture:** `MarketTickReplay` assembles each replayed candle's path from deals reconciled against M1 bars and records substitutions in a report shared by the replays of one run. The job layer classifies trades from that report and adds notes to the result. One contract field carries the notes to the results view.
**Tech stack:** Python 3.12, numpy, pandas, pytest; OpenAPI and the `q_contracts` generators; React 19, TypeScript, Vitest.
**Spec:** [Specification](../specs/Q-107-intrabar-replay-with-incomplete-tick-history-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- One task, three repositories. `./work start` creates the task branch in `q_backend`. Create the same-named branch in `q_contracts` and `q_frontend` as worktrees under `.worktrees/<repository>/<branch>`; `./work inspect` and `./work finish` discover every repository that has it.
- Contracts flow one way. Commit the schema change in `q_contracts` first, set `CONTRACTS_REV` in each consumer to that commit and vendor with `make contracts CONTRACTS_REPO=<path to the q_contracts worktree>`. Never hand-edit vendored code. The pin resolves remotely once the human runs `./work finish Q-107 --push`.
- Real ticks are used wherever a minute is complete; substitution is per minute and never widens beyond the minutes that need it.
- Incomplete or inconsistent history never raises. Provider and gateway errors still do.
- The substitution order, offsets, one-second boundary margin, `exit_resolution` values and the note code are exactly as the spec states.
- `q_frontend` tests run with `TZ=America/Sao_Paulo`.
- Commit focused units on the task branches; no push, merge or protected-branch checkout.

## Review focus

- Volume-pinned assignment at boundaries: a deal moves to the previous minute only when that makes both minutes reproduce their M1 bars.
- Prices are snapped to the tick grid before comparison, as today.
- The assembled path starts at the candle's open, ends at its close and spans its high and low in every branch.
- Substituted timestamps are unique within a run so that `exit_tick_time` identifies them exactly.
- The note's counts are derived from the classified trades, not counted separately.
- A stored result without `notes` still loads in the backend and the results view.

## Ordered implementation

### 1. Contract field (`q_contracts`)

**Files:** Modify `schema/api/openapi.yaml`, the generated outputs and the contract tests that cover `BacktestResponse`.

- [x] Add `BacktestNote` and the optional `notes` array on `BacktestResponse`; add or extend a test asserting a response with and without `notes` validates.
- [x] Regenerate with the repository's generator and run `make check`.
- [x] Commit; record the commit hash for the consumers.

### 2. Replay (`q_backend`)

**Files:** Modify `src/q_backend/backtesting/intrabar.py`; extend `tests/backtesting/test_breakeven_intrabar.py`.
**Interfaces:** `MarketTickReplay.__call__(bar)` keeps its return type. A `ReplayReport` shared by the replays that `breakeven_market_inputs` creates records, per run, the candles replayed, the candles with a substitution, the affected session dates and the substituted timestamps with their resolution. `breakeven_market_inputs` returns it beside `intrabar_factory`.

- [x] Write failing tests for spec acceptance 1 to 5 and 8 with a fake service that serves M1 bars and deals.
- [x] Implement the per-minute assembly: M1 read for the candle, deal read extended to cover the boundary margin and the session-close case, volume-pinned reconciliation, substitution, candle-level fallback, and the report.
- [x] Remove the two `ValueError`s and update the class docstring to the new rule.
- [x] Run `uv run pytest tests/backtesting -q` and confirm it passes.
- [x] Commit this unit.

### 3. Classification and notes (`q_backend`)

**Files:** Modify `src/q_backend/backtesting/models.py`, `src/q_backend/api/backtest_jobs.py`, `CONTRACTS_REV`, the vendored `contracts/`, the response model that mirrors `BacktestResponse`, and their tests. `src/q_backend/ml_filters/evaluation.py` only needs to accept the extra return value.
**Interfaces:** `Trade.exit_resolution: Optional[str]`. Candle results gain `notes: list[dict]`.

- [x] Pin and vendor the contract commit; run `make contracts-check CONTRACTS_REPO=<path>`.
- [x] Write failing tests for spec acceptance 6 and 7, and for a stored result without `notes`.
- [x] Set `exit_resolution` on closed trades from `exit_tick_time` and the report; build the `intrabar_incomplete_ticks` note; add `notes` to the candle payload and an empty list to the tick payload.
- [x] Run `uv run pytest tests/backtesting tests/api -q` and confirm it passes. Result: 1181 passed; the remaining 3 failures and 3 errors in `tests/api/test_stream_replay.py` need Postgres on port 5434, which is not running in this environment.
- [x] Commit this unit.

### 4. Results view (`q_frontend`)

**Files:** Modify `CONTRACTS_REV`, the vendored contracts, `src/types/backtesting.ts`, `src/components/backtests/BacktestResultsTabs.tsx`, `src/components/backtests/TradeHoverCard.tsx`, the trades table component, `src/mocks/backtest.ts` and their tests. Add a small `BacktestNotes` component beside them.

- [x] Pin and vendor the contract commit; run `make contracts-check CONTRACTS_REPO=<path>`.
- [x] Write failing component tests for spec acceptance 10 and for the exit-resolution label.
- [x] Implement the Notes section and the label, following the existing results components and design tokens.
- [x] Run the repository's unit tests and type check with `TZ=America/Sao_Paulo`.
- [x] Commit this unit.

## Verification and handoff

- [x] Run each repository's focused checks listed above plus its lint and format checks on the touched paths.
- [x] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [x] Note in the handoff that `q_contracts` must be pushed with the consumers (`./work finish Q-107 --push`) for remote `contracts-check` to resolve the pin.
- [ ] Use `./work board set Q-107 in-review -m "<changes per repository; checks and results; follow-ups>"`.

### Recorded results

- `q_contracts` (`04cd4af`): `make check` passed (251 passed, 7 skipped; black, ruff, validate, generator diff clean).
- `q_backend` (`17c5c11`): `make contracts-check CONTRACTS_REPO=<q_contracts worktree>` exit 0; `uv run ruff check src` passed; black clean on touched files; `uv run pytest tests/backtesting/test_breakeven_intrabar.py` 28 passed; `tests/backtesting` and `tests/api` 1181 passed. Not passing here: `tests/api/test_stream_replay.py`, and `tests/optimization` (6 tests) which need Postgres on port 5434. The full `./scripts/ci.sh` was not run (it starts disposable Postgres and Redis containers under Docker).
- `q_frontend` (`99b4e07`): `make contracts-check CONTRACTS_REPO=<q_contracts worktree>` exit 0; `pnpm typecheck` passed; eslint and prettier clean on touched files; `TZ=America/Sao_Paulo pnpm test:run` 219 files, 1074 tests passed.

### Follow-ups

- `q_contracts` must be pushed with the consumers (`./work finish Q-107 --push`) so the pins in `q_backend` and `q_frontend` resolve remotely.
- A real deal stamped at exactly a substituted timestamp (to the millisecond) would be labelled with the substitution's resolution; the spec's timestamps are not guarded against real deals.

