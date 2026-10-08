# Q-104: Stop and target orders in research backtests

**Status:** written spec awaiting human review; the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2) is the status of record.
**Batch:** 18 — Stop and target orders in research backtests
**Depends on:** Q-102, Q-103
**Implementation plan:** [Plan](../plans/Q-104-stop-and-target-orders-in-research-backtests-plan.md)

## Purpose

A `ResearchStrategy` can open a position and ask to close it, and nothing else. Price exits exist only as `exit_params` rules that apply to every trade, are evaluated on completed bars and fill at the next bar's open, so the exit price differs from the level.

After this task an entry order carries its own stop and target price, and `backtest()` fills them inside the bar where they trade, using the ticks in a `TickStore`. This fill model is for research backtests only.

## Behaviour

### Orders

```python
class Breakout(ResearchStrategy):
    def entry_strategy(self, frame):
        last = frame.iloc[-1]
        if last["close"] > last["upper"]:
            return TradeOrder.buy(
                stop_loss=last["close"] - 2 * last["atr"],
                take_profit=last["close"] + 4 * last["atr"],
            )
        return None
```

- `TradeOrder.buy` and `TradeOrder.sell` accept keyword-only `stop_loss` and `take_profit`, both optional price levels. `TradeOrder.close()` takes none.
- Construction raises `ValueError` for a level that is not a finite positive number, for a buy whose stop is not below its target, for a sell whose stop is not above its target, and for a close with a level.
- An order without levels behaves exactly as today.

### Running

```python
store = TickStore("WDO$N")
frame = store.bars("M10", start="2025-10-01")
result = backtest(frame, strategy=Breakout(), symbol="WDO$N", ticks=store, point_value=10.0)
```

- `backtest()` gains `ticks: TickStore | None = None`.
- The first entry order that carries a level without `ticks` raises `ValueError` naming the strategy, the bar and the `ticks` argument. `ticks.symbol` must equal `symbol`.
- `ticks` is not read unless an open trade's level lies within a bar's range. A strategy that sets no level produces the same result with and without it.
- The frame must be built from the same store with `TickStore.bars`. When the engine loads a bar's prices it checks that their first, highest, lowest and last equal the bar's open, high, low and close, and raises `ValueError` naming the bar and `TickStore.bars` when they do not.
- A bar whose prices are needed but not stored raises `NoMarketDataError` naming the session day and `TickStore.sync`. There is no bar-only fallback.

### Fills

The rule is the candle kernel's (Q-102) and is restated in the guide:

- The entry fills at the next bar's open, as today. If a level is already on the wrong side of that fill price, the entry is not taken.
- A stop triggers on the first traded price at or beyond its level and fills at that price, so a gap through the level fills at the gap price.
- A target triggers on the first traded price strictly beyond its level and fills at the level.
- When both levels lie inside one bar, the order in which prices traded decides.
- Levels are fixed at entry. `exit_strategy`, `exit_params` rules, day-trade closes and `force_close_at_end` keep working alongside them and fill at a bar's open or close as today.
- Costs are charged per side as configured, on protective fills too.

### Results

- `trades["exit_reason"]` is `STOP_LOSS` or `TAKE_PROFIT` for a protective fill.
- `trades` gains `exit_tick_time`, the time of the traded price that filled the order, and `NaT` for every other exit. `exit_time` stays the timestamp of the bar in which the trade closed, so `equity` and every consumer of `trades` keep working.
- `trades` gains `stop_loss` and `take_profit`, the levels the trade was opened with, `NaN` when unset.
- `BacktestResult` gains `rejected_entries`, a frame with the bar time, side, fill price and both levels of each entry that was not taken. It is empty when there are none.

### Research only

- Only `q_backend.research.backtest` passes levels and a price source to the kernel. Stack backtest jobs, optimisation, walk-forward and the forward evaluator call it as before and their results do not change.
- `check_engine` requires a `q_core` release with protective orders, and the `q_core` pin moves to the tag that contains Q-102.

## Documentation

`docs/research-library.md`:

- "Strategy hooks and causality": the two keyword arguments, with the example above.
- "Execution model": a "Stop and target orders" subsection with the fill rule, the wrong-side rule, the requirement to build the frame from the store, the history limit that follows from it, and the statement that live trading still decides on completed bars, so these fills are not reproduced by a deployed strategy.
- "Transaction costs": protective fills are traded prices and carry the configured per-side cost; the half-spread term stays in the cost, and a target, which is a resting order, is charged it as well.
- "Backtest results": the new columns and `rejected_entries`.

`examples/research/stop_target_backtest.py` runs a strategy with both levels over a store and prints metrics, the exit-reason counts and the rejected entries.

## Focused acceptance

Each case uses a `TickStore` written under `tmp_path` from handwritten ticks.

1. A long whose stop trades first inside a bar closes at the traded price with `STOP_LOSS`, and `exit_tick_time` is that tick's time; the mirrored short does the same.
2. A bar containing both levels resolves by trade order, in both orders.
3. A price that only touches the target does not fill; a gap through the stop fills at the bar's open.
4. An entry whose stop is on the wrong side of its fill is absent from `trades` and present in `rejected_entries`.
5. A level without `ticks`, a store for another symbol, a frame that does not match the store and a bar without stored ticks each raise the error the spec names.
6. A strategy without levels returns identical `trades`, `metrics`, `equity` and `data` with and without `ticks`, and the store reads no file.
7. A counting store shows that only bars whose range reaches a level are read.
8. `equity` includes the profit of a protective exit on the bar in which it closed.
9. Invalid `TradeOrder` levels raise at construction.
10. Existing research, candle golden and evaluator tests pass with unchanged bodies.

Verification uses synthetic data. No gateway, database, Docker, GPU, Wine or desktop run.

## Delivery boundary

- No stop or limit entry orders, no change to a level after entry, no position state in hooks.
- No change to `exit_params` rules or their next-open fill, the forward evaluator, execution orders or any contract.
- `publish()` keeps sending the fields it sends today; showing the new columns in the Research desktop is not part of this task.
- Registered strategies run by name do not gain levels.
