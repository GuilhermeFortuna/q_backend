# Batch 18 — Stop and target orders in research backtests

**Status:** issues published on 2026-10-08; written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_backend, with a kernel task in q_core.

## Outcome

A `ResearchStrategy` returns `TradeOrder.buy(stop_loss=..., take_profit=...)`, and `backtest()` closes the trade inside the bar where the level trades, at the price the stored ticks show. The engine walks bars and reads a bar's ticks only when the bar's range reaches a level of an open trade.

## Findings this batch answers

Each was read from the code on 2026-10-08.

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A research order has no price levels. | `research/orders.py`: `TradeOrder` holds only `action` (`buy`, `sell`, `close`). | Q-104 |
| 2 | Price exits fill at the next bar's open. | `q_core` `candle/run.rs` closes every queued exit at `fill_price` (the bar's open); Q-094 documents that the exit price differs from the level. | Q-102 |
| 3 | The tick kernel cannot run a candle strategy. | `q_core` `tick/simulate.rs` takes a `direction` value per tick and triggers on bid and ask for every tick of the stream. | Q-102 |
| 4 | Fetched ticks are not kept. | `research/data.py` `load_ticks` calls the gateway with `use_cache=False` on every call; the only per-session copy is an untracked experiment script. | Q-103 |
| 5 | A year of ticks does not fit a backtest's memory comfortably. | 258 stored `WDO$N` sessions hold 71.4 million rows (about 277,000 per session, 266 MB compressed). | Q-102, Q-103 |
| 6 | Tick history is short and MT5 hides gaps. | `WDO$N` ticks start on 2025-10-01; MT5 answers with success and no rows for history it has not synchronised (research guide, "History depth and completeness"). | Q-103 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| [Q-102](https://github.com/GuilhermeFortuna/q_core/issues/14) — Intrabar stop and target orders in the candle kernel | q_core | none | `q_core/docs/development/specs/Q-102-intrabar-stop-and-target-orders-in-the-candle-kernel-spec.md` | `q_core/docs/development/plans/Q-102-intrabar-stop-and-target-orders-in-the-candle-kernel-plan.md` |
| [Q-103](https://github.com/GuilhermeFortuna/q_backend/issues/38) — Research tick store | q_backend | none | [Spec](../specs/Q-103-research-tick-store-spec.md) | [Plan](../plans/Q-103-research-tick-store-plan.md) |
| [Q-104](https://github.com/GuilhermeFortuna/q_backend/issues/39) — Stop and target orders in research backtests | q_backend | Q-102, Q-103 | [Spec](../specs/Q-104-stop-and-target-orders-in-research-backtests-spec.md) | [Plan](../plans/Q-104-stop-and-target-orders-in-research-backtests-plan.md) |

Q-102 and Q-103 have no prerequisite and can run in parallel. Q-104 starts when both are Done; it pins the `q_core` tag that `./work finish Q-102` publishes.

Q-103 and Q-104 both change `research/__init__.py`, `research/data.py` and `docs/research-library.md`; running them in order keeps each branch free of overlapping edits.

## Shared decisions

- **Screen on bars, resolve on ticks.** A bar's high and low decide whether its ticks are read. Most bars are never loaded.
- **One price basis.** Bars are built from the same stored ticks the engine later walks, so the screen cannot disagree with the fill. A frame from `load_bars` is rejected when it does not match the store.
- **Traded prices, not quotes.** A stop triggers on a traded price and fills at that price; a target fills at its level once price trades through it. Bid and ask are not used, so the half-spread stays in `TransactionCostConfig` for every fill and is not counted twice.
- **No bar-only fallback.** A backtest that uses levels covers only the sessions in the tick store. Missing ticks are an error, not a weaker rule.
- **Price levels.** Orders carry absolute prices. An entry whose level is already on the wrong side of its fill price is not taken and is reported.
- **Levels are fixed at entry.** Hooks stay stateless.
- **Research only.** The live forward evaluator decides on completed bars and places no protective orders, so a deployed strategy would not reproduce these fills. Only `q_backend.research.backtest` can request them.
- **A file store, not the lake catalog.** The research library runs without a database (Q-089), so the tick store is plain files under a configurable root.
- **The kernel owns the rule.** Simulation semantics stay in `q_core`; the backend supplies prices and adds no fill logic.
- **Focused verification.** Hand-computed fixtures, a mocked gateway and stores under `tmp_path`. No gateway, Docker, GPU, Wine or desktop run for automated checks.

## Recorded, not in this batch

- **Broker-side protective orders in live trading.** The execution order payload already carries `sl` and `tp`; the evaluator does not set them. Doing so is what would let a deployed strategy use this fill model, and it needs its own batch covering order placement, reconciliation and paper-trading audit.
- **Stop and limit entry orders.** The same screen-and-resolve step can fill a pending entry. It needs an order lifetime rule first (how long an unfilled order rests).
- **Changing a level after entry.** Trailing a stop from a hook needs position state in hooks, which they do not have. Bar-based trailing remains available through `exit_params`.
- **Queue position.** Requiring price to trade through a target is the conservative stand-in for not knowing the order's place in the queue.
- **Unifying with the lake catalog.** The stack stores ticks through the database-backed catalog; the research store is separate. Serving one from the other is a later decision.
- **Hook speed.** A custom `ResearchStrategy` took 57.5 s on 70,000 bars (Batch 16). Tick resolution adds little to that; the per-bar prefix copies remain the cost.
- **Showing the new columns in the Research desktop.** `publish()` is unchanged by this batch.

## Operator steps

- After Q-103 merges, sync each symbol of interest from the start of broker tick history (`WDO$N`: 2025-10-01) and repeat regularly. Whether the broker keeps old sessions indefinitely is not known, so the store is the copy of record.
- The existing experiment cache under `data/experiments/wdo_ticks/` has the same columns per day and can be compared against a fresh sync; it is not imported by any task.

## Review and integration

Documentation is written on local `docs/batch-18-stop-and-target-orders` branches in isolated worktrees under `.worktrees/<repository>/docs-batch-18-stop-and-target-orders`. No branch is pushed or merged by the authoring session.

The human integrates these documentation branches before launching implementation tasks, and owns the initial board Status and the move to Todo. No product code is changed by the authoring session.

Launch an approved task whose dependencies are Done from the workspace root with `./work start Q-NNN --agent <agent> --worktree`.
