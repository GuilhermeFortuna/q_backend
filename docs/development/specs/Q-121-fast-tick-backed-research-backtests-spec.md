# Q-121: Fast tick-backed research backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 28 — Fast tick-backed backtests
**Depends on:** Q-119, Q-120
**Implementation plan:** [Plan](../plans/Q-121-fast-tick-backed-research-backtests-plan.md)

## Purpose

A research backtest with ticks should cost about one Python decision per closed bar, plus Rust work for ticks. Today it costs work proportional to the history on every Python call, plus a full indicator rebuild per replayed tick (Batch 28, findings 1, 2 and 4). After this task:
- prices come from Q-119's native source;
- hook frames are built in constant time;
- tick-phase exits stop rebuilding indicators;
- `workers` runs independent day-trade sessions in parallel, with identical results.

## Current system

- `TickReplay` (`research/intrabar.py`) serves prices to the kernel through a Python callable, one call per bar that needs them.
- `_owned_prefix` copies the full history before every `exit_strategy` and `entry_strategy` call, and before every screen.
- `TickEvaluator` re-runs `compute_indicators` over the whole raw history for each evaluated tick.
- `TickWorkerPool` spreads tick decisions over processes in waves of `16 × workers`. This throws away most decisions once an early tick exits. It also lets hooks diverge on process-local state: a sample run's exit changed between 1 and 20 workers.

## Behaviour

### Prices from the native source

- When `ticks=` is given, `backtest()` builds a `q_core.engine.SessionTickSource` from the store's session files, with each bar's interval computed as `TickReplay` does today. `TickReplay` is removed from research backtests.
- The errors keep their current types and messages, mapped from `IntrabarDataError`:
  - `NoMarketDataError` naming the session and `TickStore.sync` for a missing session or an empty interval;
  - `ValueError` naming the bar and `TickStore.bars` for an OHLC mismatch.
- Phase-aware replay gets a bar's prices from the same source.

### Hook frames in constant time

- `entry_strategy`, `exit_strategy` (every phase) and the screen receive a frame over the prepared history that is not copied: a positional slice ending at the current bar. Its `attrs` are empty.
- With pandas copy-on-write, a hook that writes to its frame changes nothing the engine or later calls see.
- Reaching later rows through NumPy `base` attributes is not guarded. `compute_indicators` already sees the full frame, and the causality check below covers indicator lookahead.

### Tick-phase exits without rebuilding indicators

- A `tick` call receives the prepared history before the current bar, plus the forming row. The forming row's `open`, `high`, `low`, `close` and `tick_volume` are as Q-104 specifies; **every other column is `NaN`, indicator columns included.**
- `compute_indicators` is no longer called during replay. A hook that needs a value from the forming candle computes it from the frame's tail.
- Building a `tick` frame costs constant time in the history length.
- Replay runs in-process, in tick order, and stops at the first exit. `TickWorkerPool` is removed.

### Parallel day-trade sessions

- **When it applies.** With `day_trade=True` and `workers > 1`, sessions run in up to `workers` processes; each worker takes a contiguous block of sessions. Sessions are independent because every position closes by the end of its session and research sizing is a fixed quantity.
- **Results.** `trades`, `metrics`, `equity`, `rejected_entries` and `data` are identical to `workers=1`, whatever the worker count. Progress reports bars across all workers.
- **Strategy state.** Each worker starts from the strategy as it was after `compute_indicators`. Instance attributes that hooks write are not shared between workers. The guide states that `workers > 1` requires hook decisions to depend only on their arguments, plus state that resets within a session.
- **Otherwise.** With `day_trade=False` the run is sequential for any `workers` value, and the guide says so. `workers="auto"` keeps its current sizing.

### Causality check

- `backtest(..., check_causality=False)`. When `True`, before running, `compute_indicators` is re-run on prefixes ending at up to 20 evenly spaced session ends. The first indicator value that differs from the full-frame value raises `ValueError` naming the column and the bar.
- The check is off by default and then costs nothing.

## Documentation

`docs/research-library.md`:
- **"Performance and progress":**
  - the cost model: one decision per closed bar, ticks in Rust;
  - what `workers` parallelises, and its requirements;
  - `check_causality`;
  - the advice to express level-based exits with `amend` (Q-120) rather than tick-phase hooks.
- **"Entry levels and phase-aware exits":** the forming row's indicator columns are `NaN`, and indicators are not recomputed during replay. Include a short migration example that computes a forming-candle value from the frame's tail.

`examples/research/stop_target_backtest.py` is updated to the new tick-phase contract. `examples/research/tick_backtest_benchmark.py` writes a synthetic store to a temporary directory and times a trailing-stop strategy:
- `--sessions` and `--workers` arguments;
- runs once with an `amend` exit and once with a phase-aware exit;
- prints the timings.

`tests/research/test_examples.py` runs it with two sessions.

## Focused acceptance

Each case uses a `TickStore` written under `tmp_path` from handwritten or generated ticks.

1. Every existing research test, including the Q-104, Q-106 and Q-120 suites, passes with unchanged bodies on the native source, with the same errors and messages.
2. A spy shows that `TickStore.trade_prices` is not called during a backtest.
3. A counting strategy with a phase-aware exit and screened candles calls `compute_indicators` exactly once per `backtest()`.
4. **Zero copy.** Bar, screen, entry and tick frames share memory with the prepared history (`numpy.shares_memory`). A hook that writes to its frame's columns cannot change later frames, positions or results.
5. **Forming row.** It holds the observed OHLC and tick count and `NaN` elsewhere. Earlier rows equal the prepared indicators, and a later tick's value never reaches an earlier decision.
6. **Parallel sessions.** On at least five sessions, a strategy using market and resting entries, amendments and a phase-aware exit gives identical results with `workers` 1, 2 and 4 in day-trade mode. With `day_trade=False`, `workers=4` gives the `workers=1` result.
7. **Causality check.** A `shift(-1)` indicator raises with its column and bar; a causal one passes. With the check off, `compute_indicators` is called once.
8. The benchmark runs and its example test passes. The plan records before-and-after timings for 60 synthetic sessions on the development workstation, with and without parallel sessions.

Verification uses synthetic data. No gateway, database, Docker, GPU, Wine or desktop run.

## Delivery boundary

Out of scope:
- bars aggregated in Rust;
- a vectorised order API that bypasses per-bar hooks;
- streaming forming-candle indicators;
- parallelism for runs that are not day-trade;
- any change to stack backtest jobs, optimisation, walk-forward, the forward evaluator or contracts.
