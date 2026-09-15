# Q-028: Candle backtests on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §2, §3.3, §5, §9 invariant 1, §10 Phase 2](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#10-roadmap)  
**Depends on:** Q-027  
**Implementation plan:** [`../plans/Q-028-candle-backtests-on-q-core-plan.md`](../plans/Q-028-candle-backtests-on-q-core-plan.md)

## Purpose

After Q-024 the backtest engine reads every strategy decision from columns, but it
still walks the indicator frame one row object at a time to fill orders, size
entries, apply exit rules and day-trade windows, and queue the next bar. Every
backtest job, walk-forward window, lockbox run and optimizer trial pays that
per-bar Python cost, which architecture §2 names the largest throughput cost in
research. Q-027 put an equal loop in `q_core`, proven against this engine by
fixtures. This task makes the engine call it: indicators and decision columns are
still computed in Python, the bars are simulated by `q_core`, and every backtest
result, golden file and API response stays exactly as it is. It also moves the
backtest-to-live parity reference onto the `q_core` decision step, so that parity
keeps comparing the kernel's semantics with the live evaluator until Q-031 swaps
the evaluator too.

## Requirements

### One loop, in q_core

- The candle backtest engine simulates bars only through the `q_core` candle
  kernel, in both sequential and day-trade parallel modes, with and without
  day-trade rules, costs, a trade start, and a declared holding period. No part of
  the engine iterates over bars in Python.
- The engine's public surface is unchanged: its constructor arguments, its run
  method, the parallel modes, the trade-start argument, and the trade registry it
  returns. Callers in backtest jobs, the optimizer, walk-forward, lockbox,
  hypothesis tests and research acceptance need no change.
- Indicators and decision columns are computed as after Q-024, once per chunk, and
  handed to the kernel as columns without a Python object per bar. Day-trade
  parallel mode still splits the series by calendar date in Python and runs each
  day with fresh capital and force-close at end.
- Bar times reach the kernel as the index's own wall clock, so times of day and
  calendar dates are exactly those the engine uses today, for UTC goldens and for
  Brasília wall-clock lake data alike.
- The three position sizers are passed to the kernel with their configuration,
  including the separate point value the volatility sizer is built with. A sizer
  the kernel cannot represent is rejected with an error that names its class,
  before any bar is simulated.
- Invalid day-trade time strings keep raising today's error with today's message.

### Results

- The registry holds the same trades in the same order, each equal field for field
  to today's: symbol, side, quantity, entry and exit time, entry and exit price,
  status, profit and loss, cost, point value, and exit reason. Trade and order ids
  are still generated per trade. One order per opened trade is still registered
  with the quantity actually opened.
- Performance metrics, equity curves, chart data, backtest job payloads and
  optimizer trial results are unchanged.

### Parity during the transition

- The reference queued-signal extractor that backtest-to-live parity compares
  against is computed with the `q_core` decision step. The forward evaluator keeps
  its Python decision path until Q-031, so the parity test now compares the kernel's
  decision semantics with the live path on every golden case, with and without an
  open trade.
- The Python per-bar decision consumer and the Python exit rules remain only for
  the forward evaluator. Their remaining call sites are listed in the code at the
  point of definition, and nothing in backtesting calls them.

### Boundary

- `q_core` is imported only by the backend's bridge modules, and the import
  hygiene test lists them. The backend fails at import with a clear error when the
  installed `q_core` lacks the candle kernel, naming the missing functions and the
  installed version.
- The `q_core` dependency is pinned to the tag that contains Q-027, and the
  container build and lock file follow it.

### Preserved behavior

- Every committed golden file is byte-identical, and the golden, double-run
  determinism, backtest-to-live parity, strategy causality, genome parity and
  queued-signal baseline tests pass with unchanged test bodies.
- Engine-level tests for fills, costs, the position cap, exit rules through the
  engine, day-trade behaviour and holding periods pass with their expected values
  unchanged.
- The pairs strategy, which overwrites price columns and writes a floating-point
  spread column, produces the same trades, and the finding recorded about that
  collision is triaged with its decision written beside it.

### Cost

- Engine wall time is measured before and after on the same machine for three
  golden cases and for one long synthetic series, and the differences are reported
  whichever way they go.
- The time of one optimization study over real lake data is measured before and
  after, and reported.

## Constraints and non-goals

- **No change to the forward evaluator.** Its rolling window, decision path and
  sizing move to `q_core` in Q-031. Moving the parity reference here is the only
  execution-side change.
- **No deletion of the Python exit rules or their metadata.** The evaluator still
  calls the rules, and labels, parameter specs and presets are UI metadata that
  stay in the backend in every case. Q-031 deletes the rules' per-bar semantics.
- **No change to how indicators or decision columns are computed,** and no
  indicator math, which Q-023 owns.
- **No tick engine change.** That is Q-030.
- **No fixes to quirks found along the way.** Sequential runs leave trades open,
  a queued close for one side closes both, the position cap counts both sides, and
  candle days are local while tick days are UTC. The goldens hold these still.
- **No new engine features:** no multi-symbol runs, slippage, partial fills or
  order types, and no change to metrics or trade serialization.
- **No contract change,** and no change to the API's request or response shapes.

## Acceptance criteria

### Agent-verifiable

1. Before any engine change, engine wall time for `ma_crossover_baseline`,
   `composite_majority_three` and `genome_ma_session_gate`, and for a 50,000-bar
   synthetic MA-crossover run with a trailing stop, is recorded as the median of
   five runs. The same measurements after the change are reported alongside.
2. `git diff` of the goldens directory against the commit before the engine
   change is empty, and `test_golden`, `test_determinism_double_run`,
   `test_backtest_live_parity`, the causality suites, the genome parity test and
   the queued-signal baseline test pass with unchanged test bodies.
3. An engine-level registry comparison test, recorded before the change from the
   Python loop, passes after it: for every golden case, the day-trade parallel
   golden case, a `trade_start` case, a holding-period case and a pairs case, every
   trade field except ids and order creation time is equal, and each trade has one
   registered order with its opened quantity.
4. The engine contains no per-bar loop over the frame, and backtesting code does not
   call the Python decision consumer or the Python exit rules' per-bar methods.
5. The import hygiene test lists exactly the bridge modules that import `q_core`;
   importing the backend against a `q_core` build without the candle kernel raises
   an error naming the missing functions and the version.
6. Passing an unsupported sizer raises an error naming its class, and malformed
   day-trade times raise today's message.
7. The parity reference is produced by the `q_core` decision step, and deliberately
   changing one queued exit reason in the reference makes the parity test fail.
8. The pairs-strategy collision finding carries a triage decision.
9. The full validation suite passes.

### Human-verifiable

1. Backtests over real lake data run from `development` and from the task branch
   give identical trade counts and headline metrics for MACrossover on WIN$N M15
   with day-trade rules, TSMOM on PETR4 D1 with inverse-volatility sizing, and a
   genome strategy with a trailing stop.
   Command: `pnpm tauri:dev` (in `q_frontend`; Backtests workspace, same
   configuration against each backend branch)
2. One optimization study of 50 trials over real lake data is timed on both
   branches with the same seed, and the wall times and best trial's metrics are
   reported.
   Command: `pnpm tauri:dev` (Optimization workspace, same study against each
   backend branch)
