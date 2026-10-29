# Q-141: Remove same-candle priced orders

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 39 — Live-placeable research orders
**Depends on:** Q-139, and the author's confirmation that every strategy in use is ported
**Implementation plan:** [Plan](../plans/Q-141-remove-same-candle-priced-orders-plan.md)

## Purpose

Q-139 deprecates `price=` entries and closes, and priced order lists. With them gone, a research strategy cannot
act on a candle it has already seen, and every strategy file is deployable as written. This task removes them
once the author has ported and compared every strategy still in use. That way, no result is lost without a
matching replacement.

## Current system

- After Q-139:
  - priced orders backtest as before, with `PricedOrderDeprecationWarning`;
  - their replacements are OCO groups, timed exits, resting entries with expiry, and protective levels with
    `amend`;
  - `compare_results` proves equivalence, and the migration guide lists the known differences.
- The kernel's priced-entry paths and the Q-132 `touch_before_exit` rule serve priced orders. The rule also
  applies to OCO members.

## Behaviour

- `TradeOrder` refuses `price=` on `buy`, `sell` and `close` with a `ValueError` that names the replacement from
  the migration guide. Lists of priced orders are refused the same way. Resting stop and limit entries keep their
  `price` argument.
- `result.rejected_entries` keeps its columns. The reasons that only priced entries produced (`not_filled`) are
  documented as historical.
- `order_kinds` no longer produces `priced_entry` or `priced_close`, and the deploy check's refusal of them stays
  for runs stored before this task.
- The migration guide becomes the "Live-placeable orders" reference, without the deprecation notes. The research
  guide's priced-entry sections are removed, and the caution "Same-candle priced fills are a research model" goes
  with them.
- The kernel's priced-entry entry points stay in `q_core` until a later release removes them. This task changes
  only `q_backend`.

## Acceptance criteria

1. Before work starts, the board issue records the author's confirmation and the list of ported strategies with
   their `compare_results` outcomes.
2. Every `price=` entry or close, and every priced list, raises the documented `ValueError`. Resting entries,
   OCO groups, timed exits and protective levels are unaffected.
3. Q-139's equivalence fixtures keep their new-form halves, and their new-form results are unchanged.
4. Tests that exercised priced orders are removed or rewritten to the replacement forms, each one named in the
   board message with its replacement. No other existing test changes.
5. The guide no longer documents priced orders.

## Out of scope

- Removing kernel code from `q_core`.
- Changing stored runs or their `order_kinds`.
