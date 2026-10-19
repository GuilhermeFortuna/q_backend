# Q-120: Resting orders and stop amendments in research backtests

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 28 — Fast tick-backed backtests
**Depends on:** Q-118
**Implementation plan:** [Plan](../plans/Q-120-resting-orders-and-stop-amendments-in-research-backtests-plan.md)

## Purpose

Give `ResearchStrategy` the orders that Q-118 adds to the kernel: stop and limit entries, and moving the stop and target of an open position. A trailing stop or a break-out entry can then be decided once per closed bar and filled exactly from ticks in Rust. Today it needs a phase-aware `exit_strategy` that re-runs `compute_indicators` on every price change (Batch 28, finding 1). Hooks also become stateless, because each position carries its current levels.

## Current system

- `TradeOrder` has the actions `buy`, `sell` and `close`. Entries carry a fixed `stop_loss` and `take_profit` (Q-104) and an optional same-bar `price` (Q-106). Levels never change after entry.
- `ResearchPosition` carries side, entry time, entry price and quantity.
- `ResearchStrategyAdapter` runs strategies in one of two ways:
  - **runtime callbacks**, when a hook declares `positions` or `phase`;
  - otherwise, a loop that precomputes every bar's decision before the kernel runs.

## Behaviour

### Orders

```python
class Breakout(ResearchStrategy):
    def entry_strategy(self, frame, positions):
        if positions:
            return None
        last = frame.iloc[-1]
        return TradeOrder.buy_stop(last["high"], stop_loss=last["low"] - 5.0, expire_bars=1)

    def exit_strategy(self, frame, positions):
        if positions and positions[0].side == "long":
            new_stop = frame["low"].iloc[-1]
            if positions[0].stop_loss is None or new_stop > positions[0].stop_loss:
                return TradeOrder.amend(stop_loss=new_stop)
        return None
```

- **Order fields.** `TradeOrder` gains `kind` (`"market"` by default, `"stop"` or `"limit"`) and `expire_bars` (a positive integer or `None`, default `1`).
- **New constructors.** `TradeOrder.buy_stop`, `buy_limit`, `sell_stop` and `sell_limit` take the order price and the keyword-only arguments `stop_loss`, `take_profit` and `expire_bars`.
- **Two new actions.** `TradeOrder.amend(*, stop_loss=None, take_profit=None)` needs at least one level. `TradeOrder.cancel()` removes the working entry order.
- **Construction errors.** Construction raises `ValueError` for:
  - a stop or limit order without a price;
  - `expire_bars` on a market order;
  - an `expire_bars` value that is not a positive integer;
  - levels on the wrong side of the order price, by the existing rule for priced orders;
  - an `amend` without a level;
  - levels or a price on `cancel`.
- **Where each action is allowed.**
  - `entry_strategy` may return a `buy` or `sell` of any kind, or `cancel`.
  - `exit_strategy` may return `close` or `amend` in the bar phase and in hooks without `phase`. The `screen` and `tick` phases still allow only `close`.
  - Any other combination raises with the strategy, hook, phase and bar, as today.
- **An `amend` while flat does nothing.**
- **The kernel's rules apply as specified by Q-118:**
  - One working entry order; a new entry replaces it.
  - Fills: a stop at the first trade at or beyond its price, a limit at its price once traded strictly through.
  - `expire_bars` and the end of the session in day-trade mode.
  - Amendments take effect from the next bar.
  - At the same price: protective exit, then custom exit, then resting fill.
  - The cap runs before the wrong-side check.
- **Ticks required.** Resting orders and amendments are resolved from ticks. The first one returned without `ticks=` raises `ValueError` naming the strategy, the bar and the `ticks` argument, as entry levels already do.

### Positions

`ResearchPosition` gains `stop_loss` and `take_profit`: the levels in force at the decision boundary, after amendments, or `None` where unset. A hook can therefore trail a stop without keeping its own state.

### One execution path

Every `ResearchStrategy` runs through the kernel's runtime callbacks, and the loop that precomputed decisions is removed. Hooks without `positions` keep their existing signatures and calls. Results of existing strategies do not change.

### Results

`trades` gains:
- `order_kind` (`"market"`, `"priced"`, `"stop"`, `"limit"`);
- `entry_tick_time`: the tick time of a resting fill, `NaT` otherwise;
- `final_stop_loss` and `final_take_profit`: the levels in force when the trade closed.

`stop_loss` and `take_profit` remain the levels the trade opened with. `publish()` sends the fields it sends today. Empty results return the complete schema.

## Documentation

`docs/research-library.md`:
- **"Execution model":** a "Resting entry orders" subsection and a "Moving stops and targets" subsection, with the fill, expiry, replacement and same-price rules. Remove the statement that levels are fixed at entry.
- **"Strategy hooks and causality":** a trailing stop written with `amend` and `positions[0].stop_loss`, recommended over a phase-aware exit whenever the trigger is a price level.
- **"Backtest results":** the new columns.

`examples/research/resting_orders_backtest.py` runs a stop-entry break-out with a trailing `amend` over a store written to a temporary directory. It prints trades with `order_kind`, `entry_tick_time` and the final levels. `tests/research/test_examples.py` runs it.

## Focused acceptance

Each case uses a `TickStore` written under `tmp_path` from handwritten ticks.

1. `buy_stop`, `buy_limit`, `sell_stop` and `sell_limit` fill at the prices Q-118 specifies, with `order_kind` and `entry_tick_time` set. An expired order and a replaced order do not fill, and `cancel` removes a working order.
2. A trailing stop amended every bar closes at the first tick at or beyond the latest level, with `STOP_LOSS`, and `final_stop_loss` equals that level. `positions[0].stop_loss` shows each amendment from the next decision on.
3. **Parity.** A trailing stop written as a phase-aware exit (the stop set from the previous closed bar and triggered at `close <= stop` in the `tick` phase) and the same rule written with `amend` produce identical exit bars, exit tick times and exit prices. Only the reasons differ: `SIGNAL` and `STOP_LOSS`.
4. The invalid orders listed under *Construction errors* raise at construction. `amend` from `entry_strategy`, or in the `screen`/`tick` phase, and `cancel` from `exit_strategy`, raise with context.
5. A resting order or `amend` without `ticks` raises the specified `ValueError`.
6. Existing research, candle golden and evaluator tests pass with unchanged bodies, including strategies without `positions` that now run through runtime callbacks.
7. The example runs offline and its test passes.

Verification uses synthetic data. No gateway, database, Docker, GPU, Wine or desktop run.

## Delivery boundary

Out of scope:
- stop-limit and one-cancels-other entries, and removing a level;
- resting orders for registered strategies run by name, stack backtest jobs, optimisation, walk-forward and the forward evaluator;
- showing the new columns in the Research desktop;
- the performance work of Q-121.
