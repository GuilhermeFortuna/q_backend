# Batch 39 — Live-placeable research orders

**Status:** issues published on 2026-10-29; written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a kernel task in q_core.

## Outcome

Research strategies decide on completed bars and act through orders a broker can hold:
- market orders;
- resting stop and limit entries, with OCO groups and expiry;
- protective levels and `amend`;
- timed exits;
- trailing and breakeven stops.

The simulated broker fills them against ticks, as MT5 fills them live, so every strategy file deploys as written.
A strategy can also decide on M1 with indicators from completed coarser bars. Same-candle priced orders
(`price=`) are deprecated, and removed only after every strategy in use is ported and shown to backtest the same.

## Findings this batch answers

Each was read from the code on 2026-10-29.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | Same-candle priced orders cannot be held by a broker. | A `price=` entry or close fills inside the candle that decided it (`research/orders.py`; `execution.rst`: "Same-candle priced fills are a research model; live deployment does not reproduce them"). Q-136 refuses to deploy them. | Q-139, Q-141 |
| 2 | Some priced patterns have no live-placeable equivalent. | A priced entry list (bracket) needs OCO. A close at a bar's open after a time needs a timed exit. Resting entries expire only by bar count, and one entry works at a time (`crates/q-engine/src/candle/`). | Q-138, Q-139 |
| 3 | The author's strategies rely on these patterns. | On `research/experiments`, in `experiments/strategies/`: `BracketMomentumStrategy` (priced bracket), `RSIStrategy` (prior-bar priced entries and stop closes), `AlphaExperimentStrategy` (priced stops and time exits at the open). | Q-139 |
| 4 | Sub-bar decisions need a coarse-bar context that cannot look ahead. | `backtest()` decides once per frame bar; nothing resamples coarser bars from completed bars only. | Q-140 |
| 5 | Live execution must carry the new orders. | Q-137 maps protective levels and resting entries only. | Q-137 (revised) |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| [Q-138](https://github.com/GuilhermeFortuna/q_core/issues/22) — Live-placeable orders in the candle kernel | q_core | none | `q_core/docs/development/specs/Q-138-live-placeable-orders-in-the-candle-kernel-spec.md` | `q_core/docs/development/plans/Q-138-live-placeable-orders-in-the-candle-kernel-plan.md` |
| [Q-139](https://github.com/GuilhermeFortuna/q_backend/issues/62) — Live-placeable orders in research backtests | q_backend | Q-138, Q-136 | [Spec](../specs/Q-139-live-placeable-orders-in-research-backtests-spec.md) | [Plan](../plans/Q-139-live-placeable-orders-in-research-backtests-plan.md) |
| [Q-140](https://github.com/GuilhermeFortuna/q_backend/issues/63) — Finer decision timeframes for research strategies | q_backend | Q-136 | [Spec](../specs/Q-140-finer-decision-timeframes-for-research-strategies-spec.md) | [Plan](../plans/Q-140-finer-decision-timeframes-for-research-strategies-plan.md) |
| [Q-141](https://github.com/GuilhermeFortuna/q_backend/issues/64) — Remove same-candle priced orders | q_backend | Q-139, author confirmation | [Spec](../specs/Q-141-remove-same-candle-priced-orders-spec.md) | [Plan](../plans/Q-141-remove-same-candle-priced-orders-plan.md) |
| [Q-137](https://github.com/GuilhermeFortuna/q_backend/issues/61) — Stop, limit and protective orders on MT5 (Batch 38, revised) | q_backend | Q-136, Q-139 | [Spec](../specs/Q-137-stop-limit-and-protective-orders-on-mt5-spec.md) | [Plan](../plans/Q-137-stop-limit-and-protective-orders-on-mt5-plan.md) |

Suggested order: Q-135 and Q-136 (Batch 38), then Q-138, Q-139, Q-137 and Q-140, then Q-141 once the author has
ported their strategies. Q-138 is released as a `q_core` tag before Q-139 pins it. Q-139 and Q-137 both
change the order-kind classification, so serialize them.

## Shared decisions

- **Backtests must match before and after.** No existing order type changes its fills. Every existing kernel
  golden and research test passes unchanged, and runs without new arguments are byte-identical.
- **Equivalence is proven, not assumed.**
  - Q-138 has kernel gates pairing each legacy priced pattern with its replacement on the same ticks, and
    requiring identical ledgers.
  - Q-139 repeats that through `backtest()` with `compare_results`, and runs the author's three strategies,
    original against port, outside the repositories.
  - Q-140 shows that an M5 strategy and its M1-decision version trade identically.
- **Differences only where the original used the candle.** Where a port cannot be identical, the guide names the
  reason. The two known cases are a strictly-beyond touch condition and several self-resolved stops. A pattern
  that reads the candle's close, high or low and then prices a fill inside it has no replacement, because it
  uses information from after the fill.
- **Deprecate, port, then remove.** Priced orders keep working, with a warning, until the author confirms on the
  Q-141 issue that every strategy in use is ported and compared.
- **Placement latency defaults to 0**, so it cannot shift any result unless set.
- **Private strategies stay private.** The author's strategies and their ports are never committed. The board
  messages carry only the comparison reports.

## Recorded, not in this batch

- Removing the priced-entry paths from `q_core`, after Q-141.
- Decisions on ticks (tier B).

## Review and integration

Documentation is committed on local `docs/batch-39-live-placeable-orders` branches of `q_core` and `q_backend`, in
`.worktrees/<repository>/docs-batch-39-live-placeable-orders`. The complete specification and plan text is embedded
in each issue.
