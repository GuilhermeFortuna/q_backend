# Q-128: Explicit exit price and removal of phase in research hooks

**Status:** written spec and plan awaiting human review; status of record is the [Q project board](https://github.com/users/GuilhermeFortuna/projects/2).
**Batch:** 32 — Explicit exit prices in research strategies
**Depends on:** Q-127 (q_core release that accepts `exit_price` and no longer has the screen and tick callbacks)
**Implementation plan:** [Plan](../plans/Q-128-explicit-exit-price-research-plan.md)

## Purpose

Strategies choose between three exit call modes (`phase="screen"`, `"tick"`, `"bar"`), and they must know which mode fills where. This task removes `phase`. An exit that declares `ticks` runs once per closed bar, and it can close at a price it computed before the candle opened, provided the market traded there during the candle:

```python
def exit_strategy(self, frame, positions=(), *, ticks=None):
    level = positions[0].entry_price - 2.0
    if ticks is not None and frame["low"].iloc[-1] <= level <= frame["high"].iloc[-1]:
        return TradeOrder.close(price=level)
    return None
```

The engine still detects intrabar mode from the `ticks` parameter in the signature.

## Current system

- `detect_hook_call` (`research/hooks.py`) reads `phase`. `ResearchStrategyAdapter` runs the screen and tick callbacks (`research/adapter.py`, `_RuntimeHooks._screen` and `_tick`) and builds `FormingRow` histories (`research/tick_phase.py`).
- `TradeOrder.close()` carries no price (`research/orders.py`).
- `TickData` has a tick-phase mode that resolves its cutoff from a trade row (`research/tick_data.py`).
- The kernel's runtime callbacks are Q-127's subject; after Q-127 they receive no screen or tick callbacks.

## Behaviour

### Phase removed

- `exit_strategy` no longer takes `phase`. A hook that declares a `phase` parameter raises `TypeError` when the adapter is built, naming the parameter and saying it was removed. Silent acceptance would change behaviour without warning.
- Each `exit_strategy` is called once per closed bar with `ticks` cut off at the bar's end, if it declares `ticks`. Without `ticks` it behaves as today's bar call.
- `compute_indicators` and `entry_strategy` keep the `ticks` rules from Q-126.
- No screen, tick or forming-row path remains. `tick_phase.py` keeps only `resolve_workers`.

### Priced close

- `TradeOrder.close(price=None)` accepts a finite positive price. A priced close is valid only on an exit decision. Other order kinds still carry no price.
- The adapter passes a priced close to the kernel as `exit_price` (Q-127). The kernel fills at the first trade of the bar that reaches the price, or does nothing when none does.
- A priced close requires ticks: `backtest()` without `ticks=` raises `ValueError` naming the hook, as for amend.
- A value outside the bar's `[low, high]` raises `ValueError` naming the bar and the price, from the kernel's validation.
- Closes without a price fill at the next open, as before.

### TickData

- The `tick` argument, the trade-row cutoff and the screen cutoff are removed. The cutoff is the bar's end for every hook call.
- `bar()`, `between()` and `cutoff` keep their Q-126 behaviour for that cutoff.

## Documentation

- `docs/research-library.md`: replace "Entry levels and phase-aware exits" with a section on the priced close and the removed phase. Update the "Tick data in hooks" table and examples. Replace the phase-based `TickStopExit` example.
- `research/strategy.py` docstrings for `exit_strategy` and the `ticks` parameter.
- Any repository-owned strategy that declares `phase` (checked with `git grep -n "phase" -- experiments src examples`).

## Focused acceptance

1. An exit with a `phase` parameter raises `TypeError` naming `phase`.
2. An exit with `ticks` is called once per bar, with `ticks.cutoff` equal to the bar's end.
3. `TradeOrder.close(price=P)` with P inside the bar's range fills at the first trade reaching P, with that trade's price and time.
4. P outside the bar's range raises `ValueError` naming the bar and the price.
5. A priced close without `ticks=` raises `ValueError` naming the hook.
6. A stop that fills earlier in the bar closes the position, and the priced close then does nothing.
7. A priced close that no trade reaches keeps the position open.
8. `close()` without a price still fills at the next open.
9. All research tests pass after the phase-based tests are rewritten. Registered strategies, goldens and backtesting tests do not change.

## Delivery boundary

Out of scope:
- q_core changes (Q-127);
- ticks for registered strategies, stack jobs, optimisation and walk-forward;
- limit-style exits that rest across bars.

Compatibility: existing strategies that declare `phase` stop working until migrated. The `TypeError` names the parameter.
