# Q-031: Execution evaluator on q_core

**Status:** authoritative in the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2)  
**Project direction:** [`q_contracts/docs/system-architecture.md` §3.3, §6.1, §9 invariants 1 and 4, §10 Phase 2](https://github.com/GuilhermeFortuna/q_contracts/blob/f2273a88e52c5b9a8ad5ac9d7eb27f8069cf643d/docs/system-architecture.md#10-roadmap)  
**Depends on:** Q-028  
**Implementation plan:** [`../plans/Q-031-execution-evaluator-on-q-core-plan.md`](../plans/Q-031-execution-evaluator-on-q-core-plan.md)

## Purpose

After Q-028 every backtest decides and simulates through `q_core`, but the
execution worker's strategy evaluator still decides each live bar with the Python
decision consumer, the Python exit rules and the Python sizers. That is a second
implementation of the semantics research and live trading must share, kept alive
only by this one caller, and it is the last piece of architecture §10 Phase 2:
the evaluator calls `q_core` for evaluation and nothing else changes in it. This
task moves the evaluator's per-bar decision, exit-rule state and entry sizing to
the `q_core` decision step, deletes the Python per-bar implementations, and keeps
every forward decision, recovery replay and parity result exactly as it is. It
closes Phase 2 with an acceptance record that gathers the batch's measured
results in one place.

## Requirements

### Evaluation in q_core

- For every completed bar it evaluates, the forward evaluator decides queued exits
  and entries with the `q_core` decision step: exit rules with their per-trade
  state, strategy exits, the declared holding period, and at most one entry.
- Exit-rule state persists across bars for the open trade's identity, as it does
  today, including across recovery replay: replaying a history reconstructs the
  same state that evaluating those bars live would have produced.
- An open trade's entry bar is found in the current window by its entry time; a
  trade whose entry time is not a bar of the window is never closed by the holding
  period, as today.
- The requested quantity of an entry is sized by `q_core` at the bar close with
  the evaluator's configured capital and the bar's volatility, exactly as today.
- Everything else about the evaluator is unchanged: its constructor and
  factories, the rolling window and its bound, indicator recomputation over the
  window, replay and duplicate handling, the watermark, the mapping to a domain
  action and reason, timing fields, and every field of the forward decision
  result.

### One implementation

- The Python per-bar decision consumer is deleted. No backend code decides a bar's
  queued signals outside `q_core`.
- The Python exit rules' per-bar state updates and exit decisions are deleted. The
  exit-rule catalog keeps its identifiers, labels, descriptions, groups, parameter
  specs, presets and listing for the UI and the optimizer.
- Which indicator columns enabled exit rules need is answered by `q_core`, for
  indicator augmentation and for the evaluator's window bound alike, with the same
  answer as today.
- The exit strategy object that strategies carry keeps its parameters and its
  exit check for a set of trades on a bar, now answered by `q_core`, so every
  existing caller and test of it keeps working.
- The Python sizers keep their public classes and configuration, and any sizing
  decision they make is answered by `q_core`.

### Preserved behavior

- Backtest-to-live parity passes for every golden case with and without an open
  trade, with an unchanged test body. The reference and the evaluator both use the
  `q_core` step, and the test still compares full-frame evaluation with windowed,
  bar-by-bar evaluation.
- The evaluator, recovery, worker and service tests pass with unchanged expected
  values, and the exit-rule, exit-strategy, genome exit-policy and position-sizing
  tests pass with unchanged expected values.
- A recovery test shows that a deployment restarted mid-trade and replayed to its
  last evaluated bar emits the same next decision as one that never restarted, for
  a trailing stop, a parabolic SAR stop and a time stop.
- Goldens, the registry baseline and the queued-signal baseline are unchanged.

### Cost

- The evaluator benchmark's indicator and evaluate medians, and the worker's
  full-path benchmark, are measured before and after on the same machine and
  reported. The full path stays within its documented bound of 50 ms at p95.

### Phase 2 acceptance

- A Phase 2 acceptance record is written against a named commit. It gathers the
  automated results and the measured human results of Q-026 to Q-031, states what
  Phase 2 delivered against architecture §10, and lists deferred follow-ups, each
  with the measurement that would justify it.

## Constraints and non-goals

- **No change to the evaluator's rolling window.** The `q_core` rolling window from
  Q-025 carries contracted bar columns only, while the evaluator's frames carry a
  non-contracted floating-point `volume` column that genome nodes read in research
  and in the parity test. Swapping the window would change indicator inputs, and
  architecture §10 limits this phase's evaluator change to evaluation. The window
  swap is recorded as a deferred follow-up for when a consumer needs it.
- **No fixes to quirks found along the way.** The evaluator sizes at the close
  with initial capital, labels strategy exits `exit_rule`, and matches holding
  periods by exact entry time. Each changes live decisions and needs its own
  decision.
- **The worker still never updates the evaluator's open trade after a fill.** A
  position opened while the worker runs is invisible to exit rules and strategy
  exits until the next restart. It is recorded as a finding with high priority for
  triage, and not fixed here, because fixing it changes live trading behaviour and
  deserves its own task and review.
- **No change to execution orchestration:** leases, ledger, reconciliation,
  recovery flow, risk checks, broker calls and the kill switch stay as they are, as
  architecture §3.3 requires.
- **No change to candle or tick backtests,** which Q-028 and Q-030 own.
- **No contract change,** and no change to decision persistence or API shapes.

## Acceptance criteria

### Agent-verifiable

1. Before any change, three runs of the evaluator benchmark and of the worker's
   full-path benchmark are recorded; the same measurements after the change are
   reported alongside, and the full path's p95 stays under 50 ms.
2. `test_backtest_live_parity` passes for every golden case with and without an
   open trade, with its body unchanged, and a deliberate change to one queued exit
   reason inside the evaluator's step output makes it fail.
3. A restart-and-replay test for trailing, parabolic SAR and time-stop exits emits
   the same next decision and exit reason as uninterrupted evaluation, and fails if
   replay is skipped.
4. No backend module defines a per-bar exit-rule update or decision, or a Python
   queued-signal consumer. `q_core` is imported only by the listed bridge modules.
5. `tests/execution`, `test_exit_rules.py`, `test_exit_strategy.py`,
   `test_genome_exit_policy.py`, `test_position_sizing.py`,
   `test_position_sizing_factory.py` and `test_warmup.py` pass with unchanged
   expected values, and the window bound for every exit preset equals today's.
6. Goldens, the registry baseline and the queued-signal baseline are unchanged.
7. The Phase 2 acceptance record exists, names its commit, and has an entry for
   every human-verifiable criterion of Q-026 to Q-031, each with its command and
   the figure it is compared against.
8. The new findings are in the findings document with a triage note.
9. The full validation suite passes.

### Human-verifiable

1. A paper deployment of MACrossover with a trailing stop on WIN$N M15 runs on the
   task branch through at least one entry and, after the worker is restarted once
   while the position is open, one trailing-stop exit. The decision log shows the
   exit with reason `trailing` on a bar where the recorded bars reach the trail,
   and the worker log shows no evaluation error and per-bar timings within bound.
   Command: `uv run q-execution --log-level INFO run 2>&1 | tee worker.log`
   (stop with Ctrl+C and start again while the position is open), then
   `curl -s http://127.0.0.1:8000/api/v1/execution/deployments/<id>/decisions | jq`
2. The Phase 2 acceptance record's pending entries are filled from the recorded
   human results of Q-026 to Q-031 and this task, and Phase 2 is marked accepted.
   Command: `$EDITOR docs/development/baselines/Q-031/phase-2-acceptance.md`
