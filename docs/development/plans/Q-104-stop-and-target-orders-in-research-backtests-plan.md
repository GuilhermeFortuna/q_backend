# Q-104 implementation plan: Stop and target orders in research backtests

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-104 --agent <agent> --worktree` after written-plan approval and with Q-102 and Q-103 Done. Implement natively; delegation requires separate authorization.

**Goal:** A research entry order carries a stop and a target price that `backtest()` fills inside the bar from a `TickStore`.
**Architecture:** `TradeOrder` gains two levels; the research adapter compiles them into two columns; the candle bridge forwards them and a price callable backed by the store to the `q_core` kernel; results gain the fill time, the levels and the rejected entries.
**Tech stack:** Python 3.12, pandas, numpy, `q_core` (tag containing Q-102), pytest.
**Spec:** [Specification](../specs/Q-104-stop-and-target-orders-in-research-backtests-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- The fill rule lives in `q_core`. This task adds no price comparison of its own beyond the frame-versus-store check.
- Levels and the price source reach the kernel only from `q_backend.research`. `BacktestEngine` callers that pass neither take today's path unchanged.
- Results for strategies without levels are byte-for-byte unchanged, including the empty-frame path.
- `exit_time` stays a timestamp of the frame's index.
- Tests use stores written under `tmp_path`; no gateway.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- The bar interval handed to the store is `[index[i], index[i + 1])`, and for the last bar it ends with that bar's calendar day, so the timeframe is never guessed.
- The frame-versus-store check runs on every bar whose prices are loaded and its error names the bar.
- A level set on a bar whose entry is later skipped by the position cap neither opens a trade nor appears in `rejected_entries`.
- `ledger_to_registry` still asserts that kernel and registry profit agree for protective exits.
- The public export list and the import test agree, and importing `q_backend.research` stays inert.
- `publish()` output is unchanged for a result that has the new columns.

## Ordered implementation

### 1. Pin the kernel and extend the order

**Files:** Modify `pyproject.toml`, `uv.lock`, `src/q_backend/backtesting/candle_kernel.py` (`check_engine`), `src/q_backend/research/orders.py`, `tests/research/test_research_strategy.py` and the `check_engine` test.
**Interfaces:** `TradeOrder.buy(*, stop_loss=None, take_profit=None)`, `TradeOrder.sell(...)`.

- [ ] Move the `q-core` pin to the released tag that contains Q-102 and record the tag here.
- [ ] Add failing tests for spec acceptance item 9 and for `check_engine` rejecting a module without `PROTECTIVE_ORDERS`.
- [ ] Implement, run `uv run pytest tests/research/test_research_strategy.py tests/backtesting -q` and confirm green. Commit this unit.

### 2. Carry levels to the kernel

**Files:** Modify `src/q_backend/backtesting/signal_columns.py`, `src/q_backend/backtesting/candle_kernel.py` (`run_chunk`, `ChunkRun`, `ledger_to_registry`), `src/q_backend/backtesting/engine.py`, `src/q_backend/backtesting/models.py` (`Trade` gains optional `exit_tick_time`, `stop_loss`, `take_profit`) and `src/q_backend/research/adapter.py`; add `tests/research/test_stop_target_backtest.py`.
**Interfaces:** two optional level columns in the signal contract; `BacktestEngine(..., intrabar=None)` forwarded to `run_chunk`.

- [ ] Add failing tests for spec acceptance items 1 to 3 and 6 to 8 with a counting store.
- [ ] Have the adapter record each entry order's levels in the two columns, and the bridge forward them with the callable. Map `exit_time_us` to `exit_tick_time` in Brasília time.
- [ ] Implement the price callable in `src/q_backend/research/engine.py`: bar interval, `TickStore.trade_prices`, the frame-versus-store check, and the missing-session error.
- [ ] Run `uv run pytest tests/research tests/backtesting -q` and confirm green, including the unchanged candle goldens. Commit this unit.

### 3. Expose results and errors

**Files:** Modify `src/q_backend/research/engine.py`, `src/q_backend/research/results.py`, `tests/research/test_stop_target_backtest.py` and `tests/research/test_backtest.py`.
**Interfaces:** `backtest(..., ticks=None)`; `BacktestResult.rejected_entries`; `trades` columns `exit_tick_time`, `stop_loss`, `take_profit`.

- [ ] Add failing tests for spec acceptance items 4 and 5.
- [ ] Implement the argument checks, the new columns on both the normal and the empty-frame path, and `rejected_entries`.
- [ ] Run `uv run pytest tests/research tests/execution -q` and confirm green with unchanged evaluator tests. Commit this unit.

### 4. Document

**Files:** Create `examples/research/stop_target_backtest.py`; modify `docs/research-library.md` and `tests/research/test_examples.py`.

- [ ] Update the four guide sections and add the example as the spec lists, with an example test that runs it over a `tmp_path` store.
- [ ] Run `uv run pytest tests/research/test_examples.py -q`. Commit docs and example.

## Verification and handoff

- [ ] Run `uv run pytest tests/research tests/backtesting tests/execution -q`, `uv run ruff check src tests examples/research` and `uv run black --check` on the changed paths.
- [ ] Record the commands actually run and their results in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-104 in-review -m "<changes; checks and results; follow-ups>"`.

`make contracts-check` is not needed: this task changes no contract.
