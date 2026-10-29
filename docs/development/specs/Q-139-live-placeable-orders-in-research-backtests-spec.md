# Q-139: Live-placeable orders in research backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 39 — Live-placeable research orders
**Depends on:** Q-138, Q-136
**Implementation plan:** [Plan](../plans/Q-139-live-placeable-orders-in-research-backtests-plan.md)

## Purpose

A research strategy should be deployable as written. Same-candle priced orders (`price=`) are the one construct
a broker cannot hold. They are not look-ahead in themselves, but the hook places them after seeing the candle,
so live trading cannot reproduce them. This task brings Q-138's primitives to the research library: OCO groups,
timed exits and expiry, and placement latency. It deprecates `price=`, and gives the author a comparison tool
and a migration guide. A ported strategy is then proven to backtest the same as its original before anything is
removed.

## Current system

- `TradeOrder` (`src/q_backend/research/orders.py`) offers:
  - market entries and closes;
  - `price=` on entries and closes, filled within the deciding candle;
  - resting `buy_stop`/`buy_limit`/`sell_stop`/`sell_limit` with `expire_bars`, and one working entry at a time;
  - `stop_loss`/`take_profit`, `amend` and `cancel`;
  - order lists of priced entries or priced closes.
- `backtest()` (`research/engine.py`) runs through `ResearchStrategyAdapter` and the `q_core` candle kernel. Exit
  rules (trailing, breakeven) are configured with `exit_params`.
- Q-136 records each stack run's `order_kinds` and refuses to deploy `priced_entry` and `priced_close`.
- The author's strategies (on `research/experiments`) use `price=` for:
  - brackets (`BracketMomentumStrategy`);
  - prior-bar-level entries and stop exits (`RSIStrategy`);
  - stops and time exits at the bar's open (`AlphaExperimentStrategy`).

## Behaviour

### New orders

- `TradeOrder.oco(order, order, ...)` returns an OCO group of resting entries, as Q-138 defines it. It is
  returned from `entry_strategy` like a single order.
- `TradeOrder.close(at=...)` is a timed exit. It takes a time of day (the next occurrence after the decision, in
  the frame's timezone) or a timestamp.
- Resting entries accept `expire_at=` (a time of day or a timestamp) besides `expire_bars`.
- `backtest(..., latency_ms=0)` sets the placement latency. Stack runs take it from the run request, defaulting
  to 0.
- `order_kinds` gains `oco`, `timed_exit` and `expiring_entry`. All three are deployable once Q-137 lands; until
  then, Q-136's deployable set refuses them.

### Deprecation of same-candle priced orders

- Every `price=` entry or close, and every priced order list, still backtests exactly as today. On the first such
  order of a run, it emits `PricedOrderDeprecationWarning`, naming the hook and pointing to the migration guide.
- A strategy *file* that places one is refused at deployment, as Q-136 already does. It is marked
  "not deployable" in the stack's strategy list after its first run.

### Comparison tool

- `q_backend.research.compare_results(a, b, *, tolerance=0.0)` compares two `BacktestResult`s trade by trade
  (entry and exit time, price, side, quantity, reasons and PnL). It returns a report with:
  - the matched count;
  - each differing trade pair, with its first differing field;
  - trades present in only one result;
  - total PnL, profit factor and drawdown for both.

  `report.identical` is true only when every trade matches. Printing the report gives a readable summary.

### Migration guide

- A research guide page, "Live-placeable orders", maps each priced pattern to its replacement, says when the two
  are identical, and lists the known differences:

  | Pattern | Replacement | Identical when |
  | --- | --- | --- |
  | priced entry at a level fixed before the candle | resting stop or limit placed at the previous close, `expire_bars=1` | always |
  | priced entry list (bracket) | `TradeOrder.oco(...)` of resting entries | the levels are fixed before the candle |
  | priced stop close at a level fixed before the candle | `stop_loss` set or moved by `amend` at the previous close | the stop is first set at the entry bar's close, as the legacy form cannot stop out on its entry bar |
  | priced close at the bar's open after a time | `close(at=time)` | always |
  | a condition that reads the candle's close, high or low and then prices a fill inside it | none | never: it uses information from after the fill |

- Known differences that the guide explains with examples:
  - **Strictly beyond.** A condition such as `high > prev_high` (strictly beyond) gating a stop at `prev_high`
    enters on exact touches once it is a stop order.
  - **Several stops.** Several priced stop closes, resolved by the hook's own precedence, become one stop at
    the level reached first.

### Equivalence of the author's strategies

The implementing agent proves the before/after match on real strategies without committing them, because they
are private and the repositories are public. In a scratch directory outside every repository, it ports
`BracketMomentumStrategy` (IND), `RSIStrategy` and `AlphaExperimentStrategy` (WDO) from
`experiments/strategies/` on the `research/experiments` branch. It then runs each original and its port on the
same bars and tick store with `compare_results`. The board message reports, for each strategy:
- whether the results are identical;
- otherwise, the differing trades, each attributed to a known difference from the guide.

Any unexplained difference is a defect in this task or in Q-138.

## Acceptance criteria

1. Every existing research and backtest test passes unchanged, and results for strategies without priced orders
   are byte-identical to the previous release.
2. Fixture strategies written for the tests reproduce the identical rows of the migration table through
   `backtest()`: legacy form and port give `compare_results(...).identical`. The known-difference rows produce
   exactly the documented differing trades.
3. OCO, timed exit, `expire_at` and `latency_ms` behave as Q-138 defines, through the library.
4. The deprecation warning appears once per run with priced orders, and never otherwise.
5. `order_kinds` records the new kinds, and Q-136's deployment check refuses them until Q-137.
6. `compare_results` reports matches, field differences and one-sided trades correctly on constructed results.
7. The board message contains the three comparisons described above.

## Out of scope

- Removing `price=` (Q-141).
- Decision timeframes (Q-140).
- Live submission of the new orders (Q-137).
- Committing the author's strategies or their ports anywhere.
