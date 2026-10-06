# Q-094: Exit rules state their trigger and fill

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 16 — Research data and backtest correctness
**Depends on:** none
**Implementation plan:** [Plan](../plans/Q-094-exit-rules-state-their-trigger-and-fill-plan.md)

## Purpose

The exit-rule catalog tells users what a rule watches but not when it acts. "Fixed Stop Loss" reads "Exit when price moves against the position by a fixed percentage from entry", which a reader takes to mean an exit at the stop level. The engine does something else, by design:

- every exit rule is evaluated once per completed bar;
- price-level rules trigger on that bar's high or low;
- a triggered rule closes the position at the next bar's open.

On a synthetic series a long with a 2% stop exited at +0.6%: the bar's low pierced the level, the bar closed above it and the next bar opened higher. On 10-minute bars the gap between a stop level and its fill is routinely several ticks in either direction.

This is the specified behaviour of the candle kernel (`q_core` Q-027: rule exits queue on the closed bar and fill at the next bar's price) and it equals live execution, where the forward evaluator decides on completed bars with the same decision step. Filling stops at their level in backtests only would break the platform rule that backtests and live trading produce identical results. The defect is that users are not told. This task makes the catalog say it and pins the behaviour with tests so the text cannot drift from the engine.

## Requirements

### Catalog text

- Each rule in `src/q_backend/backtesting/exit_rules/` states its trigger basis and its fill in its `description`:
  - stop rules (`fixed_sl`, `atr_sl`, `trailing`, `chandelier`, `breakeven`, `psar`, `profit_target_ratchet`, `donchian_stop`): a completed bar's low for a long position and its high for a short position;
  - target rules (`fixed_tp`, `atr_tp`): a completed bar's high for a long position and its low for a short position;
  - `time_stop`: the number of completed bars in the trade.
- Every description ends with one shared sentence, defined once in the package and reused, that says the position closes at the next bar's open and that the exit price can differ from the level. One wording for all rules; no per-rule variants of that sentence.
- Parameter hints describe a level or a period and stay as they are, except a hint that describes exit timing (the Donchian period hint does), which uses wording consistent with the description. Labels, ids, parameter names, defaults, ranges and groups do not change.
- The `/api/v1/exit-rules` response keeps its shape; only strings change. No contract or frontend change is required.

### Pinned behaviour

Focused engine-level tests on small synthetic frames pin what the text claims:

1. A long with a fixed percentage stop: a bar whose low pierces the level and whose close recovers above it triggers the rule; the trade closes at the next bar's open with reason `fixed_sl`, at a price above the stop level.
2. A long with a fixed percentage target: a bar whose high reaches the level triggers the rule; the trade closes at the next bar's open with reason `fixed_tp`, at a price different from the level.
3. The mirrored short cases for 1 and 2.
4. A rule can trigger on the bar whose open filled the entry.
5. `max_bars_in_trade = N` closes at the open of the Nth bar after the entry bar with reason `time_stop`.
6. A catalog test asserts every listed rule's description ends with the shared sentence.

Cases already covered by an existing test or golden are cited rather than duplicated.

## Focused acceptance

- The six pinned behaviours pass and existing exit-rule, golden and research backtest suites are unchanged in outcome.
- `GET /api/v1/exit-rules` returns the new descriptions through the existing route test or a focused addition to it.
- Verification uses synthetic frames only. No kernel change, `q_core` release, Wine, GPU, Docker or desktop run.

## Delivery boundary

- No change to fill semantics, the candle kernel, exit-rule parameters or the forward evaluator.
- The research guide's execution section is written in Q-096, which depends on this task, so that guide edits in this batch do not overlap.
- `q_frontend` keeps its own copy of the old strings in `src/mocks/data.ts` for offline development; that mock is not updated here.
- A fill model in which a stop fills at its level belongs with broker-side protective orders, so that live and backtest change together. It is outside this batch.
