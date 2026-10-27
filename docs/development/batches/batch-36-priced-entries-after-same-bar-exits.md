# Batch 36 — Priced entries after same-bar exits

**Status:** written task specs and plans await human review. The [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Owner:** q_core (kernel) and q_backend (research layer).

## Outcome

A priced entry can no longer fill at a price that traded while a position closed later on the same candle was
still open. Research backtests report why each entry was refused.

## Findings this batch answers

| # | Finding | Evidence | Task |
| --- | --- | --- | --- |
| 1 | A priced entry's touch is found from the candle's first trade, even when a trade closed later on that candle. | `crates/q-engine/src/candle/run.rs`, `resolve_entry_levels` | Q-132 |
| 2 | A strategy that only enters when flat takes those earlier touches, because hooks see positions after the candle's intrabar exits. On an `IND$N` M5 bracket strategy, 3,765 of 9,090 trades had this kind of entry and made +192,312, while the rest lost 6,608. | `experiments/strategies/ind_strategies.py` (`BracketMomentumStrategy`) | Q-132, Q-133 |
| 3 | `rejected_entries` has no reason column. | `research/results.py` | Q-132, Q-133 |

## Tasks and delivery order

| Task | Repository | Depends on | Specification | Implementation plan |
| --- | --- | --- | --- | --- |
| Q-132 — Priced entries after same-bar exits in the candle kernel | q_core | — | `q_core/docs/development/specs/Q-132-priced-entries-after-same-bar-exits-spec.md` | `q_core/docs/development/plans/Q-132-priced-entries-after-same-bar-exits-plan.md` |
| Q-133 — Priced entries after same-bar exits in research backtests | q_backend | Q-132 | [Spec](../specs/Q-133-priced-entries-after-same-bar-exits-research-spec.md) | [Plan](../plans/Q-133-priced-entries-after-same-bar-exits-research-plan.md) |

The tasks run in sequence: Q-132 is tagged and released before Q-133 pins it.

## Shared decisions

- **The touch is searched again after the exit.** It is not simply refused. On a candle with an intrabar exit,
  the touch search starts at the last exit's tick, and an entry with no reaching trade after it is refused,
  not an error.
- **The exit's own print counts.** This matches resting fills, which already share a print with exits.
- **Unchanged elsewhere.** Candles with no intrabar exit keep every output.

## Recorded, not in this batch

- A native one-cancels-other resting bracket, which would let bracket strategies avoid priced entries.
- Filling a stop-style priced entry at the first trade beyond its level instead of at the level.

## Review and integration

Documentation is written on local `docs/batch-36-priced-entries-after-same-bar-exits` branches, in isolated
worktrees under `.worktrees/<repository>/docs-batch-36-priced-entries-after-same-bar-exits`. The authoring session
pushes or merges no branch.

The human integrates these branches before launching implementation tasks, and owns the initial board Status and
the move to Todo.
