# Batch 28 — Fast tick-backed backtests

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with two kernel tasks in q_core.

## Outcome

A research backtest that combines candles and ticks makes one Python decision per closed bar. Everything inside a bar is resolved in Rust from trade prices, with no loss of fill fidelity:
- stop and target levels, including levels moved at bar close;
- stop and limit entries;
- the reads of the ticks themselves.

Results do not depend on worker count, and day-trade sessions can run in parallel.

## Findings this batch answers

The evidence comes from a profile on 2026-10-19: `WDO$N` M5, 1,253 bars (2026-10-01 to 10-16), with a trailing-stop strategy written as a phase-aware exit, `workers=20`. The run took 68 s; the tick-phase decisions took 63.6 s of it, and the bar-phase hooks 3.7 s.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A stop that moves at bar close can only be written as a phase-aware Python exit. That exit rebuilds `compute_indicators` over the whole history at every tick that changes the price. | `TickEvaluator._frame` (`research/tick_phase.py`). 1,789 rebuilds of 0.86 s each for 3 trades. The kernel fixes levels at entry (`Levels` in `q-engine/src/candle/protective.rs`). | Q-118, Q-120 |
| 2 | Tick decisions spread over processes waste work and depend on process-local state. | Waves of `16 × workers` ticks: 1,469 decisions with 20 workers against 214 with 1. On 10/01–10/10 the 10/09 trade exits at 12:20 @ 5002.5 with 20 workers and at 12:05 @ 5005 with 1. | Q-121 |
| 3 | Each bar that needs ticks costs a Python call that reads, filters and checks them. | `PyIntrabar` in `q-py/src/engine.rs` calls `TickReplay.__call__`. More bars need ticks once resting orders exist. | Q-119, Q-121 |
| 4 | Every hook call copies the whole history, so cost per bar grows with the history. | `_owned_prefix` (`research/adapter.py`) runs two or three times per bar. | Q-121 |
| 5 | An entry that is both capped and on the wrong side is reported as rejected. | Known deviation recorded in the Q-104 plan. | Q-118 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-118 — Resting orders and stop amendments in the candle kernel | q_core | — | `q_core/docs/development/specs/Q-118-resting-orders-and-stop-amendments-in-the-candle-kernel-spec.md` | `q_core/docs/development/plans/Q-118-resting-orders-and-stop-amendments-in-the-candle-kernel-plan.md` |
| Q-119 — Native session tick source for the candle kernel | q_core | — | `q_core/docs/development/specs/Q-119-native-session-tick-source-for-the-candle-kernel-spec.md` | `q_core/docs/development/plans/Q-119-native-session-tick-source-for-the-candle-kernel-plan.md` |
| Q-120 — Resting orders and stop amendments in research backtests | q_backend | Q-118 | [Spec](../specs/Q-120-resting-orders-and-stop-amendments-in-research-backtests-spec.md) | [Plan](../plans/Q-120-resting-orders-and-stop-amendments-in-research-backtests-plan.md) |
| Q-121 — Fast tick-backed research backtests | q_backend | Q-119, Q-120 | [Spec](../specs/Q-121-fast-tick-backed-research-backtests-spec.md) | [Plan](../plans/Q-121-fast-tick-backed-research-backtests-plan.md) |

- Q-118 and Q-119 are independent and can run in parallel. Each is released by `./work finish`.
- Q-120 pins the Q-118 tag. Q-121 pins the Q-119 tag (or a later one).

## Shared decisions

- **Decide on closed bars, execute on ticks.** Python decides once per bar, and Rust resolves everything inside the bar from trade prices, as a strategy's resting orders would be matched at the exchange.
- **One scan per bar.** Protective exits, the custom tick exit and a resting fill are resolved in one chronological walk. At the same price they apply in that order.
- **Existing fill rules.** A stop fills at the first trade at or beyond its level, a limit or target at its level once traded strictly through. Gaps fill at the traded price. A trade never exits on its own entry price.
- **Stateless hooks.** Positions carry their current levels, so hooks need no instance state. Parallel sessions rely on this.
- **No silent fallback.** Missing or inconsistent ticks still fail the run with the current errors.
- **Identical results.** Every existing strategy keeps its results, with two deliberate exceptions:
  - Q-118 stops reporting capped entries as rejected;
  - Q-121 leaves indicator columns `NaN` on the forming row of a tick-phase frame.

## Recorded, not in this batch

- Stop-limit and one-cancels-other (straddle) entries; more than one working order; removing a level.
- A vectorised order API that lets strategies with no position state skip per-bar hooks.
- Streaming forming-candle indicators from `q-indicators` for tick-phase hooks.
- Aggregating bars from ticks in Rust; bid/ask fill models.
- Resting orders in stack backtest jobs, optimisation, walk-forward and the forward evaluator.

## Review and integration

Documentation is written on local `docs/batch-28-fast-tick-backtests` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-28-fast-tick-backtests`. No branch is pushed or merged by the authoring session.

The human integrates these branches before launching implementation tasks and owns the initial board Status and the move to Todo. Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`.
