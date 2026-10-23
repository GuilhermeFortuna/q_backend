# Q-126: Tick data in research strategy hooks

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 31 — Tick data in research strategy hooks
**Depends on:** none
**Implementation plan:** [Plan](../plans/Q-126-tick-data-in-research-strategy-hooks-plan.md)

## Purpose

A `ResearchStrategy` can compute indicators and take decisions from candles only. Ticks passed to `backtest(..., ticks=)` reach the q_core kernel for stops, targets, resting orders and phase-aware exits, but no hook can read them. After this task:
- `compute_indicators`, `entry_strategy` and `exit_strategy` can each declare a `ticks` parameter and receive a `TickData` object;
- a hook loads one candle's ticks, or a time range, only when it asks for them;
- `TickData` never returns ticks after the moment the hook is deciding at;
- a strategy whose hooks need ticks fails before the run when `backtest()` has no `ticks=`.

## Current system

- `backtest()` hands the `TickStore` to the kernel only, through `_NativeTickSources` (`research/engine.py`) and `native_tick_source` (`research/intrabar.py`). The kernel reads a session file the first time a bar of that session needs prices: an open position or working order whose level the bar's range reaches, or a candle a phase-aware exit screened. It keeps one session in memory.
- `detect_hook_call` (`research/hooks.py`) reads `positions` and `phase` from a hook's signature. A `phase` parameter on `exit_strategy` makes ticks required before the run (`ResearchStrategyAdapter.requires_ticks`). Stops, targets, resting orders and `amend` require them when issued.
- In `phase="tick"`, a hook sees the forming candle's OHLC and tick count, never the ticks themselves.
- `compute_indicators(frame)` takes only the frame and is called directly by the adapter.

## Behaviour

### Declaring ticks

- Each of `compute_indicators`, `entry_strategy` and `exit_strategy` may declare a `ticks` parameter, positional-or-keyword or keyword-only. The engine passes it by keyword.
- **Optional.** A `ticks` parameter with a default receives a `TickData` when `backtest()` has `ticks=`, and `None` otherwise.
- **Required.** A `ticks` parameter without a default makes the strategy require ticks. `backtest()` without `ticks=` raises `ValueError` before any hook runs, naming the strategy class and the hook, and saying to pass a `TickStore` as `ticks=`.
- A hook without `ticks` behaves exactly as today. `**kwargs` alone does not receive ticks.
- The existing `phase` rule is unchanged: a phase-aware exit still requires ticks.
- Registered strategies passed by name are unaffected.

### `TickData`

`q_backend.research.TickData` is exported from the package. Hooks receive instances; they do not build them.

- **`ticks.bar(timestamp=None) -> pandas.DataFrame`** returns the ticks of the candle that opens at `timestamp`, a value of the hook's `frame.index`. `None` means the frame's last bar. A candle's interval is the one the kernel uses: from its open to the next bar's open in the same session, or to the end of the session day for the session's last bar. A timestamp that is not a bar of the backtest frame, or that opens after the cutoff, raises `ValueError`.
- **`ticks.between(start, end) -> pandas.DataFrame`** returns the ticks in `[start, end)`, across sessions, with `start` and `end` as `TickStore.ticks` accepts them. `end` is clipped to the cutoff. A `start` at or after the cutoff raises `ValueError` naming the cutoff.
- **`ticks.cutoff -> pandas.Timestamp`**: the latest moment this instance can return. A `bar` or `between` result never holds a tick after it.
- **Returned frames** have the columns and index of `TickStore.ticks`: a tz-aware `America/Sao_Paulo` index and `bid`, `ask`, `last`, `volume` and `flags`, in stored order. `attrs` are empty. A candle or range without ticks in a stored session returns an empty frame with those columns.
- **Missing sessions.** Requesting ticks from a session that holds a bar of the backtest frame but has no stored file raises `NoMarketDataError` naming the session and `TickStore.sync`. Days without bars in the frame, such as weekends, are skipped.
- Writing to a returned frame changes nothing the engine or later calls see.

### Cutoff per call

| Call | Cutoff |
|---|---|
| `compute_indicators` | end of the frame's last bar |
| `compute_indicators` in `check_causality` | end of the prefix's last bar |
| `entry_strategy`, `exit_strategy` without `phase`, `exit_strategy` with `phase="bar"` | end of the current bar |
| `exit_strategy` with `phase="screen"` | open of the current bar, so `bar()` with no timestamp raises |
| `exit_strategy` with `phase="tick"` | the current trade tick, inclusive |

In the tick phase the cutoff is a position, not only a time. `ticks.bar()` returns the current candle's stored rows up to and including the row of the trade being decided, so a later row with the same `time_msc` is not returned. Trade rows are those with a finite `last > 0`, in the stable `time_msc` order q_core uses. `ticks.cutoff` reports that trade's time.

### Loading and cost

- Nothing is read until a hook calls `bar` or `between`. A run whose hooks never call them reads no tick file from Python.
- One session's full columns are cached per run (per worker with `workers > 1`). Bar-by-bar calls in time order read each session once. A `bar` call costs a `searchsorted` and a slice of the cached session, independent of the history length.
- The Python cache is separate from the kernel's. A session can be read once by each.
- `TickData` and its store pickle without the cached session, so `workers > 1` works unchanged.

### Causality check

With `check_causality=True`, each prefix re-run of `compute_indicators` receives a `TickData` cut off at the prefix's end. A tick-based indicator that reads later ticks differs from the full run and raises the existing `ValueError` naming the column and bar.

## Documentation

`docs/research-library.md`, "Strategy hooks and causality": a "Tick data in hooks" subsection with:
- the optional and required forms of `ticks`, and the error for a missing store;
- `bar`, `between` and `cutoff`, and the cutoff table above;
- a per-bar example (an entry filter from the current candle's trade volume) and a `compute_indicators` example that builds a per-bar column from `between` once per session.

`strategy.py` docstrings describe the `ticks` parameter on all three hooks. The `ticks` docstring in `backtest()` says hooks can read it.

## Focused acceptance

Each case uses a `TickStore` written under `tmp_path` from handwritten ticks.

1. **Signatures.** A hook with no `ticks` gets no argument. One with an optional `ticks` gets `None` without a store and a `TickData` with one. One with a required `ticks` makes `backtest()` without `ticks=` raise before `compute_indicators` runs, with the class and hook named. This holds for each of the three hooks.
2. **Bar ticks.** `bar(ts)` returns exactly the stored rows of that candle's interval, including the last bar of a session. `bar()` returns the current bar. A timestamp outside the frame raises.
3. **No look-ahead.** In the bar phase, a later bar raises and `between` is clipped to the current bar's end. In the screen phase, `bar()` raises. In the tick phase, `bar()` ends at the decided trade's row, including when the next row has the same `time_msc`.
4. **Laziness and caching.** With a spy on session reads, a strategy that declares `ticks` but never calls it reads nothing. A strategy calling `bar()` on every bar over three sessions reads each session once.
5. **Indicators.** A `compute_indicators` column built from `between` passes `check_causality`. One that reads the next session's ticks raises with its column and bar.
6. **Missing session.** A `bar()` call in an unstored session raises `NoMarketDataError` naming that session.
7. **Workers.** A day-trade strategy reading `bar()` gives identical results with `workers` 1 and 2.
8. All existing research tests pass with unchanged bodies.

Verification uses synthetic data. No gateway, database, Docker, GPU, Wine or desktop run.

## Delivery boundary

Out of scope:
- ticks for registered strategies, stack backtest jobs, optimisation, walk-forward or the forward evaluator;
- sharing the session cache between Python and q_core;
- tick-level decisions on candles the kernel does not replay (the tick phase stays limited to screened candles);
- any change to fills, the kernel or contracts.
