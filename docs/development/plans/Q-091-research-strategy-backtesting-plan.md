# Q-091 implementation plan: Research strategy classes and local backtesting

> **For implementation agents:** Read the linked specification and repository instructions. Use superpowers:executing-plans when available. Launch only through `./work start Q-091 --agent <agent> --worktree` after written-plan approval and completed dependencies. Implement natively; delegation requires separate authorization.

**Goal:** Run three-hook ResearchStrategy classes or existing candle strategies against DataFrames using Q execution semantics.
**Architecture:** A private TradingStrategy adapter compiles prefix-history TradeOrder decisions into existing signal columns and supplies them to BacktestEngine; result wrappers present registry data without new simulation math.
**Tech stack:** Python 3.12+, pandas, existing q_backend adapters and pinned q_core.
**Spec:** [Specification](../specs/Q-091-research-strategy-backtesting-spec.md)
**Status:** written plan awaiting human review.

## Global constraints

- Python namespace inside q_backend; retain existing installation/dependency scope and service behavior.
- No alternative indicator/fill/PnL implementation, generated contract changes, GPU, live trades, desktop or optimizer integration.
- Use lazy dependencies and explicit gateway overrides when fetching data. No import-time startup or global environment/runtime-config mutation.
- Verify changed behavior with small fixtures and focused mocks. No benchmarks, full-stack runs, live gateway runs or blanket full CI requirement.
- Commit focused task changes locally. Human owns integration, publication and initial board Status/Todo approval.

## Review focus

- Each decision sees its own closed-bar prefix; hooks cannot alter later bars or metadata to reveal future rows.
- exit_strategy is a user method, distinct from the engine ExitStrategy object on the private adapter.
- Next-open fills, reversal/close ordering and final-bar behavior match the existing engine.
- Naive timestamps, invalid config and unsupported tick/ML variants fail clearly.
- Open and empty results, costs and realized-equity labels match registry semantics.

## Ordered implementation

### 1. Define orders and compile custom strategies

**Files:** Create src/q_backend/research/{orders,strategy,adapter}.py and tests/research/test_strategy.py. Extend research/__init__.py.
**Interfaces:** immutable TradeOrder(action), buy()/sell()/close(); ResearchStrategy ABC with compute_indicators default identity, abstract entry_strategy and exit_strategy default None. Private adapter implements TradingStrategy and returns already prepared signals; no user chart/symbol/registry requirement.

- [x] Add small scripted strategies asserting indicator hook once, exit then entry on every nonempty prefix, last-bar calls, isolated mutable frame copies, no future-range attrs, None/buy/sell/close mapping and unchanged caller data.
- [x] Add invalid action/return type, illegal action per hook, reserved columns, modified index/market data and hook exception cases. Assert descriptive method/timestamp errors and preserved causes; no silent skips.
- [x] Run `uv run pytest tests/research/test_strategy.py -q`; implement ABC/orders and prefix compilation using existing signal_columns helpers. Keep engine ExitStrategy on the adapter so it cannot shadow the user method. Hooks receive no position state; no public causality utility.
- [x] Run the focused tests and commit the strategy adapter.

### 2. Run the existing engine and expose results

**Files:** Create src/q_backend/research/{backtest,results}.py and tests/research/test_backtest.py. Extend lazy exports. Reuse backtesting/{engine,factory,position_sizing,costs,registry,indicator_frame}.py.
**Interfaces:** backtest() and BacktestResult(metrics, trades, equity, data) exactly as Q-091. Q-089 frame validation is reused with optional volumes/valid empty input; aware index converts to Brasília.

- [x] Add a small parity fixture comparing custom compiled decisions and built-in MACrossover to direct existing engine calls. Cover long/short, repeated requests, reversal and same-bar close+entry, stop/target precedence, next-open execution, final-bar decisions and forced/unforced close. Compare numeric fields/times rather than generated UUIDs.
- [x] Add focused boundary cases for costs/point multiplier, UTC-input day-trade times, invalid quantity/capital/costs/session bounds, unknown/conflicting params and unsupported tick/ML names. Confirm configured exit columns are included in result data, without invoking user indicator hooks again.
- [x] Add result assertions for all/open/empty trades, stable columns/dtypes, closed-trade metrics and realized equity equals initial capital plus registry closed PnL. Preserve independent outputs and omit internal signal columns.
- [x] Run `uv run pytest tests/research/test_strategy.py tests/research/test_backtest.py -q`; implement the synchronous facade with FixedQuantitySizer and sequential BacktestEngine, no service persistence or alternate fill/PnL math.
- [x] Run `uv run pytest tests/research tests/backtesting/test_engine.py tests/backtesting/test_signal_columns.py tests/backtesting/test_indicator_frame_path.py -q`. These cover the new interface and its existing execution/exit-column seams; do not repeat unrelated kernel goldens. No full CI unless shared engine behavior changes; explain the specific additional gate if it does. Commit backtest/results.

### 3. Deliver the scripting workflow and examples

**Files:** Create examples/research/{rsi_reversion,mt5_backtest}.py and tests/research/test_examples.py; update docs/research-library.md and README.md.

- [x] Document three hooks, None/TradeOrder semantics, fixed run sizing, optional exits/costs/session settings, built-in strategy use, result fields and the load_bars gateway prerequisite. The backtest/indicator functions operate on supplied frames without fetching or accessing a database. Explain callbacks have no fill/position state, compute_indicators must be causal, repeated conditions request repeated entries, and per-prefix Python evaluation/copies are slower than built-in vectorized strategies. No performance claims or benchmark work.
- [x] Ship an offline RSI example accepting historical Parquet and an MT5 example calling load_bars with end omitted, then adding indicators and running a built-in/custom backtest. Replace the stale missing CCM script link in README. Avoid hard-coded paths/sys.path changes.
- [x] Exercise the offline example on one temporary fixture and the MT5 example with mocked Q-089 load_bars (no live terminal or database). Verify the RSI example at a few fixed independently prepared prefixes and verify no API/worker/DB/Redis/GPU/native MT5 startup on the offline path.
- [x] Run `uv run pytest tests/research -q`; commit docs/examples. No live feed, Docker/GPU/Wine/desktop run, benchmark or dedicated benchmark harness.

## Handoff

- [x] Check the spec against the implementation and focused results; document actual commands and outcomes without claiming unrun checks passed.
- [ ] Commit the final documentation/examples and use `./work board set Q-091 in-review -m "<changes; focused checks and results; follow-ups>"`. If a required prerequisite blocks progress, use the documented blocked workflow.
