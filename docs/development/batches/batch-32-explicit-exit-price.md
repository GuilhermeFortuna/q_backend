# Batch 32 — Explicit exit prices in research strategies

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_core (kernel) and q_backend (research layer).

## Outcome

A research strategy can close a position at a price it computed before the candle opened, provided the market traded there during the candle. The strategy no longer chooses between `phase` modes.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | Closed-bar closes fill only at the next open, and intrabar exits need a `phase` callback pair. | `crates/q-engine/src/candle/run.rs`; `research/adapter.py` | Q-127, Q-128 |
| 2 | Strategy authors must learn three exit call modes to exit intrabar. | `research/hooks.py`, `_permitted_actions` | Q-128 |
| 3 | `TradeOrder.close` cannot carry a price. | `research/orders.py` | Q-128 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-127 — Explicit exit price in the candle kernel | q_core | — | [Spec](../../../q_core/docs/development/specs/Q-127-explicit-exit-price-kernel-spec.md) | [Plan](../../../q_core/docs/development/plans/Q-127-explicit-exit-price-kernel-plan.md) |
| Q-128 — Explicit exit price and removal of phase in research hooks | q_backend | Q-127 | [Spec](../specs/Q-128-explicit-exit-price-research-spec.md) | [Plan](../plans/Q-128-explicit-exit-price-research-plan.md) |

The tasks run in sequence: Q-127 is tagged and released before Q-128 pins it.

## Shared decisions

- **Priced closes fill at traded prices.** A price is valid only if a trade of the same bar reaches it, and the fill is that trade's price.
- **Stops first.** Stop, target and breakeven fills of the bar are applied before a priced close, so the strategy's view of its positions does not change.
- **One call per bar.** An exit with `ticks` runs once per closed bar.

## Recorded, not in this batch

- Limit-style exits that rest across bars.
- Priced closes for registered strategies.
- Changes to next-open fills.

## Review and integration

Documentation is written on local `docs/batch-32-explicit-exit-price` branches, in isolated worktrees under `.worktrees/<repository>/docs-batch-32-explicit-exit-price`. The authoring session pushes or merges no branch.

The human integrates these branches before launching implementation tasks, and owns the initial board Status and the move to Todo.
