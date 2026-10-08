# Q-096 implementation plan: Research guide: costs, execution, ticks and history depth

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-096 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** The research guide states how backtests execute and cost, what tick rows mean and how deep history goes; the examples show their costs.
**Architecture:** Four guide sections backed by named tests, a cost flag on two examples, and an operator note.
**Tech stack:** Markdown, Python 3.12, argparse, pytest.
**Spec:** [Specification](../specs/Q-096-research-guide-costs-execution-ticks-and-history-depth-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Write only what a test in this repository demonstrates or what the spec gives as a dated measurement. Do not restate a measurement as a general guarantee.
- Use the exit-rule wording delivered by Q-094 and the series section delivered by Q-095; link to them instead of repeating them.
- No change to `backtest()`, the loaders, the gateway, defaults or presets. Example behaviour with no flag is unchanged apart from the two printed lines.
- Tests use synthetic frames and mocked loaders. No Wine, live terminal, Docker, GPU or desktop run.
- Commit focused units on the task branch; no push, merge or protected-branch checkout.

## Review focus

- Each execution statement matches the engine, including the inclusive day-trade entry window and the two different forced-close prices (open at the close time, close on the last bar of the day).
- The cost formula's units are right: reais per contract per side.
- The tick section does not suggest any quote-age filter.
- The history section separates what the platform controls from what the terminal and broker control.

## Ordered implementation

### 1. Pin any execution statement that lacks a test

**Files:** Modify `tests/research/test_backtest.py`.

- [x] List the execution statements from the spec and find the existing test for each in `tests/research/test_backtest.py`, `tests/backtesting/test_engine.py` and `tests/backtesting/test_exit_rules.py`. Record the mapping in this plan.
- [x] For each statement with no test, add one focused case on a small synthetic frame: likely candidates are the inclusive day-trade entry window, a skipped opposite entry while a position is open, the same-bar close-and-reverse, and overnight carry without `day_trade`.
- [x] Run `uv run pytest tests/research/test_backtest.py -q`. These tests describe existing behaviour and should pass without a source change; a failure means the statement is wrong, so correct the statement, not the engine.
- [x] Commit the tests.

#### Execution statement to test mapping

1. Strategy hooks see completed bars only. An entry or close decided on a bar fills at the next bar's open.
   - `tests/backtesting/test_engine.py::test_engine_fills_at_next_bar_open_not_signal_bar_close`
   - `tests/backtesting/test_engine.py::test_engine_sequential_swing_trade`
   - `tests/research/test_backtest.py::test_parity_with_direct_engine_calls`
2. Exit rules follow the catalog text from Q-094: evaluated on each completed bar against its high or low, closing at the next bar's open, so the exit price can differ from the level. A rule can trigger on the entry bar.
   - `tests/backtesting/test_exit_rules.py::test_stop_loss_long_exits_at_next_bar_open`
   - `tests/backtesting/test_exit_rules.py::test_take_profit_long_exits_at_next_bar_open`
   - `tests/backtesting/test_exit_rules.py::test_stop_triggered_on_entry_bar_exits_at_next_open`
3. One position per symbol under fixed-quantity sizing: repeated entry requests do not stack, and an opposite entry request is skipped while the position cap is full. Returning a close and an opposite entry on the same bar reverses at the next open.
   - Repeated entry requests do not stack: `tests/backtesting/test_engine.py::test_engine_caps_position_at_risk_model_size`
   - Skipped opposite entry while position is open: `tests/research/test_backtest.py::test_opposite_entry_skipped_while_position_open`
   - Same-bar close and reverse: `tests/research/test_backtest.py::test_same_bar_close_and_reverse`
4. With `day_trade=True`: an entry is taken only from a signal bar whose time lies between the start and end times inclusive; open positions close at the open of the first bar at or after the close time; a position still open on the last bar of a calendar day closes at that bar's close.
   - Inclusive entry window bounds (start and end times inclusive): `tests/research/test_backtest.py::test_day_trade_inclusive_entry_window_and_forced_close_prices`
   - Forced close at open of first bar at or after close time: `tests/backtesting/test_engine.py::test_engine_day_trade_hours`, `tests/research/test_backtest.py::test_day_trade_inclusive_entry_window_and_forced_close_prices`
   - Forced close at close of last bar of calendar day: `tests/backtesting/test_engine.py::test_engine_parallel_day_trade`, `tests/backtesting/test_engine.py::test_engine_sequential_day_trade`, `tests/research/test_backtest.py::test_day_trade_inclusive_entry_window_and_forced_close_prices`
5. Without `day_trade`, positions carry across sessions. `force_close_at_end` closes at the last bar's close.
   - Carry across sessions: `tests/backtesting/test_engine.py::test_engine_sequential_swing_trade`, `tests/research/test_backtest.py::test_overnight_carry_without_day_trade`
   - Force close at end: `tests/research/test_backtest.py::test_open_trade_and_unforced_vs_forced_close`, `tests/research/test_backtest.py::test_overnight_carry_without_day_trade`
6. `equity` is realized only, as the guide already says.
   - `tests/research/test_backtest.py::test_realized_equity_equals_initial_capital_plus_closed_pnl`


### 2. Show costs in the examples

**Files:** Modify `examples/research/rsi_reversion.py`, `examples/research/mt5_backtest.py` and `tests/research/test_examples.py`.
**Interfaces:** `--cost-per-contract FLOAT` (default `0.0`) on both scripts, forwarded as `TransactionCostConfig(cost_per_contract=...)`; both print total commission and, at zero, one line saying no transaction costs were applied.

- [x] Add failing tests for the flag, the forwarded configuration, the reported commission and the zero-cost line.
- [x] Run `uv run pytest tests/research/test_examples.py -q` and confirm they fail for the missing flag.
- [x] Implement the flag and the two printed lines.
- [x] Run the same command and confirm it passes.
- [x] Commit this unit.

### 3. Write the guide sections

**Files:** Modify `docs/research-library.md`.

- [x] Write "Execution model", "Transaction costs", "Reading tick rows" and "History depth and completeness" from the spec, in that order, each as short declarative statements. Put the measurement context (symbol, period, sample) next to each measured number.
- [x] Extend the `exit_params` and `costs` rows of the configuration table to point at the new sections, and add the cost flag to the example commands.
- [x] Check the cost example arithmetic by hand and state the result in the plan record.
- [x] Commit the guide.

#### Cost example arithmetic verification
- Mini dollar future (`WDO$N`): `point_value = 10.0` R$/point, `tick_size = 0.5` points.
- Spread = 1 tick = 0.5 points. Half-spread = 0.5 × 0.5 = 0.25 points = 0.25 × 10.0 = R$2.50 per contract per side.
- Assumed fee per side = R$1.25 per contract.
- Total per-side cost = fee_per_side + 0.5 × tick_size × point_value = 1.25 + 2.50 = 3.75 (`cost_per_contract = 3.75` R$ per contract per side).
- Round trip cost (entry + exit) = 2 × R$3.75 = R$7.50 per contract.
- In points: R$7.50 / 10.0 R$/point = 0.75 points.
- All numbers match the spec and guide text.

### 4. Add the operator note

**Files:** Modify `docs/mt5-wine-gateway.md`.

- [ ] Add the "History depth" section and the troubleshooting row from the spec. Unless a human has confirmed the steps on the target machine, say in the section that it records expected MetaTrader 5 behaviour and give the `/v1/available_range` command that confirms it.
- [ ] Commit the note.

## Verification and handoff

- [ ] Run `uv run pytest tests/research -q`, `uv run ruff check examples/research tests/research` and `uv run black --check` on the same paths.
- [ ] Read the four guide sections once against the statement-to-test mapping and remove anything unsupported.
- [ ] Record the commands actually run, their results and the mapping in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-096 in-review -m "<changes; checks and results; follow-ups>"`. Say in the message whether the operator steps were confirmed on the target machine.
