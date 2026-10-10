# Batch 19 — Same-bar priced entries

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a kernel task in q_core.

## Outcome

A `ResearchStrategy` returns `TradeOrder.buy(price=...)` and `backtest()` opens the trade on the bar being evaluated, at that price, when the bar's range contains it. A strategy can buy at the previous bar's high when the current bar trades through it.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | An entry fills only at the next bar's open. | `q_core` `candle/run.rs` queues the decision in section D and fills it in section C of the following bar. | Q-105 |
| 2 | A research order carries no entry price. | `research/orders.py`: `TradeOrder` holds `action`, `stop_loss`, `take_profit`. | Q-106 |
| 3 | Batch 18 left entry orders out until their lifetime was decided. | Batch 18 "Recorded, not in this batch". | Q-105, Q-106 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-105 — Same-bar priced entries in the candle kernel | q_core | Q-102 | `q_core/docs/development/specs/Q-105-same-bar-priced-entries-in-the-candle-kernel-spec.md` | `q_core/docs/development/plans/Q-105-same-bar-priced-entries-in-the-candle-kernel-plan.md` |
| Q-106 — Priced entry orders in research backtests | q_backend | Q-105 | [Spec](../specs/Q-106-priced-entry-orders-in-research-backtests-spec.md) | [Plan](../plans/Q-106-priced-entry-orders-in-research-backtests-plan.md) |

Q-106 starts when Q-105 is Done; it pins the `q_core` tag that `./work finish Q-105` publishes. Batch 18 (Q-102, Q-103, Q-104) must be merged first.

## Shared decisions

- **Same bar, stated price.** The order fills on the deciding bar at exactly its price. The order type is implied by where the price lies against the open and is not declared.
- **Range is the validity test.** `low <= price <= high`; a price outside it is an error, not a skipped or substituted fill.
- **The order lives for one bar.** It is decided and filled in the same step; nothing rests.
- **Levels need ticks.** An entry's stop and target resolve from ticks after the touch and keep the Batch 18 `ticks=` requirement; a bare priced entry needs none.
- **The kernel owns the rule.** The backend supplies the price and adds no fill logic.
- **Research only.** The live evaluator decides on completed bars and cannot reproduce same-bar fills.
- **Causality is the strategy's job.** The kernel cannot detect a hook that uses information after the fill time; the guide says so.

## Recorded, not in this batch

- **Resting orders with a lifetime** (`expire_bars`, until cancelled), which need pending-order state visible to hooks.
- **Broker-side entry orders in live trading**, needing their own batch for placement, reconciliation and audit.
- **A fill-time guard** that rejects conditions on the bar's close for a priced order.

## Review and integration

Documentation is written on local `docs/batch-19-same-bar-priced-entries` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-19-same-bar-priced-entries`. No branch is pushed or merged by the authoring session.

The human integrates these branches before launching implementation tasks and owns the initial board Status and the move to Todo. Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`.
