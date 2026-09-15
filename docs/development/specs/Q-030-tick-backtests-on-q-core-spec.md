# Q-030: Tick backtests on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §2, §3.3, §5, §9 invariant 1, §10 Phase 2](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#10-roadmap)  
**Depends on:** Q-029  
**Implementation plan:** [`../plans/Q-030-tick-backtests-on-q-core-plan.md`](../plans/Q-030-tick-backtests-on-q-core-plan.md)

## Purpose

The tick backtest engine simulates ticks with a `numba` kernel that is compiled on
first use, splits streams into days with numpy in Python, and builds the tick
backtest chart by looping over display bars in Python. None of it is covered by
the `q_core` parity gate, and invariant 1 wants one implementation of simulation
semantics, in `q_core`. Q-029 implemented the tick simulation, the day split and
the bar aggregation in Rust and proved them equal to these functions. This task
makes the tick engine and the tick chart use them, removes the `numba` tick
kernel, and keeps every tick backtest result, chart payload and optimizer trial
exactly as it is.

## Requirements

### Simulation and day split in q_core

- Tick backtests simulate ticks only through the `q_core` tick simulation, in
  day-trade and sequential modes. Day-trade mode splits the stream with the
  `q_core` day split, computes signals per day as today, and simulates each day
  with the initial capital.
- The tick engine's public surface is unchanged: constructor arguments, the run
  method and its modes, and the trade registry it returns. Backtest jobs and the
  tick optimizer runner need no change.
- The registry holds the same trades with the same fields as today: side,
  quantity, entry and exit times, prices, profit and loss, point value, and exit
  reason names.
- The kernel's existing Python entry point keeps its signature and return shape,
  so every existing caller and test of it keeps working without edits.
- A sizing configuration the tick engine does not support fails before any
  simulation with an error that names the model. Today it fails with an attribute
  error from inside the sizing conversion. This change is recorded as a finding
  with its triage.

### Tick chart in q_core

- The tick chart's bars and indicator values are produced by the `q_core` bar
  aggregation, interval resolution and bar-end sampling. The chart payload is
  identical to today's: bar timestamps, prices, volumes, indicator values and
  missing values, for every display timeframe and for streams long enough to
  double the interval.
- Timeframe name validation and its error message, and timestamp formatting, stay
  as they are.

### Boundary

- The `numba` tick kernel is gone. `numba` stays a backend dependency only
  because candle strategies still use it, and those uses are listed where the
  dependency is declared.
- `q_core` is imported only by the backend's bridge modules, and the import
  hygiene test lists them. The backend fails at import with a clear error when the
  installed `q_core` lacks the tick functions.
- The `q_core` dependency is pinned to a tag that contains Q-029, and, if the
  candle engine already runs on `q_core`, also Q-027.

### Preserved behavior

- The tick golden file is byte-identical, and the tick golden and double-run
  determinism tests pass with unchanged bodies.
- The tick kernel, tick engine, tick chart and tick strategy causality tests pass
  with unchanged bodies and expected values.
- Tick backtest job payloads and tick optimizer trial results are unchanged.

### Cost

- Tick engine wall time is measured before and after on the same machine for the
  golden tick case and for a 1,000,000-tick synthetic stream, including a first
  call in a fresh process with no compiled cache, and the differences are
  reported whichever way they go.
- Tick chart serialization time for the 1,000,000-tick stream at M1 is measured
  before and after and reported.

## Constraints and non-goals

- **No change to tick strategies.** They compute directions and stop and target
  distances with vectorised numpy, which the tick strategy contract requires.
- **No change to candle backtests or to `numba` use in candle strategies.** TSMOM
  and the pairs strategy use `numba` inside indicator computation. Moving that is
  indicator work for a later task, not tick simulation.
- **No fixes to quirks found along the way.** The dead re-entry flag, UTC tick
  days, unfloored fixed quantity, the absence of transaction costs in tick
  backtests, and the whole-stream choice between last price and midpoint are
  reproduced. Adding costs or volatility-targeted sizing to the tick engine would
  change results and is a feature decision.
- **No live tick aggregation** and no Q-025 frames on the tick path.
- **No contract change** and no change to API request or response shapes.

## Acceptance criteria

### Agent-verifiable

1. Before any change, tick engine wall time for the golden tick case and a
   1,000,000-tick synthetic stream (median of five warm runs, and one cold first
   call with an empty compilation cache) and tick chart serialization time for that
   stream at M1 (median of five) are recorded. The same measurements after the
   change are reported alongside.
2. A chart-payload baseline recorded before the change reproduces exactly after
   it for: M1 and M15 over the golden tick stream, a stream whose last prices are
   all zero, a stream long enough to double the interval, and a strategy indicator
   with missing values.
3. `git diff` of the tick golden file against the commit before the change is
   empty, and `test_golden` and `test_determinism_double_run` for the tick case
   pass unchanged.
4. `tests/backtesting/tick/test_kernel.py`, `test_tick_engine.py`,
   `test_tick_chart_data.py` and `test_tick_strategy_causality.py` pass with no
   change to their contents.
5. No backend module imports `numba` for tick simulation, the tick kernel module
   contains no compiled function, and the import hygiene test lists exactly the
   bridge modules that import `q_core`.
6. Running a tick backtest with an inverse-volatility sizing configuration raises
   an error naming the model before any simulation, and the finding records the
   change from today's attribute error.
7. The full validation suite passes.

### Human-verifiable

1. A tick backtest over one day of real WIN$N ticks runs from `development` and
   from the task branch with the same configuration and shows identical trade
   count, headline metrics and chart bars at M1.
   Command: `pnpm tauri:dev` (in `q_frontend`; Backtests workspace, tick engine,
   same configuration against each backend branch)
