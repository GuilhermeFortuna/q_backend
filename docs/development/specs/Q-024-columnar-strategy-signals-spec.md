# Q-024: Columnar strategy signals

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.3, §9 invariant 1, §10 Phase 2](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#33-rust-scope-in-execution)  
**Depends on:** Q-023  
**Implementation plan:** [`../plans/Q-024-columnar-strategy-signals-plan.md`](../plans/Q-024-columnar-strategy-signals-plan.md)

## Purpose

A candle strategy's trading decision is made by calling it once per bar with a
single row. The backtest engine calls a strategy's entry and exit methods on
every bar, and the forward evaluator calls the same methods through a shared
helper. Most of those methods only re-read trigger columns that the strategy
already computed over the whole frame. Some decisions exist only inside the
row methods, though: the order in which long and short entries win, signal
strength, rebalance-every-bar entries and exits, composite stance exits, and
fixed holding periods. A Rust candle kernel (Q-027) cannot call back into
Python on every bar. While those decisions live in row methods, the candle loop
cannot move to `q_core`. This task makes every strategy's decision a set of
columns computed with its indicators. The one exception is the holding period,
which is declared by the strategy. The engine and the evaluator read the
decision through one shared consumer, and every result stays the same.

## Requirements

### Decisions as columns

- Every candle strategy writes its decision as columns of its indicator frame,
  computed over the whole frame with no Python call per row. This covers the
  eleven registered hand-written strategies, the genome interpreter, and the
  multi-entry composite. The decision is made of the entry, the long exit, the
  short exit, and the entry strength.
- The columns carry the whole decision. A consumer that reads only these
  columns, the strategy's symbol, the declared holding period, and the open
  trades queues exactly the signals queued today on every bar. The signals have
  the same actions, order, count, symbol, and strength. Strategy exits carry no
  exit reason.
- On a bar where both a long and a short entry trigger are true, the long entry
  wins, as it does today. The entry column cannot express both at once, so no
  consumer has to decide which one wins.
- Entry strength is between 0 and 1 on every bar that has an entry, and it has a
  defined value on every other bar. No consumer handles missing values.
- Signal columns are causal. The value at bar `i` depends only on bars up to
  `i`. The existing causality guardrail covers them for registered strategies
  without new wiring. The multi-entry composite, which is not registered, is
  checked explicitly.
- If a strategy's indicator frame is missing a signal column, has one with the
  wrong type, or has values out of range, indicator computation fails with an
  error that names the strategy and the column. It never produces no signals
  silently.

### Decisions that depend on the open trade

- An exit that depends only on the open trade's direction is written as two
  columns, one for long trades and one for short trades. This covers crossover
  exits, band exits, pair-spread exits, stance exits, and rebalance exits.
- A fixed holding period depends on the bar where the open trade entered, so it
  cannot be a column. This applies to TRB, FMA, and genomes with a fixed-holding
  exit. Such a strategy declares its holding period as a whole number of bars.
  The consumer applies it with today's semantics:
  - A trade is closed when the bars elapsed since its entry bar reach the
    period.
  - A trade whose entry time is not a bar in the frame is never closed by the
    holding period.
  - While a holding period is declared, the strategy's other exit triggers are
    not used.
- Exit rules keep working on rows: stops, targets, trailing, breakeven,
  chandelier, Donchian, parabolic SAR, ratchet, and time stop. For a symbol, an
  exit rule still takes precedence over a strategy exit, and it keeps its exit
  reason.

### One consumer

- The backtest engine, in both its sequential and day-trade paths, and the
  forward evaluator queue signals through one shared consumer of the signal
  columns. No part of the backend calls a strategy on each bar for its entry or
  exit decision.
- The strategy contract no longer has per-row entry and exit methods. That way,
  no second definition of a strategy's decision can drift away from its columns.
- The engine's per-bar loop is unchanged, and so are fill timing at the next
  bar's open, day-trade windows and forced closes, `trade_start` warm-up,
  position sizing, the position cap, costs, and exit-rule state.

### Preserved behavior

- The committed golden backtests, the double-run determinism test, the
  backtest-to-live parity test, the genome registry parity test, and the
  strategy, node, and random-genome causality tests all pass. None of their test
  bodies change. No golden file changes.
- Before any strategy changes, a baseline of queued signals is recorded from
  today's code, per bar. It covers every strategy and every configuration branch
  that changes its decision, with no open trade, with an open long, and with an
  open short. Afterwards the baseline is reproduced exactly, strength included.
- The signal columns that strategies write today keep their names and values:
  buy, sell, and exit triggers, net stance, rebalance flags, momentum strength,
  and bar index. The multi-entry composite reads its members' triggers, the
  genome momentum node exposes them as ports, and the genome registry parity
  test and strategy unit tests assert on them.
- Saved custom strategies and strategies compiled by the AI strategy builder
  need no migration. They are parameter sets of registered strategies or
  genomes.
- Backtest results, chart data, deployment decisions, and their API response
  shapes are unchanged.

### Cost

- The evaluator benchmark is measured before and after on the same machine, and
  both readings are reported. Its existing median bounds still hold.
- The engine's wall time on the golden candle cases is measured before and
  after, and the difference is reported whichever way it goes.

## Constraints and non-goals

- **No change to the per-bar loop in the engine, and no Rust.** Removing the
  loop is Q-028, and the candle kernel that reads these columns is Q-027. This
  task changes where the decision is made, not what drives the bars.
- **No change to exit rules.** Exit rules move to `q_core` in Q-026. Folding
  the fixed holding period into the time-stop rule is tempting, but it would
  change results. The time stop fires one bar earlier, labels its exit with a
  reason, and takes precedence over strategy exits.
- **No fixes to quirks found along the way.** A genome whose exit is
  "rebalance" never closes a trade. A genome's middle-band long exit overrides
  its short exit reference. A live holding-period exit fires only if the
  position's open time equals a bar timestamp. Forward decisions label strategy
  exits as exit-rule exits. RSI can raise both entry triggers on one bar. These
  are recorded, not fixed, because the goldens exist to hold results still.
- **No vectorization of the composite's signal-manager vote or of the pairs
  strategy's spread state machine.** Both run once per frame inside indicator
  computation, not once per bar in the engine. Neither one blocks a kernel from
  reading the result.
- **No change to tick strategies.** They already emit aligned signal arrays with
  no per-tick callback.
- **No contract change.** Signal columns stay inside the backend and are not a
  cross-process payload. The frame format the kernels consume is defined in
  Q-025.
- **No change to sizing inputs.** Sizers still read the fill bar's row.
- **No new strategies, parameters, or signal managers,** and no change to
  indicator math, which Q-023 owns.

## Acceptance criteria

### Agent-verifiable

1. The queued-signal baseline is committed before any strategy source changes.
   It covers all 13 strategy classes and every decision-changing branch: TSMOM
   and Hurst with and without rebalance every bar, TSMOM's trend rule, genomes
   with fixed-holding, middle-band, rebalance, and opposite-signal exits, and
   the three signal managers. Each is run with no open trade, an open long, and
   an open short. After the migration, every case reproduces exactly, including
   strength.
2. Golden candle cases for the strategies and paths the goldens do not cover
   today are added and committed before the migration: a fixed holding period,
   strength-scaled sizing, rebalance every bar, band exits, and the day-trade
   path. After the migration, `git diff` against that commit shows no change
   under the goldens directory.
3. `test_golden`, `test_determinism_double_run`, `test_backtest_live_parity`,
   the genome registry parity test, and the three causality suites pass, and
   their test bodies are unchanged.
4. Contract tests pass for every strategy:
   - The signal columns are present with the declared types.
   - A bar with both entry triggers yields a long entry.
   - Strength is in range on entry bars and has its declared value elsewhere.
   - A strategy that omits a column raises an error that names the strategy and
     the column.
5. The existing TRB and FMA engine tests for holding-period exits pass
   unchanged. A trade whose entry time is not in the frame is never closed by
   the holding period. A strategy that declares a holding period has
   all-false exit columns.
6. No backend source defines or calls per-row entry or exit methods. A strategy
   that implements only indicators and chart specs can be instantiated.
7. The multi-entry composite's signal columns pass the prefix-causality check.
8. The evaluator benchmark medians before and after are reported for its three
   fixtures, and both stay within the existing bounds. Engine wall time for
   three golden candle cases is reported as the median of five runs, before and
   after.
9. The full validation suite passes.

### Human-verifiable

1. A backtest over real lake data runs from `development` and from the task
   branch for MACrossover on WIN$N M15, TSMOM on PETR4 D1 (trend rule,
   rebalance every bar, inverse-volatility sizing), and TRB on PETR4 D1. Each
   strategy's trade count and headline metrics are identical between the two
   runs.
   Command: `pnpm tauri:dev` (Backtests workspace, same configuration on both
   backend branches)
