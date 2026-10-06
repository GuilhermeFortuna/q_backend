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

- [ ] List the execution statements from the spec and find the existing test for each in `tests/research/test_backtest.py`, `tests/backtesting/test_engine.py` and `tests/backtesting/test_exit_rules.py`. Record the mapping in this plan.
- [ ] For each statement with no test, add one focused case on a small synthetic frame: likely candidates are the inclusive day-trade entry window, a skipped opposite entry while a position is open, the same-bar close-and-reverse, and overnight carry without `day_trade`.
- [ ] Run `uv run pytest tests/research/test_backtest.py -q`. These tests describe existing behaviour and should pass without a source change; a failure means the statement is wrong, so correct the statement, not the engine.
- [ ] Commit the tests.

### 2. Show costs in the examples

**Files:** Modify `examples/research/rsi_reversion.py`, `examples/research/mt5_backtest.py` and `tests/research/test_examples.py`.
**Interfaces:** `--cost-per-contract FLOAT` (default `0.0`) on both scripts, forwarded as `TransactionCostConfig(cost_per_contract=...)`; both print total commission and, at zero, one line saying no transaction costs were applied.

- [ ] Add failing tests for the flag, the forwarded configuration, the reported commission and the zero-cost line.
- [ ] Run `uv run pytest tests/research/test_examples.py -q` and confirm they fail for the missing flag.
- [ ] Implement the flag and the two printed lines.
- [ ] Run the same command and confirm it passes.
- [ ] Commit this unit.

### 3. Write the guide sections

**Files:** Modify `docs/research-library.md`.

- [ ] Write "Execution model", "Transaction costs", "Reading tick rows" and "History depth and completeness" from the spec, in that order, each as short declarative statements. Put the measurement context (symbol, period, sample) next to each measured number.
- [ ] Extend the `exit_params` and `costs` rows of the configuration table to point at the new sections, and add the cost flag to the example commands.
- [ ] Check the cost example arithmetic by hand and state the result in the plan record.
- [ ] Commit the guide.

### 4. Add the operator note

**Files:** Modify `docs/mt5-wine-gateway.md`.

- [ ] Add the "History depth" section and the troubleshooting row from the spec. Unless a human has confirmed the steps on the target machine, say in the section that it records expected MetaTrader 5 behaviour and give the `/v1/available_range` command that confirms it.
- [ ] Commit the note.

## Verification and handoff

- [ ] Run `uv run pytest tests/research -q`, `uv run ruff check examples/research tests/research` and `uv run black --check` on the same paths.
- [ ] Read the four guide sections once against the statement-to-test mapping and remove anything unsupported.
- [ ] Record the commands actually run, their results and the mapping in this plan; do not claim unrun checks passed.
- [ ] Use `./work board set Q-096 in-review -m "<changes; checks and results; follow-ups>"`. Say in the message whether the operator steps were confirmed on the target machine.
