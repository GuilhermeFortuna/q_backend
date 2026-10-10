# Q-106: Priced entry orders in research backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 19 — Same-bar priced entries
**Depends on:** Q-105
**Implementation plan:** [Plan](../plans/Q-106-priced-entry-orders-in-research-backtests-plan.md)

## Purpose

Let a `ResearchStrategy` return `TradeOrder.buy(price=...)` or `TradeOrder.sell(price=...)` and have `backtest()` fill it on the bar being evaluated, at that price.

```python
class PreviousHighBreakout(ResearchStrategy):
    def entry_strategy(self, frame):
        if frame["high"].iloc[-1] > frame["high"].iloc[-2]:
            return TradeOrder.buy(price=frame["high"].iloc[-2])
        return None
```

The condition says bar `i` traded through the previous high, so a buy at that level did fill inside bar `i`. This fill model is for research only.

## Behaviour

### Orders

- `TradeOrder.buy` and `TradeOrder.sell` accept a keyword-only `price`, an optional finite positive number. `TradeOrder.close()` takes none.
- With `stop_loss` or `take_profit` set, a buy needs `stop_loss < price < take_profit` and a sell `stop_loss > price > take_profit`. Construction raises `ValueError` otherwise.
- An order without `price` behaves exactly as today: it fills at the next bar's open.

### Fill

- A priced order fills on the bar whose frame ended with the decision, at exactly `price`, charged the configured per-side cost.
- No order type is declared. The kernel (Q-105) fills at `price` when `low <= price <= high` of that bar, whether the level lies above the open (breakout) or below it (pullback).
- A price outside the bar's range raises an error naming the strategy, the bar, the price and the bar's low and high. The backtest does not continue.
- A priced order with `stop_loss` or `take_profit` resolves them from ticks that trade after the touch and therefore needs `ticks=`, as in Q-104. A priced order without levels needs no ticks.
- A rejected entry (levels on the wrong side of the price) is reported as Q-104 already reports wrong-side entries.

### Caveat to document

The hook sees the whole bar. A condition on the bar's close or low combined with a priced fill can use information that was not yet known at the fill time. The kernel checks that the price lies in the bar's range; it cannot check the strategy's causality. The guide states this and shows the previous-high example as a causal pattern.

### Unchanged

- `exit_strategy()`, `exit_params`, sizing, costs and the live forward evaluator. A deployed strategy decides on completed bars and does not reproduce same-bar fills.

## Interfaces and ownership

- `research/orders.py` owns validation; `research/adapter.py` writes the price into a signal column beside the stop and target columns, in both the batch loop and the runtime-hook path; the engine passes it to `run_candle`.
- The kernel owns the range and fill rule; the backend adds no fill logic.
- The `q-core` tag in `pyproject.toml` (`[tool.uv.sources]`) moves to the Q-105 release and `uv.lock` is refreshed.

## Acceptance criteria

Hand-computed fixtures; a mocked gateway and stores under `tmp_path`.

1. Order construction: price validation, the buy and sell level ordering, `close` rejecting a price.
2. The example strategy on a small frame fills at the previous high on the deciding bar; the ledger entry time is that bar and its price equals the level.
3. A pullback price below the open fills at the price when the low reaches it.
4. A price outside the bar's range fails the backtest with a message naming strategy, bar, price and range.
5. A priced order with a target and no `ticks=` raises the existing `ticks` error; with `ticks=` the target resolves after the touch.
6. A run with no priced orders is identical to today's output.
7. `docs/research-library.md` documents the rule, the error, the research-only note and the caveat.
8. The task's focused tests and `make check` pass.

## Implementation boundary

This issue authorizes only its listed deliverable after written-plan approval and
`./work start Q-106 --agent <agent> --worktree`.
No resting or multi-bar orders, no live-evaluator change, no contract change and no change to publishing.
