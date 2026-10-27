# Q-133: Priced entries after same-bar exits in research backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 36 — Priced entries after same-bar exits
**Depends on:** Q-132
**Implementation plan:** [Plan](../plans/Q-133-priced-entries-after-same-bar-exits-research-plan.md)

## Purpose

Q-132 changes the candle kernel so that a priced entry on a bar where a trade closed intrabar fills only from a
trade at or after that exit. Otherwise the entry is refused with a reason. This task brings the fix into
`q_backend`:
- it pins the Q-132 release;
- it reports why each entry was refused;
- it documents the rule for strategy authors.

## Current system

- `pyproject.toml` and `uv.lock` pin `q-core` at `v2026.10.25`.
- `candle_kernel.run_chunk` (`src/q_backend/backtesting/candle_kernel.py`) copies the `rejected_*` arrays named
  in a fixed tuple. `rejected_entries_frame` (`src/q_backend/research/results.py`) turns them into
  `result.rejected_entries`, with the columns `REJECTED_COLUMNS`. Its docstrings describe only wrong-side
  levels.
- `docs/research/user_guide/execution.rst`, section "Priced entries", says the engine checks only the price
  range. The `TradeOrder.price` docstring says a priced market entry "fills on the same bar".
- Rejected entries are not published or imported, so no contract changes.

## Behaviour

- `q-core` is pinned at the Q-132 release tag.
- `result.rejected_entries` gains a `reason` column (`object`), after `take_profit`:
  - `level_on_wrong_side` for kernel reason `0`;
  - `touch_before_exit` for kernel reason `1`.

  Any other code raises `ValueError` naming the code, so a newer kernel cannot be misreported. The empty frame
  has the same columns and dtypes.
- Session-parallel runs (`workers=`) join the reason with the other rejected arrays.
- Docs:
  - "Priced entries" in `execution.rst` states the rule. On a candle where a stop, target, breakeven or priced
    close closed a trade, a priced entry fills only from a trade at or after that exit. Otherwise it appears in
    `result.rejected_entries` with reason `touch_before_exit`. The existing causality warning stays.
  - The `TradeOrder.price` and `BacktestResult.rejected_entries` docstrings and the `backtesting.rst`
    `rejected_entries` row say the same thing.
- Research results for strategies that never place a priced entry on a bar with an intrabar exit do not change.

## Acceptance criteria

1. A research strategy on hand-built bars and a `tmp_path` tick store holds a long that is stopped mid-candle.
   On that candle it returns a priced long at a price that traded only before the stop: no trade opens, and
   `rejected_entries` has one row with reason `touch_before_exit`.
2. The same strategy, with the price trading again after the stop, opens the long at that price. Its stop and
   target resolve from the trades after the new touch.
3. A wrong-side level refusal reports `level_on_wrong_side`. An unknown code raises `ValueError`.
4. `rejected_entries` has the same columns and reasons with `workers=1` and `workers=2` on a multi-session
   fixture.
5. Existing tests pass unchanged, including `tests/research/test_priced_entries.py`,
   `tests/research/test_resting_orders_backtest.py`, `tests/research/test_stop_target_backtest.py` and
   `tests/research/test_parallel_sessions.py`.
6. The docs listed above describe the rule and the reason column.

## Out of scope

- Kernel behaviour (Q-132).
- Publishing rejected entries, and any change to the UI or contracts.
- Changing the experiment strategies in `experiments/`. The implementing agent reruns the `IND$N`
  `BracketMomentumStrategy` from `experiments/strategies/ind_strategies.py` (on the `research/experiments`
  branch) once on the new pin, and reports its
  trades, PnL, drawdown and the count of `touch_before_exit` refusals in the board message.
